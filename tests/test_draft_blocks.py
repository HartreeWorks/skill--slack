#!/usr/bin/env python3
"""Unit checks for the mrkdwn -> rich_text converter used by draft and draft-edit."""

from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "slack_client.py"
SPEC = importlib.util.spec_from_file_location("slack_client", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def els(text: str) -> list:
    return MODULE.mrkdwn_to_blocks(text)[0]["elements"]


def main() -> None:
    # A fenced block with a language tag, after a label line, followed by a list.
    text = (
        "Run *this*:\n"
        "```bash\n"
        "1. not a list\n"
        "- nor this *bold* or <@U123ABC>\n"
        "curl https://example.com/x\n"
        "```\n"
        "\n"
        "1. first\n"
        "2. second\n"
    )
    e = els(text)
    assert [x["type"] for x in e] == ["rich_text_section", "rich_text_preformatted", "rich_text_section"], e
    assert e[0]["elements"][1] == {"type": "text", "text": "this", "style": {"bold": True}}
    assert e[1]["elements"] == [{"type": "text", "text":
        "1. not a list\n- nor this *bold* or <@U123ABC>\ncurl https://example.com/x"}], e[1]
    # Numbered lines stay plain text: the user never wants Slack's numbered-list element.
    assert e[2]["elements"] == [{"type": "text", "text": "\n1. first\n2. second"}], e[2]

    # Bullet lines still become a bullet list.
    e = els("- a\n• b\n")
    assert e[0]["type"] == "rich_text_list" and e[0]["style"] == "bullet" and len(e[0]["elements"]) == 2, e

    # Text after the block keeps its blank-line gap; no language tag.
    e = els("```\ncode\n```\n\nafter")
    assert e[0]["type"] == "rich_text_preformatted"
    assert e[1]["elements"] == [{"type": "text", "text": "\nafter"}], e[1]

    # An unclosed fence stays literal text.
    e = els("```\nnot closed")
    assert e == [{"type": "rich_text_section", "elements": [{"type": "text", "text": "```\nnot closed"}]}], e

    # Inline code still works.
    e = els("use `x = 1` here")
    assert e[0]["elements"][1] == {"type": "text", "text": "x = 1", "style": {"code": True}}, e

    print("ok")


if __name__ == "__main__":
    main()
