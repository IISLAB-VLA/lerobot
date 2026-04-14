import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Camera, Cpu } from "lucide-react";
import { Card } from "@/components/ui/card";
import { StatusDot, type StatusKind } from "@/components/robots/StatusDot";
import { fetchRobotStatus, type RobotEntry, type CameraEntry } from "@/lib/api/robots";

interface RobotCardProps {
  robot: RobotEntry;
  cameras: CameraEntry[];
}

export function RobotCard({ robot, cameras }: RobotCardProps): JSX.Element {
  const navigate = useNavigate();

  const { data, isLoading } = useQuery({
    queryKey: ["robot-status", robot.id],
    queryFn: () => fetchRobotStatus(robot.id),
    refetchInterval: 2_000,
    refetchIntervalInBackground: false,
  });

  const statusKind: StatusKind = isLoading
    ? "registering"
    : data?.online
      ? "online"
      : "offline";

  const boundCameras = cameras.filter((c) => robot.cameras.includes(c.id));
  const connectionSummary = summarizeConnection(robot);

  return (
    <Card
      role="button"
      tabIndex={0}
      aria-label={`Open ${robot.name}`}
      onClick={() => navigate(`/robots/${robot.id}`)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          navigate(`/robots/${robot.id}`);
        }
      }}
      className="group relative flex cursor-pointer flex-col overflow-hidden transition-all hover:border-ring hover:shadow-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
    >
      <div className="relative aspect-video w-full overflow-hidden bg-muted">
        {robot.image_ref ? (
          <img
            src={robot.image_ref}
            alt={`${robot.robot_type} illustration`}
            className="h-full w-full object-cover transition-transform group-hover:scale-[1.02]"
            loading="lazy"
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-muted-foreground">
            <Cpu className="h-10 w-10" aria-hidden />
          </div>
        )}
        <div className="absolute right-3 top-3 rounded-full bg-background/90 px-2.5 py-1 backdrop-blur">
          <StatusDot kind={statusKind} />
        </div>
      </div>

      <div className="flex flex-col gap-2 p-4">
        <div className="flex items-baseline justify-between gap-2">
          <h3 className="truncate text-lg font-semibold">{robot.name}</h3>
        </div>
        <p className="truncate text-sm text-muted-foreground">
          {robot.robot_type} · {connectionSummary}
        </p>

        {boundCameras.length > 0 ? (
          <ul className="mt-1 flex flex-wrap gap-1.5" aria-label="cameras">
            {boundCameras.map((cam) => (
              <li
                key={cam.id}
                className="inline-flex items-center gap-1 rounded-full bg-secondary px-2 py-0.5 text-xs text-secondary-foreground"
              >
                <Camera className="h-3 w-3" aria-hidden />
                <span className="max-w-[10rem] truncate">{cam.name}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-1 text-xs text-muted-foreground">No cameras attached</p>
        )}
      </div>
    </Card>
  );
}

function summarizeConnection(robot: RobotEntry): string {
  const c = robot.connection;
  if (c.kind === "serial") return `serial · ${c.port}`;
  return `${c.protocol} · ${c.host}:${c.port}`;
}
