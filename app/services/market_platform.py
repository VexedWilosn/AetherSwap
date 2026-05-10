from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class BaseMarketPlatform(ABC):
    """Unified interface for market platform operations."""

    @abstractmethod
    def get_price(self, *args: Any, **kwargs: Any) -> Optional[float]:
        """Return the current price for a market item."""

    @abstractmethod
    def buy(self, *args: Any, **kwargs: Any) -> Any:
        """Buy an item from the platform."""

    @abstractmethod
    def sell(self, *args: Any, **kwargs: Any) -> Any:
        """Sell/list an item on the platform."""

    @abstractmethod
    def query_inventory(self, *args: Any, **kwargs: Any) -> Any:
        """Query inventory data from the platform."""
