# Slack skill setup

Use this when `config.json` is missing, a workspace needs adding, or tokens have expired.

## First-time setup flow

If `config.json` is missing from the data folder (`~/.config/hartreeworks-skills/slack/`) when running a Slack command, walk the user through setup:

1. **Create config file:**
   ```bash
   mkdir -p ~/.config/hartreeworks-skills/slack
   cp <this skill's folder>/config.example.json ~/.config/hartreeworks-skills/slack/config.json
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
   SLACK=~/.agents/skills/slack/scripts/slack
   $SLACK add-workspace "workspace-name" "xoxc-token" "xoxd-token" "user-agent"
   ```

5. **Test the connection:**
   ```bash
   $SLACK -w workspace-name auth
   ```

## Adding additional workspaces

To add another workspace, repeat the token extraction for the new workspace and run:
```bash
SLACK=~/.agents/skills/slack/scripts/slack
$SLACK add-workspace "new-workspace" "xoxc-token" "xoxd-token"
```

The `user_agent` is optional when adding subsequent workspaces (defaults to first workspace's value).

## Config file structure

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
  "link_style": "app"
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `workspaces` | Yes | Object keyed by workspace name |
| `workspaces.*.xoxc_token` | Yes | Browser token from localStorage |
| `workspaces.*.xoxd_token` | Yes | Browser cookie token |
| `workspaces.*.user_agent` | Yes | Your browser's User-Agent string |
| `link_style` | Yes | `"app"` (native Slack) or `"browser"` (web browser) |

## Test the connection

```bash
~/.agents/skills/slack/scripts/slack -w <workspace> auth
```

## Token expiry

Browser tokens (xoxc/xoxd) expire periodically. When requests fail with `invalid_auth`, extract fresh tokens as above, update `config.json`, and test with `$SLACK -w <workspace> auth`.
