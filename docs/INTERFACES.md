# Interface standard, v1

This is the contract between the layers. If your code follows this file, it
will work with the other layers. Change it only through a pull request
approved by the producer and the consumer of that interface (see
[ARCHITECTURE.md](ARCHITECTURE.md) §3). Machine-readable versions are in
[`contracts/`](../contracts): JSON Schemas, examples, the OPC UA node list
and the view columns. `tools/oracle.py` produces the exact expected output of
every interface for any seed (see [TESTING.md](TESTING.md)).

---

## 1. Rules for every layer

### 1.1 Identifiers

| Thing | Format | Values |
|---|---|---|
| site | lowercase | `lab` |
| line | lowercase, from `config/factory.yaml` | `line1`, `line2` |
| station | lowercase, from `config/default.yaml` | `printer`, `spi`, `pnp`, `reflow`, `aoi`, `ict` |
| buffer | `buf_<from>_<to>` | `buf_printer_spi`, `buf_spi_pnp`, `buf_pnp_reflow`, `buf_reflow_aoi`, `buf_aoi_ict` |
| product | as in config | `CTRL-BOARD-A`, `SENSOR-BOARD-B`, `POWER-BOARD-C` |
| serial | as in simulator | `SN000123` (unique per line and run) |
| run_id | `<UTC start>-<seed>-<scenario or base>` | `20261021T0600Z-s1-nozzle_fault` |

The simulator writes buffers as `printer->spi`. L2 converts them to the
`buf_` form, because `>` causes trouble in MQTT topics, OPC UA browse paths and Ignition tag paths.

### 1.2 Time: always simulation time

The simulator runs faster than real time (default **60×**), so wall-clock
time is meaningless for OEE. Every timestamp in every layer is derived from
simulation time:

```
ts = epoch + sim_time_s
```

- `epoch` is fixed when a run starts (default: today's date at 06:00 UTC, so
  shift A starts at sim time 0). L2 publishes it as `Run/Epoch`.
- JSON timestamps are ISO 8601 UTC with milliseconds: `"2026-10-21T06:12:30.500Z"`.
  OPC UA uses `DateTime`, and the database uses `timestamptz`.
- Messages also carry the raw `sim_time_s` (float) for debugging.
- **Never** use `Date.now()` / `now()` for data. Wall time may appear only in
  logs and in `edge/status`.

### 1.3 States and reason codes

The only valid values are those in `smt_sim/events.py` (`StationState`,
`ReasonCode`). Adding a code means changing `events.py` first.

Every layer classifies states the same way:

| State | Category | Station availability | Downtime Pareto |
|---|---|---|---|
| `RUNNING` | productive | counts as running | no |
| `STARVED`, `BLOCKED` | external (caused by a neighbour) | removed from the denominator | no |
| `DOWN`, `STARVED_MATERIAL`, `SETUP` | own loss | counts as lost | yes |
| `PM` | planned | removed from planned time | yes |

An empty reason code is sent as `""` in OPC UA and as `null` in JSON. Stop
states always have a reason code. If one is missing, L4 stores it as
`UNKNOWN`.

### 1.4 Units

Units go in the name: `_s` seconds, `_pct` percent (0–100), `_g` grams, `_c`
°C, `_um` µm. Ratios in L4 views (availability etc.) are **0–1**, and the
dashboard formats them as percent.

### 1.5 Versioning

MQTT topics contain `v1`, and every payload has `"v": 1`. **Adding** a field is
non-breaking and allowed at any time: consumers must ignore unknown fields.
Renaming, removing or changing the meaning of a field means v2 and a team
decision.

---

## 2. I1: Simulator → OPC UA server (Python, in-process)

The L2 server imports `smt_sim` and drives it:

```python
factory = Factory.from_config("config/factory.yaml", seed=seed)   # or Factory.single(...)
while True:                                  # paced loop
    factory.step()                           # one tick = 0.5 sim-s
    sleep_until(tick_wall_time / speed)      # speed = 60 → 120 ticks per wall-second
```

`--speed 0` means unpaced (as fast as possible). Tests depend on it, see
[TESTING.md](TESTING.md) §4.

L2 uses:
- `factory.get_tags()`: snapshot for the typed variables, refreshed every
  **1 sim-second** (not every tick).
- **Event listener** (L1 addition): called on every `EventLog.record(event)`.
- **Board listener** (L1 addition): called once when a board reaches the
  line exit or is rejected.
- `sim.ideal_cycle_time_s(station, product)` and `station.current_product`.

Scenarios (`config/scenarios/*.yaml`) are chosen at server start (`--scenario`),
exactly as in `scripts/run_headless.py`.

---

## 3. I2: OPC UA address space (L2 → L3)

- Endpoint: `opc.tcp://<host>:4840/smt/`
- Namespace URI: `urn:smt-line:sim` (look up its index at connect time; don't hard-code `ns=2`)
- NodeIds are **string ids** equal to the browse path joined by dots, e.g.
  `ns=<idx>;s=line1.pnp.State`
- Security: `None` for development. `Basic256Sha256 / SignAndEncrypt` with
  self-signed certs is a stretch goal.

```
Objects/
  Run/
    RunId            String
    Epoch            DateTime
    SimTime          DateTime      ts of the latest tick (1 s resolution)
    SpeedFactor      Double        e.g. 60
    Scenario         String        "" for baseline
  <line>/                          line1, line2
    <station>/                     printer … ict
      State          String        StationState value
      ReasonCode     String        "" when none
      StateSince     DateTime
      Product        String        product the station is set up for
      IdealCycle_s   Double        for that product at 100 % speed
      PartCount      UInt32        boards processed, cumulative
      RejectCount    UInt32        boards this station pulled off the line
      SpeedPct       Double        read-only in v1 (writing comes with the control path)
      Extra/                       station-specific tags from get_tags(), same names,
                                   plus parts_<PRODUCT> (UInt32) per product
                                   (pnp: slot1_part, slot1_qty, …, mispick_count;
                                    printer: paste_g, prints_since_wipe, stencil_cycles;
                                    reflow: peak_zone_offset_c)
    Buffers/<buffer>/
      Level          UInt32
      Capacity       UInt32
    Stream/
      EventSeq       UInt32        +1 per state change on this line
      LastEvent      String (JSON) the state change that just happened, §3.1
      BoardSeq       UInt32        +1 per finished board on this line
      LastBoard      String (JSON) the board that just finished, §3.2
```

The typed variables are for live status and browsing. They are sampled, so a
short state can be missed. The **Stream** nodes are for lossless delivery.
L2 updates `LastEvent` once for every event, in order. L3 must subscribe to
`LastEvent` and `LastBoard` with `samplingInterval = 0` and
`queueSize >= 1000`, and check that the matching `Seq` increases by exactly 1.
A gap is logged as an error and published to `edge/status`.

Timing rules, so every implementation gives identical output:
- An event's `product` is the station's product **at the end of the tick** in
  which the event happened (L2 collects a tick's events, then publishes them).
- A board's `ts` is the sim time of the tick in which it reached the line exit
  or a reject bin.
- `seq` starts at 1 per line and per stream at the start of a run.

### 3.1 `LastEvent` JSON

```json
{"seq": 4812, "line": "line1", "station": "pnp", "ts": "2026-10-21T08:41:07.500Z",
 "sim_time_s": 9667.5, "old_state": "RUNNING", "new_state": "DOWN", "reason_code": "NOZZLE_CLOG",
 "product": "CTRL-BOARD-A"}
```

### 3.2 `LastBoard` JSON

Observed data only. **No `true_defects`** (see ARCHITECTURE §8).

```json
{"seq": 233, "line": "line1", "serial": "SN000233", "product": "CTRL-BOARD-A",
 "ts": "2026-10-21T08:52:10.000Z", "sim_time_s": 10330.0,
 "disposition": "rework", "rejected_at": "aoi",
 "detected_defects": [{"type": "MISPLACEMENT", "detected_at": "aoi", "attributed_origin": "pnp"}],
 "false_calls": [], "order_id": null}
```

`disposition` is `good`, `rework` or `scrap`. `rejected_at` is `null` for good
boards.

---

## 4. I3: MQTT (L3 → L4)

### 4.1 Broker and connection

| Setting | Value |
|---|---|
| Broker | Mosquitto, `mqtts://<broker-host>:8883`, TLS 1.2+ |
| Server cert | Signed by our own CA; `certs/ca.crt` is committed, **private keys never are** |
| Auth | Username/password per client (`edge-line1`, `ingest`, `replay`), listed in Mosquitto ACLs |
| ACL | `edge-*` and `replay` may publish `smt/v1/#`; `ingest` may subscribe `smt/v1/#` |
| Client ids | Fixed (`edge-lab`, `ingest-1`); clean session **false** so QoS 1 messages queue while a client is offline |
| Last will | `smt/v1/lab/edge/status` → `{"v":1,"online":false}`, retained |

### 4.2 Topics

```
smt/v1/<site>/<line>/<station>/state       retained  QoS 1   current state (live status)
smt/v1/<site>/<line>/<station>/interval              QoS 1   one closed state interval
smt/v1/<site>/<line>/<station>/counts                QoS 1   counters, every 60 sim-s
smt/v1/<site>/<line>/board                           QoS 1   one finished board
smt/v1/<site>/<line>/buffers                         QoS 0   buffer levels, every 10 sim-s
smt/v1/<site>/run                          retained  QoS 1   run metadata, at start
smt/v1/<site>/edge/status                  retained  QoS 1   edge health, every 30 wall-s + LWT
```

### 4.3 Envelope (every payload)

| Field | Type | Meaning |
|---|---|---|
| `v` | int | always `1` |
| `msg_id` | string | **unique and deterministic**: the same data gives the same id after a replay. Pattern: `<run_id>/<line>/<kind>/<seq or serial>` |
| `run_id` | string | §1.1 |
| `ts` | string | §1.2: the moment the message describes |
| `sim_time_s` | number | same moment in sim seconds |

L4 inserts with `ON CONFLICT (msg_id) DO NOTHING`, so the edge may resend
freely. **At-least-once delivery with idempotent storage = exactly-once data.**

### 4.4 Payloads

**`state`**: published on every state change, retained.
```json
{"v":1,"msg_id":"20261021T0600Z-s1-nozzle_fault/line1/state/4812","run_id":"20261021T0600Z-s1-nozzle_fault",
 "ts":"2026-10-21T08:41:07.500Z","sim_time_s":9667.5,
 "line":"line1","station":"pnp","state":"DOWN","reason_code":"NOZZLE_CLOG","category":"own_loss","product":"CTRL-BOARD-A"}
```

**`interval`**: the main OEE input. When event *n+1* arrives for a station,
the edge closes the interval that event *n* opened and publishes it. Every
state is published, including `RUNNING`, `STARVED` and `BLOCKED`.
```json
{"v":1,"msg_id":"…/line1/interval/4790","run_id":"…","ts":"2026-10-21T08:41:07.500Z","sim_time_s":9667.5,
 "line":"line1","station":"pnp","state":"RUNNING","reason_code":null,"category":"productive",
 "start":"2026-10-21T08:30:02.000Z","end":"2026-10-21T08:41:07.500Z","duration_s":665.5,"product":"CTRL-BOARD-A"}
```
`ts` = `end`. `msg_id` uses the seq of the event that **opened** the interval.
The interval still open at any moment is not published. L4 counts it from the
latest `state` message up to the latest `sim_time_s` it has seen.
`category` is from §1.3: `productive` | `external` | `own_loss` | `planned`.

**`counts`**: cumulative counters, so a lost message does no harm (L4 takes
differences). Published after the tick that reaches each multiple of 60 sim-s.
`msg_id = <run_id>/<line>/counts/<station>/<sim seconds as integer>`.
`extra` holds every station-specific tag from `get_tags()`.
```json
{"v":1,"msg_id":"…/line1/counts/pnp/9660","run_id":"…","ts":"2026-10-21T08:41:00.000Z","sim_time_s":9660.0,
 "line":"line1","station":"pnp","part_count":402,"reject_count":0,
 "part_count_by_product":{"CTRL-BOARD-A":402},"speed_pct":100.0,"extra":{"mispick_count":17}}
```
`part_count_by_product` needs one more L1 tag (`parts_by_product`). L2
exposes it in `Extra/` as `parts_<PRODUCT>` (UInt32).

**`board`**: the `LastBoard` JSON from §3.2 inside the envelope.
`msg_id = <run_id>/<line>/board/<serial>`.

**`buffers`**:
```json
{"v":1,"msg_id":"…/line1/buffers/9660","run_id":"…","ts":"…","sim_time_s":9660.0,"line":"line1",
 "levels":{"buf_printer_spi":3,"buf_spi_pnp":5,"buf_pnp_reflow":0,"buf_reflow_aoi":1,"buf_aoi_ict":0},"capacity":5}
```

**`run`**:
```json
{"v":1,"msg_id":"…/run","run_id":"…","ts":"2026-10-21T06:00:00.000Z","sim_time_s":0,
 "epoch":"2026-10-21T06:00:00.000Z","speed_factor":60,"seed":1,"scenario":"nozzle_fault","lines":["line1","line2"]}
```

**`edge/status`**:
```json
{"v":1,"online":true,"wall_ts":"2026-10-21T14:03:11Z","queued":0,"event_seq_gaps":0,"opcua_connected":true}
```

### 4.5 Edge buffering

If the broker is unreachable, the edge queues messages **on disk**
(file-backed, survives a Node-RED restart, at least 24 h of data) and sends
them **in order** after reconnecting. `queued` in `edge/status` shows the
backlog.

---

## 5. I4: Database views (L4 → L5)

L5 (Ignition) connects as the read-only user `dashboard_ro` through a JDBC
database connection, and its Named Queries may read **only the views below**,
never the raw tables. The column lists are also in `contracts/db_views.json`.
L4 may change its tables freely as long as these views keep their columns. Every view has `run_id` and `line` so the
dashboard can filter by them.

**Master data (L4 owns, seeded from config):**
- `shifts(name, start_offset_h, length_h)`: A 06–14, B 14–22, C 22–06
  relative to the epoch.
- `ideal_cycle(line, station, product, ideal_cycle_s)`: taken from the
  simulator, e.g. `run_oracle(...).i4_expected["master_data"]["ideal_cycle"]`.
- `targets(run_id, line, product, target_qty, due_ts)`: from the production
  plan or orders.

| View | Columns | Used for |
|---|---|---|
| `v_station_status` | run_id, line, station, state, reason_code, category, since_ts, product | Live line diagram |
| `v_buffer_levels` | run_id, line, ts, buffer, level, capacity | Live line diagram |
| `v_station_oee` | run_id, line, station, shift, shift_start, planned_s, running_s, starved_s, blocked_s, own_loss_s, boards, ideal_work_s, availability, performance, quality, oee | OEE per station and shift |
| `v_line_oee` | run_id, line, shift, shift_start, planned_s, good_out, ideal_output_s, oee | Line OEE trend |
| `v_downtime_pareto` | run_id, line, station, reason_code, state, total_s, occurrences, shift | Ranked downtime causes |
| `v_state_timeline` | run_id, line, station, start_ts, end_ts, state, reason_code | Gantt-style state timeline |
| `v_quality` | run_id, line, shift, finished, good, rework, scrap, fpy | First-pass yield |
| `v_reject_blame` | run_id, line, shift, blamed_station, defect_type, count | Reject Pareto by root station |
| `v_output_vs_target` | run_id, line, product, target_qty, good_out, due_ts, on_track | Target vs actual |
| `v_edge_health` | site, online, last_seen_wall, queued, event_seq_gaps | Pipeline health |

### Formulas (must match `smt_sim/metrics.py`)

```
planned_s     = observed_s − PM_s
availability  = running_s / (planned_s − starved_s − blocked_s)
performance   = ideal_work_s / running_s          (reflow: 1.0, it is a fixed-speed conveyor)
ideal_work_s  = Σ_product boards(product) × ideal_cycle_s(station, product)
quality       = 1 − boards blamed on this station / boards processed by it
                (blame = detected_defects[].attributed_origin, not ground truth)
oee           = availability × performance × quality

line oee      = Σ_product good_out(product) × max_station ideal_cycle_s(product) / planned_s
fpy           = good / (good + rework + scrap)    (boards that have left the line)
```

An interval that crosses a shift boundary is split at the boundary. Ratios
with a zero denominator are `NULL`, never 0.

---

## 6. Acceptance tests (step 6)

`tools/compare_to_truth.py --run-id <id>` runs `tools/oracle.py` with the same
seed, scenario and epoch, and compares `i4_expected` with the L4 views:

| Metric | Tolerance |
|---|---|
| Station availability, performance, quality, OEE | ±0.5 percentage points |
| Line OEE, FPY | ±0.5 percentage points |
| Board counts (good, rework, scrap) | exact |
| Downtime Pareto top 3 (station, reason) | same order |
| Reject blame top cause | same station and defect type |
| Intervals and boards with gaps or duplicates | 0 |

Tolerance exists only because the last open interval is cut at a slightly
different moment. Any other difference is a bug.

The three scenarios and what the dashboard must show:

| Scenario | Dashboard must point to |
|---|---|
| `stencil_wear` | `printer`, rejects `INSUFFICIENT_PASTE` rising, FPY down |
| `feeder_depletion` | `pnp` / `FEEDER_EMPTY` at the top of the downtime Pareto |
| `nozzle_fault` | `pnp`, rejects `MISPLACEMENT`, FPY down |

---

## 7. Not in v1

- **Control path.** Writing back to the line (speed setpoints, kill switches
  for the simulator) is planned for the end of the project. It will come as a
  new contract version, so v1 consumers keep working.
- **OPC UA security.** `None` during development, as in §3.
