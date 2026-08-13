import { contextBridge, ipcRenderer } from "electron";

import type { HarnessEvent } from "../shared/protocol.js";

const ALLOWED_METHODS = new Set([
  "ping", "get_settings", "save_settings", "test_integration", "parse_report", "list_reports",
  "list_runs", "get_agents", "save_agent_config", "start_run", "retry_run", "get_run_snapshot",
  "get_prompt_workspace", "create_prompt_draft", "publish_prompt_version", "rollback_prompt_version",
  "get_workflows",
  "pause_run", "resume_run", "cancel_run", "steer_run",
]);

contextBridge.exposeInMainWorld("quantAgent", {
  request: (method: string, payload: Record<string, unknown> = {}) =>
    ALLOWED_METHODS.has(method)
      ? ipcRenderer.invoke("harness:request", method, payload)
      : Promise.reject(new Error(`不允许的桌面请求: ${method}`)),
  exportRun: (runId: string) => ipcRenderer.invoke("run:export", runId),
  onEvent: (listener: (event: HarnessEvent) => void) => {
    const handler = (_event: Electron.IpcRendererEvent, value: HarnessEvent) => listener(value);
    ipcRenderer.on("harness:event", handler);
    return () => ipcRenderer.removeListener("harness:event", handler);
  },
  onCrash: (listener: (message: string) => void) => {
    const handler = (_event: Electron.IpcRendererEvent, value: string) => listener(value);
    ipcRenderer.on("harness:crashed", handler);
    return () => ipcRenderer.removeListener("harness:crashed", handler);
  },
  platform: process.platform,
});
