# Testing standard

How we test each layer, and how we prove the layers fit together. The
contracts being tested are in [INTERFACES.md](INTERFACES.md).

## 1. The idea: the simulator knows the answer

The simulator is deterministic: the same seed, scenario and duration always
give the same run. So for any test, the expected output can be **computed**
instead of written by hand. `tools/oracle.py` does that:

```python
from tools.oracle import run_oracle
r = run_oracle(seed=1, scenario="nozzle_fault", hours=1, epoch=EPOCH)
r.i2_events      # every LastEvent the OPC UA server must emit, per line, in order
r.i2_boards      # every LastBoard
r.i3_messages    # every (topic, payload) the edge must publish, msg_id included
r.i4_expected    # the metrics L4's views must show, plus master data (ideal cycles)
```

```bash
python tools/oracle.py --scenario nozzle_fault --seed 1 --hours 1 --epoch 2026-10-21T06:00:00Z --out out/oracle
python tools/oracle.py --factory --seed 3 --hours 0.5 --out out/oracle_factory
```

Always pass a fixed `epoch` in tests. The `run_id` and every timestamp depend
on it.

## 2. Four levels

| Level | Written by | Checks | Runs |
|---|---|---|---|
| **1. Unit** | Each owner, any tools they like | The inside of their own layer | Every commit |
| **2. Contract** | Shared, mandatory | What the layer emits or accepts matches the schemas | Every pull request (CI) |
| **3. Pairwise integration** | Producer and consumer together | Two neighbouring layers work together | Before merging an integration, plus nightly |
| **4. End-to-end** | Everyone (step 6) | The dashboard shows the simulator's ground truth | At milestones |

Level 1 is the owner's business. Levels 2–4 are the team standard below.

## 3. Shared test assets (`contracts/`)

| File | Use |
|---|---|
| `schemas/*.schema.json` | JSON Schema (draft 2020-12) for every JSON message in I2 and I3 |
| `examples/*.json` | One valid example per schema, for consumer tests |
| `opcua_nodes.csv` | Every required OPC UA node, its type and access |
| `db_views.json` | Every I4 view and its required columns |

Rules:
- A producer test checks **"everything I emit validates against the schema."**
- A consumer test checks **"I can ingest every example file."**
- Consumers must ignore unknown fields. Schemas allow extra fields on purpose.
- A contract change updates the doc, the schema, the example and both sides'
  tests **in the same pull request**. `tests/test_contracts.py` fails if the
  schemas, the examples or the simulator's enums drift apart.

Python layers validate with `jsonschema`. Node-RED can use `ajv` in a function
node or the `node-red-contrib-json-schema-validator` node.

## 4. What each layer must test

### L1 Simulator (`tests/`, pytest)
- The existing milestone tests.
- New hooks (event listener, board listener, run id): each hook fires exactly
  once per event or board, in order, and matches `run_oracle` for the same seed.

### L2 OPC UA server (`opcua_server/tests/`, pytest + asyncua client)
- Needs an **unpaced mode** (`--speed 0`) so a simulated hour takes seconds.
- Start the server on a free port in the test and connect a client.
- **Address space:** every path in `contracts/opcua_nodes.csv` exists with the
  right data type.
- **Stream:** subscribe to `LastEvent`/`LastBoard` with `samplingInterval=0`,
  `queueSize>=1000`, run seed 1 for 1 h, and collect everything. The result
  must **equal `r.i2_events` / `r.i2_boards` exactly**, with no `EventSeq` gaps.
- **Typed tags:** `State`, `PartCount` etc. equal `get_tags()` at the same sim time.

### L3 Edge (black box: Docker + pytest)
Node-RED internals are hard to unit test, so test what goes in and out:
1. Start L2 (`--speed 0`, seed 1, 1 h), Node-RED and a test Mosquitto
   (`docker compose -f compose.test.yml up`).
2. A pytest subscriber collects every `smt/v1/#` message.
3. Every message validates against its schema.
4. The set of `msg_id`s **equals** the oracle's, and each payload equals the
   oracle's payload for that `msg_id` (ignore field order).

Resilience tests, where edge layers actually break:

| Test | Pass when |
|---|---|
| Stop the broker for 5 min mid-run, then restart | Same message set as the oracle: none missing, none extra |
| Restart Node-RED mid-run | Same; queued messages survive on disk |
| Kill the OPC UA server | `edge/status` shows `opcua_connected: false` within 30 s |
| Pull the network | `edge/status.queued` rises, then drains to 0 after reconnect |

A duplicate `msg_id` after a restart is **allowed** (L4 de-duplicates). A
missing one is a failure.

### L4 Cloud (`cloud/tests/`, pytest + a throwaway Postgres in Docker)
- **Ingest:** publish `r.i3_messages` to a test broker, or call the ingest
  function directly, then check row counts.
- **Repeats and order:** send every message twice, shuffled. The view results
  must not change.
- **Accuracy:** the views must match `r.i4_expected` for the `nozzle_fault`
  and factory runs. Tolerance is ±0.5 percentage points on ratios (exact
  matching is possible; see `reference_l4` in `tests/test_contracts.py`).
  Exact match is required on board counts, plus the same top-3 Pareto order.
- **Known-answer tests** with hand-made intervals:
  - an interval crossing a shift boundary is split correctly
  - all-PM time gives `NULL`, not 0 or an error
  - reflow performance is 1.0
  - a stop with no reason code appears as `UNKNOWN`
  - the open interval (from the latest `state`) is counted up to "now"
- **View columns:** `information_schema.columns` contains every column in
  `contracts/db_views.json`, so L5 is never broken without warning.
- **Ignition's queries:** the SQL of every Ignition Named Query (exported from
  the Ignition project into `ignition/`) runs against the test database
  without errors and returns the expected columns. This is how most of
  L5's risk gets tested automatically.

### L5 Ignition (manual checklist + optional smoke test)
Ignition can't run in CI (licence), so:
- **Rule:** screens bind only to Named Queries on I4 views. No OEE maths in
  tag expressions or scripts. Reviewers check this.
- **Checklist (`ignition/TEST_CHECKLIST.md`):** for each scenario, the exact
  numbers each screen must show, taken from `r.i4_expected` (e.g. "nozzle_fault
  seed 1, 1 h: line OEE 0.xx, top reject = pnp / MISPLACEMENT").
- **Optional:** a Playwright script that opens the Perspective session URL
  and checks every view loads without an error banner.

## 5. End-to-end (step 6)

Full stack: L2 (paced at 60×) → Node-RED → Mosquitto (TLS) → ingest →
Postgres → Ignition.

1. **Data check:** `tools/compare_to_truth.py --run-id <id>` reads the I4 views
   and compares them with `run_oracle` for the same seed, scenario and epoch.
   Same tolerances as the L4 accuracy test.
2. **Ground-truth check:** for each scenario, the cause the dashboard points
   to matches the injected fault (INTERFACES.md §6).
3. **Blind operator test:** someone who doesn't know which scenario is running
   names the root cause using only the Ignition screens. This is the proof
   for objective 5.

## 6. Where tests run

| Where | What |
|---|---|
| GitHub Actions, every pull request | L1 unit, `tests/test_contracts.py`, L2 in-process, L4 with a Postgres service container |
| `docker compose`, nightly or by hand | L3 black box + resilience, full end-to-end |
| By hand | Ignition checklist, blind operator test |

`python -m pytest` at the repo root runs the simulator tests and the contract
tests. Each layer adds its own folder to `testpaths` in `pyproject.toml` when
it has tests.
