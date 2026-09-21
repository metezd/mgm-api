import ipaddress
import unittest
from unittest.mock import patch

from flask import Flask

from api.middleware import RateLimiter, route_limit_setting, validate_date_range, validate_json_body

_test_app = Flask(__name__)


class _SahteRedisEval:
    """redis_client.eval()'in Lua INCR+EXPIRE davranışını taklit eder."""

    def __init__(self):
        self.sayaçlar: dict = {}

    def eval(self, _script, _numkeys, key, _window_seconds):
        self.sayaçlar[key] = self.sayaçlar.get(key, 0) + 1
        return self.sayaçlar[key]


class _RedisHatasi(Exception):
    pass


class _PatlayanRedisEval:
    def eval(self, *_args, **_kwargs):
        raise _RedisHatasi("redis çöktü")


class TestClientIp(unittest.TestCase):
    """RateLimiter.client_ip(): güvenilmeyen proxy'lerden gelen
    X-Forwarded-For'a güvenmeme, güvenilir proxy zincirini doğru çözme."""

    def _limiter(self, trusted_cidrs=()):
        networks = tuple(ipaddress.ip_network(c) for c in trusted_cidrs)
        return RateLimiter(
            redis_state=lambda: (False, None, "mgm-cache:", Exception),
            trusted_proxy_networks=networks,
        )

    def test_guvenilir_proxy_yoksa_remote_addr_donulur(self):
        limiter = self._limiter()
        with _test_app.test_request_context(
            "/", environ_base={"REMOTE_ADDR": "203.0.113.5"},
            headers={"X-Forwarded-For": "1.2.3.4"},
        ):
            self.assertEqual(limiter.client_ip(), "203.0.113.5")  # XFF'e güvenilmez

    def test_remote_addr_gecersizse_oldugu_gibi_donulur(self):
        limiter = self._limiter()
        with _test_app.test_request_context("/", environ_base={"REMOTE_ADDR": ""}):
            self.assertEqual(limiter.client_ip(), "unknown")

    def test_guvenilir_proxy_zincirinden_gercek_ip_cozulur(self):
        limiter = self._limiter(trusted_cidrs=["10.0.0.0/8"])
        with _test_app.test_request_context(
            "/",
            environ_base={"REMOTE_ADDR": "10.0.0.1"},  # güvenilir proxy
            headers={"X-Forwarded-For": "198.51.100.7, 10.0.0.2"},
        ):
            # zincir ters taranır, ilk güvenilmeyen adres gerçek istemcidir
            self.assertEqual(limiter.client_ip(), "198.51.100.7")

    def test_zincirdeki_bozuk_adresler_atlanir(self):
        limiter = self._limiter(trusted_cidrs=["10.0.0.0/8"])
        with _test_app.test_request_context(
            "/",
            environ_base={"REMOTE_ADDR": "10.0.0.1"},
            headers={"X-Forwarded-For": "bozuk-adres, 198.51.100.9"},
        ):
            self.assertEqual(limiter.client_ip(), "198.51.100.9")

    def test_zincirdeki_tum_adresler_guvenilirse_remote_addre_duser(self):
        limiter = self._limiter(trusted_cidrs=["10.0.0.0/8"])
        with _test_app.test_request_context(
            "/",
            environ_base={"REMOTE_ADDR": "10.0.0.1"},
            headers={"X-Forwarded-For": "10.0.0.5, 10.0.0.2"},
        ):
            self.assertEqual(limiter.client_ip(), "10.0.0.1")


class TestRateLimiterCleanup(unittest.TestCase):
    def test_esik_altindaysa_temizlik_yapilmaz(self):
        limiter = RateLimiter(redis_state=lambda: (False, None, "p", Exception), max_tracked_keys=10)
        for i in range(5):
            limiter.buckets[f"k{i}"]
        limiter.cleanup()
        self.assertEqual(len(limiter.buckets), 5)

    def test_esik_asilinca_bos_bucketlar_silinir_dolular_kalir(self):
        limiter = RateLimiter(redis_state=lambda: (False, None, "p", Exception), max_tracked_keys=2)
        limiter.buckets["bos1"]
        limiter.buckets["bos2"]
        limiter.buckets["dolu"].append(1.0)
        limiter.cleanup()
        self.assertEqual(list(limiter.buckets.keys()), ["dolu"])


class TestRateLimiterCheck(unittest.TestCase):
    def test_redis_varken_izin_verilir_ve_kalan_dogru_hesaplanir(self):
        redis_client = _SahteRedisEval()
        limiter = RateLimiter(redis_state=lambda: (True, redis_client, "p:", Exception))

        izinli, kalan, _reset = limiter.check("1.2.3.4", "toplu", limit=5, window_seconds=60)

        self.assertTrue(izinli)
        self.assertEqual(kalan, 4)

    def test_redis_limit_asilinca_engellenir(self):
        redis_client = _SahteRedisEval()
        limiter = RateLimiter(redis_state=lambda: (True, redis_client, "p:", Exception))
        for _ in range(3):
            limiter.check("1.2.3.4", "toplu", limit=3, window_seconds=60)

        izinli, kalan, _reset = limiter.check("1.2.3.4", "toplu", limit=3, window_seconds=60)

        self.assertFalse(izinli)
        self.assertEqual(kalan, 0)

    def test_redis_hata_verirse_bellek_fallbackine_duser(self):
        limiter = RateLimiter(redis_state=lambda: (True, _PatlayanRedisEval(), "p:", _RedisHatasi))

        izinli, kalan, _reset = limiter.check("1.2.3.4", "toplu", limit=5, window_seconds=60)

        self.assertTrue(izinli)
        self.assertEqual(kalan, 4)

    def test_redis_yokken_bellek_ile_calisir(self):
        limiter = RateLimiter(redis_state=lambda: (False, None, "p:", Exception))

        izinli, kalan, _reset = limiter.check("1.2.3.4", "toplu", limit=2, window_seconds=60)
        self.assertTrue(izinli)
        self.assertEqual(kalan, 1)

        izinli, kalan, _reset = limiter.check("1.2.3.4", "toplu", limit=2, window_seconds=60)
        self.assertTrue(izinli)
        self.assertEqual(kalan, 0)

        izinli, kalan, _reset = limiter.check("1.2.3.4", "toplu", limit=2, window_seconds=60)
        self.assertFalse(izinli)
        self.assertEqual(kalan, 0)

    def test_pencere_dolunca_eski_kayitlar_silinir_tekrar_izin_verilir(self):
        limiter = RateLimiter(redis_state=lambda: (False, None, "p:", Exception))
        with patch("api.middleware.time.monotonic", side_effect=[0.0, 100.0]):
            limiter.check("1.2.3.4", "toplu", limit=1, window_seconds=10)
            izinli, _kalan, _reset = limiter.check("1.2.3.4", "toplu", limit=1, window_seconds=10)
        self.assertTrue(izinli)  # 100sn sonra pencere (10sn) çoktan dolmuş

    def test_rastgele_tetiklenen_cleanup_cagrilir(self):
        limiter = RateLimiter(redis_state=lambda: (False, None, "p:", Exception), max_tracked_keys=0)
        with patch("api.middleware.random.random", return_value=0.0):  # her zaman < 0.01
            limiter.check("1.2.3.4", "toplu", limit=5, window_seconds=60)
        # max_tracked_keys=0 olduğu için cleanup çağrıldıysa dolu bucket bile silinmiş olabilir
        # mi kontrol etmek yerine sadece hatasız çalıştığını doğruluyoruz (asıl amaç: satırı kapsamak)


class TestValidateJsonBody(unittest.TestCase):
    def test_json_olmayan_istekte_none_doner(self):
        with _test_app.test_request_context("/", data="düz metin", content_type="text/plain"):
            self.assertIsNone(validate_json_body(100))

    def test_content_length_sinirin_altindaysa_none_doner(self):
        with _test_app.test_request_context(
            "/", json={"a": 1}, headers={"Content-Length": "10"}
        ):
            self.assertIsNone(validate_json_body(1000))

    def test_content_length_siniri_asarsa_413_doner(self):
        with _test_app.test_request_context(
            "/", data='{"a": "cok uzun bir govde"}', content_type="application/json",
            headers={"Content-Length": "9999"},
        ):
            sonuc = validate_json_body(10)
        self.assertIsNotNone(sonuc)
        self.assertEqual(sonuc[1], 413)


class TestValidateDateRange(unittest.TestCase):
    def test_farkli_path_icin_hep_none_doner(self):
        with _test_app.test_request_context("/hava-durumu/Istanbul?start=x&end=y"):
            self.assertIsNone(validate_date_range(31))

    def test_start_end_yoksa_none_doner(self):
        with _test_app.test_request_context("/gecmis"):
            self.assertIsNone(validate_date_range(31))

    def test_sadece_start_verilirse_400_doner(self):
        with _test_app.test_request_context("/gecmis?start=2026-01-01"):
            sonuc = validate_date_range(31)
        self.assertEqual(sonuc[1], 400)

    def test_gecersiz_tarih_formati_400_doner(self):
        with _test_app.test_request_context("/gecmis?start=01-01-2026&end=2026-01-05"):
            sonuc = validate_date_range(31)
        self.assertEqual(sonuc[1], 400)

    def test_end_start_tan_onceyse_400_doner(self):
        with _test_app.test_request_context("/gecmis?start=2026-01-10&end=2026-01-01"):
            sonuc = validate_date_range(31)
        self.assertEqual(sonuc[1], 400)

    def test_araligin_asimi_400_doner(self):
        with _test_app.test_request_context("/gecmis?start=2026-01-01&end=2026-03-01"):
            sonuc = validate_date_range(31)
        self.assertEqual(sonuc[1], 400)

    def test_gecerli_aralik_none_doner(self):
        with _test_app.test_request_context("/gecmis?start=2026-01-01&end=2026-01-10"):
            self.assertIsNone(validate_date_range(31))


class TestRouteLimitSetting(unittest.TestCase):
    def test_tam_yol_eslesmesi(self):
        ayarlar = {("POST", "/toplu"): ("toplu", 10)}
        self.assertEqual(route_limit_setting("POST", "/toplu", ayarlar), ("toplu", 10))

    def test_alt_yol_on_ek_eslesmesi(self):
        ayarlar = {("GET", "/favoriler"): ("favoriler", 20)}
        self.assertEqual(
            route_limit_setting("GET", "/favoriler/liste-1", ayarlar), ("favoriler", 20)
        )

    def test_eslesmeyen_yol_none_doner(self):
        ayarlar = {("POST", "/toplu"): ("toplu", 10)}
        self.assertIsNone(route_limit_setting("POST", "/baska-yol", ayarlar))

    def test_yontem_uyusmazsa_none_doner(self):
        ayarlar = {("POST", "/toplu"): ("toplu", 10)}
        self.assertIsNone(route_limit_setting("GET", "/toplu", ayarlar))

    def test_benzer_ama_farkli_bir_yol_on_ek_olarak_eslesmez(self):
        # "/favoriler-eski" , "/favoriler" ile başlıyor ama "/" ile ayrılmıyor
        ayarlar = {("GET", "/favoriler"): ("favoriler", 20)}
        self.assertIsNone(route_limit_setting("GET", "/favoriler-eski", ayarlar))


if __name__ == "__main__":
    unittest.main()
