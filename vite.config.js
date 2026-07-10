import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [tailwindcss()],
  build: {
    outDir: "static/dist",
    emptyOutDir: true,
    manifest: true,
    rollupOptions: {
      input: {
        main: "static/src/main.js",
      },
      output: {
        // Stable filenames so Django templates can reference them directly
        // (e.g. {% static 'assets/main.css' %}) without reading the Vite
        // manifest. Revisit with cache-busting once there's a real deploy
        // pipeline (Task 24) to serve from.
        entryFileNames: "assets/[name].js",
        assetFileNames: "assets/[name][extname]",
      },
    },
  },
});
