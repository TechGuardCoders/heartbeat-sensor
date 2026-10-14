import json

import pytest

from metronome import Metronome, parse_gauges, window_stats

METRICS_OK = """
# HELP vllm:kv_cache_usage_perc KV cache usage
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{model_name="glm"} 0.42
vllm:num_requests_waiting{model_name="glm"} 3
vllm:num_requests_running{model_name="glm"} 2
junk line without value
not_a_number abc
"""

METRICS_ALT = """
sglang:token_usage 0.9
sglang:queue_size 7
"""


def fetcher_of(body):
    return lambda url: body


def fetcher_fail(url):
    raise ConnectionError("down")


def test_parse_gauges_maps_defensively():
    g = parse_gauges(METRICS_OK)
    assert g == {"kv_cache_usage": 0.42, "queue_depth": 3, "running": 2}


def test_parse_gauges_alt_engine_names():
    g = parse_gauges(METRICS_ALT)
    assert g == {"kv_cache_usage": 0.9, "queue_depth": 7}


def test_parse_gauges_skips_malformed():
    assert parse_gauges("# comment\n\nbroken") == {}


def test_tick_success_records_sample():
    m = Metronome("http://x/metrics", fetcher=fetcher_of(METRICS_OK))
    s = m.tick()
    assert s is not None
    assert s.gauges["queue_depth"] == 3
    assert m.is_alive()


def test_tick_failure_counts_silence():
    m = Metronome("http://x/metrics", fetcher=fetcher_fail, silence_ticks=3)
    for _ in range(2):
        m.tick()
    assert m.is_alive() is True
    m.tick()
    assert m.is_alive() is False


def test_recovery_resets_silence():
    m = Metronome("http://x/metrics", fetcher=fetcher_fail, silence_ticks=2)
    m.tick()
    m.tick()
    assert not m.is_alive()
    m.fetcher = fetcher_of(METRICS_OK)
    m.tick()
    assert m.is_alive()


def test_ring_is_bounded():
    m = Metronome("http://x/metrics", fetcher=fetcher_of(METRICS_OK), ring_size=5)
    for _ in range(8):
        m.tick()
    assert len(m.ring) == 5


def test_stats_window():
    m = Metronome("http://x/metrics", fetcher=fetcher_of(METRICS_OK), ring_size=10)
    bodies = ["vllm:num_requests_waiting 1\n", "vllm:num_requests_waiting 5\n",
              "vllm:num_requests_waiting 10\n"]
    for b in bodies:
        m.fetcher = fetcher_of(b)
        m.tick()
    st = m.stats("queue_depth")
    assert (st.count, st.min, st.max) == (3, 1.0, 10.0)
    assert st.mean == pytest.approx(16 / 3)
    assert st.p95 == 10.0


def test_stats_empty_window():
    st = window_stats([])
    assert st.count == 0 and st.min is None


def test_jsonl_output(tmp_path):
    path = tmp_path / "out.jsonl"
    m = Metronome("http://x/metrics", fetcher=fetcher_of(METRICS_OK), jsonl_path=str(path))
    m.tick()
    m.close()
    rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["gauges"]["kv_cache_usage"] == 0.42
    assert rec["node"] == "http://x/metrics"


def test_jsonl_survives_failure_tick(tmp_path):
    path = tmp_path / "out.jsonl"
    m = Metronome("http://x/metrics", fetcher=fetcher_fail, jsonl_path=str(path))
    m.tick()
    m.close()
    assert path.read_text(encoding="utf-8") == ""
