# claude-code-el-coderabbit

Community event source for [claude-code-event-listeners](https://github.com/mividtim/claude-code-event-listeners) that polls for new [CodeRabbit](https://coderabbit.ai) activity on a GitHub pull request.

## Install

```bash
# From the marketplace (recommended — auto-discovers, pulls in el as dependency)
claude plugin marketplace add mividtim/claude-code-el-coderabbit
claude plugin install el-coderabbit

# Or manually register the source
git clone https://github.com/mividtim/claude-code-el-coderabbit.git
/el:register ./claude-code-el-coderabbit/sources.d/coderabbit.sh
```

## Usage

```
/el:listen coderabbit 1316
```

Pass a PR number to start polling. When CodeRabbit posts a review, inline comment, thread reply, or top-level comment, the listener exits with the event details.

### With a timestamp filter

```
/el:listen coderabbit 1316 2025-01-15T10:30:00Z
```

Only detect activity **after** the given timestamp. Useful for re-subscribing after processing an event — use the `TIMESTAMP` from the previous output as the new baseline.

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REPO` | Current `gh` repo | GitHub repo in `owner/name` format |
| `POLL_INTERVAL` | `30` | Seconds between API polls |

## Output Format

```
---EVENT---
TYPE thread_reply
TIMESTAMP 2025-01-15T10:36:15Z
PATH src/syndication/syndication.controller.e2e-spec.ts
COMMENT_ID 2784545640
IN_REPLY_TO 2784537786
BODY
@mividtim Thank you for the clarification. You're right...
```

### Event types

| TYPE | When | Extra fields |
|------|------|-------------|
| `review` | CodeRabbit posts a full review (APPROVED, CHANGES_REQUESTED, COMMENTED) | `STATE` |
| `comment` | CodeRabbit posts an initial inline review comment | `PATH`, `COMMENT_ID` |
| `thread_reply` | CodeRabbit replies to an existing review thread | `PATH`, `COMMENT_ID`, `IN_REPLY_TO` |
| `issue_comment` | CodeRabbit posts a top-level comment (walkthrough, summary) | — |

All events include `TIMESTAMP` and `BODY`.

### Thread reply workflow

The `thread_reply` event type enables automated CodeRabbit thread management:

1. CodeRabbit posts `CHANGES_REQUESTED` review with inline comments
2. You reply in-thread with fixes or pushback
3. Start the listener: `/el:listen coderabbit 1326 <timestamp>`
4. CodeRabbit replies in thread → listener fires with `TYPE thread_reply`
5. Read the `BODY` — if CodeRabbit accepts, resolve the thread using the GitHub
   GraphQL `resolveReviewThread` mutation
6. Resolving all threads triggers CodeRabbit to post an `APPROVED` review

The `IN_REPLY_TO` field contains the root comment ID, which you can use to find
the corresponding review thread for resolution.

## Typical workflow

```
Push code → start listener → CodeRabbit reviews →
  listener fires → read comments → make fixes / push back →
  re-subscribe → CodeRabbit replies in thread →
  listener fires → resolve accepted threads →
  CodeRabbit approves → merge
```

After processing each notification, re-subscribe with the `TIMESTAMP` from the output:

```
/el:listen coderabbit 1316 2025-01-15T10:35:22Z
```

## Requirements

- [claude-code-event-listeners](https://github.com/mividtim/claude-code-event-listeners) plugin installed
- [`gh` CLI](https://cli.github.com/) (authenticated)
- [`jq`](https://jqlang.github.io/jq/download/)

## License

MIT
