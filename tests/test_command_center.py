import json
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from agents import BusinessAnalysisAgent
from api import create_app
from command_center import CommandCenter
from orchestrator import Orchestrator
from schemas import DAGEdge, Ticket, TicketStatus as S
from state_store import StatusHistoryORM
from tests.conftest import FakeLLM
from tests.test_business_analysis import GOOD_PLAN


def decomposed_ask(store):
    ask = store.create_ticket(Ticket(id=str(uuid.uuid4()), title="auth", description="d"))
    BusinessAnalysisAgent(store, FakeLLM(json.dumps(GOOD_PLAN))).run(ask)
    store.update_ticket_status(ask.id, S.DECOMPOSED)
    kids = {t.title: t for t in store.get_children(ask.id)}
    return ask, kids


def age(store, ticket_id, seconds):
    """Backdate a ticket's latest history row so it looks like it has sat in its stage."""
    s = store.get_session()
    row = s.query(StatusHistoryORM).filter_by(ticket_id=ticket_id).order_by(StatusHistoryORM.id.desc()).first()
    row.entered_at = datetime.utcnow() - timedelta(seconds=seconds)
    s.commit()
    s.close()


def escalate(store, ticket_id):
    orch = Orchestrator(store)
    store.update_ticket_status(ticket_id, S.PLAN_CRITIQUE)
    for _ in range(3):
        orch.handle_gate_decision(store.get_ticket(ticket_id), True)
        if store.get_ticket(ticket_id).status is S.SPEC_AUTHORING:
            store.update_ticket_status(ticket_id, S.PLAN_CRITIQUE)


@pytest.fixture
def cc(store):
    return CommandCenter(store, stage_timeout_seconds=60)


def test_ask_rollup_percent_and_status(store, cc):
    ask, kids = decomposed_ask(store)
    v = cc.ask_view(ask.id)
    assert (v["status"], v["total"], v["percent_complete"]) == ("not_started", 3, 0.0)
    assert {f["name"] for f in v["features"]} == {"Auth", "Email"}

    store.update_ticket_status(kids["User table"].id, S.MERGED)
    v = cc.ask_view(ask.id)
    assert (v["status"], v["percent_complete"]) == ("in_progress", 33.3)

    for t in kids.values():
        store.update_ticket_status(t.id, S.MERGED)
    assert cc.ask_view(ask.id)["status"] == "complete"


def test_feature_rollup_counts(store, cc):
    ask, kids = decomposed_ask(store)
    store.update_ticket_status(kids["User table"].id, S.MERGED)
    auth = next(f for f in cc.ask_view(ask.id)["features"] if f["name"] == "Auth")
    assert auth["total"] == 2 and auth["percent_complete"] == 50.0
    assert auth["by_status"] == {"merged": 1, "approved": 1}


def test_undecomposed_ask_is_in_analysis(store, cc):
    ask = store.create_ticket(Ticket(id="a", title="a", description="d"))
    assert cc.ask_view("a")["status"] == "analysis"
    assert [a["id"] for a in cc.list_asks()] == ["a"]


def test_child_ticket_is_not_an_ask(store, cc):
    ask, kids = decomposed_ask(store)
    assert cc.ask_view(kids["Login API"].id) is None
    assert [a["id"] for a in cc.list_asks()] == [ask.id]


def test_stage_timeout_flags_stuck_but_not_when_waiting_on_dependencies(store, cc):
    ask, kids = decomposed_ask(store)
    root, login = kids["User table"], kids["Login API"]
    age(store, root.id, 3600)
    age(store, login.id, 3600)  # old too, but it is waiting on `root`, so not a timeout itself
    assert cc.ticket_view(root.id)["stuck_reasons"] == ["stage_timeout"]
    login_view = cc.ticket_view(login.id)
    assert login_view["stuck_reasons"] == ["blocked_by_stuck_dependency"]
    assert login_view["stuck_dependencies"] == [root.id]
    assert login_view["waiting_on"] == [root.id]


def test_fresh_ticket_is_not_stuck(store, cc):
    ask, kids = decomposed_ask(store)
    assert cc.stuck_tickets() == []


def test_stuck_propagates_transitively(store, cc):
    a, b, c = (store.create_ticket(Ticket(id=x, title=x, description="d", status=S.APPROVED)) for x in "abc")
    store.add_dag_edge(DAGEdge(source_ticket_id="a", target_ticket_id="b", reasoning="r"))
    store.add_dag_edge(DAGEdge(source_ticket_id="b", target_ticket_id="c", reasoning="r"))
    escalate(store, "a")
    assert cc.ticket_view("c")["stuck_reasons"] == ["blocked_by_stuck_dependency"]


def test_decision_queue_lists_escalated_oldest_first(store, cc):
    ask, kids = decomposed_ask(store)
    first, second = kids["User table"], kids["Verify email"]
    escalate(store, second.id)
    escalate(store, first.id)
    age(store, second.id, 500)
    queue = cc.decision_queue()
    assert [q["id"] for q in queue] == [second.id, first.id]
    assert queue[0]["blocked_from"] == "plan_critique" and "needs_human" in queue[0]["stuck_reasons"]


# ---- HTTP ----

@pytest.fixture
def client(store):
    return TestClient(create_app(store))


def test_api_read_endpoints(store, client):
    ask, kids = decomposed_ask(store)
    assert client.get("/api/asks").json()[0]["id"] == ask.id
    body = client.get(f"/api/asks/{ask.id}").json()
    assert body["total"] == 3
    fid = body["features"][0]["id"]
    assert client.get(f"/api/features/{fid}").json()["ask_id"] == ask.id
    t = client.get(f"/api/tickets/{kids['Login API'].id}").json()
    assert t["status"] == "approved" and t["history"][0]["to_status"] == "approved"
    assert client.get("/api/stuck").json() == []
    assert client.get("/api/decisions").json() == []
    for url in ("/api/asks/x", "/api/features/x", "/api/tickets/x"):
        assert client.get(url).status_code == 404


def test_api_decision_flow(store, client):
    ask, kids = decomposed_ask(store)
    tid = kids["User table"].id
    escalate(store, tid)
    assert [d["id"] for d in client.get("/api/decisions").json()] == [tid]

    r = client.post(f"/api/tickets/{tid}/decision",
                    json={"decision_type": "resume_with_guidance", "reasoning": "retry", "created_by": "me"})
    assert r.status_code == 200 and r.json()["status"] == "spec_authoring"
    assert client.get("/api/decisions").json() == []


def test_api_decision_errors_and_no_record_on_invalid(store, client):
    ask, kids = decomposed_ask(store)
    tid = kids["User table"].id
    body = {"decision_type": "approve", "reasoning": "r", "created_by": "me"}
    assert client.post(f"/api/tickets/{tid}/decision", json=body).status_code == 409  # not escalated
    assert client.post("/api/tickets/nope/decision", json=body).status_code == 404
    escalate(store, tid)
    bad = {**body, "decision_type": "shrug"}
    assert client.post(f"/api/tickets/{tid}/decision", json=bad).status_code == 409
    assert store.get_ticket(tid).status is S.NEEDS_HUMAN  # untouched, still queued
    assert client.post(f"/api/tickets/{tid}/decision", json={"reasoning": "x"}).status_code == 422


def test_decision_is_recorded_with_blocked_stage(store, client):
    from state_store import HumanDecisionORM
    ask, kids = decomposed_ask(store)
    tid = kids["User table"].id
    escalate(store, tid)
    client.post(f"/api/tickets/{tid}/decision",
                json={"decision_type": "force_advance", "reasoning": "ok", "created_by": "me"})
    s = store.get_session()
    rows = s.query(HumanDecisionORM).filter_by(ticket_id=tid).all()
    s.close()
    assert [(r.stage, r.decision_type, r.created_by) for r in rows] == [("plan_critique", "force_advance", "me")]


def test_dashboard_page_is_served_and_only_calls_known_endpoints(client):
    import re
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "Command Center" in r.text
    # every API path the page fetches must exist on the server
    routes = {route.path for route in client.app.routes}
    called = set(re.findall(r'"(/api/[a-z]+)', r.text))
    assert called and all(any(p == c or p.startswith(c + "/") for p in routes) for c in called), called
    assert "innerHTML" not in r.text  # API strings must never be injected as markup


def test_ticket_view_has_the_plain_english_context_the_dashboard_needs(store, cc):
    ask, kids = decomposed_ask(store)
    root, login = kids["User table"], kids["Login API"]
    escalate(store, root.id)

    v = cc.ticket_view(root.id)
    assert v["ask_title"] == "auth" and v["feature_name"] == "Auth"
    # what each human decision would do, straight from the orchestrator's tables
    assert v["blocked_from"] == "plan_critique"
    assert v["accept_goes_to"] == "plan_approved" and v["send_back_goes_to"] == "spec_authoring"

    w = cc.ticket_view(login.id)
    assert w["waiting_on_titles"] == ["User table"]
    assert w["stuck_dependency_titles"] == ["User table"]  # the escalated root is what blocks it
    assert w["accept_goes_to"] is None and w["send_back_goes_to"] is None  # not escalated: nothing to decide


def test_escalation_outside_a_gate_retries_the_same_step(store, cc):
    t = store.create_ticket(Ticket(id="t", title="t", description="d", status=S.APPROVED))
    store.set_blocked_from("t", S.APPROVED)
    store.update_ticket_status("t", S.NEEDS_HUMAN)
    v = cc.ticket_view("t")
    assert v["send_back_goes_to"] == "approved" and v["accept_goes_to"] == "spec_authoring"
