import random
import types
import unittest
from unittest.mock import patch
import validate_holdout as runner


class OrderTests(unittest.TestCase):
    def test_default_preserves_exact_order(self):
        items = ['z', 'a', 'b']
        runner.order_items(items)
        self.assertEqual(items, ['z', 'a', 'b'])

    def test_reverse_is_exact_and_shuffle_is_seeded_permutation(self):
        original = list(range(30))
        reversed_items = original.copy()
        runner.order_items(reversed_items, 'reversed')
        self.assertEqual(reversed_items, original[::-1])
        first, second, different = original.copy(), original.copy(), original.copy()
        runner.order_items(first, 'shuffle', 42)
        runner.order_items(second, 'shuffle', 42)
        runner.order_items(different, 'shuffle', 43)
        self.assertEqual(first, second)
        self.assertNotEqual(first, different)
        self.assertEqual(sorted(first), original)

    def test_shuffle_does_not_modify_global_random_state(self):
        before = random.getstate()
        runner.order_items(list(range(30)), 'shuffle', 42)
        self.assertEqual(random.getstate(), before)

    def test_collection_filters_before_order_and_records_exact_receipt(self):
        items = [types.SimpleNamespace(nodeid=node) for node in ['a', 'skip', 'c', 'b']]
        with patch.object(runner, '_requested', {'a', 'b', 'c'}), patch.object(runner, '_collected', set()), patch.object(runner, '_collected_order', []), patch.dict(runner.os.environ, {'HOLDOUT_ORDER': 'reversed', 'HOLDOUT_ORDER_SEED': '0'}):
            runner.pytest_collection_modifyitems(None, None, items)
            self.assertEqual([item.nodeid for item in items], ['b', 'c', 'a'])
            self.assertEqual(runner._collected_order, ['b', 'c', 'a'])
            self.assertEqual(runner._collected, {'a', 'b', 'c'})

    def test_unknown_order_rejected(self):
        with self.assertRaises(ValueError):
            runner.order_items([], 'unknown')


if __name__ == '__main__':
    unittest.main()
