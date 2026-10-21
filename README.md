# Cloud-Connected OEE Monitoring for a Simulated SMT PCB Assembly Line

*Shao-Chi Cheng · Aldrick Peter Thomas Peter · Yi-Hung Ting · Jatin*

Surface-mount technology (SMT) assembly is the process used to build most modern printed circuit boards, and a typical line chains six machines together: a stencil printer, a solder paste inspection station, a pick-and-place machine, a reflow oven and two test stations. This repository holds our IIoT (Industrial Internet of Things) project: a working prototype that measures OEE (Overall Equipment Effectiveness) across such a line and shows where the losses come from. Since a real SMT line was not available to us, the data comes from a tick-based Python simulator that we built for the purpose. It models the machines, their consumables, wear, breakdowns and defects closely enough that OEE, first-pass yield and the downtime Pareto behave the way they do on a real line.

![Live view of the two-line factory](docs/figures/viewer_factory.png)

*The pygame view of the two-line factory at 3 h of simulated time. Each station is coloured by its state, the stacks between stations are the buffers, and line 1's pick-and-place has been slowed to 85 % from the keyboard.*

---

## 1. The problem and our objectives

An OEE figure on its own says little. In our 24-hour baseline run the line reached an OEE of 59 %, but that number alone does not say whether the time was lost to changeovers, empty feeders, paste defects or stations waiting on a neighbour, and each of these needs a different fix. A monitoring stack that claims to tell these causes apart is also hard to validate on a real line, because the true cause of a loss is rarely known. Our simulator addresses this by keeping **ground truth separate from observation**: every board carries its true defects (for example "misplacement caused by nozzle 1 while clogged") and, separately, records what each inspection station detected and which station it blamed. The dashboard's conclusions can therefore be checked against what really happened.

The project has five objectives:

- **Measure OEE live:** Availability × performance × quality, per station, per shift and for the whole line, computed in the cloud.
- **Explain downtime:** Raw state changes become downtime events with reason codes, ranked by lost time.
- **Trace every reject:** Each board is serialised, so a reject found at SPI, AOI or ICT is blamed on the station that created it, and quality is reported as first-pass yield (FPY).
- **Find the bottleneck:** Starved and blocked time is separated from true faults to show which station limits the line.
- **Prove it works:** Scripted faults must lead an operator on the dashboard to the correct root cause.

## 2. Architecture

The system follows the five-layer IIoT stack, with data flowing from the simulator at the bottom to the dashboard at the top. Each pair of neighbouring layers is joined by a written contract (I1 to I4), which is described in Section 3.

```
 L5  Application   Ignition Perspective (HMI)                   ignition/
        ▲  SQL views only, through Ignition Named Queries        ── I4
 L4  Cloud         MQTT ingest → TimescaleDB, OEE / FPY / Pareto  cloud/
        ▲  MQTT over TLS (port 8883), JSON v1                    ── I3
 L3  Edge          Node-RED: intervals, counts, boards, buffering edge/
        ▲  OPC UA subscription (port 4840)                       ── I2
 L2  Network       OPC UA server wrapping the simulator          opcua_server/
        ▲  in-process Python API                                 ── I1
 L1  Device        smt_sim, the line simulator                   smt_sim/
```

| Step | Layer | Status |
|---|---|---|
| 1 | L1 simulator and OEE metrics | Done |
| — | Contracts, test tools and contract tests | Done (`contracts/`, `tools/oracle.py`, `tests/test_contracts.py`) |
| 2 | L2 OPC UA server on the simulator tags | Next |
| 3 | L3 Node-RED gateway: events, buffering, MQTT over TLS | Not started |
| 4 | L4 cloud database and OEE calculations | Not started |
| 5 | L5 Ignition dashboard as the HMI | Not started |
| 6 | End-to-end validation with the three scripted faults | Not started |

The dashboard is built in Ignition, since that is the platform used in our course. Ignition could do most of the stack by itself (it has its own OPC UA client, MQTT modules and historian), so one rule keeps the architecture honest: **Ignition reads only the L4 database views** and never calculates OEE, yield or Pareto itself. All metrics therefore come from one place, and the dashboard and the cloud cannot disagree.

## 3. How the team works

Steps 2 to 5 are split across the four of us, with one owner per layer. Although each owner is free to choose their own tools and internal design, the inputs and outputs of every layer are fixed by contract, so the layers fit together without constant coordination. The rules are kept in three documents:

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md):** Who builds what, the technology choices, how to work in parallel against stand-ins, and the week-by-week plan.
- **[docs/INTERFACES.md](docs/INTERFACES.md):** The contract itself, covering identifiers, simulation time, state categories, the OPC UA address space, MQTT topics and payloads, the database views and the OEE formulas.
- **[docs/TESTING.md](docs/TESTING.md):** The four levels of testing (unit, contract, pairwise integration and end-to-end) and exactly what each layer must test.

Machine-readable versions of the contracts live in [`contracts/`](contracts): a JSON Schema and an example for every message, the list of required OPC UA nodes, and the required view columns. A contract change goes through a pull request approved by both the producing and the consuming layer, and it updates the document, the schema and the example together.

## 4. The simulated line (L1)

```
Stencil Printer -> [buf] -> SPI -> [buf] -> Pick-and-Place -> [buf] -> Reflow Oven -> [buf] -> AOI -> [buf] -> ICT
```

Rather than using a discrete-event library such as SimPy, the simulator is deliberately tick-based, so that it can stream its state in real time to the IIoT layers. The line runs in fixed 0.5 s ticks, and stations are updated from downstream to upstream so that a slot freed in a buffer can be used upstream in the same tick. Buffers hold five boards, so a stoppage spreads: downstream stations go STARVED and upstream stations go BLOCKED. Although a tick loop is less efficient than jumping from one event to the next, the simulation still runs headless at roughly 30,000 times real time, which is fast enough for tests over many seeds.

The modelling follows four rules throughout:

- **Randomness lives in physical quantities, not in defect decisions:** A defect is never a dice roll. Stencil clogging, stencil wear and paste drying reduce the paste volume on each pad, nozzle wear and clogs widen the placement offset, and a drifting oven zone moves the peak temperature. A defect exists only when that measured quantity is out of specification, and every defect record carries its cause.
- **Normal noise for continuous quantities, hazard rates for discrete events:** Cycle times, paste volumes and offsets use normal noise. Breakdowns use exponential (random) or Weibull (wear-out) time-to-failure, and repair times are lognormal.
- **Determinism:** A single seeded random generator is passed to every component, so the same seed and configuration always give an identical run. This makes runs reproducible, lets a scenario be compared fairly against a baseline, and allows the expected output of every layer to be computed in advance (Section 7).
- **Everything in YAML:** Cycle times, failure rates, feeder parts, changeover times and inspection rates are all kept in `config/default.yaml`, so none of them are hard-coded and any of them can be tuned without touching the code.

| Station | What is modelled |
|---|---|
| Stencil printer | Paste consumption and refills, wipe every 10 prints, aperture clogging, stencil wear, paste drying; one stencil per product |
| SPI (solder paste inspection) | Re-measures paste volume with noise and rejects pads outside 70-130 % |
| Pick-and-place | 12 feeder slots with reels and splicing, round-robin nozzles with a Weibull clog model, placement offsets, mispicks, head speed setting |
| Reflow oven | A 16-board conveyor pipeline, drifting peak-zone temperature, cold joints, voids and tombstones |
| AOI (automated optical inspection) | Imperfect detection per defect type, plus false calls |
| ICT (in-circuit test) | Catches electrical defects that AOI missed, with a fixture changeover per product |

Three products share the line: a 120-part control board, a 104-part sensor board and a 96-part power board. Each station changes over when the first board of a new product reaches it, so a product change ripples down the line rather than stopping it all at once. A second line, configured in `config/factory.yaml`, has a faster placement machine; as a result, its bottleneck moves from the pick-and-place to the ICT station. The simulator can also track customer orders (`config/orders/`), replacing rejected boards until each order has its good boards.

## 5. Getting started

Python 3.11 or newer is required. The dependencies are installed and the test suite is run with:

```bash
pip install -r requirements.txt
python -m pytest
```

The suite contains 136 tests (108 simulator tests grouped by milestone, plus 28 contract tests) and takes approximately two minutes. The main entry points are listed below.

```bash
python scripts/run_headless.py --hours 24                        # one line, full metrics report
python scripts/run_headless.py --factory --hours 24              # both lines
python scripts/run_headless.py --scenario nozzle_fault --seed 1  # an injected fault (add --baseline to compare)
python viz/pygame_view.py --factory                              # live view of both lines
python tools/oracle.py --scenario nozzle_fault --seed 1 --hours 1 --epoch 2026-10-21T06:00:00Z --out out/oracle
```

In the viewer, Up/Down changes the simulation speed from 1x to 3600x, Space pauses, Tab selects a station, and Left/Right changes that station's speed. The last command writes the expected output of every interface for that run to `out/oracle/` (Section 7).

## 6. Simulator results

Over 24 simulated hours with the default three-product plan, line 1 delivers 1,445 good boards at a line OEE of 59 %. Although that figure looks low, most of the loss is planned changeover time rather than breakdowns. With a product change every 100-150 boards, the stencil, feeder, oven profile and ICT fixture changes add up to roughly 35 minutes of lost line time per switch. The active-period method identifies the pick-and-place as the bottleneck, and this is confirmed by blocked time building up upstream of it and starved time downstream.

To check that the metrics point to the right cause, three faults can be injected through the scenario files in `config/scenarios/`. These are the same three faults the finished dashboard must diagnose in step 6. Each scenario is compared with the same run without the fault (seed 1):

| Scenario | Injected fault | What the diagnosis reports | Effect against baseline |
|---|---|---|---|
| Stencil wear | Stencil wears 12x faster from 2 h | Insufficient paste, blamed on the printer | 127 paste defects instead of 55; FPY 82.7 % instead of 88.8 % |
| Feeder depletion | Low-reel calls missed, 10 min operator response | FEEDER_EMPTY is the bottleneck's largest stop | 116 min of empty-feeder stops instead of 7; 506 good boards instead of 588 |
| Nozzle fault | Nozzle 1 clogs at 30 min, lenient vision check | Misplacement, blamed on the pick-and-place | 124 misplacement rejects; FPY 66.2 % instead of 89.7 % |

In all three cases the diagnosis names the injected cause, and the ground-truth records agree. The nozzle scenario also produced a lesson we had not planned for. Nozzle 1's injected clog was caught after about 10 minutes, but nozzle 3 then clogged on its own, and the lenient vision check let it run for about 1.6 hours. Consequently, the real root cause was the lenient check rather than any one nozzle—a fault injected in one place exposed a weakness somewhere else.

## 7. Contracts and the test oracle

Because the simulator is deterministic, the expected output of every layer can be computed rather than written by hand. `tools/oracle.py` runs the simulator once and records everything each interface should carry: the OPC UA event and board stream (I2), every MQTT message with its `msg_id` (I3), and the metrics that the database views must show, together with the ideal cycle times L4 needs as master data (I4). Each layer's tests compare its real output with the oracle's, so a test never depends on hand-typed expected values.

The 28 contract tests in `tests/test_contracts.py` check that the schemas, the examples and the simulator's own states, reason codes and defect types agree, and that everything the oracle produces passes the schemas with no gaps in its sequence numbers. The most useful of them rebuilds OEE, first-pass yield and the downtime Pareto from the MQTT messages alone, and the result matches the simulator's own metrics exactly. This confirms that the MQTT contract carries everything the cloud layer needs, and the code doubles as a worked example for the L4 implementation.

## 8. Limitations and future work

The simulator is a model, not a digital twin of a specific factory, and its parameters are plausible placeholders rather than measured values. While this rules out any claim about absolute OEE or yield figures, the relationships between causes and effects are what this project relies on, and those relationships are tested explicitly.

### 8.1 Control path

Version 1 of the contracts carries data upwards only. Writing back to the line, such as station speed setpoints and kill switches for the simulator, will be added near the end of the project as a new contract version, so that it does not disturb the layers already built.

### 8.2 Security

The MQTT link uses TLS with per-client credentials from the start. The OPC UA link runs without security during development, and signed and encrypted OPC UA with self-signed certificates is a stretch goal.

### 8.3 Further metrics

The architecture can later be extended to energy per board, and to a fuller view of multiple products and changeovers on the dashboard.

## 9. Repository layout

```
config/
  default.yaml            line, products, stations, consumables, failures, inspection
  factory.yaml            two lines: base config plus per-line overrides
  scenarios/              stencil_wear, feeder_depletion, nozzle_fault
  orders/                 demo, tight and large customer order books
smt_sim/                  L1: the simulator core (no pygame)
  sim.py, station.py, stations/, components.py, failures.py, defects.py,
  metrics.py, events.py, orders.py, factory.py, scenarios.py, export.py
viz/pygame_view.py        live view of one line or the factory
scripts/                  headless runner
contracts/                JSON Schemas, examples, OPC UA node list, view columns
tools/oracle.py           expected output of every interface for any seed
tests/                    136 tests: simulator milestones and contract tests
docs/                     ARCHITECTURE.md, INTERFACES.md, TESTING.md, figures/
OEE_Monitoring_SMT_Line.pptx   project proposal presentation
```

Still to be added by the layer owners: `opcua_server/` (L2), `edge/` (L3), `cloud/` (L4), `ignition/` (L5), `docker-compose.yml`, and the shared tools `tools/mqtt_replay.py` and `tools/compare_to_truth.py`.
