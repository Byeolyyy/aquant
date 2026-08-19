import { app, BrowserWindow } from "electron";
import { ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import path from "node:path";
import readline from "node:readline";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";

import {
  HarnessRequest,
  HarnessResponse,
  PROTOCOL_VERSION,
  SidecarMessage,
} from "../shared/protocol.js";

interface PendingRequest {
  resolve: (value: Record<string, unknown>) => void;
  reject: (error: Error) => void;
  timer: NodeJS.Timeout;
}

export class HarnessSidecar {
  private process: ChildProcessWithoutNullStreams | null = null;
  private pending = new Map<string, PendingRequest>();
  private readyPromise: Promise<void> | null = null;
  private resolveReady: (() => void) | null = null;

  start(): Promise<void> {
    if (this.readyPromise) return this.readyPromise;
    this.readyPromise = new Promise<void>((resolve) => {
      this.resolveReady = resolve;
    });

    const currentDir = path.dirname(fileURLToPath(import.meta.url));
    const projectRoot = process.env.QUANT_AGENT_ROOT
      ? path.resolve(process.env.QUANT_AGENT_ROOT)
      : path.resolve(currentDir, "../../../..");
    const pythonPath = process.env.QUANT_AGENT_PYTHON || "python";
    const harnessExe = app.isPackaged
      ? path.join(process.resourcesPath, "harness", "quant-agent-harness.exe")
      : null;
    const harnessSource = path.join(projectRoot, "services", "harness", "src");
    const existingPythonPath = process.env.PYTHONPATH || "";
    const pythonModulePath = existingPythonPath
      ? `${harnessSource}${path.delimiter}${existingPythonPath}`
      : harnessSource;

    this.process = spawn(harnessExe ?? pythonPath, harnessExe ? [] : ["-m", "quant_agent_harness.server"], {
      cwd: projectRoot,
      windowsHide: true,
      env: {
        ...process.env,
        PYTHONPATH: pythonModulePath,
        QUANT_AGENT_DATA_DIR: app.getPath("userData"),
        PYTHONUNBUFFERED: "1",
        PYTHONUTF8: "1",
        PYTHONIOENCODING: "utf-8",
      },
      stdio: ["pipe", "pipe", "pipe"],
    });

    const lines = readline.createInterface({ input: this.process.stdout });
    lines.on("line", (line) => this.handleLine(line));
    this.process.stderr.on("data", (chunk) => {
      console.error(`[harness] ${String(chunk).trimEnd()}`);
    });
    this.process.once("exit", (code, signal) => {
      const error = new Error(`Harness 已退出（code=${code}, signal=${signal}）`);
      for (const request of this.pending.values()) {
        clearTimeout(request.timer);
        request.reject(error);
      }
      this.pending.clear();
      this.process = null;
      this.readyPromise = null;
      this.resolveReady = null;
      for (const window of BrowserWindow.getAllWindows()) {
        window.webContents.send("harness:crashed", error.message);
      }
    });
    return this.readyPromise;
  }

  async request(method: string, payload: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
    await this.start();
    const child = this.process;
    if (!child) throw new Error("Harness 未启动");
    const requestId = randomUUID();
    const message: HarnessRequest = {
      type: "request",
      protocol_version: PROTOCOL_VERSION,
      request_id: requestId,
      method,
      payload,
    };
    return new Promise<Record<string, unknown>>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(requestId);
        reject(new Error(`Harness 请求超时: ${method}`));
      }, 60_000);
      this.pending.set(requestId, { resolve, reject, timer });
      child.stdin.write(`${JSON.stringify(message)}\n`, "utf8");
    });
  }

  stop(): void {
    if (this.process && !this.process.killed) this.process.kill();
  }

  private handleLine(line: string): void {
    let message: SidecarMessage;
    try {
      message = JSON.parse(line) as SidecarMessage;
    } catch {
      console.error("Harness 输出了无效 JSON", line);
      return;
    }
    if (message.type === "ready") {
      if (message.protocol_version !== PROTOCOL_VERSION) {
        console.error(`Harness 协议不兼容: ${message.protocol_version}`);
        return;
      }
      this.resolveReady?.();
      this.resolveReady = null;
      return;
    }
    if (message.type === "event") {
      for (const window of BrowserWindow.getAllWindows()) {
        window.webContents.send("harness:event", message.event);
      }
      return;
    }
    this.resolveResponse(message);
  }

  private resolveResponse(message: HarnessResponse): void {
    const pending = this.pending.get(message.request_id);
    if (!pending) return;
    clearTimeout(pending.timer);
    this.pending.delete(message.request_id);
    if (message.ok) pending.resolve(message.result || {});
    else pending.reject(new Error(message.error || "Harness 请求失败"));
  }
}
