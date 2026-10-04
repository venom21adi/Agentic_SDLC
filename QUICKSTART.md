# Quick Start Guide

Get the Agentic SDLC system running in 5 minutes.

## Prerequisites

- ✅ Python 3.12 or higher
- ✅ Docker Desktop (running)
- ✅ OpenAI API key (from https://platform.openai.com)

## Step-by-Step Setup

### 1. Start PostgreSQL

```bash
docker-compose up -d
```

Verify it's running:
```bash
docker ps | grep agentic-sdlc-postgres
```

You should see: `agentic-sdlc-postgres   postgres:15-alpine`

### 2. Create Environment File

```bash
cp .env.example .env
```

Edit `.env` and add your OpenAI API key:
```bash
OPENAI_API_KEY=sk-... (your key here)
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Initialize Database

```bash
python setup.py
```

This will:
- Create the `.env` file (if needed)
- Verify PostgreSQL is running
- Create all database tables

You should see: `✅ Database connection successful and tables created`

### 5. Run the Pipeline

```bash
python main.py
```

You should see output like:
```
2026-01-15 10:30:45 - root - INFO - Starting SDLC pipeline
2026-01-15 10:30:45 - root - INFO - Created ticket: <uuid>
2026-01-15 10:30:46 - agents.business_analysis - INFO - Running business analysis for ticket <uuid>
2026-01-15 10:30:48 - root - INFO - Advanced ticket <uuid> to approved
...
```

## What Just Happened?

The pipeline:
1. Created a sample ticket with a user auth system requirement
2. Ran Business Analysis Agent → Extracted key features
3. Advanced ticket to APPROVED status
4. Ran Planning Agent → Created implementation plan
5. Advanced ticket to SPEC_AUTHORING status
6. Ran Spec Author Agent → Wrote technical specification
7. Advanced ticket to PLAN_CRITIQUE status
8. Stopped (Plan Critique Agent not yet implemented)

## Explore the Data

### PostgreSQL Query

```bash
docker exec -it agentic-sdlc-postgres psql -U sdlc -d agentic_sdlc
```

List all tickets:
```sql
SELECT id, title, status FROM tickets;
```

View a ticket's plan:
```sql
SELECT * FROM plans WHERE ticket_id = '<your-ticket-id>';
```

Exit: `\q`

### Python Script

Check ticket status programmatically:
```python
from state_store import StateStore
from config import Config

store = StateStore(Config.DATABASE_URL)
tickets = store.get_all_tickets()
for ticket in tickets:
    print(f"{ticket.id}: {ticket.title} → {ticket.status}")
```

## Next Steps

### 1. Build More Agents

The system currently has stub implementations for the first 3 agents. The most impactful next stage is **Plan Critique** — the first quality gate.

Create `agents/plan_critique.py`:
```python
from agents.base import BaseAgent
from state_store import StateStore
from schemas import Ticket

class PlanCritiqueAgent(BaseAgent):
    def __init__(self, state_store: StateStore):
        super().__init__(state_store, "plan_critique")
        self.llm = ChatOpenAI(model="gpt-4-turbo-preview")
    
    def run(self, ticket: Ticket) -> dict:
        # Get the plan and spec from store
        # Run 4-pillar critique (perf, security, scale, maintainability)
        # Return blocker/major/nitpick findings
        pass
```

Then register it in `main.py`:
```python
self.agents = {
    ...
    "plan_critique": PlanCritiqueAgent(self.state_store),
}
```

### 2. Add Test Coverage

Create `tests/test_state_store.py`:
```python
import pytest
from state_store import StateStore
from schemas import Ticket, TicketStatus

def test_create_ticket():
    store = StateStore()
    ticket = Ticket(id="test-1", title="Test", description="Desc")
    saved = store.create_ticket(ticket)
    assert saved.id == "test-1"
```

Run tests:
```bash
pytest -v
```

### 3. Add Logging & Monitoring

Currently logs go to stdout. Add:
- Structured JSON logging to a file
- Prometheus metrics for token usage and latency
- Correlation IDs for tracing ticket flow

### 4. Build the Command Center

Create a simple web dashboard showing:
- All tickets by status
- Stuck detection
- Feature groupings
- Human decision queue

Example using FastAPI:
```python
from fastapi import FastAPI
from state_store import StateStore

app = FastAPI()
store = StateStore()

@app.get("/api/tickets")
def list_tickets():
    return store.get_all_tickets()

@app.get("/api/status/{ticket_id}")
def get_status(ticket_id: str):
    return store.get_ticket(ticket_id)
```

## Troubleshooting

### `psycopg2` connection error
```
Ensure PostgreSQL is running:
docker-compose up -d

Check container logs:
docker logs agentic-sdlc-postgres
```

### `OPENAI_API_KEY` not set
```
Add your key to .env:
OPENAI_API_KEY=sk-...

Restart your Python session
```

### Database already initialized?
```
Drop and recreate:
docker-compose down
docker volume rm agentic-sdlc_postgres_data
docker-compose up -d
python setup.py
```

## File Reference

- **`main.py`** — Entry point, Pipeline coordinator
- **`state_store.py`** — PostgreSQL persistence layer
- **`orchestrator.py`** — Scheduling and routing logic
- **`agents/`** — Individual agent implementations
- **`schemas.py`** — Pydantic models for all entities
- **`config.py`** — Configuration and environment
- **`ARCHITECTURE.md`** — Detailed system design
- **`docs/agentic-sdlc-concept-v3.html`** — Full concept document

## Key Concepts

**Ticket**: Atomic unit of work (e.g., "Build user auth system")

**Status**: Ticket's position in pipeline (CREATED → ... → MERGED)

**Agent**: LLM-powered worker that processes one stage

**Gate**: Quality checkpoint (Plan Critique, Code Critique) with retry logic

**Orchestrator**: Routes tickets based on status and dependencies

**State Store**: Central database holding all artifacts (plans, specs, critiques, code, tests)

**DAG**: Dependency graph defining which tickets must complete before others start

For more details, see `ARCHITECTURE.md`
