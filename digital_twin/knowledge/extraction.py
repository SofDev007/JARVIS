"""Document text extraction — turn a file into plain text for ingestion.

One entry point, :func:`extract_text`, dispatches on suffix. The design
mirrors every other backend in the system: the common formats (``.txt``,
``.md``, source code) need nothing; the heavy ones (``.pdf``, ``.docx``)
resolve their dependency lazily and, when it is missing, raise
:class:`ExtractionError` with install guidance rather than silently
producing garbage. Nothing here reaches the network or executes document
content.

Supported today:

* ``.txt`` / ``.md`` / ``.rst`` / source files → read as UTF-8
  (replacement on bad bytes);
* ``.pdf`` → ``pdftotext`` (Poppler) if on PATH, else the ``pypdf``
  package if importable, else a guided error;
* ``.docx`` → stdlib zip + XML (no dependency — a .docx *is* a zip of
  XML), extracting paragraph text;
* ``.html`` / ``.htm`` → stdlib ``HTMLParser``, scripts/styles dropped.
"""

from __future__ import annotations

import html
import logging
import shutil
import subprocess
import zipfile
from html.parser import HTMLParser
from pathlib import Path

logger = logging.getLogger(__name__)

_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv",
    ".py", ".js", ".ts", ".java", ".c", ".h", ".cpp", ".go", ".rs",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sh",
}
_SUPPORTED = _TEXT_SUFFIXES | {".pdf", ".docx", ".html", ".htm"}


class ExtractionError(ValueError):
    """A file could not be turned into text (bad format or missing tool)."""


def supported_suffixes() -> frozenset[str]:
    return frozenset(_SUPPORTED)


def is_supported(path: str | Path) -> bool:
    return Path(path).suffix.lower() in _SUPPORTED


# ---------------------------------------------------------------------------
def extract_text(path: str | Path) -> str:
    """Extract plain text from a document, dispatching on suffix."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in _TEXT_SUFFIXES:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".pdf":
        return _extract_pdf(path)
    if suffix == ".docx":
        return _extract_docx(path)
    if suffix in (".html", ".htm"):
        return _extract_html(path)
    raise ExtractionError(
        f"unsupported document type {suffix!r} — supported: "
        f"{', '.join(sorted(_SUPPORTED))}"
    )


# ---------------------------------------------------------------------------
def _extract_pdf(path: Path) -> str:
    pdftotext = shutil.which("pdftotext")
    if pdftotext:
        try:
            result = subprocess.run(
                [pdftotext, "-q", "-enc", "UTF-8", str(path), "-"],
                capture_output=True, text=True, timeout=120, check=True,
            )
            return result.stdout
        except (subprocess.SubprocessError, OSError) as exc:
            raise ExtractionError(f"pdftotext failed on {path.name}: {exc}") from exc
    try:
        import pypdf
    except ImportError as exc:
        raise ExtractionError(
            "PDF extraction needs either the 'pdftotext' command (install "
            "Poppler) or the pypdf package (pip install pypdf)"
        ) from exc
    try:
        reader = pypdf.PdfReader(str(path))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:  # pypdf raises a zoo of errors
        raise ExtractionError(f"pypdf failed on {path.name}: {exc}") from exc


def _extract_docx(path: Path) -> str:
    """A .docx is a zip of XML — read paragraphs without a dependency."""
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise ExtractionError(
            f"not a readable .docx: {path.name} ({exc})") from exc
    # Paragraphs are <w:p>…</w:p>; text lives in <w:t>…</w:t>. A tiny
    # state machine over the tags avoids a full XML dependency and keeps
    # paragraph boundaries (which the chunker cares about).
    import re

    paragraphs = re.split(r"</w:p>", xml)
    lines: list[str] = []
    for paragraph in paragraphs:
        runs = re.findall(r"<w:t[^>]*>(.*?)</w:t>", paragraph, flags=re.S)
        if runs:
            lines.append(html.unescape("".join(runs)))
    return "\n\n".join(lines)


class _TextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        if tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3",
                   "h4", "h5", "h6"):
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1
        if tag in ("p", "div", "li", "tr"):
            self._chunks.append("\n")

    def handle_data(self, data):
        if not self._skip and data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        import re

        joined = "".join(self._chunks)
        return re.sub(r"\n{3,}", "\n\n", joined).strip()


def _extract_html(path: Path) -> str:
    parser = _TextHTMLParser()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    return parser.text()
