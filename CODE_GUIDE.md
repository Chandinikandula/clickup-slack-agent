# Code guide

A walk through the codebase, module by module. For *why* things are built
this way rather than *how*, see [ARCHITECTURE.md](ARCHITECTURE.md).

```
src/clickup_slack_agent/
├── config.py            typed settings, loaded once
├── __main__.py          entry point — wires everything together
├── clickup/
│   ├── models.py        typed views over ClickUp's JSON
│   └── client.py        HTTP, retries, paging, list filtering
├── digest/
│   ├── builder.py       pure bucketing logic — no I/O
│   ├── service.py       fetch → build → post
│   └── scheduler.py     the 09:30 cron
├── slack/
│   ├── blocks.py        Block Kit rendering
│   └── app.py           event handlers, the write gate's UI half
└── agent/
    ├── llm.py           provider-neutral types
    ├── tools.py         the seven tools and how they execute
    ├── prompts.py       the system prompt
    ├── loop.py          the agent loop
    └── providers/
        └── gemini.py    one vendor's translation layer
```

---

## Execution flow

Four traces: startup, the digest, a question, and a write. The last two are
the ones worth understanding.

### 1. Startup

`uv run python -m clickup_slack_agent` → `__main__.main()`

```
main()
├── get_settings()                         config.py
│     Settings()                           reads .env, raises on a missing key
│
├── ClickUpClient(token, team_id, ...)     one httpx.Client, reused throughout
│
├── if settings.agent_enabled:             false when no key for the provider
│   ├── ToolRegistry(clickup, user_id, tz)
│   │     _build()                         seven Tool objects, schemas carry today's date
│   ├── build_provider("gemini", key, model)
│   │     GeminiProvider(...)              lazy import — only this SDK loads
│   └── Agent(provider, registry, tz)
│                                          agent stays None if no key
│
├── build_app(clickup, settings, agent)    slack/app.py
│     App(token=...)                       registers 4 handlers:
│                                            message, app_mention,
│                                            confirm_write, cancel_write
│
├── start_scheduler(clickup, app.client, settings)
│     BackgroundScheduler(timezone=tz)     separate thread
│     add_job(send_digest, CronTrigger(9, 30))
│     scheduler.start()                    returns immediately
│
└── start_socket_mode(app, settings)
      SocketModeHandler(app, xapp_token)
      handler.start()                      opens a WebSocket, BLOCKS here forever
```

Two threads from then on: the scheduler waiting for 09:30, and the Socket
Mode connection waiting for events. A missing model key costs you the chat
layer only — the digest path never touches `agent`.

### 2. The digest — no model involved

Two different things can start this, and they converge immediately:

```
APScheduler's thread, 09:30 IST          GitHub Actions cron, 04:00 UTC
(only while the app is running)          (.github/workflows/digest.yml)
        │                                        │
        │                                scripts/send_digest_now.py
        │                                └── get_settings()
        │                                └── ClickUpClient(...) / WebClient(...)
        └────────────────┬───────────────────────┘
                         ▼
              send_digest(clickup, slack, settings)
```

The scheduled Action is why `SLACK_APP_TOKEN` is optional and why
`send_digest` returns a bool: a one-shot run needs no Socket Mode and no
model, and the script exits non-zero on failure so a broken digest shows as
a red run rather than as silence.

From there both paths are identical:

```
└── send_digest(clickup, slack, settings)              digest/service.py
    ├── collect_digest(clickup, settings)
    │   ├── datetime.now(ZoneInfo("Asia/Kolkata"))     "now" in YOUR timezone
    │   ├── clickup.get_tasks(assignee_ids=[240049726])
    │   │   └── _request("GET", "/team/{id}/task")     paged; 429 → sleep + retry
    │   │       └── _keep(list_id)                     drops excluded lists
    │   │       └── Task.from_api(raw)                 epoch ms → aware datetime
    │   └── build_digest(tasks, tz, now)               digest/builder.py — pure
    │       └── for each task:
    │             status.is_terminal?      → skip
    │             due_date is None?        → in_progress if actively worked
    │             _local_date(due, tz)     → overdue / due_today / upcoming
    │
    ├── digest_blocks(digest)                          slack/blocks.py
    │     _section("⚠️ Overdue", ..., show_late=True)
    │     _task_line(task)                             priority icon + <url|name>
    │
    └── slack.chat_postMessage(channel=SLACK_USER_ID, blocks=..., text=...)
```

The `try/except` wraps steps 1–2, not the post:

```python
try:
    digest = collect_digest(clickup, settings, now=now)
    blocks, text = digest_blocks(digest), digest_text(digest)
except Exception as exc:
    blocks, text = error_blocks(str(exc)), "Digest failed"

slack.chat_postMessage(channel=target, blocks=blocks, text=text)
```

The message goes out either way. A silent failure would read as a clear day,
which is the worst possible outcome for a tool you're meant to trust.

### 3. A question — the agent loop

You DM: **"what's the latest on the eval harness task?"**

```
Slack WebSocket delivers a `message` event
└── handle_dm(body, event, client)                     slack/app.py
    ├── event.get("subtype") or bot_id?    → return    ignore edits, joins, itself
    ├── already_handled(event_id)?         → return    Slack redelivers after 3s
    └── _respond(text, channel, client)
        ├── text in DIGEST_WORDS?          → send_digest, done  (the bot path)
        ├── text in {"reset", ...}?        → clear history, done
        ├── chat_postMessage("_thinking…_")            placeholder, keeps ts
        └── agent.run(question, conversations.get(channel))
```

Inside `Agent.run` — this is the part that matters:

```
build_system_prompt(today, tz)             injects today's real date

── iteration 0 ────────────────────────────────────────────────────────────
messages = [ user("what's the latest on the eval harness task?") ]

provider.complete(system, messages, tools)
└── GeminiProvider.complete
    ├── _to_content(m) for each message    Message → types.Content
    ├── _generate_with_retry(...)          429/503 → honour retryDelay, retry
    └── _from_response(response)
          part.function_call               → ToolCall(name="search_tasks", ...)
          part.thought_signature           → stashed in ToolCall.meta

response.wants_tools == True
registry.is_write("search_tasks") == False → no gate
registry.execute("search_tasks", {"query": "eval harness"})
└── _search_tasks(query="eval harness")
    └── clickup.get_tasks(...)  → filter by name → JSON string

messages now:
  [ user(question),
    assistant(tool_calls=[search_tasks]),
    user(tool_results=[{...1 match, id 14yqfu3a15f...}]) ]

── iteration 1 ────────────────────────────────────────────────────────────
provider.complete(...)                     model SEES the search result
                                           and decides it wants the comments
→ ToolCall(name="get_task_comments", arguments={"task_id": "14yqfu3a15f"})

registry.execute → clickup.get_comments(...) → {"count": 0, "comments": []}

messages grows by two more entries.

── iteration 2 ────────────────────────────────────────────────────────────
provider.complete(...)                     comments were empty, so it asks
→ ToolCall(name="get_task_details", ...)      for details instead

── iteration 3 ────────────────────────────────────────────────────────────
provider.complete(...)
→ response.wants_tools == False             it has enough

return AgentResult(
    text="No comments yet. It's Open, High priority, due Friday…",
    steps=[Step(search_tasks…), Step(get_task_comments…), Step(get_task_details…)],
    messages=[…],          ← saved as history for the next question
    pending_write=None,
    usage=Usage(4133, 161),
)
```

**Nothing in the code sequenced those three calls.** The loop only asks "do
you want a tool?" and feeds back whatever comes out. The model picked
`get_task_comments` after seeing the search result, then `get_task_details`
after finding the comments empty. That emergent chaining is the line between
this and a lookup function.

Back in Slack:

```
conversations.set(channel, result.messages)          last 8 turns kept
client.chat_update(channel, ts=placeholder_ts,
                   blocks=agent_blocks(text, tools_used))
```

The placeholder becomes the answer, with the tool trail underneath.

### 4. A write — proposal, then confirmation

You DM: **"move the ARCHITECTURE task to in review"**

**Part one — the proposal.** Same loop, until a write appears:

```
── iteration 0 ──  ToolCall(search_tasks, {"query": "ARCHITECTURE"})
                   is_write? No  → execute, feed back

── iteration 1 ──  ToolCall(list_statuses, {})
                   is_write? No  → execute, feed back
                                   (the model checks "in review" exists)

── iteration 2 ──  ToolCall(update_task_status,
                            {"task_id": "14yqfu3a16g", "status": "in review"})

                   for call in response.tool_calls:
                       if self.registry.is_write(call.name):
                           return AgentResult(pending_write=call, ...)
                                   ↑
                                   returns BEFORE registry.execute
                                   ClickUp is never called
```

Slack renders buttons, carrying the action in the payload itself:

```python
confirm_write_blocks(
    summary=_describe(call, result.text),
    payload=json.dumps({"name": call.name, "arguments": call.arguments}),
)
```

**Part one ends here.** The HTTP request is over. No state is stored
anywhere — not in memory, not on disk.

**Part two — minutes later, you click "Do it".** A completely separate
request arrives:

```
Slack delivers a block_actions payload
└── on_confirm(ack, body, client)                     slack/app.py
    ├── ack()                                         within 3s, always
    ├── payload = json.loads(body["actions"][0]["value"])
    │     {"name": "update_task_status", "arguments": {...}}
    │     ↑ the action came back from the button, not from memory
    ├── ToolCall(id="confirmed", name=..., arguments=...)
    └── agent.execute_confirmed_write(call)
        ├── if not registry.is_write(name): return error
        │     ↑ this path may run writes and nothing else
        └── registry.execute("update_task_status", {...})
            └── clickup.update_status(task_id, status)
                └── PUT /task/{id}  {"status": "in review"}   ← the board changes

    client.chat_update(...)        buttons REPLACED by the outcome,
                                   so it cannot be clicked twice
```

Three properties fall out of this shape:

- **A restart between the two parts is harmless.** The pending action lives
  in Slack's message, not in the app.
- **The model cannot write.** `run()` returns before `execute`; only a human
  click reaches `execute_confirmed_write`.
- **The confirm path is not a back door.** Its `is_write` guard stops it
  being a second, ungated way to run arbitrary tools.

Clicking **Cancel** replaces the buttons and does nothing else — there is no
state to clean up.

---

## `clickup/models.py`

ClickUp sends timestamps as epoch-millisecond *strings*, omits fields rather
than nulling them, and nests priority inside an object that may be absent.
This module normalises all of that once, so nothing downstream deals with it.

The important type is `Status`:

```python
class Status(BaseModel):
    name: str = Field(alias="status")
    type: str

    @property
    def is_terminal(self) -> bool:
        return self.type in TERMINAL_TYPES  # {"done", "closed"}
```

`is_terminal` is the single place the app decides whether work is finished,
and it reads ClickUp's own grouping rather than matching status names. Add a
`UAT` status to the board and nothing here changes.

`Priority` is an `IntEnum` so tasks sort correctly:

```python
class Priority(IntEnum):
    URGENT = 1
    HIGH = 2
    NORMAL = 3
    LOW = 4
    NONE = 5  # absent priority sorts last, not first
```

## `clickup/client.py`

Every ClickUp call goes through `_request`, which handles the two failures
that actually happen:

```python
if r.status_code == 429:
    wait = float(r.headers.get("Retry-After", 2**attempt))
    time.sleep(wait)
    continue

if r.status_code >= 500 and attempt < MAX_RETRIES - 1:
    time.sleep(2**attempt)
    continue
```

ClickUp allows 100 requests/minute per token. Without this the agent would
show a stack trace in Slack the first time it made a few calls quickly.

`_keep` decides which lists count:

```python
def _keep(self, list_id: str) -> bool:
    if self.include_list_ids:
        return list_id in self.include_list_ids
    return list_id not in self.exclude_list_ids
```

Include-by-default. An allowlist would make a new Space silently invisible,
and a quietly incomplete digest is worse than a noisy one.

`get_tasks` pages until a short page, with a hard ceiling so an unexpected
response can't loop forever.

## `digest/builder.py`

Pure functions — no HTTP, no clock, no Slack. That makes the date rules
directly testable, and they are the part most likely to be subtly wrong.

```python
def _local_date(when: datetime, tz: ZoneInfo) -> date:
    return when.astimezone(tz).date()
```

A task due 22:00 UTC on the 8th is due at 03:30 on the 9th in Kolkata.
Comparing in UTC makes the digest a day wrong for evening deadlines. There
is a test asserting the same instant buckets differently under UTC and IST.

Four buckets, in the order you should read them:

| Bucket | Rule |
| --- | --- |
| `overdue` | due before today, not terminal |
| `due_today` | due today, not terminal |
| `in_progress` | no due date, but an active custom status |
| `upcoming` | due within the next 3 days |

The `in_progress` bucket exists for the task that quietly rots for three
weeks — started, no deadline, invisible to any date filter.

## `agent/llm.py`

Four dataclasses and one Protocol. The loop is written against these, never
against a vendor SDK.

```python
class LLMProvider(Protocol):
    name: str
    model: str

    def complete(
        self, *, system: str, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> LLMResponse: ...
```

`ToolCall` carries a `meta` dict for opaque provider data — Gemini's thought
signatures, for instance. The loop never reads it; only the provider that
produced it does.

## `agent/tools.py`

`ToolRegistry` binds tool schemas to a ClickUp client and your user id. Each
tool is a schema plus a handler plus one flag:

```python
@dataclass(frozen=True)
class Tool:
    spec: ToolSpec
    handler: Callable[..., Any]
    is_write: bool = False
```

That `is_write` flag is what the loop's gate keys on.

`execute` turns failures into data rather than exceptions:

```python
try:
    return json.dumps(tool.handler(**arguments), default=str)
except TypeError as exc:
    return json.dumps({"error": f"bad arguments for {name}: {exc}"})
except Exception as exc:
    return json.dumps({"error": str(exc)})
```

A wrong task id should produce a correction on the next turn, not a crash.
The model reads the error and tries something else.

Tool descriptions carry real dates, injected at build time, so the model has
a concrete anchor for "today":

```python
"description": f"Earliest due date, YYYY-MM-DD (e.g. {today}).",
```

## `agent/loop.py`

The loop, in full shape:

```python
for step_no in range(self.max_steps):
    response = self.provider.complete(system=system, messages=messages, tools=specs)

    if not response.wants_tools:
        return AgentResult(text=response.text, steps=steps, ...)

    for call in response.tool_calls:
        if self.registry.is_write(call.name):
            return AgentResult(pending_write=call, ...)   # propose, don't run

    messages.append(Message(role="assistant", tool_calls=response.tool_calls))

    results = [ToolResult(id=c.id, name=c.name, content=self.registry.execute(...))
               for c in response.tool_calls]
    messages.append(Message(role="user", tool_results=results))
```

Three things worth noticing:

**The chaining is emergent.** Nothing sequences the tools. The model sees
the result of call one and decides whether it needs call two. That is the
whole difference between this and a lookup.

**The write gate returns early.** A write never reaches `registry.execute`
inside `run()`. The only path that executes one is:

```python
def execute_confirmed_write(self, call: ToolCall) -> str:
    if not self.registry.is_write(call.name):
        return '{"error": "not a write tool"}'
    return self.registry.execute(call.name, call.arguments)
```

Called only from the Slack confirm-button handler. The guard stops it
becoming a second, ungated way to run arbitrary tools.

**The step ceiling is real.** Six steps, then it stops and says the answer is
incomplete rather than presenting a partial one as final.

Every call is recorded as a `Step`, which is what the eval harness asserts
on and what renders as the grey tool trail under each Slack answer.

## `agent/providers/gemini.py`

All of Gemini's peculiarities live here:

- the assistant role is called `"model"`
- tool results go back in a **user** turn — there is no tool role
- function calls carry a `thought_signature` that must be replayed verbatim,
  or Gemini 3 rejects the request

```python
part = types.Part.from_function_call(name=call.name, args=call.arguments)
part.thought_signature = call.meta.get("thought_signature")
```

Retries honour the server's own hint, capped so a live Slack turn never
stalls waiting out a quota window:

```python
hinted = _suggested_delay(exc)
delay = min(hinted + 0.5, MAX_HONOURED_DELAY_S) if hinted is not None else 2**attempt + jitter
```

Adding a provider means writing one class with a `complete` method and
registering it in `providers/__init__.py`. Nothing else changes.

## `slack/app.py`

Three pieces of state, all deliberate:

```python
conversations = Conversations()  # in-memory, last 8 turns per channel
seen: OrderedDict[str, None] = OrderedDict()  # event ids, capped at 500
```

Slack redelivers any event not acked within three seconds, and an agent turn
takes longer than that. Without the dedupe you get the same question answered
twice.

The confirm handler carries its action in the button's own payload:

```python
"value": json.dumps({"name": call.name, "arguments": call.arguments})
```

So approving a change five minutes later works even though the app stored
nothing. After executing, the buttons are replaced with the outcome — a
change cannot be applied twice by clicking again.

## `config.py`

```python
@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
```

A function, not a module-level instance. Importing a module should not
require a populated environment, or CI has to carry real credentials just to
test code that mocks every network call.

`agent_enabled` lets the app degrade rather than fail:

```python
@property
def agent_enabled(self) -> bool:
    return bool(self.llm_api_key)
```

No model key means the digest still works and chat explains why it cannot
answer.

---

## Extending it

### Adding a tool

One entry in `ToolRegistry._build()`:

```python
(
    Tool(
        ToolSpec(
            name="get_task_time_tracked",
            description="How much time has been logged against a task. Use this "
            "when the user asks how long something has taken.",
            parameters={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        ),
        self._get_task_time_tracked,
        is_write=False,
    ),
)
```

Plus the handler method, and a client method if ClickUp needs a new call.
The loop, the gate and the Slack layer need no changes.

Write the description for a reader who cannot see your code — *when* to use
the tool matters more than what it returns.

### Adding a provider

```python
class GroqProvider:
    name = "groq"

    def __init__(self, api_key: str, model: str) -> None: ...

    def complete(self, *, system, messages, tools) -> LLMResponse:
        # translate Message/ToolSpec in, LLMResponse out
```

Register it in `providers/__init__.py`, add its key to `Settings`, set
`LLM_PROVIDER`. Then run the eval suite against both and compare.

---

## Testing

Three layers, and each answers a different question.

**Unit tests** (`tests/`, 35 of them, no network):

- `test_clickup_client.py` — mocks HTTP with `respx`. Covers epoch parsing,
  list filtering, paging, rate-limit retry, and that `update_status` sends
  only the status field.
- `test_digest_builder.py` — date boundaries, mostly. The one that matters:

  ```python
  def test_timezone_choice_changes_the_answer():
      late = task("late", due=datetime(2026, 9, 8, 22, 0, tzinfo=UTC))
      assert build_digest([late], tz=ZoneInfo("UTC"), now=NOW).due_today == [late]
      assert build_digest([late], tz=IST, now=NOW).due_today == []
  ```

- `test_agent_loop.py` — drives the loop with a `ScriptedProvider` that
  replays fixed responses. Tests chaining, the step ceiling, and that a
  proposed write leaves the board untouched.

**Evals** (`evals/`, 30 cases, live model): what a real model *chooses*.
Assertions are on the tool trail, not the answer text, because wording varies
between runs and between models.

**Manual scripts** (`scripts/`): `check_clickup.py` and `check_slack.py`
verify credentials; `ask.py` runs one question and prints the tool trail.

The split matters: unit tests tell you the code is correct, evals tell you
the agent is *behaving*. Neither substitutes for the other.
