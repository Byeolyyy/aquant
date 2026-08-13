from __future__ import annotations

import json
import sys
import threading
import urllib.parse
from typing import Any

from .harness import Harness
from .integrations import TavilyClient, TushareClient
from .llm import OpenAICompatibleClient
from .models import AgentRuntimeConfig, HarnessEvent, RunPolicy
from .parser import parse_ptrade_report
from .public_sources import PublicAStockClient
from .global_markets import GlobalMarketClient
from .repository import Repository
from .workflows import WORKFLOW_DEFINITIONS


PROTOCOL_VERSION = 1
MODEL_BASE_URL = "model.base_url"
MODEL_NAME = "model.name"
MODEL_API_KEY = "model.api_key"
TUSHARE_TOKEN = "tushare.token"
TAVILY_API_KEY = "tavily.api_key"


class ProtocolServer:
    def __init__(self, repository: Repository | None = None):
        self.repository = repository or Repository()
        self._write_lock = threading.Lock()
        self.llm_client: OpenAICompatibleClient | None = None
        self.tushare_client: TushareClient | None = None
        self.tavily_client: TavilyClient | None = None
        self.public_a_stock_client = PublicAStockClient()
        self.global_market_client = GlobalMarketClient()
        self._reload_integrations()
        self.harness = Harness(
            self.repository,
            self._send_event,
            self.llm_client,
            self.tushare_client,
            self.tavily_client,
            self.public_a_stock_client,
            self.global_market_client,
        )

    def serve(self) -> int:
        self._write({"type": "ready", "protocol_version": PROTOCOL_VERSION})
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            request_id = ""
            try:
                message = json.loads(line)
                request_id = str(message.get("request_id") or "")
                if message.get("type") != "request":
                    raise ValueError("消息 type 必须为 request")
                if int(message.get("protocol_version") or 0) != PROTOCOL_VERSION:
                    raise ValueError("协议版本不兼容")
                result = self.handle(str(message.get("method") or ""), message.get("payload") or {})
                self._write(
                    {
                        "type": "response",
                        "protocol_version": PROTOCOL_VERSION,
                        "request_id": request_id,
                        "ok": True,
                        "result": result,
                    }
                )
            except Exception as exc:
                self._write(
                    {
                        "type": "response",
                        "protocol_version": PROTOCOL_VERSION,
                        "request_id": request_id,
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        return 0

    def handle(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if method == "ping":
            return {
                "service": "quant-agent-harness",
                "protocol_version": PROTOCOL_VERSION,
                "database": str(self.repository.database_path),
                "mode": "openai-compatible" if self.llm_client else "deterministic-demo",
                "model": self.llm_client.model if self.llm_client else "",
            }
        if method == "get_settings":
            return {"settings": self._public_settings()}
        if method == "save_settings":
            return {"settings": self._save_settings(payload)}
        if method == "test_integration":
            return self._test_integration(str(payload.get("target") or ""))
        if method == "parse_report":
            report = parse_ptrade_report(str(payload.get("raw_text") or ""))
            self.repository.save_report(report)
            return {"report": report.model_dump(mode="json")}
        if method == "list_reports":
            return {"reports": self.repository.list_reports(int(payload.get("limit") or 50))}
        if method == "list_runs":
            return {"runs": self.repository.list_runs(int(payload.get("limit") or 50))}
        if method == "get_agents":
            return {"agents": self.repository.list_agent_configs()}
        if method == "get_prompt_workspace":
            return {"prompts": self.repository.prompt_workspace()}
        if method == "get_workflows":
            return {"workflows": list(WORKFLOW_DEFINITIONS.values())}
        if method == "create_prompt_draft":
            version_id = self.repository.create_prompt_draft(
                str(payload.get("prompt_id") or ""),
                str(payload.get("content") or ""),
                str(payload.get("change_note") or ""),
            )
            return {"version_id": version_id, "prompts": self.repository.prompt_workspace()}
        if method == "publish_prompt_version":
            self.repository.publish_prompt_version(str(payload.get("version_id") or ""))
            return {"prompts": self.repository.prompt_workspace()}
        if method == "rollback_prompt_version":
            version_id = self.repository.rollback_prompt_version(
                str(payload.get("prompt_id") or ""),
                str(payload.get("version_id") or ""),
            )
            return {"version_id": version_id, "prompts": self.repository.prompt_workspace()}
        if method == "save_agent_config":
            agent_id = str(payload.get("agent_id") or "")
            required = {"coordinator", "quant_signal", "risk"}
            enabled = bool(payload.get("enabled", True))
            if agent_id in required and not enabled:
                raise ValueError("统筹、量化信号和风险 Agent 是治理链必需角色，不能停用")
            config = AgentRuntimeConfig(
                agent_id=agent_id,
                enabled=enabled,
                custom_instructions=str(payload.get("custom_instructions") or "")[:4000],
            )
            self.repository.update_agent_config(config)
            return {"agents": self.repository.list_agent_configs()}
        if method == "start_run":
            policy = RunPolicy.model_validate(payload.get("policy") or {})
            return {"run_id": self.harness.start(str(payload.get("report_id") or ""), policy)}
        if method == "get_run_snapshot":
            snapshot = self.repository.run_snapshot(str(payload.get("run_id") or ""))
            if snapshot is None:
                raise ValueError("找不到运行")
            return {"snapshot": snapshot}
        if method == "retry_run":
            snapshot = self.repository.run_snapshot(str(payload.get("run_id") or ""))
            if snapshot is None:
                raise ValueError("找不到要重跑的运行")
            return {"run_id": self.harness.start(str(snapshot["report_id"]), RunPolicy())}
        if method == "pause_run":
            self.harness.pause(str(payload.get("run_id") or ""))
            return {"accepted": True}
        if method == "resume_run":
            self.harness.resume(str(payload.get("run_id") or ""))
            return {"accepted": True}
        if method == "cancel_run":
            self.harness.cancel(str(payload.get("run_id") or ""))
            return {"accepted": True}
        if method == "steer_run":
            self.harness.steer(str(payload.get("run_id") or ""), str(payload.get("message") or ""))
            return {"accepted": True}
        raise ValueError(f"不支持的方法: {method}")

    def _public_settings(self) -> dict[str, Any]:
        return {
            "model": {
                "base_url": self.repository.get_setting(MODEL_BASE_URL, "https://api.openai.com/v1"),
                "model": self.repository.get_setting(MODEL_NAME),
                "api_key_configured": self.repository.secret_is_configured(MODEL_API_KEY),
                "ready": self.llm_client is not None,
            },
            "tushare": {
                "token_configured": self.repository.secret_is_configured(TUSHARE_TOKEN),
            },
            "tavily": {
                "api_key_configured": self.repository.secret_is_configured(TAVILY_API_KEY),
            },
            "storage": {
                "database": str(self.repository.database_path),
                "secret_backend": "Windows DPAPI (current user)",
            },
        }

    def _save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        model = payload.get("model") or {}
        if not isinstance(model, dict):
            raise ValueError("model 设置必须是对象")
        base_url = str(model.get("base_url") or "").strip().rstrip("/")
        model_name = str(model.get("model") or "").strip()
        if base_url:
            parsed = urllib.parse.urlparse(base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("模型 Base URL 必须是有效的 http/https 地址")
        self.repository.set_settings({MODEL_BASE_URL: base_url, MODEL_NAME: model_name})

        secret_updates = {
            MODEL_API_KEY: model.get("api_key"),
            TUSHARE_TOKEN: (payload.get("tushare") or {}).get("token")
            if isinstance(payload.get("tushare") or {}, dict)
            else None,
            TAVILY_API_KEY: (payload.get("tavily") or {}).get("api_key")
            if isinstance(payload.get("tavily") or {}, dict)
            else None,
        }
        for key, value in secret_updates.items():
            text = str(value or "").strip()
            if text:
                self.repository.set_secret(key, text)

        clear_secrets = payload.get("clear_secrets") or []
        if not isinstance(clear_secrets, list):
            raise ValueError("clear_secrets 必须是数组")
        allowed_clear = {MODEL_API_KEY, TUSHARE_TOKEN, TAVILY_API_KEY}
        for key in clear_secrets:
            if key in allowed_clear:
                self.repository.delete_secret(str(key))

        self._reload_integrations()
        self.harness.llm_client = self.llm_client
        self.harness.tushare_client = self.tushare_client
        self.harness.tavily_client = self.tavily_client
        return self._public_settings()

    def _reload_integrations(self) -> None:
        base_url = self.repository.get_setting(MODEL_BASE_URL)
        model = self.repository.get_setting(MODEL_NAME)
        api_key = self.repository.get_secret(MODEL_API_KEY)
        self.llm_client = (
            OpenAICompatibleClient(base_url, api_key, model)
            if base_url and api_key and model
            else None
        )
        tushare_token = self.repository.get_secret(TUSHARE_TOKEN)
        tavily_api_key = self.repository.get_secret(TAVILY_API_KEY)
        self.tushare_client = TushareClient(tushare_token) if tushare_token else None
        self.tavily_client = TavilyClient(tavily_api_key) if tavily_api_key else None

    def _test_integration(self, target: str) -> dict[str, Any]:
        if target == "model":
            if self.llm_client is None:
                raise ValueError("请先保存完整的模型 Base URL、API Key 和模型名")
            result = self.llm_client.complete_json(
                "你是连接测试。只输出 JSON 对象。",
                '输出 {"status":"ok","message":"连接成功"}',
            )
            return {
                "ok": True,
                "message": "模型连接成功",
                "model": result.model,
                "response": result.data,
            }
        if target == "tushare":
            token = self.repository.get_secret(TUSHARE_TOKEN)
            if not token:
                raise ValueError("请先保存 Tushare token")
            return {"ok": True, **TushareClient(token).test_connection()}
        if target == "tavily":
            api_key = self.repository.get_secret(TAVILY_API_KEY)
            if not api_key:
                raise ValueError("请先保存 Tavily API Key")
            return {"ok": True, **TavilyClient(api_key).test_connection()}
        raise ValueError(f"不支持的连接测试目标: {target}")

    def _send_event(self, event: HarnessEvent) -> None:
        self._write(
            {
                "type": "event",
                "protocol_version": PROTOCOL_VERSION,
                "event": event.model_dump(mode="json"),
            }
        )

    def _write(self, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
        with self._write_lock:
            sys.stdout.write(encoded + "\n")
            sys.stdout.flush()


def main() -> int:
    # Windows pipes otherwise inherit the active ANSI code page (often GBK),
    # while Electron's streams are UTF-8. Force the protocol boundary itself
    # to UTF-8 so Chinese report input and Agent events survive both ways.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="strict")
    return ProtocolServer().serve()


if __name__ == "__main__":
    raise SystemExit(main())
