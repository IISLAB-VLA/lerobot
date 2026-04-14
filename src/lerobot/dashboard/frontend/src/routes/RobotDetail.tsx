import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import axios from "axios";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
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

      <section aria-label="Camera streams" className="flex-1">
        <StreamLayout
          robotId={robot.id}
          cameras={cameras}
          layout={layout}
          spotlightCameraId={spotlightCameraId}
          onSpotlightChange={setSpotlightCameraId}
          enabled={streamsEnabled && statusKind === "online"}
        />
      </section>

      {statusKind !== "online" ? (
        <p className="text-xs text-muted-foreground">
          Streams activate once the robot reports <span className="font-medium">online</span>.
          Use <span className="font-mono">POST /api/robots/{robot.id}/connect</span> to bring it up.
        </p>
      ) : null}
    </div>
  );
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
