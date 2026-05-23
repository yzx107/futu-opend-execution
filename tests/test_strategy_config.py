from __future__ import annotations

import unittest

from futu_opend_execution.strategy_config import CostReducerRuntimeParams, validate_cost_reducer_params


class StrategyConfigTests(unittest.TestCase):
    def test_auto_cost_reducer_disabled_by_default(self) -> None:
        params = CostReducerRuntimeParams()
        self.assertFalse(params.enable_auto_cost_reducer)
        validate_cost_reducer_params(params)

    def test_auto_cost_reducer_cannot_be_enabled_by_default_config(self) -> None:
        with self.assertRaisesRegex(ValueError, "experimental"):
            validate_cost_reducer_params(CostReducerRuntimeParams(enable_auto_cost_reducer=True))


if __name__ == "__main__":
    unittest.main()
