import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
} from "@tanstack/react-query";
import { ArrowLeft, Loader2, Play, RefreshCw, Square } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { listRobots, type RobotEntry } from "@/lib/api/robots";
import {
  InferenceConflictError,
  listPolicies,
  setInferenceCommand,
  setInferenceDeadman,
  startInference,
  stopInference,
  type InferenceSession,
  type PolicyDescriptor,
} from "@/lib/api/inference";
import { useInferenceSession } from "@/hooks/useInferenceSession";

const FPS_OPTIONS = [10, 15, 20, 30, 60];
const STEP_TAIL = 50;

export function RobotInferencePage(): JSX.Element {
  const { id = "" } = useParams<{ id: string }>();
  const queryClient = useQueryClient();

  const robotsQuery = useQuery<RobotEntry[]>({
    queryKey: ["robots"],
    queryFn: listRobots,
  });
  const policiesQuery = useQuery<PolicyDescriptor[]>({
    queryKey: ["policies"],
    queryFn: listPolicies,
  });

  const robot = robotsQuery.data?.find((r) => r.id === id);
  const policies = useMemo(() => policiesQuery.data ?? [], [policiesQuery.data]);

  const [repoId, setRepoId] = useState<string>("");
  const [fps, setFps] = useState<number>(30);
  const [taskDescription, setTaskDescription] = useState("");
  const [dryRun, setDryRun] = useState(true);
  const [deadmanRequired, setDeadmanRequired] = useState(true);
  const [maxActionMagnitudeText, setMaxActionMagnitudeText] = useState("");
  const [activeSession, setActiveSession] = useState<InferenceSession | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [commandDraft, setCommandDraft] = useState("");
  const [deadmanHeld, setDeadmanHeld] = useState(false);

  const selectedPolicy = useMemo(
    () => policies.find((p) => p.repo_id === repoId) ?? null,
    [policies, repoId],
  );

  useEffect(() => {
    if (!repoId && policies.length > 0 && policies[0]) {
      setRepoId(policies[0].repo_id);
    }
  }, [policies, repoId]);

  useEffect(() => {
    setCommandDraft(taskDescription);
  }, [activeSession?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const subscription = useInferenceSession({
    sessionId: activeSession?.id ?? null,
    enabled: Boolean(activeSession && !subscriptionTerminal(activeSession)),
  });

  useEffect(() => {
    if (subscription.terminal && activeSession) {
      setActiveSession({
        ...activeSession,
        status: "stopped",
        stopped_at: new Date().toISOString(),
        step: subscription.terminal.steps,
      });
    } else if (subscription.errorMessage && activeSession) {
      setActiveSession({
        ...activeSession,
        status: "failed",
        error: subscription.errorMessage,
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subscription.terminal, subscription.errorMessage]);

  const parsedMaxAction = useMemo<number | null>(() => {
    const trimmed = maxActionMagnitudeText.trim();
    if (!trimmed) return null;
    const n = Number.parseFloat(trimmed);
    return Number.isFinite(n) && n > 0 ? n : null;
  }, [maxActionMagnitudeText]);

  const startMutation = useMutation({
    mutationFn: () =>
      startInference({
        robot_id: id,
        repo_id: repoId,
        fps,
        task_description: taskDescription,
        dry_run: dryRun,
        deadman_required: deadmanRequired,
        max_action_magnitude: parsedMaxAction,
      }),
    onSuccess: (session) => {
      setActiveSession(session);
      setSubmitError(null);
      setDeadmanHeld(false);
      void queryClient.invalidateQueries({ queryKey: ["inference"] });
    },
    onError: (err: unknown) => {
      setSubmitError(
        err instanceof InferenceConflictError
          ? err.message
          : err instanceof Error
            ? err.message
            : "failed to start",
      );
    },
  });

  const stopMutation = useMutation({
    mutationFn: () => {
      if (!activeSession) throw new Error("no active session");
      return stopInference(activeSession.id);
    },
    onSuccess: (final) => {
      setActiveSession(final);
    },
  });

  const commandMutation = useMutation({
    mutationFn: () => {
      if (!activeSession) throw new Error("no active session");
      return setInferenceCommand(activeSession.id, { text: commandDraft });
    },
  });

  const deadmanMutation = useMutation({
    mutationFn: (held: boolean) => {
      if (!activeSession) throw new Error("no active session");
      return setInferenceDeadman(activeSession.id, { held });
    },
    onSuccess: (session, held) => {
      setActiveSession(session);
      setDeadmanHeld(held);
    },
  });

  const deadmanActive = Boolean(
    activeSession &&
      activeSession.deadman_required &&
      !activeSession.dry_run &&
      (activeSession.status === "running" || activeSession.status === "starting"),
  );

  useEffect(() => {
    if (!deadmanActive) return undefined;
    const isEditable = (target: EventTarget | null) => {
      if (!(target instanceof HTMLElement)) return false;
      if (target.isContentEditable) return true;
      const tag = target.tagName;
      return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.code !== "Space" || event.repeat) return;
      if (isEditable(event.target)) return;
      event.preventDefault();
      if (!deadmanHeld) deadmanMutation.mutate(true);
    };
    const onKeyUp = (event: KeyboardEvent) => {
      if (event.code !== "Space") return;
      if (isEditable(event.target)) return;
      event.preventDefault();
      if (deadmanHeld) deadmanMutation.mutate(false);
    };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      // Release if we navigate away or the session ends mid-hold so the loop pauses cleanly.
      if (deadmanHeld) deadmanMutation.mutate(false);
    };
  }, [deadmanActive, deadmanHeld, deadmanMutation]);

  if (robotsQuery.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading robot…</p>;
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

  const running = activeSession?.status === "running" || activeSession?.status === "starting";
  const tailSteps = subscription.steps.slice(-STEP_TAIL);

  return (
    <div className="flex h-full flex-col gap-4">
      <header className="flex items-center gap-3">
        <Button asChild variant="ghost" size="icon" aria-label="Back to robot">
          <Link to={`/robots/${robot.id}`}>
            <ArrowLeft className="h-4 w-4" aria-hidden />
          </Link>
        </Button>
        <div className="flex-1">
          <h1 className="text-xl font-semibold tracking-tight">Run policy on {robot.name}</h1>
          <p className="text-xs text-muted-foreground">
            {robot.robot_type} ·{" "}
            {policiesQuery.isLoading
              ? "scanning HF cache…"
              : `${policies.length} cached polic${policies.length === 1 ? "y" : "ies"}`}
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => void policiesQuery.refetch()}
          disabled={policiesQuery.isFetching}
        >
          <RefreshCw
            className={`h-4 w-4 ${policiesQuery.isFetching ? "animate-spin" : ""}`}
            aria-hidden
          />
          Refresh cache
        </Button>
      </header>

      <div className="grid gap-4 md:grid-cols-[2fr_3fr]">
        <ConfigPanel
          policies={policies}
          loadingPolicies={policiesQuery.isLoading}
          errorPolicies={policiesQuery.isError}
          repoId={repoId}
          setRepoId={setRepoId}
          selectedPolicy={selectedPolicy}
          fps={fps}
          setFps={setFps}
          taskDescription={taskDescription}
          setTaskDescription={setTaskDescription}
          dryRun={dryRun}
          setDryRun={setDryRun}
          deadmanRequired={deadmanRequired}
          setDeadmanRequired={setDeadmanRequired}
          maxActionMagnitudeText={maxActionMagnitudeText}
          setMaxActionMagnitudeText={setMaxActionMagnitudeText}
          startMutation={startMutation}
          stopMutation={stopMutation}
          activeSession={activeSession}
          submitError={submitError}
          running={Boolean(running)}
        />

        <SessionPanel
          activeSession={activeSession}
          tailSteps={tailSteps}
          subscription={subscription}
          commandDraft={commandDraft}
          setCommandDraft={setCommandDraft}
          commandMutation={commandMutation}
          deadmanHeld={deadmanHeld}
          deadmanActive={deadmanActive}
          deadmanPending={deadmanMutation.isPending}
          supportsLanguage={selectedPolicy?.supports_language ?? false}
          running={Boolean(running)}
        />
      </div>
    </div>
  );
}

function subscriptionTerminal(session: InferenceSession): boolean {
  return session.status === "stopped" || session.status === "failed";
}

interface ConfigPanelProps {
  policies: PolicyDescriptor[];
  loadingPolicies: boolean;
  errorPolicies: boolean;
  repoId: string;
  setRepoId: (v: string) => void;
  selectedPolicy: PolicyDescriptor | null;
  fps: number;
  setFps: (v: number) => void;
  taskDescription: string;
  setTaskDescription: (v: string) => void;
  dryRun: boolean;
  setDryRun: (v: boolean) => void;
  deadmanRequired: boolean;
  setDeadmanRequired: (v: boolean) => void;
  maxActionMagnitudeText: string;
  setMaxActionMagnitudeText: (v: string) => void;
  startMutation: UseMutationResult<InferenceSession, unknown, void, unknown>;
  stopMutation: UseMutationResult<InferenceSession, unknown, void, unknown>;
  activeSession: InferenceSession | null;
  submitError: string | null;
  running: boolean;
}

function ConfigPanel({
  policies,
  loadingPolicies,
  errorPolicies,
  repoId,
  setRepoId,
  selectedPolicy,
  fps,
  setFps,
  taskDescription,
  setTaskDescription,
  dryRun,
  setDryRun,
  deadmanRequired,
  setDeadmanRequired,
  maxActionMagnitudeText,
  setMaxActionMagnitudeText,
  startMutation,
  stopMutation,
  activeSession,
  submitError,
  running,
}: ConfigPanelProps): JSX.Element {
  const canStart =
    !running &&
    repoId.length > 0 &&
    taskDescription.trim().length > 0 &&
    !startMutation.isPending;

  return (
    <section className="flex flex-col gap-4 rounded-md border bg-card p-4">
      <div className="grid gap-2">
        <Label htmlFor="policy">Policy</Label>
        {loadingPolicies ? (
          <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            Scanning HF cache…
          </p>
        ) : errorPolicies ? (
          <p role="alert" className="text-xs text-destructive">
            Failed to load policy list. Check that the backend is reachable.
          </p>
        ) : policies.length > 0 ? (
          <Select id="policy" value={repoId} onChange={(e) => setRepoId(e.target.value)}>
            {policies.map((p) => (
              <option key={p.repo_id} value={p.repo_id}>
                {p.repo_id} ({p.policy_type})
              </option>
            ))}
          </Select>
        ) : (
          <p className="text-xs text-muted-foreground">
            No cached policies discovered. Pre-pull one with{" "}
            <code className="font-mono">huggingface-cli download {`<repo>`}</code>.
          </p>
        )}
        {selectedPolicy ? <PolicyDetails policy={selectedPolicy} /> : null}
      </div>

      <div className="grid grid-cols-[1fr_auto] gap-3">
        <div className="grid gap-2">
          <Label htmlFor="fps">FPS</Label>
          <Select id="fps" value={String(fps)} onChange={(e) => setFps(Number.parseInt(e.target.value, 10))}>
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
            checked={dryRun}
            onChange={(e) => setDryRun(e.target.checked)}
            className="h-4 w-4"
          />
          Dry run
        </label>
      </div>

      <div className="grid gap-2">
        <Label htmlFor="task">Task description</Label>
        <textarea
          id="task"
          value={taskDescription}
          onChange={(e) => setTaskDescription(e.target.value)}
          rows={2}
          maxLength={500}
          placeholder="pick up the red cube"
          className="flex min-h-[60px] w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        />
      </div>

      <fieldset className="grid gap-3 rounded-md border border-input p-3" disabled={running}>
        <legend className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Safety
        </legend>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={deadmanRequired}
            onChange={(e) => setDeadmanRequired(e.target.checked)}
            className="h-4 w-4"
          />
          <span>
            Require deadman
            <span className="ml-1 text-xs text-muted-foreground">
              (hold <kbd className="rounded border px-1 text-[10px]">SPACE</kbd> to send actions)
            </span>
          </span>
        </label>
        <div className="grid gap-1.5">
          <Label htmlFor="max-action">Max action magnitude</Label>
          <Input
            id="max-action"
            inputMode="decimal"
            value={maxActionMagnitudeText}
            onChange={(e) => setMaxActionMagnitudeText(e.target.value)}
            placeholder="leave empty for no session-level cap"
          />
          <p className="text-[11px] text-muted-foreground">
            Each element of the policy action vector is clamped to ±value before send_action. Adapter-level safety still applies on top.
          </p>
        </div>
      </fieldset>

      {submitError ? (
        <p role="alert" className="text-sm text-destructive">
          {submitError}
        </p>
      ) : null}

      <div className="flex items-center gap-2">
        <Button
          type="button"
          disabled={!canStart}
          onClick={() => startMutation.mutate()}
          data-testid="inference-start"
        >
          {startMutation.isPending ? (
            <>
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
              Starting…
            </>
          ) : (
            <>
              <Play className="h-4 w-4" aria-hidden />
              Start
            </>
          )}
        </Button>
        <Button
          type="button"
          variant="destructive"
          disabled={!activeSession || !running || stopMutation.isPending}
          onClick={() => stopMutation.mutate()}
        >
          <Square className="h-3.5 w-3.5" aria-hidden />
          Stop
        </Button>
        {dryRun ? (
          <span className="text-xs text-amber-600 dark:text-amber-400" data-testid="dry-run-badge">
            dry-run · actions are not sent to the robot
          </span>
        ) : null}
      </div>
    </section>
  );
}

function PolicyDetails({ policy }: { policy: PolicyDescriptor }): JSX.Element {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 rounded-md border border-dashed border-input p-3 text-xs">
      <dt className="text-muted-foreground">type</dt>
      <dd className="font-mono">{policy.policy_type}</dd>
      <dt className="text-muted-foreground">params</dt>
      <dd className="font-mono">
        {policy.num_parameters !== null ? formatParams(policy.num_parameters) : "—"}
      </dd>
      <dt className="text-muted-foreground">device</dt>
      <dd className="font-mono">{policy.device ?? "auto"}</dd>
      <dt className="text-muted-foreground">language</dt>
      <dd>{policy.supports_language ? "supported" : "ignored"}</dd>
    </dl>
  );
}

interface SessionPanelProps {
  activeSession: InferenceSession | null;
  tailSteps: Array<{ step: number; action: number[]; latency_ms: number; text?: string | null }>;
  subscription: ReturnType<typeof useInferenceSession>;
  commandDraft: string;
  setCommandDraft: (v: string) => void;
  commandMutation: UseMutationResult<void, unknown, void, unknown>;
  deadmanHeld: boolean;
  deadmanActive: boolean;
  deadmanPending: boolean;
  supportsLanguage: boolean;
  running: boolean;
}

function SessionPanel({
  activeSession,
  tailSteps,
  subscription,
  commandDraft,
  setCommandDraft,
  commandMutation,
  deadmanHeld,
  deadmanActive,
  deadmanPending,
  supportsLanguage,
  running,
}: SessionPanelProps): JSX.Element {
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [tailSteps.length]);

  if (!activeSession) {
    return (
      <section className="flex items-center justify-center rounded-md border border-dashed border-input p-8 text-sm text-muted-foreground">
        Pick a policy and press <span className="mx-1 font-medium">Start</span> to begin streaming.
      </section>
    );
  }

  const lastLatency = tailSteps[tailSteps.length - 1]?.latency_ms ?? activeSession.last_latency_ms;
  const phase = subscription.errorMessage
    ? "error"
    : subscription.terminal
      ? "stopped"
      : subscription.connected
        ? "live"
        : "connecting";

  return (
    <section className="flex flex-col gap-3 rounded-md border bg-card p-4" data-testid="inference-session">
      <header className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">Live session</h2>
          <p className="font-mono text-xs text-muted-foreground">
            {activeSession.repo_id} · {activeSession.fps} fps · session {activeSession.id.slice(0, 8)}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {deadmanActive ? (
            <span
              data-testid="inference-deadman"
              className={`inline-flex items-center gap-1.5 rounded px-2 py-0.5 text-[11px] font-medium ${
                deadmanHeld
                  ? "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400"
                  : "bg-destructive/15 text-destructive"
              }`}
              title={deadmanHeld ? "Deadman held — actions sending" : "Hold SPACE to send actions"}
            >
              <span
                className={`h-2 w-2 rounded-full ${
                  deadmanHeld ? "animate-pulse bg-emerald-500" : "bg-destructive"
                }`}
                aria-hidden
              />
              {deadmanHeld ? "deadman: held" : "deadman: release"}
              {deadmanPending ? " …" : ""}
            </span>
          ) : null}
          <span
            className={`text-[11px] font-medium ${
              phase === "live"
                ? "text-emerald-600 dark:text-emerald-400"
                : phase === "error"
                  ? "text-destructive"
                  : "text-muted-foreground"
            }`}
            data-testid="inference-phase"
          >
            {phase}
            {phase === "connecting" && subscription.reconnectAttempt > 0
              ? ` · attempt ${subscription.reconnectAttempt}`
              : ""}
          </span>
        </div>
      </header>

      <dl className="grid grid-cols-3 gap-2 text-[11px] font-mono">
        <div>
          <dt className="uppercase tracking-wide text-muted-foreground">step</dt>
          <dd className="text-base font-semibold">
            {tailSteps[tailSteps.length - 1]?.step ?? activeSession.step}
          </dd>
        </div>
        <div>
          <dt className="uppercase tracking-wide text-muted-foreground">latency</dt>
          <dd className="text-base font-semibold">
            {lastLatency !== null && lastLatency !== undefined ? `${lastLatency.toFixed(1)}ms` : "—"}
          </dd>
        </div>
        <div>
          <dt className="uppercase tracking-wide text-muted-foreground">events</dt>
          <dd className="text-base font-semibold">{subscription.steps.length}</dd>
        </div>
      </dl>

      {supportsLanguage ? (
        <div className="grid gap-2 rounded-md border border-input p-3">
          <Label htmlFor="command">Live instruction</Label>
          <div className="flex gap-2">
            <Input
              id="command"
              value={commandDraft}
              onChange={(e) => setCommandDraft(e.target.value)}
              maxLength={500}
            />
            <Button
              type="button"
              size="sm"
              disabled={!running || commandMutation.isPending}
              onClick={() => commandMutation.mutate()}
            >
              Send
            </Button>
          </div>
        </div>
      ) : null}

      <div
        ref={scrollRef}
        className="max-h-72 overflow-y-auto rounded-md border border-input bg-muted/30 font-mono text-[11px]"
        data-testid="inference-timeline"
      >
        {tailSteps.length === 0 ? (
          <p className="p-3 text-muted-foreground">Waiting for first step…</p>
        ) : (
          <ul className="divide-y divide-border">
            {tailSteps.map((event, index) => (
              <li key={`${event.step}-${index}`} className="flex items-baseline gap-3 px-3 py-1">
                <span className="w-16 shrink-0 text-muted-foreground">#{event.step}</span>
                <span className="w-24 shrink-0 text-muted-foreground">
                  {event.latency_ms.toFixed(1)}ms
                </span>
                <span className="truncate">[{event.action.map((a) => a.toFixed(3)).join(", ")}]</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      {subscription.errorMessage ? (
        <p role="alert" className="text-sm text-destructive">
          {subscription.errorMessage}
        </p>
      ) : null}
    </section>
  );
}

function formatParams(n: number): string {
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(1)} B`;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)} M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)} K`;
  return String(n);
}
