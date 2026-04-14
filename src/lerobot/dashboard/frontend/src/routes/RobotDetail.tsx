import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import axios from "axios";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, BrainCircuit, Disc3, Sliders, Video } from "lucide-react";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import {
  fetchRobotStatus,
  listRobots,
  type CameraEntry,
  type RobotEntry,
} from "@/lib/api/robots";
import { StatusDot, type StatusKind } from "@/components/robots/StatusDot";
import { LayoutSwitcher } from "@/components/streaming/LayoutSwitcher";
import {
  LAYOUT_DEFINITIONS,
  type StreamLayoutKind,
} from "@/components/streaming/layouts";
import { StreamLayout } from "@/components/streaming/StreamLayout";
import { useStreamStageShortcuts } from "@/hooks/useStreamStageShortcuts";
import { useStreamStats } from "@/hooks/useStreamStats";
import { useStreamStatsStore } from "@/store/streamStats";
import { TeleopPanel } from "@/components/teleop/TeleopPanel";
import { StartRecordingModal } from "@/components/recording/StartRecordingModal";
import { RecordingIndicator } from "@/components/recording/RecordingIndicator";
import type { RecordingSession } from "@/lib/api/recordings";

async function listCameras(): Promise<CameraEntry[]> {
  try {
    const { data } = await api.get<CameraEntry[]>("/cameras");
    return data;
  } catch (err) {
    if (axios.isAxiosError(err) && err.response?.status === 404) return [];
    throw err;
  }
}

export function RobotDetailPage(): JSX.Element {
  const { id = "" } = useParams<{ id: string }>();
  const [layout, setLayout] = useState<StreamLayoutKind>("single");
  const [spotlightCameraId, setSpotlightCameraId] = useState<string | null>(null);
  const [streamsEnabled, setStreamsEnabled] = useState(true);
  const showStats = useStreamStatsStore((s) => s.showOverlay);
  const toggleStats = useStreamStatsStore((s) => s.toggle);
  const [cameraSessions, setCameraSessions] = useState<Map<string, string>>(
    () => new Map(),
  );
  const [recordingOpen, setRecordingOpen] = useState(false);
  const [activeRecording, setActiveRecording] = useState<RecordingSession | null>(null);
  const [finishedRecording, setFinishedRecording] = useState<RecordingSession | null>(null);
  const stageRef = useRef<HTMLElement>(null);
  const recordingEnabled = import.meta.env.VITE_ENABLE_RECORDING !== "0";

  const robotsQuery = useQuery<RobotEntry[]>({
    queryKey: ["robots"],
    queryFn: listRobots,
  });
  const camerasQuery = useQuery<CameraEntry[]>({
    queryKey: ["cameras"],
    queryFn: listCameras,
  });
  const statusQuery = useQuery({
    queryKey: ["robot-status", id],
    queryFn: () => fetchRobotStatus(id),
    refetchInterval: 2_000,
    enabled: Boolean(id),
  });

  const robot = robotsQuery.data?.find((r) => r.id === id);
  const cameras = useMemo(() => {
    if (!robot || !camerasQuery.data) return [];
    const byId = new Map(camerasQuery.data.map((c) => [c.id, c] as const));
    return robot.cameras.map((cid) => byId.get(cid)).filter((c): c is CameraEntry => !!c);
  }, [robot, camerasQuery.data]);

  useEffect(() => {
    if (layout !== "spotlight") return;
    const first = cameras[0];
    if (!first) return;
    const stillValid = cameras.some((c) => c.id === spotlightCameraId);
    if (!stillValid) setSpotlightCameraId(first.id);
  }, [cameras, layout, spotlightCameraId]);

  useEffect(() => {
    const maxLayout = LAYOUT_DEFINITIONS.find((l) => l.kind === layout);
    if (maxLayout && cameras.length > 0 && cameras.length < maxLayout.minTiles) {
      setLayout("single");
    }
  }, [cameras, layout]);

  useStreamStageShortcuts({
    stageRef,
    tileCount: cameras.length,
    onLayoutChange: setLayout,
    onToggleStats: toggleStats,
    enabled: cameras.length > 0,
  });

  const handleSessionChange = useCallback(
    (cameraId: string, sessionId: string | null) => {
      setCameraSessions((prev) => {
        const current = prev.get(cameraId) ?? null;
        if (current === sessionId) return prev;
        const next = new Map(prev);
        if (sessionId === null) {
          next.delete(cameraId);
        } else {
          next.set(cameraId, sessionId);
        }
        return next;
      });
    },
    [],
  );

  const sessionIds = useMemo(
    () => Array.from(cameraSessions.values()),
    [cameraSessions],
  );

  const streamStats = useStreamStats({
    sessionIds,
    enabled: showStats && sessionIds.length > 0,
  });

  if (robotsQuery.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading robot…</p>;
  }

  if (robotsQuery.isError) {
    return (
      <div
        role="alert"
        className="rounded-md border border-destructive/50 bg-destructive/5 p-4 text-sm text-destructive"
      >
        Failed to load robots: {errorMessage(robotsQuery.error)}
      </div>
    );
  }

  if (!robot) {
    return (
      <div className="space-y-3">
        <p className="text-sm text-muted-foreground">Robot not found.</p>
        <Button asChild variant="outline">
          <Link to="/robots">
            <ArrowLeft className="h-4 w-4" aria-hidden />
            Back to robots
          </Link>
        </Button>
      </div>
    );
  }

  const statusKind: StatusKind = statusQuery.isLoading
    ? "registering"
    : statusQuery.data?.online
      ? "online"
      : "offline";

  return (
    <div className="flex h-full flex-col gap-4">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Button asChild variant="ghost" size="icon" aria-label="Back to robots">
            <Link to="/robots">
              <ArrowLeft className="h-4 w-4" aria-hidden />
            </Link>
          </Button>
          <div>
            <h1 className="text-xl font-semibold tracking-tight">{robot.name}</h1>
            <p className="text-xs text-muted-foreground">
              {robot.robot_type} · {summarizeConnection(robot)}
            </p>
          </div>
          <StatusDot kind={statusKind} className="ml-2" />
        </div>

        <div className="flex items-center gap-2">
          {recordingEnabled ? (
            <Button
              variant={activeRecording ? "destructive" : "outline"}
              size="sm"
              onClick={() => {
                if (activeRecording) {
                  // The indicator owns the stop flow; clicking again is a no-op.
                  return;
                }
                setFinishedRecording(null);
                setRecordingOpen(true);
              }}
              disabled={statusKind !== "online"}
              title={
                statusKind !== "online"
                  ? "Connect the robot to start recording"
                  : "Start a new recording"
              }
            >
              <Disc3 className="h-4 w-4" aria-hidden />
              {activeRecording ? "Recording…" : "Record"}
            </Button>
          ) : null}
          {import.meta.env.VITE_ENABLE_CALIBRATION !== "0" ? (
            <Button asChild variant="outline" size="sm">
              <Link to={`/robots/${robot.id}/calibrate`}>
                <Sliders className="h-4 w-4" aria-hidden />
                Calibrate
              </Link>
            </Button>
          ) : null}
          {import.meta.env.VITE_ENABLE_INFERENCE !== "0" ? (
            <Button asChild variant="outline" size="sm">
              <Link to={`/robots/${robot.id}/inference`}>
                <BrainCircuit className="h-4 w-4" aria-hidden />
                Run policy
              </Link>
            </Button>
          ) : null}
          <Button
            variant="outline"
            size="sm"
            onClick={() => setStreamsEnabled((v) => !v)}
          >
            {streamsEnabled ? "Pause streams" : "Resume streams"}
          </Button>
          <LayoutSwitcher
            current={layout}
            tileCount={cameras.length}
            onChange={setLayout}
          />
        </div>
      </header>

      {recordingEnabled && activeRecording ? (
        <RecordingIndicator
          session={activeRecording}
          onFinished={(_, outcome) => {
            setActiveRecording(null);
            setFinishedRecording(outcome);
          }}
        />
      ) : null}

      {recordingEnabled && finishedRecording && !activeRecording ? (
        <RecordingSummary
          session={finishedRecording}
          onDismiss={() => setFinishedRecording(null)}
        />
      ) : null}

      {import.meta.env.VITE_ENABLE_TELEOP !== "0" ? (
        <TeleopPanel robotId={robot.id} enabled={statusKind === "online"} />
      ) : null}

      {recordingEnabled ? (
        <StartRecordingModal
          open={recordingOpen}
          onOpenChange={setRecordingOpen}
          robotId={robot.id}
          robotName={robot.name}
          onStarted={(session) => setActiveRecording(session)}
        />
      ) : null}

      <section
        ref={stageRef}
        aria-label="Camera streams"
        className="flex-1 focus-visible:outline-none"
        tabIndex={-1}
      >
        <StreamLayout
          robotId={robot.id}
          cameras={cameras}
          layout={layout}
          spotlightCameraId={spotlightCameraId}
          onSpotlightChange={setSpotlightCameraId}
          enabled={streamsEnabled && statusKind === "online"}
          showStats={showStats}
          statsBySessionId={streamStats.byId}
          cameraToSession={cameraSessions}
          onSessionChange={handleSessionChange}
        />
      </section>

      {cameras.length > 1 && layout === "single" ? (
        <ThumbnailStrip
          cameras={cameras}
          onSelect={(cameraId) => {
            setSpotlightCameraId(cameraId);
            setLayout("spotlight");
          }}
        />
      ) : null}

      {statusKind !== "online" ? (
        <p className="text-xs text-muted-foreground">
          Streams activate once the robot reports <span className="font-medium">online</span>.
          Use <span className="font-mono">POST /api/robots/{robot.id}/connect</span> to bring it up.
        </p>
      ) : (
        <p className="text-xs text-muted-foreground">
          Shortcuts — <kbd className="rounded border px-1">1</kbd>…<kbd className="rounded border px-1">5</kbd> layout,{" "}
          <kbd className="rounded border px-1">F</kbd> fullscreen,{" "}
          <kbd className="rounded border px-1">I</kbd> toggle stats,{" "}
          <kbd className="rounded border px-1">Esc</kbd> exit fullscreen.
        </p>
      )}
    </div>
  );
}

function ThumbnailStrip({
  cameras,
  onSelect,
}: {
  cameras: CameraEntry[];
  onSelect: (cameraId: string) => void;
}): JSX.Element {
  return (
    <div
      className="flex items-center gap-2 overflow-x-auto py-1"
      aria-label="Switch camera"
      data-testid="thumbnail-strip"
    >
      {cameras.map((cam) => (
        <button
          key={cam.id}
          type="button"
          onClick={() => onSelect(cam.id)}
          aria-label={`Spotlight ${cam.name}`}
          data-testid="thumbnail-strip-item"
          className="flex shrink-0 items-center gap-1.5 rounded-md border bg-card px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:border-primary hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Video className="h-3 w-3" aria-hidden />
          <span className="max-w-[8rem] truncate">{cam.name}</span>
        </button>
      ))}
      <span className="shrink-0 text-[11px] text-muted-foreground">
        Click to spotlight
      </span>
    </div>
  );
}

function RecordingSummary({
  session,
  onDismiss,
}: {
  session: RecordingSession;
  onDismiss: () => void;
}): JSX.Element {
  const ok = session.status === "saved";
  const folderHref = session.dataset_path ? toFolderHref(session.dataset_path) : null;
  return (
    <div
      role="status"
      className={`flex items-center justify-between gap-3 rounded-md border px-4 py-2 text-sm ${
        ok ? "border-emerald-500/40 bg-emerald-500/5" : "border-amber-500/40 bg-amber-500/5"
      }`}
      data-testid="recording-summary"
    >
      <div className="flex flex-col">
        <span className="font-medium">
          {ok ? "Recording saved" : `Recording ${session.status}`}
        </span>
        <span className="font-mono text-xs text-muted-foreground">
          {session.dataset_name} · {session.frames_captured} frames
          {session.episode_index !== null ? ` · episode ${session.episode_index}` : ""}
        </span>
        {ok && folderHref ? (
          <a
            href={folderHref}
            className="font-mono text-[11px] text-primary underline-offset-2 hover:underline"
            title={session.dataset_path}
            data-testid="open-dataset-folder"
            onClick={(event) => {
              // file:// links are silently blocked by most browsers when the page is on
              // http(s); fall back to copying the path so the operator can paste it.
              if (
                folderHref.startsWith("file://") &&
                window.location.protocol !== "file:"
              ) {
                event.preventDefault();
                void navigator.clipboard?.writeText(session.dataset_path);
              }
            }}
          >
            Open dataset folder ({session.dataset_path})
          </a>
        ) : session.dataset_path ? (
          <span
            className="font-mono text-[11px] text-muted-foreground"
            title={session.dataset_path}
          >
            {session.dataset_path}
          </span>
        ) : null}
        {session.error ? (
          <span className="text-xs text-destructive">{session.error}</span>
        ) : null}
      </div>
      <Button variant="ghost" size="sm" onClick={onDismiss}>
        Dismiss
      </Button>
    </div>
  );
}

function toFolderHref(path: string): string {
  // Strip a trailing dataset file (e.g. "...metadata.json"); show the parent dir.
  const trimmed = path.replace(/\/+$/, "");
  if (/^[a-z]+:\/\//i.test(trimmed)) return trimmed;
  if (trimmed.startsWith("/")) return `file://${trimmed}`;
  return `file:///${trimmed.replace(/^([A-Za-z]):/, "$1:/")}`;
}

function summarizeConnection(robot: RobotEntry): string {
  const c = robot.connection;
  if (c.kind === "serial") return `serial · ${c.port}`;
  return `${c.protocol} · ${c.host}:${c.port}`;
}

function errorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  return "unknown error";
}
