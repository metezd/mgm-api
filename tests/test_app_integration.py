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


if __name__ == "__main__":
    unittest.main()
