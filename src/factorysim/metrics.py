"""Prometheus metrics for the simulated site.

GPU telemetry uses the metric names NVIDIA's dcgm-exporter emits (DCGM field
identifiers), so a Grafana dashboard built for a real GPU cluster reads this one
too. To keep the series count sane, each rack reports one line per metric: the
average GPU in that rack. Site-level numbers use the factorysim_ prefix.
"""

from __future__ import annotations

from typing import Any

from .engine import STATE_NAMES


def _line(name: str, labels: dict[str, Any], value: float) -> str:
    body = ",".join(f'{k}="{v}"' for k, v in labels.items())
    return f"{name}{{{body}}} {value:.6g}" if body else f"{name} {value:.6g}"


def render(snap: dict[str, Any], rack_ids: list[str], bus: dict[str, Any]) -> str:
    if not snap:
        return "# simulator has not produced a tick yet\n"
    plat, racks, power, work, cooling = (snap[k] for k in ("platform", "racks", "power", "work", "cooling"))
    gpus = plat["gpus_per_rack"]
    out: list[str] = []

    def family(name: str, kind: str, help_text: str) -> None:
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} {kind}")

    dcgm = [
        ("DCGM_FI_DEV_GPU_UTIL", "GPU utilization (in %).",
         lambda i: racks["util"][i] * 100),
        ("DCGM_FI_DEV_POWER_USAGE", "Power draw (in W).",
         lambda i: racks["power_frac"][i] * plat["rack_kw_maxp"] * 1000 / gpus),
        ("DCGM_FI_DEV_POWER_MGMT_LIMIT", "Current power limit (in W).",
         lambda i: racks["cap"][i] * plat["rack_kw_maxp"] * 1000 / gpus),
        # Synthetic: coolant return temperature plus a fixed chip-to-coolant rise.
        ("DCGM_FI_DEV_GPU_TEMP", "GPU temperature (in C).",
         lambda i: racks["return_c"][i] + 12 if racks["power_frac"][i] > 0 else 0),
    ]  # fmt: skip
    for name, help_text, fn in dcgm:
        family(name, "gauge", help_text)
        for i, rid in enumerate(rack_ids):
            labels = {"gpu": "avg", "UUID": f"GPU-sim-{rid}", "modelName": plat["name"], "Hostname": rid}
            out.append(_line(name, labels, fn(i)))

    site = [
        ("factorysim_grid_power_watts", "Power drawn from the grid.", power["grid_kw"] * 1000),
        ("factorysim_power_limit_watts", "Grid limit in force this tick.", power["limit_kw"] * 1000),
        ("factorysim_it_power_watts", "IT load: GPU racks plus network and storage.", power["it_kw"] * 1000),
        ("factorysim_pue", "Power usage effectiveness: site load over IT load.", power["pue"]),
        ("factorysim_tokens_per_second", "Inference tokens served per second.", work["tokens_per_s"]),
        ("factorysim_tokens_demand_per_second", "Inference tokens requested per second.",
         work["demand_tokens_per_s"]),
        ("factorysim_training_h100_gpu_hours_per_hour", "Training output in H100-equivalent GPU-hours per hour.",
         work["train_h100_gpu_hours_per_h"]),
        ("factorysim_power_cap_ratio", "Rack power cap set by the control loop, as a fraction of MaxP.",
         work["cap_inference"]),
        ("factorysim_outdoor_temperature_celsius", "Outdoor air temperature.", cooling["ambient_c"]),
        ("factorysim_chiller_share", "Fraction of heat that needs the chiller.", cooling["chiller_share"]),
        ("factorysim_flex_target_active", "1 while a DSX Flex load target is in force.",
         float(snap["flex"]["active"])),
        ("factorysim_flex_compliant", "1 while the site is within its Flex target.",
         float(snap["flex"]["compliant"])),
        ("factorysim_bus_messages_total", "Messages published to the event bus.", bus["published_total"]),
    ]  # fmt: skip
    for name, help_text, value in site:
        family(name, "counter" if name.endswith("_total") else "gauge", help_text)
        out.append(_line(name, {}, value))

    family("factorysim_cooling_power_watts", "gauge", "Cooling plant power by component.")
    for comp in ("chiller", "fans", "pumps"):
        out.append(_line("factorysim_cooling_power_watts", {"component": comp}, power[f"{comp}_kw"] * 1000))
    family("factorysim_racks", "gauge", "Racks by state.")
    for code, label in STATE_NAMES.items():
        out.append(_line("factorysim_racks", {"state": label}, sum(1 for s in racks["state"] if s == code)))
    cost = snap["econ"]["allin_usd_per_mtok"]
    if cost is not None:
        family("factorysim_cost_usd_per_million_tokens", "gauge", "All-in cost per million tokens.")
        out.append(_line("factorysim_cost_usd_per_million_tokens", {}, cost))
    return "\n".join(out) + "\n"
