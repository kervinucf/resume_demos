#!/usr/bin/env python3
import json
import time
from HyperCoreSDK import HyperClient

hc = HyperClient(relay="http://localhost:8766", root="demo_tictactoe")
hc.start_relay()
hc.clear()

# 1. Strictly inline styles. No <style> blocks that get stripped.
# 9 explicit buttons with simple IDs to match the demo's getElementById JS pattern.
HTML = """
<div style="width:100%;height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;background:#111;color:#eee;font-family:sans-serif">
  <div data-bind-text="status" style="font-size:24px;margin-bottom:20px;height:30px"></div>

  <div style="display:grid;grid-template-columns:100px 100px 100px;gap:5px;background:#333;padding:5px;border-radius:5px">
    <button id="c0" data-bind-text="v0" style="width:100px;height:100px;font-size:40px;background:#222;color:#fff;border:none;cursor:pointer"></button>
    <button id="c1" data-bind-text="v1" style="width:100px;height:100px;font-size:40px;background:#222;color:#fff;border:none;cursor:pointer"></button>
    <button id="c2" data-bind-text="v2" style="width:100px;height:100px;font-size:40px;background:#222;color:#fff;border:none;cursor:pointer"></button>
    <button id="c3" data-bind-text="v3" style="width:100px;height:100px;font-size:40px;background:#222;color:#fff;border:none;cursor:pointer"></button>
    <button id="c4" data-bind-text="v4" style="width:100px;height:100px;font-size:40px;background:#222;color:#fff;border:none;cursor:pointer"></button>
    <button id="c5" data-bind-text="v5" style="width:100px;height:100px;font-size:40px;background:#222;color:#fff;border:none;cursor:pointer"></button>
    <button id="c6" data-bind-text="v6" style="width:100px;height:100px;font-size:40px;background:#222;color:#fff;border:none;cursor:pointer"></button>
    <button id="c7" data-bind-text="v7" style="width:100px;height:100px;font-size:40px;background:#222;color:#fff;border:none;cursor:pointer"></button>
    <button id="c8" data-bind-text="v8" style="width:100px;height:100px;font-size:40px;background:#222;color:#fff;border:none;cursor:pointer"></button>
  </div>

  <button id="reset" style="margin-top:20px;padding:10px 20px;font-size:16px;cursor:pointer;background:#444;color:#fff;border:none;border-radius:4px">Reset</button>
</div>
"""

# 2. Pure getElementById and .onclick assignments, mirroring the chat/slider demos.
JS = r"""
(function(){
  const r = document.getElementById("reset");
  if (!r || r.dataset.on) return;
  r.dataset.on = 1;

  const send = (idx) => {
    window.$scene.get("inbox/" + Date.now() + "_" + Math.random().toString(36).slice(2,7)).put({
      data: JSON.stringify({ move: idx })
    });
  };

  for(let i=0; i<9; i++) {
    const btn = document.getElementById("c"+i);
    if(btn) btn.onclick = () => send(i);
  }

  r.onclick = () => send(-1);
})();
"""

hc.mount("root/game", html=HTML, js=JS, fixed=True, layer=10)

board = [""] * 9
turn = "X"
winner = None


def check_win():
    lines = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]
    for a, b, c in lines:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    return "Draw" if "" not in board else None


def update_ui():
    if winner == "Draw":
        stat = "Draw!"
    elif winner:
        stat = f"{winner} Wins!"
    else:
        stat = f"{turn}'s Turn"

    # Push all 10 bound text values simultaneously
    hc.write("root/game", status=stat, v0=board[0], v1=board[1], v2=board[2], v3=board[3], v4=board[4], v5=board[5],
             v6=board[6], v7=board[7], v8=board[8])


update_ui()

seen = set()

while True:
    snap = hc.snapshot() or {}
    for k, v in snap.items():
        if not k.startswith("inbox/") or k in seen:
            continue
        seen.add(k)

        try:
            raw = v.get("data")
            msg = raw if isinstance(raw, dict) else json.loads(raw)
            move = msg.get("move")

            if move == -1:
                board = [""] * 9
                turn = "X"
                winner = None
                update_ui()
            elif move is not None and not winner:
                if 0 <= move <= 8 and board[move] == "":
                    board[move] = turn
                    winner = check_win()
                    if not winner:
                        turn = "O" if turn == "X" else "X"
                    update_ui()

        except Exception:
            pass

        # Actively delete processed inbox items to prevent snapshot bloat
        hc.remove(k)

    time.sleep(0.1)