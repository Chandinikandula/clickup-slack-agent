# ClickUp Slack Agent

A Slack-native assistant for ClickUp. Two layers:

- **Digest (no LLM)** — a scheduled 9:30 IST DM with today's tasks, yesterday's
  carry-over, and anything overdue.
- **Agent (LLM)** — ask questions in plain English and it decides which ClickUp
  calls to make: *"priority-wise tickets for today"*, *"what was assigned to me
  on August 29th"*, *"move CU-402 to review"*.

## Why two layers

The digest is deterministic — same code path every morning, no model involved.
The agent is a tool-calling loop over the same ClickUp client: Claude picks the
tools, may chain several in one turn, and any write is confirmed by a Slack
button before it executes.

```
                    ┌──────────────────┐
   9:30 IST ───────▶│   APScheduler    │
                    └────────┬─────────┘
                             ▼
                    ┌──────────────────┐
   your DM  ───────▶│   slack-bolt     │
                    │   (Socket Mode)  │
                    └────────┬─────────┘
                             ▼
              ┌──────────────┴──────────────┐
              ▼                             ▼
    ┌──────────────────┐          ┌──────────────────┐
    │  Digest builder  │          │   Agent loop     │
    │  (no LLM)        │          │   (Claude)       │
    └────────┬─────────┘          └────────┬─────────┘
             │                             │
             └──────────────┬──────────────┘
                            ▼
                  ┌──────────────────┐
                  │  ClickUp client  │
                  └──────────────────┘
```

## Tools available to the agent

| Tool | Kind |
| --- | --- |
| `get_my_tasks(due_date_range, status, priority)` | read |
| `search_tasks(query)` | read |
| `get_task_details(task_id)` | read |
| `get_task_comments(task_id)` | read |
| `update_task_status(task_id, status)` | write — confirmed |
| `add_comment(task_id, text)` | write — confirmed |

## Stack

Python 3.11 · slack-bolt (Socket Mode) · Anthropic SDK · httpx · APScheduler ·
pydantic-settings · pytest · ruff · Fly.io

## Setup

```bash
uv sync
cp .env.example .env   # then fill in the tokens
uv run python -m clickup_slack_agent
```

See [`.env.example`](.env.example) for the credentials required.

## Development

```bash
uv run pytest        # tests
uv run ruff check .  # lint
uv run ruff format . # format
```
