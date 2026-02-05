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
#   inline review comments, or top-level issue comments.
#   Outputs event details to stdout when activity is detected, then exits.
#
# Output format:
#   TYPE <review|comment|issue_comment>
#   STATE <APPROVED|CHANGES_REQUESTED|COMMENTED> (reviews only)
#   TIMESTAMP <iso-timestamp>
#   BODY <review/comment body>

set -euo pipefail

command -v gh &>/dev/null || { echo "ERROR: gh CLI not installed" >&2; exit 1; }
command -v jq &>/dev/null || { echo "ERROR: jq not installed" >&2; exit 1; }

PR="${1:?Usage: coderabbit.sh <pr-number> [since-timestamp]}"
REPO="${REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
SINCE="${2:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
INTERVAL="${POLL_INTERVAL:-30}"
BOT="coderabbitai[bot]"

while true; do
  # Check for new full reviews (APPROVED, CHANGES_REQUESTED, COMMENTED)
  REVIEW=$(gh api "repos/${REPO}/pulls/${PR}/reviews" \
    --jq "[.[] | select(.user.login == \"${BOT}\" and .submitted_at > \"${SINCE}\")] | .[-1] // empty" 2>/dev/null || true)
  if [ -n "$REVIEW" ]; then
    STATE=$(echo "$REVIEW" | jq -r '.state')
    TS=$(echo "$REVIEW" | jq -r '.submitted_at')
    BODY=$(echo "$REVIEW" | jq -r '.body // ""')
    echo "TYPE review"
    echo "STATE ${STATE}"
    echo "TIMESTAMP ${TS}"
    echo "BODY"
    echo "$BODY"
    exit 0
  fi

  # Check for new inline review comments (thread replies, new comments)
  COMMENT=$(gh api "repos/${REPO}/pulls/${PR}/comments" \
    --jq "[.[] | select(.user.login == \"${BOT}\" and .created_at > \"${SINCE}\")] | .[-1] // empty" 2>/dev/null || true)
  if [ -n "$COMMENT" ]; then
    TS=$(echo "$COMMENT" | jq -r '.created_at')
    FILE_PATH=$(echo "$COMMENT" | jq -r '.path // ""')
    BODY=$(echo "$COMMENT" | jq -r '.body // ""')
    echo "TYPE comment"
    echo "TIMESTAMP ${TS}"
    echo "PATH ${FILE_PATH}"
    echo "BODY"
    echo "$BODY"
    exit 0
  fi

  # Check for new top-level issue comments (walkthrough, summary updates)
  ISSUE_COMMENT=$(gh api "repos/${REPO}/issues/${PR}/comments" \
    --jq "[.[] | select(.user.login == \"${BOT}\" and .created_at > \"${SINCE}\")] | .[-1] // empty" 2>/dev/null || true)
  if [ -n "$ISSUE_COMMENT" ]; then
    TS=$(echo "$ISSUE_COMMENT" | jq -r '.created_at')
    BODY=$(echo "$ISSUE_COMMENT" | jq -r '.body // ""')
    echo "TYPE issue_comment"
    echo "TIMESTAMP ${TS}"
    echo "BODY"
    echo "$BODY"
    exit 0
  fi

  sleep "$INTERVAL"
done
