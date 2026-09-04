/**
 * 浏览器版的 window.quantAgent。
 *
 * 桌面版由 preload 通过 contextBridge 注入同名对象；网页版没有 preload，
 * 这里用 fetch + EventSource 实现完全相同的接口，于是渲染层那 34 处调用
 * 一行都不用改。
 *
 * 注入必须发生在 React 渲染之前——main.tsx 里有 `if (!window.quantAgent)`
 * 的兜底分支，晚一步就会显示"桥接未加载"。
 */

import { PROTOCOL_VERSION } from "../shared/protocol";
import type { HarnessEvent } from "../shared/protocol";
import { snapshotToMarkdown } from "../shared/export-markdown";

const RPC_URL = "/api/rpc";
const EVENTS_URL = "/api/events";

export class UnauthenticatedError extends Error {
  constructor() {
    super("会话已失效，请重新登录");
    this.name = "UnauthenticatedError";
  }
}

/**
 * 会话失效通知：任何请求/事件流发现 401（通常是服务端重启导致内存态
 * 会话全部作废）时调用。app 层收到后回到口令门，让用户重新登录，
 * 而不是永远卡在"服务异常"。
 */
type SessionLostHandler = () => void;
let sessionLostHandler: SessionLostHandler | null = null;
export function onSessionLost(handler: SessionLostHandler): void {
  sessionLostHandler = handler;
}
function notifySessionLost(): void {
  if (sessionLostHandler) sessionLostHandler();
}

async function postJson(url: string, body: unknown): Promise<{ status: number; data: any }> {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify(body),
  });
  let data: any = {};
  try {
    data = await response.json();
  } catch {
    // 网关返回的非 JSON 错误页，按状态码处理即可。
  }
  return { status: response.status, data };
}

function newRequestId(): string {
  // crypto.randomUUID 只在安全上下文（HTTPS）可用；裸 IP 的 http 入口
  // 没有它，会导致 ping/get_settings 在发出前就抛异常。兜底一个随机 ID。
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return `web-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

async function request(method: string, payload: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
  const { status, data } = await postJson(RPC_URL, {
    type: "request",
    protocol_version: PROTOCOL_VERSION,
    request_id: newRequestId(),
    method,
    payload,
  });
  if (status === 401) {
    notifySessionLost();
    throw new UnauthenticatedError();
  }
  // 信封里的 ok:false 与 HTTP 层的 4xx/5xx 都要抛成 Error，
  // 语义与桌面版 sidecar 的 reject 保持一致。
  if (!data || data.ok !== true) {
    throw new Error(String(data?.error || `请求失败（HTTP ${status}）`));
  }
  return (data.result || {}) as Record<string, unknown>;
}

async function exportRun(runId: string): Promise<{ exported: boolean; path?: string }> {
  const result = await request("get_run_snapshot", { run_id: runId });
  const markdown = snapshotToMarkdown(result.snapshot as Record<string, unknown>);
  const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `aquant-${runId.slice(0, 8)}.md`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // 立刻 revoke 会让部分浏览器来不及取到内容，交给下一轮事件循环。
  setTimeout(() => URL.revokeObjectURL(url), 0);
  return { exported: true };
}

function onEvent(listener: (event: HarnessEvent) => void): () => void {
  const source = new EventSource(EVENTS_URL, { withCredentials: true });
  source.onmessage = (message) => {
    try {
      listener(JSON.parse(message.data) as HarnessEvent);
    } catch {
      // 单条事件解析失败不该拖垮整条流。
    }
  };
  source.onerror = () => {
    // EventSource 拿不到状态码，连不上最常见的原因是会话失效
    // （服务端重启后内存态会话全没了）。探一次：失效就回登录门，
    // 网络抖动则交给 EventSource 自带的自动重连。
    void probeSessionLost();
  };
  return () => source.close();
}

/**
 * 桌面版的 onCrash 对应 harness 子进程退出；网页版对应事件流断开
 * （服务重启、网关掐断、网络中断）。EventSource 会自动重连，所以只在
 * 第一次断开时通知一次，恢复收到消息后重置。
 */
function onCrash(listener: (message: string) => void): () => void {
  const source = new EventSource(EVENTS_URL, { withCredentials: true });
  let notified = false;
  source.onmessage = () => {
    notified = false;
  };
  source.onerror = () => {
    void probeSessionLost();
    if (notified) return;
    notified = true;
    listener("与服务器的事件连接已断开，正在尝试重连…");
  };
  return () => source.close();
}

let probeInFlight: Promise<boolean> | null = null;
async function probeSessionLost(): Promise<boolean> {
  // 并发的事件流可能同时探活，合并成一次。
  if (!probeInFlight) {
    probeInFlight = fetch("/api/session", { credentials: "same-origin" })
      .then(async (response) => {
        // /api/session 永远返回 200，用响应体判断；网络失败不算会话失效。
        if (response.status === 401) return true;
        const data = await response.json();
        return data?.authenticated !== true;
      })
      .catch(() => false)
      .finally(() => {
        probeInFlight = null;
      });
  }
  const lost = await probeInFlight;
  if (lost) notifySessionLost();
  return lost;
}

async function login(password: string): Promise<void> {
  const { status, data } = await postJson("/api/login", { password });
  if (status === 200 && data?.ok) return;
  throw new Error(String(data?.error || "登录失败"));
}

async function logout(): Promise<void> {
  await postJson("/api/logout", {});
}

async function authState(): Promise<"authed" | "guest"> {
  try {
    const response = await fetch("/api/session", { credentials: "same-origin" });
    const data = await response.json();
    return data?.authenticated ? "authed" : "guest";
  } catch {
    return "guest";
  }
}

/** 桌面版已由 preload 注入时什么都不做；浏览器里装上 fetch 版实现。 */
export function ensureBridge(): void {
  if (typeof window === "undefined" || window.quantAgent) return;
  window.quantAgent = {
    request,
    exportRun,
    onEvent,
    onCrash,
    platform: "web",
    login,
    logout,
    authState,
  };
}
