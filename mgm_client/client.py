"""MGMWeather: tüm mixin'leri birleştiren nihai sınıf. Dışarıya (app.py,
testler, PyPI kullanıcıları) hiçbir davranış değişikliği yansımaz."""

from __future__ import annotations

from dataclasses import dataclass

from ._arama import _AramaMixin
from ._base import _MGMWeatherTemel
from ._don_gun_ay import _DonGunAyMixin
from ._harita import _HaritaMixin
from ._kalite_deniz import _KaliteDenizMixin
from ._sondurum import _SondurumMixin
from ._tahmin import _TahminMixin
from ._turkiye_geneli import _TurkiyeGeneliMixin


@dataclass
class MGMWeather(
    _MGMWeatherTemel,
    _SondurumMixin,
    _KaliteDenizMixin,
    _TurkiyeGeneliMixin,
    _TahminMixin,
    _DonGunAyMixin,
    _HaritaMixin,
    _AramaMixin,
):
    """servis.mgm.gov.tr uç noktalarına istek atan istemci (bkz. paket
    docstring'i, mgm_client/__init__.py). Alan tanımları _MGMWeatherTemel'de,
    metodlar konuya göre ayrı mixin dosyalarında."""
