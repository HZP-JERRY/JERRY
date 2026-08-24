import unittest
from unittest.mock import patch

from app import (
    LingxingAutomation,
    choose_removal_targets,
    is_numeric_platform_sku,
    normalize_blank,
    should_remove_product_row,
)


def row(platform, sku="-", name="-", inventory="-", row_id="r1"):
    return {
        "rowId": row_id,
        "platformSku": platform,
        "sku": sku,
        "productName": name,
        "availableInventory": inventory,
        "quantity": "1",
    }


class RuleTests(unittest.TestCase):
    def test_numeric_ascii_only(self):
        self.assertTrue(is_numeric_platform_sku("123456"))
        self.assertFalse(is_numeric_platform_sku("12A456"))
        self.assertFalse(is_numeric_platform_sku("１２３"))
        self.assertFalse(is_numeric_platform_sku(""))

    def test_blank_values(self):
        self.assertTrue(normalize_blank(""))
        self.assertTrue(normalize_blank(" - "))
        self.assertFalse(normalize_blank("0"))

    def test_all_five_conditions_are_required(self):
        self.assertTrue(should_remove_product_row(row("123")))
        self.assertFalse(should_remove_product_row(row("A123")))
        self.assertFalse(should_remove_product_row(row("123", sku="SKU-1")))
        self.assertFalse(should_remove_product_row(row("123", name="产品")))
        self.assertFalse(should_remove_product_row(row("123", inventory="0")))

    def test_keeps_at_least_one_product_row(self):
        self.assertEqual(choose_removal_targets([row("123")]), [])
        targets = choose_removal_targets([row("123", row_id="a"), row("456", row_id="b")])
        self.assertEqual(len(targets), 1)

    def test_targets_bottom_to_top(self):
        rows = [row("123", row_id="a"), row("SKU", sku="SKU", name="P", inventory="9", row_id="b"), row("456", row_id="c")]
        self.assertEqual([x["rowId"] for x in choose_removal_targets(rows)], ["c", "a"])


class ListSnapshotTests(unittest.TestCase):
    @staticmethod
    def state(candidates, scanned=10, order_ids=None, ready=True):
        return {
            "ready": ready,
            "scanned": scanned,
            "total": scanned,
            "tabTotal": scanned,
            "pageSize": 100,
            "allOrderIds": order_ids or [f"SO{i}" for i in range(scanned)],
            "candidates": candidates,
        }

    def automation_with_states(self, states):
        automation = LingxingAutomation(lambda _: None, lambda _x, _y: None)
        iterator = iter(states)
        last = states[-1]
        automation._read_list_state = lambda: next(iterator, last)  # type: ignore[method-assign]
        return automation

    @patch("app.time.sleep", return_value=None)
    def test_unrelated_order_churn_does_not_block_candidate_snapshot(self, _sleep):
        candidate = [{"systemOrderId": "SO9", "platformOrderId": "P9", "platformSkuText": "多个(2)"}]
        states = [
            self.state(candidate, scanned=10, order_ids=["old"]),
            self.state(candidate, scanned=11, order_ids=["new"]),
        ]
        result = self.automation_with_states(states)._read_stable_list_state(timeout=0.1)
        self.assertEqual(result["scanned"], 11)

    @patch("app.time.sleep", return_value=None)
    def test_empty_candidate_snapshot_requires_three_complete_reads(self, _sleep):
        states = [
            self.state([], scanned=9),
            self.state([], scanned=10),
            self.state([], scanned=11),
        ]
        result = self.automation_with_states(states)._read_stable_list_state(timeout=0.1)
        self.assertEqual(result["scanned"], 11)

    @patch("app.time.sleep", return_value=None)
    def test_incomplete_read_resets_candidate_confirmation(self, _sleep):
        candidate = [{"systemOrderId": "SO9", "platformOrderId": "P9", "platformSkuText": "多个(2)"}]
        states = [
            self.state(candidate),
            self.state(candidate, ready=False),
            self.state(candidate),
            self.state(candidate),
        ]
        result = self.automation_with_states(states)._read_stable_list_state(timeout=0.1)
        self.assertTrue(result["ready"])


if __name__ == "__main__":
    unittest.main()
