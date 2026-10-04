# Agentic SDLC - System Architecture

## System Layers

```
┌─────────────────────────────────────────────────────────────┐
│                    Pipeline Coordinator                      │
│                      (main.py)                               │
│              - Ticket creation & management                  │
│              - Pipeline execution loop                       │
└────────────────┬────────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────────────┐
│                    Orchestrator                              │
│              (orchestrator.py)                               │
│   - Stateless scheduler                                      │
│   - Dependency resolution (DAG traversal)                    │
│   - Status → Agent routing                                   │
│   - Gate decision handling                                   │
│   - Retry bounds enforcement                                │
└────────────────┬────────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────────────┐
│               Agent Implementations                          │
│                  (agents/)                                   │
│                                                              │
│  ┌─────────────┐  ┌──────────┐  ┌──────────────┐           │
│  │  Business   │  │Planning  │  │Spec Author   │           │
│  │ Analysis    │  │Agent     │  │Agent         │           │
│  └─────────────┘  └──────────┘  └──────────────┘           │
│                                                              │
│  ┌─────────────┐  ┌──────────┐  ┌──────────────┐           │
│  │Implementation│ │Test      │  │QA Strategy   │           │
│  │Agents       │  │Executor  │  │Agent         │           │
│  └─────────────┘  └──────────┘  └──────────────┘           │
└────────────────┬────────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────────────┐
│                  State Store                                 │
│              (state_store.py)                               │
│                                                              │
│   ┌──────────┐  ┌───────┐  ┌──────────┐  ┌──────────┐     │
│   │ Tickets  │  │ Plans │  │ Specs    │  │Critiques │     │
│   └──────────┘  └───────┘  └──────────┘  └──────────┘     │
│   ┌──────────┐  ┌───────┐  ┌──────────┐  ┌──────────┐     │
│   │Code Arty │  │Tests  │  │Features  │  │DAG Edges │     │
│   └──────────┘  └───────┘  └──────────┘  └──────────┘     │
└────────────────┬────────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────────────┐
│              PostgreSQL Database                             │
│         (Docker container)                                  │
│                                                              │
│  - Persists all entities                                    │
│  - Transaction-safe updates                                 │
│  - Query-based dependency resolution                        │
└─────────────────────────────────────────────────────────────┘
```

## Data Flow

### Linear Flow (Happy Path)

```
Ticket: CREATED
    ↓
Business Analysis Agent
    ↓ writes to Store
Ticket: APPROVED
    ↓
Planning Agent
    ↓ writes Plan to Store
Ticket: SPEC_AUTHORING
    ↓
Spec Author Agent
    ↓ writes Specification to Store
Ticket: PLAN_CRITIQUE
    ↓
Plan Critique Gate
    ├─ if blockers → Ticket: SPEC_AUTHORING (retry)
    └─ if no blockers → Ticket: PLAN_APPROVED
    ↓
Implementation Agent
    ↓ writes CodeArtifact to Store
Ticket: CODE_CRITIQUE
    ↓
Code Critique Gate
    ├─ if blockers → Ticket: IMPLEMENTATION (retry)
    └─ if no blockers → Ticket: QA_STRATEGY
    ↓
QA Strategy Agent
    ↓ writes TestStrategy to Store
Ticket: TESTING
    ↓
Test Executor Agent
    ↓ writes TestResults to Store
Ticket: PR_READY
    ↓
PR Packager (Human)
    ↓
Ticket: MERGED
```

### Retry Loop

```
Gate: Finds blockers
    ↓
Increment retry_count
    ↓
retry_count < MAX_RETRIES?
    ├─ YES → Send back to previous stage
    └─ NO → Escalate to Human Decision Queue
```

## State Store Schema

### Tickets (Primary Entity)
- id, title, description
- status (enum: CREATED, APPROVED, SPEC_AUTHORING, ...)
- feature_id (grouping for dashboard)
- retry_count, max_retries
- created_at, updated_at

### Plans
- id, ticket_id (FK)
- content (JSON), assumptions (text)
- created_at

### Specifications
- id, ticket_id (FK), plan_id (FK)
- content (JSON: interfaces, data models, edge cases)
- created_at

### Critiques (Plan & Code)
- id, ticket_id (FK), artifact_id (FK)
- items (JSON array of CritiqueItem)
  - pillar (PERFORMANCE, SECURITY, SCALABILITY, MAINTAINABILITY)
  - severity (BLOCKER, MAJOR, NITPICK)
  - finding, evidence, resolved

### Code Artifacts
- id, ticket_id (FK)
- branch_name, files_changed (JSON), commit_hash
- created_at

### Test Strategy & Results
- TestStrategy: id, ticket_id, scope, priorities, risk_areas
- TestResult: id, ticket_id, strategy_id, unit/integration/playwright results

### DAG (Dependency Graph)
- source_ticket_id, target_ticket_id (primary key pair)
- reasoning (why this dependency exists)

### Features & Human Decisions
- Feature: id, name, description, ticket_ids (JSON)
- HumanDecision: id, ticket_id, stage, decision_type, reasoning, created_by

## Key Design Principles

### 1. Shared State Store Backbone
- No direct agent-to-agent communication
- All inputs read from store, all outputs written to store
- Enables full auditability and critic integrity

### 2. Stateless Orchestrator
- No local memory between invocations
- All routing logic determined from store state
- Can crash, restart, or scale to multiple instances safely

### 3. Dependency-Aware Scheduling
- DAG edges define strict ordering constraints
- Ticket only processes after all predecessors are merged
- Prevents out-of-order execution of dependent features

### 4. Quality Gates with Bounded Retries
- Two critical gates: Plan Critique, Code Critique
- Each stage can retry up to MAX_RETRIES times
- After limit exceeded: escalate to human decision queue
- Prevents infinite loops on stuck tickets

### 5. Agent Specialization
- Each agent focuses on one stage
- Uses LLM for reasoning within scope
- Clear input/output contracts
- Easy to replace or upgrade individual agents

## Extensibility Points

### Add New Agent
1. Create `agents/my_agent.py` extending `BaseAgent`
2. Implement `run(ticket: Ticket) -> dict`
3. Register in orchestrator dispatch table
4. Add corresponding TicketStatus enum value

### Add New Gate
1. Create gate agent (e.g., `agents/my_critique.py`)
2. Add CritiqueItem logic to evaluate artifact
3. Implement boolean "has_blockers" logic
4. Register in orchestrator gate decision logic

### Add New Artifact Type
1. Define Pydantic schema in `schemas.py`
2. Create SQLAlchemy ORM in `state_store.py`
3. Add CRUD methods to StateStore class
4. Update agent to create/read artifact

### Add Observability
1. **Metrics**: Add Prometheus emission to orchestrator and agents
2. **Logs**: Structured JSON logging with ticket_id correlation
3. **Traces**: OpenTelemetry instrumentation of orchestrator + agents
4. **Dashboards**: Query store for command center views (status rollup, stuck detection)

## Current Implementation Status

### ✅ Completed
- [x] Core schemas (Pydantic models for all entities)
- [x] State store (PostgreSQL persistence layer)
- [x] Orchestrator (stateless scheduling logic)
- [x] Base agent structure
- [x] Business Analysis Agent (stub)
- [x] Planning Agent (stub)
- [x] Spec Author Agent (stub)
- [x] Docker compose for PostgreSQL
- [x] Pipeline coordinator (main.py)

### 🔄 In Progress / Next
- [ ] Plan Critique Agent (quality gate)
- [ ] Code Critique Agent (quality gate)
- [ ] Implementation Agents (frontend, backend, schema)
- [ ] Test Strategy Agent
- [ ] Test Executor Agent
- [ ] PR Packager Agent
- [ ] Human Decision Queue UI
- [ ] Command Center Dashboard
- [ ] Observability Stack (Prometheus, Grafana, Loki, Tempo)
- [ ] Offline Eval Sets
- [ ] Per-Stage Metrics
- [ ] Critic Safeguards

## Running the System

```bash
# Start database
docker-compose up -d

# Initialize and run
python setup.py      # One-time setup
python main.py       # Run pipeline
```

## Monitoring

### Current Logging
- All operations logged to stdout with timestamp, logger, level, message
- Ticket ID included in all agent logs for tracing

### Future Observability
- Prometheus metrics for token spend, latency, retry rates
- Grafana dashboards for real-time health
- Loki for log aggregation and correlation
- Tempo for distributed traces across pipeline
- Command center dashboard for Ask/Feature/Ticket status
