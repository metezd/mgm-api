"""Cache configuration and key helpers exposed by the modular package."""

from mgm_client import (
    CACHE_KEY_MAX_LENGTH,
    CACHE_KEY_NAMESPACE,
    CACHE_KEY_VERSION,
    CACHE_RESPONSE_MAX_BYTES,
)

__all__ = [
    "CACHE_KEY_MAX_LENGTH",
    "CACHE_KEY_NAMESPACE",
    "CACHE_KEY_VERSION",
    "CACHE_RESPONSE_MAX_BYTES",
]
