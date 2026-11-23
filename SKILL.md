---
name: slack
description: Reads, searches, digests and sends Slack messages across multiple workspaces. Use whenever a Slack link is shared, or when asked to find, summarise, export or post Slack content.
---

# Slack

A self-contained Python client for Slack, run through one launcher:

```bash
SLACK=~/.agents/skills/slack/scripts/slack
$SLACK help   # every command and flag
```

## Before starting: check for updates

If `scripts/check-update.sh` exists in this skill's folder, run
`bash <this skill's folder>/scripts/check-update.sh` from the user's project
folder. It checks at most once a day and prints nothing when the skill is
current.

- **It reports an update:** run it with `--diff` and read the code changes.
  Show the user the release notes and a short summary of the changes, flagging
  anything the notes do not explain. Ask whether to update now, update now and
  automatically from now on, or not now. For the first run it with `--apply`;
  for the second run it with `--enable-auto`, then `--apply`; for the third
  carry on.
- **It reports an automatic update:** run it with `--diff` and read the
  changes. If they match the release notes and do nothing risky, run it with
  `--apply` and tell the user in one line. Otherwise show the user your
  concerns and ask.
- **After `--apply` succeeds:** re-read this SKILL.md before continuing.
- **It reports a migration conflict:** show the user the conflicting files
  and wait for them to choose which version to keep.

## Rules for every Slack task

1. **Use this CLI for all Slack work.** It covers every configured workspace; leave any Slack MCP connector unused, even when one is available.
2. **Name the workspace on every command:** `$SLACK -w <workspace> ...`, choosing from `$SLACK workspaces`. Workspace names are local labels and can differ from Slack domains; `read-link` maps a link's domain for you, and commands on a channel ID the client has seen before infer it.
3. **Read a shared link in full before acting on it** (next section).
4. **Attribute messages to resolved names** (see "User ID resolution").

User data lives in the data folder, `~/.config/hartreeworks-skills/slack/`, outside the skill folder so updates keep it: `config.json` (tokens and settings), the name caches, session state and digests. If `config.json` is missing there, a workspace needs adding, or a command fails with `invalid_auth`, follow [references/setup.md](references/setup.md).

## Reading a Slack link

When the user shares a Slack message link and the task depends on what it says, read it with:

```bash
$SLACK read-link "<url>"
```

`read-link` takes the workspace from the URL and returns the whole thread (parent and every reply) with names and local times; the linked message carries `"linked": true`, and `linked_is_reply` says whether it sits inside a thread. A message with no thread also gets five channel messages either side (`context_before`, `context_after`), because conversations often continue outside threads; `--context N` changes that number.

The linked message is rarely the whole story: the parent frames the question, and later replies may have answered, changed or reversed it. Reading is done when you can say who asked what, what was decided, and what is still open, as of the thread's latest reply. If a neighbouring message has its own `reply_count`, or a reply points to another thread, read that too when it bears on the task.

## User ID resolution

Slack returns user IDs (`U0123456789`), not names. Attributing a message from context clues or writing style spreads misinformation, so every name you present comes from a lookup.

- `read-link` output, and `history`, `replies` and `search` with `--resolve`, carry `user_name`, a local `time` and readable mentions. Use these for anything you will read or summarise.
- The client caches user and channel names per workspace in the data folder's `slack-cache-<workspace>.json`, looks up each unknown ID once (Slack Connect and deactivated users included), and refreshes the user list in the background after 14 days.
- With raw output, run `$SLACK -w <workspace> user-lookup` first for the `user_id → name` map. An ID that still has no name is shown as the ID, with a note.

---

## CRITICAL: Slack Connect (external) users

**Slack's `from:username` search does NOT work for Slack Connect (external) users.** These are people who belong to another workspace but participate in shared channels on your workspace. Their user IDs belong to the external workspace, so `search "from:username"` returns zero results even though they've posted messages.

### How to identify Slack Connect users

Slack Connect users appear on shared channels. When listing channels, shared channels have `is_shared: true` or `is_ext_shared: true`. External users' messages in channel history include an embedded `user_profile` with fields like `real_name`, `display_name`, and `team` (their home workspace ID, different from yours).

### Finding messages from Slack Connect users

Since search won't work, use channel history instead:

1. **Identify the relevant shared channel(s):**
   ```bash
   $SLACK -w <workspace> channels "public_channel,private_channel"
   ```
   Look for channels with `is_shared: true` or `is_ext_shared: true` that are likely to contain the person's messages (e.g., a channel named `client--exampleco--*` for a ExampleCo employee).

2. **Fetch channel history and filter by user ID:**
   ```bash
   $SLACK -w <workspace> history "C0123456789" 100
   ```
   Then filter the JSON for messages from the target user ID. External users' messages include `user_profile.real_name` and `user_profile.display_name` inline, so you can identify them even without `user-lookup`.

3. **If you don't know the user ID yet**, scan the channel history for messages where `user_profile.display_name` or `user_profile.real_name` matches the person's name.

### Example

```bash
# 1. Find the shared ExampleCo channel
$SLACK -w <workspace> channels "public_channel,private_channel"
# Look for: C0123456789 client--exampleco--ai-tips-and-tricks (shared=True)

# 2. Fetch history and filter for the external user
$SLACK -w <workspace> history "C0123456789" 100
# Filter results for messages where user_profile.display_name == "Alice"
```

### Key gotcha

`user-lookup` returns workspace members plus only those Slack Connect users the client has already met in a message. For anyone else, rely on the `user_profile` embedded in their messages within shared channels.

---

## CRITICAL: ambiguous user names

When the user asks for messages "from [name]", there may be **multiple people matching that name** across workspace members and Slack Connect users. You MUST check for ambiguity before returning results.

### Mandatory disambiguation workflow

1. **Check workspace members** via `user-lookup` or `users` for name matches.
2. **Check Slack Connect channels** for external users with matching names. Look at shared channels whose names suggest a relevant organisation.
3. **If multiple matches exist**, use AskUserQuestion to clarify:

```
question: "I found multiple people matching that name. Which one?"
header: "Which [name]?"
options:
  - label: "[Full Name 1]"
    description: "[email or org] — workspace member"
  - label: "[Full Name 2]"
    description: "[email or org] — Slack Connect, #channel-name"
multiSelect: false
```

4. **If the user provides an email or organisation** (e.g., "alice@example.com"), use that to narrow down:
   - Check workspace member emails from the `users` command output
   - Check shared channel names for org references (e.g., `client--exampleco--*`)
   - Check `user_profile.team` in shared channel messages

### What NOT to do

- Do not assume the first `from:username` search result is the right person
- Do not ignore Slack Connect users just because they don't appear in search or user-lookup
- Do not return results from the wrong person without flagging the ambiguity

### What to do

- Always consider whether the target person might be a Slack Connect user, especially if an external email/org is mentioned
- When an org name is mentioned, look for shared channels named `client--[org]--*`
- If in doubt, ask — a wrong attribution is worse than an extra clarification step

---

## Commands

`$SLACK help` lists every command and flag. What the help text leaves out:

- `replies <channel> <thread_ts>` needs the thread parent's ts. Given a reply's ts, Slack returns only that reply, and the output carries a `warning` naming the parent ts. Take `thread_ts` from a link's query string, or use `read-link`.
- `history <channel> [limit] --with-replies` attaches each thread's replies to its parent, in one call.
- `--resolve` output has one shape: `{ok, _workspace, channel, messages: [...]}`, oldest first for `history`; `search` adds `channel` and `permalink` to each message. Without it, output is the Slack API's raw JSON.
- `mark-read` (channels and DMs) and `mark-thread-read` (followed threads) move the user's read cursor, so run them only with their go-ahead. Both move it forward only; marking a channel read leaves its threads' replies unread.
- `focus` pauses or resumes the user's notifications in one workspace and sets the "🔭 In focus mode" status, imitating Slack's focus mode, which has no API. Colleagues see both. `off` ends any current pause, including one set by the user's notification schedule, so don't run it during scheduled quiet hours unless asked.
- Always call the launcher. It uses `.venv/bin/python` when healthy and falls back to `python3`; if neither has `requests`, repair the virtualenv:

  ```bash
  python3 -m venv ~/.agents/skills/slack/.venv
  ~/.agents/skills/slack/.venv/bin/pip install -r ~/.agents/skills/slack/requirements.txt
  ```

## Workspaces and channels

- Each workspace has a cache at `slack-cache-<workspace>.json` in the data folder. Its `channel_names` map (`channel_id → name`) fills in as channels are read or listed; search it for a channel's ID before listing channels with `$SLACK -w <workspace> channels "public_channel,private_channel"`.
- `session-state.json` in the data folder records which workspace each channel ID belongs to; that is how channel commands infer `-w`.
- `channels [types]` takes a comma-separated list of `public_channel`, `private_channel`, `im` (DMs) and `mpim` (group DMs).
- When the user names a channel that could be in more than one workspace, ask which with AskUserQuestion, then pass `-w`.

## Workflow 1: send a message

If you have a writing-style skill, load it before drafting any message sent as the user.

Show the user the exact final text and send only once they have approved that text; an instruction to message someone is not approval.

`send` starts every message with `[agent] ` (unless the text already does), so readers can tell it from the user's own posts under their name; when the first line is a bold title, the prefix goes inside the bold (`*[agent] Title*`). Don't add it yourself. Drafts carry no prefix: the user sends them themselves.

### To a channel

1. Find the channel ID (check cache or list channels):
   ```bash
   $SLACK -w <workspace> channels "public_channel,private_channel"
   ```

2. Send the message:
   ```bash
   $SLACK -w <workspace> send "C0123456789" "Your message here"
   ```

### To a thread

1. Read the thread first (`read-link` on its URL, or `replies --resolve` with the parent ts), so the reply fits where the conversation has got to.
2. Send the reply with the parent's `ts`:
   ```bash
   $SLACK -w <workspace> send "C0123456789" "Thread reply" "1234567890.123456"
   ```

### To a DM

1. Find the DM channel ID (check cache or use @username):
   ```bash
   $SLACK -w <workspace> channels "im"
   ```

2. Send the message:
   ```bash
   $SLACK -w <workspace> send "D0123456789" "Your DM message"
   ```

### As an unsent draft

To leave a message in a channel's message box for the person to check and send themselves, write it to a `.txt` file in Slack mrkdwn and run `$SLACK -w <workspace> draft "C0123456789" message.txt --open`. It converts the text to rich_text (`*bold*`, `_italic_`, `~strike~`, `` `code` ``, `__underline__`, `<@U…>` mentions, `<url|text>` and bare links, `•` lists (numbered lines stay plain text, never Slack's numbered-list element), ```` ``` ```` fenced code blocks kept verbatim), saves the draft, prints the conversation's `url` and, with `--open`, opens it in Chrome, in the profile named by the workspace's `chrome_profile` in `config.json` (a directory such as `Profile 9`; plain `open` when unset). `--thread <parent ts>` puts the draft in that thread's reply box instead, and the `url` opens the thread. `--file <path>` (repeatable) attaches a file, such as a screenshot: it is uploaded unshared and appears in the draft, so nobody sees it until the person sends. It prints the `draft_id`, `channel` and any `thread_ts`; keep the id. A `.json` file of `rich_text` blocks is sent as it is. Use a draft for anything with @-mentions or formatting the person will paste: mentions stay real and underline survives, which neither a paste nor a sent mrkdwn message manages. Creating a draft posts nothing, attachments included, so it needs no send approval. For a DM, find the channel ID with `user-lookup` (the user's ID), then `channels im` (the `id` whose `user` matches). Make one draft per message: a second call for the same channel may clash with the draft already in its message box.

Drafts you make are yours to clean up; never leave stale ones for the user to delete:
- When a drafted message changes, edit it in place: `draft-edit <draft_id> <file> [--open]` replaces the text (same file formats as `draft`), keeping its conversation, thread and attachments. Don't make a second draft.
- Delete a superseded or abandoned draft with `draft-delete <draft_id>`.
- If the id is lost, `drafts [channel]` lists unsent drafts, newest first, with `draft_id`, `channel`, `thread_ts`, `updated` and the first 80 characters. It lists the user's own drafts too: touch only ones you made.

### Message formatting

Messages support Slack's mrkdwn format:
- `*bold*` for bold text
- `_italic_` for italic text
- `~strikethrough~` for strikethrough
- `` `code` `` for inline code
- `<@USER_ID>` for user mentions
- `<#CHANNEL_ID>` for channel mentions

### Tables in Slack

**IMPORTANT:** When including tables in Slack messages, always wrap them in triple backticks (code blocks). Slack uses a proportional font by default, so table columns won't align properly without monospace formatting.

**Correct format:**
```
*Summary Title*

\`\`\`
| Metric      | Score |
|-------------|-------|
| Quality     | 4.5/5 |
| Usefulness  | 4.2/5 |
\`\`\`

More text here...
```

**Why this matters:** Without code blocks, pipe characters and spacing won't align, making tables unreadable.

## Workflow 2: read recent activity

To check what the user missed or review recent activity:

1. Get recent messages, with their threads, from relevant channels:
   ```bash
   $SLACK -w <workspace> history "C0123456789" 50 --with-replies --resolve
   ```

2. For each channel of interest, summarize:
   - New messages since last check
   - Mentions of the user
   - Important threads that need attention

3. Search for messages mentioning the user:
   ```bash
   $SLACK -w <workspace> search "<@USER_ID>" 20
   ```

## Workflow 3: Slack activity digest

When the user asks for a "Slack digest" or "activity summary", use the AskUserQuestion tool to prompt for the time period:

### Time period selection

**Note:** Slack search only supports date-based queries (`after:YYYY-MM-DD`), not datetime. Options are designed around this limitation.

Present these options using AskUserQuestion:

| Option | Search Query | Notes |
|--------|-------------|-------|
| Today only | `from:@username after:YYYY-MM-DD` (today's date) | Messages from today |
| Since yesterday | `from:@username after:YYYY-MM-DD` (yesterday's date) | Yesterday + today |
| Last 7 days | `from:@username after:YYYY-MM-DD` (7 days ago) | Full week |
| Last calendar week | `from:@username after:YYYY-MM-DD before:YYYY-MM-DD` | Mon-Sun of previous week |

### Generating the digest

1. Search for user's sent messages in the selected period:
   ```bash
   $SLACK -w <workspace> search "from:@username after:2025-01-01" 100
   ```

2. Analyze messages and group by theme/conversation:
   - Identify main topics and projects discussed
   - Group related messages together
   - Note key people involved in each thread

3. **Build the message index** as you analyze:
   - Assign each referenced message a numbered ID (1.1, 1.2, 2.1, etc.)
   - First number = theme/section, second number = item within section
   - Store in `last-digest.json` (see format below)

4. Present digest using **numbered lists** (not bullets):

```markdown
## Your Slack activity digest: [Date Range]

### 1. [Theme/Project Name]

1.1. [Brief description of message/activity]
1.2. [Another message in this theme]
1.3. [Key decision or outcome]

People: [names involved]

### 2. [Theme/Project Name]

2.1. [Brief description]
2.2. [Another item]

People: [names involved]

### 3. Misc

3.1. [One-off message]
3.2. [Another minor item]

---

**Stats:** ~X messages | Channels: [list] | Busiest: [days]

💡 Say "open 1.2" to view any message in Slack
```

### Message index file

After generating the digest, write `~/.agents/skills/slack/last-digest.json`:

```json
{
  "generated": "2025-01-15T14:30:00Z",
  "period": "2025-01-13 to 2025-01-15",
  "workspace": "hartreeworks",
  "messages": {
    "1.1": {"channel": "C0123456789", "ts": "1736789012.123456"},
    "1.2": {"channel": "C0123456789", "ts": "1736789100.654321"},
    "2.1": {"channel": "D18U650RY", "ts": "1736801234.111111"},
    "3.1": {"channel": "C02ABC123", "ts": "1736812345.222222"}
  }
}
```

### Opening messages

When user says "open 1.2" or "open message 2.1":

1. Read the data folder's `config.json` to get `link_style` preference
2. Read `last-digest.json` and look up the message reference (includes workspace)
3. Generate permalink using the `permalink` command with the user's link_style:
   ```bash
   # For link_style: "app" (default)
   $SLACK -w <workspace> permalink "C0123456789" "1736789100.654321" "hartreeworks" "app"
   # Returns: https://hartreeworks.slack.com/archives/C0123456789/p1736789100654321

   # For link_style: "browser"
   $SLACK -w <workspace> permalink "C0123456789" "1736789100.654321" "hartreeworks" "browser"
   # Returns: https://hartreeworks.slack.com/messages/C0123456789/p1736789100654321
   ```
5. Open the link:
   ```bash
   open "<permalink>"
   ```

### Example AskUserQuestion call

```
Use AskUserQuestion with:
- question: "What time period would you like the Slack digest for?"
- header: "Time period"
- options:
  - label: "Today only"
    description: "Messages from today"
  - label: "Since yesterday"
    description: "Yesterday and today"
  - label: "Last 7 days"
    description: "Messages from the past week"
  - label: "Last calendar week"
    description: "Monday to Sunday of last week"
- multiSelect: false
```

## Workflow 4: get sent messages

To retrieve messages the user sent during a specific period:

1. Search for the user's messages:
   ```bash
   $SLACK -w <workspace> search "from:@username" 50
   ```

2. Filter by date range if specified:
   ```bash
   $SLACK -w <workspace> search "from:@username after:2025-01-01 before:2025-01-31" 100
   ```

3. Present results grouped by channel or date.

### Common search queries

| Query | Purpose |
|-------|---------|
| `from:@username` | All messages from user |
| `from:@username in:#channel` | Messages in specific channel |
| `from:@username after:YYYY-MM-DD` | Messages after date |
| `from:@username before:YYYY-MM-DD` | Messages before date |
| `from:@username has:link` | Messages containing links |
| `from:@username has:reaction` | Messages with reactions |

## Workflow 5: export messages archive

Export the user's sent messages with full thread context to a JSON file. Supports resume if interrupted.

### Basic export

```bash
SLACK=~/.agents/skills/slack/scripts/slack

# Export last 6 months of messages
$SLACK -w <workspace> export --from 2025-07-01 --to 2026-01-05 --output ~/slack-export.json

# Export for a specific workspace
$SLACK -w hartreeworks export --from 2025-07-01 --to 2026-01-05 --output ~/slack-export.json
```

### Resume an interrupted export

If the export is interrupted (Ctrl+C or error), resume from where it left off:

```bash
$SLACK -w <workspace> export --resume
```

### Check export status

```bash
$SLACK -w <workspace> export-status
```

### How it works

The export runs in three phases:

1. **Search phase**: Searches for all messages sent by the user in the date range using paginated search
2. **Thread fetch phase**: For each thread the user participated in, fetches the complete thread (including messages from others) for context
3. **Write phase**: Outputs a JSON file with all data

### Rate limiting

The export respects Slack's rate limits:
- ~45 search requests per minute (Tier 3)
- ~90 thread fetch requests per minute (Tier 4)
- Automatic backoff if rate limited

### Output format

The JSON export contains:

```json
{
  "metadata": {
    "workspace": "hartreeworks",
    "user": {"id": "U...", "username": "your.username"},
    "date_range": {"from": "2025-07-01", "to": "2026-01-05"},
    "exported_at": "2026-01-05T12:00:00Z",
    "stats": {
      "total_messages": 2847,
      "total_threads": 423,
      "standalone_messages": 312,
      "channels_count": 45
    }
  },
  "channels": {
    "C0123456789": {"id": "...", "name": "general", "type": "channel"}
  },
  "threads": [
    {
      "thread_id": "C0123456789:1735000000.111111",
      "channel_id": "C0123456789",
      "user_message_count": 3,
      "total_message_count": 12,
      "messages": [
        {"ts": "...", "user": "U...", "text": "...", "is_user_message": true}
      ]
    }
  ],
  "standalone_messages": [
    {"ts": "...", "channel_id": "...", "text": "...", "is_user_message": true}
  ]
}
```

### Scale estimates

For 6 months of active usage:
- ~3,000 messages → ~30 search API calls
- ~800 unique threads → ~800 thread API calls
- Total time: 15-30 minutes with rate limiting
- Output file: 10-20 MB

## Error handling

### Common issues

| Error | Cause | Solution |
|-------|-------|----------|
| `"ok": false` | API error | Check the `error` field in response |
| `invalid_auth` | Token expired | Refresh tokens: [references/setup.md](references/setup.md) |
| `channel_not_found` | Usually the wrong workspace | Retry with the right `-w`; the error's `hint` lists the workspaces |
| `not_in_channel` | User not a member | Join channel first |

## Known limitations

### No activity feed

The Slack Activity feed API is not available, so the skill cannot fetch the mentions-and-reactions feed, its badge count, or reactions to the user's messages. To find mentions, search for the user's ID: `<@USER_ID>`.

Read state is available. Slack keeps a read cursor (`last_read`) per conversation and per followed thread; every message newer than the cursor is unread:

- `counts`: each channel's and DM's `last_read`, latest timestamp and unread flag, in one call.
- `info <channel>`: one channel's `last_read`.
- `thread-view`: each followed thread's `last_read` and `latest_reply`. Threads the user doesn't follow have no cursor of their own here.

To list unread messages, take the flagged conversations and threads and fetch the messages after each cursor. `mark-read` and `mark-thread-read` move these cursors.
