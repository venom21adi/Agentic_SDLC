import logging
import re
import uuid
from pathlib import Path
from typing import Optional

from agents.base import BaseAgent
from agents.critique import CritiqueValidationError, flatten, parse_critique
from config import Config
from schemas import CodeCritique, CritiqueItem, Pillar, Severity, Ticket
from state_store import StateStore
from workspace import GitWorkspace

logger = logging.getLogger(__name__)

PROMPT = """You are an independent code reviewer. You see only the specification and the committed
code below, not how the code was produced.

Review the code against four pillars: performance, security, scalability, maintainability.
Also check it against the specification's contracts: a deviation from the spec is a finding.

Severity:
- blocker: a serious defect, vulnerability or spec violation that must be fixed before this ships
- major: a significant weakness that should be fixed but does not stop progress
- nitpick: minor or stylistic

Rules:
- Every finding must quote the code (or the specification/ticket) VERBATIM in "evidence", and give
  the file in "reference" (e.g. "app/auth.py"). A finding without a real quote will be discarded.
- If a pillar has no findings, you must justify why it is clean in "pass_justification" with
  specifics from the code. "Looks fine" is not acceptable.
- Do not pad. Report only real issues.

=== TICKET ===
Title: {title}
Description: {description}

=== SPECIFICATION ===
{spec}

=== CODE (as committed) ===
{code}

Reply with one JSON object, nothing else:
{{"pillars": {{
  "performance":     {{"findings": [{{"severity": "blocker|major|nitpick", "finding": "...", "reference": "...", "evidence": "..."}}], "pass_justification": "..."}},
  "security":        {{...same shape...}},
  "scalability":     {{...same shape...}},
  "maintainability": {{...same shape...}}
}}}}"""

_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$", re.I)


def has_tests(paths) -> bool:
    return any(_TEST_PATH.search(p) for p in paths)


class CodeCritiqueAgent(BaseAgent):
    """
    Gate: reviews the latest committed implementation. The code is read back from git at the recorded
    commit (not trusted from the store), and the critic never sees generator notes or earlier critiques.
    """

    model_name = Config.CRITIQUE_MODEL
    temperature = 0.0

    def __init__(self, state_store: StateStore, llm=None, workspace: Optional[GitWorkspace] = None):
        super().__init__(state_store, "code_critique", llm)
        self._workspace = workspace

    @property
    def workspace(self) -> GitWorkspace:
        if self._workspace is None:
            self._workspace = GitWorkspace(Path(Config.WORKSPACE_DIR))
        return self._workspace

    def run(self, ticket: Ticket) -> dict:
        artifact = self.state_store.get_code_artifact_for_ticket(ticket.id)
        spec = self.state_store.get_specification_for_ticket(ticket.id)
        if not artifact or not spec:
            return {"error": "code artifact and specification are both required"}
        try:
            files = self.workspace.read_verified(artifact.commit_hash, artifact.files_changed)
        except ValueError as e:
            logger.error("Artifact %s failed verification: %s", artifact.id, e)
            return {"error": f"artifact verification failed: {e}"}

        code_text = "\n".join(f"--- {path} ---\n{content}" for path, content in files.items())
        spec_text = flatten(spec.content)
        # Deliberately excludes implementer notes, prior critiques and any generator prompt.
        data = self.ask_json(PROMPT.format(
            title=ticket.title, description=ticket.description, spec=spec_text, code=code_text,
        ))
        corpus = "\n".join([ticket.title, ticket.description, spec_text, code_text])
        try:
            items, justifications = parse_critique(data, corpus)
        except CritiqueValidationError as e:
            logger.warning("Rejected code critique for %s: %s", ticket.id, e)
            return {"error": f"invalid critique: {e}"}

        # Mechanical floor: whatever the model says, a change with no tests is at least a major finding.
        if not has_tests(files):
            items.append(CritiqueItem(
                id=str(uuid.uuid4()), pillar=Pillar.MAINTAINABILITY, severity=Severity.MAJOR,
                finding="No automated tests are included with this change.",
                evidence="files: " + ", ".join(sorted(files)), reference="artifact",
            ))
            justifications.pop(Pillar.MAINTAINABILITY.value, None)

        critique = CodeCritique(
            id=str(uuid.uuid4()), ticket_id=ticket.id, artifact_id=artifact.id,
            items=items, pass_justifications=justifications,
        )
        self.state_store.create_code_critique(critique)
        counts = {s: sum(1 for i in items if i.severity == s) for s in Severity}
        logger.info("Code critique %s for %s: %s", critique.id, ticket.id, {s.value: n for s, n in counts.items()})
        return {
            "critique_id": critique.id,
            "has_blockers": critique.has_blockers,
            "blockers": counts[Severity.BLOCKER],
            "majors": counts[Severity.MAJOR],
            "nitpicks": counts[Severity.NITPICK],
        }
