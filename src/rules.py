from datetime import date, datetime

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


# Human-readable explanations for why a released result lost its basis.
# Used by cascading invalidation, expiry sweeps and legacy backfill alike,
# so the audit trail always says which dependency failed first.
INVALID_REASON_LABELS = {
    "instrument_missing": "仪器已不存在",
    "instrument_inactive": "仪器当前不是在用状态",
    "calibration_missing": "放行登记的校准记录不存在",
    "calibration_revoked": "校准已被撤销或不再批准",
    "calibration_superseded": "校准已更新，放行登记的校准版本不再是当前批准版本",
    "calibration_expired": "仪器校准已到期",
    "method_missing": "检测方法不存在",
    "method_revoked": "检测方法已被吊销",
    "method_not_for_instrument": "当前方法版本未覆盖该仪器",
    "legacy_binding_unknown": "历史数据缺少放行登记，无法确认依赖",
}


def _today():
    return date.today().isoformat()


def calibration_current(due_at, as_of):
    return str(due_at) >= str(as_of)


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def resolve_current_calibration(calibrations, as_of):
    """Pick the calibration a release must bind to: the most recently
    approved calibration of the instrument whose due date still covers
    as_of. Returns None when no calibration is currently valid."""
    candidates = []
    for item in calibrations or []:
        if item.get("status") != "approved":
            continue
        due_at = str(item.get("data", {}).get("due_at", ""))
        if due_at and calibration_current(due_at, as_of):
            candidates.append(item)
    candidates.sort(
        key=lambda item: (
            str(item.get("data", {}).get("approved_at")
                or item.get("data", {}).get("performed_at")
                or item.get("updated_at") or ""),
            str(item.get("id", "")),
        )
    )
    return candidates[-1] if candidates else None


def evaluate_release_binding(instrument, calibration, method, as_of):
    """Recompute whether a release snapshot is still valid against the
    CURRENT state of instrument / calibration / method. Returns
    (ok, reason, details). `reason` identifies the first dependency that
    fails so incidents can be attributed to one item."""
    checks = []

    def record(name, ok, reason):
        checks.append({"check": name, "ok": bool(ok), "reason": None if ok else reason})
        return ok

    if instrument is None:
        record("instrument", False, "instrument_missing")
        return False, "instrument_missing", {"checks": checks}
    if instrument.get("status") != "active":
        record("instrument", False, "instrument_inactive")
        return False, "instrument_inactive", {"checks": checks}
    record("instrument", True, None)

    if calibration is None:
        record("calibration", False, "calibration_missing")
        return False, "calibration_missing", {"checks": checks}
    if calibration.get("status") != "approved":
        record("calibration", False, "calibration_revoked")
        return False, "calibration_revoked", {"checks": checks}
    due_at = str(calibration.get("data", {}).get("due_at", ""))
    if not due_at or not calibration_current(due_at, as_of):
        record("calibration", False, "calibration_expired")
        return False, "calibration_expired", {"checks": checks}
    record("calibration", True, None)

    if method is None:
        record("method", False, "method_missing")
        return False, "method_missing", {"checks": checks}
    if method.get("status") != "validated":
        record("method", False, "method_revoked")
        return False, "method_revoked", {"checks": checks}
    instrument_id = instrument.get("id")
    if instrument_id not in method.get("data", {}).get("instrument_ids", []):
        record("method", False, "method_not_for_instrument")
        return False, "method_not_for_instrument", {"checks": checks}
    record("method", True, None)

    return True, None, {"checks": checks}


def _validate_calibration(actor, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("instrument does not exist")


def _validate_perform(actor, entity, data, lookup):
    if data.get("result") not in ("passed", "failed"):
        raise ValidationError("calibration result must be passed or failed")
    if data.get("result") == "passed" and not data.get("due_at"):
        raise ValidationError("passed calibration requires due_at")


def _validate_revoke_calibration(actor, entity, data, lookup):
    # reason is required via ACTION_REQUIRED; nothing else to check.
    return {}


def _validate_result_release(actor, entity, data, lookup, as_of=_today()):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    method = _find_one(lookup, "method", "id", data.get("method_id"))
    if not instrument or instrument["status"] != "active":
        raise ValidationError("result requires an active instrument")
    calibrations = lookup("calibration", "instrument_id", data.get("instrument_id")) or []
    calibration = resolve_current_calibration(calibrations, as_of)
    if not calibration:
        raise ValidationError("instrument calibration is not current")
    if not method or method["status"] != "validated":
        raise ValidationError("result requires a validated method")
    if data.get("instrument_id") not in method["data"].get("instrument_ids", []):
        raise ValidationError("method is not validated for this instrument")
    # Snapshot the exact instrument/calibration/method versions the result
    # is released against; later recomputation is done from this snapshot.
    return {
        "released_by": actor.user_id,
        "released_at": data.get("released_at"),
        "calibration_id": calibration["id"],
        "calibration_version": calibration["version"],
        "instrument_version": instrument["version"],
        "method_version": method["version"],
        "due_at": calibration["data"].get("due_at"),
    }


CUSTOM_CREATE = {'calibration': _validate_calibration}
CUSTOM_TRANSITIONS = {
    ('calibration', 'perform'): _validate_perform,
    ('calibration', 'revoke'): _validate_revoke_calibration,
    ('result', 'release'): _validate_result_release,
}


class RuleEngine:
    ALIASES = {'instruments': 'instrument', 'calibrations': 'calibration', 'methods': 'method', 'results': 'result'}
    INITIAL_STATUS = {'instrument': 'active', 'calibration': 'requested', 'method': 'draft', 'result': 'pending'}
    TRANSITIONS = {
        'instrument': {
            'send_calibration': (('active',), 'calibrating'),
            'calibrate': (('calibrating',), 'active'),
            'quarantine': (('active',), 'quarantined'),
            'restore': (('quarantined',), 'active'),
        },
        'calibration': {
            'perform': (('requested', 'failed'), 'passed'),
            'approve': (('passed',), 'approved'),
            'reject': (('failed',), 'rejected'),
            'revoke': (('approved', 'passed'), 'revoked'),
        },
        'method': {
            'validate_method': (('draft',), 'validated'),
            'revoke_method': (('validated',), 'revoked'),
        },
        'result': {
            # Release is allowed both for fresh results and for results that
            # fell back to pending_review after a dependency changed.
            'release': (('pending', 'pending_review'), 'released'),
            'block': (('pending', 'pending_review'), 'blocked'),
            'reanalyze': (('blocked',), 'pending'),
        },
    }
    CREATE_REQUIRED = {'instrument': ('name', 'serial'), 'calibration': ('instrument_id', 'requested_at'), 'method': ('name', 'version'), 'result': ('sample_id', 'measurement')}
    ACTION_REQUIRED = {
        ('instrument', 'calibrate'): ('due_at', 'passed'),
        ('instrument', 'quarantine'): ('reason',),
        ('calibration', 'perform'): ('result', 'performed_at', 'uncertainty'),
        ('calibration', 'approve'): ('authorized_by',),
        ('calibration', 'reject'): ('reason',),
        ('calibration', 'revoke'): ('reason',),
        ('method', 'validate_method'): ('parameters', 'instrument_ids'),
        ('method', 'revoke_method'): ('reason',),
        ('result', 'release'): ('instrument_id', 'method_id', 'value', 'unit'),
        ('result', 'block'): ('reason',),
        ('result', 'reanalyze'): ('reason',),
    }
    CREATE_ROLES = {'instrument': ('admin', 'technician'), 'calibration': ('admin', 'metrology'), 'method': ('admin', 'authorizer'), 'result': ('admin', 'analyst')}
    ROLE_ACTIONS = {
        'send_calibration': ('admin', 'technician'),
        'calibrate': ('admin', 'metrology'),
        'quarantine': ('admin', 'metrology'),
        'restore': ('admin', 'metrology'),
        'perform': ('admin', 'metrology'),
        'approve': ('admin', 'authorizer'),
        'reject': ('admin', 'authorizer'),
        'revoke': ('admin', 'authorizer', 'metrology'),
        'validate_method': ('admin', 'authorizer'),
        'revoke_method': ('admin', 'authorizer'),
        'release': ('admin', 'analyst'),
        'block': ('admin', 'analyst'),
        'reanalyze': ('admin', 'analyst'),
    }

    def __init__(self, as_of=None):
        # Injectable clock so expiry recomputation is testable; production
        # callers default to today.
        self._as_of = as_of

    @property
    def as_of(self):
        return self._as_of or _today()

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
        if custom:
            if action == "release":
                extra = custom(actor, entity, data, lookup, self.as_of)
            else:
                extra = custom(actor, entity, data, lookup)
        else:
            extra = {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch
