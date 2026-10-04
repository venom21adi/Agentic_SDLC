import asyncio
import copy
import json
import uuid

import pytest

from agents import BusinessAnalysisAgent, PlanningAgent, SpecAuthorAgent
from main import AgenticSDLC
from schemas import Ticket, TicketStatus as S
from tests.conftest import FakeLLM

GOOD_PLAN = {
    "features": [
        {"name": "Auth", "description": "login", "tickets": [
            {"key": "schema", "title": "User table", "description": "d1"},
            {"key": "login", "title": "Login API", "description": "d2"}]},
        {"name": "Email", "description": "verify", "tickets": [
            {"key": "verify", "title": "Verify email", "description": "d3"}]},
    ],
    "dependencies": [
        {"from": "schema", "to": "login", "reasoning": "needs table"},
        {"from": "schema", "to": "verify", "reasoning": "needs table"},
    ],
    "open_questions": ["which OAuth providers?"],
}


def mk_ask(store):
    return store.create_ticket(Ticket(id=str(uuid.uuid4()), title="ask", description="build auth"))


def ba(store, plan):
    return BusinessAnalysisAgent(store, FakeLLM(json.dumps(plan)))


def test_creates_features_tickets_and_edges(store):
    ask = mk_ask(store)
    result = ba(store, GOOD_PLAN).run(ask)
    assert result["tickets"] == 3 and result["edges"] == 2

    kids = {t.title: t for t in store.get_children(ask.id)}
    assert set(kids) == {"User table", "Login API", "Verify email"}
    assert all(t.status is S.APPROVED and t.parent_id == ask.id and t.feature_id for t in kids.values())

    feats = {f.name: f for f in store.get_features_for_ask(ask.id)}
    assert set(feats["Auth"].ticket_ids) == {kids["User table"].id, kids["Login API"].id}
    assert kids["Verify email"].feature_id == feats["Email"].id
    assert store.get_dag_predecessors(kids["Login API"].id) == [kids["User table"].id]


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(features=[]),
    lambda p: p["features"][1]["tickets"][0].update(key="schema"),  # duplicate key
    lambda p: p["dependencies"].append({"from": "login", "to": "ghost", "reasoning": "r"}),
    lambda p: p["dependencies"].append({"from": "login", "to": "login", "reasoning": "r"}),
    lambda p: p["dependencies"].append({"from": "login", "to": "schema", "reasoning": "r"}),  # cycle
    lambda p: p["features"][0]["tickets"][0].pop("title"),
], ids=["no-features", "dup-key", "unknown-ref", "self-dep", "cycle", "missing-title"])
def test_invalid_plan_is_error_and_writes_nothing(store, mutate):
    plan = copy.deepcopy(GOOD_PLAN)
    mutate(plan)
    ask = mk_ask(store)
    assert "error" in ba(store, plan).run(ask)
    assert store.get_children(ask.id) == []
    assert store.get_features_for_ask(ask.id) == []
    assert len(store.get_all_tickets()) == 1


def test_non_json_reply_is_error(store):
    assert "error" in BusinessAnalysisAgent(store, FakeLLM("sorry, no")).run(mk_ask(store))


def test_refuses_to_decompose_twice(store):
    ask = mk_ask(store)
    agent = ba(store, GOOD_PLAN)
    assert "error" not in agent.run(ask)
    assert "error" in agent.run(ask)
    assert len(store.get_children(ask.id)) == 3


class RoutingLLM(FakeLLM):
    """BA prompt gets the decomposition; planning/spec prompts get generic JSON."""

    def invoke(self, prompt):
        self.calls += 1
        return self._Msg(json.dumps(GOOD_PLAN) if "Ask title" in prompt else '{"assumptions": "a"}')


def test_pipeline_decomposes_then_plans_only_unblocked_children(store):
    llm = RoutingLLM()
    sdlc = AgenticSDLC(store, {
        "business_analysis": BusinessAnalysisAgent(store, llm),
        "planning": PlanningAgent(store, llm),
        "spec_author": SpecAuthorAgent(store, llm),
    })
    ask = sdlc.create_ticket("auth", "build auth")
    asyncio.run(sdlc.run_pipeline(max_iterations=30))

    assert store.get_ticket(ask.id).status is S.DECOMPOSED
    by_title = {t.title: t for t in store.get_children(ask.id)}
    # "User table" has no predecessors, so it runs up to the (missing) plan critique gate;
    # its dependents wait because nothing has merged.
    assert by_title["User table"].status is S.PLAN_CRITIQUE
    assert by_title["Login API"].status is S.APPROVED
    assert by_title["Verify email"].status is S.APPROVED
