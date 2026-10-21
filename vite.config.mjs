import { readFileSync } from "node:fs";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built app includes MIT and Apache-2.0 code, so each build ships LICENSE (with the
// third-party notices) next to the app.
const shipLicense = {
  name: "ship-license",
  generateBundle() {
    this.emitFile({
      type: "asset",
      fileName: "LICENSE.txt",
      source: readFileSync(new URL("./LICENSE", import.meta.url), "utf8"),
    });
  },
};

// GitHub Pages serves the project at /PDFEditor/ in CI; local dev uses /.
export default defineConfig({
  base: process.env.GITHUB_ACTIONS ? "/PDFEditor/" : "/",
  plugins: [react(), shipLicense],
});
