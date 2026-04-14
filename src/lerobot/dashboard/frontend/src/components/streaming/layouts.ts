import { Columns2, Focus, LayoutGrid, Rows2, Square } from "lucide-react";

export type StreamLayoutKind =
  | "single"
  | "horizontal-split"
  | "vertical-split"
  | "grid"
  | "spotlight";

export interface StreamLayoutDefinition {
  kind: StreamLayoutKind;
  label: string;
  Icon: typeof Square;
  minTiles: number;
}

export const LAYOUT_DEFINITIONS: ReadonlyArray<StreamLayoutDefinition> = [
  { kind: "single", label: "Single", Icon: Square, minTiles: 1 },
  { kind: "horizontal-split", label: "Horizontal split", Icon: Rows2, minTiles: 2 },
  { kind: "vertical-split", label: "Vertical split", Icon: Columns2, minTiles: 2 },
  { kind: "grid", label: "Grid", Icon: LayoutGrid, minTiles: 2 },
  { kind: "spotlight", label: "Spotlight", Icon: Focus, minTiles: 2 },
];
