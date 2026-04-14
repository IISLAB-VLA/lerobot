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

export async function postKeyframe(sessionId: string): Promise<KeyframeResponse> {
  try {
    const { data } = await api.post<KeyframeResponse>(`/streams/${sessionId}/keyframe`);
    return data;
  } catch (err) {
    throwIf503(err);
  }
}
