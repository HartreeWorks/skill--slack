#!/usr/bin/env python3
"""Unit checks for Slack read-cursor support."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "slack_client.py"
SPEC = importlib.util.spec_from_file_location("slack_client", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def main() -> None:
    client = MODULE.SlackClient("xoxc-test", "xoxd-test")
    calls: list[tuple[str, dict]] = []
    client._post = lambda endpoint, data=None: calls.append((endpoint, data)) or {"ok": True}
    info_result = client.conversations_info("C0123456789")
    result = client.conversations_mark("C0123456789", "1736789012.123456")
    assert info_result == {"ok": True}
    assert result == {"ok": True}
    assert calls == [
        ("conversations.info", {"channel": "C0123456789"}),
        (
            "conversations.mark",
            {"channel": "C0123456789", "ts": "1736789012.123456"},
        ),
    ]

    cli_calls: list[tuple[str, ...]] = []
    current_cursor = ["1736789000.000001"]

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def conversations_info(self, channel: str) -> dict:
            cli_calls.append(("info", channel))
            return {"ok": True, "channel": {"last_read": current_cursor[0]}}

        def conversations_mark(self, channel: str, message_ts: str) -> dict:
            cli_calls.append(("mark", channel, message_ts))
            return {"ok": True}

    original_argv = MODULE.sys.argv
    original_load_config = MODULE.load_config
    original_client = MODULE.SlackClient
    original_set_active = MODULE.set_active_workspace
    original_record_channel = MODULE.record_channel_workspace
    try:
        MODULE.load_config = lambda _workspace=None: ({
            "xoxc_token": "xoxc-test",
            "xoxd_token": "xoxd-test",
            "user_agent": "test",
        }, "example")
        MODULE.SlackClient = FakeClient
        MODULE.set_active_workspace = lambda _workspace: None
        MODULE.record_channel_workspace = lambda _channel, _workspace: None

        MODULE.sys.argv = [
            "slack_client.py", "-w", "example", "mark-read",
            "C0123456789", "1736789012.123456",
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                MODULE.main()
            except SystemExit as exc:
                assert exc.code == 1
            else:
                raise AssertionError("Slack CLI accepted a read mutation without --confirm")
        assert cli_calls == []

        MODULE.sys.argv = [
            "slack_client.py", "-w", "example", "mark-read",
            "C0123456789", "1736789012.123456", "--confirm",
        ]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            MODULE.main()
        payload = json.loads(output.getvalue())
        assert payload["ok"] is True
        assert payload["marked_through"] == "1736789012.123456"
        assert cli_calls == [
            ("info", "C0123456789"),
            ("mark", "C0123456789", "1736789012.123456"),
        ]

        current_cursor[0] = "1736789020.000001"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            MODULE.main()
        payload = json.loads(output.getvalue())
        assert payload["ok"] is True
        assert payload["no_op"] is True
        assert payload["current_last_read"] == "1736789020.000001"
        assert cli_calls == [
            ("info", "C0123456789"),
            ("mark", "C0123456789", "1736789012.123456"),
            ("info", "C0123456789"),
        ]
    finally:
        MODULE.sys.argv = original_argv
        MODULE.load_config = original_load_config
        MODULE.SlackClient = original_client
        MODULE.set_active_workspace = original_set_active
        MODULE.record_channel_workspace = original_record_channel

    print("PASS: Slack read cursor uses conversations.mark with the exact channel and timestamp")


if __name__ == "__main__":
    main()
