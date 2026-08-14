import { app, BrowserWindow, dialog, ipcMain, session, shell } from "electron";
import { writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { HarnessSidecar } from "./sidecar.js";

const sidecar = new HarnessSidecar();
const currentDir = path.dirname(fileURLToPath(import.meta.url));

function trustedSender(url: string): boolean {
  try {
    const parsed = new URL(url);
    const devServer = process.env.VITE_DEV_SERVER_URL;
    if (devServer) return parsed.origin === new URL(devServer).origin;
    if (parsed.protocol !== "file:") return false;
    return path.resolve(fileURLToPath(parsed)) === path.resolve(currentDir, "../../dist/index.html");
  } catch {
    return false;
  }
}

function createWindow(): BrowserWindow {
  const window = new BrowserWindow({
    width: 1480,
    height: 940,
    minWidth: 1120,
    minHeight: 720,
    backgroundColor: "#07111f",
    title: "QuantAgent Research Room",
    webPreferences: {
      preload: path.join(currentDir, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  const devServer = process.env.VITE_DEV_SERVER_URL;
  window.webContents.on("did-fail-load", (_event, code, description, url) => {
    console.error(`Renderer 加载失败（${code}）: ${description} · ${url}`);
  });
  window.webContents.on("render-process-gone", (_event, details) => {
    console.error("Renderer 进程退出", details);
  });
  window.webContents.setWindowOpenHandler(({ url }) => {
    try {
      const parsed = new URL(url);
      if (parsed.protocol === "https:" && !parsed.username && !parsed.password) void shell.openExternal(url);
    } catch {
      // Reject malformed or non-HTTPS evidence links.
    }
    return { action: "deny" };
  });
  window.webContents.on("will-navigate", (event, url) => {
    if (!trustedSender(url)) event.preventDefault();
  });
  if (devServer) void window.loadURL(devServer);
  else void window.loadFile(path.join(currentDir, "../../dist/index.html"));
  return window;
}

app.whenReady().then(async () => {
  session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  ipcMain.handle(
    "harness:request",
    (event, method: string, payload: Record<string, unknown>) => {
      if (!trustedSender(event.senderFrame?.url || "")) throw new Error("拒绝来自非受信页面的 IPC 请求");
      return sidecar.request(method, payload);
    }
  );
  ipcMain.handle("run:export", async (event, runId: string) => {
    if (!trustedSender(event.senderFrame?.url || "")) throw new Error("拒绝来自非受信页面的导出请求");
    const result = await sidecar.request("get_run_snapshot", { run_id: runId });
    const snapshot = result.snapshot as Record<string, unknown>;
    const owner = BrowserWindow.fromWebContents(event.sender);
    const options = {
      title: "导出研究报告",
      defaultPath: `QuantAgent-${runId.slice(0, 8)}.md`,
      filters: [{ name: "Markdown", extensions: ["md"] }],
    };
    const selected = owner
      ? await dialog.showSaveDialog(owner, options)
      : await dialog.showSaveDialog(options);
    if (selected.canceled || !selected.filePath) return { exported: false };
    await writeFile(selected.filePath, snapshotToMarkdown(snapshot), "utf8");
    return { exported: true, path: selected.filePath };
  });
  createWindow();
  try {
    await sidecar.start();
  } catch (error) {
    console.error("Harness 启动失败", error);
  }
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => sidecar.stop());

function snapshotToMarkdown(snapshot: Record<string, unknown>): string {
  const final = (snapshot.final || {}) as Record<string, unknown>;
  const metrics = (snapshot.metrics || {}) as Record<string, unknown>;
  const events = Array.isArray(snapshot.events) ? snapshot.events as Array<Record<string, unknown>> : [];
  const names: Record<string, string> = {
    coordinator: "统筹 Agent",
    quant_signal: "量化信号 Agent",
    company_industry: "公司与行业 Agent",
    global_market: "外围市场 Agent",
    risk: "风险 Agent",
    market_event: "市场事件 Agent（历史）",
    evidence_risk: "证据与风险 Agent（历史）",
  };
  const lines = [
    `# ${String(final.title || "QuantAgent 研究报告")}`,
    "",
    `- Run ID: \`${String(snapshot.run_id || "")}\``,
    `- 状态: ${String(snapshot.status || "")}`,
    `- 创建时间: ${String(snapshot.created_at || "")}`,
    `- 耗时: ${String(metrics.duration_seconds || 0)} 秒`,
    `- 证据: ${String(metrics.evidence_count || 0)} 条`,
    `- 风险项: ${String(metrics.risk_count || 0)} 条`,
    "",
    "## 最终综合",
    "",
    valueToText(final.executive_summary),
    "",
    "### 量化依据",
    "",
    valueToText(final.signal_interpretation),
    "",
    "### 消息面",
    "",
    valueToText(final.news_summary),
    "",
    "### 风险",
    "",
    valueToText(final.risk_notes),
    "",
    "### 证据缺口",
    "",
    valueToText(final.evidence_gaps),
  ];
  const evidence = new Map<string, Record<string, unknown>>();
  for (const event of events) {
    if (event.kind !== "agent.message") continue;
    const agentId = String(event.agent_id || "");
    const payload = (event.payload || {}) as Record<string, unknown>;
    if (Array.isArray(payload.evidence)) {
      for (const item of payload.evidence as Array<Record<string, unknown>>) {
        const evidenceId = String(item.evidence_id || "");
        if (evidenceId) evidence.set(evidenceId, item);
      }
    }
    const content = valueToText(payload.summary || payload.content);
    if (!content) continue;
    lines.push("", `## ${names[agentId] || agentId}`, "", content);
  }
  if (evidence.size) {
    lines.push("", "## 证据登记", "");
    for (const item of evidence.values()) {
      const title = String(item.title || "未命名资料");
      const url = String(item.url || "");
      const sourceType = String(item.source_type || "unknown");
      lines.push(`- ${url.startsWith("https://") ? `[${title}](${url})` : title} · ${sourceType} · ${String(item.published_at || "日期未知")}`);
    }
  }
  lines.push("", "---", "仅供研究解读与风险提示，不构成买卖或仓位建议。", "");
  return lines.join("\n");
}

function valueToText(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map((item) => `- ${valueToText(item)}`).join("\n");
  if (value && typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, item]) => `- ${key}: ${valueToText(item)}`)
      .join("\n");
  }
  return value == null ? "" : String(value);
}
