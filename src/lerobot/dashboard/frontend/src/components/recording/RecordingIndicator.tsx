import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Loader2, Square } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import {
  formatBytes,
  formatDuration,
  stopRecording,
  type RecordingProgressEvent,
  type RecordingSession,
} from "@/lib/api/recordings";
import { useRecordingProgress } from "@/hooks/useRecordingProgress";

interface RecordingIndicatorProps {
  session: RecordingSession;
  onFinished: (saved: boolean, outcome: RecordingSession) => void;
}

export function RecordingIndicator({
  session,
  onFinished,
}: RecordingIndicatorProps): JSX.Element {
  const queryClient = useQueryClient();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [pendingAction, setPendingAction] = useState<"save" | "discard" | null>(null);

  const terminal = isTerminal(session.status);
  const progress = useRecordingProgress({
    sessionId: session.id,
    enabled: !terminal,
  });

  const snapshot = mergeProgress(session, progress.event);

  const stopMutation = useMutation({
    mutationFn: ({ save }: { save: boolean }) => stopRecording(session.id, save),
    onSuccess: async (finalState, vars) => {
      await queryClient.invalidateQueries({ queryKey: ["recordings"] });
      setConfirmOpen(false);
      setPendingAction(null);
      onFinished(vars.save, finalState);
    },
    onError: () => {
      setPendingAction(null);
    },
  });

  useEffect(() => {
    if (progress.event?.type === "stopped" || progress.event?.type === "error") {
      // Terminal event from server — hand off to parent so it can clear active session.
      onFinished(progress.event.saved ?? false, { ...session, ...toSessionPatch(progress.event) });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [progress.event?.type]);

  const handleConfirm = (save: boolean) => {
    setPendingAction(save ? "save" : "discard");
    stopMutation.mutate({ save });
  };

  return (
    <>
      <section
        data-testid="recording-indicator"
        role="status"
        aria-live="polite"
        className={cn(
          "flex items-center justify-between gap-4 rounded-md border border-destructive/60",
          "bg-destructive/5 px-4 py-2 text-sm",
        )}
      >
        <div className="flex items-center gap-3">
          <span
            className={cn(
              "inline-flex h-2.5 w-2.5 rounded-full bg-destructive",
              snapshot.status === "recording" ? "animate-pulse" : null,
            )}
            aria-hidden
          />
          <div className="flex flex-col">
            <span className="flex items-center gap-2 font-semibold">
              REC
              <span className="font-mono text-xs text-muted-foreground">
                {snapshot.dataset_name}
              </span>
            </span>
            <span className="font-mono text-xs text-muted-foreground">
              {formatDuration(snapshot.duration_s)} · {snapshot.frames_captured} frames
              {snapshot.drop_count > 0 ? ` · ${snapshot.drop_count} drops` : ""}
              {snapshot.disk_bytes > 0 ? ` · ${formatBytes(snapshot.disk_bytes)}` : ""}
            </span>
          </div>
          {progress.connected ? null : (
            <span className="flex items-center gap-1 text-xs text-amber-600 dark:text-amber-400">
              <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
              reconnecting…
            </span>
          )}
        </div>
        <Button
          type="button"
          variant="destructive"
          size="sm"
          onClick={() => setConfirmOpen(true)}
          disabled={terminal || stopMutation.isPending}
        >
          <Square className="h-3.5 w-3.5" aria-hidden />
          Stop
        </Button>
      </section>

      <Dialog open={confirmOpen} onOpenChange={(o) => !stopMutation.isPending && setConfirmOpen(o)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Stop recording</DialogTitle>
            <DialogDescription>
              Save <span className="font-mono">{snapshot.dataset_name}</span> or discard the
              buffered frames?
            </DialogDescription>
          </DialogHeader>

          {stopMutation.isError ? (
            <p role="alert" className="flex items-center gap-1.5 text-sm text-destructive">
              <AlertTriangle className="h-4 w-4" aria-hidden />
              {(stopMutation.error as Error | undefined)?.message ?? "stop failed"}
            </p>
          ) : null}

          <DialogFooter className="gap-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => handleConfirm(false)}
              disabled={stopMutation.isPending}
            >
              {pendingAction === "discard" && stopMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
              ) : null}
              Discard
            </Button>
            <Button
              type="button"
              onClick={() => handleConfirm(true)}
              disabled={stopMutation.isPending}
            >
              {pendingAction === "save" && stopMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
              ) : null}
              Save dataset
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

function isTerminal(status: RecordingSession["status"]): boolean {
  return status === "saved" || status === "discarded" || status === "failed";
}

function mergeProgress(
  session: RecordingSession,
  event: RecordingProgressEvent | null,
): RecordingSession & { duration_s: number } {
  const base = session as RecordingSession & { duration_s: number };
  if (!event) {
    const startedMs = Date.parse(session.started_at);
    const duration =
      Number.isFinite(startedMs) && session.stopped_at == null
        ? (Date.now() - startedMs) / 1000
        : 0;
    return { ...base, duration_s: duration };
  }
  return {
    ...base,
    frames_captured: event.frames_captured,
    drop_count: event.drop_count,
    disk_bytes: event.disk_bytes,
    duration_s: event.duration_s,
    saved: event.saved ?? session.saved,
    episode_index: event.episode_index ?? session.episode_index,
    error: event.type === "error" ? event.message ?? session.error : session.error,
  };
}

function toSessionPatch(event: RecordingProgressEvent): Partial<RecordingSession> {
  return {
    frames_captured: event.frames_captured,
    drop_count: event.drop_count,
    disk_bytes: event.disk_bytes,
    saved: event.saved ?? false,
    episode_index: event.episode_index ?? null,
    error: event.type === "error" ? event.message ?? null : null,
    status: event.type === "error" ? "failed" : event.saved ? "saved" : "discarded",
    stopped_at: new Date().toISOString(),
  };
}
