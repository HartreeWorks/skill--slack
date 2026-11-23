# Slack

A skill for searching, reading and sending Slack messages from your agent across multiple workspaces. Paste a Slack link and your agent reads the whole thread around it, with names and times filled in. It can leave a message as an unsent draft in Slack for you to check and send, see which channels and threads have unread messages, and mark them read. It also handles shared Slack Connect channels, digests and message exports.

## Requirements

- Python 3.10 or later with the `requests` package
- Access to Slack in a browser so you can copy your browser session tokens into a local config file

The tokens stay on your machine, in `~/.config/hartreeworks-skills/slack/config.json`. Follow the [setup guide](./references/setup.md) to configure a workspace and test the connection.

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

## Updates

At most once a day, the skill checks for a new release. When there is one, your agent shows what changed, reviews the code changes, and asks whether to update now, update automatically from now on, or not now. Automatic updates wait three days after a release first appears. To stop the checks, ask your agent to run `scripts/check-update.sh --off`.

### Updating from an earlier version

If you installed a version without update checks, ask your agent to update the skill; it follows these steps. Agents: follow them exactly, in order. A plain `npx skills update` deletes the installed skill folder, and older copies keep the user's data inside it, in `data/`.

1. Find every installed copy: folders named `slack` under `~/.agents/skills/`, `~/.claude/skills/`, `~/.codex/skills/` and the project's `.claude/skills/` or `.agents/skills/`.
2. If an installed copy has `scripts/check-update.sh`, run `bash scripts/check-update.sh --apply` from the project folder and stop here: it handles migration and the update itself.
3. Otherwise, download this repository to a temporary folder (`git clone --depth 1 https://github.com/HartreeWorks/skill--slack`) and, for each installed copy that has a `data/` folder, run `bash <temporary folder>/scripts/migrate.sh --from <installed copy>`. It backs up the data, copies it to `~/.config/hartreeworks-skills/slack/`, verifies the copy and stops on conflicts. Do not continue until every copy reports "Migrated" or "Nothing to migrate"; show the user any conflict.
4. Install the latest release, giving the scope explicitly: `npx -y skills@1.7.4 add HartreeWorks/skill--slack#v$(cat <temporary folder>/VERSION) --skill slack -g -y` for a global copy, and the same without `-g`, run from the project folder, for a project copy.
5. Confirm `~/.config/hartreeworks-skills/slack/` holds the user's files, then tell the user what changed (`CHANGES.md`).

Older copies also kept `config.json` (the Slack tokens) at the top of the skill folder, which `migrate.sh` does not carry over. Before step 4, copy each copy's `config.json` to `~/.config/hartreeworks-skills/slack/config.json` if that file does not exist yet.

## Documentation

See [SKILL.md](./SKILL.md) for complete setup, command and workflow documentation.

## About

Created by [Peter Hartree](https://x.com/peterhartree) of [AI Wow](https://wow.pjh.is).

Find more skills at [HartreeWorks/skills](https://github.com/HartreeWorks/skills).
