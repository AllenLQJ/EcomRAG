/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string;
  readonly VITE_PROXY_TARGET?: string;
  readonly VITE_HUMAN_SUPPORT_URL?: string;
  readonly VITE_SHOW_SAMPLE_BADGE?: string;
  readonly VITE_STORE_NAME?: string;
  readonly VITE_MOCK?: string;
  readonly VITE_SHOW_TRACE_TOGGLE?: string;
}
