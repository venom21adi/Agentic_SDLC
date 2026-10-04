import asyncio
import json
import subprocess
import uuid
from pathlib import Path

import pytest

from agents import (BusinessAnalysisAgent, ImplementationAgent, PlanCritiqueAgent, PlanningAgent,
                    SpecAuthorAgent)
from agents.implementation import branch_for, parse_implementation, ImplementationError
from main import AgenticSDLC
from schemas import (CodeArtifact, CodeCritique, CritiqueItem, Pillar, Plan, Severity, Specification,
                     Ticket, TicketStatus as S)
from tests.test_plan_critique import ScriptedLLM, clean_critique, seed
from workspace import GitWorkspace, UnsafePathError

CODE = {
    "app/auth.py": "def login(user, password):\n    return bool(user and password)\n",
    "tests/test_auth.py": "from app.auth import login\n\ndef test_login():\n    assert login('a', 'b')\n",
}


@pytest.fixture
def ws(tmp_path):
    return GitWorkspace(tmp_path / "ws")


def git(ws, *args):
    return subprocess.run(["git", *args], cwd=ws.repo, capture_output=True, text=True).stdout.strip()


# ---- workspace ----

def test_commit_creates_isolated_branch_and_leaves_main_alone(ws):
    r = ws.commit_files("ticket/abc", CODE, message="add auth")
    assert r.created and len(r.commit_hash) == 40
    assert ws.read_file("ticket/abc", "app/auth.py") == CODE["app/auth.py"]
    assert sorted(ws.list_files("ticket/abc")) == sorted(CODE)
    assert ws.list_files("main") == []                       # main untouched
    assert git(ws, "rev-parse", "ticket/abc") == r.commit_hash
    assert not (ws.repo / "app").exists()                    # main checkout has no files
    assert not any(ws.worktrees.glob("*"))                   # temp worktree cleaned up


def test_second_commit_updates_and_removes_files(ws):
    first = ws.commit_files("ticket/abc", CODE)
    second = ws.commit_files("ticket/abc", {"app/auth.py": "x = 1\n"}, remove=["tests/test_auth.py"])
    assert second.created and second.commit_hash != first.commit_hash
    assert ws.list_files("ticket/abc") == ["app/auth.py"]
    assert git(ws, "rev-list", "--count", "ticket/abc") == "3"  # init + 2 commits


def test_identical_content_is_not_recommitted(ws):
    first = ws.commit_files("ticket/abc", CODE)
    again = ws.commit_files("ticket/abc", CODE)
    assert not again.created and again.commit_hash == first.commit_hash


def test_branches_are_independent(ws):
    ws.commit_files("ticket/a", {"a.txt": "A"})
    ws.commit_files("ticket/b", {"b.txt": "B"})
    assert ws.list_files("ticket/a") == ["a.txt"] and ws.list_files("ticket/b") == ["b.txt"]


@pytest.mark.parametrize("bad", [
    "../escape.py", "a/../../escape.py", "/abs/path.py", "C:\\win\\path.py", "c:/x.py", ".git/config",
    "sub/.git/hooks/pre-commit", ".GIT/config", ".git./config", "file.txt:stream", "", "   ", ".", None, 5,
])
def test_unsafe_paths_rejected_before_touching_disk(ws, bad):
    with pytest.raises(UnsafePathError):
        ws.commit_files("ticket/abc", {bad: "x"})
    assert not ws.repo.exists() or not ws.branch_exists("ticket/abc")


def test_duplicate_paths_after_normalization_rejected(ws):
    with pytest.raises(UnsafePathError, match="duplicate"):
        ws.commit_files("ticket/abc", {"a/b.py": "1", "a\\b.py": "2"})
    with pytest.raises(UnsafePathError, match="duplicate"):
        ws.commit_files("ticket/abc", {"A.py": "1", "a.py": "2"})


def test_backslash_paths_are_normalized(ws):
    ws.commit_files("ticket/abc", {"pkg\\mod.py": "x = 1\n"})
    assert ws.list_files("ticket/abc") == ["pkg/mod.py"]


# ---- parsing / validation ----

@pytest.mark.parametrize("data,expected", [
    ({}, "no non-empty"),
    ({"files": {}}, "no non-empty"),
    ({"files": []}, "no non-empty"),
    ({"files": {"a.py": ""}}, "non-empty string"),
    ({"files": {"a.py": 5}}, "non-empty string"),
    ({"files": {"a.py": "def broken(:\n"}}, "does not compile"),
    ({"files": {"../a.py": "x = 1"}}, "escapes"),
    ({"files": {f"f{i}.txt": "x" for i in range(31)}}, "exceeds the limit of 30"),
    ({"files": {"big.txt": "x" * 200_001}}, "per-file limit"),
])
def test_parse_rejects(data, expected):
    with pytest.raises(ImplementationError, match=expected):
        parse_implementation(data)


def test_parse_accepts_non_python_without_compiling():
    files, notes = parse_implementation({"files": {"README.md": "def broken(:", "a.py": "x = 1"}, "notes": "n"})
    assert set(files) == {"README.md", "a.py"} and notes == "n"


def test_compile_check_does_not_execute_code(tmp_path):
    marker = tmp_path / "ran"
    parse_implementation({"files": {"a.py": f"open({str(marker)!r}, 'w').write('x')\n"}})
    assert not marker.exists()


# ---- agent ----

def impl_llm(*replies):
    """Fake LLM replying to the implementation prompt with each dict in turn (last repeats)."""
    class L(ScriptedLLM):
        def __init__(self):
            super().__init__([clean_critique()])
            self.impl_prompts = []
            self.queue = list(replies)

        def invoke(self, prompt):
            if "senior implementation engineer" in prompt:
                self.calls += 1
                self.impl_prompts.append(prompt)
                reply = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
                return self._Msg(reply if isinstance(reply, str) else json.dumps(reply))
            return super().invoke(prompt)
    return L()


def agent_for(store, ws, llm):
    return ImplementationAgent(store, llm, ws)


def test_agent_commits_code_and_records_artifact(store, ws):
    t, spec = seed(store, S.PLAN_APPROVED)
    llm = impl_llm({"files": CODE, "notes": "done"})
    r = agent_for(store, ws, llm).run(t)
    assert r["files"] == 2 and r["new_commit"] and r["branch"] == branch_for(t)
    art = store.get_code_artifact_for_ticket(t.id)
    assert art.commit_hash == r["commit"] and art.files_changed == CODE and art.branch_name == branch_for(t)
    assert ws.read_file(art.branch_name, "app/auth.py") == CODE["app/auth.py"]
    prompt = llm.impl_prompts[0]
    assert "users table stores email" in prompt and "bcrypt" in prompt  # spec + plan reach the implementer


def test_agent_requires_plan_and_spec(store, ws):
    t = store.create_ticket(Ticket(id="x", title="t", description="d", status=S.PLAN_APPROVED))
    assert "error" in agent_for(store, ws, impl_llm({"files": CODE})).run(t)


@pytest.mark.parametrize("reply", [
    {"files": {"a.py": "def broken(:\n"}},
    {"files": {"../evil.py": "x = 1"}},
    {"files": {}},
    "not json at all",
])
def test_agent_rejects_bad_output_and_writes_nothing(store, ws, reply):
    t, _ = seed(store, S.PLAN_APPROVED)
    r = agent_for(store, ws, impl_llm(reply)).run(t)
    assert "error" in r
    assert store.get_code_artifact_for_ticket(t.id) is None
    assert not ws.repo.exists() or not ws.branch_exists(branch_for(t))


def test_revision_builds_on_previous_files_and_deletes_omitted_ones(store, ws):
    t, _ = seed(store, S.PLAN_APPROVED)
    llm = impl_llm(
        {"files": CODE},
        {"files": {"app/auth.py": "def login(u, p):\n    return u == p\n"}},   # drops the test file
    )
    agent = agent_for(store, ws, llm)
    first = agent.run(t)
    store.update_ticket_status(t.id, S.IMPLEMENTATION)
    second = agent.run(store.get_ticket(t.id))
    assert second["removed"] == ["tests/test_auth.py"] and second["commit"] != first["commit"]
    assert ws.list_files(branch_for(t)) == ["app/auth.py"]
    assert "YOUR PREVIOUS IMPLEMENTATION" in llm.impl_prompts[1]
    assert "def login(user, password)" in llm.impl_prompts[1]
    assert "YOUR PREVIOUS IMPLEMENTATION" not in llm.impl_prompts[0]
    assert store.get_code_artifact_for_ticket(t.id).commit_hash == second["commit"]  # latest wins


def _critique(store, t, artifact_id, severity):
    store.create_code_critique(CodeCritique(
        id=str(uuid.uuid4()), ticket_id=t.id, artifact_id=artifact_id,
        items=[CritiqueItem(id="i1", pillar=Pillar.SECURITY, severity=severity,
                            finding="Password compared in plain text", evidence="return bool(user and password)",
                            reference="app/auth.py")]))


def test_blocking_code_critique_findings_reach_the_next_attempt(store, ws):
    t, _ = seed(store, S.PLAN_APPROVED)
    llm = impl_llm({"files": CODE})
    agent = agent_for(store, ws, llm)
    agent.run(t)
    art = store.get_code_artifact_for_ticket(t.id)
    _critique(store, t, art.id, Severity.BLOCKER)
    store.update_ticket_status(t.id, S.IMPLEMENTATION)
    agent.run(store.get_ticket(t.id))
    assert "REVIEW FINDINGS TO RESOLVE" in llm.impl_prompts[1]
    assert "Password compared in plain text" in llm.impl_prompts[1]


def test_non_blocking_or_stale_critique_is_not_fed_back(store, ws):
    t, _ = seed(store, S.PLAN_APPROVED)
    llm = impl_llm({"files": CODE})
    agent = agent_for(store, ws, llm)
    agent.run(t)
    art = store.get_code_artifact_for_ticket(t.id)
    _critique(store, t, art.id, Severity.MAJOR)               # not a blocker
    store.update_ticket_status(t.id, S.IMPLEMENTATION)
    agent.run(store.get_ticket(t.id))
    assert "REVIEW FINDINGS" not in llm.impl_prompts[1]
    _critique(store, t, art.id, Severity.BLOCKER)  # about a superseded artifact (the latest is the 2nd run's)
    agent.run(store.get_ticket(t.id))
    assert "REVIEW FINDINGS" not in llm.impl_prompts[2]


# ---- pipeline ----

def build(store, llm, ws):
    return AgenticSDLC(store, {
        "business_analysis": BusinessAnalysisAgent(store, llm),
        "planning": PlanningAgent(store, llm),
        "spec_author": SpecAuthorAgent(store, llm),
        "plan_critique": PlanCritiqueAgent(store, llm),
        "implementation": ImplementationAgent(store, llm, ws),
    })


def run(store, llm, ws, iterations=40):
    sdlc = build(store, llm, ws)
    ask = sdlc.create_ticket("auth", "build login")
    asyncio.run(sdlc.run_pipeline(max_iterations=iterations))
    (child,) = store.get_children(ask.id)
    return store.get_ticket(child.id)


def test_pipeline_reaches_code_critique_with_a_real_commit(store, ws):
    t = run(store, impl_llm({"files": CODE}), ws)
    assert t.status is S.CODE_CRITIQUE  # no code critique agent yet, so it waits here
    art = store.get_code_artifact_for_ticket(t.id)
    assert ws.read_file(art.branch_name, "tests/test_auth.py") == CODE["tests/test_auth.py"]
    assert [h["to_status"] for h in store.get_status_history(t.id)][-3:] == [
        "plan_critique", "plan_approved", "code_critique"]


def test_pipeline_bad_implementations_escalate_after_bound(store, ws):
    t = run(store, impl_llm({"files": {"a.py": "def broken(:\n"}}), ws)
    assert t.status is S.NEEDS_HUMAN and t.blocked_from is S.PLAN_APPROVED
    assert store.get_code_artifact_for_ticket(t.id) is None
