import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    // 浏览器里直接 `vite dev` 联调本地 web.py（默认 127.0.0.1:8788）。
    proxy: {
      "/api": "http://127.0.0.1:8788",
    },
  },
});

