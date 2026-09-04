import time
import unittest
from unittest.mock import patch

import app as app_module
from mgm_client import MGMWeatherError


class FakeMGM:
    def __init__(
        self,
        should_fail_health: bool = False,
        redis_durum: dict[str, str] | None = None,
        circuit_breaker_durum: dict[str, str] | None = None,
        alert_observations: list[dict | Exception] | None = None,
    ):
        self.should_fail_health = should_fail_health
        self.redis_durum = redis_durum if redis_durum is not None else {"durum": "skip"}
        self.circuit_breaker_durum = (
            circuit_breaker_durum if circuit_breaker_durum is not None else {"durum": "kapali"}
        )
        self.alert_observations = list(alert_observations or [])

    def il_istasyonlari(self, il: str):
        if self.should_fail_health:
            raise MGMWeatherError("MGM servisine bağlanılamadı")
        return [{"il": il, "ilce": "Bakırköy", "merkezId": 93401}]

    def redis_saglik_ozeti(self):
        return dict(self.redis_durum)

    def circuit_breaker_saglik_ozeti(self):
        return dict(self.circuit_breaker_durum)

    def ilce_istasyonu(self, il: str, ilce: str | None = None):
        return {"il": il, "ilce": ilce or "Bakırköy", "merkezId": 93401}

    def hava_durumu(self, il: str, ilce: str | None = None):
        return {
            "il": il,
            "ilce": ilce or "Bakırköy",
            "guncel": {"sicaklik": 27.1, "durum": "Çok Bulutlu"},
            "tahmin": [{"tarih": "2026-08-14", "durum": "Parçalı Bulutlu"}],
        }

    def saatlik_tahmin(self, istasyon_id: int | str):
        return [{"gun": "2026-08-14", "saat": "12:00", "sicaklik": 27.0}]

    def guncel_durum_yedekli(self, istasyon_id: int | str, enlem: float | None, boylam: float | None):
        if not self.alert_observations:
            return {}
        observation = self.alert_observations.pop(0)
        if isinstance(observation, Exception):
            raise observation
        return observation

    def hava_durumu_akilli(self, sorgu: str):
        if sorgu == "bulunamayan sorgu":
            raise MGMWeatherError(f"'{sorgu}' herhangi bir yere çözümlenemedi.")
        if sorgu == "belirsiz sorgu":
            return {
                "durum": "belirsiz",
                "sorgu": sorgu,
                "secenekler": [
                    {"yer": "Merkez", "il": "Konya"},
                    {"yer": "Merkez", "il": "Sivas"},
                ],
            }
        return {
            "durum": "cozuldu",
            "sorgu": sorgu,
            "yontem": "il-eslesme",
            "il": "İstanbul",
            "ilce": "Bakırköy",
            "guncel": {"sicaklik": 27.1, "durum": "Çok Bulutlu", "kaynak": "mgm"},
            "tahmin": [{"tarih": "2026-08-14", "durum": "Parçalı Bulutlu"}],
        }

    def uyarilar(self, il: str | None = None):
        if il == "coken-il":
            raise MGMWeatherError("MGM'ye ulaşılamadı (simülasyon).")
        return {"ham": [], "not": "test notu"}

    def hava_durumu_konum(self, enlem: float, boylam: float):
        if enlem == 77.0:  # test tetikleyicisi, geçerli aralıkta (-90..90)
            raise MGMWeatherError("Open-Meteo'ya da ulaşılamadı (simülasyon).")
        return {
            "durum": "cozuldu",
            "yontem": "nominatim-mgm",
            "il": "İstanbul",
            "ilce": "Kadıköy",
            "enlem": enlem,
            "boylam": boylam,
            "guncel": {"sicaklik": 25.0, "kaynak": "mgm"},
            "tahmin": [],
        }


class TestAppIntegration(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        self.original_mgm = app_module.mgm
        app_module.mgm = FakeMGM()

    def tearDown(self):
        app_module.mgm = self.original_mgm

    def test_health_shallow_ok(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["basarili"])
        self.assertEqual(data["mgm"], "skip")
        self.assertEqual(data["redis"], "skip")

    def test_health_deep_ok(self):
        resp = self.client.get("/health?deep=1")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["basarili"])
        self.assertEqual(data["mgm"], "ok")

    def test_health_deep_ok_redis_ok(self):
        app_module.mgm = FakeMGM(redis_durum={"durum": "ok"})
        resp = self.client.get("/health?deep=1")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["basarili"])
        self.assertEqual(data["redis"], "ok")

    def test_health_deep_redis_hata_503(self):
        app_module.mgm = FakeMGM(redis_durum={"durum": "hata", "hata": "Down"})
        resp = self.client.get("/health?deep=1")
        self.assertEqual(resp.status_code, 503)
        data = resp.get_json()
        self.assertFalse(data["basarili"])
        self.assertEqual(data["durum"], "degraded")
        self.assertEqual(data["redis"], "hata")

    def test_health_deep_fail_503(self):
        app_module.mgm = FakeMGM(should_fail_health=True)
        resp = self.client.get("/health?deep=1")
        self.assertEqual(resp.status_code, 503)
        data = resp.get_json()
        self.assertFalse(data["basarili"])
        self.assertEqual(data["durum"], "degraded")

    def test_health_shallow_circuit_breaker_alani_doner(self):
        app_module.mgm = FakeMGM(circuit_breaker_durum={"durum": "acik"})
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["circuit_breaker"], "acik")

    def test_health_deep_circuit_breaker_alani_doner(self):
        app_module.mgm = FakeMGM(circuit_breaker_durum={"durum": "yari-acik"})
        resp = self.client.get("/health?deep=1")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["circuit_breaker"], "yari-acik")

    def test_iller_endpoint_81_il_doner(self):
        resp = self.client.get("/iller")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["basarili"])
        self.assertEqual(len(data["veri"]), 81)
        self.assertEqual(data["veri"][0], {"plakaKodu": 1, "il": "Adana"})
        self.assertEqual(data["veri"][33], {"plakaKodu": 34, "il": "İstanbul"})
        self.assertEqual(data["veri"][-1], {"plakaKodu": 81, "il": "Düzce"})

    def test_iller_endpoint_mgm_ye_istek_atmaz(self):
        # FakeMGM'de il_istasyonlari çağrılırsa should_fail_health true iken
        # hata gönderir /iller bu metodu hiç çağırmadığı için 200 dönmeli
        app_module.mgm = FakeMGM(should_fail_health=True)
        resp = self.client.get("/iller")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()["veri"]), 81)

    def test_ara_basarili_sorgu_hava_durumu_doner(self):
        resp = self.client.get("/ara?q=istanbul")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["basarili"])
        self.assertEqual(data["veri"]["durum"], "cozuldu")
        self.assertEqual(data["veri"]["guncel"]["kaynak"], "mgm")

    def test_ara_q_parametresi_yoksa_400_doner(self):
        resp = self.client.get("/ara")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()["basarili"])

    def test_ara_bos_q_400_doner(self):
        resp = self.client.get("/ara?q=")
        self.assertEqual(resp.status_code, 400)

    def test_ara_cozulemeyen_sorgu_404_doner(self):
        resp = self.client.get("/ara?q=bulunamayan+sorgu")
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(resp.get_json()["basarili"])

    def test_ara_belirsiz_sorgu_200_ile_secenek_doner(self):
        resp = self.client.get("/ara?q=belirsiz+sorgu")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["basarili"])
        self.assertEqual(data["veri"]["durum"], "belirsiz")
        self.assertEqual(len(data["veri"]["secenekler"]), 2)

    def test_uyarilar_basarili_ham_ve_not_doner(self):
        resp = self.client.get("/uyarilar")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["basarili"])
        self.assertIn("ham", data["veri"])
        self.assertIn("not", data["veri"])

    def test_uyarilar_il_parametresiyle_calisir(self):
        resp = self.client.get("/uyarilar?il=istanbul")
        self.assertEqual(resp.status_code, 200)

    def test_uyarilar_mgm_hatasinda_502_doner(self):
        resp = self.client.get("/uyarilar?il=coken-il")
        self.assertEqual(resp.status_code, 502)
        self.assertFalse(resp.get_json()["basarili"])

    def test_konum_basarili_istekte_200_doner(self):
        resp = self.client.get("/konum?lat=40.99&lon=29.02")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["basarili"])
        self.assertEqual(data["veri"]["yontem"], "nominatim-mgm")

    def test_konum_parametre_eksikse_400_doner(self):
        self.assertEqual(self.client.get("/konum").status_code, 400)
        self.assertEqual(self.client.get("/konum?lat=40.99").status_code, 400)
        self.assertEqual(self.client.get("/konum?lon=29.02").status_code, 400)

    def test_konum_gecersiz_sayida_400_doner(self):
        resp = self.client.get("/konum?lat=abc&lon=29.02")
        self.assertEqual(resp.status_code, 400)

    def test_konum_aralik_disinda_400_doner(self):
        self.assertEqual(self.client.get("/konum?lat=999&lon=29.02").status_code, 400)
        self.assertEqual(self.client.get("/konum?lat=40.99&lon=999").status_code, 400)

    def test_konum_hata_verirse_502_doner(self):
        resp = self.client.get("/konum?lat=77.0&lon=29.02")
        self.assertEqual(resp.status_code, 502)
        self.assertFalse(resp.get_json()["basarili"])

    def test_toplu_basarili_sorgular_dogru_sirada_doner(self):
        resp = self.client.post("/toplu", json={"sorgular": ["istanbul", "izmir"]})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["basarili"])
        self.assertEqual(len(data["veri"]), 2)
        self.assertEqual(data["veri"][0]["sorgu"], "istanbul")
        self.assertTrue(data["veri"][0]["basarili"])
        self.assertEqual(data["veri"][1]["sorgu"], "izmir")

    def test_toplu_kismi_basarisizlik_digerlerini_etkilemez(self):
        resp = self.client.post(
            "/toplu", json={"sorgular": ["istanbul", "bulunamayan sorgu", "ankara"]}
        )
        self.assertEqual(resp.status_code, 200)  # batch'in kendisi başarılı
        veri = resp.get_json()["veri"]
        self.assertEqual(len(veri), 3)
        self.assertTrue(veri[0]["basarili"])
        self.assertFalse(veri[1]["basarili"])
        self.assertIn("hata", veri[1])
        self.assertTrue(veri[2]["basarili"])

    def test_toplu_json_olmayan_govde_400_doner(self):
        resp = self.client.post("/toplu", data="ben json degilim")
        self.assertEqual(resp.status_code, 400)

    def test_toplu_sorgular_alani_eksikse_400_doner(self):
        resp = self.client.post("/toplu", json={})
        self.assertEqual(resp.status_code, 400)

    def test_toplu_bos_liste_400_doner(self):
        resp = self.client.post("/toplu", json={"sorgular": []})
        self.assertEqual(resp.status_code, 400)

    def test_toplu_liste_degilse_400_doner(self):
        resp = self.client.post("/toplu", json={"sorgular": "istanbul"})
        self.assertEqual(resp.status_code, 400)

    def test_toplu_bos_string_iceren_liste_400_doner(self):
        resp = self.client.post("/toplu", json={"sorgular": ["istanbul", "  "]})
        self.assertEqual(resp.status_code, 400)

    def test_toplu_limit_asilirsa_400_doner(self):
        app_module.TOPLU_MAX_SORGU = 3
        try:
            resp = self.client.post("/toplu", json={"sorgular": ["a", "b", "c", "d"]})
            self.assertEqual(resp.status_code, 400)
        finally:
            app_module.TOPLU_MAX_SORGU = 20

    def test_toplu_cors_methods_headerinda_post_var(self):
        resp = self.client.post("/toplu", json={"sorgular": ["istanbul"]})
        self.assertIn("POST", resp.headers.get("Access-Control-Allow-Methods", ""))

    def test_metrics_200_ve_prometheus_formatinda(self):
        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 200)
        gövde = resp.get_data(as_text=True)
        self.assertIn("http_requests_total", gövde)
        self.assertIn("mgm_circuit_breaker_state", gövde)

    def test_metrics_rate_limite_tabi_degil(self):
        app_module.RATE_LIMIT_MAX = 1
        app_module.RATE_LIMIT_BUCKETS.clear()
        try:
            for _ in range(5):
                self.assertEqual(self.client.get("/metrics").status_code, 200)
        finally:
            app_module.RATE_LIMIT_MAX = 60
            app_module.RATE_LIMIT_BUCKETS.clear()

    def test_metrics_gecmis_istekleri_sayar(self):
        self.client.get("/iller")
        self.client.get("/iller")
        gövde = self.client.get("/metrics").get_data(as_text=True)
        self.assertRegex(
            gövde, r'http_requests_total\{endpoint="iller",method="GET",status="200"\} \d+\.0'
        )

    def test_openapi_yaml_gecerli_yaml_doner(self):
        resp = self.client.get("/openapi.yaml")
        self.assertEqual(resp.status_code, 200)
        import yaml

        spec = yaml.safe_load(resp.get_data(as_text=True))
        self.assertEqual(spec["openapi"], "3.0.3")
        self.assertIn("/hava-durumu/{il}", spec["paths"])
        self.assertIn("/health", spec["paths"])

    def test_docs_swagger_ui_html_doner(self):
        resp = self.client.get("/docs")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.content_type)
        body = resp.get_data(as_text=True)
        self.assertIn("swagger-ui", body)
        self.assertIn("/openapi.yaml", body)

    def test_docs_ve_openapi_rate_limite_tabi_degil(self):
        app_module.RATE_LIMIT_MAX = 1
        app_module.RATE_LIMIT_BUCKETS.clear()
        try:
            for _ in range(5):
                self.assertEqual(self.client.get("/docs").status_code, 200)
                self.assertEqual(self.client.get("/openapi.yaml").status_code, 200)
        finally:
            app_module.RATE_LIMIT_MAX = 60

    def test_docs_muaf_yollarda_ratelimit_headerlari_yok(self):
        resp = self.client.get("/docs")
        self.assertNotIn("X-RateLimit-Limit", resp.headers)
        self.assertNotIn("X-RateLimit-Remaining", resp.headers)
        self.assertNotIn("X-RateLimit-Reset", resp.headers)

    def test_ratelimit_headerlari_basarili_yanitta_dogru_hesaplanir(self):
        app_module.RATE_LIMIT_MAX = 5
        app_module.RATE_LIMIT_WINDOW = 60
        app_module.RATE_LIMIT_BUCKETS.clear()
        try:
            resp1 = self.client.get("/iller")
            self.assertEqual(resp1.headers["X-RateLimit-Limit"], "5")
            self.assertEqual(resp1.headers["X-RateLimit-Remaining"], "4")

            resp2 = self.client.get("/iller")
            self.assertEqual(resp2.headers["X-RateLimit-Remaining"], "3")

            # reset epoch şimdiki zamandan ileride olmalı
            simdi = int(time.time())
            reset = int(resp2.headers["X-RateLimit-Reset"])
            self.assertGreater(reset, simdi)
            self.assertLessEqual(reset, simdi + 61)
        finally:
            app_module.RATE_LIMIT_MAX = 60
            app_module.RATE_LIMIT_WINDOW = 60
            app_module.RATE_LIMIT_BUCKETS.clear()

    def test_ratelimit_headerlari_429_yanitinda_da_var_ve_remaining_sifir(self):
        app_module.RATE_LIMIT_MAX = 1
        app_module.RATE_LIMIT_BUCKETS.clear()
        try:
            self.assertEqual(self.client.get("/iller").status_code, 200)
            resp = self.client.get("/iller")
            self.assertEqual(resp.status_code, 429)
            self.assertEqual(resp.headers["X-RateLimit-Remaining"], "0")
            self.assertEqual(resp.headers["X-RateLimit-Limit"], "1")
            self.assertIn("Retry-After", resp.headers)
        finally:
            app_module.RATE_LIMIT_MAX = 60
            app_module.RATE_LIMIT_BUCKETS.clear()

    def test_iller_gzip_ile_istenince_sikistirilmis_doner(self):
        import gzip
        import json

        resp = self.client.get("/iller", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("Content-Encoding"), "gzip")
        acilmis = json.loads(gzip.decompress(resp.data))
        self.assertEqual(len(acilmis["veri"]), 81)

    def test_iller_gzip_istenmezse_duz_metin_doner(self):
        resp = self.client.get("/iller")
        self.assertIsNone(resp.headers.get("Content-Encoding"))
        self.assertEqual(len(resp.get_json()["veri"]), 81)

    def test_hava_durumu_endpoint(self):
        resp = self.client.get("/hava-durumu/Istanbul?ilce=Bakirkoy")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["basarili"])
        self.assertEqual(data["veri"]["ilce"], "Bakirkoy")

    def test_saatlik_endpoint(self):
        resp = self.client.get("/saatlik/Istanbul?ilce=Bakirkoy")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["basarili"])
        self.assertEqual(data["veri"], [{"gun": "2026-08-14", "saat": "12:00", "sicaklik": 27.0}])

    def test_cors_ve_guvenlik_headerlari(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")
        self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(resp.headers.get("Referrer-Policy"), "no-referrer")

    def test_rate_limit_429(self):
        app_module.RATE_LIMIT_MAX = 1
        app_module.RATE_LIMIT_BUCKETS.clear()

        first = self.client.get("/istasyonlar/Istanbul")
        second = self.client.get("/istasyonlar/Istanbul")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)

        app_module.RATE_LIMIT_MAX = 60
        app_module.RATE_LIMIT_BUCKETS.clear()

    def test_rate_limit_rota_kapsamlari_beklenen_degerlerdedir(self):
        beklenen = {
            ("GET", "/hava-durumu/Istanbul"): ("hava-durumu", 60),
            ("POST", "/toplu"): ("toplu", 10),
            ("GET", "/map/geojson"): ("map-geojson", 2),
            ("GET", "/gecmis"): ("gecmis", 10),
            ("GET", "/sondurum/toplam-yagis"): ("gecmis", 10),
            ("POST", "/alerts/liste"): ("alerts", 10),
            ("POST", "/webhook/test"): ("webhook-test", 3),
        }
        for (method, path), expected in beklenen.items():
            with self.subTest(method=method, path=path):
                self.assertEqual(app_module._rota_rate_limit_ayari(method, path), expected)

    def test_guvenilmeyen_proxy_x_forwarded_for_basligini_yok_sayar(self):
        eski_aglar = app_module.TRUSTED_PROXY_NETWORKS
        app_module.TRUSTED_PROXY_NETWORKS = ()
        try:
            with app_module.app.test_request_context(
                "/iller",
                environ_base={"REMOTE_ADDR": "198.51.100.10"},
                headers={"X-Forwarded-For": "203.0.113.10"},
            ):
                self.assertEqual(app_module._istemci_ip(), "198.51.100.10")
        finally:
            app_module.TRUSTED_PROXY_NETWORKS = eski_aglar

    def test_guvenilen_proxy_x_forwarded_for_zincirinden_istemciyi_secer(self):
        eski_aglar = app_module.TRUSTED_PROXY_NETWORKS
        app_module.TRUSTED_PROXY_NETWORKS = (app_module.ipaddress.ip_network("10.0.0.0/8"),)
        try:
            with app_module.app.test_request_context(
                "/iller",
                environ_base={"REMOTE_ADDR": "10.0.0.2"},
                headers={"X-Forwarded-For": "198.51.100.10, 10.0.0.3"},
            ):
                self.assertEqual(app_module._istemci_ip(), "198.51.100.10")
        finally:
            app_module.TRUSTED_PROXY_NETWORKS = eski_aglar

    def test_json_govde_limiti_413_doner(self):
        eski_limit = app_module.MAX_JSON_BODY_BYTES
        eski_flask_limiti = app_module.app.config["MAX_CONTENT_LENGTH"]
        app_module.MAX_JSON_BODY_BYTES = 10
        app_module.app.config["MAX_CONTENT_LENGTH"] = 10
        try:
            response = self.client.post("/toplu", json={"sorgular": ["istanbul"]})
            self.assertEqual(response.status_code, 413)
        finally:
            app_module.MAX_JSON_BODY_BYTES = eski_limit
            app_module.app.config["MAX_CONTENT_LENGTH"] = eski_flask_limiti

    def test_response_boyutu_limiti_413_doner(self):
        eski_limit = app_module.MAX_RESPONSE_BYTES
        app_module.MAX_RESPONSE_BYTES = 10
        try:
            response = self.client.get("/iller")
            self.assertEqual(response.status_code, 413)
        finally:
            app_module.MAX_RESPONSE_BYTES = eski_limit

    def test_gecmis_tarih_araligi_31_gunu_asilamaz(self):
        response = self.client.get("/gecmis?start=2026-01-01&end=2026-02-02")
        self.assertEqual(response.status_code, 400)
        self.assertIn("31", response.get_json()["hata"])

    def test_webhook_url_uzunlugu_sinirlanir(self):
        with self.assertRaises(app_module.AlertHatasi):
            app_module._alert_ekle(
                "test-listesi",
                {
                    "tur": "weather.rain_started",
                    "il": "İstanbul",
                    "webhookUrl": "https://example.test/" + "a" * app_module.MAX_WEBHOOK_URL_LENGTH,
                },
            )


class TestListeYetkilendirme(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        with app_module._LISTE_YETKI_BELLEK_KILIT:
            app_module._LISTE_YETKI_BELLEK.clear()
        with app_module._FAVORI_BELLEK_KILIT:
            app_module._FAVORI_BELLEK.clear()
        with app_module._ALERT_BELLEK_KILIT:
            app_module._ALERT_BELLEK.clear()
            app_module._ALERT_LISTE_INDEX.clear()

    def tearDown(self):
        with app_module._LISTE_YETKI_BELLEK_KILIT:
            app_module._LISTE_YETKI_BELLEK.clear()
        with app_module._FAVORI_BELLEK_KILIT:
            app_module._FAVORI_BELLEK.clear()
        with app_module._ALERT_BELLEK_KILIT:
            app_module._ALERT_BELLEK.clear()
            app_module._ALERT_LISTE_INDEX.clear()

    def _liste_olustur(self):
        response = self.client.post("/favoriler", json={"listeId": "yetkili-liste"})
        self.assertEqual(response.status_code, 201)
        return response.get_json()["veri"]

    def test_liste_olusturma_manage_ve_read_token_doner_hash_saklar(self):
        data = self._liste_olustur()

        self.assertEqual(data["listeId"], "yetkili-liste")
        self.assertTrue(data["manage_token"])
        self.assertTrue(data["read_token"])
        self.assertNotEqual(data["manage_token"], data["read_token"])
        hashes = app_module._LISTE_YETKI_BELLEK["yetkili-liste"]
        self.assertNotIn(data["manage_token"], hashes.values())
        self.assertEqual(hashes["manage_token_hash"], app_module._token_hash(data["manage_token"]))
        self.assertEqual(hashes["read_token_hash"], app_module._token_hash(data["read_token"]))

    def test_favori_okuma_read_token_ile_yazma_manage_token_ile_yapilir(self):
        data = self._liste_olustur()
        read_headers = {"Authorization": f"Bearer {data['read_token']}"}
        manage_headers = {"Authorization": f"Bearer {data['manage_token']}"}

        self.assertEqual(
            self.client.post(
                "/favoriler/yetkili-liste",
                json={"sorgu": "istanbul"},
                headers=read_headers,
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/favoriler/yetkili-liste",
                json={"sorgu": "istanbul"},
                headers=manage_headers,
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get("/favoriler/yetkili-liste/liste", headers=read_headers).status_code,
            200,
        )
        self.assertEqual(
            self.client.get("/favoriler/yetkili-liste/liste", headers=manage_headers).status_code,
            401,
        )

    def test_post_favoriler_manage_token_ile_liste_id_uzerinden_ekler(self):
        data = self._liste_olustur()
        response = self.client.post(
            "/favoriler",
            json={"listeId": data["listeId"], "sorgu": "istanbul"},
            headers={"Authorization": f"Bearer {data['manage_token']}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["veri"]["sorgu"], "istanbul")

    def test_favori_ve_alert_rotalari_token_olmadan_401_doner(self):
        self._liste_olustur()
        self.assertEqual(
            self.client.post("/favoriler/yetkili-liste", json={"sorgu": "istanbul"}).status_code,
            401,
        )
        self.assertEqual(self.client.get("/favoriler/yetkili-liste/liste").status_code, 401)
        self.assertEqual(self.client.get("/alerts/yetkili-liste").status_code, 401)
        self.assertEqual(
            self.client.post(
                "/alerts/yetkili-liste",
                json={
                    "tur": "weather.rain_started",
                    "il": "İstanbul",
                    "webhookUrl": "https://example.test/webhook",
                },
            ).status_code,
            401,
        )

    def test_alert_okuma_read_token_yazma_manage_token_ile_yapilir(self):
        data = self._liste_olustur()
        read_headers = {"Authorization": f"Bearer {data['read_token']}"}
        manage_headers = {"Authorization": f"Bearer {data['manage_token']}"}
        body = {
            "tur": "weather.rain_started",
            "il": "İstanbul",
            "webhookUrl": "https://example.test/webhook",
        }

        self.assertEqual(
            self.client.post("/alerts/yetkili-liste", json=body, headers=read_headers).status_code,
            401,
        )
        response = self.client.post("/alerts/yetkili-liste", json=body, headers=manage_headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/alerts/yetkili-liste", headers=read_headers).status_code, 200)


class TestAlertTransitions(unittest.TestCase):
    def setUp(self):
        with app_module._ALERT_BELLEK_KILIT:
            app_module._ALERT_BELLEK.clear()
            app_module._ALERT_LISTE_INDEX.clear()

    def tearDown(self):
        with app_module._ALERT_BELLEK_KILIT:
            app_module._ALERT_BELLEK.clear()
            app_module._ALERT_LISTE_INDEX.clear()

    def _alert_olustur(self, tur: str, esik: float | None = None) -> dict:
        govde = {
            "tur": tur,
            "il": "İstanbul",
            "webhookUrl": "https://example.test/webhook",
        }
        if esik is not None:
            govde["esik"] = esik
        return app_module._alert_ekle("test-listesi", govde)

    def _kontrol_et(self, observations: list[dict | Exception], alert: dict) -> list[dict]:
        app_module.mgm = FakeMGM(alert_observations=observations)
        with patch.object(app_module, "_alert_webhook_gonder", return_value=True) as webhook:
            results = []
            for _ in observations:
                results.append(app_module._alert_kontrol_calistir())
            alert["webhook_calls"] = webhook.call_count
        return results

    def test_yagis_yokken_yagis_baslayinca_bir_kez_tetiklenir(self):
        alert = self._alert_olustur("weather.rain_started")
        results = self._kontrol_et(
            [{"durumKodu": "A"}, {"durumKodu": "Y"}], alert
        )

        self.assertEqual([result["tetiklenen"] for result in results], [0, 1])
        self.assertEqual(alert["webhook_calls"], 1)
        self.assertTrue(alert["sonDurum"]["yagisli"])

    def test_yagis_devam_ederken_tekrar_tetiklenmez(self):
        alert = self._alert_olustur("weather.rain_started")
        results = self._kontrol_et(
            [{"durumKodu": "Y"}, {"durumKodu": "Y"}], alert
        )

        self.assertEqual([result["tetiklenen"] for result in results], [0, 0])
        self.assertEqual(alert["webhook_calls"], 0)

    def test_yagis_varken_yagis_durunca_tetiklenir(self):
        alert = self._alert_olustur("weather.rain_stopped")
        results = self._kontrol_et(
            [{"durumKodu": "Y"}, {"durumKodu": "A"}], alert
        )

        self.assertEqual([result["tetiklenen"] for result in results], [0, 1])
        self.assertEqual(alert["webhook_calls"], 1)
        self.assertFalse(alert["sonDurum"]["yagisli"])

    def test_esik_altindan_esik_ustune_gecince_tetiklenir(self):
        alert = self._alert_olustur("weather.rain_threshold", esik=5)
        results = self._kontrol_et(
            [{"yagis": 2}, {"yagis": 6}], alert
        )

        self.assertEqual([result["tetiklenen"] for result in results], [0, 1])
        self.assertEqual(alert["webhook_calls"], 1)

    def test_esik_ustu_kalmaya_devam_ederse_her_kontrolde_tetiklenir(self):
        alert = self._alert_olustur("weather.rain_threshold", esik=5)
        results = self._kontrol_et(
            [{"yagis": 6}, {"yagis": 7}], alert
        )

        self.assertEqual([result["tetiklenen"] for result in results], [1, 1])
        self.assertEqual(alert["webhook_calls"], 2)

    def test_mgm_geri_gelince_bilinen_yagis_durumu_korunur(self):
        alert = self._alert_olustur("weather.rain_started")
        results = self._kontrol_et(
            [
                {"durumKodu": "A"},
                MGMWeatherError("MGM verisi yok"),
                {"durumKodu": "Y"},
            ],
            alert,
        )

        self.assertEqual([result["tetiklenen"] for result in results], [0, 0, 1])
        self.assertEqual(alert["webhook_calls"], 1)
        self.assertTrue(alert["sonDurum"]["yagisli"])


class _FakeWebhookResponse:
    def __init__(self, chunks: list[bytes], headers: dict[str, str] | None = None, status_code: int = 200):
        self.chunks = chunks
        self.headers = headers or {}
        self.status_code = status_code
        self.closed = False

    def iter_content(self, chunk_size: int):
        return iter(self.chunks)

    def close(self):
        self.closed = True


class TestWebhookSSRF(unittest.TestCase):
    def _alert(self, url: str = "https://webhook.example/path") -> dict:
        return {
            "id": "alert-id",
            "tur": "weather.rain_started",
            "il": "İstanbul",
            "ilce": None,
            "webhookUrl": url,
            "esik": None,
        }

    def test_webhook_kaydi_sadece_https_ve_izinli_portu_kabul_eder(self):
        for url in ("http://webhook.example", "https://webhook.example:8443"):
            with self.subTest(url=url), self.assertRaises(app_module.AlertHatasi):
                app_module._alert_ekle(
                    "test-listesi",
                    {
                        "tur": "weather.rain_started",
                        "il": "İstanbul",
                        "webhookUrl": url,
                    },
                )

    def test_webhook_ssrf_hedeflerini_reddeder(self):
        hedefler = (
            "localhost",
            "127.0.0.1",
            "::1",
            "0.0.0.0",
            "10.1.2.3",
            "172.16.1.2",
            "192.168.1.2",
            "169.254.169.254",
            "fc00::1",
            "fe80::1",
        )
        for hedef in hedefler:
            url = f"https://[{hedef}]" if ":" in hedef else f"https://{hedef}"
            with self.subTest(hedef=hedef), patch.object(
                app_module.socket,
                "getaddrinfo",
                return_value=[(app_module.socket.AF_INET, app_module.socket.SOCK_STREAM, 6, "", (hedef, 443))],
            ), self.assertRaises(app_module.AlertHatasi):
                app_module._webhook_hedefini_dogrula(url)

    def test_webhook_dns_sonuclarinin_tumu_guvenli_olmali(self):
        adresler = [
            (app_module.socket.AF_INET, app_module.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (app_module.socket.AF_INET, app_module.socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443)),
        ]
        with patch.object(app_module.socket, "getaddrinfo", return_value=adresler), self.assertRaises(
            app_module.AlertHatasi
        ):
            app_module._webhook_hedefini_dogrula("https://webhook.example")

    def test_webhook_istegi_redirectsiz_timeoutlu_ve_stream_olarak_gonderilir(self):
        response = _FakeWebhookResponse([b"ok"])
        with patch.object(
            app_module.socket,
            "getaddrinfo",
            return_value=[
                (app_module.socket.AF_INET, app_module.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
            ],
        ), patch.object(app_module.requests, "post", return_value=response) as post:
            self.assertTrue(app_module._alert_webhook_gonder(self._alert(), {"yagisli": True}))

        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["timeout"], app_module.ALERT_WEBHOOK_TIMEOUT)
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertTrue(post.call_args.kwargs["stream"])
        self.assertTrue(response.closed)

    def test_webhook_response_content_length_limiti_asilirsa_reddedilir(self):
        response = _FakeWebhookResponse(
            [b"ok"],
            headers={"Content-Length": str(app_module.ALERT_WEBHOOK_MAX_RESPONSE_BYTES + 1)},
        )
        with patch.object(
            app_module,
            "_webhook_hedefini_dogrula",
            return_value=None,
        ), patch.object(app_module.requests, "post", return_value=response):
            self.assertFalse(app_module._alert_webhook_gonder(self._alert(), {}))
        self.assertTrue(response.closed)

    def test_webhook_response_stream_limiti_asilirsa_reddedilir(self):
        response = _FakeWebhookResponse([b"x" * (app_module.ALERT_WEBHOOK_MAX_RESPONSE_BYTES + 1)])
        with patch.object(
            app_module,
            "_webhook_hedefini_dogrula",
            return_value=None,
        ), patch.object(app_module.requests, "post", return_value=response):
            self.assertFalse(app_module._alert_webhook_gonder(self._alert(), {}))
        self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
