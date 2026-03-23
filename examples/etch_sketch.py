#!/usr/bin/env python3
import json
import os
import time
from datetime import datetime

from PIL import Image, ImageColor, ImageDraw
from HyperCoreSDK.client import HyperClient

CANVAS_W = 1200
CANVAS_H = 760
DEFAULT_BRUSH = "#ff3b30"
DEFAULT_BG = "#ffffff"
DEFAULT_SIZE = 8
EXPORT_DIR = os.path.join(os.path.dirname(__file__), "exports")

hc = HyperClient(relay="http://localhost:8765", root="demo_etch_a_sketch")
hc.start_relay()
hc.clear()

APP_HTML = f"""
<div style="width:100%;height:100%;display:flex;flex-direction:column;background:#0f172a;color:#e2e8f0;font-family:Arial,sans-serif">
  <div style="padding:14px 16px;border-bottom:1px solid #1e293b;display:flex;flex-wrap:wrap;gap:12px;align-items:center;background:#111827">
    <div style="font-size:18px;font-weight:700;margin-right:10px">Hyper Etch</div>

    <label style="display:flex;align-items:center;gap:8px;background:#1f2937;padding:8px 10px;border-radius:10px;border:1px solid #334155">
      <span style="font-size:12px;color:#94a3b8">Brush</span>
      <input id="brush_color" type="color" value="{DEFAULT_BRUSH}" style="width:38px;height:28px;border:none;background:transparent;padding:0;cursor:pointer">
      <span data-bind-text="brush_hex" style="font-size:12px;min-width:72px"></span>
      <span data-bind-style="background:brush_swatch" style="width:14px;height:14px;border-radius:999px;border:1px solid rgba(255,255,255,0.25)"></span>
    </label>

    <label style="display:flex;align-items:center;gap:8px;background:#1f2937;padding:8px 10px;border-radius:10px;border:1px solid #334155">
      <span style="font-size:12px;color:#94a3b8">Thickness</span>
      <input id="brush_size" type="range" min="1" max="40" step="1" value="{DEFAULT_SIZE}" style="cursor:pointer">
      <span data-bind-text="brush_size_label" style="font-size:12px;min-width:42px"></span>
    </label>

    <label style="display:flex;align-items:center;gap:8px;background:#1f2937;padding:8px 10px;border-radius:10px;border:1px solid #334155">
      <span style="font-size:12px;color:#94a3b8">Background</span>
      <input id="bg_color" type="color" value="{DEFAULT_BG}" style="width:38px;height:28px;border:none;background:transparent;padding:0;cursor:pointer">
      <span data-bind-text="bg_hex" style="font-size:12px;min-width:72px"></span>
      <span data-bind-style="background:bg_swatch" style="width:14px;height:14px;border-radius:4px;border:1px solid rgba(255,255,255,0.25)"></span>
    </label>

    <button id="undo_btn" style="padding:10px 14px;background:#334155;color:#fff;border:none;border-radius:10px;cursor:pointer;font-weight:700">Undo</button>
    <button id="clear_btn" style="padding:10px 14px;background:#7c2d12;color:#fff;border:none;border-radius:10px;cursor:pointer;font-weight:700">Clear</button>
    <button id="save_btn" style="padding:10px 14px;background:#2563eb;color:#fff;border:none;border-radius:10px;cursor:pointer;font-weight:700">Save PNG</button>

    <div style="margin-left:auto;display:flex;gap:14px;align-items:center;flex-wrap:wrap">
      <div style="font-size:12px;color:#94a3b8">Strokes <span data-bind-text="stroke_count" style="color:#e2e8f0"></span></div>
      <div data-bind-text="status_text" style="font-size:12px;color:#cbd5e1;max-width:520px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"></div>
    </div>
  </div>

  <div style="flex:1;min-height:0;padding:18px;display:flex;align-items:center;justify-content:center;background:#020617">
    <div id="draw_area" style="position:relative;width:min(100%, {CANVAS_W}px);aspect-ratio:{CANVAS_W} / {CANVAS_H};border:1px solid #334155;border-radius:16px;overflow:hidden;box-shadow:0 24px 50px rgba(0,0,0,0.45);touch-action:none;background:#fff;cursor:crosshair">
      <div data-bind-html="canvas_html" style="position:absolute;inset:0;width:100%;height:100%"></div>
    </div>
  </div>
</div>
"""

APP_JS = r"""
(function(){
  const area = document.getElementById("draw_area");
  const brush = document.getElementById("brush_color");
  const size = document.getElementById("brush_size");
  const bg = document.getElementById("bg_color");
  const undo = document.getElementById("undo_btn");
  const clear = document.getElementById("clear_btn");
  const save = document.getElementById("save_btn");

  if (!area || !brush || !size || !bg || !undo || !clear || !save || area.dataset.on) return;
  area.dataset.on = "1";

  window.sendAction = (payload) => {
    const path = "inbox/" + Date.now() + "_" + Math.random().toString(36).slice(2, 7);
    window.$scene.get(path).put({
      data: JSON.stringify({ ...payload, timestamp: Date.now() })
    });
  };

  const sendValue = (type, value) => {
    window.sendAction({ type, value: String(value) });
  };

  brush.oninput = () => sendValue("set_brush_color", brush.value);
  size.oninput = () => sendValue("set_brush_size", size.value);
  bg.oninput = () => sendValue("set_bg_color", bg.value);
  undo.onclick = () => window.sendAction({ type: "undo" });
  clear.onclick = () => window.sendAction({ type: "clear" });
  save.onclick = () => window.sendAction({ type: "save" });

  let drawing = false;
  let lastSent = 0;
  let lastX = null;
  let lastY = null;

  function clamp01(v){
    return Math.max(0, Math.min(1, v));
  }

  function pointFromEvent(e){
    const r = area.getBoundingClientRect();
    const x = clamp01((e.clientX - r.left) / r.width);
    const y = clamp01((e.clientY - r.top) / r.height);
    return { x, y };
  }

  area.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    drawing = true;
    lastSent = 0;
    const p = pointFromEvent(e);
    lastX = p.x;
    lastY = p.y;
    if (area.setPointerCapture) area.setPointerCapture(e.pointerId);
    window.sendAction({ type: "pointer_down", x: p.x.toFixed(5), y: p.y.toFixed(5) });
  });

  area.addEventListener("pointermove", (e) => {
    if (!drawing) return;
    const now = Date.now();
    const p = pointFromEvent(e);
    const dx = lastX === null ? 1 : Math.abs(p.x - lastX);
    const dy = lastY === null ? 1 : Math.abs(p.y - lastY);

    if (now - lastSent < 12 && (dx + dy) < 0.0025) return;

    lastSent = now;
    lastX = p.x;
    lastY = p.y;
    window.sendAction({ type: "pointer_move", x: p.x.toFixed(5), y: p.y.toFixed(5) });
  });

  function finishStroke(e){
    if (!drawing) return;
    drawing = false;
    const p = pointFromEvent(e);
    lastX = null;
    lastY = null;
    window.sendAction({ type: "pointer_up", x: p.x.toFixed(5), y: p.y.toFixed(5) });
  }

  area.addEventListener("pointerup", finishStroke);
  area.addEventListener("pointercancel", finishStroke);
})();
"""


def escape_xml(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


brush_color = DEFAULT_BRUSH
bg_color = DEFAULT_BG
brush_size = DEFAULT_SIZE
strokes = []
current_stroke = None
status_text = "Draw with the mouse or a pen. Save exports a PNG on the server."


hc.mount("root/app", html=APP_HTML, js=APP_JS, fixed=True, layer=10)


def to_abs_point(msg: dict) -> tuple[float, float]:
    try:
        nx = float(msg.get("x", 0.0))
        ny = float(msg.get("y", 0.0))
    except Exception:
        nx, ny = 0.0, 0.0

    nx = max(0.0, min(1.0, nx))
    ny = max(0.0, min(1.0, ny))
    return nx * CANVAS_W, ny * CANVAS_H


def render_svg() -> str:
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CANVAS_W} {CANVAS_H}" width="100%" height="100%" style="display:block;pointer-events:none">',
        f'<rect x="0" y="0" width="{CANVAS_W}" height="{CANVAS_H}" fill="{escape_xml(bg_color)}" />'
    ]

    for stroke in strokes:
        pts = stroke.get("points", [])
        if not pts:
            continue

        color = escape_xml(stroke.get("color", DEFAULT_BRUSH))
        width = max(1, int(round(float(stroke.get("size", DEFAULT_SIZE)))))

        if len(pts) == 1:
            x, y = pts[0]
            r = width / 2
            parts.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{r:.2f}" fill="{color}" />')
        else:
            point_text = " ".join(f"{x:.2f},{y:.2f}" for x, y in pts)
            parts.append(
                f'<polyline points="{point_text}" fill="none" stroke="{color}" '
                f'stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round" />'
            )

    parts.append("</svg>")
    return "".join(parts)


def push_ui():
    hc.write(
        "root/app",
        canvas_html=render_svg(),
        brush_hex=brush_color.upper(),
        bg_hex=bg_color.upper(),
        brush_size_label=f"{brush_size}px",
        brush_swatch=brush_color,
        bg_swatch=bg_color,
        stroke_count=str(len(strokes)),
        status_text=status_text,
    )


def add_point_to_current(x: float, y: float):
    global current_stroke
    if current_stroke is None:
        return

    pts = current_stroke["points"]
    if not pts:
        pts.append((x, y))
        return

    lx, ly = pts[-1]
    if abs(lx - x) + abs(ly - y) >= 0.8:
        pts.append((x, y))


def save_png() -> str:
    os.makedirs(EXPORT_DIR, exist_ok=True)
    filename = datetime.now().strftime("etch_%Y%m%d_%H%M%S.png")
    out_path = os.path.join(EXPORT_DIR, filename)

    bg_rgb = ImageColor.getrgb(bg_color)
    image = Image.new("RGBA", (CANVAS_W, CANVAS_H), bg_rgb + (255,))
    draw = ImageDraw.Draw(image)

    for stroke in strokes:
        pts = stroke.get("points", [])
        if not pts:
            continue

        color_rgba = ImageColor.getrgb(stroke.get("color", DEFAULT_BRUSH)) + (255,)
        width = max(1, int(round(float(stroke.get("size", DEFAULT_SIZE)))))

        if len(pts) == 1:
            x, y = pts[0]
            r = width / 2
            draw.ellipse((x - r, y - r, x + r, y + r), fill=color_rgba)
        else:
            draw.line(pts, fill=color_rgba, width=width)
            r = width / 2
            x0, y0 = pts[0]
            x1, y1 = pts[-1]
            draw.ellipse((x0 - r, y0 - r, x0 + r, y0 + r), fill=color_rgba)
            draw.ellipse((x1 - r, y1 - r, x1 + r, y1 + r), fill=color_rgba)

    image.save(out_path, "PNG")
    return out_path


push_ui()

while True:
    snap = hc.snapshot() or {}
    dirty = False

    inbox_keys = sorted(k for k in snap.keys() if k.startswith("inbox/"))

    for key in inbox_keys:
        value = snap.get(key) or {}

        try:
            raw = value.get("data", {})
            msg = raw if isinstance(raw, dict) else json.loads(raw)
        except Exception:
            hc.remove(key)
            continue

        action = msg.get("type")

        try:
            if action == "set_brush_color":
                new_color = str(msg.get("value", DEFAULT_BRUSH)).strip() or DEFAULT_BRUSH
                if new_color != brush_color:
                    brush_color = new_color
                    status_text = f"Brush color set to {brush_color.upper()}"
                    dirty = True

            elif action == "set_bg_color":
                new_bg = str(msg.get("value", DEFAULT_BG)).strip() or DEFAULT_BG
                if new_bg != bg_color:
                    bg_color = new_bg
                    status_text = f"Background set to {bg_color.upper()}"
                    dirty = True

            elif action == "set_brush_size":
                try:
                    new_size = int(float(msg.get("value", DEFAULT_SIZE)))
                except Exception:
                    new_size = DEFAULT_SIZE
                new_size = max(1, min(40, new_size))
                if new_size != brush_size:
                    brush_size = new_size
                    status_text = f"Brush size set to {brush_size}px"
                    dirty = True

            elif action == "pointer_down":
                x, y = to_abs_point(msg)
                current_stroke = {
                    "color": brush_color,
                    "size": brush_size,
                    "points": [(x, y)],
                }
                strokes.append(current_stroke)
                status_text = "Drawing…"
                dirty = True

            elif action == "pointer_move":
                if current_stroke is not None:
                    x, y = to_abs_point(msg)
                    add_point_to_current(x, y)
                    dirty = True

            elif action == "pointer_up":
                if current_stroke is not None:
                    x, y = to_abs_point(msg)
                    add_point_to_current(x, y)
                    current_stroke = None
                    status_text = f"Stroke added. Total strokes: {len(strokes)}"
                    dirty = True

            elif action == "undo":
                current_stroke = None
                if strokes:
                    strokes.pop()
                    status_text = f"Undid last stroke. Remaining: {len(strokes)}"
                    dirty = True
                else:
                    status_text = "Nothing to undo."
                    dirty = True

            elif action == "clear":
                current_stroke = None
                if strokes:
                    strokes.clear()
                    status_text = "Canvas cleared."
                    dirty = True
                else:
                    status_text = "Canvas is already empty."
                    dirty = True

            elif action == "save":
                current_stroke = None
                out_path = save_png()
                status_text = f"Saved PNG to {out_path}"
                dirty = True

        except Exception as e:
            status_text = f"Action error: {e}"
            dirty = True

        hc.remove(key)

    if dirty:
        push_ui()

    time.sleep(0.05)
