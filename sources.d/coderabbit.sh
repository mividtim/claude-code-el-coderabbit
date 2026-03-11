#!/bin/bash
# coderabbit — Poll for new CodeRabbit activity on a GitHub pull request.
#
# Community event source for claude-code-event-listeners.
# Install: /el:register ./coderabbit.sh
#
# Args: <pr-number> [since-timestamp]
# Env:  REPO (optional, defaults to current gh repo)
#       POLL_INTERVAL (optional, default 30s)
# Requires: gh CLI (authenticated), jq
#
# Event Source Protocol:
#   Polls GitHub API every POLL_INTERVAL seconds for new CodeRabbit reviews,
#   inline review comments (including thread replies), or top-level issue
#   comments. Returns ALL events since the given timestamp (sorted by time),
#   then exits. This enables "catch-up" mode: when launched, immediately
#   returns any pending events the caller hasn't seen yet.
#
# v2.1.0 additions:
#   - UNRESOLVED_THREADS <count> on every event (P0)
#   - REVIEW_COMPLETE event when CodeRabbit check run finishes (P1)
#   - STALE true/false on review events (P4)
#   - Push-aware polling backoff (P5)
#
# Output format (multiple events separated by ---EVENT---):
#   ---EVENT---
#   TYPE <review|comment|thread_reply|issue_comment|review_complete>
#   STATE <APPROVED|CHANGES_REQUESTED|COMMENTED> (reviews only)
#   STALE <true|false> (reviews only — true if approval predates latest push)
#   TIMESTAMP <iso-timestamp>
#   PATH <file-path> (comments only)
#   COMMENT_ID <numeric-id> (comments and thread replies)
#   IN_REPLY_TO <numeric-id> (thread replies only — the root comment ID)
#   THREAD_NODE_ID <graphql-node-id> (thread replies — for resolveReviewThread)
#   UNRESOLVED_THREADS <count> (all events)
#   BODY
#   <review/comment body>
#   ---EVENT---
#   ...

set -euo pipefail

command -v gh &>/dev/null || { echo "ERROR: gh CLI not installed" >&2; exit 1; }
command -v jq &>/dev/null || { echo "ERROR: jq not installed" >&2; exit 1; }

PR="${1:?Usage: coderabbit.sh <pr-number> [since-timestamp]}"
REPO="${REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
SINCE="${2:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
INTERVAL="${POLL_INTERVAL:-30}"
BOT="coderabbitai[bot]"

OWNER="${REPO%%/*}"
NAME="${REPO##*/}"

# Temporary file to collect all events (timestamp-prefixed for sorting)
EVENTS_FILE=$(mktemp)
trap 'rm -f "$EVENTS_FILE"' EXIT

# --- P5: Push-aware polling backoff ---
# Track the latest push timestamp. Right after a push, CodeRabbit takes 2-5
# minutes to start reviewing. Poll less aggressively during that window.
LAST_KNOWN_HEAD_SHA=""
LAST_PUSH_TIME=0

get_effective_interval() {
  local now
  now=$(date +%s)
  local elapsed=$(( now - LAST_PUSH_TIME ))
  # Within 3 minutes of a push, poll at 2x the normal interval
  if [ "$LAST_PUSH_TIME" -gt 0 ] && [ "$elapsed" -lt 180 ]; then
    echo $(( INTERVAL * 2 ))
  else
    echo "$INTERVAL"
  fi
}

# --- P4: Get head SHA and commit date for staleness detection ---
get_head_sha_and_date() {
  # Returns "sha\tdate" for the PR head commit
  local pr_data
  pr_data=$(gh api "repos/${REPO}/pulls/${PR}" 2>/dev/null) || { echo ""; return; }
  local sha
  sha=$(echo "$pr_data" | jq -r '.head.sha // ""')
  if [ -z "$sha" ]; then
    echo ""
    return
  fi
  local commit_data
  commit_data=$(gh api "repos/${REPO}/commits/${sha}" 2>/dev/null) || { printf '%s\t' "$sha"; return; }
  local date
  date=$(echo "$commit_data" | jq -r '.commit.committer.date // ""')
  printf '%s\t%s' "$sha" "$date"
}

# --- P1: Check if CodeRabbit check run has completed ---
# Track the last known CodeRabbit check status to detect transitions.
LAST_CR_CHECK_STATUS=""

check_coderabbit_check_run() {
  local head_sha="$1"
  [ -z "$head_sha" ] && return

  local check_data
  check_data=$(gh api "repos/${REPO}/commits/${head_sha}/check-runs" 2>/dev/null) || return
  # Look for CodeRabbit check run (name contains "coderabbit" case-insensitive)
  local cr_check
  cr_check=$(echo "$check_data" | jq -r '
    [.check_runs[] | select(.app.slug == "coderabbitai" or (.name | ascii_downcase | contains("coderabbit")))] | first // empty
  ' 2>/dev/null) || return

  [ -z "$cr_check" ] && return

  local status conclusion completed_at
  status=$(echo "$cr_check" | jq -r '.status // ""')
  conclusion=$(echo "$cr_check" | jq -r '.conclusion // ""')
  completed_at=$(echo "$cr_check" | jq -r '.completed_at // ""')

  local current_state="${status}:${conclusion}"

  # Emit event on transition to completed (only once per transition)
  if [ "$status" = "completed" ] && [ "$current_state" != "$LAST_CR_CHECK_STATUS" ]; then
    local timestamp="${completed_at:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
    # Only emit if this completion happened after our SINCE timestamp
    if [[ "$timestamp" > "$SINCE" ]] || [[ "$timestamp" == "$SINCE" ]]; then
      printf '%s\treview_complete\t%s\t\t\t\t\t%s\n' \
        "$timestamp" "$conclusion" "CodeRabbit review check completed with conclusion: ${conclusion}" \
        >> "$EVENTS_FILE"
    fi
  fi

  LAST_CR_CHECK_STATUS="$current_state"
}

while true; do
  # Clear events from previous iteration
  > "$EVENTS_FILE"

  # --- P5: Detect new pushes ---
  HEAD_INFO=$(get_head_sha_and_date)
  HEAD_SHA=$(printf '%s' "$HEAD_INFO" | cut -f1)
  HEAD_DATE=$(printf '%s' "$HEAD_INFO" | cut -f2)

  if [ -n "$HEAD_SHA" ] && [ "$HEAD_SHA" != "$LAST_KNOWN_HEAD_SHA" ]; then
    if [ -n "$LAST_KNOWN_HEAD_SHA" ]; then
      # New push detected — reset backoff timer
      LAST_PUSH_TIME=$(date +%s)
    fi
    LAST_KNOWN_HEAD_SHA="$HEAD_SHA"
  fi

  # --- P0: Get unresolved thread count (from the same GraphQL query) ---
  THREAD_QUERY_RESULT=$(gh api graphql -f query="
    { repository(owner: \"${OWNER}\", name: \"${NAME}\") {
      pullRequest(number: ${PR}) {
        reviewThreads(first: 100) { nodes {
          id
          isResolved
          comments(first: 1) { nodes { databaseId } }
        } }
      }
    } }" 2>/dev/null) || THREAD_QUERY_RESULT=""

  # Build thread map (comment ID → thread node ID) and count unresolved
  if [ -n "$THREAD_QUERY_RESULT" ]; then
    THREAD_MAP=$(echo "$THREAD_QUERY_RESULT" | jq -r '
      [.data.repository.pullRequest.reviewThreads.nodes[] |
        { key: (.comments.nodes[0].databaseId | tostring), value: .id }
      ] | from_entries
    ' 2>/dev/null) || THREAD_MAP="{}"

    UNRESOLVED_COUNT=$(echo "$THREAD_QUERY_RESULT" | jq -r '
      [.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false)] | length
    ' 2>/dev/null) || UNRESOLVED_COUNT="?"
  else
    THREAD_MAP="{}"
    UNRESOLVED_COUNT="?"
  fi

  # --- P1: Check CodeRabbit check run status ---
  check_coderabbit_check_run "$HEAD_SHA"

  # Collect all reviews since timestamp
  gh api "repos/${REPO}/pulls/${PR}/reviews" 2>/dev/null | \
    jq -r --arg bot "$BOT" --arg since "$SINCE" --arg head_date "$HEAD_DATE" '
      .[] | select(.user.login == $bot and .submitted_at > $since) |
      # P4: staleness detection — compare review timestamp against latest commit date
      (if $head_date != "" and .submitted_at < $head_date then "true" else "false" end) as $stale |
      "\(.submitted_at)\treview\t\(.state)\t\t\t\t\t\($stale)\t\(.body // "")"
    ' >> "$EVENTS_FILE" 2>/dev/null || true

  # Collect all inline review comments since timestamp.
  gh api "repos/${REPO}/pulls/${PR}/comments" 2>/dev/null | \
    jq -r --arg bot "$BOT" --arg since "$SINCE" --argjson threads "$THREAD_MAP" '
      .[] | select(.user.login == $bot and .created_at > $since) |
      if .in_reply_to_id then
        ($threads[.in_reply_to_id | tostring] // "") as $thread_id |
        "\(.created_at)\tthread_reply\t\t\(.path // "")\t\(.id)\t\(.in_reply_to_id)\t\($thread_id)\t\t\(.body // "")"
      else
        "\(.created_at)\tcomment\t\t\(.path // "")\t\(.id)\t\t\t\t\(.body // "")"
      end
    ' >> "$EVENTS_FILE" 2>/dev/null || true

  # Collect all top-level issue comments since timestamp
  gh api "repos/${REPO}/issues/${PR}/comments" 2>/dev/null | \
    jq -r --arg bot "$BOT" --arg since "$SINCE" '
      .[] | select(.user.login == $bot and .created_at > $since) |
      "\(.created_at)\tissue_comment\t\t\t\t\t\t\t\(.body // "")"
    ' >> "$EVENTS_FILE" 2>/dev/null || true

  # If we found any events, output them all sorted by timestamp and exit
  if [ -s "$EVENTS_FILE" ]; then
    sort "$EVENTS_FILE" | while IFS=$'\t' read -r timestamp type state path comment_id in_reply_to thread_node_id stale body; do
      echo "---EVENT---"
      echo "TYPE $type"
      [ -n "$state" ] && echo "STATE $state"
      # P4: Staleness annotation (reviews only)
      if [ "$type" = "review" ] && [ -n "$stale" ]; then
        echo "STALE $stale"
      fi
      echo "TIMESTAMP $timestamp"
      [ -n "$path" ] && echo "PATH $path"
      [ -n "$comment_id" ] && echo "COMMENT_ID $comment_id"
      [ -n "$in_reply_to" ] && echo "IN_REPLY_TO $in_reply_to"
      [ -n "$thread_node_id" ] && echo "THREAD_NODE_ID $thread_node_id"
      # P0: Unresolved thread count on every event
      echo "UNRESOLVED_THREADS $UNRESOLVED_COUNT"
      echo "BODY"
      echo "$body"
    done
    exit 0
  fi

  # P5: Use adaptive interval
  EFFECTIVE_INTERVAL=$(get_effective_interval)
  sleep "$EFFECTIVE_INTERVAL"
done
