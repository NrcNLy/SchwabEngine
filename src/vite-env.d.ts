/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_ENGINE_BASE_URL?: string;
  readonly VITE_SHOW_DEBUG?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
