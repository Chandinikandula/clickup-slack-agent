# ClickUp Slack Agent

A Slack-native assistant for ClickUp. Two layers:

- **Digest (no LLM)** — a scheduled 09:30 DM with today's tasks, yesterday's
  carry-over, and anything overdue.
- **Agent (LLM)** — ask questions in plain English and it decides which
  ClickUp calls to make, chaining them when one answer needs another.

```
you: what's the latest on the eval harness task?

  search_tasks({"query": "eval harness"})        →  1 match
  get_task_comments({"task_id": "14yqfu3a15f"})  →  0 comments
  get_task_details({"task_id": "14yqfu3a15f"})   →  Open, High, due Friday

bot: No comments yet. It's Open, High priority, due Friday 11 September.
     search_tasks → get_task_comments → get_task_details
```

Nothing in the code knows that "the latest on X" means *read the comments*.
The model chose the second call after seeing the first result — that is the
line between a bot and an agent.

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — the design decisions and why they
  were made that way.
- **[CODE_GUIDE.md](CODE_GUIDE.md)** — a walk through the code, module by
  module, and how to add a tool or a model provider.

## What it can do

| Tool | Kind |
| --- | --- |
| `get_my_tasks(due_from, due_to, status, priority)` | read |
| `search_tasks(query)` | read |
| `get_task_details(task_id)` | read |
| `get_task_comments(task_id)` | read |
| `list_statuses()` | read |
| `update_task_status(task_id, status)` | write — confirmed in Slack |
| `add_comment(task_id, text)` | write — confirmed in Slack |

Writes never execute inline. The loop stops, Slack renders *Do it / Cancel*,
and only your click runs the change.

## Stack

Python 3.11 · slack-bolt (Socket Mode) · httpx · APScheduler ·
pydantic-settings · pytest + respx · ruff · GitHub Actions

The model sits behind one interface, selected by `LLM_PROVIDER` — Gemini
today, with Anthropic and Groq as drop-in alternatives.

## Running it

```bash
uv sync
cp .env.example .env      # then fill in the tokens
uv run python -m clickup_slack_agent
```

Then DM the bot. Say `digest` for the morning brief, or ask it anything.

Credentials required are documented in [`.env.example`](.env.example). Two
scripts verify them before you start:

```bash
uv run python scripts/check_clickup.py   # token, lists, statuses, your tasks
uv run python scripts/check_slack.py     # both tokens, scopes, your user id
uv run python scripts/send_digest_now.py # digest without waiting for 09:30
uv run python scripts/ask.py "what's due today?"
```

## Development

```bash
uv run pytest              # 35 tests, no network
uv run ruff check .
uv run ruff format .
```

## Evaluation

Thirty cases asserting on which tools the agent chose, not on how it worded
the answer.

```bash
uv run python evals/run.py --tag chaining
uv run python evals/run.py --tag injection
uv run python evals/run.py --provider anthropic --model claude-sonnet-5
```

A full run costs roughly 90 model requests — check your provider's quota
first, and use `--delay` to stay inside a free tier's per-minute limit.

## Where it runs

The two layers need different hosting, so they get it.

**The digest** runs as a GitHub Actions cron — `.github/workflows/digest.yml`,
04:00 UTC weekdays, which is 09:30 in Kolkata. No server, nothing to keep
alive, and it fires whether or not your laptop is awake. Set these as
repository secrets:

```
SLACK_BOT_TOKEN  SLACK_USER_ID  CLICKUP_API_TOKEN
CLICKUP_TEAM_ID  CLICKUP_USER_ID  CLICKUP_EXCLUDE_LIST_IDS
```

Neither `SLACK_APP_TOKEN` nor a model key is needed — a one-shot digest uses
no Socket Mode and no LLM. Trigger it by hand from the Actions tab to test.

**The chat agent** has to be listening when you type, so it needs a
long-running process — `uv run python -m clickup_slack_agent`, started when
you want it. There is no free way around that, so it isn't hosted.
