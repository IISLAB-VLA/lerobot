import { useEffect, type RefObject } from "react";
import { LAYOUT_DEFINITIONS, type StreamLayoutKind } from "@/components/streaming/layouts";

interface UseStreamStageShortcutsArgs {
  stageRef: RefObject<HTMLElement>;
  tileCount: number;
  onLayoutChange: (kind: StreamLayoutKind) => void;
  onToggleStats?: () => void;
  enabled: boolean;
}

function isTextInputTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  if (target.isContentEditable) return true;
  return false;
}

async function toggleFullscreen(el: HTMLElement | null): Promise<void> {
  if (!el) return;
  try {
    if (document.fullscreenElement) {
      await document.exitFullscreen();
    } else {
      await el.requestFullscreen();
    }
  } catch {
    // Some browsers reject fullscreen if not triggered from a user gesture;
    // the keydown itself is a gesture, so failures are typically policy-related.
  }
}

export function useStreamStageShortcuts({
  stageRef,
  tileCount,
  onLayoutChange,
  onToggleStats,
  enabled,
}: UseStreamStageShortcutsArgs): void {
  useEffect(() => {
    if (!enabled) return undefined;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (isTextInputTarget(event.target)) return;

      if (event.key === "f" || event.key === "F") {
        event.preventDefault();
        void toggleFullscreen(stageRef.current);
        return;
      }

      if ((event.key === "i" || event.key === "I") && onToggleStats) {
        event.preventDefault();
        onToggleStats();
        return;
      }

      if (event.key === "Escape") {
        if (document.fullscreenElement) {
          event.preventDefault();
          void document.exitFullscreen().catch(() => undefined);
        }
        return;
      }

      // 1..9 -> layout switch (clamped to available layouts)
      if (/^[1-9]$/.test(event.key)) {
        const idx = Number.parseInt(event.key, 10) - 1;
        const def = LAYOUT_DEFINITIONS[idx];
        if (!def) return;
        if (tileCount < def.minTiles) return;
        event.preventDefault();
        onLayoutChange(def.kind);
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [stageRef, tileCount, onLayoutChange, onToggleStats, enabled]);
}
