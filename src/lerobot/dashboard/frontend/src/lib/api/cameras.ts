// Camera capabilities client (Task #22 — settings drawer).
// Backend: GET /api/cameras/{id}/capabilities → CameraCapabilities
// Endpoint shipped in dash-backend commit 0baa406b.

import axios from "axios";
import { api } from "@/lib/api";

export type CameraSourceType = "v4l2" | "realsense" | "fake";
export type Codec = "h264" | "vp8" | "vp9";

export interface Resolution {
  width: number;
  height: number;
}

export interface CurrentMode {
  width: number;
  height: number;
  fps: number;
  /** null when codec_hint not set in registry */
  codec: string | null;
}

export interface CameraCapabilities {
  /** Available resolutions (static list; current always included). */
  resolutions: Resolution[];
  /** Available FPS options (flat list, not per-resolution). */
  fps_options: number[];
  /** Codecs ordered by server preference (best first). */
  codecs: Codec[];
  /** Active mode from registry entry (not live driver state). */
  current: CurrentMode;
  /** Camera source type. */
  source: CameraSourceType;
}

export async function getCameraCapabilities(
  cameraId: string,
): Promise<CameraCapabilities | null> {
  try {
    const { data } = await api.get<CameraCapabilities>(
      `/cameras/${encodeURIComponent(cameraId)}/capabilities`,
    );
    return data;
  } catch (err) {
    if (axios.isAxiosError(err) && err.response?.status === 404) return null;
    throw err;
  }
}
