"""Compare this chat app with other assistants, feature by feature, and rank what to build next.

The comparison data is a hand-kept checklist (data/feature_matrix.json) with a date and sources. It is not measured;
the dashboard says so. What is measured is the other half: which features THIS chat declares it has, read live from the
version config it registered with the Auditor.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_DATA = Path(__file__).resolve().parent.parent / "data" / "feature_matrix.json"
EFFORT_ORDER = {"S": 0, "M": 1, "L": 2}


@lru_cache(maxsize=1)
def load_matrix() -> dict[str, Any]:
    return json.loads(_DATA.read_text(encoding="utf-8"))


def analyse(ours: list[str] | None, matrix: dict[str, Any] | None = None) -> dict[str, Any]:
    """`ours` is the list of feature ids the chat says it supports (None: the chat has not reported any)."""
    m = matrix or load_matrix()
    have = set(ours or [])
    rows: list[dict[str, Any]] = []
    for f in m["features"]:
        vals = dict(zip(m["products"], f["values"]))
        yes = sum(1 for v in f["values"] if v == "yes")
        known = sum(1 for v in f["values"] if v != "unknown")
        mine = "yes" if f["id"] in have else "no"
        rows.append({**{k: f[k] for k in ("id", "group", "name", "impact", "effort", "how")}, "values": vals, "others_yes": yes,
                     "others_known": known, "ours": mine,
                     "status": "parity" if mine == "yes" else ("gap" if yes >= max(2, (known + 1) // 2) else "optional")})
    gaps = [r for r in rows if r["status"] == "gap"]
    # most widespread first, then biggest impact, then cheapest to build
    gaps.sort(key=lambda r: (-r["others_yes"], -r["impact"], EFFORT_ORDER.get(r["effort"], 3)))
    covered = sum(1 for r in rows if r["ours"] == "yes")
    return {"as_of": m["as_of"], "note": m["note"], "sources": m["sources"], "products": m["products"], "reported": ours is not None,
            "rows": rows, "gaps": [{"id": g["id"], "name": g["name"], "others_yes": g["others_yes"], "of": len(m["products"]),
                                    "impact": g["impact"], "effort": g["effort"], "how": g["how"]} for g in gaps],
            "covered": covered, "total": len(rows), "unique": m["unique"]}
