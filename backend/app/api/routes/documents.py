"""Add documents to the knowledge base, and re-ingest it (Phase 6).

``POST /api/documents/upload`` (multipart form): ``file`` (.md, .txt or .pdf, at most
``UPLOAD_MAX_MB``), ``title``, ``category`` (policy, sop, faq, supplier_terms,
shop_profile, supplier_flyer), ``shop_id`` (when not logged in), ``trusted`` (owners
only) and ``ingest`` (default true: start ingestion right away). The checks are in
app/services/documents_service.py.

``POST /api/documents/ingest`` starts ingestion of the knowledge base plus all
uploads as a background task and answers 202 at once with a ``job_id``;
``GET /api/documents/ingest/{job_id}`` shows how it went, including uploads that were
skipped because they cannot be used. Only one ingestion runs at a time. With
``AUTH_REQUIRED=true`` all three need a login.
"""

import asyncio
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Path, Request, UploadFile

from app.core.auth import CurrentUser, Principal
from app.core.exceptions import ForbiddenError, InvalidRequestError, NotFoundError
from app.core.rate_limit import rate_limit
from app.models import Shop
from app.models.schemas import IngestJobResponse, UploadResponse
from app.services import documents_service
from app.tools.base import business_now

router = APIRouter(prefix="/documents", tags=["documents"])


def _job_view(job: documents_service.IngestJob, principal: Principal | None) -> IngestJobResponse:
    def iso(stamp: float | None) -> str | None:
        return datetime.fromtimestamp(stamp, UTC).isoformat() if stamp else None

    summary = dict(job.summary)
    if principal is not None and principal.role != "admin" and "skipped_uploads" in summary:
        # Only the caller's own shop's (and shared) uploads, not other shops' file names.
        visible = (f"uploads/{principal.shop_id}/", "uploads/shared/")
        summary["skipped_uploads"] = [
            line for line in summary["skipped_uploads"] if line.startswith(visible)
        ]
    return IngestJobResponse(
        job_id=job.job_id,
        status=job.status,
        started_at=iso(job.started_at) or "",
        finished_at=iso(job.finished_at),
        summary=summary,
        error=job.error,
    )


def _start(request: Request, background: BackgroundTasks) -> documents_service.IngestJob:
    from app.api.identity import session_factory_for

    job = documents_service.start_job(request.app)
    background.add_task(
        documents_service.run_ingestion, request.app, job, session_factory_for(request)
    )
    return job


@router.post(
    "/upload",
    response_model=UploadResponse,
    status_code=201,
    summary="Upload a document (.md, .txt or .pdf) to the knowledge base",
)
async def upload(
    request: Request,
    background: BackgroundTasks,
    principal: CurrentUser,
    _: Annotated[None, Depends(rate_limit("upload"))],
    file: Annotated[UploadFile, File(description=".md, .txt or .pdf")],
    title: Annotated[str, Form(max_length=200)] = "",
    category: Annotated[documents_service.UploadCategory, Form()] = "sop",
    shop_id: Annotated[str | None, Form(pattern=r"^SHOP-\d{3}$")] = None,
    trusted: Annotated[bool, Form(description="Owners only: agents may rely on it")] = False,
    ingest: Annotated[bool, Form(description="Start ingestion now")] = True,
) -> UploadResponse:
    from app.api.identity import session_factory_for

    settings = request.app.state.settings
    notes: list[str] = []
    if principal is not None and principal.role != "admin":
        if shop_id and shop_id != principal.shop_id:
            raise ForbiddenError(f"You are logged in for {principal.shop_id}, not {shop_id}.")
        shop_id = principal.shop_id
    if shop_id is None and (principal is None or principal.role != "admin"):
        raise InvalidRequestError("Say which shop the document is for (shop_id), or log in.")
    if shop_id is not None:
        async with session_factory_for(request)() as db:
            if await db.get(Shop, shop_id) is None:
                raise NotFoundError(f"No shop with ID {shop_id}.")
    if trusted and (principal is None or principal.role not in {"owner", "admin"}):
        trusted = False
        notes.append(
            "Saved as untrusted (reference only): only a logged-in owner can mark a "
            "document as one the agents may rely on."
        )
    data = await documents_service.read_limited(file, int(settings.upload_max_mb * 1_000_000))
    # Reading the file back (PDF text) is blocking work: keep it off the event loop.
    stored = await asyncio.to_thread(
        documents_service.store_upload,
        settings=settings,
        data=data,
        filename=file.filename or "",
        title=title,
        category=category,
        shop_id=shop_id,
        trusted=trusted,
        today=business_now(settings.business_date).date(),
    )
    job_id = None
    if ingest and not stored.already_uploaded:
        try:
            job_id = _start(request, background).job_id
        except Exception as exc:  # a running ingestion picks it up next time
            notes.append(f"Not ingested yet: {getattr(exc, 'message', exc)}")
    return UploadResponse(
        document_id=stored.document_id,
        title=stored.title,
        category=stored.category,
        shop_id=stored.shop_id,
        trust=stored.trust,  # type: ignore[arg-type]
        path=stored.path,
        size_bytes=stored.size_bytes,
        already_uploaded=stored.already_uploaded,
        ingest_job_id=job_id,
        notes=notes,
    )


@router.post(
    "/ingest",
    response_model=IngestJobResponse,
    status_code=202,
    summary="Re-ingest the knowledge base and the uploads (in the background)",
)
async def ingest(
    request: Request,
    background: BackgroundTasks,
    principal: CurrentUser,
    _: Annotated[None, Depends(rate_limit("upload"))],
) -> IngestJobResponse:
    if principal is not None and principal.role not in {"owner", "admin"}:
        raise ForbiddenError("Only an owner can start ingestion.")
    return _job_view(_start(request, background), principal)


@router.get("/ingest/{job_id}", response_model=IngestJobResponse, summary="How an ingestion went")
async def ingest_status(
    request: Request,
    principal: CurrentUser,
    job_id: Annotated[str, Path(max_length=36, pattern=r"^[A-Za-z0-9-]+$")],
) -> IngestJobResponse:
    job = documents_service.jobs(request.app).get(job_id)
    if job is None:
        raise NotFoundError(f"No ingestion job {job_id}.")
    return _job_view(job, principal)
