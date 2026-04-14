/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_ENABLE_CALIBRATION?: string;
  readonly VITE_ENABLE_TELEOP?: string;
  readonly VITE_ENABLE_RECORDING?: string;
  readonly VITE_ENABLE_INFERENCE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
