from datetime import datetime
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


class TicketStatus(str, Enum):
    CREATED = "created"
    APPROVED = "approved"
    SPEC_AUTHORING = "spec_authoring"
    PLAN_CRITIQUE = "plan_critique"
    PLAN_APPROVED = "plan_approved"
    IMPLEMENTATION = "implementation"
    CODE_CRITIQUE = "code_critique"
    QA_STRATEGY = "qa_strategy"
    TESTING = "testing"
    PR_READY = "pr_ready"
    MERGED = "merged"
    NEEDS_HUMAN = "needs_human"
    DECOMPOSED = "decomposed"  # an Ask whose sub-tickets have been created


class Severity(str, Enum):
    BLOCKER = "blocker"
    MAJOR = "major"
    NITPICK = "nitpick"


class Pillar(str, Enum):
    PERFORMANCE = "performance"
    SECURITY = "security"
    SCALABILITY = "scalability"
    MAINTAINABILITY = "maintainability"


class Ticket(BaseModel):
    id: str
    title: str
    description: str
    status: TicketStatus = TicketStatus.CREATED
    feature_id: Optional[str] = None
    parent_id: Optional[str] = None  # the Ask this ticket was decomposed from
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    retry_count: int = 0
    max_retries: int = 3
    blocked_from: Optional[TicketStatus] = None  # stage a NEEDS_HUMAN ticket was escalated from


class Plan(BaseModel):
    id: str
    ticket_id: str
    content: dict[str, Any]
    assumptions: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Specification(BaseModel):
    id: str
    ticket_id: str
    plan_id: str
    content: dict[str, Any]
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CritiqueItem(BaseModel):
    id: str
    pillar: Pillar
    severity: Severity
    finding: str
    evidence: str  # verbatim quote from the artifact (or ticket) that supports the finding
    reference: str = ""  # where in the artifact the evidence lives, e.g. "spec.data_models"
    resolved: bool = False


class PlanCritique(BaseModel):
    id: str
    ticket_id: str
    spec_id: str
    items: list[CritiqueItem]
    pass_justifications: dict[str, str] = Field(default_factory=dict)  # pillar -> why it has no findings
    created_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def has_blockers(self) -> bool:
        return any(i.severity == Severity.BLOCKER for i in self.items)


class CodeArtifact(BaseModel):
    id: str
    ticket_id: str
    branch_name: str
    files_changed: dict[str, str]
    commit_hash: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CodeCritique(BaseModel):
    id: str
    ticket_id: str
    artifact_id: str
    items: list[CritiqueItem]
    pass_justifications: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def has_blockers(self) -> bool:
        return any(i.severity == Severity.BLOCKER for i in self.items)


class TestStrategy(BaseModel):
    __test__ = False  # not a pytest class

    id: str
    ticket_id: str
    scope: str
    priorities: list[str]
    risk_areas: list[str]
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TestResult(BaseModel):
    __test__ = False  # not a pytest class

    id: str
    ticket_id: str
    strategy_id: str
    unit_tests_passed: int
    unit_tests_failed: int
    integration_tests_passed: int
    integration_tests_failed: int
    playwright_evidence_link: Optional[str] = None
    artifact_id: Optional[str] = None  # the code artifact these tests ran against
    passed: bool = False
    report: str = ""  # tail of the runner output, shown to the implementer when tests fail
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Feature(BaseModel):
    id: str
    ask_id: Optional[str] = None
    name: str
    description: str
    ticket_ids: list[str]
    created_at: datetime = Field(default_factory=datetime.utcnow)


class DAGEdge(BaseModel):
    source_ticket_id: str
    target_ticket_id: str
    reasoning: str


class HumanDecision(BaseModel):
    id: str
    ticket_id: str
    stage: str
    decision_type: str  # "approve", "reject", "force_advance", "resume_with_guidance"
    reasoning: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: str
