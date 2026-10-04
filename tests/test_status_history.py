import json
import uuid
from datetime import datetime, timedelta

from agents import BusinessAnalysisAgent
from orchestrator import Orchestrator
from schemas import Ticket, TicketStatus as S
from tests.conftest import FakeLLM
from tests.test_business_analysis import GOOD_PLAN


def mk(store, status=S.CREATED):
    return store.create_ticket(Ticket(id=str(uuid.uuid4()), title="t", description="d", status=status))


def path(store, ticket_id):
    return [(h["from_status"], h["to_status"]) for h in store.get_status_history(ticket_id)]


def test_creation_and_transitions_are_logged_in_order(store):
    t = mk(store)
    store.update_ticket_status(t.id, S.DECOMPOSED)
    assert path(store, t.id) == [(None, "created"), ("created", "decomposed")]


def test_same_status_update_logs_nothing(store):
    t = mk(store)
    store.update_ticket_status(t.id, S.CREATED)
    assert len(store.get_status_history(t.id)) == 1


def test_stage_entered_at_tracks_latest_transition(store):
    t = mk(store)
    first = store.get_stage_entered_at(t.id)
    store.update_ticket_status(t.id, S.APPROVED)
    second = store.get_stage_entered_at(t.id)
    assert second >= first
    assert datetime.utcnow() - second < timedelta(seconds=5)
    assert store.get_stage_entered_at("missing") is None


def test_decomposed_children_get_an_initial_entry(store):
    ask = mk(store)
    BusinessAnalysisAgent(store, FakeLLM(json.dumps(GOOD_PLAN))).run(ask)
    kids = store.get_children(ask.id)
    assert kids and all(path(store, k.id) == [(None, "approved")] for k in kids)


def test_failed_decomposition_leaves_no_history(store):
    ask = mk(store)
    BusinessAnalysisAgent(store, FakeLLM('{"features": []}')).run(ask)
    assert len(store.get_status_history(ask.id)) == 1  # only the ask's own creation


def test_escalation_appears_in_history(store):
    orch = Orchestrator(store)
    t = mk(store, S.PLAN_CRITIQUE)
    for _ in range(3):
        orch.handle_gate_decision(store.get_ticket(t.id), True)
        if store.get_ticket(t.id).status is S.SPEC_AUTHORING:
            store.update_ticket_status(t.id, S.PLAN_CRITIQUE)
    assert path(store, t.id)[-1] == ("plan_critique", "needs_human")
