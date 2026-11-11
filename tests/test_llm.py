from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from dropship.db import connect, llm_tokens_today, record_llm_usage, start_run
from dropship.llm import LLM, LLMBudgetExceeded, LLMUnusable


class Out(BaseModel):
    answer: str


class FakeMessages:
    def __init__(self, owner, response):
        self.owner = owner
        self.response = response

    def parse(self, **kw):
        self.owner.calls.append(kw)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeClient:
    def __init__(self, parsed=None, stop_reason="end_turn", in_tok=100, out_tok=20, error=None):
        self.calls: list[dict] = []
        self.beta_calls: list[dict] = []
        resp = error or SimpleNamespace(
            stop_reason=stop_reason,
            parsed_output=parsed,
            usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
        )
        self.messages = FakeMessages(self, resp)
        beta_owner = SimpleNamespace(calls=self.beta_calls)
        self.beta = SimpleNamespace(messages=FakeMessages(beta_owner, resp))


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def make(conn, client, budget=1000):
    return LLM(client, conn, start_run(conn), budget)


def call(llm, **kw):
    return llm.parse("claude-haiku-5-5", "SYS", "hi", Out, "low", **kw)


def test_budget_blocks_call(conn):
    client = FakeClient(Out(answer="x"))
    llm = make(conn, client, budget=100)
    record_llm_usage(conn, llm.run_id, "m", 60, 40)
    with pytest.raises(LLMBudgetExceeded):
        call(llm)
    assert client.calls == [] and client.beta_calls == []
    assert llm_tokens_today(conn) == 100


def test_refusal_raises_unusable(conn):
    client = FakeClient(None, stop_reason="refusal")
    llm = make(conn, client)
    with pytest.raises(LLMUnusable):
        call(llm)
    assert llm_tokens_today(conn) == 120  # tokens were spent


def test_none_parsed_output_raises_unusable(conn):
    with pytest.raises(LLMUnusable):
        call(make(conn, FakeClient(None)))


def test_validation_error_raises_unusable(conn):
    try:
        Out.model_validate({})
    except Exception as e:  # pydantic ValidationError
        err = e
    with pytest.raises(LLMUnusable):
        call(make(conn, FakeClient(error=err)))


def test_api_errors_propagate(conn):
    with pytest.raises(RuntimeError):
        call(make(conn, FakeClient(error=RuntimeError("rate limited"))))


def test_usage_recorded(conn):
    llm = make(conn, FakeClient(Out(answer="x"), in_tok=123, out_tok=45))
    assert call(llm) == Out(answer="x")
    assert llm_tokens_today(conn) == 168


def test_system_prompt_cached(conn):
    client = FakeClient(Out(answer="x"))
    call(make(conn, client))
    kw = client.calls[0]
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["system"][0]["text"] == "SYS"
    assert kw["output_config"] == {"effort": "low"}
    assert kw["output_format"] is Out
    assert kw["messages"] == [{"role": "user", "content": "hi"}]


def test_fallbacks_routes_to_beta(conn):
    client = FakeClient(Out(answer="x"))
    call(make(conn, client), fallbacks=True)
    assert client.calls == []
    kw = client.beta_calls[0]
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["fallbacks"] == "default"


def test_no_sampling_params_sent(conn):
    for fb in (False, True):
        client = FakeClient(Out(answer="x"))
        call(make(conn, client), fallbacks=fb)
        kw = (client.beta_calls if fb else client.calls)[0]
        for bad in ("temperature", "top_p", "top_k", "thinking", "budget_tokens"):
            assert bad not in kw
