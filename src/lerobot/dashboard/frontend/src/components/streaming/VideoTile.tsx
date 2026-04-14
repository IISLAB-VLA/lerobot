import { memo, useEffect, useRef, useState } from "react";
import { AlertTriangle, Loader2, Maximize2, Minimize2, Video, VideoOff } from "lucide-react";
import { cn } from "@/lib/utils";
import { useWebRTCStream } from "@/hooks/useWebRTCStream";
import type { CameraEntry } from "@/lib/api/robots";
import { Button } from "@/components/ui/button";
import { postKeyframe, type ParsedStreamStats } from "@/lib/api/streams";

interface VideoTileProps {
  robotId: string;
  camera: CameraEntry;
  enabled: boolean;
  emphasised?: boolean;
  className?: string;
  stats?: ParsedStreamStats | null;
  showStats?: boolean;
  onSessionChange?: (cameraId: string, sessionId: string | null) => void;
}

function VideoTileImpl({
  robotId,
  camera,
  enabled,
  emphasised,
  className,
  stats,
  showStats,
  onSessionChange,
}: VideoTileProps): JSX.Element {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const figureRef = useRef<HTMLElement | null>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);

  useEffect(() => {
    const onChange = () => {
      setIsFullscreen(document.fullscreenElement === figureRef.current);
    };
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);

  const onToggleFullscreen = async (event: React.MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    try {
      if (document.fullscreenElement === figureRef.current) {
        await document.exitFullscreen();
      } else {
        if (document.fullscreenElement) {
          await document.exitFullscreen().catch(() => undefined);
        }
        await figureRef.current?.requestFullscreen();
      }
    } catch {
      // browser may reject without an active gesture; the click itself is one,
      // so failures are typically policy-related (iframe sandbox, etc.).
    }
  };
  const {
    phase,
    stream,
    error,
    sessionId,
    appliedCodecs,
    reconnectAttempt,
    nextReconnectAt,
    restart,
  } = useWebRTCStream({
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

  useEffect(() => {
    onSessionChange?.(camera.id, sessionId);
    return () => {
      onSessionChange?.(camera.id, null);
    };
  }, [camera.id, sessionId, onSessionChange]);

  const overlay = renderOverlay(phase, error, restart, reconnectAttempt, nextReconnectAt);
  const codecBadge = appliedCodecs[0]?.split("/")[1] ?? null;

  return (
    <figure
      ref={figureRef}
      data-testid="video-tile"
      data-camera-id={camera.id}
      aria-label={camera.name}
      className={cn(
        "group relative flex h-full w-full flex-col overflow-hidden rounded-md border bg-black text-white",
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
      <button
        type="button"
        onClick={onToggleFullscreen}
        aria-label={isFullscreen ? "Exit fullscreen" : "Enter fullscreen"}
        title={isFullscreen ? "Exit fullscreen" : "Fullscreen this tile"}
        data-testid="video-tile-fullscreen"
        className="absolute left-2 top-2 inline-flex h-7 w-7 items-center justify-center rounded bg-black/45 text-white/80 opacity-0 transition-opacity hover:bg-black/70 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring group-hover:opacity-100 group-focus-within:opacity-100"
      >
        {isFullscreen ? (
          <Minimize2 className="h-3.5 w-3.5" aria-hidden />
        ) : (
          <Maximize2 className="h-3.5 w-3.5" aria-hidden />
        )}
      </button>
      {showStats && stats ? (
        <dl
          className="pointer-events-none absolute right-2 top-2 grid grid-cols-[auto_auto] gap-x-2 gap-y-0.5 rounded bg-black/65 px-2 py-1 text-[10px] font-mono text-white/85"
          data-testid="video-tile-stats"
        >
          <dt>codec</dt>
          <dd className="text-right">{codecBadge ?? "—"}</dd>
          <dt>res</dt>
          <dd className="text-right">
            {stats.frameWidth !== null && stats.frameHeight !== null
              ? `${stats.frameWidth}×${stats.frameHeight}`
              : "—"}
          </dd>
          <dt>fps</dt>
          <dd className="text-right">{formatStat(stats.fps, 1)}</dd>
          <dt>kbps</dt>
          <dd className="text-right">{formatStat(stats.bitrateKbps, 0)}</dd>
          <dt>rtt</dt>
          <dd className="text-right">
            {stats.rttMs !== null ? `${stats.rttMs.toFixed(0)} ms` : "—"}
          </dd>
          <dt>jitter</dt>
          <dd className="text-right">
            {stats.jitterMs !== null ? `${stats.jitterMs.toFixed(1)} ms` : "—"}
          </dd>
          <dt>dropped</dt>
          <dd className="text-right">{formatStat(stats.framesDropped, 0)}</dd>
        </dl>
      ) : null}
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
  reconnectAttempt: number,
  nextReconnectAt: number | null,
): JSX.Element | null {
  if (phase === "live") return null;
  if (phase === "negotiating" || phase === "connecting") {
    return (
      <div className="absolute inset-0 flex items-center justify-center bg-black/50 text-sm">
        <Loader2 className="h-5 w-5 animate-spin" aria-hidden />
        <span className="ml-2">
          {reconnectAttempt > 0 ? `Reconnecting (attempt ${reconnectAttempt})…` : "Connecting…"}
        </span>
      </div>
    );
  }
  if (phase === "error") {
    return (
      <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-black/60 p-4 text-center text-sm">
        <AlertTriangle className="h-5 w-5 text-destructive" aria-hidden />
        <span>{error ?? "Stream error"}</span>
        {nextReconnectAt !== null ? (
          <ReconnectCountdown targetMs={nextReconnectAt} attempt={reconnectAttempt} />
        ) : null}
        <Button size="sm" variant="secondary" onClick={restart}>
          Retry now
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

function ReconnectCountdown({
  targetMs,
  attempt,
}: {
  targetMs: number;
  attempt: number;
}): JSX.Element {
  const [remaining, setRemaining] = useState(() => Math.max(0, targetMs - Date.now()));
  useEffect(() => {
    const tick = () => setRemaining(Math.max(0, targetMs - Date.now()));
    tick();
    const id = window.setInterval(tick, 250);
    return () => window.clearInterval(id);
  }, [targetMs]);
  const seconds = Math.ceil(remaining / 1000);
  return (
    <span className="text-xs text-white/70" aria-live="polite">
      Auto-retry in {seconds}s · attempt {attempt}
    </span>
  );
}

function formatStat(value: number | null, digits: number): string {
  if (value === null || !Number.isFinite(value)) return "—";
  return value.toFixed(digits);
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
