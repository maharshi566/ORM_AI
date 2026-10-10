"""Collects every API router under the /api prefix."""

from fastapi import APIRouter

from app.api.routes import agent, approval, auth, chat, documents, health, knowledge, records

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(knowledge.router)  # Phase 3: GET /knowledge/search
api_router.include_router(chat.router)  # Phase 4: POST /chat, GET /chat/graph
api_router.include_router(approval.router)  # Phase 5: POST/GET /approval/{workflow_id}
api_router.include_router(auth.router)  # Phase 6: POST /auth/dev-token, GET /auth/me
api_router.include_router(agent.router)  # Phase 6: POST /agent/run
api_router.include_router(documents.router)  # Phase 6: upload, ingest, ingest/{job_id}
api_router.include_router(records.router)  # Phase 6: sessions, workflows, metrics
