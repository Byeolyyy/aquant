"""Web 传输层：把 ProtocolServer 暴露成带口令的 HTTP + SSE 服务。

只依赖标准库。Harness 本身是线程模型（每个 run 一个 daemon 线程 +
ThreadPoolExecutor），所以线程式 HTTP 服务器是天然匹配；引入 async 框架
反而要把阻塞调用塞回线程池。静态文件由 nginx 托管，不经这里。

只监听回环地址，公网入口只有 nginx。

路由：
    GET  /api/healthz   免鉴权，供 nginx 与监控探活
    POST /api/login     口令登录，下发签名 cookie
    POST /api/logout    注销
    GET  /api/session   当前登录状态
    POST /api/rpc       复用 ProtocolServer.dispatch_message
    GET  /api/events    SSE，只推本会话的事件
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import secrets
import threading
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .models import HarnessEvent
from .repository import Repository
from .server import PROTOCOL_VERSION, RunAccessDenied, ProtocolServer


COOKIE_NAME = "aquant_session"
DEFAULT_PORT = 8788
DEFAULT_HOST = "127.0.0.1"
DEFAULT_SESSION_TTL_DAYS = 7
DEFAULT_DAILY_RUN_LIMIT = 20
DEFAULT_MAX_CONCURRENT_RUNS = 2
DEFAULT_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 900
SSE_HEARTBEAT_SECONDS = 20
SSE_MAX_STREAMS_PER_SESSION = 3
EVENT_RING_SIZE = 200
MAX_BODY_BYTES = 4 * 1024 * 1024

# 服务端统一配置，访客不得改动。save_settings 已由 read_only_settings 挡住，
# 这里挡住 Agent 与提示词的写入：访客发布一个坏 prompt 会影响之后所有人的
# 运行，演示进行中被搞坏是真实风险。提示词工作台仍可只读浏览。
BLOCKED_METHODS = frozenset(
    {
        "save_settings",
        "save_agent_config",
        "create_prompt_draft",
        "publish_prompt_version",
        "rollback_prompt_version",
    }
)
# 会花钱或打外部接口的方法，计入每日额度。
QUOTA_METHODS = frozenset({"start_run", "retry_run", "test_integration"})

SHANGHAI = timezone(timedelta(hours=8))


def _today() -> str:
    """额度按上海时区跨日，不依赖进程默认时区。"""
    return datetime.now(SHANGHAI).strftime("%Y-%m-%d")


class SessionStore:
    """内存态会话。随机 token 本身即凭证，不需要额外签名。

    进程重启即全体登出——演示场景可以接受，换来的是即时失效
    与按会话计数的能力。
    """

    def __init__(self, ttl_days: int = DEFAULT_SESSION_TTL_DAYS):
        self._ttl = timedelta(days=ttl_days)
        self._sessions: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = datetime.now(timezone.utc) + self._ttl
        return token

    def validate(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            expires = self._sessions.get(token)
            if expires is None:
                return False
            if expires < datetime.now(timezone.utc):
                self._sessions.pop(token, None)
                return False
        return True

    def drop(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    @property
    def ttl_seconds(self) -> int:
        return int(self._ttl.total_seconds())


class LoginThrottle:
    """按来源 IP 的失败次数滑窗，挡口令爆破。"""

    def __init__(self, attempts: int = DEFAULT_LOGIN_ATTEMPTS, window: int = LOGIN_WINDOW_SECONDS):
        self._attempts = attempts
        self._window = timedelta(seconds=window)
        self._failures: dict[str, list[datetime]] = {}
        self._lock = threading.Lock()

    def blocked(self, ip: str) -> bool:
        now = datetime.now(timezone.utc)
        with self._lock:
            recent = [item for item in self._failures.get(ip, []) if now - item < self._window]
            self._failures[ip] = recent
            return len(recent) >= self._attempts

    def record_failure(self, ip: str) -> None:
        with self._lock:
            self._failures.setdefault(ip, []).append(datetime.now(timezone.utc))

    def clear(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)


class DailyQuota:
    def __init__(self, limit: int = DEFAULT_DAILY_RUN_LIMIT):
        self.limit = limit
        self._counts: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def check_and_consume(self, session: str) -> None:
        if self.limit <= 0:
            return
        key = (_today(), session)
        with self._lock:
            used = self._counts.get(key, 0)
            if used >= self.limit:
                raise ValueError(f"今日额度已用完（每日 {self.limit} 次），请明天再试")
            self._counts[key] = used + 1

    def refund(self, session: str) -> None:
        """调用没真正发生时把额度还回去（例如并发上限拦下）。"""
        key = (_today(), session)
        with self._lock:
            if self._counts.get(key):
                self._counts[key] -= 1


class EventBroker:
    """按运行归属分发事件。

    渲染层虽然按 run_id 过滤显示，但那只是 UI 去噪：事件一旦广播，
    别人的研报原文和分析结论就已经进了对方浏览器的内存。真正的隔离
    必须在服务端源头做。
    """

    def __init__(self, repository: Repository):
        self.repository = repository
        self._subscribers: dict[str, list[queue.Queue]] = {}
        self._ring: dict[str, list[dict[str, Any]]] = {}
        self._run_owner: dict[str, str] = {}
        self._lock = threading.Lock()

    def register_run(self, run_id: str, session: str) -> None:
        with self._lock:
            self._run_owner[run_id] = session

    def _owner_of(self, run_id: str) -> str:
        with self._lock:
            cached = self._run_owner.get(run_id)
        if cached is not None:
            return cached
        # 进程重启后内存映射没了，回落到数据库。
        owner = self.repository.get_run_owner(run_id)
        with self._lock:
            self._run_owner[run_id] = owner
        return owner

    def publish(self, event: HarnessEvent) -> None:
        owner = self._owner_of(event.run_id)
        if not owner:
            # 无归属（桌面版或历史数据）不广播给任何 Web 会话。
            return
        payload = event.model_dump(mode="json")
        with self._lock:
            ring = self._ring.setdefault(owner, [])
            ring.append(payload)
            del ring[:-EVENT_RING_SIZE]
            for channel in self._subscribers.get(owner, []):
                try:
                    channel.put_nowait(payload)
                except queue.Full:
                    pass

    def subscribe(self, session: str) -> queue.Queue | None:
        channel: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            channels = self._subscribers.setdefault(session, [])
            if len(channels) >= SSE_MAX_STREAMS_PER_SESSION:
                return None
            channels.append(channel)
            # 断线重连或刷新页面时先补发最近事件；渲染层按 event_id 去重，
            # 重放是幂等的。
            for payload in self._ring.get(session, []):
                channel.put_nowait(payload)
        return channel

    def unsubscribe(self, session: str, channel: queue.Queue) -> None:
        with self._lock:
            channels = self._subscribers.get(session, [])
            if channel in channels:
                channels.remove(channel)


class WebApp:
    """把上面几件东西组装起来，供 handler 调用。"""

    def __init__(self, repository: Repository | None = None):
        self.password = os.environ.get("QUANT_AGENT_ACCESS_PASSWORD", "").strip()
        if not self.password:
            raise SystemExit(
                "缺少 QUANT_AGENT_ACCESS_PASSWORD：Web 模式必须设置访问口令后才能启动"
            )
        self.repository = repository or Repository()
        self.broker = EventBroker(self.repository)
        self.protocol = ProtocolServer(
            self.repository,
            event_sink=self.broker.publish,
            read_only_settings=True,
        )
        self.sessions = SessionStore(_env_int("QUANT_AGENT_SESSION_TTL_DAYS", DEFAULT_SESSION_TTL_DAYS))
        self.throttle = LoginThrottle()
        self.quota = DailyQuota(_env_int("QUANT_AGENT_DAILY_RUN_LIMIT", DEFAULT_DAILY_RUN_LIMIT))
        self.max_concurrent = _env_int("QUANT_AGENT_MAX_CONCURRENT_RUNS", DEFAULT_MAX_CONCURRENT_RUNS)
        self.cookie_secure = os.environ.get("QUANT_AGENT_COOKIE_SECURE", "1").strip() != "0"
        self._seed_settings()
        self._mark_interrupted_runs()

    def _seed_settings(self) -> None:
        """把服务端环境变量里的非密钥配置写进 settings。

        密钥走 EnvVarSecretBackend 直接读环境变量，不落库；这里只种
        Base URL、模型名、收信邮箱这类可见配置，因为应用内已禁止修改。
        """
        seeds = {
            "model.base_url": os.environ.get("QUANT_AGENT_MODEL_BASE_URL", "").strip(),
            "model.name": os.environ.get("QUANT_AGENT_MODEL_NAME", "").strip(),
            "mail.address": os.environ.get("QUANT_AGENT_MAIL_ADDRESS", "").strip(),
            "mail.imap_host": os.environ.get("QUANT_AGENT_MAIL_IMAP_HOST", "").strip(),
            "mail.mailbox": os.environ.get("QUANT_AGENT_MAIL_MAILBOX", "").strip(),
            "mail.from_allowlist": os.environ.get("QUANT_AGENT_MAIL_FROM_ALLOWLIST", "").strip(),
        }
        values = {key: value for key, value in seeds.items() if value}
        if values:
            self.repository.set_settings(values)
        self.protocol._reload_integrations()
        self.protocol.harness.llm_client = self.protocol.llm_client
        self.protocol.harness.tushare_client = self.protocol.tushare_client
        self.protocol.harness.tavily_client = self.protocol.tavily_client

    def _mark_interrupted_runs(self) -> None:
        """重启后把卡在非终态的运行标成 interrupted。

        运行控制标志只在内存，进程一重启就没了；不标记的话这些运行会
        永远停在 planning，界面上看是一个转不完的圈。
        """
        for run in self.repository.list_runs(200):
            if str(run.get("status")) in {"planning", "running", "paused"}:
                self.repository.update_run(str(run["run_id"]), "interrupted")

    def login(self, password: str, ip: str) -> str | None:
        if self.throttle.blocked(ip):
            raise PermissionError("尝试次数过多，请稍后再试")
        if not hmac.compare_digest(password, self.password):
            self.throttle.record_failure(ip)
            return None
        self.throttle.clear(ip)
        return self.sessions.create()

    def dispatch(self, message: dict[str, Any], session: str) -> dict[str, Any]:
        method = str(message.get("method") or "")
        if method in BLOCKED_METHODS:
            raise PermissionError("演示环境下该配置为只读")
        if method in QUOTA_METHODS:
            self.quota.check_and_consume(session)
            if method in {"start_run", "retry_run"}:
                if self.protocol.harness.active_run_count() >= self.max_concurrent:
                    self.quota.refund(session)
                    raise ValueError(
                        f"当前已有 {self.max_concurrent} 个分析在跑，请等其中一个结束后再试"
                    )
        response = self.protocol.dispatch_message(message, owner_session=session)
        run_id = str((response.get("result") or {}).get("run_id") or "")
        if run_id:
            self.broker.register_run(run_id, session)
        return response


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


class WebHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "aquant"
    sys_version = ""

    app: WebApp  # 由 serve() 注入到 handler 类上

    # ---- 基础工具 ----

    def log_message(self, fmt: str, *args: Any) -> None:
        # 默认实现会把访问日志写 stderr 并带上时间戳，journald 已经有了。
        pass

    @property
    def client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0]

    def _session(self) -> str:
        raw = self.headers.get("Cookie", "")
        if not raw:
            return ""
        try:
            cookie = SimpleCookie()
            cookie.load(raw)
        except Exception:
            return ""
        morsel = cookie.get(COOKIE_NAME)
        token = morsel.value if morsel else ""
        return token if self.app.sessions.validate(token) else ""

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc}") from exc
        return value if isinstance(value, dict) else {}

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any], cookie: str = "") -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _cookie_value(self, token: str, max_age: int) -> str:
        parts = [
            f"{COOKIE_NAME}={token}",
            "HttpOnly",
            "SameSite=Lax",
            "Path=/",
            f"Max-Age={max_age}",
        ]
        if self.app.cookie_secure:
            parts.append("Secure")
        return "; ".join(parts)

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定命名
        path = self.path.split("?", 1)[0]
        if path == "/api/healthz":
            self._send_json(HTTPStatus.OK, {"ok": True, "protocol_version": PROTOCOL_VERSION})
            return
        if path == "/api/session":
            self._send_json(HTTPStatus.OK, {"authenticated": bool(self._session())})
            return
        if path == "/api/events":
            self._stream_events()
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "未知接口"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/login":
                self._login()
                return
            if path == "/api/logout":
                self._logout()
                return
            if path == "/api/rpc":
                self._rpc()
                return
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        except PermissionError as exc:
            self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": str(exc)})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "未知接口"})

    def _login(self) -> None:
        payload = self._read_json()
        try:
            token = self.app.login(str(payload.get("password") or ""), self.client_ip)
        except PermissionError as exc:
            self._send_json(HTTPStatus.TOO_MANY_REQUESTS, {"ok": False, "error": str(exc)})
            return
        if not token:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "访问口令不正确"})
            return
        self._send_json(
            HTTPStatus.OK,
            {"ok": True},
            cookie=self._cookie_value(token, self.app.sessions.ttl_seconds),
        )

    def _logout(self) -> None:
        self.app.sessions.drop(self._session())
        self._send_json(HTTPStatus.OK, {"ok": True}, cookie=self._cookie_value("", 0))

    def _rpc(self) -> None:
        session = self._session()
        if not session:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "请先登录"})
            return
        message = self._read_json()
        try:
            response = self.app.dispatch(message, session)
        except PermissionError as exc:
            self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": str(exc)})
            return
        except RunAccessDenied as exc:
            self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": str(exc)})
            return
        except ValueError as exc:
            self._send_json(HTTPStatus.TOO_MANY_REQUESTS, {"ok": False, "error": str(exc)})
            return
        self._send_json(HTTPStatus.OK, response)

    def _stream_events(self) -> None:
        session = self._session()
        if not session:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "请先登录"})
            return
        channel = self.app.broker.subscribe(session)
        if channel is None:
            self._send_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"ok": False, "error": "事件连接数过多，请关闭多余的标签页"},
            )
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        # nginx 即使漏配 proxy_buffering off，这个头也能让它别缓冲。
        self.send_header("X-Accel-Buffering", "no")
        # 不带 Content-Length 的响应体靠连接关闭界定；显式声明 close，
        # 客户端才会一直读下去而不是等长度。
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        try:
            while True:
                try:
                    payload = channel.get(timeout=SSE_HEARTBEAT_SECONDS)
                except queue.Empty:
                    # 心跳注释行，防止 nginx 与中间设备掐掉空闲连接。
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                body = json.dumps(payload, ensure_ascii=False, default=str)
                self.wfile.write(f"data: {body}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.broker.unsubscribe(session, channel)


def serve() -> int:
    app = WebApp()
    handler = type("BoundWebHandler", (WebHandler,), {"app": app})
    host = os.environ.get("QUANT_AGENT_WEB_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    port = _env_int("QUANT_AGENT_WEB_PORT", DEFAULT_PORT)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    mode = "openai-compatible" if app.protocol.llm_client else "deterministic-demo"
    print(
        f"aquant web 已启动 http://{host}:{port} · 模型模式 {mode} · "
        f"每日额度 {app.quota.limit} · 并发上限 {app.max_concurrent}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


def main() -> int:
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
