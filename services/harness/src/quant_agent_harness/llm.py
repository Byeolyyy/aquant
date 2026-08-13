from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelResult:
    data: dict[str, Any]
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class OpenAICompatibleClient:
    """Minimal Chat Completions adapter with strict JSON outputs.

    The client is intentionally small so the harness owns orchestration,
    validation and permissions instead of delegating them to a provider SDK.
    """

    def __init__(self, base_url: str, api_key: str, model: str, *, timeout_seconds: int = 60):
        base_url = base_url.strip().rstrip("/")
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("LLM Base URL 必须是有效的 http/https 地址")
        self.base_url = base_url
        self.provider_host = (parsed.hostname or "").lower()
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.timeout_seconds = timeout_seconds
        if not self.api_key or not self.model:
            raise ValueError("LLM API Key 和模型名不能为空")

    def complete_json(self, system: str, user: str) -> ModelResult:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "max_tokens": _json_max_tokens(system),
        }
        # DeepSeek V4 enables high-effort thinking by default. These calls only
        # produce small, schema-constrained control objects, so reasoning tokens
        # add latency without improving the deterministic workflow around them.
        if self.provider_host == "api.deepseek.com" or self.model.lower().startswith("deepseek-v4"):
            payload["thinking"] = {"type": "disabled"}
        try:
            response = self._post(payload)
        except urllib.error.HTTPError as exc:
            if exc.code not in {400, 404, 422}:
                raise
            payload.pop("response_format", None)
            payload.pop("thinking", None)
            response = self._post(payload)
        choices = response.get("choices") or []
        if not choices:
            raise RuntimeError("模型响应缺少 choices")
        content = str(((choices[0].get("message") or {}).get("content")) or "").strip()
        data = _parse_json_object(content)
        usage = response.get("usage") or {}
        return ModelResult(
            data=data,
            model=str(response.get("model") or self.model),
            prompt_tokens=_optional_int(usage.get("prompt_tokens")),
            completion_tokens=_optional_int(usage.get("completion_tokens")),
        )

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        endpoint = self.base_url
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "QuantAgent/0.1",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            raw = response.read().decode("utf-8", errors="replace")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise RuntimeError("模型响应不是 JSON 对象")
        return parsed


def _parse_json_object(content: str) -> dict[str, Any]:
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, re.I | re.S)
    if fenced:
        content = fenced.group(1)
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise RuntimeError("模型输出必须是 JSON 对象")
    return parsed


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _json_max_tokens(system: str) -> int:
    """Keep operational JSON calls short while leaving room for final reports."""

    if "selected_agents" in system:
        return 450
    if "现在不是做最终总结" in system:
        return 800
    if "连接测试" in system:
        return 200
    if "字段必须是 title" in system:
        return 1400
    return 1800
