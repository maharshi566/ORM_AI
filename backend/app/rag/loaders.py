"""Load knowledge-base files and clean their text.

Supported files:

* Markdown (``.md``) and plain text (``.txt``) that start with YAML front-matter
  (see ``knowledge_base/README.md`` for the fields).
* PDF (``.pdf``), read page by page with pypdf. A PDF cannot hold front-matter, so
  put its metadata in a side file next to it, ``<name>.meta.yaml``. Without one the
  loader derives what it can from the folder and file name, and warns.

Cleaning keeps headings and tables (the chunker needs them) and removes what only
gets in the way of search: Windows line endings, invisible characters, HTML
comments, extra blank lines and, for PDFs, page headers, footers and page numbers.
"""

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import yaml

from app.core.logging import get_logger
from app.rag.text import normalize_unicode

logger = get_logger(__name__)

SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt", ".pdf"}
REQUIRED_FIELDS = ("document_id", "title", "source", "category", "version", "effective_date")
CATEGORIES = {"policy", "sop", "supplier_terms", "faq", "shop_profile", "supplier_flyer"}
SOURCES = {"internal", "supplier", "external"}

# Folder name -> category, used for files without metadata (PDFs without a side file).
FOLDER_CATEGORY = {
    "policies": "policy",
    "sops": "sop",
    "suppliers": "supplier_terms",
    "faqs": "faq",
    "shops": "shop_profile",
    "external": "supplier_flyer",
}


class LoaderError(ValueError):
    """A knowledge-base file is missing, unreadable or has invalid metadata."""

    def __init__(self, path: Path | str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = str(path)


@dataclass(frozen=True)
class DocumentMetadata:
    document_id: str
    title: str
    source: str  # internal, supplier or external
    category: str
    version: int
    effective_date: date
    status: str = "current"  # current or superseded
    shop_id: str | None = None  # set when the document applies to one shop only
    trust: Literal["trusted", "untrusted"] = "trusted"

    @property
    def key(self) -> str:
        """One version of one document, e.g. ``POL-CREDIT-001@v2`` (= documents.id)."""
        return f"{self.document_id}@v{self.version}"

    @property
    def is_current(self) -> bool:
        return self.status == "current"


@dataclass(frozen=True)
class Page:
    text: str
    number: int | None = None  # PDF page number; None for Markdown and text


@dataclass
class LoadedDocument:
    metadata: DocumentMetadata
    path: str  # relative to the knowledge-base folder, with forward slashes
    format: Literal["markdown", "text", "pdf"]
    pages: list[Page]
    content_hash: str  # SHA-256 of the file's bytes
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(page.text for page in self.pages)


# ----------------------------------------------------------------- cleaning

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BLANK_LINES = re.compile(r"\n{3,}")
_PAGE_NUMBER = re.compile(r"^(page\s+)?\d+(\s*(of|/)\s*\d+)?$", re.IGNORECASE)
_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")


def clean_text(text: str) -> str:
    """Normalise text without touching its structure (headings, lists, tables)."""
    text = normalize_unicode(text.replace("\r\n", "\n").replace("\r", "\n"))
    text = _HTML_COMMENT.sub("", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", text).strip()


def clean_pdf_pages(pages: list[str]) -> list[str]:
    """Clean PDF page text: join hyphenated words, drop page numbers and repeated
    header/footer lines (lines found on at least 60% of pages, when there are 3+)."""
    pages = [_HYPHEN_BREAK.sub(r"\1\2", clean_text(page)) for page in pages]
    repeated: set[str] = set()
    if len(pages) >= 3:
        counts = Counter(
            line.strip() for page in pages for line in set(page.split("\n")) if line.strip()
        )
        repeated = {line for line, n in counts.items() if n >= 0.6 * len(pages)}
    cleaned = []
    for page in pages:
        lines = [
            line
            for line in page.split("\n")
            if line.strip() not in repeated and not _PAGE_NUMBER.match(line.strip())
        ]
        cleaned.append(clean_text("\n".join(lines)))
    return cleaned


# ---------------------------------------------------------------- metadata


def split_front_matter(text: str) -> tuple[dict[str, Any] | None, str]:
    """Return (front-matter dict or None, body). Expects text with ``\\n`` line endings."""
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError("front-matter starts with --- but has no closing ---")
    meta = yaml.safe_load(text[4:end]) or {}
    if not isinstance(meta, dict):
        raise ValueError("front-matter is not a set of key: value lines")
    return meta, text[end + 5 :]


def _as_date(value: Any, path: Path) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise LoaderError(path, f"effective_date {value!r} is not a YYYY-MM-DD date") from exc


def parse_metadata(meta: dict[str, Any], path: Path) -> DocumentMetadata:
    missing = [name for name in REQUIRED_FIELDS if meta.get(name) in (None, "")]
    if missing:
        raise LoaderError(path, f"missing front-matter fields: {', '.join(missing)}")
    category = str(meta["category"])
    if category not in CATEGORIES:
        raise LoaderError(path, f"unknown category {category!r}; use one of {sorted(CATEGORIES)}")
    source = str(meta["source"])
    if source not in SOURCES:
        raise LoaderError(path, f"unknown source {source!r}; use one of {sorted(SOURCES)}")
    try:
        version = int(meta["version"])
    except (TypeError, ValueError) as exc:
        raise LoaderError(path, f"version {meta['version']!r} is not a whole number") from exc
    status = str(meta.get("status", "current"))
    if status not in {"current", "superseded"}:
        raise LoaderError(path, f"status must be current or superseded, not {status!r}")
    untrusted = meta.get("trust") == "untrusted" or source == "external"
    return DocumentMetadata(
        document_id=str(meta["document_id"]).strip(),
        title=str(meta["title"]).strip(),
        source=source,
        category=category,
        version=version,
        effective_date=_as_date(meta["effective_date"], path),
        status=status,
        shop_id=str(meta["shop_id"]).strip() if meta.get("shop_id") else None,
        trust="untrusted" if untrusted else "trusted",
    )


def _derived_metadata(path: Path, base_dir: Path, title: str | None) -> DocumentMetadata:
    """Metadata for a file without any: from the folder and the file name."""
    relative = path.relative_to(base_dir)
    folder = relative.parts[0] if len(relative.parts) > 1 else ""
    category = FOLDER_CATEGORY.get(folder, "policy")
    source = "external" if folder == "external" else "internal"
    stem = re.sub(r"[^A-Za-z0-9]+", "-", path.stem).strip("-").upper()
    return DocumentMetadata(
        document_id=stem,
        title=title or path.stem.replace("-", " ").replace("_", " ").capitalize(),
        source=source,
        category=category,
        version=1,
        effective_date=datetime.fromtimestamp(path.stat().st_mtime).date(),
        trust="untrusted" if source == "external" else "trusted",
    )


# ------------------------------------------------------------------ loaders


def _load_pdf(path: Path, base_dir: Path) -> tuple[DocumentMetadata, list[Page], list[str]]:
    from pypdf import PdfReader  # imported here: only needed when a PDF exists

    try:
        reader = PdfReader(path)
        raw_pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # pypdf raises many types for damaged files
        raise LoaderError(path, f"cannot read PDF ({type(exc).__name__})") from exc
    warnings: list[str] = []
    side_file = path.with_name(path.stem + ".meta.yaml")
    if side_file.exists():
        try:
            meta = yaml.safe_load(side_file.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise LoaderError(side_file, f"invalid YAML: {exc}") from exc
        metadata = parse_metadata(meta, side_file)
    else:
        pdf_title = (reader.metadata.title if reader.metadata else None) or None
        metadata = _derived_metadata(path, base_dir, pdf_title)
        warnings.append(
            f"{path.name}: no {side_file.name}, so its ID, category and date were guessed"
        )
    pages = [
        Page(text=text, number=number)
        for number, text in enumerate(clean_pdf_pages(raw_pages), start=1)
        if text
    ]
    if not pages:
        raise LoaderError(path, "PDF has no extractable text (is it a scanned image?)")
    return metadata, pages, warnings


def load_file(path: Path, base_dir: Path) -> LoadedDocument:
    """Load one file. Raises LoaderError with a clear message when it cannot."""
    raw = path.read_bytes()
    relative = path.relative_to(base_dir).as_posix()
    content_hash = hashlib.sha256(raw).hexdigest()
    if path.suffix.lower() == ".pdf":
        metadata, pages, warnings = _load_pdf(path, base_dir)
        return LoadedDocument(metadata, relative, "pdf", pages, content_hash, warnings)

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise LoaderError(path, "is not UTF-8 text") from exc
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    try:
        meta, body = split_front_matter(text)
    except (ValueError, yaml.YAMLError) as exc:
        raise LoaderError(path, f"invalid front-matter: {exc}") from exc
    if meta is None:
        raise LoaderError(path, "missing front-matter (start the file with --- metadata ---)")
    metadata = parse_metadata(meta, path)
    body = clean_text(body)
    if not body:
        raise LoaderError(path, "has no text after the front-matter")
    file_format: Literal["markdown", "text"] = (
        "text" if path.suffix.lower() == ".txt" else "markdown"
    )
    return LoadedDocument(metadata, relative, file_format, [Page(body)], content_hash)


def iter_knowledge_files(base_dir: Path) -> list[Path]:
    """Every loadable file under ``base_dir``, sorted. README and hidden files are skipped."""
    return sorted(
        path
        for path in base_dir.rglob("*")
        if path.is_file()
        and path.suffix.lower() in SUPPORTED_SUFFIXES
        and path.name.lower() != "readme.md"
        and not any(part.startswith(".") for part in path.relative_to(base_dir).parts)
    )


def load_directory(base_dir: Path) -> list[LoadedDocument]:
    """Load every document under ``base_dir``.

    All problems are collected and raised together, so one run lists every file
    that needs fixing. Two files may not claim the same document ID and version.
    """
    if not base_dir.is_dir():
        raise LoaderError(base_dir, "knowledge-base folder not found")
    documents: list[LoadedDocument] = []
    problems: list[str] = []
    for path in iter_knowledge_files(base_dir):
        try:
            documents.append(load_file(path, base_dir))
        except LoaderError as exc:
            problems.append(str(exc))
    seen: dict[str, str] = {}
    for doc in documents:
        key = doc.metadata.key
        if key in seen:
            problems.append(f"{doc.path}: {key} is already used by {seen[key]}")
        seen[key] = doc.path
        for warning in doc.warnings:
            logger.warning("knowledge_file_warning", warning=warning)
    if problems:
        raise LoaderError(base_dir, "cannot load the knowledge base:\n  " + "\n  ".join(problems))
    return documents
