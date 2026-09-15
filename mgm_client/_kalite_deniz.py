from __future__ import annotations

import datetime as _dt
import json
import math
import re
from typing import Any, ClassVar

import requests

from ._constants import logger
from ._errors import MGMWeatherError


class _KaliteDenizMixin:
    """Hava kalitesi, polen, deniz suyu/dalga (haversine tabanli en-yakin-istasyon)."""

    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """İki koordinat arası büyük daire mesafesi (km). En yakın İBB
        istasyonunu bulmak için kullanılır."""
        yaricap = 6371.0088
        f1, f2 = math.radians(lat1), math.radians(lat2)
        dfi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = (
            math.sin(dfi / 2) ** 2
            + math.cos(f1) * math.cos(f2) * math.sin(dlambda / 2) ** 2
        )
        return 2 * yaricap * math.asin(math.sqrt(a))



    def _ibb_istasyonlari(self) -> list[dict[str, Any]]:
        """İBB hava kalitesi istasyon listesini getirir ve uzun süre cacheler.
        """
        cache_key = self._cache_key("ibb-hk-istasyonlar")

        def loader() -> list[dict[str, Any]]:
            try:
                resp = self.session.get(
                    self.IBB_HAVA_KALITESI_ISTASYONLAR_URL, timeout=self.timeout
                )
                resp.raise_for_status()
                ham = resp.json()
            except (requests.RequestException, ValueError) as exc:
                raise MGMWeatherError(
                    f"İBB hava kalitesi istasyon listesi alınamadı: {exc}"
                ) from exc

            istasyonlar: list[dict[str, Any]] = []
            for kayit in ham if isinstance(ham, list) else []:
                konum = str(kayit.get("Location") or "")
                parcalar = [p.strip() for p in konum.split(",")]
                if len(parcalar) != 2:
                    continue
                try:
                    enlem, boylam = float(parcalar[0]), float(parcalar[1])
                except ValueError:
                    continue
                istasyonlar.append(
                    {
                        "id": kayit.get("Id"),
                        "ad": kayit.get("Name"),
                        "adres": kayit.get("Adress") or kayit.get("Address"),
                        "enlem": enlem,
                        "boylam": boylam,
                    }
                )
            if not istasyonlar:
                raise MGMWeatherError(
                    "İBB hava kalitesi istasyon listesi boş ya da beklenmeyen "
                    "formatta döndü."
                )
            return istasyonlar

        return self._cached_get(
            cache_key, loader, ttl_override=self.ibb_istasyon_ttl_saniye
        )

    def _ibb_en_yakin_istasyon(
        self, enlem: float, boylam: float
    ) -> dict[str, Any] | None:
        """Verilen koordinata en yakın İBB istasyonunu döner. En yakını
        `ibb_max_mesafe_km`'den uzaksa (İstanbul dışı) None döner."""
        istasyonlar = self._ibb_istasyonlari()
        en_yakin = min(
            istasyonlar,
            key=lambda s: self._haversine_km(enlem, boylam, s["enlem"], s["boylam"]),
        )
        mesafe = self._haversine_km(
            enlem, boylam, en_yakin["enlem"], en_yakin["boylam"]
        )
        if mesafe > self.ibb_max_mesafe_km:
            return None
        return en_yakin



    def _piri_reis_deniz_istasyonlari(self) -> list[dict[str, Any]]:
        """
        MGM'nin Piri Reis "Deniz Suyu Sıcaklıkları" sayfasını çekip <script id="__NEXT_DATA__"> ,
        içine gömülü JSON'dan istasyon listesi ve anlık sıcaklıkları ayıklar.
        Bu resmi bir REST API DEĞİLDİR Bu yüzden sadece birincil kaynak olarak kullanılır
        herhangi bir adımda başarısız olursa Open-Meteo'ya düşer.
        """
        cache_key = self._cache_key("piri-reis-deniz-suyu")

        def loader() -> list[dict[str, Any]]:
            try:
                resp = self.session.get(
                    self.PIRI_REIS_DENIZ_SUYU_URL,
                    headers=self.PIRI_REIS_HEADERS,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
            except requests.RequestException as exc:
                raise MGMWeatherError(
                    f"Piri Reis deniz suyu sıcaklığı sayfasına ulaşılamadı: {exc}"
                ) from exc

            eslesme = re.search(
                r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                resp.text,
                re.DOTALL,
            )
            if not eslesme:
                raise MGMWeatherError(
                    "Piri Reis sayfa yapısı değişmiş görünüyor "
                    "(__NEXT_DATA__ bulunamadı)."
                )
            try:
                yuk = json.loads(eslesme.group(1))
                ham_veri = yuk["props"]["pageProps"]["data"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise MGMWeatherError(
                    f"Piri Reis JSON yapısı beklenmedik: {exc}"
                ) from exc

            istasyonlar: list[dict[str, Any]] = []
            for kayit in ham_veri if isinstance(ham_veri, list) else []:
                sicaklik = kayit.get("denizSicaklik")
                if sicaklik is None:
                    continue
                try:
                    enlem = float(kayit["enlem"])
                    boylam = float(kayit["boylam"])
                except (KeyError, TypeError, ValueError):
                    continue
                istasyonlar.append(
                    {
                        "istasyonId": kayit.get("istNo"),
                        "ad": kayit.get("istAd"),
                        "il": kayit.get("il"),
                        "ilce": kayit.get("ilce"),
                        "enlem": enlem,
                        "boylam": boylam,
                        "sicaklik": float(sicaklik),
                        "olcumZamani": kayit.get("denizVeriZamani"),
                    }
                )
            if not istasyonlar:
                raise MGMWeatherError(
                    "Piri Reis deniz suyu sıcaklığı verisi boş ya da "
                    "beklenmeyen formatta döndü."
                )
            return istasyonlar

        return self._cached_get(
            cache_key, loader, ttl_override=self.piri_reis_ttl_saniye
        )

    def _piri_reis_en_yakin_istasyon(
        self, enlem: float, boylam: float
    ) -> dict[str, Any] | None:
        """Verilen koordinata en yakın Piri Reis deniz istasyonunu döner.
        en yakını `piri_reis_max_mesafe_km`'den uzaksa None döner."""
        istasyonlar = self._piri_reis_deniz_istasyonlari()
        en_yakin = min(
            istasyonlar,
            key=lambda s: self._haversine_km(enlem, boylam, s["enlem"], s["boylam"]),
        )
        mesafe = self._haversine_km(
            enlem, boylam, en_yakin["enlem"], en_yakin["boylam"]
        )
        if mesafe > self.piri_reis_max_mesafe_km:
            return None
        return en_yakin

    @staticmethod


    def _sozlukten_kirletici_deger(veri: dict[str, Any], *adaylar: str) -> float | None:
        """İBB'nin Concentration/AQI nesnelerindeki alan adı casing'i resmi
        dokümanda net belirtilmediğinden, olası varyasyonları (PM10, Pm10,
        pm10 vb.) büyük/küçük harf duyarsız arar."""
        kucuk_harfli = {str(k).lower(): v for k, v in veri.items()}
        for aday in adaylar:
            if aday.lower() in kucuk_harfli:
                try:
                    return float(kucuk_harfli[aday.lower()])
                except (TypeError, ValueError):
                    return None
        return None

    def _ibb_olcum(self, istasyon_id: str) -> dict[str, Any]:
        """Verilen İBB istasyon id'si için en güncel PM10/NO2 ölçümünü döner.

        Not: bu servisin tam JSON şeması resmi dokümanda örnek yanıt olarak
        verilmemiştir. bu yöntem hem liste hem tekil kayıt biçimini kabul
        eder ve alan adlarını harf duyarsız arar. Servis yanıtı beklenmedik
        şekilde değişirse MGMWeatherError fırlatılır ve çağıran taraf
        `hava_kalitesi` Open-Meteo'ya düşer.
        """
        simdi = _dt.datetime.now()
        baslangic = simdi - _dt.timedelta(hours=3)
        params = {
            "StationId": istasyon_id,
            "StartDate": baslangic.strftime("%d.%m.%Y %H:%M:%S"),
            "EndDate": simdi.strftime("%d.%m.%Y %H:%M:%S"),
        }
        cache_key = self._cache_key("ibb-hk-olcum", params)

        def loader() -> dict[str, Any]:
            try:
                resp = self.session.get(
                    self.IBB_HAVA_KALITESI_OLCUM_URL, params=params, timeout=self.timeout
                )
                resp.raise_for_status()
                ham = resp.json()
            except (requests.RequestException, ValueError) as exc:
                raise MGMWeatherError(
                    f"İBB hava kalitesi ölçüm servisinden veri alınamadı: {exc}"
                ) from exc

            kayitlar = ham if isinstance(ham, list) else [ham] if isinstance(ham, dict) else []
            if not kayitlar:
                raise MGMWeatherError(
                    f"İBB istasyon {istasyon_id} için ölçüm kaydı bulunamadı."
                )

            def _zaman(kayit: dict[str, Any]) -> str:
                return str(kayit.get("ReadTime") or "")

            en_guncel = max(kayitlar, key=_zaman)
            konsantrasyon = en_guncel.get("Concentration") or {}
            aqi = en_guncel.get("AQI") or {}
            if not isinstance(konsantrasyon, dict):
                konsantrasyon = {}
            if not isinstance(aqi, dict):
                aqi = {}

            pm10 = self._sozlukten_kirletici_deger(konsantrasyon, "PM10")
            no2 = self._sozlukten_kirletici_deger(konsantrasyon, "NO2", "NO_2")
            if pm10 is None and no2 is None:
                raise MGMWeatherError(
                    f"İBB istasyon {istasyon_id} yanıtında beklenen kirletici "
                    "alanları bulunamadı."
                )
            return {
                "pm10": pm10,
                "no2": no2,
                "so2": self._sozlukten_kirletici_deger(konsantrasyon, "SO2"),
                "o3": self._sozlukten_kirletici_deger(konsantrasyon, "O3"),
                "co": self._sozlukten_kirletici_deger(konsantrasyon, "CO"),
                "hki": self._sozlukten_kirletici_deger(aqi, "AQI", "HKI"),
                "olcumZamani": en_guncel.get("ReadTime"),
            }

        return self._cached_get(
            cache_key, loader, ttl_override=self.hava_kalitesi_ttl_saniye
        )

    def _open_meteo_hava_kalitesi(self, enlem: float, boylam: float) -> dict[str, Any]:
        """Open-Meteo Air Quality API'sinden (key gerektirmez, CAMS verisine
        dayanır) anlık PM10, PM2.5, NO2 ve UV indeksini döner. İBB'nin
        kapsamadığı bölgeler için tam fallback, İstanbul için ise PM2.5 ve
        UV indeksinin tamamlayıcı kaynağıdır
        """
        params = {
            "latitude": enlem,
            "longitude": boylam,
            "current": "pm10,pm2_5,nitrogen_dioxide,uv_index,european_aqi",
            "timezone": "Europe/Istanbul",
        }
        cache_key = self._cache_key("open-meteo-hava-kalitesi", params)

        def loader() -> dict[str, Any]:
            try:
                resp = self.session.get(
                    self.OPEN_METEO_AIR_QUALITY_URL, params=params, timeout=self.timeout
                )
                resp.raise_for_status()
                veri = resp.json()["current"]
            except (requests.RequestException, KeyError, ValueError) as exc:
                raise MGMWeatherError(
                    f"Open-Meteo hava kalitesi servisinden veri alınamadı: {exc}"
                ) from exc

            return {
                "pm10": veri.get("pm10"),
                "pm25": veri.get("pm2_5"),
                "no2": veri.get("nitrogen_dioxide"),
                "uvIndeksi": veri.get("uv_index"),
                "avrupaHKI": veri.get("european_aqi"),
                "olcumZamani": veri.get("time"),
            }

        return self._cached_get(
            cache_key, loader, ttl_override=self.hava_kalitesi_ttl_saniye
        )



    def hava_kalitesi(self, enlem: float, boylam: float) -> dict[str, Any]:
        """Anlık UV indeksi ve hava kalitesi (PM10, PM2.5, NO2) döner.

        Öncelik İBB'nin resmi hava kalitesi ölçüm ağındadır (yalnızca
        İstanbul'u, `ibb_max_mesafe_km` yarıçapında kapsar). PM2.5 ve UV
        indeksi İBB servisinde bulunmadığından her zaman Open-Meteo Air
        Quality API'siyle (key gerektirmez) tamamlanır. İBB'ye hiç
        ulaşılamazsa (İstanbul dışı konum, istasyon/servis hatası vb.)
        tüm alanlar Open-Meteo'dan gelir. Döndürülen sözlükte her zaman
        `kaynaklar` alanı bulunur: hangi alanın hangi servisten geldiğini
        gösterir (örn. {"pm10": "ibb", "pm25": "open-meteo", ...}).
        """
        acik_meteo = self._open_meteo_hava_kalitesi(enlem, boylam)
        sonuc: dict[str, Any] = {
            "pm10": acik_meteo["pm10"],
            "pm25": acik_meteo["pm25"],
            "no2": acik_meteo["no2"],
            "uvIndeksi": acik_meteo["uvIndeksi"],
            "avrupaHKI": acik_meteo["avrupaHKI"],
            "olcumZamani": acik_meteo["olcumZamani"],
            "istasyon": None,
            "kaynaklar": {
                "pm10": "open-meteo",
                "pm25": "open-meteo",
                "no2": "open-meteo",
                "uvIndeksi": "open-meteo",
            },
        }

        try:
            istasyon = self._ibb_en_yakin_istasyon(enlem, boylam)
            if istasyon is None:
                return sonuc
            ibb_olcum = self._ibb_olcum(istasyon["id"])
        except MGMWeatherError as exc:
            logger.warning("İBB hava kalitesi alınamadı, Open-Meteo kullanılıyor: %s", exc)
            return sonuc

        if ibb_olcum.get("pm10") is not None:
            sonuc["pm10"] = ibb_olcum["pm10"]
            sonuc["kaynaklar"]["pm10"] = "ibb"
            sonuc["olcumZamani"] = ibb_olcum.get("olcumZamani") or sonuc["olcumZamani"]
        if ibb_olcum.get("no2") is not None:
            sonuc["no2"] = ibb_olcum["no2"]
            sonuc["kaynaklar"]["no2"] = "ibb"
        sonuc["istasyon"] = {
            "ad": istasyon["ad"],
            "adres": istasyon["adres"],
            "enlem": istasyon["enlem"],
            "boylam": istasyon["boylam"],
        }
        return sonuc

    # Polen ve alerji indeksi (tur_esik_dusuk, tur_esik_orta, tur_esik_yuksek)
    # grains/m³ EAN/CAMS tabanlı yaygın kullanılan yaklaşık eşikler
    # kesin klinik eşikler türe ve bölgeye göre değişebilir,
    # bu sınıflandırma genel bir yönlendirme amaçlıdır, tıbbi tavsiye değildir.
    POLEN_TURLERI: ClassVar[dict[str, tuple[str, int, int, int]]] = {
        "grass_pollen": ("cimen", 20, 50, 150),
        "birch_pollen": ("huş", 30, 90, 500),
        "alder_pollen": ("kızılağaç", 30, 90, 300),
        "mugwort_pollen": ("pelin otu", 20, 50, 150),
        "olive_pollen": ("zeytin", 10, 50, 200),
        "ragweed_pollen": ("ambrosia", 10, 30, 100),
    }



    def _polen_seviyesi(self, tur_anahtari: str, deger: float | None) -> str:
        if deger is None:
            return "Veri Yok"
        if deger <= 0:
            return "Yok"
        _, dusuk, orta, yuksek = self.POLEN_TURLERI[tur_anahtari]
        if deger <= dusuk:
            return "Düşük"
        if deger <= orta:
            return "Orta"
        if deger <= yuksek:
            return "Yüksek"
        return "Çok Yüksek"

    def polen_indeksi(self, enlem: float, boylam: float) -> dict[str, Any]:
        """
        Anlık polen konsantrasyonlarını (Grains/m³) ve her tür için
        basitleştirilmiş risk seviyesini (Yok/Düşük/Orta/Yüksek/Çok
        Yüksek) döner. Open-Meteo Air Quality API'sinden (CAMS Avrupa
        modeli, key gerektirmez) beslenir.

        Kapsam notu: CAMS polen verisi yalnızca Avrupa bölgesini ve
        yalnızca ilgili türün polen sezonunu kapsar. Sezon dışında veya
        modelin kapsamadığı konumlarda tür bazında değer null döner
        (`seviye: "Veri Yok"` olarak işaretlenir), bu bir hata değildir.

        Not: Seviyeler kesin klinik/tıbbi eşikler değildir. Genel
        yönlendirme amaçlı, EAN/CAMS tabanlı yaklaşık sınıflandırmadır.
        """
        params = {
            "latitude": enlem,
            "longitude": boylam,
            "current": ",".join(self.POLEN_TURLERI.keys()),
            "timezone": "Europe/Istanbul",
        }
        cache_key = self._cache_key("open-meteo-polen", params)

        def loader() -> dict[str, Any]:
            try:
                resp = self.session.get(
                    self.OPEN_METEO_AIR_QUALITY_URL, params=params, timeout=self.timeout
                )
                resp.raise_for_status()
                veri = resp.json()["current"]
            except (requests.RequestException, KeyError, ValueError) as exc:
                raise MGMWeatherError(
                    f"Open-Meteo polen servisinden veri alınamadı: {exc}"
                ) from exc

            turler: dict[str, Any] = {}
            en_yuksek_seviye_sirasi = 0
            baskin_tur = None
            seviye_sirasi = {
                "Veri Yok": 0,
                "Yok": 0,
                "Düşük": 1,
                "Orta": 2,
                "Yüksek": 3,
                "Çok Yüksek": 4,
            }
            for anahtar, (tr_adi, _, _, _) in self.POLEN_TURLERI.items():
                deger = veri.get(anahtar)
                seviye = self._polen_seviyesi(anahtar, deger)
                turler[anahtar] = {
                    "ad": tr_adi,
                    "deger": deger,
                    "birim": "Grains/m³",
                    "seviye": seviye,
                }
                sira = seviye_sirasi.get(seviye, 0)
                if sira > en_yuksek_seviye_sirasi:
                    en_yuksek_seviye_sirasi = sira
                    baskin_tur = tr_adi

            return {
                "turler": turler,
                "baskinTur": baskin_tur,
                "genelRiskSeviyesi": next(
                    (k for k, v in seviye_sirasi.items() if v == en_yuksek_seviye_sirasi),
                    "Veri Yok",
                ),
                "olcumZamani": veri.get("time"),
                "kaynak": "open-meteo (CAMS Avrupa)",
                "aciklama": (
                    "Seviyeler EAN/CAMS tabanlı yaklaşık sınıflandırmadır, "
                    "kesin klinik eşik değildir. CAMS yalnızca Avrupa "
                    "bölgesini ve ilgili türün sezonunu kapsar."
                ),
            }

        return self._cached_get(
            cache_key, loader, ttl_override=self.hava_kalitesi_ttl_saniye
        )



    def _open_meteo_deniz(self, enlem: float, boylam: float) -> dict[str, Any]:
        """Open-Meteo Marine Weather API'sinden anlık dalga
        durumu ve deniz suyu sıcaklığını döner.
        """
        params = {
            "latitude": enlem,
            "longitude": boylam,
            "current": (
                "wave_height,wave_period,wave_direction,sea_surface_temperature"
            ),
            "timezone": "Europe/Istanbul",
        }
        cache_key = self._cache_key("open-meteo-deniz", params)

        def loader() -> dict[str, Any]:
            try:
                resp = self.session.get(
                    self.OPEN_METEO_MARINE_URL, params=params, timeout=self.timeout
                )
                resp.raise_for_status()
                veri = resp.json()["current"]
            except (requests.RequestException, KeyError, ValueError) as exc:
                raise MGMWeatherError(
                    f"Open-Meteo deniz durumu servisinden veri alınamadı: {exc}"
                ) from exc

            return {
                "denizSuyuSicakligi": veri.get("sea_surface_temperature"),
                "dalgaYuksekligi": veri.get("wave_height"),
                "dalgaPeriyodu": veri.get("wave_period"),
                "dalgaYonu": veri.get("wave_direction"),
                "olcumZamani": veri.get("time"),
            }

        return self._cached_get(cache_key, loader, ttl_override=self.deniz_ttl_saniye)

    def deniz_durumu(self, enlem: float, boylam: float) -> dict[str, Any]:
        """
        Anlık deniz suyu sıcaklığı ve dalga durumu (yükseklik, periyot,
        yön) döner.

        Kaynak önceliği: deniz suyu sıcaklığı için ÖNCELİK MGM'nin Piri
        Reis sayfasındaki (pirireis.mgm.gov.tr) gerçek istasyon
        ölçümlerindedir. Dalga yüksekliği/periyodu/yönü Piri Reis kaynağında bulunmadığından
        Open-Meteo'dan gelir. Döndürülen sözlükte `kaynaklar` alanı hangi verinin nereden geldiğini gösterir.

        Kapsam notu: Open-Meteo'nun deniz modeli yalnızca deniz
        grid hücrelerini kapsar. Piri Reis istasyonu da bulunamazsa
        (ikisi de kapsam dışıysa) tüm alanlar null döner ve
        `kapsamDisi: true` ile işaretlenir
        """
        acik_meteo = self._open_meteo_deniz(enlem, boylam)
        sonuc: dict[str, Any] = {
            "denizSuyuSicakligi": acik_meteo["denizSuyuSicakligi"],
            "dalgaYuksekligi": acik_meteo["dalgaYuksekligi"],
            "dalgaPeriyodu": acik_meteo["dalgaPeriyodu"],
            "dalgaYonu": acik_meteo["dalgaYonu"],
            "olcumZamani": acik_meteo["olcumZamani"],
            "istasyon": None,
            "kaynaklar": {
                "denizSuyuSicakligi": "open-meteo",
                "dalga": "open-meteo",
            },
        }

        try:
            istasyon = self._piri_reis_en_yakin_istasyon(enlem, boylam)
            if istasyon is not None:
                sonuc["denizSuyuSicakligi"] = istasyon["sicaklik"]
                sonuc["olcumZamani"] = istasyon.get("olcumZamani") or sonuc["olcumZamani"]
                sonuc["kaynaklar"]["denizSuyuSicakligi"] = "piri-reis"
                sonuc["istasyon"] = {
                    "ad": istasyon["ad"],
                    "il": istasyon.get("il"),
                    "ilce": istasyon.get("ilce"),
                    "enlem": istasyon["enlem"],
                    "boylam": istasyon["boylam"],
                }
        except MGMWeatherError as exc:
            logger.warning(
                "Piri Reis deniz suyu sıcaklığı alınamadı, Open-Meteo kullanılıyor: %s",
                exc,
            )

        kapsam_disi = (
            sonuc["denizSuyuSicakligi"] is None and sonuc["dalgaYuksekligi"] is None
        )
        sonuc["kapsamDisi"] = kapsam_disi
        sonuc["aciklama"] = (
            "Kıyıya/açık denize yakın koordinat gerektirir, karasal "
            "konumlarda ve Piri Reis'in kapsamadığı kıyılarda tüm "
            "alanlar null döner."
            if kapsam_disi
            else None
        )
        return sonuc

    # Günlük tahmin (5 günlük)
