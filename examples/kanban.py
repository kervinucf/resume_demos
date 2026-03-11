#!/usr/bin/env python3
import time
import json
import uuid
from HyperCoreSDK import HyperClient

hc = HyperClient(relay="http://localhost:8765", root="demo_kanban")
hc.start_relay()
hc.clear()

# --- 1. Parent Board Layout ---
BOARD_HTML = """
<div style="height:100vh; background:#0f172a; color:#e2e8f0; font-family:sans-serif; display:flex; flex-direction:column;">
    <div style="padding:20px; background:#1e293b; border-bottom:1px solid #334155; display:flex; gap:10px; align-items:center;">
        <div style="font-weight:900; font-size:20px; letter-spacing:1px; color:#38bdf8; margin-right:20px;">AGENT OPS</div>
        <input id="new_task" placeholder="Enter new task..." style="padding:10px 15px; border-radius:6px; border:1px solid #475569; background:#0f172a; color:#fff; flex:1; outline:none; font-size:14px;">
        <button id="add_btn" style="padding:10px 24px; background:#0ea5e9; border:none; border-radius:6px; color:#fff; cursor:pointer; font-weight:bold; transition:background 0.2s;">Deploy Task</button>
    </div>
    <div data-children style="flex:1; display:flex; padding:20px; gap:20px; overflow-x:auto;"></div>
</div>
"""

# The Global Bridge: We expose window.sendAction so dynamic task buttons can trigger events
# without complex local event delegation, while maintaining strict JSON serialization.
BOARD_JS = r"""
(function(){
    const addBtn = document.getElementById("add_btn");
    const input = document.getElementById("new_task");
    if (!addBtn || !input || addBtn.dataset.on) return;
    addBtn.dataset.on = 1;

    // Global bridge for dynamic children
    window.sendAction = (type, taskId = null) => {
        let payload = { type: type, timestamp: Date.now() };

        if (type === 'add') {
            const text = input.value.trim();
            if (!text) return;
            payload.text = text;
            input.value = '';
            input.focus();
        } else {
            payload.taskId = taskId;
        }

        const path = "inbox/" + Date.now() + "_" + Math.random().toString(36).slice(2,7);
        // Strict serialization prevents graph shredding
        window.$scene.get(path).put({
            data: JSON.stringify(payload)
        });
    };

    addBtn.onclick = () => window.sendAction('add');
    input.addEventListener('keydown', e => e.key === 'Enter' && window.sendAction('add'));
})();
"""

# --- 2. Column Template ---
# Each column acts as a sub-parent, housing its own data-children drop zone.
COL_HTML = """
<div style="flex:1; min-width:320px; max-width:400px; background:#1e293b; border-radius:8px; display:flex; flex-direction:column; border:1px solid #334155; box-shadow:0 4px 6px rgba(0,0,0,0.1);">
    <div style="padding:15px 20px; border-bottom:1px solid #334155; display:flex; justify-content:space-between; align-items:center;">
        <span data-bind-text="col_title" style="font-weight:bold; font-size:14px; color:#94a3b8; text-transform:uppercase; letter-spacing:1px;"></span>
        <span data-bind-text="task_count" style="background:#0f172a; padding:2px 8px; border-radius:12px; font-size:12px; color:#38bdf8;">0</span>
    </div>
    <div data-children style="flex:1; padding:15px; display:flex; flex-direction:column; gap:12px; overflow-y:auto;"></div>
</div>
"""

# --- Initial Mount Sequence ---
hc.mount("root/board", html=BOARD_HTML, js=BOARD_JS, fixed=True, layer=10)

columns = ["todo", "doing", "done"]
for col in columns:
    hc.mount(f"root/board/col_{col}", html=COL_HTML, layer=5)
    hc.write(f"root/board/col_{col}", col_title=col, task_count="0")

# --- App State ---
tasks = {}
seen = set()


def mount_task(task_id: str, text: str, status: str):
    """
    Dynamically generates the HTML for a single task, baking the ID directly into
    the inline event handlers to guarantee accurate click targeting.
    """
    border_color = '#ef4444' if status == 'todo' else '#f59e0b' if status == 'doing' else '#22c55e'

    html = f"""
    <div style="background:#334155; padding:16px; border-radius:6px; border-left:4px solid {border_color}; display:flex; flex-direction:column; gap:12px; box-shadow:0 2px 4px rgba(0,0,0,0.2);">
        <div data-bind-text="text" style="font-size:14px; line-height:1.5; color:#f8fafc; word-break:break-word;"></div>
        <div style="display:flex; justify-content:space-between; align-items:center; border-top:1px solid #475569; padding-top:12px; margin-top:4px;">
            <button onclick="window.sendAction('delete', '{task_id}')" style="background:transparent; color:#ef4444; border:1px solid #ef4444; padding:4px 10px; border-radius:4px; cursor:pointer; font-size:12px; font-weight:bold;">Drop</button>
            <div style="display:flex; gap:6px;">
    """

    if status != "todo":
        html += f"""<button onclick="window.sendAction('move_left', '{task_id}')" style="background:#475569; color:#f8fafc; border:none; padding:4px 12px; border-radius:4px; cursor:pointer; font-size:12px; font-weight:bold;">&larr;</button>"""
    if status != "done":
        html += f"""<button onclick="window.sendAction('move_right', '{task_id}')" style="background:#0ea5e9; color:#f8fafc; border:none; padding:4px 12px; border-radius:4px; cursor:pointer; font-size:12px; font-weight:bold;">&rarr;</button>"""

    html += """
            </div>
        </div>
    </div>
    """

    path = f"root/board/col_{status}/{task_id}"
    hc.mount(path, html=html, layer=10)
    hc.write(path, text=text)


def update_counts():
    for col in columns:
        count = sum(1 for t in tasks.values() if t['status'] == col)
        hc.write(f"root/board/col_{col}", task_count=str(count))


# --- Main Event Loop ---
while True:
    snap = hc.snapshot() or {}

    for k, v in snap.items():
        if not k.startswith("inbox/") or k in seen:
            continue

        seen.add(k)

        try:
            # Safely handle the payload whether it arrives as a string or pre-parsed dictionary
            raw_data = v.get("data", {})
            msg = raw_data if isinstance(raw_data, dict) else json.loads(raw_data)
            action = msg.get("type")

            if action == "add":
                task_id = f"task_{uuid.uuid4().hex[:8]}"
                tasks[task_id] = {"text": msg.get("text"), "status": "todo"}
                mount_task(task_id, tasks[task_id]["text"], "todo")
                update_counts()

            elif action in ["move_left", "move_right", "delete"]:
                task_id = msg.get("taskId")
                if task_id in tasks:
                    old_status = tasks[task_id]["status"]
                    old_path = f"root/board/col_{old_status}/{task_id}"

                    # Graph Cleanup: Purge the old node completely
                    hc.remove(old_path)

                    if action == "delete":
                        del tasks[task_id]
                    else:
                        # State mutation
                        idx = columns.index(old_status)
                        new_idx = idx - 1 if action == "move_left" else idx + 1
                        new_status = columns[max(0, min(2, new_idx))]

                        tasks[task_id]["status"] = new_status
                        # Remount in the new graph location
                        mount_task(task_id, tasks[task_id]["text"], new_status)

                    update_counts()

        except Exception as e:
            print(f"Server execution error: {e}")

        # Aggressive memory cleanup: nuke the action to protect the relay
        hc.remove(k)

    # Hard CPU throttle
    time.sleep(0.1)