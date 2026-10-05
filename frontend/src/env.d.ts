/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_MODE?: "demo" | "api";
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_TRUSTED_ORG_ID?: string;
  readonly VITE_TRUSTED_ACTOR_ID?: string;
  readonly VITE_TRUSTED_ROLE?: "sales" | "reviewer" | "admin";
  readonly VITE_QUOTATION_ID?: string;
  readonly VITE_QUOTE_REVISION_ID?: string;
  readonly VITE_QUOTE_CONTENT_HASH?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
