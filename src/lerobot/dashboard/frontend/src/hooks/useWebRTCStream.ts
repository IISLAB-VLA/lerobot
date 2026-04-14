import { useCallback, useEffect, useRef, useState } from "react";
import {
  postIce,
  postOffer,
  postStop,
  StreamingUnavailableError,
} from "@/lib/api/streams";

export type StreamPhase =
  | "idle"
  | "negotiating"
  | "connecting"
  | "live"
  | "closed"
  | "error";

export interface StreamState {
  phase: StreamPhase;
  sessionId: string | null;
  appliedCodecs: string[];
  error: string | null;
  stream: MediaStream | null;
}

export interface UseWebRTCStreamArgs {
  robotId: string;
  cameraId: string;
  enabled: boolean;
  iceServers?: RTCIceServer[];
}

interface UseWebRTCStreamResult extends StreamState {
  restart: () => void;
}

const DEFAULT_ICE_SERVERS: RTCIceServer[] = [{ urls: "stun:stun.l.google.com:19302" }];

export function useWebRTCStream({
  robotId,
  cameraId,
  enabled,
  iceServers = DEFAULT_ICE_SERVERS,
}: UseWebRTCStreamArgs): UseWebRTCStreamResult {
  const [phase, setPhase] = useState<StreamPhase>("idle");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [appliedCodecs, setAppliedCodecs] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [stream, setStream] = useState<MediaStream | null>(null);
  const [restartCount, setRestartCount] = useState(0);

  const sessionIdRef = useRef<string | null>(null);

  const restart = useCallback(() => {
    setRestartCount((n) => n + 1);
  }, []);

  useEffect(() => {
    if (!enabled || !robotId || !cameraId) {
      setPhase("idle");
      setSessionId(null);
      setAppliedCodecs([]);
      setError(null);
      setStream(null);
      return undefined;
    }

    let cancelled = false;
    const pc = new RTCPeerConnection({ iceServers });
    const remoteStream = new MediaStream();

    setPhase("negotiating");
    setError(null);
    setStream(null);
    setSessionId(null);
    setAppliedCodecs([]);
    sessionIdRef.current = null;

    pc.addTransceiver("video", { direction: "recvonly" });

    pc.ontrack = (event) => {
      if (cancelled) return;
      event.streams[0]?.getTracks().forEach((track) => {
        if (!remoteStream.getTracks().includes(track)) {
          remoteStream.addTrack(track);
        }
      });
      setStream(remoteStream);
    };

    pc.onconnectionstatechange = () => {
      if (cancelled) return;
      const s = pc.connectionState;
      if (s === "connected") setPhase("live");
      else if (s === "failed" || s === "disconnected") {
        setPhase("error");
        setError(`peer ${s}`);
      } else if (s === "closed") setPhase("closed");
    };

    pc.onicecandidate = (event) => {
      const sid = sessionIdRef.current;
      if (!sid) return;
      const candidate = event.candidate;
      void postIce(sid, {
        candidate: candidate ? candidate.candidate : "",
        sdpMid: candidate?.sdpMid ?? null,
        sdpMLineIndex: candidate?.sdpMLineIndex ?? null,
      });
    };

    const run = async () => {
      try {
        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);
        if (cancelled) return;

        const answer = await postOffer({
          robot_id: robotId,
          camera_id: cameraId,
          sdp: offer.sdp ?? "",
          type: "offer",
        });
        if (cancelled) return;

        sessionIdRef.current = answer.session_id;
        setSessionId(answer.session_id);
        setAppliedCodecs(answer.applied_codecs);
        setPhase("connecting");

        await pc.setRemoteDescription({ type: "answer", sdp: answer.sdp });
      } catch (err) {
        if (cancelled) return;
        const message =
          err instanceof StreamingUnavailableError
            ? "Streaming unavailable"
            : err instanceof Error
              ? err.message
              : "unknown error";
        setPhase("error");
        setError(message);
      }
    };

    void run();

    return () => {
      cancelled = true;
      pc.onicecandidate = null;
      pc.ontrack = null;
      pc.onconnectionstatechange = null;
      try {
        pc.close();
      } catch {
        // ignore
      }
      const sid = sessionIdRef.current;
      sessionIdRef.current = null;
      if (sid) void postStop(sid);
    };
  }, [robotId, cameraId, enabled, iceServers, restartCount]);

  return { phase, sessionId, appliedCodecs, error, stream, restart };
}
