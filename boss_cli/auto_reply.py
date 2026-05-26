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
from datetime import datetime
from pathlib import Path

from .client import BossClient
from .pacing import (
    PaceState,
    load_pace_state,
    next_action,
    reading_pause_seconds,
    record_send,
    save_pace_state,
    work_intensity,
)

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

# Typing-speed model: roughly N chars/sec, plus jitter.
TYPING_CHARS_PER_SEC = 6.0
TYPING_JITTER = 1.5

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
    """Backwards-compatible wrapper: true iff current intensity is > 0.

    The actual intraday curve lives in `pacing.work_intensity`; this function
    just acts as a hard binary gate for callers that don't care about the
    underlying intensity (e.g. CLI's early-exit check).
    """
    return work_intensity(now) > 0.0


# ── Eligibility check ───────────────────────────────────────────────


@dataclass
class PendingCandidate:
    friend_id: int
    uid: int
    name: str
    job_name: str
    last_text: str
    last_time: str
    encrypt_friend_id: str = ""   # for view_geek (the candidate's encryptGeekId)
    job_id: int = 0               # for view_geek context


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
            encrypt_friend_id=detail.get("encryptFriendId", ""),
            job_id=detail.get("jobId", 0),
        ))
    return out


# ── Browse simulation (look like a real HR opening the candidate) ────


@dataclass
class BrowseTraces:
    view_ts: str = ""
    read_ts: str = ""


def pre_reply_browse(
    client: BossClient,
    candidate: PendingCandidate,
    enc_job_id: str = "",
    sleep_fn=time.sleep,
) -> BrowseTraces:
    """Open the candidate's resume + read chat history, then pause "reading".

    Mimics how a real HR uses the web UI: click candidate → resume opens → eyes
    on screen for several seconds → switch to chat tab → start typing. Returns
    timestamps of the two API calls for audit. Failures are non-fatal — we
    return whatever succeeded so the reply can still go.
    """
    traces = BrowseTraces()

    # 1) View resume — best-effort. Needs encrypt_friend_id; pick the best job id.
    if candidate.encrypt_friend_id:
        job_eid = enc_job_id
        if not job_eid:
            # Fall back to whatever job the candidate is tied to
            try:
                jobs = client.get_boss_chatted_jobs()
                if jobs:
                    job_eid = jobs[0].get("encryptJobId", "")
            except Exception as exc:  # noqa: BLE001
                logger.debug("chatted_jobs lookup failed: %s", exc)
        if job_eid:
            try:
                client.get_boss_view_geek(
                    encrypt_geek_id=candidate.encrypt_friend_id,
                    encrypt_job_id=job_eid,
                )
                traces.view_ts = datetime.now().isoformat(timespec="seconds")
            except Exception as exc:  # noqa: BLE001
                logger.debug("view_geek failed for friendId=%s: %s", candidate.friend_id, exc)

    # 2) Reading pause — the most "human" part of the whole sequence
    sleep_fn(reading_pause_seconds())

    # 3) Read recent chat — natural before replying
    try:
        client.get_boss_chat_history(gid=candidate.friend_id, count=10)
        traces.read_ts = datetime.now().isoformat(timespec="seconds")
    except Exception as exc:  # noqa: BLE001
        logger.debug("chat_history failed for friendId=%s: %s", candidate.friend_id, exc)

    return traces


# ── Send loop with throttling ───────────────────────────────────────


@dataclass
class SendResult:
    friend_id: int
    name: str
    message: str
    ok: bool
    error: str = ""
    skipped_reason: str = ""
    burst_position: str = ""


@dataclass
class AutoReplyReport:
    sent: list[SendResult] = field(default_factory=list)
    skipped: list[SendResult] = field(default_factory=list)
    quota_after: int = 0
    dry_run: bool = False
    pace_intensity: float = 0.0
    pace_next_wait_seconds: float = 0.0

    def summary(self) -> dict:
        return {
            "sent_count": len(self.sent),
            "skipped_count": len(self.skipped),
            "quota_remaining": self.quota_after,
            "dry_run": self.dry_run,
            "pace_intensity": round(self.pace_intensity, 2),
            "pace_next_wait_seconds": round(self.pace_next_wait_seconds, 1),
            "sent": [
                {"friend_id": r.friend_id, "name": r.name, "message": r.message,
                 "burst_position": r.burst_position}
                for r in self.sent
            ],
            "skipped": [
                {"friend_id": r.friend_id, "name": r.name,
                 "reason": r.skipped_reason or r.error}
                for r in self.skipped
            ],
        }


def _typing_delay(text: str) -> float:
    base = len(text) / TYPING_CHARS_PER_SEC
    return max(0.5, base + random.uniform(0, TYPING_JITTER))


def run_auto_reply(
    client: BossClient,
    candidates: list[PendingCandidate],
    templates: list[str],
    *,
    daily_limit: int = DEFAULT_DAILY_QUOTA,
    dry_run: bool = False,
    ignore_hours: bool = False,
    max_send: int | None = None,
    respect_pacing: bool = False,
    sleep_fn=time.sleep,
    enc_job_id: str = "",
) -> AutoReplyReport:
    """Send a templated first reply to each pending candidate.

    Two flow modes:
    - respect_pacing=False (CLI batch): walk the candidate list; for each, if
      the pacing state says "wait" / "rest", sleep that long then re-check.
      Sends are spaced by burst/rest gaps from `pacing.next_action`. Can run
      for hours; use --max-send to bound.
    - respect_pacing=True (MCP/cron tick): at most ONE send per invocation.
      If pacing state says wait/rest/silent, skip immediately and report why
      so the caller can come back later. Recommended cadence: every 10 min.
    """
    report = AutoReplyReport(dry_run=dry_run)
    intensity = work_intensity()
    report.pace_intensity = intensity

    if not ignore_hours and intensity <= 0.0:
        for c in candidates:
            report.skipped.append(SendResult(
                friend_id=c.friend_id, name=c.name, message="",
                ok=False, skipped_reason="outside work-intensity window (lunch / off-hours)",
            ))
        return report

    quota = load_quota(limit=daily_limit)
    pace_state = load_pace_state()

    for c in candidates:
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

        # Pacing gate — decide whether we may send right now
        action = next_action(pace_state, intensity)

        if action.action == "silent":
            report.skipped.append(SendResult(
                friend_id=c.friend_id, name=c.name, message="",
                ok=False, skipped_reason=action.reason,
            ))
            break  # silent slot applies to all remaining candidates

        if action.action in ("wait", "rest"):
            if respect_pacing:
                report.skipped.append(SendResult(
                    friend_id=c.friend_id, name=c.name, message="",
                    ok=False, skipped_reason=f"{action.action} ({action.reason})",
                ))
                report.pace_next_wait_seconds = action.wait_seconds
                # In tick mode, one decision per invocation — return now
                save_pace_state(pace_state)
                report.quota_after = quota.remaining()
                return report
            else:
                # Batch mode: actually sleep, then re-evaluate this same candidate
                logger.info("pacing %s: sleeping %.0fs (%s)",
                            action.action, action.wait_seconds, action.reason)
                sleep_fn(action.wait_seconds)
                # Re-check eligibility (intensity may have flipped to 0 mid-sleep)
                intensity = work_intensity()
                if intensity <= 0.0:
                    report.skipped.append(SendResult(
                        friend_id=c.friend_id, name=c.name, message="",
                        ok=False, skipped_reason="entered silent slot mid-batch",
                    ))
                    break
                action = next_action(pace_state, intensity)
                if action.action != "send":
                    # Shouldn't happen normally, but be defensive
                    report.skipped.append(SendResult(
                        friend_id=c.friend_id, name=c.name, message="",
                        ok=False, skipped_reason=f"unexpected post-sleep {action.action}",
                    ))
                    continue

        # action == "send"
        msg = pick_template(templates, candidate_name=c.name)

        if dry_run:
            report.sent.append(SendResult(
                friend_id=c.friend_id, name=c.name, message=msg, ok=True,
                burst_position=action.burst_position,
            ))
            # In dry-run, advance pace_state too so the printed plan is realistic
            record_send(pace_state, intensity)
            continue

        # Real send: browse → read pause → typing → send
        traces = pre_reply_browse(client, c, enc_job_id=enc_job_id, sleep_fn=sleep_fn)
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
                "view_ts": traces.view_ts, "read_ts": traces.read_ts,
                "intensity": round(intensity, 2),
                "burst_position": action.burst_position,
            })
            # Stop the batch on suspected rate-limit/risk-control
            if "code" in str(exc).lower() or "stoken" in str(exc).lower():
                logger.warning("aborting batch after error: %s", exc)
                break
            continue

        quota.consume(1)
        save_quota(quota)
        record_send(pace_state, intensity)
        save_pace_state(pace_state)
        report.sent.append(SendResult(
            friend_id=c.friend_id, name=c.name, message=msg, ok=True,
            burst_position=action.burst_position,
        ))
        _audit({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "friend_id": c.friend_id, "name": c.name,
            "message": msg, "ok": True,
            "view_ts": traces.view_ts, "read_ts": traces.read_ts,
            "intensity": round(intensity, 2),
            "burst_position": action.burst_position,
        })

        if respect_pacing:
            # Single-send tick: stop after one successful send
            report.pace_next_wait_seconds = max(0.0, pace_state.next_eligible_ts - time.time())
            break

    save_pace_state(pace_state)
    report.quota_after = quota.remaining()
    return report


def pacing_snapshot() -> dict:
    """Convenience: return current pace state + intensity for status display."""
    state = load_pace_state()
    intensity = work_intensity()
    action = next_action(state, intensity)
    return {
        "intensity": round(intensity, 2),
        "burst_count": state.burst_count,
        "burst_target": state.burst_target,
        "next_eligible_in_seconds": max(0.0, state.next_eligible_ts - time.time()),
        "action": action.action,
        "reason": action.reason,
    }


def _audit(entry: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with AUDIT_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
