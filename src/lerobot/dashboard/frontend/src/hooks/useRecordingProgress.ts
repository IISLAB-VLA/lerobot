import { useEffect, useRef, useState } from "react";
import { resolveWsUrl } from "@/lib/ws";
import {
  recordingsWsPath,
  type RecordingProgressEvent,
} from "@/lib/api/recordings";

export interface RecordingProgressState {
  event: RecordingProgressEvent | null;
  connected: boolean;
  reconnectAttempt: number;
}

interface Args {
  sessionId: string | null;
  enabled: boolean;
}

const RECONNECT_BASE_MS = 500;
const RECONNECT_MAX_MS = 15_000;

export function useRecordingProgress({
  sessionId,
  enabled,
}: Args): RecordingProgressState {
  const [event, setEvent] = useState<RecordingProgressEvent | null>(null);
  const [connected, setConnected] = useState(false);
  const [reconnectAttempt, setReconnectAttempt] = useState(0);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectAttemptRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const retryableRef = useRef(true);

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
      setEvent(null);
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
      const ws = new WebSocket(resolveWsUrl(recordingsWsPath(sessionId)));
      wsRef.current = ws;

      ws.addEventListener("open", () => {
        reconnectAttemptRef.current = 0;
        setReconnectAttempt(0);
        setConnected(true);
      });

      ws.addEventListener("message", (ev) => {
        if (typeof ev.data !== "string") return;
        try {
          const parsed = JSON.parse(ev.data) as RecordingProgressEvent;
          if (parsed && typeof parsed.type === "string") {
            setEvent(parsed);
            // Terminal events — server closes the socket after sending these.
            // Mark as non-retryable so the close handler doesn't schedule a
            // reconnect before the parent component has a chance to unmount.
            if (parsed.type === "stopped" || parsed.type === "error") {
              retryableRef.current = false;
            }
          }
        } catch {
          // ignore malformed frames
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
  }, [sessionId, enabled]);

  return { event, connected, reconnectAttempt };
}
