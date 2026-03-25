"""
HyperCore UI — tiny composable building blocks.

    from HyperCoreSDK.ui import page, row, col, btn, text, input, grid, card, bar

Everything returns an HTML string. Compose them:

    page(
        bar(text("title", "font-weight:700"), btn("Logout", "logout")),
        row(
            card("sidebar", col(btn("Home", "nav", page="home"))),
            card("main", scroll()),
        ),
    )
"""

import json as _json

# ── Theme defaults ─────────────────────────────────────────────────

BG       = "#111827"
BG_CARD  = "#1f2937"
BORDER   = "#374151"
TEXT_CLR  = "#e5e7eb"
ACCENT   = "#3b82f6"
FONT     = "Arial,sans-serif"


# ── Helpers ────────────────────────────────────────────────────────

def _action_attr(act, **data):
    """Build an onclick='action({...})' attribute string."""
    if act is None:
        return ""
    payload = {"_action": act, **data}
    return f" onclick='action({_json.dumps(payload)})'"


def _bind(bind_text=None, bind_html=None, bind_style=None):
    """Build data-bind-* attributes."""
    parts = []
    if bind_text: parts.append(f'data-bind-text="{bind_text}"')
    if bind_html: parts.append(f'data-bind-html="{bind_html}"')
    if bind_style: parts.append(f'data-bind-style="{bind_style}"')
    return " ".join(parts)


def _id_attr(id):
    return f' id="{id}"' if id else ""


# ── Layout ─────────────────────────────────────────────────────────

def page(*children, bg=None, font=None):
    """Full-screen flex column container."""
    return (
        f'<div style="width:100%;height:100%;display:flex;flex-direction:column;'
        f'background:{bg or BG};color:{TEXT_CLR};font-family:{font or FONT}">'
        f'{"".join(children)}</div>'
    )


def row(*children, gap="8px", style=""):
    """Horizontal flex row."""
    return (
        f'<div style="display:flex;gap:{gap};{style}">'
        f'{"".join(children)}</div>'
    )


def col(*children, gap="8px", style="", children_slot=False):
    """Vertical flex column. children_slot=True adds data-children for dynamic mounts."""
    dc = ' data-children' if children_slot else ''
    return (
        f'<div{dc} style="display:flex;flex-direction:column;gap:{gap};{style}">'
        f'{"".join(children)}</div>'
    )


def grid(cols, items, size="100px", gap="5px", style=""):
    """CSS grid with N columns."""
    return (
        f'<div style="display:grid;grid-template-columns:repeat({cols},{size});gap:{gap};{style}">'
        f'{"".join(items)}</div>'
    )


def scroll(*children, gap="8px", style=""):
    """Scrollable flex column with data-children for dynamic mounts."""
    return (
        f'<div data-children style="flex:1;min-height:0;overflow:auto;'
        f'display:flex;flex-direction:column;gap:{gap};padding:12px;{style}">'
        f'{"".join(children)}</div>'
    )


# ── Components ─────────────────────────────────────────────────────

def text(bind, style=""):
    """Bound text element."""
    return f'<div {_bind(bind_text=bind)} style="{style}"></div>'


def span(bind, style=""):
    """Inline bound text."""
    return f'<span {_bind(bind_text=bind)} style="{style}"></span>'


def html(bind, style=""):
    """Bound HTML element (renders raw HTML)."""
    return f'<div {_bind(bind_html=bind)} style="{style}"></div>'


def btn(label, act=None, id=None, style="", **data):
    """Button. Static label, optional action with data payload.

    Pass id= when this button is a trigger for actions_js().
    For static actions (inline onclick), id is optional.
    """
    bg = data.pop("bg", ACCENT)
    default = f"padding:10px 14px;background:{bg};color:#fff;border:none;border-radius:4px;cursor:pointer;font-weight:700"
    return f'<button{_id_attr(id)}{_action_attr(act, **data)} style="{default};{style}">{label}</button>'


def input(id, placeholder="", value="", style="", width=None):
    """Text input. Use with actions_js() to read its value."""
    w = f"width:{width};" if width else "flex:1;"
    default = f"{w}padding:10px;background:{BG_CARD};color:{TEXT_CLR};border:1px solid {BORDER};border-radius:8px;outline:none"
    return f'<input id="{id}" placeholder="{placeholder}" value="{value}" style="{default};{style}">'


def card(*children, style=""):
    """Bordered card container."""
    default = f"padding:10px;background:{BG_CARD};border:1px solid {BORDER};border-radius:10px"
    return f'<div style="{default};{style}">{"".join(children)}</div>'


def bar(*children, style=""):
    """Horizontal bar — for headers, footers, toolbars."""
    default = f"display:flex;align-items:center;justify-content:space-between;padding:12px 14px;border-bottom:1px solid {BORDER}"
    return f'<div style="{default};{style}">{"".join(children)}</div>'


def divider(style=""):
    """Horizontal line."""
    return f'<div style="border-top:1px solid {BORDER};{style}"></div>'


def cell(bind, act=None, style="", **data):
    """Grid cell — bound text + optional action. For game boards, data grids."""
    default = f"display:flex;align-items:center;justify-content:center;background:#222;color:#fff;border:none;cursor:pointer"
    return (
        f'<button {_bind(bind_text=bind)}{_action_attr(act, **data)} '
        f'style="{default};{style}"></button>'
    )