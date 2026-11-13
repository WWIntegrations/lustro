"""Request queue: forefront adds and add_many in one journal write."""
from ww_scraper.storage import MemoryRequestQueue


def test_queue_forefront_and_add_many(storage):
    for q in (storage.queue("q1"), MemoryRequestQueue()):
        q.add("https://a.example/1")
        q.add("https://a.example/2")
        assert q.add("https://a.example/0", forefront=True) is True
        assert q.add_many(["https://a.example/3", {"url": "https://a.example/4", "depth": 2},
                           "https://a.example/3", "https://a.example/1"]) == [True, True, False, False]
        assert q.add_many(["https://a.example/x", "https://a.example/y"], forefront=True) == [True, True]
        order = []
        while (r := q.fetch_next()) is not None:
            order.append(r["url"].rsplit("/", 1)[1])
            q.mark_handled(r)
        assert order == ["x", "y", "0", "1", "2", "3", "4"]
    # the journal replays forefront adds in the same order
    q = storage.queue("q2")
    q.add("https://a.example/1")
    q.add_many(["https://a.example/x", "https://a.example/y"], forefront=True)
    q2 = storage.queue("q2")
    assert [q2.fetch_next()["url"][-1] for _ in range(3)] == ["x", "y", "1"]
