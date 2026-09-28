"""
tests/test_e2e_resilience.py

Uçtan uca (HTTP -> Flask -> MGMAdapter -> GERÇEK MGMWeather -> sahte session)
dayanıklılık testleri. Mevcut testlerin bıraktığı boşluğu kapatır:

  - test_app_integration.py  -> app katmanı, ama `mgm` yerine FakeMGM (cache/breaker yok)
  - test_mgm_client.py       -> gerçek MGMWeather, ama HTTP/Flask katmanı yok

Burada ikisi birleşir: gerçek cache/SWR/LKG/circuit breaker/fallback zinciri,
yalnızca dış dünya (`client.session`) sahte. Konvansiyonlar mevcut testlerle aynı:
unittest, sahte `session.get`, kısa TTL + gerçek `time.sleep`, Prometheus sayaçlarında
mutlak değer yerine fark kontrolü.

Repo'nun tamamı (mgm_client/ paketi dahil) klonlanıp bu dosya gerçek kodla
çalıştırıldı: 23/23 geçiyor, repo'nun geri kalan testleriyle birlikte 422/422.
Kaynak: tests/ klasörüne bu haliyle konur, `python -m unittest discover -s tests`
"""

import threading
import time
import unittest

import requests

import app as app_module
from mgm_client import CACHE_SONUC_SAYAC, MGMWeather
from weather_provider import MGMAdapter

# ----------------------------------------------------------------


class _DummyResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _E2ESession:
    """URL'nin içerdiği parçaya göre davranan, testin ortasında yeniden
    ayarlanabilen sahte session.

    davranislar: {url_parcasi: yük | Exception | callable(params) -> (yük | Exception)}
    """

    def __init__(self, davranislar):
        self.davranislar = dict(davranislar)
        self.calls: list[tuple[str, dict]] = []

    def ayarla(self, parca, davranis):
        self.davranislar[parca] = davranis

    def sayi(self, parca):
        return sum(1 for url, _ in list(self.calls) if parca in url)

    def get(self, url, params=None, **kwargs):
        params = dict(params or {})
        self.calls.append((url, params))
        for parca, davranis in list(self.davranislar.items()):
            if parca in url:
                if callable(davranis):
                    davranis = davranis(params)
                if isinstance(davranis, Exception):
                    raise davranis
                return _DummyResponse(davranis)
        raise AssertionError(f"Beklenmeyen URL: {url} params={params}")


def _kesinti():
    return requests.ConnectionError("bağlantı reddedildi (simülasyon)")


def _istasyon_yuku(isaret):
    """`merkezler` yanıtı. `isaret`, yanıtın hangi sürümden geldiğini gövdede
    ayırt etmek için ilçe alanına yazılır (ör. eski veri "V1", yenilenen "V2")."""
    return [{"il": "Ankara", "ilce": isaret, "istasyonId": 17130, "enlem": 39.9, "boylam": 32.8}]


_BAKIRKOY = [
    {"il": "İstanbul", "ilce": "Bakırköy", "istasyonId": 17062, "enlem": 40.98, "boylam": 28.87}
]
_SONDURUM_MGM = [{"sicaklik": 25.0, "hadiseKodu": "A", "veriZamani": "x"}]
_GUN_DOGUMU = {
    "results": {
        "sunrise": "2026-08-15T03:00:00+00:00",
        "sunset": "2026-08-15T16:00:00+00:00",
    }
}


def _open_meteo_yuku(sicaklik=19.9):
    return {
        "current": {
            "temperature_2m": sicaklik,
            "relative_humidity_2m": 60,
            "wind_speed_10m": 10.5,
            "wind_direction_10m": 180,
            "surface_pressure": 1012.0,
            "pressure_msl": 1015.0,
            "weather_code": 1,
            "time": "2026-08-15T09:00",
        }
    }


def _tam_saglikli_session(**ekstra):
    davranislar = {
        "merkezler": _BAKIRKOY,
        "sondurumlar": _SONDURUM_MGM,
        "tahminler/gunluk": [],
        "sunrise-sunset": _GUN_DOGUMU,
        "api.open-meteo.com": _open_meteo_yuku(),
    }
    davranislar.update(ekstra)
    return _E2ESession(davranislar)


def _sayac(etiket):
    return CACHE_SONUC_SAYAC.labels(sonuc=etiket)._value.get()


# ----------------------------------------------------------------

# Her biri farklı cache anahtarı üretsin diye farklı iller
_ILLER = ["Ankara", "Izmir", "Bursa", "Adana", "Konya", "Antalya", "Mersin", "Kayseri"]


class _E2ETabani(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        self.original_mgm = app_module.mgm
        app_module.RATE_LIMIT_BUCKETS.clear()  # 429'lar (rate limiter) testleri etkilemesin

    def tearDown(self):
        app_module.mgm = self.original_mgm
        app_module.RATE_LIMIT_BUCKETS.clear()

    def kur(self, session, **kwargs):
        """Gerçek MGMWeather'ı sahte session ile kurup app'e bağlar."""
        ayarlar = {"timeout": 1, "retry_total": 0, "cache_ttl_seconds": 60}
        ayarlar.update(kwargs)
        istemci = MGMWeather(**ayarlar)
        istemci.session = session
        app_module.mgm = MGMAdapter(istemci)
        return istemci

    def devre(self):
        return self.client.get("/health").get_json()["circuit_breaker"]

    def bekle(self, kosul, deneme=100, aralik=0.02):
        for _ in range(deneme):
            if kosul():
                return True
            time.sleep(aralik)
        return False

    def devreyi_ac(self):
        """Her biri cache'te olmayan illere istek atarak, devre açılana kadar ilerler."""
        for il in _ILLER:
            self.client.get(f"/istasyonlar/{il}")
            if self.devre() == "acik":
                return
        self.fail("Devre beklenen sayıda hatadan sonra açılmadı.")


# ---------------------------------------------------------------- 1) cache katmanları


class TestE2ECacheKatmanlari(_E2ETabani):
    """Tek upstream çağrısı olan /istasyonlar/<il> üzerinden: TTL/SWR/LKG'nin
    /hava-durumu'ndaki dinamik TTL ve ayrı tahmin TTL'inden bağımsız,
    deterministik biçimde HTTP seviyesinde doğrulanması."""

    def test_normal_akis_mgm_verisi_doner(self):
        session = _E2ESession({"merkezler": _istasyon_yuku("V1")})
        self.kur(session)

        resp = self.client.get("/istasyonlar/Ankara")

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["basarili"])
        self.assertIn("V1", resp.get_data(as_text=True))
        self.assertEqual(session.sayi("merkezler"), 1)

    def test_taze_cache_hit_mgmye_gitmez(self):
        session = _E2ESession({"merkezler": _istasyon_yuku("V1")})
        self.kur(session, cache_ttl_seconds=60)

        ilk = self.client.get("/istasyonlar/Ankara")
        ikinci = self.client.get("/istasyonlar/Ankara")

        self.assertEqual(ilk.get_json(), ikinci.get_json())
        self.assertEqual(session.sayi("merkezler"), 1)

    def test_etag_ile_kosullu_get_304_doner(self):
        session = _E2ESession({"merkezler": _istasyon_yuku("V1")})
        self.kur(session)

        ilk = self.client.get("/istasyonlar/Ankara")
        etag = ilk.headers.get("ETag")
        self.assertTrue(etag)

        ikinci = self.client.get("/istasyonlar/Ankara", headers={"If-None-Match": etag})
        self.assertEqual(ikinci.status_code, 304)
        self.assertEqual(session.sayi("merkezler"), 1)

    def test_stale_pencerede_eski_veri_doner_ve_arka_planda_yenilenir(self):
        session = _E2ESession({"merkezler": _istasyon_yuku("V1")})
        self.kur(session, cache_ttl_seconds=1, stale_while_revalidate_seconds=300)

        ilk = self.client.get("/istasyonlar/Ankara")
        self.assertIn("V1", ilk.get_data(as_text=True))

        session.ayarla("merkezler", _istasyon_yuku("V2"))
        time.sleep(1.2)  # TTL doldu, SWR penceresinde

        stale_once = _sayac("stale_hit")
        ikinci = self.client.get("/istasyonlar/Ankara")
        self.assertEqual(ikinci.status_code, 200)
        self.assertIn("V1", ikinci.get_data(as_text=True))  # bekletmeden eski veri
        self.assertGreater(_sayac("stale_hit"), stale_once)

        # Arka plan yenilemesi bitince yeni veri gelmeli
        self.assertTrue(self.bekle(lambda: session.sayi("merkezler") >= 2))
        self.assertTrue(
            self.bekle(
                lambda: "V2" in self.client.get("/istasyonlar/Ankara").get_data(as_text=True),
                deneme=25,
            )
        )

    def test_stale_pencerede_es_zamanli_istekler_tek_yenileme_yapar(self):
        # Upstream'e gerçekçi bir gecikme veriyoruz: yenileme sürerken gelen
        # eşzamanlı istekler tek yenilemeye toplanmalı. Gecikmesiz sahte
        # session ile bu test zamanlamaya bağlı kırılır: _cached_get
        # stale kaydı okuduktan sonra _renew_try_lock'a gelene kadar başka bir
        # thread'in yenilemesi bitip kilidi bırakabiliyor
        def gecikmeli(_params):
            time.sleep(0.15)
            return _istasyon_yuku("V1")

        session = _E2ESession({"merkezler": gecikmeli})
        self.kur(session, cache_ttl_seconds=2, stale_while_revalidate_seconds=300)

        self.client.get("/istasyonlar/Ankara")
        onceki = session.sayi("merkezler")
        time.sleep(2.2)  # stale

        durumlar = []

        def istek():
            # Her thread kendi test_client'ını kullanır
            durumlar.append(app_module.app.test_client().get("/istasyonlar/Ankara").status_code)

        ipler = [threading.Thread(target=istek) for _ in range(8)]
        for ip in ipler:
            ip.start()
        for ip in ipler:
            ip.join()

        self.assertEqual(durumlar, [200] * 8)
        self.assertTrue(self.bekle(lambda: session.sayi("merkezler") > onceki))
        time.sleep(0.3)  # geç kalan ikinci bir yenileme olursa yakalayalım
        self.assertEqual(session.sayi("merkezler"), onceki + 1)  # 8 istek 1 yenileme

    def test_mgm_kesintisinde_ttl_ve_swr_dolunca_lkg_doner(self):
        session = _E2ESession({"merkezler": _istasyon_yuku("V1")})
        self.kur(
            session,
            cache_ttl_seconds=1,
            stale_while_revalidate_seconds=1,
            lkg_ttl_saniye=30,
            circuit_breaker_failure_threshold=50,
        )
        self.client.get("/istasyonlar/Ankara")

        session.ayarla("merkezler", _kesinti())
        time.sleep(2.3)  # TTL + SWR doldu

        lkg_once = _sayac("lkg_fallback")
        resp = self.client.get("/istasyonlar/Ankara")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("V1", resp.get_data(as_text=True))  # hata değil, son bilinen iyi değer
        self.assertGreaterEqual(_sayac("lkg_fallback"), lkg_once + 1)
        self.assertGreaterEqual(session.sayi("merkezler"), 2)  # gerçek istek denendi

    def test_lkg_omru_da_dolunca_hata_doner(self):
        session = _E2ESession({"merkezler": _istasyon_yuku("V1")})
        self.kur(
            session,
            cache_ttl_seconds=1,
            stale_while_revalidate_seconds=1,
            lkg_ttl_saniye=1,
            circuit_breaker_failure_threshold=50,
        )
        self.client.get("/istasyonlar/Ankara")

        session.ayarla("merkezler", _kesinti())
        time.sleep(3.3)  # TTL + SWR + LKG hepsi doldu

        resp = self.client.get("/istasyonlar/Ankara")

        # GÜNCELLEME GEREKEBİLİR BAK: upstream kesintisi 404 olarak dönüyor (app.py
        # `_hata_yanit(exc, 404)`). Semantik olarak 502/503 daha doğru olurdu
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(resp.get_json()["basarili"])


# ---------------------------------------------------------------- 2) tam akış + Open-Meteo


class TestE2EHavaDurumuAkisi(_E2ETabani):
    def test_saglikli_akis_mgm_kaynakli_doner_ve_open_meteo_cagrilmaz(self):
        session = _tam_saglikli_session()
        self.kur(session)

        resp = self.client.get("/hava-durumu/Istanbul?ilce=Bakirkoy")

        self.assertEqual(resp.status_code, 200)
        guncel = resp.get_json()["veri"]["guncel"]
        self.assertEqual(guncel["kaynak"], "mgm")
        self.assertEqual(guncel["sicaklik"], 25.0)
        self.assertEqual(session.sayi("open-meteo"), 0)

    def test_ikinci_istek_tum_cache_katmanlarindan_gelir(self):
        session = _tam_saglikli_session()
        self.kur(session)

        self.client.get("/hava-durumu/Istanbul?ilce=Bakirkoy")
        upstream_once = len(session.calls)
        resp = self.client.get("/hava-durumu/Istanbul?ilce=Bakirkoy")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(session.calls), upstream_once)  # hiçbir upstream çağrısı yok

    def test_sondurum_ve_tahmin_coker_open_meteoya_duser(self):
        session = _tam_saglikli_session(
            **{"sondurumlar": _kesinti(), "tahminler/gunluk": _kesinti()}
        )
        self.kur(session, cache_ttl_seconds=0)

        resp = self.client.get("/hava-durumu/Istanbul?ilce=Bakirkoy")

        self.assertEqual(resp.status_code, 200)
        veri = resp.get_json()["veri"]
        self.assertEqual(veri["guncel"]["kaynak"], "open-meteo")
        self.assertEqual(veri["guncel"]["sicaklik"], 19.9)
        self.assertEqual(veri["tahmin"], [])  # tahmin için fallback yok

    def test_mgm_ve_open_meteo_ikisi_de_coker_hata_doner(self):
        session = _tam_saglikli_session(
            **{
                "sondurumlar": _kesinti(),
                "tahminler/gunluk": _kesinti(),
                "api.open-meteo.com": _kesinti(),
            }
        )
        self.kur(session, cache_ttl_seconds=0)

        resp = self.client.get("/hava-durumu/Istanbul?ilce=Bakirkoy")

        self.assertEqual(resp.status_code, 404)  # mevcut hata eşlemesi (bkz. LKG testindeki not)
        self.assertFalse(resp.get_json()["basarili"])


# ---------------------------------------------------------------- 3) circuit breaker


class TestE2EDevreKesici(_E2ETabani):
    def test_esik_asilinca_devre_acilir_ve_mgmye_gidilmez(self):
        session = _E2ESession({"merkezler": _kesinti()})
        self.kur(session, cache_ttl_seconds=0, circuit_breaker_failure_threshold=3)

        self.assertEqual(self.devre(), "kapali")
        self.devreyi_ac()
        self.assertEqual(session.sayi("merkezler"), 3)  # tam eşik kadar gerçek deneme

        onceki = session.sayi("merkezler")
        resp = self.client.get("/istasyonlar/Trabzon")

        # 429 rate limiter'ın kodudur, devre kesicinin değil. Devre açıkken
        # istek ağa hiç gitmeden hata ile döner (şu an 404)
        self.assertNotEqual(resp.status_code, 429)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(session.sayi("merkezler"), onceki)

        metrikler = self.client.get("/metrics").get_data(as_text=True)
        self.assertIn("mgm_circuit_breaker_state 2.0", metrikler)

    def test_devre_acikken_stale_cache_kullaniciya_donmeye_devam_eder(self):
        session = _E2ESession({"merkezler": _istasyon_yuku("V1")})
        self.kur(
            session,
            cache_ttl_seconds=1,
            stale_while_revalidate_seconds=300,
            circuit_breaker_failure_threshold=1,
            circuit_breaker_open_seconds=60,
        )
        self.client.get("/istasyonlar/Ankara")  # cache'i doldur

        session.ayarla("merkezler", _kesinti())
        time.sleep(1.2)  # stale pencere

        resp = self.client.get("/istasyonlar/Ankara")
        self.assertEqual(resp.status_code, 200)  # eski veri anında döner

        # Arka plan yenilemesi başarısız olunca devre açılır
        self.assertTrue(self.bekle(lambda: self.devre() == "acik"))

        resp = self.client.get("/istasyonlar/Ankara")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("V1", resp.get_data(as_text=True))

        # Cache'te olmayan bir yer ise ağa gitmeden hata almalı
        onceki = session.sayi("merkezler")
        self.assertEqual(self.client.get("/istasyonlar/Izmir").status_code, 404)
        self.assertEqual(session.sayi("merkezler"), onceki)

    def test_yari_acik_deneme_basarili_olursa_devre_kapanir(self):
        session = _E2ESession({"merkezler": _kesinti()})
        self.kur(
            session,
            cache_ttl_seconds=0,
            circuit_breaker_failure_threshold=2,
            circuit_breaker_open_seconds=0.3,
        )
        self.devreyi_ac()

        session.ayarla("merkezler", _istasyon_yuku("V1"))  # MGM düzeldi
        time.sleep(0.4)  # open_seconds doldu -> yarı açık

        resp = self.client.get("/istasyonlar/Mersin")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.devre(), "kapali")

    def test_yari_acik_deneme_basarisiz_olursa_devre_tekrar_acilir(self):
        session = _E2ESession({"merkezler": _kesinti()})
        self.kur(
            session,
            cache_ttl_seconds=0,
            circuit_breaker_failure_threshold=1,
            circuit_breaker_open_seconds=0.3,
        )
        self.devreyi_ac()
        onceki = session.sayi("merkezler")

        time.sleep(0.4)  # yarı açık
        resp = self.client.get("/istasyonlar/Mersin")  # MGM hâlâ çökük

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(session.sayi("merkezler"), onceki + 1)  # tek bir deneme (probe)
        self.assertEqual(self.devre(), "acik")

    def test_health_deep_devre_acikken_503_ve_degraded_doner(self):
        session = _E2ESession({"merkezler": _kesinti()})
        self.kur(
            session,
            cache_ttl_seconds=0,
            circuit_breaker_failure_threshold=1,
            circuit_breaker_open_seconds=60,
        )
        self.devreyi_ac()

        resp = self.client.get("/health?deep=1")

        self.assertEqual(resp.status_code, 503)
        veri = resp.get_json()
        self.assertEqual(veri["durum"], "degraded")
        self.assertEqual(veri["mgm"], "hata")
        self.assertEqual(veri["circuit_breaker"], "acik")

    def test_health_deep_saglikliyken_ok_doner(self):
        session = _E2ESession({"merkezler": _istasyon_yuku("V1")})
        self.kur(session)

        resp = self.client.get("/health?deep=1")

        self.assertEqual(resp.status_code, 200)
        veri = resp.get_json()
        self.assertEqual(veri["mgm"], "ok")
        self.assertEqual(veri["circuit_breaker"], "kapali")


# ---------------------------------------------------------------- 4) Nominatim
#
# Desenler TestKonumCozumleyici'den birebir alındı


def _konum_session(nominatim_adres=None, nominatim_hata=False, merkezler_davranisi=None, **ekstra):
    """test_mgm_client.py::_KonumSession ile aynı sözleşme: nominatim_adres=None
    -> adres bulunamadı, nominatim_hata=True -> Nominatim'e bağlanılamadı."""
    def nominatim(_params=None):
        if nominatim_hata:
            return _kesinti()
        if nominatim_adres is None:
            return {}  # "address" alanı yok
        return {"address": nominatim_adres}

    def merkezler(params):
        anahtar = (params.get("il"), params.get("ilce"))
        return (merkezler_davranisi or {}).get(anahtar, [])

    davranislar = {
        "nominatim.openstreetmap.org": nominatim,
        "merkezler": merkezler,
        "sondurumlar": _SONDURUM_MGM,
        "tahminler/gunluk": [],
        "sunrise-sunset": _GUN_DOGUMU,
        "api.open-meteo.com": _open_meteo_yuku(),
    }
    davranislar.update(ekstra)
    return _E2ESession(davranislar)


class TestE2ENominatim(_E2ETabani):
    def test_konum_nominatim_ve_mgm_ile_cozulur(self):
        # test_nominatim_mgmde_bulunan_ilceyi_dogru_cozer ile aynı senaryo
        session = _konum_session(
            nominatim_adres={"state": "İstanbul", "county": "Kadıköy"},
            merkezler_davranisi={
                ("istanbul", "kadikoy"): [
                    {"il": "İstanbul", "ilce": "Kadıköy", "istasyonId": 93409,
                     "enlem": 40.99, "boylam": 29.02}
                ],
            },
        )
        self.kur(session, cache_ttl_seconds=0)

        resp = self.client.get("/konum?lat=40.99&lon=29.02")

        self.assertEqual(resp.status_code, 200)
        veri = resp.get_json()["veri"]
        self.assertEqual(veri["durum"], "cozuldu")
        self.assertEqual(veri["yontem"], "nominatim-mgm")
        self.assertEqual(veri["il"], "İstanbul")
        self.assertEqual(veri["ilce"], "Kadıköy")
        self.assertEqual(veri["guncel"]["kaynak"], "mgm")

    def test_nominatim_adres_bulamazsa_dogrudan_open_meteoya_duser(self):
        # test_nominatim_adres_bulamazsa_dogrudan_open_meteoya_duser
        session = _konum_session(nominatim_adres=None)
        self.kur(session, cache_ttl_seconds=0)

        resp = self.client.get("/konum?lat=36.0&lon=30.0")

        self.assertEqual(resp.status_code, 200)
        veri = resp.get_json()["veri"]
        self.assertEqual(veri["yontem"], "open-meteo-dogrudan")
        self.assertIsNone(veri["il"])
        self.assertEqual(veri["guncel"]["kaynak"], "open-meteo")

    def test_nominatim_cokerse_mgmye_hic_gidilmeden_open_meteoya_duser(self):
        # test_nominatim_cokerse_mgm_denenmeden_open_meteoya_duser
        session = _konum_session(nominatim_hata=True)
        self.kur(session, cache_ttl_seconds=0)

        resp = self.client.get("/konum?lat=41.0&lon=29.0")

        self.assertEqual(resp.status_code, 200)  # 5xx'e çökmemeli
        veri = resp.get_json()["veri"]
        self.assertEqual(veri["yontem"], "open-meteo-dogrudan")
        self.assertEqual(session.sayi("merkezler"), 0)  # MGM'ye hiç gidilmedi

    def test_nominatim_sinir_disi_il_verirse_open_meteoya_duser(self):
        # test_il_mgmnin_81_il_listesiyle_eslesmezse_open_meteoya_duser: Nominatim
        # bir adres buluyor ama 81 il listesiyle eşleşmiyor -> ayrı "yontem" etiketi
        session = _konum_session(nominatim_adres={"state": "Attiki", "county": "Athens"})
        self.kur(session, cache_ttl_seconds=0)

        resp = self.client.get("/konum?lat=37.98&lon=23.72")

        self.assertEqual(resp.status_code, 200)
        veri = resp.get_json()["veri"]
        self.assertEqual(veri["yontem"], "nominatim-open-meteo")
        self.assertEqual(veri["guncel"]["kaynak"], "open-meteo")

    def test_ayni_koordinat_nominatime_tekrar_gitmez(self):
        # Doğrulandı: _nominatim_ters_geocode sonucu _cached_get ile
        # "nominatim-reverse" anahtarında cache'leniyor
        session = _konum_session(
            nominatim_adres={"state": "İstanbul", "county": "Kadıköy"},
            merkezler_davranisi={("istanbul", "kadikoy"): [
                {"il": "İstanbul", "ilce": "Kadıköy", "istasyonId": 93409,
                 "enlem": 40.99, "boylam": 29.02}
            ]},
        )
        self.kur(session, cache_ttl_seconds=60)

        self.client.get("/konum?lat=40.99&lon=29.02")
        self.client.get("/konum?lat=40.99&lon=29.02")

        self.assertEqual(session.sayi("nominatim.openstreetmap.org"), 1)

    def test_nominatim_hatalari_mgm_devresini_acmaz(self):
        # Doğrulandı: devre kesici yalnızca MGMWeather._get() içinde (MGM
        # çağrılarında) devreye giriyor; Nominatim/Open-Meteo doğrudan
        # session.get çağırdığı için bu hatalar breaker'a hiç yazılmıyor
        session = _konum_session(nominatim_hata=True)
        self.kur(session, cache_ttl_seconds=60, circuit_breaker_failure_threshold=3)

        for i in range(6):  # her seferinde farklı koordinat -> cache'e takılmaz
            resp = self.client.get(f"/konum?lat=41.{i}&lon=29.{i}")
            self.assertEqual(resp.status_code, 200)

        self.assertEqual(self.devre(), "kapali")


if __name__ == "__main__":
    unittest.main()
