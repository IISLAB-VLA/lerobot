import { useCallback, useEffect, useRef, useState } from "react";
import { resolveWsUrl } from "@/lib/ws";
import {
  CLOSE_CONFLICT,
  CLOSE_ROBOT_OFFLINE,
  HEARTBEAT_INTERVAL_MS,
  parseServerFrame,
  teleopWsPath,
  type ClientFramePayload,
  type DeadmanState,
  type InputPayload,
  type ServerFrame,
  type TelemetryPayload,
} from "@/lib/teleop/protocol";

export type TeleopConnectionPhase =
  | "idle"
  | "connecting"
  | "connected"
  | "reconnecting"
  | "conflict"
  | "offline"
  | "error"
  | "closed";

export interface TeleopSessionState {
  phase: TeleopConnectionPhase;
  deadman: DeadmanState;
  deadmanHeld: boolean;
  telemetry: TelemetryPayload | null;
  rttMs: number | null;
  lastError: string | null;
  reconnectAttempt: number;
}

export interface UseTeleopSessionArgs {
  robotId: string;
  enabled: boolean;
}

export interface UseTeleopSessionResult extends TeleopSessionState {
  sendInput: (payload: InputPayload) => void;
  setDeadmanHeld: (held: boolean) => void;
  sendMode: (mode: "idle" | "teleop" | "replay") => void;
}

const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;
const RTT_SAMPLE_CAP = 10;

export function useTeleopSession({
  robotId,
  enabled,
}: UseTeleopSessionArgs): UseTeleopSessionResult {
  const [phase, setPhase] = useState<TeleopConnectionPhase>("idle");
  const [deadman, setDeadman] = useState<DeadmanState>("idle");
  const [deadmanHeld, setDeadmanHeldState] = useState(false);
  const [telemetry, setTelemetry] = useState<TelemetryPayload | null>(null);
  const [rttMs, setRttMs] = useState<number | null>(null);
  const [lastError, setLastError] = useState<string | null>(null);
  const [reconnectAttempt, setReconnectAttempt] = useState(0);

  const wsRef = useRef<WebSocket | null>(null);
  const heartbeatRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectAttemptRef = useRef(0);
  const seqRef = useRef(1);
  const pendingSentAtRef = useRef<Map<number, number>>(new Map());
  const rttSamplesRef = useRef<number[]>([]);
  const deadmanHeldRef = useRef(false);
  const retryableRef = useRef(true);

  const send = useCallback((frame: ClientFramePayload): number | null => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return null;
    const seq = seqRef.current++;
    const tsClient = Date.now();
    const wire = {
      seq,
      ts_client_ms: tsClient,
      ...frame,
    };
    try {
      ws.send(JSON.stringify(wire));
    } catch {
      return null;
    }
    // Only track non-heartbeat frames for RTT.
    if (frame.type !== "heartbeat") {
      const map = pendingSentAtRef.current;
      map.set(seq, tsClient);
      if (map.size > 128) {
        const oldest = map.keys().next().value;
        if (oldest !== undefined) map.delete(oldest);
      }
    }
    return seq;
  }, []);

  const handleServerFrame = useCallback((frame: ServerFrame) => {
    if (frame.type === "ack") {
      const sentAt = pendingSentAtRef.current.get(frame.payload.seq);
      if (sentAt !== undefined) {
        pendingSentAtRef.current.delete(frame.payload.seq);
        const rtt = Date.now() - sentAt;
        const samples = rttSamplesRef.current;
        samples.push(rtt);
        if (samples.length > RTT_SAMPLE_CAP) samples.shift();
        const avg = samples.reduce((a, b) => a + b, 0) / samples.length;
        setRttMs(avg);
      }
    } else if (frame.type === "telemetry") {
      setTelemetry(frame.payload);
      setDeadman(frame.payload.deadman);
    } else if (frame.type === "state") {
      setDeadman(frame.payload.state);
      if (frame.payload.state === "lockout" && frame.payload.reason) {
        setLastError(`lockout: ${frame.payload.reason}`);
      }
    } else if (frame.type === "error") {
      setLastError(`${frame.payload.code}: ${frame.payload.message}`);
    }
  }, []);

  const clearHeartbeat = useCallback(() => {
    if (heartbeatRef.current !== null) {
      clearInterval(heartbeatRef.current);
      heartbeatRef.current = null;
    }
  }, []);

  const clearReconnect = useCallback(() => {
    if (reconnectTimerRef.current !== null) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }, []);

  const openSocketRef = useRef<() => void>(() => undefined);

  const scheduleReconnect = useCallback(() => {
    clearReconnect();
    const attempt = reconnectAttemptRef.current;
    const delay = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** attempt);
    reconnectAttemptRef.current = attempt + 1;
    setReconnectAttempt(attempt + 1);
    setPhase("reconnecting");
    reconnectTimerRef.current = setTimeout(() => {
      reconnectTimerRef.current = null;
      openSocketRef.current();
    }, delay);
  }, [clearReconnect]);

  const openSocket = useCallback(() => {
    if (!enabled || !robotId) return;
    clearHeartbeat();
    if (wsRef.current) {
      try {
        wsRef.current.close();
      } catch {
        // ignore
      }
      wsRef.current = null;
    }
    setPhase((p) => (p === "reconnecting" ? p : "connecting"));
    setLastError(null);
    const url = resolveWsUrl(teleopWsPath(robotId));
    const ws = new WebSocket(url);
    wsRef.current = ws;
    retryableRef.current = true;

    ws.addEventListener("open", () => {
      reconnectAttemptRef.current = 0;
      setReconnectAttempt(0);
      setPhase("connected");
      heartbeatRef.current = setInterval(() => {
        send({ type: "heartbeat", payload: {} });
      }, HEARTBEAT_INTERVAL_MS);
      // Re-assert deadman state after reconnect so the server doesn't lag behind the user intent.
      send({ type: "deadman", payload: { held: deadmanHeldRef.current } });
    });

    ws.addEventListener("message", (ev) => {
      if (typeof ev.data !== "string") return;
      const frame = parseServerFrame(ev.data);
      if (frame) handleServerFrame(frame);
    });

    ws.addEventListener("error", () => {
      setLastError("websocket error");
    });

    ws.addEventListener("close", (ev) => {
      clearHeartbeat();
      if (ev.code === CLOSE_CONFLICT) {
        retryableRef.current = false;
        setPhase("conflict");
        setLastError("another client is controlling this robot");
        return;
      }
      if (ev.code === CLOSE_ROBOT_OFFLINE) {
        retryableRef.current = false;
        setPhase("offline");
        setLastError("robot is offline");
        return;
      }
      if (!retryableRef.current) {
        setPhase("closed");
        return;
      }
      if (!enabled) {
        setPhase("closed");
        return;
      }
      scheduleReconnect();
    });
  }, [
    enabled,
    robotId,
    clearHeartbeat,
    send,
    handleServerFrame,
    scheduleReconnect,
  ]);

  useEffect(() => {
    openSocketRef.current = openSocket;
  }, [openSocket]);

  useEffect(() => {
    if (!enabled || !robotId) {
      clearHeartbeat();
      clearReconnect();
      retryableRef.current = false;
      if (wsRef.current) {
        try {
          wsRef.current.close(1000, "disabled");
        } catch {
          // ignore
        }
        wsRef.current = null;
      }
      reconnectAttemptRef.current = 0;
      setReconnectAttempt(0);
      setPhase("idle");
      setDeadman("idle");
      setTelemetry(null);
      setRttMs(null);
      setLastError(null);
      pendingSentAtRef.current.clear();
      rttSamplesRef.current = [];
      return undefined;
    }
    openSocket();
    return () => {
      retryableRef.current = false;
      clearHeartbeat();
      clearReconnect();
      if (wsRef.current) {
        try {
          wsRef.current.close(1000, "unmount");
        } catch {
          // ignore
        }
        wsRef.current = null;
      }
    };
  }, [enabled, robotId, openSocket, clearHeartbeat, clearReconnect]);

  const sendInput = useCallback(
    (payload: InputPayload) => {
      send({ type: "input", payload });
    },
    [send],
  );

  const setDeadmanHeld = useCallback(
    (held: boolean) => {
      if (deadmanHeldRef.current === held) return;
      deadmanHeldRef.current = held;
      setDeadmanHeldState(held);
      send({ type: "deadman", payload: { held } });
    },
    [send],
  );

  const sendMode = useCallback(
    (mode: "idle" | "teleop" | "replay") => {
      send({ type: "mode", payload: { mode } });
    },
    [send],
  );

  return {
    phase,
    deadman,
    deadmanHeld,
    telemetry,
    rttMs,
    lastError,
    reconnectAttempt,
    sendInput,
    setDeadmanHeld,
    sendMode,
  };
}
