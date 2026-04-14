import axios from "axios";
import { api } from "@/lib/api";

export type RecordingStatus =
  | "starting"
  | "recording"
  | "stopping"
  | "saved"
  | "discarded"
  | "failed";

export interface RecordingSession {
  id: string;
  robot_id: string;
  dataset_name: string;
  dataset_path: string;
  task_description: string;
  fps: number;
  status: RecordingStatus;
  frames_captured: number;
  drop_count: number;
  disk_bytes: number;
  started_at: string;
  stopped_at: string | null;
  error: string | null;
  episode_index: number | null;
  saved: boolean;
}

export interface StartRecordingRequest {
  robot_id: string;
  dataset_name: string;
  task_description: string;
  fps: number;
  use_videos?: boolean;
}

export interface RecordingProgressEvent {
  type: "progress" | "stopped" | "error";
  frames_captured: number;
  duration_s: number;
  disk_bytes: number;
  drop_count: number;
  saved?: boolean | null;
  episode_index?: number | null;
  message?: string | null;
}

export class RecorderConflictError extends Error {
  constructor(
    message: string,
    public readonly detail: Record<string, unknown>,
  ) {
    super(message);
    this.name = "RecorderConflictError";
  }
}

function liftConflict(err: unknown): never {
  if (axios.isAxiosError(err)) {
    const status = err.response?.status;
    if (status === 409 || status === 400) {
      const data = (err.response?.data ?? {}) as Record<string, unknown>;
      const detail = typeof data.detail === "string" ? data.detail : "recording error";
      throw new RecorderConflictError(detail, data);
    }
  }
  throw err;
}

export async function listRecordings(): Promise<RecordingSession[]> {
  try {
    const { data } = await api.get<RecordingSession[]>("/recordings");
    return data;
  } catch (err) {
    if (axios.isAxiosError(err) && err.response?.status === 404) return [];
    throw err;
  }
}

export async function startRecording(
  payload: StartRecordingRequest,
): Promise<RecordingSession> {
  try {
    const { data } = await api.post<RecordingSession>("/recordings", payload);
    return data;
  } catch (err) {
    liftConflict(err);
  }
}

export async function getRecording(sessionId: string): Promise<RecordingSession | null> {
  try {
    const { data } = await api.get<RecordingSession>(`/recordings/${sessionId}`);
    return data;
  } catch (err) {
    if (axios.isAxiosError(err) && err.response?.status === 404) return null;
    throw err;
  }
}

export async function stopRecording(
  sessionId: string,
  save: boolean,
): Promise<RecordingSession> {
  try {
    const { data } = await api.post<RecordingSession>(`/recordings/${sessionId}/stop`, {
      save,
    });
    return data;
  } catch (err) {
    liftConflict(err);
  }
}

export function recordingsWsPath(sessionId: string): string {
  return `/ws/recordings/${sessionId}`;
}

export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const mm = String(Math.floor(s / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return `${mm}:${ss}`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}
