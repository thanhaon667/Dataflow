"""
Typography tokens for every Python-rendered surface: one place to change the project's font.

The project uses MONTSERRAT for everything people read on a SCREEN (headings, body, labels, KPI
numbers, buttons, chart text). A monospace stack is kept only where literal code must line up: the
Script Center code editor, file paths, log / terminal snippets and JSON blocks.

No surface renders an image any more: the one that did (erp/task_timeline_chart.py -> task_timeline.png,
matplotlib) was dropped on 2026-09-23 with the rest of the non-ERP-Desk report surfaces. Its matplotlib
font helper (apply_matplotlib_style(), lesson L-122) went with it on 2026-09-24 once it had sat with zero
callers through a full cycle - see PROJECT_NOTES.md for the history if a future matplotlib output needs
the same "Montserrat when installed, else Segoe UI, else the default, logged once" recipe again.

Surfaces and where their copy of the font lives (change the font in these places, nothing else):
  - ERP Desk (desktop/static/*.css, *.js) ....... CSS variables --font / --code in desktop/static/shell.css
                                                  (the Google Fonts <link> in desktop/static/shell.html and the
                                                  weight list the font gate loads in desktop/static/shell.js)
  - dashboard/script_center.py, the Streamlit
    --theme flags, the e-mail HTML ...... this module
  - run_script_center.bat ............................ carries a literal copy of the --theme.* font flags
                                                       (a .bat cannot import Python); tests/smoke.py checks it

Standard library only, safe to import from anywhere (no side effects at import time).
Nothing to run; use it as:  from erp import typography
"""
from __future__ import annotations

FONT_NAME = "Montserrat"
# Only weights a CSS rule really paints: every extra weight in this list is a font file downloaded for nothing AND
# a slower first chart draw, because the ERP Desk chart gate waits for Promise.all of every weight (lesson L-113).
# tests/smoke.py check 6 fails if a weight here is painted nowhere, or if shell.js loads a different list.
FONT_WEIGHTS = "400;500;600;700"              # 400 body · 500 labels · 600 headings · 700 big numbers

GOOGLE_FONTS_CSS_URL = f"https://fonts.googleapis.com/css2?family={FONT_NAME}:wght@{FONT_WEIGHTS}&display=swap"

# Fallbacks stay reasonable when Google Fonts is unreachable (offline, firewall): Segoe UI ships with Windows.
FONT_STACK = f"'{FONT_NAME}', 'Segoe UI', system-ui, -apple-system, sans-serif"
# Only for literal code: editor text, file paths, logs, JSON.
CODE_STACK = "Consolas, 'Cascadia Mono', 'Courier New', monospace"
# E-mail clients cannot load web fonts: Montserrat is used when installed, otherwise the next one is.
EMAIL_STACK = f"{FONT_NAME}, 'Segoe UI', Arial, sans-serif"


def css_tokens() -> str:
    """CSS custom properties for a page's :root block (--font, --code)."""
    return f"--font: {FONT_STACK}; --code: {CODE_STACK};"


def streamlit_font_css(include_import: bool = True, code_widget_keys: tuple[str, ...] = ()) -> str:
    """CSS rules (no <style> tag) that put a Streamlit page in Montserrat, code in the monospace stack.

    Belt and braces next to streamlit_theme_args(): the page is Montserrat even when it is started with a plain
    `streamlit run` (no --theme flags). Streamlit styles most text with its own emotion classes (a "Source Sans" family
    on every p / label / tab), so the rule must be !important; wrapped in :where() it has zero specificity, which lets
    any more specific !important rule (the code font below, a page's own code classes) still win. Icon glyph fonts
    (Material Symbols), svg and code stay untouched. An @import is only valid before any other rule, so a caller that
    already loads the font at the top of its own <style> block passes include_import=False.

    `code_widget_keys` is the OPT-IN for monospace text areas: pass the `key=` of each st.text_area that holds literal
    code (the Script Center editor is `sc_editor::<path>`, so the key prefix "sc_editor" is enough - Streamlit puts a
    `st-key-<key>` class on the widget's wrapper). A blanket `textarea { var(--code) }` rule used to put EVERY
    free-text box in the project - including ones nobody has written yet - in Consolas (lesson L-115).
    """
    editor_rules = "".join(
        f'div[class*="st-key-{key}"] textarea {{ font-family: var(--code) !important; }}\n'
        for key in code_widget_keys
    )
    return (
        (f"@import url('{GOOGLE_FONTS_CSS_URL}');\n" if include_import else "")
        + f":root {{ {css_tokens()} }}\n"
        # Figures: tabular everywhere they line up or move (tables, and any KPI value that is re-rendered or counts
        # up - Montserrat's proportional "1" is half the width of its "4", so a changing number resizes its tile,
        # lesson L-112). Prose keeps the default proportional figures.
        ".stApp table, .stApp th, .stApp td, .stApp .tnum { font-variant-numeric: tabular-nums; }\n"
        # textarea is NOT excluded here: a free-text box is prose and belongs in the text font. The code editor opts
        # back out below through code_widget_keys, which beats this zero-specificity :where() rule.
        ":where(.stApp, .stApp *:not([data-testid=\"stIconMaterial\"], .material-symbols-outlined, svg, svg *, code, pre,\n"
        "  [data-testid=\"stCode\"] *)) { font-family: var(--font) !important; }\n"
        '[data-testid="stCode"] pre, [data-testid="stCode"] code,\n'
        '[data-testid="stCode"] span { font-family: var(--code) !important; }\n'
        + editor_rules
    )


def streamlit_theme_args() -> list[str]:
    """`streamlit run` command-line flags that give Streamlit's own widgets the same fonts.

    Passed on the command line, not stored in .streamlit/config.toml, so each app keeps control of its look.

    A `--theme.font` / `--theme.headingFont` value is `"<family>:<source url>"` and Streamlit splits it on the FIRST
    colon, so EVERYTHING after that colon is the URL. A `,sans-serif` fallback appended here therefore ends up inside
    the URL (`...&display=swap,sans-serif`): Google Fonts still answers 200 but without `font-display: swap`, which
    means an invisible-text flash of up to ~3 s on every load. Fallback stacks belong in CSS only (FONT_STACK, used by
    streamlit_font_css) - never in a --theme flag. Lesson L-114; tests/smoke.py check 6 parses these values with
    Streamlit's own `theme_util._parse_font_config` instead of comparing two copies of the string.
    """
    face = f"'{FONT_NAME}':{GOOGLE_FONTS_CSS_URL}"
    return [
        "--theme.font", face,
        "--theme.headingFont", face,
        "--theme.codeFont", CODE_STACK,
    ]
