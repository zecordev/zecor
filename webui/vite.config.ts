import { defineConfig } from "vite";

// The Python `zecor serve` serves `webui/dist/` as static files, so the build must be
// self-contained with relative asset paths.
export default defineConfig({
  base: "./",
  build: { outDir: "dist", emptyOutDir: true },
  test: { environment: "jsdom", globals: true },
});
