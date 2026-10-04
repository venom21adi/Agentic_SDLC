import logging
import uuid
from state_store import StateStore
from schemas import Ticket, Specification

from agents.base import BaseAgent

logger = logging.getLogger(__name__)


class SpecAuthorAgent(BaseAgent):
    def __init__(self, state_store: StateStore, llm=None):
        super().__init__(state_store, "spec_author", llm)

    def run(self, ticket: Ticket) -> dict:
        """
        Write detailed technical specification based on the plan.
        Input: Plan + assumptions document
        Output: Technical spec with interface contracts, data models, edge cases
        """
        logger.info(f"Running spec authoring for ticket {ticket.id}")

        # Get the plan from the state store
        plan = self.state_store.get_plan_for_ticket(ticket.id)
        if not plan:
            logger.error(f"No plan found for ticket {ticket.id}")
            return {"error": "No plan found"}

        feedback = ""
        critique = self.state_store.get_latest_plan_critique(ticket.id)
        if critique and critique.has_blockers:
            lines = [
                f"- [{i.severity.value}/{i.pillar.value}] {i.finding} (re: {i.reference}; quote: \"{i.evidence}\")"
                for i in critique.items if i.severity.value != "nitpick"
            ]
            feedback = (
                "\n        A previous version of this spec was rejected by review. "
                "Revise it so every finding below is resolved:\n        " + "\n        ".join(lines) + "\n"
            )

        spec_prompt = f"""
        Based on the following plan, write a detailed technical specification:
        {feedback}

        Ticket: {ticket.title}
        Description: {ticket.description}
        Plan: {plan.content}

        Provide:
        1. API/Interface contracts - exact signatures and behaviors
        2. Data models - schemas and relationships
        3. Edge cases and error handling
        4. Performance requirements and constraints
        5. Security considerations
        6. Testing strategy overview

        Format your response as JSON with these sections.
        """

        content = self.ask_json(spec_prompt)

        # Create and store the specification
        spec = Specification(
            id=str(uuid.uuid4()),
            ticket_id=ticket.id,
            plan_id=plan.id,
            content=content
        )
        self.state_store.create_specification(spec)

        logger.info(f"Spec authoring completed for ticket {ticket.id}, spec: {spec.id}")

        return {
            "spec_id": spec.id,
            "stage": "spec_authoring_complete"
        }
