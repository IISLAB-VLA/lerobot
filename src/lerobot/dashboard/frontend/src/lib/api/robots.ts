import axios from "axios";
import { api } from "@/lib/api";

export type NetworkProtocol = "rtde" | "fci" | "xmlrpc" | "websocket" | "custom";

export type KnownRobotType =
  | "so100_follower"
  | "so101_follower"
  | "koch_follower"
  | "ur"
  | "lekiwi"
  | "hopejr"
  | "reachy2"
  | "viper"
  | "unitree_g1";

export const KNOWN_ROBOT_TYPES: KnownRobotType[] = [
  "so100_follower",
  "so101_follower",
  "koch_follower",
  "ur",
  "lekiwi",
  "hopejr",
  "reachy2",
  "viper",
  "unitree_g1",
];

export interface SerialConnection {
  kind: "serial";
  port: string;
  baudrate?: number | null;
  model_opts?: Record<string, unknown>;
}

export interface NetworkConnection {
  kind: "network";
  protocol: NetworkProtocol;
  host: string;
  port: number;
  auth?: Record<string, string> | null;
  extra?: Record<string, unknown>;
}

export type Connection = SerialConnection | NetworkConnection;

export type CameraBackend = "opencv" | "realsense" | "network";

export interface CameraSource {
  path?: string | null;
  index?: number | null;
  url?: string | null;
}

export interface CameraEntry {
  id: string;
  name: string;
  backend: CameraBackend;
  source: CameraSource;
  width: number;
  height: number;
  fps: number;
  codec_hint?: string | null;
}

export interface RobotEntry {
  id: string;
  name: string;
  robot_type: string;
  connection: Connection;
  cameras: string[];
  teleop?: string | null;
  image_ref?: string | null;
}

export interface RobotStatus {
  online: boolean;
  last_error?: string | null;
  connected_at?: string | null;
}

export interface SerialDeviceInfo {
  path: string;
  description?: string | null;
  vid?: number | null;
  pid?: number | null;
}

export interface VideoDeviceInfo {
  path: string;
  index: number;
  name?: string | null;
  width?: number | null;
  height?: number | null;
  fps?: number | null;
}

export interface NetworkProbeResult {
  ok: boolean;
  latency_ms?: number | null;
  error?: string | null;
}

export interface CameraCreateRequest {
  name: string;
  backend: CameraBackend;
  source: CameraSource;
  width: number;
  height: number;
  fps: number;
  codec_hint?: string | null;
}

export interface RobotCreateRequest {
  name: string;
  robot_type: string;
  connection: Connection;
  cameras?: string[];
  teleop?: string | null;
  image_ref?: string | null;
}

function isNotFound(err: unknown): boolean {
  return axios.isAxiosError(err) && err.response?.status === 404;
}

export async function listRobots(): Promise<RobotEntry[]> {
  try {
    const { data } = await api.get<RobotEntry[]>("/robots");
    return data;
  } catch (err) {
    if (isNotFound(err)) return [];
    throw err;
  }
}

export async function createRobot(payload: RobotCreateRequest): Promise<RobotEntry> {
  const { data } = await api.post<RobotEntry>("/robots", payload);
  return data;
}

export async function fetchRobotStatus(id: string): Promise<RobotStatus> {
  try {
    const { data } = await api.get<RobotStatus>(`/robots/${id}/status`);
    return data;
  } catch (err) {
    if (isNotFound(err)) return { online: false };
    throw err;
  }
}

export async function listSerialDevices(): Promise<SerialDeviceInfo[]> {
  try {
    const { data } = await api.get<SerialDeviceInfo[]>("/devices/serial");
    return data;
  } catch (err) {
    if (isNotFound(err)) return [];
    throw err;
  }
}

export async function listVideoDevices(): Promise<VideoDeviceInfo[]> {
  try {
    const { data } = await api.get<VideoDeviceInfo[]>("/devices/cameras");
    return data;
  } catch (err) {
    if (isNotFound(err)) return [];
    throw err;
  }
}

export async function probeNetwork(payload: {
  protocol: NetworkProtocol;
  host: string;
  port: number;
}): Promise<NetworkProbeResult> {
  try {
    const { data } = await api.post<NetworkProbeResult>("/devices/network", payload);
    return data;
  } catch (err) {
    if (axios.isAxiosError(err)) {
      const detail = (err.response?.data as { detail?: string } | undefined)?.detail;
      return { ok: false, error: detail ?? err.message };
    }
    throw err;
  }
}

export async function createCamera(payload: CameraCreateRequest): Promise<CameraEntry> {
  const { data } = await api.post<CameraEntry>("/cameras", payload);
  return data;
}
