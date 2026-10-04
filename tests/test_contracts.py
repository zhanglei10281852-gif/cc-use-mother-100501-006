"""多式联运异常重排基础契约测试。"""

import unittest

from freight_replanning import ShipmentPlan, unique_by_identity


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.values = {'plan_code': 'plan-code-001', 'shipment_code': 'shipment-code-001', 'route_revision': 'route-revision-001', 'state': 'state-001'}

    def test_fingerprint_is_stable(self) -> None:
        left = ShipmentPlan(**self.values)
        right = ShipmentPlan(**dict(reversed(list(self.values.items()))))
        self.assertEqual(left.fingerprint(), right.fingerprint())

    def test_evolve_keeps_original(self) -> None:
        original = ShipmentPlan(**self.values)
        change_key = next(key for key, value in self.values.items() if isinstance(value, str))
        changed = original.evolve(**{change_key: "revised-value"})
        self.assertNotEqual(original.fingerprint(), changed.fingerprint())
        self.assertEqual(getattr(original, change_key), self.values[change_key])

    def test_conflicting_identity_is_rejected(self) -> None:
        first = ShipmentPlan(**self.values)
        changed_values = dict(self.values)
        change_key = next(key for key in self.values if key != "plan_code")
        changed_values[change_key] = 2 if isinstance(changed_values[change_key], int) else "conflict"
        second = ShipmentPlan(**changed_values)
        with self.assertRaises(ValueError):
            unique_by_identity([first, second])


if __name__ == "__main__":
    unittest.main()
