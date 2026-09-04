"""Resilience exception types exposed by the modular package."""

from mgm_client import MGMCircuitOpenError, MGMWeatherError

__all__ = ["MGMCircuitOpenError", "MGMWeatherError"]
