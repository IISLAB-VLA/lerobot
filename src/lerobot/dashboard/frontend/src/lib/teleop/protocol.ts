// Teleop WS contract (shared with server ClientFrame / ServerFrame).
// Reference: src/lerobot/dashboard/teleop/protocol.py

export type ClientFrameType = "action" | "deadman" | "heartbeat" | "mode" | "input";

export type ServerFrameType = "ack" | "state" | "error" | "telemetry";

export type TeleopMode = "idle" | "teleop" | "replay";

export type DeadmanState = "idle" | "arming" | "engaged" | "lockout";

export type LockoutReason = null | "heartbeat_timeout" | "e_stop";

export type KeyboardInputPayload = {
  kind: "keyboard";
  key: string;
  pressed: boolean;
};

export type MouseInputPayload = {
  kind: "mouse";
  dx: number;
  dy: number;
  buttons: number;
};

export type GamepadInputPayload = {
  kind: "gamepad";
  axes: number[];
  buttons: boolean[];
};

export type InputPayload = KeyboardInputPayload | MouseInputPayload | GamepadInputPayload;

export interface ActionPayload {
  values: Record<string, number>;
}

export interface DeadmanPayload {
  held: boolean;
}

export interface ModePayload {
  mode: TeleopMode;
}

export type HeartbeatPayload = Record<string, never>;

export type ClientFramePayload =
  | { type: "heartbeat"; payload: HeartbeatPayload }
  | { type: "deadman"; payload: DeadmanPayload }
  | { type: "input"; payload: InputPayload }
  | { type: "action"; payload: ActionPayload }
  | { type: "mode"; payload: ModePayload };

export type ClientFrame = {
  seq: number;
  ts_client_ms: number;
} & ClientFramePayload;

export interface AckPayload {
  seq: number;
  received_at_ms: number;
}

export interface TelemetryPayload {
  deadman: DeadmanState;
  lockout_reason: LockoutReason;
  forwarded: number;
  dropped_deadman: number;
  dropped_validation: number;
  aux_events_forwarded: number;
  aux_events_dropped: number;
  last_action_ts_ms: number | null;
}

export interface StatePayload {
  state: DeadmanState;
  reason?: LockoutReason;
}

export interface ErrorPayload {
  code: string;
  message: string;
}

export type ServerFramePayload =
  | { type: "ack"; payload: AckPayload }
  | { type: "telemetry"; payload: TelemetryPayload }
  | { type: "state"; payload: StatePayload }
  | { type: "error"; payload: ErrorPayload };

export type ServerFrame = {
  seq: number;
  ts_server_ms: number;
} & ServerFramePayload;

export function teleopWsPath(robotId: string): string {
  return `/ws/teleop/${robotId}`;
}

export function parseServerFrame(raw: string): ServerFrame | null {
  try {
    const obj = JSON.parse(raw) as unknown;
    if (!obj || typeof obj !== "object") return null;
    const r = obj as Record<string, unknown>;
    if (typeof r.seq !== "number" || typeof r.ts_server_ms !== "number") return null;
    if (typeof r.type !== "string") return null;
    const payload = (r.payload ?? {}) as Record<string, unknown>;
    switch (r.type) {
      case "ack":
        return {
          seq: r.seq,
          ts_server_ms: r.ts_server_ms,
          type: "ack",
          payload: payload as unknown as AckPayload,
        };
      case "telemetry":
        return {
          seq: r.seq,
          ts_server_ms: r.ts_server_ms,
          type: "telemetry",
          payload: payload as unknown as TelemetryPayload,
        };
      case "state":
        return {
          seq: r.seq,
          ts_server_ms: r.ts_server_ms,
          type: "state",
          payload: payload as unknown as StatePayload,
        };
      case "error":
        return {
          seq: r.seq,
          ts_server_ms: r.ts_server_ms,
          type: "error",
          payload: payload as unknown as ErrorPayload,
        };
      default:
        return null;
    }
  } catch {
    return null;
  }
}

// Close codes (from streaming-engineer-3 contract).
export const CLOSE_CONFLICT = 4001;
export const CLOSE_ROBOT_OFFLINE = 4002;

export const HEARTBEAT_INTERVAL_MS = 100;
export const DEADMAN_TIMEOUT_MS = 300;
