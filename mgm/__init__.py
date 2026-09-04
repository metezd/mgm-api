"""Modular MGM client package."""

from .client import MGMWeather
from .errors import MGMCircuitOpenError, MGMWeatherError

__all__ = ["MGMCircuitOpenError", "MGMWeather", "MGMWeatherError"]
