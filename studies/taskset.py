"""The task set and the two tools every study agent gets. Small and checkable on purpose: each question has one right answer, so
the runner can score correctness itself and compare it with what the canary says."""
from __future__ import annotations

import ast
import math
import operator
import random
import re
import unicodedata

COUNTRIES = {
    "Australia": {"capital": "Canberra", "currency_code": "AUD"}, "Japan": {"capital": "Tokyo", "currency_code": "JPY"},
    "Canada": {"capital": "Ottawa", "currency_code": "CAD"}, "Brazil": {"capital": "Brasilia", "currency_code": "BRL"},
    "Kenya": {"capital": "Nairobi", "currency_code": "KES"}, "Norway": {"capital": "Oslo", "currency_code": "NOK"},
    "Egypt": {"capital": "Cairo", "currency_code": "EGP"}, "India": {"capital": "New Delhi", "currency_code": "INR"},
    "Peru": {"capital": "Lima", "currency_code": "PEN"}, "Poland": {"capital": "Warsaw", "currency_code": "PLN"},
    "Chile": {"capital": "Santiago", "currency_code": "CLP"}, "Vietnam": {"capital": "Hanoi", "currency_code": "VND"},
}

# (question, tool the question needs, the tool's arguments, the right answer)
TASKS: list[tuple[str, str, dict, str]] = []
for _country, _row in COUNTRIES.items():
    TASKS.append((f"What is the capital of {_country}?", "lookup_country", {"country": _country, "field": "capital"}, _row["capital"]))
    TASKS.append((f"Which currency code does {_country} use?", "lookup_country", {"country": _country, "field": "currency_code"}, _row["currency_code"]))
for _q, _expr, _ans in [
    ("What is 17% of 2340?", "0.17 * 2340", "397.8"), ("What is 1284 times 37?", "1284 * 37", "47508"),
    ("What is 15 + 28 * 3?", "15 + 28 * 3", "99"), ("What is 2 to the power of 12?", "2 ** 12", "4096"),
    ("What is 9876 minus 4321?", "9876 - 4321", "5555"), ("What is 144 divided by 6, plus 19?", "144 / 6 + 19", "43"),
    ("What is 250 times 0.08?", "250 * 0.08", "20"), ("What is 3.5 times 12?", "3.5 * 12", "42"),
    ("What is 1000 divided by 8?", "1000 / 8", "125"), ("What is 45 squared?", "45 ** 2", "2025"),
    ("What is 12.5% of 800?", "0.125 * 800", "100"), ("What is 7 times 8 times 9?", "7 * 8 * 9", "504"),
]:
    TASKS.append((_q, "calculate", {"expression": _expr}, _ans))


def sample(n: int, seed: int = 11) -> list[tuple[str, str, dict, str]]:
    """n tasks, cycling through a shuffled copy of the whole set, the same every time for the same seed."""
    rng = random.Random(seed)
    pool: list = []
    while len(pool) < n:
        batch = list(TASKS)
        rng.shuffle(batch)
        pool += batch
    return pool[:n]


def lookup_country(country: str, field: str) -> str:
    row = COUNTRIES.get(str(country).strip().title())
    if row is None:
        return f"No data for {country}."
    if field not in row:
        return f"Unknown field {field}. Fields: capital, currency_code."
    return row[field]


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.Pow: operator.pow,
        ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Mod: operator.mod}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("only + - * / ** % and numbers are allowed")


def calculate(expression: str) -> str:
    try:
        value = _eval(ast.parse(str(expression).replace("^", "**").replace(",", ""), mode="eval"))
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError) as exc:
        return f"Error: {exc}"
    return str(round(value, 10)).rstrip("0").rstrip(".") if isinstance(value, float) else str(value)


def _plain(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).lower()


def is_correct(answer: str | None, expected: str) -> bool:
    """Right if the answer contains the expected value: a number equal to it, or the expected words (accents ignored)."""
    if not answer:
        return False
    try:
        want = float(expected)
    except ValueError:
        return _plain(expected) in _plain(answer)
    for tok in re.findall(r"-?\d[\d,]*\.?\d*", answer):
        try:
            if math.isclose(float(tok.replace(",", "").rstrip(".")), want, rel_tol=1e-9, abs_tol=1e-9):
                return True
        except ValueError:
            continue
    return False
