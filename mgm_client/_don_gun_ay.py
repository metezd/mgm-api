from __future__ import annotations

import datetime as _dt
import math
from typing import Any

import requests

from ._errors import MGMWeatherError


class _DonGunAyMixin:
    """Don/kiragi riski, gun dogumu/batimi, ay evresi."""

    def _don_seviyesi(self, en_dusuk: float | None) -> tuple[str, str]:
        if en_dusuk is None:
            return "Bilinmiyor", "Tahmin verisi eksik."
        for esik, seviye, aciklama in self._DON_ESIKLERI:
            if en_dusuk <= esik:
                return seviye, aciklama
        return "Risk Yok", "Don/kırağı beklenmiyor."

    def don_kiragi_riski(self, istasyon_id: int | str, il: str = "", ilce: str | None = None) -> dict[str, Any]:
        """
        Verilen istasyon için 5 günlük tarımsal don/kırağı riski tahmini.

        Yöntem: gunluk_tahmin()'den gelen günlük en düşük sıcaklık (°C)
        eşik tablosuna göre sınıflandırılır (Kırağı Riski / Hafif /
        Orta / Kuvvetli / Çok Kuvvetli Don). Ayrıca düşük rüzgar
        (<10 km/h) + yüksek nem (>%60) birlikte görüldüğünde radyatif
        kırağı oluşumu için elverişli koşul olduğu ayrıca işaretlenir
        (`kiragiKosuluUygun`). Bu, çıplak gökyüzü/durgun gece gibi
        klasik kırağı oluşum koşullarının sıcaklık dışı bir yaklaşımıdır.

        Not: Bu, MGM'nin resmi bir don uyarı ürünü DEĞİLDİR. 5 günlük
        sıcaklık tahmininden türetilmiş bir risk göstergesidir. Kritik
        tarımsal kararlar için MGM'nin resmi tarım meteorolojisi
        bültenleri esas alınmalıdır.
        """
        gunler = self.gunluk_tahmin(istasyon_id)

        sonuc_gunler = []
        seviye_sirasi = {
            "Risk Yok": 0,
            "Bilinmiyor": 0,
            "Kırağı Riski": 1,
            "Hafif Don": 2,
            "Orta Don": 3,
            "Kuvvetli Don": 4,
            "Çok Kuvvetli Don": 5,
        }
        en_yuksek_risk_sirasi = 0
        for gun in gunler:
            en_dusuk = gun.get("enDusuk")
            nem = gun.get("enYuksekNem")
            ruzgar = gun.get("ruzgarHizi")
            seviye, aciklama = self._don_seviyesi(
                float(en_dusuk) if en_dusuk is not None else None
            )
            kiragi_kosulu_uygun = (
                en_dusuk is not None
                and float(en_dusuk) <= 4.0
                and nem is not None
                and float(nem) >= 60.0
                and ruzgar is not None
                and float(ruzgar) < 10.0
            )
            sonuc_gunler.append(
                {
                    "tarih": gun.get("tarih"),
                    "enDusukSicaklik": en_dusuk,
                    "seviye": seviye,
                    "aciklama": aciklama,
                    "kiragiKosuluUygun": kiragi_kosulu_uygun,
                }
            )
            en_yuksek_risk_sirasi = max(
                en_yuksek_risk_sirasi, seviye_sirasi.get(seviye, 0)
            )

        genel_seviye = next(
            (k for k, v in seviye_sirasi.items() if v == en_yuksek_risk_sirasi),
            "Risk Yok",
        )
        return {
            "il": il,
            "ilce": ilce,
            "genelRiskSeviyesi": genel_seviye,
            "gunler": sonuc_gunler,
            "aciklama": (
                "5 günlük sıcaklık tahmininden türetilmiş sezgisel risk "
                "göstergesidir, MGM'nin resmi don uyarı ürünü değildir."
            ),
        }

    # Saatlik tahmin


    def gun_dogumu_batimi(self, enlem: float, boylam: float) -> dict[str, str]:
        params = {"lat": enlem, "lng": boylam, "formatted": 0}
        cache_key = self._cache_key("gun-dogumu-batimi", params)

        def loader() -> dict[str, str]:
            try:
                resp = self.session.get(
                    self.SUNRISE_URL, params=params, timeout=self.timeout
                )
                resp.raise_for_status()
                sonuc = resp.json()["results"]
            except (requests.RequestException, KeyError, ValueError) as exc:
                raise MGMWeatherError(
                    f"Gün doğumu/batımı verisi alınamadı: {exc}"
                ) from exc

            tz = _dt.timezone(_dt.timedelta(hours=3))  # Bizim saat (UTC+3)
            dogum = _dt.datetime.fromisoformat(sonuc["sunrise"]).astimezone(tz)
            batim = _dt.datetime.fromisoformat(sonuc["sunset"]).astimezone(tz)
            return {
                "gunDogumu": dogum.strftime("%H:%M"),
                "gunBatimi": batim.strftime("%H:%M"),
            }

        return self._cached_get(cache_key, loader)

    _AY_EVRE_ADLARI = (
        "Yeni Ay",
        "Hilal (Büyüyen)",
        "İlk Dördün",
        "Şişkin Ay (Büyüyen)",
        "Dolunay",
        "Şişkin Ay (Küçülen)",
        "Son Dördün",
        "Hilal (Küçülen)",
    )
    _SINODIK_AY_GUN = 29.530588861
    # 2000-01-06 18:14 UTC referans yeni ay (bilinen astronomik epoch)
    _AY_EPOKU = _dt.datetime(2000, 1, 6, 18, 14, tzinfo=_dt.UTC)

    def ay_evresi(self, tarih: _dt.date | None = None) -> dict[str, Any]:
        """
        Verilen tarih (UTC gün ortası referans alınarak) için ay evresini
        yerel olarak hesaplar, dış servis/bağımlılık gerektirmez.

        Döner: evreAdi, yasGunu (0-29.53), aydinlanmaOrani (0-1),
        buyuyorMu (bir sonraki dolunaya mı yaklaşıyor).
        """
        an = _dt.datetime.combine(
            tarih or _dt.datetime.now(_dt.UTC).date(),
            _dt.time(hour=12),
            tzinfo=_dt.UTC,
        )
        gecen_gun = (an - self._AY_EPOKU).total_seconds() / 86400.0
        yas = gecen_gun % self._SINODIK_AY_GUN

        evre_orani = yas / self._SINODIK_AY_GUN  # 0-1
        evre_indeksi = int((evre_orani * 8) + 0.5) % 8
        aydinlanma = (1 - math.cos(2 * math.pi * evre_orani)) / 2

        return {
            "evreAdi": self._AY_EVRE_ADLARI[evre_indeksi],
            "yasGunu": round(yas, 2),
            "aydinlanmaOrani": round(aydinlanma, 4),
            "buyuyorMu": evre_orani < 0.5,
        }

