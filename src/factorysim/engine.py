"""The AI factory model.

One `Engine` is one site: a grid connection, a hall of GPU racks, a cooling plant,
and the control loops that sit between them. `step()` advances simulated time and
returns a snapshot the dashboard, the event bus and the metrics endpoint all read.

The chain it models, input to output:

    grid power -> distribution losses -> IT load (GPU racks + network/storage)
               -> heat -> cooling plant (pumps, dry coolers, chillers) -> outdoors
    demand     -> GPU utilization -> tokens (inference) and GPU-hours (training)

and the three controls NVIDIA's DSX stack puts on that chain:

    sizing policy   MaxP / MaxQ / MaxLPS: how many racks a fixed grid connection carries
    power control   a DPS-style loop that caps rack power to stay inside the envelope
    grid response   DSX Flex load targets, breach alerts and enforcement

Equations and where each constant comes from are in docs/MODEL.md.
"""

from __future__ import annotations

import math
import uuid
from collections import deque
from dataclasses import asdict, dataclass, replace
from typing import Any

import numpy as np

from .catalog import Catalog, Platform, load_catalog

# Heat carried by 1 litre/min of 25% propylene glycol per kelvin of temperature rise.
# density 1.02 kg/L, specific heat 3.9 kJ/kg.K -> 1.02 / 60 * 3.9
LPM_KW_PER_K = 0.0663
# Rack pressure drop at the reference temperature rise (Lenovo GB300: 18.4 psi at 45 C supply).
REF_DP_KPA = 127.0
REF_DT_K = 11.0
MAX_COP = 8.0
SUSPENDED_POWER_FRACTION = 0.08

OK, THROTTLED, SUSPENDED, ISOLATED = 0, 1, 2, 3
STATE_NAMES = {OK: "ok", THROTTLED: "throttled", SUSPENDED: "suspended", ISOLATED: "isolated"}
INFERENCE, TRAINING = 0, 1
RACKS_PER_ROW = 10
POLICIES = ("maxp", "maxq", "maxlps")


@dataclass
class SiteConfig:
    """Everything the operator chooses. These are the dashboard's inputs."""

    platform: str = "vera_rubin_nvl72"
    site_mw: float = 10.0  # grid connection size
    policy: str = "maxlps"  # maxp | maxq | maxlps
    oversubscription: float = 1.4  # MaxLPS only: racks installed relative to MaxP sizing
    supply_c: float = 45.0  # facility water supply temperature
    climate: str = "temperate"
    inference_share: float = 0.7  # share of racks serving inference; the rest train
    demand: float = 0.7  # mean inference demand as a fraction of nameplate capacity
    claim_credit: float = 0.0  # 0 = chip compute ratios, 1 = NVIDIA full-stack claims
    price_usd_mwh: float = 70.0
    bess_mwh: float = 0.0  # battery energy storage

    def validated(self, cat: Catalog) -> SiteConfig:
        plat = cat.platforms.get(self.platform)
        if plat is None:
            raise ValueError(f"unknown platform {self.platform!r}")
        if self.policy not in POLICIES:
            raise ValueError(f"unknown policy {self.policy!r}")
        if self.climate not in cat.climates:
            raise ValueError(f"unknown climate {self.climate!r}")

        def clamp(x: float, lo: float, hi: float) -> float:
            return min(hi, max(lo, float(x)))

        return replace(
            self,
            site_mw=clamp(self.site_mw, 1, 50),
            oversubscription=clamp(self.oversubscription, 1.0, 1.6),
            supply_c=clamp(self.supply_c, 18, plat.max_supply_c),
            inference_share=clamp(self.inference_share, 0, 1),
            demand=clamp(self.demand, 0, 1.5),
            claim_credit=clamp(self.claim_credit, 0, 1),
            price_usd_mwh=clamp(self.price_usd_mwh, 0, 1000),
            bess_mwh=clamp(self.bess_mwh, 0, 50),
        )


# --------------------------------------------------------------------------- physics


def cap_perf(cap: np.ndarray | float, k: float) -> np.ndarray | float:
    """Throughput at a power cap, relative to uncapped. cap is a fraction of MaxP."""
    return 1.0 - k * (1.0 - cap) ** 2


def cooling_power(
    cat: Catalog, plat: Platform, heat_kw: float, racks_flowing: float, supply_c: float, ambient_c: float
) -> dict[str, float]:
    """Electrical power the cooling plant needs to reject `heat_kw`.

    Liquid heat goes to a warm-water loop. Dry coolers can bring the return water down
    to (ambient + approach) for the cost of fans alone. Whatever temperature drop is
    still missing to reach the supply setpoint is done by a chiller, which is the
    expensive part. Warmer supply water therefore means less chiller, and at 45 C
    almost none in most climates. Air heat needs ~18 C chilled water, so it nearly
    always needs the chiller.
    """
    f = cat.facility
    approach = f["dry_cooler_approach_c"]
    q_liq = heat_kw * plat.liquid_fraction
    q_air = heat_kw - q_liq
    coolest_free = ambient_c + approach  # coldest water a dry cooler can make right now

    def chiller(q: float, water_c: float, rise_k: float) -> tuple[float, float]:
        frac = min(1.0, max(0.0, (coolest_free - water_c) / rise_k))
        lift = max(1.0, coolest_free + 5.0 - water_c)
        cop = min(MAX_COP, f["chiller_carnot_fraction"] * (water_c + 273.15) / lift)
        return frac, frac * q / cop

    dt_liq = max(3.0, plat.return_limit_c - supply_c)
    liq_frac, liq_chiller_kw = chiller(q_liq, supply_c, dt_liq) if q_liq > 0 else (0.0, 0.0)
    air_frac, air_chiller_kw = chiller(q_air, f["air_supply_water_c"], 10.0) if q_air > 0 else (0.0, 0.0)
    chiller_kw = liq_chiller_kw + air_chiller_kw

    fans_kw = f["dry_cooler_overhead"] * (heat_kw + chiller_kw)
    crah_kw = f["crah_fan_overhead"] * q_air

    # Pumps hold the design flow: enough to carry a MaxP rack's heat at the allowed rise.
    flow_lpm = plat.rack_kw_maxp * plat.liquid_fraction / (LPM_KW_PER_K * dt_liq)
    dp_kpa = REF_DP_KPA * (REF_DT_K / dt_liq) ** 2
    pump_kw_per_rack = (flow_lpm / 60000.0) * dp_kpa * f["loop_pressure_multiplier"] / f["pump_efficiency"]
    pumps_kw = pump_kw_per_rack * racks_flowing if plat.liquid_fraction > 0 else 0.0

    total = chiller_kw + fans_kw + crah_kw + pumps_kw
    chiller_share = (liq_frac * q_liq + air_frac * q_air) / heat_kw if heat_kw > 0 else 0.0
    mode = "free" if chiller_share < 0.01 else ("chiller" if chiller_share > 0.99 else "partial")
    return {
        "chiller_kw": chiller_kw,
        "fans_kw": fans_kw,
        "crah_kw": crah_kw,
        "pumps_kw": pumps_kw,
        "total_kw": total,
        "chiller_share": chiller_share,
        "mode": mode,
        "flow_lpm_per_rack": flow_lpm if plat.liquid_fraction > 0 else 0.0,
        "dt_k": dt_liq,
    }


def sizing_rack_kw(cfg: SiteConfig, plat: Platform) -> float:
    """Power each rack is budgeted when deciding how many fit on the grid connection."""
    if cfg.policy == "maxp":
        return plat.rack_kw_maxp
    if cfg.policy == "maxq":
        return plat.rack_kw_maxp * plat.maxq_fraction
    return plat.rack_kw_maxp / cfg.oversubscription


def size_site(
    cfg: SiteConfig, cat: Catalog, *, integer: bool = True, pue_override: float | None = None
) -> dict[str, float]:
    """How many racks the grid connection carries on the hottest design day."""
    plat = cat.platforms[cfg.platform]
    f = cat.facility
    climate = cat.climates[cfg.climate]
    rack_it_kw = sizing_rack_kw(cfg, plat) * (1 + f["network_storage_overhead"])
    if pue_override is not None:
        design_pue = pue_override
    else:
        cool = cooling_power(cat, plat, rack_it_kw, 1, cfg.supply_c, climate.design_c)
        design_pue = (1 + cool["total_kw"] / rack_it_kw + f["misc_load"]) / (1 - f["distribution_loss"])
    racks = cfg.site_mw * 1000.0 / (rack_it_kw * design_pue)
    if integer:
        racks = max(1, math.floor(racks))
    return {
        "racks": racks,
        "gpus": racks * plat.gpus_per_rack,
        "design_pue": design_pue,
        "sizing_rack_kw": sizing_rack_kw(cfg, plat),
        "it_budget_kw": cfg.site_mw * 1000.0 / design_pue,
    }


# ---------------------------------------------------------------------------- engine


class Engine:
    def __init__(
        self,
        cfg: SiteConfig | None = None,
        cat: Catalog | None = None,
        *,
        seed: int = 7,
        steady: bool = False,
    ) -> None:
        self.cat = cat or load_catalog()
        self.steady = steady  # no weather, no demand curve, no noise: used for comparisons
        self.rng = np.random.default_rng(seed)
        self.t_min = 8 * 60.0  # start at 08:00 on day 1
        self.history: deque[dict[str, float]] = deque(maxlen=600)
        self.events: list[dict[str, Any]] = []  # drained by the event bus publisher
        self.faults: list[dict[str, Any]] = []
        self.target: dict[str, Any] | None = None  # active DSX Flex load target
        self.breach: dict[str, Any] | None = None
        self.last_flex_status_t = -1e9
        self.snapshot: dict[str, Any] = {}
        self.configure(cfg or SiteConfig())

    # ----- setup

    def configure(self, cfg: SiteConfig) -> None:
        """Apply a new configuration. Rebuilds the hall if its size would change."""
        old = getattr(self, "cfg", None)
        cfg = cfg.validated(self.cat)
        self.cfg = cfg
        self.plat = self.cat.platforms[cfg.platform]
        self.climate = self.cat.climates[cfg.climate]
        self.sizing = size_site(cfg, self.cat)
        n = int(self.sizing["racks"])
        rebuilt = old is None or n != getattr(self, "n", None) or old.platform != cfg.platform
        if rebuilt:
            self.n = n
            self.rows = math.ceil(n / RACKS_PER_ROW)
            self.row_of = np.arange(n) // RACKS_PER_ROW
            self.jitter = np.zeros(n) if self.steady else self.rng.normal(0, 0.06, n)
            self.flow_frac = np.ones(n)
            self.state = np.zeros(n, dtype=int)
            self.suspended = np.zeros(n, dtype=bool)
            self.isolated = np.zeros(n, dtype=bool)
            self.faults = []
            self.target = None
            self.breach = None
            self.history.clear()
            self.bess_soc_kwh = cfg.bess_mwh * 1000.0
        if old is not None and old.supply_c != cfg.supply_c and self.plat.cooling == "liquid":
            self.events.append({"kind": "bms.setpoint_request", "value": cfg.supply_c})
            self._log("setpoint", f"Supply water setpoint request: {cfg.supply_c:.0f} C")
        self.bess_soc_kwh = min(getattr(self, "bess_soc_kwh", 0.0), cfg.bess_mwh * 1000.0)
        if old is not None and old.bess_mwh == 0 and cfg.bess_mwh > 0:
            self.bess_soc_kwh = cfg.bess_mwh * 1000.0
        n_inf = int(round(self.n * cfg.inference_share))
        self.role = np.where(np.arange(self.n) < n_inf, INFERENCE, TRAINING)
        self.rebuilt = rebuilt

    # ----- operator and grid actions

    def set_load_target(self, fraction: float = 0.75, duration_min: float = 60, ramp_min: float = 10) -> dict:
        """A grid services provider asks the site to hold below a power limit (DSX Flex)."""
        fraction = min(1.0, max(0.1, fraction))
        corr = str(uuid.uuid4())
        self.target = {
            "correlation_id": corr,
            "limit_kw": self.cfg.site_mw * 1000 * fraction,
            "fraction": fraction,
            "start_t": self.t_min,
            "end_t": self.t_min + duration_min,
            "ramp_min": ramp_min,
            "ramp_from_kw": self.snapshot.get("power", {}).get("grid_kw", self.cfg.site_mw * 1000),
            "phase": "ramp_down",
            "ramp_up_start": None,
        }
        self.events.append({"kind": "flex.loadtarget.set", "target": dict(self.target)})
        self.events.append({"kind": "flex.status", "event": "target_set"})
        self.events.append({"kind": "flex.status", "event": "start_ramp_down"})
        self._log(
            "grid",
            f"Grid load target: hold below {self.target['limit_kw'] / 1000:.1f} MW "
            f"({fraction:.0%} of connection) for {duration_min:.0f} min",
        )
        return self.target

    def inject_fault(self, kind: str, index: int | None = None) -> dict:
        if kind == "pump_failure":
            row = int(index if index is not None else self.rng.integers(0, self.rows))
            row = min(max(row, 0), self.rows - 1)
            fault = {"kind": kind, "row": row, "until": self.t_min + 120}
            self.flow_frac[self.row_of == row] = 0.4
            self.events.append({"kind": "bms.cdu.status", "row": row, "value": 0})
            self._log("fault", f"CDU pump failure on row {row + 1}: flow down to 40%, racks throttle")
        elif kind == "leak":
            rack = int(index if index is not None else self.rng.integers(0, self.n))
            rack = min(max(rack, 0), self.n - 1)
            fault = {"kind": kind, "rack": rack, "until": self.t_min + 240}
            self.isolated[rack] = True
            self.events.append({"kind": "bms.rack.leak", "rack": rack, "value": 1})
            self.events.append({"kind": "bms.rack.isolation_request", "rack": rack, "value": 1})
            self.events.append({"kind": "bms.rack.isolation_status", "rack": rack, "value": 1})
            self._log("fault", f"Leak detected at rack {self.rack_id(rack)}: liquid and power isolated")
        elif kind == "heat_wave":
            fault = {"kind": kind, "until": self.t_min + 360, "delta_c": 10.0}
            self._log("weather", "Heat wave: outdoor temperature +10 C for 6 hours")
        elif kind == "demand_surge":
            fault = {"kind": kind, "until": self.t_min + 120, "multiplier": 1.5}
            self._log("demand", "Demand surge: inference requests x1.5 for 2 hours")
        else:
            raise ValueError(f"unknown fault {kind!r}")
        self.faults.append(fault)
        return fault

    # ----- helpers

    def rack_id(self, i: int) -> str:
        return f"R{i // RACKS_PER_ROW + 1:02d}-{i % RACKS_PER_ROW + 1:02d}"

    def _log(self, category: str, text: str) -> None:
        self.events.append({"kind": "log", "category": category, "text": text, "t": self.t_min})

    def clock(self) -> str:
        day = int(self.t_min // 1440) + 1
        minutes = int(self.t_min % 1440)
        return f"Day {day} {minutes // 60:02d}:{minutes % 60:02d}"

    def _fault(self, kind: str) -> dict | None:
        return next((f for f in self.faults if f["kind"] == kind), None)

    def _expire_faults(self) -> None:
        keep = []
        for f in self.faults:
            if self.t_min < f["until"]:
                keep.append(f)
                continue
            if f["kind"] == "pump_failure":
                self.flow_frac[self.row_of == f["row"]] = 1.0
                self.events.append({"kind": "bms.cdu.status", "row": f["row"], "value": 1})
                self._log("fault", f"CDU on row {f['row'] + 1} repaired")
            elif f["kind"] == "leak":
                self.isolated[f["rack"]] = False
                self.events.append({"kind": "bms.rack.leak", "rack": f["rack"], "value": 0})
                self.events.append({"kind": "bms.rack.isolation_status", "rack": f["rack"], "value": 0})
                self._log("fault", f"Rack {self.rack_id(f['rack'])} repaired and back in service")
        self.faults = keep

    # ----- the plant, evaluated at one control setting

    def _evaluate(self, s: float, ambient_c: float, demand_frac: float) -> dict[str, Any]:
        """Site state for control setting s in [0, 1].

        s = 0 runs every rack at its ceiling (MaxP, or MaxQ under that policy).
        s = 1 holds every rack at the minimum cap. The cap is the same for all racks;
        a rack that is not busy draws less than its cap anyway, which is how unused
        headroom ends up with the racks that need it.
        """
        cfg, plat, ref, f = self.cfg, self.plat, self.cat.reference, self.cat.facility
        ceiling = plat.maxq_fraction if cfg.policy == "maxq" else 1.0
        floor = min(ref["min_cap"], ceiling)
        c_train = c_inf = ceiling - s * (ceiling - floor)
        thermal_cap = np.where(self.flow_frac < 1.0, np.maximum(floor, self.flow_frac), 1.0)
        cap = np.minimum(np.where(self.role == TRAINING, c_train, c_inf), thermal_cap)

        active = ~(self.suspended | self.isolated)
        inf = self.role == INFERENCE
        trn = ~inf
        gpu_tps = ref["h100_tokens_per_sec"] * plat.perf_index("inference", cfg.claim_credit)
        rack_tps = plat.gpus_per_rack * gpu_tps * cap_perf(cap, ref["inference_k"])
        nameplate_tps = float(inf.sum()) * plat.gpus_per_rack * gpu_tps
        demand_tps = demand_frac * nameplate_tps
        capacity_tps = float(rack_tps[inf & active].sum())
        u_base = min(1.0, demand_tps / capacity_tps) if capacity_tps > 0 else 0.0

        util = np.zeros(self.n)
        live_inf = inf & active
        spread = self.jitter - (self.jitter[live_inf].mean() if live_inf.any() else 0.0)
        util[inf] = np.clip(u_base * (1 + spread[inf]), 0, 1)
        util[trn] = 1.0
        util[~active] = 0.0
        served_tps = min(demand_tps, float((rack_tps * util)[inf].sum()))

        p_max = plat.rack_kw_maxp
        p_idle = ref["idle_power_fraction"] * p_max
        power = p_idle + (cap * p_max - p_idle) * util
        power[self.suspended] = SUSPENDED_POWER_FRACTION * p_max
        power[self.isolated] = 0.0

        gpu_kw = float(power.sum())
        it_kw = gpu_kw * (1 + f["network_storage_overhead"])
        cool = cooling_power(
            cat=self.cat,
            plat=plat,
            heat_kw=it_kw,
            racks_flowing=float((~self.isolated).sum()),
            supply_c=cfg.supply_c,
            ambient_c=ambient_c,
        )
        misc_kw = f["misc_load"] * it_kw
        facility_kw = it_kw + cool["total_kw"] + misc_kw
        load_kw = facility_kw / (1 - f["distribution_loss"])

        train_idx = plat.perf_index("training", cfg.claim_credit)
        train_rate = float((plat.gpus_per_rack * train_idx * cap_perf(cap, ref["training_k"]) * util)[trn].sum())
        return {
            "s": s, "cap": cap, "util": util, "power": power, "gpu_kw": gpu_kw, "it_kw": it_kw,
            "cool": cool, "misc_kw": misc_kw, "load_kw": load_kw,
            "loss_kw": load_kw - facility_kw, "served_tps": served_tps, "demand_tps": demand_tps,
            "nameplate_tps": nameplate_tps, "train_rate": train_rate,
            "c_train": c_train, "c_inf": c_inf,
        }  # fmt: skip

    def _solve(self, limit_kw: float, ambient_c: float, demand_frac: float) -> dict[str, Any]:
        """The power control loop: the loosest setting that keeps the site under its limit."""
        loose = self._evaluate(0.0, ambient_c, demand_frac)
        if loose["load_kw"] <= limit_kw:
            return loose
        tight = self._evaluate(1.0, ambient_c, demand_frac)
        if tight["load_kw"] > limit_kw:
            return tight  # capping alone cannot get there
        lo, hi, best = 0.0, 1.0, tight
        for _ in range(22):
            mid = (lo + hi) / 2
            trial = self._evaluate(mid, ambient_c, demand_frac)
            if trial["load_kw"] <= limit_kw:
                hi, best = mid, trial
            else:
                lo = mid
        return best

    # ----- one tick

    def step(self, dt_min: float = 5.0) -> dict[str, Any]:
        self.t_min += dt_min
        self._expire_faults()
        cfg, plat = self.cfg, self.plat
        site_kw = cfg.site_mw * 1000.0
        hour = (self.t_min / 60.0) % 24

        # Weather and demand
        if self.steady:
            ambient, demand_frac = self.climate.mean_c, cfg.demand
        else:
            ambient = self.climate.mean_c + self.climate.swing_c * math.sin(2 * math.pi * (hour - 9) / 24)
            demand_frac = cfg.demand * (1 + 0.25 * math.sin(2 * math.pi * (hour - 8) / 24))
            demand_frac *= 1 + float(self.rng.normal(0, 0.015))
        if hw := self._fault("heat_wave"):
            ambient += hw["delta_c"]
        if surge := self._fault("demand_surge"):
            demand_frac *= surge["multiplier"]

        # Grid limit for this tick
        limit_kw, flex_events = self._advance_target(site_kw)

        # Battery can cover part of a curtailment so the IT load does not have to
        bess_rating = cfg.bess_mwh * 1000.0  # 1C
        hours = dt_min / 60.0
        bess_avail = min(bess_rating, self.bess_soc_kwh / hours) if self.target else 0.0

        sol = self._solve(limit_kw + bess_avail, ambient, demand_frac)
        discharge = min(bess_avail, max(0.0, sol["load_kw"] - limit_kw))
        charge = 0.0
        if not self.target and self.bess_soc_kwh < cfg.bess_mwh * 1000.0 and bess_rating > 0:
            room = (cfg.bess_mwh * 1000.0 - self.bess_soc_kwh) / hours
            charge = max(0.0, min(0.25 * bess_rating, site_kw - sol["load_kw"], room))
        self.bess_soc_kwh += (charge - discharge) * hours
        grid_kw = sol["load_kw"] - discharge + charge

        # Breach handling (DSX Flex): alert, enforce by suspending racks, resolve
        sol, grid_kw = self._handle_breach(sol, grid_kw, limit_kw, discharge, ambient, demand_frac)
        for ev in flex_events:
            self.events.append({"kind": "flex.status", "event": ev})
        if self.target and self.t_min - self.last_flex_status_t >= 5:
            self.events.append({"kind": "flex.status", "event": "periodic"})
        if any(e["kind"] == "flex.status" for e in self.events):
            self.last_flex_status_t = self.t_min

        # Per-rack thermal state
        dt_k = sol["cool"]["dt_k"]
        heat_frac = sol["power"] / plat.rack_kw_maxp
        return_c = cfg.supply_c + dt_k * heat_frac / np.maximum(self.flow_frac, 0.05)
        if plat.cooling == "air":
            return_c = 24 + 14 * heat_frac  # exhaust air temperature, for display only
        state = np.full(self.n, OK)
        state[self.flow_frac < 1.0] = THROTTLED
        state[self.suspended] = SUSPENDED
        state[self.isolated] = ISOLATED
        self.state = state

        self.snapshot = self._snapshot(sol, grid_kw, limit_kw, site_kw, ambient, discharge, charge, return_c)
        p = self.snapshot["power"]
        w = self.snapshot["work"]
        self.history.append({
            "t": self.t_min, "grid_kw": p["grid_kw"], "limit_kw": p["limit_kw"],
            "tokens": w["tokens_per_s"], "demand": w["demand_tokens_per_s"], "pue": p["pue"],
            "ambient": ambient, "cooling_kw": p["cooling_kw"],
        })  # fmt: skip
        return self.snapshot

    def _advance_target(self, site_kw: float) -> tuple[float, list[str]]:
        """Move the DSX Flex target through its lifecycle and return this tick's limit."""
        tg = self.target
        if tg is None:
            return site_kw, []
        events: list[str] = []
        t = self.t_min
        if tg["phase"] == "ramp_down":
            x = min(1.0, (t - tg["start_t"]) / max(tg["ramp_min"], 1e-6))
            limit = tg["ramp_from_kw"] + (tg["limit_kw"] - tg["ramp_from_kw"]) * x
            if x >= 1.0:
                tg["phase"] = "hold"
                events.append("end_ramp_down")
            return min(limit, site_kw), events
        if tg["phase"] == "hold":
            if t >= tg["end_t"]:
                tg["phase"] = "ramp_up"
                tg["ramp_up_start"] = t
                events.append("start_ramp_up")
                self._log("grid", "Grid load target ended: ramping back up")
            return tg["limit_kw"], events
        x = min(1.0, (t - tg["ramp_up_start"]) / max(tg["ramp_min"], 1e-6))
        limit = tg["limit_kw"] + (site_kw - tg["limit_kw"]) * x
        if x >= 1.0:
            events.append("end_ramp_up")
            self._revert_enforcement()
            self.target = None
        return limit, events

    def _handle_breach(self, sol, grid_kw, limit_kw, discharge, ambient, demand_frac):
        tg = self.target
        holding = tg is not None and tg["phase"] == "hold"
        over = holding and grid_kw > tg["limit_kw"] * 1.02
        if over and self.breach is None:
            excess = grid_kw / tg["limit_kw"] - 1
            hints = [
                {"resource_id": self.rack_id(int(i)), "priority": n + 1} for n, i in enumerate(self._shed_order()[:8])
            ]
            self.breach = {
                "breach_id": str(uuid.uuid4()), "detected_t": self.t_min, "status": "active",
                "severity": "critical" if excess > 0.20 else "warning",
                "measured_kw": grid_kw, "enforced": False, "hints": hints,
            }  # fmt: skip
            self.events.append({"kind": "flex.breach", "breach": dict(self.breach)})
            self._log(
                "grid",
                f"Power breach: {grid_kw / 1000:.2f} MW against a {tg['limit_kw'] / 1000:.2f} MW target. "
                "Capping is exhausted, so racks must be suspended.",
            )
        elif over and self.breach and not self.breach["enforced"]:
            results = []
            for i in self._shed_order():
                if grid_kw <= tg["limit_kw"]:
                    break
                self.suspended[i] = True
                results.append(self.rack_id(int(i)))
                sol = self._evaluate(1.0, ambient, demand_frac)
                grid_kw = sol["load_kw"] - discharge
            self.breach["enforced"] = True
            self.breach["suspended_racks"] = results
            self.events.append({"kind": "flex.enforcement", "action": "applied", "racks": results,
                                "breach_id": self.breach["breach_id"]})  # fmt: skip
            self._log("grid", f"Enforcement applied: {len(results)} racks suspended, training first")
        elif self.breach and self.breach["status"] != "resolved" and (not over):
            self.breach["status"] = "resolved"
            self.breach["measured_kw"] = grid_kw
            self.events.append({"kind": "flex.breach", "breach": dict(self.breach)})
            self._log("grid", "Power breach resolved: site is back under the target")
        return sol, grid_kw

    def _shed_order(self) -> np.ndarray:
        """Racks in the order they would be suspended: training first, from the back of the hall."""
        idx = np.arange(self.n)
        live = ~(self.suspended | self.isolated)
        trn = idx[(self.role == TRAINING) & live][::-1]
        inf = idx[(self.role == INFERENCE) & live][::-1]
        return np.concatenate([trn, inf])

    def _revert_enforcement(self) -> None:
        if self.breach and self.breach.get("enforced"):
            self.suspended[:] = False
            self.breach["status"] = "resolved"
            self.events.append({"kind": "flex.enforcement", "action": "reverted",
                                "racks": self.breach.get("suspended_racks", []),
                                "breach_id": self.breach["breach_id"]})  # fmt: skip
            self._log("grid", "Enforcement reverted: suspended racks resume")
        self.breach = None

    # ----- outputs

    def _snapshot(self, sol, grid_kw, limit_kw, site_kw, ambient, discharge, charge, return_c):
        cfg, plat, f = self.cfg, self.plat, self.cat.facility
        cool = sol["cool"]
        inf = self.role == INFERENCE
        live = ~(self.suspended | self.isolated)
        gpu_kw, it_kw = sol["gpu_kw"], sol["it_kw"]
        pue = sol["load_kw"] / it_kw if it_kw > 0 else 0.0

        # Money. Inference carries the share of site cost that matches its share of rack power.
        inf_power_share = float(sol["power"][inf].sum()) / gpu_kw if gpu_kw > 0 else 0.0
        energy_usd_h = grid_kw / 1000.0 * cfg.price_usd_mwh
        capex_usd_h = (
            self.n * plat.rack_capex_usd_m * 1e6 / f["it_amortization_years"]
            + cfg.site_mw * f["facility_capex_usd_m_per_mw"] * 1e6 / f["facility_amortization_years"]
        ) / 8760.0
        mtok_h = sol["served_tps"] * 3600 / 1e6
        inf_grid_mw = grid_kw * inf_power_share / 1000.0

        def mean(mask, arr):
            return float(arr[mask].mean()) if mask.any() else 0.0

        return {
            "t_min": self.t_min,
            "clock": self.clock(),
            "config": asdict(cfg),
            "platform": {"key": plat.key, "name": plat.name, "cooling": plat.cooling,
                         "rack_kw_maxp": plat.rack_kw_maxp, "gpus_per_rack": plat.gpus_per_rack,
                         "return_limit_c": plat.return_limit_c, "max_supply_c": plat.max_supply_c},
            "site": {
                "racks": self.n, "gpus": self.n * plat.gpus_per_rack, "rows": self.rows,
                "racks_per_row": RACKS_PER_ROW, "design_pue": self.sizing["design_pue"],
                "sizing_rack_kw": self.sizing["sizing_rack_kw"],
                "inference_racks": int(inf.sum()), "training_racks": int((~inf).sum()),
            },
            "power": {
                "grid_kw": grid_kw, "limit_kw": limit_kw, "site_kw": site_kw,
                "gpu_kw": gpu_kw, "network_kw": it_kw - gpu_kw, "it_kw": it_kw,
                "chiller_kw": cool["chiller_kw"], "fans_kw": cool["fans_kw"] + cool["crah_kw"],
                "pumps_kw": cool["pumps_kw"], "cooling_kw": cool["total_kw"],
                "misc_kw": sol["misc_kw"], "loss_kw": sol["loss_kw"],
                "bess_discharge_kw": discharge, "bess_charge_kw": charge,
                "bess_soc": self.bess_soc_kwh / (cfg.bess_mwh * 1000.0) if cfg.bess_mwh > 0 else None,
                "headroom_kw": max(0.0, limit_kw - grid_kw), "pue": pue,
            },
            "cooling": {
                "ambient_c": ambient, "supply_c": cfg.supply_c if plat.cooling == "liquid" else None,
                "return_c": mean(live, return_c), "mode": cool["mode"],
                "chiller_share": cool["chiller_share"], "flow_lpm_per_rack": cool["flow_lpm_per_rack"],
                "heat_kw": it_kw,
            },
            "work": {
                "tokens_per_s": sol["served_tps"], "demand_tokens_per_s": sol["demand_tps"],
                "unserved_tokens_per_s": max(0.0, sol["demand_tps"] - sol["served_tps"]),
                "nameplate_tokens_per_s": sol["nameplate_tps"],
                "train_h100_gpu_hours_per_h": sol["train_rate"],
                "util_inference": mean(inf & live, sol["util"]),
                "cap_inference": sol["c_inf"], "cap_training": sol["c_train"],
                "control": sol["s"], "suspended": int(self.suspended.sum()),
                "isolated": int(self.isolated.sum()),
            },
            "econ": {
                "energy_usd_h": energy_usd_h, "capex_usd_h": capex_usd_h,
                "tokens_per_s_per_mw": sol["served_tps"] / inf_grid_mw if inf_grid_mw > 0 else 0.0,
                "tokens_per_kwh": sol["served_tps"] * 3600 / (inf_grid_mw * 1000) if inf_grid_mw > 0 else 0.0,
                "energy_usd_per_mtok": energy_usd_h * inf_power_share / mtok_h if mtok_h > 0 else None,
                "allin_usd_per_mtok": (energy_usd_h + capex_usd_h) * inf_power_share / mtok_h
                if mtok_h > 0 else None,
            },
            "flex": self._flex_state(grid_kw),
            "faults": [dict(x) for x in self.faults],
            "racks": {
                "power_frac": np.round(sol["power"] / plat.rack_kw_maxp, 3).tolist(),
                "util": np.round(sol["util"], 3).tolist(),
                "cap": np.round(sol["cap"], 3).tolist(),
                "return_c": np.round(return_c, 1).tolist(),
                "state": self.state.tolist(),
                "role": self.role.tolist(),
                "flow_frac": np.round(self.flow_frac, 2).tolist(),
            },
        }  # fmt: skip

    def _flex_state(self, grid_kw: float) -> dict[str, Any]:
        tg = self.target
        if tg is None:
            return {"active": False, "compliant": True, "in_flight": False, "breach": None}
        return {
            "active": True,
            "correlation_id": tg["correlation_id"],
            "target_kw": tg["limit_kw"],
            "phase": tg["phase"],
            "in_flight": tg["phase"] != "hold",
            "compliant": grid_kw <= tg["limit_kw"] * 1.005 or tg["phase"] != "hold",
            "ends_in_min": max(0.0, tg["end_t"] - self.t_min),
            "start_t": tg["start_t"], "end_t": tg["end_t"],
            "breach": dict(self.breach) if self.breach else None,
        }  # fmt: skip

    def drain_events(self) -> list[dict[str, Any]]:
        out, self.events = self.events, []
        return out


# ------------------------------------------------------------------------ comparisons


def summarize(snap: dict[str, Any]) -> dict[str, Any]:
    return {
        "platform": snap["platform"]["key"],
        "name": snap["platform"]["name"],
        "policy": snap["config"]["policy"],
        "racks": snap["site"]["racks"],
        "gpus": snap["site"]["gpus"],
        "design_pue": snap["site"]["design_pue"],
        "pue": snap["power"]["pue"],
        "grid_mw": snap["power"]["grid_kw"] / 1000,
        "cooling_mode": snap["cooling"]["mode"],
        "cooling_kw": snap["power"]["cooling_kw"],
        "supply_c": snap["cooling"]["supply_c"],
        "tokens_per_s": snap["work"]["tokens_per_s"],
        "tokens_per_s_per_mw": snap["econ"]["tokens_per_s_per_mw"],
        "tokens_per_kwh": snap["econ"]["tokens_per_kwh"],
        "energy_usd_per_mtok": snap["econ"]["energy_usd_per_mtok"],
        "allin_usd_per_mtok": snap["econ"]["allin_usd_per_mtok"],
        "cap_inference": snap["work"]["cap_inference"],
    }


def compare(cfg: SiteConfig, cat: Catalog | None = None) -> dict[str, Any]:
    """Same grid connection, same climate, same demand: every platform and every policy.

    Runs each case in steady state (average weather, flat demand, all racks on
    inference, sold out) so the only thing that differs is the hardware or the policy.
    """
    cat = cat or load_catalog()
    base = replace(cfg, inference_share=1.0, demand=1.0, bess_mwh=0.0)
    platforms = []
    for key, plat in cat.platforms.items():
        c = replace(base, platform=key, supply_c=min(cfg.supply_c, plat.max_supply_c))
        platforms.append(summarize(Engine(c, cat, steady=True).step()))
    policies = []
    for policy in POLICIES:
        policies.append(summarize(Engine(replace(base, policy=policy), cat, steady=True).step()))
    return {"platforms": platforms, "policies": policies, "config": asdict(base.validated(cat))}
