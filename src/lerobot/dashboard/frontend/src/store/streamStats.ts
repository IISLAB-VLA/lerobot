import { create } from "zustand";

interface StreamStatsState {
  showOverlay: boolean;
  toggle: () => void;
  setShowOverlay: (value: boolean) => void;
}

const STORAGE_KEY = "lerobot-stats-overlay";

function readInitial(): boolean {
  if (typeof window === "undefined") return false;
  return window.localStorage.getItem(STORAGE_KEY) === "1";
}

function persist(value: boolean): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(STORAGE_KEY, value ? "1" : "0");
}

export const useStreamStatsStore = create<StreamStatsState>((set, get) => ({
  showOverlay: readInitial(),
  toggle: () => {
    const next = !get().showOverlay;
    persist(next);
    set({ showOverlay: next });
  },
  setShowOverlay: (value) => {
    persist(value);
    set({ showOverlay: value });
  },
}));
