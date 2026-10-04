import json
import re
from abc import ABC, abstractmethod
from typing import Any, Optional, TypedDict
from langgraph.graph import StateGraph, START, END
from state_store import StateStore
from schemas import Ticket


class AgentState(TypedDict, total=False):
    ticket: Ticket
    result: dict


class BaseAgent(ABC):
    model_name: Optional[str] = None  # override per agent; falls back to Config.PLANNING_MODEL

    def __init__(self, state_store: StateStore, name: str, llm: Optional[Any] = None):
        self.state_store = state_store
        self.name = name
        self._llm = llm
        self._graph = None

    @property
    def llm(self):
        """Chat model; built lazily so tests can inject a fake and skip needing an API key."""
        if self._llm is None:
            from langchain_openai import ChatOpenAI
            from config import Config
            self._llm = ChatOpenAI(model=self.model_name or Config.PLANNING_MODEL, api_key=Config.OPENAI_API_KEY)
        return self._llm

    @abstractmethod
    def run(self, ticket: Ticket) -> dict:
        """Do this stage's work: read inputs from the store, write outputs to the store."""

    def create_graph(self):
        graph = StateGraph(AgentState)
        graph.add_node(self.name, self._run_node)
        graph.add_edge(START, self.name)
        graph.add_edge(self.name, END)
        return graph.compile()

    def _run_node(self, state: AgentState) -> AgentState:
        return {"result": self.run(state["ticket"])}

    def invoke(self, ticket: Ticket) -> dict:
        """Execute the stage through its LangGraph graph."""
        if self._graph is None:
            self._graph = self.create_graph()
        return self._graph.invoke({"ticket": ticket})["result"]

    def ask_json(self, prompt: str) -> dict:
        """Call the LLM and parse a JSON object from the reply; fall back to {"raw": text}."""
        text = self.llm.invoke(prompt).content
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        return {"raw": text}
