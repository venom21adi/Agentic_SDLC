"""Command Center HTTP API.

Run:  uvicorn api:create_app --factory --reload
"""
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from command_center import CommandCenter
from config import Config
from orchestrator import Orchestrator
from schemas import HumanDecision
from state_store import StateStore


UI_FILE = Path(__file__).resolve().parent / "ui" / "index.html"


class DecisionRequest(BaseModel):
    decision_type: str  # approve | reject | force_advance | resume_with_guidance
    reasoning: str
    created_by: str


def create_app(store: Optional[StateStore] = None) -> FastAPI:
    store = store or StateStore(Config.DATABASE_URL)
    cc = CommandCenter(store)
    orchestrator = Orchestrator(store)
    app = FastAPI(title="Agentic SDLC Command Center")

    def found(view):
        if view is None:
            raise HTTPException(404, "not found")
        return view

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(UI_FILE, media_type="text/html")

    @app.get("/api/asks")
    def list_asks():
        return cc.list_asks()

    @app.get("/api/asks/{ask_id}")
    def get_ask(ask_id: str):
        return found(cc.ask_view(ask_id))

    @app.get("/api/features/{feature_id}")
    def get_feature(feature_id: str):
        return found(cc.feature_view(feature_id))

    @app.get("/api/tickets/{ticket_id}")
    def get_ticket(ticket_id: str):
        return found(cc.ticket_view(ticket_id))

    @app.get("/api/stuck")
    def stuck():
        return cc.stuck_tickets()

    @app.get("/api/decisions")
    def decision_queue():
        return cc.decision_queue()

    @app.post("/api/tickets/{ticket_id}/decision")
    def decide(ticket_id: str, body: DecisionRequest):
        if store.get_ticket(ticket_id) is None:
            raise HTTPException(404, "not found")
        try:
            orchestrator.handle_human_decision(HumanDecision(
                id=str(uuid.uuid4()), ticket_id=ticket_id, stage="",
                decision_type=body.decision_type, reasoning=body.reasoning,
                created_by=body.created_by,
            ))
        except ValueError as e:
            raise HTTPException(409, str(e))
        return cc.ticket_view(ticket_id)

    return app
