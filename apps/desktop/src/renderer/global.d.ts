import type { HarnessEvent } from "../shared/protocol";

declare global {
  interface Window {
    quantAgent: {
      request: (method: string, payload?: Record<string, unknown>) => Promise<Record<string, unknown>>;
      exportRun: (runId: string) => Promise<{ exported: boolean; path?: string }>;
      onEvent: (listener: (event: HarnessEvent) => void) => () => void;
      onCrash: (listener: (message: string) => void) => () => void;
      platform: string;
    };
  }
}

export {};
