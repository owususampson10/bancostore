import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [tailwindcss()],
  // Static files are served under /static/ (bancostore/settings.py's
  // STATIC_URL), not site root. vite_tags.py's vite_asset/vite_css tags
  // already account for this themselves (they resolve the manifest's
  // relative path through Django's own static() helper), but this `base`
  // is still required for URLs Vite rewrites directly inside emitted CSS
  // (e.g. @font-face src: url(...) referencing another built asset) --
  // those never go through vite_tags.py, so without this they resolve to
  // site root and 404. Found via the Task 38 icon-webfont self-hosting
  // fix (2026-08-17): the subsetted font's url() came out as
  // "/assets/...woff2" instead of "/static/assets/...woff2".
  base: "/static/",
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
