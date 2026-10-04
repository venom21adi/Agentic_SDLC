import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from state_store import StateStore  # noqa: E402


class FakeLLM:
    """Stands in for ChatOpenAI; returns canned JSON."""

    class _Msg:
        def __init__(self, content):
            self.content = content

    def __init__(self, reply='{"assumptions": "none", "approach": "x"}'):
        self.reply = reply
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        return self._Msg(self.reply)


@pytest.fixture
def store():
    s = StateStore("sqlite://")
    s.init_db()
    return s
