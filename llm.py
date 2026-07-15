"""Small, provider-isolated Ollama client used by the NL-to-SQL layer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol


class LLMUnavailable(RuntimeError):
    pass


class LLMOutputError(ValueError):
    pass


@dataclass(frozen=True)
class SQLProposal:
    answerable: bool
    sql: str | None
    reason: str | None
    token_count: int
    raw: str


@dataclass(frozen=True)
class AnswerResult:
    answer: str
    token_count: int


class LLMClient(Protocol):
    async def propose_sql(self, system_prompt: str, user_prompt: str) -> SQLProposal: ...

    async def summarize(self, system_prompt: str, user_prompt: str) -> AnswerResult: ...


class OllamaClient:
    """HTTP adapter for a local/private Ollama deployment.

    The rest of the application only depends on ``LLMClient``. A cloud or
    tenant-specific provider can therefore replace this class without changing
    prompt composition or SQL safety controls.
    """

    def __init__(self, base_url: str, model: str, timeout_seconds: float = 45) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    async def _chat(self, system_prompt: str, user_prompt: str) -> tuple[str, int]:
        import httpx

        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(f"{self.base_url}/api/chat", json=payload)
                response.raise_for_status()
                body = response.json()
            content = str(body["message"]["content"])
            tokens = int(body.get("prompt_eval_count", 0)) + int(
                body.get("eval_count", 0)
            )
            return content, tokens
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise LLMUnavailable(f"LLM provider request failed: {exc}") from exc

    @staticmethod
    def _json_object(raw: str) -> dict[str, Any]:
        content = raw.strip()
        if content.startswith("```"):
            lines = content.splitlines()
            content = "\n".join(lines[1:-1])
            if content.lstrip().startswith("json"):
                content = content.lstrip()[4:].lstrip()
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMOutputError(f"model returned invalid JSON: {exc.msg}") from exc
        if not isinstance(parsed, dict):
            raise LLMOutputError("model response must be a JSON object")
        return parsed

    async def propose_sql(self, system_prompt: str, user_prompt: str) -> SQLProposal:
        raw, tokens = await self._chat(system_prompt, user_prompt)
        parsed = self._json_object(raw)
        answerable = parsed.get("answerable")
        if not isinstance(answerable, bool):
            raise LLMOutputError("model response omitted boolean 'answerable'")
        sql = parsed.get("sql")
        reason = parsed.get("reason")
        if answerable and (not isinstance(sql, str) or not sql.strip()):
            raise LLMOutputError("answerable response omitted SQL")
        if not answerable and not isinstance(reason, str):
            reason = "The question cannot be answered from the orders schema."
        return SQLProposal(
            answerable=answerable,
            sql=sql.strip() if isinstance(sql, str) else None,
            reason=reason,
            token_count=tokens,
            raw=raw,
        )

    async def summarize(self, system_prompt: str, user_prompt: str) -> AnswerResult:
        raw, tokens = await self._chat(system_prompt, user_prompt)
        parsed = self._json_object(raw)
        answer = parsed.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            raise LLMOutputError("model response omitted 'answer'")
        return AnswerResult(answer=answer.strip(), token_count=tokens)
