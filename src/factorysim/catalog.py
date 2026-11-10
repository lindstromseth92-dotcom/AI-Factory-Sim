"""Load the hardware catalog and expose it as plain dataclasses.

The YAML keeps a `basis` tag next to every number (official / reported / derived /
assumed). The loader keeps those tags so the dashboard and README can show them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CATALOG_PATH = Path(__file__).with_name("catalog.yaml")


@dataclass(frozen=True)
class Platform:
    key: str
    name: str
    short: str
    generation: str
    cooling: str  # "air" or "liquid"
    gpus_per_rack: int
    rack_kw_maxp: float
    maxq_fraction: float
    liquid_fraction: float
    max_supply_c: float
    return_limit_c: float
    gpu_memory_gb: float
    perf: dict[str, dict[str, float]]
    rack_capex_usd_m: float
    basis: dict[str, str] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)

    def perf_index(self, workload: str, claim_credit: float) -> float:
        """Per-GPU throughput relative to one H100.

        claim_credit = 0 uses the ratio of peak chip compute from NVIDIA spec tables.
        claim_credit = 1 uses NVIDIA's full-stack speedup claims at face value.
        In between is a geometric blend, because speedups compound multiplicatively.
        """
        p = self.perf[workload]
        credit = min(1.0, max(0.0, claim_credit))
        return p["compute"] ** (1 - credit) * p["claim"] ** credit


@dataclass(frozen=True)
class Climate:
    key: str
    name: str
    mean_c: float
    swing_c: float
    design_c: float


@dataclass(frozen=True)
class Catalog:
    platforms: dict[str, Platform]
    climates: dict[str, Climate]
    facility: dict[str, float]
    reference: dict[str, Any]
    basis: dict[str, str]


def _leaf(node: Any) -> tuple[Any, str | None, str | None]:
    if isinstance(node, dict) and "value" in node:
        return node["value"], node.get("basis"), node.get("note")
    return node, None, None


@lru_cache(maxsize=1)
def load_catalog(path: str | None = None) -> Catalog:
    raw = yaml.safe_load(Path(path or CATALOG_PATH).read_text(encoding="utf-8"))

    platforms: dict[str, Platform] = {}
    for key, spec in raw["platforms"].items():
        values: dict[str, Any] = {}
        basis: dict[str, str] = {}
        notes: dict[str, str] = {}
        for name, node in spec.items():
            if name == "perf":
                values["perf"] = {
                    w: {"compute": float(v["compute"]), "claim": float(v["claim"])} for w, v in node.items()
                }
                for w, v in node.items():
                    if "note" in v:
                        notes[f"perf.{w}"] = v["note"]
                continue
            value, b, note = _leaf(node)
            values[name] = value
            if b:
                basis[name] = b
            if note:
                notes[name] = note
        platforms[key] = Platform(key=key, basis=basis, notes=notes, **values)

    climates = {k: Climate(key=k, **v) for k, v in raw["climates"].items()}

    facility: dict[str, float] = {}
    fac_basis: dict[str, str] = {}
    for name, node in raw["facility"].items():
        value, b, _ = _leaf(node)
        facility[name] = float(value)
        if b:
            fac_basis[f"facility.{name}"] = b

    ref = raw["reference"]
    reference = {
        "h100_tokens_per_sec": float(ref["h100_tokens_per_sec"]["value"]),
        "idle_power_fraction": float(ref["idle_power_fraction"]["value"]),
        "inference_k": float(ref["power_cap_curve"]["inference_k"]["value"]),
        "training_k": float(ref["power_cap_curve"]["training_k"]["value"]),
        "min_cap": float(ref["power_cap_curve"]["min_cap"]["value"]),
    }
    return Catalog(platforms, climates, facility, reference, fac_basis)


def catalog_as_dict(cat: Catalog) -> dict[str, Any]:
    """JSON-friendly view for the dashboard."""
    return {
        "platforms": {
            k: {
                "key": p.key,
                "name": p.name,
                "short": p.short,
                "generation": p.generation,
                "cooling": p.cooling,
                "gpus_per_rack": p.gpus_per_rack,
                "rack_kw_maxp": p.rack_kw_maxp,
                "rack_kw_maxq": round(p.rack_kw_maxp * p.maxq_fraction, 1),
                "liquid_fraction": p.liquid_fraction,
                "max_supply_c": p.max_supply_c,
                "gpu_memory_gb": p.gpu_memory_gb,
                "perf": p.perf,
                "rack_capex_usd_m": p.rack_capex_usd_m,
                "basis": p.basis,
                "notes": p.notes,
            }
            for k, p in cat.platforms.items()
        },
        "climates": {
            k: {"name": c.name, "mean_c": c.mean_c, "swing_c": c.swing_c, "design_c": c.design_c}
            for k, c in cat.climates.items()
        },
        "facility": cat.facility,
        "reference": cat.reference,
    }
