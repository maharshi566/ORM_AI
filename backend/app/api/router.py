"""Collects every API router under the /api prefix."""

from fastapi import APIRouter

from app.api.routes import health

api_router = APIRouter()
api_router.include_router(health.router)

# Added in later phases (spec section 20):
# Phase 4: POST /chat                       -> routes/chat.py
# Phase 6: POST /agent/run                  -> routes/agent.py
# Phase 6: POST /documents/upload, /ingest  -> routes/documents.py
# Phase 6: GET  /sessions/{session_id}      -> routes/sessions.py
# Phase 6: GET  /workflows/{workflow_id}    -> routes/workflows.py
# Phase 5: POST /approval/{workflow_id}     -> routes/approval.py
# Phase 8: GET  /metrics                    -> routes/metrics.py
