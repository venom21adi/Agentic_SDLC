import logging
import uuid
from pathlib import Path
from typing import Optional

from agents.base import BaseAgent
from agents.code_critique import has_tests
from config import Config
from runners import TestRunner, default_runner, summarize
from schemas import TestResult, Ticket
from state_store import StateStore
from workspace import GitWorkspace

logger = logging.getLogger(__name__)

NO_TESTS_REPORT = (
    "No runnable tests found. Only Python/pytest tests are executed: add test files named test_*.py "
    "(for example under tests/) that import and exercise the code."
)


class TestExecutorAgent(BaseAgent):
    """
    Test gate: runs the committed change's pytest suite in a sandbox and records the outcome. No model call.
    Failing tests count as a blocker (loops back to implementation); infrastructure problems are an agent
    error instead, so a broken sandbox is never blamed on the implementer.
    """

    __test__ = False  # not a pytest class

    def __init__(
        self, state_store: StateStore, workspace: Optional[GitWorkspace] = None,
        runner: Optional[TestRunner] = None, timeout: Optional[int] = None,
    ):
        super().__init__(state_store, "test_executor")
        self._workspace, self._runner = workspace, runner
        self.timeout = timeout or Config.TEST_TIMEOUT_SECONDS

    @property
    def workspace(self) -> GitWorkspace:
        if self._workspace is None:
            self._workspace = GitWorkspace(Path(Config.WORKSPACE_DIR))
        return self._workspace

    @property
    def runner(self) -> TestRunner:
        if self._runner is None:
            self._runner = default_runner(Config.TEST_RUNNER, Config.SANDBOX_IMAGE)
        return self._runner

    def run(self, ticket: Ticket) -> dict:
        artifact = self.state_store.get_code_artifact_for_ticket(ticket.id)
        strategy = self.state_store.get_latest_test_strategy(ticket.id)
        if not artifact or not strategy:
            return {"error": "code artifact and test strategy are both required"}
        try:
            files = self.workspace.read_verified(artifact.commit_hash, artifact.files_changed)
        except ValueError as e:
            logger.error("Artifact %s failed verification: %s", artifact.id, e)
            return {"error": f"artifact verification failed: {e}"}

        if not any(p.endswith(".py") and has_tests([p]) for p in files):
            return self._record(ticket, artifact.id, strategy.id, passed=False, report=NO_TESTS_REPORT)

        try:
            run = self.runner.run(files, self.timeout)
        except (PermissionError, ValueError) as e:  # misconfigured runner, e.g. local runner not enabled
            return {"error": f"test runner unavailable: {e}"}
        if run.error:
            logger.error("Test infrastructure failure for %s: %s", ticket.id, run.error)
            return {"error": f"test infrastructure failure: {run.error}"}

        s = summarize(run)
        report = ("; ".join(s.reasons) + "\n" if s.reasons else "") + s.report
        return self._record(
            ticket, artifact.id, strategy.id, s.passed, report,
            counts=(s.unit_passed, s.unit_failed, s.integration_passed, s.integration_failed),
        )

    def _record(self, ticket, artifact_id, strategy_id, passed, report, counts=(0, 0, 0, 0)) -> dict:
        up, uf, ip, if_ = counts
        result = self.state_store.create_test_result(TestResult(
            id=str(uuid.uuid4()), ticket_id=ticket.id, strategy_id=strategy_id, artifact_id=artifact_id,
            unit_tests_passed=up, unit_tests_failed=uf, integration_tests_passed=ip, integration_tests_failed=if_,
            passed=passed, report=report,
        ))
        logger.info("Tests for %s: %s (unit %d/%d, integration %d/%d)", ticket.id,
                    "passed" if passed else "FAILED", up, up + uf, ip, ip + if_)
        return {
            "result_id": result.id, "passed": passed, "has_blockers": not passed,
            "unit_passed": up, "unit_failed": uf, "integration_passed": ip, "integration_failed": if_,
        }
