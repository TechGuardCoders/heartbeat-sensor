"""Metronome — heartbeat/sampling recorder for fleet metrics.

Each tick, scrape a node's /metrics endpoint, parse a few gauges
defensively (metric names differ across engines), append to a bounded
ring buffer + optional JSONL file, and expose window stats. Detects
silence: no successful scrape in N ticks -> node is not alive.
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# Metric names differ across engines (vLLM, SGLang, ...). Map defensively:
# canonical name -> candidate raw names, first match wins.
GAUGE_CANDIDATES: Dict[str, List[str]] = {
    "kv_cache_usage": [
        "vllm:kv_cache_usage_perc",
        "vllm:gpu_cache_usage_perc",
        "sglang:token_usage",
    ],
    "queue_depth": [
        "vllm:num_requests_waiting",
        "vllm:waiting_requests",
        "sglang:queue_size",
    ],
    "running": [
        "vllm:num_requests_running",
        "sglang:running_requests",
    ],
}

Fetcher = Callable[[str], str]


def default_fetcher(url: str) -> str:
    """GET a URL and return the body as text. Injectable in tests."""
    with urllib.request.urlopen(url, timeout=5) as resp:  # nosec - operator-supplied URL
        return resp.read().decode("utf-8", errors="replace")


def parse_gauges(text: str) -> Dict[str, float]:
    """Parse Prometheus text exposition, mapping raw names to canonical ones.

    Defensive: skips comments, malformed lines, and anything unparsable.
    """
    raw: Dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.rsplit(None, 1)
        if len(parts) != 2:
            continue
        name, value = parts
        name = name.split("{", 1)[0]  # strip labels
        try:
            raw[name] = float(value)
        except ValueError:
            continue
    gauges: Dict[str, float] = {}
    for canonical, candidates in GAUGE_CANDIDATES.items():
        for cand in candidates:
            if cand in raw:
                gauges[canonical] = raw[cand]
                break
    return gauges


@dataclass
class Sample:
    ts: float
    node: str
    gauges: Dict[str, float]


@dataclass
class WindowStats:
    count: int
    min: Optional[float] = None
    mean: Optional[float] = None
    max: Optional[float] = None
    p95: Optional[float] = None


def window_stats(values: List[float]) -> WindowStats:
    """min/mean/max/p95 over a list of floats (p95 = nearest-rank)."""
    if not values:
        return WindowStats(count=0)
    s = sorted(values)

    def pct(p: float) -> float:
        k = max(0, min(len(s) - 1, int(round(p / 100 * (len(s) - 1)))))
        return s[k]

    return WindowStats(
        count=len(s),
        min=s[0],
        mean=sum(s) / len(s),
        max=s[-1],
        p95=pct(95),
    )


class Metronome:
    """Heartbeat sampler: tick() scrapes a node, records gauges, tracks liveness."""

    def __init__(
        self,
        node_url: str,
        fetcher: Fetcher = default_fetcher,
        ring_size: int = 600,
        jsonl_path: Optional[str] = None,
        silence_ticks: int = 3,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.node_url = node_url
        self.fetcher = fetcher
        self.ring: deque = deque(maxlen=ring_size)
        self.jsonl_path = jsonl_path
        self.silence_ticks = silence_ticks
        self.clock = clock
        self.ticks = 0
        self.ticks_since_success = 0
        self._jsonl_fh = None
        if jsonl_path:
            self._jsonl_fh = open(jsonl_path, "a", encoding="utf-8")

    def tick(self) -> Optional[Sample]:
        """One heartbeat. Returns the Sample on success, None on failure."""
        self.ticks += 1
        try:
            text = self.fetcher(self.node_url)
            gauges = parse_gauges(text)
        except Exception:
            self.ticks_since_success += 1
            return None
        self.ticks_since_success = 0
        sample = Sample(ts=self.clock(), node=self.node_url, gauges=gauges)
        self.ring.append(sample)
        if self._jsonl_fh:
            self._jsonl_fh.write(
                json.dumps({"ts": sample.ts, "node": sample.node, "gauges": gauges}) + "\n"
            )
            self._jsonl_fh.flush()
        return sample

    def is_alive(self) -> bool:
        """False if no successful scrape in the last N ticks."""
        return self.ticks_since_success < self.silence_ticks

    def stats(self, gauge: str) -> WindowStats:
        """Window stats (min/mean/max/p95, count) for one canonical gauge."""
        return window_stats([s.gauges[gauge] for s in self.ring if gauge in s.gauges])

    def close(self) -> None:
        if self._jsonl_fh:
            self._jsonl_fh.close()
            self._jsonl_fh = None
