// Per-camera settings panel (Task #22).
// Shows capabilities fetched from GET /api/cameras/{id}/capabilities.
// Resolution/codec changes require a stream restart (read-only for now —
// write endpoint is a separate RFC).

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Loader2, Settings2, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { getCameraCapabilities, type CameraCapabilities } from "@/lib/api/cameras";

const SOURCE_LABEL: Record<string, string> = {
  v4l2: "V4L2 (local USB/CSI)",
  realsense: "Intel RealSense",
  fake: "Synthetic / network",
};

interface CameraSettingsDrawerProps {
  cameraId: string;
  cameraName: string;
}

export function CameraSettingsDrawer({
  cameraId,
  cameraName,
}: CameraSettingsDrawerProps): JSX.Element {
  const [open, setOpen] = useState(false);

  return (
    <div className="relative">
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        aria-label={`${cameraName} settings`}
        aria-expanded={open}
        data-testid="camera-settings-btn"
        className={cn(
          "inline-flex h-7 w-7 items-center justify-center rounded bg-black/45 text-white/80",
          "opacity-0 transition-opacity hover:bg-black/70 hover:text-white",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          "group-hover:opacity-100 group-focus-within:opacity-100",
          open && "opacity-100",
        )}
      >
        <Settings2 className="h-3.5 w-3.5" aria-hidden />
      </button>

      {open ? (
        <SettingsPanel
          cameraId={cameraId}
          cameraName={cameraName}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </div>
  );
}

function SettingsPanel({
  cameraId,
  cameraName,
  onClose,
}: {
  cameraId: string;
  cameraName: string;
  onClose: () => void;
}): JSX.Element {
  const { data, isLoading, isError } = useQuery<CameraCapabilities | null>({
    queryKey: ["camera-capabilities", cameraId],
    queryFn: () => getCameraCapabilities(cameraId),
    staleTime: 30_000,
  });

  return (
    <div
      className={cn(
        "absolute right-0 top-9 z-50 w-64 rounded-md border bg-popover shadow-lg",
        "text-popover-foreground",
      )}
      data-testid="camera-settings-panel"
    >
      <div className="flex items-center justify-between border-b px-3 py-2">
        <span className="truncate text-xs font-semibold">{cameraName}</span>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close settings"
          className="rounded p-0.5 hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <X className="h-3.5 w-3.5" aria-hidden />
        </button>
      </div>

      <div className="p-3 text-xs">
        {isLoading ? (
          <div className="flex items-center gap-1.5 text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            Loading capabilities…
          </div>
        ) : isError ? (
          <p role="alert" className="text-destructive">
            Failed to load capabilities.
          </p>
        ) : data == null ? (
          <p className="text-muted-foreground">No capabilities info available.</p>
        ) : (
          <CapabilitiesView caps={data} />
        )}
      </div>
    </div>
  );
}

function CapabilitiesView({ caps }: { caps: CameraCapabilities }): JSX.Element {
  return (
    <dl className="space-y-2">
      <Row label="Source" value={SOURCE_LABEL[caps.source] ?? caps.source} />
      <Row
        label="Active mode"
        value={`${caps.current.width}×${caps.current.height} @ ${caps.current.fps} fps${caps.current.codec ? ` · ${caps.current.codec}` : ""}`}
      />
      <div>
        <dt className="font-medium text-muted-foreground">Resolutions</dt>
        <dd className="mt-0.5 font-mono" data-testid="camera-settings-resolution">
          {caps.resolutions.map((r) => `${r.width}×${r.height}`).join(", ")}
        </dd>
      </div>
      <div>
        <dt className="font-medium text-muted-foreground">FPS options</dt>
        <dd className="mt-0.5 font-mono" data-testid="camera-settings-fps">
          {caps.fps_options.join(", ")}
        </dd>
      </div>
      <div>
        <dt className="font-medium text-muted-foreground">Codecs (pref. order)</dt>
        <dd className="mt-0.5 flex flex-wrap gap-1" data-testid="camera-settings-codec">
          {caps.codecs.map((c, i) => (
            <span
              key={c}
              className={cn(
                "rounded border px-1.5 py-0.5 font-mono uppercase",
                i === 0
                  ? "border-primary/40 bg-primary/10 text-primary"
                  : "border-border bg-muted text-muted-foreground",
              )}
            >
              {c}
            </span>
          ))}
        </dd>
      </div>
      <p className="pt-1 text-[10px] text-muted-foreground">
        Resolution and codec changes take effect on the next stream restart.
      </p>
    </dl>
  );
}

function Row({ label, value }: { label: string; value: string }): JSX.Element {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <dt className="shrink-0 font-medium text-muted-foreground">{label}</dt>
      <dd className="truncate font-mono text-right">{value}</dd>
    </div>
  );
}
