#!/usr/bin/env python3
"""Unit checks for Slack thread read-cursor support."""

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

CHANNEL = "C0123456789"
THREAD = "1736780000.000001"
REPLY = "1736789099.000001"


def run(argv: list[str]) -> tuple[int, dict]:
    MODULE.sys.argv = ["slack_client.py", "-w", "example", *argv]
    output = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(output):
        try:
            MODULE.main()
        except SystemExit as exc:
            code = exc.code or 0
    return code, json.loads(output.getvalue())


def main() -> None:
    client = MODULE.SlackClient("xoxc-test", "xoxd-test")
    calls: list[tuple[str, dict]] = []
    client._post = lambda endpoint, data=None: calls.append((endpoint, data)) or {"ok": True}
    assert client.thread_mark(CHANNEL, THREAD, REPLY) == {"ok": True}
    assert calls == [(
        "subscriptions.thread.mark",
        {"channel": CHANNEL, "thread_ts": THREAD, "ts": REPLY, "read": "1"},
    )]

    cli_calls: list[tuple[str, ...]] = []
    root = {"ts": THREAD, "last_read": "1736789050.000001"}

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def conversations_replies(self, channel: str, thread_ts: str) -> dict:
            cli_calls.append(("replies", channel, thread_ts))
            return {"ok": True, "messages": [dict(root)]}

        def thread_mark(self, channel: str, thread_ts: str, message_ts: str) -> dict:
            cli_calls.append(("mark", channel, thread_ts, message_ts))
            return {"ok": True}

    saved = (MODULE.sys.argv, MODULE.load_config, MODULE.SlackClient,
             MODULE.set_active_workspace, MODULE.record_channel_workspace)
    try:
        MODULE.load_config = lambda _workspace=None: ({
            "xoxc_token": "xoxc-test", "xoxd_token": "xoxd-test", "user_agent": "test",
        }, "example")
        MODULE.SlackClient = FakeClient
        MODULE.set_active_workspace = lambda _workspace: None
        MODULE.record_channel_workspace = lambda _channel, _workspace: None

        code, _ = run(["mark-thread-read", CHANNEL, THREAD, REPLY])
        assert code == 1 and cli_calls == [], "accepted a read mutation without --confirm"

        code, payload = run(["mark-thread-read", CHANNEL, THREAD, REPLY, "--confirm"])
        assert code == 0 and payload["ok"] is True
        assert payload["marked_through"] == REPLY
        assert payload["previous_last_read"] == "1736789050.000001"
        assert cli_calls[-2:] == [("replies", CHANNEL, THREAD), ("mark", CHANNEL, THREAD, REPLY)]

        root["last_read"] = REPLY
        cli_calls.clear()
        code, payload = run(["mark-thread-read", CHANNEL, THREAD, REPLY, "--confirm"])
        assert payload["no_op"] is True and cli_calls == [("replies", CHANNEL, THREAD)]

        root["ts"] = REPLY
        cli_calls.clear()
        code, payload = run(["mark-thread-read", CHANNEL, THREAD, REPLY, "--confirm"])
        assert code == 1 and "parent" in payload["error"]
        assert not any(c[0] == "mark" for c in cli_calls)

        root.update(ts=THREAD)
        root.pop("last_read")
        cli_calls.clear()
        code, payload = run(["mark-thread-read", CHANNEL, THREAD, REPLY, "--confirm"])
        assert code == 1 and "no read cursor" in payload["error"]
        assert not any(c[0] == "mark" for c in cli_calls)
    finally:
        (MODULE.sys.argv, MODULE.load_config, MODULE.SlackClient,
         MODULE.set_active_workspace, MODULE.record_channel_workspace) = saved

    print("PASS: thread read cursor uses subscriptions.thread.mark, forward only, parent ts only")


if __name__ == "__main__":
    main()
