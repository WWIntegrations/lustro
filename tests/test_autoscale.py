"""Load-driven concurrency: LoadScaler moves between 1 and maxConcurrency; off by default."""
from ww_scraper.actor import run_actor
from ww_scraper.crawler import LoadScaler, machine_load


def _scaler(maximum, readings):
    it = iter(readings)
    return LoadScaler(maximum, interval=0, probe=lambda: next(it))


def test_scales_up_when_idle_and_down_under_load():
    s = _scaler(4, [(0.1, None)] * 3 + [(1.5, None)] * 4 + [(0.7, 0.5)])
    assert s.value == 2
    assert [s.current() for _ in range(3)] == [3, 4, 4]                # never past maxConcurrency
    assert [s.current() for _ in range(4)] == [3, 2, 1, 1]             # never below one
    assert s.current() == 1                                            # between the thresholds: hold


def test_low_memory_scales_down_even_when_load_is_low():
    s = _scaler(8, [(0.1, 0.05), (0.1, 0.05), (0.1, 0.5)])
    assert [s.current() for _ in range(3)] == [1, 1, 2]


def test_one_worker_stays_one():
    assert LoadScaler(1, probe=lambda: (0.0, None)).current() == 1


def test_readings_are_numbers():
    load, free = machine_load()
    assert load >= 0 and (free is None or 0 <= free <= 1)


def test_crawl_with_autoscale(site, storage):
    inp = {"startUrls": [site.base + "/"], "maxPages": 6, "minIntervalSecs": 0, "maxConcurrency": 4}
    plain = run_actor("website-content-crawler", inp, storage, allow_loopback=True)
    assert "concurrency" not in plain["stats"]
    rec = run_actor("website-content-crawler", dict(inp, autoscaleConcurrency=True), storage, allow_loopback=True)
    assert rec["status"] == "SUCCEEDED" and 1 <= rec["stats"]["concurrency"] <= 4
    assert rec["itemCount"] == plain["itemCount"]
