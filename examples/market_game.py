#!/usr/bin/env python3
import time
import random
import json
from HyperCoreSDK.client import HyperClient

hc = HyperClient(relay="http://localhost:8765", root="demo_market")
hc.start_relay()
hc.clear()

# --- 1. Parent Template ---
PARENT_HTML = """
<div style="width:100%;height:100%;display:flex;flex-direction:column;background:#111;color:#eee;font-family:sans-serif">

  <div style="padding:20px;background:#222;border-bottom:1px solid #333;display:flex;justify-content:space-between;align-items:center">
    <div>
      <div style="font-size:12px;color:#888;text-transform:uppercase">Market Price</div>
      <div data-bind-text="price" style="font-size:32px;font-weight:bold;color:#3b82f6">$100.00</div>
    </div>
    <div style="text-align:right">
      <div style="font-size:12px;color:#888;text-transform:uppercase">Portfolio</div>
      <div style="font-size:18px"><span data-bind-text="shares">0</span> Shares</div>
      <div style="font-size:18px;color:#10b981">$<span data-bind-text="cash">1000.00</span></div>
    </div>
  </div>

  <div style="padding:15px;display:flex;gap:10px;border-bottom:1px solid #333">
    <button id="btn_buy" style="flex:1;padding:12px;background:#10b981;color:#fff;border:none;border-radius:4px;cursor:pointer;font-weight:bold">Buy 1 Share</button>
    <button id="btn_sell" style="flex:1;padding:12px;background:#ef4444;color:#fff;border:none;border-radius:4px;cursor:pointer;font-weight:bold">Sell 1 Share</button>
    <button id="btn_clear" style="padding:12px;background:#444;color:#fff;border:none;border-radius:4px;cursor:pointer">Clear Log</button>
  </div>

  <div data-children style="flex:1;overflow:auto;display:flex;flex-direction:column;gap:5px;padding:15px"></div>
</div>
"""

PARENT_JS = r"""
(function(){
  const buy = document.getElementById("btn_buy");
  const sell = document.getElementById("btn_sell");
  const clear = document.getElementById("btn_clear");

  if (!buy || !sell || !clear || buy.dataset.on) return;
  buy.dataset.on = 1;

  const send = (actionType) => {
    const path = "inbox/" + Date.now() + "_" + Math.random().toString(36).slice(2,7);
    window.$scene.get(path).put({
      // CRITICAL FIX: Stringify the payload to prevent GunDB from converting it to a detached graph node
      data: JSON.stringify({ type: actionType, timestamp: Date.now() }) 
    });
  };

  buy.onclick = () => send("buy");
  sell.onclick = () => send("sell");
  clear.onclick = () => send("clear");
})();
"""

# --- 2. Child Template ---
CHILD_HTML = """
<div data-bind-style="borderLeft:border_color" style="padding:10px;background:#222;border-left:4px solid #444;border-radius:4px;display:flex;justify-content:space-between;align-items:center">
  <span data-bind-text="log_action" style="font-weight:bold;font-size:14px"></span>
  <span data-bind-text="log_details" style="color:#aaa;font-size:12px"></span>
</div>
"""

# --- 3. Initial Mount ---
hc.mount("root/app", html=PARENT_HTML, js=PARENT_JS, fixed=True, layer=10)

# --- 4. Event Loop ---
price = 100.00
shares = 0
cash = 1000.00
log_count = 0
seen = set()

last_tick = time.time()
hc.write("root/app", price=f"${price:.2f}", shares=shares, cash=f"{cash:.2f}")

while True:
    snap = hc.snapshot() or {}

    # Independent Market Simulation
    current_time = time.time()
    if current_time - last_tick > 2.0:
        price = max(1.0, price + random.uniform(-5.0, 5.5))
        hc.write("root/app", price=f"${price:.2f}")
        last_tick = current_time

    # Process User Actions
    for k, v in snap.items():
        if not k.startswith("inbox/") or k in seen:
            continue

        seen.add(k)

        try:
            # CRITICAL FIX: Safely parse the stringified JSON payload
            raw_data = v.get("data", {})
            msg = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
            action_type = msg.get("type")

            if action_type in ("buy", "sell"):
                if action_type == "buy" and cash >= price:
                    shares += 1
                    cash -= price
                    action_text = "BUY EXECUTED"
                    b_color = "4px solid #10b981"
                elif action_type == "sell" and shares > 0:
                    shares -= 1
                    cash += price
                    action_text = "SELL EXECUTED"
                    b_color = "4px solid #ef4444"
                else:
                    action_text = "ORDER REJECTED"
                    b_color = "4px solid #f59e0b"

                log_count += 1
                child_path = f"root/app/log_{log_count}"

                hc.mount(child_path, html=CHILD_HTML)
                hc.write(
                    child_path,
                    log_action=action_text,
                    log_details=f"@ ${price:.2f}",
                    border_color=b_color
                )
                hc.write("root/app", shares=shares, cash=f"{cash:.2f}")

            elif action_type == "clear":
                log_count = 0
                hc.remove("root/app")
                hc.mount("root/app", html=PARENT_HTML, js=PARENT_JS, fixed=True, layer=10)
                hc.write("root/app", price=f"${price:.2f}", shares=shares, cash=f"{cash:.2f}")

        except Exception as e:
            print(f"Error processing event: {e}")

        # Clean up to protect server memory
        hc.remove(k)

    # Throttle to protect CPU
    time.sleep(0.1)