import asyncio
import copy
import json
import uuid

import pytest

from agents import BusinessAnalysisAgent, PlanCritiqueAgent, PlanningAgent, SpecAuthorAgent
from agents.plan_critique import CritiqueValidationError, normalize, parse_critique
from main import AgenticSDLC
from schemas import Plan, Severity, Specification, Ticket, TicketStatus as S
from tests.conftest import FakeLLM

ONE_TICKET = {"features": [{"name": "Auth", "description": "d", "tickets": [
    {"key": "t1", "title": "User table", "description": "store users"}]}], "dependencies": []}

PLAN = {"approach": "Use a single users table with bcrypt hashed passwords"}
ASSUMPTIONS = "Traffic stays under one thousand requests per second"
SPEC = {"data_models": "users table stores email and password_hash columns",
        "security": "login endpoint accepts unlimited attempts"}

QUOTE = "login endpoint accepts unlimited attempts"
JUSTIFIED = "Plan uses bcrypt hashed passwords and a single users table, which is adequate here."


def critique_json(severity="blocker", evidence=QUOTE):
    clean = {"findings": [], "pass_justification": JUSTIFIED}
    return {"pillars": {
        "performance": clean, "scalability": clean, "maintainability": clean,
        "security": {"findings": [{
            "severity": severity, "finding": "No rate limiting on login allows brute force",
            "reference": "spec.security", "evidence": evidence}], "pass_justification": ""},
    }}


def clean_critique():
    clean = {"findings": [], "pass_justification": JUSTIFIED}
    return {"pillars": {p: clean for p in ("performance", "security", "scalability", "maintainability")}}


class ScriptedLLM(FakeLLM):
    """Answers by prompt type; `critiques` is a list of replies consumed in order (last one repeats)."""

    def __init__(self, critiques):
        super().__init__()
        self.critiques = list(critiques)
        self.prompts = {"critique": [], "spec": []}

    def invoke(self, prompt):
        self.calls += 1
        if "Ask title" in prompt:
            return self._Msg(json.dumps(ONE_TICKET))
        if "independent reviewer" in prompt:
            self.prompts["critique"].append(prompt)
            reply = self.critiques.pop(0) if len(self.critiques) > 1 else self.critiques[0]
            return self._Msg(json.dumps(reply))
        if "technical specification" in prompt:
            self.prompts["spec"].append(prompt)
            return self._Msg(json.dumps(SPEC))
        return self._Msg(json.dumps({**PLAN, "assumptions": ASSUMPTIONS}))


def seed(store, status=S.PLAN_CRITIQUE):
    t = store.create_ticket(Ticket(id=str(uuid.uuid4()), title="auth", description="build login", status=status))
    plan = store.create_plan(Plan(id=str(uuid.uuid4()), ticket_id=t.id, content=PLAN, assumptions=ASSUMPTIONS))
    spec = store.create_specification(Specification(id=str(uuid.uuid4()), ticket_id=t.id, plan_id=plan.id, content=SPEC))
    return t, spec


# ---- validation rules ----

CORPUS = json.dumps(SPEC) + " " + ASSUMPTIONS


def test_valid_critique_parses():
    items, why = parse_critique(critique_json(), CORPUS)
    assert [(i.pillar.value, i.severity) for i in items] == [("security", Severity.BLOCKER)]
    assert items[0].reference == "spec.security" and set(why) == {"performance", "scalability", "maintainability"}


def test_evidence_matching_tolerates_case_whitespace_and_punctuation():
    ev = "Login   endpoint ACCEPTS unlimited attempts!"
    items, _ = parse_critique(critique_json(evidence=ev), CORPUS)
    assert len(items) == 1


@pytest.mark.parametrize("mutate,expected", [
    (lambda p: p["security"]["findings"][0].update(evidence="the system is slow under heavy load"), "not a quote"),
    (lambda p: p["security"]["findings"][0].update(evidence="users"), "too short"),
    (lambda p: p["security"]["findings"][0].update(evidence=""), "too short"),
    (lambda p: p["security"]["findings"][0].update(reference=""), "no reference"),
    (lambda p: p["security"]["findings"][0].update(severity="critical"), "invalid severity"),
    (lambda p: p["security"]["findings"][0].update(finding="bad"), "too short"),
    (lambda p: p.pop("performance"), "performance: missing"),
    (lambda p: p["performance"].update(pass_justification="Looks fine"), "no real pass_justification"),
    (lambda p: p["performance"].pop("pass_justification"), "no real pass_justification"),
], ids=["fabricated-quote", "tiny-quote", "empty-quote", "no-ref", "bad-severity", "tiny-finding",
        "missing-pillar", "lazy-pass", "no-pass"])
def test_integrity_rules_reject(mutate, expected):
    data = critique_json()
    mutate(data["pillars"])
    with pytest.raises(CritiqueValidationError, match=expected):
        parse_critique(data, CORPUS)


def test_all_problems_are_reported_together():
    data = critique_json(evidence="invented text that is nowhere in the artifact")
    data["pillars"].pop("scalability")
    with pytest.raises(CritiqueValidationError) as e:
        parse_critique(data, CORPUS)
    assert "not a quote" in str(e.value) and "scalability: missing" in str(e.value)


def test_normalize():
    assert normalize("  Foo,\nBAR_baz!! ") == "foo bar baz"


# ---- agent ----

def test_agent_stores_critique(store):
    t, spec = seed(store)
    r = PlanCritiqueAgent(store, ScriptedLLM([critique_json("major")])).run(t)
    assert r["has_blockers"] is False and r["majors"] == 1 and r["blockers"] == 0
    c = store.get_latest_plan_critique(t.id)
    assert c.spec_id == spec.id and c.items[0].severity is Severity.MAJOR and len(c.pass_justifications) == 3


def test_agent_blocker_flag(store):
    t, _ = seed(store)
    assert PlanCritiqueAgent(store, ScriptedLLM([critique_json("blocker")])).run(t)["has_blockers"] is True


def test_agent_clean_pass_stores_justifications(store):
    t, _ = seed(store)
    r = PlanCritiqueAgent(store, ScriptedLLM([clean_critique()])).run(t)
    assert r["has_blockers"] is False
    assert len(store.get_latest_plan_critique(t.id).pass_justifications) == 4


def test_agent_rejects_invalid_output_and_writes_nothing(store):
    t, _ = seed(store)
    r = PlanCritiqueAgent(store, ScriptedLLM([critique_json(evidence="fabricated nonsense quote here")])).run(t)
    assert "error" in r and store.get_plan_critiques(t.id) == []


def test_agent_needs_plan_and_spec(store):
    t = store.create_ticket(Ticket(id="x", title="t", description="d", status=S.PLAN_CRITIQUE))
    assert "error" in PlanCritiqueAgent(store, ScriptedLLM([clean_critique()])).run(t)


def test_critic_prompt_has_artifact_but_not_prior_critiques(store):
    t, _ = seed(store)
    llm = ScriptedLLM([critique_json("blocker"), clean_critique()])
    agent = PlanCritiqueAgent(store, llm)
    agent.run(t)
    agent.run(t)
    first, second = llm.prompts["critique"]
    assert "password_hash" in second and "bcrypt" in second and ASSUMPTIONS in second
    assert "No rate limiting on login" not in second  # earlier finding must not leak into the next pass
    assert first == second


def test_latest_spec_wins(store):
    t, old = seed(store)
    new = store.create_specification(Specification(
        id=str(uuid.uuid4()), ticket_id=t.id, plan_id=old.plan_id, content={"data_models": "v2"}))
    assert store.get_specification_for_ticket(t.id).id == new.id


# ---- the gate loop through the whole pipeline ----

def build(store, llm):
    return AgenticSDLC(store, {
        "business_analysis": BusinessAnalysisAgent(store, llm),
        "planning": PlanningAgent(store, llm),
        "spec_author": SpecAuthorAgent(store, llm),
        "plan_critique": PlanCritiqueAgent(store, llm),
    })


def run(store, llm, iterations=40):
    sdlc = build(store, llm)
    ask = sdlc.create_ticket("auth", "build login")
    asyncio.run(sdlc.run_pipeline(max_iterations=iterations))
    (child,) = store.get_children(ask.id)
    return store.get_ticket(child.id)


def test_clean_critique_approves_plan(store):
    t = run(store, ScriptedLLM([clean_critique()]))
    assert t.status is S.PLAN_APPROVED  # no implementation agent yet, so it waits here


def test_blocker_loops_back_to_spec_author_then_passes(store):
    llm = ScriptedLLM([critique_json("blocker"), clean_critique()])
    t = run(store, llm)
    assert t.status is S.PLAN_APPROVED and t.retry_count == 0
    assert len(store.get_plan_critiques(t.id)) == 2
    # the re-authored spec was shown the blocking finding, and the first spec prompt had no feedback
    first, second = llm.prompts["spec"]
    assert "previous version of this spec was rejected" not in first
    assert "No rate limiting on login" in second and QUOTE in second
    # critique 2 reviewed the new spec, not the old one
    c1, c2 = store.get_plan_critiques(t.id)
    assert c1.spec_id != c2.spec_id == store.get_specification_for_ticket(t.id).id


def test_endless_blockers_escalate_after_bound_instead_of_looping(store):
    llm = ScriptedLLM([critique_json("blocker")])
    t = run(store, llm, iterations=60)
    assert t.status is S.NEEDS_HUMAN and t.blocked_from is S.PLAN_CRITIQUE
    assert len(store.get_plan_critiques(t.id)) == 3  # exactly the retry bound
    assert len(llm.prompts["spec"]) == 3  # authored once, then re-authored twice


def test_major_and_nitpick_findings_do_not_block(store):
    t = run(store, ScriptedLLM([critique_json("major")]))
    assert t.status is S.PLAN_APPROVED
    t2 = run(store, ScriptedLLM([critique_json("nitpick")]))
    assert t2.status is S.PLAN_APPROVED


def test_retries_accumulate_across_loops(store):
    from orchestrator import Orchestrator
    t, _ = seed(store)
    orch = Orchestrator(store)
    orch.handle_gate_decision(store.get_ticket(t.id), True)         # blocker -> spec_authoring, retry 1
    assert store.get_ticket(t.id).retry_count == 1
    orch.advance(store.get_ticket(t.id))                            # spec re-authored -> plan_critique
    assert store.get_ticket(t.id).retry_count == 1                  # must NOT reset inside the loop
    orch.handle_gate_decision(store.get_ticket(t.id), False)        # gate passes -> plan_approved
    assert store.get_ticket(t.id).retry_count == 0


def test_corrupt_critique_replies_are_bounded_too(store):
    t = run(store, FakeLLMWithBadCritic())
    assert t.status is S.NEEDS_HUMAN and t.blocked_from is S.PLAN_CRITIQUE


class FakeLLMWithBadCritic(ScriptedLLM):
    def __init__(self):
        super().__init__([{"pillars": {}}])
