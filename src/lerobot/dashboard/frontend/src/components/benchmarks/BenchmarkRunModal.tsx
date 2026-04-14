// Modal for starting a new benchmark run (Task #16 Phase A).

import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
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
  listBenchmarkEnvs,
  startBenchmarkRun,
  type BenchmarkInfo,
  type BenchmarkRunSummary,
} from "@/lib/api/benchmarks";

interface BenchmarkRunModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onStarted: (run: BenchmarkRunSummary) => void;
}

const EPISODE_OPTIONS = [1, 5, 10, 20, 50, 100];

export function BenchmarkRunModal({
  open,
  onOpenChange,
  onStarted,
}: BenchmarkRunModalProps): JSX.Element {
  const [envName, setEnvName] = useState("");
  const [episodes, setEpisodes] = useState(5);
  const [policyRef, setPolicyRef] = useState("");
  const [seedText, setSeedText] = useState("");
  const [submitError, setSubmitError] = useState<string | null>(null);

  const envsQuery = useQuery<BenchmarkInfo[]>({
    queryKey: ["benchmark-envs"],
    queryFn: listBenchmarkEnvs,
    enabled: open,
    staleTime: 60_000,
  });

  // Auto-select first env when list loads.
  useEffect(() => {
    if (!envName && envsQuery.data && envsQuery.data.length > 0) {
      setEnvName(envsQuery.data[0]!.env_name);
    }
  }, [envsQuery.data, envName]);

  // Reset on close.
  useEffect(() => {
    if (!open) {
      setEnvName("");
      setEpisodes(5);
      setPolicyRef("");
      setSeedText("");
      setSubmitError(null);
    }
  }, [open]);

  const { mutate: startRun, isPending } = useMutation({
    mutationFn: () => {
      const seed = seedText.trim() ? Number.parseInt(seedText.trim(), 10) : null;
      return startBenchmarkRun({
        env_name: envName,
        episodes,
        policy_refs: policyRef.trim() ? [policyRef.trim()] : [],
        seed: Number.isFinite(seed) ? seed : null,
      });
    },
    onSuccess: (run) => {
      onStarted(run);
      onOpenChange(false);
    },
    onError: (err: unknown) => {
      setSubmitError(err instanceof Error ? err.message : "Failed to start run");
    },
  });

  const selectedEnv = envsQuery.data?.find((e) => e.env_name === envName) ?? null;
  const canSubmit = envName.length > 0 && episodes > 0 && !isPending;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Run new benchmark</DialogTitle>
          <DialogDescription>
            Start a benchmark run. Leave policy blank for a random-action baseline.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4">
          <div className="grid gap-2">
            <Label htmlFor="bench-env">Environment</Label>
            {envsQuery.isLoading ? (
              <div className="flex items-center gap-1.5 text-sm text-muted-foreground">
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
                Loading environments…
              </div>
            ) : envsQuery.isError ? (
              <p role="alert" className="text-xs text-destructive">
                Failed to load environments.
              </p>
            ) : (
              <Select
                id="bench-env"
                value={envName}
                onChange={(e) => setEnvName(e.target.value)}
                data-testid="benchmark-env-select"
              >
                {(envsQuery.data ?? []).map((env) => (
                  <option key={env.env_name} value={env.env_name}>
                    {env.env_name}
                    {env.task ? ` — ${env.task}` : ""}
                  </option>
                ))}
              </Select>
            )}
            {selectedEnv ? (
              <p className="text-[11px] text-muted-foreground">
                {selectedEnv.config_type} · {selectedEnv.fps} fps
              </p>
            ) : null}
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="grid gap-2">
              <Label htmlFor="bench-episodes">Episodes</Label>
              <Select
                id="bench-episodes"
                value={String(episodes)}
                onChange={(e) => setEpisodes(Number.parseInt(e.target.value, 10))}
                data-testid="benchmark-episodes"
              >
                {EPISODE_OPTIONS.map((v) => (
                  <option key={v} value={v}>
                    {v}
                  </option>
                ))}
              </Select>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="bench-seed">Seed (optional)</Label>
              <Input
                id="bench-seed"
                inputMode="numeric"
                value={seedText}
                onChange={(e) => setSeedText(e.target.value)}
                placeholder="random"
              />
            </div>
          </div>

          <div className="grid gap-2">
            <Label htmlFor="bench-policy">Policy repo (optional)</Label>
            <Input
              id="bench-policy"
              value={policyRef}
              onChange={(e) => setPolicyRef(e.target.value)}
              placeholder="lerobot/smolvla_base — empty for random actions"
              data-testid="benchmark-policy"
            />
            <p className="text-[11px] text-muted-foreground">
              Leave blank to run a random-action baseline.
            </p>
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
            disabled={isPending}
          >
            Cancel
          </Button>
          <Button
            type="button"
            disabled={!canSubmit}
            onClick={() => { setSubmitError(null); startRun(); }}
            data-testid="benchmark-start"
          >
            {isPending ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                Starting…
              </>
            ) : (
              "Start run"
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
