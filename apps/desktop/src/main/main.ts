import { app, BrowserWindow, dialog, ipcMain, session, shell } from "electron";
import { writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { snapshotToMarkdown } from "../shared/export-markdown.js";
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
    backgroundColor: "#f7f2e7",
    title: "aquant · 研房",
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
      defaultPath: `aquant-${runId.slice(0, 8)}.md`,
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
