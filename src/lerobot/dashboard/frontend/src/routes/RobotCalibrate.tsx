import { useEffect } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ArrowLeft, CheckCircle2, Loader2, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { listRobots, type RobotEntry } from "@/lib/api/robots";
import { useCalibrationSession } from "@/hooks/useCalibrationSession";

export function RobotCalibratePage(): JSX.Element {
  const { id = "" } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const robotsQuery = useQuery<RobotEntry[]>({
    queryKey: ["robots"],
    queryFn: listRobots,
  });
  const robot = robotsQuery.data?.find((r) => r.id === id);

  const session = useCalibrationSession({ robotId: id });

  useEffect(() => {
    if (session.phase === "done" && session.done?.result === "ok") {
      const timer = setTimeout(() => navigate(`/robots/${id}`), 2_000);
      return () => clearTimeout(timer);
    }
    return undefined;
  }, [session.phase, session.done, navigate, id]);

  if (!id) return <ErrorBlock message="Missing robot id in URL." />;
  if (robotsQuery.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading robot…</p>;
  }
  if (!robot) return <ErrorBlock message="Robot not found." backLink />;

  const progressPct =
    session.currentStep && session.totalSteps
      ? Math.min(100, Math.round(session.currentStep.progress * 100))
      : null;

  return (
    <div className="flex h-full flex-col gap-6">
      <header className="flex items-center gap-3">
        <Button asChild variant="ghost" size="icon" aria-label="Back to robot">
          <Link to={`/robots/${robot.id}`}>
            <ArrowLeft className="h-4 w-4" aria-hidden />
          </Link>
        </Button>
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Calibrate {robot.name}</h1>
          <p className="text-xs text-muted-foreground">
            {robot.robot_type} · session {session.sessionId ?? "—"}
          </p>
        </div>
      </header>

      {session.phase === "idle" ? (
        <IdleView onStart={session.start} robotName={robot.name} />
      ) : session.phase === "error" ? (
        <ErrorView
          error={session.error}
          onRetry={session.start}
          onBack={() => navigate(`/robots/${robot.id}`)}
        />
      ) : session.phase === "done" ? (
        <DoneView done={session.done} />
      ) : (
        <ActiveView
          phase={session.phase}
          step={session.currentStep}
          progressPct={progressPct}
          stepIndex={guessStepIndex(session.stepIds, session.currentStep?.step_id)}
          totalSteps={session.totalSteps}
          jointValues={session.jointValues}
          reconnectAttempt={session.reconnectAttempt}
          onAck={session.ack}
          onCancel={session.cancel}
        />
      )}
    </div>
  );
}

function guessStepIndex(ids: string[], current: string | undefined): number | null {
  if (!current) return null;
  const i = ids.indexOf(current);
  return i >= 0 ? i + 1 : null;
}

function IdleView({
  onStart,
  robotName,
}: {
  onStart: () => void;
  robotName: string;
}): JSX.Element {
  return (
    <div className="flex flex-col items-start gap-4 rounded-md border bg-card p-6">
      <h2 className="text-lg font-semibold">Ready to calibrate {robotName}?</h2>
      <p className="max-w-prose text-sm text-muted-foreground">
        This wizard walks through each calibration step. The robot must be connected and idle.
        You can cancel at any time; the current calibration file is preserved until you complete
        a new one successfully.
      </p>
      <Button onClick={onStart}>Start calibration</Button>
    </div>
  );
}

function ActiveView({
  phase,
  step,
  progressPct,
  stepIndex,
  totalSteps,
  jointValues,
  reconnectAttempt,
  onAck,
  onCancel,
}: {
  phase: string;
  step: ReturnType<typeof useCalibrationSession>["currentStep"];
  progressPct: number | null;
  stepIndex: number | null;
  totalSteps: number | null;
  jointValues: Record<string, number>;
  reconnectAttempt: number;
  onAck: () => Promise<void>;
  onCancel: () => Promise<void>;
}): JSX.Element {
  const awaitingInput = phase === "awaiting_user_input";
  const reconnecting = phase === "reconnecting";

  return (
    <div className="grid gap-4 md:grid-cols-[2fr_1fr]">
      <div className="flex flex-col gap-4 rounded-md border bg-card p-6">
        <div className="flex items-center gap-3 text-xs uppercase tracking-wide text-muted-foreground">
          <span>
            Step {stepIndex ?? "?"}
            {totalSteps ? ` / ${totalSteps}` : ""}
          </span>
          {progressPct !== null ? <span>· {progressPct}%</span> : null}
          {reconnecting ? (
            <span className="flex items-center gap-1 text-amber-600 dark:text-amber-400">
              <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
              Reconnecting (attempt {reconnectAttempt})…
            </span>
          ) : null}
        </div>

        {step?.image_ref ? (
          <img
            src={step.image_ref}
            alt={`${step.step_id} illustration`}
            className="max-h-72 w-full rounded-md object-contain"
            loading="eager"
          />
        ) : null}

        <h2 className="text-lg font-semibold">{step?.prompt ?? "Waiting for server…"}</h2>
        {step?.instruction ? (
          <p className="whitespace-pre-line text-sm text-foreground/80">{step.instruction}</p>
        ) : (
          <p className="text-sm text-muted-foreground">
            {phase === "starting"
              ? "Asking server to start a calibration session…"
              : phase === "connecting"
                ? "Opening the event stream…"
                : "Server has not sent a step yet."}
          </p>
        )}

        {progressPct !== null ? (
          <div className="h-2 w-full overflow-hidden rounded bg-muted">
            <div
              className="h-full bg-primary transition-[width]"
              style={{ width: `${progressPct}%` }}
              role="progressbar"
              aria-valuenow={progressPct}
              aria-valuemin={0}
              aria-valuemax={100}
            />
          </div>
        ) : null}

        <div className="mt-2 flex items-center gap-2">
          <Button
            onClick={() => void onAck()}
            disabled={!awaitingInput}
            title={awaitingInput ? "Advance to the next step" : "Server is still working"}
          >
            Next
          </Button>
          <Button variant="outline" onClick={() => void onCancel()}>
            Cancel
          </Button>
        </div>
      </div>

      <JointFeedbackPanel values={jointValues} />
    </div>
  );
}

function JointFeedbackPanel({
  values,
}: {
  values: Record<string, number>;
}): JSX.Element {
  const entries = Object.entries(values).sort(([a], [b]) => a.localeCompare(b));
  return (
    <div className="flex flex-col gap-2 rounded-md border bg-card p-4">
      <h3 className="text-sm font-semibold">Joint feedback</h3>
      {entries.length === 0 ? (
        <p className="text-xs text-muted-foreground">No telemetry yet.</p>
      ) : (
        <ul className="grid gap-1 text-xs font-mono">
          {entries.map(([name, value]) => (
            <li key={name} className="flex items-center justify-between gap-2">
              <span className="truncate text-muted-foreground">{name}</span>
              <span>{Number.isFinite(value) ? value.toFixed(3) : "—"}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function DoneView({
  done,
}: {
  done: ReturnType<typeof useCalibrationSession>["done"];
}): JSX.Element {
  const ok = done?.result === "ok";
  return (
    <div
      className={`flex flex-col items-start gap-3 rounded-md border p-6 ${
        ok ? "border-emerald-500/40 bg-emerald-500/5" : "border-amber-500/40 bg-amber-500/5"
      }`}
    >
      <div className="flex items-center gap-2 text-lg font-semibold">
        {ok ? (
          <CheckCircle2 className="h-5 w-5 text-emerald-500" aria-hidden />
        ) : (
          <AlertTriangle className="h-5 w-5 text-amber-500" aria-hidden />
        )}
        {ok ? "Calibration complete" : `Calibration ${done?.result ?? "ended"}`}
      </div>
      {done?.calibration_path ? (
        <p className="text-xs text-muted-foreground">
          Saved to <span className="font-mono">{done.calibration_path}</span>
        </p>
      ) : null}
      {done?.error ? (
        <p className="text-sm text-destructive">
          {done.error.code}: {done.error.message}
        </p>
      ) : null}
      {ok ? (
        <p className="text-sm text-muted-foreground">Returning to robot view…</p>
      ) : null}
    </div>
  );
}

function ErrorView({
  error,
  onRetry,
  onBack,
}: {
  error: string | null;
  onRetry: () => void;
  onBack: () => void;
}): JSX.Element {
  return (
    <div className="flex flex-col items-start gap-3 rounded-md border border-destructive/50 bg-destructive/5 p-6">
      <div className="flex items-center gap-2 text-lg font-semibold text-destructive">
        <XCircle className="h-5 w-5" aria-hidden />
        Calibration failed
      </div>
      {error ? <p className="text-sm text-destructive">{error}</p> : null}
      <div className="flex gap-2">
        <Button onClick={onRetry}>Try again</Button>
        <Button variant="outline" onClick={onBack}>
          Back to robot
        </Button>
      </div>
    </div>
  );
}

function ErrorBlock({
  message,
  backLink = false,
}: {
  message: string;
  backLink?: boolean;
}): JSX.Element {
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">{message}</p>
      {backLink ? (
        <Button asChild variant="outline">
          <Link to="/robots">
            <ArrowLeft className="h-4 w-4" aria-hidden />
            Back to robots
          </Link>
        </Button>
      ) : null}
    </div>
  );
}
