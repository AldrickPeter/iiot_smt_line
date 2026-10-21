# Architecture and team plan

This document says **who builds what** and **how the pieces fit**. The exact
wire formats each layer must follow are in [INTERFACES.md](INTERFACES.md); that
file is the contract between team members.

## 1. The stack

```
 L5  Application   Ignition Perspective (HMI)                  ignition/
        ▲  reads SQL views only (read-only DB user)            ── contract I4
 L4  Cloud         MQTT ingest service → TimescaleDB,          cloud/
                   OEE / FPY / Pareto as SQL views
        ▲  MQTT over TLS (port 8883), JSON v1 payloads         ── contract I3
 L3  Edge          Node-RED: state changes → intervals,        edge/
                   counts, board records; store-and-forward
        ▲  OPC UA subscription (opc.tcp://…:4840)              ── contract I2
 L2  Network       Python OPC UA server (asyncua) wrapping     opcua_server/
                   the simulator, paced to real time × N
        ▲  in-process Python API                               ── contract I1
 L1  Device        smt_sim (done)                              smt_sim/
```

Data flows up only in v1. Writing back to the line (speed setpoints, kill
switches for the simulator) is planned for the end of the project as a new
contract version, so it does not disturb the layers already built.

### Technology choices

| Layer | Choice | Why |
|---|---|---|
| L2 | Python + `asyncua` | Same language as the simulator; runs it in-process, so there is no second hop. |
| L3 | Node-RED + `node-red-contrib-opcua` + built-in MQTT node | Named in the proposal; low-code flows are easy to demo. |
| Broker | Eclipse Mosquitto with TLS | Free, standard, runs in Docker locally or on a cloud VM. |
| L4 | TimescaleDB (PostgreSQL) + a small Python ingest service (`paho-mqtt`, `psycopg`) | OEE needs joins (intervals × shifts × ideal cycles), which SQL handles well. Unique keys make replays idempotent. |
| L5 | Ignition Perspective (required by the course) | Connects to the database over JDBC and binds screens to Named Queries on the I4 views. |

Everything except Ignition runs with one `docker compose up` (Mosquitto,
Node-RED, TimescaleDB, ingest) plus `python -m opcua_server`. Ignition runs as
the course's gateway installation, or the official `inductiveautomation/ignition`
image. "Cloud" means the same compose file on a cloud VM; nothing in the code
changes, only hostnames and certs.

These are recommendations. Section 7 explains what each owner may change.

## 2. Repository layout (monorepo)

```
smt_sim/          L1  simulator                                    (exists)
opcua_server/     L2  OPC UA server around smt_sim.Factory
edge/             L3  Node-RED flows.json, settings.js, its Dockerfile
cloud/            L4  sql/ (schema, views), ingest/ (MQTT → DB), seed/ (master data)
ignition/         L5  exported Ignition project (Named Queries, Perspective views) + TEST_CHECKLIST.md
contracts/        shared: JSON Schemas, examples, OPC UA nodes, view columns  (exists)
tools/            shared: oracle.py (exists), mqtt_replay.py, compare_to_truth.py (to write)
docker-compose.yml
docs/             ARCHITECTURE.md, INTERFACES.md, TESTING.md        (exists)
```

## 3. Ownership

One person owns each layer. The **producer owns the contract above it**: the
L2 owner owns I2, the L3 owner owns I3, and so on. A contract change needs a
pull request approved by both the producer and the consumer.

| Step | Layer | Owner | Builds | Owns contract | Done when |
|---|---|---|---|---|---|
| 2 | L2 Network | _name_ | OPC UA server, pacing, small L1 additions (§4) | I2 | UaExpert browses the address space; `EventSeq`/`BoardSeq` have no gaps over a 1 h run at 60×. |
| 3 | L3 Edge | _name_ | Node-RED flows: intervals, counts, boards, buffering, TLS | I3 | Every message validates against `contracts/schemas` and the message set equals the oracle's; pulling the network for 5 min loses nothing. |
| 4 | L4 Cloud | _name_ | Broker + TLS certs, DB schema, ingest, OEE views, shift table | I4 | Fed by `tools/mqtt_replay.py`, the views match the oracle's `i4_expected` for the same seed (tolerances in INTERFACES §6). |
| 5 | L5 App | _name_ | Ignition Perspective screens: line status, OEE trend, downtime Pareto, reject blame, output vs target | (none) | An operator with no other help finds the root cause of each of the 3 scenarios. |
| 6 | All | everyone | End-to-end validation | | The three scenarios pass `tools/compare_to_truth.py` through the full stack. |

Suggested side duties, so no one is idle while waiting on others:
- **L2 owner:** writes `tools/mqtt_replay.py` early. It publishes
  `run_oracle(...).i3_messages` to a broker (paced or unpaced), so L4 has real
  input from day 1.
- **L3 owner:** owns `docker-compose.yml` and the TLS certificate script.
- **L4 owner:** writes `tools/compare_to_truth.py` (cloud views vs `run_oracle(...).i4_expected`).
- **L5 owner:** writes the scenario demo scripts and the operator walkthrough for step 6.

## 4. Changes needed in L1

The current `get_tags()` is almost enough. The L2 owner adds these to `smt_sim`
(small, tested, reviewed by the L1 author):

1. A `product` tag per station (`current_product`), an `ideal_cycle_s` tag
   (`sim.ideal_cycle_time_s(station, product)`) and the per-product part
   counts (`parts_by_product`).
2. An event listener hook on `EventLog.record`, so the OPC UA server receives
   every state change instead of polling and possibly missing one.
3. A board-finished listener on the sink and the reject bins, so every finished
   board (good or rejected) is pushed out once.
4. A `run_id` and `epoch` on `Simulator`/`Factory` (see INTERFACES §1;
   `tools/oracle.py` already has `make_run_id`).

The hooks must follow the timing rules in INTERFACES §3, and their output must
equal `run_oracle(...).i2_events` / `i2_boards` for the same seed.

## 5. Working in parallel: stubs first

Nobody waits for the layer below. In week 1 each producer publishes something
fake but contract-correct:

| Consumer | Develops against |
|---|---|
| L3 | L2's server, which can run on day 1: the simulator already exists. |
| L4 | `tools/mqtt_replay.py`: the oracle's I3 messages published straight to MQTT, skipping L2 and L3. |
| L5 | A database filled by replaying a recorded 24 h run (`cloud/seed/`). |
| Everyone | `contracts/examples/*.json` and `tools/oracle.py`: the expected output of every interface for any seed (see [TESTING.md](TESTING.md)). |

## 6. Plan

| Week | Milestone |
|---|---|
| 1 | Review and **freeze contracts v1**. L1 additions merged. Compose file with an empty broker and database. Replay tool publishing. |
| 2–3 | Each layer built against its stub. Weekly 30-min sync: only contract questions. |
| 4 | Pairwise integration: L2↔L3, then L3↔L4 (replace replay with the real edge), then L4↔L5. |
| 5 | Step 6: the three scenarios end to end, compare to ground truth, record the demo. |

## 7. Freedom inside a layer, rules at the edges

Each owner chooses their own tools, languages and internal design. Only the
interfaces (I1–I4), the shared rules in INTERFACES.md §1 and the tests in
[TESTING.md](TESTING.md) are binding. The tool choices in §1 are
recommendations, except where the interface fixes them: OPC UA for I2, MQTT
over TLS with JSON for I3, and a SQL database for I4 (Ignition needs JDBC).

Ignition can do almost every layer by itself (OPC UA, MQTT, historian), so
one rule protects the architecture: **Ignition reads only the I4 views.** It
does not connect to the OPC UA server or the broker, and it does not compute
OEE, yield or Pareto in tag expressions or scripts. All metrics come from L4,
so the dashboard and the cloud can never disagree.

## 8. Ground truth stays out of band

Real machines don't know true defects or hidden nozzle clogs, so neither does
the IIoT pipeline. The pipeline carries only what a real line would expose
(states, reason codes, counts, inspection results). Ground truth goes from
the simulator straight to the test tools (`tools/oracle.py`, `out/boards.csv`)
and is used **only** by tests and by `tools/compare_to_truth.py` in step 6. That separation is what lets us prove
the dashboard finds the right cause.
