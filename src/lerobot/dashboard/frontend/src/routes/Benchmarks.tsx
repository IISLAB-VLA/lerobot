// Benchmarks list page (Task #16 Phase A).
// Shows available envs, past runs table, and "Run new benchmark" modal.

import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, Loader2, Play } from "lucide-react";
import { Button } from "@/components/ui/button";
import { BenchmarkRunModal } from "@/components/benchmarks/BenchmarkRunModal";
import {
  isTerminalStatus,
  listBenchmarkRuns,
  runnerLabel,
  type BenchmarkRunSummary,
  type RunStatus,
} from "@/lib/api/benchmarks";

export function BenchmarksPage(): JSX.Element {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [modalOpen, setModalOpen] = useState(false);

  const runsQuery = useQuery<BenchmarkRunSummary[]>({
    queryKey: ["benchmark-runs"],
    queryFn: listBenchmarkRuns,
    refetchInterval: (query) => {
      // Poll every 2s while any run is active; back off to 10s when all terminal.
      const runs = query.state.data ?? [];
      return runs.some((r) => !isTerminalStatus(r.status)) ? 2_000 : 10_000;
    },
  });

  const runs = runsQuery.data ?? [];

  return (
    <div className="space-y-6">
      <header className="flex items-end justify-between gap-4">
        <div className="space-y-1">
          <h1 className="text-2xl font-bold tracking-tight">Benchmarks</h1>
          <p className="text-muted-foreground">
            Run policy or random-action evaluations against registered Gym environments.
          </p>
        </div>
        <Button onClick={() => setModalOpen(true)} data-testid="benchmark-new-btn">
          <Play className="h-4 w-4" aria-hidden />
          Run new benchmark
        </Button>
      </header>

      {runsQuery.isLoading ? (
        <RunsTableSkeleton />
      ) : runsQuery.isError ? (
        <div
          role="alert"
          className="rounded-md border border-destructive/50 bg-destructive/5 p-4 text-sm text-destructive"
        >
          Failed to load runs:{" "}
          {runsQuery.error instanceof Error ? runsQuery.error.message : "unknown error"}
        </div>
      ) : runs.length === 0 ? (
        <EmptyState onNew={() => setModalOpen(true)} />
      ) : (
        <RunsTable runs={runs} />
      )}

      <BenchmarkRunModal
        open={modalOpen}
        onOpenChange={setModalOpen}
        onStarted={(run) => {
          void queryClient.invalidateQueries({ queryKey: ["benchmark-runs"] });
          navigate(`/benchmarks/runs/${run.run_id}`);
        }}
      />
    </div>
  );
}

function RunsTable({ runs }: { runs: BenchmarkRunSummary[] }): JSX.Element {
  // Most-recent first.
  const sorted = [...runs].sort(
    (a, b) => new Date(b.started_at).getTime() - new Date(a.started_at).getTime(),
  );

  return (
    <div
      className="overflow-hidden rounded-md border"
      data-testid="benchmark-runs-table"
    >
      <table className="w-full text-sm">
        <thead className="border-b bg-muted/40">
          <tr>
            <th className="px-4 py-2.5 text-left font-medium text-muted-foreground">
              Environment
            </th>
            <th className="px-4 py-2.5 text-left font-medium text-muted-foreground">
              Runner
            </th>
            <th className="px-4 py-2.5 text-right font-medium text-muted-foreground">
              Episodes
            </th>
            <th className="px-4 py-2.5 text-left font-medium text-muted-foreground">
              Status
            </th>
            <th className="w-40 px-4 py-2.5 text-left font-medium text-muted-foreground">
              Progress
            </th>
            <th className="px-4 py-2.5 text-left font-medium text-muted-foreground">
              Started
            </th>
            <th className="w-10 px-2" />
          </tr>
        </thead>
        <tbody className="divide-y">
          {sorted.map((run) => (
            <RunRow key={run.run_id} run={run} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RunRow({ run }: { run: BenchmarkRunSummary }): JSX.Element {
  return (
    <tr
      className="hover:bg-muted/30 transition-colors"
      data-testid="benchmark-run-row"
    >
      <td className="px-4 py-2.5 font-medium">
        {run.env_name}
        {run.task ? (
          <span className="ml-1.5 text-xs text-muted-foreground">{run.task}</span>
        ) : null}
      </td>
      <td className="px-4 py-2.5 font-mono text-xs text-muted-foreground">
        {runnerLabel(run)}
      </td>
      <td className="px-4 py-2.5 text-right tabular-nums">{run.episodes}</td>
      <td className="px-4 py-2.5">
        <StatusBadge status={run.status} />
      </td>
      <td className="px-4 py-2.5">
        <ProgressCell run={run} />
      </td>
      <td className="px-4 py-2.5 text-xs text-muted-foreground">
        {new Date(run.started_at).toLocaleString()}
      </td>
      <td className="px-2 py-2.5">
        <Button asChild variant="ghost" size="icon" aria-label="View run">
          <Link to={`/benchmarks/runs/${run.run_id}`}>
            <ChevronRight className="h-4 w-4" aria-hidden />
          </Link>
        </Button>
      </td>
    </tr>
  );
}

const STATUS_STYLE: Record<RunStatus, string> = {
  queued: "bg-muted text-muted-foreground",
  running: "bg-blue-500/15 text-blue-600 dark:text-blue-400",
  completed: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  cancelled: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
  failed: "bg-destructive/15 text-destructive",
};

function StatusBadge({ status }: { status: RunStatus }): JSX.Element {
  return (
    <span
      className={`inline-flex items-center rounded px-2 py-0.5 text-[11px] font-medium ${STATUS_STYLE[status]}`}
      data-testid="benchmark-status"
    >
      {status === "running" ? (
        <Loader2 className="mr-1 h-2.5 w-2.5 animate-spin" aria-hidden />
      ) : null}
      {status}
    </span>
  );
}

function ProgressCell({ run }: { run: BenchmarkRunSummary }): JSX.Element {
  if (isTerminalStatus(run.status)) {
    return (
      <span className="text-xs text-muted-foreground">
        {run.current_episode} / {run.episodes} ep
      </span>
    );
  }
  const pct = Math.round(run.progress * 100);
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-24 overflow-hidden rounded bg-muted">
        <div
          className="h-full rounded bg-primary transition-[width]"
          style={{ width: `${pct}%` }}
          role="progressbar"
          aria-valuenow={pct}
          aria-valuemin={0}
          aria-valuemax={100}
        />
      </div>
      <span className="w-8 text-right text-[11px] text-muted-foreground tabular-nums">
        {pct}%
      </span>
    </div>
  );
}

function EmptyState({ onNew }: { onNew: () => void }): JSX.Element {
  return (
    <div
      className="flex flex-col items-center justify-center gap-3 rounded-lg border border-dashed py-16 text-center"
      data-testid="benchmark-empty-state"
    >
      <h2 className="text-lg font-semibold">No runs yet</h2>
      <p className="max-w-md text-sm text-muted-foreground">
        Start a benchmark to evaluate a policy or random-action baseline on a Gym
        environment.
      </p>
      <Button onClick={onNew}>
        <Play className="h-4 w-4" aria-hidden />
        Run first benchmark
      </Button>
    </div>
  );
}

function RunsTableSkeleton(): JSX.Element {
  return (
    <div className="overflow-hidden rounded-md border" aria-hidden>
      {Array.from({ length: 3 }).map((_, i) => (
        <div key={i} className="flex items-center gap-4 border-b px-4 py-3">
          <div className="h-4 w-32 animate-pulse rounded bg-muted" />
          <div className="h-4 w-20 animate-pulse rounded bg-muted" />
          <div className="ml-auto h-4 w-16 animate-pulse rounded bg-muted" />
        </div>
      ))}
    </div>
  );
}
