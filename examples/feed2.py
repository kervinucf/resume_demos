from HyperCoreSDK.client import HyperClient

hc = HyperClient(root="demo3", discovery="lan", port=8766)
hc.connect()

hc.at("weather.nyc").write(data={
    "temp": 72,
    "condition": "Sunny",
})

hc.at("weather.nyc.panel").write(html="""
<div class="card">
  <h1 data-bind-text="temp"></h1>
  <p data-bind-text="condition"></p>
</div>
""")

print(hc.at("weather.nyc").read())
print(hc.at("weather.nyc.panel").stream_url())

for evt in hc.at("weather.nyc").stream():
    print(evt)