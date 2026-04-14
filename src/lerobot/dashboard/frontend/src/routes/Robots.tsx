import { useQuery } from "@tanstack/react-query";
import axios from "axios";
import { Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { RobotCardGrid } from "@/components/robots/RobotCardGrid";
import { api } from "@/lib/api";
import { listRobots, type CameraEntry, type RobotEntry } from "@/lib/api/robots";

async function listCameras(): Promise<CameraEntry[]> {
  try {
    const { data } = await api.get<CameraEntry[]>("/cameras");
    return data;
  } catch (err) {
    if (axios.isAxiosError(err) && err.response?.status === 404) return [];
    throw err;
  }
}

export function RobotsPage(): JSX.Element {
  const robotsQuery = useQuery<RobotEntry[]>({
    queryKey: ["robots"],
    queryFn: listRobots,
  });

  const camerasQuery = useQuery<CameraEntry[]>({
    queryKey: ["cameras"],
    queryFn: listCameras,
  });

  const robots = robotsQuery.data ?? [];
  const cameras = camerasQuery.data ?? [];

  return (
    <div className="space-y-6">
      <header className="flex items-end justify-between gap-4">
        <div className="space-y-1">
          <h1 className="text-2xl font-bold tracking-tight">Robots</h1>
          <p className="text-muted-foreground">
            Registered robots and their live connection status.
          </p>
        </div>
        <Button disabled title="Add-Robot modal lands in the next commit">
          <Plus className="h-4 w-4" aria-hidden />
          Add robot
        </Button>
      </header>

      {robotsQuery.isLoading ? (
        <RobotGridSkeleton />
      ) : robotsQuery.isError ? (
        <ErrorState message={errorMessage(robotsQuery.error)} />
      ) : robots.length === 0 ? (
        <EmptyState />
      ) : (
        <RobotCardGrid robots={robots} cameras={cameras} />
      )}
    </div>
  );
}

function EmptyState(): JSX.Element {
  return (
    <div
      className="flex flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-input py-16 text-center"
      data-testid="robots-empty-state"
    >
      <h2 className="text-lg font-semibold">No robots yet</h2>
      <p className="max-w-md text-sm text-muted-foreground">
        Add your first robot to start streaming video, teleoperating, and recording episodes.
      </p>
    </div>
  );
}

function ErrorState({ message }: { message: string }): JSX.Element {
  return (
    <div
      role="alert"
      className="rounded-lg border border-destructive/50 bg-destructive/5 p-4 text-sm text-destructive"
    >
      Failed to load robots: <span className="font-mono text-xs">{message}</span>
    </div>
  );
}

function RobotGridSkeleton(): JSX.Element {
  return (
    <div
      className="grid gap-4"
      style={{ gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))" }}
      aria-hidden
    >
      {Array.from({ length: 3 }).map((_, i) => (
        <div key={i} className="animate-pulse rounded-lg border bg-card">
          <div className="aspect-video w-full bg-muted" />
          <div className="space-y-2 p-4">
            <div className="h-4 w-1/2 rounded bg-muted" />
            <div className="h-3 w-2/3 rounded bg-muted" />
          </div>
        </div>
      ))}
    </div>
  );
}

function errorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  return "unknown error";
}
