from datetime import datetime
from enum import Enum
from sqlalchemy import create_engine, Column, String, DateTime, Integer, JSON, Boolean, Text, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from sqlalchemy.pool import StaticPool
from typing import Optional, List
from config import Config
from schemas import (
    Ticket, TicketStatus, Plan, Specification, PlanCritique, CodeArtifact,
    CodeCritique, TestStrategy, TestResult, Feature, DAGEdge, HumanDecision,
    CritiqueItem, Severity, Pillar
)

Base = declarative_base()


def _dump(model) -> dict:
    """Pydantic model -> ORM kwargs, with enums flattened to their string values."""
    return {
        k: (v.value if isinstance(v, Enum) else v)
        for k, v in model.model_dump().items()
    }


class TicketORM(Base):
    __tablename__ = "tickets"

    id = Column(String, primary_key=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=False)
    status = Column(String, default=TicketStatus.CREATED.value)
    feature_id = Column(String, nullable=True)
    parent_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    retry_count = Column(Integer, default=0)
    max_retries = Column(Integer, default=3)
    blocked_from = Column(String, nullable=True)


class PlanORM(Base):
    __tablename__ = "plans"

    id = Column(String, primary_key=True)
    ticket_id = Column(String, ForeignKey("tickets.id"), nullable=False)
    content = Column(JSON, nullable=False)
    assumptions = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class SpecificationORM(Base):
    __tablename__ = "specifications"

    id = Column(String, primary_key=True)
    ticket_id = Column(String, ForeignKey("tickets.id"), nullable=False)
    plan_id = Column(String, ForeignKey("plans.id"), nullable=False)
    content = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class PlanCritiqueORM(Base):
    __tablename__ = "plan_critiques"

    id = Column(String, primary_key=True)
    ticket_id = Column(String, ForeignKey("tickets.id"), nullable=False)
    spec_id = Column(String, ForeignKey("specifications.id"), nullable=False)
    items = Column(JSON, nullable=False)
    pass_justifications = Column(JSON, nullable=False, default=dict)  # pillar -> why it is clean
    created_at = Column(DateTime, default=datetime.utcnow)


class CodeArtifactORM(Base):
    __tablename__ = "code_artifacts"

    id = Column(String, primary_key=True)
    ticket_id = Column(String, ForeignKey("tickets.id"), nullable=False)
    branch_name = Column(String, nullable=False)
    files_changed = Column(JSON, nullable=False)
    commit_hash = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class CodeCritiqueORM(Base):
    __tablename__ = "code_critiques"

    id = Column(String, primary_key=True)
    ticket_id = Column(String, ForeignKey("tickets.id"), nullable=False)
    artifact_id = Column(String, ForeignKey("code_artifacts.id"), nullable=False)
    items = Column(JSON, nullable=False)
    pass_justifications = Column(JSON, nullable=False, default=dict)  # pillar -> why it is clean
    created_at = Column(DateTime, default=datetime.utcnow)


class TestStrategyORM(Base):
    __tablename__ = "test_strategies"

    id = Column(String, primary_key=True)
    ticket_id = Column(String, ForeignKey("tickets.id"), nullable=False)
    scope = Column(Text, nullable=False)
    priorities = Column(JSON, nullable=False)
    risk_areas = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class TestResultORM(Base):
    __tablename__ = "test_results"

    id = Column(String, primary_key=True)
    ticket_id = Column(String, ForeignKey("tickets.id"), nullable=False)
    strategy_id = Column(String, ForeignKey("test_strategies.id"), nullable=False)
    unit_tests_passed = Column(Integer, default=0)
    unit_tests_failed = Column(Integer, default=0)
    integration_tests_passed = Column(Integer, default=0)
    integration_tests_failed = Column(Integer, default=0)
    playwright_evidence_link = Column(String, nullable=True)
    artifact_id = Column(String, nullable=True)
    passed = Column(Boolean, default=False)
    report = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class FeatureORM(Base):
    __tablename__ = "features"

    id = Column(String, primary_key=True)
    ask_id = Column(String, nullable=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=False)
    ticket_ids = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class DAGEdgeORM(Base):
    __tablename__ = "dag_edges"

    source_ticket_id = Column(String, ForeignKey("tickets.id"), primary_key=True)
    target_ticket_id = Column(String, ForeignKey("tickets.id"), primary_key=True)
    reasoning = Column(Text, nullable=False)


class HumanDecisionORM(Base):
    __tablename__ = "human_decisions"

    id = Column(String, primary_key=True)
    ticket_id = Column(String, ForeignKey("tickets.id"), nullable=False)
    stage = Column(String, nullable=False)
    decision_type = Column(String, nullable=False)
    reasoning = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(String, nullable=False)


class StatusHistoryORM(Base):
    """Append-only log of every ticket status transition; drives time-in-stage and stuck detection."""
    __tablename__ = "status_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticket_id = Column(String, ForeignKey("tickets.id"), nullable=False, index=True)
    from_status = Column(String, nullable=True)  # None for a ticket's first entry
    to_status = Column(String, nullable=False)
    entered_at = Column(DateTime, nullable=False)


class StateStore:
    def __init__(self, database_url: str = Config.DATABASE_URL):
        if database_url.startswith("postgresql://"):
            # SQLAlchemy 2.1 defaults a bare postgresql:// URL to the psycopg3 driver; this project uses psycopg2.
            database_url = "postgresql+psycopg2://" + database_url[len("postgresql://"):]
        kwargs = {}
        if database_url in ("sqlite://", "sqlite:///:memory:"):
            # An in-memory SQLite DB is per-connection; share one so every thread sees the same data.
            kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
        self.engine = create_engine(database_url, **kwargs)
        self.SessionLocal = sessionmaker(bind=self.engine)

    def init_db(self):
        Base.metadata.create_all(self.engine)

    def get_session(self) -> Session:
        return self.SessionLocal()

    # Ticket operations
    def create_ticket(self, ticket: Ticket) -> Ticket:
        session = self.get_session()
        try:
            orm = TicketORM(**_dump(ticket))
            session.add(orm)
            session.flush()
            session.add(self._history_row(orm.id, None, orm.status))
            session.commit()
            return Ticket.model_validate(orm, from_attributes=True)
        finally:
            session.close()

    @staticmethod
    def _history_row(ticket_id: str, from_status: Optional[str], to_status: str) -> "StatusHistoryORM":
        return StatusHistoryORM(
            ticket_id=ticket_id, from_status=from_status, to_status=to_status,
            entered_at=datetime.utcnow(),
        )

    def get_status_history(self, ticket_id: str) -> List[dict]:
        """Transitions oldest-first: [{from_status, to_status, entered_at}, ...]."""
        session = self.get_session()
        try:
            rows = (
                session.query(StatusHistoryORM)
                .filter(StatusHistoryORM.ticket_id == ticket_id)
                .order_by(StatusHistoryORM.id)
                .all()
            )
            return [
                {"from_status": r.from_status, "to_status": r.to_status, "entered_at": r.entered_at}
                for r in rows
            ]
        finally:
            session.close()

    def get_stage_entered_at(self, ticket_id: str) -> Optional[datetime]:
        """When the ticket entered its current status (None if it has no history)."""
        session = self.get_session()
        try:
            row = (
                session.query(StatusHistoryORM)
                .filter(StatusHistoryORM.ticket_id == ticket_id)
                .order_by(StatusHistoryORM.id.desc())
                .first()
            )
            return row.entered_at if row else None
        finally:
            session.close()

    def get_ticket(self, ticket_id: str) -> Optional[Ticket]:
        session = self.get_session()
        try:
            orm = session.query(TicketORM).filter(TicketORM.id == ticket_id).first()
            return Ticket.model_validate(orm, from_attributes=True) if orm else None
        finally:
            session.close()

    def update_ticket_status(self, ticket_id: str, status: TicketStatus) -> Ticket:
        session = self.get_session()
        try:
            orm = session.query(TicketORM).filter(TicketORM.id == ticket_id).first()
            if orm:
                new_status = TicketStatus(status).value
                if new_status != orm.status:
                    session.add(self._history_row(orm.id, orm.status, new_status))
                orm.status = new_status
                orm.updated_at = datetime.utcnow()
                session.commit()
                return Ticket.model_validate(orm, from_attributes=True)
        finally:
            session.close()

    def increment_retry_count(self, ticket_id: str) -> int:
        session = self.get_session()
        try:
            orm = session.query(TicketORM).filter(TicketORM.id == ticket_id).first()
            if orm:
                orm.retry_count += 1
                session.commit()
                return orm.retry_count
        finally:
            session.close()

    def reset_retry_count(self, ticket_id: str) -> None:
        session = self.get_session()
        try:
            orm = session.query(TicketORM).filter(TicketORM.id == ticket_id).first()
            if orm:
                orm.retry_count = 0
                session.commit()
        finally:
            session.close()

    def set_blocked_from(self, ticket_id: str, stage: Optional[TicketStatus]) -> None:
        session = self.get_session()
        try:
            orm = session.query(TicketORM).filter(TicketORM.id == ticket_id).first()
            if orm:
                orm.blocked_from = TicketStatus(stage).value if stage else None
                session.commit()
        finally:
            session.close()

    def record_human_decision(self, decision: HumanDecision) -> None:
        session = self.get_session()
        try:
            session.add(HumanDecisionORM(**_dump(decision)))
            session.commit()
        finally:
            session.close()

    def get_tickets_by_status(self, status: TicketStatus) -> List[Ticket]:
        session = self.get_session()
        try:
            orms = session.query(TicketORM).filter(TicketORM.status == TicketStatus(status).value).all()
            return [Ticket.model_validate(orm, from_attributes=True) for orm in orms]
        finally:
            session.close()

    # Plan operations
    def create_plan(self, plan: Plan) -> Plan:
        session = self.get_session()
        try:
            orm = PlanORM(**_dump(plan))
            session.add(orm)
            session.commit()
            return Plan.model_validate(orm, from_attributes=True)
        finally:
            session.close()

    def get_plan_for_ticket(self, ticket_id: str) -> Optional[Plan]:
        session = self.get_session()
        try:
            orm = (
                session.query(PlanORM).filter(PlanORM.ticket_id == ticket_id)
                .order_by(PlanORM.created_at.desc()).first()
            )
            return Plan.model_validate(orm, from_attributes=True) if orm else None
        finally:
            session.close()

    # Specification operations
    def create_specification(self, spec: Specification) -> Specification:
        session = self.get_session()
        try:
            orm = SpecificationORM(**_dump(spec))
            session.add(orm)
            session.commit()
            return Specification.model_validate(orm, from_attributes=True)
        finally:
            session.close()

    def get_specification_for_ticket(self, ticket_id: str) -> Optional[Specification]:
        session = self.get_session()
        try:
            orm = (
                session.query(SpecificationORM).filter(SpecificationORM.ticket_id == ticket_id)
                .order_by(SpecificationORM.created_at.desc()).first()
            )
            return Specification.model_validate(orm, from_attributes=True) if orm else None
        finally:
            session.close()

    # Plan critique operations
    def create_plan_critique(self, critique: PlanCritique) -> PlanCritique:
        session = self.get_session()
        try:
            data = critique.model_dump(mode="json")  # items hold enums; store plain JSON
            data["created_at"] = critique.created_at
            session.add(PlanCritiqueORM(**data))
            session.commit()
            return critique
        finally:
            session.close()

    def get_latest_plan_critique(self, ticket_id: str) -> Optional[PlanCritique]:
        session = self.get_session()
        try:
            orm = (
                session.query(PlanCritiqueORM).filter(PlanCritiqueORM.ticket_id == ticket_id)
                .order_by(PlanCritiqueORM.created_at.desc()).first()
            )
            return PlanCritique.model_validate(orm, from_attributes=True) if orm else None
        finally:
            session.close()

    def get_plan_critiques(self, ticket_id: str) -> List[PlanCritique]:
        """All critique passes for a ticket, oldest first."""
        session = self.get_session()
        try:
            orms = (
                session.query(PlanCritiqueORM).filter(PlanCritiqueORM.ticket_id == ticket_id)
                .order_by(PlanCritiqueORM.created_at).all()
            )
            return [PlanCritique.model_validate(o, from_attributes=True) for o in orms]
        finally:
            session.close()

    # Code Artifact operations
    def create_code_artifact(self, artifact: CodeArtifact) -> CodeArtifact:
        session = self.get_session()
        try:
            orm = CodeArtifactORM(**_dump(artifact))
            session.add(orm)
            session.commit()
            return CodeArtifact.model_validate(orm, from_attributes=True)
        finally:
            session.close()

    def get_code_artifact_for_ticket(self, ticket_id: str) -> Optional[CodeArtifact]:
        session = self.get_session()
        try:
            orm = (
                session.query(CodeArtifactORM).filter(CodeArtifactORM.ticket_id == ticket_id)
                .order_by(CodeArtifactORM.created_at.desc()).first()
            )
            return CodeArtifact.model_validate(orm, from_attributes=True) if orm else None
        finally:
            session.close()

    # Code critique operations
    def create_code_critique(self, critique: CodeCritique) -> CodeCritique:
        session = self.get_session()
        try:
            data = critique.model_dump(mode="json")
            data["created_at"] = critique.created_at
            session.add(CodeCritiqueORM(**data))
            session.commit()
            return critique
        finally:
            session.close()

    def get_latest_code_critique(self, ticket_id: str) -> Optional[CodeCritique]:
        session = self.get_session()
        try:
            orm = (
                session.query(CodeCritiqueORM).filter(CodeCritiqueORM.ticket_id == ticket_id)
                .order_by(CodeCritiqueORM.created_at.desc()).first()
            )
            return CodeCritique.model_validate(orm, from_attributes=True) if orm else None
        finally:
            session.close()

    # Test strategy / result operations
    def create_test_strategy(self, strategy: TestStrategy) -> TestStrategy:
        session = self.get_session()
        try:
            session.add(TestStrategyORM(**_dump(strategy)))
            session.commit()
            return strategy
        finally:
            session.close()

    def get_latest_test_strategy(self, ticket_id: str) -> Optional[TestStrategy]:
        session = self.get_session()
        try:
            orm = (
                session.query(TestStrategyORM).filter(TestStrategyORM.ticket_id == ticket_id)
                .order_by(TestStrategyORM.created_at.desc()).first()
            )
            return TestStrategy.model_validate(orm, from_attributes=True) if orm else None
        finally:
            session.close()

    def create_test_result(self, result: TestResult) -> TestResult:
        session = self.get_session()
        try:
            session.add(TestResultORM(**_dump(result)))
            session.commit()
            return result
        finally:
            session.close()

    def get_latest_test_result(self, ticket_id: str) -> Optional[TestResult]:
        session = self.get_session()
        try:
            orm = (
                session.query(TestResultORM).filter(TestResultORM.ticket_id == ticket_id)
                .order_by(TestResultORM.created_at.desc()).first()
            )
            return TestResult.model_validate(orm, from_attributes=True) if orm else None
        finally:
            session.close()

    # DAG operations
    def add_dag_edge(self, edge: DAGEdge) -> None:
        session = self.get_session()
        try:
            orm = DAGEdgeORM(**_dump(edge))
            session.add(orm)
            session.commit()
        finally:
            session.close()

    def get_dag_predecessors(self, ticket_id: str) -> List[str]:
        session = self.get_session()
        try:
            edges = session.query(DAGEdgeORM).filter(
                DAGEdgeORM.target_ticket_id == ticket_id
            ).all()
            return [edge.source_ticket_id for edge in edges]
        finally:
            session.close()

    def create_decomposition(
        self, features: List[Feature], tickets: List[Ticket], edges: List[DAGEdge]
    ) -> None:
        """Persist features, sub-tickets and DAG edges atomically (all or nothing)."""
        session = self.get_session()
        try:
            session.add_all(FeatureORM(**_dump(f)) for f in features)
            session.add_all(TicketORM(**_dump(t)) for t in tickets)
            session.flush()  # tickets must exist before edges reference them
            session.add_all(self._history_row(t.id, None, TicketStatus(t.status).value) for t in tickets)
            session.add_all(DAGEdgeORM(**_dump(e)) for e in edges)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_children(self, parent_id: str) -> List[Ticket]:
        session = self.get_session()
        try:
            orms = session.query(TicketORM).filter(TicketORM.parent_id == parent_id).all()
            return [Ticket.model_validate(o, from_attributes=True) for o in orms]
        finally:
            session.close()

    def get_all_features(self) -> List[Feature]:
        session = self.get_session()
        try:
            return [Feature.model_validate(o, from_attributes=True) for o in session.query(FeatureORM).all()]
        finally:
            session.close()

    def get_features_for_ask(self, ask_id: str) -> List[Feature]:
        session = self.get_session()
        try:
            orms = session.query(FeatureORM).filter(FeatureORM.ask_id == ask_id).all()
            return [Feature.model_validate(o, from_attributes=True) for o in orms]
        finally:
            session.close()

    def get_all_tickets(self) -> List[Ticket]:
        session = self.get_session()
        try:
            orms = session.query(TicketORM).all()
            return [Ticket.model_validate(orm, from_attributes=True) for orm in orms]
        finally:
            session.close()
