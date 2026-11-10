"""Print what is on the event bus. Usage: python scripts/listen.py ['BMS/v1/#'] [mqtt://localhost:1883]

Flex messages carry a signed payload; this decodes it for reading without verifying
the signature (a real consumer must verify).
"""

import base64
import json
import sys
from urllib.parse import urlparse

import paho.mqtt.client as mqtt

topic = sys.argv[1] if len(sys.argv) > 1 else "#"
url = urlparse(sys.argv[2] if len(sys.argv) > 2 else "mqtt://localhost:1883")


def on_connect(client, _userdata, _flags, reason_code, _props):
    print(f"connected ({reason_code}); subscribing to {topic}", flush=True)
    client.subscribe(topic)


def on_message(_client, _userdata, msg):
    body = json.loads(msg.payload)
    if isinstance(body.get("data"), str) and body["data"].count(".") == 2:
        segment = body["data"].split(".")[1]
        body["data"] = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    print(msg.topic, json.dumps(body), flush=True)


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.on_message = on_message
client.connect(url.hostname or "localhost", url.port or 1883)
client.loop_forever()
