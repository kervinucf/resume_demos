#!/usr/bin/env python3
import time
from HyperCoreSDK.client import HyperClient
from HyperCoreSDK.ui import page, grid, cell, btn, text

hc = HyperClient(root="tictactoe", port=8765)
hc.connect()
hc.clear()

BOARD = page(
    text("status", "font-size:24px;margin-bottom:20px"),
    grid(3, [cell(f"v{i}", "move", cell=i, style="width:100px;height:100px;font-size:40px") for i in range(9)],
         style="background:#333;padding:5px;border-radius:5px"),
    btn("Reset", "reset", bg="#444"),
    bg="#111",
)

hc.mount("root/game", html=BOARD, fixed=True, layer=10)

board = [""] * 9
turn = "X"
winner = None
LINES = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]

def check_win():
    for a, b, c in LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    return "Draw" if "" not in board else None

def update_ui():
    status = "Draw!" if winner == "Draw" else f"{winner} Wins!" if winner else f"{turn}'s Turn"
    hc.write("root/game", status=status, **{f"v{i}": board[i] for i in range(9)})

update_ui()

while True:
    for act in hc.actions():
        if act.name == "reset":
            board = [""] * 9
            turn = "X"
            winner = None
        elif act.name == "move" and not winner:
            c = act.get("cell")
            if c is not None and 0 <= c <= 8 and board[c] == "":
                board[c] = turn
                winner = check_win()
                if not winner:
                    turn = "O" if turn == "X" else "X"
        update_ui()
    time.sleep(0.1)