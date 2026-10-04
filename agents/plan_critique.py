import logging
import uuid

from agents.base import BaseAgent
from agents.critique import CritiqueValidationError, flatten, normalize, parse_critique  # noqa: F401  (re-exported)
from config import Config
from schemas import PlanCritique, Severity, Ticket
from state_store import StateStore

logger = logging.getLogger(__name__)

PROMPT = """You are an independent reviewer of an implementation plan and technical specification.
You see only the artifact below, not how it was produced.

Review it against four pillars: performance, security, scalability, maintainability.

Severity:
- blocker: implementing this as written would cause a serious defect, vulnerability or rework
- major: a significant weakness that should be fixed but does not stop implementation
- nitpick: minor or stylistic

Rules:
- Every finding must quote the artifact (or the ticket) VERBATIM in "evidence", and say where in
  "reference" (e.g. "spec.data_models"). A finding without a real quote will be discarded.
- If a pillar has no findings, you must justify why it is clean in "pass_justification"
  with specifics from the artifact. "Looks fine" is not acceptable.
- Do not pad. Report only real issues.

=== TICKET ===
Title: {title}
Description: {description}

=== PLAN ===
{plan}

=== SPECIFICATION ===
{spec}

Reply with one JSON object, nothing else:
{{"pillars": {{
  "performance":     {{"findings": [{{"severity": "blocker|major|nitpick", "finding": "...", "reference": "...", "evidence": "..."}}], "pass_justification": "..."}},
  "security":        {{...same shape...}},
  "scalability":     {{...same shape...}},
  "maintainability": {{...same shape...}}
}}}}"""


class PlanCritiqueAgent(BaseAgent):
    """Gate: reviews the latest plan + spec on four pillars. Sees the artifact only, never generator reasoning."""

    model_name = Config.CRITIQUE_MODEL

    def __init__(self, state_store: StateStore, llm=None):
        super().__init__(state_store, "plan_critique", llm)

    def run(self, ticket: Ticket) -> dict:
        plan = self.state_store.get_plan_for_ticket(ticket.id)
        spec = self.state_store.get_specification_for_ticket(ticket.id)
        if not plan or not spec:
            return {"error": "plan and specification are both required"}

        plan_text = flatten(plan.content) + "\n" + plan.assumptions
        spec_text = flatten(spec.content)
        # Deliberately excludes prior critiques and any generator prompt/reasoning: each pass is independent.
        data = self.ask_json(PROMPT.format(
            title=ticket.title, description=ticket.description, plan=plan_text, spec=spec_text,
        ))
        corpus = "\n".join([ticket.title, ticket.description, plan_text, spec_text])
        try:
            items, justifications = parse_critique(data, corpus)
        except CritiqueValidationError as e:
            logger.warning("Rejected critique for %s: %s", ticket.id, e)
            return {"error": f"invalid critique: {e}"}

        critique = PlanCritique(
            id=str(uuid.uuid4()), ticket_id=ticket.id, spec_id=spec.id,
            items=items, pass_justifications=justifications,
        )
        self.state_store.create_plan_critique(critique)
        counts = {s: sum(1 for i in items if i.severity == s) for s in Severity}
        logger.info("Plan critique %s for %s: %s", critique.id, ticket.id, {s.value: n for s, n in counts.items()})
        return {
            "critique_id": critique.id,
            "has_blockers": critique.has_blockers,
            "blockers": counts[Severity.BLOCKER],
            "majors": counts[Severity.MAJOR],
            "nitpicks": counts[Severity.NITPICK],
        }
