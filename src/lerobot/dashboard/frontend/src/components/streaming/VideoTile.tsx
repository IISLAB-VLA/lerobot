import { memo, useEffect, useRef } from "react";
import { AlertTriangle, Loader2, Video, VideoOff } from "lucide-react";
import { cn } from "@/lib/utils";
import { useWebRTCStream } from "@/hooks/useWebRTCStream";
import type { CameraEntry } from "@/lib/api/robots";
import { Button } from "@/components/ui/button";
import { postKeyframe } from "@/lib/api/streams";

interface VideoTileProps {
  robotId: string;
  camera: CameraEntry;
  enabled: boolean;
  emphasised?: boolean;
  className?: string;
}

function VideoTileImpl({
  robotId,
  camera,
  enabled,
  emphasised,
  className,
}: VideoTileProps): JSX.Element {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const { phase, stream, error, sessionId, appliedCodecs, restart } = useWebRTCStream({
    robotId,
    cameraId: camera.id,
    enabled,
  });

  useEffect(() => {
    const el = videoRef.current;
    if (!el) return;
    if (el.srcObject !== stream) {
      el.srcObject = stream;
    }
  }, [stream]);

  useEffect(() => {
    if (!sessionId) return undefined;
    const onVisible = () => {
      if (document.visibilityState === "visible" && sessionId) {
        void postKeyframe(sessionId);
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [sessionId]);

  const overlay = renderOverlay(phase, error, restart);
  const codecBadge = appliedCodecs[0]?.split("/")[1] ?? null;

  return (
    <figure
      data-testid="video-tile"
      data-camera-id={camera.id}
      aria-label={camera.name}
      className={cn(
        "relative flex h-full w-full flex-col overflow-hidden rounded-md border bg-black text-white",
        emphasised ? "ring-2 ring-primary" : null,
        className,
      )}
    >
      <video
        ref={videoRef}
        autoPlay
        playsInline
        muted
        className="h-full w-full bg-black object-contain"
      />
      {overlay}
      <figcaption className="pointer-events-none absolute inset-x-0 bottom-0 flex items-center justify-between gap-2 bg-gradient-to-t from-black/75 to-transparent px-3 py-2 text-xs">
        <span className="flex items-center gap-1.5 font-medium">
          <Video className="h-3.5 w-3.5" aria-hidden />
          <span className="truncate">{camera.name}</span>
        </span>
        <span className="flex items-center gap-2 text-[10px] uppercase tracking-wide text-white/75">
          {codecBadge ? <span className="rounded bg-white/15 px-1.5 py-0.5">{codecBadge}</span> : null}
          <PhaseBadge phase={phase} />
        </span>
      </figcaption>
    </figure>
  );
}

function renderOverlay(
  phase: string,
  error: string | null,
  restart: () => void,
): JSX.Element | null {
  if (phase === "live") return null;
  if (phase === "negotiating" || phase === "connecting") {
    return (
      <div className="absolute inset-0 flex items-center justify-center bg-black/50 text-sm">
        <Loader2 className="h-5 w-5 animate-spin" aria-hidden />
        <span className="ml-2">Connecting…</span>
      </div>
    );
  }
  if (phase === "error") {
    return (
      <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-black/60 p-4 text-center text-sm">
        <AlertTriangle className="h-5 w-5 text-destructive" aria-hidden />
        <span>{error ?? "Stream error"}</span>
        <Button size="sm" variant="secondary" onClick={restart}>
          Retry
        </Button>
      </div>
    );
  }
  if (phase === "closed" || phase === "idle") {
    return (
      <div className="absolute inset-0 flex items-center justify-center bg-black/40 text-sm text-muted-foreground">
        <VideoOff className="h-5 w-5" aria-hidden />
        <span className="ml-2">Disabled</span>
      </div>
    );
  }
  return null;
}

function PhaseBadge({ phase }: { phase: string }): JSX.Element {
  const label =
    phase === "live"
      ? "live"
      : phase === "connecting" || phase === "negotiating"
        ? "…"
        : phase === "error"
          ? "err"
          : "off";
  return <span>{label}</span>;
}

export const VideoTile = memo(VideoTileImpl);
