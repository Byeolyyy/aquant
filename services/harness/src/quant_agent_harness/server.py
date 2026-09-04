from __future__ import annotations

import json
import sys
import threading
import time
import urllib.parse
from typing import Any, Callable

from .harness import Harness
from .integrations import TavilyClient, TushareClient
from .llm import OpenAICompatibleClient
from .mail_ingest import MailIngestor
from .mailbox import (
    DEFAULT_IMAP_HOST,
    DEFAULT_MAILBOX,
    DEFAULT_SINCE_DAYS,
    DEFAULT_SUBJECT_KEYWORDS,
    MailboxClient,
)
from .models import DEFAULT_AGENT_PROFILES, AgentRuntimeConfig, HarnessEvent, RunPolicy
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
MAIL_ADDRESS = "mail.address"
MAIL_IMAP_HOST = "mail.imap_host"
MAIL_MAILBOX = "mail.mailbox"
MAIL_SUBJECT_KEYWORDS = "mail.subject_keywords"
MAIL_FROM_ALLOWLIST = "mail.from_allowlist"
MAIL_AUTH_CODE = "mail.auth_code"
# 同步是用户点出来的，连点会白白拖慢界面并反复登录邮箱。
MAIL_SYNC_MIN_INTERVAL_SECONDS = 30.0


class RunAccessDenied(Exception):
    """访问了不属于当前会话的运行。Web 层据此返回 403。"""


class ProtocolServer:
    def __init__(
        self,
        repository: Repository | None = None,
        *,
        event_sink: Callable[[HarnessEvent], None] | None = None,
        read_only_settings: bool = False,
    ):
        self.repository = repository or Repository()
        # 事件出口。stdio 模式写 stdout；Web 模式换成按会话分发的 broker。
        # Harness 内部对此完全无感。
        self._event_sink = event_sink or self._write_event
        self.read_only_settings = read_only_settings
        self._write_lock = threading.Lock()
        self.llm_client: OpenAICompatibleClient | None = None
        self.tushare_client: TushareClient | None = None
        self.tavily_client: TavilyClient | None = None
        self.public_a_stock_client = PublicAStockClient()
        self.global_market_client = GlobalMarketClient()
        self._last_mail_sync = 0.0
        self._mail_lock = threading.Lock()
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
        """stdio 传输：逐行读请求、逐行写响应。"""
        self._write({"type": "ready", "protocol_version": PROTOCOL_VERSION})
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            self._write(self.dispatch_line(line))
        return 0

    def dispatch_line(self, line: str) -> dict[str, Any]:
        try:
            message = json.loads(line)
        except Exception as exc:
            return _error_envelope("", exc)
        return self.dispatch_message(message)

    def dispatch_message(
        self,
        message: dict[str, Any],
        *,
        owner_session: str | None = None,
    ) -> dict[str, Any]:
        """校验信封、分发、把异常包成响应。

        stdio 与 HTTP 两种传输共用这一个入口，错误信封逐字节一致。
        """
        request_id = ""
        try:
            request_id = str(message.get("request_id") or "")
            if message.get("type") != "request":
                raise ValueError("消息 type 必须为 request")
            if int(message.get("protocol_version") or 0) != PROTOCOL_VERSION:
                raise ValueError("协议版本不兼容")
            result = self.handle(
                str(message.get("method") or ""),
                message.get("payload") or {},
                owner_session=owner_session,
            )
            return {
                "type": "response",
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": True,
                "result": result,
            }
        except Exception as exc:
            return _error_envelope(request_id, exc)

    def handle(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        owner_session: str | None = None,
    ) -> dict[str, Any]:
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
            if self.read_only_settings:
                raise ValueError("服务端模式下连接与密钥由服务器统一配置，应用内不可修改")
            return {"settings": self._save_settings(payload)}
        if method == "test_integration":
            return self._test_integration(str(payload.get("target") or ""))
        if method == "parse_report":
            report = parse_ptrade_report(str(payload.get("raw_text") or ""))
            # 手动粘贴的报告归属发起者；邮件来的报告归属为空串（共享池）。
            self.repository.save_report(report, owner_session=owner_session)
            return {"report": report.model_dump(mode="json")}
        if method == "sync_mailbox":
            return self._sync_mailbox(payload)
        if method == "get_report":
            report = self.repository.get_report(str(payload.get("report_id") or ""))
            if report is None:
                raise ValueError("找不到报告")
            return {"report": report.model_dump(mode="json")}
        if method == "list_reports":
            return {
                "reports": self.repository.list_reports(
                    int(payload.get("limit") or 50), owner_session=owner_session
                )
            }
        if method == "list_runs":
            return {
                "runs": self.repository.list_runs(
                    int(payload.get("limit") or 50), owner_session=owner_session
                )
            }
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
            required = {profile.agent_id for profile in DEFAULT_AGENT_PROFILES if profile.required}
            enabled = bool(payload.get("enabled", True))
            if agent_id in required and not enabled:
                raise ValueError("大脑、统筹、量化信号和风险 Agent 是治理链必需角色，不能停用")
            config = AgentRuntimeConfig(
                agent_id=agent_id,
                enabled=enabled,
                custom_instructions=str(payload.get("custom_instructions") or "")[:4000],
            )
            self.repository.update_agent_config(config)
            return {"agents": self.repository.list_agent_configs()}
        if method == "start_run":
            policy = RunPolicy.model_validate(payload.get("policy") or {})
            return {
                "run_id": self.harness.start(
                    str(payload.get("report_id") or ""), policy, owner_session=owner_session
                )
            }
        if method == "get_run_snapshot":
            snapshot = self.repository.run_snapshot(self._owned_run_id(payload, owner_session))
            if snapshot is None:
                raise ValueError("找不到运行")
            return {"snapshot": snapshot}
        if method == "retry_run":
            snapshot = self.repository.run_snapshot(self._owned_run_id(payload, owner_session))
            if snapshot is None:
                raise ValueError("找不到要重跑的运行")
            return {
                "run_id": self.harness.start(
                    str(snapshot["report_id"]), RunPolicy(), owner_session=owner_session
                )
            }
        if method == "pause_run":
            self.harness.pause(self._owned_run_id(payload, owner_session))
            return {"accepted": True}
        if method == "resume_run":
            self.harness.resume(self._owned_run_id(payload, owner_session))
            return {"accepted": True}
        if method == "cancel_run":
            self.harness.cancel(self._owned_run_id(payload, owner_session))
            return {"accepted": True}
        if method == "steer_run":
            self.harness.steer(
                self._owned_run_id(payload, owner_session), str(payload.get("message") or "")
            )
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
            "mail": self._mail_settings(),
            "storage": {
                # 服务端不回传真实库路径，避免向访客泄露服务器目录结构。
                "database": "" if self.read_only_settings else str(self.repository.database_path),
                "secret_backend": self.repository.secret_backend.describe(),
                "writable": not self.read_only_settings,
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

        mail = payload.get("mail") or {}
        if not isinstance(mail, dict):
            raise ValueError("mail 设置必须是对象")
        if mail:
            mail_updates = {
                MAIL_ADDRESS: str(mail.get("address") or "").strip(),
                MAIL_IMAP_HOST: str(mail.get("imap_host") or "").strip() or DEFAULT_IMAP_HOST,
                MAIL_MAILBOX: str(mail.get("mailbox") or "").strip() or DEFAULT_MAILBOX,
                MAIL_SUBJECT_KEYWORDS: str(mail.get("subject_keywords") or "").strip()
                or ",".join(DEFAULT_SUBJECT_KEYWORDS),
                MAIL_FROM_ALLOWLIST: str(mail.get("from_allowlist") or "").strip(),
            }
            self.repository.set_settings(mail_updates)

        secret_updates = {
            MAIL_AUTH_CODE: mail.get("auth_code") if isinstance(mail, dict) else None,
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
        allowed_clear = {MODEL_API_KEY, TUSHARE_TOKEN, TAVILY_API_KEY, MAIL_AUTH_CODE}
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
        if target == "mail":
            return {"ok": True, **self._mail_ingestor().client.test_connection()}
        raise ValueError(f"不支持的连接测试目标: {target}")

    def _owned_run_id(self, payload: dict[str, Any], owner_session: str | None) -> str:
        """取出 run_id 并确认归属。桌面版 owner_session 为 None，不校验。"""
        run_id = str(payload.get("run_id") or "")
        if owner_session is None:
            return run_id
        owner = self.repository.get_run_owner(run_id)
        if owner and owner != owner_session:
            raise RunAccessDenied("这条运行属于其他会话")
        return run_id

    def _mail_settings(self) -> dict[str, Any]:
        return {
            "address": self.repository.get_setting(MAIL_ADDRESS),
            "imap_host": self.repository.get_setting(MAIL_IMAP_HOST, DEFAULT_IMAP_HOST),
            "mailbox": self.repository.get_setting(MAIL_MAILBOX, DEFAULT_MAILBOX),
            "subject_keywords": self.repository.get_setting(
                MAIL_SUBJECT_KEYWORDS, ",".join(DEFAULT_SUBJECT_KEYWORDS)
            ),
            "from_allowlist": self.repository.get_setting(MAIL_FROM_ALLOWLIST),
            "auth_code_configured": self.repository.secret_is_configured(MAIL_AUTH_CODE),
        }

    def _mail_ingestor(self) -> MailIngestor:
        settings = self._mail_settings()
        client = MailboxClient(
            settings["address"],
            self.repository.get_secret(MAIL_AUTH_CODE),
            host=settings["imap_host"] or DEFAULT_IMAP_HOST,
            mailbox=settings["mailbox"] or DEFAULT_MAILBOX,
        )
        return MailIngestor(
            self.repository,
            client,
            subject_keywords=_split_list(settings["subject_keywords"]) or DEFAULT_SUBJECT_KEYWORDS,
            from_allowlist=_split_list(settings["from_allowlist"]),
        )

    def _sync_mailbox(self, payload: dict[str, Any]) -> dict[str, Any]:
        # 单飞：并发点击只放一次真正的连接进去，其余立刻得到明确回应。
        if not self._mail_lock.acquire(blocking=False):
            raise ValueError("邮件同步正在进行，请稍候")
        try:
            elapsed = time.monotonic() - self._last_mail_sync
            if self._last_mail_sync and elapsed < MAIL_SYNC_MIN_INTERVAL_SECONDS:
                wait = int(MAIL_SYNC_MIN_INTERVAL_SECONDS - elapsed) + 1
                raise ValueError(f"刚刚已同步过，请 {wait} 秒后再试")
            result = self._mail_ingestor().sync(
                since_days=int(payload.get("since_days") or DEFAULT_SINCE_DAYS)
            )
            self._last_mail_sync = time.monotonic()
            return {
                "sync": result.as_payload(),
                "reports": self.repository.list_reports(50),
            }
        finally:
            self._mail_lock.release()

    def _send_event(self, event: HarnessEvent) -> None:
        self._event_sink(event)

    def _write_event(self, event: HarnessEvent) -> None:
        """stdio 模式的事件出口：写一行 JSONL 给 Electron 主进程。"""
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


def _error_envelope(request_id: str, exc: Exception) -> dict[str, Any]:
    return {
        "type": "response",
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": False,
        "error": f"{type(exc).__name__}: {exc}",
    }


def _split_list(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in str(value or "").split(",") if item.strip())


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
