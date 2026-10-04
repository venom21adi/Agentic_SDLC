"""Fill an EMPTY database with a small, realistic sample so the Command Center has something to show.

    python demo_seed.py                      # uses DATABASE_URL
    python demo_seed.py sqlite:///demo.db    # or an explicit URL

Refuses to touch a database that already has tickets. Everything it writes is fake data.
"""
import sys
import uuid
from datetime import datetime, timedelta

from config import Config
from schemas import DAGEdge, Feature, Ticket, TicketStatus as S
from state_store import StateStore, StatusHistoryORM

HOUR = 3600


def _age(store: StateStore, ticket_id: str, seconds_in_current: float, step_seconds: float = 1500) -> None:
    """Backdate a ticket's history: the latest row `seconds_in_current` ago, earlier rows spaced further back."""
    session = store.get_session()
    try:
        rows = session.query(StatusHistoryORM).filter_by(ticket_id=ticket_id).order_by(StatusHistoryORM.id).all()
        now = datetime.utcnow()
        for i, row in enumerate(reversed(rows)):
            row.entered_at = now - timedelta(seconds=seconds_in_current + i * step_seconds)
        session.commit()
    finally:
        session.close()


def _walk(store: StateStore, ticket_id: str, *path: S) -> None:
    for status in path:
        store.update_ticket_status(ticket_id, status)


def seed(store: StateStore) -> dict:
    if store.get_all_tickets():
        raise SystemExit("Database already has tickets; refusing to seed. Use an empty database.")

    def ask(title: str, description: str) -> Ticket:
        return store.create_ticket(Ticket(id=str(uuid.uuid4()), title=title, description=description))

    def decompose(parent: Ticket, features: dict[str, list[str]], deps: list[tuple[str, str]]) -> dict[str, Ticket]:
        feats, tickets, by_title = [], [], {}
        for name, titles in features.items():
            feat = Feature(id=str(uuid.uuid4()), ask_id=parent.id, name=name, description=f"{name} work", ticket_ids=[])
            for title in titles:
                t = Ticket(id=str(uuid.uuid4()), title=title, description=f"Implement: {title}",
                           status=S.APPROVED, feature_id=feat.id, parent_id=parent.id)
                feat.ticket_ids.append(t.id)
                tickets.append(t)
                by_title[title] = t
            feats.append(feat)
        edges = [DAGEdge(source_ticket_id=by_title[a].id, target_ticket_id=by_title[b].id, reasoning=f"{b} needs {a}")
                 for a, b in deps]
        store.create_decomposition(feats, tickets, edges)
        _walk(store, parent.id, S.DECOMPOSED)
        return by_title

    # Ask 1: mid-flight, with a healthy ticket, a retrying one, a timed-out one and an escalated one.
    auth = ask("Build user authentication system", "Registration, login, JWT sessions, password reset, OAuth.")
    a = decompose(
        auth,
        {"Core auth": ["User table", "Login API", "JWT sessions", "Password reset"],
         "OAuth": ["Google OAuth", "GitHub OAuth"]},
        [("User table", "Login API"), ("Login API", "JWT sessions"), ("Login API", "Password reset"),
         ("Google OAuth", "GitHub OAuth")],
    )
    _walk(store, a["User table"].id, S.SPEC_AUTHORING, S.PLAN_CRITIQUE, S.PLAN_APPROVED, S.CODE_CRITIQUE,
          S.QA_STRATEGY, S.TESTING, S.PR_READY, S.MERGED)
    _age(store, a["User table"].id, 6 * HOUR)

    _walk(store, a["Login API"].id, S.SPEC_AUTHORING, S.PLAN_CRITIQUE, S.PLAN_APPROVED, S.CODE_CRITIQUE,
          S.QA_STRATEGY, S.TESTING)
    store.increment_retry_count(a["Login API"].id)
    _age(store, a["Login API"].id, 8 * 60)

    _walk(store, a["Google OAuth"].id, S.SPEC_AUTHORING, S.PLAN_CRITIQUE, S.PLAN_APPROVED, S.CODE_CRITIQUE)
    for _ in range(3):
        store.increment_retry_count(a["Google OAuth"].id)
    store.set_blocked_from(a["Google OAuth"].id, S.CODE_CRITIQUE)
    store.update_ticket_status(a["Google OAuth"].id, S.NEEDS_HUMAN)
    _age(store, a["Google OAuth"].id, 3 * HOUR + 20 * 60)

    # Ask 2: stage timeout on a root ticket, which also blocks its dependent.
    audit = ask("Add audit logging", "Record who changed what, and expose it to admins.")
    b = decompose(audit, {"Audit trail": ["Event schema", "Write path", "Admin viewer"]},
                  [("Event schema", "Write path"), ("Write path", "Admin viewer")])
    _walk(store, b["Event schema"].id, S.SPEC_AUTHORING, S.PLAN_CRITIQUE)
    _age(store, b["Event schema"].id, 2 * HOUR + 30 * 60)

    # Ask 3: freshly submitted, not yet analysed.
    ask("Export reports as PDF", "Users should be able to download any report as a PDF.")

    # Ask 4: finished.
    done = ask("Fix login redirect bug", "Users land on a blank page after logging in.")
    c = decompose(done, {"Bugfix": ["Fix redirect"]}, [])
    _walk(store, c["Fix redirect"].id, S.SPEC_AUTHORING, S.PLAN_CRITIQUE, S.PLAN_APPROVED, S.CODE_CRITIQUE,
          S.QA_STRATEGY, S.TESTING, S.PR_READY, S.MERGED)
    _age(store, c["Fix redirect"].id, 2 * 24 * HOUR)

    return {"asks": 4, "tickets": len(store.get_all_tickets())}


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else Config.DATABASE_URL
    store = StateStore(url)
    store.init_db()
    print(seed(store), "->", url.split("@")[-1])
