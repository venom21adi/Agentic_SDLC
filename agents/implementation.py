import logging
import re
import uuid
from pathlib import Path
from typing import Optional

from agents.base import BaseAgent
from config import Config
from schemas import CodeArtifact, Ticket, TicketStatus
from state_store import StateStore
from workspace import GitWorkspace, UnsafePathError, WorkspaceError, normalize_paths

logger = logging.getLogger(__name__)

MAX_FILES = 30
MAX_FILE_BYTES = 200_000
MAX_TOTAL_BYTES = 1_000_000

PROMPT = """You are a senior implementation engineer. Implement the ticket below exactly as the
specification describes. Write production-quality code and include automated tests for it.

=== TICKET ===
Title: {title}
Description: {description}

=== PLAN ===
{plan}

=== SPECIFICATION ===
{spec}
{existing}{feedback}
Reply with one JSON object, nothing else:
{{"files": {{"relative/path.py": "complete file contents", "tests/test_x.py": "..."}},
  "notes": "anything the reviewer should know"}}

Rules:
- Paths are relative to the repository root. No absolute paths, no "..", nothing under .git.
- Give the COMPLETE set of files for this ticket on every reply; any previously committed file you omit is deleted.
- At most {max_files} files. Do not include binaries or generated artifacts."""


class ImplementationError(ValueError):
    pass


def parse_implementation(data: dict) -> tuple[dict[str, str], str]:
    """Validate the LLM's file set; raise ImplementationError listing every problem."""
    files = data.get("files")
    if not isinstance(files, dict) or not files:
        raise ImplementationError("response has no non-empty 'files' object")

    problems: list[str] = []
    if len(files) > MAX_FILES:
        problems.append(f"{len(files)} files exceeds the limit of {MAX_FILES}")
    try:
        paths = normalize_paths(files.keys())
    except UnsafePathError as e:
        raise ImplementationError(str(e)) from e

    out: dict[str, str] = {}
    total = 0
    for path, (raw_path, content) in zip(paths, files.items()):
        if not isinstance(content, str) or not content.strip():
            problems.append(f"{raw_path}: content must be a non-empty string")
            continue
        size = len(content.encode("utf-8"))
        total += size
        if size > MAX_FILE_BYTES:
            problems.append(f"{raw_path}: {size} bytes exceeds the per-file limit of {MAX_FILE_BYTES}")
        if path.endswith(".py"):
            try:
                compile(content, path, "exec")  # syntax check only; never executes the code
            except Exception as e:  # SyntaxError, ValueError (NUL bytes), RecursionError...
                problems.append(f"{raw_path}: does not compile ({type(e).__name__}: {e})")
        out[path] = content
    if total > MAX_TOTAL_BYTES:
        problems.append(f"total size {total} bytes exceeds the limit of {MAX_TOTAL_BYTES}")
    if problems:
        raise ImplementationError("; ".join(problems))

    notes = data.get("notes")
    return out, notes if isinstance(notes, str) else ""


def branch_for(ticket: Ticket) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", ticket.title.lower()).strip("-")[:30].strip("-") or "work"
    return f"ticket/{ticket.id[:8]}-{slug}"


class ImplementationAgent(BaseAgent):
    """Turns an approved spec into code committed on an isolated per-ticket branch. Never runs the code."""

    model_name = Config.IMPLEMENTATION_MODEL

    def __init__(self, state_store: StateStore, llm=None, workspace: Optional[GitWorkspace] = None):
        super().__init__(state_store, "implementation", llm)
        self._workspace = workspace

    @property
    def workspace(self) -> GitWorkspace:
        if self._workspace is None:
            self._workspace = GitWorkspace(Path(Config.WORKSPACE_DIR))
        return self._workspace

    def run(self, ticket: Ticket) -> dict:
        spec = self.state_store.get_specification_for_ticket(ticket.id)
        plan = self.state_store.get_plan_for_ticket(ticket.id)
        if not spec or not plan:
            return {"error": "approved plan and specification are required"}

        prior = self.state_store.get_code_artifact_for_ticket(ticket.id)
        existing = feedback = ""
        if prior:
            files = "\n".join(f"--- {p} ---\n{c}" for p, c in prior.files_changed.items())
            existing = f"\n=== YOUR PREVIOUS IMPLEMENTATION (revise it) ===\n{files}\n"
        if ticket.status is TicketStatus.IMPLEMENTATION and prior:
            critique = self.state_store.get_latest_code_critique(ticket.id)
            if critique and critique.artifact_id == prior.id and critique.has_blockers:
                lines = [
                    f"- [{i.severity.value}/{i.pillar.value}] {i.finding} (re: {i.reference}; quote: \"{i.evidence}\")"
                    for i in critique.items if i.severity.value != "nitpick"
                ]
                feedback = (
                    "\n=== REVIEW FINDINGS TO RESOLVE ===\nYour previous implementation was rejected. "
                    "Fix every finding:\n" + "\n".join(lines) + "\n"
                )
            result = self.state_store.get_latest_test_result(ticket.id)
            if result and result.artifact_id == prior.id and not result.passed:
                feedback += (
                    "\n=== TEST FAILURES TO FIX ===\nYour previous implementation failed its tests. "
                    "Runner output (tail):\n" + result.report + "\n"
                )
            strategy = self.state_store.get_latest_test_strategy(ticket.id)
            if strategy:
                feedback += (
                    "\n=== QA STRATEGY (tests must cover this) ===\n"
                    f"Scope: {strategy.scope}\nPriorities: {'; '.join(strategy.priorities)}\n"
                    f"Risk areas: {'; '.join(strategy.risk_areas)}\n"
                )

        data = self.ask_json(PROMPT.format(
            title=ticket.title, description=ticket.description, plan=plan.content, spec=spec.content,
            existing=existing, feedback=feedback, max_files=MAX_FILES,
        ))
        try:
            files, notes = parse_implementation(data)
        except ImplementationError as e:
            logger.warning("Rejected implementation for %s: %s", ticket.id, e)
            return {"error": f"invalid implementation: {e}"}

        branch = branch_for(ticket)
        removed = sorted(set(prior.files_changed) - set(files)) if prior else []
        try:
            result = self.workspace.commit_files(
                branch, files, remove=removed, message=f"{ticket.title}\n\nTicket: {ticket.id}",
            )
        except (WorkspaceError, UnsafePathError) as e:
            logger.error("Commit failed for %s: %s", ticket.id, e)
            return {"error": f"commit failed: {e}"}

        artifact = self.state_store.create_code_artifact(CodeArtifact(
            id=str(uuid.uuid4()), ticket_id=ticket.id, branch_name=branch,
            files_changed=files, commit_hash=result.commit_hash,
        ))
        logger.info("Implemented %s on %s @ %s (%d files)", ticket.id, branch, result.commit_hash[:8], len(files))
        return {
            "artifact_id": artifact.id, "branch": branch, "commit": result.commit_hash,
            "files": len(files), "removed": removed, "new_commit": result.created, "notes": notes,
        }
