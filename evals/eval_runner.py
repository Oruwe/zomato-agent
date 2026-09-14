"""Eval harness: security (red-team) + behavioural (golden workflows).

    python -m evals.eval_runner              # everything
    python -m evals.eval_runner --security   # injection corpus only
    python -m evals.eval_runner --scenarios  # end-to-end workflows only
    python -m evals.eval_runner --json       # machine-readable summary

Exits non-zero on any failure so it can gate CI. The security suite reports precision
and recall separately -- a filter that blocks everything scores perfect recall and is
useless, so the benign cases matter as much as the malicious ones.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from app.deps import build_agent
from app.payments.wallet import Wallet, WalletCaps
from app.security.guardrails import sanitize
from app.security.policy import Decision, OrderPolicy, PolicyEngine

HERE = Path(__file__).parent
IST = timezone(timedelta(hours=5, minutes=30))
NOON = datetime(2026, 9, 15, 12, 30, tzinfo=IST)


@dataclass
class Outcome:
    case_id: str
    passed: bool
    expected: str
    actual: str
    detail: str = ""


@dataclass
class Report:
    name: str
    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for o in self.outcomes if o.passed)

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def ok(self) -> bool:
        return self.passed == self.total

    def print(self) -> None:
        print(f"\n=== {self.name} ===")
        for o in self.outcomes:
            mark = "PASS" if o.passed else "FAIL"
            line = f"  [{mark}] {o.case_id:<26} expected={o.expected:<9} actual={o.actual}"
            if not o.passed and o.detail:
                line += f"\n         {o.detail}"
            print(line)
        rate = 100.0 * self.passed / self.total if self.total else 0.0
        print(f"  -> {self.passed}/{self.total} ({rate:.1f}%)")


def _policy_engine() -> PolicyEngine:
    wallet = Wallet(
        WalletCaps(per_order_paise=100_000, daily_paise=150_000, monthly_paise=2_000_000)
    )
    return PolicyEngine(
        OrderPolicy(
            max_per_order_paise=100_000,
            human_approval_above_paise=80_000,
            allowed_payment_types=frozenset({"upi"}),
            allow_autonomous_checkout=True,
        ),
        wallet,
    )


def run_security() -> tuple[Report, dict[str, float]]:
    data = json.loads((HERE / "test_injections.json").read_text())
    report = Report("Security / red-team")
    engine = _policy_engine()
    tp = fp = tn = fn = 0

    for case in data["cases"]:
        expect = case["expect"]
        if expect == "denied":
            decision = engine.validate(case["tool"], case.get("args", {})).decision
            actual = "denied" if decision is Decision.DENY else decision.value
            report.outcomes.append(
                Outcome(case["id"], actual == "denied", expect, actual,
                        f"tool={case['tool']} was not refused")
            )
            continue

        verdict = sanitize(case["text"], source=case["channel"])
        actual = "flagged" if verdict.score > 0 else "clean"
        passed = actual == expect
        if expect == "flagged":
            tp += passed
            fn += not passed
        else:
            tn += passed
            fp += not passed
        report.outcomes.append(
            Outcome(case["id"], passed, expect, actual,
                    f"score={verdict.score} reasons={verdict.reasons}")
        )

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return report, {
        "precision": precision, "recall": recall, "f1": f1,
        "true_positives": tp, "false_positives": fp,
        "true_negatives": tn, "false_negatives": fn,
    }


async def run_scenarios(tmp_root: Path) -> Report:
    data = json.loads((HERE / "test_scenarios.json").read_text())
    report = Report("Behavioural / golden workflows")

    for i, sc in enumerate(data["scenarios"]):
        base: dict[str, Any] = dict(
            use_mocks=True, payment_rail="mock", gemini_api_key="",
            memory_path=str(tmp_root / f"s{i}"),
            max_per_order_inr=1000, daily_cap_inr=1500, monthly_cap_inr=20000,
            human_approval_above_inr=800,
        )
        base.update(sc.get("settings", {}))
        agent = build_agent(Settings(**base), user_id=f"eval{i}")
        run = await agent.run(slot=sc.get("slot"), now=NOON)

        failures: list[str] = []
        if run.state.value not in sc["expect_state"]:
            failures.append(f"state {run.state.value} not in {sc['expect_state']}")
        if sc.get("expect_no_order_id") and run.order_id:
            failures.append(f"unexpected order_id {run.order_id}")
        if sc.get("expect_no_order_id") is False and not run.order_id:
            failures.append("expected an order_id but none was produced")
        if (want := sc.get("expect_spend_paise")) is not None:
            spent = agent.d.wallet.snapshot()["day_spent_paise"]
            if spent != want:
                failures.append(f"spend {spent} != expected {want}")
        if sc.get("expect_injection_detected") and not run.injection_events:
            failures.append("expected an injection to be detected")
        if (bad := sc.get("forbid_restaurant_substring")) and bad in (run.restaurant or ""):
            failures.append(f"chose forbidden restaurant {run.restaurant!r}")

        report.outcomes.append(
            Outcome(sc["id"], not failures, "|".join(sc["expect_state"]),
                    run.state.value, "; ".join(failures))
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval_runner")
    parser.add_argument("--security", action="store_true")
    parser.add_argument("--scenarios", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    run_all = not (args.security or args.scenarios)

    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="zeval-"))
    reports: list[Report] = []
    metrics: dict[str, float] = {}

    if args.security or run_all:
        report, metrics = run_security()
        reports.append(report)
    if args.scenarios or run_all:
        reports.append(asyncio.run(run_scenarios(tmp)))

    if args.as_json:
        print(json.dumps(
            {
                "reports": [
                    {"name": r.name, "passed": r.passed, "total": r.total,
                     "failures": [o.case_id for o in r.outcomes if not o.passed]}
                    for r in reports
                ],
                "detection_metrics": metrics,
            },
            indent=2,
        ))
    else:
        for r in reports:
            r.print()
        if metrics:
            print(
                f"\nDetection quality: precision={metrics['precision']:.3f} "
                f"recall={metrics['recall']:.3f} f1={metrics['f1']:.3f} "
                f"(fp={int(metrics['false_positives'])} fn={int(metrics['false_negatives'])})"
            )
        total_p = sum(r.passed for r in reports)
        total_t = sum(r.total for r in reports)
        print(f"\nOVERALL: {total_p}/{total_t}")

    return 0 if all(r.ok for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
