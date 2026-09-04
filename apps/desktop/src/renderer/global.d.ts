import type { HarnessEvent } from "../shared/protocol";

declare global {
  interface Window {
    quantAgent: {
      request: (method: string, payload?: Record<string, unknown>) => Promise<Record<string, unknown>>;
      exportRun: (runId: string) => Promise<{ exported: boolean; path?: string }>;
      onEvent: (listener: (event: HarnessEvent) => void) => () => void;
      onCrash: (listener: (message: string) => void) => () => void;
      platform: string;
      // 仅网页版提供：桌面版没有登录概念，这三项为 undefined，
      // 渲染层据此跳过登录门。
      login?: (password: string) => Promise<void>;
      logout?: () => Promise<void>;
      authState?: () => Promise<"authed" | "guest">;
    };
  }
}

export {};
