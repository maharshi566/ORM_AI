"""Uploading documents to the knowledge base, and re-ingesting it in the background.

An upload goes through these checks before anything is stored:

* **Type**: only .md, .txt and .pdf, and the bytes must match: a PDF must start with
  ``%PDF-``; a text file must be UTF-8 with no NUL bytes (so a renamed program or
  archive is refused whatever its name says). The ``Content-Type`` the browser sends is
  not trusted.
* **Size**: at most ``UPLOAD_MAX_MB``; reading stops as soon as the file is too big.
* **Name**: the file is saved under a name made here (``UPL-<shop>-<hash>``), never
  the uploaded name, so a name like ``../../app/main.py`` cannot write anywhere else.
  The same file uploaded again by the same shop is recognised, whatever its extension.
* **Metadata**: written here, never taken from the file. An uploaded Markdown file's
  own front matter is removed, so an upload cannot pose as a shop policy
  (``document_id: POL-CREDIT-001``) or mark itself trusted. Uploads are ``untrusted``
  (reference only, like a supplier flyer) unless the shop owner says otherwise, and
  belong to the uploader's shop.
* **Usable**: the stored file is read back with the same loader ingestion uses (a PDF
  must have text, a text file must have more than comments), so a file that would
  break ingestion is refused at upload. Ingestion also skips, rather than stops at,
  any upload it cannot use.

Files are stored in ``UPLOAD_DIR`` (``backend/data/uploads``), which the web server
never serves. ``store_upload`` reads files (and PDFs) and is slow, blocking work, so
the route runs it in a worker thread; PDFs are limited to ``MAX_PDF_PAGES`` pages.
Ingestion (the same pipeline as ``python -m scripts.ingest``, now with the uploads
included) runs as a background task; its progress is kept per job.
"""

import asyncio
import hashlib
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import FastAPI, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.paths import backend_path
from app.config.settings import Settings
from app.core.exceptions import AppError, ConflictError, InvalidRequestError
from app.core.logging import get_logger
from app.rag.loaders import (
    CATEGORIES,
    LoadedDocument,
    LoaderError,
    load_file,
    split_front_matter,
)

logger = get_logger(__name__)

ALLOWED_SUFFIXES = {".md": "markdown", ".markdown": "markdown", ".txt": "text", ".pdf": "pdf"}
UploadCategory = Literal["policy", "sop", "faq", "supplier_terms", "shop_profile", "supplier_flyer"]


class TooLargeError(AppError):
    status_code = 413
    code = "file_too_large"


class UnsupportedFileError(AppError):
    status_code = 415
    code = "unsupported_file"


@dataclass
class StoredUpload:
    document_id: str
    title: str
    category: str
    shop_id: str | None
    trust: str
    path: str  # relative to UPLOAD_DIR
    size_bytes: int
    already_uploaded: bool


@dataclass
class IngestJob:
    job_id: str
    status: Literal["running", "done", "failed"] = "running"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def _clean_title(title: str) -> str:
    title = re.sub(r"[\x00-\x1f\x7f]", " ", title or "").strip()
    return re.sub(r"\s+", " ", title)[:200]


async def read_limited(file: UploadFile, max_bytes: int) -> bytes:
    """The file's bytes, refusing as soon as it is bigger than ``max_bytes``."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise TooLargeError(
                f"The file is larger than {max_bytes / 1_000_000:.1f} MB (UPLOAD_MAX_MB)."
            )
        chunks.append(chunk)
    return b"".join(chunks)


def sniff(data: bytes, suffix: str) -> str:
    """The text of a .md/.txt file, or "" for a valid PDF. Raises when the bytes do not
    match the file type."""
    if not data.strip():
        raise InvalidRequestError("The file is empty.")
    kind = ALLOWED_SUFFIXES.get(suffix)
    if kind is None:
        shown = suffix or "a file without an extension"
        raise UnsupportedFileError(
            f"Only {', '.join(sorted(ALLOWED_SUFFIXES))} files can be uploaded, not {shown}."
        )
    if kind == "pdf":
        if not data.startswith(b"%PDF-"):
            raise UnsupportedFileError("The file is named .pdf but is not a PDF.")
        return ""
    if b"\x00" in data or data.startswith((b"%PDF-", b"PK\x03\x04", b"MZ", b"\x7fELF")):
        raise UnsupportedFileError(f"The file is named {suffix} but is not plain text.")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UnsupportedFileError("Text files must be UTF-8.") from exc


def upload_id(shop_id: str | None, data: bytes) -> str:
    """``UPL-<shop number>-<content hash>``: the same file uploaded twice by one shop is
    one document; uploaded by two shops, it is two documents that never clash."""
    shop_code = shop_id.rsplit("-", 1)[-1] if shop_id else "ALL"
    return f"UPL-{shop_code}-{hashlib.sha256(data).hexdigest()[:10].upper()}"


def _stored(doc: LoadedDocument, root: Path, path: Path, size: int, again: bool) -> StoredUpload:
    meta = doc.metadata
    return StoredUpload(
        document_id=meta.document_id,
        title=meta.title,
        category=meta.category,
        shop_id=meta.shop_id,
        trust=meta.trust,
        path=path.relative_to(root).as_posix(),
        size_bytes=size,
        already_uploaded=again,
    )


def _existing(folder: Path, document_id: str) -> Path | None:
    for extension in (".md", ".txt", ".pdf"):
        candidate = folder / f"{document_id}{extension}"
        if candidate.exists():
            return candidate
    return None


# Reading a PDF's text is slow, pure-Python work; very long PDFs are refused up front.
MAX_PDF_PAGES = 60
STAGING_PREFIX = ".staging-"
_MOVE_LOCK = threading.Lock()


def _check_pdf_size(path: Path) -> None:
    from pypdf import PdfReader

    try:
        pages = len(PdfReader(path).pages)
    except Exception as exc:  # pypdf raises many types for damaged files
        raise InvalidRequestError("This file cannot be used: it is not a readable PDF.") from exc
    if pages > MAX_PDF_PAGES:
        raise InvalidRequestError(
            f"The PDF has {pages} pages; at most {MAX_PDF_PAGES} can be uploaded. "
            "Split it, or upload the pages that matter."
        )


def sweep_staging(settings: Settings, *, older_than_seconds: float = 3600) -> int:
    """Remove staging folders left behind by a crash (run at startup)."""
    root = backend_path(settings.upload_dir)
    removed = 0
    if not root.is_dir():
        return removed
    for folder in root.glob(f"{STAGING_PREFIX}*"):
        try:
            if folder.is_dir() and time.time() - folder.stat().st_mtime > older_than_seconds:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def _reason(exc: LoaderError) -> str:
    return str(exc).split(": ", 1)[-1]  # without the (temporary) file path


def store_upload(
    *,
    settings: Settings,
    data: bytes,
    filename: str,
    title: str,
    category: str,
    shop_id: str | None,
    trusted: bool,
    today: date,
) -> StoredUpload:
    suffix = Path(filename or "").suffix.lower()
    text = sniff(data, suffix)
    if category not in CATEGORIES:
        raise InvalidRequestError(f"category must be one of {sorted(CATEGORIES)}.")
    title = _clean_title(title) or _clean_title(Path(filename).stem) or "Uploaded document"
    document_id = upload_id(shop_id, data)
    root = backend_path(settings.upload_dir)
    folder = root / (shop_id or "shared")
    folder.mkdir(parents=True, exist_ok=True)

    # Already uploaded (under any extension): report what is stored, unchanged.
    existing = _existing(folder, document_id)
    if existing is not None:
        try:
            doc = load_file(existing, folder)
            return _stored(doc, root, existing, len(data), True)
        except LoaderError:
            logger.warning("upload_replaced_broken_copy", document_id=document_id)

    extension = ".pdf" if suffix == ".pdf" else (".txt" if suffix == ".txt" else ".md")
    trust = "trusted" if trusted else "untrusted"
    meta = {
        "document_id": document_id,
        "title": title,
        # internal: written by the shop; the trust field decides how agents may use it
        "source": "internal",
        "category": category,
        "version": 1,
        "effective_date": today.isoformat(),
        "status": "current",
        "trust": trust,
        **({"shop_id": shop_id} if shop_id else {}),
    }
    meta_yaml = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)
    # Build the files in a hidden folder (ingestion skips hidden folders), check them
    # with the same loader ingestion uses, and only then move them into place.
    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=root))
    try:
        main = staging / f"{document_id}{extension}"
        side = staging / f"{document_id}.meta.yaml"
        if extension == ".pdf":
            side.write_text(meta_yaml, encoding="utf-8")
            main.write_bytes(data)
        else:
            try:
                _, body = split_front_matter(text.replace("\r\n", "\n"))
            except (ValueError, yaml.YAMLError):
                body = text  # not front matter we understand: keep the whole text as the body
            main.write_text(f"---\n{meta_yaml}---\n\n{body.strip()}\n", encoding="utf-8")
        if extension == ".pdf":
            _check_pdf_size(main)
        try:
            doc = load_file(main, staging)
        except LoaderError as exc:
            raise InvalidRequestError(f"This file cannot be used: {_reason(exc)}.") from exc
        target = folder / main.name
        with _MOVE_LOCK:  # two identical uploads at once: the first one stays
            existing = _existing(folder, document_id)
            if existing is not None and existing != target:
                return _stored(load_file(existing, folder), root, existing, len(data), True)
            if target.exists():
                return _stored(load_file(target, folder), root, target, len(data), True)
            if extension == ".pdf":  # metadata first: a PDF never sits there without it
                os.replace(side, folder / side.name)
            os.replace(main, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    logger.info("document_uploaded", document_id=document_id, shop_id=shop_id, trust=trust)
    return _stored(doc, root, target, len(data), False)


# ------------------------------------------------------------------ ingestion jobs


def _relative(message: str, settings: Settings) -> str:
    for folder in (backend_path("."), backend_path(settings.knowledge_base_dir).parent):
        message = message.replace(f"{folder.resolve()}{os.sep}", "")
    return message[:2000]


def jobs(app: FastAPI) -> dict[str, IngestJob]:
    if getattr(app.state, "ingest_jobs", None) is None:
        app.state.ingest_jobs = {}
        app.state.ingest_lock = asyncio.Lock()
    return app.state.ingest_jobs


def start_job(app: FastAPI) -> IngestJob:
    running = [job for job in jobs(app).values() if job.status == "running"]
    if running:
        raise ConflictError(
            f"Ingestion is already running (job {running[0].job_id}).",
            details={"job_id": running[0].job_id},
        )
    job = IngestJob(job_id=str(uuid.uuid4()))
    jobs(app)[job.job_id] = job
    return job


async def run_ingestion(
    app: FastAPI, job: IngestJob, session_factory: "async_sessionmaker[AsyncSession] | None"
) -> None:
    """Ingest the knowledge base and the uploads with the app's own store and embedder."""
    from app.rag.ingestion import ingest_knowledge_base
    from app.services.knowledge_service import shared_retriever

    settings: Settings = app.state.settings
    jobs(app)
    async with app.state.ingest_lock:
        try:
            retriever = await shared_retriever(app)
            report = await ingest_knowledge_base(
                kb_dir=backend_path(settings.knowledge_base_dir),
                upload_dir=backend_path(settings.upload_dir),
                store=retriever.store,
                embedder=retriever.embedder,
                session_factory=session_factory,
            )
            job.summary = {
                "skipped_uploads": report.skipped_uploads,
                "documents": report.documents,
                "chunks": report.chunks,
                "added": report.added,
                "unchanged": report.unchanged,
                "removed": report.removed,
                "database": report.database,
                "seconds": round(report.seconds, 2),
            }
            job.status = "failed" if report.database_failed else "done"
            if report.database_failed:
                job.error = f"The vectors are stored, but the database was not: {report.database}"
        except LoaderError as exc:
            # A knowledge-base file needs fixing (uploads are skipped, not fatal). Paths
            # are shown from backend/, never as full paths on the server.
            logger.warning("ingestion_failed", job_id=job.job_id, error=str(exc))
            job.status, job.error = "failed", _relative(str(exc), settings)
        except AppError as exc:
            job.status, job.error = "failed", exc.message
        except Exception as exc:  # a background task must record its failure, not vanish
            logger.exception("ingestion_failed", job_id=job.job_id)
            job.status = "failed"
            job.error = f"Ingestion failed ({type(exc).__name__}); the server log has details."
        finally:
            job.finished_at = time.time()
            logger.info("ingestion_finished", job_id=job.job_id, status=job.status)
