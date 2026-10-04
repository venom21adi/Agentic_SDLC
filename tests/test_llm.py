import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

import llm
from agents import BusinessAnalysisAgent
from config import Config
from llm import CallRecord, MeteredLLM, TokenBudgetExceeded, UsageMeter, build_chat
from main import AgenticSDLC
from schemas import TicketStatus as S


def msg(content="{}", inp=100, out=50, cached=0, finish="stop"):
    return SimpleNamespace(
        content=content, usage_metadata={"input_tokens": inp, "output_tokens": out, "input_token_details": {"cache_read": cached}},
        response_metadata={"finish_reason": finish})


class FakeChat:
    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def rec(model="deepseek-flash", inp=1000, out=500, cached=0, agent="a", ticket=None, finish="stop"):
    return CallRecord(agent, ticket, model, inp, cached, out, 1.0, finish)


# ---- metering and the cap ----

def test_cost_uses_cache_hit_miss_and_output_rates():
    m = UsageMeter()
    # 600k cached @0.006 + 400k uncached @0.30 + 100k out @1.20  =  3.6c + 120c + 120c  per million
    cost = m.cost_of(rec(inp=1_000_000, cached=600_000, out=100_000))
    assert cost == pytest.approx(0.6 * 0.006 + 0.4 * 0.30 + 0.1 * 1.20)


def test_unknown_model_costs_zero_rather_than_guessing():
    assert UsageMeter().cost_of(rec(model="mystery-1")) == 0.0


def test_summary_breaks_down_by_agent_and_ticket():
    m = UsageMeter()
    for r in (rec(agent="planning", ticket="t1"), rec(agent="planning", ticket="t2"), rec(agent="spec_author", ticket="t1", finish="length")):
        m.add(r)
    s = m.summary()
    assert s["calls"] == 3 and s["total_tokens"] == 4500 and s["truncated_replies"] == 1
    assert s["by_agent"]["planning"]["calls"] == 2 and s["by_ticket"]["t1"]["calls"] == 2


def test_cap_blocks_the_next_call_but_never_the_one_in_flight():
    meter = UsageMeter(cap=300)
    chat = FakeChat(msg(inp=100, out=50), msg(inp=100, out=50), msg(inp=100, out=50))
    wrapped = MeteredLLM(chat, "x", "deepseek-flash", meter)
    wrapped.invoke("1")          # 150 used
    wrapped.invoke("2")          # 300 used: reaches the cap
    with pytest.raises(TokenBudgetExceeded, match="300 of 300"):
        wrapped.invoke("3")
    assert len(chat.prompts) == 2  # the third call never reached the provider


def test_no_cap_means_unlimited():
    meter = UsageMeter(cap=None)
    wrapped = MeteredLLM(FakeChat(msg(inp=10**7, out=10**7)), "x", "deepseek-flash", meter)
    wrapped.invoke("a")
    wrapped.invoke("b")


def test_records_agent_ticket_and_missing_usage_is_tolerated():
    meter = UsageMeter()
    meter.current_ticket = "tick-9"
    MeteredLLM(FakeChat(SimpleNamespace(content="x")), "planning", "deepseek-flash", meter).invoke("p")
    (r,) = meter.records
    assert (r.agent, r.ticket_id, r.input_tokens, r.output_tokens) == ("planning", "tick-9", 0, 0)


# ---- provider construction (no network: ChatOpenAI is replaced by a recorder) ----

class Recorder:
    last = None

    def __init__(self, **kw):
        Recorder.last = SimpleNamespace(kwargs=kw, bound=None)

    def bind(self, **kw):
        Recorder.last.bound = kw
        return self


@pytest.fixture
def recorder(monkeypatch):
    import langchain_openai
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Recorder)
    Recorder.last = None
    return Recorder


def test_deepseek_uses_its_base_url_key_and_json_mode(monkeypatch, recorder):
    monkeypatch.setattr(Config, "LLM_PROVIDER", "deepseek")
    monkeypatch.setattr(Config, "DEEPSEEK_API_KEY", "sk-test-123")
    monkeypatch.setattr(Config, "LLM_JSON_MODE", True)
    build_chat("deepseek-flash", 0.2)
    kw = recorder.last.kwargs
    assert kw["base_url"] == "https://api.deepseek.com" and kw["api_key"] == "sk-test-123"
    assert kw["model"] == "deepseek-flash" and kw["timeout"] == Config.LLM_TIMEOUT_SECONDS
    assert kw["max_retries"] == Config.LLM_MAX_RETRIES and kw["max_tokens"] == Config.LLM_MAX_OUTPUT_TOKENS
    assert recorder.last.bound == {"response_format": {"type": "json_object"}}


def test_json_mode_can_be_switched_off(monkeypatch, recorder):
    monkeypatch.setattr(Config, "LLM_PROVIDER", "deepseek")
    monkeypatch.setattr(Config, "DEEPSEEK_API_KEY", "k")
    monkeypatch.setattr(Config, "LLM_JSON_MODE", False)
    build_chat("deepseek-flash", 0)
    assert recorder.last.bound is None


def test_openai_provider_does_not_get_deepseeks_url(monkeypatch, recorder):
    monkeypatch.setattr(Config, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(Config, "OPENAI_API_KEY", "sk-openai")
    build_chat("gpt-4o", 0)
    assert "base_url" not in recorder.last.kwargs and recorder.last.kwargs["api_key"] == "sk-openai"


@pytest.mark.parametrize("provider,attr,match", [("deepseek", "DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
                                                 ("openai", "OPENAI_API_KEY", "OPENAI_API_KEY")])
def test_missing_key_is_a_clear_error(monkeypatch, recorder, provider, attr, match):
    monkeypatch.setattr(Config, "LLM_PROVIDER", provider)
    monkeypatch.setattr(Config, attr, None)
    with pytest.raises(ValueError, match=match):
        build_chat("m", 0)


def test_unknown_provider_is_rejected(monkeypatch, recorder):
    monkeypatch.setattr(Config, "LLM_PROVIDER", "mystery")
    with pytest.raises(ValueError, match="unknown LLM_PROVIDER"):
        build_chat("m", 0)


def test_critics_run_cold_and_generators_do_not():
    from agents import CodeCritiqueAgent, PlanCritiqueAgent, PlanningAgent
    assert PlanCritiqueAgent.temperature == 0.0 and CodeCritiqueAgent.temperature == 0.0
    assert PlanningAgent.temperature > 0


def test_default_model_is_deepseek_flash_when_the_key_is_present():
    # the repo's .env has a DeepSeek key, and flash is what that account offers
    if Config.LLM_PROVIDER == "deepseek":
        assert Config.PLANNING_MODEL == "deepseek-flash" and Config.CRITIQUE_MODEL == "deepseek-flash"


# ---- a budget stop ends the run cleanly and is not blamed on the ticket ----

def test_token_cap_stops_the_run_without_counting_a_ticket_failure(store, monkeypatch):
    meter = UsageMeter(cap=1)
    monkeypatch.setattr(llm, "METER", meter)
    import main as main_mod
    monkeypatch.setattr(main_mod, "METER", meter)

    class OneCallThenCapped(FakeChat):
        def invoke(self, prompt):
            return msg(json.dumps({"features": []}), inp=5, out=5)   # invalid plan, but it spends the budget

    agent = BusinessAnalysisAgent(store, MeteredLLM(OneCallThenCapped(msg()), "business_analysis", "deepseek-flash", meter))
    sdlc = AgenticSDLC(store, {"business_analysis": agent})
    ask = sdlc.create_ticket("big", "do a lot")
    asyncio.run(sdlc.run_pipeline(max_iterations=20))

    t = store.get_ticket(ask.id)
    assert sdlc.stop_reason == "token_cap"
    assert t.status is S.CREATED and t.retry_count == 1   # only the real invalid-plan attempt counted; the cap stop did not
    assert sdlc.usage()["total_tokens"] == 10 and sdlc.usage()["cap"] == 1
