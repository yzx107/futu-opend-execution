from __future__ import annotations

import unittest

from futu_opend_execution.strategy_config import CostReducerRuntimeParams, cost_reducer_preset


class StrategyConfigTests(unittest.TestCase):
    def test_auto_cost_reducer_is_disabled_by_default(self) -> None:
        self.assertFalse(CostReducerRuntimeParams().enable_auto_cost_reducer)
        self.assertFalse(cost_reducer_preset("manual_cost_reducer_safe").enable_auto_cost_reducer)


if __name__ == "__main__":
    unittest.main()
