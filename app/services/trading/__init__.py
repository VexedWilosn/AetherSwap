"""Trading automation foundation services."""

from app.database import PlatformAction

from .actions import create_platform_action, claim_due_actions, transition_action
from .capabilities import CAPABILITY_REGISTRY, get_platform_capabilities, supports
from .order_status import normalize_order_status
from .platform_adapters import PlatformClientAdapter, build_platform_adapters
from .runtime import PlatformActionWorkerRuntime, platform_action_worker_config_from_app_config
from .sell_actions import SellerActionService, is_sell_side_action_type
from .states import PlatformActionState

__all__ = [
    "CAPABILITY_REGISTRY",
    "PlatformAction",
    "PlatformActionState",
    "PlatformActionWorkerRuntime",
    "PlatformClientAdapter",
    "SellerActionService",
    "build_platform_adapters",
    "claim_due_actions",
    "create_platform_action",
    "get_platform_capabilities",
    "is_sell_side_action_type",
    "normalize_order_status",
    "platform_action_worker_config_from_app_config",
    "supports",
    "transition_action",
]
