import axios from "axios";
import { api } from "@/lib/api";

export interface CalibrateStartResponse {
  session_id: string;
  total_steps: number;
  robot_type: string;
  step_ids: string[];
}

export interface CalibrateAckRequest {
  session_id: string;
  step_id: string;
}

export interface CalibrateCancelRequest {
  session_id: string;
}

export interface CalibrateStatusResponse {
  session_id: string;
  step_id: string;
  step_index: number;
  total_steps: number;
  progress: number;
  awaiting_user_input: boolean;
  started_at: string;
  robot_type: string;
}

export interface CalibrationSummary {
  calibration_id: string;
  completed_at: string;
  robot_type: string;
  step_ids: string[];
  result: "ok" | "error" | "cancelled";
  error?: Record<string, string> | null;
}

export class CalibrationConflictError extends Error {
  constructor(
    message: string,
    public readonly detail: Record<string, unknown>,
  ) {
    super(message);
    this.name = "CalibrationConflictError";
  }
}

function throwConflict(err: unknown): never {
  if (axios.isAxiosError(err) && err.response?.status === 409) {
    const data = (err.response.data ?? {}) as Record<string, unknown>;
    const detail = typeof data.detail === "string" ? data.detail : "conflict";
    throw new CalibrationConflictError(detail, data);
  }
  throw err;
}

export async function startCalibration(robotId: string): Promise<CalibrateStartResponse> {
  try {
    const { data } = await api.post<CalibrateStartResponse>(
      `/robots/${robotId}/calibrate/start`,
    );
    return data;
  } catch (err) {
    throwConflict(err);
  }
}

export async function ackCalibrationStep(
  robotId: string,
  payload: CalibrateAckRequest,
): Promise<void> {
  try {
    await api.post(`/robots/${robotId}/calibrate/ack`, payload);
  } catch (err) {
    throwConflict(err);
  }
}

export async function cancelCalibration(
  robotId: string,
  payload: CalibrateCancelRequest,
): Promise<void> {
  try {
    await api.post(`/robots/${robotId}/calibrate/cancel`, payload);
  } catch (err) {
    if (axios.isAxiosError(err) && err.response?.status === 404) return;
    throw err;
  }
}

export async function getCalibrationStatus(
  robotId: string,
): Promise<CalibrateStatusResponse | null> {
  try {
    const { data } = await api.get<CalibrateStatusResponse>(
      `/robots/${robotId}/calibrate/status`,
    );
    return data;
  } catch (err) {
    if (axios.isAxiosError(err) && err.response?.status === 404) return null;
    throw err;
  }
}

export async function getLatestCalibration(
  robotId: string,
): Promise<CalibrationSummary | null> {
  try {
    const { data } = await api.get<CalibrationSummary>(`/robots/${robotId}/calibration`);
    return data;
  } catch (err) {
    if (axios.isAxiosError(err) && err.response?.status === 404) return null;
    throw err;
  }
}

// -------- WS message types --------

export interface CalibrationStepEvent {
  type: "step";
  step_id: string;
  prompt: string;
  instruction: string;
  progress: number;
  awaiting_user_input: boolean;
  image_ref?: string | null;
}

export interface CalibrationJointFeedbackEvent {
  type: "joint_feedback";
  step_id: string;
  values: Record<string, number>;
}

export interface CalibrationDoneEvent {
  type: "done";
  result: "ok" | "error" | "cancelled";
  calibration_path?: string | null;
  calibration_id?: string | null;
  error?: { code: string; message: string } | null;
  summary?: Record<string, unknown> | null;
}

export type CalibrationServerEvent =
  | CalibrationStepEvent
  | CalibrationJointFeedbackEvent
  | CalibrationDoneEvent;

export type CalibrationClientEvent = { type: "heartbeat" };

export function calibrationWsPath(robotId: string, sessionId: string): string {
  const params = new URLSearchParams({ session_id: sessionId });
  return `/ws/robots/${robotId}/calibrate?${params.toString()}`;
}
