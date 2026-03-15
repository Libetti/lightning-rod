from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from app import glm


class GLMCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        with glm._recent_cache_lock:
            glm._recent_cache.clear()
        with glm._refresh_locks_guard:
            glm._refresh_locks.clear()

    def test_cache_hit_skips_fetch(self) -> None:
        cache_key = ("goes-east", 10)
        with glm._recent_cache_lock:
            glm._recent_cache[cache_key] = glm._RecentCacheEntry(
                expires_at=time.monotonic() + 60,
                events=[
                    glm.FlashEvent(
                        id="1",
                        latitude=1.0,
                        longitude=2.0,
                        time="2026-03-15T00:00:00Z",
                    )
                ],
            )

        with patch("app.glm._latest_glm_file", side_effect=AssertionError("should not fetch")):
            events = glm.fetch_recent_lightning("goes-east", 10)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].id, "1")

    def test_latest_glm_file_surfaces_goes_latest_error(self) -> None:
        def raise_boom(**_: object):
            raise RuntimeError("boom")

        with patch("app.glm._load_goes_latest", return_value=raise_boom):
            with self.assertRaises(glm.GLMFetchError) as ctx:
                glm._latest_glm_file(18)

        self.assertIn("Unable to resolve latest GLM file with goes2go", str(ctx.exception))
        self.assertIn("boom", str(ctx.exception))

    def test_expired_cache_singleflight_same_key(self) -> None:
        cache_key = ("goes-east", 5)
        with glm._recent_cache_lock:
            glm._recent_cache[cache_key] = glm._RecentCacheEntry(
                expires_at=time.monotonic() - 1,
                events=[],
            )

        fetch_calls = 0
        fetch_calls_lock = threading.Lock()

        def fake_latest(*, satellite_id: int):
            nonlocal fetch_calls
            with fetch_calls_lock:
                fetch_calls += 1
            time.sleep(0.05)
            return glm.Path("/tmp/fake.nc")

        def fake_parse(*, nc_path: glm.Path, limit: int):
            return [
                glm.FlashEvent(
                    id="singleflight",
                    latitude=0.0,
                    longitude=0.0,
                    time="2026-03-15T00:00:00Z",
                )
                for _ in range(limit if limit < 2 else 1)
            ]

        results: list[list[glm.FlashEvent]] = []
        errors: list[Exception] = []

        def call_fetch() -> None:
            try:
                results.append(glm.fetch_recent_lightning("goes-east", 5))
            except Exception as exc:  # pragma: no cover - debug helper
                errors.append(exc)

        threads = [threading.Thread(target=call_fetch) for _ in range(6)]
        with patch("app.glm._latest_glm_file", side_effect=fake_latest), patch(
            "app.glm._parse_flashes", side_effect=fake_parse
        ):
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(errors, [])
        self.assertEqual(fetch_calls, 1)
        self.assertEqual(len(results), 6)
        self.assertTrue(all(len(r) == 1 for r in results))

    def test_concurrent_different_keys_are_serialized_for_native_io(self) -> None:
        started = 0
        started_lock = threading.Lock()

        def fake_latest(*, satellite_id: int):
            nonlocal started
            with started_lock:
                started += 1
            time.sleep(0.05)
            return glm.Path("/tmp/fake.nc")

        def fake_parse(*, nc_path: glm.Path, limit: int):
            return [
                glm.FlashEvent(
                    id="parallel",
                    latitude=0.0,
                    longitude=0.0,
                    time="2026-03-15T00:00:00Z",
                )
            ]

        errors: list[Exception] = []

        def call_fetch(limit: int) -> None:
            try:
                glm.fetch_recent_lightning("goes-east", limit)
            except Exception as exc:  # pragma: no cover - debug helper
                errors.append(exc)

        t1 = threading.Thread(target=call_fetch, args=(10,))
        t2 = threading.Thread(target=call_fetch, args=(20,))

        begin = time.perf_counter()
        with patch("app.glm._latest_glm_file", side_effect=fake_latest), patch(
            "app.glm._parse_flashes", side_effect=fake_parse
        ):
            t1.start()
            t2.start()
            t1.join()
            t2.join()
        elapsed = time.perf_counter() - begin

        self.assertEqual(errors, [])
        self.assertEqual(started, 2)
        self.assertGreaterEqual(elapsed, 0.09)


if __name__ == "__main__":
    unittest.main()
