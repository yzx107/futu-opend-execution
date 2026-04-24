"""futu_opend_execution package."""

from futu_opend_execution.config import RuntimeConfig
from futu_opend_execution.execution import (
    BrokerConfigurationError,
    BrokerDependencyError,
    BrokerError,
    BrokerOrderNotFoundError,
    BrokerResponseError,
    FutuOpenDQuoteClient,
    FutuOpenDTradeBroker,
    MarketDataClient,
    MarketDataError,
    MarketDataResponseError,
    MarketDataTimeoutError,
    TradeBroker,
)
from futu_opend_execution.models import (
    BrokerOrderSnapshot,
    BrokerOrderStatus,
    FillLeg,
    GreyMarketBuyPlan,
    GreyMarketBuyRequest,
    GreyMarketExecutionReport,
    GreyMarketSnatchReport,
    MarketSession,
    OrderBookSnapshot,
    OrderSide,
    QuoteLevel,
    SimulatedExecutionResult,
    TimeInForce,
    TradeMode,
)
from futu_opend_execution.risk import (
    ExecutionValidationError,
    InsufficientLiquidityError,
    PriceCapExceededError,
    QuoteValidationError,
    RealTradeDisabledError,
)
from futu_opend_execution.services.greymarket import build_grey_market_buy_plan
from futu_opend_execution.services.orders import (
    execute_grey_market_buy,
    submit_grey_market_buy_plan,
)
from futu_opend_execution.services.snatch import (
    run_grey_market_snatch,
    wait_for_grey_market_open,
)

_GREY_OPEN_EXPORTS = {
    "GreyMarketOpenTrigger",
    "GreyMarketOrderIntent",
    "GreyMarketSignal",
    "GreyMarketTriggerDecision",
    "GreyMarketTriggerRules",
    "JsonlEventLogger",
    "TriggerAction",
    "run_replay",
    "signal_from_record",
}


def __getattr__(name: str):
    if name in _GREY_OPEN_EXPORTS:
        from futu_opend_execution import grey_open

        return getattr(grey_open, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BrokerConfigurationError",
    "BrokerDependencyError",
    "BrokerError",
    "BrokerOrderNotFoundError",
    "BrokerOrderSnapshot",
    "BrokerOrderStatus",
    "BrokerResponseError",
    "ExecutionValidationError",
    "FillLeg",
    "FutuOpenDQuoteClient",
    "FutuOpenDTradeBroker",
    "GreyMarketBuyPlan",
    "GreyMarketBuyRequest",
    "GreyMarketExecutionReport",
    "GreyMarketOpenTrigger",
    "GreyMarketOrderIntent",
    "GreyMarketSnatchReport",
    "GreyMarketSignal",
    "GreyMarketTriggerDecision",
    "GreyMarketTriggerRules",
    "InsufficientLiquidityError",
    "JsonlEventLogger",
    "MarketSession",
    "MarketDataClient",
    "MarketDataError",
    "MarketDataResponseError",
    "MarketDataTimeoutError",
    "OrderBookSnapshot",
    "OrderSide",
    "PriceCapExceededError",
    "QuoteLevel",
    "QuoteValidationError",
    "RealTradeDisabledError",
    "RuntimeConfig",
    "SimulatedExecutionResult",
    "TradeBroker",
    "TriggerAction",
    "TimeInForce",
    "TradeMode",
    "build_grey_market_buy_plan",
    "execute_grey_market_buy",
    "run_grey_market_snatch",
    "run_replay",
    "signal_from_record",
    "submit_grey_market_buy_plan",
    "wait_for_grey_market_open",
]
