from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied
from .rules import RuleEngine, calibration_current, instrument_calibration_current, _today


class DomainService:
    # (kind, action) -> (dependency field in result data, invalidation reason)
    CASCADE_INVALIDATIONS = {
        ("instrument", "quarantine"): ("instrument_id", "instrument quarantined"),
        ("calibration", "revoke"): ("calibration_id", "calibration revoked"),
        ("method", "revoke_method"): ("method_id", "method revoked"),
    }

    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        def work(connection):
            entity = self.repository.get_entity_conn(connection, entity_id)
            if not entity:
                raise NotFoundError("entity not found: " + entity_id)
            expected = int(expected_version) if expected_version is not None else entity["version"]

            def lookup(kind, field, value):
                return self.repository.find_entities_conn(
                    connection, self.rules.normalize_kind(kind), field, value
                )

            next_status, patch = self.rules.validate_transition(
                actor, entity, action, dict(data or {}), lookup
            )
            merged = dict(entity["data"])
            merged.update(patch)
            self.repository.update_entity_conn(
                connection, entity_id, expected, next_status, merged
            )
            invalidated = self._cascade_invalidate(connection, entity, action, actor)
            self.repository.append_audit_conn(
                connection,
                entity_id,
                actor.user_id,
                actor.role,
                action,
                entity["status"],
                next_status,
                {"patch": patch, "invalidated": invalidated},
            )
            return self.repository.get_entity_conn(connection, entity_id)

        return self.repository.run_in_transaction(work)

    def _cascade_invalidate(self, connection, entity, action, actor):
        spec = self.CASCADE_INVALIDATIONS.get((entity["kind"], action))
        if not spec:
            return []
        field, reason = spec
        results = [
            item
            for item in self.repository.list_entities_conn(connection, kind="result", status="released")
            if item["data"].get(field) == entity["id"]
        ]
        invalidated = []
        for result in results:
            self.repository.update_entity_conn(
                connection, result["id"], result["version"], "review", result["data"]
            )
            self.repository.append_audit_conn(
                connection,
                result["id"],
                actor.user_id,
                actor.role,
                "invalidate",
                "released",
                "review",
                {"reason": reason, "trigger_kind": entity["kind"], "trigger_id": entity["id"]},
            )
            invalidated.append(result["id"])
        return invalidated

    def _dependency_snapshot(self, connection, result_data):
        instrument = (
            self.repository.get_entity_conn(connection, result_data.get("instrument_id"))
            if result_data.get("instrument_id")
            else None
        )
        method = (
            self.repository.get_entity_conn(connection, result_data.get("method_id"))
            if result_data.get("method_id")
            else None
        )

        def lookup(kind, field, value):
            return self.repository.find_entities_conn(connection, kind, field, value)

        as_of = _today()
        cal_current, calibration = (
            instrument_calibration_current(lookup, instrument, as_of)
            if instrument
            else (False, None)
        )
        valid = (
            instrument is not None
            and instrument["status"] == "active"
            and cal_current
            and method is not None
            and method["status"] == "validated"
            and instrument["id"] in method["data"].get("instrument_ids", [])
        )
        return instrument, calibration, method, valid

    def backfill(self, actor, batch_size=50):
        if actor.role != "admin":
            raise PermissionDenied("backfill requires admin role")
        stats = {"backfilled": 0, "reviewed": 0, "skipped": 0}
        while True:
            candidates = [
                item
                for item in self.repository.list_entities(kind="result", status="released")
                if not item["data"].get("releases")
            ]
            batch = candidates[:batch_size]
            if not batch:
                break
            for result in batch:
                stats[self._backfill_one(result, actor)] += 1
            if len(batch) < batch_size:
                break
        return stats

    def _backfill_one(self, result, actor):
        def work(connection):
            current = self.repository.get_entity_conn(connection, result["id"])
            if not current or current["status"] != "released" or current["data"].get("releases"):
                return "skipped"
            data = dict(current["data"])
            instrument, calibration, method, valid = self._dependency_snapshot(connection, data)
            if valid:
                record = {
                    "backfilled": True,
                    "released_at": current["updated_at"],
                    "released_by": data.get("released_by") or current["created_by"],
                    "instrument_id": instrument["id"],
                    "instrument_version": instrument["version"],
                    "instrument_status": instrument["status"],
                    "calibration_id": calibration["id"] if calibration else None,
                    "calibration_version": calibration["version"] if calibration else None,
                    "calibration_due_at": (
                        calibration["data"].get("due_at")
                        if calibration
                        else instrument["data"].get("due_at")
                    ),
                    "method_id": method["id"],
                    "method_version": method["version"],
                    "value": data.get("value"),
                    "unit": data.get("unit"),
                }
                data["releases"] = [record]
                self.repository.update_entity_conn(
                    connection, current["id"], current["version"], "released", data
                )
                self.repository.append_audit_conn(
                    connection,
                    current["id"],
                    actor.user_id,
                    actor.role,
                    "backfill",
                    "released",
                    "released",
                    {"record": record},
                )
                return "backfilled"
            self.repository.update_entity_conn(
                connection, current["id"], current["version"], "review", data
            )
            self.repository.append_audit_conn(
                connection,
                current["id"],
                actor.user_id,
                actor.role,
                "invalidate",
                "released",
                "review",
                {"reason": "backfill: dependencies no longer valid"},
            )
            return "reviewed"

        return self.repository.run_in_transaction(work)

    def recalculate(self, actor):
        if actor.role != "admin":
            raise PermissionDenied("recalculate requires admin role")
        stats = {"checked": 0, "reviewed": 0}
        for result in self.repository.list_entities(kind="result", status="released"):
            stats["checked"] += 1
            if self._is_still_valid(result):
                continue
            self._flag_for_review(result, actor, "recalculate: dependency no longer valid")
            stats["reviewed"] += 1
        return stats

    def _is_still_valid(self, result):
        def work(connection):
            current = self.repository.get_entity_conn(connection, result["id"])
            if not current or current["status"] != "released":
                return True
            latest = current["data"].get("releases", [{}])[-1]
            instrument = (
                self.repository.get_entity_conn(connection, latest.get("instrument_id"))
                if latest.get("instrument_id")
                else None
            )
            calibration = (
                self.repository.get_entity_conn(connection, latest.get("calibration_id"))
                if latest.get("calibration_id")
                else None
            )
            method = (
                self.repository.get_entity_conn(connection, latest.get("method_id"))
                if latest.get("method_id")
                else None
            )
            as_of = _today()
            if latest.get("calibration_id"):
                calibration_ok = (
                    calibration is not None
                    and calibration["status"] == "approved"
                    and calibration_current(calibration["data"].get("due_at", ""), as_of)
                )
            else:
                calibration_ok = (
                    instrument is not None
                    and calibration_current(instrument["data"].get("due_at", ""), as_of)
                )
            instrument_ok = (
                instrument is not None
                and instrument["status"] == "active"
                and calibration_ok
            )
            method_ok = (
                method is not None
                and method["status"] == "validated"
                and instrument is not None
                and instrument["id"] in method["data"].get("instrument_ids", [])
            )
            return instrument_ok and method_ok

        return self.repository.run_in_transaction(work)

    def _flag_for_review(self, result, actor, reason):
        def work(connection):
            current = self.repository.get_entity_conn(connection, result["id"])
            if not current or current["status"] != "released":
                return False
            self.repository.update_entity_conn(
                connection, current["id"], current["version"], "review", current["data"]
            )
            self.repository.append_audit_conn(
                connection,
                current["id"],
                actor.user_id,
                actor.role,
                "invalidate",
                "released",
                "review",
                {"reason": reason},
            )
            return True

        return self.repository.run_in_transaction(work)

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
