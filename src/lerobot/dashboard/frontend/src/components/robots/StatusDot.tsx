import { cn } from "@/lib/utils";

export type StatusKind = "online" | "offline" | "registering";

interface StatusDotProps {
  kind: StatusKind;
  label?: string;
  className?: string;
}

const dotClasses: Record<StatusKind, string> = {
  online: "bg-emerald-500 shadow-[0_0_0_4px_rgba(16,185,129,0.18)] animate-pulse",
  offline: "bg-red-500",
  registering: "bg-zinc-400",
};

const textClasses: Record<StatusKind, string> = {
  online: "text-emerald-600 dark:text-emerald-400",
  offline: "text-red-600 dark:text-red-400",
  registering: "text-muted-foreground",
};

const defaultLabels: Record<StatusKind, string> = {
  online: "online",
  offline: "offline",
  registering: "registering",
};

export function StatusDot({ kind, label, className }: StatusDotProps): JSX.Element {
  const text = label ?? defaultLabels[kind];
  return (
    <span
      className={cn("inline-flex items-center gap-2 text-xs font-medium", className)}
      role="status"
      aria-live="polite"
    >
      <span
        className={cn("h-2.5 w-2.5 rounded-full transition-colors", dotClasses[kind])}
        aria-hidden
      />
      <span className={textClasses[kind]}>{text}</span>
    </span>
  );
}
