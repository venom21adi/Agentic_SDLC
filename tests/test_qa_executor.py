import asyncio
import json
import uuid

import pytest

from agents import (BusinessAnalysisAgent, CodeCritiqueAgent, ImplementationAgent, PlanCritiqueAgent,
                    PlanningAgent, QAStrategyAgent, SpecAuthorAgent, TestExecutorAgent)
from agents.executor import NO_TESTS_REPORT
from agents.qa_strategy import StrategyError, parse_strategy
from config import Config
from main import AgenticSDLC
from orchestrator import Orchestrator
from runners import LocalRunner, RunResult
from schemas import HumanDecision, TestStrategy, Ticket, TicketStatus as S
from tests.test_code_critique import FullLLM, implemented
from tests.test_implementation import CODE, ws  # noqa: F401  (ws is a fixture)
from tests.test_plan_critique import seed
from tests.test_runners import junit

STRATEGY = {
    "scope": "Unit tests for login(); no UI or load testing is in scope.",
    "priorities": ["unit: login rejects empty credentials", "unit: login accepts valid credentials"],
    "risk_areas": ["app/auth.py login treats any truthy password as valid"],
}
BUGGY = {**CODE, "app/auth.py": "def login(user, password):\n    return False\n"}
LOCAL = LocalRunner(unsafe_ok=True)  # real pytest on fixed, trusted code strings


class QALLM(FullLLM):
    def __init__(self, strategy=STRATEGY, **kw):
        super().__init__(**kw)
        self.strategy, self.qa_prompts = strategy, []

    def invoke(self, prompt):
        if "You are a QA lead" in prompt:
            self.calls += 1
            self.qa_prompts.append(prompt)
            return self._Msg(json.dumps(self.strategy) if isinstance(self.strategy, dict) else self.strategy)
        return super().invoke(prompt)


class FakeRunner:
    def __init__(self, result):
        self.result, self.calls = result, []

    def run(self, files, timeout):
        self.calls.append((dict(files), timeout))
        return self.result


def passing():
    return RunResult(exit_code=0, output="2 passed", junit_xml=junit(passed=2))


def failing():
    return RunResult(exit_code=1, output="FAILED tests/test_auth.py::test_login - assert False",
                     junit_xml=junit(passed=1, failed=1))


def with_strategy(store, t):
    return store.create_test_strategy(TestStrategy(id=str(uuid.uuid4()), ticket_id=t.id, **STRATEGY))


def executor(store, ws, runner, **kw):
    return TestExecutorAgent(store, workspace=ws, runner=runner, **kw)


# ---- strategy parsing ----

@pytest.mark.parametrize("mutate,expected", [
    (lambda d: d.update(scope="short"), "scope"),
    (lambda d: d.pop("scope"), "scope"),
    (lambda d: d.update(priorities=[]), "priorities"),
    (lambda d: d.update(priorities=["ok", ""]), "priorities"),
    (lambda d: d.update(priorities="unit tests"), "priorities"),
    (lambda d: d.update(risk_areas=[]), "risk_areas"),
    (lambda d: d.update(risk_areas=[5]), "risk_areas"),
])
def test_parse_strategy_rejects(mutate, expected):
    data = json.loads(json.dumps(STRATEGY))
    mutate(data)
    with pytest.raises(StrategyError, match=expected):
        parse_strategy(data)


def test_parse_strategy_accepts_and_strips():
    scope, pri, risk = parse_strategy({**STRATEGY, "priorities": ["  a  "]})
    assert pri == ["a"] and scope.startswith("Unit tests")


# ---- QA strategy agent ----

def test_qa_agent_stores_strategy_after_seeing_spec_and_code(store, ws):
    t, art = implemented(store, ws)
    llm = QALLM()
    r = QAStrategyAgent(store, llm).run(t)
    assert r["priorities"] == 2 and r["risk_areas"] == 1
    s = store.get_latest_test_strategy(t.id)
    assert s.priorities == STRATEGY["priorities"] and s.risk_areas == STRATEGY["risk_areas"]
    assert "password_hash" in llm.qa_prompts[0] and "def login(user, password)" in llm.qa_prompts[0]


@pytest.mark.parametrize("reply", [{"scope": "tiny"}, "not json", {}])
def test_qa_agent_rejects_bad_output_and_stores_nothing(store, ws, reply):
    t, _ = implemented(store, ws)
    assert "error" in QAStrategyAgent(store, QALLM(strategy=reply)).run(t)
    assert store.get_latest_test_strategy(t.id) is None


def test_qa_agent_needs_spec_and_artifact(store):
    t, _ = seed(store, S.QA_STRATEGY)  # plan + spec but no artifact
    assert "error" in QAStrategyAgent(store, QALLM()).run(t)


# ---- executor ----

def test_passing_run_is_recorded_and_not_a_blocker(store, ws):
    t, art = implemented(store, ws)
    with_strategy(store, t)
    runner = FakeRunner(passing())
    r = executor(store, ws, runner, timeout=77).run(t)
    assert r["passed"] and not r["has_blockers"] and r["unit_passed"] == 2
    assert runner.calls[0][1] == 77 and set(runner.calls[0][0]) == set(CODE)   # committed files, given timeout
    res = store.get_latest_test_result(t.id)
    assert res.passed and res.artifact_id == art.id and res.strategy_id == store.get_latest_test_strategy(t.id).id


def test_failing_run_is_a_blocker_and_keeps_the_runner_output(store, ws):
    t, _ = implemented(store, ws)
    with_strategy(store, t)
    r = executor(store, ws, FakeRunner(failing())).run(t)
    assert not r["passed"] and r["has_blockers"] and r["unit_failed"] == 1
    res = store.get_latest_test_result(t.id)
    assert not res.passed and "assert False" in res.report and "1 test(s) failed" in res.report


def test_infrastructure_failure_is_an_error_not_a_test_result(store, ws):
    t, _ = implemented(store, ws)
    with_strategy(store, t)
    r = executor(store, ws, FakeRunner(RunResult(error="docker unavailable: daemon not running"))).run(t)
    assert "error" in r and "daemon not running" in r["error"] and "has_blockers" not in r
    assert store.get_latest_test_result(t.id) is None


def test_no_python_tests_fails_without_running_anything(store, ws):
    t, _ = implemented(store, ws, {"app/auth.py": CODE["app/auth.py"], "web/Button.test.tsx": "test('x', () => {})\n"})
    with_strategy(store, t)
    runner = FakeRunner(passing())
    r = executor(store, ws, runner).run(t)
    assert r["has_blockers"] and runner.calls == []
    assert store.get_latest_test_result(t.id).report == NO_TESTS_REPORT


def test_requires_artifact_and_strategy(store, ws):
    t, _ = implemented(store, ws)
    assert "error" in executor(store, ws, FakeRunner(passing())).run(t)              # no strategy yet
    t2, _ = seed(store, S.TESTING)
    with_strategy(store, t2)
    assert "error" in executor(store, ws, FakeRunner(passing())).run(t2)             # no artifact


def test_tampered_artifact_is_refused_before_running_tests(store, ws):
    from schemas import CodeArtifact
    t, art = implemented(store, ws)
    with_strategy(store, t)
    store.create_code_artifact(CodeArtifact(
        id=str(uuid.uuid4()), ticket_id=t.id, branch_name="x", commit_hash=art.commit_hash,
        files_changed={**art.files_changed, "app/auth.py": "def login(u, p):\n    return True\n"}))
    runner = FakeRunner(passing())
    r = executor(store, ws, runner).run(t)
    assert "verification failed" in r["error"] and runner.calls == []


def test_unusable_runner_configuration_is_an_error(store, ws, monkeypatch):
    t, _ = implemented(store, ws)
    with_strategy(store, t)
    monkeypatch.setattr(Config, "TEST_RUNNER", "local")
    monkeypatch.delenv("ALLOW_UNSAFE_LOCAL_RUNNER", raising=False)
    r = TestExecutorAgent(store, workspace=ws).run(t)
    assert "test runner unavailable" in r["error"]


def test_real_pytest_on_committed_code_passes_and_fails(store, ws):
    t, _ = implemented(store, ws)
    with_strategy(store, t)
    ok = executor(store, ws, LOCAL).run(t)
    assert ok["passed"] and ok["unit_passed"] == 1

    t2, _ = implemented(store, ws, BUGGY)
    with_strategy(store, t2)
    bad = executor(store, ws, LOCAL).run(t2)
    assert bad["has_blockers"] and bad["unit_failed"] == 1
    assert "assert" in store.get_latest_test_result(t2.id).report


# ---- orchestrator treats tests as a bounded gate ----

def test_testing_failures_loop_to_implementation_then_escalate(store):
    orch = Orchestrator(store)
    t = store.create_ticket(Ticket(id="t", title="t", description="d", status=S.TESTING))
    orch.handle_gate_decision(store.get_ticket("t"), True)
    assert store.get_ticket("t").status is S.IMPLEMENTATION and store.get_ticket("t").retry_count == 1
    store.update_ticket_status("t", S.TESTING)
    orch.handle_gate_decision(store.get_ticket("t"), True)
    store.update_ticket_status("t", S.TESTING)
    orch.handle_gate_decision(store.get_ticket("t"), True)
    got = store.get_ticket("t")
    assert got.status is S.NEEDS_HUMAN and got.blocked_from is S.TESTING


def test_retry_count_spans_the_whole_build_region_and_resets_only_on_exit(store):
    """Regression: intermediate gate passes used to reset the counter, so test failures could loop forever."""
    orch = Orchestrator(store)
    store.create_ticket(Ticket(id="t", title="t", description="d", status=S.TESTING))
    get = lambda: store.get_ticket("t")

    orch.handle_gate_decision(get(), True)           # tests fail            -> implementation, count 1
    assert (get().status, get().retry_count) == (S.IMPLEMENTATION, 1)
    orch.advance(get())                              # re-implemented        -> code_critique
    orch.handle_gate_decision(get(), False)          # code critique passes  -> qa_strategy
    orch.advance(get())                              # strategy written      -> testing
    assert (get().status, get().retry_count) == (S.TESTING, 1)   # nothing in between reset it

    orch.handle_gate_decision(get(), True)           # second failure -> count 2
    assert get().retry_count == 2
    for step in ("advance", "gate", "advance"):
        orch.advance(get()) if step == "advance" else orch.handle_gate_decision(get(), False)
    orch.handle_gate_decision(get(), True)           # third failure hits the bound
    assert get().status is S.NEEDS_HUMAN and get().blocked_from is S.TESTING


def test_leaving_the_build_region_resets_the_counter(store):
    orch = Orchestrator(store)
    store.create_ticket(Ticket(id="t", title="t", description="d", status=S.TESTING))
    store.increment_retry_count("t")
    store.increment_retry_count("t")
    orch.handle_gate_decision(store.get_ticket("t"), False)     # tests pass -> pr_ready
    got = store.get_ticket("t")
    assert got.status is S.PR_READY and got.retry_count == 0


@pytest.mark.parametrize("decision,expected", [("force_advance", S.PR_READY), ("resume_with_guidance", S.IMPLEMENTATION)])
def test_human_can_resolve_an_escalated_test_failure(store, decision, expected):
    orch = Orchestrator(store)
    store.create_ticket(Ticket(id="t", title="t", description="d", status=S.TESTING))
    for _ in range(3):
        orch.handle_gate_decision(store.get_ticket("t"), True)
        if store.get_ticket("t").status is S.IMPLEMENTATION:
            store.update_ticket_status("t", S.TESTING)
    orch.handle_human_decision(HumanDecision(
        id="d", ticket_id="t", stage="", decision_type=decision, reasoning="r", created_by="me"))
    assert store.get_ticket("t").status is expected


# ---- the whole pipeline ----

def build(store, llm, ws, runner):
    return AgenticSDLC(store, {
        "business_analysis": BusinessAnalysisAgent(store, llm),
        "planning": PlanningAgent(store, llm),
        "spec_author": SpecAuthorAgent(store, llm),
        "plan_critique": PlanCritiqueAgent(store, llm),
        "implementation": ImplementationAgent(store, llm, ws),
        "code_critique": CodeCritiqueAgent(store, llm, ws),
        "qa_strategy": QAStrategyAgent(store, llm),
        "test_executor": TestExecutorAgent(store, workspace=ws, runner=runner),
    })


def run(store, llm, ws, runner=LOCAL, iterations=120):
    sdlc = build(store, llm, ws, runner)
    ask = sdlc.create_ticket("auth", "build login")
    asyncio.run(sdlc.run_pipeline(max_iterations=iterations))
    (child,) = store.get_children(ask.id)
    return store.get_ticket(child.id)


def test_ticket_reaches_pr_ready_with_real_tests(store, ws):
    t = run(store, QALLM(), ws)
    assert t.status is S.PR_READY and t.retry_count == 0
    res = store.get_latest_test_result(t.id)
    assert res.passed and res.artifact_id == store.get_code_artifact_for_ticket(t.id).id
    assert [h["to_status"] for h in store.get_status_history(t.id)][-4:] == [
        "code_critique", "qa_strategy", "testing", "pr_ready"]


def v(n):
    return {**BUGGY, "app/auth.py": f"def login(user, password):\n    return {bool(n)}  # v{n}\n"}


def test_failing_tests_send_the_failure_back_to_the_implementer_then_pass(store, ws):
    llm = QALLM(impl=(BUGGY, CODE))
    t = run(store, llm, ws)
    assert t.status is S.PR_READY and t.retry_count == 0
    assert len(llm.impl_prompts) == 2 and len(llm.qa_prompts) == 2
    second = llm.impl_prompts[1]
    assert "TEST FAILURES TO FIX" in second and "test_auth.py" in second       # real pytest output reached the implementer
    assert "QA STRATEGY" in second and "login rejects empty credentials" in second
    assert "return False" in second                                              # and the code it is revising
    results = [store.get_latest_test_result(t.id)]
    assert results[0].passed


def test_tests_that_never_pass_escalate_after_the_bound(store, ws):
    llm = QALLM(impl=(v(0), {**v(0), "app/auth.py": "def login(user, password):\n    return False  # again\n"},
                      {**v(0), "app/auth.py": "def login(user, password):\n    return False  # third\n"}))
    t = run(store, llm, ws)
    assert t.status is S.NEEDS_HUMAN and t.blocked_from is S.TESTING
    assert len(llm.impl_prompts) == 3  # exactly the retry bound; not endless


def test_broken_sandbox_is_not_blamed_on_the_implementer(store, ws):
    llm = QALLM()
    runner = FakeRunner(RunResult(error="docker unavailable"))
    t = run(store, llm, ws, runner=runner)
    assert t.status is S.NEEDS_HUMAN and t.blocked_from is S.TESTING
    assert len(llm.impl_prompts) == 1                       # implementation never re-run
    assert len(runner.calls) == 3 and store.get_latest_test_result(t.id) is None
