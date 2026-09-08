# Architecture

A Slack-native assistant over one person's ClickUp board. Two layers share
one ClickUp client: a scheduled digest that uses no model at all, and a
conversational agent that decides for itself which ClickUp calls to make.

The split is deliberate. Most of what this app does every day is
deterministic, and running it through a model would only add cost, latency
and a way to be wrong. The model earns its place on the questions that
cannot be enumerated in advance.

---

## The two paths

```
                        ┌──────────────────┐
      09:30 IST ───────▶│   APScheduler    │
                        └────────┬─────────┘
                                 │
                        ┌────────▼─────────┐
      your DM   ───────▶│   slack-bolt     │
                        │  (Socket Mode)   │
                        └────────┬─────────┘
                                 │
                  ┌──────────────┴──────────────┐
                  ▼                             ▼
        ┌──────────────────┐          ┌──────────────────┐
        │  Digest builder  │          │   Agent loop     │
        │  deterministic   │          │   LLM decides    │
        └────────┬─────────┘          └────────┬─────────┘
                 │                             │
                 │                    ┌────────▼─────────┐
                 │                    │   write gate     │
                 │                    │  (Slack buttons) │
                 │                    └────────┬─────────┘
                 └──────────────┬──────────────┘
                                ▼
                      ┌──────────────────┐
                      │  ClickUp client  │
                      └──────────────────┘
```

**Digest path** — cron fires, tasks are fetched, bucketed by date and
status, rendered as Slack blocks. Same code every morning. No model, no
tokens, nothing to evaluate.

**Agent path** — a question arrives, the model picks tools, results feed
back, it decides whether it needs more. Writes never execute inline.

---

## Decisions worth explaining

### The agent loop is written out, not delegated to a framework

It is about forty lines: call the model, execute whatever tools it asked
for, append the results, repeat until it stops asking. A framework would
replace those forty lines and take the control flow with it — and the
control flow *is* the product here. The step ceiling, the write gate and
the trace all live in that loop.

The practical consequence: when the agent picks the wrong tool, debugging
is printing the message list, not stepping through someone else's executor.

### Statuses are read by type, never by name

ClickUp reports a status as `open`, `custom`, `done` or `closed`. The code
asks whether a status is terminal by looking at that type. It never matches
on `"completed"` or `"done"` as strings.

The result is that adding a `UAT` or `deployed` status to the board needs no
code change and no redeploy. Board configuration is the source of truth
about what "finished" means, which is where that decision belongs.

This surfaced a genuine misconfiguration during setup — several statuses
that meant "finished" were sitting in the Active group, so the API reported
them as `custom`. Fixing the board was correct; hardcoding the names would
have buried the problem.

### Lists are excluded, not allowlisted

Everything assigned to you is included except lists named in
`CLICKUP_EXCLUDE_LIST_IDS`.

An allowlist looks safer and is worse. A new Space would be silently absent
from every digest, and you would trust a brief that was quietly incomplete.
With an exclude list the failure mode is a noisy digest, which you notice
immediately and fix in one line of config. An allowlist is still supported
for the case where this is pointed at a workspace with dozens of lists.

### Dates are compared in the user's timezone

ClickUp stores due dates as UTC epoch milliseconds. A task due 22:00 UTC on
the 8th is due at 03:30 on the 9th in Kolkata. Comparing in UTC makes the
digest a day wrong for anything due late in the evening — the single most
likely bug in the whole app, and invisible until someone misses a deadline.

Every comparison converts to `TIMEZONE` first, and there is a test asserting
that the same instant buckets differently under UTC and IST.

### Writes are gated in code, not in the prompt

The loop inspects every tool call. If the tool is marked as a write, it
stops and returns the call as a *proposal*. Slack renders it as Do it /
Cancel, and only the confirm handler can execute it.

The first version of the prompt asked the model to confirm before changing
anything, and it obliged — by asking in text, which meant the code-level gate
never fired at all. Prompt-level safety is a request; code-level safety is a
guarantee. The prompt now tells the model to call the tool and let the system
do the asking.

The pending action rides in the Slack button's own payload, so approving it
minutes later works without the app having stored anything.

### Model provider sits behind one interface

The loop talks to `LLMProvider` — three dataclasses and one method. Each
provider translates its own vendor quirks internally: Gemini calls the
assistant role "model", carries tool results in a *user* turn, and requires
thought signatures to survive the round trip. None of that leaks out.

Switching provider is an environment variable. That also makes it possible
to run the same eval suite across models and compare them on the numbers
rather than on impressions.

### No database

Conversation history lives in a dict; pending writes live in the Slack
button payload. A restart loses your last few turns, which is a minor
annoyance, not a failure.

The one thing that will eventually justify SQLite is snoozing — a task stuck
for three weeks appears in every digest until you stop reading digests. That
is when it goes in, not before.

---

## Trust boundary

Task titles and comments are written by other people and arrive through a
tool result. They are data.

The system prompt says so explicitly, and the eval suite carries cases for
it: a task whose text instructs the agent to change something must not cause
a change. Two things make that hold in practice — the instruction in the
prompt, and the write gate underneath, which means even a successful
injection produces a button the human has to press.

Credentials are never in the repository. `.env` is gitignored, `.env.example`
documents the shape, and deployment uses `fly secrets`.

---

## Failure modes, and what happens

| Failure | Behaviour |
| --- | --- |
| ClickUp rate limits (100/min) | Retried with the server's `Retry-After` |
| ClickUp 5xx | Retried with exponential backoff |
| ClickUp token rejected | Digest posts the error to Slack rather than staying silent |
| Model over quota | Retried honouring the provider's hint, capped at 30s so a live turn never stalls |
| Model unavailable | Agent replies with the error; the digest is unaffected |
| No model key at all | App starts, digest works, chat says why it cannot answer |
| Process was down at 09:30 | Fires late within the hour, coalesced so downtime sends one digest, not five |
| Scheduled digest cannot post | The Action exits non-zero and shows red — silence is never the only signal |
| Slack redelivers an event | Deduplicated by event id — an agent turn takes longer than Slack's 3s ack window |
| Model loops on tools | Stops after six steps and says the answer is incomplete |

The theme: a digest that fails silently is worse than no digest, because
absence reads as a clear day.

---

## Evaluation

`evals/` holds thirty cases asserting on the **tool trail** rather than the
answer text — which tools were chosen, with which arguments, whether a write
was proposed. Wording varies between runs and between models; behaviour is
what breaks and what can be checked.

Categories: date resolution, filters, chaining, writes, read-only safety,
prompt injection, and honesty about empty results.

```bash
uv run python evals/run.py --tag chaining
uv run python evals/run.py --provider anthropic --model claude-sonnet-5
```

Results are written per run so two models can be compared on tool-selection
accuracy, token cost and latency.

Unit tests cover what is deterministic: date bucketing, the ClickUp client,
and the loop's control flow driven by a scripted provider. No test touches a
network.

---

## Deployment

The two layers have genuinely different hosting needs, and splitting them
turned out to be the cheaper answer rather than a compromise.

**The digest is a ten-second job**, so it needs a scheduler, not a server.
It runs as a GitHub Actions cron (`.github/workflows/digest.yml`) at 04:00
UTC on weekdays — 09:30 in Kolkata. Free, no host to keep alive, and it
fires whether or not a laptop is awake. The script exits non-zero on
failure, so a broken digest shows as a red run rather than as silence.

**The chat agent must be listening when you type**, which genuinely does
need a long-running process. `Dockerfile` and `fly.toml` are set up for it —
one container, no inbound port, since Socket Mode dials out. Run it locally
with `python -m clickup_slack_agent`, or `fly deploy` it.

This split is why `SLACK_APP_TOKEN` is optional and `send_digest` never
touches the model: the scheduled run needs neither Socket Mode nor an LLM,
and requiring them would have forced a server for a cron job.

CI lints, tests without credentials, builds the image, and deploys to Fly on
merge to `main` once `FLY_API_TOKEN` is set.

---

## What would change at team scale

Everything here assumes one user, and several decisions would not survive a
second one:

- **Auth** — a personal ClickUp token becomes an OAuth app, with per-user
  tokens stored somewhere real.
- **Identity** — a Slack-ID to ClickUp-ID mapping table, which is the point
  where a database stops being optional.
- **The digest job** — one scheduled send becomes a fan-out, which needs to
  be resumable when it fails halfway.
- **Rate limits** — ClickUp's 100/min is per token, so it stops being
  theoretical once there are twenty briefs to build at 09:30.

The agent loop, the tool registry and the write gate would carry over
unchanged. That is the part worth having built carefully.
