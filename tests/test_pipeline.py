import asyncio
import uuid

import pytest

from agents import BusinessAnalysisAgent, PlanningAgent, SpecAuthorAgent
from main import AgenticSDLC
from orchestrator import Orchestrator
from schemas import DAGEdge, HumanDecision, Ticket, TicketStatus as S
from tests.conftest import FakeLLM


def mk(store, status=S.CREATED, title="t"):
    return store.create_ticket(Ticket(id=str(uuid.uuid4()), title=title, description="d", status=status))


def test_store_roundtrip_and_status_enum(store):
    t = mk(store)
    assert store.get_ticket(t.id).status is S.CREATED
    store.update_ticket_status(t.id, S.APPROVED)
    assert [x.id for x in store.get_tickets_by_status(S.APPROVED)] == [t.id]


def test_retry_count_persists(store):
    t = mk(store)
    assert store.increment_retry_count(t.id) == 1
    assert store.get_ticket(t.id).retry_count == 1
    store.reset_retry_count(t.id)
    assert store.get_ticket(t.id).retry_count == 0


def test_dependency_blocks_any_stage(store):
    orch = Orchestrator(store)
    a, b = mk(store), mk(store, status=S.APPROVED)
    store.add_dag_edge(DAGEdge(source_ticket_id=a.id, target_ticket_id=b.id, reasoning="r"))
    assert [t.id for t in orch.list_ready_tickets()] == [a.id]
    store.update_ticket_status(a.id, S.MERGED)
    assert [t.id for t in orch.list_ready_tickets()] == [b.id]


def test_gate_blocker_sends_back_then_escalates(store):
    orch = Orchestrator(store)
    t = mk(store, S.PLAN_CRITIQUE)
    for expected in (S.SPEC_AUTHORING, S.SPEC_AUTHORING):
        orch.handle_gate_decision(store.get_ticket(t.id), True)
        assert store.get_ticket(t.id).status is expected
        store.update_ticket_status(t.id, S.PLAN_CRITIQUE)  # pretend spec re-authored
    orch.handle_gate_decision(store.get_ticket(t.id), True)  # 3rd blocker hits bound
    got = store.get_ticket(t.id)
    assert got.status is S.NEEDS_HUMAN and got.blocked_from is S.PLAN_CRITIQUE


def test_gate_pass_advances_and_resets_retries_only_when_leaving_a_retry_region(store):
    orch = Orchestrator(store)
    leaving = mk(store, S.PLAN_CRITIQUE)          # plan_critique -> plan_approved leaves the "plan" region
    store.increment_retry_count(leaving.id)
    orch.handle_gate_decision(store.get_ticket(leaving.id), False)
    got = store.get_ticket(leaving.id)
    assert got.status is S.PLAN_APPROVED and got.retry_count == 0

    inside = mk(store, S.CODE_CRITIQUE)           # code_critique -> qa_strategy stays inside the "build" region
    store.increment_retry_count(inside.id)
    orch.handle_gate_decision(store.get_ticket(inside.id), False)
    got = store.get_ticket(inside.id)
    assert got.status is S.QA_STRATEGY and got.retry_count == 1


def _escalated(store):
    orch = Orchestrator(store)
    t = mk(store, S.PLAN_CRITIQUE)
    for _ in range(3):
        cur = store.get_ticket(t.id)
        orch.handle_gate_decision(cur, True)
        if store.get_ticket(t.id).status is S.SPEC_AUTHORING:
            store.update_ticket_status(t.id, S.PLAN_CRITIQUE)
    return orch, t


@pytest.mark.parametrize("decision,expected", [
    ("force_advance", S.PLAN_APPROVED),
    ("resume_with_guidance", S.SPEC_AUTHORING),
    ("reject", S.SPEC_AUTHORING),
])
def test_human_decisions(store, decision, expected):
    orch, t = _escalated(store)
    orch.handle_human_decision(HumanDecision(
        id=str(uuid.uuid4()), ticket_id=t.id, stage="plan_critique",
        decision_type=decision, reasoning="because", created_by="me"))
    got = store.get_ticket(t.id)
    assert got.status is expected and got.retry_count == 0 and got.blocked_from is None


def test_human_decision_rejected_when_not_escalated(store):
    orch = Orchestrator(store)
    t = mk(store)
    with pytest.raises(ValueError):
        orch.handle_human_decision(HumanDecision(
            id="d", ticket_id=t.id, stage="x", decision_type="approve", reasoning="r", created_by="me"))


def make_sdlc(store, llm):
    return AgenticSDLC(store, {
        "business_analysis": BusinessAnalysisAgent(store, llm),
        "planning": PlanningAgent(store, llm),
        "spec_author": SpecAuthorAgent(store, llm),
    })


def test_failing_agent_is_bounded_and_escalates(store):
    class Boom(FakeLLM):
        def invoke(self, prompt):
            raise RuntimeError("api down")
    sdlc = make_sdlc(store, Boom())
    t = sdlc.create_ticket("x", "y")
    asyncio.run(sdlc.run_pipeline(max_iterations=20))
    got = store.get_ticket(t.id)
    assert got.status is S.NEEDS_HUMAN and got.blocked_from is S.CREATED
