import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// GitHub Pages serves the project at /PDFEditor/ in CI; local dev uses /.
export default defineConfig({
  base: process.env.GITHUB_ACTIONS ? "/PDFEditor/" : "/",
  plugins: [react()],
});
