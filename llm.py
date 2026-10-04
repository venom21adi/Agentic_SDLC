"""LLM access for every agent: provider selection, usage metering and a hard token cap.

DeepSeek's API is OpenAI-compatible, so both providers go through langchain-openai's ChatOpenAI;
only the base URL, key and a few options differ. Every call is metered so runs can be costed and capped.
"""
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from config import Config

logger = logging.getLogger(__name__)

# USD per 1M tokens: (input cache hit, input cache miss, output). PEAK-hour list prices (the conservative
# end; off-peak is half) from api-docs.deepseek.com/quick_start/pricing. An estimate, not a bill.
PRICES = {
    "deepseek-flash": (0.006, 0.30, 1.20),
    "deepseek-v4-pro": (0.044, 1.32, 3.96),
}


class TokenBudgetExceeded(RuntimeError):
    """The run has used its token allowance; no further model calls are made."""


@dataclass
class CallRecord:
    agent: str
    ticket_id: Optional[str]
    model: str
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    seconds: float
    finish_reason: Optional[str]


class UsageMeter:
    def __init__(self, cap: Optional[int] = None):
        self.cap = cap
        self.records: list[CallRecord] = []
        self.current_ticket: Optional[str] = None
        self._lock = threading.Lock()

    def reset(self, cap: Optional[int] = None) -> None:
        with self._lock:
            self.records.clear()
            self.current_ticket = None
            if cap is not None:
                self.cap = cap

    @property
    def total_tokens(self) -> int:
        return sum(r.input_tokens + r.output_tokens for r in self.records)

    def check(self) -> None:
        if self.cap and self.total_tokens >= self.cap:
            raise TokenBudgetExceeded(f"token cap reached: {self.total_tokens:,} of {self.cap:,} tokens used")

    def add(self, record: CallRecord) -> None:
        with self._lock:
            self.records.append(record)

    @staticmethod
    def cost_of(r: CallRecord) -> float:
        hit, miss, out = PRICES.get(r.model, (0.0, 0.0, 0.0))
        uncached = max(r.input_tokens - r.cached_tokens, 0)
        return (r.cached_tokens * hit + uncached * miss + r.output_tokens * out) / 1_000_000

    def summary(self) -> dict:
        """Totals plus breakdowns by agent and by ticket. Cost is an estimate (unknown models count as $0)."""
        def roll(key):
            out: dict = {}
            for r in self.records:
                d = out.setdefault(key(r) or "-", {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "seconds": 0.0})
                d["calls"] += 1
                d["input_tokens"] += r.input_tokens
                d["output_tokens"] += r.output_tokens
                d["cost_usd"] = round(d["cost_usd"] + self.cost_of(r), 6)
                d["seconds"] = round(d["seconds"] + r.seconds, 2)
            return out
        return {
            "calls": len(self.records),
            "input_tokens": sum(r.input_tokens for r in self.records),
            "output_tokens": sum(r.output_tokens for r in self.records),
            "total_tokens": self.total_tokens,
            "cap": self.cap,
            "cost_usd": round(sum(self.cost_of(r) for r in self.records), 6),
            "truncated_replies": sum(1 for r in self.records if r.finish_reason == "length"),
            "by_agent": roll(lambda r: r.agent),
            "by_ticket": roll(lambda r: r.ticket_id),
        }


METER = UsageMeter(cap=Config.RUN_TOKEN_CAP)


class MeteredLLM:
    """Wraps a chat model: enforces the token cap before each call and records usage after it."""

    def __init__(self, chat, agent: str, model: str, meter: UsageMeter):
        self.chat, self.agent, self.model, self.meter = chat, agent, model, meter

    def invoke(self, prompt):
        self.meter.check()
        start = time.monotonic()
        msg = self.chat.invoke(prompt)
        usage = getattr(msg, "usage_metadata", None) or {}
        details = usage.get("input_token_details") or {}
        meta = getattr(msg, "response_metadata", None) or {}
        record = CallRecord(
            agent=self.agent, ticket_id=self.meter.current_ticket, model=self.model,
            input_tokens=int(usage.get("input_tokens", 0)), cached_tokens=int(details.get("cache_read", 0)),
            output_tokens=int(usage.get("output_tokens", 0)), seconds=time.monotonic() - start,
            finish_reason=meta.get("finish_reason"),
        )
        self.meter.add(record)
        if record.finish_reason == "length":
            logger.warning("%s: reply hit the output token limit and is probably truncated", self.agent)
        return msg


def build_chat(model: str, temperature: float):
    """The raw chat client for the configured provider. Raises ValueError if its key is missing."""
    from langchain_openai import ChatOpenAI

    kwargs = dict(
        model=model, temperature=temperature, timeout=Config.LLM_TIMEOUT_SECONDS,
        max_retries=Config.LLM_MAX_RETRIES, max_tokens=Config.LLM_MAX_OUTPUT_TOKENS,
    )
    if Config.LLM_PROVIDER == "deepseek":
        if not Config.DEEPSEEK_API_KEY:
            raise ValueError("DEEPSEEK_API_KEY is not set (add it to .env)")
        kwargs.update(api_key=Config.DEEPSEEK_API_KEY, base_url=Config.DEEPSEEK_BASE_URL)
    elif Config.LLM_PROVIDER == "openai":
        if not Config.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY is not set (add it to .env)")
        kwargs.update(api_key=Config.OPENAI_API_KEY)
    else:
        raise ValueError(f"unknown LLM_PROVIDER {Config.LLM_PROVIDER!r} (expected 'deepseek' or 'openai')")

    chat = ChatOpenAI(**kwargs)
    if Config.LLM_JSON_MODE:
        # Guarantees syntactically valid JSON. Every prompt in this project asks for a JSON object,
        # which the API requires for this mode.
        chat = chat.bind(response_format={"type": "json_object"})
    return chat


def make_llm(agent: str, model: str, temperature: float = 0.2, meter: Optional[UsageMeter] = None) -> MeteredLLM:
    return MeteredLLM(build_chat(model, temperature), agent, model, meter or METER)
