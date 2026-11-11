from __future__ import annotations

import sqlite3

import anthropic
from pydantic import BaseModel, ValidationError

from dropship.db import llm_tokens_today, record_llm_usage

FALLBACK_BETAS = ["server-side-fallback-2026-07-01"]


class LLMBudgetExceeded(Exception):
    """The daily token budget is spent; no call was made."""


class LLMUnusable(Exception):
    """The model refused or returned output that does not match the schema."""


class LLM:
    def __init__(
        self, client: anthropic.Anthropic, conn: sqlite3.Connection, run_id: int, daily_budget: int
    ) -> None:
        self.client = client
        self.conn = conn
        self.run_id = run_id
        self.daily_budget = daily_budget

    def parse(
        self,
        model: str,
        system: str,
        user: str,
        schema: type[BaseModel],
        effort: str,
        max_tokens: int = 4000,
        fallbacks: bool = False,
    ) -> BaseModel:
        if llm_tokens_today(self.conn) >= self.daily_budget:
            raise LLMBudgetExceeded(
                f"daily LLM token budget of {self.daily_budget} reached"
            )
        kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            output_config={"effort": effort},
        )
        try:
            if fallbacks:
                response = self.client.beta.messages.parse(
                    betas=FALLBACK_BETAS, fallbacks="default", **kwargs
                )
            else:
                response = self.client.messages.parse(**kwargs)
        except ValidationError as e:
            raise LLMUnusable(f"output failed validation: {e.error_count()} error(s)") from e
        usage = response.usage
        record_llm_usage(
            self.conn, self.run_id, model, usage.input_tokens, usage.output_tokens
        )
        if response.stop_reason == "refusal":
            raise LLMUnusable("model refused the request")
        if response.parsed_output is None:
            raise LLMUnusable("no parsed output (stop_reason=%s)" % response.stop_reason)
        return response.parsed_output
