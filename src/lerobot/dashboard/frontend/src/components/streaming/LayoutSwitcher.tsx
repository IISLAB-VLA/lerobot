import { cn } from "@/lib/utils";
import { LAYOUT_DEFINITIONS, type StreamLayoutKind } from "@/components/streaming/layouts";

interface LayoutSwitcherProps {
  current: StreamLayoutKind;
  tileCount: number;
  onChange: (kind: StreamLayoutKind) => void;
}

export function LayoutSwitcher({
  current,
  tileCount,
  onChange,
}: LayoutSwitcherProps): JSX.Element {
  return (
    <div
      role="toolbar"
      aria-label="Stream layout"
      className="inline-flex items-center gap-1 rounded-md border bg-background p-1"
    >
      {LAYOUT_DEFINITIONS.map(({ kind, label, Icon, minTiles }) => {
        const disabled = tileCount < minTiles;
        const selected = kind === current;
        return (
          <button
            key={kind}
            type="button"
            aria-pressed={selected}
            aria-label={label}
            title={label}
            disabled={disabled}
            onClick={() => onChange(kind)}
            className={cn(
              "inline-flex h-8 w-8 items-center justify-center rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              "disabled:cursor-not-allowed disabled:opacity-30",
              selected ? "bg-primary text-primary-foreground" : "hover:bg-accent",
            )}
          >
            <Icon className="h-4 w-4" aria-hidden />
          </button>
        );
      })}
    </div>
  );
}
