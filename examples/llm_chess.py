#!/usr/bin/env python3
import json
import re
import time
import requests
import chess
from client import HyperClient

# -----------------------------
# Config
# -----------------------------
RELAY = "http://localhost:8765"   # change to 8766 if needed
ROOT = "demo_llm_chess_streaming_compat"

MODEL_WHITE = "qwen2.5:3b"
MODEL_BLACK = "gemma3:4b"
OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"

TURN_DELAY = 1.2
RESTART_DELAY = 3.0
REQUEST_TIMEOUT = 120
THINK = False   # compatibility-first: disabled


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
def build_board_grid_html():
    cells = []
    for row in range(8):
        for col in range(8):
            idx = row * 8 + col
            light = (row + col) % 2 == 0
            bg = "#f0d9b5" if light else "#b58863"
            fg = "#111111"
            cells.append(
                f'<div data-bind-text="s{idx}" '
                f'style="height:48px;display:flex;align-items:center;justify-content:center;'
                f'background:{bg};color:{fg};border-radius:6px;font-size:30px;font-weight:800;'
                f'font-family:Segoe UI Symbol,Apple Symbols,Noto Sans Symbols,Arial,sans-serif"></div>'
            )
    return "\n".join(cells)


BOARD_GRID_HTML = build_board_grid_html()

APP_HTML = f"""
<div style="width:100%;height:100%;display:flex;flex-direction:column;background:#0f172a;color:#e2e8f0;font-family:Arial,sans-serif">

  <div style="padding:14px 16px;border-bottom:1px solid #334155;display:flex;justify-content:space-between;align-items:center;gap:12px;background:#111827">
    <div style="min-width:0">
      <div style="font-size:18px;font-weight:800">LLM Chess Loop</div>
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
    <div style="flex:0 0 460px;max-width:460px;display:flex;flex-direction:column;gap:14px">
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

        <div style="display:grid;grid-template-columns:repeat(8,1fr);gap:4px;background:#0b1220;padding:8px;border-radius:12px">
          {BOARD_GRID_HTML}
        </div>
      </div>

      <div style="background:#111827;border:1px solid #334155;border-radius:14px;padding:14px;display:flex;flex-direction:column;gap:10px">
        <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.12em">Live Analysis</div>

        <div style="padding:10px 12px;background:#0b1220;border-radius:10px">
          <div data-bind-text="analysis_title" style="font-size:13px;font-weight:800;color:#93c5fd"></div>
          <div data-bind-text="analysis_choice" style="font-size:12px;color:#cbd5e1;margin-top:4px"></div>
        </div>

        <div style="padding:10px 12px;background:#0b1220;border-radius:10px">
          <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.12em;margin-bottom:6px">Top Candidates</div>
          <div data-bind-text="candidate_1" style="font-size:12px;line-height:1.5;min-height:18px"></div>
          <div data-bind-text="candidate_2" style="font-size:12px;line-height:1.5;min-height:18px;margin-top:4px"></div>
          <div data-bind-text="candidate_3" style="font-size:12px;line-height:1.5;min-height:18px;margin-top:4px"></div>
        </div>

        <div style="padding:10px 12px;background:#0b1220;border-radius:10px">
          <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.12em;margin-bottom:6px">Thinking Stream</div>
          <div data-bind-text="analysis_thinking"
               style="white-space:pre-wrap;word-break:break-word;font-size:11px;line-height:1.45;min-height:80px;max-height:160px;overflow:auto;color:#e2e8f0;font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace"></div>
        </div>

        <div style="padding:10px 12px;background:#0b1220;border-radius:10px">
          <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.12em;margin-bottom:6px">Structured Output Stream</div>
          <div data-bind-text="analysis_content"
               style="white-space:pre-wrap;word-break:break-word;font-size:11px;line-height:1.45;min-height:80px;max-height:160px;overflow:auto;color:#e2e8f0;font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace"></div>
        </div>
      </div>

      <div style="background:#111827;border:1px solid #334155;border-radius:14px;padding:14px;display:flex;flex-direction:column;gap:10px">
        <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.12em">Models</div>

        <div style="display:flex;justify-content:space-between;gap:10px;padding:10px 12px;background:#0b1220;border-radius:10px">
          <div>
            <div style="font-size:12px;color:#93c5fd;font-weight:800">White</div>
            <div data-bind-text="white_model" style="font-size:13px;margin-top:3px"></div>
          </div>
          <div style="font-size:24px;font-weight:900" data-bind-text="white_score">0</div>
        </div>

        <div style="display:flex;justify-content:space-between;gap:10px;padding:10px 12px;background:#0b1220;border-radius:10px">
          <div>
            <div style="font-size:12px;color:#fca5a5;font-weight:800">Black</div>
            <div data-bind-text="black_model" style="font-size:13px;margin-top:3px"></div>
          </div>
          <div style="font-size:24px;font-weight:900" data-bind-text="black_score">0</div>
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

      <div id="move_log" data-children style="flex:1;min-height:0;overflow:auto;display:flex;flex-direction:column;gap:10px;padding:14px"></div>
    </div>
  </div>
</div>
"""

APP_JS = r"""
(function(){
  const toggleBtn = document.getElementById("toggle_btn");
  const resetGameBtn = document.getElementById("reset_game_btn");
  const resetScoresBtn = document.getElementById("reset_scores_btn");
  const moveLog = document.getElementById("move_log");

  if (!toggleBtn || !resetGameBtn || !resetScoresBtn || !moveLog || toggleBtn.dataset.on) return;
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

  const scrollToBottom = () => {
    moveLog.scrollTop = moveLog.scrollHeight;
  };

  const observer = new MutationObserver(() => {
    requestAnimationFrame(scrollToBottom);
  });

  observer.observe(moveLog, {
    childList: true,
    subtree: true,
    characterData: true
  });

  setTimeout(scrollToBottom, 50);
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
PIECE_TEXT = {
    "P": "♙", "N": "♘", "B": "♗", "R": "♖", "Q": "♕", "K": "♔",
    "p": "♟", "n": "♞", "b": "♝", "r": "♜", "q": "♛", "k": "♚",
}

board = chess.Board()
winner = None

auto_running = True
game_no = 0
completed_games = 0
white_score = 0
black_score = 0
draw_score = 0

last_move = "—"
next_move_at = 0.0
restart_at = 0.0

log_counter = 0
log_paths = []


# -----------------------------
# Helpers
# -----------------------------
def legal_moves(state: chess.Board):
    return [m.uci() for m in state.legal_moves]


def check_winner(state: chess.Board):
    if not state.is_game_over(claim_draw=True):
        return None
    outcome = state.outcome(claim_draw=True)
    if outcome is None or outcome.winner is None:
        return "Draw"
    return "White" if outcome.winner == chess.WHITE else "Black"


def board_cells_payload(state: chess.Board):
    payload = {}
    idx = 0
    for rank in range(7, -1, -1):
        for file in range(8):
            sq = chess.square(file, rank)
            piece = state.piece_at(sq)
            payload[f"s{idx}"] = PIECE_TEXT.get(piece.symbol(), "") if piece else ""
            idx += 1
    return payload


def current_side(state: chess.Board):
    return "White" if state.turn == chess.WHITE else "Black"


def current_model(state: chess.Board):
    return MODEL_WHITE if state.turn == chess.WHITE else MODEL_BLACK


def short_text(s, n=140):
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def clear_analysis():
    hc.write(
        "root/app",
        analysis_title="Idle",
        analysis_choice="",
        candidate_1="",
        candidate_2="",
        candidate_3="",
        analysis_thinking="Thinking disabled for compatibility mode.",
        analysis_content="",
    )


def render_candidates(candidates):
    lines = []
    for i, c in enumerate(candidates[:3], start=1):
        move = c.get("move", "?")
        score = c.get("score", "?")
        why = c.get("why", "")
        lines.append(f"{i}. {move} · {score}/100 · {short_text(why, 60)}")
    while len(lines) < 3:
        lines.append("")
    return lines[0], lines[1], lines[2]


def write_ui(status_override=None):
    if status_override is not None:
        status = status_override
    elif winner == "Draw":
        status = f"Game {game_no} ended in a draw. Next game queued."
    elif winner in ("White", "Black"):
        model = MODEL_WHITE if winner == "White" else MODEL_BLACK
        status = f"Game {game_no} won by {winner} ({model}). Next game queued."
    elif auto_running:
        status = f"Game {game_no} · {current_side(board)} to move · {current_model(board)}"
    else:
        status = f"Paused on game {game_no} · {current_side(board)} to move"

    payload = {
        "status": status,
        "toggle_label": "Pause" if auto_running else "Resume",
        "game_label": f"Game {game_no}",
        "last_move": last_move,
        "white_model": MODEL_WHITE,
        "black_model": MODEL_BLACK,
        "white_score": str(white_score),
        "black_score": str(black_score),
        "draw_score": str(draw_score),
        "completed_games": str(completed_games),
    }
    payload.update(board_cells_payload(board))
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
    global board, winner, game_no, completed_games
    global white_score, black_score, draw_score, last_move, next_move_at, restart_at

    if reset_scores:
        completed_games = 0
        white_score = 0
        black_score = 0
        draw_score = 0
        game_no = 0

    clear_logs()
    board = chess.Board()
    winner = None
    last_move = "—"
    restart_at = 0.0
    game_no += 1
    next_move_at = time.time() + TURN_DELAY

    add_log(
        "system",
        f"Started game {game_no}. White = {MODEL_WHITE} · Black = {MODEL_BLACK}",
        "#38bdf8"
    )
    clear_analysis()
    write_ui()


def strip_code_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def extract_json_object(text: str):
    t = strip_code_fences(text)
    try:
        return json.loads(t)
    except Exception:
        pass

    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        chunk = t[start:end + 1]
        try:
            return json.loads(chunk)
        except Exception:
            return None
    return None


def parse_decision_json(text, legal):
    obj = extract_json_object(text)
    if not isinstance(obj, dict):
        return None, []

    choice = str(obj.get("choice", "")).strip().lower()
    raw_candidates = obj.get("candidates", []) or []

    parsed = []
    for item in raw_candidates[:3]:
        if not isinstance(item, dict):
            continue

        move = str(item.get("move", "")).strip().lower()
        why = str(item.get("why", "")).strip()
        score = item.get("score", 0)

        try:
            score = int(score)
        except Exception:
            score = 0

        if move in legal:
            parsed.append({
                "move": move,
                "score": max(0, min(100, score)),
                "why": why,
            })

    if choice not in legal:
        choice = None

    return choice, parsed


def build_prompt(side, state: chess.Board, invalid_reply=None):
    legal = legal_moves(state)

    system_text = (
        "You are a chess move engine.\n"
        "Return JSON only.\n"
        'Format exactly like this: {"choice":"e2e4","candidates":[{"move":"e2e4","score":83,"why":"controls the center"}]}\n'
        "Rules:\n"
        "- choice must be one legal UCI move\n"
        "- candidates must contain up to 3 legal UCI moves\n"
        "- score must be an integer from 0 to 100\n"
        "- why must be short\n"
        "- do not output any text before or after the JSON"
    )

    user_text = (
        f"Player: {side}\n"
        f"FEN: {state.fen()}\n"
        f"Legal moves: {legal}\n"
        'Reply with one JSON object only.'
    )

    if invalid_reply is not None:
        user_text += (
            f"\nPrevious reply was invalid: {invalid_reply!r}\n"
            "Try again and return only valid JSON."
        )

    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]


def ollama_stream_decision(model, side, state: chess.Board, invalid_reply=None):
    content = ""
    last_flush = 0.0
    legal = legal_moves(state)

    payload = {
        "model": model,
        "messages": build_prompt(side, state, invalid_reply=invalid_reply),
        "stream": True,
        "options": {"temperature": 0.2},
        "keep_alive": "10m",
    }

    with requests.post(
        OLLAMA_CHAT_URL,
        json=payload,
        timeout=REQUEST_TIMEOUT,
        stream=True,
    ) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"Ollama {resp.status_code}: {resp.text}")

        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue

            try:
                chunk = json.loads(line)
            except Exception:
                continue

            msg = chunk.get("message", {}) or {}
            piece_content = msg.get("content") or ""
            if piece_content:
                content += piece_content

            now = time.time()
            if now - last_flush >= 0.08:
                choice, candidates = parse_decision_json(content, legal)
                c1, c2, c3 = render_candidates(candidates)
                hc.write(
                    "root/app",
                    analysis_title=f"{side} · {model} is deciding…",
                    analysis_choice=f"Chosen move: {choice if choice else 'parsing...'}",
                    candidate_1=c1,
                    candidate_2=c2,
                    candidate_3=c3,
                    analysis_thinking="Thinking disabled for compatibility mode.",
                    analysis_content=content[-2400:],
                )
                last_flush = now

    return content


def fallback_move(state: chess.Board):
    legal = list(state.legal_moves)
    if not legal:
        return None

    for move in legal:
        state.push(move)
        mate = state.is_checkmate()
        state.pop()
        if mate:
            return move.uci()

    for move in legal:
        if state.is_capture(move):
            return move.uci()

    for move in legal:
        if state.gives_check(move):
            return move.uci()

    for move in legal:
        if move.promotion is not None:
            return move.uci()

    return legal[0].uci()


def choose_move(model, side, state: chess.Board):
    legal = legal_moves(state)
    invalid_reply = None

    for _ in range(3):
        raw_json = ollama_stream_decision(
            model=model,
            side=side,
            state=state,
            invalid_reply=invalid_reply,
        )

        choice, candidates = parse_decision_json(raw_json, legal)
        c1, c2, c3 = render_candidates(candidates)

        hc.write(
            "root/app",
            analysis_title=f"{side} · {model}",
            analysis_choice=f"Chosen move: {choice if choice else 'invalid'}",
            candidate_1=c1,
            candidate_2=c2,
            candidate_3=c3,
            analysis_thinking="Thinking disabled for compatibility mode.",
            analysis_content=raw_json[-2400:],
        )

        if choice in legal:
            return choice, raw_json, candidates, False

        invalid_reply = raw_json or "<empty>"

    move = fallback_move(state)
    hc.write(
        "root/app",
        analysis_title=f"{side} · {model}",
        analysis_choice=f"Fallback move used: {move if move else 'none'}",
        candidate_1="",
        candidate_2="",
        candidate_3="",
        analysis_content=(invalid_reply or "")[-2400:],
    )
    return move, invalid_reply, [], True


def apply_move():
    global board, winner, last_move, next_move_at, restart_at
    global white_score, black_score, draw_score, completed_games

    side = current_side(board)
    model = current_model(board)

    write_ui(f"Game {game_no} · {side} is choosing · {model}")

    try:
        move_uci, raw_json, candidates, used_fallback = choose_move(model, side, board)
    except Exception as e:
        move_uci = fallback_move(board)
        raw_json = str(e)
        candidates = []
        used_fallback = True
        hc.write(
            "root/app",
            analysis_title=f"{side} · {model}",
            analysis_choice="Request failed, using fallback",
            candidate_1="",
            candidate_2="",
            candidate_3="",
            analysis_thinking="Thinking disabled for compatibility mode.",
            analysis_content=raw_json[-2400:],
        )

    if move_uci is None:
        winner = "Draw"
    else:
        move = chess.Move.from_uci(move_uci)
        san = board.san(move)
        board.push(move)
        last_move = san

        candidate_summary = []
        for c in candidates[:3]:
            candidate_summary.append(f"{c['move']}={c['score']}")
        candidate_text = ", ".join(candidate_summary) if candidate_summary else "no parsed candidates"

        if used_fallback:
            add_log(
                f"{side} · {model}",
                f"played {san} ({move_uci}) via fallback\nmodel output: {short_text(raw_json, 260)}",
                "#f59e0b",
            )
        else:
            add_log(
                f"{side} · {model}",
                f"played {san} ({move_uci})\nself-scores: {candidate_text}",
                "#22c55e" if side == "White" else "#ef4444",
            )

        winner = check_winner(board)

    if winner == "White":
        white_score += 1
        completed_games += 1
        add_log("result", f"Game {game_no} winner: White ({MODEL_WHITE})", "#10b981")
        restart_at = time.time() + RESTART_DELAY

    elif winner == "Black":
        black_score += 1
        completed_games += 1
        add_log("result", f"Game {game_no} winner: Black ({MODEL_BLACK})", "#ef4444")
        restart_at = time.time() + RESTART_DELAY

    elif winner == "Draw":
        draw_score += 1
        completed_games += 1
        add_log("result", f"Game {game_no} ended in a draw", "#94a3b8")
        restart_at = time.time() + RESTART_DELAY

    else:
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