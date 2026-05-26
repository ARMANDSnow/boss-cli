"""Tests for boss_cli.pacing — intraday intensity curve + pulse state machine."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import pytest

from boss_cli import pacing


# ── work_intensity ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "hour,minute,expected",
    [
        (9, 0,   0.0),    # before workday
        (9, 30,  0.5),    # boundary in
        (10, 0,  0.5),
        (10, 30, 1.0),    # morning peak begins
        (11, 30, 1.0),
        (12, 0,  0.0),    # lunch — hard silence
        (12, 45, 0.0),
        (13, 30, 0.4),    # post-lunch slow start
        (14, 30, 1.0),    # afternoon peak
        (16, 0,  1.0),
        (16, 30, 0.7),
        (17, 30, 0.7),
        (18, 0,  0.3),    # winding down
        (19, 0,  0.0),    # after work
        (22, 0,  0.0),
        (3, 0,   0.0),
    ],
)
def test_work_intensity_curve(hour, minute, expected):
    now = datetime(2026, 5, 26, hour, minute)
    assert pacing.work_intensity(now) == pytest.approx(expected)


def test_lunch_is_hard_silence_throughout():
    # 12:00 to 13:30 must all return 0.0 — non-negotiable
    for hh in range(12 * 60, 13 * 60 + 30, 5):
        h, m = divmod(hh, 60)
        assert pacing.work_intensity(datetime(2026, 5, 26, h, m)) == 0.0


# ── next_action state machine ───────────────────────────────────────


def test_silent_when_intensity_zero():
    state = pacing.PaceState()
    action = pacing.next_action(state, intensity=0.0, now_ts=1000.0)
    assert action.action == "silent"


def test_fresh_state_can_send_immediately():
    state = pacing.PaceState()
    action = pacing.next_action(state, intensity=1.0, now_ts=1000.0)
    assert action.action == "send"
    assert state.burst_target >= pacing.BURST_MIN
    assert state.burst_target <= pacing.BURST_MAX
    assert action.burst_position == f"1/{state.burst_target}"


def test_wait_when_cooldown_active():
    state = pacing.PaceState(
        next_eligible_ts=1500.0,
        burst_target=4, burst_count=1,
    )
    action = pacing.next_action(state, intensity=1.0, now_ts=1000.0)
    assert action.action == "wait"
    assert action.wait_seconds == pytest.approx(500.0)


def test_rest_when_burst_full():
    state = pacing.PaceState(
        burst_target=3, burst_count=3,
        next_eligible_ts=900.0,  # cooldown already expired
    )
    action = pacing.next_action(state, intensity=1.0, now_ts=1000.0)
    assert action.action == "rest"
    # Rest gap is 15-40 min scaled by intensity; at intensity=1.0, base range applies
    assert pacing.REST_GAP_SEC[0] <= action.wait_seconds <= pacing.REST_GAP_SEC[1]
    # After rest action, state should be reset for next burst to pick fresh target
    assert state.burst_count == 0
    assert state.burst_target == 0


def test_record_send_advances_burst_and_sets_cooldown():
    state = pacing.PaceState(burst_target=4, burst_count=1, next_eligible_ts=0.0)
    pacing.record_send(state, intensity=1.0, now_ts=2000.0)
    assert state.burst_count == 2
    assert state.last_send_ts == 2000.0
    # next_eligible_ts should be 30-180s out
    delta = state.next_eligible_ts - 2000.0
    assert pacing.INTRA_BURST_SEC[0] <= delta <= pacing.INTRA_BURST_SEC[1]


def test_low_intensity_stretches_gaps():
    state = pacing.PaceState(burst_target=4, burst_count=1)
    pacing.record_send(state, intensity=0.3, now_ts=2000.0)
    delta = state.next_eligible_ts - 2000.0
    # At intensity 0.3, stretch ≈ 3.33x; upper bound roughly 180 * 3.33 = ~600s
    assert delta > pacing.INTRA_BURST_SEC[1]  # must be larger than peak max
    assert delta < pacing.INTRA_BURST_SEC[1] * 5  # but not absurdly so


# ── State persistence round-trip ────────────────────────────────────


def test_pace_state_persistence(tmp_path, monkeypatch):
    fake_path = tmp_path / "session_state.json"
    monkeypatch.setattr(pacing, "STATE_PATH", fake_path)
    monkeypatch.setattr(pacing, "CONFIG_DIR", tmp_path)

    state = pacing.PaceState(
        last_send_ts=1234.5, burst_count=2, burst_target=4, next_eligible_ts=5678.9,
    )
    pacing.save_pace_state(state)

    loaded = pacing.load_pace_state()
    assert loaded.last_send_ts == 1234.5
    assert loaded.burst_count == 2
    assert loaded.burst_target == 4
    assert loaded.next_eligible_ts == 5678.9


def test_load_pace_state_missing_file_returns_default(tmp_path, monkeypatch):
    monkeypatch.setattr(pacing, "STATE_PATH", tmp_path / "nope.json")
    s = pacing.load_pace_state()
    assert s.last_send_ts == 0.0
    assert s.burst_count == 0
    assert s.burst_target == 0


def test_load_pace_state_corrupt_file_returns_default(tmp_path, monkeypatch):
    bad = tmp_path / "session_state.json"
    bad.write_text("not json at all", encoding="utf-8")
    monkeypatch.setattr(pacing, "STATE_PATH", bad)
    s = pacing.load_pace_state()
    assert s.last_send_ts == 0.0


# ── Full burst-then-rest cycle simulation ────────────────────────────


def test_full_burst_cycle():
    """Simulate a burst: 4 sends, then forced rest."""
    state = pacing.PaceState()
    now = 1000.0

    sends = 0
    for _ in range(20):  # bound the loop
        action = pacing.next_action(state, intensity=1.0, now_ts=now)
        if action.action == "send":
            pacing.record_send(state, intensity=1.0, now_ts=now)
            sends += 1
            now += 0.1  # tiny gap
        elif action.action == "rest":
            now += action.wait_seconds + 0.1
        elif action.action == "wait":
            now = state.next_eligible_ts + 0.1
        if sends >= 8:
            break

    # 8 sends should span at least one rest gap
    # so at least one ~900s+ jump must have happened
    assert sends == 8
