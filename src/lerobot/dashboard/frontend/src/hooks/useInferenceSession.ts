import { useCallback, useEffect, useRef, useState } from "react";
import { resolveWsUrl } from "@/lib/ws";
import {
  inferenceWsPath,
  type InferenceServerEvent,
  type InferenceStepEvent,
} from "@/lib/api/inference";

interface Args {
  sessionId: string | null;
  enabled: boolean;
  capacity?: number;
}

export interface InferenceSubscription {
  steps: InferenceStepEvent[];
  terminal: { reason: string; steps: number } | null;
  errorMessage: string | null;
  connected: boolean;
  reconnectAttempt: number;
  clear: () => void;
}

const RECONNECT_BASE_MS = 500;
const RECONNECT_MAX_MS = 15_000;

export function useInferenceSession({
  sessionId,
  enabled,
  capacity = 200,
}: Args): InferenceSubscription {
  const [steps, setSteps] = useState<InferenceStepEvent[]>([]);
  const [terminal, setTerminal] = useState<InferenceSubscription["terminal"]>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [reconnectAttempt, setReconnectAttempt] = useState(0);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectAttemptRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const retryableRef = useRef(true);

  const clear = useCallback(() => {
    setSteps([]);
    setTerminal(null);
    setErrorMessage(null);
  }, []);

  useEffect(() => {
    if (!enabled || !sessionId) {
      retryableRef.current = false;
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
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
      setConnected(false);
      return undefined;
    }

    retryableRef.current = true;

    const open = () => {
      if (wsRef.current) {
        try {
          wsRef.current.close();
        } catch {
          // ignore
        }
      }
      const ws = new WebSocket(resolveWsUrl(inferenceWsPath(sessionId)));
      wsRef.current = ws;

      ws.addEventListener("open", () => {
        reconnectAttemptRef.current = 0;
        setReconnectAttempt(0);
        setConnected(true);
      });

      ws.addEventListener("message", (ev) => {
        if (typeof ev.data !== "string") return;
        let event: InferenceServerEvent;
        try {
          event = JSON.parse(ev.data) as InferenceServerEvent;
        } catch {
          return;
        }
        if (event.type === "step") {
          setSteps((prev) => {
            const next = prev.length >= capacity ? prev.slice(prev.length - capacity + 1) : prev.slice();
            next.push(event);
            return next;
          });
        } else if (event.type === "stopped") {
          setTerminal({ reason: event.reason, steps: event.steps });
          retryableRef.current = false;
        } else if (event.type === "error") {
          setErrorMessage(event.message);
          retryableRef.current = false;
        }
      });

      ws.addEventListener("close", () => {
        setConnected(false);
        if (!retryableRef.current) return;
        const attempt = reconnectAttemptRef.current;
        const delay = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** attempt);
        reconnectAttemptRef.current = attempt + 1;
        setReconnectAttempt(attempt + 1);
        reconnectTimerRef.current = setTimeout(() => {
          reconnectTimerRef.current = null;
          if (retryableRef.current) open();
        }, delay);
      });
    };

    open();

    return () => {
      retryableRef.current = false;
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      if (wsRef.current) {
        try {
          wsRef.current.close(1000, "unmount");
        } catch {
          // ignore
        }
        wsRef.current = null;
      }
    };
  }, [sessionId, enabled, capacity]);

  return { steps, terminal, errorMessage, connected, reconnectAttempt, clear };
}
