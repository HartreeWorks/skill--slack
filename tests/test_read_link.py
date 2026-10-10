#!/usr/bin/env python3
"""Offline checks for Slack link parsing and full-thread reading."""

from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "slack_client.py"
SPEC = importlib.util.spec_from_file_location("slack_client", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)

PARENT, REPLY = "1736789100.100001", "1736789110.200002"


def test_parse() -> None:
    link = MODULE.parse_slack_link(f"https://acme.slack.com/archives/C01/p{REPLY.replace('.', '')}")
    assert link == {"domain": "acme", "team_id": None, "channel": "C01", "ts": REPLY, "thread_ts": None}
    link = MODULE.parse_slack_link(
        f"https://acme.slack.com/archives/C01/p{REPLY.replace('.', '')}?thread_ts={PARENT}&cid=C01")
    assert link["thread_ts"] == PARENT
    link = MODULE.parse_slack_link(f"https://app.slack.com/client/T01/C01/thread/C01-{PARENT}")
    assert (link["team_id"], link["channel"], link["ts"]) == ("T01", "C01", PARENT)
    try:
        MODULE.parse_slack_link("https://example.com/archives/C01")
    except ValueError:
        pass
    else:
        raise AssertionError("non-message link accepted")


def test_reply_link_reads_whole_thread() -> None:
    thread = [
        {"ts": PARENT, "user": "U1", "text": "Question for <@U2>?", "reply_count": 2},
        {"ts": REPLY, "thread_ts": PARENT, "user": "U2", "text": "Answer"},
        {"ts": "1789764400.000001", "thread_ts": PARENT, "user": "U1", "text": "Thanks"},
    ]

    class FakeClient:
        def conversations_replies(self, channel, ts):
            # Slack returns only the reply when given a reply's ts.
            return {"ok": True, "messages": thread if ts == PARENT else [thread[1]]}

        def conversations_info(self, channel):
            return {"ok": True, "channel": {"id": channel, "name": "general"}}

        def users_info(self, user_id):
            return {"ok": False}

    cache = {"users": {"U1": {"real_name": "Alice"}, "U2": {"real_name": "Bob"}}}
    originals = {name: getattr(MODULE, name) for name in (
        "load_config", "make_client", "load_cache", "save_cache", "is_user_cache_empty",
        "is_user_cache_stale", "record_channel_workspace")}
    try:
        MODULE.load_config = lambda workspace=None: ({}, "acme")
        MODULE.make_client = lambda creds: FakeClient()
        MODULE.load_cache = lambda workspace: cache
        MODULE.save_cache = lambda workspace, data: None
        MODULE.is_user_cache_empty = lambda workspace: False
        MODULE.is_user_cache_stale = lambda workspace: False
        MODULE.record_channel_workspace = lambda channel, workspace: None
        result = MODULE.read_link(f"https://acme.slack.com/archives/C01/p{REPLY.replace('.', '')}", "acme")
    finally:
        for name, value in originals.items():
            setattr(MODULE, name, value)

    assert result["linked_is_reply"] is True
    assert result["thread_ts"] == PARENT
    assert [m["user_name"] for m in result["thread"]] == ["Alice", "Bob", "Alice"]
    assert result["thread"][0]["text"] == "Question for @Bob?"
    assert [m["ts"] for m in result["thread"] if m.get("linked")] == [REPLY]
    assert result["channel"]["name"] == "#general"
    assert "context_before" not in result


if __name__ == "__main__":
    test_parse()
    test_reply_link_reads_whole_thread()
    print("PASS: Slack links parse and reply links read the whole thread")
