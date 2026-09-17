import re
from pathlib import Path

from fontTools.ttLib import TTFont

BASE_DIR = Path(__file__).resolve().parents[2]
FONT = BASE_DIR / "static/src/fonts/material-symbols-outlined-subset.woff2"

ICON_SPAN = re.compile(
    r'class="[^"]*material-symbols-outlined[^"]*"[^>]*>\s*([a-z0-9_]+)\s*<', re.S
)


def _ligature_names(font):
    """Every icon name the font's ligature table turns into a glyph."""
    char_for_glyph = {glyph: chr(code) for code, glyph in font.getBestCmap().items()}
    names = set()
    for lookup in font["GSUB"].table.LookupList.Lookup:
        for subtable in lookup.SubTable:
            subtable = getattr(subtable, "ExtSubTable", subtable)
            for first, ligatures in getattr(subtable, "ligatures", {}).items():
                for ligature in ligatures:
                    chars = [
                        char_for_glyph.get(g) for g in [first, *ligature.Component]
                    ]
                    if all(chars):
                        names.add("".join(chars))
    return names


def _icon_names_used_in_code():
    used = set()
    for template in (BASE_DIR / "templates").glob("**/*.html"):
        text = template.read_text()
        used |= set(ICON_SPAN.findall(text))
        used |= set(re.findall(r'\bicon="([a-z0-9_]+)"', text))
    for module in ["apps/notifications/models.py", "apps/admin_portal/views.py"]:
        text = (BASE_DIR / module).read_text()
        for block in re.findall(
            # \s* before the closing brace (Task 63c): the dicts inside model
            # classes are indented, and the old `\n\}` only matched a brace
            # at column 0 -- so a new, missing bell icon passed unnoticed.
            r"_(?:ICON_BY_EVENT_TYPE|GROUP_ICONS)\s*=\s*\{(.*?)\n\s*\}",
            text,
            re.S,
        ):
            # \(? (Task 63c): event-type icons are tuples, `: ("name", classes)`,
            # sometimes wrapped onto the next line. Without it none of them
            # were ever checked.
            used |= set(re.findall(r':\s*\(?\s*"([a-z0-9_]+)"', block))
    return used


def test_icon_font_can_render_filled_icons():
    """Task 58: Task 50's self-hosted subset was fetched as a static font, so
    every `font-variation-settings: 'FILL' 1` (rating stars, the saved-to-
    wishlist heart, the 2FA shield/lock icons) silently rendered as an
    outline. The pre-Task-50 CDN link loaded `wght,FILL@100..700,0..1`."""
    font = TTFont(FONT)

    assert "fvar" in font, "icon font is static -- it has no variation axes"
    fill = {axis.axisTag: axis for axis in font["fvar"].axes}.get("FILL")
    assert fill is not None
    assert (fill.minValue, fill.maxValue) == (0, 1)


def test_icon_font_covers_every_icon_the_code_renders():
    """Guards any future re-subset of the font: a missing name renders as its
    raw ligature text (e.g. "shopping_cart") instead of a glyph."""
    missing = _icon_names_used_in_code() - _ligature_names(TTFont(FONT))

    assert (
        not missing
    ), f"icons used in code but absent from the font: {sorted(missing)}"
