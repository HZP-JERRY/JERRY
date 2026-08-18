import unittest

from app import choose_removal_targets, is_numeric_platform_sku, normalize_blank, should_remove_product_row


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


if __name__ == "__main__":
    unittest.main()
