import { useMemo } from "react";
import { VideoTile } from "@/components/streaming/VideoTile";
import type { CameraEntry } from "@/lib/api/robots";
import type { ParsedStreamStats } from "@/lib/api/streams";
import type { StreamLayoutKind } from "@/components/streaming/layouts";

interface StreamLayoutProps {
  robotId: string;
  cameras: CameraEntry[];
  layout: StreamLayoutKind;
  spotlightCameraId: string | null;
  onSpotlightChange: (cameraId: string) => void;
  enabled: boolean;
  showStats: boolean;
  statsBySessionId: Map<string, ParsedStreamStats>;
  cameraToSession: Map<string, string>;
  onSessionChange: (cameraId: string, sessionId: string | null) => void;
}

export function StreamLayout({
  robotId,
  cameras,
  layout,
  spotlightCameraId,
  onSpotlightChange,
  enabled,
  showStats,
  statsBySessionId,
  cameraToSession,
  onSessionChange,
}: StreamLayoutProps): JSX.Element {
  const { primary, rest } = useMemo(() => {
    if (layout !== "spotlight") return { primary: null, rest: cameras };
    const spotlight =
      cameras.find((c) => c.id === spotlightCameraId) ?? cameras[0] ?? null;
    return {
      primary: spotlight,
      rest: cameras.filter((c) => c.id !== spotlight?.id),
    };
  }, [layout, cameras, spotlightCameraId]);

  const tileFor = (camera: CameraEntry, emphasised?: boolean): JSX.Element => {
    const sessionId = cameraToSession.get(camera.id) ?? null;
    const stats = sessionId ? statsBySessionId.get(sessionId) ?? null : null;
    return (
      <VideoTile
        robotId={robotId}
        camera={camera}
        enabled={enabled}
        emphasised={emphasised}
        stats={stats}
        showStats={showStats}
        onSessionChange={onSessionChange}
      />
    );
  };

  if (cameras.length === 0) {
    return (
      <div className="flex aspect-video w-full items-center justify-center rounded-md border border-dashed text-sm text-muted-foreground">
        No cameras attached to this robot.
      </div>
    );
  }

  if (layout === "single") {
    const only = cameras[0]!;
    return <div className="aspect-video w-full">{tileFor(only)}</div>;
  }

  if (layout === "horizontal-split") {
    const top = cameras[0]!;
    const bottom = cameras[1];
    return (
      <div className="grid aspect-video w-full grid-rows-2 gap-2">
        {tileFor(top)}
        {bottom ? tileFor(bottom) : <div />}
      </div>
    );
  }

  if (layout === "vertical-split") {
    const left = cameras[0]!;
    const right = cameras[1];
    return (
      <div className="grid aspect-video w-full grid-cols-2 gap-2">
        {tileFor(left)}
        {right ? tileFor(right) : <div />}
      </div>
    );
  }

  if (layout === "grid") {
    const cols = Math.ceil(Math.sqrt(cameras.length));
    return (
      <div
        className="grid aspect-video w-full gap-2"
        style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}
      >
        {cameras.map((cam) => (
          <div key={cam.id}>{tileFor(cam)}</div>
        ))}
      </div>
    );
  }

  // spotlight
  return (
    <div className="grid aspect-video w-full grid-cols-[1fr_14rem] gap-2">
      {primary ? tileFor(primary, true) : <div />}
      <div className="flex flex-col gap-2 overflow-y-auto pr-1">
        {rest.map((cam) => (
          <button
            key={cam.id}
            type="button"
            onClick={() => onSpotlightChange(cam.id)}
            className="aspect-video w-full rounded-md border transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            aria-label={`Spotlight ${cam.name}`}
          >
            {tileFor(cam)}
          </button>
        ))}
      </div>
    </div>
  );
}
