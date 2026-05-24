from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from futu_opend_execution.contracts import ContractSpec, ContractSpecError, load_contract_specs, write_contract_specs


class FuturesContractSpecTests(unittest.TestCase):
    def test_contract_spec_validates_tick_and_margin(self) -> None:
        spec = ContractSpec(
            symbol="hk.hsi2606",
            exchange="hkfe",
            asset_class="INDEX_FUTURE",
            contract_multiplier="50",
            tick_size="1",
            margin_rate="0.08",
        )

        self.assertEqual(spec.symbol, "HK.HSI2606")
        self.assertEqual(str(spec.tick_value), "50")
        self.assertEqual(str(spec.notional(price="20000", quantity=2)), "2000000")
        self.assertEqual(str(spec.initial_margin(price="20000", quantity=2)), "160000.00")

        with self.assertRaisesRegex(ContractSpecError, "tick_size"):
            spec.validate_price("20000.5")

    def test_load_and_write_contract_specs(self) -> None:
        spec = ContractSpec(
            symbol="HK.MHI2606",
            exchange="HKFE",
            asset_class="INDEX_FUTURE",
            contract_multiplier="10",
            tick_size="1",
            commission_per_contract="8",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "contracts.json"
            write_contract_specs({spec.symbol: spec}, path)
            loaded = load_contract_specs(path)

        self.assertEqual(tuple(loaded), ("HK.MHI2606",))
        self.assertEqual(loaded["HK.MHI2606"].commission_per_contract, spec.commission_per_contract)

    def test_invalid_contract_is_rejected(self) -> None:
        with self.assertRaisesRegex(ContractSpecError, "contract_multiplier"):
            ContractSpec(
                symbol="HK.HSI2606",
                exchange="HKFE",
                asset_class="INDEX_FUTURE",
                contract_multiplier="0",
                tick_size="1",
            )


if __name__ == "__main__":
    unittest.main()
