import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def transaction(self):
        """Serialize writers: BEGIN IMMEDIATE takes the write lock up front,
        so concurrent commits (e.g. quarantine vs release) have a global
        first-committer-wins order."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS release_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    result_id TEXT NOT NULL,
                    released_at TEXT NOT NULL,
                    released_by TEXT NOT NULL,
                    instrument_id TEXT,
                    instrument_version INTEGER,
                    calibration_id TEXT,
                    calibration_version INTEGER,
                    due_at TEXT,
                    method_id TEXT,
                    method_version INTEGER,
                    value TEXT,
                    unit TEXT,
                    active INTEGER NOT NULL,
                    invalidated_at TEXT,
                    invalid_reason TEXT,
                    detail TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_release_result
                    ON release_records(result_id, id);
                CREATE INDEX IF NOT EXISTS idx_release_active
                    ON release_records(active);
                CREATE INDEX IF NOT EXISTS idx_release_instrument
                    ON release_records(active, instrument_id);
                CREATE INDEX IF NOT EXISTS idx_release_calibration
                    ON release_records(active, calibration_id);
                CREATE INDEX IF NOT EXISTS idx_release_method
                    ON release_records(active, method_id);
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _release_from_row(row):
        return {
            "id": row["id"],
            "result_id": row["result_id"],
            "released_at": row["released_at"],
            "released_by": row["released_by"],
            "instrument_id": row["instrument_id"],
            "instrument_version": row["instrument_version"],
            "calibration_id": row["calibration_id"],
            "calibration_version": row["calibration_version"],
            "due_at": row["due_at"],
            "method_id": row["method_id"],
            "method_version": row["method_version"],
            "value": row["value"],
            "unit": row["unit"],
            "active": bool(row["active"]),
            "invalidated_at": row["invalidated_at"],
            "invalid_reason": row["invalid_reason"],
            "detail": json.loads(row["detail"] or "{}"),
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity_conn(self, connection, entity_id):
        row = connection.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return self._entity_from_row(row) if row else None

    def get_entity(self, entity_id):
        connection = self._connect()
        try:
            return self.get_entity_conn(connection, entity_id)
        finally:
            connection.close()

    def _list_entities_conn(self, connection, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = connection.execute(
            "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
        ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def list_entities(self, kind=None, status=None):
        connection = self._connect()
        try:
            return self._list_entities_conn(connection, kind=kind, status=status)
        finally:
            connection.close()

    def find_entities_conn(self, connection, kind, field, value):
        return [
            entity
            for entity in self._list_entities_conn(connection, kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def find_entities(self, kind, field, value):
        connection = self._connect()
        try:
            return self.find_entities_conn(connection, kind, field, value)
        finally:
            connection.close()

    def update_entity_conn(self, connection, entity_id, expected_version, status, data):
        row = connection.execute(
            "SELECT version FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("entity not found: " + entity_id)
        current_version = int(row["version"])
        if expected_version is not None and current_version != int(expected_version):
            raise ConflictError(
                "version conflict: expected %s, found %s"
                % (expected_version, current_version)
            )
        connection.execute(
            "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
            "WHERE id = ? AND version = ?",
            (status, json.dumps(data, ensure_ascii=False, sort_keys=True), utcnow(),
             entity_id, current_version),
        )
        return self.get_entity_conn(connection, entity_id)

    def update_entity(self, entity_id, expected_version, status, data):
        with self.transaction() as connection:
            return self.update_entity_conn(
                connection, entity_id, expected_version, status, data
            )

    def insert_release_record_conn(self, connection, record):
        now = utcnow()
        cursor = connection.execute(
            "INSERT INTO release_records(result_id, released_at, released_by, "
            "instrument_id, instrument_version, calibration_id, calibration_version, "
            "due_at, method_id, method_version, value, unit, active, "
            "invalidated_at, invalid_reason, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record["result_id"],
                record.get("released_at") or now,
                record.get("released_by", "unknown"),
                record.get("instrument_id"),
                record.get("instrument_version"),
                record.get("calibration_id"),
                record.get("calibration_version"),
                record.get("due_at"),
                record.get("method_id"),
                record.get("method_version"),
                None if record.get("value") is None else str(record.get("value")),
                record.get("unit"),
                1 if record.get("active", True) else 0,
                record.get("invalidated_at"),
                record.get("invalid_reason"),
                json.dumps(record.get("detail", {}), ensure_ascii=False, sort_keys=True),
            ),
        )
        return cursor.lastrowid

    def mark_release_inactive_conn(self, connection, result_id, reason, at=None):
        connection.execute(
            "UPDATE release_records SET active = 0, invalidated_at = ?, invalid_reason = ? "
            "WHERE result_id = ? AND active = 1",
            (at or utcnow(), reason, result_id),
        )

    def list_release_records_conn(self, connection, result_id=None):
        if result_id:
            rows = connection.execute(
                "SELECT * FROM release_records WHERE result_id = ? ORDER BY id",
                (result_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM release_records ORDER BY id"
            ).fetchall()
        return [self._release_from_row(row) for row in rows]

    def list_release_records(self, result_id):
        connection = self._connect()
        try:
            return self.list_release_records_conn(connection, result_id)
        finally:
            connection.close()

    def list_active_release_records_conn(self, connection):
        rows = connection.execute(
            "SELECT * FROM release_records WHERE active = 1 ORDER BY id"
        ).fetchall()
        return [self._release_from_row(row) for row in rows]

    def find_active_releases_conn(self, connection, instrument_id=None,
                                  calibration_id=None, method_id=None,
                                  exclude_calibration_id=None):
        clauses = ["active = 1"]
        params = []
        if instrument_id is not None:
            clauses.append("instrument_id = ?")
            params.append(instrument_id)
        if calibration_id is not None:
            clauses.append("calibration_id = ?")
            params.append(calibration_id)
        if method_id is not None:
            clauses.append("method_id = ?")
            params.append(method_id)
        if exclude_calibration_id is not None:
            clauses.append("calibration_id != ?")
            params.append(exclude_calibration_id)
        rows = connection.execute(
            "SELECT * FROM release_records WHERE " + " AND ".join(clauses) + " ORDER BY id",
            params,
        ).fetchall()
        return [self._release_from_row(row) for row in rows]

    def list_legacy_released_results_conn(self, connection, limit):
        """Released results that existed before dependency tracking existed:
        they have no release_records row at all."""
        rows = connection.execute(
            "SELECT * FROM entities e WHERE e.kind = 'result' AND e.status = 'released' "
            "AND NOT EXISTS (SELECT 1 FROM release_records r WHERE r.result_id = e.id) "
            "ORDER BY e.created_at, e.id LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def append_audit_conn(self, connection, entity_id, actor_id, actor_role, action,
                          from_status, to_status, detail):
        connection.execute(
            "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity_id,
                actor_id,
                actor_role,
                action,
                from_status,
                to_status,
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
                utcnow(),
            ),
        )

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self.transaction() as connection:
            self.append_audit_conn(
                connection, entity_id, actor_id, actor_role, action,
                from_status, to_status, detail,
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
