from datetime import datetime, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def _validate_calibration(actor, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("instrument does not exist")


def _validate_perform(actor, entity, data, lookup):
    if data.get("result") not in ("passed", "failed"):
        raise ValidationError("calibration result must be passed or failed")
    if data.get("result") == "passed" and not data.get("due_at"):
        raise ValidationError("passed calibration requires due_at")


def calibration_current(due_at, as_of):
    return str(due_at) >= str(as_of)


def _current_calibration(lookup, instrument_id, as_of):
    calibrations = lookup("calibration", "instrument_id", instrument_id) or []
    current = [
        cal
        for cal in calibrations
        if cal["status"] == "approved"
        and calibration_current(cal["data"].get("due_at", ""), as_of)
    ]
    return current[-1] if current else None


def instrument_calibration_current(lookup, instrument, as_of):
    calibrations = lookup("calibration", "instrument_id", instrument["id"]) or []
    current = [
        cal
        for cal in calibrations
        if cal["status"] == "approved"
        and calibration_current(cal["data"].get("due_at", ""), as_of)
    ]
    if calibrations:
        return bool(current), (current[-1] if current else None)
    return calibration_current(instrument["data"].get("due_at", ""), as_of), None


def _validate_result_release(actor, entity, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("instrument does not exist")
    if instrument["status"] != "active":
        raise ValidationError("result requires an active instrument")
    as_of = _today()
    cal_current, calibration = instrument_calibration_current(lookup, instrument, as_of)
    if not cal_current:
        raise ValidationError("instrument calibration is not current")
    method = _find_one(lookup, "method", "id", data.get("method_id"))
    if not method or method["status"] != "validated":
        raise ValidationError("result requires a validated method")
    if instrument["id"] not in method["data"].get("instrument_ids", []):
        raise ValidationError("method is not validated for this instrument")
    expected = data.get("expected_versions") or {}
    for key, dependency in (
        ("instrument", instrument),
        ("calibration", calibration),
        ("method", method),
    ):
        expected_version = expected.get(key)
        if expected_version is not None and int(expected_version) != int(dependency["version"]):
            raise ConflictError(
                "dependency %s version conflict: expected %s, found %s"
                % (key, expected_version, dependency["version"])
            )
    record = {
        "released_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "released_by": actor.user_id,
        "instrument_id": instrument["id"],
        "instrument_version": instrument["version"],
        "instrument_status": instrument["status"],
        "calibration_id": calibration["id"] if calibration else None,
        "calibration_version": calibration["version"] if calibration else None,
        "calibration_due_at": (
            calibration["data"].get("due_at")
            if calibration
            else instrument["data"].get("due_at", "")
        ),
        "method_id": method["id"],
        "method_version": method["version"],
        "value": data.get("value"),
        "unit": data.get("unit"),
    }
    releases = list(entity["data"].get("releases", []))
    releases.append(record)
    return {
        "instrument_id": instrument["id"],
        "calibration_id": record["calibration_id"],
        "method_id": method["id"],
        "value": data.get("value"),
        "unit": data.get("unit"),
        "releases": releases,
    }


CUSTOM_CREATE = {'calibration': _validate_calibration}
CUSTOM_TRANSITIONS = {('calibration', 'perform'): _validate_perform, ('result', 'release'): _validate_result_release}


class RuleEngine:
    ALIASES = {'instruments': 'instrument', 'calibrations': 'calibration', 'methods': 'method', 'results': 'result'}
    INITIAL_STATUS = {'instrument': 'active', 'calibration': 'requested', 'method': 'draft', 'result': 'pending'}
    TRANSITIONS = {'instrument': {'send_calibration': (('active',), 'calibrating'), 'calibrate': (('calibrating',), 'active'), 'quarantine': (('active',), 'quarantined'), 'restore': (('quarantined',), 'active')}, 'calibration': {'perform': (('requested', 'failed'), 'passed'), 'approve': (('passed',), 'approved'), 'reject': (('failed',), 'rejected'), 'revoke': (('approved',), 'revoked')}, 'method': {'validate_method': (('draft',), 'validated'), 'revoke_method': (('validated',), 'revoked')}, 'result': {'release': (('pending', 'review'), 'released'), 'block': (('pending',), 'blocked'), 'reanalyze': (('blocked',), 'pending')}}
    CREATE_REQUIRED = {'instrument': ('name', 'serial'), 'calibration': ('instrument_id', 'requested_at'), 'method': ('name', 'version'), 'result': ('sample_id', 'measurement')}
    ACTION_REQUIRED = {('instrument', 'calibrate'): ('due_at', 'passed'), ('instrument', 'quarantine'): ('reason',), ('calibration', 'perform'): ('result', 'performed_at', 'uncertainty'), ('calibration', 'approve'): ('authorized_by',), ('calibration', 'reject'): ('reason',), ('calibration', 'revoke'): ('reason',), ('method', 'validate_method'): ('parameters', 'instrument_ids'), ('method', 'revoke_method'): ('reason',), ('result', 'release'): ('instrument_id', 'method_id', 'value', 'unit'), ('result', 'block'): ('reason',), ('result', 'reanalyze'): ('reason',)}
    CREATE_ROLES = {'instrument': ('admin', 'technician'), 'calibration': ('admin', 'metrology'), 'method': ('admin', 'authorizer'), 'result': ('admin', 'analyst')}
    ROLE_ACTIONS = {'send_calibration': ('admin', 'technician'), 'calibrate': ('admin', 'metrology'), 'quarantine': ('admin', 'metrology'), 'restore': ('admin', 'metrology'), 'perform': ('admin', 'metrology'), 'approve': ('admin', 'authorizer'), 'reject': ('admin', 'authorizer'), 'revoke': ('admin', 'metrology'), 'validate_method': ('admin', 'authorizer'), 'revoke_method': ('admin', 'authorizer'), 'release': ('admin', 'analyst'), 'block': ('admin', 'analyst'), 'reanalyze': ('admin', 'analyst')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
