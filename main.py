import logging
import asyncio
import uuid
from state_store import StateStore
from orchestrator import Orchestrator, GATE_STATUSES
from schemas import Ticket, TicketStatus
from agents import (
    BusinessAnalysisAgent, PlanningAgent, SpecAuthorAgent, PlanCritiqueAgent, ImplementationAgent,
    CodeCritiqueAgent, QAStrategyAgent, TestExecutorAgent,
)
from config import Config
from llm import METER, TokenBudgetExceeded

logging.basicConfig(
    level=Config.LOG_LEVEL,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class AgenticSDLC:
    def __init__(self, state_store=None, agents=None):
        self.state_store = state_store or StateStore(Config.DATABASE_URL)
        self.orchestrator = Orchestrator(self.state_store)
        self.stop_reason: str | None = None   # set when a run ends early, e.g. "token_cap"
        self.agents = agents or {
            "business_analysis": BusinessAnalysisAgent(self.state_store),
            "planning": PlanningAgent(self.state_store),
            "spec_author": SpecAuthorAgent(self.state_store),
            "plan_critique": PlanCritiqueAgent(self.state_store),
            "implementation": ImplementationAgent(self.state_store),
            "code_critique": CodeCritiqueAgent(self.state_store),
            "qa_strategy": QAStrategyAgent(self.state_store),
            "test_executor": TestExecutorAgent(self.state_store),
        }

    def init_database(self):
        """Initialize the database with all required tables."""
        logger.info("Initializing database...")
        self.state_store.init_db()
        logger.info("Database initialized successfully")

    def create_ticket(self, title: str, description: str) -> Ticket:
        """Create a new ticket."""
        ticket = Ticket(
            id=str(uuid.uuid4()),
            title=title,
            description=description,
            status=TicketStatus.CREATED
        )
        return self.state_store.create_ticket(ticket)

    async def process_next_ticket(self) -> bool:
        """
        Run the agent for the next ready ticket and let the orchestrator advance it.
        Returns True if a ticket was processed, False if nothing is ready.
        """
        ticket = self.orchestrator.get_next_ticket(set(self.agents))
        if not ticket:
            logger.info("No tickets ready for processing")
            return False

        stage = self.orchestrator.dispatch_ticket(ticket)
        agent = self.agents[stage]
        logger.info(f"Processing ticket {ticket.id} at {ticket.status.value} via {stage}")

        METER.current_ticket = ticket.id
        try:
            result = agent.invoke(ticket)
        except TokenBudgetExceeded:
            raise  # a spending stop is not the ticket's fault: no retry is counted, the run just ends
        except Exception:
            logger.exception(f"Agent {stage} failed on ticket {ticket.id}")
            self.orchestrator.handle_failure(ticket)
            return True

        if "error" in result:
            logger.error(f"Agent {stage} reported error on {ticket.id}: {result['error']}")
            self.orchestrator.handle_failure(ticket)
        elif ticket.status in GATE_STATUSES:
            self.orchestrator.handle_gate_decision(ticket, result["has_blockers"])
        else:
            self.orchestrator.advance(ticket)
        return True

    async def run_pipeline(self, max_iterations: int = 10):
        """Run the pipeline, processing tickets until none are ready."""
        logger.info("Starting SDLC pipeline")

        for i in range(max_iterations):
            logger.info(f"Iteration {i+1}/{max_iterations}")

            try:
                processed = await self.process_next_ticket()
            except TokenBudgetExceeded as e:
                logger.error("Stopping run: %s", e)
                self.stop_reason = "token_cap"
                break
            if not processed:
                logger.info("No more tickets ready for processing")
                break

        logger.info("Pipeline completed (%s)", self.stop_reason or "finished")

    def usage(self) -> dict:
        """Token, cost (estimate) and timing totals for this process's model calls."""
        return METER.summary()

    def get_status(self, ticket_id: str) -> Ticket:
        """Get the current status of a ticket."""
        return self.state_store.get_ticket(ticket_id)

    def list_all_tickets(self) -> list[Ticket]:
        """List all tickets."""
        return self.state_store.get_all_tickets()


def main():
    """Main entry point."""
    # Initialize the system
    sdlc = AgenticSDLC()
    sdlc.init_database()

    # Create a sample ticket
    ticket = sdlc.create_ticket(
        title="Build user authentication system",
        description="""
        Implement a complete user authentication system with:
        - User registration and email verification
        - Login/logout functionality
        - JWT token-based session management
        - Password reset capability
        - OAuth2 integration for Google and GitHub
        - Rate limiting on login attempts
        """
    )

    logger.info(f"Created ticket: {ticket.id}")

    # Run the pipeline
    asyncio.run(sdlc.run_pipeline(max_iterations=5))

    # Print final status
    final_ticket = sdlc.get_status(ticket.id)
    logger.info(f"Final ticket status: {final_ticket.status}")

    # List all tickets
    all_tickets = sdlc.list_all_tickets()
    logger.info(f"Total tickets: {len(all_tickets)}")
    for t in all_tickets:
        logger.info(f"  - {t.id}: {t.title} ({t.status})")


if __name__ == "__main__":
    main()
