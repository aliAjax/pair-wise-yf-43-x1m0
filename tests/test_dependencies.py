import threading
import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class DependencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.rules = RuleEngine(as_of="2026-10-01")
        self.service = DomainService(self.repo, self.rules)
        self.admin = Actor("admin", "admin")
        self.metrology = Actor("metro-1", "metrology")
        self.authorizer = Actor("auth-1", "authorizer")
        self.analyst = Actor("analyst-1", "analyst")

    def tearDown(self):
        self.tmp.cleanup()

    # -- fixtures -------------------------------------------------------

    def setup_release(self, due_at="2099-01-01", as_of="2026-10-01"):
        instrument = self.service.create(
            self.admin, "instrument", {"name": "Analyzer", "serial": "A-1"}
        )
        calibration = self.service.create(
            self.metrology, "calibration",
            {"instrument_id": instrument["id"], "requested_at": "2026-01-01"},
        )
        self.service.transition(
            self.metrology, calibration["id"], "perform",
            {"result": "passed", "performed_at": "2026-01-02",
             "uncertainty": 0.01, "due_at": due_at},
        )
        self.service.transition(
            self.authorizer, calibration["id"], "approve",
            {"authorized_by": "QA-1"},
        )
        method = self.service.create(
            self.authorizer, "method", {"name": "Assay-A", "version": "v1"}
        )
        self.service.transition(
            self.authorizer, method["id"], "validate_method",
            {"parameters": {"range": [0, 10]},
             "instrument_ids": [instrument["id"]]},
        )
        result = self.service.create(
            self.analyst, "result",
            {"sample_id": "S-1", "measurement": "raw-reading-001"},
        )
        released = self.service.transition(
            self.analyst, result["id"], "release",
            {"instrument_id": instrument["id"], "method_id": method["id"],
             "value": 4.2, "unit": "mg/L"},
        )
        return {
            "instrument": instrument,
            "calibration": calibration,
            "method": method,
            "result": released,
        }

    # -- release snapshot ----------------------------------------------

    def test_release_records_dependency_snapshot(self):
        parts = self.setup_release()
        result = parts["result"]
        self.assertEqual(result["status"], "released")
        # Raw measurement is preserved untouched.
        self.assertEqual(result["data"]["measurement"], "raw-reading-001")
        records = self.service.release_records(result["id"])
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertTrue(record["active"])
        self.assertEqual(record["instrument_id"], parts["instrument"]["id"])
        self.assertEqual(record["calibration_id"], parts["calibration"]["id"])
        self.assertEqual(record["method_id"], parts["method"]["id"])
        self.assertEqual(record["due_at"], "2099-01-01")
        self.assertEqual(record["value"], "4.2")
        audit = self.service.audit_log(result["id"])
        release_audit = [row for row in audit if row["action"] == "release"][0]
        self.assertEqual(release_audit["detail"]["calibration_id"],
                         parts["calibration"]["id"])

    def test_release_requires_current_approved_calibration(self):
        # Instrument without any approved calibration cannot release.
        instrument = self.service.create(
            self.admin, "instrument", {"name": "B", "serial": "B-1"})
        method = self.service.create(
            self.authorizer, "method", {"name": "M", "version": "v1"})
        self.service.transition(
            self.authorizer, method["id"], "validate_method",
            {"parameters": {"range": [0, 1]},
             "instrument_ids": [instrument["id"]]})
        result = self.service.create(
            self.analyst, "result", {"sample_id": "S", "measurement": "x"})
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.analyst, result["id"], "release",
                {"instrument_id": instrument["id"], "method_id": method["id"],
                 "value": 1, "unit": "x"})

    # -- cascading invalidation ----------------------------------------

    def test_calibration_revoke_invalidates_result(self):
        parts = self.setup_release()
        updated = self.service.transition(
            self.authorizer, parts["calibration"]["id"], "revoke",
            {"reason": "found systematic bias"})
        self.assertEqual(updated["status"], "revoked")
        result = self.service.get(parts["result"]["id"])
        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(result["data"]["pending_review_reason"],
                         "calibration_revoked")
        record = self.service.release_records(result["id"])[0]
        self.assertFalse(record["active"])
        self.assertEqual(record["invalid_reason"], "calibration_revoked")
        audit = [row for row in self.service.audit_log(result["id"])
                 if row["action"] == "dependency_invalidated"]
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["from_status"], "released")
        self.assertEqual(audit[0]["to_status"], "pending_review")
        self.assertEqual(audit[0]["detail"]["source"], "calibration_revoked")

    def test_method_revoke_invalidates_result(self):
        parts = self.setup_release()
        self.service.transition(
            self.authorizer, parts["method"]["id"], "revoke_method",
            {"reason": "standard withdrawn"})
        result = self.service.get(parts["result"]["id"])
        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(result["data"]["pending_review_reason"],
                         "method_revoked")

    def test_instrument_quarantine_invalidates_result(self):
        parts = self.setup_release()
        self.service.transition(
            self.metrology, parts["instrument"]["id"], "quarantine",
            {"reason": "maintenance"})
        result = self.service.get(parts["result"]["id"])
        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(result["data"]["pending_review_reason"],
                         "instrument_inactive")

    def test_new_approved_calibration_supersedes_released_results(self):
        parts = self.setup_release()
        new_cal = self.service.create(
            self.metrology, "calibration",
            {"instrument_id": parts["instrument"]["id"],
             "requested_at": "2026-06-01"})
        self.service.transition(
            self.metrology, new_cal["id"], "perform",
            {"result": "passed", "performed_at": "2026-06-02",
             "uncertainty": 0.02, "due_at": "2100-01-01"})
        self.service.transition(
            self.authorizer, new_cal["id"], "approve",
            {"authorized_by": "QA-2"})
        result = self.service.get(parts["result"]["id"])
        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(result["data"]["pending_review_reason"],
                         "calibration_superseded")
        records = self.service.release_records(result["id"])
        self.assertFalse(records[0]["active"])

    # -- re-release against current basis ------------------------------

    def test_rerelease_revalidates_and_appends_history(self):
        parts = self.setup_release()
        self.service.transition(
            self.authorizer, parts["method"]["id"], "revoke_method",
            {"reason": "withdrawn"})
        result = self.service.get(parts["result"]["id"])
        self.assertEqual(result["status"], "pending_review")

        # Re-release must fail while the method is still revoked.
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.analyst, result["id"], "release",
                {"instrument_id": parts["instrument"]["id"],
                 "method_id": parts["method"]["id"],
                 "value": 4.3, "unit": "mg/L"})

        # Publish a new validated method and re-release against it.
        method_v2 = self.service.create(
            self.authorizer, "method", {"name": "Assay-A", "version": "v2"})
        self.service.transition(
            self.authorizer, method_v2["id"], "validate_method",
            {"parameters": {"range": [0, 10]},
             "instrument_ids": [parts["instrument"]["id"]]})
        re_released = self.service.transition(
            self.analyst, result["id"], "release",
            {"instrument_id": parts["instrument"]["id"],
             "method_id": method_v2["id"],
             "value": 4.3, "unit": "mg/L"})
        self.assertEqual(re_released["status"], "released")
        self.assertIsNone(re_released["data"]["pending_review_reason"])
        # Original raw measurement and all release history survive.
        self.assertEqual(re_released["data"]["measurement"], "raw-reading-001")
        records = self.service.release_records(result["id"])
        self.assertEqual(len(records), 2)
        self.assertFalse(records[0]["active"])
        self.assertEqual(records[0]["method_id"], parts["method"]["id"])
        self.assertTrue(records[1]["active"])
        self.assertEqual(records[1]["method_id"], method_v2["id"])
        actions = [row["action"] for row in self.service.audit_log(result["id"])
                   if row["action"] != "create"]
        self.assertEqual(
            actions,
            ["release", "dependency_invalidated", "release"],
        )

    # -- expiry recomputation ------------------------------------------

    def test_recalculation_invalidates_expired_calibration(self):
        # Frozen clock at release time says the calibration is current.
        self.rules._as_of = "2026-10-01"
        parts = self.setup_release(due_at="2026-10-02")
        # Time advances past the due date with no calibration update.
        report = self.service.recalculate_dependencies(as_of="2026-10-03")
        self.assertEqual(report["checked"], 1)
        self.assertEqual(report["invalidated"], 1)
        result = self.service.get(parts["result"]["id"])
        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(result["data"]["pending_review_reason"],
                         "calibration_expired")

    def test_recalculation_leaves_valid_releases_untouched(self):
        parts = self.setup_release()
        report = self.service.recalculate_dependencies(as_of="2026-10-01")
        self.assertEqual(report["invalidated"], 0)
        self.assertEqual(
            self.service.get(parts["result"]["id"])["status"], "released")

    # -- legacy backfill ------------------------------------------------

    def test_backfill_binds_legacy_result_from_current_state(self):
        # Simulate a result released before dependency tracking existed:
        # write it straight to released with old-style data and no records.
        instrument = self.service.create(
            self.admin, "instrument", {"name": "Legacy", "serial": "L-1"})
        calibration = self.service.create(
            self.metrology, "calibration",
            {"instrument_id": instrument["id"], "requested_at": "2026-01-01"})
        self.service.transition(
            self.metrology, calibration["id"], "perform",
            {"result": "passed", "performed_at": "2026-01-02",
             "uncertainty": 0.01, "due_at": "2099-01-01"})
        self.service.transition(
            self.authorizer, calibration["id"], "approve",
            {"authorized_by": "QA-1"})
        method = self.service.create(
            self.authorizer, "method", {"name": "M", "version": "v1"})
        self.service.transition(
            self.authorizer, method["id"], "validate_method",
            {"parameters": {"range": [0, 1]},
             "instrument_ids": [instrument["id"]]})
        result = self.service.create(
            self.analyst, "result",
            {"sample_id": "OLD-1", "measurement": "legacy-raw",
             "instrument_id": instrument["id"], "method_id": method["id"],
             "value": 9.9, "unit": "mg/L"})
        result = self.repo.update_entity(result["id"], None, "released",
                                         result["data"])
        self.assertEqual(self.service.release_records(result["id"]), [])

        report = self.service.backfill_legacy_releases(as_of="2026-10-01")
        self.assertEqual(report["processed"], 1)
        self.assertEqual(report["backfilled"], 1)
        self.assertEqual(report["invalidated"], 0)
        result = self.service.get(result["id"])
        self.assertEqual(result["status"], "released")
        records = self.service.release_records(result["id"])
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["active"])
        self.assertEqual(records[0]["calibration_id"], calibration["id"])

    def test_backfill_flags_legacy_result_with_revoked_method(self):
        instrument = self.service.create(
            self.admin, "instrument", {"name": "Legacy", "serial": "L-2"})
        calibration = self.service.create(
            self.metrology, "calibration",
            {"instrument_id": instrument["id"], "requested_at": "2026-01-01"})
        self.service.transition(
            self.metrology, calibration["id"], "perform",
            {"result": "passed", "performed_at": "2026-01-02",
             "uncertainty": 0.01, "due_at": "2099-01-01"})
        self.service.transition(
            self.authorizer, calibration["id"], "approve",
            {"authorized_by": "QA-1"})
        method = self.service.create(
            self.authorizer, "method", {"name": "M", "version": "v1"})
        self.service.transition(
            self.authorizer, method["id"], "validate_method",
            {"parameters": {"range": [0, 1]},
             "instrument_ids": [instrument["id"]]})
        result = self.service.create(
            self.analyst, "result",
            {"sample_id": "OLD-2", "measurement": "legacy-raw-2",
             "instrument_id": instrument["id"], "method_id": method["id"],
             "value": 1.1, "unit": "mg/L"})
        self.repo.update_entity(result["id"], None, "released", result["data"])
        # Method has since been revoked.
        self.service.transition(
            self.authorizer, method["id"], "revoke_method",
            {"reason": "old standard"})

        report = self.service.backfill_legacy_releases(as_of="2026-10-01")
        self.assertEqual(report["backfilled"], 0)
        self.assertEqual(report["invalidated"], 1)
        result = self.service.get(result["id"])
        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(result["data"]["pending_review_reason"],
                         "method_revoked")
        records = self.service.release_records(result["id"])
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["active"])
        self.assertEqual(records[0]["detail"]["source"], "legacy_backfill")

    def test_backfill_does_not_touch_tracked_releases(self):
        parts = self.setup_release()
        report = self.service.backfill_legacy_releases(as_of="2026-10-01")
        self.assertEqual(report["processed"], 0)
        self.assertEqual(
            self.service.get(parts["result"]["id"])["status"], "released")

    # -- concurrency: first committer wins -----------------------------

    def test_quarantine_and_release_on_same_instrument_first_committer_wins(self):
        # Repeated barrier-synchronized rounds: quarantine and release for
        # the same instrument are committed concurrently. The repository
        # serializes them, so "first committed wins" must leave the system
        # in one of two consistent states and never keep a result released
        # against a quarantined instrument.
        outcomes = []
        for _ in range(10):
            self._race_round(outcomes)
        self.assertEqual(len(outcomes), 0)

    def _race_round(self, outcomes):
        instrument = self.service.create(
            self.admin, "instrument",
            {"name": "Race-Inst", "serial": "R-%d" % len(outcomes)})
        calibration = self.service.create(
            self.metrology, "calibration",
            {"instrument_id": instrument["id"], "requested_at": "2026-01-01"})
        self.service.transition(
            self.metrology, calibration["id"], "perform",
            {"result": "passed", "performed_at": "2026-01-02",
             "uncertainty": 0.01, "due_at": "2099-01-01"})
        self.service.transition(
            self.authorizer, calibration["id"], "approve",
            {"authorized_by": "QA-1"})
        method = self.service.create(
            self.authorizer, "method",
            {"name": "Race-M", "version": "v%d" % len(outcomes)})
        self.service.transition(
            self.authorizer, method["id"], "validate_method",
            {"parameters": {"range": [0, 1]},
             "instrument_ids": [instrument["id"]]})
        target = self.service.create(
            self.analyst, "result",
            {"sample_id": "RACE-%d" % len(outcomes), "measurement": "raw"})

        barrier = threading.Barrier(2)
        errors = []

        def quarantine():
            barrier.wait()
            try:
                self.service.transition(
                    self.metrology, instrument["id"], "quarantine",
                    {"reason": "concurrent"})
            except Exception as exc:
                errors.append(("quarantine", exc))

        def release():
            barrier.wait()
            try:
                self.service.transition(
                    self.analyst, target["id"], "release",
                    {"instrument_id": instrument["id"],
                     "method_id": method["id"], "value": 7.0,
                     "unit": "mg/L"})
            except ValidationError as exc:
                # Legitimate "quarantine committed first" branch: the
                # loser sees the instrument as no longer active.
                errors.append(("release-rejected", str(exc)))
            except Exception as exc:
                errors.append(("release", exc))

        t1 = threading.Thread(target=quarantine)
        t2 = threading.Thread(target=release)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        for name, detail in errors:
            self.assertEqual(name, "release-rejected",
                             "unexpected error: %r" % (detail,))

        instrument_after = self.service.get(instrument["id"])
        result_after = self.service.get(target["id"])
        # Serialized first-committer order gives exactly one of:
        # - quarantine first: release is rejected, result stays pending;
        # - release first: it is released, then the quarantine immediately
        #   cascades it to pending_review in the same serialized order.
        # In BOTH orders the result must never remain released against a
        # quarantined instrument.
        self.assertEqual(instrument_after["status"], "quarantined")
        self.assertNotEqual(result_after["status"], "released")
        if result_after["status"] == "pending_review":
            self.assertEqual(result_after["data"]["pending_review_reason"],
                             "instrument_inactive")
        else:
            self.assertEqual(result_after["status"], "pending")
            self.assertTrue(
                any(name == "release-rejected" for name, _ in errors))


if __name__ == "__main__":
    unittest.main()
