#!/usr/bin/env python3
"""Unit checks for Do Not Disturb and focus support."""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "slack_client.py"
SPEC = importlib.util.spec_from_file_location("slack_client", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def main() -> None:
    now = datetime(2026, 10, 29, 9, 30, 20)
    assert MODULE.minutes_until("12:00", now) == 150
    assert MODULE.minutes_until("09:00", now) == 23 * 60 + 30
    for bad in ("25:00", "12", "noon"):
        try:
            MODULE.minutes_until(bad, now)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted {bad}")

    client = MODULE.SlackClient("xoxc-test", "xoxd-test")
    calls: list[tuple[str, dict]] = []
    client._post = lambda endpoint, data=None: calls.append((endpoint, data)) or {"ok": True}
    client.dnd_set_snooze(45)
    client.dnd_end_snooze()
    client.set_status("", "")
    assert calls[0] == ("dnd.setSnooze", {"num_minutes": "45"})
    assert calls[1] == ("dnd.endSnooze", None)
    assert calls[2][0] == "users.profile.set"

    summary = MODULE.dnd_summary({"snooze_enabled": False, "dnd_enabled": False}, "test")
    assert summary["paused"] is False and summary["schedule_pause"] is None
    print("ok")


if __name__ == "__main__":
    main()
