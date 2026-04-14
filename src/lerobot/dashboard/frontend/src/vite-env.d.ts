/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_ENABLE_CALIBRATION?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
