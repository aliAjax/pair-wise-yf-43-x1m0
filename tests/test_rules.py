import unittest

from src.rules import calibration_current
from src.domain import Actor, PermissionDenied, ValidationError
from src.rules import RuleEngine


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RuleEngine()
        self.admin = Actor("rule-tester", "admin")

    def test_rule_calculation_or_validation(self):
        self.assertTrue(calibration_current("2099-01-01", "2026-09-24"))
        self.assertFalse(calibration_current("2025-01-01", "2026-09-24"))
        with self.assertRaises(ValidationError):
            self.rules.validate_transition(self.admin, {"kind": "calibration", "status": "requested", "data": {}}, "perform", {"result": "unknown", "performed_at": "2026-01-01", "uncertainty": 0.1})


if __name__ == "__main__":
    unittest.main()
