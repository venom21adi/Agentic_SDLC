import asyncio
import json
import uuid

import pytest

from agents import (BusinessAnalysisAgent, CodeCritiqueAgent, ImplementationAgent, PlanCritiqueAgent,
                    PlanningAgent, SpecAuthorAgent)
from agents.code_critique import has_tests
from main import AgenticSDLC
from schemas import CodeArtifact, Severity, TicketStatus as S
from tests.test_implementation import CODE, ws  # noqa: F401  (ws is a fixture)
from tests.test_plan_critique import QUOTE, ScriptedLLM, clean_critique, critique_json, seed

CODE_QUOTE = "return bool(user and password)"
NO_TESTS = {"app/auth.py": CODE["app/auth.py"]}


class FullLLM(ScriptedLLM):
    """Scripted replies for implementation and code-critique prompts; everything else is ScriptedLLM's."""

    def __init__(self, impl=(CODE,), code_critiques=(None,)):
        super().__init__([clean_critique()])
        self.impl, self.code_critiques = list(impl), list(code_critiques)
        self.impl_prompts, self.code_prompts = [], []

    @staticmethod
    def _next(queue):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def invoke(self, prompt):
        if "senior implementation engineer" in prompt:
            self.calls += 1
            self.impl_prompts.append(prompt)
            return self._Msg(json.dumps({"files": self._next(self.impl), "notes": "IMPLEMENTER-NOTES-XYZ"}))
        if "independent code reviewer" in prompt:
            self.calls += 1
            self.code_prompts.append(prompt)
            reply = self._next(self.code_critiques)
            return self._Msg(json.dumps(reply if reply is not None else clean_critique()))
        return super().invoke(prompt)


def implemented(store, ws, files=CODE, llm=None):
    """A ticket with a real committed artifact, ready for code critique."""
    t, _ = seed(store, S.PLAN_APPROVED)
    ImplementationAgent(store, llm or FullLLM(impl=(files,)), ws).run(t)
    store.update_ticket_status(t.id, S.CODE_CRITIQUE)
    return store.get_ticket(t.id), store.get_code_artifact_for_ticket(t.id)


def critic(store, ws, *critiques):
    llm = FullLLM(code_critiques=critiques or (None,))
    return CodeCritiqueAgent(store, llm, ws), llm


# ---- review behaviour ----

def test_stores_critique_against_the_artifact(store, ws):
    t, art = implemented(store, ws)
    agent, llm = critic(store, ws, critique_json("major", evidence=CODE_QUOTE))
    r = agent.run(t)
    assert r["has_blockers"] is False and r["majors"] == 1
    c = store.get_latest_code_critique(t.id)
    assert c.artifact_id == art.id and c.items[0].severity is Severity.MAJOR and len(c.pass_justifications) == 3


def test_blocker_flag_and_quote_can_come_from_code_or_spec(store, ws):
    t, _ = implemented(store, ws)
    for evidence in (CODE_QUOTE, QUOTE):
        agent, _ = critic(store, ws, critique_json("blocker", evidence=evidence))
        assert agent.run(t)["has_blockers"] is True


def test_clean_pass_records_justifications_for_all_pillars(store, ws):
    t, _ = implemented(store, ws)
    agent, _ = critic(store, ws)
    assert agent.run(t)["has_blockers"] is False
    assert len(store.get_latest_code_critique(t.id).pass_justifications) == 4


def test_fabricated_quote_rejected_and_nothing_stored(store, ws):
    t, art = implemented(store, ws)
    agent, _ = critic(store, ws, critique_json("blocker", evidence="eval(request.body) is called on input"))
    assert "error" in agent.run(t)
    assert store.get_latest_code_critique(t.id) is None


def test_critic_sees_committed_code_and_spec_but_no_generator_notes_or_old_critiques(store, ws):
    t, _ = implemented(store, ws)
    agent, llm = critic(store, ws, critique_json("blocker", evidence=CODE_QUOTE), None)
    agent.run(t)
    agent.run(t)
    first, second = llm.code_prompts
    assert "def login(user, password)" in second and "password_hash" in second  # code + spec
    assert "IMPLEMENTER-NOTES-XYZ" not in second
    assert "No rate limiting on login" not in second  # earlier finding must not leak into the next pass
    assert first == second


def test_only_the_latest_artifact_is_reviewed(store, ws):
    llm = FullLLM(impl=(CODE, {**CODE, "app/auth.py": "def login(u, p):\n    return 'v2-marker'\n"}))
    t, first = implemented(store, ws, llm=llm)
    store.update_ticket_status(t.id, S.IMPLEMENTATION)
    ImplementationAgent(store, llm, ws).run(store.get_ticket(t.id))
    latest = store.get_code_artifact_for_ticket(t.id)
    assert latest.id != first.id
    agent, cllm = critic(store, ws)
    agent.run(store.get_ticket(t.id))
    assert store.get_latest_code_critique(t.id).artifact_id == latest.id
    assert "v2-marker" in cllm.code_prompts[0] and "return bool(user and password)" not in cllm.code_prompts[0]


def test_requires_artifact_and_spec(store, ws):
    t, _ = seed(store, S.CODE_CRITIQUE)  # has plan + spec but no artifact
    agent, _ = critic(store, ws)
    assert "error" in agent.run(t)


# ---- artifact verification ----

def _artifact(store, t, commit, files):
    store.create_code_artifact(CodeArtifact(
        id=str(uuid.uuid4()), ticket_id=t.id, branch_name="x", files_changed=files, commit_hash=commit))


@pytest.mark.parametrize("tamper", [
    lambda files: {**files, "app/auth.py": files["app/auth.py"] + "# edited after commit\n"},
    lambda files: {**files, "extra.py": "x = 1\n"},
    lambda files: {k: v for k, v in files.items() if k != "tests/test_auth.py"},
], ids=["content-differs", "recorded-extra-file", "recorded-missing-file"])
def test_artifact_that_differs_from_its_commit_is_rejected(store, ws, tamper):
    t, art = implemented(store, ws)
    _artifact(store, t, art.commit_hash, tamper(art.files_changed))
    agent, llm = critic(store, ws)
    r = agent.run(t)
    assert "error" in r and "verification failed" in r["error"]
    assert llm.code_prompts == [] and store.get_latest_code_critique(t.id) is None  # model never consulted


def test_unknown_commit_is_rejected(store, ws):
    t, art = implemented(store, ws)
    _artifact(store, t, "deadbeef" * 5, art.files_changed)
    agent, _ = critic(store, ws)
    assert "verification failed" in agent.run(t)["error"]


# ---- mechanical no-tests floor ----

def test_missing_tests_add_a_major_finding_even_if_the_model_says_clean(store, ws):
    t, _ = implemented(store, ws, NO_TESTS)
    agent, _ = critic(store, ws)
    r = agent.run(t)
    assert r["majors"] == 1 and r["has_blockers"] is False
    c = store.get_latest_code_critique(t.id)
    (item,) = c.items
    assert item.pillar.value == "maintainability" and "No automated tests" in item.finding
    assert "maintainability" not in c.pass_justifications  # can't be "clean" and flagged at once


def test_tests_present_adds_nothing(store, ws):
    t, _ = implemented(store, ws)
    agent, _ = critic(store, ws)
    assert agent.run(t)["majors"] == 0


@pytest.mark.parametrize("path,expected", [
    ("tests/test_auth.py", True), ("test/foo.py", True), ("app/test_auth.py", True),
    ("src/auth_test.go", True), ("web/Button.test.tsx", True), ("web/Button.spec.ts", True),
    ("__tests__/a.js", True), ("app/auth.py", False), ("app/latest.py", False), ("docs/contest.md", False),
])
def test_has_tests(path, expected):
    assert has_tests([path]) is expected


# ---- the gate loop through the pipeline ----

def build(store, llm, ws):
    return AgenticSDLC(store, {
        "business_analysis": BusinessAnalysisAgent(store, llm),
        "planning": PlanningAgent(store, llm),
        "spec_author": SpecAuthorAgent(store, llm),
        "plan_critique": PlanCritiqueAgent(store, llm),
        "implementation": ImplementationAgent(store, llm, ws),
        "code_critique": CodeCritiqueAgent(store, llm, ws),
    })


def run(store, llm, ws, iterations=60):
    sdlc = build(store, llm, ws)
    ask = sdlc.create_ticket("auth", "build login")
    asyncio.run(sdlc.run_pipeline(max_iterations=iterations))
    (child,) = store.get_children(ask.id)
    return store.get_ticket(child.id)


def v(n):
    return {**CODE, "app/auth.py": f"def login(user, password):\n    return {n}\n"}


def test_clean_code_critique_advances_to_qa(store, ws):
    t = run(store, FullLLM(), ws)
    assert t.status is S.QA_STRATEGY  # no QA agent yet, so it waits here
    assert len(store.get_code_artifact_for_ticket(t.id).files_changed) == 2


def test_blocker_sends_back_to_implementation_with_findings_then_passes(store, ws):
    llm = FullLLM(impl=(v(1), v(2)), code_critiques=(critique_json("blocker", evidence=QUOTE), None))
    t = run(store, llm, ws)
    # The one blocker still counts: the counter spans the whole build region and only resets on leaving it.
    assert t.status is S.QA_STRATEGY and t.retry_count == 1
    assert len(llm.impl_prompts) == 2 and len(llm.code_prompts) == 2
    assert "REVIEW FINDINGS TO RESOLVE" in llm.impl_prompts[1] and "No rate limiting on login" in llm.impl_prompts[1]
    assert "return 1" in llm.impl_prompts[1]                       # revision starts from the previous code
    assert "return 2" in llm.code_prompts[1] and "return 1" not in llm.code_prompts[1]  # critic saw the new code
    assert store.get_latest_code_critique(t.id).artifact_id == store.get_code_artifact_for_ticket(t.id).id


def test_endless_blockers_escalate_after_bound(store, ws):
    llm = FullLLM(impl=(v(1), v(2), v(3)), code_critiques=(critique_json("blocker", evidence=QUOTE),))
    t = run(store, llm, ws)
    assert t.status is S.NEEDS_HUMAN and t.blocked_from is S.CODE_CRITIQUE
    assert len(llm.impl_prompts) == 3 and len(llm.code_prompts) == 3  # exactly the retry bound


def test_majors_and_nitpicks_do_not_block(store, ws):
    for sev in ("major", "nitpick"):
        t = run(store, FullLLM(code_critiques=(critique_json(sev, evidence=QUOTE),)), ws)
        assert t.status is S.QA_STRATEGY
