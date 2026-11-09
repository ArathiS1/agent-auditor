"""Measure the classifier on synthetic failures with known causes.

    python -m agent_auditor.rootcause.benchmark            # prints a report
    python -m agent_auditor.rootcause.benchmark --seed 3 --per-cause 100

Read the caveats in `synthetic.py`: this checks the rules and shows where evidence runs out; it is an
optimistic bound, not your production accuracy.
"""
from __future__ import annotations

import argparse
from collections import Counter
from typing import Any

from .classify import classify_run
from .synthetic import TIERS, Case, generate


def run_benchmark(seed: int = 0, per_cause: int = 40) -> dict[str, Any]:
    cases = generate(seed, per_cause)
    return evaluate_cases(cases)


def evaluate_cases(cases: list[Case]) -> dict[str, Any]:
    confusion: Counter = Counter()
    tier_total: Counter = Counter()
    tier_ok: Counter = Counter()
    strength_total: Counter = Counter()
    strength_ok: Counter = Counter()
    for case in cases:
        d = classify_run(case.run)
        pred = d.primary or "healthy"
        ok = pred == case.truth
        confusion[(case.truth, pred)] += 1
        tier_total[case.tier] += 1
        tier_ok[case.tier] += ok
        if d.strength:
            strength_total[d.strength] += 1
            strength_ok[d.strength] += ok

    labels = sorted({t for t, _ in confusion} | {p for _, p in confusion})
    per_cause: dict[str, dict[str, float | int]] = {}
    for label in labels:
        tp = confusion[(label, label)]
        support = sum(n for (t, _), n in confusion.items() if t == label)
        predicted = sum(n for (_, p), n in confusion.items() if p == label)
        precision = tp / predicted if predicted else None
        recall = tp / support if support else None
        f1 = 2 * precision * recall / (precision + recall) if precision and recall else (0.0 if support and predicted else None)
        per_cause[label] = {"support": support, "predicted": predicted, "precision": precision, "recall": recall, "f1": f1}

    total = len(cases)
    healthy_n = tier_total["healthy"]
    false_pos = sum(n for (t, p), n in confusion.items() if t == "healthy" and p != "healthy")
    return {
        "cases": total,
        "accuracy": sum(tier_ok.values()) / total if total else None,
        "by_tier": {t: {"n": tier_total[t], "accuracy": tier_ok[t] / tier_total[t]} for t in TIERS if tier_total[t]},
        "by_strength": {s: {"n": strength_total[s], "accuracy": strength_ok[s] / strength_total[s]} for s in ("strong", "moderate", "weak") if strength_total[s]},
        "healthy_false_positive_rate": false_pos / healthy_n if healthy_n else None,
        "per_cause": per_cause,
        "confusion": {f"{t} -> {p}": n for (t, p), n in sorted(confusion.items()) if t != p},
    }


def format_report(result: dict[str, Any]) -> str:
    pct = lambda x: "  n/a" if x is None else f"{x:5.0%}"  # noqa: E731
    lines = [f"Synthetic root-cause benchmark: {result['cases']} cases, overall accuracy {pct(result['accuracy']).strip()}", ""]
    lines.append("By tier")
    for tier, row in result["by_tier"].items():
        lines.append(f"  {tier:<9} n={row['n']:<5} accuracy {pct(row['accuracy'])}")
    lines.append(f"  healthy runs wrongly flagged as failing: {pct(result['healthy_false_positive_rate']).strip()}")
    lines += ["", "When the classifier says...  (how often it was right)"]
    for strength, row in result["by_strength"].items():
        lines.append(f"  {strength:<9} n={row['n']:<5} accuracy {pct(row['accuracy'])}")
    lines += ["", f"{'cause':<26}{'support':>8}{'precision':>11}{'recall':>9}"]
    for label, row in result["per_cause"].items():
        lines.append(f"{label:<26}{row['support']:>8}{pct(row['precision']):>11}{pct(row['recall']):>9}")
    lines += ["", "Most common mistakes (truth -> predicted)"]
    mistakes = sorted(result["confusion"].items(), key=lambda kv: -kv[1])[:8]
    lines += [f"  {n:>4}  {pair}" for pair, n in mistakes] or ["  none"]
    lines += ["", "Caveat: synthetic data written alongside the classifier. This is an optimistic bound;",
              "check diagnoses against incidents you have root-caused yourself."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--per-cause", type=int, default=40)
    args = parser.parse_args(argv)
    print(format_report(run_benchmark(args.seed, args.per_cause)))


if __name__ == "__main__":
    main()
