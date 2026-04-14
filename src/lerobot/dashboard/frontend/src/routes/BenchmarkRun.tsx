// Benchmark run detail page (Task #16 Phase B — stub).
// Full implementation (WS progress, obs preview, cancel confirmation)
// follows after robotics-integrator-3 answers RFC open questions.

import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  getBenchmarkRun,
  isTerminalStatus,
  runnerLabel,
  type BenchmarkRunSummary,
  type RunStatus,
} from "@/lib/api/benchmarks";

export function BenchmarkRunPage(): JSX.Element {
  const { runId = "" } = useParams<{ runId: string }>();

  const runQuery = useQuery<BenchmarkRunSummary | null>({
    queryKey: ["benchmark-run", runId],
    queryFn: () => getBenchmarkRun(runId),
    enabled: Boolean(runId),
    refetchInterval: (query) => {
      const run = query.state.data;
      if (!run || isTerminalStatus(run.status)) return false;
      return 2_000;
    },
  });

  if (runQuery.isLoading) {
    return (
      <div className="flex items-center gap-2 text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
        Loading run…
      </div>
    );
  }

  if (runQuery.isError || runQuery.data === null) {
    return (
      <div className="space-y-3">
        <p className="text-sm text-muted-foreground">Run not found.</p>
        <Button asChild variant="outline">
          <Link to="/benchmarks">
            <ArrowLeft className="h-4 w-4" aria-hidden />
            Back to benchmarks
          </Link>
        </Button>
      </div>
    );
  }

  const run = runQuery.data;

  return (
    <div className="space-y-6">
      <header className="flex items-center gap-3">
        <Button asChild variant="ghost" size="icon" aria-label="Back to benchmarks">
          <Link to="/benchmarks">
            <ArrowLeft className="h-4 w-4" aria-hidden />
          </Link>
        </Button>
        <div className="flex-1">
          <h1 className="text-xl font-semibold tracking-tight">{run.env_name}</h1>
          <p className="text-xs text-muted-foreground">
            {runnerLabel(run)} · {run.episodes} episodes ·{" "}
            <span className="font-mono">{run.run_id.slice(0, 8)}</span>
          </p>
        </div>
        <StatusPill status={run.status} />
      </header>

      {/* Progress summary */}
      <div
        className="rounded-md border bg-card p-4"
        data-testid="benchmark-run-summary"
      >
        <div className="grid grid-cols-3 gap-4 text-sm">
          <div>
            <p className="text-xs text-muted-foreground">Episodes</p>
            <p className="text-xl font-semibold tabular-nums">
              {run.current_episode} / {run.episodes}
            </p>
          </div>
          <div>
            <p className="text-xs text-muted-foreground">Steps</p>
            <p className="text-xl font-semibold tabular-nums">{run.steps_total}</p>
          </div>
          <div>
            <p className="text-xs text-muted-foreground">Last reward</p>
            <p className="text-xl font-semibold tabular-nums">
              {run.last_reward !== null ? run.last_reward.toFixed(3) : "—"}
            </p>
          </div>
        </div>

        {!isTerminalStatus(run.status) ? (
          <div className="mt-4">
            <div className="h-2 w-full overflow-hidden rounded bg-muted">
              <div
                className="h-full rounded bg-primary transition-[width]"
                style={{ width: `${Math.round(run.progress * 100)}%` }}
                role="progressbar"
                aria-valuenow={Math.round(run.progress * 100)}
                aria-valuemin={0}
                aria-valuemax={100}
                data-testid="benchmark-progress"
              />
            </div>
            <p className="mt-1 text-right text-[11px] text-muted-foreground">
              {Math.round(run.progress * 100)}%
            </p>
          </div>
        ) : null}

        {run.error ? (
          <p role="alert" className="mt-3 text-sm text-destructive">
            {run.error.code}: {run.error.message}
          </p>
        ) : null}
      </div>

      {/* Phase B: WS live progress + obs preview will be added here */}
      <div className="rounded-md border border-dashed p-6 text-center text-sm text-muted-foreground">
        Live step stream and observation preview coming in Phase B.
      </div>
    </div>
  );
}

function StatusPill({ status }: { status: RunStatus }): JSX.Element {
  const styles: Record<RunStatus, string> = {
    queued: "bg-muted text-muted-foreground",
    running: "bg-blue-500/15 text-blue-600 dark:text-blue-400",
    completed: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
    cancelled: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
    failed: "bg-destructive/15 text-destructive",
  };
  return (
    <span
      className={`inline-flex items-center gap-1 rounded px-2.5 py-1 text-xs font-medium ${styles[status]}`}
      data-testid="benchmark-run-status"
    >
      {status === "running" ? (
        <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
      ) : null}
      {status}
    </span>
  );
}
