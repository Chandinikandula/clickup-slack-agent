"""Run the eval suite against a live model and score the tool trail.

    uv run python evals/run.py                    # everything
    uv run python evals/run.py --tag chaining     # one slice
    uv run python evals/run.py --limit 5          # cheap smoke run
    uv run python evals/run.py --provider groq --model llama-3.3-70b-versatile

Each case costs 2-4 model requests, so a full run is ~90. Check your
provider's quota before running the whole suite.
"""

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases import CASES, Case  # noqa: E402

from clickup_slack_agent.agent.loop import Agent  # noqa: E402
from clickup_slack_agent.agent.providers import build_provider  # noqa: E402
from clickup_slack_agent.agent.tools import ToolRegistry  # noqa: E402
from clickup_slack_agent.clickup.client import ClickUpClient  # noqa: E402
from clickup_slack_agent.config import settings  # noqa: E402

RESULTS_DIR = Path(__file__).parent / "results"


@dataclass
class Outcome:
    case_id: str
    question: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    proposed_write: str | None = None
    answer: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0


def resolve(value: str, today: date) -> str:
    """Turn a date placeholder into the date it means today."""
    return {
        "TODAY": today.isoformat(),
        "YESTERDAY": (today - timedelta(days=1)).isoformat(),
        "TOMORROW": (today + timedelta(days=1)).isoformat(),
    }.get(value, value)


def contains_in_order(trail: list[str], expected: list[str]) -> bool:
    """Expected tools appear in this order, other calls in between allowed."""
    remaining = list(expected)
    for tool in trail:
        if remaining and tool == remaining[0]:
            remaining.pop(0)
    return not remaining


def score(case: Case, result, today: date) -> list[str]:
    """Every way this case fell short. Empty list means it passed."""
    failures: list[str] = []
    trail = result.tools_used

    if case.expect_tools and not contains_in_order(trail, case.expect_tools):
        failures.append(f"expected tools {case.expect_tools} in order, got {trail}")

    forbidden = [t for t in case.forbid_tools if t in trail]
    if forbidden:
        failures.append(f"called forbidden tool(s) {forbidden}")

    # A proposed write counts as forbidden too — the gate stops execution,
    # but proposing a write on a read-only question is still wrong.
    if result.pending_write and result.pending_write.name in case.forbid_tools:
        failures.append(f"proposed forbidden write {result.pending_write.name}")

    if case.expect_write:
        actual = result.pending_write.name if result.pending_write else None
        if actual != case.expect_write:
            failures.append(f"expected write {case.expect_write}, got {actual}")

    for tool_name, expected_args in case.expect_args.items():
        called = [s for s in result.steps if s.tool == tool_name]
        if result.pending_write and result.pending_write.name == tool_name:
            called.append(type("S", (), {"arguments": result.pending_write.arguments})())
        if not called:
            failures.append(f"{tool_name} was never called, so args unchecked")
            continue
        args = called[0].arguments
        for key, raw in expected_args.items():
            want = resolve(raw, today)
            got = str(args.get(key, ""))
            if got != want:
                failures.append(f"{tool_name}.{key}: expected {want!r}, got {got!r}")

    if case.expect_text:
        lowered = result.text.lower()
        if not any(fragment in lowered for fragment in case.expect_text):
            failures.append(f"answer mentioned none of {case.expect_text}")

    return failures


def build_agent(provider_name: str, model: str) -> Agent:
    api_key = {
        "gemini": settings.gemini_api_key,
        "anthropic": settings.anthropic_api_key,
        "groq": settings.groq_api_key,
    }[provider_name]

    clickup = ClickUpClient(
        settings.clickup_api_token,
        settings.clickup_team_id,
        exclude_list_ids=settings.clickup_exclude_list_ids,
        include_list_ids=settings.clickup_include_list_ids,
    )
    registry = ToolRegistry(clickup, settings.clickup_user_id, ZoneInfo(settings.timezone))
    return Agent(
        build_provider(provider_name, api_key, model), registry, timezone=settings.timezone
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default=settings.llm_provider)
    parser.add_argument("--model", default=settings.llm_model)
    parser.add_argument("--tag", help="only cases carrying this tag")
    parser.add_argument("--limit", type=int, help="stop after N cases")
    parser.add_argument("--case", help="run one case by id")
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="seconds to wait between cases. Free tiers cap requests per "
        "minute, and each case spends several — try 20 on Gemini's free tier.",
    )
    args = parser.parse_args()

    selected = CASES
    if args.tag:
        selected = [c for c in selected if args.tag in c.tags]
    if args.case:
        selected = [c for c in selected if c.id == args.case]
    if args.limit:
        selected = selected[: args.limit]

    if not selected:
        print("no cases matched")
        raise SystemExit(1)

    tz = ZoneInfo(settings.timezone)
    today = datetime.now(tz).date()
    agent = build_agent(args.provider, args.model)

    print(f"\n{args.provider} / {args.model} — {len(selected)} cases\n")
    outcomes: list[Outcome] = []

    for index, case in enumerate(selected):
        if index and args.delay:
            time.sleep(args.delay)
        started = time.monotonic()
        try:
            result = agent.run(case.question)
            failures = score(case, result, today)
            outcome = Outcome(
                case_id=case.id,
                question=case.question,
                passed=not failures,
                failures=failures,
                tools_used=result.tools_used,
                proposed_write=result.pending_write.name if result.pending_write else None,
                answer=result.text[:300],
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - a crash is a failed case
            outcome = Outcome(
                case_id=case.id,
                question=case.question,
                passed=False,
                failures=[f"raised {type(exc).__name__}: {exc}"],
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        outcomes.append(outcome)
        mark = "PASS" if outcome.passed else "FAIL"
        trail = " → ".join(outcome.tools_used) or "(no tools)"
        print(f"  {mark}  {outcome.case_id:<34} {trail}")
        for failure in outcome.failures:
            print(f"        {failure}")

    passed = sum(o.passed for o in outcomes)
    tokens_in = sum(o.input_tokens for o in outcomes)
    tokens_out = sum(o.output_tokens for o in outcomes)

    print(f"\n  {passed}/{len(outcomes)} passed  ·  {tokens_in}in {tokens_out}out tokens")

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(tz).strftime("%Y%m%d-%H%M%S")
    path = RESULTS_DIR / f"{args.provider}-{args.model}-{stamp}.json"
    path.write_text(
        json.dumps(
            {
                "provider": args.provider,
                "model": args.model,
                "ran_at": datetime.now(tz).isoformat(),
                "passed": passed,
                "total": len(outcomes),
                "input_tokens": tokens_in,
                "output_tokens": tokens_out,
                "outcomes": [asdict(o) for o in outcomes],
            },
            indent=2,
        )
    )
    print(f"  written to {path.relative_to(Path.cwd())}\n")


if __name__ == "__main__":
    main()
