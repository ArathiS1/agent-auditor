import dataclasses
import math
from datetime import datetime, timezone
from enum import Enum

from agent_auditor.sdk import to_jsonable


class Color(Enum):
    RED = "red"


@dataclasses.dataclass
class Doc:
    title: str
    score: float


class Weird:
    def __repr__(self):
        return "<Weird>"


def test_primitives_pass_through():
    assert to_jsonable({"a": 1, "b": [True, None, "x"]}) == {"a": 1, "b": [True, None, "x"]}


def test_long_strings_truncated():
    out = to_jsonable("x" * 100, max_str=10)
    assert out.startswith("x" * 10) and "truncated 90 chars" in out


def test_collections_capped():
    out = to_jsonable(list(range(100)), max_items=5)
    assert out[:5] == [0, 1, 2, 3, 4] and "+95 more" in out[5]
    d = to_jsonable({i: i for i in range(10)}, max_items=3)
    assert len(d) == 4 and "+7 more keys" in d["..."]


def test_depth_limited():
    nested = {"a": {"b": {"c": {"d": 1}}}}
    out = to_jsonable(nested, max_depth=2)
    assert isinstance(out["a"]["b"], str)


def test_special_types():
    assert to_jsonable(Color.RED) == "red"
    assert to_jsonable(b"abc") == "<3 bytes>"
    assert to_jsonable(datetime(2026, 1, 1, tzinfo=timezone.utc)).startswith("2026-01-01")
    assert to_jsonable(float("nan")) == "nan"
    assert to_jsonable(math.inf) == "inf"
    assert to_jsonable(Doc("a", 0.5)) == {"title": "a", "score": 0.5}
    assert to_jsonable(Weird()) == "<Weird>"


def test_object_with_model_dump():
    class M:
        def model_dump(self):
            return {"k": "v"}

    assert to_jsonable(M()) == {"k": "v"}


def test_cyclic_structure_terminates():
    a = {}
    a["self"] = a
    assert to_jsonable(a) is not None
