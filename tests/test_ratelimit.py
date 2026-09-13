import threading
import time
import unittest

from discord_username_checker.ratelimit import RateLimiter, sleep_interruptible


class RateLimiterTests(unittest.TestCase):
    def test_spacing_between_requests(self):
        limiter = RateLimiter(50)  # 20 ms d'intervalle
        start = time.monotonic()
        for _ in range(6):
            self.assertTrue(limiter.acquire())
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.09)

    def test_pause_blocks_everyone(self):
        limiter = RateLimiter(1000)
        limiter.pause(0.3)
        self.assertGreater(limiter.paused_for(), 0.1)
        start = time.monotonic()
        self.assertTrue(limiter.acquire())
        self.assertGreaterEqual(time.monotonic() - start, 0.25)

    def test_acquire_returns_false_when_stopped(self):
        limiter = RateLimiter(1000)
        limiter.pause(5)
        stop = threading.Event()
        stop.set()
        self.assertFalse(limiter.acquire(stop))

    def test_invalid_rate(self):
        with self.assertRaises(ValueError):
            RateLimiter(0)

    def test_sleep_interruptible(self):
        stop = threading.Event()
        stop.set()
        start = time.monotonic()
        self.assertFalse(sleep_interruptible(5, stop))
        self.assertLess(time.monotonic() - start, 1)
        self.assertTrue(sleep_interruptible(0.01))


if __name__ == "__main__":
    unittest.main()
