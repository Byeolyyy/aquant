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
    # 模型输出常见两种毛病：包在代码块里、尾部带解释文字或截断。
    # 先剥代码块，再取"从最外层 { 到最后一个 }"之间的最大有效前缀，
    # 而不是要求整段都是合法 JSON——否则一次调用就白烧了。
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, re.I | re.S)
    if fenced:
        content = fenced.group(1)
    parsed = _extract_json_prefix(content)
    if not isinstance(parsed, dict):
        raise RuntimeError("模型输出必须是 JSON 对象")
    return parsed


def _extract_json_prefix(content: str) -> Any:
    """从字符串里取以 { 开头的最长合法 JSON 前缀。

    模型截断时最后一组括号往往根本没闭合，仅从已有的 } 往前截取会
    一无所获。所以按两层恢复：

    1. 从最后一个 } 开始向前找截断点，能解析的最长前缀即为答案；
    2. 都不行时数出未闭合的括号层级，尝试补上缺失的收尾括号再解析。
       （截断的最后一串字符若是半个字符串值，补括号也救不回来，
       只能退到上一层截断点。）

    修复后的输出可能比预期少最后一个字段，但好过整份报废。
    """
    start = content.find("{")
    if start < 0:
        raise RuntimeError("模型输出中没有 JSON 对象")
    content = content[start:]

    last_close = content.rfind("}")
    while last_close >= 0:
        candidate = content[: last_close + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            last_close = content.rfind("}", 0, last_close)

    for closing in _closing_suffixes(content):
        candidate = content + closing
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    # 截断恰好切在某个值中间时，补括号也救不回来；把尾部未完成的
    # 字符逐步剥掉，再对剥掉后的前缀补括号。候选按"保留最多结构"排序：
    # 先试直接补括号（可能带出一个空的末项），再逐字符回退。
    for cut in range(1, min(80, len(content))):
        prefix = content[:-cut]
        for closing in _closing_suffixes(prefix):
            candidate = prefix + closing
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    raise RuntimeError("无法从模型输出中恢复合法 JSON")


def _closing_suffixes(content: str) -> list[str]:
    """按未闭合括号栈推出候选收尾串（最完整优先）。

    只补能被 json.loads 证实的组合：栈里倒数第 k 个未闭合括号
    之前的所有字符都已完成时，补上后 k 个反括号即可解析。
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in content:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":  # 括号栈
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if stack:
                stack.pop()
    if not stack or len(stack) > 16:
        return []
    return ["".join(reversed(stack[:k])) for k in range(1, len(stack) + 1)]


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
    # 专业 Agent 贡献对象要容纳 10 只股票的 claim/风险/未知项，
    # 1800 上限会截断输出导致 JSON 报废。
    return 4096
