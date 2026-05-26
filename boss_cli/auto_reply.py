"""Auto-reply helpers: template pool, daily quota, work-hours gate, audit log.

Used by `boss recruiter auto-reply` (CLI) and the MCP server. The actual API
calls stay in BossClient — this module only orchestrates which friends are
eligible, picks templates, and enforces local rate limits.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path

from .client import BossClient

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "boss-cli"
TEMPLATES_PATH = CONFIG_DIR / "templates.txt"
QUOTA_PATH = CONFIG_DIR / "auto_reply_quota.json"
AUDIT_PATH = CONFIG_DIR / "auto_reply_audit.jsonl"

DEFAULT_TEMPLATES = [
    "你好，方便发一份最新的简历吗？",
    "你好，看到你的简历对我们职位比较匹配，可以先发一份完整简历过来吗？",
    "你好，麻烦发份简历，方便我们进一步沟通～",
    "Hi，可以先把简历发我看看吗？谢谢！",
    "你好，先看下简历哈，麻烦发一下～",
]

# Inter-send delay range (seconds). Floor is what really matters for risk.
SEND_DELAY_MIN = 12.0
SEND_DELAY_MAX = 30.0

# Typing-speed model: roughly N chars/sec, plus jitter.
TYPING_CHARS_PER_SEC = 6.0
TYPING_JITTER = 1.5

# Work-hours gate (local time). Outside this range auto-reply refuses to run.
WORK_HOURS = [
    (dtime(9, 30), dtime(12, 0)),
    (dtime(14, 0), dtime(19, 0)),
]

DEFAULT_DAILY_QUOTA = 80


# ── Templates ───────────────────────────────────────────────────────


def load_templates() -> list[str]:
    """Load reply templates from ~/.config/boss-cli/templates.txt (one per line).

    Lines starting with # are skipped; blank lines too. Falls back to a small
    built-in pool if the file does not exist.
    """
    if not TEMPLATES_PATH.exists():
        return list(DEFAULT_TEMPLATES)
    out: list[str] = []
    for line in TEMPLATES_PATH.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out or list(DEFAULT_TEMPLATES)


def save_templates(templates: list[str]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    body = "\n".join(templates) + "\n"
    TEMPLATES_PATH.write_text(body, encoding="utf-8")


def pick_template(templates: list[str], candidate_name: str = "") -> str:
    """Pick one template and lightly personalize it.

    Slot fill: leading "{name}你好" or trailing "～{name}" — kept simple to
    avoid LLM-style content fingerprints.
    """
    body = random.choice(templates)
    if "{name}" in body and candidate_name:
        body = body.replace("{name}", candidate_name)
    elif "{name}" in body:
        body = body.replace("{name}", "")
    return body


# ── Daily quota ─────────────────────────────────────────────────────


@dataclass
class DailyQuota:
    date: str  # YYYY-MM-DD
    used: int = 0
    limit: int = DEFAULT_DAILY_QUOTA

    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def consume(self, n: int = 1) -> None:
        self.used += n


def load_quota(limit: int = DEFAULT_DAILY_QUOTA) -> DailyQuota:
    today = datetime.now().strftime("%Y-%m-%d")
    if QUOTA_PATH.exists():
        try:
            raw = json.loads(QUOTA_PATH.read_text(encoding="utf-8"))
            if raw.get("date") == today:
                return DailyQuota(date=today, used=int(raw.get("used", 0)), limit=limit)
        except (json.JSONDecodeError, ValueError):
            pass
    return DailyQuota(date=today, used=0, limit=limit)


def save_quota(q: DailyQuota) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    QUOTA_PATH.write_text(
        json.dumps({"date": q.date, "used": q.used, "limit": q.limit}, ensure_ascii=False),
        encoding="utf-8",
    )


# ── Work-hours gate ─────────────────────────────────────────────────


def is_within_work_hours(now: datetime | None = None) -> bool:
    t = (now or datetime.now()).time()
    return any(start <= t <= end for start, end in WORK_HOURS)


# ── Eligibility check ───────────────────────────────────────────────


@dataclass
class PendingCandidate:
    friend_id: int
    uid: int
    name: str
    job_name: str
    last_text: str
    last_time: str


def list_pending(client: BossClient, enc_job_id: str = "", strict_check: bool = True) -> list[PendingCandidate]:
    """Return candidates that look eligible for auto-reply.

    1. Pull label_id=1 (新招呼) friend list.
    2. Fetch friend details + last messages in batch.
    3. (Optional, strict_check=True) For each, fetch a few history messages
       to confirm there is NO prior recruiter reply (every message has
       received=True). Skips otherwise.
    """
    raw = client.get_boss_friend_list(label_id=1, enc_job_id=enc_job_id)
    friend_list = raw.get("result", [])
    if not friend_list:
        return []

    friend_ids = [f["friendId"] for f in friend_list if f.get("friendId")]
    if not friend_ids:
        return []

    details_resp = client.get_boss_friend_details(friend_ids)
    details = {f.get("friendId"): f for f in details_resp.get("friendList", [])}

    last_msgs_raw = client.get_boss_last_messages(friend_ids)
    last_by_uid: dict[int, dict] = {}
    if isinstance(last_msgs_raw, list):
        for m in last_msgs_raw:
            uid = m.get("uid")
            if uid:
                last_by_uid[uid] = m

    out: list[PendingCandidate] = []
    for fid in friend_ids:
        detail = details.get(fid, {})
        uid = detail.get("uid", 0)
        msg_info = last_by_uid.get(uid, {})
        last_text = ""
        if msg_info.get("lastMsgInfo"):
            last_text = msg_info["lastMsgInfo"].get("showText", "")

        if strict_check and uid:
            try:
                hist = client.get_boss_chat_history(gid=fid, count=10)
                msgs = hist.get("messages", [])
                if not msgs:
                    continue
                # All messages must be received=True (= from candidate)
                if any(not m.get("received", True) for m in msgs):
                    continue
            except Exception as exc:  # noqa: BLE001
                logger.warning("history check failed for friendId=%s: %s", fid, exc)
                continue

        out.append(PendingCandidate(
            friend_id=fid,
            uid=uid,
            name=detail.get("name", "?"),
            job_name=detail.get("jobName", ""),
            last_text=last_text,
            last_time=msg_info.get("lastTime", detail.get("lastTime", "")),
        ))
    return out


# ── Send loop with throttling ───────────────────────────────────────


@dataclass
class SendResult:
    friend_id: int
    name: str
    message: str
    ok: bool
    error: str = ""
    skipped_reason: str = ""


@dataclass
class AutoReplyReport:
    sent: list[SendResult] = field(default_factory=list)
    skipped: list[SendResult] = field(default_factory=list)
    quota_after: int = 0
    dry_run: bool = False

    def summary(self) -> dict:
        return {
            "sent_count": len(self.sent),
            "skipped_count": len(self.skipped),
            "quota_remaining": self.quota_after,
            "dry_run": self.dry_run,
            "sent": [{"friend_id": r.friend_id, "name": r.name, "message": r.message} for r in self.sent],
            "skipped": [{"friend_id": r.friend_id, "name": r.name, "reason": r.skipped_reason or r.error} for r in self.skipped],
        }


def _typing_delay(text: str) -> float:
    base = len(text) / TYPING_CHARS_PER_SEC
    return max(0.5, base + random.uniform(0, TYPING_JITTER))


def _inter_send_delay() -> float:
    return random.uniform(SEND_DELAY_MIN, SEND_DELAY_MAX)


def run_auto_reply(
    client: BossClient,
    candidates: list[PendingCandidate],
    templates: list[str],
    *,
    daily_limit: int = DEFAULT_DAILY_QUOTA,
    dry_run: bool = False,
    ignore_hours: bool = False,
    max_send: int | None = None,
    sleep_fn=time.sleep,
) -> AutoReplyReport:
    """Send a templated first reply to each pending candidate.

    Returns a report with sent/skipped breakdown. Honors daily quota and
    work-hours gate; dry_run skips the actual send but still returns the
    full plan so the agent can show it to the user.
    """
    report = AutoReplyReport(dry_run=dry_run)

    if not ignore_hours and not is_within_work_hours():
        for c in candidates:
            report.skipped.append(SendResult(
                friend_id=c.friend_id, name=c.name, message="",
                ok=False, skipped_reason="outside work hours (09:30-12:00 / 14:00-19:00)",
            ))
        return report

    quota = load_quota(limit=daily_limit)

    for idx, c in enumerate(candidates):
        if max_send is not None and len(report.sent) >= max_send:
            report.skipped.append(SendResult(
                friend_id=c.friend_id, name=c.name, message="",
                ok=False, skipped_reason=f"hit max_send={max_send}",
            ))
            continue
        if quota.remaining() <= 0:
            report.skipped.append(SendResult(
                friend_id=c.friend_id, name=c.name, message="",
                ok=False, skipped_reason=f"daily quota exhausted ({quota.used}/{quota.limit})",
            ))
            continue

        msg = pick_template(templates, candidate_name=c.name)

        if dry_run:
            report.sent.append(SendResult(friend_id=c.friend_id, name=c.name, message=msg, ok=True))
            continue

        # Pre-send pacing: simulate reading + typing the message
        sleep_fn(_typing_delay(msg))

        try:
            client.boss_send_message(gid=c.friend_id, content=msg)
        except Exception as exc:  # noqa: BLE001
            report.skipped.append(SendResult(
                friend_id=c.friend_id, name=c.name, message=msg,
                ok=False, error=str(exc),
            ))
            _audit({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "friend_id": c.friend_id, "name": c.name,
                "message": msg, "ok": False, "error": str(exc),
            })
            # Stop the batch on suspected rate-limit/risk-control
            if "code" in str(exc).lower() or "stoken" in str(exc).lower():
                logger.warning("aborting batch after error: %s", exc)
                break
            continue

        quota.consume(1)
        save_quota(quota)
        report.sent.append(SendResult(friend_id=c.friend_id, name=c.name, message=msg, ok=True))
        _audit({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "friend_id": c.friend_id, "name": c.name,
            "message": msg, "ok": True,
        })

        # Inter-send gap (skip after the last one)
        if idx + 1 < len(candidates):
            sleep_fn(_inter_send_delay())

    report.quota_after = quota.remaining()
    return report


def _audit(entry: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with AUDIT_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
