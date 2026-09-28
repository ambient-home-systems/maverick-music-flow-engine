"""The shared LRU-with-expiry cache used for the library, search and artwork caches."""

from __future__ import annotations

import runpy
from pathlib import Path
from unittest import TestCase, main

MODULE = runpy.run_path(
    str(
        Path(__file__).resolve().parents[1]
        / "custom_components/maverick_music_flow/bounded_cache.py"
    )
)
BoundedCache = MODULE["BoundedCache"]
estimate_json_size = MODULE["estimate_json_size"]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class EvictionTests(TestCase):
    def test_inserting_past_the_limit_evicts_the_least_recently_used_entry(self):
        cache = BoundedCache(max_entries=3)
        for key in "abc":
            cache[key] = key.upper()
        cache["d"] = "D"
        self.assertEqual(cache.keys(), ["b", "c", "d"])
        self.assertEqual(cache.evicted_capacity, 1)

    def test_reading_an_entry_protects_it_from_eviction(self):
        cache = BoundedCache(max_entries=3)
        for key in "abc":
            cache[key] = key.upper()
        self.assertEqual(cache.get("a"), "A")
        cache["d"] = "D"
        self.assertEqual(cache.keys(), ["c", "a", "d"])

    def test_touch_and_reassignment_protect_an_entry_but_indexing_does_not(self):
        cache = BoundedCache(max_entries=3)
        for key in "abc":
            cache[key] = 1
        self.assertEqual(cache["a"], 1)  # plain read, no LRU update
        self.assertEqual(cache.keys(), ["a", "b", "c"])
        cache.touch("a")
        cache["b"] = 2  # reassigning an existing key moves it to the end
        self.assertEqual(cache.keys(), ["c", "a", "b"])
        cache["d"] = 4
        self.assertEqual(cache.keys(), ["a", "b", "d"])

    def test_byte_limit_is_respected_and_evicts_least_recently_used(self):
        cache = BoundedCache(max_entries=100, max_bytes=100, sizeof=len)
        cache["a"] = "x" * 40
        cache["b"] = "x" * 40
        cache.get("a")
        cache["c"] = "x" * 40
        self.assertEqual(cache.keys(), ["a", "c"])
        self.assertEqual(cache.total_bytes, 80)
        cache["d"] = "x" * 90
        self.assertEqual(cache.keys(), ["d"])
        self.assertLessEqual(cache.total_bytes, 100)

    def test_value_larger_than_the_byte_limit_is_not_stored_and_replaces_nothing(self):
        cache = BoundedCache(max_entries=10, max_bytes=100, sizeof=len)
        cache["a"] = "x" * 10
        cache["b"] = "x" * 101
        self.assertEqual(cache.keys(), ["a"])  # nothing was evicted to make room
        # Replacing a key with a value that is too big drops the old value rather than
        # keeping stale data under that key.
        cache["a"] = "x" * 200
        self.assertNotIn("a", cache)
        self.assertEqual(cache.total_bytes, 0)
        self.assertEqual(cache.rejected_oversize, 2)

    def test_replacing_a_key_updates_the_byte_total(self):
        cache = BoundedCache(max_entries=10, max_bytes=100, sizeof=len)
        cache["a"] = "x" * 60
        cache["a"] = "x" * 10
        self.assertEqual(cache.total_bytes, 10)
        cache.pop("a")
        self.assertEqual(cache.total_bytes, 0)
        cache["a"] = "x" * 5
        del cache["a"]
        cache["b"] = "x" * 5
        cache.clear()
        self.assertEqual((len(cache), cache.total_bytes), (0, 0))

    def test_the_entry_just_stored_is_never_the_one_evicted(self):
        cache = BoundedCache(max_entries=1, max_bytes=50, sizeof=len)
        cache["a"] = "x" * 50
        cache["b"] = "x" * 50
        self.assertEqual(cache.keys(), ["b"])


class ExpiryTests(TestCase):
    def make(self, **kwargs):
        self.clock = Clock()
        return BoundedCache(
            max_entries=kwargs.pop("max_entries", 10),
            expires_at=lambda value: value[0],
            clock=self.clock,
            **kwargs,
        )

    def test_expired_entries_are_evicted_on_insert(self):
        cache = self.make()
        cache["old"] = (self.clock.now + 5, "old")
        cache["fresh"] = (self.clock.now + 500, "fresh")
        self.clock.now += 10
        cache["new"] = (self.clock.now + 5, "new")
        self.assertEqual(cache.keys(), ["fresh", "new"])
        self.assertEqual(cache.evicted_expired, 1)

    def test_expired_entries_go_before_live_ones_when_over_the_limit(self):
        cache = self.make(max_entries=3)
        cache["stale"] = (self.clock.now + 5, 1)
        cache["a"] = (self.clock.now + 500, 2)
        cache["b"] = (self.clock.now + 500, 3)
        cache.get("stale")  # most recently used, but about to expire
        self.clock.now += 10
        cache["c"] = (self.clock.now + 500, 4)
        self.assertEqual(cache.keys(), ["a", "b", "c"])

    def test_get_drops_an_expired_entry(self):
        cache = self.make()
        cache["a"] = (self.clock.now + 5, "A")
        self.clock.now += 5
        self.assertIsNone(cache.get("a"))
        self.assertNotIn("a", cache)

    def test_expiry_is_read_from_the_live_value(self):
        cache = self.make()
        entry = [self.clock.now + 500, "A"]
        cache["a"] = entry
        entry[0] = self.clock.now - 1  # edited in place, like marking a library entry expired
        self.assertIsNone(cache.get("a"))

    def test_none_means_the_entry_never_expires(self):
        cache = BoundedCache(max_entries=2, expires_at=lambda value: None)
        cache["a"] = 1
        self.assertEqual(cache.get("a"), 1)
        self.assertEqual(cache.purge_expired(), 0)

    def test_purge_scan_is_throttled_until_a_limit_is_exceeded(self):
        cache = self.make(purge_interval=60)
        cache["old"] = (self.clock.now + 5, "old")
        self.clock.now += 10
        cache["a"] = (self.clock.now + 500, "a")
        self.assertIn("old", cache)  # scanned less than a minute ago
        self.clock.now += 60
        cache["b"] = (self.clock.now + 500, "b")
        self.assertNotIn("old", cache)

    def test_on_evict_reports_the_reason_but_not_explicit_removals(self):
        events = []
        cache = self.make(max_entries=2, on_evict=lambda *args: events.append(args))
        cache["a"] = (self.clock.now + 5, 1)
        cache["b"] = (self.clock.now + 500, 2)
        cache["c"] = (self.clock.now + 500, 3)
        self.assertEqual(events, [("a", (1005.0, 1), "capacity")])
        self.clock.now += 1000
        cache["d"] = (self.clock.now + 5, 4)
        self.assertEqual([event[2] for event in events], ["capacity", "expired", "expired"])
        cache.pop("d")
        cache.clear()
        cache["e"] = (self.clock.now + 5, 5)
        cache["e"] = (self.clock.now + 5, 6)
        self.assertEqual(len(events), 3)


class StatsTests(TestCase):
    def test_stats_report_sizes_limits_and_evictions(self):
        cache = BoundedCache(max_entries=2, max_bytes=100, sizeof=len)
        cache["a"] = "x" * 30
        cache["b"] = "x" * 30
        cache["c"] = "x" * 30
        cache["d"] = "x" * 500
        self.assertEqual(
            cache.stats(),
            {
                "entries": 2,
                "max_entries": 2,
                "bytes": 60,
                "max_bytes": 100,
                "evicted_capacity": 1,
                "evicted_expired": 0,
                "rejected_oversize": 1,
            },
        )

    def test_estimate_json_size(self):
        self.assertEqual(estimate_json_size({"a": [1, 2]}), len('{"a":[1,2]}'))
        self.assertGreater(estimate_json_size({"a": object()}), 0)
        circular: list = []
        circular.append(circular)
        self.assertGreater(estimate_json_size(circular), 0)


if __name__ == "__main__":
    main()
