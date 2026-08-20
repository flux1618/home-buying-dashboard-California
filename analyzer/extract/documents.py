"""Getting text out of a file, and only sending the part that matters.

Two jobs, and the second is a security control rather than a cost optimisation.

`docs/THREAT_MODEL.md` says "send only what's needed -- the relevant pages, not the whole
file". A 40-page inspection report has maybe six pages that mention systems and defects; the
rest is boilerplate, photographs, and the inspector's contact details. Narrowing to the pages
that contain schema-relevant keywords shrinks what leaves the machine, which shrinks the blast
radius if the provider is compromised or is retaining input.

## On PDFs

Plain text and Markdown are read directly. PDFs are attempted with `pdftotext` if it is on
the PATH, and refused with a clear message otherwise -- not silently skipped, and not handled
by adding a PDF library to a project whose core is deliberately dependency-free (ADR 0002).
The refusal tells the user the one command that fixes it. An inspection report is a PDF, so
this is the common path, and "install poppler-utils" is a better answer than a parse of
unknown quality from a dependency nobody reviewed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".text"}

# A page mentioning any of these plausibly contains a field in the declared schema. Kept in
# sync with `core.extraction.FIELDS` by hand, which is a real maintenance seam -- a field
# added there without a keyword here will simply never be found, because the page holding it
# gets filtered out before the model sees it. `tests/test_extraction_documents.py` asserts
# every schema field has at least one keyword that could plausibly locate it.
RELEVANT_KEYWORDS = (
    "roof", "shingle", "hvac", "furnace", "heat pump", "air condition", "water heater",
    "hoa", "association", "assessment", "covenant", "dues",
    "well", "septic", "sewer", "water supply", "public water",
    "foundation", "crawl space", "slab", "basement",
    "flood", "water intrusion", "moisture", "drainage",
    "easement", "right-of-way", "right of way", "encroach",
    "permit", "unpermitted", "code violation",
    "defect", "deficien", "repair", "recommend", "safety",
    "year built", "square feet", "square footage", "heated area",
)

# `\f` is what pdftotext emits between pages, so page numbers in the output are real page
# numbers rather than an approximation from character counts.
_PAGE_BREAK = "\f"


@dataclass(frozen=True)
class Document:
    """Loaded text, plus what was dropped on the way in.

    `pages_total` and `pages_sent` are both surfaced to the caller and written to the call
    log. A run that sent 6 of 41 pages and found nothing is a different problem from a run
    that sent all 41 and found nothing, and without these two numbers those look identical.
    """

    path: str
    text: str
    pages_total: int
    pages_sent: int
    filtered: bool

    @property
    def chars(self) -> int:
        return len(self.text)


class DocumentError(RuntimeError):
    """The file could not be turned into text. Raised with an actionable message."""


def _pdf_to_text(path: Path) -> str:
    if shutil.which("pdftotext") is None:
        raise DocumentError(
            f"{path.name} is a PDF and `pdftotext` is not installed. Install it "
            "(`apt install poppler-utils` / `brew install poppler`), or convert the file to "
            "text first. A PDF library is deliberately not a dependency of this project -- "
            "see docs/adr/0002."
        )
    try:
        proc = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - needs a pathological PDF
        raise DocumentError(f"pdftotext timed out on {path.name} after 120s") from exc
    if proc.returncode != 0:
        raise DocumentError(f"pdftotext failed on {path.name}: {proc.stderr.strip()[:300]}")
    if not proc.stdout.strip():
        raise DocumentError(
            f"{path.name} produced no text. It is most likely a scan of paper, which needs "
            "OCR -- this project does not do OCR, so the file has to be converted elsewhere "
            "first."
        )
    return proc.stdout


def _is_relevant(page: str) -> bool:
    low = page.lower()
    return any(keyword in low for keyword in RELEVANT_KEYWORDS)


def load_document(path: str | Path, *, filter_pages: bool = True, max_chars: int = 120_000) -> Document:
    """Read a file to text, optionally keeping only schema-relevant pages.

    `filter_pages=False` exists for the case where a user says the filter dropped the page
    they cared about. It is an explicit widening of what gets sent, which is why it is a
    parameter here and why redaction -- which narrows -- has no such escape hatch.
    """
    p = Path(path)
    if not p.exists():
        raise DocumentError(f"no such file: {p}")
    if p.is_dir():
        raise DocumentError(f"{p} is a directory")

    suffix = p.suffix.lower()
    if suffix == ".pdf":
        raw = _pdf_to_text(p)
    elif suffix in TEXT_SUFFIXES or suffix == "":
        try:
            raw = p.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise DocumentError(
                f"{p.name} is not UTF-8 text. If it is a PDF or a Word file, convert it first."
            ) from exc
    else:
        raise DocumentError(
            f"unsupported file type {suffix!r}. Supported: .txt, .md, .pdf (with pdftotext)."
        )

    pages = raw.split(_PAGE_BREAK)
    pages_total = len(pages)

    if filter_pages and pages_total > 1:
        kept = [pg for pg in pages if _is_relevant(pg)]
        # If the filter would drop everything, send it all instead. A keyword list that
        # matches nothing means the list is wrong for this document, and silently sending an
        # empty string to a model would report "no findings" for what is really a filter bug.
        if not kept:
            kept = pages
            filtered = False
        else:
            filtered = len(kept) < pages_total
        text = "\n".join(kept)
    else:
        text = raw
        kept = pages
        filtered = False

    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()

    if len(text) > max_chars:
        # Truncation is reported, not hidden. The alternative -- sending a 400KB document to a
        # model with a context limit -- fails in a way that looks like a model problem.
        text = text[:max_chars]
        filtered = True

    return Document(
        path=str(p),
        text=text,
        pages_total=pages_total,
        pages_sent=len(kept),
        filtered=filtered,
    )
