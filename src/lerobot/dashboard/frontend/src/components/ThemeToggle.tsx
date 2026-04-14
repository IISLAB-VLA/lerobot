import { Monitor, Moon, Sun } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useThemeStore, type ThemeMode } from "@/store/theme";

const ORDER: ThemeMode[] = ["system", "light", "dark"];

const ICONS: Record<ThemeMode, typeof Sun> = {
  system: Monitor,
  light: Sun,
  dark: Moon,
};

export function ThemeToggle(): JSX.Element {
  const mode = useThemeStore((s) => s.mode);
  const setMode = useThemeStore((s) => s.setMode);
  const Icon = ICONS[mode];

  const next = () => {
    const idx = ORDER.indexOf(mode);
    const nextMode = ORDER[(idx + 1) % ORDER.length];
    if (nextMode) setMode(nextMode);
  };

  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={next}
      aria-label={`Theme: ${mode}. Click to cycle.`}
    >
      <Icon className="h-5 w-5" />
    </Button>
  );
}
