"""Kaynak sağlığı: pasif izleyici, istemci entegrasyonu, /health/kaynaklar
ve Prometheus metrikleri.

Üç katman:
  1. `_KaynakSagligi` birim testleri (durum makinesi, gecikme, hata sınıflandırma)
  2. Gerçek `MGMWeather` + sahte session: hangi çağrı hangi kaynağa yazılıyor
  3. Flask: `/health/kaynaklar` sözleşmesi, muafiyetler, `/metrics`
"""

import json
import threading
import time
import unittest

import requests

import app as app_module
from mgm_client import MGMCircuitOpenError, MGMWeather, MGMWeatherError
from mgm_client._constants import KAYNAKLAR
from mgm_client._resilience import _KaynakSagligi
from weather_provider import MGMAdapter

# ---------------------------------------------------------------- 1) birim


class TestKaynakSagligiBirim(unittest.TestCase):
    def test_baslangicta_tum_kaynaklar_bilinmiyor(self):
        ozet = _KaynakSagligi().ozet()

        self.assertEqual(set(ozet), set(KAYNAKLAR))
        for kaynak, d in ozet.items():
            with self.subTest(kaynak=kaynak):
                self.assertEqual(d["durum"], "bilinmiyor")
                self.assertIsNone(d["son_basarili"])
                self.assertIsNone(d["son_basarili_yas_saniye"])
                self.assertIsNone(d["son_hata"])
                self.assertEqual((d["basarili_toplam"], d["hata_toplam"]), (0, 0))

    def test_basari_durumu_ok_yapar_ve_zamani_iso_utc_yazar(self):
        izleyici = _KaynakSagligi()
        izleyici.basari("mgm", 0.05)

        d = izleyici.ozet()["mgm"]

        self.assertEqual(d["durum"], "ok")
        self.assertRegex(d["son_basarili"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertGreaterEqual(d["son_basarili_yas_saniye"], 0)
        self.assertEqual(d["son_gecikme_ms"], 50.0)

    def test_ardisik_hata_esigine_gore_kararsiz_sonra_hata(self):
        izleyici = _KaynakSagligi(hata_esigi=3)
        izleyici.basari("open-meteo", 0.01)

        izleyici.hata("open-meteo", 0.01, "ConnectionError")
        self.assertEqual(izleyici.ozet()["open-meteo"]["durum"], "kararsiz")
        izleyici.hata("open-meteo", 0.01, "ConnectionError")
        self.assertEqual(izleyici.ozet()["open-meteo"]["durum"], "kararsiz")
        izleyici.hata("open-meteo", 0.01, "Timeout")

        d = izleyici.ozet()["open-meteo"]
        self.assertEqual(d["durum"], "hata")
        self.assertEqual(d["ardisik_hata"], 3)
        self.assertEqual(d["son_hata_turu"], "Timeout")

    def test_basari_ardisik_hatayi_sifirlar_ama_toplamlar_korunur(self):
        izleyici = _KaynakSagligi(hata_esigi=2)
        izleyici.hata("nominatim", 0.01, "Timeout")
        izleyici.hata("nominatim", 0.01, "Timeout")
        self.assertEqual(izleyici.ozet()["nominatim"]["durum"], "hata")

        izleyici.basari("nominatim", 0.01)

        d = izleyici.ozet()["nominatim"]
        self.assertEqual(d["durum"], "ok")
        self.assertEqual(d["ardisik_hata"], 0)
        self.assertEqual((d["basarili_toplam"], d["hata_toplam"]), (1, 2))
        self.assertIsNotNone(d["son_hata"])  # son hata zamanı geçmiş olarak kalır

    def test_kaynaklar_birbirinden_bagimsiz(self):
        izleyici = _KaynakSagligi(hata_esigi=1)
        izleyici.hata("mgm", 0.01, "Timeout")

        ozet = izleyici.ozet()

        self.assertEqual(ozet["mgm"]["durum"], "hata")
        self.assertEqual(ozet["open-meteo"]["durum"], "bilinmiyor")

    def test_ortalama_gecikme_ustel_hareketli_ortalama(self):
        izleyici = _KaynakSagligi()
        izleyici.basari("mgm", 0.100)
        self.assertEqual(izleyici.ozet()["mgm"]["ortalama_gecikme_ms"], 100.0)

        izleyici.basari("mgm", 0.200)

        d = izleyici.ozet()["mgm"]
        self.assertEqual(d["son_gecikme_ms"], 200.0)
        self.assertEqual(d["ortalama_gecikme_ms"], 120.0)  # 0.2*200 + 0.8*100

    def test_hata_turu_http_kodunu_ve_sinif_adini_dondurur(self):
        cevap = requests.Response()
        cevap.status_code = 503
        http_hatasi = requests.HTTPError("503 Server Error", response=cevap)
        try:
            raise MGMWeatherError("x") from http_hatasi
        except MGMWeatherError as exc:
            self.assertEqual(_KaynakSagligi.hata_turu(exc), "http_503")

        try:
            raise MGMWeatherError("x") from requests.ConnectTimeout("t")
        except MGMWeatherError as exc:
            self.assertEqual(_KaynakSagligi.hata_turu(exc), "ConnectTimeout")

        dogrudan = MGMWeatherError("MGM servisi beklenmeyen durum kodu döndürdü: 502")
        dogrudan.durum_kodu = 502
        self.assertEqual(_KaynakSagligi.hata_turu(dogrudan), "http_502")

    def test_hata_turu_mesaji_sizdirmaz(self):
        # Mesajlar URL/koordinat içerebilir; tür bilgisi bunları taşımamalı.
        try:
            raise MGMWeatherError("https://x/reverse?lat=40.123456&lon=29.654321") from (
                requests.ConnectionError("lat=40.123456")
            )
        except MGMWeatherError as exc:
            tur = _KaynakSagligi.hata_turu(exc)

        self.assertEqual(tur, "ConnectionError")
        self.assertNotIn("40.123456", tur)

    def test_eszamanli_kayitlar_kaybolmaz(self):
        izleyici = _KaynakSagligi()

        def calis():
            for _ in range(200):
                izleyici.basari("mgm", 0.001)
                izleyici.hata("mgm", 0.001, "Timeout")

        ipler = [threading.Thread(target=calis) for _ in range(8)]
        for ip in ipler:
            ip.start()
        for ip in ipler:
            ip.join()

        d = izleyici.ozet()["mgm"]
        self.assertEqual(d["basarili_toplam"], 1600)
        self.assertEqual(d["hata_toplam"], 1600)

    def test_ozet_kopya_doner_dis_degisiklik_izleyiciyi_etkilemez(self):
        izleyici = _KaynakSagligi()
        izleyici.ozet()["mgm"]["durum"] = "hata"
        self.assertEqual(izleyici.ozet()["mgm"]["durum"], "bilinmiyor")


# ---------------------------------------------------------------- sahte dış dünya


class _Yanit:
    def __init__(self, yuk, kod=200, metin=""):
        self.status_code = kod
        self._yuk = yuk
        self.text = metin

    def raise_for_status(self):
        if self.status_code != 200:
            raise requests.HTTPError(f"{self.status_code} hata", response=self)

    def json(self):
        return self._yuk


class _Session:
    """URL parçasına göre davranan sahte session; davranış yük | Exception |
    _Yanit | callable(params) olabilir ve test sırasında değiştirilebilir."""

    def __init__(self, davranislar):
        self.davranislar = dict(davranislar)
        self.calls = []

    def ayarla(self, parca, davranis):
        self.davranislar[parca] = davranis

    def sayi(self, parca):
        return sum(1 for url, _ in list(self.calls) if parca in url)

    def get(self, url, params=None, **kwargs):
        self.calls.append((url, dict(params or {})))
        for parca, davranis in list(self.davranislar.items()):
            if parca in url:
                if callable(davranis):
                    davranis = davranis(params)
                if isinstance(davranis, Exception):
                    raise davranis
                if isinstance(davranis, _Yanit):
                    return davranis
                return _Yanit(davranis)
        raise AssertionError(f"Beklenmeyen URL: {url}")


_ISTASYON = [{"il": "Ankara", "ilce": "Merkez", "istasyonId": 1, "enlem": 39.9, "boylam": 32.8}]
_OPEN_METEO = {
    "current": {
        "temperature_2m": 20.0,
        "relative_humidity_2m": 50,
        "wind_speed_10m": 5.0,
        "wind_direction_10m": 90,
        "surface_pressure": 1010.0,
        "pressure_msl": 1013.0,
        "weather_code": 1,
        "time": "2026-08-15T09:00",
    }
}


def _kesinti(mesaj="bağlantı reddedildi"):
    return requests.ConnectionError(mesaj)


def _istemci(session, **kwargs):
    ayarlar = {"timeout": 1, "retry_total": 0, "cache_ttl_seconds": 60}
    ayarlar.update(kwargs)
    istemci = MGMWeather(**ayarlar)
    istemci.session = session
    return istemci


def _bekle(kosul, deneme=100, aralik=0.02):
    for _ in range(deneme):
        if kosul():
            return True
        time.sleep(aralik)
    return False


# ---------------------------------------------------------------- 2) istemci entegrasyonu


class TestKaynakSagligiIstemci(unittest.TestCase):
    def test_mgm_basarisi_yalnizca_mgm_kaynagina_yazilir(self):
        istemci = _istemci(_Session({"merkezler": _ISTASYON}))

        istemci.il_istasyonlari("Ankara")

        ozet = istemci.kaynak_saglik_ozeti()
        self.assertEqual(ozet["mgm"]["durum"], "ok")
        self.assertEqual(ozet["mgm"]["basarili_toplam"], 1)
        self.assertEqual(ozet["open-meteo"]["durum"], "bilinmiyor")

    def test_cache_isabeti_gercek_istek_sayilmaz(self):
        session = _Session({"merkezler": _ISTASYON})
        istemci = _istemci(session)

        istemci.il_istasyonlari("Ankara")
        istemci.il_istasyonlari("Ankara")  # taze cache

        self.assertEqual(session.sayi("merkezler"), 1)
        self.assertEqual(istemci.kaynak_saglik_ozeti()["mgm"]["basarili_toplam"], 1)

    def test_baglanti_hatasi_kaydedilir_ve_esikte_hata_olur(self):
        istemci = _istemci(_Session({"merkezler": _kesinti()}))

        for il in ("Ankara", "Izmir"):
            with self.assertRaises(MGMWeatherError):
                istemci.il_istasyonlari(il)
        self.assertEqual(istemci.kaynak_saglik_ozeti()["mgm"]["durum"], "kararsiz")

        with self.assertRaises(MGMWeatherError):
            istemci.il_istasyonlari("Bursa")

        d = istemci.kaynak_saglik_ozeti()["mgm"]
        self.assertEqual(d["durum"], "hata")  # varsayılan eşik: 3 ardışık hata
        self.assertEqual(d["son_hata_turu"], "ConnectionError")
        self.assertEqual(d["hata_toplam"], 3)

    def test_mgm_http_hata_kodu_turde_gorunur(self):
        istemci = _istemci(_Session({"merkezler": _Yanit(None, kod=503)}))

        with self.assertRaises(MGMWeatherError):
            istemci.il_istasyonlari("Ankara")

        self.assertEqual(istemci.kaynak_saglik_ozeti()["mgm"]["son_hata_turu"], "http_503")

    def test_open_meteo_arizasi_mgm_sagligini_etkilemez(self):
        session = _Session({"merkezler": _ISTASYON, "api.open-meteo.com": _kesinti()})
        istemci = _istemci(session)
        istemci.il_istasyonlari("Ankara")

        with self.assertRaises(MGMWeatherError):
            istemci._open_meteo_guncel_durum(39.9, 32.8)

        ozet = istemci.kaynak_saglik_ozeti()
        self.assertEqual(ozet["open-meteo"]["durum"], "kararsiz")
        self.assertEqual(ozet["open-meteo"]["hata_toplam"], 1)
        self.assertEqual(ozet["mgm"]["durum"], "ok")

    def test_konum_akisinda_her_kaynak_kendi_etiketine_yazilir(self):
        # Nominatim çöker -> MGM'ye hiç gidilmez -> Open-Meteo'ya düşülür
        session = _Session(
            {"nominatim.openstreetmap.org": _kesinti(), "api.open-meteo.com": _OPEN_METEO}
        )
        istemci = _istemci(session)

        sonuc = istemci.hava_durumu_konum(41.0, 29.0)

        self.assertEqual(sonuc["yontem"], "open-meteo-dogrudan")
        ozet = istemci.kaynak_saglik_ozeti()
        self.assertEqual(ozet["nominatim"]["hata_toplam"], 1)
        self.assertEqual(ozet["open-meteo"]["durum"], "ok")
        self.assertEqual(ozet["mgm"]["durum"], "bilinmiyor")

    def test_hata_mesajindaki_koordinat_ozete_sizmaz(self):
        session = _Session(
            {
                "nominatim.openstreetmap.org": _kesinti("GET /reverse?lat=40.123456&lon=29.654321"),
                "api.open-meteo.com": _OPEN_METEO,
            }
        )
        istemci = _istemci(session)

        istemci.hava_durumu_konum(40.123456, 29.654321)

        govde = json.dumps(istemci.kaynak_saglik_ozeti())
        self.assertNotIn("40.123456", govde)
        self.assertNotIn("29.654321", govde)
        self.assertNotIn("reverse", govde)

    def test_piri_reis_ayri_kaynak_olarak_izlenir_mgme_karismaz(self):
        istemci = _istemci(_Session({"pirireis.mgm.gov.tr": _kesinti()}))

        with self.assertRaises(MGMWeatherError):
            istemci._piri_reis_deniz_istasyonlari()

        ozet = istemci.kaynak_saglik_ozeti()
        self.assertEqual(ozet["piri-reis"]["hata_toplam"], 1)
        self.assertEqual(ozet["mgm"]["durum"], "bilinmiyor")

    def test_acik_devre_atlanan_istekler_kaynak_hatasi_sayilmaz(self):
        istemci = _istemci(
            _Session({"merkezler": _kesinti()}),
            circuit_breaker_failure_threshold=1,
            circuit_breaker_open_seconds=60,
        )
        with self.assertRaises(MGMWeatherError):
            istemci.il_istasyonlari("Ankara")  # devreyi açar
        self.assertEqual(istemci.kaynak_saglik_ozeti()["mgm"]["hata_toplam"], 1)

        with self.assertRaises(MGMCircuitOpenError):
            istemci.il_istasyonlari("Izmir")  # ağa gitmeden reddedilir

        self.assertEqual(istemci.kaynak_saglik_ozeti()["mgm"]["hata_toplam"], 1)

    def test_swr_arka_plan_yenilemesi_de_kaydedilir(self):
        session = _Session({"merkezler": _ISTASYON})
        istemci = _istemci(session, cache_ttl_seconds=1, stale_while_revalidate_seconds=300)
        istemci.il_istasyonlari("Ankara")
        time.sleep(1.2)  # TTL doldu, SWR penceresinde

        istemci.il_istasyonlari("Ankara")  # bayat veri döner, arka planda yenilenir

        self.assertTrue(
            _bekle(lambda: istemci.kaynak_saglik_ozeti()["mgm"]["basarili_toplam"] == 2)
        )

    def test_hata_esigi_yapilandirilabilir(self):
        istemci = _istemci(_Session({"merkezler": _kesinti()}), kaynak_saglik_hata_esigi=1)

        with self.assertRaises(MGMWeatherError):
            istemci.il_istasyonlari("Ankara")

        self.assertEqual(istemci.kaynak_saglik_ozeti()["mgm"]["durum"], "hata")


# ---------------------------------------------------------------- 3) endpoint + metrikler


class TestHealthKaynaklarEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        self.original_mgm = app_module.mgm
        app_module.RATE_LIMIT_BUCKETS.clear()

    def tearDown(self):
        app_module.mgm = self.original_mgm
        app_module.RATE_LIMIT_MAX = 60
        app_module.RATE_LIMIT_BUCKETS.clear()

    def kur(self, session, **kwargs):
        istemci = _istemci(session, **kwargs)
        app_module.mgm = MGMAdapter(istemci)
        return istemci

    def test_yanit_sozlesmesi_ve_tum_kaynaklar_listelenir(self):
        self.kur(_Session({"merkezler": _ISTASYON}))

        resp = self.client.get("/health/kaynaklar")

        self.assertEqual(resp.status_code, 200)
        veri = resp.get_json()
        self.assertTrue(veri["basarili"])
        self.assertEqual(veri["durum"], "ok")
        self.assertEqual(veri["servis"], "hava-durumu")
        self.assertEqual(veri["hatali_kaynaklar"], [])
        self.assertEqual(set(veri["kaynaklar"]), set(KAYNAKLAR))
        self.assertEqual(veri["kaynaklar"]["mgm"]["durum"], "bilinmiyor")  # henüz trafik yok

    def test_trafik_sonrasi_kaynak_ok_gorunur(self):
        self.kur(_Session({"merkezler": _ISTASYON}))
        self.client.get("/istasyonlar/Ankara")

        veri = self.client.get("/health/kaynaklar").get_json()

        self.assertEqual(veri["kaynaklar"]["mgm"]["durum"], "ok")
        self.assertEqual(veri["kaynaklar"]["mgm"]["basarili_toplam"], 1)

    def test_kaynak_hataya_dusunce_degraded_ama_http_200_kalir(self):
        self.kur(_Session({"merkezler": _kesinti()}))
        for il in ("Ankara", "Izmir", "Bursa"):
            self.assertEqual(self.client.get(f"/istasyonlar/{il}").status_code, 404)

        resp = self.client.get("/health/kaynaklar")

        # Bilgilendirme amaçlı: 3. taraf kesintisi probe'u kırmamalı
        self.assertEqual(resp.status_code, 200)
        veri = resp.get_json()
        self.assertEqual(veri["durum"], "degraded")
        self.assertEqual(veri["hatali_kaynaklar"], ["mgm"])
        self.assertEqual(veri["kaynaklar"]["mgm"]["son_hata_turu"], "ConnectionError")

    def test_kararsiz_kaynak_tek_basina_degraded_yapmaz(self):
        self.kur(_Session({"merkezler": _kesinti()}))
        self.client.get("/istasyonlar/Ankara")

        veri = self.client.get("/health/kaynaklar").get_json()

        self.assertEqual(veri["kaynaklar"]["mgm"]["durum"], "kararsiz")
        self.assertEqual(veri["durum"], "ok")

    def test_rate_limite_tabi_degil(self):
        self.kur(_Session({}))
        app_module.RATE_LIMIT_MAX = 1
        app_module.RATE_LIMIT_BUCKETS.clear()

        for _ in range(5):
            resp = self.client.get("/health/kaynaklar")
            self.assertEqual(resp.status_code, 200)
            self.assertNotIn("X-RateLimit-Limit", resp.headers)

    def test_etag_uretilmez(self):
        self.kur(_Session({}))

        resp = self.client.get("/health/kaynaklar")

        self.assertNotIn("ETag", resp.headers)

    def test_health_sozlesmesi_degismedi(self):
        self.kur(_Session({}))

        veri = self.client.get("/health").get_json()

        self.assertNotIn("kaynaklar", veri)
        self.assertEqual(veri["durum"], "ok")

    def test_metrikler_kaynak_bazinda_uretilir(self):
        session = _Session({"merkezler": _ISTASYON, "api.open-meteo.com": _kesinti()})
        istemci = self.kur(session)
        self.client.get("/istasyonlar/Ankara")
        with self.assertRaises(MGMWeatherError):
            istemci._open_meteo_guncel_durum(39.9, 32.8)

        govde = self.client.get("/metrics").get_data(as_text=True)

        self.assertIn('mgm_source_requests_total{kaynak="mgm",sonuc="ok"}', govde)
        self.assertIn('mgm_source_requests_total{kaynak="open-meteo",sonuc="hata"}', govde)
        self.assertIn('mgm_source_up{kaynak="mgm"} 1.0', govde)
        self.assertIn('mgm_source_up{kaynak="open-meteo"} 0.0', govde)
        self.assertIn('mgm_source_request_duration_seconds_bucket{kaynak="mgm"', govde)
        self.assertIn('mgm_source_last_success_timestamp_seconds{kaynak="mgm"}', govde)


if __name__ == "__main__":
    unittest.main()
