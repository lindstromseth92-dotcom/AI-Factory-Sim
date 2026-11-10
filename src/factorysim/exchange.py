"""Publish the simulated site onto a DSX Exchange style event bus.

DSX Exchange is NVIDIA's open-source IT/OT integration hub: a NATS-based bus that
speaks MQTT, with AsyncAPI schemas for what goes on it. This module makes the sim
talk like the two kinds of participant those schemas describe:

  BMS Event Bus   the building management system publishing facility points
                  (rack power, coolant temperatures, leak detectors...), and an IT
                  "integration" publishing requests back (setpoints, isolation).
                  Topics: BMS/v1/PUB/{Value|Metadata}/{objectType}/{pointType}/{tagPath}
                          BMS/v1/{integration}/Value/{objectType}/{pointType}/{tagPath}
                  Value payload: {"value", "timestamp" (epoch ms), "quality"}

  DSX Flex        grid load targets, power state, breach alerts and enforcement,
                  as CloudEvents 1.0.2 whose `data` is a JWS-signed JSON payload.

What follows the published schema (docs.nvidia.com/dsx-exchange/schema): BMS topic
patterns, object and point types, value and metadata fields, the Flex event types,
CloudEvents envelope, source URIs, payload fields and lifecycle states.

What I could not confirm and had to choose, marked INFERRED below: the Flex MQTT
topic names, the shape of items in SetLoadTargetData.targets, and the status event
names other than target_set and start_ramp_down.

With no broker configured the publisher runs dry: it builds every message and keeps
recent ones in memory for the dashboard, but sends nothing.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import uuid
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from .engine import ISOLATED, RACKS_PER_ROW, Engine

REPUBLISH_S = 100.0  # values and metadata are republished this often even if unchanged
HEARTBEAT_S = 10.0
FEED_TAG = "utility-main"
NEVER = float("-inf")  # timers start here so the first tick always publishes, whatever the clock reads

# INFERRED: the Flex schema pages list event types but not topic strings.
FLEX_TOPICS = {
    "grid.loadtarget.set.v1": "flex/v1/loadtarget/set",
    "grid.powerstate.status.v1": "flex/v1/powerstate/status",
    "grid.powerbreach.alert.v1": "flex/v1/powerbreach/alert",
    "grid.powerbreach.enforcement.v1": "flex/v1/powerbreach/enforcement",
}
FLEX_SUBJECTS = {
    "grid.loadtarget.set.v1": "loadtarget",
    "grid.powerstate.status.v1": "powerstate",
    "grid.powerbreach.alert.v1": "powerbreach",
    "grid.powerbreach.enforcement.v1": "powerbreach",
}

LEAK_TEXT = [{"value": 0, "text": "NoLeak"}, {"value": 1, "text": "Leak"}]
ISOLATION_TEXT = [{"value": 0, "text": "NotIsolated"}, {"value": 1, "text": "Isolated"}]
REQUEST_TEXT = [{"value": 0, "text": "NotRequested"}, {"value": 1, "text": "Requested"}]
STATUS_TEXT = [{"value": 0, "text": "Fault"}, {"value": 1, "text": "Running"}]
ONOFF_TEXT = [{"value": 0, "text": "Off"}, {"value": 1, "text": "On"}]


def kw(value: float) -> dict[str, Any]:
    return {"value": round(max(0.0, value), 1), "unit": "kilowatt"}


class ExchangePublisher:
    def __init__(
        self,
        mqtt_url: str | None = None,
        site: str = "site1",
        integration: str = "FACTORYSIM",
        feed_size: int = 300,
    ) -> None:
        self.mqtt_url = mqtt_url if mqtt_url is not None else os.environ.get("DSX_MQTT_URL", "")
        self.site = site
        self.integration = integration
        self.client = None
        self.connected = False
        self.feed: deque[dict[str, Any]] = deque(maxlen=feed_size)
        self.seq = 0
        self.published_total = 0
        self.last_tick_count = 0
        self._last: dict[str, tuple[Any, float]] = {}
        self._meta: dict[str, dict[str, Any]] = {}
        self._meta_sent_at = NEVER
        self._heartbeat_at = NEVER
        self._sample_at = NEVER
        self._layout: tuple[str, int] | None = None
        self._wall0 = datetime.now(UTC)
        self._sim0: float | None = None
        # Flex requires every payload to be signed. A real deployment registers keys
        # with the bus; the sim makes a throwaway one each start.
        self._key = ec.generate_private_key(ec.SECP256R1())
        self._kid = f"factorysim-dev-{secrets.token_hex(3)}"

    # ----- transport

    def connect(self) -> None:
        if not self.mqtt_url:
            return
        import paho.mqtt.client as mqtt

        url = urlparse(self.mqtt_url)
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"factorysim-{secrets.token_hex(3)}")
        if url.username:
            client.username_pw_set(url.username, url.password)

        def on_connect(_c, _u, _f, reason_code, _p):
            self.connected = not reason_code.is_failure
            self._meta_sent_at = NEVER  # resend metadata after every reconnect

        def on_disconnect(_c, _u, _f, _rc, _p):
            self.connected = False

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.connect_async(url.hostname or "localhost", url.port or 1883, keepalive=30)
        client.loop_start()
        self.client = client

    def close(self) -> None:
        if self.client is not None:
            self.client.loop_stop()
            self.client.disconnect()

    def status(self) -> dict[str, Any]:
        mode = "dry-run" if not self.mqtt_url else ("connected" if self.connected else "connecting")
        return {
            "mode": mode,
            "url": self.mqtt_url or None,
            "published_total": self.published_total,
            "last_tick": self.last_tick_count,
            "points": len(self._meta),
        }

    def _send(self, topic: str, payload: Any, *, retain: bool = False, notable: bool = False,
              family: str = "bms", summary: str = "", decoded: Any = None) -> None:  # fmt: skip
        body = json.dumps(payload, separators=(",", ":"))
        if self.client is not None and self.connected:
            self.client.publish(topic, body, qos=0, retain=retain)
        self.published_total += 1
        self.last_tick_count += 1
        if notable:
            self.seq += 1
            self.feed.append({
                "seq": self.seq, "family": family, "topic": topic, "summary": summary,
                "payload": payload, "decoded": decoded, "clock": self._clock,
            })  # fmt: skip

    # ----- time

    def _sim_time(self, t_min: float) -> datetime:
        """Simulated time as a real timestamp: the wall clock at start plus sim minutes."""
        if self._sim0 is None:
            self._sim0 = t_min
        return self._wall0 + timedelta(minutes=t_min - self._sim0)

    def _iso(self, t_min: float) -> str:
        return self._sim_time(t_min).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _ms(self, t_min: float) -> int:
        return int(self._sim_time(t_min).timestamp() * 1000)

    # ----- BMS Event Bus

    def _tag(self, *parts: str) -> str:
        return "/".join((self.site, *parts))

    def _row(self, i: int) -> str:
        return f"row{i // RACKS_PER_ROW + 1:02d}"

    def _point(self, object_type: str, point_type: str, tag: str, meta: dict[str, Any], publisher: str = "PUB") -> str:
        """Register a point's metadata and return its value topic."""
        self._meta[f"BMS/v1/PUB/Metadata/{object_type}/{point_type}/{tag}"] = {
            "objectType": object_type,
            "pointType": point_type,
            **meta,
        }
        return f"BMS/v1/{publisher}/Value/{object_type}/{point_type}/{tag}"

    def _value(self, topic: str, value: float | int, now_ms: int, wall: float, *, deadband: float = 0.0,
               notable: bool = False, summary: str = "", force: bool = False) -> None:  # fmt: skip
        last = self._last.get(topic)
        changed = last is None or abs(value - last[0]) > deadband
        if not (changed or force or wall - last[1] >= REPUBLISH_S):
            return
        self._last[topic] = (value, wall)
        self._send(topic, {"value": value, "timestamp": now_ms, "quality": 1},
                   notable=notable and (changed or force), summary=summary)  # fmt: skip

    def _publish_bms(self, engine: Engine, snap: dict[str, Any], events: list[dict[str, Any]]) -> None:
        wall = time.monotonic()
        now_ms = self._ms(snap["t_min"])
        plat, racks, power, cooling = snap["platform"], snap["racks"], snap["power"], snap["cooling"]
        liquid = plat["cooling"] == "liquid"
        n = snap["site"]["racks"]
        layout = (plat["key"], n)
        if layout != self._layout:  # the hall was rebuilt: forget the old points
            self._layout, self._last, self._meta, self._meta_sent_at = layout, {}, {}, NEVER

        sample = wall - self._sample_at >= 20.0
        for i in range(n):
            rid = engine.rack_id(i)
            tag = self._tag(self._row(i), rid)
            rack_meta = {"rackLocationName": f"Hall A {rid}", "rackLocationId": rid}
            isolated = racks["state"][i] == ISOLATED
            kw_now = round(racks["power_frac"][i] * plat["rack_kw_maxp"], 1)
            show = sample and i == 0
            self._value(self._point("Rack", "RackPower", tag, {**rack_meta, "engUnit": "kW"}), kw_now, now_ms, wall,
                        deadband=0.5, notable=show, force=show, summary=f"{rid} power {kw_now} kW")  # fmt: skip
            if not liquid:
                continue
            flow = 0.0 if isolated else round(cooling["flow_lpm_per_rack"] * racks["flow_frac"][i], 1)
            self._value(self._point("Rack", "RackLiquidSupplyTemperature", tag, {**rack_meta, "engUnit": "C"}),
                        cooling["supply_c"], now_ms, wall, deadband=0.2)  # fmt: skip
            self._value(self._point("Rack", "RackLiquidReturnTemperature", tag, {**rack_meta, "engUnit": "C"}),
                        racks["return_c"][i], now_ms, wall, deadband=0.3)  # fmt: skip
            self._value(self._point("Rack", "RackLiquidFlow", tag, {**rack_meta, "engUnit": "LPM"}),
                        flow, now_ms, wall, deadband=1.0)  # fmt: skip
            self._point("Rack", "RackLeakDetect", tag, {**rack_meta, "stateText": LEAK_TEXT})
            self._point("Rack", "RackLiquidIsolationStatus", tag, {**rack_meta, "stateText": ISOLATION_TEXT})
            self._point("Rack", "RackLiquidIsolationRequest", tag,
                        {**rack_meta, "stateText": REQUEST_TEXT, "integration": self.integration})  # fmt: skip

        if liquid:
            for row in range(snap["site"]["rows"]):
                cdu = f"cdu-{row + 1:02d}"
                tag = self._tag(f"row{row + 1:02d}", cdu)
                ident = {"objectName": f"CDU {row + 1:02d}", "objectId": cdu}
                members = [i for i in range(row * RACKS_PER_ROW, min(n, (row + 1) * RACKS_PER_ROW))]
                serves = {"servesId": [engine.rack_id(i) for i in members]}
                flow = sum(
                    0.0 if racks["state"][i] == ISOLATED else cooling["flow_lpm_per_rack"] * racks["flow_frac"][i]
                    for i in members
                )
                ret = sum(racks["return_c"][i] for i in members) / len(members)
                supply_meta = {**ident, **serves, "engUnit": "C", "processArea": ["Secondary", "Supply"]}
                self._value(self._point("CDU", "LiquidTemperature", f"{tag}/secondary-supply", supply_meta),
                            cooling["supply_c"], now_ms, wall, deadband=0.2)  # fmt: skip
                self._value(self._point("CDU", "LiquidTemperature", f"{tag}/secondary-return",
                                        {**ident, "engUnit": "C", "processArea": ["Secondary", "Return"]}),
                            round(ret, 1), now_ms, wall, deadband=0.3)  # fmt: skip
                self._value(self._point("CDU", "LiquidFlow", tag,
                                        {**ident, "engUnit": "LPM", "processArea": ["Secondary"]}),
                            round(flow, 0), now_ms, wall, deadband=5)  # fmt: skip
                self._point("CDU", "Status", tag, {**ident, "stateText": STATUS_TEXT})
                self._point("CDU", "LiquidTemperatureSpRequest", tag,
                            {**ident, "engUnit": "C", "isSetpoint": True, "integration": self.integration})  # fmt: skip

        meters = {
            "utility-main": ("Utility feed", power["grid_kw"]),
            "it-load": ("IT load", power["it_kw"]),
            "cooling-plant": ("Cooling plant", power["cooling_kw"]),
        }
        for meter_id, (name, value) in meters.items():
            show = sample and meter_id == "utility-main"
            self._value(self._point("PowerMeter", "ActivePower", self._tag("power", meter_id),
                                    {"objectName": name, "objectId": meter_id, "servesId": [], "engUnit": "kW"}),
                        round(value, 0), now_ms, wall, deadband=5, notable=show, force=show,
                        summary=f"{name} {value / 1000:.2f} MW")  # fmt: skip
        self._value(self._point("Sensor", "AirTemperature", self._tag("outdoor", "oat-1"),
                                {"objectName": "Outdoor air", "objectId": "oat-1", "engUnit": "C"}),
                    round(cooling["ambient_c"], 1), now_ms, wall, deadband=0.2)  # fmt: skip
        self._value(self._point("Chiller", "Status", self._tag("plant", "chiller-1"),
                                {"objectName": "Chiller 1", "objectId": "chiller-1", "stateText": ONOFF_TEXT}),
                    int(cooling["chiller_share"] > 0.01), now_ms, wall, notable=True,
                    summary="Chiller " + ("on" if cooling["chiller_share"] > 0.01 else "off"))  # fmt: skip
        if snap["config"]["bess_mwh"] > 0:
            discharging = power["bess_discharge_kw"] > 0
            self._value(self._point("BESS", "Status", self._tag("power", "bess-1"),
                                    {"objectName": "Battery 1", "objectId": "bess-1", "stateText": ONOFF_TEXT}),
                        int(discharging), now_ms, wall, notable=True,
                        summary="Battery " + ("discharging" if discharging else "standing by"))  # fmt: skip
        if sample:
            self._sample_at = wall

        # Discrete events from the engine
        for ev in events:
            kind = ev["kind"]
            if kind == "bms.cdu.status":
                row = ev["row"] + 1
                cdu = f"cdu-{row:02d}"
                topic = "BMS/v1/PUB/Value/CDU/Status/" + self._tag(f"row{row:02d}", cdu)
                self._value(topic, ev["value"], now_ms, wall, notable=True, force=True,
                            summary=f"{cdu} status {'Running' if ev['value'] else 'Fault'}")  # fmt: skip
            elif kind.startswith("bms.rack."):
                rid = engine.rack_id(ev["rack"])
                tag = self._tag(self._row(ev["rack"]), rid)
                point, publisher, label = {
                    "bms.rack.leak": ("RackLeakDetect", "PUB", "leak detect"),
                    "bms.rack.isolation_request": ("RackLiquidIsolationRequest", self.integration, "isolation request"),
                    "bms.rack.isolation_status": ("RackLiquidIsolationStatus", "PUB", "isolation status"),
                }[kind]
                self._value(f"BMS/v1/{publisher}/Value/Rack/{point}/{tag}", ev["value"], now_ms, wall,
                            notable=True, force=True, summary=f"{rid} {label} = {ev['value']}")  # fmt: skip
            elif kind == "bms.setpoint_request" and liquid:
                for row in range(snap["site"]["rows"]):
                    cdu = f"cdu-{row + 1:02d}"
                    topic = (f"BMS/v1/{self.integration}/Value/CDU/LiquidTemperatureSpRequest/"
                             + self._tag(f"row{row + 1:02d}", cdu))  # fmt: skip
                    self._value(topic, ev["value"], now_ms, wall, notable=row == 0, force=True,
                                summary=f"IT asks the BMS for {ev['value']:.0f} C supply water (all CDUs)")  # fmt: skip

        if wall - self._heartbeat_at >= HEARTBEAT_S:
            self._heartbeat_at = wall
            self._point("System", "HeartbeatTimestampBms", self._tag("system", "bms"),
                        {"objectName": "BMS", "objectId": "BMS", "engUnit": "ms"})  # fmt: skip
            self._send(f"BMS/v1/PUB/Value/System/HeartbeatTimestampBms/{self._tag('system', 'bms')}",
                       {"value": now_ms, "timestamp": now_ms, "quality": 1})  # fmt: skip
            self._send(f"BMS/v1/{self.integration}/Value/System/HeartbeatTimestampIntegration/"
                       f"{self._tag('system', self.integration.lower())}",
                       {"value": now_ms, "timestamp": now_ms, "quality": 1})  # fmt: skip
        if wall - self._meta_sent_at >= REPUBLISH_S:
            self._meta_sent_at = wall
            for topic, meta in self._meta.items():
                self._send(topic, meta, retain=True)

    # ----- DSX Flex

    def _cloud_event(self, event_type: str, role: str, identifier: str, data: dict[str, Any],
                     t_min: float, correlation_id: str | None, summary: str) -> None:  # fmt: skip
        signed = jwt.api_jws.encode(
            json.dumps(data, separators=(",", ":")).encode(), self._key, algorithm="ES256",
            headers={"kid": self._kid},
        )  # fmt: skip
        envelope = {
            "specversion": "1.0.2",
            "id": str(uuid.uuid4()),
            "source": f"//grid/v1/{role}/{identifier}",
            "type": event_type,
            "time": self._iso(t_min),
            "datacontenttype": "application/json",
            "subject": FLEX_SUBJECTS[event_type],
            "traceparent": f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01",
            "data": signed,
        }
        if correlation_id:
            envelope["correlationid"] = correlation_id
        self._send(FLEX_TOPICS[event_type], envelope, notable=True, family="flex", summary=summary, decoded=data)

    def _feed_target(self, tg: dict[str, Any], active: bool) -> dict[str, Any]:
        return {
            "active": active,
            "load_constraint": kw(tg["limit_kw"]),
            "interval": {"start_time": self._iso(tg["start_t"]), "end_time": self._iso(tg["end_t"])},
            "strategy": {"best_effort": True},
            "correlation_id": tg["correlation_id"],
        }

    def _publish_flex(self, engine: Engine, snap: dict[str, Any], events: list[dict[str, Any]]) -> None:
        t = snap["t_min"]
        power, flex = snap["power"], snap["flex"]
        tg = engine.target
        for ev in events:
            kind = ev["kind"]
            if kind == "flex.loadtarget.set":
                target = ev["target"]
                data = {  # INFERRED item shape, mirroring FeedTarget in the status schema
                    "targets": [{
                        "feed_tags": [FEED_TAG],
                        "load_constraint": kw(target["limit_kw"]),
                        "interval": {"start_time": self._iso(target["start_t"]),
                                     "end_time": self._iso(target["end_t"])},
                        "strategy": {"best_effort": True},
                    }]
                }  # fmt: skip
                self._cloud_event("grid.loadtarget.set.v1", "isv", "sim-grid-services", data, t,
                                  target["correlation_id"],
                                  f"Grid ISV sets load target {target['limit_kw'] / 1000:.1f} MW")  # fmt: skip
            elif kind == "flex.status":
                targets = [self._feed_target(tg, tg["phase"] != "ramp_up")] if tg else []
                feed = {
                    "metadata": {
                        "power_minimum": kw(power["site_kw"] * 0.1),
                        "power_maximum": kw(power["site_kw"]),
                        "default_constraint": kw(power["site_kw"]),
                    },
                    "targets": targets,
                    "calculated_load": kw(power["grid_kw"]),
                    "in_flight": bool(flex["in_flight"]),
                    "event": ev["event"],  # INFERRED beyond target_set / start_ramp_down
                    "power_event_start_time": self._iso(tg["start_t"]) if tg else None,
                    "compliant": bool(flex["compliant"]),
                }
                data = {"snapshot_time": self._iso(t), "feeds": {FEED_TAG: feed}}
                self._cloud_event("grid.powerstate.status.v1", "dsx-flex-agent", "factorysim", data, t,
                                  tg["correlation_id"] if tg else None,
                                  f"Power state: {ev['event']}, load {power['grid_kw'] / 1000:.2f} MW, "
                                  f"{'compliant' if flex['compliant'] else 'NOT compliant'}")  # fmt: skip
            elif kind == "flex.breach" and tg:
                b = ev["breach"]
                data = {
                    "feed_tag": FEED_TAG,
                    "status": b["status"],
                    "severity": b["severity"],
                    "breach": {"breach_id": b["breach_id"], "detected_at": self._iso(b["detected_t"])},
                    "target": {"correlation_id": tg["correlation_id"], "load_constraint": kw(tg["limit_kw"])},
                    "measured_load": kw(b["measured_kw"]),
                    "infrastructure_actions": [] if b["status"] == "resolved" else ["power_cap", "suspend"],
                }
                if b["status"] == "resolved":
                    data["breach"]["resolved_at"] = self._iso(t)
                else:
                    data["shed_hints"] = [
                        {"resource_id": h["resource_id"], "resource_type": "chassis", "action": "suspend",
                         "priority": h["priority"]} for h in b["hints"]
                    ]  # fmt: skip
                self._cloud_event("grid.powerbreach.alert.v1", "dsx-flex-agent", "factorysim", data, t,
                                  tg["correlation_id"], f"Power breach {b['status']} ({b['severity']})")  # fmt: skip
            elif kind == "flex.enforcement":
                per_rack_kw = snap["platform"]["rack_kw_maxp"] * 0.08
                data = {
                    "breach_id": ev["breach_id"],
                    "feed_tag": FEED_TAG,
                    "action": ev["action"],
                    "code": "success",
                    "diag_msg": f"{len(ev['racks'])} racks {'suspended' if ev['action'] == 'applied' else 'resumed'}",
                    "results": [
                        {"resource_id": r, "resource_type": "chassis", "requested_action": "suspend",
                         "code": "applied", "applied_power": kw(per_rack_kw)} for r in ev["racks"]
                    ],
                    "timestamp": self._iso(t),
                }  # fmt: skip
                self._cloud_event("grid.powerbreach.enforcement.v1", "infra", "factorysim-ima", data, t,
                                  tg["correlation_id"] if tg else None,
                                  f"Enforcement {ev['action']}: {len(ev['racks'])} racks")  # fmt: skip

    # ----- entry point

    def publish_tick(self, engine: Engine, snap: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Publish everything for one tick. Returns the notable messages added to the feed."""
        before = self.seq
        self.last_tick_count = 0
        self._clock = snap["clock"]
        self._publish_flex(engine, snap, events)
        self._publish_bms(engine, snap, events)
        return [m for m in self.feed if m["seq"] > before]

    def verify(self, jws: str) -> dict[str, Any]:
        """Verify a signed Flex payload with this publisher's public key (used in tests)."""
        return json.loads(jwt.api_jws.decode(jws, self._key.public_key(), algorithms=["ES256"]))
