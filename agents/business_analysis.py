import logging
import uuid
from state_store import StateStore
from schemas import Ticket, TicketStatus, Feature, DAGEdge
from agents.base import BaseAgent

logger = logging.getLogger(__name__)

PROMPT = """Break the following requirement (an "Ask") into features and atomic implementation tickets.

Ask title: {title}
Ask description:
{description}

Reply with a single JSON object, nothing else:
{{
  "features": [
    {{"name": "...", "description": "...",
      "tickets": [{{"key": "short-unique-key", "title": "...", "description": "..."}}]}}
  ],
  "dependencies": [
    {{"from": "ticket-key", "to": "ticket-key", "reasoning": "why `to` cannot start until `from` is merged"}}
  ],
  "open_questions": ["..."]
}}
Rules: every ticket key is unique across the whole response; dependencies reference those keys;
only add a dependency when it is genuinely required; the dependency graph must have no cycles."""


class DecompositionError(ValueError):
    pass


def validate_decomposition(data: dict) -> None:
    """Raise DecompositionError unless the LLM output is a usable ticket plan."""
    features = data.get("features")
    if not isinstance(features, list) or not features:
        raise DecompositionError("no features in response")

    keys: set[str] = set()
    for f in features:
        if (
            not isinstance(f, dict)
            or not f.get("name")
            or not isinstance(f.get("tickets"), list)
            or not f["tickets"]
        ):
            raise DecompositionError("each feature needs a name and at least one ticket")
        for t in f["tickets"]:
            if not isinstance(t, dict) or not all(
                isinstance(t.get(k), str) and t[k] for k in ("key", "title", "description")
            ):
                raise DecompositionError("each ticket needs key, title and description")
            if t["key"] in keys:
                raise DecompositionError(f"duplicate ticket key {t['key']!r}")
            keys.add(t["key"])

    graph: dict[str, list[str]] = {k: [] for k in keys}
    for d in data.get("dependencies") or []:
        if not isinstance(d, dict) or d.get("from") not in keys or d.get("to") not in keys:
            raise DecompositionError(f"dependency references unknown ticket: {d!r}")
        if d["from"] == d["to"]:
            raise DecompositionError(f"ticket {d['from']!r} depends on itself")
        graph[d["from"]].append(d["to"])

    # Kahn's algorithm: if not every node can be consumed, there is a cycle
    indegree = {k: 0 for k in keys}
    for targets in graph.values():
        for t in targets:
            indegree[t] += 1
    queue = [k for k, n in indegree.items() if n == 0]
    seen = 0
    while queue:
        node = queue.pop()
        seen += 1
        for t in graph[node]:
            indegree[t] -= 1
            if indegree[t] == 0:
                queue.append(t)
    if seen != len(keys):
        raise DecompositionError("dependency cycle detected")


class BusinessAnalysisAgent(BaseAgent):
    def __init__(self, state_store: StateStore, llm=None):
        super().__init__(state_store, "business_analysis", llm)

    def run(self, ticket: Ticket) -> dict:
        """
        Decompose an Ask into features, sub-tickets and a dependency DAG, and persist them.
        Sub-tickets start APPROVED (ready for planning); the orchestrator then marks the Ask DECOMPOSED.
        """
        if self.state_store.get_children(ticket.id):
            return {"error": "ask already decomposed"}

        data = self.ask_json(PROMPT.format(title=ticket.title, description=ticket.description))
        try:
            validate_decomposition(data)
        except DecompositionError as e:
            logger.warning("Bad decomposition for %s: %s", ticket.id, e)
            return {"error": f"invalid decomposition: {e}"}

        features: list[Feature] = []
        tickets: list[Ticket] = []
        id_for_key: dict[str, str] = {}
        for f in data["features"]:
            feature = Feature(
                id=str(uuid.uuid4()),
                ask_id=ticket.id,
                name=f["name"],
                description=f.get("description") or f["name"],
                ticket_ids=[],
            )
            for t in f["tickets"]:
                sub = Ticket(
                    id=str(uuid.uuid4()),
                    title=t["title"],
                    description=t["description"],
                    status=TicketStatus.APPROVED,
                    feature_id=feature.id,
                    parent_id=ticket.id,
                )
                id_for_key[t["key"]] = sub.id
                feature.ticket_ids.append(sub.id)
                tickets.append(sub)
            features.append(feature)

        edges = [
            DAGEdge(
                source_ticket_id=id_for_key[d["from"]],
                target_ticket_id=id_for_key[d["to"]],
                reasoning=d.get("reasoning") or "unspecified",
            )
            for d in data.get("dependencies") or []
        ]
        self.state_store.create_decomposition(features, tickets, edges)
        logger.info(
            "Ask %s -> %d features, %d tickets, %d edges",
            ticket.id, len(features), len(tickets), len(edges),
        )
        return {
            "features": len(features),
            "tickets": len(tickets),
            "edges": len(edges),
            "open_questions": data.get("open_questions", []),
        }
