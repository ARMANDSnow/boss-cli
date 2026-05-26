"""Pulse pacing + intraday intensity curve for auto-reply.

Why this exists: a fixed 12-30s interval looks like a metronome to risk-control.
Real HR work is bursty (3-5 messages in a cluster, then 15-40 min away to do
something else) and follows an intraday intensity curve (low after lunch,
peak mid-morning and mid-afternoon, zero during the lunch break).

Three primitives:
- work_intensity(now) -> 0.0..1.0   intraday curve, 0 means hard silence
- PaceState (persisted)             remembers where we are in the current burst
- next_action(state, intensity)     state machine: send / wait / rest / silent
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, asdict
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Literal

CONFIG_DIR = Path.home() / ".config" / "boss-cli"
STATE_PATH = CONFIG_DIR / "session_state.json"

# Intraday intensity curve. List of (start, end, intensity).
# 0.0 = hard silence (we will not send no matter what)
# 1.0 = peak (intra-burst gaps are minimal, rest gaps are minimal)
# fractional values scale gaps inversely (lower intensity = longer gaps).
INTENSITY_CURVE: list[tuple[dtime, dtime, float]] = [
    (dtime(9, 30),  dtime(10, 30), 0.5),
    (dtime(10, 30), dtime(12, 0),  1.0),
    (dtime(12, 0),  dtime(13, 30), 0.0),  # lunch — hard silence
    (dtime(13, 30), dtime(14, 30), 0.4),
    (dtime(14, 30), dtime(16, 30), 1.0),
    (dtime(16, 30), dtime(18, 0),  0.7),
    (dtime(18, 0),  dtime(19, 0),  0.3),
]

# Burst-cluster parameters
BURST_MIN, BURST_MAX = 3, 5              # how many sends per cluster
INTRA_BURST_SEC = (30, 180)              # gap between sends within a cluster
REST_GAP_SEC = (900, 2400)               # gap between clusters (15-40 min)

ActionType = Literal["send", "wait", "rest", "silent"]


def work_intensity(now: datetime | None = None) -> float:
    """Return 0.0..1.0 based on intraday curve. 0 means do not send."""
    t = (now or datetime.now()).time()
    for start, end, intensity in INTENSITY_CURVE:
        if start <= t < end:
            return intensity
    return 0.0


def _scale_gap(low: float, high: float, intensity: float) -> float:
    """Pick a gap in [low, high], stretched when intensity is low.

    intensity=1.0 → unstretched. intensity=0.3 → roughly 2x longer.
    Floor at 0.1 to avoid division blow-up.
    """
    stretch = 1.0 / max(0.3, intensity)
    return random.uniform(low * stretch, high * stretch)


@dataclass
class PaceState:
    """Persisted across CLI/MCP invocations so pulse pattern survives."""
    last_send_ts: float = 0.0
    burst_count: int = 0          # how many sends in the current burst so far
    burst_target: int = 0         # 0 = pick a fresh target on next send
    next_eligible_ts: float = 0.0  # we may not send before this wall-clock time

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> PaceState:
        return cls(
            last_send_ts=float(d.get("last_send_ts", 0.0)),
            burst_count=int(d.get("burst_count", 0)),
            burst_target=int(d.get("burst_target", 0)),
            next_eligible_ts=float(d.get("next_eligible_ts", 0.0)),
        )


def load_pace_state() -> PaceState:
    if not STATE_PATH.exists():
        return PaceState()
    try:
        return PaceState.from_dict(json.loads(STATE_PATH.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValueError, OSError):
        return PaceState()


def save_pace_state(state: PaceState) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")


@dataclass
class NextAction:
    action: ActionType
    wait_seconds: float = 0.0
    burst_position: str = ""    # "2/4" for audit
    reason: str = ""


def next_action(
    state: PaceState,
    intensity: float,
    now_ts: float | None = None,
) -> NextAction:
    """Decide what to do right now.

    - silent : intraday intensity is 0 (lunch / off-hours). Caller must skip.
    - wait   : we're still in cooldown from the last send. Sleep wait_seconds.
    - rest   : burst is finished, take a long break.
    - send   : OK to send a new message right now.

    Mutates state's burst counters when returning "send" (so a freshly-started
    burst gets a target). Caller is responsible for persisting state AFTER a
    successful send (so a retry on transient error doesn't double-advance).
    """
    now_ts = now_ts if now_ts is not None else time.time()

    if intensity <= 0.0:
        return NextAction("silent", reason="outside work-intensity curve (silent slot)")

    # Honor an explicit cooldown from a prior call
    if now_ts < state.next_eligible_ts:
        return NextAction(
            "wait",
            wait_seconds=state.next_eligible_ts - now_ts,
            reason=f"cooldown until {datetime.fromtimestamp(state.next_eligible_ts).strftime('%H:%M:%S')}",
        )

    # Fresh burst: pick a target
    if state.burst_target == 0:
        state.burst_target = random.randint(BURST_MIN, BURST_MAX)
        state.burst_count = 0

    # Burst finished? Force a rest gap.
    if state.burst_count >= state.burst_target:
        rest = _scale_gap(*REST_GAP_SEC, intensity)
        state.next_eligible_ts = now_ts + rest
        # Reset burst so next eligible window starts a new one
        state.burst_count = 0
        state.burst_target = 0
        return NextAction("rest", wait_seconds=rest,
                          reason=f"burst done, resting {rest:.0f}s")

    return NextAction(
        "send",
        burst_position=f"{state.burst_count + 1}/{state.burst_target}",
        reason="ok to send",
    )


def record_send(state: PaceState, intensity: float, now_ts: float | None = None) -> None:
    """Call this AFTER a successful send. Updates burst counters + cooldown."""
    now_ts = now_ts if now_ts is not None else time.time()
    state.last_send_ts = now_ts
    state.burst_count += 1
    # Set cooldown to next intra-burst gap (will be overwritten by rest if burst ends)
    state.next_eligible_ts = now_ts + _scale_gap(*INTRA_BURST_SEC, intensity)


def reading_pause_seconds() -> float:
    """How long to 'read the resume' between view and typing. 8-25s, gaussian-ish."""
    return max(6.0, random.gauss(15.0, 4.0))
