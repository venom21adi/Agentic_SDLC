"""Two tiny real model calls to confirm the provider setup works (costs well under a cent).

    python smoke_llm.py

1. A trivial JSON round trip: key, base URL, model name and JSON mode all work.
2. The real Business Analysis agent on a small ask, using an in-memory database: the actual prompt and
   validator against the real model.
"""
import json
import sys
import uuid

from agents import BusinessAnalysisAgent
from config import Config
from llm import METER, make_llm
from schemas import Ticket
from state_store import StateStore


def main() -> int:
    print(f"provider={Config.LLM_PROVIDER} model={Config.PLANNING_MODEL} json_mode={Config.LLM_JSON_MODE} cap={Config.RUN_TOKEN_CAP:,}")
    METER.reset(Config.RUN_TOKEN_CAP)

    # 1. trivial round trip
    reply = make_llm("smoke", Config.PLANNING_MODEL, 0).invoke('Reply with a JSON object {"ok": true, "word": "<any single word>"}.')
    try:
        parsed = json.loads(reply.content)
    except json.JSONDecodeError:
        print("FAIL 1: reply was not valid JSON:", reply.content[:200])
        return 1
    print("PASS 1: JSON round trip ->", parsed)

    # 2. the real BA agent on a small ask
    store = StateStore("sqlite://")
    store.init_db()
    ask = store.create_ticket(Ticket(
        id=str(uuid.uuid4()), title="CSV to JSON converter",
        description="A command-line tool that converts CSV files to JSON, with a --pretty flag and clear errors for malformed rows."))
    result = BusinessAnalysisAgent(store).run(ask)
    if "error" in result:
        print("FAIL 2: Business Analysis was rejected:", result["error"])
        return 1
    print(f"PASS 2: decomposed into {result['features']} feature(s), {result['tickets']} ticket(s), {result['edges']} dependency edge(s)")
    titles = {t.id: t.title for t in store.get_children(ask.id)}
    for f in store.get_features_for_ask(ask.id):
        print("  feature:", f.name)
        for tid in f.ticket_ids:
            deps = [titles[p] for p in store.get_dag_predecessors(tid)]
            print("    -", titles[tid], ("  (after: " + ", ".join(deps) + ")") if deps else "")
    if result.get("open_questions"):
        print("  open questions:", result["open_questions"])

    u = METER.summary()
    print(f"usage: {u['calls']} calls, {u['input_tokens']:,} in / {u['output_tokens']:,} out tokens, ~${u['cost_usd']:.5f} (estimate), truncated replies: {u['truncated_replies']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
