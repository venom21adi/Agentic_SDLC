import logging
import uuid

from agents.base import BaseAgent
from agents.critique import flatten
from config import Config
from schemas import TestStrategy, Ticket
from state_store import StateStore

logger = logging.getLogger(__name__)

PROMPT = """You are a QA lead. Plan how the change below should be tested. You do not write tests;
you decide what matters and where the risk is, so reviewers and implementers know what coverage is expected.

=== TICKET ===
Title: {title}
Description: {description}

=== SPECIFICATION ===
{spec}

=== CODE (as committed) ===
{code}

Reply with one JSON object, nothing else:
{{"scope": "what is and is not covered by testing, in a few sentences",
  "priorities": ["most important behaviour or test type first, e.g. 'unit: password hashing'", "..."],
  "risk_areas": ["specific places this code is most likely to be wrong, referencing the code or spec", "..."]}}

Rules: be specific to this change. Name concrete functions, endpoints or edge cases. Generic advice
("test thoroughly") is not acceptable."""

MIN_SCOPE_CHARS = 20


class StrategyError(ValueError):
    pass


def parse_strategy(data: dict) -> tuple[str, list[str], list[str]]:
    problems = []
    scope = data.get("scope")
    if not isinstance(scope, str) or len(scope.strip()) < MIN_SCOPE_CHARS:
        problems.append("scope missing or too short")
    lists = {}
    for key in ("priorities", "risk_areas"):
        value = data.get(key)
        if not isinstance(value, list) or not value or not all(isinstance(v, str) and v.strip() for v in value):
            problems.append(f"{key} must be a non-empty list of non-empty strings")
        else:
            lists[key] = [v.strip() for v in value]
    if problems:
        raise StrategyError("; ".join(problems))
    return scope.strip(), lists["priorities"], lists["risk_areas"]


class QAStrategyAgent(BaseAgent):
    """Plans the testing approach for the latest implementation. Planning only; the executor runs tests."""

    model_name = Config.PLANNING_MODEL

    def __init__(self, state_store: StateStore, llm=None):
        super().__init__(state_store, "qa_strategy", llm)

    def run(self, ticket: Ticket) -> dict:
        spec = self.state_store.get_specification_for_ticket(ticket.id)
        artifact = self.state_store.get_code_artifact_for_ticket(ticket.id)
        if not spec or not artifact:
            return {"error": "specification and code artifact are both required"}

        code = "\n".join(f"--- {p} ---\n{c}" for p, c in artifact.files_changed.items())
        data = self.ask_json(PROMPT.format(
            title=ticket.title, description=ticket.description, spec=flatten(spec.content), code=code,
        ))
        try:
            scope, priorities, risk_areas = parse_strategy(data)
        except StrategyError as e:
            logger.warning("Rejected QA strategy for %s: %s", ticket.id, e)
            return {"error": f"invalid strategy: {e}"}

        strategy = self.state_store.create_test_strategy(TestStrategy(
            id=str(uuid.uuid4()), ticket_id=ticket.id, scope=scope, priorities=priorities, risk_areas=risk_areas,
        ))
        logger.info("QA strategy %s for %s", strategy.id, ticket.id)
        return {"strategy_id": strategy.id, "priorities": len(priorities), "risk_areas": len(risk_areas)}
