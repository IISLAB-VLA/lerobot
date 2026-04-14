import { useEffect, useRef } from "react";
import { AlertTriangle, Circle, Keyboard, Power } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { useTeleopSession } from "@/hooks/useTeleopSession";
import type { DeadmanState } from "@/lib/teleop/protocol";

interface TeleopPanelProps {
  robotId: string;
  enabled: boolean;
}

const TRACKED_KEYS = new Set([
  "w",
  "a",
  "s",
  "d",
  "q",
  "e",
  "r",
  "f",
  "[",
  "]",
]);
const KEY_HINTS: Array<{ keys: string; label: string }> = [
  { keys: "WASD", label: "EE X/Y" },
  { keys: "Q / E", label: "EE Z" },
  { keys: "R / F", label: "Yaw" },
  { keys: "[ / ]", label: "Gripper" },
  { keys: "SPACE", label: "Deadman (hold)" },
];

export function TeleopPanel({ robotId, enabled }: TeleopPanelProps): JSX.Element {
  const session = useTeleopSession({ robotId, enabled });
  const panelRef = useRef<HTMLDivElement | null>(null);
  const pressedKeysRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (!enabled) {
      pressedKeysRef.current.clear();
      return undefined;
    }

    const sendKey = (key: string, pressed: boolean) => {
      session.sendInput({ kind: "keyboard", key, pressed });
    };

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      const target = event.target;
      if (target instanceof HTMLElement) {
        const tag = target.tagName;
        if (
          tag === "INPUT" ||
          tag === "TEXTAREA" ||
          tag === "SELECT" ||
          target.isContentEditable
        ) {
          return;
        }
      }
      const key = event.key.toLowerCase();
      if (event.code === "Space") {
        if (!session.deadmanHeld) {
          event.preventDefault();
          session.setDeadmanHeld(true);
        }
        return;
      }
      if (!TRACKED_KEYS.has(key)) return;
      if (event.repeat) return;
      if (pressedKeysRef.current.has(key)) return;
      pressedKeysRef.current.add(key);
      event.preventDefault();
      sendKey(key, true);
    };

    const onKeyUp = (event: KeyboardEvent) => {
      const key = event.key.toLowerCase();
      if (event.code === "Space") {
        if (session.deadmanHeld) {
          event.preventDefault();
          session.setDeadmanHeld(false);
        }
        return;
      }
      if (!TRACKED_KEYS.has(key)) return;
      if (!pressedKeysRef.current.has(key)) return;
      pressedKeysRef.current.delete(key);
      event.preventDefault();
      sendKey(key, false);
    };

    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    const pressedKeys = pressedKeysRef.current;
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      // Release any keys still held so the server sees a clean state on unmount.
      for (const key of pressedKeys) {
        session.sendInput({ kind: "keyboard", key, pressed: false });
      }
      pressedKeys.clear();
      if (session.deadmanHeld) session.setDeadmanHeld(false);
    };
  }, [enabled, session]);

  return (
    <section
      ref={panelRef}
      aria-label="Teleop"
      className="flex flex-col gap-3 rounded-md border bg-card p-4"
      data-testid="teleop-panel"
    >
      <header className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Keyboard className="h-4 w-4 text-muted-foreground" aria-hidden />
          <h3 className="text-sm font-semibold">Teleop</h3>
          <DeadmanBadge state={session.deadman} />
        </div>
        <ConnectionStatus
          phase={session.phase}
          reconnectAttempt={session.reconnectAttempt}
          rttMs={session.rttMs}
        />
      </header>

      <DeadmanToggle
        held={session.deadmanHeld}
        onToggle={() => session.setDeadmanHeld(!session.deadmanHeld)}
        disabled={session.phase !== "connected" && session.phase !== "reconnecting"}
      />

      <KeyHints />

      <TelemetryRow telemetry={session.telemetry} />

      {session.lastError ? (
        <p
          role="alert"
          className="flex items-center gap-1.5 text-xs text-destructive"
          data-testid="teleop-error"
        >
          <AlertTriangle className="h-3.5 w-3.5" aria-hidden />
          {session.lastError}
        </p>
      ) : null}
    </section>
  );
}

function DeadmanBadge({ state }: { state: DeadmanState }): JSX.Element {
  const tone =
    state === "engaged"
      ? "text-emerald-600 dark:text-emerald-400"
      : state === "lockout"
        ? "text-destructive"
        : state === "arming"
          ? "text-amber-600 dark:text-amber-400"
          : "text-muted-foreground";
  return (
    <span className={cn("inline-flex items-center gap-1 text-xs font-medium", tone)}>
      <Circle className="h-2.5 w-2.5 fill-current" aria-hidden />
      {state}
    </span>
  );
}

function DeadmanToggle({
  held,
  onToggle,
  disabled,
}: {
  held: boolean;
  onToggle: () => void;
  disabled: boolean;
}): JSX.Element {
  return (
    <Button
      type="button"
      onClick={onToggle}
      variant={held ? "default" : "outline"}
      size="sm"
      disabled={disabled}
      className="self-start"
      aria-pressed={held}
      data-testid="teleop-deadman-toggle"
    >
      <Power className="h-4 w-4" aria-hidden />
      {held ? "Deadman held" : "Hold to enable (SPACE)"}
    </Button>
  );
}

function KeyHints(): JSX.Element {
  return (
    <ul className="grid grid-cols-2 gap-1 text-xs text-muted-foreground">
      {KEY_HINTS.map((hint) => (
        <li key={hint.keys} className="flex items-center gap-2">
          <kbd className="rounded border bg-muted px-1.5 py-0.5 font-mono text-[10px]">
            {hint.keys}
          </kbd>
          <span>{hint.label}</span>
        </li>
      ))}
    </ul>
  );
}

function TelemetryRow({
  telemetry,
}: {
  telemetry: ReturnType<typeof useTeleopSession>["telemetry"];
}): JSX.Element | null {
  if (!telemetry) return null;
  return (
    <dl
      className="grid grid-cols-3 gap-2 text-[11px] font-mono text-muted-foreground"
      data-testid="teleop-telemetry"
    >
      <div>
        <dt className="uppercase tracking-wide">forwarded</dt>
        <dd className="font-semibold text-foreground">{telemetry.forwarded}</dd>
      </div>
      <div>
        <dt className="uppercase tracking-wide">dropped</dt>
        <dd className="font-semibold text-foreground">
          {telemetry.dropped_deadman + telemetry.dropped_validation}
        </dd>
      </div>
      <div>
        <dt className="uppercase tracking-wide">aux events</dt>
        <dd className="font-semibold text-foreground">{telemetry.aux_events_forwarded}</dd>
      </div>
    </dl>
  );
}

function ConnectionStatus({
  phase,
  reconnectAttempt,
  rttMs,
}: {
  phase: ReturnType<typeof useTeleopSession>["phase"];
  reconnectAttempt: number;
  rttMs: number | null;
}): JSX.Element {
  if (phase === "connected") {
    return (
      <span className="text-[11px] text-muted-foreground" data-testid="teleop-phase">
        live{rttMs !== null ? ` · ${rttMs.toFixed(0)}ms RTT` : ""}
      </span>
    );
  }
  if (phase === "reconnecting") {
    return (
      <span className="text-[11px] text-amber-600 dark:text-amber-400" data-testid="teleop-phase">
        reconnecting · attempt {reconnectAttempt}
      </span>
    );
  }
  if (phase === "conflict") {
    return (
      <span className="text-[11px] text-destructive" data-testid="teleop-phase">
        conflict
      </span>
    );
  }
  if (phase === "offline") {
    return (
      <span className="text-[11px] text-destructive" data-testid="teleop-phase">
        robot offline
      </span>
    );
  }
  if (phase === "error") {
    return (
      <span className="text-[11px] text-destructive" data-testid="teleop-phase">
        error
      </span>
    );
  }
  return (
    <span className="text-[11px] text-muted-foreground" data-testid="teleop-phase">
      {phase}
    </span>
  );
}
