import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const apiTarget = process.env.BRIGADE_WEB_PROXY || "http://127.0.0.1:58080";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true
  },
  server: {
    proxy: {
      "/api": apiTarget,
      "/healthz": apiTarget
    }
  }
});
