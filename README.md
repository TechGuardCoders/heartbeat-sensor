# Heartbeat Sensor

Heartbeat/sampling recorder for fleet metrics. On each tick it scrapes a
node's `/metrics` endpoint, parses a few gauges defensively, appends to a
bounded in-memory ring (plus optional JSONL file), and exposes window
stats. Silence detection: no successful scrape in N ticks → node is
declared not alive.

## Why

Fleet monitors need a tiny, dependency-free pulse check per node — not a
full TSDB. Metric names differ across engines (`vllm:kv_cache_usage_perc`
vs `sglang:token_usage`, `num_requests_waiting` vs `queue_size`), so
Heartbeat Sensor maps raw names to canonical gauges defensively and skips
anything unparsable instead of crashing mid-tick.

## Architecture

- `default_fetcher` — plain `urllib` GET (injectable for tests; no live
  network in CI).
- `parse_gauges` — Prometheus text exposition → canonical gauges
  (`kv_cache_usage`, `queue_depth`, `running`), first candidate match wins.
- `Metronome.tick()` — one heartbeat; failures increment a silence
  counter, successes reset it. `is_alive()` is False after N failed ticks.
- `stats(gauge)` — min/mean/max/p95 + sample count over the ring
  (nearest-rank p95).
- Optional JSONL append — one record per successful tick, crash-safe.

## Usage

```python
from metronome import Metronome

m = Metronome("http://192.168.0.182:8000/metrics", silence_ticks=3)
m.tick()
if not m.is_alive():
    alert("node silent")
print(m.stats("queue_depth").p95)
```

## Results

- 12 tests, all passing, fetcher mocked — zero network in CI.
- Malformed metric lines, connection failures, and recovery are covered.

## CI

GitHub Actions runs `pytest` on push. Badge:

[![CI](https://github.com/TechGuardCoders/heartbeat-sensor/actions/workflows/ci.yml/badge.svg)](https://github.com/TechGuardCoders/heartbeat-sensor/actions/workflows/ci.yml)

## Docker

Multi-stage, multi-arch (`linux/amd64` + `linux/arm64`), runs as non-root.

```bash
docker build -t heartbeat-sensor .
docker run --rm heartbeat-sensor
```

The build stage runs the test suite as a gate; the runtime stage ships only
the module on `python:3.12-slim` under a non-root user.
