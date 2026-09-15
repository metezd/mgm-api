"""
mgm_client
----------
Not: MGM'nin eski "web servis" (SOAP/REST) API'sinin yerini alan,
www.mgm.gov.tr sitesinin kendisinin kullandığı iç JSON servislerine dayanır.
Resmi belgelenmiş bir API değildir. MGM bu uç noktaların yapısını
değiştirirse istemcinin de güncellenmesi gerekir.

Kullanım:
    from mgm_client import MGMWeather

    mgm = MGMWeather()
    istasyonlar = mgm.il_istasyonlari("İstanbul")
    guncel = mgm.guncel_durum(istasyonlar[0]["istasyonId"])
    tahmin = mgm.gunluk_tahmin(istasyonlar[0]["istasyonId"])
"""

from __future__ import annotations

__version__ = "0.1.0"  # pyproject.toml'daki version ile birlikte güncellenmeli

import datetime as _dt

from ._constants import (
    CACHE_SONUC_SAYAC,
    REDIS_STARTUP_RETRY_ATTEMPTS,
    REDIS_STARTUP_RETRY_DELAY_SECONDS,
    _tr_normalize,
)
from ._errors import MGMCircuitOpenError, MGMWeatherError
from .client import MGMWeather
from .iller import TURKIYE_ILLERI, turkiye_illeri

__all__ = [
    "CACHE_SONUC_SAYAC",
    "REDIS_STARTUP_RETRY_ATTEMPTS",
    "REDIS_STARTUP_RETRY_DELAY_SECONDS",
    "TURKIYE_ILLERI",
    "MGMCircuitOpenError",
    "MGMWeather",
    "MGMWeatherError",
    "__version__",
    "_dt",
    "_tr_normalize",
    "turkiye_illeri",
]
