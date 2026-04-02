---
name: slack
description: This skill should be used when the user asks to send Slack messages, check notifications, get a Slack digest, or search message history. Supports multiple workspaces.
---

# Slack Integration Skill

This skill provides workflows for interacting with Slack via a self-contained Python client. It supports multiple workspaces with contextual auto-selection, and four primary workflows: sending messages, reading notifications, generating activity digests, and retrieving sent message history.

## Setup

### First-Time Setup Flow

If `config.json` is missing when running a Slack command, walk the user through setup:

1. **Create config file:**
   ```bash
   cd ~/.agents/skills/slack
   cp config.example.json config.json
   ```

2. **Guide user to extract browser tokens:**
   - Open Slack in browser and log into the desired workspace
   - Open Developer Tools (F12 or Cmd+Option+I)
   - Get `xoxc_token` by running in Console tab:
     ```javascript
     JSON.parse(localStorage.localConfig_v2).teams[document.location.pathname.match(/^\/client\/([A-Z0-9]+)/)[1]].token
     ```
   - Get `xoxd_token` from Application tab:
     - Go to **Application** > **Cookies** > **https://app.slack.com**
     - Find the cookie named `d` and copy its value
   - Get `user_agent` by running in Console tab: `navigator.userAgent`
   - Note the workspace name from the URL (e.g., `hartreeworks` from `hartreeworks.slack.com`)

3. **Ask for preferences using AskUserQuestion:**
   ```
   question: "How would you like Slack links to open?"
   header: "Link style"
   options:
     - label: "Browser"
       description: "Open links in web browser"
     - label: "Native app"
       description: "Open links in Slack desktop app"
   multiSelect: false
   ```

4. **Add the workspace using the CLI:**
   ```bash
   SCRIPT=~/.agents/skills/slack/scripts/slack_client.py
   python3 $SCRIPT add-workspace "workspace-name" "xoxc-token" "xoxd-token" "user-agent"
   ```

5. **Test the connection:**
   ```bash
   python3 $SCRIPT auth
   ```

### Adding Additional Workspaces

To add another workspace, repeat the token extraction for the new workspace and run:
```bash
python3 $SCRIPT add-workspace "new-workspace" "xoxc-token" "xoxd-token"
```

The `user_agent` is optional when adding subsequent workspaces (defaults to first workspace's value).

### Config File Structure

`config.json` supports multiple workspaces:

```json
{
  "workspaces": {
    "hartreeworks": {
      "xoxc_token": "xoxc-...",
      "xoxd_token": "xoxd-...",
      "user_agent": "Mozilla/5.0..."
    },
    "another-workspace": {
      "xoxc_token": "xoxc-...",
      "xoxd_token": "xoxd-...",
      "user_agent": "Mozilla/5.0..."
    }
  },
  "default_workspace": "hartreeworks",
  "link_style": "app"
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `workspaces` | Yes | Object keyed by workspace name |
| `workspaces.*.xoxc_token` | Yes | Browser token from localStorage |
| `workspaces.*.xoxd_token` | Yes | Browser cookie token |
| `workspaces.*.user_agent` | Yes | Your browser's User-Agent string |
| `default_workspace` | Yes | Fallback workspace when none specified |
| `link_style` | Yes | `"app"` (native Slack) or `"browser"` (web browser) |

### Test the Connection

```bash
python3 ~/.agents/skills/slack/scripts/slack_client.py auth
```

## CRITICAL: User ID Resolution

**Slack API returns user IDs (e.g., `U02GYLM0A`), NOT display names.** You MUST resolve these IDs to names before presenting any Slack content to the user.

### Why This Matters

Guessing names from context is a **critical failure mode**. User IDs like `U7DTUK3U6` give no indication of who the person is. If you summarize a thread and attribute quotes to the wrong people, you're spreading misinformation.

### Mandatory Workflow

**Before summarizing ANY Slack content containing user IDs (threads, messages, search results):**

1. **Get the user lookup for the workspace:**
   ```bash
   python3 $SCRIPT -w <workspace> user-lookup
   ```
   This returns a JSON mapping of user_id → display_name.

2. **Resolve all user IDs** in the content using the lookup before presenting to the user.

That's it - just two steps. The `user-lookup` command handles caching automatically:
- **First time:** Fetches users from Slack API (may take a few seconds)
- **Cache stale (>14 days):** Returns cached data immediately, refreshes in background
- **Cache fresh:** Returns cached data instantly

### User Resolution Commands

| Command | Purpose |
|---------|---------|
| `user-lookup` | Get user_id → display_name mapping (auto-fetches if empty, background-refreshes if stale) |
| `fetch-users` | Force refresh the user cache from Slack API |

### Example

```bash
# 1. Get user lookup FIRST (handles caching automatically)
python3 $SCRIPT -w 80000hours user-lookup
# Returns: {"U02GYLM0A": "Benjamin Todd", "U7DTUK3U6": "Niel", ...}
# (First time: fetches from API. Later: uses cache, refreshes in background if >14 days old)

# 2. Fetch a thread
python3 $SCRIPT -w 80000hours replies "C039MDQ91" "1767837883.421009"
# Returns messages with user IDs like "U02GYLM0A", "U7DTUK3U6"

# 3. Now you can correctly attribute: "Benjamin Todd said..." not "User U02GYLM0A said..."
```

### What NOT To Do

- ❌ Guess names based on context clues in messages
- ❌ Assume you know who someone is from their writing style
- ❌ Present a summary with user IDs instead of names
- ❌ Skip the lookup step because "it's just one message"

### What To Do

- ✅ Always run `user-lookup` before summarizing Slack content (it handles caching automatically)
- ✅ If a user ID isn't in the lookup, run `fetch-users` to force refresh, or show the ID with a note

---

## CRITICAL: Slack Connect (External) Users

**Slack's `from:username` search does NOT work for Slack Connect (external) users.** These are people who belong to another workspace but participate in shared channels on your workspace. Their user IDs belong to the external workspace, so `search "from:username"` returns zero results even though they've posted messages.

### How to Identify Slack Connect Users

Slack Connect users appear on shared channels. When listing channels, shared channels have `is_shared: true` or `is_ext_shared: true`. External users' messages in channel history include an embedded `user_profile` with fields like `real_name`, `display_name`, and `team` (their home workspace ID, different from yours).

### Finding Messages from Slack Connect Users

Since search won't work, use channel history instead:

1. **Identify the relevant shared channel(s):**
   ```bash
   python3 $SCRIPT channels "public_channel,private_channel"
   ```
   Look for channels with `is_shared: true` or `is_ext_shared: true` that are likely to contain the person's messages (e.g., a channel named `client--forethought--*` for a Forethought employee).

2. **Fetch channel history and filter by user ID:**
   ```bash
   python3 $SCRIPT history "C084G9PKYGN" 100
   ```
   Then filter the JSON for messages from the target user ID. External users' messages include `user_profile.real_name` and `user_profile.display_name` inline, so you can identify them even without `user-lookup`.

3. **If you don't know the user ID yet**, scan the channel history for messages where `user_profile.display_name` or `user_profile.real_name` matches the person's name.

### Example

```bash
# 1. Find the shared Forethought channel
python3 $SCRIPT channels "public_channel,private_channel"
# Look for: C084G9PKYGN client--forethought--ai-tips-and-tricks (shared=True)

# 2. Fetch history and filter for the external user
python3 $SCRIPT history "C084G9PKYGN" 100
# Filter results for messages where user_profile.display_name == "Fin"
```

### Key Gotcha

The `user-lookup` command only returns users who are **members of your workspace**. Slack Connect users will NOT appear in the lookup. Instead, rely on the `user_profile` embedded in their messages within shared channels.

---

## CRITICAL: Ambiguous User Names

When the user asks for messages "from [name]", there may be **multiple people matching that name** across workspace members and Slack Connect users. You MUST check for ambiguity before returning results.

### Mandatory Disambiguation Workflow

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

4. **If the user provides an email or organisation** (e.g., "fin@forethought.org"), use that to narrow down:
   - Check workspace member emails from the `users` command output
   - Check shared channel names for org references (e.g., `client--forethought--*`)
   - Check `user_profile.team` in shared channel messages

### What NOT To Do

- Do not assume the first `from:username` search result is the right person
- Do not ignore Slack Connect users just because they don't appear in search or user-lookup
- Do not return results from the wrong person without flagging the ambiguity

### What To Do

- Always consider whether the target person might be a Slack Connect user, especially if an external email/org is mentioned
- When an org name is mentioned, look for shared channels named `client--[org]--*`
- If in doubt, ask — a wrong attribution is worse than an extra clarification step

---

## Python Client Commands

The `scripts/slack_client.py` script provides these commands. All commands support an optional `-w <workspace>` flag to specify the workspace.

### Core Commands

| Command | Arguments | Purpose |
|---------|-----------|---------|
| `auth` | - | Test authentication, get user info |
| `channels` | [types] | List channels (default: all types) |
| `users` | - | List all workspace users |
| `user-lookup` | - | Get user_id → display_name mapping (auto-fetches/refreshes as needed) |
| `fetch-users` | - | Force refresh user cache from Slack API |
| `history` | channel_id [limit] | Get message history |
| `replies` | channel_id thread_ts | Get thread replies |
| `search` | query [count] | Search messages |
| `send` | channel_id text [thread_ts] | Send a message |
| `permalink` | channel_id message_ts [workspace] | Get message permalink |

### Workspace Management Commands

| Command | Arguments | Purpose |
|---------|-----------|---------|
| `workspaces` | - | List configured workspaces |
| `switch` | workspace_name | Set active workspace |
| `add-workspace` | name xoxc xoxd [user_agent] | Add a new workspace |

### Example Usage

```bash
SCRIPT=~/.agents/skills/slack/scripts/slack_client.py

# List configured workspaces
python3 $SCRIPT workspaces

# Switch active workspace
python3 $SCRIPT switch acme-corp

# Use specific workspace for one command
python3 $SCRIPT -w hartreeworks channels

# List public channels
python3 $SCRIPT channels "public_channel"

# Search for messages
python3 $SCRIPT search "from:@username after:2025-01-01" 50

# Send a message
python3 $SCRIPT send "C0123456789" "Hello world!"

# Send a thread reply
python3 $SCRIPT send "C0123456789" "Thread reply" "1234567890.123456"

# Get channel history
python3 $SCRIPT history "C0123456789" 20

# Get message permalink
python3 $SCRIPT permalink "C0123456789" "1234567890.123456"
```

## Workspace Selection

The skill supports multiple workspaces with contextual auto-selection.

### Selection Priority

When a command runs without `-w`, the workspace is selected in this order:

1. **Explicit flag**: `-w workspace-name` always wins
2. **Channel context**: If operating on a channel known to belong to a workspace
3. **Recent activity**: Active workspace from the last 10 minutes
4. **Default workspace**: Configured in `config.json`
5. **First workspace**: If nothing else matches

### Handling Ambiguous Workspace

When the workspace is ambiguous (e.g., user asks to "send a message to #general" but multiple workspaces have a #general channel), use AskUserQuestion:

```
question: "Which Slack workspace should I use?"
header: "Workspace"
options:
  - label: "hartreeworks"
    description: "hartreeworks.slack.com"
  - label: "acme-corp"
    description: "acme-corp.slack.com"
multiSelect: false
```

Then pass the selected workspace using the `-w` flag.

### Session State

The skill tracks workspace context in `session-state.json`:
- `active_workspace`: Most recently used workspace
- `workspace_channel_map`: Maps channel IDs to their workspaces

This enables automatic workspace inference when operating on previously-seen channels.

## Performance Cache

Each workspace has its own cache file (`data/slack-cache-{workspace}.json`) storing frequently-used IDs.

### Using the Cache

Before making API calls to look up users or channels:
1. Read `data/slack-cache-{workspace}.json` for the current workspace
2. Check if the needed ID is already cached
3. If found, use the cached value directly
4. If not found, make the API call, then update the cache

### Cache Structure

```json
{
  "user": {"id": "...", "username": "...", "display_name": "..."},
  "self_dm_channel": "D...",
  "workspace": "hartreeworks",
  "frequent_contacts": {"username": {"id": "...", "display_name": "..."}},
  "channels": {"#channel-name": "C..."}
}
```

### Updating the Cache

After successful lookups, add new entries:
- New user lookups → add to `frequent_contacts`
- New channel lookups → add to `channels`
- Update `last_updated` date

On lookup errors (user not found, channel not found), the cached entry may be stale - remove it and retry.

## Workflow 1: Send a Message

### To a Channel

1. Find the channel ID (check cache or list channels):
   ```bash
   python3 $SCRIPT channels "public_channel,private_channel"
   ```

2. Send the message:
   ```bash
   python3 $SCRIPT send "C0123456789" "Your message here"
   ```

### To a Thread

1. Get the thread's parent message timestamp (`ts`)
2. Send the reply:
   ```bash
   python3 $SCRIPT send "C0123456789" "Thread reply" "1234567890.123456"
   ```

### To a DM

1. Find the DM channel ID (check cache or use @username):
   ```bash
   python3 $SCRIPT channels "im"
   ```

2. Send the message:
   ```bash
   python3 $SCRIPT send "D0123456789" "Your DM message"
   ```

### Message Formatting

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

## Workflow 2: Read Recent Activity

To check what the user missed or review recent activity:

1. Get recent messages from relevant channels:
   ```bash
   python3 $SCRIPT history "C0123456789" 50
   ```

2. For each channel of interest, summarize:
   - New messages since last check
   - Mentions of the user
   - Important threads that need attention

3. Search for messages mentioning the user:
   ```bash
   python3 $SCRIPT search "<@USER_ID>" 20
   ```

## Workflow 3: Slack Activity Digest

When the user asks for a "Slack digest" or "activity summary", use the AskUserQuestion tool to prompt for the time period:

### Time Period Selection

**Note:** Slack search only supports date-based queries (`after:YYYY-MM-DD`), not datetime. Options are designed around this limitation.

Present these options using AskUserQuestion:

| Option | Search Query | Notes |
|--------|-------------|-------|
| Today only | `from:@username after:YYYY-MM-DD` (today's date) | Messages from today |
| Since yesterday | `from:@username after:YYYY-MM-DD` (yesterday's date) | Yesterday + today |
| Last 7 days | `from:@username after:YYYY-MM-DD` (7 days ago) | Full week |
| Last calendar week | `from:@username after:YYYY-MM-DD before:YYYY-MM-DD` | Mon-Sun of previous week |

### Generating the Digest

1. Search for user's sent messages in the selected period:
   ```bash
   python3 $SCRIPT search "from:@username after:2025-01-01" 100
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
## Your Slack Activity Digest: [Date Range]

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

### Message Index File

After generating the digest, write `~/.agents/skills/slack/last-digest.json`:

```json
{
  "generated": "2025-01-15T14:30:00Z",
  "period": "2025-01-13 to 2025-01-15",
  "workspace": "hartreeworks",
  "messages": {
    "1.1": {"channel": "C04AFNMCNFP", "ts": "1736789012.123456"},
    "1.2": {"channel": "C04AFNMCNFP", "ts": "1736789100.654321"},
    "2.1": {"channel": "D18U650RY", "ts": "1736801234.111111"},
    "3.1": {"channel": "C02ABC123", "ts": "1736812345.222222"}
  }
}
```

### Opening Messages

When user says "open 1.2" or "open message 2.1":

1. Read `config.json` to get `link_style` preference
2. Read `last-digest.json` and look up the message reference (includes workspace)
3. Generate permalink using the `permalink` command with the user's link_style:
   ```bash
   # For link_style: "app" (default)
   python3 $SCRIPT permalink "C04AFNMCNFP" "1736789100.654321" "hartreeworks" "app"
   # Returns: https://hartreeworks.slack.com/archives/C04AFNMCNFP/p1736789100654321

   # For link_style: "browser"
   python3 $SCRIPT permalink "C04AFNMCNFP" "1736789100.654321" "hartreeworks" "browser"
   # Returns: https://hartreeworks.slack.com/messages/C04AFNMCNFP/p1736789100654321
   ```
5. Open the link:
   ```bash
   open "<permalink>"
   ```

### Example AskUserQuestion Call

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

## Workflow 4: Get Sent Messages

To retrieve messages the user sent during a specific period:

1. Search for the user's messages:
   ```bash
   python3 $SCRIPT search "from:@username" 50
   ```

2. Filter by date range if specified:
   ```bash
   python3 $SCRIPT search "from:@username after:2025-01-01 before:2025-01-31" 100
   ```

3. Present results grouped by channel or date.

### Common Search Queries

| Query | Purpose |
|-------|---------|
| `from:@username` | All messages from user |
| `from:@username in:#channel` | Messages in specific channel |
| `from:@username after:YYYY-MM-DD` | Messages after date |
| `from:@username before:YYYY-MM-DD` | Messages before date |
| `from:@username has:link` | Messages containing links |
| `from:@username has:reaction` | Messages with reactions |

## Workflow 5: Export messages archive

Export the user's sent messages with full thread context to a JSON file. Supports resume if interrupted.

### Basic export

```bash
SCRIPT=~/.agents/skills/slack/scripts/slack_client.py

# Export last 6 months of messages
python3 $SCRIPT export --from 2025-07-01 --to 2026-01-05 --output ~/slack-export.json

# Export for a specific workspace
python3 $SCRIPT -w 80000hours export --from 2025-07-01 --to 2026-01-05 --output ~/slack-export.json
```

### Resume an interrupted export

If the export is interrupted (Ctrl+C or error), resume from where it left off:

```bash
python3 $SCRIPT export --resume
```

### Check export status

```bash
python3 $SCRIPT export-status
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
    "workspace": "80000hours",
    "user": {"id": "U...", "username": "pete.hartree"},
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

## Working with Threads

### Fetch Thread Replies

```bash
python3 $SCRIPT replies "C0123456789" "1234567890.123456"
```

### Reply to a Thread

```bash
python3 $SCRIPT send "C0123456789" "Your reply" "1234567890.123456"
```

## Channel Discovery

### List All Channels

```bash
python3 $SCRIPT channels "public_channel,private_channel"
```

### List DM Conversations

```bash
python3 $SCRIPT channels "im,mpim"
```

### List All Users

```bash
python3 $SCRIPT users
```

## Error Handling

### Common Issues

| Error | Cause | Solution |
|-------|-------|----------|
| `"ok": false` | API error | Check the `error` field in response |
| `invalid_auth` | Token expired | Extract fresh tokens from browser |
| `channel_not_found` | Invalid channel ID | List channels to verify |
| `not_in_channel` | User not a member | Join channel first |

### Token Expiry

Browser tokens (xoxc/xoxd) expire periodically. If requests fail with auth errors:
1. Extract fresh tokens from browser
2. Update `config.json`
3. Test with `python3 $SCRIPT auth`

## Known Limitations

### No Activity Feed / Unread Notifications

The Slack Activity feed API is not available. Cannot directly fetch:

- Unread notification count
- Reactions to user's messages

Workarounds:
- Search for messages mentioning the user's ID: `<@USER_ID>`
- Search for recent activity in user's DMs
- Check specific channels for recent messages

## Additional Resources

### Reference Files

For detailed API response formats:
- **`references/tools-reference.md`** - Complete API documentation

### Token Setup

For extracting browser tokens:
- **`references/setup-guide.md`** - Step-by-step token extraction guide
