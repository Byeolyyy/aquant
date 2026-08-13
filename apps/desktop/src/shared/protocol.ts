export const PROTOCOL_VERSION = 1;

export interface HarnessRequest {
  type: "request";
  protocol_version: number;
  request_id: string;
  method: string;
  payload: Record<string, unknown>;
}

export interface HarnessEvent {
  event_id: string;
  seq: number;
  run_id: string;
  kind: string;
  timestamp: string;
  agent_id: string;
  payload: Record<string, unknown>;
}

export interface HarnessResponse {
  type: "response";
  protocol_version: number;
  request_id: string;
  ok: boolean;
  result?: Record<string, unknown>;
  error?: string;
}

export interface HarnessEventEnvelope {
  type: "event";
  protocol_version: number;
  event: HarnessEvent;
}

export type SidecarMessage =
  | HarnessResponse
  | HarnessEventEnvelope
  | { type: "ready"; protocol_version: number };

