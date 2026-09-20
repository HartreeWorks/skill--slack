# Slack

A skill for searching, reading and sending Slack messages from your agent across multiple workspaces. It resolves Slack user IDs before presenting messages, handles shared Slack Connect channels, and supports thread activity, digests and message exports.

## Requirements

- Python 3.10 or later with the `requests` package
- Access to Slack in a browser so you can copy your browser session tokens into a local `config.json`

The tokens remain in the local skill folder and are excluded from publication. Follow the [setup guide in SKILL.md](./SKILL.md#setup) to configure a workspace and test the connection.

## Installation

```bash
npx skills add HartreeWorks/skill--slack
```

## Try it

Ask your agent:

```text
Summarise what I missed in #project-alpha since yesterday. Resolve every user ID to a name and link the messages that need my response.
```

The skill selects the relevant workspace, retrieves the channel activity, resolves message authors and returns a concise digest with links back to Slack.

## Documentation

See [SKILL.md](./SKILL.md) for complete setup, command and workflow documentation.

## About

Created by [Peter Hartree](https://x.com/peterhartree) of [AI Wow](https://wow.pjh.is).

Find more skills at [HartreeWorks/skills](https://github.com/HartreeWorks/skills).
