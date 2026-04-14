// Benchmark run detail page (Task #16 Phase B).
// WS-driven: live step stream, obs JPG preview, episode mp4 player,
// Cancel with confirmation dialog.

import { useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  Loader2,
  Play,
  Square,
  Video,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  cancelBenchmarkRun,
  isTerminalStatus,
  runnerLabel,
  type BenchmarkRunSummary,
  type RunStatus,
} from "@/lib/api/benchmarks";
import { useBenchmarkRun } from "@/hooks/useBenchmarkRun";

export function BenchmarkRunPage(): JSX.Element {
  const { runId = "" } = useParams<{ runId: string }>();
  const queryClient = useQueryClient();
  const [cancelOpen, setCancelOpen] = useState(false);

  const {
    summary,
    steps,
    previews,
    latestObsUrl,
    terminal,
    errorMessage,
    connected,
    reconnectAttempt,
  } = useBenchmarkRun(runId || null);

  const { mutate: cancelRun, isPending: cancelling } = useMutation({
    mutationFn: () => cancelBenchmarkRun(runId),
    onSuccess: () => {
      setCancelOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["benchmark-runs"] });
    },
  });

  if (!summary) {
    return (
      <div className="flex items-center gap-2 text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
        {connected ? "Waiting for run data…" : reconnectAttempt > 0
          ? `Reconnecting… (attempt ${reconnectAttempt})`
          : "Connecting…"}
      </div>
    );
  }

  const active = !isTerminalStatus(summary.status);
  const pct = Math.round(summary.progress * 100);

  return (
    <div className="space-y-5">
      {/* Header */}
      <header className="flex items-center gap-3">
        <Button asChild variant="ghost" size="icon" aria-label="Back to benchmarks">
          <Link to="/benchmarks">
            <ArrowLeft className="h-4 w-4" aria-hidden />
          </Link>
        </Button>
        <div className="flex-1">
          <h1 className="text-xl font-semibold tracking-tight">{summary.env_name}</h1>
          <p className="text-xs text-muted-foreground">
            {runnerLabel(summary)} · {summary.episodes} ep ·{" "}
            <span className="font-mono">{summary.run_id.slice(0, 8)}</span>
            {!terminal && !connected ? (
              <span className="ml-2 text-amber-600 dark:text-amber-400">
                <Loader2 className="mr-0.5 inline h-2.5 w-2.5 animate-spin" aria-hidden />
                reconnecting
              </span>
            ) : null}
          </p>
        </div>
        <StatusPill status={summary.status} />
        {active ? (
          <Button
            variant="destructive"
            size="sm"
            onClick={() => setCancelOpen(true)}
            data-testid="benchmark-cancel-btn"
          >
            <Square className="h-3.5 w-3.5" aria-hidden />
            Cancel
          </Button>
        ) : null}
      </header>

      {/* Progress + stats */}
      <div
        className="grid gap-4 rounded-md border bg-card p-4 md:grid-cols-[1fr_auto]"
        data-testid="benchmark-progress"
      >
        <div className="space-y-3">
          <div className="grid grid-cols-3 gap-4 text-sm">
            <Stat label="Episode" value={`${summary.current_episode} / ${summary.episodes}`} />
            <Stat label="Steps" value={String(summary.steps_total)} />
            <Stat
              label="Last reward"
              value={summary.last_reward !== null ? summary.last_reward.toFixed(3) : "—"}
            />
          </div>
          {active ? (
            <div>
              <div className="h-2 overflow-hidden rounded bg-muted">
                <div
                  className="h-full rounded bg-primary transition-[width]"
                  style={{ width: `${pct}%` }}
                  role="progressbar"
                  aria-valuenow={pct}
                  aria-valuemin={0}
                  aria-valuemax={100}
                />
              </div>
              <p className="mt-1 text-right text-[11px] tabular-nums text-muted-foreground">
                {pct}%
              </p>
            </div>
          ) : null}
          {summary.error ? (
            <p role="alert" className="flex items-center gap-1 text-sm text-destructive">
              <AlertTriangle className="h-4 w-4" aria-hidden />
              {summary.error.code}: {summary.error.message}
            </p>
          ) : null}
          {errorMessage && !summary.error ? (
            <p role="alert" className="flex items-center gap-1 text-sm text-destructive">
              <AlertTriangle className="h-4 w-4" aria-hidden />
              {errorMessage}
            </p>
          ) : null}
          {summary.status === "completed" && summary.result ? (
            <p className="text-xs text-muted-foreground">
              Completed{" "}
              {typeof summary.result.episodes_completed === "number"
                ? `${summary.result.episodes_completed} episodes`
                : ""}
              .
            </p>
          ) : null}
        </div>

        {/* Live obs preview */}
        {latestObsUrl ? (
          <div className="flex flex-col items-center gap-1">
            <img
              src={latestObsUrl}
              alt="Latest observation"
              className="h-28 w-auto rounded border object-contain"
              data-testid="benchmark-obs-preview"
            />
            <span className="text-[10px] text-muted-foreground">Latest obs</span>
          </div>
        ) : (
          <div
            className="flex h-28 w-32 items-center justify-center rounded border bg-muted text-muted-foreground"
            data-testid="benchmark-obs-preview"
            aria-label="No observation yet"
          >
            <Video className="h-6 w-6" aria-hidden />
          </div>
        )}
      </div>

      {/* Episode previews (mp4) */}
      {previews.length > 0 ? (
        <section className="space-y-2">
          <h2 className="text-sm font-semibold">Episode previews</h2>
          <div
            className="flex flex-wrap gap-3"
            data-testid="benchmark-previews"
          >
            {previews.map((p) => (
              <EpisodePreviewCard key={p.episode} preview={p} />
            ))}
          </div>
        </section>
      ) : null}

      {/* Step timeline */}
      <section className="space-y-2">
        <h2 className="text-sm font-semibold">
          Step log{" "}
          <span className="font-normal text-muted-foreground">
            (last {Math.min(steps.length, 100)})
          </span>
        </h2>
        <StepTimeline steps={steps} />
      </section>

      {/* Cancel confirmation */}
      <CancelDialog
        open={cancelOpen}
        envName={summary.env_name}
        isPending={cancelling}
        onConfirm={() => cancelRun()}
        onCancel={() => setCancelOpen(false)}
      />
    </div>
  );
}

// ── Sub-components ────────────────────────────────────────────────────────────

function EpisodePreviewCard({
  preview,
}: {
  preview: { episode: number; preview_url: string; policy_slug: string };
}): JSX.Element {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [playing, setPlaying] = useState(false);

  const toggle = () => {
    const v = videoRef.current;
    if (!v) return;
    if (playing) {
      v.pause();
    } else {
      void v.play();
    }
  };

  return (
    <div
      className="flex flex-col items-center gap-1"
      data-testid="benchmark-preview-card"
    >
      <div className="group relative h-20 w-32 overflow-hidden rounded border bg-black">
        <video
          ref={videoRef}
          src={preview.preview_url}
          className="h-full w-full object-contain"
          muted
          loop
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
        />
        <button
          type="button"
          onClick={toggle}
          aria-label={playing ? `Pause episode ${preview.episode}` : `Play episode ${preview.episode}`}
          className="absolute inset-0 flex items-center justify-center bg-black/30 opacity-0 transition-opacity group-hover:opacity-100"
        >
          {playing ? (
            <Square className="h-5 w-5 text-white" aria-hidden />
          ) : (
            <Play className="h-5 w-5 text-white" aria-hidden />
          )}
        </button>
      </div>
      <span className="text-[10px] text-muted-foreground">ep {preview.episode}</span>
    </div>
  );
}

function StepTimeline({
  steps,
}: {
  steps: Array<{ episode: number; step: number; reward: number; done: boolean }>;
}): JSX.Element {
  const tail = steps.slice(-100);

  if (tail.length === 0) {
    return (
      <div
        className="rounded-md border border-dashed p-4 text-sm text-muted-foreground"
        data-testid="benchmark-timeline"
      >
        Waiting for first step…
      </div>
    );
  }

  return (
    <div
      className="max-h-56 overflow-y-auto rounded-md border bg-muted/30 font-mono text-[11px]"
      data-testid="benchmark-timeline"
    >
      <ul className="divide-y divide-border">
        {tail.map((s, i) => (
          <li
            key={`${s.episode}-${s.step}-${i}`}
            className="flex items-baseline gap-3 px-3 py-1"
          >
            <span className="w-16 shrink-0 text-muted-foreground">
              ep {s.episode}
            </span>
            <span className="w-16 shrink-0 text-muted-foreground">
              step {s.step}
            </span>
            <span className="tabular-nums">{s.reward.toFixed(4)}</span>
            {s.done ? (
              <span className="ml-auto text-[10px] text-emerald-600 dark:text-emerald-400">
                done
              </span>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

function CancelDialog({
  open,
  envName,
  isPending,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  envName: string;
  isPending: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}): JSX.Element {
  return (
    <Dialog
      open={open}
      onOpenChange={(o) => { if (!isPending && !o) onCancel(); }}
    >
      <DialogContent data-testid="benchmark-cancel-confirm">
        <DialogHeader>
          <DialogTitle>Cancel benchmark?</DialogTitle>
          <DialogDescription>
            Cancelling <span className="font-medium">{envName}</span> will stop the
            run immediately. Steps collected so far are preserved in the parquet
            file, but the run will be marked as cancelled.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter className="gap-2">
          <Button
            type="button"
            variant="outline"
            onClick={onCancel}
            disabled={isPending}
          >
            Keep running
          </Button>
          <Button
            type="button"
            variant="destructive"
            onClick={onConfirm}
            disabled={isPending}
          >
            {isPending ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                Cancelling…
              </>
            ) : (
              "Cancel run"
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Stat({ label, value }: { label: string; value: string }): JSX.Element {
  return (
    <div>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="text-xl font-semibold tabular-nums">{value}</p>
    </div>
  );
}

const STATUS_STYLES: Record<RunStatus, string> = {
  queued: "bg-muted text-muted-foreground",
  running: "bg-blue-500/15 text-blue-600 dark:text-blue-400",
  completed: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  cancelled: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
  failed: "bg-destructive/15 text-destructive",
};

function StatusPill({ status }: { status: RunStatus }): JSX.Element {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded px-2.5 py-1 text-xs font-medium ${STATUS_STYLES[status]}`}
      data-testid="benchmark-run-status"
    >
      {status === "running" ? (
        <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
      ) : null}
      {status}
    </span>
  );
}
