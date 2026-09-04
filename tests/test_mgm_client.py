import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import requests

from mgm_client import (
    CACHE_SONUC_SAYAC,
    MGMCircuitOpenError,
    MGMWeather,
    MGMWeatherError,
    _tr_normalize,
    turkiye_illeri,
)


class _DummyResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _CountingSession:
    def __init__(self, payload):
        self.calls = 0
        self.payload = payload

    def get(self, url, **kwargs):
        self.calls += 1
        return _DummyResponse(self.payload)


class TestMGMClientUnit(unittest.TestCase):
    def test_tr_normalize_ascii_cevirir(self):
        self.assertEqual(_tr_normalize("İstanbul"), "istanbul")
        self.assertEqual(_tr_normalize(" Üsküdar "), "uskudar")

    def test_turkiye_illeri_81_il_plaka_kodu_sirali(self):
        illeri = turkiye_illeri()
        self.assertEqual(len(illeri), 81)
        plaka_kodlari = [kayit["plakaKodu"] for kayit in illeri]
        self.assertEqual(plaka_kodlari, list(range(1, 82)))
        self.assertEqual(illeri[0]["il"], "Adana")
        self.assertEqual(illeri[33]["il"], "İstanbul")

    def test_turkiye_illeri_kopya_doner(self):
        ilk = turkiye_illeri()
        ilk[0]["il"] = "DEĞİŞTİRİLDİ"
        ikinci = turkiye_illeri()
        self.assertEqual(ikinci[0]["il"], "Adana")

    def test_retry_ayarlari_sessiona_uygulanir(self):
        client = MGMWeather(timeout=7, retry_total=4, retry_backoff=0.5)
        https_adapter = client.session.adapters["https://"]
        retries = https_adapter.max_retries

        self.assertEqual(retries.total, 4)
        self.assertEqual(retries.connect, 4)
        self.assertEqual(retries.read, 4)
        self.assertEqual(retries.status, 4)
        self.assertEqual(retries.backoff_factor, 0.5)

    def test_cache_aktifken_ayni_istek_tek_kez_yapilir(self):
        payload = [{"istasyonId": 1}]
        client = MGMWeather(cache_ttl_seconds=60)
        fake_session = _CountingSession(payload)
        client.session = fake_session

        ilk = client._get("merkezler", {"il": "ankara"})
        ikinci = client._get("merkezler", {"il": "ankara"})

        self.assertEqual(fake_session.calls, 1)
        self.assertEqual(ilk, payload)
        self.assertEqual(ikinci, payload)

    def test_cache_kapaliyken_istek_tekrarlanir(self):
        payload = [{"istasyonId": 1}]
        client = MGMWeather(cache_ttl_seconds=0)
        fake_session = _CountingSession(payload)
        client.session = fake_session

        client._get("merkezler", {"il": "ankara"})
        client._get("merkezler", {"il": "ankara"})

        self.assertEqual(fake_session.calls, 2)

    def test_gun_dogumu_batimi_ayni_konum_icin_cache_lenir(self):
        payload = {
            "results": {
                "sunrise": "2026-08-14T04:00:00+00:00",
                "sunset": "2026-08-14T18:30:00+00:00",
            }
        }
        client = MGMWeather(cache_ttl_seconds=3600)
        fake_session = _CountingSession(payload)
        client.session = fake_session

        ilk = client.gun_dogumu_batimi(40.98, 29.02)
        ikinci = client.gun_dogumu_batimi(40.98, 29.02)

        self.assertEqual(fake_session.calls, 1)
        self.assertEqual(ilk, ikinci)
        self.assertEqual(ilk["gunDogumu"], "07:00")
        self.assertEqual(ilk["gunBatimi"], "21:30")

    def test_gun_dogumu_batimi_farkli_konumda_istek_tekrarlanir(self):
        payload = {
            "results": {
                "sunrise": "2026-08-14T04:00:00+00:00",
                "sunset": "2026-08-14T18:30:00+00:00",
            }
        }
        client = MGMWeather(cache_ttl_seconds=3600)
        fake_session = _CountingSession(payload)
        client.session = fake_session

        client.gun_dogumu_batimi(40.98, 29.02)
        client.gun_dogumu_batimi(41.02, 28.97)

        self.assertEqual(fake_session.calls, 2)


class _SinirliYanitliSession:
    """İlk N istekte farklı, sonrasında aynı yükü döndüren sahte oturum.

    SWR akışını test etmek için `_cached_get`'ten gelen istek sayısını ve
    dönen veriyi gözlemlemeye yarar.
    """

    def __init__(self, yukler):
        self.yukler = list(yukler)
        self.calls = 0

    def get(self, url, **kwargs):
        yuk = self.yukler[min(self.calls, len(self.yukler) - 1)]
        self.calls += 1
        return _DummyResponse(yuk)


class TestStaleWhileRevalidate(unittest.TestCase):
    def _istek(self, client, path, params):
        return client._get(path, params)

    def test_stale_iken_eski_veri_doner_ve_arka_planda_yenilenir(self):
        eski_yuk = [{"deger": 1}]
        yeni_yuk = [{"deger": 2}]
        session = _SinirliYanitliSession([eski_yuk, yeni_yuk])
        client = MGMWeather(
            cache_ttl_seconds=1,
            stale_while_revalidate_seconds=300,
            timeout=1,
        )
        client.session = session
        # 1. istek: cache miss -> eski yük
        ilk = self._istek(client, "merkezler", {"il": "ankara"})
        self.assertEqual(ilk, eski_yuk)

        # TTL geçsin (1 sn) stale pencere içinde kalsın
        time.sleep(1.2)

        # 2. istek: stale -> eski veri anında döner arka planda yenileme başlar
        ikinci = self._istek(client, "merkezler", {"il": "ankara"})
        self.assertEqual(ikinci, eski_yuk)
        self.assertGreaterEqual(session.calls, 2)
        # Arka plan görevinin bitmesi için kısa bekle
        time.sleep(0.2)
        # Sonraki istek: taze veri cache'ten döner
        ucuncu = self._istek(client, "merkezler", {"il": "ankara"})
        self.assertEqual(ucuncu, yeni_yuk)

    def test_swr_kapaliyken_stale_veri_donmez(self):
        yuk = [{"deger": 1}]
        session = _SinirliYanitliSession([yuk, yuk])
        client = MGMWeather(
            cache_ttl_seconds=1,
            stale_while_revalidate_seconds=0,
            timeout=1,
        )
        client.session = session

        ilk = self._istek(client, "merkezler", {"il": "ankara"})
        time.sleep(1.2)
        ikinci = self._istek(client, "merkezler", {"il": "ankara"})

        # SWR kapalı: TTL dolunca bloklayıcı yeniden yükleme yapılır (2. çağrı)
        self.assertEqual(ilk, yuk)
        self.assertEqual(ikinci, yuk)
        self.assertEqual(session.calls, 2)

    def test_stale_pencere_asilinca_bloklayici_yenileme_olur(self):
        eski_yuk = [{"deger": 1}]
        yeni_yuk = [{"deger": 2}]
        session = _SinirliYanitliSession([eski_yuk, yeni_yuk])
        client = MGMWeather(
            cache_ttl_seconds=1,
            stale_while_revalidate_seconds=1,
            timeout=1,
        )
        client.session = session

        self._istek(client, "merkezler", {"il": "ankara"})
        time.sleep(2.2)  # TTL (1) + SWR (1) toplam ömür doldu

        sonuc = self._istek(client, "merkezler", {"il": "ankara"})
        self.assertEqual(sonuc, yeni_yuk)
        self.assertEqual(session.calls, 2)

    def test_yenileme_kilidi_tektir(self):
        client = MGMWeather(cache_ttl_seconds=60)
        key = "test-key"

        ilk = client._renew_try_lock(key)
        ikinci = client._renew_try_lock(key)
        self.assertTrue(ilk)
        self.assertFalse(ikinci)

        client._renew_release(key)
        ucuncu = client._renew_try_lock(key)
        self.assertTrue(ucuncu)
        client._renew_release(key)

    def test_single_flight_es_zamanli_yuklemede_tek_istek(self):
        client = MGMWeather(cache_ttl_seconds=60)
        yukleme_sayisi = 0
        kilit = threading.Lock()

        def yavas_loader():
            nonlocal yukleme_sayisi
            with kilit:
                yukleme_sayisi += 1
            time.sleep(0.2)
            return [{"deger": yukleme_sayisi}]

        sonuclar = []
        hatalar = []

        def istek_atan():
            try:
                sonuclar.append(client._yukle_singleton("single-key", yavas_loader))
            except Exception as exc:
                hatalar.append(exc)

        ipler = [threading.Thread(target=istek_atan) for _ in range(5)]
        for ip in ipler:
            ip.start()
        for ip in ipler:
            ip.join()

        self.assertEqual(hatalar, [])
        self.assertEqual(yukleme_sayisi, 1)
        self.assertEqual(len(sonuclar), 5)
        self.assertTrue(all(s == sonuclar[0] for s in sonuclar))

    def test_redis_saglik_ozeti_redis_kapaliyken_skip(self):
        client = MGMWeather()
        self.assertEqual(client.redis_saglik_ozeti(), {"durum": "skip"})


class _PatlayanSession:
    """Her çağrıda bağlantı hatası fırlatan sahte oturum (MGM kesintisi)."""

    def __init__(self):
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        raise requests.ConnectionError("bağlantı reddedildi")


class _AyarlanabilirSession:
    """İlk `basarisiz_sayisi` çağrıda hata fırlatan, sonrasında başarılı
    yanıt dönen sahte oturum. Yarı açık deneme senaryolarını test etmek
    için kullanılır."""

    def __init__(self, basarisiz_sayisi, payload):
        self.basarisiz_sayisi = basarisiz_sayisi
        self.payload = payload
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        if self.calls <= self.basarisiz_sayisi:
            raise requests.ConnectionError("bağlantı reddedildi")
        return _DummyResponse(self.payload)


class TestCircuitBreaker(unittest.TestCase):
    def test_esik_asilinca_devre_acilir_ve_istek_atlanir(self):
        session = _PatlayanSession()
        client = MGMWeather(
            cache_ttl_seconds=0,
            timeout=1,
            retry_total=0,
            circuit_breaker_failure_threshold=3,
            circuit_breaker_window_seconds=30,
            circuit_breaker_open_seconds=60,
        )
        client.session = session

        for _ in range(3):
            with self.assertRaises(MGMWeatherError):
                client._get("merkezler", {"il": "ankara"})

        self.assertEqual(session.calls, 3)
        self.assertEqual(client.circuit_breaker_saglik_ozeti(), {"durum": "acik"})

        # Devre açıkken 4. çağrı ağa hiç gitmemeli, doğrudan hata dönmeli.
        with self.assertRaises(MGMCircuitOpenError):
            client._get("merkezler", {"il": "ankara"})
        self.assertEqual(session.calls, 3)

    def test_pencere_disindaki_hatalar_devreyi_actirmaz(self):
        session = _PatlayanSession()
        client = MGMWeather(
            cache_ttl_seconds=0,
            timeout=1,
            retry_total=0,
            circuit_breaker_failure_threshold=3,
            circuit_breaker_window_seconds=0.3,
            circuit_breaker_open_seconds=60,
        )
        client.session = session

        with self.assertRaises(MGMWeatherError):
            client._get("merkezler", {"il": "ankara"})
        time.sleep(0.4)  # pencere dışına çık
        with self.assertRaises(MGMWeatherError):
            client._get("merkezler", {"il": "ankara"})
        with self.assertRaises(MGMWeatherError):
            client._get("merkezler", {"il": "ankara"})

        # Pencere kaydığı için sadece son 2 hata sayılır, devre açılmaz.
        self.assertEqual(client.circuit_breaker_saglik_ozeti(), {"durum": "kapali"})
        self.assertEqual(session.calls, 3)

    def test_acik_devre_suresi_dolunca_yari_acik_deneme_basarili_olursa_kapanir(self):
        session = _AyarlanabilirSession(basarisiz_sayisi=2, payload=[{"istasyonId": 1}])
        client = MGMWeather(
            cache_ttl_seconds=0,
            timeout=1,
            retry_total=0,
            circuit_breaker_failure_threshold=2,
            circuit_breaker_window_seconds=30,
            circuit_breaker_open_seconds=0.3,
        )
        client.session = session

        for _ in range(2):
            with self.assertRaises(MGMWeatherError):
                client._get("merkezler", {"il": "ankara"})
        self.assertEqual(client.circuit_breaker_saglik_ozeti(), {"durum": "acik"})

        # open_seconds dolmadan istek atlanmaya devam eder.
        with self.assertRaises(MGMCircuitOpenError):
            client._get("merkezler", {"il": "ankara"})
        self.assertEqual(session.calls, 2)

        time.sleep(0.4)  # open_seconds doldu -> yarı açık
        sonuc = client._get("merkezler", {"il": "ankara"})
        self.assertEqual(sonuc, session.payload)
        self.assertEqual(session.calls, 3)
        self.assertEqual(client.circuit_breaker_saglik_ozeti(), {"durum": "kapali"})

    def test_yari_acik_deneme_basarisiz_olursa_devre_tekrar_acilir(self):
        session = _PatlayanSession()
        client = MGMWeather(
            cache_ttl_seconds=0,
            timeout=1,
            retry_total=0,
            circuit_breaker_failure_threshold=1,
            circuit_breaker_window_seconds=30,
            circuit_breaker_open_seconds=0.3,
        )
        client.session = session

        with self.assertRaises(MGMWeatherError):
            client._get("merkezler", {"il": "ankara"})
        self.assertEqual(client.circuit_breaker_saglik_ozeti(), {"durum": "acik"})

        time.sleep(0.4)  # yarı açık deneme hakkı doğar
        with self.assertRaises(MGMWeatherError):
            client._get("merkezler", {"il": "ankara"})
        self.assertEqual(client.circuit_breaker_saglik_ozeti(), {"durum": "acik"})
        self.assertEqual(session.calls, 2)

    def test_devre_acikken_stale_cache_verisi_donmeye_devam_eder(self):
        """Redis/in-memory cache tampon görevi görür: breaker açık olsa da
        stale-while-revalidate penceresindeki eski veri kullanıcıya
        dönmeye devam eder, sadece arka plandaki asıl ağ isteği atlanır."""
        eski_yuk = [{"deger": 1}]
        session = _PatlayanSession()
        client = MGMWeather(
            cache_ttl_seconds=1,
            stale_while_revalidate_seconds=300,
            timeout=1,
            retry_total=0,
            circuit_breaker_failure_threshold=1,
            circuit_breaker_window_seconds=30,
            circuit_breaker_open_seconds=60,
        )
        # Önce cache'i başarılı bir yanıtla doldur.
        basarili_session = _CountingSession(eski_yuk)
        client.session = basarili_session
        ilk = client._get("merkezler", {"il": "ankara"})
        self.assertEqual(ilk, eski_yuk)

        # Devreyi patlayan oturumla aç.
        client.session = session
        time.sleep(1.2)  # TTL geçsin, stale pencereye düşsün
        ikinci = client._get("merkezler", {"il": "ankara"})
        self.assertEqual(ikinci, eski_yuk)
        self.assertEqual(client.circuit_breaker_saglik_ozeti(), {"durum": "acik"})

        time.sleep(0.2)  # arka plan yenileme denemesinin bitmesini bekle
        ucuncu = client._get("merkezler", {"il": "ankara"})
        self.assertEqual(ucuncu, eski_yuk)  # devre açık: hâlâ eski veri


class _UrlBazliSession:
    """URL'nin içerdiği alt path'e göre farklı davranan sahte session.
    MGM/Open-Meteo fallback senaryolarını (aynı istek zincirinde bazı
    uçların başarılı, bazılarının başarısız olması) test etmek için."""

    def __init__(self, davranislar):
        # davranislar: {url_parcasi: yük_sözlüğü_veya_Exception}
        self.davranislar = davranislar
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        for parca, davranis in self.davranislar.items():
            if parca in url:
                if isinstance(davranis, Exception):
                    raise davranis
                return _DummyResponse(davranis)
        raise AssertionError(f"Beklenmeyen URL çağrıldı: {url}")


def _open_meteo_basarili_yuk(sicaklik=21.4):
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


class TestOpenMeteoFallback(unittest.TestCase):
    def test_mgm_basariliysa_fallback_hic_denenmez(self):
        session = _UrlBazliSession(
            {
                "sondurumlar": [{"sicaklik": 25.0, "hadiseKodu": "A", "veriZamani": "x"}],
                "open-meteo.com": _open_meteo_basarili_yuk(),
            }
        )
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        veri = client.guncel_durum_yedekli(17062, 40.98, 28.87)
        self.assertEqual(veri["kaynak"], "mgm")
        self.assertEqual(veri["sicaklik"], 25.0)
        self.assertTrue(all("open-meteo" not in u for u in session.calls))

    def test_mgm_hata_verince_open_meteo_ya_dusuluyor(self):
        session = _UrlBazliSession(
            {
                "sondurumlar": requests.ConnectionError("bağlantı reddedildi"),
                "open-meteo.com": _open_meteo_basarili_yuk(sicaklik=18.2),
            }
        )
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        veri = client.guncel_durum_yedekli(17062, 40.98, 28.87)
        self.assertEqual(veri["kaynak"], "open-meteo")
        self.assertEqual(veri["sicaklik"], 18.2)
        self.assertEqual(veri["durum"], "Genel Olarak Açık")  # WMO kod 1
        self.assertEqual(veri["istasyonId"], 17062)

    def test_mgm_hata_ve_konum_yoksa_fallback_denenmez_orijinal_hata_doner(self):
        session = _UrlBazliSession({"sondurumlar": requests.ConnectionError("x")})
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        with self.assertRaises(MGMWeatherError) as ctx:
            client.guncel_durum_yedekli(17062)  # enlem/boylam verilmedi
        self.assertIn("bağlanılamadı", str(ctx.exception))
        self.assertTrue(all("open-meteo" not in u for u in session.calls))

    def test_ikisi_de_basarisizsa_birlesik_hata_mesaji_verilir(self):
        session = _UrlBazliSession(
            {
                "sondurumlar": requests.ConnectionError("mgm çöktü"),
                "open-meteo.com": requests.ConnectionError("open-meteo de çöktü"),
            }
        )
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        with self.assertRaises(MGMWeatherError) as ctx:
            client.guncel_durum_yedekli(17062, 40.98, 28.87)
        mesaj = str(ctx.exception)
        self.assertIn("MGM", mesaj)
        self.assertIn("Open-Meteo", mesaj)

    def test_hava_durumu_mgm_cokse_bile_fallback_ile_doner(self):
        merkez_yuk = [
            {
                "il": "İstanbul",
                "ilce": "Bakırköy",
                "istasyonId": 17062,
                "enlem": 40.98,
                "boylam": 28.87,
            }
        ]
        session = _UrlBazliSession(
            {
                "merkezler": merkez_yuk,
                "sondurumlar": requests.ConnectionError("mgm çöktü"),
                "tahminler/gunluk": requests.ConnectionError("mgm çöktü"),
                "open-meteo.com": _open_meteo_basarili_yuk(sicaklik=19.9),
                "sunrise-sunset.org": {
                    "results": {
                        "sunrise": "2026-08-15T03:00:00+00:00",
                        "sunset": "2026-08-15T16:00:00+00:00",
                    }
                },
            }
        )
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.hava_durumu("İstanbul", "Bakırköy")
        self.assertEqual(sonuc["guncel"]["kaynak"], "open-meteo")
        self.assertEqual(sonuc["guncel"]["sicaklik"], 19.9)
        self.assertEqual(sonuc["tahmin"], [])  # tahmin fallback'i yok, boş liste


class TestRedisBaslangicYenidenDeneme(unittest.TestCase):
    """Render/Docker Compose gibi ortamlarda web servis ile Redis'in
    yaklaşık eşzamanlı başlatılması, ilk ping'in geçici olarak (DNS henüz
    hazır değilken) başarısız olmasına yol açabiliyor. Bu, gerçek bir
    yanlış yapılandırmadan ayırt edilmeli. Birkaç deneme sonra toparlanmalı,
    ama denemeler gerçekten tükenirse hâlâ sert şekilde hata vermeli."""

    def test_gecici_baglanti_hatasi_birkac_denemeden_sonra_toparlanir(self):
        import redis as redis_module

        sahte_client = MagicMock()
        sahte_client.ping.side_effect = [
            redis_module.exceptions.ConnectionError("dns henüz hazır değil"),
            redis_module.exceptions.ConnectionError("dns henüz hazır değil"),
            True,
        ]

        with (
            patch("redis.Redis.from_url", return_value=sahte_client),
            patch("mgm_client.REDIS_STARTUP_RETRY_DELAY_SECONDS", 0.01),
        ):
            client = MGMWeather(redis_url="redis://sahte-host:6379/0")

        self.assertTrue(client._redis_available)
        self.assertEqual(sahte_client.ping.call_count, 3)

    def test_denemeler_tukenirse_hata_firlatilir(self):
        import redis as redis_module

        sahte_client = MagicMock()
        sahte_client.ping.side_effect = redis_module.exceptions.ConnectionError(
            "gerçekten çökük"
        )

        with (
            patch("redis.Redis.from_url", return_value=sahte_client),
            patch("mgm_client.REDIS_STARTUP_RETRY_DELAY_SECONDS", 0.01),
            patch("mgm_client.REDIS_STARTUP_RETRY_ATTEMPTS", 3),self.assertRaises(MGMWeatherError) as ctx
        ):
            MGMWeather(redis_url="redis://sahte-host:6379/0")

        self.assertEqual(sahte_client.ping.call_count, 3)
        self.assertIn("3 denemeden", str(ctx.exception))


class _IlceFarkindaSession:
    """merkezler isteklerinde `il`/`ilce` parametrelerine göre farklı sahte
    yanıt döner. MGM'nin gerçek (canlıda doğrulanmış) davranışını taklit
    eder: il-only sorgu o ilin sadece varsayılan istasyonunu döner, il+ilce
    sorgusu o ilçeye özel (farklı) bir istasyon döner."""

    def __init__(self):
        self.calls: list[dict] = []

    def get(self, url, params=None, **kwargs):
        params = dict(params or {})
        self.calls.append(params)
        if "merkezler" not in url:
            raise AssertionError(f"Beklenmeyen URL: {url}")
        il, ilce = params.get("il"), params.get("ilce")
        if il == "istanbul" and not ilce:
            return _DummyResponse(
                [{"il": "İstanbul", "ilce": "Bakırköy", "istasyonId": 93401,
                  "enlem": 40.98, "boylam": 28.82}]
            )
        if il == "istanbul" and ilce == "kadikoy":
            return _DummyResponse(
                [{"il": "İstanbul", "ilce": "Kadıköy", "istasyonId": 93409,
                  "enlem": 40.99, "boylam": 29.02}]
            )
        if il == "istanbul" and ilce == "olmayanilce":
            return _DummyResponse([])  # MGM: bulunamadı, boş dizi
        raise AssertionError(f"Beklenmeyen params: {params}")


class TestIlceDogrudanSorgu(unittest.TestCase):
    """MGM'nin merkezler uç noktası il-only sorguda o ilin sadece bir
    (varsayılan) istasyonunu döner, tüm ilçelerini değil. Bu canlıda
    doğrulandı (İstanbul: il=istanbul -> yalnızca Bakırköy, ama
    il=istanbul&ilce=kadikoy -> ayrı ve doğru bir sonuç döner). Doğru
    davranış: ilce verildiğinde MGM'ye doğrudan parametre olarak
    gönderilmeli, il_istasyonlari()'nin dar listesinde client-side arama
    yapılmamalı."""

    def test_ilce_verilmezse_ilin_varsayilan_istasyonu_doner(self):
        session = _IlceFarkindaSession()
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        istasyon = client.ilce_istasyonu("istanbul")
        self.assertEqual(istasyon["ilce"], "Bakırköy")

    def test_ilce_mgmye_dogrudan_parametre_olarak_gonderilir(self):
        session = _IlceFarkindaSession()
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        istasyon = client.ilce_istasyonu("istanbul", "kadikoy")
        self.assertEqual(istasyon["ilce"], "Kadıköy")
        self.assertEqual(istasyon["istasyonId"], 93409)
        self.assertTrue(any(c.get("ilce") == "kadikoy" for c in session.calls))

    def test_mgmde_gercekten_olmayan_ilce_icin_durust_hata_verir(self):
        session = _IlceFarkindaSession()
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        with self.assertRaises(MGMWeatherError) as ctx:
            client.ilce_istasyonu("istanbul", "olmayanilce")
        mesaj = str(ctx.exception)
        # Artık "Kullanılabilir ilçe(ler): X" gibi yanlışlıkla tam liste
        # iddia eden bir ifade yok, sadece varsayılan istasyonu öneriyor.
        self.assertNotIn("Kullanılabilir ilçe(ler)", mesaj)
        self.assertIn("Bakırköy", mesaj)


class _AkilliAramaSession:
    """/ara için gereken tüm dış servisleri tek bir sahte
    oturumda birleştirir. Her testin ihtiyacına göre `merkezler_davranisi`
    ve `geocode_sonucu`/`geocode_sorgu_bazli` enjekte edilir."""

    def __init__(self, merkezler_davranisi=None, geocode_sonucu=None, geocode_sorgu_bazli=None):
        # merkezler_davranisi: {(il, ilce_veya_None): [kayit, ...]}
        # geocode_sorgu_bazli verilirse (sorgu_metni -> sonuç_listesi)
        # geocode_sonucu'ndan önceliklidir, sorgu metnine göre farklı
        # yanıt dönmek gerektiğinde kullanılır.
        self.merkezler_davranisi = merkezler_davranisi or {}
        self.geocode_sonucu = geocode_sonucu if geocode_sonucu is not None else []
        self.geocode_sorgu_bazli = geocode_sorgu_bazli or {}
        self.calls: list[tuple] = []

    def get(self, url, params=None, **kwargs):
        params = dict(params or {})
        self.calls.append((url, params))
        if "geocoding-api.open-meteo.com" in url:
            sorgu_metni = params.get("name")
            if sorgu_metni in self.geocode_sorgu_bazli:
                return _DummyResponse({"results": self.geocode_sorgu_bazli[sorgu_metni]})
            return _DummyResponse({"results": self.geocode_sonucu})
        if "merkezler" in url:
            anahtar = (params.get("il"), params.get("ilce"))
            return _DummyResponse(self.merkezler_davranisi.get(anahtar, []))
        if "sondurumlar" in url:
            return _DummyResponse([{"sicaklik": 20.0, "hadiseKodu": "A", "veriZamani": "x"}])
        if "tahminler/gunluk" in url:
            return _DummyResponse([])
        if "sunrise-sunset" in url:
            return _DummyResponse(
                {"results": {"sunrise": "2026-08-15T03:00:00+00:00", "sunset": "2026-08-15T16:00:00+00:00"}}
            )
        if "api.open-meteo.com" in url:  # güncel durum
            return _DummyResponse(_open_meteo_basarili_yuk())
        raise AssertionError(f"Beklenmeyen URL: {url} params={params}")


class TestAkilliAramaCozumleyici(unittest.TestCase):
    """akilli_yer_bul()'un üç katmanını (tam eşleşme, parçalama, geocoding)
    ve belirsizlik/bulunamama durumlarını test eder."""

    def test_katman1_tam_eslesme_ag_istegi_atmadan_cozulur(self):
        session = _AkilliAramaSession()
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.akilli_yer_bul("istanbul")
        self.assertEqual(sonuc, {"durum": "cozuldu", "yontem": "il-eslesme", "il": "İstanbul", "ilce": None})
        self.assertEqual(session.calls, [])  # tier 1: hiç ağ isteği yok

    def test_katman1_typo_toleransli_calisir(self):
        session = _AkilliAramaSession()
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.akilli_yer_bul("istambul")  # typo
        self.assertEqual(sonuc["il"], "İstanbul")

    def test_katman2_parcalama_ile_ilce_dogrudan_mgmye_gonderilir(self):
        session = _AkilliAramaSession(
            merkezler_davranisi={
                ("istanbul", "kadikoy"): [
                    {"il": "İstanbul", "ilce": "Kadıköy", "istasyonId": 93409,
                     "enlem": 40.99, "boylam": 29.02}
                ],
            }
        )
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.akilli_yer_bul("kadikoy/istanbul")
        self.assertEqual(sonuc["durum"], "cozuldu")
        self.assertEqual(sonuc["yontem"], "il-ilce-parcalama")
        self.assertEqual(sonuc["il"], "İstanbul")
        self.assertEqual(sonuc["ilce"], "Kadıköy")
        # il ve ilce doğru sırada olsa da olmasa da (kadikoy/istanbul VE
        # istanbul/kadikoy) çözülebilmeli
        sonuc2 = client.akilli_yer_bul("istanbul kadikoy")
        self.assertEqual(sonuc2["ilce"], "Kadıköy")

    def test_katman3_geocoding_mgmde_bulunan_ilceyle_cozulur(self):
        session = _AkilliAramaSession(
            merkezler_davranisi={
                ("istanbul", "besiktas"): [
                    {"il": "İstanbul", "ilce": "Beşiktaş", "istasyonId": 93410,
                     "enlem": 41.04, "boylam": 29.01}
                ],
            },
            geocode_sonucu=[
                {"name": "Beşiktaş", "admin1": "İstanbul", "country": "Türkiye",
                 "country_code": "TR", "latitude": 41.04, "longitude": 29.01},
            ],
        )
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.akilli_yer_bul("besiktass")  # tier1/2 çözemez, geocoding devreye girer
        self.assertEqual(sonuc["durum"], "cozuldu")
        self.assertEqual(sonuc["yontem"], "geocoding-mgm")
        self.assertEqual(sonuc["il"], "İstanbul")
        self.assertEqual(sonuc["ilce"], "Beşiktaş")

    def test_katman3_mgmde_olmayan_mahalle_dogrudan_open_meteoya_duser(self):
        session = _AkilliAramaSession(
            merkezler_davranisi={},  # MGM Maslak'ı hiç tanımıyor
            geocode_sonucu=[
                {"name": "Maslak", "admin1": "İstanbul", "country": "Türkiye",
                 "country_code": "TR", "latitude": 41.11, "longitude": 29.02},
            ],
        )
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.akilli_yer_bul("maslak itü")
        self.assertEqual(sonuc["durum"], "cozuldu")
        self.assertEqual(sonuc["yontem"], "geocoding-dogrudan")
        self.assertEqual(sonuc["il"], "İstanbul")
        self.assertEqual(sonuc["ilce"], "Maslak")
        self.assertEqual(sonuc["enlem"], 41.11)

    def test_katman3_birlesik_sorgu_bos_donerse_tek_kelime_denenir(self):
        """Regresyon: canlıda 'maslak itü' Open-Meteo'da birleşik metin
        olarak hiç sonuç vermiyordu (GeoNames'te böyle bir kayıt yok,
        İTÜ bir kurum, yer adı değil). Tam sorgu boş dönerse kelimeler
        tek tek denenmeli, 'maslak' tek başına bulunmalı."""
        session = _AkilliAramaSession(
            merkezler_davranisi={},
            geocode_sorgu_bazli={
                "maslak itü": [],  # gerçek Open-Meteo davranışı: birleşik sonuç yok
                "maslak": [
                    {"name": "Maslak", "admin1": "İstanbul", "country": "Türkiye",
                     "country_code": "TR", "latitude": 41.11, "longitude": 29.02},
                ],
            },
        )
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.akilli_yer_bul("maslak itü")
        self.assertEqual(sonuc["durum"], "cozuldu")
        self.assertEqual(sonuc["yontem"], "geocoding-dogrudan")
        self.assertEqual(sonuc["il"], "İstanbul")
        self.assertEqual(sonuc["ilce"], "Maslak")

        # Hem 'maslak itü' hem 'maslak' için geocoding çağrıldığını doğrula
        geocode_sorgulari = [
            c[1].get("name") for c in session.calls if "geocoding-api" in c[0]
        ]
        self.assertIn("maslak itü", geocode_sorgulari)
        self.assertIn("maslak", geocode_sorgulari)

    def test_katman3_hicbir_kelime_bulunamazsa_bulunamadi_doner(self):
        session = _AkilliAramaSession(geocode_sorgu_bazli={})  # her sorguda boş
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.akilli_yer_bul("tamamen anlamsiz bir sorgu xyzq")
        self.assertEqual(sonuc["durum"], "bulunamadi")

    def test_farkli_illerde_birden_fazla_aday_belirsiz_doner(self):
        session = _AkilliAramaSession(
            geocode_sonucu=[
                {"name": "Merkez", "admin1": "Konya", "country": "Türkiye",
                 "country_code": "TR", "latitude": 37.87, "longitude": 32.49},
                {"name": "Merkez", "admin1": "Sivas", "country": "Türkiye",
                 "country_code": "TR", "latitude": 39.75, "longitude": 37.02},
            ],
        )
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.akilli_yer_bul("merkez mahallesi")
        self.assertEqual(sonuc["durum"], "belirsiz")
        self.assertEqual(len(sonuc["secenekler"]), 2)
        self.assertNotIn("il", sonuc)

    def test_hicbir_katman_cozemezse_bulunamadi_doner(self):
        session = _AkilliAramaSession(geocode_sonucu=[])
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.akilli_yer_bul("tamamen anlamsiz bir sorgu xyzq")
        self.assertEqual(sonuc["durum"], "bulunamadi")

    def test_bos_sorgu_bulunamadi_doner(self):
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = _AkilliAramaSession()
        self.assertEqual(client.akilli_yer_bul("   ")["durum"], "bulunamadi")


class TestHavaDurumuAkilli(unittest.TestCase):
    """hava_durumu_akilli()'nin akilli_yer_bul() sonucunu gerçek hava
    durumu verisine çevirdiğini uçtan uca doğrular."""

    def test_mgm_uzerinden_cozulen_sorguda_tam_hava_durumu_doner(self):
        session = _AkilliAramaSession(
            merkezler_davranisi={
                ("istanbul", None): [
                    {"il": "İstanbul", "ilce": "Bakırköy", "istasyonId": 93401,
                     "enlem": 40.98, "boylam": 28.82}
                ],
            }
        )
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.hava_durumu_akilli("istanbul")
        self.assertEqual(sonuc["durum"], "cozuldu")
        self.assertEqual(sonuc["yontem"], "il-eslesme")
        self.assertEqual(sonuc["sorgu"], "istanbul")
        self.assertEqual(sonuc["guncel"]["kaynak"], "mgm")
        self.assertIn("tahmin", sonuc)

    def test_dogrudan_koordinatla_cozulen_sorguda_sadece_guncel_doner(self):
        session = _AkilliAramaSession(
            geocode_sonucu=[
                {"name": "Maslak", "admin1": "İstanbul", "country": "Türkiye",
                 "country_code": "TR", "latitude": 41.11, "longitude": 29.02},
            ],
        )
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.hava_durumu_akilli("maslak itü")
        self.assertEqual(sonuc["durum"], "cozuldu")
        self.assertEqual(sonuc["yontem"], "geocoding-dogrudan")
        self.assertEqual(sonuc["guncel"]["kaynak"], "open-meteo")
        self.assertEqual(sonuc["tahmin"], [])

    def test_belirsiz_durumda_hava_durumu_getirmeden_secenek_doner(self):
        session = _AkilliAramaSession(
            geocode_sonucu=[
                {"name": "Merkez", "admin1": "Konya", "country": "Türkiye",
                 "country_code": "TR", "latitude": 37.87, "longitude": 32.49},
                {"name": "Merkez", "admin1": "Sivas", "country": "Türkiye",
                 "country_code": "TR", "latitude": 39.75, "longitude": 37.02},
            ],
        )
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.hava_durumu_akilli("merkez mahallesi")
        self.assertEqual(sonuc["durum"], "belirsiz")
        self.assertNotIn("guncel", sonuc)

    def test_cozulemezse_hata_firlatir(self):
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = _AkilliAramaSession(geocode_sonucu=[])
        with self.assertRaises(MGMWeatherError):
            client.hava_durumu_akilli("tamamen anlamsiz xyzq")


class TestGuncelDurumDinamikTTL(unittest.TestCase):
    """MGM'nin ölçümlerinin saat başından birkaç dakika sonra düştüğü gözlemine
    göre dinamik TTL'in doğru hesaplandığını ve cache_ttl_seconds=0
    durumlarında devre dışı kaldığını doğrular.
    """

    @staticmethod
    def _sahte_simdi(dakika, saat=12):
        # Varsayılan saat=12 öğlen seçildi: varsayılan gece penceresinin tamamen dışında
        sahte = MagicMock()
        sahte.minute = dakika
        sahte.hour = saat
        return sahte

    def test_sicak_pencerede_kisa_ttl_doner(self):
        client = MGMWeather(cache_ttl_seconds=60)
        with patch("mgm_client._dt.datetime") as mock_dt:
            mock_dt.now.return_value = self._sahte_simdi(8)  # XX:08
            self.assertEqual(client._guncel_durum_dinamik_ttl(), client.guncel_sicak_ttl_saniye)

    def test_soguk_pencerede_uzun_ttl_doner(self):
        client = MGMWeather(cache_ttl_seconds=60)
        with patch("mgm_client._dt.datetime") as mock_dt:
            mock_dt.now.return_value = self._sahte_simdi(40)  # XX:40
            self.assertEqual(client._guncel_durum_dinamik_ttl(), client.guncel_soguk_ttl_saniye)

    def test_pencere_sinirlari_dahildir(self):
        client = MGMWeather(cache_ttl_seconds=60)
        with patch("mgm_client._dt.datetime") as mock_dt:
            mock_dt.now.return_value = self._sahte_simdi(5)  # alt sınır
            self.assertEqual(client._guncel_durum_dinamik_ttl(), client.guncel_sicak_ttl_saniye)
            mock_dt.now.return_value = self._sahte_simdi(15)  # üst sınır
            self.assertEqual(client._guncel_durum_dinamik_ttl(), client.guncel_sicak_ttl_saniye)
            mock_dt.now.return_value = self._sahte_simdi(16)  # sınır dışı
            self.assertEqual(client._guncel_durum_dinamik_ttl(), client.guncel_soguk_ttl_saniye)

    def test_cache_kapaliyken_dinamik_ttl_devre_disi(self):
        client = MGMWeather(cache_ttl_seconds=0)
        self.assertEqual(client._guncel_durum_dinamik_ttl(), 0)

    def test_ozellik_kapatilirsa_statik_ttl_doner(self):
        client = MGMWeather(cache_ttl_seconds=60, guncel_dinamik_ttl_aktif=False)
        self.assertEqual(client._guncel_durum_dinamik_ttl(), 60)

    def test_zaman_dilimi_cozulemezse_guvenle_statige_duser(self):
        client = MGMWeather(cache_ttl_seconds=60, guncel_zaman_dilimi="Gecersiz/Bolge")
        self.assertEqual(client._guncel_durum_dinamik_ttl(), 60)

    def test_gece_penceresinde_en_uzun_ttl_doner(self):
        client = MGMWeather(cache_ttl_seconds=60)
        with patch("mgm_client._dt.datetime") as mock_dt:
            # Saat 03:08, hem "sıcak pencere" dakikasında (8) hem gece
            # penceresinde (0-6). Gece önceliklidir.
            mock_dt.now.return_value = self._sahte_simdi(dakika=8, saat=3)
            self.assertEqual(client._guncel_durum_dinamik_ttl(), client.guncel_gece_ttl_saniye)

    def test_gece_penceresi_sinirlari_dahildir(self):
        client = MGMWeather(cache_ttl_seconds=60)
        with patch("mgm_client._dt.datetime") as mock_dt:
            mock_dt.now.return_value = self._sahte_simdi(dakika=40, saat=0)  # alt sınır
            self.assertEqual(client._guncel_durum_dinamik_ttl(), client.guncel_gece_ttl_saniye)
            mock_dt.now.return_value = self._sahte_simdi(dakika=40, saat=5)  # içeride
            self.assertEqual(client._guncel_durum_dinamik_ttl(), client.guncel_gece_ttl_saniye)
            mock_dt.now.return_value = self._sahte_simdi(dakika=40, saat=6)  # üst sınır, dışarıda
            self.assertEqual(client._guncel_durum_dinamik_ttl(), client.guncel_soguk_ttl_saniye)

    def test_gece_yarisini_saran_ozel_pencere_dogru_calisir(self):
        client = MGMWeather(
            cache_ttl_seconds=60, guncel_gece_baslangic_saat=22, guncel_gece_bitis_saat=6
        )
        with patch("mgm_client._dt.datetime") as mock_dt:
            mock_dt.now.return_value = self._sahte_simdi(dakika=40, saat=23)  # 22-06 içinde
            self.assertEqual(client._guncel_durum_dinamik_ttl(), client.guncel_gece_ttl_saniye)
            mock_dt.now.return_value = self._sahte_simdi(dakika=40, saat=2)  # 22-06 içinde
            self.assertEqual(client._guncel_durum_dinamik_ttl(), client.guncel_gece_ttl_saniye)
            mock_dt.now.return_value = self._sahte_simdi(dakika=40, saat=12)  # dışında
            self.assertEqual(client._guncel_durum_dinamik_ttl(), client.guncel_soguk_ttl_saniye)

    def test_guncel_durum_dinamik_ttlyi_gete_iletir(self):
        client = MGMWeather(cache_ttl_seconds=60, timeout=1, retry_total=0)
        client.session = _CountingSession(
            [{"sicaklik": 20.0, "hadiseKodu": "A", "veriZamani": "x"}]
        )
        with (
            patch.object(client, "_guncel_durum_dinamik_ttl", return_value=999),
            patch.object(client, "_get", wraps=client._get) as mock_get,
        ):
            client.guncel_durum(123)
            _, kwargs = mock_get.call_args
            self.assertEqual(kwargs.get("ttl_override"), 999)


class TestTahminAyriTTL(unittest.TestCase):
    """gunluk_tahmin()/saatlik_tahmin()'in guncel_durum()'dan ayrı, daha
    uzun bir statik TTL (tahmin_ttl_saniye) kullandığını ve
    cache_ttl_seconds=0 ile cache tamamen kapatıldığında bu ayrı TTL'in
    de devre dışı kaldığını (test uyumluluğu için kritik) doğrular."""

    def test_gunluk_tahmin_ayri_ttl_ile_gete_iletir(self):
        client = MGMWeather(cache_ttl_seconds=60, tahmin_ttl_saniye=12345, timeout=1, retry_total=0)
        client.session = _CountingSession([{"hadiseGun1": "A"}])
        with patch.object(client, "_get", wraps=client._get) as mock_get:
            client.gunluk_tahmin(123)
            _, kwargs = mock_get.call_args
            self.assertEqual(kwargs.get("ttl_override"), 12345)

    def test_saatlik_tahmin_ayri_ttl_ile_gete_iletir(self):
        client = MGMWeather(cache_ttl_seconds=60, tahmin_ttl_saniye=12345, timeout=1, retry_total=0)
        client.session = _CountingSession([])
        with patch.object(client, "_get", wraps=client._get) as mock_get:
            client.saatlik_tahmin(123)
            _, kwargs = mock_get.call_args
            self.assertEqual(kwargs.get("ttl_override"), 12345)

    def test_cache_kapaliyken_tahmin_ttli_de_kapanir(self):
        # Kritik regresyon: cache_ttl_seconds=0 birçok testte "cache'i
        # kapat" niyetiyle kullanılıyor, tahmin_ttl_saniye'nin kendi
        # varsayılanı (10800) bu niyeti ezmemeli.
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        self.assertEqual(client._tahmin_ttl(), 0)


class _UyariSession:
    """merkezler gibi diğer uçlara dokunmayan, sadece
    alarmlar isteklerini kaydeden minimal sahte oturum."""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple] = []

    def get(self, url, params=None, **kwargs):
        self.calls.append((url, dict(params or {})))
        return _DummyResponse(self.payload)


class TestUyarilar(unittest.TestCase):
    """uyarilar()'ın MGM şemasını tahmin etmeden ham geçiş yaptığını,
    il parametresini doğru ilettiğini/iletmediğini doğrular. Gerçek bir
    uyarı örneği görmediğimiz için alan bazlı bir dönüşüm YOK
    """

    def test_bos_liste_donerse_ham_bos_liste_ve_not_iceriyor(self):
        session = _UyariSession([])
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.uyarilar()
        self.assertEqual(sonuc["ham"], [])
        self.assertIn("not", sonuc)
        self.assertIn("bu kodun yazıldığı sıradaki", sonuc["not"])

    def test_il_verilmezse_parametresiz_istek_atilir(self):
        session = _UyariSession([])
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        client.uyarilar()
        _, params = session.calls[0]
        self.assertNotIn("il", params)

    def test_il_verilirse_mgmye_dogrudan_parametre_olarak_gonderilir(self):
        session = _UyariSession([])
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        client.uyarilar("İstanbul")
        _, params = session.calls[0]
        self.assertEqual(params.get("il"), "istanbul")

    def test_dolu_veri_donerse_donusturmeden_oldugu_gibi_gecer(self):
        # MGM'nin gerçek alan adlarını bilmiyoruz, kasıtlı olarak
        # "bilmediğimiz" alan adlarıyla bir örnek veriyoruz
        mgm_ham_ornek = [
            {"bilinmeyenAlan1": "sarı", "bilinmeyenAlan2": "fırtına", "il": "Rize"}
        ]
        session = _UyariSession(mgm_ham_ornek)
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.uyarilar()
        self.assertEqual(sonuc["ham"], mgm_ham_ornek)  # birebir aynı, dönüşüm yok


class _KonumSession:
    """/konum için gereken tüm dış servisleri (Nominatim ters geocoding,
    MGM merkezler/sondurumlar/tahminler, Open-Meteo current) tek bir
    sahte oturumda birleştirir."""

    def __init__(self, nominatim_adres=None, merkezler_davranisi=None, nominatim_hata=False):
        self.nominatim_adres = nominatim_adres  # None -> adres bulunamadı
        self.nominatim_hata = nominatim_hata  # True -> Nominatim'e bağlanılamadı
        self.merkezler_davranisi = merkezler_davranisi or {}
        self.calls: list[tuple] = []

    def get(self, url, params=None, headers=None, **kwargs):
        params = dict(params or {})
        self.calls.append((url, params))
        if "nominatim.openstreetmap.org" in url:
            if self.nominatim_hata:
                raise requests.ConnectionError("Nominatim'e ulaşılamadı (simülasyon)")
            if self.nominatim_adres is None:
                return _DummyResponse({})  # address alanı yok
            return _DummyResponse({"address": self.nominatim_adres})
        if "merkezler" in url:
            anahtar = (params.get("il"), params.get("ilce"))
            return _DummyResponse(self.merkezler_davranisi.get(anahtar, []))
        if "sondurumlar" in url:
            return _DummyResponse([{"sicaklik": 20.0, "hadiseKodu": "A", "veriZamani": "x"}])
        if "tahminler/gunluk" in url:
            return _DummyResponse([])
        if "sunrise-sunset" in url:
            return _DummyResponse(
                {"results": {"sunrise": "2026-08-15T03:00:00+00:00", "sunset": "2026-08-15T16:00:00+00:00"}}
            )
        if "api.open-meteo.com" in url:
            return _DummyResponse(_open_meteo_basarili_yuk())
        raise AssertionError(f"Beklenmeyen URL: {url} params={params}")


class TestKonumCozumleyici(unittest.TestCase):
    """hava_durumu_konum()'un Nominatim -> MGM -> Open-Meteo zincirini
    doğru sırayla denediğini doğrular."""

    def test_nominatim_mgmde_bulunan_ilceyi_dogru_cozer(self):
        session = _KonumSession(
            nominatim_adres={"state": "İstanbul", "county": "Kadıköy"},
            merkezler_davranisi={
                ("istanbul", "kadikoy"): [
                    {"il": "İstanbul", "ilce": "Kadıköy", "istasyonId": 93409,
                     "enlem": 40.99, "boylam": 29.02}
                ],
            },
        )
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.hava_durumu_konum(40.99, 29.02)
        self.assertEqual(sonuc["durum"], "cozuldu")
        self.assertEqual(sonuc["yontem"], "nominatim-mgm")
        self.assertEqual(sonuc["il"], "İstanbul")
        self.assertEqual(sonuc["ilce"], "Kadıköy")
        self.assertEqual(sonuc["guncel"]["kaynak"], "mgm")

    def test_ilce_alani_farkli_isimde_gelse_de_denenir(self):
        # Nominatim bazı bölgelerde 'county' yerine 'town'/'suburb' gibi
        # farklı alan adları kullanabiliyor, hepsi sırayla denenmeli.
        session = _KonumSession(
            nominatim_adres={"state": "İstanbul", "town": "Beşiktaş"},
            merkezler_davranisi={
                ("istanbul", "besiktas"): [
                    {"il": "İstanbul", "ilce": "Beşiktaş", "istasyonId": 93410,
                     "enlem": 41.04, "boylam": 29.01}
                ],
            },
        )
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.hava_durumu_konum(41.04, 29.01)
        self.assertEqual(sonuc["yontem"], "nominatim-mgm")
        self.assertEqual(sonuc["ilce"], "Beşiktaş")

    def test_mgmde_olmayan_ilce_dogrudan_open_meteoya_duser(self):
        # Nominatim "Maslak" diyor ama MGM böyle bir ilçe tanımıyor.
        session = _KonumSession(
            nominatim_adres={"state": "İstanbul", "suburb": "Maslak"},
            merkezler_davranisi={},  # MGM boş dönüyor
        )
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.hava_durumu_konum(41.11, 29.02)
        self.assertEqual(sonuc["durum"], "cozuldu")
        self.assertEqual(sonuc["yontem"], "nominatim-open-meteo")
        self.assertEqual(sonuc["guncel"]["kaynak"], "open-meteo")
        self.assertEqual(sonuc["tahmin"], [])
        self.assertEqual(sonuc["il"], "İstanbul")
        self.assertEqual(sonuc["ilce"], "Maslak")

    def test_nominatim_adres_bulamazsa_dogrudan_open_meteoya_duser(self):
        session = _KonumSession(nominatim_adres=None)  # deniz ortası vb.
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.hava_durumu_konum(36.0, 30.0)
        self.assertEqual(sonuc["yontem"], "open-meteo-dogrudan")
        self.assertIsNone(sonuc["il"])
        self.assertEqual(sonuc["guncel"]["kaynak"], "open-meteo")

    def test_nominatim_cokerse_mgm_denenmeden_open_meteoya_duser(self):
        session = _KonumSession(nominatim_hata=True)
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.hava_durumu_konum(41.0, 29.0)
        self.assertEqual(sonuc["yontem"], "open-meteo-dogrudan")
        # MGM'ye hiç merkezler isteği atılmamış olmalı
        self.assertFalse(any("merkezler" in u for u, _ in session.calls))

    def test_il_mgmnin_81_il_listesiyle_eslesmezse_open_meteoya_duser(self):
        # Nominatim yurt dışı bir il adı dönerse (örn. sınır ötesi bir
        # koordinat), 81 il listesiyle eşleşmez. Nominatim yine de bir
        # adres bulduğu için yontem "nominatim-open-meteo" olur (Nominatim
        # hiç adres bulamadığı "open-meteo-dogrudan" durumundan bilinçli
        # olarak ayrı tutuluyor, hangisinin olduğu debug için faydalı).
        session = _KonumSession(nominatim_adres={"state": "Attiki", "county": "Athens"})
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.hava_durumu_konum(37.98, 23.72)
        self.assertEqual(sonuc["yontem"], "nominatim-open-meteo")
        self.assertEqual(sonuc["guncel"]["kaynak"], "open-meteo")

    def test_nominatim_user_agent_gonderilir(self):
        session = _KonumSession(nominatim_adres=None)
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session
        client._nominatim_ters_geocode(41.0, 29.0)
        # get() çağrısına headers kwarg'ı olarak User-Agent geçirilmiş mi
        # kontrol etmek için session.get'i sarmalayalım
        orijinal_get = session.get
        cagrilar = []

        def sarmalayici(url, params=None, headers=None, **kwargs):
            cagrilar.append(headers)
            return orijinal_get(url, params=params, headers=headers, **kwargs)

        session.get = sarmalayici
        client2 = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client2.session = session
        client2._nominatim_ters_geocode(42.0, 30.0)
        self.assertTrue(any(h and "User-Agent" in h for h in cagrilar))


class TestPrometheusMetrikleri(unittest.TestCase):
    """_cached_get()'in hit/stale_hit/miss dallarının Prometheus
    sayacını (CACHE_SONUC_SAYAC) doğru artırdığını doğrular. Sayaç
    modül seviyesinde global registry'de yaşadığı için testler arası
    sızıntıyı önlemek adına her testte önce/sonra farkına bakılır,
    mutlak değere değil."""

    @staticmethod
    def _sayac(etiket: str) -> float:
        return CACHE_SONUC_SAYAC.labels(sonuc=etiket)._value.get()

    def test_ilk_cagri_miss_ikinci_cagri_hit_artirir(self):
        session = _CountingSession([{"il": "Ankara"}])
        client = MGMWeather(cache_ttl_seconds=60, timeout=1, retry_total=0)
        client.session = session

        miss_once = self._sayac("miss")
        hit_once = self._sayac("hit")

        client._get("merkezler", {"il": "ankara"})
        self.assertEqual(self._sayac("miss"), miss_once + 1)
        self.assertEqual(self._sayac("hit"), hit_once)

        client._get("merkezler", {"il": "ankara"})
        self.assertEqual(self._sayac("miss"), miss_once + 1)  # değişmedi
        self.assertEqual(self._sayac("hit"), hit_once + 1)

    def test_stale_pencerede_stale_hit_artirir(self):
        session = _CountingSession([{"il": "Ankara"}])
        client = MGMWeather(
            cache_ttl_seconds=1,
            stale_while_revalidate_seconds=60,
            timeout=1,
            retry_total=0,
        )
        client.session = session

        client._get("merkezler", {"il": "ankara"})  # miss + yaz
        time.sleep(1.2)  # ttl geçsin, stale pencereye düşsün

        stale_once = self._sayac("stale_hit")
        client._get("merkezler", {"il": "ankara"})
        self.assertEqual(self._sayac("stale_hit"), stale_once + 1)


class _SondurumSession:
    """Yol bazlı (path -> yanıt) çok basit sahte oturum. sondurumlar/*,
    merkezler/iller gibi tek-endpoint testleri için."""

    def __init__(self, path_yanitlari: dict[str, object]):
        self.path_yanitlari = path_yanitlari
        self.calls: list[tuple] = []

    def get(self, url, params=None, **kwargs):
        self.calls.append((url, dict(params or {})))
        for path, yanit in self.path_yanitlari.items():
            if path in url:
                return _DummyResponse(yanit)
        raise AssertionError(f"Beklenmeyen URL: {url}")


class TestSonDurumlarAilesi(unittest.TestCase):
    """en_dusuk/en_yuksek_sicakliklar, toplam_yagislar, kar_kalinliklari,
    son_gozlemler. Bu ailede canlı testte gerçek bir hata bulunmuştu
    (kar_kalinliklari'ye yanlışlıkla istAd filtresi kopyalanmıştı,
    kaynak JS'te böyle bir filtre yoktu, tüm kayıtlar sessizce
    siliniyordu) - bu sınıf o regresyonu bir daha yakalar."""

    def test_en_dusuk_sicakliklar_dogru_tarih_servisini_kullanir(self):
        session = _SondurumSession({
            "minimumMaxTarih": ["2026-01-10T18:00:00"],
            "sondurumlar/endusuk": [{"il": "Ardahan", "istAd": "ARDAHAN", "deger": -5.2}],
        })
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.en_dusuk_sicakliklar()
        self.assertEqual(sonuc["tarih"], "2026-01-10")
        self.assertEqual(len(sonuc["kayitlar"]), 1)
        # minimumMaxTarih çağrıldı, maximumMaxTarih çağrılmadı
        self.assertTrue(any("minimumMaxTarih" in u for u, _ in session.calls))
        self.assertFalse(any("maximumMaxTarih" in u for u, _ in session.calls))

    def test_en_yuksek_sicakliklar_ayri_tarih_servisini_kullanir(self):
        # Regresyon: en_yuksek yanlışlıkla minimumMaxTarih kullanıyordu,
        # bu da maximumMaxTarih'in verdiği farklı tarihi görmezden geliyordu
        session = _SondurumSession({
            "maximumMaxTarih": ["2026-01-09T18:00:00"],
            "sondurumlar/enyuksek": [{"il": "Şırnak", "istAd": "SIRNAK", "deger": 41.0}],
        })
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.en_yuksek_sicakliklar()
        self.assertEqual(sonuc["tarih"], "2026-01-09")
        self.assertEqual(len(sonuc["kayitlar"]), 1)
        self.assertTrue(any("maximumMaxTarih" in u for u, _ in session.calls))
        self.assertFalse(any("minimumMaxTarih" in u for u, _ in session.calls))

    def test_toplam_yagislar_yagis_max_tarih_servisini_kullanir(self):
        session = _SondurumSession({
            "yagisMaxTarih": ["2026-08-28T06:00:00"],
            "sondurumlar/toplamyagis": [{"il": "Rize", "istAd": "RIZE", "deger": 45.2}],
        })
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.toplam_yagislar()
        self.assertEqual(sonuc["tarih"], "2026-08-28")
        self.assertEqual(len(sonuc["kayitlar"]), 1)

    def test_en_dusuk_sicakliklar_istadi_olmayan_kayitlari_filtreler(self):
        session = _SondurumSession({
            "minimumMaxTarih": ["2026-01-10T18:00:00"],
            "sondurumlar/endusuk": [
                {"il": "Ardahan", "istAd": "ARDAHAN", "deger": -5.2},
                {"il": None, "istAd": None, "deger": None},
            ],
        })
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.en_dusuk_sicakliklar()
        self.assertEqual(len(sonuc["kayitlar"]), 1)

    def test_kar_kalinliklari_istad_filtresi_UYGULAMAZ(self):
        # Regresyon testi: kaynak karkalinlik.js'te istAd filtresi YOK
        # (yalnızca görüntüde ng-show var, veri dizisi filtrelenmiyor).
        # Önceki bir sürüm yanlışlıkla bu filtreyi eklemişti ve
        # sondurumlar/kar'ın alan adı farklıysa TÜM kayıtları sessizce
        # siliyordu. Bu test, istAd alanı olmayan kayıtların da
        # kaybolmadığını doğrular.
        session = _SondurumSession({
            "sondurumlar/kar": [
                {"il": "Erzurum", "istAd": "PALANDÖKEN", "karYukseklik": 35},
                {"il": "Bursa", "istAd": None, "karYukseklik": 12},  # istAd yok, yine de kalmalı
            ],
        })
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.kar_kalinliklari()
        self.assertEqual(len(sonuc["kayitlar"]), 2)  # HİÇBİRİ filtrelenmemeli

    def test_kar_kalinliklari_tarih_parametresi_gondermez(self):
        session = _SondurumSession({"sondurumlar/kar": []})
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        client.kar_kalinliklari()
        self.assertEqual(len(session.calls), 1)  # tek çağrı, tarih servisi yok
        _, params = session.calls[0]
        self.assertNotIn("tarih", params)

    def test_son_gozlemler_il_adini_dogru_esler(self):
        session = _SondurumSession({
            "merkezler/iller": [{"sondurumIstNo": 17130, "ilPlaka": 34}],
            "sondurumlar/ilmerkezleri": [{"istNo": 17130, "sicaklik": 24.5}],
        })
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.son_gozlemler()
        self.assertEqual(sonuc["kayitlar"][0]["il"], "İstanbul")
        self.assertNotIn("_uyari", sonuc)

    def test_son_gozlemler_eslesme_basarisizsa_uyari_ekler(self):
        # merkezler/iller'in alan adları beklenenden farklıysa (MGM
        # değiştirdiyse) sessizce "il": null yazıp geçmemeli
        session = _SondurumSession({
            "merkezler/iller": [{"bilinmeyenAlan": 17130}],
            "sondurumlar/ilmerkezleri": [{"istNo": 17130, "sicaklik": 24.5}],
        })
        client = MGMWeather(cache_ttl_seconds=0, timeout=1, retry_total=0)
        client.session = session

        sonuc = client.son_gozlemler()
        self.assertIsNone(sonuc["kayitlar"][0]["il"])
        self.assertIn("_uyari", sonuc)


if __name__ == "__main__":
    unittest.main()
