/// <reference types="vite/client" />

// CSS side-effect imports (Tailwind entry). Satisfies noUncheckedSideEffectImports.
declare module "*.css";

// Injected by vite.config.ts `define` — short commit hash or build timestamp.
// Used to key/bust the persisted query cache so a deploy never serves stale data.
declare const __BUILD_VERSION__: string;
