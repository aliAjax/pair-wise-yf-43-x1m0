import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _resolve(value, created):
    if isinstance(value, str):
        for key, item in created.items():
            value = value.replace("{" + key + "}", str(item))
        return value
    if isinstance(value, list):
        return [_resolve(item, created) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, created) for key, item in value.items()}
    return value


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_workflow(self):
        created = {}
        steps = [{'op': 'create', 'as': 'instrument', 'kind': 'instrument', 'data': {'name': 'Analyzer', 'serial': 'A-1'}}, {'op': 'transition', 'target': 'instrument', 'action': 'send_calibration', 'data': {}, 'expect': 'calibrating'}, {'op': 'transition', 'target': 'instrument', 'action': 'calibrate', 'data': {'due_at': '2099-01-01', 'passed': True}, 'expect': 'active'}, {'op': 'create', 'as': 'calibration', 'kind': 'calibration', 'data': {'instrument_id': '{instrument}', 'requested_at': '2026-01-01'}}, {'op': 'transition', 'target': 'calibration', 'action': 'perform', 'data': {'result': 'passed', 'performed_at': '2026-01-02', 'uncertainty': 0.01, 'due_at': '2099-01-01'}, 'expect': 'passed'}, {'op': 'transition', 'target': 'calibration', 'action': 'approve', 'data': {'authorized_by': 'QA-1'}, 'expect': 'approved'}, {'op': 'create', 'as': 'method', 'kind': 'method', 'data': {'name': 'Assay-A', 'version': 'v1'}}, {'op': 'transition', 'target': 'method', 'action': 'validate_method', 'data': {'parameters': {'range': [0, 10]}, 'instrument_ids': ['{instrument}']}, 'expect': 'validated'}, {'op': 'create', 'as': 'result', 'kind': 'result', 'data': {'sample_id': 'S-1', 'measurement': 'initial'}}, {'op': 'transition', 'target': 'result', 'action': 'release', 'data': {'instrument_id': '{instrument}', 'method_id': '{method}', 'value': 4.2, 'unit': 'mg/L'}, 'expect': 'released'}]
        for step in steps:
            if step["op"] == "create":
                entity = self.service.create(
                    self.actor,
                    step["kind"],
                    _resolve(step.get("data", {}), created),
                    step.get("idempotency_key"),
                )
                created[step["as"]] = entity["id"]
            else:
                entity = self.service.transition(
                    self.actor,
                    created[step["target"]],
                    step["action"],
                    _resolve(step.get("data", {}), created),
                    step.get("expected_version"),
                )
            if "expect" in step:
                self.assertEqual(entity["status"], step["expect"])


if __name__ == "__main__":
    unittest.main()
