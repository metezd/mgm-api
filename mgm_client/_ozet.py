from __future__ import annotations

import datetime as _dt
from typing import Any

_GUN_ADLARI = (
    "Pazartesi",
    "Salı",
    "Çarşamba",
    "Perşembe",
    "Cuma",
    "Cumartesi",
    "Pazar",
)

_YAGIS_ANAHTAR_KELIMELER = ("yağmur", "yağış", "kar", "dolu", "sağanak")
_SICAK_ESIK = 32.0
_DON_ESIK = 0.0
_RUZGAR_ESIK = 40.0
_TREND_ESIK = 3.0  # °C


class _OzetMixin:
    """Akıllı Özetleme (NLP): guncel_durum + gunluk_tahmin verilerini tek
    bir Türkçe doğal dil paragrafına çeviren kural tabanlı (rule-based)
    bir NLG katmanı. Gerçek bir dil modeli/ML KULLANMAZ — mevcut sayısal/
    kodlu MGM verisini (sıcaklık, durum kodu, rüzgar vb.) şablonlarla
    okunabilir Türkçe cümlelere dönüştürür.
    """

    @staticmethod
    def _gun_etiketi(index: int, tarih: str | None) -> str:
        if tarih:
            try:
                hafta_gunu = _dt.date.fromisoformat(tarih).weekday()
                ad: str | None = _GUN_ADLARI[hafta_gunu]
            except ValueError:
                ad = None
        else:
            ad = None
        if index == 0:
            return "Bugün"
        if index == 1:
            return "Yarın"
        return ad or f"{index + 1}. gün"

    @staticmethod
    def _yagisli_mi(durum: str | None) -> bool:
        if not durum:
            return False
        metin = durum.lower()
        return any(k in metin for k in _YAGIS_ANAHTAR_KELIMELER)

    def akilli_ozet(self, il: str, ilce: str | None = None) -> dict[str, Any]:
        """
        hava_durumu()'nun döndürdüğü güncel durum + 5 günlük tahmini tek
        bir Türkçe özet paragrafına, kısa "anahtarNoktalar" ve "uyarilar"
        listelerine, ayrıca basit bir sıcaklık trendine dönüştürür.

        Yöntem tamamen kural tabanlıdır (şablon + eşik değer), bir dil
        modeli çağrılmaz. MGM'nin resmi bir metin ürünü DEĞİLDİR,
        mevcut sayısal veriden türetilmiş bir sunum katmanıdır.
        """
        veri = self.hava_durumu(il, ilce)
        guncel = veri.get("guncel") or {}
        gunler = veri.get("tahmin") or []

        gosterilen_il = veri.get("il") or il
        gosterilen_ilce = veri.get("ilce") or ilce
        yer_adi = f"{gosterilen_ilce}, {gosterilen_il}" if gosterilen_ilce else gosterilen_il

        cumleler: list[str] = []
        anahtar_noktalar: list[str] = []
        uyarilar: list[str] = []

        sicaklik = guncel.get("sicaklik")
        durum = guncel.get("durum")
        if sicaklik is not None and durum:
            cumleler.append(
                f"{yer_adi} için şu an hava {durum.lower()} ve sıcaklık {sicaklik:g}°C."
            )
            anahtar_noktalar.append(f"Şu an {sicaklik:g}°C, {durum}")
        elif durum:
            cumleler.append(f"{yer_adi} için şu an hava {durum.lower()}.")
            anahtar_noktalar.append(f"Şu an {durum}")

        guncel_ruzgar = guncel.get("ruzgarHizi")
        if guncel_ruzgar is not None and guncel_ruzgar >= _RUZGAR_ESIK:
            cumleler.append(f"Rüzgar {guncel_ruzgar:g} km/s ile kuvvetli esiyor.")
            uyarilar.append(f"Şu an kuvvetli rüzgar: {guncel_ruzgar:g} km/s")

        gun_cumleleri: list[str] = []
        for i, gun in enumerate(gunler):
            etiket = self._gun_etiketi(i, gun.get("tarih"))
            en_dusuk = gun.get("enDusuk")
            en_yuksek = gun.get("enYuksek")
            gun_durum = gun.get("durum")

            if en_dusuk is not None and en_yuksek is not None:
                gun_cumleleri.append(
                    f"{etiket} {en_dusuk:g}/{en_yuksek:g}°C, {gun_durum or 'bilinmiyor'}"
                )
            elif gun_durum:
                gun_cumleleri.append(f"{etiket} {gun_durum}")

            if self._yagisli_mi(gun_durum):
                anahtar_noktalar.append(f"{etiket} yağış bekleniyor ({gun_durum})")

            if en_yuksek is not None and en_yuksek >= _SICAK_ESIK:
                uyarilar.append(f"{etiket} sıcaklık {en_yuksek:g}°C'ye çıkabilir")
            if en_dusuk is not None and en_dusuk <= _DON_ESIK:
                uyarilar.append(f"{etiket} donma riski ({en_dusuk:g}°C)")

            gun_ruzgar = gun.get("ruzgarHizi")
            if gun_ruzgar is not None and gun_ruzgar >= _RUZGAR_ESIK:
                uyarilar.append(f"{etiket} kuvvetli rüzgar bekleniyor ({gun_ruzgar:g} km/s)")

        if gun_cumleleri:
            cumleler.append("5 günlük tahmin: " + "; ".join(gun_cumleleri) + ".")

        trend: dict[str, Any] = {"yon": "bilinmiyor", "farkC": None}
        gecerli_yuksekler = [
            g.get("enYuksek") for g in gunler if g.get("enYuksek") is not None
        ]
        if len(gecerli_yuksekler) >= 2:
            fark = round(float(gecerli_yuksekler[-1]) - float(gecerli_yuksekler[0]), 1)
            if fark >= _TREND_ESIK:
                trend = {"yon": "yukseliyor", "farkC": fark}
                cumleler.append(
                    f"Önümüzdeki günlerde sıcaklık yükseliş eğiliminde ({fark:+.1f}°C)."
                )
            elif fark <= -_TREND_ESIK:
                trend = {"yon": "dusuyor", "farkC": fark}
                cumleler.append(
                    f"Önümüzdeki günlerde sıcaklık düşüş eğiliminde ({fark:+.1f}°C)."
                )
            else:
                trend = {"yon": "sabit", "farkC": fark}
                cumleler.append(
                    "Önümüzdeki günlerde sıcaklıkta belirgin bir değişim beklenmiyor."
                )

        if not cumleler:
            cumleler.append(f"{yer_adi} için şu anda yeterli veri yok.")

        return {
            "il": gosterilen_il,
            "ilce": gosterilen_ilce,
            "ozet": " ".join(cumleler),
            "anahtarNoktalar": anahtar_noktalar,
            "uyarilar": uyarilar,
            "trend": trend,
            "aciklama": (
                "Bu özet guncel_durum ve gunluk_tahmin verilerinden kural "
                "tabanlı (rule-based) doğal dil üretimiyle otomatik "
                "oluşturulur, bir dil modeli kullanılmaz. MGM'nin resmi "
                "bir metin ürünü değildir."
            ),
        }
