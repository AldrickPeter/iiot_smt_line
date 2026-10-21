# Contracts

Machine-readable versions of [docs/INTERFACES.md](../docs/INTERFACES.md). How
to use them in tests: [docs/TESTING.md](../docs/TESTING.md).

- `schemas/<name>.schema.json`: JSON Schema for each message below
- `examples/<name>.json`: one valid example of each
- `opcua_nodes.csv`: required OPC UA nodes (I2)
- `db_views.json`: required views and columns (I4)

For the expected output of a whole run (not just one example), use
`tools/oracle.py`.

| File | Interface | Topic / node |
|---|---|---|
| `opcua_last_event.json` | I2 | `<line>.Stream.LastEvent` |
| `opcua_last_board.json` | I2 | `<line>.Stream.LastBoard` |
| `mqtt_state.json` | I3 | `smt/v1/<site>/<line>/<station>/state` |
| `mqtt_interval.json` | I3 | `smt/v1/<site>/<line>/<station>/interval` |
| `mqtt_counts.json` | I3 | `smt/v1/<site>/<line>/<station>/counts` |
| `mqtt_board.json` | I3 | `smt/v1/<site>/<line>/board` |
| `mqtt_buffers.json` | I3 | `smt/v1/<site>/<line>/buffers` |
| `mqtt_run.json` | I3 | `smt/v1/<site>/run` |
| `mqtt_edge_status.json` | I3 | `smt/v1/<site>/edge/status` |

A contract change updates the doc, the schema and the example in the same pull
request. `tests/test_contracts.py` checks that they agree with each other and
with the simulator.
