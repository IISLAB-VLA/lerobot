import { RobotCard } from "@/components/robots/RobotCard";
import type { CameraEntry, RobotEntry } from "@/lib/api/robots";

interface RobotCardGridProps {
  robots: RobotEntry[];
  cameras: CameraEntry[];
}

export function RobotCardGrid({ robots, cameras }: RobotCardGridProps): JSX.Element {
  return (
    <div
      className="grid gap-4"
      style={{ gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))" }}
      data-testid="robot-card-grid"
    >
      {robots.map((robot) => (
        <RobotCard key={robot.id} robot={robot} cameras={cameras} />
      ))}
    </div>
  );
}
