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

## Two flows, traced

### The digest — no model involved

`scheduler.py` registers one cron job at startup:

```python
scheduler.add_job(
    send_digest,
    trigger=CronTrigger(hour=9, minute=30, timezone=tz),
    misfire_grace_time=3600,   # fire late rather than skip
    coalesce=True,             # downtime sends one digest, not five
    max_instances=1,
)
```

At 09:30 `service.send_digest` runs:

1. `clickup.get_tasks(assignee_ids=[you])` — every open task assigned to you
2. `build_digest(tasks, tz=..., now=...)` — buckets them
3. `digest_blocks(digest)` — renders Slack blocks
4. `slack.chat_postMessage(...)`

The whole thing is wrapped so a failure still reaches you:

```python
try:
    digest = collect_digest(clickup, settings, now=now)
    blocks, text = digest_blocks(digest), digest_text(digest)
except Exception as exc:
    blocks, text = error_blocks(str(exc)), "Digest failed"

slack.chat_postMessage(channel=target, blocks=blocks, text=text)
```

A silent failure would read as a clear day, which is the worst outcome here.

### A question — the agent

You DM the bot. `slack/app.py` receives a `message` event:

```python
@app.event("message")
def handle_dm(body, event, client):
    if event.get("subtype") or event.get("bot_id"):
        return                                  # ignore edits, joins, itself
    if already_handled(body.get("event_id")):
        return                                  # Slack redelivers; don't answer twice
    _respond(event.get("text", ""), event["channel"], client)
```

`_respond` posts a placeholder immediately, then runs the agent and edits
that message in place — Slack shows something within a second instead of a
silent gap while the model thinks.

```python
placeholder = client.chat_postMessage(channel=channel, text="_thinking…_")
result = agent.run(question, conversations.get(channel))
client.chat_update(channel=channel, ts=placeholder["ts"], ...)
```

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
        return self.type in TERMINAL_TYPES   # {"done", "closed"}
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
    NONE = 5     # absent priority sorts last, not first
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
conversations = Conversations()          # in-memory, last 8 turns per channel
seen: OrderedDict[str, None] = OrderedDict()   # event ids, capped at 500
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
