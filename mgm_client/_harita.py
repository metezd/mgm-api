from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from ._constants import _tr_normalize
from ._errors import MGMWeatherError
from .iller import TURKIYE_ILLERI


class _HaritaMixin:
    """Il sinirlari GeoJSON + son durum sicakliklari birlesimi."""

    def il_sinirlari_geojson(self) -> dict[str, Any]:
        """
        Türkiye'nin 81 ilinin sınır poligonlarını GeoJSON FeatureCollection
        olarak döner. Uzun TTL ile önbelleklenir çünkü idari sınırlar pratikte değişmez.
        """
        cache_key = self._cache_key("il-sinirlari-geojson")

        def loader() -> dict[str, Any]:
            try:
                resp = self.session.get(
                    self.GEOJSON_IL_SINIRLARI_URL, timeout=self.timeout
                )
                resp.raise_for_status()
                veri = resp.json()
            except (requests.RequestException, ValueError) as exc:
                raise MGMWeatherError(
                    f"İl sınırları GeoJSON verisi alınamadı: {exc}"
                ) from exc
            if veri.get("type") != "FeatureCollection" or not veri.get("features"):
                raise MGMWeatherError("İl sınırları GeoJSON verisi beklenen formatta değil.")
            return veri

        return self._cached_get(
            cache_key, loader, ttl_override=self.geojson_sinir_ttl_saniye
        )

    def _geojson_il_adini_coz(self, ham_ad: str) -> str | None:
        """GeoJSON kaynağındaki il adını 81-il listesindeki kanonik ada
        çözer. Önce bilinen alias sözlüğüne, sonra difflib yakın eşleşmeye
        bakar."""
        alias = self.GEOJSON_IL_ALIASLARI.get(_tr_normalize(ham_ad))
        if alias:
            return alias
        return self._il_yakin_eslesme(ham_ad)

    def harita_geojson(self) -> dict[str, Any]:
        """
        `/map/geojson` uç noktasının üst düzey yardımcı fonksiyonu:
        il sınırları GeoJSON'unu MGM son durum sıcaklıklarıyla birleştirip
        doğrudan Leaflet/Mapbox'a beslenebilecek tek bir FeatureCollection
        döner. Her feature'ın `properties` alanına şunlar eklenir:
        `il`, `sicaklik` (°C, bulunamazsa null), `durum`, `guncellemeZamani`.

        Sınır verisi bulunamayan/çözülemeyen il adları `sicaklik: null`
        ile birlikte yine de haritada kalır
        """
        sinirlar = self.il_sinirlari_geojson()

        def _il_sicakligi(il_adi: str) -> dict[str, Any]:
            try:
                istasyon = self.ilce_istasyonu(il_adi)
                istasyon_id = istasyon.get("istasyonId") or istasyon.get("merkezId")
                enlem = istasyon.get("enlem") or istasyon.get("lat")
                boylam = istasyon.get("boylam") or istasyon.get("lon")
                cache_key = self._cache_key("harita-sicaklik", {"il": il_adi})

                def loader() -> dict[str, Any]:
                    guncel = self.guncel_durum_yedekli(istasyon_id, enlem, boylam)
                    return {
                        "sicaklik": guncel.get("sicaklik"),
                        "durum": guncel.get("durum"),
                        "guncellemeZamani": guncel.get("olcumZamani"),
                    }

                return self._cached_get(
                    cache_key, loader, ttl_override=self.harita_sicaklik_ttl_saniye
                )
            except MGMWeatherError:
                return {"sicaklik": None, "durum": None, "guncellemeZamani": None}

        il_adlari = sorted({kayit["il"] for kayit in TURKIYE_ILLERI})
        with ThreadPoolExecutor(max_workers=min(len(il_adlari), 16)) as havuz:
            sicakliklar = dict(
                zip(il_adlari, havuz.map(_il_sicakligi, il_adlari), strict=True)
            )

        features_out = []
        for feature in sinirlar["features"]:
            ham_ad = (feature.get("properties") or {}).get("name", "")
            kanonik_il = self._geojson_il_adini_coz(ham_ad)
            veri = sicakliklar.get(kanonik_il, {}) if kanonik_il else {}

            yeni_feature = copy.deepcopy(feature)
            yeni_feature.setdefault("properties", {})
            yeni_feature["properties"]["il"] = kanonik_il or ham_ad
            yeni_feature["properties"]["sicaklik"] = veri.get("sicaklik")
            yeni_feature["properties"]["durum"] = veri.get("durum")
            yeni_feature["properties"]["guncellemeZamani"] = veri.get(
                "guncellemeZamani"
            )
            features_out.append(yeni_feature)

        return {"type": "FeatureCollection", "features": features_out}

