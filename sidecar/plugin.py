"""CodeRabbit sidecar plugin for el-sidecar.

Polls GitHub API for CodeRabbit bot activity on watched pull requests.
Uses the sidecar's runtime watch API — agents POST /watch with full-qualified
PR URLs, and this plugin polls all watched PRs for new reviews, comments,
and thread replies.

Registers itself with el-sidecar via register(sidecar).

Env vars:
    CODERABBIT_POLL_INTERVAL  — Polling interval in seconds (default: 30)
    CODERABBIT_BOT_LOGIN      — GitHub login to filter (default: coderabbitai[bot])

Requires: gh CLI (authenticated)
"""

import json
import os
import re
import subprocess
import sys
import threading
import time

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

POLL_INTERVAL = int(os.environ.get('CODERABBIT_POLL_INTERVAL', '30'))
BOT_LOGIN = os.environ.get('CODERABBIT_BOT_LOGIN', 'coderabbitai[bot]')

# Sidecar reference (set during register())
_sidecar = None

# Watched PRs: {url: {"owner": str, "repo": str, "number": int, "since": str}}
_watches = {}
_watches_lock = threading.Lock()

# ---------------------------------------------------------------------------
# PR URL parsing
# ---------------------------------------------------------------------------

_PR_URL_RE = re.compile(
    r'https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)'
)


def _parse_pr_url(url):
    """Parse a full-qualified GitHub PR URL into (owner, repo, number)."""
    m = _PR_URL_RE.match(url)
    if not m:
        return None
    return m.group(1), m.group(2), int(m.group(3))


# ---------------------------------------------------------------------------
# GitHub API helper
# ---------------------------------------------------------------------------


def _gh_api(endpoint):
    """Call GitHub API via gh CLI. Returns parsed JSON or None."""
    try:
        result = subprocess.run(
            ['gh', 'api', endpoint],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None


def _gh_graphql(query):
    """Call GitHub GraphQL API via gh CLI. Returns parsed JSON or None."""
    try:
        result = subprocess.run(
            ['gh', 'api', 'graphql', '-f', f'query={query}'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None


# ---------------------------------------------------------------------------
# Event collection for a single PR
# ---------------------------------------------------------------------------


def _poll_pr(owner, repo, number, since):
    """Poll a single PR for new CodeRabbit activity since timestamp.
    Returns list of event dicts and the new high-water timestamp."""
    events = []
    full_repo = f"{owner}/{repo}"
    new_since = since

    # 1. Reviews
    reviews = _gh_api(f"repos/{full_repo}/pulls/{number}/reviews")
    if reviews:
        for r in reviews:
            if r.get('user', {}).get('login') != BOT_LOGIN:
                continue
            ts = r.get('submitted_at', '')
            if ts <= since:
                continue
            events.append({
                'type': 'review',
                'state': r.get('state', ''),
                'timestamp': ts,
                'body': r.get('body', ''),
                'pr_url': f"https://github.com/{full_repo}/pull/{number}",
                'repo': full_repo,
                'pr_number': number,
            })
            if ts > new_since:
                new_since = ts

    # 2. Build thread node ID map via GraphQL
    thread_map = {}
    gql = _gh_graphql(f'''{{
        repository(owner: "{owner}", name: "{repo}") {{
            pullRequest(number: {number}) {{
                reviewThreads(first: 100) {{ nodes {{
                    id
                    comments(first: 1) {{ nodes {{ databaseId }} }}
                }} }}
            }}
        }}
    }}''')
    if gql:
        try:
            threads = gql['data']['repository']['pullRequest']['reviewThreads']['nodes']
            for t in threads:
                comments = t.get('comments', {}).get('nodes', [])
                if comments:
                    db_id = str(comments[0].get('databaseId', ''))
                    if db_id:
                        thread_map[db_id] = t['id']
        except (KeyError, TypeError, IndexError):
            pass

    # 3. Inline review comments + thread replies
    comments = _gh_api(f"repos/{full_repo}/pulls/{number}/comments")
    if comments:
        for c in comments:
            if c.get('user', {}).get('login') != BOT_LOGIN:
                continue
            ts = c.get('created_at', '')
            if ts <= since:
                continue

            evt = {
                'timestamp': ts,
                'path': c.get('path', ''),
                'comment_id': c.get('id'),
                'body': c.get('body', ''),
                'pr_url': f"https://github.com/{full_repo}/pull/{number}",
                'repo': full_repo,
                'pr_number': number,
            }

            if c.get('in_reply_to_id'):
                evt['type'] = 'thread_reply'
                evt['in_reply_to'] = c['in_reply_to_id']
                evt['thread_node_id'] = thread_map.get(
                    str(c['in_reply_to_id']), ''
                )
            else:
                evt['type'] = 'comment'

            events.append(evt)
            if ts > new_since:
                new_since = ts

    # 4. Top-level issue comments
    issue_comments = _gh_api(f"repos/{full_repo}/issues/{number}/comments")
    if issue_comments:
        for c in issue_comments:
            if c.get('user', {}).get('login') != BOT_LOGIN:
                continue
            ts = c.get('created_at', '')
            if ts <= since:
                continue
            events.append({
                'type': 'issue_comment',
                'timestamp': ts,
                'body': c.get('body', ''),
                'pr_url': f"https://github.com/{full_repo}/pull/{number}",
                'repo': full_repo,
                'pr_number': number,
            })
            if ts > new_since:
                new_since = ts

    return events, new_since


# ---------------------------------------------------------------------------
# Poller (runs in background thread)
# ---------------------------------------------------------------------------


def poll_coderabbit():
    """Background poller: check all watched PRs for CodeRabbit activity."""
    sys.stderr.write(f"[el-coderabbit] Polling every {POLL_INTERVAL}s\n")

    while True:
        time.sleep(POLL_INTERVAL)

        with _watches_lock:
            watched = dict(_watches)

        if not watched:
            continue

        for url, info in watched.items():
            try:
                events, new_since = _poll_pr(
                    info['owner'], info['repo'], info['number'], info['since']
                )

                # Update watermark
                with _watches_lock:
                    if url in _watches:
                        _watches[url]['since'] = new_since

                # Insert events into sidecar
                for evt in events:
                    summary = f"[{evt['type']}] {evt.get('state', '')} {evt['repo']}#{evt['pr_number']}"
                    _sidecar['insert_event'](
                        source='coderabbit',
                        text=json.dumps(evt),
                        summary=summary.strip(),
                    )
                    _sidecar['notify_waiters']()

            except Exception as e:
                sys.stderr.write(f"[el-coderabbit] poll error for {url}: {e}\n")


# ---------------------------------------------------------------------------
# Watch handlers
# ---------------------------------------------------------------------------


def add_watch(url):
    """Add a PR to the watch list."""
    parsed = _parse_pr_url(url)
    if not parsed:
        return {"ok": False, "error": f"Invalid PR URL: {url}"}

    owner, repo, number = parsed
    with _watches_lock:
        if url in _watches:
            return {"ok": True, "already_watching": True}
        _watches[url] = {
            'owner': owner,
            'repo': repo,
            'number': number,
            'since': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        }

    sys.stderr.write(f"[el-coderabbit] Watching {owner}/{repo}#{number}\n")
    return {"ok": True, "watching": url}


def remove_watch(url):
    """Remove a PR from the watch list."""
    with _watches_lock:
        removed = _watches.pop(url, None)
    return {"ok": True, "removed": removed is not None}


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def register(sidecar):
    """Register this CodeRabbit plugin with el-sidecar."""
    global _sidecar
    _sidecar = sidecar

    # Register watch handler (enables POST /watch {"plugin": "coderabbit", ...})
    sidecar['register_watch_handler']('coderabbit', add_watch, remove_watch)

    # Register background poller
    sidecar['register_poller']('coderabbit', poll_coderabbit)

    sys.stderr.write(f"[el-coderabbit] Registered (bot={BOT_LOGIN}, poll={POLL_INTERVAL}s)\n")
