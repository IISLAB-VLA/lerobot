import axios from "axios";
import { api } from "@/lib/api";

export interface OfferRequest {
  robot_id: string;
  camera_id: string;
  sdp: string;
  type: "offer";
}

export interface OfferResponse {
  session_id: string;
  sdp: string;
  type: string;
  applied_codecs: string[];
}

export interface IceRequest {
  candidate: string;
  sdpMid: string | null;
  sdpMLineIndex: number | null;
}

export interface QualityRequest {
  width?: number;
  height?: number;
  fps?: number;
  bitrate_kbps?: number;
}

export interface QualityResponse {
  width: number | null;
  height: number | null;
  fps: number | null;
  bitrate_kbps: number | null;
}

export interface StatsResponse {
  session_id: string;
  entries: Array<Record<string, unknown>>;
}

export interface BatchStatsResponse {
  sessions: StatsResponse[];
}

export interface ParsedStreamStats {
  fps: number | null;
  bitrateKbps: number | null;
  rttMs: number | null;
  jitterMs: number | null;
  framesDecoded: number | null;
  framesDropped: number | null;
}

interface InboundRtpEntry {
  type: "inbound-rtp";
  bytesReceived?: number;
  framesPerSecond?: number;
  framesDecoded?: number;
  framesDropped?: number;
  jitter?: number;
  timestamp?: number;
}

interface RemoteCandidatePair {
  type: "candidate-pair";
  state?: string;
  currentRoundTripTime?: number;
  selected?: boolean;
  nominated?: boolean;
}

const previousByteSamples = new Map<string, { ts: number; bytes: number }>();

export function parseStreamStats(
  sessionId: string,
  entries: Array<Record<string, unknown>>,
): ParsedStreamStats {
  const inbound = entries.find((e) => e.type === "inbound-rtp") as
    | (Record<string, unknown> & InboundRtpEntry)
    | undefined;
  const candidatePair = entries.find(
    (e) => e.type === "candidate-pair" && (e.nominated || e.selected),
  ) as (Record<string, unknown> & RemoteCandidatePair) | undefined;

  let bitrateKbps: number | null = null;
  if (inbound && typeof inbound.bytesReceived === "number" && typeof inbound.timestamp === "number") {
    const prev = previousByteSamples.get(sessionId);
    if (prev && inbound.timestamp > prev.ts) {
      const deltaBytes = inbound.bytesReceived - prev.bytes;
      const deltaSec = (inbound.timestamp - prev.ts) / 1000;
      if (deltaSec > 0 && deltaBytes >= 0) {
        bitrateKbps = (deltaBytes * 8) / deltaSec / 1000;
      }
    }
    previousByteSamples.set(sessionId, {
      ts: inbound.timestamp,
      bytes: inbound.bytesReceived,
    });
  }

  return {
    fps: typeof inbound?.framesPerSecond === "number" ? inbound.framesPerSecond : null,
    bitrateKbps,
    rttMs:
      typeof candidatePair?.currentRoundTripTime === "number"
        ? candidatePair.currentRoundTripTime * 1000
        : null,
    jitterMs:
      typeof inbound?.jitter === "number" ? inbound.jitter * 1000 : null,
    framesDecoded:
      typeof inbound?.framesDecoded === "number" ? inbound.framesDecoded : null,
    framesDropped:
      typeof inbound?.framesDropped === "number" ? inbound.framesDropped : null,
  };
}

export function clearStreamStatsSamples(sessionId: string): void {
  previousByteSamples.delete(sessionId);
}

export interface KeyframeResponse {
  session_id: string;
  senders_signalled: number;
}

export class StreamingUnavailableError extends Error {
  constructor(message = "streaming subsystem not initialised") {
    super(message);
    this.name = "StreamingUnavailableError";
  }
}

function throwIf503(err: unknown): never {
  if (axios.isAxiosError(err) && err.response?.status === 503) {
    const detail = (err.response.data as { detail?: string } | undefined)?.detail;
    throw new StreamingUnavailableError(detail);
  }
  throw err;
}

export async function postOffer(payload: OfferRequest): Promise<OfferResponse> {
  try {
    const { data } = await api.post<OfferResponse>("/streams/offer", payload);
    return data;
  } catch (err) {
    throwIf503(err);
  }
}

export async function postIce(sessionId: string, payload: IceRequest): Promise<void> {
  try {
    await api.post(`/streams/${sessionId}/ice`, payload);
  } catch (err) {
    if (axios.isAxiosError(err) && err.response?.status === 404) return;
    throwIf503(err);
  }
}

export async function postStop(sessionId: string): Promise<void> {
  try {
    await api.post(`/streams/${sessionId}/stop`);
  } catch (err) {
    if (axios.isAxiosError(err) && err.response?.status === 404) return;
    throwIf503(err);
  }
}

export async function patchQuality(
  sessionId: string,
  payload: QualityRequest,
): Promise<QualityResponse> {
  try {
    const { data } = await api.patch<QualityResponse>(
      `/streams/${sessionId}/quality`,
      payload,
    );
    return data;
  } catch (err) {
    throwIf503(err);
  }
}

export async function getStats(sessionId: string): Promise<StatsResponse> {
  try {
    const { data } = await api.get<StatsResponse>(`/streams/${sessionId}/stats`);
    return data;
  } catch (err) {
    throwIf503(err);
  }
}

export async function getBatchStats(
  sessionIds?: string[],
): Promise<BatchStatsResponse> {
  try {
    const params = sessionIds && sessionIds.length > 0
      ? { params: { session_ids: sessionIds.join(",") } }
      : undefined;
    const { data } = await api.get<BatchStatsResponse>("/streams/stats", params);
    return data;
  } catch (err) {
    throwIf503(err);
  }
}

export async function postKeyframe(sessionId: string): Promise<KeyframeResponse> {
  try {
    const { data } = await api.post<KeyframeResponse>(`/streams/${sessionId}/keyframe`);
    return data;
  } catch (err) {
    throwIf503(err);
  }
}
