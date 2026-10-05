import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class DependencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _build_chain(self):
        instrument = self.service.create(
            self.actor, "instrument", {"name": "Analyzer", "serial": "A-1"}
        )
        self.service.transition(self.actor, instrument["id"], "send_calibration", {})
        self.service.transition(
            self.actor,
            instrument["id"],
            "calibrate",
            {"due_at": "2099-01-01", "passed": True},
        )
        calibration = self.service.create(
            self.actor,
            "calibration",
            {"instrument_id": instrument["id"], "requested_at": "2026-01-01"},
        )
        self.service.transition(
            self.actor,
            calibration["id"],
            "perform",
            {
                "result": "passed",
                "performed_at": "2026-01-02",
                "uncertainty": 0.01,
                "due_at": "2099-01-01",
            },
        )
        self.service.transition(
            self.actor, calibration["id"], "approve", {"authorized_by": "QA-1"}
        )
        method = self.service.create(
            self.actor, "method", {"name": "Assay-A", "version": "v1"}
        )
        self.service.transition(
            self.actor,
            method["id"],
            "validate_method",
            {"parameters": {"range": [0, 10]}, "instrument_ids": [instrument["id"]]},
        )
        result = self.service.create(
            self.actor,
            "result",
            {"sample_id": "S-1", "measurement": "4.2 +/- 0.1 mg/L"},
        )
        return (
            self.service.get(instrument["id"]),
            self.service.get(calibration["id"]),
            self.service.get(method["id"]),
            self.service.get(result["id"]),
        )

    def _release(self, result, instrument, method, **extra):
        data = {
            "instrument_id": instrument["id"],
            "method_id": method["id"],
            "value": 4.2,
            "unit": "mg/L",
        }
        data.update(extra)
        return self.service.transition(self.actor, result["id"], "release", data)

    def test_release_registers_dependency_snapshot(self):
        instrument, calibration, method, result = self._build_chain()
        released = self._release(result, instrument, method)
        self.assertEqual(released["status"], "released")
        record = released["data"]["releases"][0]
        self.assertEqual(record["instrument_id"], instrument["id"])
        self.assertEqual(record["instrument_version"], instrument["version"])
        self.assertEqual(record["instrument_status"], "active")
        self.assertEqual(record["calibration_id"], calibration["id"])
        self.assertEqual(record["calibration_version"], calibration["version"])
        self.assertEqual(record["calibration_due_at"], "2099-01-01")
        self.assertEqual(record["method_id"], method["id"])
        self.assertEqual(record["method_version"], method["version"])
        self.assertEqual(record["value"], 4.2)
        self.assertEqual(record["unit"], "mg/L")
        self.assertEqual(record["released_by"], "admin")
        self.assertTrue(record["released_at"])
        # original measurement untouched
        self.assertEqual(released["data"]["measurement"], "4.2 +/- 0.1 mg/L")

    def test_revoke_method_invalidates_released_result(self):
        instrument, _, method, result = self._build_chain()
        self._release(result, instrument, method)
        self.service.transition(
            self.actor, method["id"], "revoke_method", {"reason": "withdrawn"}
        )
        flagged = self.service.get(result["id"])
        self.assertEqual(flagged["status"], "review")
        entries = self.service.audit_log(result["id"])
        self.assertEqual(entries[-1]["action"], "invalidate")
        self.assertEqual(entries[-1]["to_status"], "review")
        self.assertEqual(entries[-1]["detail"]["reason"], "method revoked")

    def test_revoke_calibration_invalidates_released_result(self):
        instrument, calibration, method, result = self._build_chain()
        self._release(result, instrument, method)
        self.service.transition(
            self.actor, calibration["id"], "revoke", {"reason": "drift found"}
        )
        self.assertEqual(self.service.get(result["id"])["status"], "review")

    def test_quarantine_invalidates_released_result(self):
        instrument, _, method, result = self._build_chain()
        self._release(result, instrument, method)
        self.service.transition(
            self.actor, instrument["id"], "quarantine", {"reason": "suspected fault"}
        )
        self.assertEqual(self.service.get(result["id"])["status"], "review")

    def test_rerelease_revalidates_and_keeps_history(self):
        instrument, _, method, result = self._build_chain()
        self._release(result, instrument, method)
        self.service.transition(
            self.actor, method["id"], "revoke_method", {"reason": "withdrawn"}
        )
        flagged = self.service.get(result["id"])
        self.assertEqual(flagged["status"], "review")
        # re-release against the revoked method must fail and stay in review
        with self.assertRaises(ValidationError):
            self._release(flagged, instrument, method)
        self.assertEqual(self.service.get(result["id"])["status"], "review")
        # a new validated method version makes re-release possible
        method2 = self.service.create(
            self.actor, "method", {"name": "Assay-A", "version": "v2"}
        )
        self.service.transition(
            self.actor,
            method2["id"],
            "validate_method",
            {"parameters": {"range": [0, 10]}, "instrument_ids": [instrument["id"]]},
        )
        rereleased = self._release(flagged, instrument, method2)
        self.assertEqual(rereleased["status"], "released")
        self.assertEqual(len(rereleased["data"]["releases"]), 2)
        self.assertEqual(rereleased["data"]["releases"][-1]["method_id"], method2["id"])
        self.assertEqual(rereleased["data"]["measurement"], "4.2 +/- 0.1 mg/L")

    def test_expected_versions_conflict_when_dependency_changed(self):
        instrument, _, method, result = self._build_chain()
        with self.assertRaises(ConflictError):
            self._release(
                result,
                instrument,
                method,
                expected_versions={"instrument": instrument["version"] + 1},
            )
        self.assertEqual(self.service.get(result["id"])["status"], "pending")
        released = self._release(
            result, instrument, method, expected_versions={"instrument": instrument["version"]}
        )
        self.assertEqual(released["status"], "released")

    def test_quarantine_first_blocks_release(self):
        instrument, _, method, result = self._build_chain()
        self.service.transition(
            self.actor, instrument["id"], "quarantine", {"reason": "hold"}
        )
        with self.assertRaises(ValidationError):
            self._release(result, instrument, method)
        self.assertEqual(self.service.get(result["id"])["status"], "pending")

    def test_recalculate_flags_expired_calibration(self):
        instrument, calibration, method, result = self._build_chain()
        self._release(result, instrument, method)
        # calibration ages out
        stale = dict(self.service.get(calibration["id"])["data"])
        stale["due_at"] = "2020-01-01"
        self.repo.update_entity(calibration["id"], calibration["version"], "approved", stale)
        stats = self.service.recalculate(self.actor)
        self.assertGreaterEqual(stats["reviewed"], 1)
        self.assertEqual(self.service.get(result["id"])["status"], "review")

    def test_backfill_registers_snapshot_and_is_idempotent(self):
        instrument, _, method, _ = self._build_chain()
        # simulate a legacy released result without dependency registration
        legacy = self.repo.create_entity(
            "legacy-1",
            "result",
            "released",
            {
                "sample_id": "S-9",
                "measurement": "1.0",
                "instrument_id": instrument["id"],
                "method_id": method["id"],
                "value": 1.0,
                "unit": "mg/L",
            },
            "admin",
        )
        stats = self.service.backfill(self.actor)
        self.assertEqual(stats["backfilled"], 1)
        self.assertEqual(stats["reviewed"], 0)
        backfilled = self.service.get(legacy["id"])
        self.assertEqual(len(backfilled["data"]["releases"]), 1)
        self.assertTrue(backfilled["data"]["releases"][0]["backfilled"])
        self.assertEqual(backfilled["data"]["releases"][0]["instrument_id"], instrument["id"])
        # second run: nothing to do, and new releases are not touched
        stats2 = self.service.backfill(self.actor)
        self.assertEqual(stats2["backfilled"], 0)
        self.assertEqual(stats2["reviewed"], 0)
        instrument2, _, method2, result2 = self._build_chain()
        self._release(result2, instrument2, method2)
        stats3 = self.service.backfill(self.actor)
        self.assertEqual(stats3["backfilled"], 0)
        self.assertEqual(self.service.get(result2["id"])["data"]["releases"][0].get("backfilled"), None)

    def test_backfill_flags_legacy_with_dead_dependency(self):
        instrument, _, method, _ = self._build_chain()
        self.service.transition(
            self.actor, instrument["id"], "quarantine", {"reason": "hold"}
        )
        self.repo.create_entity(
            "legacy-2",
            "result",
            "released",
            {
                "sample_id": "S-10",
                "measurement": "2.0",
                "instrument_id": instrument["id"],
                "method_id": method["id"],
                "value": 2.0,
                "unit": "mg/L",
            },
            "admin",
        )
        stats = self.service.backfill(self.actor)
        self.assertEqual(stats["reviewed"], 1)
        self.assertEqual(self.service.get("legacy-2")["status"], "review")

    def test_release_requires_current_calibration(self):
        instrument, calibration, method, result = self._build_chain()
        stale = dict(self.service.get(calibration["id"])["data"])
        stale["due_at"] = "2020-01-01"
        self.repo.update_entity(calibration["id"], calibration["version"], "approved", stale)
        with self.assertRaises(ValidationError):
            self._release(result, instrument, method)

    def test_release_requires_method_covering_instrument(self):
        instrument, _, method, result = self._build_chain()
        other = self.service.create(
            self.actor, "instrument", {"name": "Other", "serial": "O-1"}
        )
        self.service.transition(self.actor, other["id"], "send_calibration", {})
        self.service.transition(
            self.actor, other["id"], "calibrate", {"due_at": "2099-01-01", "passed": True}
        )
        with self.assertRaises(ValidationError):
            self._release(result, other, method)

    def test_concurrent_release_and_quarantine_first_wins(self):
        instrument, _, method, result = self._build_chain()
        barrier = threading.Barrier(2)
        outcomes = []

        def release_worker():
            barrier.wait()
            try:
                released = self._release(result, instrument, method)
                outcomes.append(("released", released))
            except (ValidationError, ConflictError) as exc:
                outcomes.append(("blocked", exc))

        def quarantine_worker():
            barrier.wait()
            try:
                self.service.transition(
                    self.actor, instrument["id"], "quarantine", {"reason": "race"}
                )
                outcomes.append(("quarantined", None))
            except (ValidationError, ConflictError) as exc:
                outcomes.append(("quarantine_failed", exc))

        threads = [
            threading.Thread(target=release_worker),
            threading.Thread(target=quarantine_worker),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive(), "worker deadlocked")

        result_state = self.service.get(result["id"])
        instrument_state = self.service.get(instrument["id"])
        # quarantine always lands; release outcome depends on arrival order
        self.assertEqual(instrument_state["status"], "quarantined")
        release_outcome = [item for item in outcomes if item[0] == "released"]
        if release_outcome:
            # release arrived first: it committed under the active instrument,
            # then the quarantine cascaded and flagged the result for review
            record = result_state["data"].get("releases", [{}])[-1]
            self.assertEqual(record.get("instrument_status"), "active")
            self.assertEqual(result_state["status"], "review")
        else:
            # quarantine arrived first: release was rejected, result untouched
            self.assertEqual(result_state["status"], "pending")
            self.assertNotIn("releases", result_state["data"])
        # no unexpected failures (e.g. database locked / 500-class errors)
        unexpected = [
            item for item in outcomes
            if item[0] not in ("released", "blocked", "quarantined")
        ]
        self.assertEqual(unexpected, [])


if __name__ == "__main__":
    unittest.main()
