"""Collects every API router under the /api prefix."""

from fastapi import APIRouter

from app.api.routes import approval, chat, health, knowledge

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(knowledge.router)  # Phase 3: GET /knowledge/search
api_router.include_router(chat.router)  # Phase 4: POST /chat, GET /chat/graph
api_router.include_router(approval.router)  # Phase 5: POST/GET /approval/{workflow_id}

# Added in later phases (spec section 20):
# Phase 6: POST /agent/run                  -> routes/agent.py
# Phase 6: POST /documents/upload, /ingest  -> routes/documents.py
# Phase 6: GET  /sessions/{session_id}      -> routes/sessions.py
# Phase 6: GET  /workflows/{workflow_id}    -> routes/workflows.py
# Phase 8: GET  /metrics                    -> routes/metrics.py
