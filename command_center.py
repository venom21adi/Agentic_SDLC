"""Read-side views for the Command Center: Ask / Feature / Ticket rollups, stuck detection, decision queue.

Everything is computed from the state store on demand; nothing here is persisted or cached.
"""
from collections import Counter
from datetime import datetime
from typing import Optional

from config import Config
from orchestrator import NEXT_STATUS, ROLLBACK_STATUS
from schemas import Ticket, TicketStatus as S
from state_store import StateStore

# Statuses where time-in-stage is not a stuck signal: nothing is expected to move them.
_NO_TIMEOUT = {S.MERGED, S.DECOMPOSED}


class CommandCenter:
    def __init__(self, store: StateStore, stage_timeout_seconds: float = Config.STAGE_TIMEOUT):
        self.store = store
        self.stage_timeout = stage_timeout_seconds

    # ---- ticket level ----

    def _unmerged_predecessors(self, ticket_id: str) -> list[str]:
        out = []
        for pid in self.store.get_dag_predecessors(ticket_id):
            pred = self.store.get_ticket(pid)
            if pred is None or pred.status is not S.MERGED:
                out.append(pid)
        return out

    def _seconds_in_stage(self, ticket: Ticket, now: datetime) -> Optional[float]:
        entered = self.store.get_stage_entered_at(ticket.id)
        return (now - entered).total_seconds() if entered else None

    def _direct_stuck_reasons(self, ticket: Ticket, now: datetime) -> list[str]:
        reasons = []
        if ticket.status is S.NEEDS_HUMAN:
            reasons.append("needs_human")
        # A ticket waiting on dependencies is expected to sit still, so it can't time out.
        if ticket.status not in _NO_TIMEOUT and not self._unmerged_predecessors(ticket.id):
            secs = self._seconds_in_stage(ticket, now)
            if secs is not None and secs > self.stage_timeout:
                reasons.append("stage_timeout")
        return reasons

    def _blocked_by_stuck(self, ticket_id: str, now: datetime, memo: dict[str, bool]) -> list[str]:
        """Unmerged predecessors that are themselves stuck, directly or transitively."""
        blockers = []
        for pid in self._unmerged_predecessors(ticket_id):
            if pid not in memo:
                pred = self.store.get_ticket(pid)
                memo[pid] = bool(
                    pred
                    and (
                        self._direct_stuck_reasons(pred, now)
                        or self._blocked_by_stuck(pid, now, memo)
                    )
                )
            if memo[pid]:
                blockers.append(pid)
        return blockers

    def _title(self, ticket_id: str) -> str:
        t = self.store.get_ticket(ticket_id)
        return t.title if t else ticket_id

    def ticket_view(self, ticket_id: str, now: Optional[datetime] = None) -> Optional[dict]:
        now = now or datetime.utcnow()
        t = self.store.get_ticket(ticket_id)
        if t is None:
            return None
        reasons = self._direct_stuck_reasons(t, now)
        stuck_deps = self._blocked_by_stuck(t.id, now, {})
        if stuck_deps:
            reasons.append("blocked_by_stuck_dependency")
        waiting_on = self._unmerged_predecessors(t.id)
        parent = self.store.get_ticket(t.parent_id) if t.parent_id else None
        feature = self.store.get_feature(t.feature_id) if t.feature_id else None
        blocked = t.blocked_from
        return {
            "id": t.id,
            "title": t.title,
            "status": t.status.value,
            "parent_id": t.parent_id,
            "feature_id": t.feature_id,
            "ask_title": parent.title if parent else None,
            "feature_name": feature.name if feature else None,
            "retry_count": t.retry_count,
            "max_retries": t.max_retries,
            "blocked_from": blocked.value if blocked else None,
            # What each human decision would do, taken from the orchestrator's own tables.
            "accept_goes_to": NEXT_STATUS[blocked].value if blocked in NEXT_STATUS else None,
            "send_back_goes_to": (ROLLBACK_STATUS.get(blocked, blocked).value if blocked else None),
            "seconds_in_stage": self._seconds_in_stage(t, now),
            "waiting_on": waiting_on,
            "waiting_on_titles": [self._title(i) for i in waiting_on],
            "stuck_reasons": reasons,
            "stuck_dependencies": stuck_deps,
            "stuck_dependency_titles": [self._title(i) for i in stuck_deps],
            "history": [
                {**h, "entered_at": h["entered_at"].isoformat()}
                for h in self.store.get_status_history(t.id)
            ],
        }

    # ---- rollups ----

    @staticmethod
    def _rollup(tickets: list[Ticket]) -> dict:
        counts = Counter(t.status.value for t in tickets)
        total = len(tickets)
        merged = counts.get(S.MERGED.value, 0)
        if total and merged == total:
            status = "complete"
        elif counts.get(S.NEEDS_HUMAN.value):
            status = "needs_attention"
        elif merged or any(t.status is not S.APPROVED for t in tickets):
            status = "in_progress"
        else:
            status = "not_started"
        return {
            "status": status,
            "total": total,
            "by_status": dict(counts),
            "percent_complete": round(100 * merged / total, 1) if total else 0.0,
        }

    def feature_view(self, feature_id: str, now: Optional[datetime] = None) -> Optional[dict]:
        now = now or datetime.utcnow()
        feat = next((f for f in self.store.get_all_features() if f.id == feature_id), None)
        if feat is None:
            return None
        tickets = [t for t in map(self.store.get_ticket, feat.ticket_ids) if t]
        views = [self.ticket_view(t.id, now) for t in tickets]
        return {
            "id": feat.id,
            "ask_id": feat.ask_id,
            "name": feat.name,
            "description": feat.description,
            **self._rollup(tickets),
            "stuck_tickets": [v["id"] for v in views if v["stuck_reasons"]],
            "tickets": views,
        }

    def ask_view(self, ask_id: str, now: Optional[datetime] = None) -> Optional[dict]:
        now = now or datetime.utcnow()
        ask = self.store.get_ticket(ask_id)
        if ask is None or ask.parent_id is not None:
            return None
        children = self.store.get_children(ask.id)
        features = [self.feature_view(f.id, now) for f in self.store.get_features_for_ask(ask.id)]
        if ask.status is S.NEEDS_HUMAN:
            rollup = {**self._rollup(children), "status": "needs_attention"}
        elif ask.status is not S.DECOMPOSED:
            rollup = {**self._rollup(children), "status": "analysis"}  # not yet broken into tickets
        else:
            rollup = self._rollup(children)
        return {
            "id": ask.id,
            "title": ask.title,
            "ask_status": ask.status.value,
            **rollup,
            "features": features,
            "stuck_tickets": [tid for f in features for tid in f["stuck_tickets"]],
        }

    def list_asks(self, now: Optional[datetime] = None) -> list[dict]:
        now = now or datetime.utcnow()
        asks = [t for t in self.store.get_all_tickets() if t.parent_id is None]
        out = []
        for a in asks:
            v = self.ask_view(a.id, now)
            out.append({k: v[k] for k in ("id", "title", "ask_status", "status", "total", "percent_complete")}
                       | {"stuck": len(v["stuck_tickets"])})
        return out

    # ---- stuck list and decision queue ----

    def stuck_tickets(self, now: Optional[datetime] = None) -> list[dict]:
        now = now or datetime.utcnow()
        views = (self.ticket_view(t.id, now) for t in self.store.get_all_tickets())
        return [v for v in views if v["stuck_reasons"]]

    def decision_queue(self, now: Optional[datetime] = None) -> list[dict]:
        """Tickets waiting on a human, oldest first."""
        now = now or datetime.utcnow()
        views = [
            self.ticket_view(t.id, now)
            for t in self.store.get_tickets_by_status(S.NEEDS_HUMAN)
        ]
        return sorted(views, key=lambda v: -(v["seconds_in_stage"] or 0))
