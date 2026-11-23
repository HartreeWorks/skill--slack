#!/usr/bin/env python3
"""
Slack client using browser tokens (xoxc/xoxd) for stealth mode.
Supports multiple workspaces; each call names its workspace with -w, or takes it
from a Slack link or a previously seen channel ID.
"""

import requests
import json
import os
import sys
import time
import uuid
import re
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import parse_qs, urlparse

# Cache staleness threshold
USER_CACHE_STALE_DAYS = 14

# User data lives outside the skill folder, which skill updates delete and recreate.
# Same location as scripts/data-dir.sh prints.
DATA_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "hartreeworks-skills" / "slack"
CONFIG_PATH = DATA_DIR / "config.json"
SESSION_STATE_PATH = DATA_DIR / "session-state.json"
DIGEST_CONFIG_PATH = DATA_DIR / "digest-config.json"


def slack_ts_value(timestamp: str) -> int:
    """Convert Slack's fixed six-decimal timestamp to an exactly comparable integer."""
    return int(timestamp.replace(".", ""))


# ==================== Rate Limiter ====================

class RateLimiter:
    """
    Manages API call pacing for Slack's tiered rate limits.

    Tier 3 (search.messages): ~50 req/min
    Tier 4 (conversations.replies): ~100 req/min

    Uses conservative limits to avoid ever hitting actual rate limits.
    """

    TIER_3_LIMIT = 35  # search - conservative to never hit 50
    TIER_4_LIMIT = 70  # replies - conservative to never hit 100

    def __init__(self):
        self.tier3_calls = []  # timestamps of search calls
        self.tier4_calls = []  # timestamps of replies calls
        self.backoff_until = None
        self.consecutive_429s = 0

    def _prune_old_calls(self, calls: list, window_seconds: int = 60) -> list:
        """Remove calls older than the window."""
        cutoff = datetime.now() - timedelta(seconds=window_seconds)
        return [t for t in calls if t > cutoff]

    def _handle_backoff(self):
        """Sleep if we're in a backoff period."""
        if self.backoff_until and datetime.now() < self.backoff_until:
            sleep_time = (self.backoff_until - datetime.now()).total_seconds()
            if sleep_time > 0:
                print(f"  Rate limit backoff: sleeping {sleep_time:.1f}s", file=sys.stderr)
                time.sleep(sleep_time)
            self.backoff_until = None

    def wait_for_tier3(self):
        """Wait if needed before making a Tier 3 call (search)."""
        self._handle_backoff()
        self.tier3_calls = self._prune_old_calls(self.tier3_calls)

        if len(self.tier3_calls) >= self.TIER_3_LIMIT:
            oldest = min(self.tier3_calls)
            sleep_time = 60 - (datetime.now() - oldest).total_seconds() + 1
            if sleep_time > 0:
                print(f"  Tier 3 limit: sleeping {sleep_time:.1f}s", file=sys.stderr)
                time.sleep(sleep_time)
            self.tier3_calls = self._prune_old_calls(self.tier3_calls)

        self.tier3_calls.append(datetime.now())

    def wait_for_tier4(self):
        """Wait if needed before making a Tier 4 call (replies)."""
        self._handle_backoff()
        self.tier4_calls = self._prune_old_calls(self.tier4_calls)

        if len(self.tier4_calls) >= self.TIER_4_LIMIT:
            oldest = min(self.tier4_calls)
            sleep_time = 60 - (datetime.now() - oldest).total_seconds() + 1
            if sleep_time > 0:
                print(f"  Tier 4 limit: sleeping {sleep_time:.1f}s", file=sys.stderr)
                time.sleep(sleep_time)
            self.tier4_calls = self._prune_old_calls(self.tier4_calls)

        self.tier4_calls.append(datetime.now())

    def handle_rate_limit_response(self, retry_after: int = None):
        """Called when we receive a 429 or rate_limited error."""
        self.consecutive_429s += 1

        # Exponential backoff: 30s, 60s, 120s, 240s, max 5min
        if retry_after:
            wait_seconds = retry_after
        else:
            wait_seconds = min(30 * (2 ** (self.consecutive_429s - 1)), 300)

        self.backoff_until = datetime.now() + timedelta(seconds=wait_seconds)
        print(f"  Rate limited! Backing off {wait_seconds}s", file=sys.stderr)

    def reset_backoff(self):
        """Called after a successful request."""
        self.consecutive_429s = 0


class SlackClient:
    BASE_URL = "https://slack.com/api"

    def __init__(self, xoxc_token: str, xoxd_token: str, user_agent: str = None):
        self.token = xoxc_token
        self.cookies = {"d": xoxd_token}
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/143.0.0.0 Safari/537.36"
        )
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept-Language": "en-NZ,en-AU;q=0.9,en;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
        })

    def _post(self, endpoint: str, data: dict = None) -> dict:
        """Make authenticated POST request with stealth fields.

        Retries transparently on HTTP 429 / "ratelimited" errors, honouring
        Retry-After. Each CLI invocation is its own process, so per-request
        retry is the only rate-limit defence that works across invocations.
        """
        payload = {
            "token": self.token,
            "_x_reason": "api-call",
            "_x_mode": "online",
            "_x_sonic": "true",
            "_x_app_name": "client",
            **(data or {})
        }

        last_result = {"ok": False, "error": "ratelimited"}
        for attempt in range(6):
            response = self.session.post(
                f"{self.BASE_URL}/{endpoint}",
                data=payload,
                cookies=self.cookies
            )
            retry_after = response.headers.get("Retry-After")
            if response.status_code == 429:
                wait = int(retry_after) if retry_after and retry_after.isdigit() else 15 * (attempt + 1)
                print(f"  {endpoint}: HTTP 429, retrying in {wait}s", file=sys.stderr)
                time.sleep(min(wait, 120))
                continue
            try:
                result = response.json()
            except ValueError:
                return {"ok": False, "error": f"Non-JSON response (status {response.status_code})"}
            if result.get("ok") is False and result.get("error") == "ratelimited":
                wait = int(retry_after) if retry_after and retry_after.isdigit() else 15 * (attempt + 1)
                print(f"  {endpoint}: ratelimited, retrying in {wait}s", file=sys.stderr)
                last_result = result
                time.sleep(min(wait, 120))
                continue
            return result
        return last_result

    # ==================== Core Functions ====================

    def channels_list(self, types: str = "public_channel,private_channel,im,mpim",
                      limit: int = 200) -> dict:
        """List channels, DMs, and group DMs. Paginates so callers see every page."""
        channels = []
        cursor = None
        result = {}
        for _ in range(25):
            data = {
                "types": types,
                "limit": str(limit),
                "exclude_archived": "true"
            }
            if cursor:
                data["cursor"] = cursor
            result = self._post("conversations.list", data)
            if not result.get("ok"):
                return result
            channels.extend(result.get("channels", []))
            cursor = (result.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break
        result["channels"] = channels
        return result

    def conversations_history(self, channel: str, limit: int = 100, oldest: str = None,
                              latest: str = None, cursor: str = None,
                              inclusive: bool = True) -> dict:
        """Get message history from a channel or DM, optionally bounded and paginated.

        With only `oldest` set, Slack returns the messages immediately after it.
        """
        data = {"channel": channel, "limit": str(limit)}
        if oldest:
            data["oldest"] = oldest
        if latest:
            data["latest"] = latest
            data["inclusive"] = "true" if inclusive else "false"
        if cursor:
            data["cursor"] = cursor
        return self._post("conversations.history", data)

    def client_counts(self) -> dict:
        """Get per-conversation read cursors, latest timestamps and unread flags in one call."""
        return self._post("client.counts", {})

    def download_file(self, url: str, output_path: str, max_bytes: int = 4 * 1024 * 1024) -> dict:
        """Download an authenticated Slack image file (url_private) to output_path."""
        if not url.startswith("https://files.slack.com/"):
            return {"ok": False, "error": "Only files.slack.com URLs are supported"}
        response = self.session.get(
            url,
            cookies=self.cookies,
            headers={"Content-Type": None},
            stream=True,
            timeout=60,
        )
        content_type = response.headers.get("Content-Type", "")
        if response.status_code != 200 or not content_type.startswith("image/"):
            return {
                "ok": False,
                "error": f"Unexpected response: status={response.status_code} type={content_type}",
            }
        total = 0
        try:
            with open(output_path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=65536):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f"File exceeds {max_bytes} byte cap")
                    handle.write(chunk)
        except ValueError as exc:
            Path(output_path).unlink(missing_ok=True)
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "path": output_path, "bytes": total, "content_type": content_type}

    def conversations_info(self, channel: str) -> dict:
        """Get conversation metadata, including the authenticated user's read cursor."""
        return self._post("conversations.info", {"channel": channel})

    def conversations_replies(self, channel: str, thread_ts: str) -> dict:
        """Get every message in a thread, parent first, following pagination.

        Given a reply's ts instead of the parent's, Slack returns only that reply.
        """
        messages = []
        seen = set()
        cursor = None
        result = {}
        for _ in range(25):
            data = {"channel": channel, "ts": thread_ts, "limit": "200"}
            if cursor:
                data["cursor"] = cursor
            result = self._post("conversations.replies", data)
            if not result.get("ok"):
                return result
            for message in result.get("messages", []):
                if message.get("ts") not in seen:
                    seen.add(message.get("ts"))
                    messages.append(message)
            cursor = (result.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break
        result["messages"] = messages
        result["has_more"] = False
        return result

    def thread_view(self, limit: int = 20, current_ts: str = None) -> dict:
        """Read the Threads view (subscribed threads, newest reply first).

        Internal web-client endpoint; read-only. Each thread carries
        `root_msg` (with the caller's `last_read` and `latest_reply`) and
        `latest_replies` (the newest few replies only). Page older threads by
        passing the previous page's oldest `latest_reply` as `current_ts`.
        """
        data = {"limit": str(limit), "org_wide_aware": "true"}
        if current_ts:
            data["current_ts"] = current_ts
        return self._post("subscriptions.thread.getView", data)

    def saved_list(self) -> dict:
        """Read the Later / To-dos list, following pagination.

        Internal web-client endpoint; read-only. Each saved item carries
        `item_id` (the channel), `ts`, `state` (`in_progress`, `completed` or
        `archived`), `date_due` and `date_snoozed_until` (epoch seconds, 0 if unset).
        """
        items, cursor, result = [], None, {}
        for _ in range(25):
            data = {"limit": "49"}
            if cursor:
                data["cursor"] = cursor
            result = self._post("saved.list", data)
            if not result.get("ok"):
                return result
            items.extend(result.get("saved_items", []))
            cursor = (result.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break
        result["saved_items"] = items
        return result

    def conversations_mark(self, channel: str, message_ts: str) -> dict:
        """Move the authenticated user's read cursor through a channel timestamp."""
        return self._post("conversations.mark", {
            "channel": channel,
            "ts": message_ts,
        })

    def thread_mark(self, channel: str, thread_ts: str, message_ts: str) -> dict:
        """Move the authenticated user's read cursor in one thread through a reply timestamp.

        Internal web-client endpoint (subscriptions.thread.mark).
        """
        return self._post("subscriptions.thread.mark", {
            "channel": channel,
            "thread_ts": thread_ts,
            "ts": message_ts,
            "read": "1",
        })

    def search_messages(self, query: str, count: int = 20, sort: str = "timestamp") -> dict:
        """Search for messages. Supports Slack search modifiers like from:, in:, after:, etc."""
        return self._post("search.messages", {
            "query": query,
            "count": str(count),
            "sort": sort,
            "sort_dir": "desc"
        })

    def search_messages_paginated(self, query: str, page: int = 1, count: int = 100,
                                   sort: str = "timestamp") -> dict:
        """
        Search messages with pagination support.

        Args:
            query: Slack search query (e.g., "from:@username after:2025-01-01")
            page: Page number (1-indexed)
            count: Results per page (max 100)
            sort: Sort order ('timestamp' or 'score')

        Returns:
            API response with 'messages.matches', 'messages.paging', etc.
        """
        return self._post("search.messages", {
            "query": query,
            "count": str(min(count, 100)),
            "page": str(page),
            "sort": sort,
            "sort_dir": "asc"  # Oldest first for consistent pagination
        })

    def post_message(self, channel: str, text: str, thread_ts: str = None) -> dict:
        """Send a message to a channel, DM, or thread."""
        data = {
            "channel": channel,
            "text": text,
            "unfurl_links": "true",
            "unfurl_media": "true"
        }
        if thread_ts:
            data["thread_ts"] = thread_ts
        return self._post("chat.postMessage", data)

    def upload_private_file(self, path: str) -> dict:
        """Upload a file without sharing it anywhere (files.getUploadURLExternal, then
        files.completeUploadExternal with no channel), so it can be attached to a draft. Returns
        {"ok": True, "file_id": ...} or the failing call's result."""
        import mimetypes
        f = Path(path)
        data = f.read_bytes()
        got = self._post("files.getUploadURLExternal", {"filename": f.name, "length": str(len(data))})
        if not got.get("ok"):
            return got
        mime = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        r = requests.post(got["upload_url"], files={"file": (f.name, data, mime)},
                          headers={"User-Agent": self.user_agent}, cookies=self.cookies, timeout=120)
        if r.status_code != 200:
            return {"ok": False, "error": f"upload failed (HTTP {r.status_code})", "file": f.name}
        done = self._post("files.completeUploadExternal",
                          {"files": json.dumps([{"id": got["file_id"], "title": f.name}])})
        if not done.get("ok"):
            return done
        return {"ok": True, "file_id": got["file_id"]}

    def delete_file(self, file_id: str) -> dict:
        return self._post("files.delete", {"file": file_id})

    def create_draft(self, channel: str, blocks: list, thread_ts: str = None, file_ids: list = None) -> dict:
        """Save Block Kit rich_text blocks as an unsent draft in a channel's message box, or in a
        thread's reply box with thread_ts, as the web client does (drafts.create). Nothing is posted:
        the person sends it from Slack."""
        import uuid
        dest = {"channel_id": channel, **({"thread_ts": thread_ts, "broadcast": False} if thread_ts else {})}
        return self._post("drafts.create", {
            "blocks": json.dumps(blocks), "client_msg_id": str(uuid.uuid4()),
            "destinations": json.dumps([dest]), "file_ids": json.dumps(file_ids or []), "is_from_composer": "false"})

    def delete_draft(self, draft_id: str) -> dict:
        """Delete an unsent draft (drafts.delete wants the client's current time, not the draft's)."""
        return self._post("drafts.delete", {"draft_id": draft_id, "client_last_updated_ts": f"{time.time():.6f}"})

    def list_drafts(self) -> dict:
        """The user's unsent drafts, newest first (drafts.list). is_active drops sent and deleted
        ones; without a limit Slack returns only 10."""
        return self._post("drafts.list", {"is_active": "true", "limit": "1000"})

    def update_draft(self, draft: dict, blocks: list) -> dict:
        """Replace an unsent draft's text in place, keeping its destination and attachments.
        `draft` is its entry from list_drafts. Like drafts.delete, drafts.update wants the client's
        current time, not the draft's (that gives draft_has_conflict); never earlier than the draft's."""
        dests = [{k: v for k, v in d.items() if k in ("channel_id", "thread_ts", "broadcast")}
                 for d in draft.get("destinations", [])]
        now = max(time.time(), float(draft.get("last_updated_ts") or 0) + 0.001)
        return self._post("drafts.update", {
            "draft_id": draft["id"], "client_last_updated_ts": f"{now:.6f}", "blocks": json.dumps(blocks),
            "destinations": json.dumps(dests), "file_ids": json.dumps(draft.get("file_ids") or []),
            "is_from_composer": "false"})

    def react(self, channel: str, timestamp: str, name: str, remove: bool = False) -> dict:
        """Add (or remove) an emoji reaction on one message."""
        endpoint = "reactions.remove" if remove else "reactions.add"
        return self._post(endpoint, {"channel": channel, "timestamp": timestamp, "name": name})

    def dnd_info(self) -> dict:
        """Get the user's Do Not Disturb state: any manual pause and the notification schedule."""
        return self._post("dnd.info")

    def dnd_set_snooze(self, minutes: int) -> dict:
        """Pause the user's notifications for a number of minutes."""
        return self._post("dnd.setSnooze", {"num_minutes": str(minutes)})

    def dnd_end_snooze(self) -> dict:
        """Resume notifications paused with dnd_set_snooze."""
        return self._post("dnd.endSnooze")

    def profile_get(self) -> dict:
        """Get the authenticated user's profile, including their status."""
        return self._post("users.profile.get")

    def set_status(self, text: str, emoji: str, expiration: int = 0) -> dict:
        """Set the user's status; empty text and emoji clear it."""
        return self._post("users.profile.set", {"profile": json.dumps({
            "status_text": text,
            "status_emoji": emoji,
            "status_expiration": expiration,
        })})

    def users_info(self, user_id: str) -> dict:
        """Get one user's profile, including Slack Connect and deactivated users."""
        return self._post("users.info", {"user": user_id})

    def users_list(self, limit: int = 200) -> dict:
        """List all users in the workspace."""
        return self._post("users.list", {"limit": str(limit)})

    def auth_test(self) -> dict:
        """Test authentication and get current user info."""
        return self._post("auth.test")

    def get_permalink(self, channel: str, message_ts: str, workspace: str = None,
                      link_style: str = "app") -> str:
        """
        Generate a permalink for a message.

        Args:
            channel: Channel ID (e.g., C0123456789)
            message_ts: Message timestamp (e.g., 1734567890.123456)
            workspace: Workspace name (e.g., "hartreeworks"). If not provided,
                      fetches from auth.test API.
            link_style: "app" for native Slack app, "browser" for web browser.
                       - app: uses /archives/ path (opens in Slack app)
                       - browser: uses /messages/ path (opens in browser)

        Returns:
            Permalink URL
        """
        if not workspace:
            auth = self.auth_test()
            if not auth.get("ok"):
                raise ValueError(f"Failed to get workspace: {auth.get('error')}")
            # Extract workspace from URL like "https://hartreeworks.slack.com/"
            url = auth.get("url", "")
            workspace = url.replace("https://", "").replace(".slack.com/", "")

        # Format timestamp: remove the "." to create the permalink format
        formatted_ts = message_ts.replace(".", "")

        # Choose path based on link style
        path = "messages" if link_style == "browser" else "archives"

        return f"https://{workspace}.slack.com/{path}/{channel}/p{formatted_ts}"


# ==================== Session State Management ====================

def load_session_state() -> dict:
    """Load current session state."""
    if SESSION_STATE_PATH.exists():
        with open(SESSION_STATE_PATH) as f:
            return json.load(f)
    return {
        "active_workspace": None,
        "last_action_timestamp": None,
        "workspace_channel_map": {}
    }


def write_json_atomic(path: Path, data: dict):
    """Write JSON via a temp file and rename, so concurrent runs never leave a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def save_session_state(state: dict):
    """Save session state."""
    write_json_atomic(SESSION_STATE_PATH, state)


def set_active_workspace(workspace: str):
    """Set the active workspace for this session."""
    state = load_session_state()
    state["active_workspace"] = workspace
    state["last_action_timestamp"] = datetime.now().isoformat()
    save_session_state(state)


def record_channel_workspace(channel_id: str, workspace: str):
    """Record which workspace a channel belongs to."""
    state = load_session_state()
    state["workspace_channel_map"][channel_id] = workspace
    save_session_state(state)


def infer_workspace_from_channel(channel_id: str) -> str | None:
    """Try to infer workspace from a channel ID."""
    state = load_session_state()
    return state.get("workspace_channel_map", {}).get(channel_id)


# ==================== Cache Management ====================

def get_cache_path(workspace: str) -> Path:
    """Get the cache file path for a specific workspace."""
    return DATA_DIR / f"slack-cache-{workspace}.json"


def load_cache(workspace: str) -> dict:
    """Load cache for a specific workspace."""
    cache_path = get_cache_path(workspace)
    if cache_path.exists():
        with open(cache_path) as f:
            cache = json.load(f)
            # Ensure users section exists
            if "users" not in cache:
                cache["users"] = {}
            return cache
    return {
        "user": None,
        "self_dm_channel": None,
        "workspace": workspace,
        "frequent_contacts": {},
        "channels": {},
        "users": {},  # user_id -> {display_name, username, real_name}
        "last_updated": None
    }


def save_cache(workspace: str, cache: dict):
    """Save cache for a specific workspace."""
    write_json_atomic(get_cache_path(workspace), cache)


def fetch_and_cache_users(client: 'SlackClient', workspace: str) -> dict:
    """
    Fetch all users from Slack and update the cache.

    Returns:
        dict with stats about the update
    """
    cache = load_cache(workspace)
    if "users" not in cache:
        cache["users"] = {}

    existing_count = len(cache["users"])
    new_count = 0
    updated_count = 0

    # Fetch users (may need pagination for large workspaces)
    cursor = None
    while True:
        data = {"limit": "200"}
        if cursor:
            data["cursor"] = cursor

        result = client._post("users.list", data)

        if not result.get("ok"):
            raise Exception(f"Failed to fetch users: {result.get('error')}")

        members = result.get("members", [])

        for user in members:
            user_id = user.get("id")
            if not user_id:
                continue

            # Skip bots and deleted users
            if user.get("is_bot") or user.get("deleted"):
                continue

            profile = user.get("profile", {})
            user_data = {
                "username": user.get("name", ""),
                "display_name": profile.get("display_name") or profile.get("real_name") or user.get("name", ""),
                "real_name": profile.get("real_name", ""),
                "first_name": profile.get("first_name", ""),
            }

            if user_id in cache["users"]:
                # Check if anything changed
                if cache["users"][user_id] != user_data:
                    cache["users"][user_id] = user_data
                    updated_count += 1
            else:
                cache["users"][user_id] = user_data
                new_count += 1

        # Check for more pages
        cursor = result.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    cache["users_last_updated"] = datetime.now().isoformat()
    save_cache(workspace, cache)

    return {
        "total_users": len(cache["users"]),
        "new": new_count,
        "updated": updated_count,
        "previously_cached": existing_count
    }


def get_user_lookup(workspace: str) -> dict:
    """
    Get a user ID to display name lookup from the cache.

    Returns:
        dict mapping user_id -> display_name
    """
    cache = load_cache(workspace)
    lookup = {}

    # Add from users cache
    for user_id, user_data in cache.get("users", {}).items():
        lookup[user_id] = user_data.get("display_name") or user_data.get("username") or user_id

    # Add from frequent_contacts (for backwards compatibility)
    for username, contact in cache.get("frequent_contacts", {}).items():
        if "id" in contact and "display_name" in contact:
            lookup[contact["id"]] = contact["display_name"]

    # Add self
    if cache.get("user") and cache["user"].get("id"):
        lookup[cache["user"]["id"]] = cache["user"].get("display_name") or cache["user"].get("username")

    return lookup


def is_user_cache_empty(workspace: str) -> bool:
    """Check if user cache is empty or missing."""
    cache = load_cache(workspace)
    users = cache.get("users", {})
    return len(users) == 0


def is_user_cache_stale(workspace: str) -> bool:
    """Check if user cache is stale (older than USER_CACHE_STALE_DAYS)."""
    cache = load_cache(workspace)
    last_updated = cache.get("users_last_updated")

    if not last_updated:
        return True

    try:
        updated_dt = datetime.fromisoformat(last_updated)
        age = datetime.now() - updated_dt
        return age > timedelta(days=USER_CACHE_STALE_DAYS)
    except (ValueError, TypeError):
        return True


def trigger_background_user_refresh(workspace: str):
    """
    Trigger a background refresh of the user cache.
    Runs fetch-users in a detached subprocess.
    """
    script_path = Path(__file__).resolve()
    cmd = [sys.executable, str(script_path), "-w", workspace, "fetch-users"]

    # Run detached - don't wait for completion
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True
    )


# ==================== Export State Management ====================

def get_export_state_path(workspace: str) -> Path:
    """Get the export state file path for a specific workspace."""
    return DATA_DIR / f"export-state-{workspace}.json"


def load_export_state(workspace: str) -> dict:
    """Load export state for resume capability."""
    state_path = get_export_state_path(workspace)
    if state_path.exists():
        with open(state_path) as f:
            return json.load(f)
    return None


def save_export_state(workspace: str, state: dict):
    """Save export state for resume capability."""
    state_path = get_export_state_path(workspace)
    state["updated_at"] = datetime.now().isoformat()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)


def create_export_state(workspace: str, user_id: str, username: str,
                        from_date: str, to_date: str, output_file: str) -> dict:
    """Create a new export state."""
    return {
        "export_id": str(uuid.uuid4())[:8],
        "workspace": workspace,
        "status": "searching",
        "started_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "config": {
            "from_date": from_date,
            "to_date": to_date,
            "output_file": output_file,
            "user_id": user_id,
            "username": username
        },
        "search_progress": {
            "total_matches": 0,
            "current_page": 1,
            "messages_fetched": 0
        },
        "thread_progress": {
            "threads_pending": [],
            "threads_fetched": [],
            "current_index": 0
        },
        "data": {
            "channels": {},
            "threads": [],
            "standalone_messages": []
        },
        "errors": [],
        "stats": {
            "api_calls": 0
        }
    }


def delete_export_state(workspace: str):
    """Delete export state file after successful completion."""
    state_path = get_export_state_path(workspace)
    if state_path.exists():
        state_path.unlink()


# ==================== Export Functions ====================

def run_export(client: 'SlackClient', workspace: str, from_date: str, to_date: str,
               output_file: str, resume: bool = False):
    """
    Export user's messages with full thread context.

    Phase 1: Search for all user messages in date range
    Phase 2: Fetch full thread context for threaded messages
    Phase 3: Write final output file
    """
    rate_limiter = RateLimiter()

    # Get user info
    auth = client.auth_test()
    if not auth.get("ok"):
        raise Exception(f"Auth failed: {auth.get('error')}")

    user_id = auth.get("user_id")
    username = auth.get("user")

    # Load or create state
    state = load_export_state(workspace) if resume else None

    if state and state.get("status") == "completed":
        print(f"Previous export completed. Use without --resume to start fresh.", file=sys.stderr)
        return state

    if not state or not resume:
        state = create_export_state(workspace, user_id, username, from_date, to_date, output_file)
        save_export_state(workspace, state)
        print(f"Starting export {state['export_id']} for @{username}", file=sys.stderr)
    else:
        print(f"Resuming export {state['export_id']} from {state['status']}", file=sys.stderr)

    try:
        # ===== PHASE 1: Search for user's messages =====
        if state["status"] == "searching":
            query = f"from:{username} after:{from_date} before:{to_date}"
            page = state["search_progress"]["current_page"]

            print(f"Phase 1: Searching for messages...", file=sys.stderr)

            while True:
                rate_limiter.wait_for_tier3()
                state["stats"]["api_calls"] += 1

                result = client.search_messages_paginated(query, page=page, count=100)

                if not result.get("ok"):
                    error = result.get("error", "unknown")
                    if error == "ratelimited":
                        rate_limiter.handle_rate_limit_response()
                        continue
                    raise Exception(f"Search failed: {error}")

                rate_limiter.reset_backoff()

                messages_data = result.get("messages", {})
                matches = messages_data.get("matches", [])
                total = messages_data.get("total", 0)
                paging = messages_data.get("paging", {})

                # Process messages from this page
                for msg in matches:
                    _process_search_result(msg, state)

                state["search_progress"]["total_matches"] = total
                state["search_progress"]["messages_fetched"] += len(matches)
                state["search_progress"]["current_page"] = page

                print(f"  Page {page}/{paging.get('pages', 1)}: "
                      f"{state['search_progress']['messages_fetched']}/{total} messages",
                      file=sys.stderr)

                save_export_state(workspace, state)

                # Check if done with search
                if page >= paging.get("pages", 1):
                    break

                page += 1

            state["status"] = "fetching_threads"
            save_export_state(workspace, state)

        # ===== PHASE 2: Fetch thread context =====
        if state["status"] == "fetching_threads":
            pending = state["thread_progress"]["threads_pending"]
            fetched = set(state["thread_progress"]["threads_fetched"])
            to_fetch = [t for t in pending if t not in fetched]

            print(f"Phase 2: Fetching {len(to_fetch)} threads...", file=sys.stderr)

            for i, thread_key in enumerate(to_fetch):
                channel_id, thread_ts = thread_key.split(":")

                rate_limiter.wait_for_tier4()
                state["stats"]["api_calls"] += 1

                result = client.conversations_replies(channel_id, thread_ts)

                if not result.get("ok"):
                    error = result.get("error", "unknown")
                    if error == "ratelimited":
                        rate_limiter.handle_rate_limit_response()
                        continue
                    elif error in ("thread_not_found", "channel_not_found", "not_in_channel"):
                        # Skip inaccessible threads
                        state["thread_progress"]["threads_fetched"].append(thread_key)
                        state["errors"].append({
                            "timestamp": datetime.now().isoformat(),
                            "type": error,
                            "thread": thread_key
                        })
                        continue
                    raise Exception(f"Thread fetch failed: {error}")

                rate_limiter.reset_backoff()

                # Store thread data
                thread_messages = result.get("messages", [])
                _store_thread_data(thread_key, thread_messages, state)
                state["thread_progress"]["threads_fetched"].append(thread_key)
                state["thread_progress"]["current_index"] = i + 1

                if (i + 1) % 10 == 0:
                    print(f"  Threads: {i + 1}/{len(to_fetch)}", file=sys.stderr)
                    save_export_state(workspace, state)

            state["status"] = "writing_output"
            save_export_state(workspace, state)

        # ===== PHASE 3: Write output file =====
        if state["status"] == "writing_output":
            print(f"Phase 3: Writing output...", file=sys.stderr)
            _write_export_file(state, workspace)

            state["status"] = "completed"
            state["completed_at"] = datetime.now().isoformat()
            save_export_state(workspace, state)

            print(f"\nExport complete!", file=sys.stderr)
            print(f"  Messages: {state['search_progress']['messages_fetched']}", file=sys.stderr)
            print(f"  Threads: {len(state['data']['threads'])}", file=sys.stderr)
            print(f"  Standalone: {len(state['data']['standalone_messages'])}", file=sys.stderr)
            print(f"  Output: {state['config']['output_file']}", file=sys.stderr)

        return state

    except KeyboardInterrupt:
        print(f"\nExport paused. Run with --resume to continue.", file=sys.stderr)
        state["status"] = "paused"
        save_export_state(workspace, state)
        raise
    except Exception as e:
        state["errors"].append({
            "timestamp": datetime.now().isoformat(),
            "type": type(e).__name__,
            "details": str(e)
        })
        save_export_state(workspace, state)
        raise


def _process_search_result(msg: dict, state: dict):
    """Process a message from search results."""
    channel_info = msg.get("channel", {})
    channel_id = channel_info.get("id")
    message_ts = msg.get("ts")
    thread_ts = msg.get("thread_ts")
    user_id = state["config"]["user_id"]

    # Check permalink for thread_ts if not in message directly
    # Permalink format: .../p1234567890123456?thread_ts=1234567890.123456
    if not thread_ts:
        permalink = msg.get("permalink", "")
        if "thread_ts=" in permalink:
            match = re.search(r'thread_ts=(\d+\.\d+)', permalink)
            if match:
                thread_ts = match.group(1)

    # Store channel metadata
    if channel_id and channel_id not in state["data"]["channels"]:
        state["data"]["channels"][channel_id] = {
            "id": channel_id,
            "name": channel_info.get("name", "unknown"),
            "type": _infer_channel_type(channel_id)
        }

    if thread_ts:
        # Message is part of a thread
        thread_key = f"{channel_id}:{thread_ts}"
        if thread_key not in state["thread_progress"]["threads_pending"]:
            state["thread_progress"]["threads_pending"].append(thread_key)
    else:
        # Standalone message
        state["data"]["standalone_messages"].append({
            "ts": message_ts,
            "channel_id": channel_id,
            "user": msg.get("user") or msg.get("username"),
            "text": msg.get("text", ""),
            "is_user_message": True,
            "permalink": msg.get("permalink")
        })


def _store_thread_data(thread_key: str, messages: list, state: dict):
    """Store thread messages."""
    channel_id, thread_ts = thread_key.split(":")
    user_id = state["config"]["user_id"]

    thread_data = {
        "thread_id": thread_key,
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "user_message_count": 0,
        "total_message_count": len(messages),
        "messages": []
    }

    for msg in messages:
        is_user = msg.get("user") == user_id
        if is_user:
            thread_data["user_message_count"] += 1

        thread_data["messages"].append({
            "ts": msg.get("ts"),
            "user": msg.get("user"),
            "text": msg.get("text", ""),
            "is_user_message": is_user
        })

    state["data"]["threads"].append(thread_data)


def _infer_channel_type(channel_id: str) -> str:
    """Infer channel type from ID prefix."""
    if channel_id.startswith("C"):
        return "channel"
    elif channel_id.startswith("D"):
        return "dm"
    elif channel_id.startswith("G"):
        return "group"
    return "unknown"


def _write_export_file(state: dict, workspace: str):
    """Write the final export JSON file."""
    # Get user lookup from cache
    user_lookup = get_user_lookup(workspace)

    output = {
        "metadata": {
            "export_id": state["export_id"],
            "workspace": workspace,
            "user": {
                "id": state["config"]["user_id"],
                "username": state["config"]["username"]
            },
            "date_range": {
                "from": state["config"]["from_date"],
                "to": state["config"]["to_date"]
            },
            "exported_at": datetime.now().isoformat(),
            "stats": {
                "total_messages": state["search_progress"]["messages_fetched"],
                "total_threads": len(state["data"]["threads"]),
                "standalone_messages": len(state["data"]["standalone_messages"]),
                "channels_count": len(state["data"]["channels"]),
                "api_calls": state["stats"]["api_calls"]
            }
        },
        "users": user_lookup,  # Include user ID -> display name mapping
        "channels": state["data"]["channels"],
        "threads": state["data"]["threads"],
        "standalone_messages": state["data"]["standalone_messages"]
    }

    output_path = Path(state["config"]["output_file"]).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)


# ==================== Digest Functions ====================

def load_digest_config() -> dict:
    """Load the digest configuration."""
    if not DIGEST_CONFIG_PATH.exists():
        return {
            "workspaces": {},
            "lookback_hours": 14,
            "output_dir": str(DATA_DIR / "digests")
        }
    with open(DIGEST_CONFIG_PATH) as f:
        return json.load(f)


def run_digest(workspace: str = None) -> dict:
    """
    Generate an overnight digest for one or all workspaces.

    Returns a structured digest with:
    - mentions: Messages mentioning the user
    - replies: Replies to user's recent messages
    - channel_activity: Summary of channel activity

    Args:
        workspace: Specific workspace to digest, or None for all configured workspaces
    """
    digest_config = load_digest_config()
    full_config = load_full_config()
    lookback_hours = digest_config.get("lookback_hours", 14)

    # Calculate time range
    now = datetime.now()
    from_time = now - timedelta(hours=lookback_hours)

    # Convert to Slack search date format (YYYY-MM-DD)
    # Slack's "after:" filter is day-granularity, so we search from a day earlier
    # and filter precisely in code
    search_start = from_time - timedelta(days=1)
    search_date = search_start.strftime("%Y-%m-%d")

    result = {
        "generated": now.isoformat(),
        "period": {
            "from": from_time.isoformat(),
            "to": now.isoformat(),
            "lookback_hours": lookback_hours
        },
        "summary": {
            "total_mentions": 0,
            "unhandled_mentions": 0,  # Mentions user hasn't replied to yet
            "total_replies": 0
        },
        "mentions": [],
        "replies": []
    }

    # Determine which workspaces to process
    if workspace:
        workspaces_to_process = [workspace]
    else:
        workspaces_to_process = list(digest_config.get("workspaces", {}).keys())

    if not workspaces_to_process:
        # Fall back to all configured workspaces in main config
        workspaces_to_process = list(full_config.get("workspaces", {}).keys())

    rate_limiter = RateLimiter()
    seen_message_ids = set()  # For deduplication

    for ws_name in workspaces_to_process:
        if ws_name not in full_config.get("workspaces", {}):
            print(f"  Skipping unknown workspace: {ws_name}", file=sys.stderr)
            continue

        print(f"Processing workspace: {ws_name}", file=sys.stderr)

        try:
            creds = full_config["workspaces"][ws_name]
            client = SlackClient(
                creds["xoxc_token"],
                creds["xoxd_token"],
                creds.get("user_agent")
            )

            # Get current user info
            auth_result = client.auth_test()
            if not auth_result.get("ok"):
                print(f"  Auth failed for {ws_name}: {auth_result.get('error')}", file=sys.stderr)
                continue

            user_id = auth_result.get("user_id")
            username = auth_result.get("user")

            # Ensure user cache is populated before looking up names
            if is_user_cache_empty(ws_name):
                print(f"  User cache empty. Fetching users from {ws_name}...", file=sys.stderr)
                fetch_and_cache_users(client, ws_name)

            # Get user lookup for name resolution
            user_lookup = get_user_lookup(ws_name)

            ws_config = digest_config.get("workspaces", {}).get(ws_name, {})

            # 1. Search for mentions of this user
            if ws_config.get("include_mentions", True):
                print(f"  Searching for mentions...", file=sys.stderr)
                rate_limiter.wait_for_tier3()

                # Search for @mentions
                mention_query = f"<@{user_id}> after:{search_date}"
                mention_result = client.search_messages(mention_query, count=50)

                if mention_result.get("ok"):
                    matches = mention_result.get("messages", {}).get("matches", [])
                    for msg in matches:
                        msg_id = f"{msg.get('channel', {}).get('id')}:{msg.get('ts')}"
                        if msg_id in seen_message_ids:
                            continue
                        seen_message_ids.add(msg_id)

                        # Skip if it's the user's own message
                        if msg.get("user") == user_id or msg.get("username") == username:
                            continue

                        sender_id = msg.get("user") or msg.get("username")
                        sender_name = user_lookup.get(sender_id, sender_id)
                        channel_info = msg.get("channel", {})
                        mention_ts = float(msg.get("ts", 0))

                        # Check if user has already replied to this thread
                        user_replied = False
                        thread_ts = msg.get("thread_ts") or msg.get("ts")
                        channel_id = channel_info.get("id")

                        if channel_id and thread_ts:
                            # Fetch the thread to check for user's reply
                            rate_limiter.wait_for_tier4()
                            thread_result = client.conversations_replies(channel_id, thread_ts)
                            if thread_result.get("ok"):
                                thread_messages = thread_result.get("messages", [])
                                for tmsg in thread_messages:
                                    if tmsg.get("user") == user_id:
                                        reply_ts = float(tmsg.get("ts", 0))
                                        if reply_ts > mention_ts:
                                            user_replied = True
                                            break

                        # Get text from message, falling back to blocks if text is empty
                        msg_text = msg.get("text", "")
                        if not msg_text.strip():
                            # Try to get text from blocks (used by bots/apps)
                            blocks = msg.get("blocks", [])
                            for block in blocks:
                                if block.get("type") == "section":
                                    block_text = block.get("text", {})
                                    if isinstance(block_text, dict):
                                        msg_text = block_text.get("text", "")
                                    else:
                                        msg_text = str(block_text)
                                    if msg_text:
                                        break

                        result["mentions"].append({
                            "workspace": ws_name,
                            "channel": channel_info.get("name", "unknown"),
                            "channel_id": channel_id,
                            "from": sender_name,
                            "from_id": sender_id,
                            "text": msg_text[:500],  # Truncate long messages
                            "ts": msg.get("ts"),
                            "permalink": msg.get("permalink"),
                            "handled": user_replied  # True if user already replied after this mention
                        })
                        result["summary"]["total_mentions"] += 1
                        if not user_replied:
                            result["summary"]["unhandled_mentions"] += 1

            # 2. Search for thread activity where user participated
            # This covers threads user started OR replied to
            print(f"  Searching for thread activity...", file=sys.stderr)

            # Find threads user participated in by searching for their messages
            rate_limiter.wait_for_tier3()
            user_msg_query = f"from:{username} after:{search_date}"
            user_msg_result = client.search_messages(user_msg_query, count=50)

            threads_checked = set()

            if user_msg_result.get("ok"):
                user_messages = user_msg_result.get("messages", {}).get("matches", [])

                for msg in user_messages:
                    # Get thread_ts - either this message is in a thread, or it started one
                    thread_ts = msg.get("thread_ts") or msg.get("ts")
                    channel_id = msg.get("channel", {}).get("id")
                    channel_name = msg.get("channel", {}).get("name", "unknown")

                    if not channel_id or not thread_ts:
                        continue

                    thread_key = f"{channel_id}:{thread_ts}"
                    if thread_key in threads_checked:
                        continue
                    threads_checked.add(thread_key)

                    # Fetch the full thread
                    rate_limiter.wait_for_tier4()
                    replies_result = client.conversations_replies(channel_id, thread_ts)

                    if not replies_result.get("ok"):
                        continue

                    thread_messages = replies_result.get("messages", [])

                    # Find user's last message timestamp in this thread
                    user_last_ts = 0
                    for tmsg in thread_messages:
                        if tmsg.get("user") == user_id:
                            user_last_ts = max(user_last_ts, float(tmsg.get("ts", 0)))

                    # Find all recent messages from others in this thread
                    for tmsg in thread_messages:
                        if tmsg.get("user") == user_id:
                            continue  # Skip user's own messages

                        tmsg_ts = float(tmsg.get("ts", 0))

                        # Only include if within lookback period
                        msg_time = datetime.fromtimestamp(tmsg_ts)
                        if msg_time < from_time:
                            continue

                        msg_id = f"{channel_id}:{tmsg.get('ts')}"
                        if msg_id in seen_message_ids:
                            continue
                        seen_message_ids.add(msg_id)

                        sender_id = tmsg.get("user")
                        sender_name = user_lookup.get(sender_id, sender_id)
                        msg_text = tmsg.get("text", "")

                        # Skip empty/system messages
                        if not msg_text.strip():
                            continue
                        if " has joined the channel" in msg_text or " has left the channel" in msg_text:
                            continue

                        result["replies"].append({
                            "workspace": ws_name,
                            "channel": channel_name,
                            "channel_id": channel_id,
                            "from": sender_name,
                            "from_id": sender_id,
                            "text": msg_text[:500],
                            "ts": tmsg.get("ts"),
                            "thread_ts": thread_ts
                        })
                        result["summary"]["total_replies"] += 1

            # Note: Channel activity scanning removed - focus on mentions and thread replies
            # which mirrors Slack's Activity screen behaviour

        except Exception as e:
            print(f"  Error processing {ws_name}: {e}", file=sys.stderr)
            continue

    return result


def write_digest_output(digest: dict, output_dir: str = None) -> str:
    """
    Write the digest to a JSON file.

    Args:
        digest: The digest data
        output_dir: Directory to write to (defaults to digest config)

    Returns:
        Path to the written file
    """
    if not output_dir:
        digest_config = load_digest_config()
        output_dir = digest_config.get("output_dir", str(DATA_DIR / "digests"))

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Generate filename with date
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"slack-digest-{date_str}.json"
    filepath = output_path / filename

    with open(filepath, "w") as f:
        json.dump(digest, f, indent=2)

    return str(filepath)


# ==================== Config Management ====================

def load_full_config() -> dict:
    """Load the full config file."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Config not found: {CONFIG_PATH}. Set up a workspace with references/setup.md "
            "(installs before update checks kept config.json in the skill folder, which updates delete)."
        )
    with open(CONFIG_PATH) as f:
        return json.load(f)


CHROME_DATA_DIR = Path.home() / "Library/Application Support/Google/Chrome"


def open_in_chrome(url: str, workspace: str) -> str | None:
    """Open url in the Chrome profile set as the workspace's chrome_profile in config.json (a profile
    directory such as "Profile 9"), so a draft lands in the profile signed in to that Slack rather
    than whichever profile is in front. Falls back to plain `open` when none is set or the directory
    is missing on this machine (config.json syncs between Macs). Returns the profile used, or None."""
    profile = (load_full_config().get("workspaces", {}).get(workspace, {}) or {}).get("chrome_profile")
    if profile and (CHROME_DATA_DIR / profile).is_dir():
        subprocess.run(["open", "-na", "Google Chrome", "--args", f"--profile-directory={profile}", url], check=False)
        return profile
    subprocess.run(["open", "-a", "Google Chrome", url], check=False)
    return None


def save_config(config: dict):
    """Save the config file."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def load_config(workspace: str = None) -> tuple[dict, str]:
    """
    Load credentials for a workspace.

    There is no implicit default: guessing silently picks the wrong workspace and
    surfaces later as channel_not_found. With one workspace configured, it is used.

    Returns:
        tuple: (credentials_dict, workspace_name)
    """
    config = load_full_config()
    workspaces = config.get("workspaces", {})

    if not workspaces:
        raise ValueError("No workspaces configured in config.json")

    if not workspace:
        if len(workspaces) == 1:
            workspace = next(iter(workspaces))
        else:
            raise ValueError(
                f"No workspace given. Pass -w <workspace>, one of: {', '.join(workspaces)}. "
                "For a Slack link, use read-link, which takes the workspace from the URL."
            )

    if workspace not in workspaces:
        raise ValueError(f"Unknown workspace: {workspace}. Available: {', '.join(workspaces)}")
    return workspaces[workspace], workspace


def make_client(creds: dict) -> 'SlackClient':
    return SlackClient(creds["xoxc_token"], creds["xoxd_token"], creds.get("user_agent"))


def get_link_style() -> str:
    """Get the configured link style preference."""
    config = load_full_config()
    return config.get("link_style", "app")


# ==================== Readable Output ====================

def format_ts(ts: str) -> str:
    """Render a Slack timestamp as local time, e.g. 2026-10-10 13:17 CEST."""
    try:
        return datetime.fromtimestamp(float(ts)).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    except (TypeError, ValueError):
        return ""


def blocks_text(blocks) -> str:
    """Fallback text for messages (often bot alerts) whose `text` is empty."""
    parts = []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("text"), str):
                parts.append(node["text"])
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(blocks or [])
    return " ".join(part.strip() for part in parts if part.strip())


class NameResolver:
    """Resolve user and channel IDs to names via the workspace cache.

    Misses are looked up once (users.info / conversations.info) and written back
    to data/slack-cache-<workspace>.json, so each name costs one API call ever.
    """

    MAX_LOOKUPS = 40

    def __init__(self, client: 'SlackClient', workspace: str):
        self.client = client
        self.workspace = workspace
        if is_user_cache_empty(workspace):
            print(f"User cache empty. Fetching users from {workspace}...", file=sys.stderr)
            fetch_and_cache_users(client, workspace)
        elif is_user_cache_stale(workspace):
            trigger_background_user_refresh(workspace)
        cache = load_cache(workspace)
        self.users = cache.get("users", {})
        self.channels = cache.get("channel_names", {})
        self.new_users = {}
        self.new_channels = {}
        self.lookups = 0

    def _lookup_allowed(self) -> bool:
        self.lookups += 1
        return self.lookups <= self.MAX_LOOKUPS

    def _remember_user(self, user_id: str, data: dict):
        self.users[user_id] = data
        self.new_users[user_id] = data

    def user_name(self, user_id: str, profile: dict = None) -> str:
        if not user_id:
            return "unknown"
        entry = self.users.get(user_id)
        if not entry and profile:
            # Slack Connect messages embed the author's profile.
            entry = {
                "username": profile.get("name", ""),
                "display_name": profile.get("display_name") or profile.get("real_name", ""),
                "real_name": profile.get("real_name", ""),
                "first_name": profile.get("first_name", ""),
            }
            self._remember_user(user_id, entry)
        if not entry and self._lookup_allowed():
            result = self.client.users_info(user_id)
            if result.get("ok"):
                user = result.get("user", {})
                p = user.get("profile", {})
                entry = {
                    "username": user.get("name", ""),
                    "display_name": p.get("display_name") or p.get("real_name") or user.get("name", ""),
                    "real_name": p.get("real_name", ""),
                    "first_name": p.get("first_name", ""),
                }
                self._remember_user(user_id, entry)
        if not entry:
            return user_id
        return entry.get("real_name") or entry.get("display_name") or entry.get("username") or user_id

    def describe_channel(self, channel: dict, lookup_users: bool = True) -> str | None:
        if channel.get("is_im"):
            other = channel.get("user")
            if not lookup_users and other not in self.users:
                return None
            return f"DM with {self.user_name(other)}"
        name = channel.get("name") or ""
        if channel.get("is_mpim"):
            members = re.sub(r"^mpdm-|-\d+$", "", name).split("--")
            return f"group DM ({', '.join(m for m in members if m)})"
        return f"#{name}" if name else None

    def remember_channels(self, channels: list):
        """Cache names from a conversations.list result without extra API calls."""
        for channel in channels:
            name = self.describe_channel(channel, lookup_users=False)
            if channel.get("id") and name:
                self.channels[channel["id"]] = name
                self.new_channels[channel["id"]] = name

    def channel_name(self, channel_id: str) -> str:
        if channel_id in self.channels:
            return self.channels[channel_id]
        if self._lookup_allowed():
            result = self.client.conversations_info(channel_id)
            if result.get("ok"):
                name = self.describe_channel(result.get("channel", {}))
                if name:
                    self.channels[channel_id] = name
                    self.new_channels[channel_id] = name
                    return name
        return channel_id

    def clean_text(self, text: str) -> str:
        """Turn Slack markup (<@U..>, <#C..|name>, <url|label>) into plain text."""
        def replace(match):
            target, _, label = match.group(1).partition("|")
            if target.startswith("@"):
                return "@" + (label or self.user_name(target[1:]))
            if target.startswith("#"):
                return "#" + (label or self.channel_name(target[1:]).lstrip("#"))
            if target.startswith("!"):
                return label or "@" + target[1:].split("^")[0]
            if label and label != target:
                return f"{label} ({target})"
            return target.removeprefix("mailto:")

        text = re.sub(r"<([^<>\n]+)>", replace, text or "")
        return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")

    def message(self, msg: dict, linked_ts: str = None) -> dict:
        ts = msg.get("ts", "")
        user_id = msg.get("user")
        if user_id:
            author = self.user_name(user_id, msg.get("user_profile"))
        else:
            author = (msg.get("bot_profile") or {}).get("name") or msg.get("username") or "unknown"
        out = {
            "ts": ts,
            "time": format_ts(ts),
            "user": user_id or msg.get("bot_id"),
            "user_name": author,
            "text": self.clean_text(msg.get("text") or blocks_text(msg.get("blocks"))),
        }
        if msg.get("thread_ts") and msg["thread_ts"] != ts:
            out["thread_ts"] = msg["thread_ts"]
        if msg.get("reply_count"):
            out["reply_count"] = msg["reply_count"]
        if msg.get("subtype"):
            out["subtype"] = msg["subtype"]
        attachments = []
        for attachment in msg.get("attachments") or []:
            pieces = [attachment.get(k) for k in ("pretext", "title", "text")]
            summary = " | ".join(p for p in pieces if p) or attachment.get("fallback")
            if summary:
                attachments.append(self.clean_text(summary))
        if attachments:
            out["attachments"] = attachments
        files = [
            {"name": f.get("name") or f.get("title"), "url": f.get("url_private")}
            for f in msg.get("files") or []
        ]
        if files:
            out["files"] = files
        if msg.get("reactions"):
            out["reactions"] = {r["name"]: r.get("count", 0) for r in msg["reactions"]}
        if msg.get("edited"):
            out["edited"] = True
        if "replies" in msg:
            out["replies"] = [self.message(reply, linked_ts) for reply in msg["replies"]]
        if linked_ts and ts == linked_ts:
            out["linked"] = True
        return out

    def save(self):
        """Merge newly learned names into the cache (a background refresh may have written it)."""
        if not self.new_users and not self.new_channels:
            return
        cache = load_cache(self.workspace)
        cache.setdefault("users", {}).update(self.new_users)
        cache.setdefault("channel_names", {}).update(self.new_channels)
        save_cache(self.workspace, cache)


def channel_hint(result: dict, workspace: str) -> dict:
    """Explain the usual cause of channel_not_found: the wrong workspace."""
    if result.get("error") in ("channel_not_found", "thread_not_found", "not_in_channel"):
        configured = list(load_full_config().get("workspaces", {}))
        result["hint"] = (
            f"Checked workspace {workspace}. This usually means the channel belongs to another "
            f"workspace; configured: {', '.join(configured)}. For a Slack link, use read-link."
        )
    return result


# ==================== Slack Links ====================

def parse_slack_link(url: str) -> dict:
    """Parse a message link into domain/team, channel, message ts and thread ts.

    Handles https://<domain>.slack.com/archives/<C>/p<ts>[?thread_ts=<ts>] (and
    /messages/), plus https://app.slack.com/client/<T>/<C>/thread/<C>-<ts>.
    """
    parsed = urlparse(url.strip().strip("<>"))
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    link = {"domain": None, "team_id": None, "channel": None, "ts": None, "thread_ts": None}

    if host == "app.slack.com" and len(parts) >= 3 and parts[0] == "client":
        link["team_id"], link["channel"] = parts[1], parts[2]
        if len(parts) >= 5 and parts[3] == "thread":
            match = re.fullmatch(r"[A-Z0-9]+-(\d{10}\.\d{6})", parts[4])
            if match:
                link["ts"] = match.group(1)
    elif host.endswith(".slack.com") and len(parts) >= 3 and parts[0] in ("archives", "messages"):
        link["domain"] = host[: -len(".slack.com")]
        link["channel"] = parts[1]
        match = re.fullmatch(r"p(\d{10})(\d{6})", parts[2])
        if match:
            link["ts"] = f"{match.group(1)}.{match.group(2)}"

    thread_ts = (parse_qs(parsed.query).get("thread_ts") or [None])[0]
    if thread_ts and re.fullmatch(r"\d{10}\.\d{6}", thread_ts):
        link["thread_ts"] = thread_ts

    if not link["channel"] or not link["ts"]:
        raise ValueError(
            "Not a Slack message link. Expected "
            "https://<workspace>.slack.com/archives/<channel>/p<timestamp>"
        )
    return link



# ---------- mrkdwn text -> rich_text blocks, for drafts ----------

_INLINE = re.compile(
    r"<@(?P<user>[UW][A-Z0-9]+)>"
    r"|<(?P<lurl>https?://[^|>]+)(?:\|(?P<ltext>[^>]+))?>"
    r"|(?P<url>https?://[^\s<>]+[^\s<>.,;:!?)])"
    r"|`(?P<code>[^`]+)`"
    r"|__(?P<u>[^_\n]+?)__"
    r"|(?<![\w*])\*(?P<b>[^*\n]+?)\*(?![\w*])"
    r"|(?<![\w_])_(?P<i>[^_\n]+?)_(?![\w_])"
    r"|(?<![\w~])~(?P<s>[^~\n]+?)~(?![\w~])")


def _inline(text: str) -> list:
    out, pos = [], 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append({"type": "text", "text": text[pos:m.start()]})
        g = m.groupdict()
        if g["user"]:
            out.append({"type": "user", "user_id": g["user"]})
        elif g["lurl"]:
            out.append({"type": "link", "url": g["lurl"], **({"text": g["ltext"]} if g["ltext"] else {})})
        elif g["url"]:
            out.append({"type": "link", "url": g["url"]})
        else:
            key = next(k for k in ("code", "u", "b", "i", "s") if g[k])
            style = {"code": "code", "u": "underline", "b": "bold", "i": "italic", "s": "strike"}[key]
            for el in _inline(g[key]) if key != "code" else [{"type": "text", "text": g[key]}]:
                el.setdefault("style", {})[style] = True
                out.append(el)
        pos = m.end()
    if pos < len(text):
        out.append({"type": "text", "text": text[pos:]})
    return out


def mrkdwn_to_blocks(text: str) -> list:
    """Slack mrkdwn as written in a draft file -> one rich_text block. *bold*, _italic_, ~strike~,
    `code`, __underline__ (not mrkdwn, but drafts can carry it), <@U…>, <url|text> and bare URLs;
    lines starting "• "/"- " become bullet lists; numbered lines ("1. ") stay plain text, since the user
    never wants Slack's numbered-list element;
    a ``` fenced block (optional language tag) becomes a code block, its contents kept verbatim."""
    elements, para = [], []

    def flush():
        if para:
            body = "\n".join(para)
            lead = "\n" if elements and body.startswith("\n") else ""   # a blank line after a list
            body = body.strip("\n")
            if body:
                elements.append({"type": "rich_text_section", "elements": _inline(lead + body)})
            para.clear()

    lines = text.rstrip("\n").split("\n")
    i = 0
    while i < len(lines):
        if re.match(r"^\s*```[\w+#.-]*\s*$", lines[i]):
            end = next((j for j in range(i + 1, len(lines)) if lines[j].strip() == "```"), None)
            if end is not None:
                flush()
                code = "\n".join(lines[i + 1:end])
                if code:
                    elements.append({"type": "rich_text_preformatted",
                                     "elements": [{"type": "text", "text": code}]})
                i = end + 1
                while i < len(lines) and not lines[i].strip():
                    para.append("")
                    i += 1
                continue
        m = re.match(r"^[•\-]\s+", lines[i])
        if not m:
            para.append(lines[i])
            i += 1
            continue
        flush()
        items = []
        while i < len(lines) and (mm := re.match(r"^[•\-]\s+", lines[i])):
            items.append({"type": "rich_text_section", "elements": _inline(lines[i][mm.end():])})
            i += 1
        elements.append({"type": "rich_text_list", "style": "bullet", "elements": items})
        while i < len(lines) and not lines[i].strip():
            para.append("")
            i += 1
    flush()
    return [{"type": "rich_text", "elements": elements}]


def rich_text_plain(blocks: list, names: 'NameResolver' = None) -> str:
    """rich_text blocks -> plain text, for previewing a draft: mentions as @name, links as their
    text, list items on lines of their own."""
    out = []

    def walk(el):
        t = el.get("type")
        if t == "text":
            out.append(el.get("text", ""))
        elif t == "user":
            out.append("@" + (names.user_name(el["user_id"]) if names else el["user_id"]))
        elif t == "channel":
            out.append(names.channel_name(el["channel_id"]) if names else "#" + el["channel_id"])
        elif t == "link":
            out.append(el.get("text") or el.get("url", ""))
        elif t == "emoji":
            out.append(f":{el.get('name', '')}:")
        elif t == "broadcast":
            out.append("@" + el.get("range", ""))
        for child in el.get("elements", []):
            walk(child)
        if t in ("rich_text_section", "rich_text_list", "rich_text_preformatted", "rich_text_quote"):
            out.append("\n")

    for block in blocks or []:
        walk(block)
    return "".join(out).strip()


def draft_url(team_id: str, channel: str, thread_ts: str = None) -> str:
    """The web-client url of the message box (or thread reply box) a draft sits in."""
    return f"https://app.slack.com/client/{team_id}/{channel}" + (
        f"/thread/{channel}-{thread_ts}" if thread_ts else "")


def read_draft_source(src: str) -> list:
    """A draft file -> rich_text blocks: .json is taken as blocks, anything else as mrkdwn."""
    if src.endswith(".json"):
        blocks = json.loads(Path(src).read_text())
        return blocks if isinstance(blocks, list) else blocks["blocks"]
    return mrkdwn_to_blocks(Path(src).read_text())

def workspace_team(workspace: str) -> dict:
    """Return {"domain", "id"} for a workspace, caching auth.test's answer."""
    cache = load_cache(workspace)
    team = cache.get("team") or {}
    if team.get("domain") and team.get("id"):
        return team
    creds, _ = load_config(workspace)
    auth = make_client(creds).auth_test()
    if not auth.get("ok"):
        return {}
    domain = urlparse(auth.get("url", "")).hostname or ""
    team = {"domain": domain.removesuffix(".slack.com"), "id": auth.get("team_id")}
    cache["team"] = team
    save_cache(workspace, cache)
    return team


def workspace_for_link(link: dict) -> str:
    """Match a link to a configured workspace by its domain or team ID."""
    workspaces = list(load_full_config().get("workspaces", {}))
    for workspace in workspaces:
        team = workspace_team(workspace)
        if (link["domain"] and team.get("domain") == link["domain"]) or \
                (link["team_id"] and team.get("id") == link["team_id"]):
            return workspace
    inferred = infer_workspace_from_channel(link["channel"])
    if inferred:
        return inferred
    raise ValueError(
        f"No configured workspace matches {link['domain'] or link['team_id']}. "
        f"Configured: {', '.join(workspaces)}. Pass -w if the channel is shared into one of them."
    )


def read_link(url: str, workspace: str = None, context: int = None) -> dict:
    """Read a pasted Slack link in full: the whole thread, plus channel context."""
    link = parse_slack_link(url)
    workspace = workspace or workspace_for_link(link)
    creds, workspace = load_config(workspace)
    client = make_client(creds)
    channel, linked_ts = link["channel"], link["ts"]

    root_ts = link["thread_ts"] or linked_ts
    thread = client.conversations_replies(channel, root_ts)
    if not thread.get("ok"):
        return channel_hint({**thread, "_workspace": workspace}, workspace)
    messages = thread.get("messages", [])
    # A link to a reply without ?thread_ts= returns only the reply; refetch the whole thread.
    parent_ts = (messages[0] if messages else {}).get("thread_ts")
    if parent_ts and parent_ts != root_ts:
        root_ts = parent_ts
        thread = client.conversations_replies(channel, root_ts)
        if not thread.get("ok"):
            return channel_hint({**thread, "_workspace": workspace}, workspace)
        messages = thread.get("messages", [])

    names = NameResolver(client, workspace)
    if context is None:
        # A lone message often continues a conversation in the channel itself.
        context = 0 if len(messages) > 1 else 5
    result = {
        "ok": True,
        "workspace": workspace,
        "channel": {"id": channel, "name": names.channel_name(channel)},
        "linked_ts": linked_ts,
        "linked_is_reply": root_ts != linked_ts,
        "thread_ts": root_ts,
        "reply_count": max(len(messages) - 1, 0),
        "thread": [names.message(m, linked_ts) for m in messages],
    }
    if context > 0:
        before = client.conversations_history(channel, context, latest=root_ts, inclusive=False)
        after = client.conversations_history(channel, context, oldest=root_ts)
        result["context_before"] = [names.message(m) for m in reversed(before.get("messages", []))]
        result["context_after"] = [names.message(m) for m in reversed(after.get("messages", []))]
    names.save()
    record_channel_workspace(channel, workspace)
    return result


HELP = """Usage: slack -w <workspace> <command> [args]

Every command needs -w <workspace> (list them with `slack workspaces`), except:
read-link (takes the workspace from the URL), digest (all workspaces unless -w),
workspaces, add-workspace, and commands on a channel ID this client has seen before.

Reading
  read-link <url> [--context N]     Read a pasted Slack link: the whole thread with names
                                    and times, the linked message flagged. A message with
                                    no thread also gets 5 channel messages either side.
  history <channel> [limit] [--oldest ts] [--latest ts] [--cursor c] [--with-replies]
  replies <channel> <thread_ts>     Whole thread. thread_ts must be the parent's ts: a
                                    reply's ts returns only that reply.
  search <query> [count]            Slack search syntax: from:@name in:#channel after:YYYY-MM-DD
  search-page <query> <page> [count]
  thread-view [limit] [--current-ts ts]
  todos [--all]                     The To-dos (Later) list: items in progress, with due
                                    dates. --all keeps the completed and archived items
                                    Slack returns too.
  info <channel> | counts | download <url> <output_path> [max_bytes]

  --resolve (history, replies, search): slim messages with names, local times and
  readable mentions instead of raw API JSON.

Names
  user-lookup | fetch-users | users | channels [types]

Writing
  send <channel> <text> [thread_ts]
  draft <channel> <file> [--open]   Save an unsent draft in the channel's message box; the person
                                    sends it. <file> is mrkdwn text (.txt/.md: *bold*, __underline__,
                                    lists, <@U…>, links) or rich_text blocks (.json). Real @-mentions,
                                    links and bold/underline survive, unlike a paste. Prints the
                                    draft_id, channel and url; --thread <ts> puts it in that thread's
                                    reply box (ts of the thread's parent); --file <path> attaches a
                                    file, such as a screenshot (repeatable; uploaded unshared, so
                                    it is seen only once the person sends); --open opens it in Chrome.
  drafts [channel]                  List unsent drafts, newest first: draft_id, channel, thread_ts,
                                    last update and the first 80 characters.
  draft-edit <draft_id> <file> [--open]
                                    Replace a draft's text in place (same file formats as draft);
                                    its conversation, thread and attachments stay.
  draft-delete <draft_id>           Delete an unsent draft.
  react <channel> <message_ts> <emoji> [--remove]
                                    Add or remove one reaction (emoji name without colons).
  mark-read <channel> <message_ts> --confirm
                                    Move the channel's read cursor forward (never back).
  mark-thread-read <channel> <thread_ts> <message_ts> --confirm
                                    Move a followed thread's read cursor forward through
                                    a reply (never back). thread_ts is the parent's ts.
  focus [status] | focus on <minutes> | focus until <HH:MM> | focus off
                                    Pause notifications in one workspace and set the "In
                                    focus mode" status, which expires with the pause.
                                    `until` takes local time, rolling to tomorrow if past.
                                    `off` also ends a pause set by the notification schedule,
                                    and clears the status only if it is still the focus one.

Other
  auth | permalink <channel> <ts> [workspace] [link_style] | workspaces
  add-workspace <name> <xoxc> <xoxd> [user_agent]
  export --from DATE --to DATE --output FILE | export --resume | export-status
  digest [--output FILE] | digest-config
"""


def minutes_until(clock: str, now: datetime = None) -> int:
    """Minutes from now to the next local HH:MM, rounded up."""
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", clock)
    if not match:
        raise ValueError("time must be HH:MM (24-hour)")
    now = now or datetime.now()
    target = now.replace(hour=int(match.group(1)), minute=int(match.group(2)),
                         second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1, -(-int((target - now).total_seconds()) // 60))


def dnd_summary(info: dict, workspace: str) -> dict:
    """Readable Do Not Disturb state from a dnd.info response."""
    now = time.time()
    snooze_end = info.get("snooze_endtime")
    schedule_start = info.get("next_dnd_start_ts")
    schedule_end = info.get("next_dnd_end_ts")
    paused_by_schedule = bool(info.get("dnd_enabled") and schedule_start and schedule_end
                              and schedule_start <= now < schedule_end)
    return {
        "ok": True,
        "_workspace": workspace,
        "paused": bool(info.get("snooze_enabled")) or paused_by_schedule,
        "manual_pause_until": format_ts(snooze_end) if info.get("snooze_enabled") and snooze_end else None,
        "schedule_enabled": bool(info.get("dnd_enabled")),
        "schedule_pause": {
            "from": format_ts(schedule_start),
            "to": format_ts(schedule_end),
        } if info.get("dnd_enabled") and schedule_start and schedule_end else None,
    }


FOCUS_STATUS = ("In focus mode", ":telescope:")


# ==================== CLI ====================

def parse_global_args() -> tuple[str | None, list[str], bool]:
    """
    Parse global flags: --workspace/-w and --resolve.

    Returns:
        tuple: (workspace_name or None, remaining args, resolve)
    """
    workspace = None
    resolve = False
    args_filtered = []
    i = 1  # Skip script name

    while i < len(sys.argv):
        if sys.argv[i] == "--resolve":
            resolve = True
            i += 1
            continue
        if sys.argv[i] in ("--workspace", "-w"):
            if i + 1 < len(sys.argv):
                workspace = sys.argv[i + 1]
                i += 2
                continue
            else:
                print(json.dumps({"error": "--workspace requires a value"}))
                sys.exit(1)
        args_filtered.append(sys.argv[i])
        i += 1

    return workspace, args_filtered, resolve


AGENT_PREFIX = "[agent] "


def agent_prefixed(text: str) -> str:
    """Mark a sent message as the agent's: it posts under the user's name.

    A bold title line keeps the prefix inside its bold: `*[agent] Title*`.
    """
    if text.startswith("[agent]") or text.startswith("*[agent]"):
        return text
    first_line = text.split("\n", 1)[0]
    if len(first_line) > 2 and first_line.startswith("*") and first_line.endswith("*"):
        return "*" + AGENT_PREFIX + text[1:]
    return AGENT_PREFIX + text


# Commands whose first argument is a channel ID, so the workspace can be inferred.
CHANNEL_COMMANDS = {"history", "info", "replies", "mark-read", "mark-thread-read", "send", "draft", "drafts", "react", "permalink"}


def main():
    """CLI interface for the Slack client."""
    workspace_arg, args, resolve = parse_global_args()

    if not args or args[0] in ("help", "--help", "-h") or "--help" in args or "-h" in args:
        print(HELP)
        sys.exit(0 if args else 1)

    command = args[0]
    cmd_args = args[1:]

    if (not workspace_arg and command in CHANNEL_COMMANDS and cmd_args
            and re.fullmatch(r"[CDG][A-Z0-9]+", cmd_args[0])):
        workspace_arg = infer_workspace_from_channel(cmd_args[0])

    # Commands that don't need a client
    if command == "workspaces":
        config = load_full_config()
        state = load_session_state()
        result = {
            "workspaces": list(config.get("workspaces", {}).keys()),
            "last_used": state.get("active_workspace"),
            "link_style": config.get("link_style", "app"),
            "note": "Pass -w <workspace> on every command; there is no default.",
        }
        print(json.dumps(result, indent=2))
        return

    if command == "switch":
        print(json.dumps({
            "error": "switch has been removed: pass -w <workspace> on each command instead",
            "workspaces": list(load_full_config().get("workspaces", {})),
        }))
        sys.exit(1)

    if command == "read-link":
        if not cmd_args:
            print(json.dumps({"error": "Usage: read-link <slack-url> [--context N]"}))
            sys.exit(1)
        context = None
        if "--context" in cmd_args:
            index = cmd_args.index("--context")
            try:
                context = int(cmd_args[index + 1])
            except (IndexError, ValueError):
                print(json.dumps({"error": "--context requires a number"}))
                sys.exit(1)
        try:
            result = read_link(cmd_args[0], workspace_arg, context)
        except Exception as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if not result.get("ok"):
            sys.exit(1)
        return

    if command == "add-workspace":
        if len(cmd_args) < 3:
            print(json.dumps({
                "error": "Usage: add-workspace <name> <xoxc_token> <xoxd_token> [user_agent]"
            }))
            sys.exit(1)
        name, xoxc, xoxd = cmd_args[0], cmd_args[1], cmd_args[2]
        user_agent = cmd_args[3] if len(cmd_args) > 3 else None

        config = load_full_config()
        if "workspaces" not in config:
            config["workspaces"] = {}

        # Use existing user_agent from another workspace if not provided
        if not user_agent and config["workspaces"]:
            first_ws = next(iter(config["workspaces"]))
            user_agent = config["workspaces"][first_ws].get("user_agent")

        config["workspaces"][name] = {
            "xoxc_token": xoxc,
            "xoxd_token": xoxd,
            "user_agent": user_agent
        }

        save_config(config)
        result = {"ok": True, "added": name, "workspaces": list(config["workspaces"].keys())}
        print(json.dumps(result, indent=2))
        return

    if command == "user-lookup":
        # Get user lookup with stale-while-revalidate pattern:
        # - Empty cache: fetch synchronously (first-time setup)
        # - Stale cache (>14 days): return stale data, refresh in background
        # - Fresh cache: return cached data
        try:
            creds, ws_name = load_config(workspace_arg)
        except Exception as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)

        cache_empty = is_user_cache_empty(ws_name)
        cache_stale = is_user_cache_stale(ws_name)
        refreshing = False

        if cache_empty:
            # First time - must fetch synchronously
            print(f"User cache empty. Fetching users from {ws_name}...", file=sys.stderr)
            try:
                client = SlackClient(
                    creds["xoxc_token"],
                    creds["xoxd_token"],
                    creds.get("user_agent")
                )
                fetch_and_cache_users(client, ws_name)
            except Exception as e:
                print(json.dumps({"error": f"Failed to fetch users: {e}"}))
                sys.exit(1)
        elif cache_stale:
            # Stale - return cached data, refresh in background
            trigger_background_user_refresh(ws_name)
            refreshing = True

        lookup = get_user_lookup(ws_name)
        cache = load_cache(ws_name)
        result = {
            "ok": True,
            "workspace": ws_name,
            "user_count": len(lookup),
            "last_updated": cache.get("users_last_updated"),
            "users": lookup
        }
        if refreshing:
            result["refreshing_in_background"] = True
        print(json.dumps(result, indent=2))
        return

    if command == "fetch-users":
        try:
            creds, ws_name = load_config(workspace_arg)
            client = SlackClient(
                creds["xoxc_token"],
                creds["xoxd_token"],
                creds.get("user_agent")
            )

            print(f"Fetching users from {ws_name}...", file=sys.stderr)
            stats = fetch_and_cache_users(client, ws_name)
            result = {
                "ok": True,
                "workspace": ws_name,
                **stats
            }
            print(json.dumps(result, indent=2))

        except Exception as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        return

    if command == "digest-config":
        config = load_digest_config()
        print(json.dumps(config, indent=2))
        return

    if command == "digest":
        # Parse digest-specific arguments
        output_file = None
        i = 0
        while i < len(cmd_args):
            if cmd_args[i] == "--output" and i + 1 < len(cmd_args):
                output_file = cmd_args[i + 1]
                i += 2
            else:
                i += 1

        try:
            print("Generating Slack digest...", file=sys.stderr)
            digest = run_digest(workspace=workspace_arg)

            # Write to file
            if output_file:
                output_path = output_file
            else:
                digest_config = load_digest_config()
                output_path = write_digest_output(digest)

            print(f"Digest written to: {output_path}", file=sys.stderr)

            # Output summary to stdout
            result = {
                "ok": True,
                "output_file": output_path,
                "summary": digest["summary"],
                "period": digest["period"]
            }
            print(json.dumps(result, indent=2))

        except Exception as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        return

    if command == "export-status":
        # Check status without requiring workspace_arg - look at default/active
        try:
            _, ws_name = load_config(workspace_arg)
        except Exception as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)

        state = load_export_state(ws_name)
        if not state:
            print(json.dumps({
                "ok": True,
                "workspace": ws_name,
                "status": "no_export",
                "message": "No export in progress or completed"
            }, indent=2))
        else:
            result = {
                "ok": True,
                "workspace": ws_name,
                "export_id": state.get("export_id"),
                "status": state.get("status"),
                "started_at": state.get("started_at"),
                "updated_at": state.get("updated_at"),
                "search_progress": state.get("search_progress"),
                "thread_progress": {
                    "pending": len(state.get("thread_progress", {}).get("threads_pending", [])),
                    "fetched": len(state.get("thread_progress", {}).get("threads_fetched", []))
                },
                "errors": len(state.get("errors", []))
            }
            if state.get("status") == "completed":
                result["output_file"] = state.get("config", {}).get("output_file")
            print(json.dumps(result, indent=2))
        return

    if command == "export":
        # Parse export-specific arguments
        from_date = None
        to_date = None
        output_file = None
        resume = False

        i = 0
        while i < len(cmd_args):
            if cmd_args[i] == "--from" and i + 1 < len(cmd_args):
                from_date = cmd_args[i + 1]
                i += 2
            elif cmd_args[i] == "--to" and i + 1 < len(cmd_args):
                to_date = cmd_args[i + 1]
                i += 2
            elif cmd_args[i] == "--output" and i + 1 < len(cmd_args):
                output_file = cmd_args[i + 1]
                i += 2
            elif cmd_args[i] == "--resume":
                resume = True
                i += 1
            else:
                i += 1

        # Validate args
        if not resume and (not from_date or not to_date or not output_file):
            print(json.dumps({
                "error": "Required: --from DATE --to DATE --output FILE (or --resume)",
                "usage": "export --from 2025-07-01 --to 2026-01-05 --output ~/slack-export.json",
                "resume_usage": "export --resume"
            }))
            sys.exit(1)

        try:
            creds, ws_name = load_config(workspace_arg)
            client = SlackClient(
                creds["xoxc_token"],
                creds["xoxd_token"],
                creds.get("user_agent")
            )

            # If resuming, get dates from saved state
            if resume:
                state = load_export_state(ws_name)
                if not state:
                    print(json.dumps({"error": "No export to resume"}))
                    sys.exit(1)
                from_date = state["config"]["from_date"]
                to_date = state["config"]["to_date"]
                output_file = state["config"]["output_file"]

            result = run_export(client, ws_name, from_date, to_date, output_file, resume)
            print(json.dumps({"ok": True, "status": result["status"]}, indent=2))

        except KeyboardInterrupt:
            print(json.dumps({"ok": True, "status": "paused", "message": "Use --resume to continue"}))
            sys.exit(0)
        except Exception as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        return

    # Commands that need a client
    try:
        creds, ws_name = load_config(workspace_arg)
        client = SlackClient(
            creds["xoxc_token"],
            creds["xoxd_token"],
            creds.get("user_agent")
        )
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    try:
        if command == "auth":
            result = client.auth_test()
            if result.get("ok"):
                # Update session state on successful auth
                set_active_workspace(ws_name)
                result["_workspace"] = ws_name

        elif command == "channels":
            types = cmd_args[0] if cmd_args else "public_channel,private_channel,im,mpim"
            result = client.channels_list(types=types)
            if result.get("ok"):
                set_active_workspace(ws_name)
                result["_workspace"] = ws_name
                names = NameResolver(client, ws_name)
                names.remember_channels(result.get("channels", []))
                names.save()

        elif command == "users":
            result = client.users_list()
            if result.get("ok"):
                set_active_workspace(ws_name)
                result["_workspace"] = ws_name

        elif command == "history":
            if not cmd_args:
                print(json.dumps({"error": "channel_id required"}))
                sys.exit(1)
            channel = cmd_args[0]
            options = {}
            positional = []
            index = 0
            remaining = cmd_args[1:]
            while index < len(remaining):
                arg = remaining[index]
                if arg == "--with-replies":
                    options["with_replies"] = True
                    index += 1
                    continue
                if arg in ("--oldest", "--latest", "--cursor"):
                    if index + 1 >= len(remaining):
                        print(json.dumps({"error": f"{arg} requires a value"}))
                        sys.exit(1)
                    options[arg[2:]] = remaining[index + 1]
                    index += 2
                    continue
                if arg.startswith("--"):
                    print(json.dumps({"error": f"Unknown option: {arg}"}))
                    sys.exit(1)
                positional.append(arg)
                index += 1
            limit = int(positional[0]) if positional else 100
            result = client.conversations_history(
                channel, limit,
                oldest=options.get("oldest"),
                latest=options.get("latest"),
                cursor=options.get("cursor"),
            )
            if result.get("ok"):
                set_active_workspace(ws_name)
                record_channel_workspace(channel, ws_name)
                result["_workspace"] = ws_name
                if options.get("with_replies"):
                    for message in result.get("messages", []):
                        if message.get("reply_count") and message.get("thread_ts") == message.get("ts"):
                            thread = client.conversations_replies(channel, message["ts"])
                            if thread.get("ok"):
                                message["replies"] = [
                                    reply for reply in thread.get("messages", [])
                                    if reply.get("ts") != message["ts"]
                                ]
                if resolve:
                    names = NameResolver(client, ws_name)
                    result = {
                        "ok": True,
                        "_workspace": ws_name,
                        "channel": {"id": channel, "name": names.channel_name(channel)},
                        "has_more": result.get("has_more", False),
                        "next_cursor": (result.get("response_metadata") or {}).get("next_cursor") or None,
                        "messages": [names.message(m) for m in reversed(result.get("messages", []))],
                    }
                    names.save()
            else:
                channel_hint(result, ws_name)

        elif command == "counts":
            result = client.client_counts()
            if result.get("ok"):
                set_active_workspace(ws_name)
                result["_workspace"] = ws_name

        elif command == "download":
            if len(cmd_args) < 2:
                print(json.dumps({"error": "url and output_path required"}))
                sys.exit(1)
            max_bytes = int(cmd_args[2]) if len(cmd_args) > 2 else 4 * 1024 * 1024
            result = client.download_file(cmd_args[0], cmd_args[1], max_bytes)
            if result.get("ok"):
                set_active_workspace(ws_name)
                result["_workspace"] = ws_name

        elif command == "info":
            if not cmd_args:
                print(json.dumps({"error": "channel_id required"}))
                sys.exit(1)
            channel = cmd_args[0]
            result = client.conversations_info(channel)
            if result.get("ok"):
                set_active_workspace(ws_name)
                record_channel_workspace(channel, ws_name)
                result["_workspace"] = ws_name

        elif command == "replies":
            if len(cmd_args) < 2:
                print(json.dumps({"error": "channel_id and thread_ts required"}))
                sys.exit(1)
            channel = cmd_args[0]
            result = client.conversations_replies(channel, cmd_args[1])
            if result.get("ok"):
                set_active_workspace(ws_name)
                record_channel_workspace(channel, ws_name)
                result["_workspace"] = ws_name
                messages = result.get("messages", [])
                parent_ts = (messages[0] if messages else {}).get("thread_ts")
                if parent_ts and parent_ts != cmd_args[1]:
                    result["warning"] = (
                        f"{cmd_args[1]} is a reply, so only that reply was returned. "
                        f"Fetch the whole thread with its parent ts: replies {channel} {parent_ts}"
                    )
                if resolve:
                    names = NameResolver(client, ws_name)
                    resolved = {
                        "ok": True,
                        "_workspace": ws_name,
                        "channel": {"id": channel, "name": names.channel_name(channel)},
                        "thread_ts": cmd_args[1],
                        "messages": [names.message(m) for m in messages],
                    }
                    if "warning" in result:
                        resolved["warning"] = result["warning"]
                    result = resolved
                    names.save()
            else:
                channel_hint(result, ws_name)

        elif command == "thread-view":
            limit = 20
            current_ts = None
            rest = list(cmd_args)
            if rest and rest[0] != "--current-ts":
                limit = int(rest.pop(0))
            if len(rest) >= 2 and rest[0] == "--current-ts":
                current_ts = rest[1]
            result = client.thread_view(limit, current_ts)
            if result.get("ok"):
                set_active_workspace(ws_name)
                result["_workspace"] = ws_name

        elif command == "todos":
            result = client.saved_list()
            if result.get("ok"):
                set_active_workspace(ws_name)
                result["_workspace"] = ws_name
                if "--all" not in cmd_args:
                    result["saved_items"] = [i for i in result["saved_items"]
                                             if i.get("state") == "in_progress"]

        elif command == "mark-read":
            if len(cmd_args) != 3 or cmd_args[2] != "--confirm":
                print(json.dumps({
                    "error": "Explicit confirmation required",
                    "usage": "mark-read <channel_id> <message_ts> --confirm",
                }))
                sys.exit(1)
            channel, message_ts = cmd_args[0], cmd_args[1]
            if not re.fullmatch(r"[A-Z][A-Z0-9]+", channel):
                print(json.dumps({"error": "Invalid channel ID"}))
                sys.exit(1)
            if not re.fullmatch(r"\d{10,}\.\d{6}", message_ts):
                print(json.dumps({"error": "Invalid Slack message timestamp"}))
                sys.exit(1)
            info = client.conversations_info(channel)
            if not info.get("ok"):
                result = info
            else:
                current_last_read = str((info.get("channel") or {}).get("last_read") or "")
                if not re.fullmatch(r"\d{10,}\.\d{6}", current_last_read):
                    print(json.dumps({
                        "error": "Slack did not return a valid current read cursor; refusing to mark",
                    }))
                    sys.exit(1)
                if slack_ts_value(current_last_read) >= slack_ts_value(message_ts):
                    result = {
                        "ok": True,
                        "no_op": True,
                        "current_last_read": current_last_read,
                    }
                else:
                    result = client.conversations_mark(channel, message_ts)
                    if result.get("ok"):
                        result["previous_last_read"] = current_last_read
            if result.get("ok"):
                set_active_workspace(ws_name)
                record_channel_workspace(channel, ws_name)
                result["_workspace"] = ws_name
                result["marked_through"] = message_ts

        elif command == "mark-thread-read":
            if len(cmd_args) != 4 or cmd_args[3] != "--confirm":
                print(json.dumps({
                    "error": "Explicit confirmation required",
                    "usage": "mark-thread-read <channel_id> <thread_ts> <message_ts> --confirm",
                }))
                sys.exit(1)
            channel, thread_ts, message_ts = cmd_args[0], cmd_args[1], cmd_args[2]
            if not re.fullmatch(r"[A-Z][A-Z0-9]+", channel):
                print(json.dumps({"error": "Invalid channel ID"}))
                sys.exit(1)
            for value in (thread_ts, message_ts):
                if not re.fullmatch(r"\d{10,}\.\d{6}", value):
                    print(json.dumps({"error": f"Invalid Slack message timestamp: {value}"}))
                    sys.exit(1)
            replies = client.conversations_replies(channel, thread_ts)
            if not replies.get("ok"):
                result = replies
            else:
                root = (replies.get("messages") or [{}])[0]
                if root.get("ts") != thread_ts:
                    print(json.dumps({
                        "error": "thread_ts is not a thread parent; pass the parent's ts",
                    }))
                    sys.exit(1)
                current_last_read = str(root.get("last_read") or "")
                if not re.fullmatch(r"\d{10,}\.\d{6}", current_last_read):
                    print(json.dumps({
                        "error": "Slack returned no read cursor for this thread (not followed?); refusing to mark",
                    }))
                    sys.exit(1)
                if slack_ts_value(current_last_read) >= slack_ts_value(message_ts):
                    result = {
                        "ok": True,
                        "no_op": True,
                        "current_last_read": current_last_read,
                    }
                else:
                    result = client.thread_mark(channel, thread_ts, message_ts)
                    if result.get("ok"):
                        result["previous_last_read"] = current_last_read
            if result.get("ok"):
                set_active_workspace(ws_name)
                record_channel_workspace(channel, ws_name)
                result["_workspace"] = ws_name
                result["thread_ts"] = thread_ts
                result["marked_through"] = message_ts

        elif command == "search":
            if not cmd_args:
                print(json.dumps({"error": "query required"}))
                sys.exit(1)
            query = cmd_args[0]
            count = int(cmd_args[1]) if len(cmd_args) > 1 else 20
            result = client.search_messages(query, count)
            if result.get("ok"):
                set_active_workspace(ws_name)
                result["_workspace"] = ws_name
                if resolve:
                    names = NameResolver(client, ws_name)
                    matches = []
                    for match in (result.get("messages") or {}).get("matches", []):
                        channel = match.get("channel") or {}
                        if channel.get("is_im") or channel.get("is_mpim"):
                            channel_name = names.channel_name(channel.get("id"))
                        else:
                            channel_name = f"#{channel.get('name')}"
                        matches.append({
                            **names.message(match),
                            "channel": {"id": channel.get("id"), "name": channel_name},
                            "permalink": match.get("permalink"),
                        })
                    result = {
                        "ok": True,
                        "_workspace": ws_name,
                        "query": query,
                        "total": ((result.get("messages") or {}).get("paging") or {}).get("total"),
                        "messages": matches,
                    }
                    names.save()

        elif command == "search-page":
            if len(cmd_args) < 2:
                print(json.dumps({"error": "query and page required"}))
                sys.exit(1)
            query = cmd_args[0]
            page = int(cmd_args[1])
            count = int(cmd_args[2]) if len(cmd_args) > 2 else 100
            result = client.search_messages_paginated(query, page=page, count=count)
            if result.get("ok"):
                set_active_workspace(ws_name)
                result["_workspace"] = ws_name

        elif command == "send":
            if len(cmd_args) < 2:
                print(json.dumps({"error": "channel_id and text required"}))
                sys.exit(1)
            channel = cmd_args[0]
            text = agent_prefixed(cmd_args[1])
            thread_ts = cmd_args[2] if len(cmd_args) > 2 else None
            result = client.post_message(channel, text, thread_ts)
            if result.get("ok"):
                set_active_workspace(ws_name)
                record_channel_workspace(channel, ws_name)
                result["_workspace"] = ws_name

        elif command == "draft":
            if len(cmd_args) < 2:
                print(json.dumps({"error": "channel_id and a blocks JSON file required"}))
                sys.exit(1)
            open_it = "--open" in cmd_args
            thread_ts = None
            if "--thread" in cmd_args:
                k = cmd_args.index("--thread")
                thread_ts = cmd_args[k + 1]
                cmd_args = cmd_args[:k] + cmd_args[k + 2:]
            files = []
            while "--file" in cmd_args:
                k = cmd_args.index("--file")
                if k + 1 >= len(cmd_args) or not Path(cmd_args[k + 1]).is_file():
                    print(json.dumps({"error": "--file needs the path of an existing file"}))
                    sys.exit(1)
                files.append(cmd_args[k + 1])
                cmd_args = cmd_args[:k] + cmd_args[k + 2:]
            channel, src = [a for a in cmd_args if a != "--open"][:2]
            blocks = read_draft_source(src)
            file_ids = []
            for f in files:
                up = client.upload_private_file(f)
                if not up.get("ok"):
                    print(json.dumps({"error": "file upload failed", "file": f, "detail": up}))
                    sys.exit(1)
                file_ids.append(up["file_id"])
            result = client.create_draft(channel, blocks, thread_ts, file_ids)
            if not result.get("ok"):
                for fid in file_ids:
                    client.delete_file(fid)
            if result.get("ok"):
                set_active_workspace(ws_name)
                record_channel_workspace(channel, ws_name)
                result = {"ok": True, "draft_id": result["draft"]["id"], "channel": channel, "_workspace": ws_name,
                          **({"file_ids": file_ids} if file_ids else {})}
                team = workspace_team(ws_name)
                if thread_ts:
                    result["thread_ts"] = thread_ts
                if team.get("id"):
                    result["url"] = draft_url(team["id"], channel, thread_ts)
                    if open_it:
                        result["chrome_profile"] = open_in_chrome(result["url"], ws_name)
                        result["opened"] = True

        elif command == "drafts":
            listed = client.list_drafts()
            if not listed.get("ok"):
                print(json.dumps(listed))
                sys.exit(1)
            names = NameResolver(client, ws_name)
            drafts = []
            for d in listed.get("drafts", []):
                dest = (d.get("destinations") or [{}])[0]
                if cmd_args and dest.get("channel_id") != cmd_args[0]:
                    continue
                text = re.sub(r"\s+", " ", rich_text_plain(d.get("blocks"), names))
                drafts.append({
                    "draft_id": d["id"], "channel": dest.get("channel_id"),
                    "channel_name": names.channel_name(dest["channel_id"]) if dest.get("channel_id") else None,
                    **({"thread_ts": dest["thread_ts"]} if dest.get("thread_ts") else {}),
                    "updated": format_ts(d.get("last_updated_ts")),
                    "text": text[:80].rstrip() + ("…" if len(text) > 80 else ""),
                    **({"file_ids": d["file_ids"]} if d.get("file_ids") else {}),
                    **({"scheduled": format_ts(d["date_scheduled"])} if d.get("date_scheduled") else {}),
                })
            names.save()
            result = {"ok": True, "_workspace": ws_name, "count": len(drafts), "drafts": drafts,
                      **({"has_more": True} if listed.get("has_more") else {})}

        elif command == "draft-edit":
            open_it = "--open" in cmd_args
            positional = [a for a in cmd_args if a != "--open"]
            if len(positional) != 2:
                print(json.dumps({"error": "draft_id and a mrkdwn or blocks JSON file required"}))
                sys.exit(1)
            draft_id, src = positional
            listed = client.list_drafts()
            if not listed.get("ok"):
                print(json.dumps(listed))
                sys.exit(1)
            draft = next((d for d in listed.get("drafts", []) if d["id"] == draft_id), None)
            if not draft:
                print(json.dumps({"error": f"no unsent draft {draft_id} in {ws_name} (sent, deleted, or "
                                           "another workspace); see `drafts`"}))
                sys.exit(1)
            result = client.update_draft(draft, read_draft_source(src))
            if result.get("ok"):
                dest = (draft.get("destinations") or [{}])[0]
                channel, thread_ts = dest.get("channel_id"), dest.get("thread_ts")
                result = {"ok": True, "draft_id": draft_id, "channel": channel, "_workspace": ws_name,
                          **({"thread_ts": thread_ts} if thread_ts else {})}
                team = workspace_team(ws_name)
                if team.get("id") and channel:
                    result["url"] = draft_url(team["id"], channel, thread_ts)
                    if open_it:
                        result["chrome_profile"] = open_in_chrome(result["url"], ws_name)
                        result["opened"] = True

        elif command == "draft-delete":
            if not cmd_args:
                print(json.dumps({"error": "draft_id required (from draft's output, or `drafts`)"}))
                sys.exit(1)
            result = client.delete_draft(cmd_args[0])

        elif command == "react":
            remove = "--remove" in cmd_args
            positional = [arg for arg in cmd_args if arg != "--remove"]
            if len(positional) != 3:
                print(json.dumps({"error": "channel_id, message_ts and emoji required"}))
                sys.exit(1)
            channel, timestamp, name = positional
            result = client.react(channel, timestamp, name.strip(":"), remove=remove)
            if result.get("ok"):
                set_active_workspace(ws_name)
                record_channel_workspace(channel, ws_name)
                result["_workspace"] = ws_name

        elif command == "focus":
            action = cmd_args[0] if cmd_args else "status"
            if action == "on" and len(cmd_args) == 2 and cmd_args[1].isdigit() and int(cmd_args[1]) > 0:
                result = client.dnd_set_snooze(int(cmd_args[1]))
            elif action == "until" and len(cmd_args) == 2:
                try:
                    minutes = minutes_until(cmd_args[1])
                except ValueError as error:
                    print(json.dumps({"error": str(error)}))
                    sys.exit(1)
                result = client.dnd_set_snooze(minutes)
            elif action == "off" and len(cmd_args) == 1:
                result = client.dnd_end_snooze()
                if not result.get("ok") and result.get("error") == "snooze_not_active":
                    result = {"ok": True}
            elif action != "status" or len(cmd_args) > 1:
                print(json.dumps({
                    "error": "Usage: focus [status] | focus on <minutes> | focus until <HH:MM> | focus off",
                }))
                sys.exit(1)
            else:
                result = {"ok": True}
            if result.get("ok") and action != "status":
                profile = client.profile_get()
                current = (profile.get("profile") or {}) if profile.get("ok") else {}
                is_focus = (current.get("status_text"), current.get("status_emoji")) == FOCUS_STATUS
                if action == "off":
                    if is_focus:
                        result = client.set_status("", "")
                elif current.get("status_text") and not is_focus:
                    result = {"ok": True, "status_kept": current.get("status_text")}
                else:
                    result = client.set_status(*FOCUS_STATUS, int(time.time()) + 60 * (
                        int(cmd_args[1]) if action == "on" else minutes))
            if result.get("ok"):
                set_active_workspace(ws_name)
                kept = result.get("status_kept")
                info = client.dnd_info()
                result = dnd_summary(info, ws_name) if info.get("ok") else info
                if info.get("ok"):
                    profile = client.profile_get().get("profile") or {}
                    result["status"] = " ".join(
                        part for part in (profile.get("status_emoji"), profile.get("status_text")) if part
                    ) or None
                    if kept:
                        result["note"] = "Kept your existing status rather than replacing it"

        elif command == "permalink":
            if len(cmd_args) < 2:
                print(json.dumps({"error": "channel_id and message_ts required"}))
                sys.exit(1)
            channel = cmd_args[0]
            message_ts = cmd_args[1]
            workspace = cmd_args[2] if len(cmd_args) > 2 else ws_name
            link_style = cmd_args[3] if len(cmd_args) > 3 else get_link_style()
            permalink = client.get_permalink(channel, message_ts, workspace, link_style)
            result = {"ok": True, "permalink": permalink, "_workspace": ws_name}

        else:
            print(json.dumps({"error": f"Unknown command: {command}. Run `slack help` for the command list."}))
            sys.exit(1)

        print(json.dumps(result, indent=2, ensure_ascii=resolve is False))

    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
