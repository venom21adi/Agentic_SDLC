# Agentic SDLC: Multi-Agent Software Development Pipeline

A Python/LangGraph-based orchestration system that automates software development using AI agents working through a 9-stage pipeline.

## Architecture Overview

The system implements the Agentic SDLC concept with:

- **9 Pipeline Stages**: Business analysis → Planning → Spec authoring → Plan critique → Implementation → Code critique → QA strategy → Testing → PR review
- **Quality Gates**: Automated critique stages that validate plans and code before advancing
- **Shared State Store**: PostgreSQL-backed persistence for all artifacts (tickets, plans, specs, critiques, code, tests)
- **Stateless Orchestrator**: Routes tickets through stages based on dependencies and status
- **Agent Workers**: Specialized LLM agents for each stage

## Quick Start

### Prerequisites

- Python 3.12+
- Docker & Docker Desktop (for PostgreSQL)
- OpenAI API key

### Setup

1. **Clone and navigate to the project**
   ```bash
   cd c:\Agentic_SDLC
   ```

2. **Start PostgreSQL**
   ```bash
   docker-compose up -d
   ```

3. **Create environment file**
   ```bash
   cp .env.example .env
   # Edit .env and add your OPENAI_API_KEY
   ```

4. **Install dependencies**
   ```bash
   pip install -e .
   ```

5. **Run the pipeline**
   ```bash
   python main.py
   ```

## Project Structure

```
agentic-sdlc/
├── config.py              # Configuration and environment
├── schemas.py             # Pydantic models for all entities
├── state_store.py         # PostgreSQL persistence layer
├── orchestrator.py        # Stateless scheduler and router
├── main.py                # Entry point and pipeline runner
├── agents/
│   ├── base.py           # Base agent class
│   ├── business_analysis.py
│   ├── planning.py
│   └── spec_author.py
├── docker-compose.yml    # PostgreSQL setup
└── README.md
```

## Core Components

### State Store (`state_store.py`)
- Manages all persistent data in PostgreSQL
- Tables: tickets, plans, specs, critiques, code artifacts, test results, DAG edges
- All agents read inputs from and write outputs to this store
- No direct agent-to-agent communication

### Orchestrator (`orchestrator.py`)
- Stateless scheduling logic
- Routes tickets based on dependencies and current status
- Enforces retry bounds and escalation to human decision queue
- Respects DAG dependencies: only processes tickets whose predecessors are merged

### Agents (`agents/`)
- **BusinessAnalysisAgent**: Decomposes raw requirements into atomic tickets with dependency DAG
- **PlanningAgent**: Designs implementation approach, documents assumptions
- **SpecAuthorAgent**: Writes detailed technical specifications from plans

## Pipeline Stages

1. **Business Analysis** → Extract tickets + DAG from raw requirements
2. **Planning** → Design implementation approach
3. **Spec Author** → Write technical specification
4. **Plan Critique** (Gate) → Validate plan against 4 pillars (perf, security, scale, maintainability)
5. **Implementation** → Write code on isolated branch
6. **Code Critique** (Gate) → Validate code quality
7. **QA Strategy** → Plan test coverage and risk areas
8. **Test & Verify** → Execute tests and gather evidence
9. **PR Packaging** → Human review and merge

## Ticket Statuses

- `created` → `approved` → `spec_authoring` → `plan_critique` → `plan_approved`
- → `implementation` → `code_critique` → `qa_strategy` → `testing` → `pr_ready` → `merged`

At each gate, if blockers are found, the ticket retries up to `MAX_RETRIES_PER_GATE` times before escalating to human review.

## Data Flow

All communication between agents flows through the shared state store:

```
Agent 1 → Writes to Store → Orchestrator reads → Routes to Agent 2
Agent 2 → Writes to Store → Orchestrator reads → Routes to Agent 3
```

This design ensures:
- Full auditability (everything is persisted)
- Critic integrity (critics never see agent reasoning, only artifacts)
- Scalability (agents can run in parallel, orchestrator is stateless)

## Environment Variables

```bash
DATABASE_URL=postgresql+psycopg2://sdlc:sdlc_password@localhost:5432/agentic_sdlc
OPENAI_API_KEY=your-api-key
LOG_LEVEL=INFO
```

## Development

### Add a New Agent

1. Create `agents/your_agent.py` extending `BaseAgent`
2. Implement the `run(ticket: Ticket)` method
3. Register in `main.py`'s `AgenticSDLC.agents` dict
4. Map the ticket status to the agent in `Orchestrator.dispatch_ticket()`

### Add a New Stage

1. Create the corresponding TicketStatus enum value
2. Create the agent implementation
3. Update `orchestrator.py` stage maps
4. Add ORM and schema models for any new artifacts

## Next Steps

- [ ] Implement plan critique agent (quality gate #1)
- [ ] Implement code critique agent (quality gate #2)
- [ ] Implement implementation agents (frontend, backend, schema)
- [ ] Implement test strategy and test executor agents
- [ ] Add command center (monitoring dashboard)
- [ ] Add structured logging and observability (Prometheus, Grafana, Loki, Tempo)
- [ ] Add offline eval sets and per-stage metrics
- [ ] Add human decision queue UI

## References

- See `docs/agentic-sdlc-concept-v3.html` for the full system design
