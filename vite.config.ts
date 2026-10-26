import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

export default defineConfig({
  root: "frontend",
  base: "/static/ui/",
  plugins: [react()],
  build: {
    outDir: "../beets_mvp/static/ui",
    emptyOutDir: true,
    rollupOptions: {
      input: fileURLToPath(new URL("./frontend/src/main.tsx", import.meta.url)),
      output: {
        entryFileNames: "cratekeep.js",
        chunkFileNames: "chunks/[name]-[hash].js",
        assetFileNames: asset => asset.name?.endsWith(".css") ? "cratekeep.css" : "assets/[name]-[hash][extname]",
      },
    },
  },
  test: { environment: "jsdom", setupFiles: ["./src/test-setup.ts"] },
});
