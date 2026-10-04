import logging
from typing import Optional
from state_store import StateStore
from schemas import Ticket, TicketStatus, HumanDecision
from config import Config

logger = logging.getLogger(__name__)

S = TicketStatus

# status -> stage name of the agent that works a ticket in that status
STAGE_FOR_STATUS = {
    S.CREATED: "business_analysis",
    S.APPROVED: "planning",
    S.SPEC_AUTHORING: "spec_author",
    S.PLAN_CRITIQUE: "plan_critique",
    S.PLAN_APPROVED: "implementation",
    S.IMPLEMENTATION: "implementation",
    S.CODE_CRITIQUE: "code_critique",
    S.QA_STRATEGY: "qa_strategy",
    S.TESTING: "test_executor",
}
# Stages whose outcome is pass/fail: a failure loops back (bounded). Tests are a gate too.
GATE_STATUSES = {S.PLAN_CRITIQUE, S.CODE_CRITIQUE, S.TESTING}

# where a ticket goes once the agent for its current status succeeds (or a gate passes)
NEXT_STATUS = {
    S.CREATED: S.DECOMPOSED,
    S.APPROVED: S.SPEC_AUTHORING,
    S.SPEC_AUTHORING: S.PLAN_CRITIQUE,
    S.PLAN_CRITIQUE: S.PLAN_APPROVED,
    S.PLAN_APPROVED: S.CODE_CRITIQUE,
    S.IMPLEMENTATION: S.CODE_CRITIQUE,
    S.CODE_CRITIQUE: S.QA_STRATEGY,
    S.QA_STRATEGY: S.TESTING,
    S.TESTING: S.PR_READY,
}

# where a gate sends a ticket back to when it finds blockers
ROLLBACK_STATUS = {
    S.PLAN_CRITIQUE: S.SPEC_AUTHORING,
    S.CODE_CRITIQUE: S.IMPLEMENTATION,
    S.TESTING: S.IMPLEMENTATION,
}


# Retry regions. A gate can send a ticket back to an earlier stage, and the stages in between may pass
# again before the next failure, so the retry counter must span the whole region. It resets only when a
# ticket leaves a region (or advances outside any region), never inside one; otherwise the bound could
# never trip, because every intermediate pass would reset it.
RETRY_REGION = {
    S.SPEC_AUTHORING: "plan", S.PLAN_CRITIQUE: "plan",
    S.IMPLEMENTATION: "build", S.CODE_CRITIQUE: "build", S.QA_STRATEGY: "build", S.TESTING: "build",
}


class Orchestrator:
    """Deterministic router. Holds no state of its own; everything is read from the store."""

    def __init__(self, state_store: StateStore):
        self.state_store = state_store
        self.max_retries = Config.MAX_RETRIES_PER_GATE

    # ---- scheduling ----

    def dependencies_met(self, ticket: Ticket) -> bool:
        for pred_id in self.state_store.get_dag_predecessors(ticket.id):
            pred = self.state_store.get_ticket(pred_id)
            if not pred or pred.status != S.MERGED:
                return False
        return True

    def is_ready(self, ticket: Ticket, available_stages: Optional[set[str]] = None) -> bool:
        stage = STAGE_FOR_STATUS.get(ticket.status)
        if stage is None:
            return False  # pr_ready / merged / needs_human wait on a human
        if available_stages is not None and stage not in available_stages:
            return False
        return self.dependencies_met(ticket)

    def list_ready_tickets(self, available_stages: Optional[set[str]] = None) -> list[Ticket]:
        return [t for t in self.state_store.get_all_tickets() if self.is_ready(t, available_stages)]

    def get_next_ticket(self, available_stages: Optional[set[str]] = None) -> Optional[Ticket]:
        ready = self.list_ready_tickets(available_stages)
        return ready[0] if ready else None

    def dispatch_ticket(self, ticket: Ticket) -> Optional[str]:
        return STAGE_FOR_STATUS.get(ticket.status)

    # ---- transitions ----

    def advance(self, ticket: Ticket) -> Optional[Ticket]:
        """Move a ticket to its next status and reset its retry counter."""
        new_status = NEXT_STATUS.get(ticket.status)
        if new_status is None:
            return None
        logger.info("Ticket %s: %s -> %s", ticket.id, ticket.status.value, new_status.value)
        region = RETRY_REGION.get(ticket.status)
        if region is None or region != RETRY_REGION.get(new_status):
            self.state_store.reset_retry_count(ticket.id)
        return self.state_store.update_ticket_status(ticket.id, new_status)

    def handle_failure(self, ticket: Ticket) -> None:
        """An agent errored. Count it against the retry bound; escalate once exhausted."""
        retries = self.state_store.increment_retry_count(ticket.id)
        if retries >= self.max_retries:
            self._escalate(ticket)

    def handle_gate_decision(self, ticket: Ticket, has_blockers: bool) -> None:
        """Apply the outcome of a critique gate (ticket.status must be a gate status)."""
        if ticket.status not in GATE_STATUSES:
            raise ValueError(f"{ticket.status} is not a gate stage")
        if not has_blockers:
            self.advance(ticket)
            return
        retries = self.state_store.increment_retry_count(ticket.id)
        if retries >= self.max_retries:
            self._escalate(ticket)
        else:
            self._send_back(ticket)

    def _send_back(self, ticket: Ticket) -> None:
        target = ROLLBACK_STATUS.get(ticket.status)
        if target:
            logger.info("Ticket %s sent back to %s", ticket.id, target.value)
            self.state_store.update_ticket_status(ticket.id, target)

    def _escalate(self, ticket: Ticket) -> None:
        logger.warning("Ticket %s exceeded retry bound at %s; needs human", ticket.id, ticket.status.value)
        self.state_store.set_blocked_from(ticket.id, ticket.status)
        self.state_store.update_ticket_status(ticket.id, S.NEEDS_HUMAN)

    # ---- human decisions ----

    def handle_human_decision(self, decision: HumanDecision) -> None:
        ticket = self.state_store.get_ticket(decision.ticket_id)
        if not ticket:
            raise ValueError(f"Unknown ticket {decision.ticket_id}")
        if ticket.status != S.NEEDS_HUMAN or ticket.blocked_from is None:
            raise ValueError(f"Ticket {ticket.id} is not waiting on a human (status {ticket.status.value})")

        stage = ticket.blocked_from
        if decision.decision_type in ("approve", "force_advance"):
            target = NEXT_STATUS.get(stage)
        elif decision.decision_type in ("reject", "resume_with_guidance"):
            target = ROLLBACK_STATUS.get(stage, stage)
        else:
            raise ValueError(f"Unsupported decision type {decision.decision_type}")
        if target is None:
            raise ValueError(f"No transition from {stage.value} for {decision.decision_type}")

        # Validated; record against the stage the ticket was actually blocked at.
        self.state_store.record_human_decision(decision.model_copy(update={"stage": stage.value}))
        self.state_store.reset_retry_count(ticket.id)
        self.state_store.set_blocked_from(ticket.id, None)
        self.state_store.update_ticket_status(ticket.id, target)

    def get_ticket_status(self, ticket_id: str) -> Optional[Ticket]:
        return self.state_store.get_ticket(ticket_id)
