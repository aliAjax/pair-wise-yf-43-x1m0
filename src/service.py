from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, NotFoundError, PermissionDenied
from .repository import utcnow
from .rules import (
    INVALID_REASON_LABELS,
    RuleEngine,
    evaluate_release_binding,
    resolve_current_calibration,
)


SYSTEM_ACTOR = Actor("system-dependency", "system")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    # -- basics ---------------------------------------------------------

    def _lookup_conn(self, connection):
        def lookup(kind, field, value):
            return self.repository.find_entities_conn(
                connection, self.rules.normalize_kind(kind), field, value
            )
        return lookup

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

    def release_records(self, result_id):
        if not self.repository.get_entity(result_id):
            raise NotFoundError("entity not found: " + result_id)
        return self.repository.list_release_records(result_id)

    # -- transitions ----------------------------------------------------

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        payload = dict(data or {})
        # A single BEGIN IMMEDIATE transaction covers the action, the
        # release snapshot and every cascading invalidation. Concurrent
        # submitters on the same instrument therefore serialize: whoever
        # gets the write lock first wins, and the loser sees the new state.
        with self.repository.transaction() as connection:
            entity = self.repository.get_entity_conn(connection, entity_id)
            if not entity:
                raise NotFoundError("entity not found: " + entity_id)
            expected = (
                int(expected_version)
                if expected_version is not None
                else entity["version"]
            )
            next_status, patch = self.rules.validate_transition(
                actor, entity, action, payload, self._lookup_conn(connection)
            )
            merged = dict(entity["data"])
            merged.update(patch)

            if entity["kind"] == "result" and action == "release":
                updated = self._register_release(
                    connection, actor, entity, expected, merged
                )
            else:
                updated = self.repository.update_entity_conn(
                    connection, entity_id, expected, next_status, merged
                )
                self.audit.record_conn(
                    connection,
                    entity_id,
                    actor,
                    action,
                    entity["status"],
                    updated["status"],
                    {"patch": self._public_patch(patch)},
                )
                self._cascade_dependency_invalidation(
                    connection, actor, entity["kind"], action, updated
                )
            return updated

    @staticmethod
    def _public_patch(patch):
        # Internal bookkeeping keys are not useful in the action audit trail.
        return {
            key: value
            for key, value in patch.items()
            if key
            not in ("released_by", "released_at", "calibration_id",
                    "calibration_version", "instrument_version",
                    "method_version", "due_at")
        }

    def _register_release(self, connection, actor, entity, expected_version, merged):
        """Validate-then-release against CURRENT calibration/method and
        register the exact versions used. The raw measurement stays on the
        result entity and every release is appended to release_records."""
        released_at = utcnow()
        merged.pop("released_at", None)
        record = {
            "result_id": entity["id"],
            "released_at": released_at,
            "released_by": actor.user_id,
            "instrument_id": merged.get("instrument_id"),
            "instrument_version": merged.get("instrument_version"),
            "calibration_id": merged.get("calibration_id"),
            "calibration_version": merged.get("calibration_version"),
            "due_at": merged.get("due_at"),
            "method_id": merged.get("method_id"),
            "method_version": merged.get("method_version"),
            "value": merged.get("value"),
            "unit": merged.get("unit"),
            "active": True,
            "detail": {
                "sample_id": merged.get("sample_id"),
            },
        }
        release_id = self.repository.insert_release_record_conn(connection, record)
        merged["last_release_id"] = release_id
        merged["pending_review_reason"] = None
        updated = self.repository.update_entity_conn(
            connection, entity["id"], expected_version, "released", merged
        )
        self.audit.record_conn(
            connection,
            entity["id"],
            actor,
            "release",
            entity["status"],
            "released",
            {
                "release_id": release_id,
                "instrument_id": record["instrument_id"],
                "instrument_version": record["instrument_version"],
                "calibration_id": record["calibration_id"],
                "calibration_version": record["calibration_version"],
                "due_at": record["due_at"],
                "method_id": record["method_id"],
                "method_version": record["method_version"],
            },
        )
        return updated

    # -- dependency cascade --------------------------------------------

    def _cascade_dependency_invalidation(self, connection, actor, kind, action, trigger):
        """After a calibration/method/instrument change, every released
        result that still relies on it immediately falls back to
        pending_review in the same transaction."""
        repo = self.repository
        if kind == "instrument" and action == "quarantine":
            releases = repo.find_active_releases_conn(
                connection, instrument_id=trigger["id"]
            )
            self._invalidate_releases(
                connection, actor, releases,
                reason="instrument_inactive", source="instrument_quarantine",
                trigger={"instrument_id": trigger["id"], "reason": trigger["data"].get("reason")},
            )
        elif kind == "instrument" and action == "calibrate":
            releases = repo.find_active_releases_conn(
                connection, instrument_id=trigger["id"]
            )
            self._invalidate_releases(
                connection, actor, releases,
                reason="calibration_superseded", source="instrument_calibrated",
                trigger={"instrument_id": trigger["id"], "due_at": trigger["data"].get("due_at")},
            )
        elif kind == "calibration" and action == "revoke":
            releases = repo.find_active_releases_conn(
                connection, calibration_id=trigger["id"]
            )
            self._invalidate_releases(
                connection, actor, releases,
                reason="calibration_revoked", source="calibration_revoked",
                trigger={"calibration_id": trigger["id"], "reason": trigger["data"].get("reason")},
            )
        elif kind == "calibration" and action == "approve":
            # A newly approved calibration supersedes earlier releases'
            # calibration version for the same instrument.
            instrument_id = trigger["data"].get("instrument_id")
            releases = repo.find_active_releases_conn(
                connection, instrument_id=instrument_id,
                exclude_calibration_id=trigger["id"],
            )
            self._invalidate_releases(
                connection, actor, releases,
                reason="calibration_superseded", source="calibration_approved",
                trigger={
                    "calibration_id": trigger["id"],
                    "instrument_id": instrument_id,
                    "due_at": trigger["data"].get("due_at"),
                },
            )
        elif kind == "method" and action == "revoke_method":
            releases = repo.find_active_releases_conn(
                connection, method_id=trigger["id"]
            )
            self._invalidate_releases(
                connection, actor, releases,
                reason="method_revoked", source="method_revoked",
                trigger={"method_id": trigger["id"], "reason": trigger["data"].get("reason")},
            )

    def _invalidate_releases(self, connection, actor, releases, reason, source, trigger=None):
        repo = self.repository
        now = utcnow()
        for record in releases:
            result = repo.get_entity_conn(connection, record["result_id"])
            if not result or result["status"] != "released":
                # Defensive: release record says active but result moved on.
                repo.mark_release_inactive_conn(connection, record["result_id"], reason, now)
                continue
            data = dict(result["data"])
            data["pending_review_reason"] = reason
            data["pending_review_at"] = now
            repo.mark_release_inactive_conn(connection, result["id"], reason, now)
            repo.update_entity_conn(connection, result["id"], None, "pending_review", data)
            self.audit.record_conn(
                connection,
                result["id"],
                actor,
                "dependency_invalidated",
                "released",
                "pending_review",
                {
                    "reason": reason,
                    "reason_label": INVALID_REASON_LABELS.get(reason, reason),
                    "source": source,
                    "trigger": trigger or {},
                    "release_id": record["id"],
                    "calibration_id": record["calibration_id"],
                    "method_id": record["method_id"],
                },
            )

    # -- recomputation & legacy backfill -------------------------------

    def recalculate_dependencies(self, actor=None, as_of=None):
        """Re-evaluate every active release against current instrument /
        calibration / method state. Covers anything event-driven cascade
        cannot see directly, in particular calibrations whose due date has
        passed."""
        actor = actor or SYSTEM_ACTOR
        as_of = as_of or self.rules.as_of
        checked = 0
        invalidated = 0
        with self.repository.transaction() as connection:
            releases = self.repository.list_active_release_records_conn(connection)
            for record in releases:
                checked += 1
                instrument = self.repository.get_entity_conn(
                    connection, record["instrument_id"]
                ) if record["instrument_id"] else None
                calibration = self.repository.get_entity_conn(
                    connection, record["calibration_id"]
                ) if record["calibration_id"] else None
                method = self.repository.get_entity_conn(
                    connection, record["method_id"]
                ) if record["method_id"] else None
                ok, reason, details = evaluate_release_binding(
                    instrument, calibration, method, as_of
                )
                if ok:
                    continue
                self._invalidate_releases(
                    connection,
                    actor,
                    [record],
                    reason=reason,
                    source="dependency_recalculated",
                    trigger={"as_of": as_of, "checks": details.get("checks", [])},
                )
                invalidated += 1
        return {"checked": checked, "invalidated": invalidated, "as_of": as_of}

    def backfill_legacy_releases(self, actor=None, as_of=None, batch_size=100):
        """Backfill one batch of results released before dependency
        tracking existed, using the instrument's CURRENT state. Each item is
        its own short transaction, so the backfill never blocks or disturbs
        new releases; fresh releases already carry a release record and are
        never picked up here."""
        actor = actor or SYSTEM_ACTOR
        as_of = as_of or self.rules.as_of
        processed = 0
        backfilled = 0
        invalidated = 0

        # Read one batch in a short transaction; each backfilled item is
        # updated in its own transaction below, so writers are not blocked.
        with self.repository.transaction() as connection:
            legacy = self.repository.list_legacy_released_results_conn(
                connection, int(batch_size)
            )
            total_remaining = connection.execute(
                "SELECT COUNT(*) AS c FROM entities e WHERE e.kind = 'result' "
                "AND e.status = 'released' "
                "AND NOT EXISTS (SELECT 1 FROM release_records r WHERE r.result_id = e.id)"
            ).fetchone()["c"]

        for result in legacy:
            outcome = self._backfill_one(actor, result, as_of)
            processed += 1
            if outcome == "backfilled":
                backfilled += 1
            else:
                invalidated += 1
        return {
            "processed": processed,
            "backfilled": backfilled,
            "invalidated": invalidated,
            "remaining": max(0, int(total_remaining) - processed),
            "as_of": as_of,
        }

    def _backfill_one(self, actor, result, as_of):
        repo = self.repository
        data = result["data"]
        with repo.transaction() as connection:
            instrument = repo.get_entity_conn(
                connection, data.get("instrument_id")
            ) if data.get("instrument_id") else None
            method = repo.get_entity_conn(
                connection, data.get("method_id")
            ) if data.get("method_id") else None
            calibration = None
            if instrument:
                calibrations = repo.find_entities_conn(
                    connection, "calibration", "instrument_id", instrument["id"]
                )
                calibration = resolve_current_calibration(calibrations, as_of)

            ok, reason, details = evaluate_release_binding(
                instrument, calibration, method, as_of
            )
            released_at = result["updated_at"] or result["created_at"]
            record = {
                "result_id": result["id"],
                "released_at": released_at,
                "released_by": data.get("released_by") or result["created_by"],
                "instrument_id": data.get("instrument_id"),
                "instrument_version": instrument["version"] if instrument else None,
                "calibration_id": calibration["id"] if calibration else None,
                "calibration_version": calibration["version"] if calibration else None,
                "due_at": (calibration["data"].get("due_at") if calibration else None),
                "method_id": data.get("method_id"),
                "method_version": method["version"] if method else None,
                "value": data.get("value"),
                "unit": data.get("unit"),
                "active": bool(ok),
                "invalidated_at": None if ok else utcnow(),
                "invalid_reason": None if ok else reason,
                "detail": {"source": "legacy_backfill", "checks": details.get("checks", [])},
            }
            repo.insert_release_record_conn(connection, record)
            if ok:
                self.audit.record_conn(
                    connection,
                    result["id"],
                    actor,
                    "release_binding_backfilled",
                    "released",
                    "released",
                    {
                        "instrument_id": record["instrument_id"],
                        "calibration_id": record["calibration_id"],
                        "method_id": record["method_id"],
                        "as_of": as_of,
                    },
                )
                return "backfilled"

            new_data = dict(data)
            new_data["pending_review_reason"] = reason
            new_data["pending_review_at"] = utcnow()
            repo.update_entity_conn(connection, result["id"], None, "pending_review", new_data)
            self.audit.record_conn(
                connection,
                result["id"],
                actor,
                "dependency_invalidated",
                "released",
                "pending_review",
                {
                    "reason": reason,
                    "reason_label": INVALID_REASON_LABELS.get(reason, reason),
                    "source": "legacy_backfill",
                    "as_of": as_of,
                    "checks": details.get("checks", []),
                },
            )
            return "invalidated"

    # -- maintenance ----------------------------------------------------

    def run_maintenance(self, actor=None, as_of=None, batch_size=100):
        """One maintenance pass: drain a batch of legacy backfill, then
        recompute all active releases (catches expired calibrations)."""
        backfill = self.backfill_legacy_releases(actor=actor, as_of=as_of, batch_size=batch_size)
        recalculation = self.recalculate_dependencies(actor=actor, as_of=as_of)
        return {"backfill": backfill, "recalculation": recalculation}

    @staticmethod
    def ensure_admin(actor):
        if actor.role != "admin":
            raise PermissionDenied("role %s is not allowed to run maintenance" % actor.role)
