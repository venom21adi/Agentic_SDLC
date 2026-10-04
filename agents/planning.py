import logging
import uuid
from state_store import StateStore
from schemas import Ticket, Plan
from agents.base import BaseAgent

logger = logging.getLogger(__name__)


class PlanningAgent(BaseAgent):
    def __init__(self, state_store: StateStore, llm=None):
        super().__init__(state_store, "planning", llm)

    def run(self, ticket: Ticket) -> dict:
        """
        Design the implementation approach for an approved ticket.
        Input: Approved ticket with description
        Output: Implementation plan + assumptions/trade-offs document
        """
        logger.info(f"Running planning for ticket {ticket.id}")

        plan_prompt = f"""
        Design an implementation plan for the following requirement:

        Title: {ticket.title}
        Description: {ticket.description}

        Provide:
        1. High-level approach and architecture decisions
        2. Key assumptions about the system
        3. Trade-offs considered and why you chose this approach
        4. Potential risks and mitigation strategies
        5. Resource and time estimates

        Format your response as JSON with these sections.
        """

        content = self.ask_json(plan_prompt)

        # Create and store the plan
        plan = Plan(
            id=str(uuid.uuid4()),
            ticket_id=ticket.id,
            content=content,
            assumptions=str(content.get("assumptions", ""))
        )
        self.state_store.create_plan(plan)

        logger.info(f"Planning completed for ticket {ticket.id}, plan: {plan.id}")

        return {
            "plan_id": plan.id,
            "stage": "planning_complete"
        }
