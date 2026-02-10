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
# Output format (multiple events separated by ---EVENT---):
#   ---EVENT---
#   TYPE <review|comment|thread_reply|issue_comment>
#   STATE <APPROVED|CHANGES_REQUESTED|COMMENTED> (reviews only)
#   TIMESTAMP <iso-timestamp>
#   PATH <file-path> (comments only)
#   COMMENT_ID <numeric-id> (comments and thread replies)
#   IN_REPLY_TO <numeric-id> (thread replies only — the root comment ID)
#   THREAD_NODE_ID <graphql-node-id> (thread replies — for resolveReviewThread)
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

# Temporary file to collect all events (timestamp-prefixed for sorting)
EVENTS_FILE=$(mktemp)
trap 'rm -f "$EVENTS_FILE"' EXIT

while true; do
  # Clear events from previous iteration
  > "$EVENTS_FILE"

  # Collect all reviews since timestamp
  gh api "repos/${REPO}/pulls/${PR}/reviews" 2>/dev/null | \
    jq -r --arg bot "$BOT" --arg since "$SINCE" '
      .[] | select(.user.login == $bot and .submitted_at > $since) |
      "\(.submitted_at)\treview\t\(.state)\t\t\t\t\t\(.body // "")"
    ' >> "$EVENTS_FILE" 2>/dev/null || true

  # Build a lookup from root comment database ID → review thread node ID.
  # The REST API doesn't expose thread node IDs, so we fetch them via GraphQL.
  # This is one call per poll cycle and gives us the PRRT_ IDs needed for
  # resolveReviewThread.
  OWNER="${REPO%%/*}"
  NAME="${REPO##*/}"
  THREAD_MAP=$(gh api graphql -f query="
    { repository(owner: \"${OWNER}\", name: \"${NAME}\") {
      pullRequest(number: ${PR}) {
        reviewThreads(first: 100) { nodes {
          id
          comments(first: 1) { nodes { databaseId } }
        } }
      }
    } }" 2>/dev/null | \
    jq -r '
      [.data.repository.pullRequest.reviewThreads.nodes[] |
        { key: (.comments.nodes[0].databaseId | tostring), value: .id }
      ] | from_entries
    ' 2>/dev/null) || THREAD_MAP="{}"

  # Collect all inline review comments since timestamp.
  # Comments with in_reply_to_id are thread replies; those without are initial
  # review comments. Thread replies include the review thread node ID so the
  # consumer can resolve threads via the GitHub GraphQL resolveReviewThread
  # mutation.
  gh api "repos/${REPO}/pulls/${PR}/comments" 2>/dev/null | \
    jq -r --arg bot "$BOT" --arg since "$SINCE" --argjson threads "$THREAD_MAP" '
      .[] | select(.user.login == $bot and .created_at > $since) |
      if .in_reply_to_id then
        ($threads[.in_reply_to_id | tostring] // "") as $thread_id |
        "\(.created_at)\tthread_reply\t\t\(.path // "")\t\(.id)\t\(.in_reply_to_id)\t\($thread_id)\t\(.body // "")"
      else
        "\(.created_at)\tcomment\t\t\(.path // "")\t\(.id)\t\t\t\(.body // "")"
      end
    ' >> "$EVENTS_FILE" 2>/dev/null || true

  # Collect all top-level issue comments since timestamp
  gh api "repos/${REPO}/issues/${PR}/comments" 2>/dev/null | \
    jq -r --arg bot "$BOT" --arg since "$SINCE" '
      .[] | select(.user.login == $bot and .created_at > $since) |
      "\(.created_at)\tissue_comment\t\t\t\t\t\t\(.body // "")"
    ' >> "$EVENTS_FILE" 2>/dev/null || true

  # If we found any events, output them all sorted by timestamp and exit
  if [ -s "$EVENTS_FILE" ]; then
    sort "$EVENTS_FILE" | while IFS=$'\t' read -r timestamp type state path comment_id in_reply_to thread_node_id body; do
      echo "---EVENT---"
      echo "TYPE $type"
      [ -n "$state" ] && echo "STATE $state"
      echo "TIMESTAMP $timestamp"
      [ -n "$path" ] && echo "PATH $path"
      [ -n "$comment_id" ] && echo "COMMENT_ID $comment_id"
      [ -n "$in_reply_to" ] && echo "IN_REPLY_TO $in_reply_to"
      [ -n "$thread_node_id" ] && echo "THREAD_NODE_ID $thread_node_id"
      echo "BODY"
      echo "$body"
    done
    exit 0
  fi

  sleep "$INTERVAL"
done
