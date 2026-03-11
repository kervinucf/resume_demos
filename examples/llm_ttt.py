#!/usr/bin/env python3
import json
import re
import time
import requests
from HyperCoreSDK import HyperClient

# -----------------------------
# Config
# -----------------------------
RELAY = "http://localhost:8765"   # change to 8766 if that is your relay
ROOT = "demo_llm_tictactoe_loop"

MODEL_X = "qwen2.5:3b"
MODEL_O = "gemma3:4b"
OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"

TURN_DELAY = 1.0        # seconds between moves
RESTART_DELAY = 2.0     # seconds before a finished game restarts
REQUEST_TIMEOUT = 90    # seconds for each Ollama call


# -----------------------------
# HyperCore setup
# -----------------------------
hc = HyperClient(relay=RELAY, root=ROOT)
hc.start_relay()
hc.clear()


# -----------------------------
# Static parent UI
# Only one data-children target in the parent: the move log.
# -----------------------------
APP_HTML = """
<div style="width:100%;height:100%;display:flex;flex-direction:column;background:#0f172a;color:#e2e8f0;font-family:Arial,sans-serif">

  <div style="padding:14px 16px;border-bottom:1px solid #334155;display:flex;justify-content:space-between;align-items:center;gap:12px;background:#111827">
    <div style="min-width:0">
      <div style="font-size:18px;font-weight:800">LLM Tic-Tac-Toe Loop</div>
      <div data-bind-text="status" style="font-size:12px;color:#94a3b8;margin-top:4px;min-height:16px"></div>
    </div>

    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <button id="toggle_btn" style="padding:10px 14px;background:#2563eb;color:#fff;border:none;border-radius:8px;cursor:pointer;font-weight:700">
        <span data-bind-text="toggle_label">Pause</span>
      </button>
      <button id="reset_game_btn" style="padding:10px 14px;background:#334155;color:#e2e8f0;border:none;border-radius:8px;cursor:pointer;font-weight:700">
        Next Game
      </button>
      <button id="reset_scores_btn" style="padding:10px 14px;background:#7c2d12;color:#fff;border:none;border-radius:8px;cursor:pointer;font-weight:700">
        Reset Scores
      </button>
    </div>
  </div>

  <div style="flex:1;min-height:0;display:flex;gap:16px;padding:16px">
    <div style="flex:0 0 420px;max-width:420px;display:flex;flex-direction:column;gap:14px">
      <div style="background:#111827;border:1px solid #334155;border-radius:14px;padding:14px">
        <div style="display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:12px">
          <div>
            <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.12em">Current Game</div>
            <div data-bind-text="game_label" style="font-size:18px;font-weight:800;margin-top:4px"></div>
          </div>
          <div style="text-align:right">
            <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.12em">Last Move</div>
            <div data-bind-text="last_move" style="font-size:14px;font-weight:700;margin-top:4px"></div>
          </div>
        </div>

        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;background:#0b1220;padding:8px;border-radius:12px">
          <div data-bind-text="v0" style="height:110px;display:flex;align-items:center;justify-content:center;background:#1f2937;border:1px solid #334155;border-radius:10px;font-size:46px;font-weight:800"></div>
          <div data-bind-text="v1" style="height:110px;display:flex;align-items:center;justify-content:center;background:#1f2937;border:1px solid #334155;border-radius:10px;font-size:46px;font-weight:800"></div>
          <div data-bind-text="v2" style="height:110px;display:flex;align-items:center;justify-content:center;background:#1f2937;border:1px solid #334155;border-radius:10px;font-size:46px;font-weight:800"></div>
          <div data-bind-text="v3" style="height:110px;display:flex;align-items:center;justify-content:center;background:#1f2937;border:1px solid #334155;border-radius:10px;font-size:46px;font-weight:800"></div>
          <div data-bind-text="v4" style="height:110px;display:flex;align-items:center;justify-content:center;background:#1f2937;border:1px solid #334155;border-radius:10px;font-size:46px;font-weight:800"></div>
          <div data-bind-text="v5" style="height:110px;display:flex;align-items:center;justify-content:center;background:#1f2937;border:1px solid #334155;border-radius:10px;font-size:46px;font-weight:800"></div>
          <div data-bind-text="v6" style="height:110px;display:flex;align-items:center;justify-content:center;background:#1f2937;border:1px solid #334155;border-radius:10px;font-size:46px;font-weight:800"></div>
          <div data-bind-text="v7" style="height:110px;display:flex;align-items:center;justify-content:center;background:#1f2937;border:1px solid #334155;border-radius:10px;font-size:46px;font-weight:800"></div>
          <div data-bind-text="v8" style="height:110px;display:flex;align-items:center;justify-content:center;background:#1f2937;border:1px solid #334155;border-radius:10px;font-size:46px;font-weight:800"></div>
        </div>
      </div>

      <div style="background:#111827;border:1px solid #334155;border-radius:14px;padding:14px;display:flex;flex-direction:column;gap:10px">
        <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.12em">Models</div>

        <div style="display:flex;justify-content:space-between;gap:10px;padding:10px 12px;background:#0b1220;border-radius:10px">
          <div>
            <div style="font-size:12px;color:#93c5fd;font-weight:800">X</div>
            <div data-bind-text="x_model" style="font-size:13px;margin-top:3px"></div>
          </div>
          <div style="font-size:24px;font-weight:900" data-bind-text="x_score">0</div>
        </div>

        <div style="display:flex;justify-content:space-between;gap:10px;padding:10px 12px;background:#0b1220;border-radius:10px">
          <div>
            <div style="font-size:12px;color:#fca5a5;font-weight:800">O</div>
            <div data-bind-text="o_model" style="font-size:13px;margin-top:3px"></div>
          </div>
          <div style="font-size:24px;font-weight:900" data-bind-text="o_score">0</div>
        </div>

        <div style="display:flex;justify-content:space-between;gap:10px;padding:10px 12px;background:#0b1220;border-radius:10px">
          <div>
            <div style="font-size:12px;color:#cbd5e1;font-weight:800">Draws</div>
            <div style="font-size:13px;margin-top:3px">Completed games: <span data-bind-text="completed_games">0</span></div>
          </div>
          <div style="font-size:24px;font-weight:900" data-bind-text="draw_score">0</div>
        </div>
      </div>
    </div>

    <div style="flex:1;min-width:0;display:flex;flex-direction:column;background:#111827;border:1px solid #334155;border-radius:14px;overflow:hidden">
      <div style="padding:12px 14px;border-bottom:1px solid #334155;display:flex;justify-content:space-between;align-items:center;background:#0b1220">
        <div style="font-size:14px;font-weight:800">Move Log</div>
        <div style="font-size:12px;color:#94a3b8">Python owns all game state</div>
      </div>

      <div data-children style="flex:1;min-height:0;overflow:auto;display:flex;flex-direction:column;gap:10px;padding:14px"></div>
    </div>
  </div>
</div>
"""

APP_JS = r"""
(function(){
  const toggleBtn = document.getElementById("toggle_btn");
  const resetGameBtn = document.getElementById("reset_game_btn");
  const resetScoresBtn = document.getElementById("reset_scores_btn");

  if (!toggleBtn || !resetGameBtn || !resetScoresBtn || toggleBtn.dataset.on) return;
  toggleBtn.dataset.on = "1";

  window.sendAction = (type) => {
    const path = "inbox/" + Date.now() + "_" + Math.random().toString(36).slice(2,7);
    window.$scene.get(path).put({
      data: JSON.stringify({ type: type, ts: Date.now() })
    });
  };

  toggleBtn.onclick = () => window.sendAction("toggle");
  resetGameBtn.onclick = () => window.sendAction("reset_game");
  resetScoresBtn.onclick = () => window.sendAction("reset_scores");
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


# -----------------------------
# Game state
# -----------------------------
board = [""] * 9
turn = "X"
winner = None

auto_running = True
game_no = 0
completed_games = 0
x_score = 0
o_score = 0
draw_score = 0

last_move = "—"
next_move_at = 0.0
restart_at = 0.0

log_counter = 0
log_paths = []


# -----------------------------
# Helpers
# -----------------------------
def legal_moves(state):
    return [i for i, v in enumerate(state) if v == ""]


def check_winner(state):
    lines = [
        (0, 1, 2), (3, 4, 5), (6, 7, 8),
        (0, 3, 6), (1, 4, 7), (2, 5, 8),
        (0, 4, 8), (2, 4, 6),
    ]
    for a, b, c in lines:
        if state[a] and state[a] == state[b] == state[c]:
            return state[a]
    return "Draw" if "" not in state else None


def board_for_prompt(state):
    vals = [state[i] if state[i] else str(i) for i in range(9)]
    return (
        f"{vals[0]}|{vals[1]}|{vals[2]}\n"
        f"{vals[3]}|{vals[4]}|{vals[5]}\n"
        f"{vals[6]}|{vals[7]}|{vals[8]}"
    )


def other_symbol(symbol):
    return "O" if symbol == "X" else "X"


def short_text(s, n=90):
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def write_ui(status_override=None):
    if status_override is not None:
        status = status_override
    elif winner == "Draw":
        status = f"Game {game_no} ended in a draw. Next game queued."
    elif winner in ("X", "O"):
        model = MODEL_X if winner == "X" else MODEL_O
        status = f"Game {game_no} won by {winner} ({model}). Next game queued."
    elif auto_running:
        model = MODEL_X if turn == "X" else MODEL_O
        status = f"Game {game_no} · {turn} to move · {model}"
    else:
        status = f"Paused on game {game_no} · {turn} to move"

    payload = {
        "status": status,
        "toggle_label": "Pause" if auto_running else "Resume",
        "game_label": f"Game {game_no}",
        "last_move": last_move,
        "x_model": MODEL_X,
        "o_model": MODEL_O,
        "x_score": str(x_score),
        "o_score": str(o_score),
        "draw_score": str(draw_score),
        "completed_games": str(completed_games),
        "v0": board[0],
        "v1": board[1],
        "v2": board[2],
        "v3": board[3],
        "v4": board[4],
        "v5": board[5],
        "v6": board[6],
        "v7": board[7],
        "v8": board[8],
    }
    hc.write("root/app", **payload)


def add_log(meta, text, border_color="#334155"):
    global log_counter, log_paths
    log_counter += 1
    path = f"root/app/log_{log_counter:03d}"
    log_paths.append(path)
    hc.mount(path, html=LOG_HTML, layer=5)
    hc.write(path, meta=meta, text=text, border_color=border_color)


def clear_logs():
    global log_counter, log_paths
    for p in log_paths:
        try:
            hc.remove(p)
        except Exception:
            pass
    log_counter = 0
    log_paths = []


def start_new_game(reset_scores=False):
    global board, turn, winner, game_no, completed_games
    global x_score, o_score, draw_score, last_move, next_move_at, restart_at

    if reset_scores:
        completed_games = 0
        x_score = 0
        o_score = 0
        draw_score = 0
        game_no = 0

    clear_logs()

    board = [""] * 9
    turn = "X"
    winner = None
    last_move = "—"
    restart_at = 0.0
    game_no += 1
    next_move_at = time.time() + TURN_DELAY

    add_log(
        "system",
        f"Started game {game_no}. X = {MODEL_X} · O = {MODEL_O}",
        "#38bdf8"
    )
    write_ui()


def extract_first_int(text):
    m = re.search(r"-?\d+", text or "")
    return int(m.group()) if m else None


def build_prompt(symbol, state, invalid_reply=None):
    legal = legal_moves(state)

    system_text = (
        "You are a Tic-Tac-Toe move engine.\n"
        "Your entire reply must be exactly one token: a legal move index.\n"
        "Allowed replies are only: " + ", ".join(map(str, legal)) + "\n"
        "Do not explain.\n"
        "Do not use JSON.\n"
        "Do not write words.\n"
        "Do not repeat the board.\n"
        "Output exactly one number."
    )

    user_text = (
        f"Player: {symbol}\n"
        f"Board: {state}\n"
        f"Legal moves: {legal}\n"
        "Reply with exactly one legal move index."
    )

    if invalid_reply is not None:
        user_text += (
            f"\nPrevious reply was invalid: {invalid_reply!r}\n"
            f"Reply with one of: {legal}"
        )

    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]

def ollama_raw_move(model, symbol, state, invalid_reply=None):
    resp = requests.post(
        OLLAMA_CHAT_URL,
        json={
            "model": model,
            "messages": build_prompt(symbol, state, invalid_reply=invalid_reply),
            "stream": False,
            "think": False,
            "options": {"temperature": 0.2},
            "keep_alive": "10m",
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    msg = data.get("message", {}) or {}
    content = (msg.get("content") or "").strip()
    thinking = (msg.get("thinking") or "").strip()
    return content or thinking or ""


def fallback_move(symbol, state):
    legal = legal_moves(state)
    if not legal:
        return None

    # win now
    for m in legal:
        tmp = state[:]
        tmp[m] = symbol
        if check_winner(tmp) == symbol:
            return m

    # block loss
    opp = other_symbol(symbol)
    for m in legal:
        tmp = state[:]
        tmp[m] = opp
        if check_winner(tmp) == opp:
            return m

    # simple priorities
    for m in [4, 0, 2, 6, 8, 1, 3, 5, 7]:
        if m in legal:
            return m

    return legal[0]


def choose_move(model, symbol, state):
    invalid_reply = None

    for _ in range(3):
        raw = ollama_raw_move(model, symbol, state, invalid_reply=invalid_reply)
        move = extract_first_int(raw)
        if move in legal_moves(state):
            return move, raw, False
        invalid_reply = raw or "<empty>"

    return fallback_move(symbol, state), invalid_reply, True


def apply_move():
    global board, turn, winner, last_move, next_move_at, restart_at
    global x_score, o_score, draw_score, completed_games

    symbol = turn
    model = MODEL_X if symbol == "X" else MODEL_O

    write_ui(f"Game {game_no} · {symbol} is choosing · {model}")

    try:
        move, raw, used_fallback = choose_move(model, symbol, board)
    except Exception as e:
        move = fallback_move(symbol, board)
        raw = f"request error: {e}"
        used_fallback = True

    if move is None:
        winner = "Draw"
    else:
        board[move] = symbol
        last_move = f"{symbol} → {move}"

        if used_fallback:
            add_log(
                f"{symbol} · {model}",
                f"played {move} via fallback after invalid reply: {short_text(raw)}",
                "#f59e0b",
            )
        else:
            add_log(
                f"{symbol} · {model}",
                f"played {move} · raw reply: {short_text(raw)}",
                "#22c55e" if symbol == "X" else "#ef4444",
            )

        winner = check_winner(board)

    if winner == "X":
        x_score += 1
        completed_games += 1
        add_log("result", f"Game {game_no} winner: X ({MODEL_X})", "#10b981")
        restart_at = time.time() + RESTART_DELAY

    elif winner == "O":
        o_score += 1
        completed_games += 1
        add_log("result", f"Game {game_no} winner: O ({MODEL_O})", "#ef4444")
        restart_at = time.time() + RESTART_DELAY

    elif winner == "Draw":
        draw_score += 1
        completed_games += 1
        add_log("result", f"Game {game_no} ended in a draw", "#94a3b8")
        restart_at = time.time() + RESTART_DELAY

    else:
        turn = other_symbol(turn)
        next_move_at = time.time() + TURN_DELAY

    write_ui()


# -----------------------------
# Initial state
# -----------------------------
start_new_game(reset_scores=True)


# -----------------------------
# Main loop
# -----------------------------
while True:
    snap = hc.snapshot() or {}

    for k, v in snap.items():
        if not k.startswith("inbox/"):
            continue

        try:
            raw = v.get("data", {})
            msg = json.loads(raw) if isinstance(raw, str) else raw
            action = msg.get("type")

            if action == "toggle":
                auto_running = not auto_running
                if auto_running and not winner:
                    next_move_at = time.time() + 0.2
                write_ui()

            elif action == "reset_game":
                start_new_game(reset_scores=False)

            elif action == "reset_scores":
                start_new_game(reset_scores=True)

        except Exception as e:
            add_log("server", f"Error processing action: {e}", "#7c3aed")

        hc.remove(k)

    now = time.time()

    if auto_running:
        if winner and restart_at and now >= restart_at:
            start_new_game(reset_scores=False)
        elif not winner and now >= next_move_at:
            apply_move()

    time.sleep(0.1)