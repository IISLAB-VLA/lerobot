// Benchmark API client (Task #16).
// Backend: src/lerobot/dashboard/api/benchmarks.py
// WS:      /ws/benchmarks/{run_id}

import axios from "axios";
import { api } from "@/lib/api";

export type RunStatus =
  | "queued"
  | "running"
  | "completed"
  | "cancelled"
  | "failed";

export interface BenchmarkInfo {
  env_name: string;
  task: string | null;
  fps: number;
  config_type: string;
}

/** Narrow result type per robotics-integrator-3 RFC answer. */
export interface BenchmarkRunResult {
  episodes_completed: number;
}

export interface BenchmarkRunSummary {
  run_id: string;
  env_name: string;
  task: string | null;
  policy_refs: string[];
  episodes: number;
  seed: number | null;
  status: RunStatus;
  /** 0.0–1.0 */
  progress: number;
  current_episode: number;
  steps_total: number;
  last_reward: number | null;
  started_at: string;
  completed_at: string | null;
  result: BenchmarkRunResult | null;
  error: { code: string; message: string } | null;
  storage_dir: string;
}

export interface BenchmarkStartRequest {
  env_name: string;
  /** Empty list → RandomAction runner; non-empty → policy runner. */
  policy_refs?: string[];
  episodes: number;
  seed?: number | null;
  task?: string | null;
}

// ── WS events ────────────────────────────────────────────────────────────────

export interface BenchmarkStepEvent {
  type: "step";
  episode: number;
  step: number;
  reward: number;
  done: boolean;
  progress: number;
  /** Relative path (storage_dir-relative) — for parquet only, FE should use obs_jpg_url. */
  obs_jpg_path?: string | null;
  /** Absolute URL ready for <img src>. Absent when env has no visual obs. */
  obs_jpg_url?: string | null;
}

export interface BenchmarkPreviewReadyEvent {
  type: "preview_ready";
  episode: number;
  policy_slug: string;
  /** storage_dir-relative path — FE should use preview_url. */
  preview_path: string;
  /** Absolute URL ready for <video src>. */
  preview_url: string;
}

export interface BenchmarkRunEvent {
  type: "run";
  summary: BenchmarkRunSummary;
}

export interface BenchmarkErrorEvent {
  type: "error";
  code: string;
  message: string;
}

export interface BenchmarkDoneEvent {
  type: "done";
  run_id: string;
  status: RunStatus;
  error?: { code: string; message: string } | null;
  result?: Record<string, unknown> | null;
}

export type BenchmarkServerEvent =
  | BenchmarkStepEvent
  | BenchmarkRunEvent
  | BenchmarkErrorEvent
  | BenchmarkDoneEvent
  | BenchmarkPreviewReadyEvent;

// ── REST clients ──────────────────────────────────────────────────────────────

export async function listBenchmarkEnvs(): Promise<BenchmarkInfo[]> {
  try {
    const { data } = await api.get<{ benchmarks: BenchmarkInfo[] }>("/benchmarks");
    return data.benchmarks;
  } catch (err) {
    if (axios.isAxiosError(err) && err.response?.status === 404) return [];
    throw err;
  }
}

export async function listBenchmarkRuns(): Promise<BenchmarkRunSummary[]> {
  try {
    const { data } = await api.get<{ runs: BenchmarkRunSummary[] }>("/benchmarks/runs");
    return data.runs;
  } catch (err) {
    if (axios.isAxiosError(err) && err.response?.status === 404) return [];
    throw err;
  }
}

export async function getBenchmarkRun(
  runId: string,
): Promise<BenchmarkRunSummary | null> {
  try {
    const { data } = await api.get<BenchmarkRunSummary>(
      `/benchmarks/runs/${encodeURIComponent(runId)}`,
    );
    return data;
  } catch (err) {
    if (axios.isAxiosError(err) && err.response?.status === 404) return null;
    throw err;
  }
}

export async function startBenchmarkRun(
  payload: BenchmarkStartRequest,
): Promise<BenchmarkRunSummary> {
  const { data } = await api.post<BenchmarkRunSummary>("/benchmarks/runs", payload);
  return data;
}

export async function cancelBenchmarkRun(
  runId: string,
): Promise<BenchmarkRunSummary> {
  const { data } = await api.post<BenchmarkRunSummary>(
    `/benchmarks/runs/${encodeURIComponent(runId)}/cancel`,
  );
  return data;
}

export function benchmarkWsPath(runId: string): string {
  return `/ws/benchmarks/${runId}`;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

export function isTerminalStatus(status: RunStatus): boolean {
  return status === "completed" || status === "cancelled" || status === "failed";
}

export function runnerLabel(run: BenchmarkRunSummary): string {
  return run.policy_refs.length > 0
    ? run.policy_refs[0] ?? "policy"
    : "random action";
}
