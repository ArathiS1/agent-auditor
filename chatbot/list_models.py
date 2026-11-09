"""Lists the model names your key can use at one provider, so you can copy one into .env.

  python chatbot\\list_models.py airouter
  python chatbot\\list_models.py nabh
  python chatbot\\list_models.py openrouter

Then put the name you want in .env, for example  AIROUTER_MODEL=openai/gpt-5-nano
The key is never printed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from envfile import load_env  # noqa: E402

load_env(Path(__file__).resolve().parent)
from providers import all_presets  # noqa: E402


def main(argv: list[str]) -> int:
    name = (argv[0] if argv else "").lower()
    p = all_presets().get(name)
    if p is None or p.style != "openai":
        print("Usage: python chatbot\\list_models.py <" + "|".join(n for n, x in all_presets().items() if x.style == "openai") + ">")
        return 2
    if p.key_env and not p.key:
        print(f"{p.key_env} is not set in .env")
        return 2
    try:
        r = httpx.get(p.base_url + "/models", headers={"authorization": f"Bearer {p.key}"} if p.key else {}, timeout=20)
    except httpx.HTTPError as exc:
        print(f"Could not reach {p.base_url}: {type(exc).__name__}")
        return 1
    if r.status_code != 200:
        print(f"HTTP {r.status_code}: {r.text[:200]}")
        return 1
    ids = sorted(str(m.get("id")) for m in r.json().get("data", []))
    print(f"{len(ids)} models at {p.base_url}  (default used by the chat: {p.model})")
    for i in ids:
        print("  " + i)
    print(f"\nTo use one, add to .env:  {name.upper()}_MODEL=<name from the list>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
