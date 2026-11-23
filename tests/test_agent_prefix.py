#!/usr/bin/env python3
"""`send` marks every message as the agent's, once."""

from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "slack_client.py"
SPEC = importlib.util.spec_from_file_location("slack_client", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def main() -> None:
    assert MODULE.agent_prefixed("Done.") == "[agent] Done."
    assert MODULE.agent_prefixed("[agent] Done.") == "[agent] Done."
    assert MODULE.agent_prefixed("") == "[agent] "
    assert MODULE.agent_prefixed("*Title*\nBody.") == "*[agent] Title*\nBody."
    assert MODULE.agent_prefixed("*[agent] Title*\nBody.") == "*[agent] Title*\nBody."
    assert MODULE.agent_prefixed("*bold* start, then more.") == "[agent] *bold* start, then more."
    print("ok")


if __name__ == "__main__":
    main()
