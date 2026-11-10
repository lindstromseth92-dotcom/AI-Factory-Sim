"""Checks on the physics and sizing math, including the two calibration points."""

import copy
from dataclasses import replace

import pytest

from factorysim.catalog import load_catalog
from factorysim.engine import Engine, SiteConfig, cap_perf, compare, cooling_power, size_site


@pytest.fixture(scope="module")
def cat():
    return load_catalog()


def test_reproduces_nvidia_maxlps_gpu_count(cat):
    """NVIDIA's public example: 400 Vera Rubin GPUs per MW under MaxLPS at PUE 1.1."""
    bare = copy.deepcopy(cat)
    bare.facility["network_storage_overhead"] = 0.0
    cfg = SiteConfig(platform="vera_rubin_nvl72", site_mw=1, policy="maxlps", oversubscription=1.4)
    gpus = size_site(cfg, bare, integer=False, pue_override=1.1)["gpus"]
    assert gpus == pytest.approx(400, rel=0.01)


def test_reproduces_nvidia_maxlps_throughput_gain(cat):
    """Same example: 1.4x the GPUs yields about 1.35x the tokens."""
    result = {p["policy"]: p for p in compare(SiteConfig(site_mw=20), cat)["policies"]}
    assert result["maxlps"]["gpus"] / result["maxp"]["gpus"] == pytest.approx(1.4, abs=0.03)
    assert result["maxlps"]["tokens_per_s"] / result["maxp"]["tokens_per_s"] == pytest.approx(1.35, abs=0.03)
    assert result["maxp"]["tokens_per_s"] < result["maxq"]["tokens_per_s"] < result["maxlps"]["tokens_per_s"]


def test_coolant_flow_matches_lenovo_gb300_table(cat):
    """Lenovo's GB300 guide: 59 L/min at 25 C supply, 177 L/min at 45 C."""
    plat = replace(cat.platforms["gb300_nvl72"], rack_kw_maxp=135)
    cold = cooling_power(cat, plat, 135, 1, 25, 10)["flow_lpm_per_rack"]
    warm = cooling_power(cat, plat, 135, 1, 45, 10)["flow_lpm_per_rack"]
    assert cold == pytest.approx(59, rel=0.05)
    assert warm == pytest.approx(177, rel=0.08)


def test_power_cap_curve_shape(cat):
    k = cat.reference["inference_k"]
    assert cap_perf(1.0, k) == 1.0
    assert cap_perf(1 / 1.4, k) == pytest.approx(1.35 / 1.4, abs=0.002)
    assert cap_perf(0.5, k) < cap_perf(0.8, k) < 1.0
    # Training loses more than inference at the same cap.
    assert cap_perf(0.7, cat.reference["training_k"]) < cap_perf(0.7, k)


def test_warm_water_avoids_the_chiller(cat):
    plat = cat.platforms["vera_rubin_nvl72"]
    warm = cooling_power(cat, plat, 1000, 5, 45, 30)
    cold = cooling_power(cat, plat, 1000, 5, 25, 30)
    assert warm["mode"] == "free" and warm["chiller_kw"] == 0
    assert cold["chiller_kw"] > 0
    assert cold["total_kw"] > warm["total_kw"]


def test_air_cooling_costs_more_than_liquid(cat):
    air = size_site(SiteConfig(platform="hgx_h100", supply_c=27), cat)
    liquid = size_site(SiteConfig(platform="vera_rubin_nvl72"), cat)
    assert air["design_pue"] > 1.25
    assert liquid["design_pue"] < 1.15


def test_each_generation_beats_the_last_per_megawatt(cat):
    rows = compare(SiteConfig(site_mw=10), cat)["platforms"]
    per_mw = [r["tokens_per_s_per_mw"] for r in rows]
    assert per_mw == sorted(per_mw)
    cost = [r["allin_usd_per_mtok"] for r in rows]
    assert cost == sorted(cost, reverse=True)


def test_claim_credit_moves_the_gap(cat):
    def ratio(credit):
        rows = {r["platform"]: r for r in compare(SiteConfig(claim_credit=credit), cat)["platforms"]}
        return rows["vera_rubin_nvl72"]["tokens_per_s_per_mw"] / rows["gb200_nvl72"]["tokens_per_s_per_mw"]

    assert ratio(0.0) < ratio(0.5) < ratio(1.0)
    assert ratio(1.0) == pytest.approx(10, rel=0.25)  # NVIDIA: 10x tokens per MW vs Blackwell


def test_site_never_exceeds_grid_connection(cat):
    for platform in cat.platforms:
        for policy in ("maxp", "maxq", "maxlps"):
            eng = Engine(SiteConfig(platform=platform, policy=policy, demand=1.2), cat)
            for _ in range(300):  # a full simulated day
                snap = eng.step(5)
                assert snap["power"]["grid_kw"] <= snap["power"]["site_kw"] * 1.001, (platform, policy)


def test_energy_balance(cat):
    snap = Engine(SiteConfig(), cat, steady=True).step()
    p = snap["power"]
    parts = p["gpu_kw"] + p["network_kw"] + p["cooling_kw"] + p["misc_kw"] + p["loss_kw"]
    assert parts == pytest.approx(p["grid_kw"], rel=1e-6)
    assert snap["cooling"]["heat_kw"] == pytest.approx(p["it_kw"])


def test_config_is_validated(cat):
    with pytest.raises(ValueError):
        SiteConfig(platform="nope").validated(cat)
    cfg = SiteConfig(platform="hgx_h100", supply_c=45, site_mw=500).validated(cat)
    assert cfg.supply_c == 27 and cfg.site_mw == 50
