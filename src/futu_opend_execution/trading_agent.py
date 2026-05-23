"""Compatibility exports for the OpenD Trading Agent runtime."""

from futu_opend_execution.agent.runtime import (
    TradingAgentConfig,
    build_inventory_for_existing_position,
    default_strategy,
    intent_to_jsonable,
    real_order_intent_from_signal,
    run_monitor,
    run_paper,
    run_replay,
    submit_auto_real_intent,
)

OpendTradingAgentConfig = TradingAgentConfig

__all__ = [
    "OpendTradingAgentConfig",
    "TradingAgentConfig",
    "build_inventory_for_existing_position",
    "default_strategy",
    "intent_to_jsonable",
    "real_order_intent_from_signal",
    "run_monitor",
    "run_paper",
    "run_replay",
    "submit_auto_real_intent",
]
