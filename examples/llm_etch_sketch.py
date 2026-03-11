#!/usr/bin/env python3
import base64
import io
import json
import os
import re
import time
import threading
import queue
from datetime import datetime
from html import escape

import requests
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat

from HyperCoreSDK import HyperClient

# ---------------------------------
# Config
# ---------------------------------
RELAY = "http://localhost:8765"
ROOT = "demo_llm_etch_battle"

MODEL_A = "qwen2.5:3b"
MODEL_B = "gemma3:4b"
OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"
REQUEST_TIMEOUT = 120
TURN_DELAY = 0.15
CANVAS = 256
DEFAULT_MAX_STEPS = 24
DEFAULT_BRUSH_MAX = 18
MAX_UPLOAD_BYTES = 3_000_000
MAX_LOGS = 120

# ---------------------------------
# HyperCore
# ---------------------------------
hc = HyperClient(relay=RELAY, root=ROOT)
hc.start_relay()
hc.clear()

# ---------------------------------
# Static parent UI
# ---------------------------------
APP_HTML = """
<div style="width:100%;height:100%;display:flex;flex-direction:column;background:#0f172a;color:#e2e8f0;font-family:Arial,sans-serif">
  <div style="padding:14px 16px;border-bottom:1px solid #334155;display:flex;justify-content:space-between;align-items:center;gap:12px;background:#111827">
    <div style="min-width:0">
      <div style="font-size:20px;font-weight:800">LLM Etch Battle</div>
      <div data-bind-text="status" style="font-size:12px;color:#94a3b8;margin-top:4px;min-height:16px"></div>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;font-size:12px;color:#94a3b8">
      <span data-bind-text="model_a_name"></span>
      <span>vs</span>
      <span data-bind-text="model_b_name"></span>
    </div>
  </div>

  <div style="padding:12px 16px;border-bottom:1px solid #1f2937;display:flex;flex-wrap:wrap;gap:10px;align-items:center;background:#0b1220">
    <label style="display:flex;align-items:center;gap:8px;padding:8px 10px;background:#111827;border:1px solid #334155;border-radius:10px;cursor:pointer">
      <span style="font-size:12px;font-weight:700">Upload Image</span>
      <input id="img_upload" type="file" accept="image/*" style="color:#94a3b8;font-size:12px">
    </label>

    <label style="display:flex;align-items:center;gap:8px;padding:8px 10px;background:#111827;border:1px solid #334155;border-radius:10px">
      <span style="font-size:12px;color:#94a3b8">Steps</span>
      <input id="step_budget" type="range" min="8" max="64" step="1" value="24">
      <span id="step_budget_val" data-bind-text="step_budget_label" style="font-size:12px;font-weight:700;min-width:24px;text-align:right">24</span>
    </label>

    <label style="display:flex;align-items:center;gap:8px;padding:8px 10px;background:#111827;border:1px solid #334155;border-radius:10px">
      <span style="font-size:12px;color:#94a3b8">Max Brush</span>
      <input id="brush_max" type="range" min="4" max="30" step="1" value="18">
      <span id="brush_max_val" data-bind-text="brush_max_label" style="font-size:12px;font-weight:700;min-width:24px;text-align:right">18</span>
    </label>

    <button id="start_btn" style="padding:10px 14px;background:#2563eb;color:#fff;border:none;border-radius:10px;cursor:pointer;font-weight:700">Start / Resume</button>
    <button id="pause_btn" style="padding:10px 14px;background:#334155;color:#e2e8f0;border:none;border-radius:10px;cursor:pointer;font-weight:700">Pause</button>
    <button id="reset_btn" style="padding:10px 14px;background:#475569;color:#fff;border:none;border-radius:10px;cursor:pointer;font-weight:700">Reset Duel</button>
    <button id="save_a_btn" style="padding:10px 14px;background:#0f766e;color:#fff;border:none;border-radius:10px;cursor:pointer;font-weight:700">Save A PNG</button>
    <button id="save_b_btn" style="padding:10px 14px;background:#7c3aed;color:#fff;border:none;border-radius:10px;cursor:pointer;font-weight:700">Save B PNG</button>
    <button id="save_both_btn" style="padding:10px 14px;background:#9333ea;color:#fff;border:none;border-radius:10px;cursor:pointer;font-weight:700">Save Both</button>
  </div>

  <div style="flex:1;min-height:0;display:grid;grid-template-columns:340px 1fr 1fr;gap:16px;padding:16px">
    <div style="min-width:0;background:#111827;border:1px solid #334155;border-radius:14px;display:flex;flex-direction:column;overflow:hidden">
      <div style="padding:12px 14px;border-bottom:1px solid #334155;background:#0b1220">
        <div style="font-size:12px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.12em">Target</div>
        <div data-bind-text="target_name" style="font-size:15px;font-weight:800;margin-top:4px;word-break:break-word"></div>
      </div>
      <div style="padding:14px;display:flex;flex-direction:column;gap:12px;min-height:0;flex:1">
        <div data-bind-html="target_html" style="height:280px;background:#020617;border:1px solid #334155;border-radius:12px;display:flex;align-items:center;justify-content:center;overflow:hidden"></div>
        <div style="font-size:12px;color:#94a3b8;line-height:1.5;white-space:pre-wrap;word-break:break-word" data-bind-text="target_summary"></div>
      </div>
    </div>

    <div style="min-width:0;background:#111827;border:1px solid #334155;border-radius:14px;display:flex;flex-direction:column;overflow:hidden">
      <div style="padding:12px 14px;border-bottom:1px solid #334155;background:#0b1220;display:flex;justify-content:space-between;gap:8px;align-items:center">
        <div>
          <div style="font-size:12px;color:#93c5fd;text-transform:uppercase;letter-spacing:0.12em">Model A</div>
          <div data-bind-text="model_a_name" style="font-size:15px;font-weight:800;margin-top:4px"></div>
        </div>
        <div style="text-align:right">
          <div style="font-size:11px;color:#94a3b8">Score</div>
          <div data-bind-text="score_a" style="font-size:22px;font-weight:900"></div>
        </div>
      </div>
      <div style="padding:14px;display:flex;flex-direction:column;gap:12px;min-height:0;flex:1">
        <div data-bind-html="svg_a" style="height:280px;background:#020617;border:1px solid #334155;border-radius:12px;display:flex;align-items:center;justify-content:center;overflow:hidden"></div>
        <div style="display:flex;justify-content:space-between;gap:10px;font-size:12px;color:#94a3b8">
          <div>Steps: <span data-bind-text="steps_a"></span>/<span data-bind-text="step_budget_label"></span></div>
          <div data-bind-text="last_a"></div>
        </div>
        <div data-bind-text="cursor_a_text" style="font-size:12px;color:#93c5fd"></div>
      </div>
    </div>

    <div style="min-width:0;background:#111827;border:1px solid #334155;border-radius:14px;display:flex;flex-direction:column;overflow:hidden">
      <div style="padding:12px 14px;border-bottom:1px solid #334155;background:#0b1220;display:flex;justify-content:space-between;gap:8px;align-items:center">
        <div>
          <div style="font-size:12px;color:#f0abfc;text-transform:uppercase;letter-spacing:0.12em">Model B</div>
          <div data-bind-text="model_b_name" style="font-size:15px;font-weight:800;margin-top:4px"></div>
        </div>
        <div style="text-align:right">
          <div style="font-size:11px;color:#94a3b8">Score</div>
          <div data-bind-text="score_b" style="font-size:22px;font-weight:900"></div>
        </div>
      </div>
      <div style="padding:14px;display:flex;flex-direction:column;gap:12px;min-height:0;flex:1">
        <div data-bind-html="svg_b" style="height:280px;background:#020617;border:1px solid #334155;border-radius:12px;display:flex;align-items:center;justify-content:center;overflow:hidden"></div>
        <div style="display:flex;justify-content:space-between;gap:10px;font-size:12px;color:#94a3b8">
          <div>Steps: <span data-bind-text="steps_b"></span>/<span data-bind-text="step_budget_label"></span></div>
          <div data-bind-text="last_b"></div>
        </div>
        <div data-bind-text="cursor_b_text" style="font-size:12px;color:#f0abfc"></div>
      </div>
    </div>
  </div>

  <div style="height:230px;min-height:230px;background:#111827;border-top:1px solid #334155;display:flex;flex-direction:column">
    <div style="padding:12px 14px;border-bottom:1px solid #334155;background:#0b1220;display:flex;justify-content:space-between;align-items:center">
      <div style="font-size:14px;font-weight:800">Move Log</div>
      <div style="font-size:12px;color:#94a3b8">Python owns image state, scoring, and exports</div>
    </div>
    <div data-children style="flex:1;min-height:0;overflow:auto;display:flex;flex-direction:column;gap:10px;padding:14px"></div>
  </div>
</div>
"""

APP_JS = r"""
(function(){
  const upload = document.getElementById("img_upload");
  const startBtn = document.getElementById("start_btn");
  const pauseBtn = document.getElementById("pause_btn");
  const resetBtn = document.getElementById("reset_btn");
  const saveABtn = document.getElementById("save_a_btn");
  const saveBBtn = document.getElementById("save_b_btn");
  const saveBothBtn = document.getElementById("save_both_btn");
  const stepBudget = document.getElementById("step_budget");
  const stepBudgetVal = document.getElementById("step_budget_val");
  const brushMax = document.getElementById("brush_max");
  const brushMaxVal = document.getElementById("brush_max_val");

  if (!upload || !startBtn || !pauseBtn || !resetBtn || !saveABtn || !saveBBtn || !saveBothBtn || !stepBudget || !brushMax || startBtn.dataset.on) return;
  startBtn.dataset.on = "1";

  const sendAction = (type, extra) => {
    const path = "inbox/" + Date.now() + "_" + Math.random().toString(36).slice(2,7);
    const payload = Object.assign({ type: type, ts: Date.now() }, extra || {});
    window.$scene.get(path).put({ data: JSON.stringify(payload) });
  };

  const pushConfig = () => {
    stepBudgetVal.textContent = stepBudget.value;
    brushMaxVal.textContent = brushMax.value;
    sendAction("config", {
      max_steps: Number(stepBudget.value),
      brush_max: Number(brushMax.value)
    });
  };

  stepBudget.addEventListener("change", pushConfig);
  brushMax.addEventListener("change", pushConfig);
  stepBudgetVal.textContent = stepBudget.value;
  brushMaxVal.textContent = brushMax.value;

  startBtn.onclick = () => sendAction("start");
  pauseBtn.onclick = () => sendAction("pause");
  resetBtn.onclick = () => sendAction("reset_duel");
  saveABtn.onclick = () => sendAction("save_a");
  saveBBtn.onclick = () => sendAction("save_b");
  saveBothBtn.onclick = () => sendAction("save_both");

  upload.addEventListener("change", () => {
    const file = upload.files && upload.files[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = () => {
      sendAction("upload_image", {
        name: file.name || "upload",
        mime: file.type || "application/octet-stream",
        size: file.size || 0,
        data_url: reader.result || ""
      });
      upload.value = "";
    };
    reader.readAsDataURL(file);
  });
})();
"""

LOG_HTML = """
<div data-bind-style="borderLeft:border_color"
     style="background:#0b1220;border-left:4px solid #334155;border-radius:10px;padding:12px 14px;display:flex;flex-direction:column;gap:6px">
  <div data-bind-text="meta" style="font-size:12px;color:#93c5fd;font-weight:700"></div>
  <div data-bind-text="text" style="font-size:14px;line-height:1.5;color:#e2e8f0;white-space:pre-wrap;word-break:break-word"></div>
</div>
"""

hc.mount("root/app", html=APP_HTML, js=APP_JS, fixed=True, layer=10)

# ---------------------------------
# State
# ---------------------------------
PALETTE_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

active = False
max_steps = DEFAULT_MAX_STEPS
brush_max = DEFAULT_BRUSH_MAX
duel_epoch = 0

worker_results = queue.SimpleQueue()
worker_running = {MODEL_A: False, MODEL_B: False}
worker_started_at = {MODEL_A: 0.0, MODEL_B: 0.0}
cursor_state = {
    MODEL_A: {"visible": True, "x": CANVAS // 2, "y": CANVAS // 2, "color": "#2563eb", "size": 10, "label": "idle"},
    MODEL_B: {"visible": True, "x": CANVAS // 2, "y": CANVAS // 2, "color": "#a855f7", "size": 10, "label": "idle"},
}

image_loaded = False
target_name = "No image yet"
target_image = None
target_preview_data_url = ""
target_summary = "Upload a picture, then click Start. Both local models will take turns proposing strokes."
target_palette = ["#ffffff", "#000000"]
target_grid = []
target_edge_grid = []
target_bg_color = "#ffffff"
target_focus_bbox = (0, 0, CANVAS - 1, CANVAS - 1)

strokes = {MODEL_A: [], MODEL_B: []}
last_action_text = {MODEL_A: "—", MODEL_B: "—"}
scores = {MODEL_A: 0.0, MODEL_B: 0.0}
last_score_delta = {MODEL_A: 0.0, MODEL_B: 0.0}
reject_memory = {MODEL_A: [], MODEL_B: []}

log_counter = 0
log_paths = []

# ---------------------------------
# Utility helpers
# ---------------------------------
def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def hex_to_rgb(hex_color):
    h = (hex_color or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6 or not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        return None
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb):
    r, g, b = rgb
    return f"#{int(r):02x}{int(g):02x}{int(b):02x}"


def safe_name(s):
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s or "untitled")
    return s[:80] or "untitled"


def image_to_data_url(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def placeholder_html(text):
    return (
        "<div style=\"width:100%;height:100%;display:flex;align-items:center;justify-content:center;"
        "font-size:13px;color:#94a3b8;text-align:center;padding:20px;box-sizing:border-box\">"
        f"{escape(text)}"
        "</div>"
    )


def image_html_from_data_url(data_url, alt="image"):
    if not data_url:
        return placeholder_html("No image loaded")
    return (
        f"<img src=\"{data_url}\" alt=\"{escape(alt)}\" "
        "style=\"width:100%;height:100%;object-fit:contain;display:block\">"
    )


def ensure_exports_dir():
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exports")
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def add_log(meta, text, border_color="#334155"):
    global log_counter, log_paths
    log_counter += 1
    path = f"root/app/log_{log_counter:04d}"
    log_paths.append(path)
    hc.mount(path, html=LOG_HTML, layer=5)
    hc.write(path, meta=meta, text=text, border_color=border_color)

    if len(log_paths) > MAX_LOGS:
        old = log_paths.pop(0)
        try:
            hc.remove(old)
        except Exception:
            pass


def clear_logs():
    global log_counter, log_paths
    for p in log_paths:
        try:
            hc.remove(p)
        except Exception:
            pass
    log_counter = 0
    log_paths = []


def render_svg(stroke_list, cursor=None):
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CANVAS} {CANVAS}" style="width:100%;height:100%;display:block;background:#ffffff">',
        f'<rect x="0" y="0" width="{CANVAS}" height="{CANVAS}" fill="#ffffff"/>',
    ]
    for s in stroke_list:
        color = escape(s.get("color", "#000000"))
        size = int(s.get("size", 4))
        tool = s.get("tool", "dot")
        if tool == "line":
            parts.append(
                f'<line x1="{int(s["x1"])}" y1="{int(s["y1"])}" x2="{int(s["x2"])}" y2="{int(s["y2"])}" '
                f'stroke="{color}" stroke-width="{size}" stroke-linecap="round" />'
            )
        else:
            parts.append(
                f'<circle cx="{int(s["x"])}" cy="{int(s["y"])}" r="{max(1, size // 2)}" fill="{color}" />'
            )
    if cursor and cursor.get("visible"):
        cx = int(clamp(cursor.get("x", CANVAS // 2), 0, CANVAS - 1))
        cy = int(clamp(cursor.get("y", CANVAS // 2), 0, CANVAS - 1))
        c = escape(cursor.get("color", "#2563eb"))
        r = int(clamp(cursor.get("size", 8), 4, 24))
        label = escape(cursor.get("label", "cursor"))
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{c}" stroke-width="2" opacity="0.95" />')
        parts.append(f'<line x1="{max(0, cx-r-6)}" y1="{cy}" x2="{min(CANVAS, cx+r+6)}" y2="{cy}" stroke="{c}" stroke-width="2" opacity="0.75" />')
        parts.append(f'<line x1="{cx}" y1="{max(0, cy-r-6)}" x2="{cx}" y2="{min(CANVAS, cy+r+6)}" stroke="{c}" stroke-width="2" opacity="0.75" />')
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="2" fill="{c}" />')
        parts.append(f'<rect x="{clamp(cx+8,0,CANVAS-64)}" y="{clamp(cy-18,0,CANVAS-20)}" width="56" height="16" rx="4" fill="{c}" opacity="0.88" />')
        parts.append(f'<text x="{clamp(cx+12,0,CANVAS-60)}" y="{clamp(cy-6,10,CANVAS-4)}" font-size="10" font-family="Arial,sans-serif" fill="#ffffff">{label}</text>')
    parts.append("</svg>")
    return "".join(parts)


def render_strokes_to_image(stroke_list):
    img = Image.new("RGB", (CANVAS, CANVAS), "white")
    draw = ImageDraw.Draw(img)
    for s in stroke_list:
        color = s.get("color", "#000000")
        size = int(s.get("size", 4))
        if s.get("tool") == "line":
            draw.line((s["x1"], s["y1"], s["x2"], s["y2"]), fill=color, width=size)
            r = max(1, size // 2)
            draw.ellipse((s["x1"] - r, s["y1"] - r, s["x1"] + r, s["y1"] + r), fill=color)
            draw.ellipse((s["x2"] - r, s["y2"] - r, s["x2"] + r, s["y2"] + r), fill=color)
        else:
            x, y = s["x"], s["y"]
            r = max(1, size // 2)
            draw.ellipse((x - r, y - r, x + r, y + r), fill=color)
    return img


def color_distance_sq(a, b):
    return sum((int(a[i]) - int(b[i])) ** 2 for i in range(3))


def likely_background_color(palette):
    if not palette:
        return "#ffffff"
    white = (255, 255, 255)
    best = palette[0]
    best_dist = None
    for hx in palette:
        rgb = hex_to_rgb(hx) or white
        dist = color_distance_sq(rgb, white)
        if best_dist is None or dist < best_dist:
            best = hx
            best_dist = dist
    return best


def subject_bbox(img, bg_hex):
    bg_rgb = hex_to_rgb(bg_hex) or (255, 255, 255)
    edge_img = img.convert("L").filter(ImageFilter.FIND_EDGES)
    xs = []
    ys = []
    for y in range(img.height):
        for x in range(img.width):
            rgb = img.getpixel((x, y))
            if color_distance_sq(rgb, bg_rgb) > (28 * 28 * 3) or edge_img.getpixel((x, y)) >= 24:
                xs.append(x)
                ys.append(y)
    if not xs:
        return (0, 0, img.width - 1, img.height - 1)
    return (min(xs), min(ys), max(xs), max(ys))


def compute_similarity(target, current):
    if target is None or current is None:
        return 0.0

    tgt_small = target.resize((64, 64), Image.Resampling.BILINEAR).convert("RGB")
    cur_small = current.resize((64, 64), Image.Resampling.BILINEAR).convert("RGB")

    global_color_diff = ImageChops.difference(tgt_small, cur_small)
    global_color_mean = sum(ImageStat.Stat(global_color_diff).mean) / 3.0
    global_color_score = 1.0 - (global_color_mean / 255.0)

    bg_rgb = hex_to_rgb(target_bg_color) or (255, 255, 255)
    tgt_edges = tgt_small.convert("L").filter(ImageFilter.FIND_EDGES)
    cur_edges = cur_small.convert("L").filter(ImageFilter.FIND_EDGES)

    subject_vals = []
    edge_vals = []
    target_subject = 0
    current_subject = 0

    for y in range(64):
        for x in range(64):
            t_rgb = tgt_small.getpixel((x, y))
            c_rgb = cur_small.getpixel((x, y))
            t_edge = tgt_edges.getpixel((x, y))
            c_edge = cur_edges.getpixel((x, y))

            is_subject = color_distance_sq(t_rgb, bg_rgb) > (24 * 24 * 3) or t_edge >= 26
            if is_subject:
                target_subject += 1
                diff = (abs(t_rgb[0] - c_rgb[0]) + abs(t_rgb[1] - c_rgb[1]) + abs(t_rgb[2] - c_rgb[2])) / 3.0
                subject_vals.append(diff)
                edge_vals.append(abs(t_edge - c_edge))

            if color_distance_sq(c_rgb, bg_rgb) > (24 * 24 * 3) or c_edge >= 26:
                current_subject += 1

    if subject_vals:
        subject_color_score = 1.0 - ((sum(subject_vals) / len(subject_vals)) / 255.0)
        subject_edge_score = 1.0 - ((sum(edge_vals) / len(edge_vals)) / 255.0)
    else:
        subject_color_score = global_color_score
        edge_diff = ImageChops.difference(tgt_edges, cur_edges)
        subject_edge_score = 1.0 - (ImageStat.Stat(edge_diff).mean[0] / 255.0)

    coverage_score = 1.0
    if target_subject > 0:
        coverage_score = 1.0 - min(1.0, abs(current_subject - target_subject) / float(target_subject))

    x1, y1, x2, y2 = subject_bbox(tgt_small, target_bg_color)
    crop_t = tgt_small.crop((x1, y1, x2 + 1, y2 + 1))
    crop_c = cur_small.crop((x1, y1, x2 + 1, y2 + 1))
    crop_diff = ImageChops.difference(crop_t, crop_c)
    crop_score = 1.0 - ((sum(ImageStat.Stat(crop_diff).mean) / 3.0) / 255.0)

    score = 100.0 * max(
        0.0,
        min(
            1.0,
            (subject_color_score * 0.40)
            + (subject_edge_score * 0.25)
            + (crop_score * 0.20)
            + (coverage_score * 0.10)
            + (global_color_score * 0.05),
        ),
    )
    return round(score, 2)


def update_scores():
    if target_image is None:
        scores[MODEL_A] = 0.0
        scores[MODEL_B] = 0.0
        return
    scores[MODEL_A] = compute_similarity(target_image, render_strokes_to_image(strokes[MODEL_A]))
    scores[MODEL_B] = compute_similarity(target_image, render_strokes_to_image(strokes[MODEL_B]))


def image_palette(img, max_colors=6):
    quant = img.convert("RGB").quantize(colors=max_colors, method=Image.Quantize.MEDIANCUT)
    pal = quant.getpalette() or []
    counts = quant.getcolors() or []
    items = []
    for count, idx in sorted(counts, reverse=True):
        base = idx * 3
        rgb = tuple(pal[base:base + 3])
        if len(rgb) == 3:
            items.append((count, rgb_to_hex(rgb)))
    seen = []
    for _, hx in items:
        if hx not in seen:
            seen.append(hx)
    return seen[:max_colors] or ["#ffffff", "#000000"]


def nearest_palette_color(rgb, palette):
    best = palette[0]
    best_dist = None
    for hx in palette:
        prgb = hex_to_rgb(hx) or (0, 0, 0)
        dist = sum((rgb[i] - prgb[i]) ** 2 for i in range(3))
        if best_dist is None or dist < best_dist:
            best = hx
            best_dist = dist
    return best


def palette_grid_lines(img, palette, size=16):
    tiny = img.convert("RGB").resize((size, size), Image.Resampling.BILINEAR)
    lines = []
    for y in range(size):
        row = []
        for x in range(size):
            rgb = tiny.getpixel((x, y))
            hx = nearest_palette_color(rgb, palette)
            idx = palette.index(hx)
            row.append(PALETTE_CHARS[idx])
        lines.append("".join(row))
    return lines


def edge_grid_lines(img, size=16, threshold=40):
    tiny = img.convert("L").filter(ImageFilter.FIND_EDGES).resize((size, size), Image.Resampling.BILINEAR)
    lines = []
    for y in range(size):
        row = []
        for x in range(size):
            row.append("#" if tiny.getpixel((x, y)) >= threshold else ".")
        lines.append("".join(row))
    return lines


def summarize_target(img, palette, grid_lines):
    x1, y1, x2, y2 = target_focus_bbox
    return (
        f"Canvas: {img.width}×{img.height}\n"
        f"Palette: {', '.join(palette)}\n"
        f"Background: {target_bg_color}\n"
        f"Focus box: {x1},{y1} → {x2},{y2}\n"
        f"Grid: {len(grid_lines)}×{len(grid_lines[0]) if grid_lines else 0} symbolic color cells\n"
        "Score favors subject color + edges, not empty white space."
    )


def preprocess_uploaded_image(data_url, filename):
    global image_loaded, target_image, target_name, target_preview_data_url
    global target_palette, target_grid, target_edge_grid, target_summary
    global target_bg_color, target_focus_bbox

    if not data_url.startswith("data:"):
        raise ValueError("expected data URL")

    try:
        _, encoded = data_url.split(",", 1)
    except ValueError:
        raise ValueError("malformed data URL")

    raw = base64.b64decode(encoded)
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    fitted = ImageOps.contain(img, (CANVAS, CANVAS), Image.Resampling.LANCZOS)
    canvas_img = Image.new("RGB", (CANVAS, CANVAS), "white")
    x = (CANVAS - fitted.width) // 2
    y = (CANVAS - fitted.height) // 2
    canvas_img.paste(fitted, (x, y))

    target_image = canvas_img
    target_name = filename or "upload"
    target_palette = image_palette(canvas_img, max_colors=6)
    target_bg_color = likely_background_color(target_palette)
    target_focus_bbox = subject_bbox(canvas_img, target_bg_color)
    target_grid = palette_grid_lines(canvas_img, target_palette, size=16)
    target_edge_grid = edge_grid_lines(canvas_img, size=16, threshold=38)
    target_summary = summarize_target(canvas_img, target_palette, target_grid)
    target_preview_data_url = image_to_data_url(canvas_img)
    image_loaded = True


def empty_stroke_state():
    return {MODEL_A: [], MODEL_B: []}


def reset_duel(keep_image=True):
    global active, strokes, last_action_text, last_score_delta, reject_memory, duel_epoch
    strokes = empty_stroke_state()
    last_action_text = {MODEL_A: "—", MODEL_B: "—"}
    last_score_delta = {MODEL_A: 0.0, MODEL_B: 0.0}
    reject_memory = {MODEL_A: [], MODEL_B: []}
    active = False
    duel_epoch += 1
    worker_running[MODEL_A] = False
    worker_running[MODEL_B] = False
    set_cursor_for_plan(MODEL_A, "idle")
    set_cursor_for_plan(MODEL_B, "idle")
    update_scores()
    if keep_image and image_loaded:
        add_log("system", "Duel reset. Click Start to let the models redraw the same target simultaneously.", "#38bdf8")
    elif not keep_image:
        add_log("system", "Cleared duel state.", "#38bdf8")


def write_ui(status_override=None):
    if status_override is not None:
        status = status_override
    elif not image_loaded:
        status = "Upload an image to begin. Python shrinks it, extracts a palette/grid brief, and validates each model stroke."
    else:
        run_a = "thinking" if worker_running[MODEL_A] else ("done" if len(strokes[MODEL_A]) >= max_steps else "ready")
        run_b = "thinking" if worker_running[MODEL_B] else ("done" if len(strokes[MODEL_B]) >= max_steps else "ready")
        if active:
            status = f"Running simultaneously · A {run_a} {len(strokes[MODEL_A])}/{max_steps} · B {run_b} {len(strokes[MODEL_B])}/{max_steps}"
        elif len(strokes[MODEL_A]) >= max_steps and len(strokes[MODEL_B]) >= max_steps:
            if scores[MODEL_A] > scores[MODEL_B]:
                winner = f"Winner: Model A ({MODEL_A})"
            elif scores[MODEL_B] > scores[MODEL_A]:
                winner = f"Winner: Model B ({MODEL_B})"
            else:
                winner = "Draw: equal score"
            status = f"Finished · {winner}"
        else:
            status = f"Paused · A {run_a} {len(strokes[MODEL_A])}/{max_steps} · B {run_b} {len(strokes[MODEL_B])}/{max_steps}"

    payload = {
        "status": status,
        "model_a_name": MODEL_A,
        "model_b_name": MODEL_B,
        "step_budget_label": str(max_steps),
        "brush_max_label": str(brush_max),
        "target_name": target_name,
        "target_html": image_html_from_data_url(target_preview_data_url, target_name) if image_loaded else placeholder_html("Upload an image to create the target"),
        "target_summary": target_summary,
        "svg_a": render_svg(strokes[MODEL_A], cursor_state[MODEL_A]),
        "svg_b": render_svg(strokes[MODEL_B], cursor_state[MODEL_B]),
        "score_a": f"{scores[MODEL_A]:.2f}",
        "score_b": f"{scores[MODEL_B]:.2f}",
        "steps_a": str(len(strokes[MODEL_A])),
        "steps_b": str(len(strokes[MODEL_B])),
        "last_a": last_action_text[MODEL_A],
        "last_b": last_action_text[MODEL_B],
        "cursor_a_text": f"Cursor: {cursor_state[MODEL_A].get('label', 'idle')} @ {cursor_state[MODEL_A].get('x',0)},{cursor_state[MODEL_A].get('y',0)}",
        "cursor_b_text": f"Cursor: {cursor_state[MODEL_B].get('label', 'idle')} @ {cursor_state[MODEL_B].get('x',0)},{cursor_state[MODEL_B].get('y',0)}",
    }
    hc.write("root/app", **payload)


def current_grid_for_model(model_name):
    img = render_strokes_to_image(strokes[model_name])
    return palette_grid_lines(img, target_palette, size=16)


def recent_stroke_text(model_name, n=6):
    items = strokes[model_name][-n:]
    if not items:
        return "none"
    out = []
    for s in items:
        if s.get("tool") == "line":
            out.append(f"line {s['x1']},{s['y1']}->{s['x2']},{s['y2']} {s['color']} sz={s['size']}")
        else:
            out.append(f"dot {s['x']},{s['y']} {s['color']} sz={s['size']}")
    return "; ".join(out)


def stroke_signature(stroke):
    if stroke.get("tool") == "line":
        return ("line", stroke["color"], int(stroke["size"]), int(stroke["x1"]), int(stroke["y1"]), int(stroke["x2"]), int(stroke["y2"]))
    return ("dot", stroke["color"], int(stroke["size"]), int(stroke["x"]), int(stroke["y"]))


def strokes_too_similar(a, b):
    if not a or not b:
        return False
    if a.get("tool") != b.get("tool") or a.get("color") != b.get("color"):
        return False
    if abs(int(a.get("size", 0)) - int(b.get("size", 0))) > 2:
        return False
    if a.get("tool") == "dot":
        return abs(a["x"] - b["x"]) <= 10 and abs(a["y"] - b["y"]) <= 10
    return (
        abs(a["x1"] - b["x1"]) <= 10
        and abs(a["y1"] - b["y1"]) <= 10
        and abs(a["x2"] - b["x2"]) <= 10
        and abs(a["y2"] - b["y2"]) <= 10
    )


def recent_reject_text(model_name):
    items = reject_memory.get(model_name, [])[-3:]
    return " | ".join(items) if items else "none"


def model_score_with_candidate(model_name, extra_stroke=None):
    trial = list(strokes[model_name])
    if extra_stroke is not None:
        trial.append(extra_stroke)
    return compute_similarity(target_image, render_strokes_to_image(trial))


def mismatch_hints_for_model(model_name, limit=12):
    current_grid = current_grid_for_model(model_name)
    hints = []
    bg_idx = target_palette.index(target_bg_color) if target_bg_color in target_palette else 0

    rows = min(len(target_grid), len(current_grid))
    for y in range(rows):
        cols = min(len(target_grid[y]), len(current_grid[y]))
        for x in range(cols):
            want = target_grid[y][x]
            have = current_grid[y][x]
            edge = target_edge_grid[y][x] if y < len(target_edge_grid) and x < len(target_edge_grid[y]) else "."
            if want == have and edge == ".":
                continue

            want_idx = PALETTE_CHARS.find(want)
            have_idx = PALETTE_CHARS.find(have)
            want_idx = want_idx if want_idx >= 0 and want_idx < len(target_palette) else 0
            have_idx = have_idx if have_idx >= 0 and have_idx < len(target_palette) else 0
            want_color = target_palette[want_idx]
            have_color = target_palette[have_idx]

            priority = 0
            if want_idx != bg_idx and want != have:
                priority += 4
            if edge == "#":
                priority += 3
            if want != have:
                priority += 2

            cell_w = CANVAS / 16.0
            cx = int(x * cell_w + cell_w / 2)
            cy = int(y * cell_w + cell_w / 2)
            hints.append((priority, f"cell {x},{y} center {cx},{cy} want {want}/{want_color} now {have}/{have_color} edge {edge}"))

    hints.sort(key=lambda item: (-item[0], item[1]))
    return [text for _, text in hints[:limit]]


def next_focus_target(model_name):
    hints = mismatch_hints_for_model(model_name, limit=1)
    if hints:
        m = re.search(r"center (\d+),(\d+) want ([0-9A-Z])/(#[0-9a-f]{6})", hints[0])
        if m:
            x, y, _, color = m.groups()
            return {"x": int(x), "y": int(y), "color": color}
    x1, y1, x2, y2 = target_focus_bbox
    return {"x": int((x1 + x2) / 2), "y": int((y1 + y2) / 2), "color": target_bg_color}


def set_cursor_for_plan(model_name, label):
    target = next_focus_target(model_name)
    cursor_state[model_name] = {
        "visible": True,
        "x": target["x"],
        "y": target["y"],
        "color": "#2563eb" if model_name == MODEL_A else "#a855f7",
        "size": clamp(max(6, brush_max // 2), 4, 20),
        "label": label,
    }


def set_cursor_from_stroke(model_name, stroke, label):
    if stroke.get("tool") == "line":
        x = int(stroke.get("x2", stroke.get("x1", CANVAS // 2)))
        y = int(stroke.get("y2", stroke.get("y1", CANVAS // 2)))
    else:
        x = int(stroke.get("x", CANVAS // 2))
        y = int(stroke.get("y", CANVAS // 2))
    cursor_state[model_name] = {
        "visible": True,
        "x": x,
        "y": y,
        "color": stroke.get("color", "#2563eb" if model_name == MODEL_A else "#a855f7"),
        "size": clamp(int(stroke.get("size", 8)), 4, 20),
        "label": label,
    }


def build_prompt(model_name, invalid_feedback=None):
    remaining = max_steps - len(strokes[model_name])
    target_grid_text = "\n".join(target_grid)
    edge_grid_text = "\n".join(target_edge_grid)
    current_grid_text = "\n".join(current_grid_for_model(model_name))
    palette_legend = "\n".join(f"{PALETTE_CHARS[i]} = {c}" for i, c in enumerate(target_palette))
    mismatch_text = "\n".join(mismatch_hints_for_model(model_name, limit=12)) or "none"
    x1, y1, x2, y2 = target_focus_bbox

    system_text = (
        "You control one brush stroke in a competitive drawing game.\n"
        "Your goal is immediate score improvement, not commentary.\n"
        "Return exactly one JSON object and nothing else.\n"
        "Allowed tools are 'line' and 'dot'.\n"
        f"Canvas coordinates are integers from 0 to {CANVAS - 1}.\n"
        f"Brush size must be an integer from 1 to {brush_max}.\n"
        f"Color must be one of these exact hex values: {', '.join(target_palette)}\n"
        "Never repeat or nearly repeat one of your recent strokes.\n"
        "Prefer a long line when multiple mismatch cells of the same color appear aligned.\n"
        "Prefer a dot when correcting one isolated cell.\n"
        "Target the mismatch cells and focus box first, not empty white background.\n"
        "For a line use: {\"tool\":\"line\",\"x1\":12,\"y1\":34,\"x2\":200,\"y2\":220,\"color\":\"#112233\",\"size\":8}\n"
        "For a dot use: {\"tool\":\"dot\",\"x\":120,\"y\":100,\"color\":\"#112233\",\"size\":10}\n"
        "Do not explain. Do not use markdown. Output JSON only."
    )

    user_text = (
        f"You are model {model_name}.\n"
        f"You have {remaining} strokes left.\n"
        f"Your current score is {scores[model_name]:.2f}.\n"
        f"Your previous score delta was {last_score_delta[model_name]:+.2f}.\n"
        f"Target focus box: {x1},{y1} -> {x2},{y2}.\n\n"
        "Palette legend:\n"
        f"{palette_legend}\n\n"
        "Target 16x16 palette grid:\n"
        f"{target_grid_text}\n\n"
        "Target 16x16 edge grid (# = edge, . = blank):\n"
        f"{edge_grid_text}\n\n"
        "Your current 16x16 palette grid:\n"
        f"{current_grid_text}\n\n"
        "Highest-priority mismatch cells:\n"
        f"{mismatch_text}\n\n"
        f"Your recent strokes: {recent_stroke_text(model_name)}\n"
        "Do not choose a white stroke unless a mismatch hint explicitly asks for white.\n"
        f"Recent rejected ideas: {recent_reject_text(model_name)}\n"
        "Choose one stroke that improves the score right now."
    )

    if invalid_feedback:
        user_text += f"\n\nPrevious candidate was rejected: {invalid_feedback}\nReturn a different stroke."

    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]


def ollama_stroke(model_name, invalid_feedback=None):
    resp = requests.post(
        OLLAMA_CHAT_URL,
        json={
            "model": model_name,
            "messages": build_prompt(model_name, invalid_feedback=invalid_feedback),
            "stream": False,
            "think": False,
            "options": {"temperature": 0.25},
            "keep_alive": "10m",
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    msg = data.get("message", {}) or {}
    return (msg.get("content") or msg.get("thinking") or "").strip()


def extract_json_object(text):
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def normalize_stroke(obj):
    if not isinstance(obj, dict):
        raise ValueError("stroke must be a JSON object")

    tool = str(obj.get("tool", "dot")).strip().lower()
    if tool not in ("line", "dot"):
        raise ValueError("tool must be 'line' or 'dot'")

    color = str(obj.get("color", target_palette[0] if target_palette else "#000000")).strip().lower()
    if color not in target_palette:
        rgb = hex_to_rgb(color)
        color = nearest_palette_color(rgb, target_palette) if rgb is not None else target_palette[0]

    size = int(round(float(obj.get("size", max(2, brush_max // 3)))))
    size = clamp(size, 1, brush_max)

    if tool == "line":
        x1 = clamp(int(round(float(obj.get("x1")))), 0, CANVAS - 1)
        y1 = clamp(int(round(float(obj.get("y1")))), 0, CANVAS - 1)
        x2 = clamp(int(round(float(obj.get("x2")))), 0, CANVAS - 1)
        y2 = clamp(int(round(float(obj.get("y2")))), 0, CANVAS - 1)
        return {"tool": "line", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "color": color, "size": size}

    x = clamp(int(round(float(obj.get("x")))), 0, CANVAS - 1)
    y = clamp(int(round(float(obj.get("y")))), 0, CANVAS - 1)
    return {"tool": "dot", "x": x, "y": y, "color": color, "size": size}


def stroke_reject_reason(model_name, stroke):
    for prior in strokes[model_name][-6:]:
        if strokes_too_similar(prior, stroke):
            return "Too similar to one of your recent strokes. Move to a different cell, change size meaningfully, or use a longer line."
    if stroke.get("tool") == "line":
        length = abs(stroke["x2"] - stroke["x1"]) + abs(stroke["y2"] - stroke["y1"])
        if length < max(12, stroke["size"] * 2):
            return "Line is too short to be useful. Use a longer line or switch to a dot."

    trial_score = model_score_with_candidate(model_name, extra_stroke=stroke)
    delta = round(trial_score - scores[model_name], 2)
    if len(strokes[model_name]) >= 2 and delta <= 0.03:
        return f"That stroke would not improve your score enough (delta {delta:.2f}). Aim at a mismatch cell you have not touched recently."
    return None


def fallback_stroke(model_name):
    hints = mismatch_hints_for_model(model_name, limit=10)
    by_color = {}
    candidates = []

    for line in hints:
        m = re.search(r"center (\d+),(\d+) want ([0-9A-Z])/(#[0-9a-f]{6})", line)
        if not m:
            continue
        cx, cy, _, color = m.groups()
        cx = int(cx)
        cy = int(cy)
        by_color.setdefault(color, []).append((cx, cy))
        candidates.append((cx, cy, color))

    for color, pts in by_color.items():
        if len(pts) >= 2:
            p1 = pts[0]
            p2 = pts[-1]
            if abs(p1[0] - p2[0]) + abs(p1[1] - p2[1]) >= 20:
                return {
                    "tool": "line",
                    "x1": p1[0],
                    "y1": p1[1],
                    "x2": p2[0],
                    "y2": p2[1],
                    "color": color,
                    "size": clamp(max(4, brush_max // 2), 1, brush_max),
                }

    if candidates:
        cx, cy, color = candidates[0]
        return {
            "tool": "dot",
            "x": cx,
            "y": cy,
            "color": color,
            "size": clamp(max(4, brush_max // 2), 1, brush_max),
        }

    current = render_strokes_to_image(strokes[model_name]).convert("RGB")
    diff = ImageChops.difference(target_image, current).resize((16, 16), Image.Resampling.BILINEAR).convert("L")

    best_x = 0
    best_y = 0
    best_v = -1
    for y in range(16):
        for x in range(16):
            v = diff.getpixel((x, y))
            if v > best_v:
                best_v = v
                best_x = x
                best_y = y

    cell_w = CANVAS / 16.0
    cx = int(best_x * cell_w + cell_w / 2)
    cy = int(best_y * cell_w + cell_w / 2)
    target_rgb = target_image.getpixel((clamp(cx, 0, CANVAS - 1), clamp(cy, 0, CANVAS - 1)))
    color = nearest_palette_color(target_rgb, target_palette)
    size = clamp(int(max(4, min(brush_max, (best_v / 255.0) * brush_max))), 1, brush_max)
    return {"tool": "dot", "x": cx, "y": cy, "color": color, "size": size}


def choose_stroke(model_name):
    invalid_feedback = None
    last_raw = ""
    for _ in range(3):
        raw = ollama_stroke(model_name, invalid_feedback=invalid_feedback)
        last_raw = raw
        parsed = extract_json_object(raw)
        stroke = normalize_stroke(parsed)
        reject_reason = stroke_reject_reason(model_name, stroke)
        if reject_reason:
            reject_memory[model_name].append(reject_reason)
            reject_memory[model_name] = reject_memory[model_name][-6:]
            invalid_feedback = reject_reason
            continue
        delta = round(model_score_with_candidate(model_name, extra_stroke=stroke) - scores[model_name], 2)
        return stroke, raw, False, delta

    stroke = fallback_stroke(model_name)
    delta = round(model_score_with_candidate(model_name, extra_stroke=stroke) - scores[model_name], 2)
    return stroke, last_raw or "no valid reply", True, delta


def describe_stroke(stroke):
    if stroke.get("tool") == "line":
        return f"line {stroke['x1']},{stroke['y1']}→{stroke['x2']},{stroke['y2']} {stroke['color']} sz {stroke['size']}"
    return f"dot {stroke['x']},{stroke['y']} {stroke['color']} sz {stroke['size']}"


def start_model_worker(model_name):
    if not active or not image_loaded:
        return
    if worker_running[model_name]:
        return
    if len(strokes[model_name]) >= max_steps:
        return
    worker_running[model_name] = True
    worker_started_at[model_name] = time.time()
    set_cursor_for_plan(model_name, "thinking")

    epoch = duel_epoch

    def _worker():
        try:
            stroke, raw, used_fallback, delta = choose_stroke(model_name)
            worker_results.put({
                "epoch": epoch,
                "model": model_name,
                "stroke": stroke,
                "raw": raw,
                "used_fallback": used_fallback,
                "delta": delta,
                "error": None,
            })
        except Exception as e:
            worker_results.put({
                "epoch": epoch,
                "model": model_name,
                "stroke": None,
                "raw": f"error: {e}",
                "used_fallback": True,
                "delta": 0.0,
                "error": str(e),
            })

    threading.Thread(target=_worker, daemon=True).start()


def maybe_launch_workers():
    if not active or not image_loaded:
        return
    for model_name in (MODEL_A, MODEL_B):
        if len(strokes[model_name]) < max_steps and not worker_running[model_name]:
            start_model_worker(model_name)


def finalize_if_done():
    global active
    a_done = len(strokes[MODEL_A]) >= max_steps
    b_done = len(strokes[MODEL_B]) >= max_steps
    if a_done and b_done and not worker_running[MODEL_A] and not worker_running[MODEL_B]:
        active = False
        if scores[MODEL_A] > scores[MODEL_B]:
            add_log("result", f"Model A wins {scores[MODEL_A]:.2f} vs {scores[MODEL_B]:.2f}", "#22c55e")
        elif scores[MODEL_B] > scores[MODEL_A]:
            add_log("result", f"Model B wins {scores[MODEL_B]:.2f} vs {scores[MODEL_A]:.2f}", "#a855f7")
        else:
            add_log("result", f"Draw at {scores[MODEL_A]:.2f}", "#94a3b8")


def apply_worker_result(item):
    global active, last_score_delta

    model_name = item["model"]
    worker_running[model_name] = False

    if item.get("epoch") != duel_epoch:
        return

    stroke = item.get("stroke")
    raw = item.get("raw") or ""
    used_fallback = bool(item.get("used_fallback"))
    if stroke is None:
        stroke = fallback_stroke(model_name)
        raw = raw or item.get("error") or "worker failed"
        used_fallback = True

    if len(strokes[model_name]) >= max_steps:
        finalize_if_done()
        return

    strokes[model_name].append(stroke)
    update_scores()
    delta = round(scores[model_name] - (scores[model_name] - item.get("delta", 0.0)), 2)
    last_score_delta[model_name] = delta
    last_action_text[model_name] = describe_stroke(stroke)
    set_cursor_from_stroke(model_name, stroke, f"step {len(strokes[model_name])}")

    border = "#f59e0b" if used_fallback else ("#22c55e" if model_name == MODEL_A else "#a855f7")
    meta = f"{model_name} · step {len(strokes[model_name])}/{max_steps} · Δ {delta:+.2f}"
    text = describe_stroke(stroke)
    if used_fallback:
        text += f"\nFallback used after invalid or low-value replies: {raw[:220]}"
    else:
        text += f"\nRaw reply: {raw[:220]}"
    add_log(meta, text, border)
    finalize_if_done()


def export_single(model_name):
    out_dir = ensure_exports_dir()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = "a" if model_name == MODEL_A else "b"
    out_path = os.path.join(out_dir, f"etch_battle_{tag}_{stamp}.png")
    render_strokes_to_image(strokes[model_name]).save(out_path)
    return out_path


def label_card(base_img, title, subtitle):
    card = Image.new("RGB", (CANVAS + 40, CANVAS + 90), "white")
    card.paste(base_img.resize((CANVAS, CANVAS)), (20, 50))
    draw = ImageDraw.Draw(card)
    draw.text((20, 18), title, fill="black")
    draw.text((20, CANVAS + 60), subtitle, fill="#444444")
    return card


def export_both():
    if target_image is None:
        raise ValueError("no target loaded")

    out_dir = ensure_exports_dir()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(out_dir, f"etch_battle_duel_{stamp}.png")

    target_card = label_card(target_image, "Target", target_name)
    a_card = label_card(render_strokes_to_image(strokes[MODEL_A]), MODEL_A, f"score {scores[MODEL_A]:.2f}")
    b_card = label_card(render_strokes_to_image(strokes[MODEL_B]), MODEL_B, f"score {scores[MODEL_B]:.2f}")

    total_w = target_card.width + a_card.width + b_card.width + 40
    total_h = max(target_card.height, a_card.height, b_card.height) + 40
    sheet = Image.new("RGB", (total_w, total_h), "#e2e8f0")
    sheet.paste(target_card, (10, 20))
    sheet.paste(a_card, (20 + target_card.width, 20))
    sheet.paste(b_card, (30 + target_card.width + a_card.width, 20))
    sheet.save(out_path)
    return out_path


def handle_action(msg):
    global active, max_steps, brush_max

    action = msg.get("type")

    if action == "config":
        max_steps = clamp(int(msg.get("max_steps", max_steps)), 8, 64)
        brush_max = clamp(int(msg.get("brush_max", brush_max)), 4, 30)
        if len(strokes[MODEL_A]) > max_steps:
            strokes[MODEL_A] = strokes[MODEL_A][:max_steps]
        if len(strokes[MODEL_B]) > max_steps:
            strokes[MODEL_B] = strokes[MODEL_B][:max_steps]
        update_scores()
        write_ui()
        return

    if action == "upload_image":
        size = int(msg.get("size") or 0)
        if size <= 0:
            raise ValueError("empty upload")
        if size > MAX_UPLOAD_BYTES:
            raise ValueError(f"upload too large ({size} bytes); keep it under {MAX_UPLOAD_BYTES} bytes")
        preprocess_uploaded_image(msg.get("data_url", ""), safe_name(msg.get("name") or "upload"))
        reset_duel(keep_image=True)
        add_log(
            "upload",
            f"Loaded target image: {target_name}\n"
            f"Palette: {', '.join(target_palette)}\n"
            f"Background: {target_bg_color}\n"
            f"Focus box: {target_focus_bbox[0]},{target_focus_bbox[1]} -> {target_focus_bbox[2]},{target_focus_bbox[3]}",
            "#38bdf8",
        )
        write_ui("Image loaded. Click Start / Resume to begin the duel.")
        return

    if action == "start":
        if not image_loaded:
            write_ui("Upload an image first.")
            return
        if len(strokes[MODEL_A]) >= max_steps and len(strokes[MODEL_B]) >= max_steps:
            reset_duel(keep_image=True)
        active = True
        maybe_launch_workers()
        write_ui()
        return

    if action == "pause":
        active = False
        write_ui("Paused.")
        return

    if action == "reset_duel":
        reset_duel(keep_image=True)
        write_ui()
        return

    if action == "save_a":
        path = export_single(MODEL_A)
        add_log("export", f"Saved Model A PNG\n{path}", "#0f766e")
        write_ui(f"Saved Model A PNG to {path}")
        return

    if action == "save_b":
        path = export_single(MODEL_B)
        add_log("export", f"Saved Model B PNG\n{path}", "#7c3aed")
        write_ui(f"Saved Model B PNG to {path}")
        return

    if action == "save_both":
        path = export_both()
        add_log("export", f"Saved combined duel PNG\n{path}", "#9333ea")
        write_ui(f"Saved combined duel PNG to {path}")
        return


# ---------------------------------
# Initial render
# ---------------------------------
update_scores()
write_ui()

# ---------------------------------
# Main loop
# ---------------------------------
while True:
    snap = hc.snapshot() or {}

    for k, v in snap.items():
        if not k.startswith("inbox/"):
            continue

        try:
            raw = v.get("data", {})
            msg = json.loads(raw) if isinstance(raw, str) else raw
            handle_action(msg)
        except Exception as e:
            add_log("server", f"Error processing action: {e}", "#7c3aed")
            write_ui(f"Error: {e}")

        hc.remove(k)

    ui_dirty = False
    while True:
        try:
            item = worker_results.get_nowait()
        except Exception:
            break
        apply_worker_result(item)
        ui_dirty = True

    if active and image_loaded:
        before = (worker_running[MODEL_A], worker_running[MODEL_B])
        maybe_launch_workers()
        after = (worker_running[MODEL_A], worker_running[MODEL_B])
        if after != before:
            ui_dirty = True
        finalize_if_done()

    if ui_dirty:
        write_ui()

    time.sleep(0.05)
