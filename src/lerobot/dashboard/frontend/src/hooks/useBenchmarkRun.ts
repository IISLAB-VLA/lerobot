// WebSocket hook for live benchmark run updates (Task #16 Phase B).
//
// Protocol (per robotics-integrator-3 RFC answers):
// - On connect: server immediately emits {type:"run", summary:{...}}
// - Then: step events, preview_ready events, run summary updates, done
// - Already-terminal runs: run → done → socket closed
// - No REST polling needed — WS is source of truth.

import { useCallback, useEffect, useRef, useState } from "react";
import { resolveWsUrl } from "@/lib/ws";
import {
  benchmarkWsPath,
  isTerminalStatus,
  type BenchmarkRunSummary,
  type BenchmarkServerEvent,
  type BenchmarkStepEvent,
} from "@/lib/api/benchmarks";

const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;
const STEP_RING_CAP = 500;

export interface BenchmarkEpisodePreview {
  episode: number;
  preview_url: string;
  policy_slug: string;
}

export interface UseBenchmarkRunResult {
  /** Current run summary (null until first WS frame arrives). */
  summary: BenchmarkRunSummary | null;
  /** Live step ring buffer (cap 500, newest last). */
  steps: BenchmarkStepEvent[];
  /** Previews collected from preview_ready events, indexed by episode. */
  previews: BenchmarkEpisodePreview[];
  /** URL from most recent step that has obs_jpg_url — null if none yet. */
  latestObsUrl: string | null;
  terminal: boolean;
  errorMessage: string | null;
  connected: boolean;
  reconnectAttempt: number;
}

export function useBenchmarkRun(runId: string | null): UseBenchmarkRunResult {
  const [summary, setSummary] = useState<BenchmarkRunSummary | null>(null);
  const [steps, setSteps] = useState<BenchmarkStepEvent[]>([]);
  const [previews, setPreviews] = useState<BenchmarkEpisodePreview[]>([]);
  const [latestObsUrl, setLatestObsUrl] = useState<string | null>(null);
  const [terminal, setTerminal] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [reconnectAttempt, setReconnectAttempt] = useState(0);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectAttemptRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const retryableRef = useRef(true);

  const handleEvent = useCallback((event: BenchmarkServerEvent) => {
    if (event.type === "run") {
      setSummary(event.summary);
      if (isTerminalStatus(event.summary.status)) {
        retryableRef.current = false;
      }
    } else if (event.type === "step") {
      setSteps((prev) => {
        const next =
          prev.length >= STEP_RING_CAP
            ? prev.slice(prev.length - STEP_RING_CAP + 1)
            : prev.slice();
        next.push(event);
        return next;
      });
      if (event.obs_jpg_url) {
        setLatestObsUrl(event.obs_jpg_url);
      }
    } else if (event.type === "preview_ready") {
      setPreviews((prev) => {
        // Deduplicate by episode.
        const next = prev.filter((p) => p.episode !== event.episode);
        next.push({
          episode: event.episode,
          preview_url: event.preview_url,
          policy_slug: event.policy_slug,
        });
        return next.sort((a, b) => a.episode - b.episode);
      });
    } else if (event.type === "done") {
      setTerminal(true);
      retryableRef.current = false;
      if (event.status === "failed" && event.error) {
        setErrorMessage(`${event.error.code}: ${event.error.message}`);
      }
    } else if (event.type === "error") {
      setErrorMessage(`${event.code}: ${event.message}`);
      retryableRef.current = false;
    }
  }, []);

  useEffect(() => {
    if (!runId) return undefined;

    retryableRef.current = true;
    reconnectAttemptRef.current = 0;

    const open = () => {
      if (wsRef.current) {
        try { wsRef.current.close(); } catch { /* ignore */ }
      }
      const ws = new WebSocket(resolveWsUrl(benchmarkWsPath(runId)));
      wsRef.current = ws;

      ws.addEventListener("open", () => {
        reconnectAttemptRef.current = 0;
        setReconnectAttempt(0);
        setConnected(true);
      });

      ws.addEventListener("message", (ev) => {
        if (typeof ev.data !== "string") return;
        try {
          const event = JSON.parse(ev.data) as BenchmarkServerEvent;
          if (event && typeof event.type === "string") handleEvent(event);
        } catch { /* ignore malformed */ }
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
        try { wsRef.current.close(1000, "unmount"); } catch { /* ignore */ }
        wsRef.current = null;
      }
    };
  }, [runId, handleEvent]);

  return {
    summary,
    steps,
    previews,
    latestObsUrl,
    terminal,
    errorMessage,
    connected,
    reconnectAttempt,
  };
}
