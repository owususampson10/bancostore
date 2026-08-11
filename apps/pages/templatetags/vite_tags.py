"""Task 36a. Resolves a Vite build entry (e.g. "main.js") to its real,
content-hashed output filename via static/dist/.vite/manifest.json, instead
of the old fixed "assets/main.js"/"assets/main.css" paths that had no
cache-busting at all -- a real production bug, not hypothetical: a CSS
change deployed cleanly (confirmed via direct curl against the live
server, correct byte-for-byte) but was invisible in real browsers, which
kept serving a stale cached copy of the old fixed filename with nothing to
tell them a new version existed.

In DEBUG (local dev), the manifest is re-read on every call so the
existing "npm run build then hard-refresh" workflow (this file's own
project-wide documented gotcha) keeps working unchanged. In production
(DEBUG=False) it's cached in memory for the life of the process --safe
because a real deploy always restarts Daphne (see deploy/README.md), so a
stale in-memory manifest can never outlive an actual release.
"""

import json
from functools import lru_cache

from django import template
from django.conf import settings
from django.templatetags.static import static

register = template.Library()

_MANIFEST_PATH = settings.BASE_DIR / "static" / "dist" / ".vite" / "manifest.json"


def _read_manifest():
    try:
        with open(_MANIFEST_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        # Local dev before the first `npm run build` -- never 500 a page
        # over a missing build artifact.
        return {}


_read_manifest_cached = lru_cache(maxsize=1)(_read_manifest)


def _manifest_entry(entry_name):
    manifest = _read_manifest() if settings.DEBUG else _read_manifest_cached()
    return manifest.get(f"static/src/{entry_name}")


@register.simple_tag
def vite_asset(entry_name):
    """{% vite_asset "main.js" %} -> the real hashed static URL for that
    entry's own output file."""
    entry = _manifest_entry(entry_name)
    if not entry:
        return static(f"assets/{entry_name}")
    return static(entry["file"])


@register.simple_tag
def vite_css(entry_name):
    """{% vite_css "main.js" %} -> the real hashed static URL for that
    entry's associated CSS (Vite bundles main.js's imported main.css
    separately, listed under manifest[entry]["css"])."""
    entry = _manifest_entry(entry_name)
    if entry and entry.get("css"):
        return static(entry["css"][0])
    return static("assets/main.css")
