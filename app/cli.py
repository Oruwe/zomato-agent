"""Command-line entrypoint -- the fastest way to see the whole pipeline run.

    python -m app.cli run                 # next meal slot, dry run
    python -m app.cli run --slot dinner   # a specific slot
    python -m app.cli memory              # what the agent knows about you
    python -m app.cli bench               # control-plane latency profile
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.config import get_settings
from app.deps import DEFAULT_USER, build_agent, execute_run
from app.observability.latency import REGISTRY
from app.observability.logger import configure_logging


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


async def _run(args) -> int:
    settings = get_settings()
    run = await execute_run(
        settings, user_id=args.user, slot=args.slot, force=args.force
    )

    print("\n" + "=" * 68)
    print(f"  state      : {run.state.value}")
    print(f"  slot       : {run.slot}")
    print(f"  restaurant : {run.restaurant}")
    print(f"  dishes     : {', '.join(run.dishes) if run.dishes else '-'}")
    print(f"  amount     : ₹{run.amount_paise / 100:.2f}")
    print(f"  order_id   : {run.order_id or '-'}")
    if run.escalation_reason:
        print(f"  note       : {run.escalation_reason}")
    if run.error:
        print(f"  error      : {run.error}")
    if run.injection_events:
        print(f"  injections : {len(run.injection_events)} blocked/flagged")
        for ev in run.injection_events:
            print(f"      - {ev['source']}: {', '.join(ev['reasons'])}")
    print("=" * 68)

    if args.verbose:
        _print(run.to_dict())
    if args.latency:
        _print(REGISTRY.snapshot())
    return 0 if run.state.value not in ("failed",) else 1


async def _memory(args) -> int:
    agent = build_agent(get_settings(), user_id=args.user)
    profile = agent.d.memory.recall()
    print(profile.to_prompt_block())
    print("\nRecent events:")
    _print(agent.d.memory.history(limit=10))
    print("\nWallet:")
    _print(agent.d.wallet.snapshot())
    return 0


async def _forget(args) -> int:
    """Erase everything held about a user."""
    from app.privacy import forget_user

    result = forget_user(get_settings(), user_id=args.user)
    _print(result)
    return 0


async def _privacy(args) -> int:
    from app.privacy import PII_INVENTORY

    print(f"{'category':<22}{'on disk':<10}{'to model':<10}where")
    print("-" * 78)
    for r in PII_INVENTORY:
        print(f"{r.category:<22}{str(r.persisted):<10}{str(r.sent_to_model):<10}{r.where[:34]}")
    return 0


async def _bench(args) -> int:
    from evals.bench_hotpath import run_benchmark

    run_benchmark(iterations=args.iterations)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zomato-agent")
    parser.add_argument("--user", default=DEFAULT_USER)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="execute one ordering cycle")
    p_run.add_argument("--slot", choices=["breakfast", "lunch", "snack", "dinner"])
    p_run.add_argument("--force", action="store_true",
                       help="order again even if this meal was already ordered today")
    p_run.add_argument("--verbose", action="store_true")
    p_run.add_argument("--latency", action="store_true", help="print latency percentiles")
    p_run.set_defaults(fn=_run)

    p_mem = sub.add_parser("memory", help="show learned preferences and wallet state")
    p_mem.set_defaults(fn=_memory)

    p_forget = sub.add_parser("forget", help="erase everything held about a user")
    p_forget.set_defaults(fn=_forget)

    p_privacy = sub.add_parser("privacy", help="what personal data is held, and where")
    p_privacy.set_defaults(fn=_privacy)

    p_bench = sub.add_parser("bench", help="microbenchmark the control plane")
    p_bench.add_argument("--iterations", type=int, default=20000)
    p_bench.set_defaults(fn=_bench)

    args = parser.parse_args(argv)
    configure_logging()
    return asyncio.run(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
