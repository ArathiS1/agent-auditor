"""Turns an uploaded file into text (or an image) the assistant can use."""
from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree

MAX_FILE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000
IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
TEXT_EXTS = {"txt", "md", "markdown", "csv", "tsv", "json", "xml", "yaml", "yml", "toml", "ini", "log", "sql", "py", "js", "ts",
             "tsx", "jsx", "java", "c", "cpp", "h", "hpp", "cs", "go", "rs", "rb", "php", "sh", "bat", "ps1", "css", "scss", "html",
             "htm", "vue", "svelte", "kt", "swift", "r", "m", "lua", "dart", "scala", "ipynb", "tex", "rst", "cfg", "gradle", "proto"}


class FileProblem(Exception):
    """A problem with the uploaded file, worded for the user."""


def _docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            root = ElementTree.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        raise FileProblem("That Word file could not be read. Try saving it again as .docx.") from None
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs = []
    for p in root.iter(ns + "p"):
        text = "".join(t.text or "" for t in p.iter(ns + "t"))
        if text.strip():
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise FileProblem("Reading PDFs needs one extra install: run  pip install pypdf  and restart the chat server.") from None
    try:
        pages = [(page.extract_text() or "") for page in PdfReader(io.BytesIO(data)).pages]
    except Exception:
        raise FileProblem("That PDF could not be read (it may be damaged or password protected).") from None
    text = "\n\n".join(f"[page {n}]\n{t}" for n, t in enumerate(pages, 1) if t.strip())
    if not text.strip():
        raise FileProblem("That PDF has no selectable text (it looks like a scan), so I cannot read it.")
    return text


def extract(name: str, data: bytes) -> tuple[str, str, str]:
    """Return (kind, text_or_empty, media_type). kind is "text" or "image". Raises FileProblem."""
    if not data:
        raise FileProblem("That file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise FileProblem(f"That file is too large (limit {MAX_FILE_BYTES // (1024 * 1024)} MB).")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in IMAGE_TYPES:
        if len(data) > MAX_IMAGE_BYTES:
            raise FileProblem("That image is too large (limit 5 MB).")
        return "image", "", IMAGE_TYPES[ext]
    if ext == "pdf":
        text = _pdf_text(data)
    elif ext == "docx":
        text = _docx_text(data)
    elif ext in TEXT_EXTS or not ext:  # code and web files are kept exactly as written
        text = data.decode("utf-8", errors="replace")
    elif ext in ("xlsx", "xls", "doc", "ppt", "pptx"):
        raise FileProblem(f"I cannot read .{ext} files yet. Save it as .csv (sheets), .docx or .pdf and upload that.")
    else:
        raise FileProblem(f"I cannot read .{ext} files. Try txt, md, csv, json, code, pdf, docx or an image.")
    text = re.sub(r"[ \t]+\n", "\n", text).strip()[:MAX_TEXT_CHARS]
    if not text:
        raise FileProblem("I found no text in that file.")
    return "text", text, "text/plain"
