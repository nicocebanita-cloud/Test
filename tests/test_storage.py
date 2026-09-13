import csv
import os
import tempfile
import unittest

from discord_username_checker.checker import STATUS_AVAILABLE, STATUS_ERROR, STATUS_TAKEN, CheckResult
import time

from discord_username_checker.storage import ResultStore, load_pause_until, save_pause_until


class ResultStoreTests(unittest.TestCase):
    def test_record_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = os.path.join(tmp, "sous", "resultats.csv")
            available = os.path.join(tmp, "sous", "disponibles.txt")
            with ResultStore(results, available) as store:
                store.record(CheckResult("abcd", STATUS_AVAILABLE, http_status=200))
                store.record(CheckResult("efgh", STATUS_TAKEN, http_status=200))
                store.record(CheckResult("ijkl", STATUS_ERROR, "réseau : timeout"))

            with open(available, encoding="utf-8") as handle:
                self.assertEqual(handle.read().split(), ["abcd"])

            with open(results, encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([r["username"] for r in rows], ["abcd", "efgh", "ijkl"])
            self.assertEqual(rows[2]["detail"], "réseau : timeout")

            # Les erreurs doivent être re-testées lors d'une reprise.
            store = ResultStore(results, available)
            self.assertEqual(store.load_checked(), {"abcd", "efgh"})

            # Une réouverture n'ajoute pas de second en-tête.
            with store:
                store.record(CheckResult("mnop", STATUS_TAKEN, http_status=200))
            with open(results, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
            self.assertEqual(lines.count("username,status,http_status,detail,checked_at"), 1)
            self.assertEqual(len(lines), 5)

    def test_load_checked_without_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(os.path.join(tmp, "x.csv"), os.path.join(tmp, "y.txt"))
            self.assertEqual(store.load_checked(), set())


if __name__ == "__main__":
    unittest.main()


class PauseFileTests(unittest.TestCase):
    def test_round_trip_and_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sous", "pause.json")
            self.assertEqual(load_pause_until(path), 0.0)
            future = time.time() + 600
            save_pause_until(path, future)
            self.assertAlmostEqual(load_pause_until(path), future, places=3)
            save_pause_until(path, time.time() - 5)
            self.assertEqual(load_pause_until(path), 0.0)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("corrompu")
            self.assertEqual(load_pause_until(path), 0.0)


class LoadLatestTests(unittest.TestCase):
    def test_last_row_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = os.path.join(tmp, "resultats.csv")
            available = os.path.join(tmp, "disponibles.txt")
            with ResultStore(results, available) as store:
                store.record(CheckResult("abcd", STATUS_TAKEN, http_status=200))
                store.record(CheckResult("abcd", STATUS_AVAILABLE, http_status=200))
                store.record(CheckResult("efgh", STATUS_ERROR, "réseau"))
            latest = ResultStore(results, available).load_latest()
            self.assertEqual(latest["abcd"][0], STATUS_AVAILABLE)
            self.assertEqual(latest["efgh"][0], STATUS_ERROR)
            self.assertAlmostEqual(latest["abcd"][1], time.time(), delta=5)
            self.assertEqual(ResultStore(os.path.join(tmp, "x.csv"), available).load_latest(), {})
