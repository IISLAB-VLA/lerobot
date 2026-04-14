import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import {
  RecorderConflictError,
  startRecording,
  type RecordingSession,
  type StartRecordingRequest,
} from "@/lib/api/recordings";

interface StartRecordingModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  robotId: string;
  robotName: string;
  onStarted: (session: RecordingSession) => void;
}

const FPS_OPTIONS = [10, 15, 20, 30, 60];

function defaultDatasetName(robotName: string): string {
  const slug = robotName.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  const now = new Date();
  const pad = (n: number) => n.toString().padStart(2, "0");
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}`;
  return `${slug || "robot"}-${stamp}`;
}

export function StartRecordingModal({
  open,
  onOpenChange,
  robotId,
  robotName,
  onStarted,
}: StartRecordingModalProps): JSX.Element {
  const [datasetName, setDatasetName] = useState(() => defaultDatasetName(robotName));
  const [taskDescription, setTaskDescription] = useState("");
  const [fps, setFps] = useState<number>(30);
  const [useVideos, setUseVideos] = useState(true);
  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      setDatasetName(defaultDatasetName(robotName));
      setTaskDescription("");
      setFps(30);
      setUseVideos(true);
      setSubmitError(null);
    }
  }, [open, robotName]);

  const mutation = useMutation({
    mutationFn: (payload: StartRecordingRequest) => startRecording(payload),
    onSuccess: (session) => {
      onStarted(session);
      onOpenChange(false);
    },
    onError: (err: unknown) => {
      if (err instanceof RecorderConflictError) {
        setSubmitError(err.message);
      } else {
        setSubmitError(err instanceof Error ? err.message : "failed to start");
      }
    },
  });

  const canSubmit =
    datasetName.trim().length > 0 &&
    taskDescription.trim().length > 0 &&
    fps > 0 &&
    !mutation.isPending;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Record dataset</DialogTitle>
          <DialogDescription>
            Capture synchronised robot state + camera frames into a LeRobot dataset.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4">
          <div className="grid gap-2">
            <Label htmlFor="dataset-name">Dataset name</Label>
            <Input
              id="dataset-name"
              value={datasetName}
              onChange={(e) => setDatasetName(e.target.value)}
              placeholder="my-robot-20260414-1700"
              maxLength={64}
              autoFocus
            />
          </div>

          <div className="grid gap-2">
            <Label htmlFor="task-description">Task description</Label>
            <textarea
              id="task-description"
              value={taskDescription}
              onChange={(e) => setTaskDescription(e.target.value)}
              placeholder="Pick up the red cube and place it in the bin."
              rows={3}
              maxLength={500}
              className="flex min-h-[72px] w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
            />
            <p className="text-xs text-muted-foreground">
              {taskDescription.length}/500 — keep it short and action-oriented.
            </p>
          </div>

          <div className="grid grid-cols-[1fr_auto] gap-3">
            <div className="grid gap-2">
              <Label htmlFor="fps">Capture FPS</Label>
              <Select
                id="fps"
                value={String(fps)}
                onChange={(e) => setFps(Number.parseInt(e.target.value, 10))}
              >
                {FPS_OPTIONS.map((v) => (
                  <option key={v} value={v}>
                    {v} fps
                  </option>
                ))}
              </Select>
            </div>
            <label className="flex items-end gap-2 pb-2 text-sm">
              <input
                type="checkbox"
                checked={useVideos}
                onChange={(e) => setUseVideos(e.target.checked)}
                className="h-4 w-4"
              />
              Save as videos
            </label>
          </div>

          {submitError ? (
            <p role="alert" className="text-sm text-destructive">
              {submitError}
            </p>
          ) : null}
        </div>

        <DialogFooter className="gap-2">
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={mutation.isPending}
          >
            Cancel
          </Button>
          <Button
            type="button"
            disabled={!canSubmit}
            onClick={() => {
              setSubmitError(null);
              mutation.mutate({
                robot_id: robotId,
                dataset_name: datasetName.trim(),
                task_description: taskDescription.trim(),
                fps,
                use_videos: useVideos,
              });
            }}
          >
            {mutation.isPending ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                Starting…
              </>
            ) : (
              "Start recording"
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
