import { useCallback, useEffect, useRef, useState } from "react";
import {
  ackCalibrationStep,
  calibrationWsPath,
  cancelCalibration,
  getCalibrationStatus,
  startCalibration,
  type CalibrateStartResponse,
  type CalibrationConflictError,
  type CalibrationDoneEvent,
  type CalibrationServerEvent,
  type CalibrationStepEvent,
} from "@/lib/api/calibration";
import { WsClient } from "@/lib/ws";

export type CalibrationPhase =
  | "idle"
  | "starting"
  | "connecting"
  | "awaiting_user_input"
  | "working"
  | "reconnecting"
  | "done"
  | "error";

export interface CalibrationState {
  phase: CalibrationPhase;
  sessionId: string | null;
  totalSteps: number | null;
  stepIds: string[];
  currentStep: CalibrationStepEvent | null;
  jointValues: Record<string, number>;
  done: CalibrationDoneEvent | null;
  error: string | null;
  reconnectAttempt: number;
}

const HEARTBEAT_INTERVAL_MS = 30_000;
const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;

interface UseCalibrationSessionArgs {
  robotId: string;
}

interface UseCalibrationSessionResult extends CalibrationState {
  start: () => Promise<void>;
  ack: () => Promise<void>;
  cancel: () => Promise<void>;
}

export function useCalibrationSession({
  robotId,
}: UseCalibrationSessionArgs): UseCalibrationSessionResult {
  const [phase, setPhase] = useState<CalibrationPhase>("idle");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [totalSteps, setTotalSteps] = useState<number | null>(null);
  const [stepIds, setStepIds] = useState<string[]>([]);
  const [currentStep, setCurrentStep] = useState<CalibrationStepEvent | null>(null);
  const [jointValues, setJointValues] = useState<Record<string, number>>({});
  const [done, setDone] = useState<CalibrationDoneEvent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reconnectAttempt, setReconnectAttempt] = useState(0);

  const wsRef = useRef<WsClient | null>(null);
  const heartbeatRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectAttemptRef = useRef(0);
  const sessionIdRef = useRef<string | null>(null);
  const phaseRef = useRef<CalibrationPhase>("idle");
  const currentStepRef = useRef<CalibrationStepEvent | null>(null);

  const setPhaseBoth = useCallback((next: CalibrationPhase) => {
    phaseRef.current = next;
    setPhase(next);
  }, []);

  useEffect(() => {
    currentStepRef.current = currentStep;
  }, [currentStep]);

  useEffect(() => {
    const ws = wsRef.current;
    const heartbeat = heartbeatRef.current;
    const reconnect = reconnectTimerRef.current;
    return () => {
      if (ws) ws.close();
      if (heartbeat !== null) clearInterval(heartbeat);
      if (reconnect !== null) clearTimeout(reconnect);
    };
    // Cleanup only on unmount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const openWs = useCallback(
    (sid: string) => {
      // Close any existing connection first.
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      if (heartbeatRef.current !== null) {
        clearInterval(heartbeatRef.current);
        heartbeatRef.current = null;
      }

      const client = new WsClient(
        calibrationWsPath(robotId, sid),
        {
          onOpen: () => {
            reconnectAttemptRef.current = 0;
            setReconnectAttempt(0);
            heartbeatRef.current = setInterval(() => {
              wsRef.current?.sendJson({ type: "heartbeat" });
            }, HEARTBEAT_INTERVAL_MS);
          },
          onMessage: (ev) => {
            if (typeof ev.data !== "string") return;
            let event: CalibrationServerEvent;
            try {
              event = JSON.parse(ev.data) as CalibrationServerEvent;
            } catch {
              return;
            }
            if (event.type === "step") {
              setCurrentStep(event);
              setPhaseBoth(event.awaiting_user_input ? "awaiting_user_input" : "working");
            } else if (event.type === "joint_feedback") {
              setJointValues((prev) => ({ ...prev, ...event.values }));
            } else if (event.type === "done") {
              setDone(event);
              if (event.result === "error") {
                setError(event.error?.message ?? "calibration failed");
                setPhaseBoth("error");
              } else {
                setPhaseBoth("done");
              }
            }
          },
          onClose: () => {
            if (heartbeatRef.current !== null) {
              clearInterval(heartbeatRef.current);
              heartbeatRef.current = null;
            }
            if (
              phaseRef.current === "done" ||
              phaseRef.current === "idle" ||
              phaseRef.current === "error"
            ) return;

            const attempt = reconnectAttemptRef.current;
            const delay = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** attempt);
            reconnectAttemptRef.current = attempt + 1;
            setReconnectAttempt(attempt + 1);
            setPhaseBoth("reconnecting");
            reconnectTimerRef.current = setTimeout(async () => {
              reconnectTimerRef.current = null;
              try {
                const status = await getCalibrationStatus(robotId);
                if (!status) {
                  setError("session expired");
                  setPhaseBoth("error");
                  return;
                }
                openWs(sid);
              } catch (err) {
                setError(err instanceof Error ? err.message : "reconnect failed");
                setPhaseBoth("error");
              }
            }, delay);
          },
        },
        { reconnect: false },
      );
      client.connect();
      wsRef.current = client;
    },
    [robotId, setPhaseBoth],
  );

  const start = useCallback(async () => {
    const p = phaseRef.current;
    if (p !== "idle" && p !== "error" && p !== "done") return;
    setError(null);
    setDone(null);
    setCurrentStep(null);
    setJointValues({});
    reconnectAttemptRef.current = 0;
    setReconnectAttempt(0);
    setPhaseBoth("starting");
    try {
      const resp: CalibrateStartResponse = await startCalibration(robotId);
      sessionIdRef.current = resp.session_id;
      setSessionId(resp.session_id);
      setTotalSteps(resp.total_steps);
      setStepIds(resp.step_ids);
      setPhaseBoth("connecting");
      openWs(resp.session_id);
    } catch (err) {
      const message =
        (err as CalibrationConflictError | Error | undefined)?.message ?? "failed to start";
      setError(message);
      setPhaseBoth("error");
    }
  }, [robotId, setPhaseBoth, openWs]);

  const ack = useCallback(async () => {
    const sid = sessionIdRef.current;
    const step = currentStepRef.current;
    if (!sid || !step) return;
    try {
      await ackCalibrationStep(robotId, { session_id: sid, step_id: step.step_id });
      setPhaseBoth("working");
    } catch (err) {
      setError(err instanceof Error ? err.message : "ack failed");
    }
  }, [robotId, setPhaseBoth]);

  const cancel = useCallback(async () => {
    const sid = sessionIdRef.current;
    if (reconnectTimerRef.current !== null) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    if (heartbeatRef.current !== null) {
      clearInterval(heartbeatRef.current);
      heartbeatRef.current = null;
    }
    if (sid) {
      try {
        await cancelCalibration(robotId, { session_id: sid });
      } catch {
        // best-effort
      }
    }
    sessionIdRef.current = null;
    setSessionId(null);
    setPhaseBoth("idle");
  }, [robotId, setPhaseBoth]);

  return {
    phase,
    sessionId,
    totalSteps,
    stepIds,
    currentStep,
    jointValues,
    done,
    error,
    reconnectAttempt,
    start,
    ack,
    cancel,
  };
}
