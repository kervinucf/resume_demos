HyperCoreSDK: Comprehensive Component Authoring Guide for AI Agents
This guide defines the required architecture and constraints for building interactive components using the HyperCoreSDK framework. You must adhere to these rules strictly to ensure components render correctly, capture events reliably, and perform efficiently without abusing the server.

1. Architectural Core Principles
Thin Client, Thick Server: The browser (HTML/JS) handles zero business logic. It strictly captures user input, serializes it, and renders state. Python maintains the absolute source of truth.

Unidirectional Flow:

User interacts with the DOM.

JavaScript pushes a strictly stringified action payload to the Python $scene.

Python's while True loop detects the action, mutates state, and deletes the action.

Python pushes the new state down to the UI via bound variables.

2. UI Mounting & DOM Structure
Do not mount static components repeatedly in a loop. Define the static layout once and mount it once before the event loop starts.

HTML Structure: Use strictly inline styles (style="..."). Avoid <style> blocks entirely, as the renderer may strip them out, breaking layouts.

Targeting: Give interactive elements explicit, simple id attributes (e.g., id="btn_submit").

The Layer Rule (CRITICAL): The framework uses the layer argument to control both CSS z-index and interactivity. If you mount a component without specifying a layer (or layer=0), the renderer applies pointer-events: none to the host node. To ensure your component can register clicks and inputs, you must pass a layer greater than 0.

Python
hc.mount("root/my_app", html=HTML_TEMPLATE, js=JS_TEMPLATE, fixed=True, layer=10)
3. Dynamic Layouts & data-children
When you need to render lists of varying lengths (like chat messages or dynamic Kanban tasks), use the data-children attribute.

The Parent Container: Include a div with the data-children attribute in the parent's HTML. This acts as the mount target for dynamic sub-components.

Interactive Children (The Dead-Click Trap): When mounting children dynamically via Python, they inherit the layer=0 default. If your dynamic child contains buttons or inputs, you must pass layer=5 (or any integer > 0) during the hc.mount() call, or the browser will silently ignore all user interactions.

Clearing Children: To reset a dynamic list, remove the specific child directories using hc.remove().

4. Data Binding & Batch Writes
Update the UI by changing bound variables, not by manipulating the DOM in JavaScript.

HTML Setup: Use data-bind-text="var_name" for text injection and data-bind-style="color:var_color" for dynamic styling.

Batch Python Execution: Consolidate your state updates. Do not execute multiple consecutive hc.write() calls for the same component. Package the entire UI state into a single dictionary and write it all at once to minimize network overhead.

5. JavaScript & Event Handling (CRITICAL)
JavaScript's only job is to listen for events and send payloads to Python.

Strict Serialization (The GunDB Rule): You must wrap your payload in JSON.stringify(). If you pass a raw nested JavaScript object to GunDB's .put(), it will shred the object into a detached relational graph node, and your events will silently drop into the void.

Idempotent Initialization: Always use a dataset flag (dataset.on = 1) to ensure event listeners are not attached multiple times.

The Global Bridge for Dynamic Children: Because you cannot easily attach unique JavaScript event listeners to dynamic children as they are mounted, attach a global function to the window object in the parent's JavaScript. Dynamic children can then use standard inline onclick="window.sendAction('action', 'id')" handlers to fire serialized events back to Python.

6. The Python Event Loop & Server Protection
The Python script runs a continuous while True: loop. You must unpack data correctly and be relentless about resource management.

Parsing the Stringified Payload: Because the frontend is forced to stringify the payload, the backend must safely parse it using json.loads() (or gracefully handle if the relay already parsed it).

Aggressive Cleanup: To ensure you do not abuse the server, you must aggressively delete processed actions using hc.remove(key). Allowing the snapshot to bloat will degrade performance rapidly.

Mandatory Throttling: Always include a time.sleep() (e.g., 0.05 or 0.1 seconds) at the bottom of the event loop to protect CPU resources.

7. Complete Boilerplate Template
Use this bulletproofed template as the starting point for any component that requires capturing user input and generating interactive dynamic lists.

Python
#!/usr/bin/env python3
import time
import json
import uuid
from HyperCoreSDK import HyperClient

hc = HyperClient(relay="http://localhost:8765", root="demo_app")
hc.start_relay()
hc.clear()

# --- 1. Parent Template ---
PARENT_HTML = """
<div style="width:100%;height:100%;display:flex;flex-direction:column;background:#111;color:#eee;font-family:sans-serif">
  <div style="padding:10px;border-bottom:1px solid #333">
    <button id="add_btn" style="padding:10px;background:#3b82f6;color:#fff;border:none;border-radius:4px;cursor:pointer">Add Interactive Item</button>
  </div>
  <div data-children style="flex:1;overflow:auto;display:flex;flex-direction:column;gap:5px;padding:10px"></div>
</div>
"""

PARENT_JS = r"""
(function(){
  const addBtn = document.getElementById("add_btn");
  if (!addBtn || addBtn.dataset.on) return;
  addBtn.dataset.on = 1;

  // Global bridge allowing dynamically generated children to safely trigger events
  window.sendAction = (actionType, itemId = null) => {
    const path = "inbox/" + Date.now() + "_" + Math.random().toString(36).slice(2,7);
    window.$scene.get(path).put({
      // CRITICAL: Strictly serialize the payload to prevent GunDB graph shredding
      data: JSON.stringify({ type: actionType, id: itemId, timestamp: Date.now() }) 
    });
  };

  addBtn.onclick = () => window.sendAction("add");
})();
"""

# --- 2. Child Template ---
# Notice the inline onclick calling the global bridge established in the parent
CHILD_HTML = """
<div style="padding:10px;background:#222;border:1px solid #444;border-radius:4px;display:flex;justify-content:space-between;">
  <span data-bind-text="item_text" style="font-size:14px"></span>
  <button onclick="window.sendAction('delete', this.dataset.id)" data-bind-text="btn_id" data-id="" style="padding:5px;background:#ef4444;color:#fff;border:none;border-radius:4px;cursor:pointer">Drop</button>
</div>
"""

# --- 3. Initial Mount ---
hc.mount("root/app", html=PARENT_HTML, js=PARENT_JS, fixed=True, layer=10)

# --- 4. Event Loop ---
seen = set()

while True:
    snap = hc.snapshot() or {}
    
    for k, v in snap.items():
        if not k.startswith("inbox/") or k in seen:
            continue
            
        seen.add(k)
        
        try:
            # CRITICAL: Safely parse the stringified JSON payload
            raw_data = v.get("data", {})
            msg = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
            action_type = msg.get("type")
            
            if action_type == "add":
                item_id = f"item_{uuid.uuid4().hex[:6]}"
                child_path = f"root/app/{item_id}"
                
                # CRITICAL: layer=5 must be passed so the child button receives pointer events!
                hc.mount(child_path, html=CHILD_HTML.replace('data-id=""', f'data-id="{item_id}"'), layer=5)
                hc.write(child_path, item_text=f"Dynamic Item {item_id}", btn_id="Drop")
                
            elif action_type == "delete":
                target_id = msg.get("id")
                if target_id:
                    hc.remove(f"root/app/{target_id}")
                
        except Exception as e:
            print(f"Error processing event: {e}")
            
        # CRITICAL: Clean up processed items to prevent server bloat
        hc.remove(k)

    # CRITICAL: Throttle the loop to prevent server abuse
    time.sleep(0.1)

# --- GET STARTED ---

1. git clone https://github.com/kervinucf/resume_demos.git
2. python3 -m venv venv    
3. pip install -r requirements.txt   
4. source ./venv/bin/activate 
