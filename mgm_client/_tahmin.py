from __future__ import annotations

import datetime as _dt
from typing import Any

from ._constants import CONDITION_CODES
from ._errors import MGMWeatherError


class _TahminMixin:
    """Gunluk ve saatlik tahmin."""

    def _tahmin_ttl(self) -> float:
        if self.cache_ttl_seconds <= 0:
            return self.cache_ttl_seconds
        return self.tahmin_ttl_saniye

    def gunluk_tahmin(self, istasyon_id: int | str) -> list[dict[str, Any]]:
        """Bir istasyon için 5 günlük tahmini gün gün liste olarak döndürür."""
        data = self._get(
            "tahminler/gunluk", {"istno": istasyon_id}, ttl_override=self._tahmin_ttl()
        )
        if not data:
            raise MGMWeatherError(
                f"{istasyon_id} numaralı istasyon için tahmin verisi bulunamadı."
            )
        kayit = data[0]
        bugun = _dt.date.today()
        sonuc = []
        for gun in range(1, 6):
            kod = kayit.get(f"hadiseGun{gun}")
            sonuc.append(
                {
                    "tarih": (bugun + _dt.timedelta(days=gun - 1)).isoformat(),
                    "enDusuk": kayit.get(f"enDusukGun{gun}"),
                    "enYuksek": kayit.get(f"enYuksekGun{gun}"),
                    "enDusukNem": kayit.get(f"enDusukNemGun{gun}"),
                    "enYuksekNem": kayit.get(f"enYuksekNemGun{gun}"),
                    "ruzgarHizi": kayit.get(f"ruzgarHizGun{gun}"),
                    "durumKodu": kod,
                    "durum": CONDITION_CODES.get(kod, kod),
                }
            )
        return sonuc

    # Tarımsal don / kırağı riski. MGM'nin ayrı bir resmi don uç noktası
    # yok, bu yüzden 5 günlük tahminin (enDusuk, nem, rüzgar) üzerinden
    # ziraat meteorolojisinde yaygın kullanılan eşiklerle sezgisel bir
    # risk sınıflandırması yapılır. RESMİ MGM DON UYARISI DEĞİLDİR.
    _DON_ESIKLERI = (
        # (üst_sinir_C, seviye, aciklama)
        (-10.0, "Çok Kuvvetli Don", "Hassas tüm bitkiler için ciddi zarar riski."),
        (-5.0, "Kuvvetli Don", "Çoğu bitki türü için zarar riski yüksek."),
        (-2.0, "Orta Don", "Soğuğa hassas bitkilerde zarar görülebilir."),
        (0.0, "Hafif Don", "Hassas fide ve çiçeklerde zarar olasılığı var."),
        (4.0, "Kırağı Riski", "Açık/durgun gecelerde yüzey kırağısı olasılığı var."),
    )



    def saatlik_tahmin(self, istasyon_id: int | str) -> list[dict[str, Any]]:
        """Bir istasyon için saatlik tahmin verisini döndürür (mevcutsa).

        gunluk_tahmin() gibi _tahmin_ttl() kullanır (bkz. orada).
        """
        data = self._get(
            "tahminler/saatlik", {"istno": istasyon_id}, ttl_override=self._tahmin_ttl()
        )
        return data or []

    # Gün doğumu ve batımı (sunrise-sunset.org üzerinden)
