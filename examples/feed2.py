#!/usr/bin/env python3

import argparse
import html
import json
import random
import time
from collections import defaultdict

from HyperCoreSDK.client import HyperClient

hc = HyperClient(root="demo3", discovery="lan", port=8766)
hc.connect()

for evt in hc.subscribe("data.weather.nyc.temp"):
    print(evt["event"], evt["kind"], evt["data"])