// VLA inference client (Task #14).
// Backend phase 1a/1b in progress; types mirror RFC_vla_inference.md.

import axios from "axios";
import { api } from "@/lib/api";

export interface PolicyDescriptor {
  repo_id: string;
  policy_type: string;
  root: string;
  num_parameters: number | null;
  last_modified: string;
  observation_features: Record<string, unknown> | null;
  action_features: Record<string, unknown> | null;
  supports_language: boolean;
  device?: string | null;
}

export type InferenceStatus =
  | "starting"
  | "running"
  | "stopping"
  | "stopped"
  | "failed";

export interface InferenceSession {
  id: string;
  robot_id: string;
  repo_id: string;
  fps: number;
  dry_run: boolean;
  task_description: string;
  status: InferenceStatus;
  step: number;
  started_at: string;
  stopped_at: string | null;
  error: string | null;
  last_latency_ms: number | null;
}

export interface StartInferenceRequest {
  robot_id: string;
  repo_id: string;
  fps?: number;
  task_description: string;
  dry_run: boolean;
}

export interface CommandRequest {
  text: string;
}

export interface InferenceStepEvent {
  type: "step";
  step: number;
  action: number[];
  latency_ms: number;
  image_hash?: string | null;
  text?: string | null;
}

export interface InferenceStoppedEvent {
  type: "stopped";
  reason: string;
  steps: number;
}

export interface InferenceErrorEvent {
  type: "error";
  message: string;
}

export type InferenceServerEvent =
  | InferenceStepEvent
  | InferenceStoppedEvent
  | InferenceErrorEvent;

export class InferenceConflictError extends Error {
  constructor(
    message: string,
    public readonly detail: Record<string, unknown>,
  ) {
    super(message);
    this.name = "InferenceConflictError";
  }
}

function liftConflict(err: unknown): never {
  if (axios.isAxiosError(err)) {
    const status = err.response?.status;
    if (status === 404 || status === 409 || status === 400) {
      const data = (err.response?.data ?? {}) as Record<string, unknown>;
      const detail = typeof data.detail === "string" ? data.detail : "inference error";
      throw new InferenceConflictError(detail, data);
    }
  }
  throw err;
}

export async function listPolicies(): Promise<PolicyDescriptor[]> {
  try {
    const { data } = await api.get<PolicyDescriptor[]>("/policies");
    return data;
  } catch (err) {
    if (axios.isAxiosError(err) && err.response?.status === 404) return [];
    throw err;
  }
}

export async function getPolicy(repoId: string): Promise<PolicyDescriptor | null> {
  try {
    const { data } = await api.get<PolicyDescriptor>(
      `/policies/${encodeURIComponent(repoId)}`,
    );
    return data;
  } catch (err) {
    if (axios.isAxiosError(err) && err.response?.status === 404) return null;
    throw err;
  }
}

export async function listInferenceSessions(): Promise<InferenceSession[]> {
  try {
    const { data } = await api.get<InferenceSession[]>("/inference");
    return data;
  } catch (err) {
    if (axios.isAxiosError(err) && err.response?.status === 404) return [];
    throw err;
  }
}

export async function startInference(
  payload: StartInferenceRequest,
): Promise<InferenceSession> {
  try {
    const { data } = await api.post<InferenceSession>("/inference", payload);
    return data;
  } catch (err) {
    liftConflict(err);
  }
}

export async function getInferenceSession(
  sessionId: string,
): Promise<InferenceSession | null> {
  try {
    const { data } = await api.get<InferenceSession>(`/inference/${sessionId}`);
    return data;
  } catch (err) {
    if (axios.isAxiosError(err) && err.response?.status === 404) return null;
    throw err;
  }
}

export async function stopInference(sessionId: string): Promise<InferenceSession> {
  try {
    const { data } = await api.post<InferenceSession>(`/inference/${sessionId}/stop`);
    return data;
  } catch (err) {
    liftConflict(err);
  }
}

export async function setInferenceCommand(
  sessionId: string,
  payload: CommandRequest,
): Promise<void> {
  try {
    await api.post(`/inference/${sessionId}/command`, payload);
  } catch (err) {
    liftConflict(err);
  }
}

export function inferenceWsPath(sessionId: string): string {
  return `/ws/inference/${sessionId}`;
}
