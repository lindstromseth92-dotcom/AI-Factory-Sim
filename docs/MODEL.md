# The model

Everything the simulator computes, in the order power flows through the site. Each
constant is tagged in `src/factorysim/catalog.yaml` as official, reported, derived or
assumed. This file explains the derived ones and is honest about the assumed ones.

## 1. Sizing: how many racks fit

A site has a grid connection of `site_mw`. On the hottest design day, each rack costs

    grid kW per rack = sizing_kw × (1 + network overhead) × design PUE

and the hall holds `floor(site_kw / that)` racks. `sizing_kw` depends on the policy:

| Policy | Budget per rack | Idea |
|---|---|---|
| MaxP | rack MaxP power | every rack can peak at once |
| MaxQ | MaxP × MaxQ fraction | every GPU held at its efficient point, fixed |
| MaxLPS | MaxP ÷ oversubscription | install more than the connection can feed at peak; a control loop shares power |

**Check against NVIDIA.** NVIDIA's MaxLPS overview gives one worked example: Vera Rubin
NVL72, 1 MW, PUE 1.1, 400 GPUs under MaxLPS, "up to 40% more GPUs". With a 230 kW
rack, 1.4× oversubscription and no network overhead the formula gives
`1000 / 1.1 / (230 / 1.4) × 72 = 398 GPUs`. That agreement is why I trust the 230 kW
figure, which NVIDIA has not published directly. `tests/test_model.py` pins it.

## 2. Rack power and throughput

A rack at power cap `c` (fraction of MaxP) and utilization `u` draws

    P = P_idle + (c × P_maxp − P_idle) × u        P_idle = 0.30 × P_maxp  (assumed)

and delivers `perf(c) = 1 − k (1 − c)²` of its uncapped throughput.

For inference `k = 0.44`, fitted to the same NVIDIA example: 1.4× the GPUs gave 1.35×
the tokens, so each GPU at `c = 1/1.4` kept `1.35/1.4 = 96.4%` of its speed. One data
point fixes one parameter; the quadratic shape is my choice. For training `k = 1.0`
(assumed), because NVIDIA says training has less headroom but gives no number.

Tokens per second for one GPU = `1000 × perf index × perf(c) × u`. The 1000 is an
arbitrary anchor (one H100 on a reference model). Every comparison in the app is a
ratio, so the anchor cancels; absolute token and dollar figures scale with it.

## 3. Performance index, and the claim slider

Per-GPU throughput relative to an H100 comes in two versions:

| | H100 | GB200 | GB300 | Vera Rubin | Basis |
|---|---|---|---|---|---|
| Inference, chip compute | 1 | 5 | 7.5 | 25 | peak PFLOPS per GPU from NVIDIA spec tables |
| Inference, vendor claim | 1 | 30 | 45 | 475 | NVIDIA full-stack claims, compounded |
| Training, chip compute | 1 | 2.5 | 3.75 | 8.75 | same |
| Training, vendor claim | 1 | 4 | 6 | 16 | same |

The claim column chains "30× H100 for real-time LLM inference" (GB200), "1.5× GB200"
(GB300) and "10× tokens per MW vs Blackwell" (Vera Rubin; 10× per MW at 190 kW vs
120 kW is 15.8× per rack). Those claims are for specific models and latency targets.
The slider blends the two columns geometrically: `index = compute^(1−x) × claim^x`.
At 0 the app is conservative. At 1 it reproduces the keynote numbers.

## 4. The control loop

Every tick the loop finds the highest common power cap that keeps the site at or
under its limit (the grid connection, or a DSX Flex target while one is active). It
is a bisection on one number. A rack that is not busy draws less than its cap, which
is how spare power reaches the racks that need it. This stands in for NVIDIA's
Dynamic Power Software, which does the same job per GPU through each server's BMC.

If the minimum cap (50%) still leaves the site over a Flex target, the model raises a
breach alert and then suspends racks, training first, until it complies.

## 5. Cooling

Heat equals IT power. A platform sends `liquid_fraction` of it to a water loop and the
rest to room air.

**Flow.** A rack's coolant flow is sized so a MaxP rack returns water at the
platform's return limit:

    flow (L/min) = heat_kW / (0.0663 × (T_return_limit − T_supply))

0.0663 kW per L/min per K is 25% propylene glycol. The 56 °C return limit for GB300
is back-solved from Lenovo's published table (59 L/min at 25 °C supply, 177 L/min at
45 °C, for about 122 kW of liquid heat); the formula reproduces both ends within 8%.
Warmer supply water means a smaller allowed temperature rise, so more flow and more
pump power. Pressure drop scales with flow squared, anchored to Lenovo's 18.4 psi.

**Heat rejection.** Dry coolers can make water as cold as outdoor air plus an 8 K
approach, for fan power only. If the supply setpoint is colder than that, a chiller
does the remaining share of the temperature drop:

    chiller share = clamp((T_outdoor + 8 − T_supply) / (T_return − T_supply), 0, 1)
    chiller power = share × heat / COP,   COP = min(8, 0.5 × Carnot)

At 45 °C supply the share is zero in most climates. That is the whole argument for
warm-water cooling, and it is why a liquid-cooled site carries more racks on the same
connection. Air-side heat needs about 18 °C chilled water, so it nearly always pays
for the chiller, plus air handler fans.

## 6. Everything else between the grid and the rack

    site load = (IT + cooling + 1% misc) / (1 − 4% distribution loss)
    PUE = site load / IT

A battery, if fitted, discharges at up to 1C to cover the gap during a Flex target
and recharges at C/4 when there is spare connection.

## 7. Money

    energy $/h = grid MW × price
    capex $/h  = racks × rack cost / 5 years + site MW × $11M / 15 years
    cost per million tokens = (energy + capex) × inference share of rack power / tokens

Rack costs are press estimates. Treat the dollar outputs as directional.

## What the model leaves out

Network topology and job placement, per-GPU detail (a rack reports one average GPU),
storage, failures other than the two injected ones, water use, transient thermal
behaviour (temperatures are steady-state each tick), millisecond power dynamics,
and any real workload. Latency is not modelled, so "sold out" is the only signal
that demand exceeds capacity.
