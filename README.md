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

Pass a PR number to start polling. When CodeRabbit posts a review, inline comment, or top-level comment, the listener exits with the event details.

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
TYPE review
STATE CHANGES_REQUESTED
TIMESTAMP 2025-01-15T10:35:22Z
BODY
**Actionable comments posted: 3**
...
```

### Event types

| TYPE | When | Extra fields |
|------|------|-------------|
| `review` | CodeRabbit posts a full review (APPROVED, CHANGES_REQUESTED, COMMENTED) | `STATE` |
| `comment` | CodeRabbit posts an inline review comment or thread reply | `PATH` (file path) |
| `issue_comment` | CodeRabbit posts a top-level comment (walkthrough, summary) | — |

All events include `TIMESTAMP` and `BODY`.

## Typical workflow

```
Push code → start listener → CodeRabbit reviews →
  listener fires → read comments → make fixes →
  push → re-subscribe with new timestamp → repeat
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
