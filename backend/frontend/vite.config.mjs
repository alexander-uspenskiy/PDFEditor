import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The backend serves the built editor at /app/ (see backend/main.py).
export default defineConfig({
  base: "/app/",
  plugins: [react()],
  build: {
    outDir: "../static/app",
    emptyOutDir: true,
  },
});
