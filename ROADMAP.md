# Implementation Roadmap

## Phase 1: Core Pipeline (✅ COMPLETE)

Core infrastructure to run tickets through the first 3 stages.

### Completed
- [x] **State Store** (`state_store.py`)
  - PostgreSQL persistence layer
  - ORM models for all entities
  - CRUD operations for tickets, plans, specs, etc.
  
- [x] **Orchestrator** (`orchestrator.py`)
  - Stateless scheduler
  - Dependency resolution
  - Status → Agent routing
  - Retry bounds enforcement
  
- [x] **Schemas** (`schemas.py`)
  - Pydantic models for all data types
  - Ticket statuses and enums
  - Type safety across system

- [x] **Base Infrastructure**
  - Docker Compose for PostgreSQL
  - Environment configuration
  - Logging setup
  - Requirements and setup scripts

- [x] **Agent Framework**
  - Base agent class with LangGraph integration
  - Business Analysis Agent (stub)
  - Planning Agent (stub)
  - Spec Author Agent (stub)

- [x] **Pipeline Coordinator** (`main.py`)
  - Main loop orchestration
  - Ticket creation and routing
  - Status tracking

## Phase 2: Quality Gates (🔄 NEXT)

Implement the two critical quality gates that validate artifacts.

### Plan Critique Agent (2-3 days)
**File**: `agents/plan_critique.py`

Validates implementation plans against 4 pillars:
- Performance: Will this scale? Any bottlenecks?
- Security: Any vulnerabilities or compliance issues?
- Scalability: Can it handle growth?
- Maintainability: Is it understandable and extensible?

**Key responsibilities**:
- Read plan + spec from state store
- Run LLM evaluation against each pillar
- Generate CritiqueItem objects (BLOCKER/MAJOR/NITPICK)
- Write PlanCritique to state store
- Orchestrator auto-routes based on blocker presence

**Test case**:
```python
# Create a ticket with deliberately flawed plan
# Should find security blockers, performance major issues
# Should not pass without fixes
```

### Code Critique Agent (2-3 days)
**File**: `agents/code_critique.py`

Same 4-pillar approach but on actual code:
- Performance: Algorithmic complexity, caching misses
- Security: SQL injection, auth bypasses, data leaks
- Scalability: Connection pooling, async patterns
- Maintainability: Code style, documentation, test coverage

**Key responsibilities**:
- Read code artifact from store
- Parse and analyze implementation
- Reference spec for contract violations
- Generate findings
- Write CodeCritique to state store

**Blocker triggers** (auto-blocker rules):
- Schema/migration changes → Major minimum
- Security-sensitive code without review → Blocker
- No tests for new code → Major minimum

## Phase 3: Implementation Agents (1-2 weeks)

Specialized agents for different implementation domains.

### Implementation Agent - Frontend (3-4 days)
**File**: `agents/implementation_frontend.py`

Generates React/TypeScript UI components:
- Reads spec for interface contracts
- Generates components matching API contracts
- Creates unit tests
- Commits to isolated branch
- Output: CodeArtifact

### Implementation Agent - Backend (3-4 days)
**File**: `agents/implementation_backend.py`

Generates API endpoints and business logic:
- Reads spec for endpoint definitions
- Generates request/response handlers
- Implements business logic
- Creates integration tests
- Output: CodeArtifact

### Implementation Agent - Schema (2-3 days)
**File**: `agents/implementation_schema.py`

Generates database migrations and models:
- Reads spec for data model definitions
- Generates SQL migrations
- Creates ORM models
- Output: CodeArtifact with migration files

## Phase 4: Testing & QA (1 week)

Agents for test strategy and execution.

### QA Strategy Agent (2-3 days)
**File**: `agents/qa_strategy.py`

Plans testing approach:
- Reads spec for risk-sensitive areas
- Identifies boundary conditions and edge cases
- Prioritizes test types (unit, integration, E2E)
- Creates TestStrategy artifact

### Test Executor Agent (3-4 days)
**File**: `agents/test_executor.py`

Runs tests and gathers evidence:
- Executes unit tests (pytest)
- Runs integration tests against test DB
- Runs E2E tests with Playwright
- Collects screenshots/videos
- Creates TestResult artifact
- Links evidence to ticket

## Phase 5: Human-In-The-Loop (3-4 days)

PR packaging and human review infrastructure.

### PR Packager Agent (2 days)
**File**: `agents/pr_packager.py`

Prepares for human review:
- Creates GitHub PR from isolated branch
- Attaches critique summary
- Links test evidence
- Hygiene checks (ticket linked, evidence attached, no unrelated files)
- Output: PR + evidence links

### Human Decision Queue (2-3 days)
**Files**: 
- `api/human_decisions.py` — Decision recording
- API endpoints for review/override/escalation
- Dashboard showing stuck tickets

Decision types:
- **Approve**: Accept artifact as-is
- **Reject**: Send back to agent with feedback
- **Edit**: Human fixes artifact, re-critiques
- **Force-Advance**: Accept despite findings (requires justification)
- **Resume with Guidance**: Add coaching, reset retry count

## Phase 6: Observability & Monitoring (2-3 weeks)

Full observability stack.

### Structured Logging (2-3 days)
**File**: `observability/logging.py`

- JSON logs with ticket_id, agent, stage, token_count
- Log every agent invocation and gate decision
- Audit trail of all state changes

### Metrics & Dashboards (4-5 days)
- Prometheus metrics emission
- Grafana dashboard for:
  - Token consumption per stage
  - Latency p50/p95/p99
  - Retry rates and spike detection
  - Blocker rates by pillar
  - Success/failure rates

### Log Aggregation with Loki (2-3 days)
- Correlate metric spikes to log lines
- Store agent prompts/responses (with access control)
- Query logs by ticket_id

### Distributed Tracing with Tempo (2-3 days)
- OpenTelemetry instrumentation
- Trace each ticket through pipeline
- Spans for each stage, gate, retry
- Identify bottlenecks within a ticket's path

### LLM Trace Viewer (Optional, 3-5 days)
Choose Langfuse vs LangSmith based on:
- Network boundary requirements
- Orchestration framework stability
- Operational cost/effort

## Phase 7: Command Center Dashboard (1-2 weeks)

High-level monitoring and control.

### Feature Layer (2-3 days)
- Group related tickets as Features
- Features grouped as Asks
- Status rollup: Feature complete when all tickets merged

### Status Rollup View (2-3 days)
- Ask view: % complete, feature breakdown
- Feature view: child tickets by status, dependency %, stuck analysis
- Ticket view: current stage, retry count vs. bound, stage time vs. p95

### Stuck Detection (2 days)
- Explicit blocked/needs_human states
- Tickets exceeding retry bounds
- Stage time exceeding p95
- Automatic escalation

### Human Decision Queue UI (2-3 days)
- Separate queue for gate majorss and escalations
- Show artifact + findings side-by-side
- Approve/Reject/Edit/Force-Advance buttons
- Track resolution reasoning

## Phase 8: Evals & Continuous Improvement (2 weeks)

Evaluate and iteratively improve agent quality.

### Offline Eval Set (3-5 days)
- Collect 10-15 past tickets with known-good outcomes
- Store as test cases with ground truth
- Run on every model/prompt change
- Track: plan match %, critique catch-rate, code pass-rate

### Per-Stage Metrics (2-3 days)
- Blocker rate per gate
- False-positive rate (human overrode)
- Retry count distribution
- Detect agent/critic drift over time

### Human Feedback Signal (2-3 days)
- Capture all overrides/rejections to state store
- Periodic batch review: Are critics too strict on style?
- Feed insights back into agent prompts

### Cost-Quality Tracking (2 days)
- Token spend per ticket vs. outcome quality
- Enable tiered matching: cheap models for high-confidence, expensive for hard cases

## Phase 9: Critic Safeguards (1 week)

Prevent rubber-stamping and maintain integrity.

### Structural Safeguards (2-3 days)
- Critic never sees agent reasoning (only artifact)
- Every finding requires specific reference + evidence
- Vague findings auto-rejected with retry
- Clean pass requires active justification

### Monitoring (2-3 days)
- Offline eval set with deliberately flawed artifacts
- Track catch-rate decline (drift signal)
- Highest-stakes tickets get second opinion
- Disagreement routes to human

## Timeline Estimate

- **Phase 1** (Core): ✅ Complete
- **Phase 2** (Gates): 1 week
- **Phase 3** (Impl): 1-2 weeks
- **Phase 4** (QA): 1 week
- **Phase 5** (Human): 1 week
- **Phase 6** (Observability): 2-3 weeks
- **Phase 7** (Dashboard): 1-2 weeks
- **Phase 8** (Evals): 2 weeks
- **Phase 9** (Safeguards): 1 week

**Total: ~12-16 weeks for full system**

## Quick Win Path (2-3 weeks)

For faster MVP, focus on:
1. ✅ Phase 1 — Core pipeline (done)
2. Phase 2 — Plan Critique gate (1 week)
3. Basic Implementation Agent (3-4 days)
4. Phase 4 — Test Executor (2-3 days)
5. Phase 5 — PR Packager (1-2 days)

This gets you a working end-to-end pipeline you can test with before building full observability.

## Success Metrics

- **Ticket throughput**: Tickets per hour from creation to PR ready
- **Agent quality**: % of tickets passing gates first try
- **Cost per ticket**: Total tokens spent normalized by complexity
- **Human time**: % of tickets requiring human intervention
- **System reliability**: Uptime, error rates, recovery from failures
