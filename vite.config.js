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
        // Task 36a: real content-hashed filenames, resolved at render time
        // via static/dist/.vite/manifest.json (apps/pages/templatetags/
        // vite_tags.py) -- the previous stable-filename setup had no
        // cache-busting at all, which was a real production bug, not
        // hypothetical: a live deploy's CSS change was invisible to real
        // browsers (stale cached main.css) even though the server was
        // serving the correct, freshly-built file the whole time.
        entryFileNames: "assets/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]",
      },
    },
  },
});
