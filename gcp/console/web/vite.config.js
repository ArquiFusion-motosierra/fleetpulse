import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// En desarrollo, /api va al BFF local: MOCK=1 uvicorn app.main:app --port 8080
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://localhost:8080" } },
  build: { outDir: "dist", chunkSizeWarningLimit: 1200 },
});
