"""Build the docs pages of the website from the Markdown in docs/.

Run from the repository root:  python site/build_docs.py
Needs:  pip install markdown
The landing page (site/index.html) is hand-written; this script only makes site/docs/*.html
and copies docs/assets into site/assets so the site is self-contained.
"""
from __future__ import annotations

import html
import re
import shutil
from pathlib import Path

import markdown

GH = "https://github.com/ArathiS1/agent-auditor"
ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
DOCS = ROOT / "docs"

# (output name, source file, sidebar label)
PAGES = [
    ("index", SITE / "_hub.md", "Overview"),
    ("reference", DOCS / "reference.md", "Reference"),
    ("architecture", DOCS / "architecture.md", "Architecture"),
    ("deployment", DOCS / "deployment.md", "Deployment"),
    ("importing-traces", DOCS / "importing-traces.md", "OpenTelemetry import"),
    ("answer-correctness", DOCS / "answer-correctness.md", "Wrong answers"),
    ("case-study", DOCS / "case-study.md", "Case study"),
]
GENERATED = {src.name: out for out, src, _ in PAGES}

TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} | Agent Auditor</title>
<meta name="description" content="{desc}">
<meta name="theme-color" content="#13211c">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Ccircle cx='8' cy='8' r='6' fill='%23c4350a'/%3E%3C/svg%3E">
<link rel="stylesheet" href="../style.css">
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="bar"><div class="wrap">
  <a class="brand" href="../index.html"><span class="dot"></span>Agent Auditor</a>
  <nav aria-label="Main">
    <a href="../index.html#loop">How it works</a>
    <a href="../index.html#verdict">Real result</a>
    <a href="index.html" aria-current="page">Docs</a>
    <a class="gh" href="{gh}">GitHub</a>
  </nav>
</div></header>
<div class="wrap docs">
  <aside class="side" aria-label="Documentation">
    <h4>Documentation</h4>
{side}
    <a class="ext" href="{gh}">Source on GitHub</a>
  </aside>
  <main class="art" id="main">
{body}
  </main>
</div>
<script src="../site.js"></script>
</body>
</html>
"""

def fix_links(page: str) -> str:
    def repl(m: re.Match) -> str:
        href = m.group(1)
        if href.startswith(("http://", "https://", "#", "mailto:", "data:")):
            return m.group(0)
        path, _, frag = href.partition("#")
        name = Path(path).name
        if name in GENERATED and not path.startswith(".."):
            return f'href="{GENERATED[name]}.html{"#" + frag if frag else ""}"'
        if path.startswith("../"):
            target = "/blob/main/" + path[3:] + ("#" + frag if frag else "")
            return f'href="{GH}{target}"'
        return m.group(0)

    return re.sub(r'href="([^"]*)"', repl, page)


def render(src: Path) -> tuple[str, str, str]:
    text = src.read_text(encoding="utf-8").replace("](assets/", "](../assets/")
    md = markdown.Markdown(extensions=["tables", "fenced_code", "toc", "sane_lists"])
    body = md.convert(text)
    # The diagram is a pre-rendered SVG (site/assets/architecture.svg), so the page needs no script.
    body = re.sub(
        r'<pre><code class="language-mermaid">.*?</code></pre>',
        '<a href="../assets/architecture.svg"><img class="diagram" src="../assets/architecture.svg" alt="Flow diagram of the architecture described below"></a>',
        body,
        flags=re.S,
    )
    body = body.replace("<table>", '<div class="tbl"><table>').replace("</table>", "</table></div>")
    body = fix_links(body)
    first = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S)
    title = re.sub(r"<[^>]+>", "", first.group(1)) if first else src.stem
    para = re.search(r"<p>(.*?)</p>", body, re.S)
    desc = re.sub(r"<[^>]+>", "", para.group(1)) if para else title
    desc = html.escape(" ".join(desc.split())[:180], quote=True)
    return html.unescape(title), desc, body


def main() -> None:
    (SITE / "docs").mkdir(exist_ok=True)
    assets = SITE / "assets"
    assets.mkdir(exist_ok=True)
    for png in (DOCS / "assets").glob("*"):
        shutil.copy2(png, assets / png.name)  # architecture.svg lives only in site/assets
    for out, src, _label in PAGES:
        title, desc, body = render(src)
        current = ' aria-current="page"'
        side = "\n".join(
            f'    <a href="{o}.html"{current if o == out else ""}>{lab}</a>'
            for o, _s, lab in PAGES
        )
        page = TEMPLATE.format(
            title=html.escape(title), desc=desc, gh=GH, side=side, body=body
        )
        (SITE / "docs" / f"{out}.html").write_text(page, encoding="utf-8")
        print("wrote", f"site/docs/{out}.html")


if __name__ == "__main__":
    main()
