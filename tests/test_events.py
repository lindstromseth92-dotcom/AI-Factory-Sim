"""Grid events, faults, the event bus messages and the HTTP API."""

import re

from fastapi.testclient import TestClient

from factorysim.engine import ISOLATED, SUSPENDED, THROTTLED, Engine, SiteConfig
from factorysim.exchange import ExchangePublisher
from factorysim.metrics import render


def run(engine, bus, ticks, dt=2):
    out = []
    for _ in range(ticks):
        snap = engine.step(dt)
        out += bus.publish_tick(engine, snap, engine.drain_events())
    return out


def flex(messages, event_type):
    return [m for m in messages if m["family"] == "flex" and m["payload"]["type"] == event_type]


def test_shallow_curtailment_rides_through_on_capping():
    eng, bus = Engine(SiteConfig()), ExchangePublisher(mqtt_url="")
    run(eng, bus, 5)
    eng.set_load_target(0.75, duration_min=60, ramp_min=10)
    msgs = run(eng, bus, 20)
    snap = eng.snapshot
    assert snap["flex"]["phase"] == "hold" and snap["flex"]["compliant"]
    assert snap["power"]["grid_kw"] <= 7500 * 1.005
    assert snap["work"]["suspended"] == 0
    assert snap["work"]["cap_inference"] < 1.0
    assert not flex(msgs, "grid.powerbreach.alert.v1")
    events = [m["decoded"]["feeds"]["utility-main"]["event"] for m in flex(msgs, "grid.powerstate.status.v1")]
    assert events[:2] == ["target_set", "start_ramp_down"] and "end_ramp_down" in events


def test_deep_curtailment_breaches_then_enforces_then_recovers():
    eng, bus = Engine(SiteConfig()), ExchangePublisher(mqtt_url="")
    run(eng, bus, 5)
    eng.set_load_target(0.4, duration_min=40, ramp_min=6)
    msgs = run(eng, bus, 15)
    alerts = flex(msgs, "grid.powerbreach.alert.v1")
    assert [a["decoded"]["status"] for a in alerts] == ["active", "resolved"]
    assert alerts[0]["decoded"]["shed_hints"][0]["action"] == "suspend"
    assert alerts[0]["decoded"]["breach"]["breach_id"] == alerts[1]["decoded"]["breach"]["breach_id"]
    applied = flex(msgs, "grid.powerbreach.enforcement.v1")
    assert applied and applied[0]["decoded"]["action"] == "applied"
    assert eng.snapshot["work"]["suspended"] > 0
    assert eng.snapshot["power"]["grid_kw"] <= 4000 * 1.005
    # Training racks are shed before inference racks.
    state, role = eng.snapshot["racks"]["state"], eng.snapshot["racks"]["role"]
    suspended_roles = {role[i] for i, s in enumerate(state) if s == SUSPENDED}
    assert 1 in suspended_roles
    msgs = run(eng, bus, 30)
    assert flex(msgs, "grid.powerbreach.enforcement.v1")[-1]["decoded"]["action"] == "reverted"
    assert not eng.snapshot["flex"]["active"] and eng.snapshot["work"]["suspended"] == 0


def test_flex_messages_are_signed_cloudevents():
    eng, bus = Engine(SiteConfig()), ExchangePublisher(mqtt_url="")
    run(eng, bus, 2)
    target = eng.set_load_target(0.8)
    msg = flex(run(eng, bus, 1), "grid.loadtarget.set.v1")[0]
    env = msg["payload"]
    assert env["specversion"] == "1.0.2" and env["datacontenttype"] == "application/json"
    assert env["source"] == "//grid/v1/isv/sim-grid-services"
    assert env["correlationid"] == target["correlation_id"]
    assert re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-01", env["traceparent"])
    assert env["data"].count(".") == 2  # JWS compact serialization
    data = bus.verify(env["data"])
    assert data == msg["decoded"]
    assert data["targets"][0]["load_constraint"] == {"value": 8000.0, "unit": "kilowatt"}


def test_bms_topics_and_payloads_follow_the_schema():
    eng, bus = Engine(SiteConfig()), ExchangePublisher(mqtt_url="")
    sent = []
    bus._send_orig = bus._send
    bus._send = lambda topic, payload, **kw: (sent.append((topic, payload)), bus._send_orig(topic, payload, **kw))
    run(eng, bus, 2)
    values = [(t, p) for t, p in sent if "/Value/" in t]
    metas = [(t, p) for t, p in sent if "/Metadata/" in t]
    assert values and metas
    pattern = re.compile(r"BMS/v1/(PUB|FACTORYSIM)/(Value|Metadata)/[A-Za-z]+/[A-Za-z]+/site1/.+")
    assert all(pattern.fullmatch(t) for t, _ in sent if t.startswith("BMS/"))
    assert all(set(p) == {"value", "timestamp", "quality"} for _, p in values)
    for topic, meta in metas:
        _, _, _, _, object_type, point_type, *_ = topic.split("/")
        assert meta["objectType"] == object_type and meta["pointType"] == point_type
        assert ("engUnit" in meta) != ("stateText" in meta)  # exactly one of the two
    rack_power = [t for t, _ in values if "/Rack/RackPower/" in t]
    assert len(rack_power) >= eng.n


def test_leak_isolates_one_rack_and_tells_the_bus():
    eng, bus = Engine(SiteConfig()), ExchangePublisher(mqtt_url="")
    run(eng, bus, 2)
    eng.inject_fault("leak", 12)
    msgs = run(eng, bus, 1)
    topics = [m["topic"] for m in msgs if m["family"] == "bms"]
    assert "BMS/v1/PUB/Value/Rack/RackLeakDetect/site1/row02/R02-03" in topics
    assert "BMS/v1/FACTORYSIM/Value/Rack/RackLiquidIsolationRequest/site1/row02/R02-03" in topics
    assert "BMS/v1/PUB/Value/Rack/RackLiquidIsolationStatus/site1/row02/R02-03" in topics
    assert eng.snapshot["racks"]["state"][12] == ISOLATED
    assert eng.snapshot["racks"]["power_frac"][12] == 0
    run(eng, bus, 130)  # four simulated hours later it is repaired
    assert eng.snapshot["racks"]["state"][12] != ISOLATED


def test_pump_failure_throttles_one_row():
    eng = Engine(SiteConfig(policy="maxp"))
    eng.step(2)
    before = eng.snapshot["racks"]["power_frac"][20]
    eng.inject_fault("pump_failure", 2)
    snap = eng.step(2)
    assert all(snap["racks"]["state"][i] == THROTTLED for i in range(20, 30))
    assert snap["racks"]["state"][5] != THROTTLED
    assert snap["racks"]["power_frac"][20] < before
    assert snap["racks"]["return_c"][20] > snap["platform"]["return_limit_c"]


def test_battery_covers_part_of_a_curtailment():
    plain, backed = Engine(SiteConfig(), seed=1), Engine(SiteConfig(bess_mwh=4), seed=1)
    for eng in (plain, backed):
        eng.step(2)
        eng.set_load_target(0.75, duration_min=30, ramp_min=4)
        for _ in range(8):
            eng.step(2)
    assert backed.snapshot["power"]["bess_discharge_kw"] > 0
    assert backed.snapshot["power"]["grid_kw"] <= 7500 * 1.005
    # With the battery carrying part of the cut, racks keep a higher power cap.
    assert backed.snapshot["work"]["cap_inference"] > plain.snapshot["work"]["cap_inference"]
    assert backed.snapshot["power"]["bess_soc"] < 1.0


def test_metrics_use_dcgm_names():
    eng, bus = Engine(SiteConfig()), ExchangePublisher(mqtt_url="")
    snap = eng.step(2)
    text = render(snap, [eng.rack_id(i) for i in range(eng.n)], bus.status())
    for name in ("DCGM_FI_DEV_GPU_UTIL", "DCGM_FI_DEV_POWER_USAGE", "DCGM_FI_DEV_GPU_TEMP", "factorysim_pue"):
        assert f"# TYPE {name} gauge" in text
    assert text.count("DCGM_FI_DEV_POWER_USAGE{") == eng.n


def test_http_api():
    from factorysim.server import app

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert "vera_rubin_nvl72" in client.get("/api/catalog").json()["platforms"]
        r = client.post("/api/config", json={"platform": "hgx_h100", "site_mw": 5})
        assert r.status_code == 200 and r.json()["supply_c"] == 27
        assert client.post("/api/config", json={"nonsense": 1}).status_code == 422
        assert client.post("/api/config", json={"platform": "nope"}).status_code == 422
        assert client.post("/api/actions/fault", json={"kind": "nope"}).status_code == 422
        assert client.post("/api/actions/load-target", json={"fraction": 0.7}).json()["limit_kw"] == 3500
        assert len(client.get("/api/compare").json()["platforms"]) == 4
        assert "factorysim_grid_power_watts" in client.get("/metrics").text
        with client.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello" and hello["snap"]["platform"]["key"] == "hgx_h100"
            assert ws.receive_json()["type"] == "tick"
        assert client.get("/").status_code == 200
