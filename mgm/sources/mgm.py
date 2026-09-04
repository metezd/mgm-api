"""MGM source facade.

The concrete implementation remains in MGMWeather during the incremental
migration to avoid changing the existing public behavior.
"""

from mgm_client import MGMWeather

__all__ = ["MGMWeather"]
