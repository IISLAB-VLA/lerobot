import { useEffect, useMemo, useRef, useState } from "react";
import {
  getBatchStats,
  parseStreamStats,
  StreamingUnavailableError,
  type ParsedStreamStats,
} from "@/lib/api/streams";

interface UseStreamStatsArgs {
  sessionIds: string[];
  enabled: boolean;
  intervalMs?: number;
}

export interface StreamStatsState {
  byId: Map<string, ParsedStreamStats>;
  unavailable: boolean;
  error: string | null;
}

export function useStreamStats({
  sessionIds,
  enabled,
  intervalMs = 1_000,
}: UseStreamStatsArgs): StreamStatsState {
  const [byId, setById] = useState<Map<string, ParsedStreamStats>>(() => new Map());
  const [unavailable, setUnavailable] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sessionIdsRef = useRef<string[]>(sessionIds);

  const sortedKey = useMemo(() => [...sessionIds].sort().join(","), [sessionIds]);

  useEffect(() => {
    sessionIdsRef.current = sessionIds;
  }, [sessionIds]);

  useEffect(() => {
    if (!enabled || sessionIds.length === 0) {
      setById((prev) => (prev.size === 0 ? prev : new Map()));
      return undefined;
    }

    let cancelled = false;

    const tick = async () => {
      try {
        const ids = sessionIdsRef.current;
        if (ids.length === 0) return;
        const resp = await getBatchStats(ids);
        if (cancelled) return;
        const next = new Map<string, ParsedStreamStats>();
        for (const session of resp.sessions) {
          next.set(session.session_id, parseStreamStats(session.session_id, session.entries));
        }
        setById(next);
        setUnavailable(false);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof StreamingUnavailableError) {
          setUnavailable(true);
          return;
        }
        setError(err instanceof Error ? err.message : "stats poll failed");
      }
    };

    void tick();
    const id = window.setInterval(() => {
      void tick();
    }, intervalMs);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
    // sortedKey changes whenever the *set* of sessionIds changes; the ref
    // covers in-place updates, so the polling timer survives most re-renders.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, sortedKey, intervalMs]);

  return { byId, unavailable, error };
}
