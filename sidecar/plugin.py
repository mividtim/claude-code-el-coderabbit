"""CodeRabbit sidecar plugin for el-sidecar.

Provides:
    - Event polling: reviews, comments, thread replies, issue comments
    - P0: Unresolved thread count on every event
    - P1: CodeRabbit check-run completion detection
    - P2: Thread resolution via POST /coderabbit/resolve-thread
    - P3: Composite "all clear" signal (CI green + CR APPROVED + 0 unresolved)
    - P4: Staleness annotation on review events
    - P5: Push-aware polling backoff

Watches GitHub PRs for CodeRabbit bot activity and the combined state of CI
checks, CodeRabbit review, and unresolved thread count. Emits a PR_READY
event when all three conditions are met simultaneously.

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
import urllib.parse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

POLL_INTERVAL = int(os.environ.get('CODERABBIT_POLL_INTERVAL', '30'))
BOT_LOGIN = os.environ.get('CODERABBIT_BOT_LOGIN', 'coderabbitai[bot]')

# Sidecar reference (set during register())
_sidecar = None

# Watched PRs: {url: {"owner": str, "repo": str, "number": int, "since": str,
#                      "last_state": dict, "last_head_sha": str, "last_push_time": float}}
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
# GitHub API helpers
# ---------------------------------------------------------------------------


def _gh_api(endpoint):
    """Call GitHub REST API via gh CLI. Returns parsed JSON or None."""
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
# P0: Unresolved thread count
# ---------------------------------------------------------------------------


def _get_thread_info(owner, repo, number):
    """Get thread map and unresolved count via GraphQL.

    Returns (thread_map, unresolved_count) where thread_map maps
    root comment database IDs to thread node IDs.
    """
    gql = _gh_graphql(f'''{{
        repository(owner: "{owner}", name: "{repo}") {{
            pullRequest(number: {number}) {{
                reviewThreads(first: 100) {{ nodes {{
                    id
                    isResolved
                    comments(first: 1) {{ nodes {{ databaseId }} }}
                }} }}
            }}
        }}
    }}''')
    thread_map = {}
    unresolved = -1
    if gql:
        try:
            threads = gql['data']['repository']['pullRequest']['reviewThreads']['nodes']
            unresolved = 0
            for t in threads:
                if not t.get('isResolved', True):
                    unresolved += 1
                comments = t.get('comments', {}).get('nodes', [])
                if comments:
                    db_id = str(comments[0].get('databaseId', ''))
                    if db_id:
                        thread_map[db_id] = t['id']
        except (KeyError, TypeError, IndexError):
            pass
    return thread_map, unresolved


# ---------------------------------------------------------------------------
# P1: CodeRabbit check-run completion detection
# ---------------------------------------------------------------------------

# Per-PR tracking of last known check status: {url: "status:conclusion"}
_last_cr_check = {}


def _check_coderabbit_check_run(owner, repo, number, head_sha, since, url):
    """Check if the CodeRabbit check run has completed since last poll.

    Returns a review_complete event dict or None.
    """
    if not head_sha:
        return None
    full_repo = f"{owner}/{repo}"
    check_data = _gh_api(f"repos/{full_repo}/commits/{head_sha}/check-runs")
    if not check_data:
        return None
    cr_check = None
    for run in check_data.get('check_runs', []):
        app_slug = run.get('app', {}).get('slug', '')
        name = run.get('name', '')
        if app_slug == 'coderabbitai' or 'coderabbit' in name.lower():
            cr_check = run
            break
    if not cr_check:
        return None
    status = cr_check.get('status', '')
    conclusion = cr_check.get('conclusion', '')
    completed_at = cr_check.get('completed_at', '')
    current_state = f"{status}:{conclusion}"
    last_state = _last_cr_check.get(url, '')
    _last_cr_check[url] = current_state
    if status == 'completed' and current_state != last_state:
        timestamp = completed_at or time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        if timestamp > since:
            return {
                'type': 'review_complete',
                'conclusion': conclusion,
                'timestamp': timestamp,
                'pr_url': f"https://github.com/{full_repo}/pull/{number}",
                'repo': full_repo,
                'pr_number': number,
            }
    return None


# ---------------------------------------------------------------------------
# P2: Thread resolution
# ---------------------------------------------------------------------------


def _resolve_thread(thread_node_id):
    """Resolve a review thread via GitHub GraphQL mutation."""
    query = '''
    mutation {
      resolveReviewThread(input: {threadId: "%s"}) {
        thread {
          isResolved
        }
      }
    }
    ''' % thread_node_id
    result = _gh_graphql(query)
    if result is None:
        return {"ok": False, "error": "GraphQL request failed"}
    errors = result.get('errors')
    if errors:
        return {"ok": False, "error": errors[0].get('message', str(errors))}
    is_resolved = (
        result.get('data', {})
        .get('resolveReviewThread', {})
        .get('thread', {})
        .get('isResolved', False)
    )
    return {"ok": True, "resolved": is_resolved, "thread_id": thread_node_id}


def _handle_resolve_thread(body):
    """Handle POST /coderabbit/resolve-thread.

    Body: {"thread_id": "PRRT_..."} or {"thread_ids": ["PRRT_...", ...]}
    """
    if not body:
        return 400, {"error": "Request body required"}
    data = json.loads(body) if isinstance(body, str) else body
    thread_ids = data.get('thread_ids', [])
    single_id = data.get('thread_id')
    if single_id:
        thread_ids.append(single_id)
    if not thread_ids:
        return 400, {"error": "thread_id or thread_ids required"}
    results = []
    for tid in thread_ids:
        r = _resolve_thread(tid)
        results.append(r)
        sys.stderr.write(
            f"[el-coderabbit] resolve {tid}: "
            f"{'ok' if r['ok'] else r.get('error', 'failed')}\n"
        )
    all_ok = all(r['ok'] for r in results)
    return 200, {"ok": all_ok, "results": results}


# ---------------------------------------------------------------------------
# P3: Composite "all clear" signal
# ---------------------------------------------------------------------------


def _get_pr_composite_state(owner, repo, number):
    """Check CI status, CodeRabbit review state, and unresolved thread count.

    Returns a dict with:
        ci_status: "success" | "failure" | "pending" | "unknown"
        cr_review: "APPROVED" | "CHANGES_REQUESTED" | "COMMENTED" | "none"
        cr_stale: True if the latest approval predates the latest push
        unresolved_threads: int
        head_sha: str
        all_clear: bool (True when CI green + CR APPROVED + 0 unresolved + not stale)
    """
    full_repo = f"{owner}/{repo}"
    state = {
        'ci_status': 'unknown',
        'cr_review': 'none',
        'cr_stale': False,
        'unresolved_threads': -1,
        'head_sha': '',
        'all_clear': False,
    }
    # Get PR head SHA and latest commit date
    pr_data = _gh_api(f"repos/{full_repo}/pulls/{number}")
    if not pr_data:
        return state
    head_sha = pr_data.get('head', {}).get('sha', '')
    state['head_sha'] = head_sha
    if not head_sha:
        return state
    # Get latest commit date for staleness detection
    commit_data = _gh_api(f"repos/{full_repo}/commits/{head_sha}")
    latest_commit_date = ''
    if commit_data:
        latest_commit_date = (
            commit_data.get('commit', {})
            .get('committer', {})
            .get('date', '')
        )
    # --- CI status: check runs + commit statuses ---
    ci_ok = True
    ci_pending = False
    check_data = _gh_api(f"repos/{full_repo}/commits/{head_sha}/check-runs")
    if check_data:
        for run in check_data.get('check_runs', []):
            run_status = run.get('status', '')
            conclusion = run.get('conclusion', '')
            # Skip the CodeRabbit check itself — we track that separately
            app_slug = run.get('app', {}).get('slug', '')
            name = run.get('name', '')
            if app_slug == 'coderabbitai' or 'coderabbit' in name.lower():
                continue
            if run_status != 'completed':
                ci_pending = True
            elif conclusion not in ('success', 'neutral', 'skipped'):
                ci_ok = False
    status_data = _gh_api(f"repos/{full_repo}/commits/{head_sha}/status")
    if status_data:
        for s in status_data.get('statuses', []):
            s_state = s.get('state', '')
            if s_state == 'pending':
                ci_pending = True
            elif s_state in ('failure', 'error'):
                ci_ok = False
    if not ci_ok:
        state['ci_status'] = 'failure'
    elif ci_pending:
        state['ci_status'] = 'pending'
    else:
        state['ci_status'] = 'success'
    # --- CodeRabbit review state ---
    reviews = _gh_api(f"repos/{full_repo}/pulls/{number}/reviews?per_page=100")
    if reviews:
        cr_reviews = [
            r for r in reviews
            if r.get('user', {}).get('login') == BOT_LOGIN
            and r.get('state') in ('APPROVED', 'CHANGES_REQUESTED', 'COMMENTED')
        ]
        if cr_reviews:
            latest = max(cr_reviews, key=lambda r: r.get('submitted_at', ''))
            state['cr_review'] = latest.get('state', 'none')
            review_time = latest.get('submitted_at', '')
            if latest_commit_date and review_time and review_time < latest_commit_date:
                state['cr_stale'] = True
    # --- Unresolved thread count ---
    _, unresolved = _get_thread_info(owner, repo, number)
    state['unresolved_threads'] = unresolved
    # --- All clear? ---
    state['all_clear'] = (
        state['ci_status'] == 'success'
        and state['cr_review'] == 'APPROVED'
        and not state['cr_stale']
        and state['unresolved_threads'] == 0
    )
    return state


# ---------------------------------------------------------------------------
# Event collection for a single PR (with P0, P1, P4 enhancements)
# ---------------------------------------------------------------------------


def _poll_pr(owner, repo, number, since, url):
    """Poll a single PR for new CodeRabbit activity since timestamp.
    Returns list of event dicts and the new high-water timestamp."""
    events = []
    full_repo = f"{owner}/{repo}"
    new_since = since
    # P0: Get thread info and unresolved count
    thread_map, unresolved_count = _get_thread_info(owner, repo, number)
    # P4: Get head SHA and commit date for staleness detection
    pr_data = _gh_api(f"repos/{full_repo}/pulls/{number}")
    head_sha = ''
    latest_commit_date = ''
    if pr_data:
        head_sha = pr_data.get('head', {}).get('sha', '')
        if head_sha:
            commit_data = _gh_api(f"repos/{full_repo}/commits/{head_sha}")
            if commit_data:
                latest_commit_date = (
                    commit_data.get('commit', {})
                    .get('committer', {})
                    .get('date', '')
                )
    # P1: Check CodeRabbit check-run completion
    cr_event = _check_coderabbit_check_run(owner, repo, number, head_sha, since, url)
    if cr_event:
        cr_event['unresolved_threads'] = unresolved_count
        events.append(cr_event)
        ts = cr_event.get('timestamp', '')
        if ts > new_since:
            new_since = ts
    # 1. Reviews (with P4 staleness annotation)
    reviews = _gh_api(f"repos/{full_repo}/pulls/{number}/reviews")
    if reviews:
        for r in reviews:
            if r.get('user', {}).get('login') != BOT_LOGIN:
                continue
            ts = r.get('submitted_at', '')
            if ts <= since:
                continue
            stale = False
            if latest_commit_date and ts < latest_commit_date:
                stale = True
            events.append({
                'type': 'review',
                'state': r.get('state', ''),
                'stale': stale,
                'timestamp': ts,
                'body': r.get('body', ''),
                'unresolved_threads': unresolved_count,
                'pr_url': f"https://github.com/{full_repo}/pull/{number}",
                'repo': full_repo,
                'pr_number': number,
            })
            if ts > new_since:
                new_since = ts
    # 2. Inline review comments + thread replies
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
                'unresolved_threads': unresolved_count,
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
    # 3. Top-level issue comments
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
                'unresolved_threads': unresolved_count,
                'pr_url': f"https://github.com/{full_repo}/pull/{number}",
                'repo': full_repo,
                'pr_number': number,
            })
            if ts > new_since:
                new_since = ts
    return events, new_since, head_sha


# ---------------------------------------------------------------------------
# P5: Push-aware polling backoff
# ---------------------------------------------------------------------------


def _get_effective_interval(info):
    """Return the polling interval, doubled within 3 minutes of a push."""
    last_push = info.get('last_push_time', 0)
    if last_push > 0:
        elapsed = time.time() - last_push
        if elapsed < 180:
            return POLL_INTERVAL * 2
    return POLL_INTERVAL


# ---------------------------------------------------------------------------
# Poller (runs in background thread)
# ---------------------------------------------------------------------------


def _poll_coderabbit():
    """Background poller: check all watched PRs for CodeRabbit activity
    and composite state changes."""
    sys.stderr.write(f"[el-coderabbit] Polling every {POLL_INTERVAL}s\n")
    while True:
        time.sleep(POLL_INTERVAL)
        with _watches_lock:
            watched = dict(_watches)
        if not watched:
            continue
        for url, info in watched.items():
            try:
                # P5: Use adaptive interval per PR
                effective = _get_effective_interval(info)
                # Skip this PR if we polled it too recently
                last_poll = info.get('last_poll_time', 0)
                if last_poll > 0 and (time.time() - last_poll) < effective:
                    continue
                events, new_since, head_sha = _poll_pr(
                    info['owner'], info['repo'], info['number'], info['since'], url
                )
                # P5: Detect new pushes
                last_sha = info.get('last_head_sha', '')
                if head_sha and head_sha != last_sha:
                    if last_sha:
                        with _watches_lock:
                            if url in _watches:
                                _watches[url]['last_push_time'] = time.time()
                    with _watches_lock:
                        if url in _watches:
                            _watches[url]['last_head_sha'] = head_sha
                # Update watermark and poll time
                with _watches_lock:
                    if url in _watches:
                        _watches[url]['since'] = new_since
                        _watches[url]['last_poll_time'] = time.time()
                # P3: Check composite state for transitions
                composite = _get_pr_composite_state(
                    info['owner'], info['repo'], info['number']
                )
                last_state = info.get('last_state', {})
                # All-clear transition
                was_clear = last_state.get('all_clear', False)
                now_clear = composite['all_clear']
                if now_clear and not was_clear:
                    events.append({
                        'type': 'pr_ready',
                        'ci_status': composite['ci_status'],
                        'cr_review': composite['cr_review'],
                        'unresolved_threads': composite['unresolved_threads'],
                        'head_sha': composite['head_sha'],
                        'pr_url': url,
                        'repo': f"{info['owner']}/{info['repo']}",
                        'pr_number': info['number'],
                    })
                # CR review state changed
                old_review = last_state.get('cr_review', 'none')
                new_review = composite['cr_review']
                if new_review != old_review and new_review != 'none':
                    events.append({
                        'type': 'cr_review_changed',
                        'cr_review': new_review,
                        'cr_stale': composite['cr_stale'],
                        'unresolved_threads': composite['unresolved_threads'],
                        'head_sha': composite['head_sha'],
                        'pr_url': url,
                        'repo': f"{info['owner']}/{info['repo']}",
                        'pr_number': info['number'],
                    })
                # CI status changed
                old_ci = last_state.get('ci_status', 'unknown')
                new_ci = composite['ci_status']
                if new_ci != old_ci and new_ci != 'unknown':
                    events.append({
                        'type': 'ci_status_changed',
                        'ci_status': new_ci,
                        'head_sha': composite['head_sha'],
                        'pr_url': url,
                        'repo': f"{info['owner']}/{info['repo']}",
                        'pr_number': info['number'],
                    })
                # Update stored composite state
                with _watches_lock:
                    if url in _watches:
                        _watches[url]['last_state'] = composite
                # Insert events into sidecar
                for evt in events:
                    if evt['type'] == 'pr_ready':
                        summary = (
                            f"[ALL CLEAR] {evt['repo']}#{evt['pr_number']} — "
                            f"CI green, CR APPROVED, 0 unresolved threads"
                        )
                    elif evt['type'] == 'cr_review_changed':
                        stale_tag = " (STALE)" if evt.get('cr_stale') else ""
                        summary = (
                            f"[{evt['cr_review']}{stale_tag}] CodeRabbit on "
                            f"{evt['repo']}#{evt['pr_number']} "
                            f"({evt.get('unresolved_threads', '?')} unresolved)"
                        )
                    elif evt['type'] == 'ci_status_changed':
                        summary = (
                            f"[CI {evt['ci_status']}] "
                            f"{evt['repo']}#{evt['pr_number']}"
                        )
                    elif evt['type'] == 'review_complete':
                        summary = (
                            f"[REVIEW COMPLETE] {evt['repo']}#{evt['pr_number']} "
                            f"conclusion={evt.get('conclusion', '?')}"
                        )
                    else:
                        summary = (
                            f"[{evt['type']}] {evt.get('state', '')} "
                            f"{evt['repo']}#{evt['pr_number']}"
                        ).strip()
                    _sidecar['insert_event'](
                        source='coderabbit',
                        text=json.dumps(evt),
                        summary=summary,
                    )
                    _sidecar['notify_waiters']()
            except Exception as e:
                sys.stderr.write(f"[el-coderabbit] poll error for {url}: {e}\n")


# ---------------------------------------------------------------------------
# Watch handlers
# ---------------------------------------------------------------------------


def _add_watch(url):
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
            'last_state': {},
            'last_head_sha': '',
            'last_push_time': 0,
            'last_poll_time': 0,
        }
    sys.stderr.write(f"[el-coderabbit] Watching {owner}/{repo}#{number}\n")
    return {"ok": True, "watching": url}


def _remove_watch(url):
    """Remove a PR from the watch list."""
    with _watches_lock:
        removed = _watches.pop(url, None)
    # Clean up check-run tracking
    _last_cr_check.pop(url, None)
    return {"ok": True, "removed": removed is not None}


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def _route_resolve_thread(method, path, headers, body):
    """Handle POST /coderabbit/resolve-thread."""
    if method != 'POST':
        return 405, {"error": "Method not allowed"}
    status, result = _handle_resolve_thread(body)
    return status, result


def _route_pr_status(method, path, headers, body):
    """Handle GET /coderabbit/status?pr=<url>.

    Returns the composite state for a specific PR without watching it.
    """
    if method != 'GET':
        return 405, {"error": "Method not allowed"}
    parsed = urllib.parse.urlparse(path)
    params = urllib.parse.parse_qs(parsed.query)
    pr_url = params.get('pr', [None])[0]
    if not pr_url:
        return 400, {"error": "pr query parameter required (full GitHub PR URL)"}
    parts = _parse_pr_url(pr_url)
    if not parts:
        return 400, {"error": f"Invalid PR URL: {pr_url}"}
    owner, repo, number = parts
    state = _get_pr_composite_state(owner, repo, number)
    state['pr_url'] = pr_url
    state['repo'] = f"{owner}/{repo}"
    state['pr_number'] = number
    return 200, state


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def register(sidecar):
    """Register the CodeRabbit plugin with el-sidecar."""
    global _sidecar
    _sidecar = sidecar
    # P2: Thread resolution endpoint
    sidecar['register_route']('POST', '/coderabbit/resolve-thread', _route_resolve_thread)
    # P3: PR status endpoint (one-shot query, no watch needed)
    sidecar['register_route']('GET', '/coderabbit/status', _route_pr_status)
    # Watch handler for composite monitoring
    sidecar['register_watch_handler']('coderabbit', _add_watch, _remove_watch)
    # Background poller for events + composite state changes
    sidecar['register_poller']('coderabbit', _poll_coderabbit)
    sys.stderr.write(
        f"[el-coderabbit] Registered sidecar plugin v2.1.0 "
        f"(bot={BOT_LOGIN}, poll={POLL_INTERVAL}s, "
        f"routes: resolve-thread, status)\n"
    )
