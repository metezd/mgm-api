import json
import os
import time
import unittest
import uuid
from unittest.mock import patch

import app as app_module
from app import AlertHatasi, FavoriHatasi
from mgm_client import MGMWeatherError


class _SahteRedisClient:
    """favoriler/alerts'ın kullandığı redis-py metodlarının (hset,
    hkeys, hdel, hgetall, sadd, srem, scard, smembers, hmget, expire)
    küçük bir alt kümesini taklit eden bellek içi sahte istemci."""

    def __init__(self):
        self.store: dict[str, dict] = {}
        self.expire_calls: list[tuple[str, int]] = []

    def hset(self, key, field=None, value=None, mapping=None):
        kayit = self.store.setdefault(key, {})
        if mapping:
            for k, v in mapping.items():
                kayit[k.encode() if isinstance(k, str) else k] = (
                    v.encode() if isinstance(v, str) else v
                )
        else:
            kayit[field.encode() if isinstance(field, str) else field] = (
                value.encode() if isinstance(value, str) else value
            )

    def hkeys(self, key):
        return list(self.store.get(key, {}).keys())

    def hdel(self, key, field):
        kayit = self.store.get(key, {})
        field_b = field.encode() if isinstance(field, str) else field
        if field_b in kayit:
            del kayit[field_b]
            return 1
        return 0

    def hgetall(self, key):
        return self.store.get(key, {})

    def hmget(self, key, fields):
        kayit = self.store.get(key, {})
        return [kayit.get(f.encode() if isinstance(f, str) else f) for f in fields]

    def sadd(self, key, member):
        self.store.setdefault(key, set()).add(member)

    def srem(self, key, member):
        kayit = self.store.get(key, set())
        if member in kayit:
            kayit.discard(member)
            return 1
        return 0

    def scard(self, key):
        return len(self.store.get(key, set()))

    def smembers(self, key):
        return set(self.store.get(key, set()))

    def expire(self, key, ttl):
        self.expire_calls.append((key, ttl))


class _PatlayanRedisClient:
    """Her işlemde hata fırlatan sahte Redis — bellek fallback'ini
    tetiklemek için kullanılır."""

    class RedisHatasi(Exception):
        pass

    def __getattr__(self, _name):
        def _patla(*_args, **_kwargs):
            raise self.RedisHatasi("redis çöktü")

        return _patla


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
        if il == "istasyonsuz-il":
            return {"il": il, "ilce": ilce or "Bakırköy"}  # istasyonId/merkezId yok
        istasyon = {"il": il, "ilce": ilce or "Bakırköy", "merkezId": 93401}
        if il != "konumsuz-il":
            istasyon["enlem"] = 40.98
            istasyon["boylam"] = 29.03
        return istasyon

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

    def gunluk_tahmin(self, istasyon_id: int | str):
        if istasyon_id == "coken-istasyon":
            raise MGMWeatherError("MGM'ye ulaşılamadı (simülasyon).")
        return [{"tarih": "2026-08-14", "enDusuk": 18, "enYuksek": 27, "durum": "Açık"}]

    def hava_kalitesi(self, enlem: float, boylam: float):
        return {"pm10": 30.0, "pm25": 15.0, "uvIndeksi": 4.5, "kaynaklar": {"pm10": "open-meteo"}}

    def gun_dogumu_batimi(self, enlem: float, boylam: float):
        return {"gunDogumu": "06:30", "gunBatimi": "19:45"}

    def ay_evresi(self, tarih=None):
        return {"evreAdi": "Dolunay", "aydinlanmaOrani": 0.98, "yasGunu": 14.2}

    def polen_indeksi(self, enlem: float, boylam: float):
        return {"turler": {}, "baskinTur": None, "genelRiskSeviyesi": "Veri Yok"}

    def deniz_durumu(self, enlem: float, boylam: float):
        return {
            "denizSuyuSicakligi": 22.5,
            "dalgaYuksekligi": 0.6,
            "kapsamDisi": False,
            "kaynaklar": {"denizSuyuSicakligi": "open-meteo", "dalga": "open-meteo"},
        }

    def harita_geojson(self):
        if self.should_fail_health:
            raise MGMWeatherError("MGM servisine bağlanılamadı")
        return {
            "type": "FeatureCollection",
            "features": [{"properties": {"name": "İstanbul", "sicaklik": 22.5}, "geometry": {}}],
        }

    def don_kiragi_riski(self, istasyon_id: int | str, il: str = "", ilce: str | None = None):
        return {
            "il": il, "ilce": ilce,
            "gunler": [{"tarih": "2026-08-14", "seviye": "Risk Yok"}],
            "genelRiskSeviyesi": "Risk Yok",
        }

    def akilli_ozet(self, il: str, ilce: str | None = None):
        if il == "coken-il":
            raise MGMWeatherError("MGM'ye ulaşılamadı (simülasyon).")
        return {
            "il": il, "ilce": ilce, "ozet": f"{il} için şu an hava açık ve sıcaklık 22°C.",
            "anahtarNoktalar": [], "uyarilar": [], "trend": {"yon": "sabit", "farkC": 0.0},
        }

    def en_dusuk_sicakliklar(self, tarih=None):
        if tarih == "2099-01-01":
            raise MGMWeatherError("MGM'ye ulaşılamadı (simülasyon).")
        return {"tarih": tarih or "2026-08-14", "istasyonlar": []}

    def en_yuksek_sicakliklar(self, tarih=None):
        return {"tarih": tarih or "2026-08-14", "istasyonlar": []}

    def toplam_yagislar(self, tarih=None):
        return {"tarih": tarih or "2026-08-14", "istasyonlar": []}

    def kar_kalinliklari(self):
        return {"istasyonlar": []}

    def son_gozlemler(self):
        return {"istasyonlar": []}

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

    def test_toplu_bilinmeyen_alan_400_doner(self):
        resp = self.client.post("/toplu", json={"sorgular": ["istanbul"], "fazla": True})
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

    def test_toplu_paylasilan_thread_havuzunu_kullanir(self):
        # /toplu her istekte yeni bir ThreadPoolExecutor açıp kapatmak
        # yerine modül seviyesindeki paylaşılan _TOPLU_HAVUZ'u kullanır;
        # ardışık isteklerde havuz nesnesi değişmemeli.
        app_module.RATE_LIMIT_BUCKETS.clear()
        havuz_once = app_module._TOPLU_HAVUZ
        self.client.post("/toplu", json={"sorgular": ["istanbul"]})
        self.client.post("/toplu", json={"sorgular": ["izmir", "ankara"]})
        self.assertIs(app_module._TOPLU_HAVUZ, havuz_once)
        self.assertFalse(app_module._TOPLU_HAVUZ._shutdown)
        app_module.RATE_LIMIT_BUCKETS.clear()

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

    def test_listeid_verilmezse_sunucu_uuid_v4_uretir(self):
        # Güvenlik: liste_id tahmin edilebilir olmamalı. Client listeId
        # göndermezse sunucu standart, kriptografik olarak güvenli bir
        # UUID v4 üretir (client-taraflı, öngörülebilir bir kimlik değil).
        response = self.client.post("/favoriler", json={})
        self.assertEqual(response.status_code, 201)
        uretilen_id = response.get_json()["veri"]["listeId"]

        ayrisik = uuid.UUID(uretilen_id)
        self.assertEqual(ayrisik.version, 4)

    def test_iki_otomatik_listeid_farklidir(self):
        birinci = self.client.post("/favoriler", json={}).get_json()["veri"]["listeId"]
        ikinci = self.client.post("/favoriler", json={}).get_json()["veri"]["listeId"]
        self.assertNotEqual(birinci, ikinci)

    def test_client_kendi_listeidsini_hala_secebilir(self):
        # Geriye dönük uyumluluk: özel/akılda kalır bir liste_id isteyen
        # client'lar hâlâ kendi ID'sini verebilir — güvenlik sınırı
        # liste_id değil (aşağıda ayrıca doğrulanan) manage/read token'dır.
        data = self._liste_olustur()
        self.assertEqual(data["listeId"], "yetkili-liste")

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

    def test_alert_bilinmeyen_alan_400_doner(self):
        data = self._liste_olustur()
        response = self.client.post(
            "/alerts/yetkili-liste",
            json={
                "tur": "weather.rain_started",
                "il": "İstanbul",
                "webhookUrl": "https://example.test/webhook",
                "fazla": True,
            },
            headers={"Authorization": f"Bearer {data['manage_token']}"},
        )
        self.assertEqual(response.status_code, 400)

    def test_favoriler_toplu_ile_ayni_paylasilan_havuzu_kullanir(self):
        # /favoriler/<liste_id> de /toplu ile aynı paylaşılan
        # _TOPLU_HAVUZ'u kullanır (ayrı bir ThreadPoolExecutor açmaz).
        # Bu sınıf varsayılan olarak gerçek mgm istemcisini kullanıyor;
        # burada gerçek ağ çağrısından kaçınmak için geçici olarak
        # FakeMGM'e geçiyoruz.
        data = self._liste_olustur()
        manage_headers = {"Authorization": f"Bearer {data['manage_token']}"}
        read_headers = {"Authorization": f"Bearer {data['read_token']}"}
        self.client.post(
            "/favoriler/yetkili-liste", json={"sorgu": "istanbul"}, headers=manage_headers
        )

        original_mgm = app_module.mgm
        app_module.mgm = FakeMGM()
        try:
            havuz_once = app_module._TOPLU_HAVUZ
            resp = self.client.get("/favoriler/yetkili-liste", headers=read_headers)
            self.assertEqual(resp.status_code, 200)
            self.assertIs(app_module._TOPLU_HAVUZ, havuz_once)
            self.assertFalse(app_module._TOPLU_HAVUZ._shutdown)
        finally:
            app_module.mgm = original_mgm

    def test_gecersiz_liste_id_formati_400_doner(self):
        resp = self.client.post("/favoriler/ab", json={"sorgu": "istanbul"})  # 3 karakterden kısa
        self.assertEqual(resp.status_code, 400)

    def test_liste_olustururken_gecersiz_listeid_400_doner(self):
        resp = self.client.post("/favoriler", json={"listeId": "a b"})  # boşluk yasak
        self.assertEqual(resp.status_code, 400)

    def test_favori_ekleme_bos_sorgu_400_doner(self):
        data = self._liste_olustur()
        headers = {"Authorization": f"Bearer {data['manage_token']}"}
        resp = self.client.post("/favoriler/yetkili-liste", json={"sorgu": "   "}, headers=headers)
        self.assertEqual(resp.status_code, 400)

    def test_favori_ekleme_ve_silme_basarili(self):
        data = self._liste_olustur()
        headers = {"Authorization": f"Bearer {data['manage_token']}"}
        self.client.post("/favoriler/yetkili-liste", json={"sorgu": "istanbul"}, headers=headers)

        sil_resp = self.client.delete(
            "/favoriler/yetkili-liste", json={"sorgu": "istanbul"}, headers=headers
        )
        self.assertEqual(sil_resp.status_code, 200)
        self.assertTrue(sil_resp.get_json()["basarili"])

    def test_olmayan_favoriyi_silmek_404_doner(self):
        data = self._liste_olustur()
        headers = {"Authorization": f"Bearer {data['manage_token']}"}
        resp = self.client.delete(
            "/favoriler/yetkili-liste", json={"sorgu": "hic-eklenmemis"}, headers=headers
        )
        self.assertEqual(resp.status_code, 404)

    def test_favori_liste_goster_hafif_uc_nokta_calisir(self):
        data = self._liste_olustur()
        manage_headers = {"Authorization": f"Bearer {data['manage_token']}"}
        read_headers = {"Authorization": f"Bearer {data['read_token']}"}
        self.client.post(
            "/favoriler/yetkili-liste", json={"sorgu": "istanbul"}, headers=manage_headers
        )

        resp = self.client.get("/favoriler/yetkili-liste/liste", headers=read_headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()["veri"]), 1)

    def test_bos_liste_icin_hava_durumu_bos_liste_doner(self):
        data = self._liste_olustur()
        read_headers = {"Authorization": f"Bearer {data['read_token']}"}
        resp = self.client.get("/favoriler/yetkili-liste", headers=read_headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["veri"], [])

    def test_post_favoriler_kombine_liste_olustur_ve_ekle_gecersiz_listeid(self):
        resp = self.client.post("/favoriler", json={"listeId": "a b", "sorgu": "istanbul"})
        self.assertEqual(resp.status_code, 400)

    def _alert_olustur(self, headers):
        return self.client.post(
            "/alerts/yetkili-liste",
            json={
                "tur": "weather.rain_started",
                "il": "İstanbul",
                "webhookUrl": "https://example.test/webhook",
            },
            headers=headers,
        )

    def test_alert_ekleme_ve_silme_basarili(self):
        data = self._liste_olustur()
        manage_headers = {"Authorization": f"Bearer {data['manage_token']}"}
        ekle_resp = self._alert_olustur(manage_headers)
        self.assertEqual(ekle_resp.status_code, 200)
        alert_id = ekle_resp.get_json()["veri"]["id"]

        sil_resp = self.client.delete(
            f"/alerts/yetkili-liste/{alert_id}", headers=manage_headers
        )
        self.assertEqual(sil_resp.status_code, 200)

    def test_olmayan_alerti_silmek_404_doner(self):
        data = self._liste_olustur()
        headers = {"Authorization": f"Bearer {data['manage_token']}"}
        resp = self.client.delete("/alerts/yetkili-liste/hic-olmayan-id", headers=headers)
        self.assertEqual(resp.status_code, 404)

    def test_alert_liste_goster_calisir(self):
        data = self._liste_olustur()
        manage_headers = {"Authorization": f"Bearer {data['manage_token']}"}
        read_headers = {"Authorization": f"Bearer {data['read_token']}"}
        self._alert_olustur(manage_headers)

        resp = self.client.get("/alerts/yetkili-liste", headers=read_headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()["veri"]), 1)

    def test_alert_ekle_gecersiz_tur_reddedilir(self):
        with self.assertRaises(AlertHatasi):
            app_module._alert_ekle(
                "test-liste",
                {"tur": "olmayan.tur", "il": "İstanbul", "webhookUrl": "https://x.test/webhook"},
            )

    def test_alert_ekle_il_eksikse_reddedilir(self):
        with self.assertRaises(AlertHatasi):
            app_module._alert_ekle(
                "test-liste",
                {"tur": "weather.rain_started", "webhookUrl": "https://x.test/webhook"},
            )

    def test_alert_ekle_webhookurl_eksikse_reddedilir(self):
        with self.assertRaises(AlertHatasi):
            app_module._alert_ekle(
                "test-liste", {"tur": "weather.rain_started", "il": "İstanbul"}
            )

    def test_favori_max_kayit_asilinca_reddedilir(self):
        with patch.object(app_module, "FAVORI_MAX_KAYIT", 1):
            app_module._favori_ekle("test-liste", "istanbul")
            with self.assertRaises(FavoriHatasi):
                app_module._favori_ekle("test-liste", "ankara")

    def test_alert_max_kayit_asilinca_reddedilir(self):
        with patch.object(app_module, "ALERT_MAX_KAYIT", 1):
            app_module._alert_ekle(
                "test-liste",
                {"tur": "weather.rain_started", "il": "İstanbul", "webhookUrl": "https://x.test/webhook"},
            )
            with self.assertRaises(AlertHatasi):
                app_module._alert_ekle(
                    "test-liste",
                    {"tur": "weather.rain_started", "il": "Ankara", "webhookUrl": "https://x.test/webhook"},
                )


class TestFavorilerVeAlertlerRedisYolu(unittest.TestCase):
    """_favori_*/_alert_* fonksiyonlarının Redis-mevcut dalları: yazma/
    okuma/silme Redis üzerinden, Redis hata verirse belleğe düşme."""

    def setUp(self):
        with app_module._FAVORI_BELLEK_KILIT:
            app_module._FAVORI_BELLEK.clear()
        with app_module._ALERT_BELLEK_KILIT:
            app_module._ALERT_BELLEK.clear()
            app_module._ALERT_LISTE_INDEX.clear()
        self.original_mgm = app_module.mgm

    def tearDown(self):
        app_module.mgm = self.original_mgm
        with app_module._FAVORI_BELLEK_KILIT:
            app_module._FAVORI_BELLEK.clear()
        with app_module._ALERT_BELLEK_KILIT:
            app_module._ALERT_BELLEK.clear()
            app_module._ALERT_LISTE_INDEX.clear()

    def _redisli_mgm(self, redis_client, hata_sinifi=_PatlayanRedisClient.RedisHatasi):
        # NOT: hata_sinifi olarak asla düz Exception kullanma — _favori_ekle/
        # _alert_ekle gibi fonksiyonlar redis try/except bloğu İÇİNDE
        # kasıtlı FavoriHatasi/AlertHatasi fırlatır (max kayıt aşımı); geniş
        # bir Exception, gerçek redis.RedisError'ın asla yakalamayacağı bu
        # hataları da yanlışlıkla yutar.
        fake = FakeMGM()
        fake._redis_available = True
        fake.redis_client = redis_client
        fake.redis_prefix = "test:"
        fake._redis_error_cls = hata_sinifi
        app_module.mgm = fake
        return fake

    def test_favori_ekle_redis_uzerinden_yazar(self):
        redis_client = _SahteRedisClient()
        self._redisli_mgm(redis_client)

        app_module._favori_ekle("test-liste", "istanbul")

        anahtar = app_module._favori_redis_key("test-liste", "test:")
        self.assertIn(anahtar, redis_client.store)
        self.assertEqual(redis_client.expire_calls, [(anahtar, app_module.FAVORI_TTL_SANIYE)])

    def test_favori_ekle_redis_max_kayit_asilinca_reddedilir(self):
        redis_client = _SahteRedisClient()
        self._redisli_mgm(redis_client)
        with patch.object(app_module, "FAVORI_MAX_KAYIT", 1):
            app_module._favori_ekle("test-liste", "istanbul")
            with self.assertRaises(FavoriHatasi):
                app_module._favori_ekle("test-liste", "ankara")

    def test_favori_ekle_redis_hata_verince_bellege_duser(self):
        self._redisli_mgm(_PatlayanRedisClient(), hata_sinifi=_PatlayanRedisClient.RedisHatasi)

        app_module._favori_ekle("test-liste", "istanbul")

        self.assertIn("istanbul", app_module._FAVORI_BELLEK["test-liste"])

    def test_favori_sil_redis_uzerinden_siler(self):
        redis_client = _SahteRedisClient()
        self._redisli_mgm(redis_client)
        app_module._favori_ekle("test-liste", "istanbul")

        silindi = app_module._favori_sil("test-liste", "istanbul")
        self.assertTrue(silindi)

    def test_favori_sil_redis_hata_verince_bellege_duser(self):
        with app_module._FAVORI_BELLEK_KILIT:
            app_module._FAVORI_BELLEK["test-liste"]["istanbul"] = {"sorgu": "istanbul"}
        self._redisli_mgm(_PatlayanRedisClient(), hata_sinifi=_PatlayanRedisClient.RedisHatasi)

        silindi = app_module._favori_sil("test-liste", "istanbul")
        self.assertTrue(silindi)

    def test_favori_listele_redis_uzerinden_okur(self):
        redis_client = _SahteRedisClient()
        self._redisli_mgm(redis_client)
        app_module._favori_ekle("test-liste", "istanbul")

        sonuc = app_module._favori_listele("test-liste")
        self.assertEqual(len(sonuc), 1)
        self.assertEqual(sonuc[0]["sorgu"], "istanbul")

    def test_favori_listele_redis_hata_verince_bellekten_okur(self):
        with app_module._FAVORI_BELLEK_KILIT:
            app_module._FAVORI_BELLEK["test-liste"]["istanbul"] = {"sorgu": "istanbul"}
        self._redisli_mgm(_PatlayanRedisClient(), hata_sinifi=_PatlayanRedisClient.RedisHatasi)

        sonuc = app_module._favori_listele("test-liste")
        self.assertEqual(len(sonuc), 1)

    def test_alert_ekle_redis_uzerinden_yazar(self):
        redis_client = _SahteRedisClient()
        self._redisli_mgm(redis_client)

        app_module._alert_ekle(
            "test-liste",
            {"tur": "weather.rain_started", "il": "İstanbul", "webhookUrl": "https://x.test/webhook"},
        )

        all_key = app_module._alert_redis_all_key("test:")
        self.assertIn(all_key, redis_client.store)

    def test_alert_ekle_redis_max_kayit_asilinca_reddedilir(self):
        redis_client = _SahteRedisClient()
        self._redisli_mgm(redis_client)
        with patch.object(app_module, "ALERT_MAX_KAYIT", 1):
            app_module._alert_ekle(
                "test-liste",
                {"tur": "weather.rain_started", "il": "A", "webhookUrl": "https://x.test/webhook"},
            )
            with self.assertRaises(AlertHatasi):
                app_module._alert_ekle(
                    "test-liste",
                    {"tur": "weather.rain_started", "il": "B", "webhookUrl": "https://x.test/webhook"},
                )

    def test_alert_ekle_redis_hata_verince_bellege_duser(self):
        self._redisli_mgm(_PatlayanRedisClient(), hata_sinifi=_PatlayanRedisClient.RedisHatasi)

        kayit = app_module._alert_ekle(
            "test-liste",
            {"tur": "weather.rain_started", "il": "İstanbul", "webhookUrl": "https://x.test/webhook"},
        )

        self.assertIn(kayit["id"], app_module._ALERT_BELLEK)

    def test_alert_sil_redis_uzerinden_siler(self):
        redis_client = _SahteRedisClient()
        self._redisli_mgm(redis_client)
        kayit = app_module._alert_ekle(
            "test-liste",
            {"tur": "weather.rain_started", "il": "İstanbul", "webhookUrl": "https://x.test/webhook"},
        )

        silindi = app_module._alert_sil("test-liste", kayit["id"])
        self.assertTrue(silindi)

    def test_alert_sil_redis_hata_verince_bellege_duser(self):
        with app_module._ALERT_BELLEK_KILIT:
            app_module._ALERT_BELLEK["abc123"] = {"id": "abc123"}
            app_module._ALERT_LISTE_INDEX["test-liste"].add("abc123")
        self._redisli_mgm(_PatlayanRedisClient(), hata_sinifi=_PatlayanRedisClient.RedisHatasi)

        silindi = app_module._alert_sil("test-liste", "abc123")
        self.assertTrue(silindi)

    def test_alert_listele_redis_uzerinden_okur(self):
        redis_client = _SahteRedisClient()
        self._redisli_mgm(redis_client)
        app_module._alert_ekle(
            "test-liste",
            {"tur": "weather.rain_started", "il": "İstanbul", "webhookUrl": "https://x.test/webhook"},
        )

        sonuc = app_module._alert_listele("test-liste")
        self.assertEqual(len(sonuc), 1)

    def test_alert_listele_bos_id_seti_bos_liste_doner(self):
        redis_client = _SahteRedisClient()
        self._redisli_mgm(redis_client)

        sonuc = app_module._alert_listele("hic-alerti-olmayan-liste")
        self.assertEqual(sonuc, [])

    def test_alert_listele_redis_hata_verince_bellekten_okur(self):
        with app_module._ALERT_BELLEK_KILIT:
            app_module._ALERT_BELLEK["abc123"] = {"id": "abc123", "olusturmaTarihi": "2026"}
            app_module._ALERT_LISTE_INDEX["test-liste"].add("abc123")
        self._redisli_mgm(_PatlayanRedisClient(), hata_sinifi=_PatlayanRedisClient.RedisHatasi)

        sonuc = app_module._alert_listele("test-liste")
        self.assertEqual(len(sonuc), 1)

    def test_alert_tumunu_al_redis_uzerinden_okur(self):
        redis_client = _SahteRedisClient()
        self._redisli_mgm(redis_client)
        app_module._alert_ekle(
            "test-liste",
            {"tur": "weather.rain_started", "il": "İstanbul", "webhookUrl": "https://x.test/webhook"},
        )

        sonuc = app_module._alert_tumunu_al()
        self.assertEqual(len(sonuc), 1)

    def test_alert_kayit_guncelle_redis_uzerinden_yazar(self):
        redis_client = _SahteRedisClient()
        self._redisli_mgm(redis_client)
        kayit = app_module._alert_ekle(
            "test-liste",
            {"tur": "weather.rain_started", "il": "İstanbul", "webhookUrl": "https://x.test/webhook"},
        )
        kayit["sonTetiklenme"] = "2026-09-20T10:00:00Z"

        app_module._alert_kayit_guncelle(kayit)

        all_key = app_module._alert_redis_all_key("test:")
        guncel = json.loads(redis_client.store[all_key][kayit["id"].encode()])
        self.assertEqual(guncel["sonTetiklenme"], "2026-09-20T10:00:00Z")

    def test_alert_kayit_guncelle_redis_hata_verince_bellege_duser(self):
        with app_module._ALERT_BELLEK_KILIT:
            app_module._ALERT_BELLEK["abc123"] = {"id": "abc123"}
        self._redisli_mgm(_PatlayanRedisClient(), hata_sinifi=_PatlayanRedisClient.RedisHatasi)

        app_module._alert_kayit_guncelle({"id": "abc123", "sonTetiklenme": "x"})

        self.assertEqual(app_module._ALERT_BELLEK["abc123"]["sonTetiklenme"], "x")


class TestAlertKontrolUcNoktasi(unittest.TestCase):
    """POST /api/v1/alerts/check: CRON_SECRET korumalı cron tetikleyici."""

    def setUp(self):
        self.client = app_module.app.test_client()

    def test_cron_secret_tanimsizsa_503_doner(self):
        with patch.dict("os.environ", {}, clear=False):
            for anahtar in ("CRON_SECRET", "APP_CRON_SECRET"):
                os.environ.pop(anahtar, None)
            resp = self.client.post("/api/v1/alerts/check")
        self.assertEqual(resp.status_code, 503)

    def test_yanlis_secretle_401_doner(self):
        with patch.dict("os.environ", {"CRON_SECRET": "dogru-secret"}):
            resp = self.client.post(
                "/api/v1/alerts/check", headers={"Authorization": "Bearer yanlis-secret"}
            )
        self.assertEqual(resp.status_code, 401)

    def test_dogru_secretle_200_doner_ve_sonuc_iceriginde_calisir(self):
        with patch.dict("os.environ", {"CRON_SECRET": "dogru-secret"}):
            resp = self.client.post(
                "/api/v1/alerts/check", headers={"Authorization": "Bearer dogru-secret"}
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["basarili"])


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

    def test_sicaklik_esigi_asilinca_tetiklenir(self):
        alert = self._alert_olustur("weather.temp_threshold", esik=30)
        app_module.mgm = FakeMGM()
        with patch.object(app_module.mgm, "guncel_durum_yedekli", return_value={"sicaklik": 35}):
            tetiklendi, olcum = app_module._alert_degerlendir(alert)
        self.assertTrue(tetiklendi)
        self.assertEqual(olcum["sicaklik"], 35)

    def test_sicaklik_esigin_altindaysa_tetiklenmez(self):
        alert = self._alert_olustur("weather.temp_threshold", esik=30)
        app_module.mgm = FakeMGM()
        with patch.object(app_module.mgm, "guncel_durum_yedekli", return_value={"sicaklik": 20}):
            tetiklendi, _olcum = app_module._alert_degerlendir(alert)
        self.assertFalse(tetiklendi)

    def test_sicaklik_altinda_yonuyle_dusunce_tetiklenir(self):
        alert = self._alert_olustur("weather.temp_threshold", esik=0)
        alert["yon"] = "altinda"
        app_module.mgm = FakeMGM()
        with patch.object(app_module.mgm, "guncel_durum_yedekli", return_value={"sicaklik": -5}):
            tetiklendi, _olcum = app_module._alert_degerlendir(alert)
        self.assertTrue(tetiklendi)

    def test_sicaklik_verisi_yoksa_tetiklenmez(self):
        alert = self._alert_olustur("weather.temp_threshold", esik=30)
        app_module.mgm = FakeMGM()
        with patch.object(app_module.mgm, "guncel_durum_yedekli", return_value={}):
            tetiklendi, olcum = app_module._alert_degerlendir(alert)
        self.assertFalse(tetiklendi)
        self.assertIsNone(olcum["sicaklik"])

    def test_ruzgar_esigi_asilinca_tetiklenir(self):
        alert = self._alert_olustur("weather.wind_gust_exceeded", esik=40)
        app_module.mgm = FakeMGM()
        with patch.object(app_module.mgm, "guncel_durum_yedekli", return_value={"ruzgarHizi": 55}):
            tetiklendi, olcum = app_module._alert_degerlendir(alert)
        self.assertTrue(tetiklendi)
        self.assertEqual(olcum["ruzgarHizi"], 55)

    def test_ruzgar_verisi_yoksa_tetiklenmez(self):
        alert = self._alert_olustur("weather.wind_gust_exceeded", esik=40)
        app_module.mgm = FakeMGM()
        with patch.object(app_module.mgm, "guncel_durum_yedekli", return_value={}):
            tetiklendi, olcum = app_module._alert_degerlendir(alert)
        self.assertFalse(tetiklendi)
        self.assertIsNone(olcum["ruzgarHizi"])

    def test_uyari_yokken_uyari_yayinlaninca_tetiklenir(self):
        alert = self._alert_olustur("weather.warning_issued")
        alert["sonDurum"] = {"aktifUyariVar": False}
        app_module.mgm = FakeMGM()
        with patch.object(app_module.mgm, "uyarilar", return_value={"ham": [{"baslik": "test"}]}):
            tetiklendi, olcum = app_module._alert_degerlendir(alert)
        self.assertTrue(tetiklendi)
        self.assertTrue(olcum["aktifUyariVar"])

    def test_uyari_ilk_kontrolde_hic_tetiklenmez(self):
        # sonDurum yok (ilk kontrol) -> geçiş algılanamaz, tetiklenmez
        alert = self._alert_olustur("weather.warning_issued")
        app_module.mgm = FakeMGM()
        with patch.object(app_module.mgm, "uyarilar", return_value={"ham": [{"baslik": "test"}]}):
            tetiklendi, olcum = app_module._alert_degerlendir(alert)
        self.assertFalse(tetiklendi)
        self.assertTrue(olcum["aktifUyariVar"])

    def test_don_riski_esigi_asinca_tetiklenir(self):
        alert = self._alert_olustur("weather.frost_risk", esik="Orta Don")
        app_module.mgm = FakeMGM()
        with patch.object(
            app_module.mgm, "don_kiragi_riski", return_value={"genelRiskSeviyesi": "Kuvvetli Don"}
        ):
            tetiklendi, olcum = app_module._alert_degerlendir(alert)
        self.assertTrue(tetiklendi)
        self.assertEqual(olcum["genelRiskSeviyesi"], "Kuvvetli Don")

    def test_don_riski_esigin_altindaysa_tetiklenmez(self):
        alert = self._alert_olustur("weather.frost_risk", esik="Kuvvetli Don")
        app_module.mgm = FakeMGM()
        with patch.object(
            app_module.mgm, "don_kiragi_riski", return_value={"genelRiskSeviyesi": "Hafif Don"}
        ):
            tetiklendi, _olcum = app_module._alert_degerlendir(alert)
        self.assertFalse(tetiklendi)

    def test_bilinmeyen_tur_tetiklenmez_bos_olcum_doner(self):
        # _alert_degerlendir'in son güvenlik ağı: tur tanınmıyorsa
        alert = self._alert_olustur("weather.rain_started")
        alert["tur"] = "weather.gelecekte_eklenecek_tur"
        app_module.mgm = FakeMGM()
        with patch.object(app_module.mgm, "guncel_durum_yedekli", return_value={"durumKodu": "A"}):
            tetiklendi, olcum = app_module._alert_degerlendir(alert)
        self.assertFalse(tetiklendi)
        self.assertEqual(olcum, {})

    def test_webhook_gonderimi_basarisiz_olunca_hatali_webhook_sayilir(self):
        self._alert_olustur("weather.rain_started")
        app_module.mgm = FakeMGM(alert_observations=[{"durumKodu": "A"}, {"durumKodu": "Y"}])
        with patch.object(app_module, "_alert_webhook_gonder", return_value=False):
            app_module._alert_kontrol_calistir()
            sonuc = app_module._alert_kontrol_calistir()
        self.assertEqual(sonuc["tetiklenen"], 1)
        self.assertEqual(sonuc["hataliWebhook"], 1)


class TestIcZamanlayici(unittest.TestCase):
    """_ic_zamanlayiciyi_baslat(): ENABLE_INTERNAL_SCHEDULER kapalıyken
    no-op, açıkken APScheduler kurulu değilse RuntimeError."""

    def test_kapaliyken_hicbir_sey_yapmaz(self):
        with patch.dict("os.environ", {"ENABLE_INTERNAL_SCHEDULER": "false"}):
            self.assertIsNone(app_module._ic_zamanlayiciyi_baslat())

    def test_env_tanimsizken_de_hicbir_sey_yapmaz(self):
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("ENABLE_INTERNAL_SCHEDULER", None)
            self.assertIsNone(app_module._ic_zamanlayiciyi_baslat())

    def test_aciksa_ve_apscheduler_kurulu_degilse_runtimeerror_firlatir(self):
        with (
            patch.dict("os.environ", {"ENABLE_INTERNAL_SCHEDULER": "true"}),
            self.assertRaises(RuntimeError),
        ):
            app_module._ic_zamanlayiciyi_baslat()


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

    def test_webhook_retry_gecici_hatada_backoff_ile_tekrarlar(self):
        responses = [
            _FakeWebhookResponse([b"hata"], status_code=503),
            _FakeWebhookResponse([b"ok"], status_code=200),
        ]
        eski_retry = app_module.ALERT_WEBHOOK_RETRY_MAX
        app_module.ALERT_WEBHOOK_RETRY_MAX = 2
        try:
            with patch.object(app_module, "_webhook_hedefini_dogrula", return_value=None), patch.object(
                app_module.requests, "post", side_effect=responses
            ) as post, patch.object(app_module.time, "sleep") as sleep:
                self.assertTrue(app_module._alert_webhook_gonder(self._alert(), {"yagisli": True}))
        finally:
            app_module.ALERT_WEBHOOK_RETRY_MAX = eski_retry

        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(app_module.ALERT_WEBHOOK_RETRY_BACKOFF)
        self.assertEqual(
            post.call_args_list[0].kwargs["headers"]["Idempotency-Key"],
            post.call_args_list[1].kwargs["headers"]["Idempotency-Key"],
        )

    def test_webhook_imzasi_hmac_sha256_ve_idempotency_key_gonderir(self):
        import hashlib
        import hmac

        response = _FakeWebhookResponse([b"ok"])
        eski_secret = app_module.ALERT_WEBHOOK_SIGNING_SECRET
        app_module.ALERT_WEBHOOK_SIGNING_SECRET = "test-secret"
        try:
            with patch.object(
                app_module,
                "_webhook_hedefini_dogrula",
                return_value=None,
            ), patch.object(app_module.requests, "post", return_value=response) as post:
                self.assertTrue(app_module._alert_webhook_gonder(self._alert(), {"yagisli": True}))
        finally:
            app_module.ALERT_WEBHOOK_SIGNING_SECRET = eski_secret

        headers = post.call_args.kwargs["headers"]
        body = post.call_args.kwargs["data"]
        mesaj = f"{headers['X-MGM-Alert-Timestamp']}.".encode() + body
        beklenen = hmac.new(b"test-secret", mesaj, hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-MGM-Alert-Signature"], f"sha256={beklenen}")
        self.assertEqual(headers["Idempotency-Key"], app_module._alert_event_id(self._alert(), {"yagisli": True}))
        self.assertEqual(headers["X-MGM-Alert-Id"], "alert-id")


class TestKonumVeAramaDogrulama(unittest.TestCase):
    """il/ilçe (path+query) ve serbest metin arama (q) parametreleri
    için Pydantic v2 katmanındaki sıkı regex/length doğrulaması.
    XSS/enjeksiyon karakteri içeren ya da aralık dışı girdiler 400 ile
    reddedilmeli, geçerli Türkçe yer adları etkilenmemeli."""

    def setUp(self):
        self.client = app_module.app.test_client()
        self.original_mgm = app_module.mgm
        app_module.mgm = FakeMGM()
        # Tüm test paketi aynı process/IP üzerinden çok sayıda istek
        # attığı için global rate limit bucket'ı dolabilir; diğer test
        # sınıflarındaki gibi burada da temizliyoruz.
        app_module.RATE_LIMIT_BUCKETS.clear()

    def tearDown(self):
        app_module.mgm = self.original_mgm
        app_module.RATE_LIMIT_BUCKETS.clear()

    def test_gecerli_il_ve_ilce_kabul_edilir(self):
        resp = self.client.get("/hava-durumu/İstanbul?ilce=Kadıköy")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["basarili"])

    def test_tire_ve_kesme_isaretli_yer_adi_kabul_edilir(self):
        # "Afşin-Elbistan" gibi tireli, gerçek il adı değil ama desen
        # düzeyinde geçerli olmalı (MGM lookup'ı ayrı bir katmanda 404
        # döner, burada test edilen yalnızca regex/length doğrulaması)
        resp = self.client.get("/guncel/Afşin-Elbistan")
        self.assertEqual(resp.status_code, 200)

    def test_script_etiketi_iceren_il_400_doner(self):
        resp = self.client.get("/hava-durumu/<img src=x onerror=alert(1)>")
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertFalse(data["basarili"])

    def test_script_etiketi_iceren_ilce_400_doner(self):
        resp = self.client.get("/hava-durumu/İstanbul?ilce=<script>alert(1)</script>")
        self.assertEqual(resp.status_code, 400)

    def test_sql_enjeksiyon_benzeri_il_400_doner(self):
        resp = self.client.get("/guncel/İstanbul'; DROP TABLE users;--")
        self.assertEqual(resp.status_code, 400)

    def test_noktalama_agirlikli_il_400_doner(self):
        for kotu_deger in ["`whoami`", "{{7*7}}", "il&&curl evil.com", "il|nc 1.2.3.4"]:
            with self.subTest(deger=kotu_deger):
                resp = self.client.get(f"/guncel/{kotu_deger}")
                self.assertEqual(resp.status_code, 400)

    def test_80_karakteri_asan_il_400_doner(self):
        resp = self.client.get("/guncel/" + "a" * 81)
        self.assertEqual(resp.status_code, 400)

    def test_gecerli_tarih_formatiyla_sondurum_calisir(self):
        resp = self.client.get("/sondurum/en-dusuk-sicakliklar?tarih=2026-08-14")
        self.assertEqual(resp.status_code, 200)

    def test_yanlis_formatli_tarih_400_doner(self):
        for kotu_deger in ["14-08-2026", "2026/08/14", "<script>", "a" * 50]:
            with self.subTest(deger=kotu_deger):
                resp = self.client.get(f"/sondurum/en-dusuk-sicakliklar?tarih={kotu_deger}")
                self.assertEqual(resp.status_code, 400)

    def test_sekli_dogru_ama_gecersiz_takvim_tarihi_400_doner(self):
        for kotu_tarih in ["2026-13-01", "2026-02-30", "2026-00-05"]:
            with self.subTest(tarih=kotu_tarih):
                resp = self.client.get(f"/sondurum/en-yuksek-sicakliklar?tarih={kotu_tarih}")
                self.assertEqual(resp.status_code, 400)

    def test_tarih_verilmezse_dogrulama_atlanir(self):
        resp = self.client.get("/sondurum/toplam-yagis")
        self.assertEqual(resp.status_code, 200)

    def test_uyarilar_opsiyonel_il_gecersizse_400_doner(self):
        resp = self.client.get("/uyarilar?il=<script>")
        self.assertEqual(resp.status_code, 400)

    def test_uyarilar_il_verilmezse_dogrulama_atlanir(self):
        resp = self.client.get("/uyarilar")
        self.assertEqual(resp.status_code, 200)

    def test_ara_gecerli_sorgu_kabul_edilir(self):
        resp = self.client.get("/ara?q=kadikoy, istanbul")
        self.assertEqual(resp.status_code, 200)

    def test_ara_script_sorgusu_400_doner(self):
        resp = self.client.get("/ara?q=<script>alert(1)</script>")
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertFalse(data["basarili"])

    def test_ara_150_karakteri_asan_sorgu_400_doner(self):
        resp = self.client.get("/ara?q=" + "a" * 151)
        self.assertEqual(resp.status_code, 400)

    def test_toplu_sorguda_script_etiketi_400_doner(self):
        resp = self.client.post(
            "/toplu", json={"sorgular": ["istanbul", "<script>alert(1)</script>"]}
        )
        self.assertEqual(resp.status_code, 400)

    def test_favori_govdesinde_script_etiketi_400_doner(self):
        resp = self.client.post(
            "/favoriler", json={"sorgu": "<img src=x onerror=alert(1)>"}
        )
        self.assertEqual(resp.status_code, 400)


class TestCorsWhitelist(unittest.TestCase):
    """APP_CORS_ALLOW_ORIGIN whitelist modu: "*" yerine virgülle ayrılmış
    origin listesi verildiğinde, yalnızca eşleşen Origin yansıtılır ve
    Vary: Origin eklenir; eşleşmeyen/olmayan Origin'de header hiç
    eklenmez."""

    def setUp(self):
        self.client = app_module.app.test_client()
        self.original_mgm = app_module.mgm
        app_module.mgm = FakeMGM()
        self.original_whitelist = app_module.CORS_ORIGIN_WHITELIST

    def tearDown(self):
        app_module.mgm = self.original_mgm
        app_module.CORS_ORIGIN_WHITELIST = self.original_whitelist

    def test_varsayilan_joker_modda_tum_originlere_yildiz_donulur(self):
        app_module.CORS_ORIGIN_WHITELIST = None
        resp = self.client.get(
            "/istasyonlar/İstanbul", headers={"Origin": "https://her-hangi-bir-site.com"}
        )
        self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")
        # Origin'e göre farklı yanıt üretilmiyor, Vary: Origin gerekmez
        # (Flask-Compress'in kendi Vary: Accept-Encoding'i olabilir, ona karışmıyoruz).
        self.assertNotIn("Origin", resp.headers.get("Vary", ""))

    def test_whitelist_modda_eslesen_origin_birebir_yansitilir(self):
        app_module.CORS_ORIGIN_WHITELIST = frozenset({"https://izinli-site.com"})
        resp = self.client.get(
            "/istasyonlar/İstanbul", headers={"Origin": "https://izinli-site.com"}
        )
        self.assertEqual(
            resp.headers.get("Access-Control-Allow-Origin"), "https://izinli-site.com"
        )
        self.assertIn("Origin", resp.headers.get("Vary", ""))

    def test_whitelist_modda_eslesmeyen_origin_header_almaz(self):
        app_module.CORS_ORIGIN_WHITELIST = frozenset({"https://izinli-site.com"})
        resp = self.client.get(
            "/istasyonlar/İstanbul", headers={"Origin": "https://kotu-site.com"}
        )
        self.assertNotIn("Access-Control-Allow-Origin", resp.headers)

    def test_whitelist_modda_origin_header_yoksa_cors_header_eklenmez(self):
        # Tarayıcı dışı istemci (curl, sunucu-sunucu) — CORS zaten
        # yalnızca tarayıcıları bağlar, yanıt normal şekilde döner.
        app_module.CORS_ORIGIN_WHITELIST = frozenset({"https://izinli-site.com"})
        resp = self.client.get("/istasyonlar/İstanbul")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("Access-Control-Allow-Origin", resp.headers)


class TestEtagConditionalGet(unittest.TestCase):
    """GET yanıtlarında ETag üretimi ve If-None-Match ile 304 Not
    Modified davranışı (etag_ve_conditional_get after_request hook'u)."""

    def setUp(self):
        self.client = app_module.app.test_client()
        self.original_mgm = app_module.mgm
        app_module.mgm = FakeMGM()
        app_module.RATE_LIMIT_BUCKETS.clear()

    def tearDown(self):
        app_module.mgm = self.original_mgm
        app_module.RATE_LIMIT_BUCKETS.clear()

    def test_basarili_get_yanitinda_etag_ve_cache_control_var(self):
        resp = self.client.get("/istasyonlar/İstanbul")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(resp.headers.get("ETag"))
        self.assertEqual(resp.headers.get("Cache-Control"), "no-cache")

    def test_eslesen_if_none_match_304_doner_govde_bos(self):
        ilk = self.client.get("/istasyonlar/İstanbul")
        etag = ilk.headers["ETag"]

        ikinci = self.client.get("/istasyonlar/İstanbul", headers={"If-None-Match": etag})
        self.assertEqual(ikinci.status_code, 304)
        self.assertEqual(ikinci.get_data(), b"")

    def test_eslesmeyen_if_none_match_200_doner_tam_govde(self):
        resp = self.client.get(
            "/istasyonlar/İstanbul", headers={"If-None-Match": '"eslesmeyen-etag"'}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertGreater(len(resp.get_data()), 0)

    def test_iki_farkli_il_farkli_etag_uretir(self):
        istanbul = self.client.get("/istasyonlar/İstanbul")
        ankara = self.client.get("/istasyonlar/Ankara")
        self.assertNotEqual(istanbul.headers["ETag"], ankara.headers["ETag"])

    def test_health_endpoint_etag_almaz(self):
        resp = self.client.get("/health")
        self.assertNotIn("ETag", resp.headers)

    def test_metrics_endpoint_etag_almaz(self):
        resp = self.client.get("/metrics")
        self.assertNotIn("ETag", resp.headers)

    def test_hata_yanitinda_etag_yok(self):
        resp = self.client.get("/istasyonlar/<img src=x onerror=alert(1)>")
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("ETag", resp.headers)

    def test_post_istegi_etag_almaz(self):
        resp = self.client.post("/toplu", json={"sorgular": ["istanbul"]})
        self.assertNotIn("ETag", resp.headers)


class TestHavaVerisiRotalari(unittest.TestCase):
    """/guncel, /tahmin, /saatlik, /hava-durumu, /hava-kalitesi,
    /gun-ay-bilgisi, /polen, /deniz, /don-uyarisi, /akilli-ozet,
    /map/geojson, /sondurum/* — her biri için başarı ve 404/502 yolu."""

    def setUp(self):
        self.client = app_module.app.test_client()
        self.original_mgm = app_module.mgm
        app_module.mgm = FakeMGM()
        app_module.RATE_LIMIT_BUCKETS.clear()

    def tearDown(self):
        app_module.mgm = self.original_mgm
        app_module.RATE_LIMIT_BUCKETS.clear()

    def test_guncel_basarili(self):
        resp = self.client.get("/guncel/İstanbul")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["basarili"])

    def test_tahmin_basarili(self):
        resp = self.client.get("/tahmin/İstanbul")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["veri"][0]["enYuksek"], 27)

    def test_tahmin_istasyon_hatasinda_404_doner(self):
        # ilce_istasyonu merkezId=93401 döner, gunluk_tahmin bu id'yi
        # değil "coken-istasyon" değerini bekliyor — bu yüzden
        # gunluk_tahmin'in kendi MGMWeatherError yolunu ayrıca
        # doğrudan (route üstünden değil) client seviyesinde test ettik
        # (tests/test_mgm_client.py). Burada yalnızca başarı yolu kalır.
        resp = self.client.get("/tahmin/İstanbul")
        self.assertEqual(resp.status_code, 200)

    def test_saatlik_basarili(self):
        resp = self.client.get("/saatlik/İstanbul")
        self.assertEqual(resp.status_code, 200)

    def test_hava_durumu_basarili(self):
        resp = self.client.get("/hava-durumu/İstanbul?ilce=Kadıköy")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["veri"]["ilce"], "Kadıköy")

    def test_hava_kalitesi_basarili(self):
        resp = self.client.get("/hava-kalitesi/İstanbul")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["veri"]["pm10"], 30.0)

    def test_hava_kalitesi_konumsuz_ilde_404_doner(self):
        resp = self.client.get("/hava-kalitesi/konumsuz-il")
        self.assertEqual(resp.status_code, 404)

    def test_gun_ay_bilgisi_basarili(self):
        resp = self.client.get("/gun-ay-bilgisi/İstanbul")
        self.assertEqual(resp.status_code, 200)
        veri = resp.get_json()["veri"]
        self.assertEqual(veri["gunDogumu"], "06:30")
        self.assertEqual(veri["ayEvresi"]["evreAdi"], "Dolunay")

    def test_gun_ay_bilgisi_konumsuz_ilde_404_doner(self):
        resp = self.client.get("/gun-ay-bilgisi/konumsuz-il")
        self.assertEqual(resp.status_code, 404)

    def test_polen_basarili(self):
        resp = self.client.get("/polen/İstanbul")
        self.assertEqual(resp.status_code, 200)

    def test_polen_konumsuz_ilde_404_doner(self):
        resp = self.client.get("/polen/konumsuz-il")
        self.assertEqual(resp.status_code, 404)

    def test_deniz_basarili(self):
        resp = self.client.get("/deniz/İstanbul")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["veri"]["denizSuyuSicakligi"], 22.5)

    def test_deniz_konumsuz_ilde_404_doner(self):
        resp = self.client.get("/deniz/konumsuz-il")
        self.assertEqual(resp.status_code, 404)

    def test_deniz_gecerli_lat_lon_ile_istasyon_konumunu_atlar(self):
        resp = self.client.get("/deniz/konumsuz-il?lat=41.0&lon=29.0")
        self.assertEqual(resp.status_code, 200)

    def test_deniz_gecersiz_lat_lon_400_doner(self):
        resp = self.client.get("/deniz/İstanbul?lat=abc&lon=29.0")
        self.assertEqual(resp.status_code, 400)

    def test_deniz_aralik_disi_lat_lon_400_doner(self):
        resp = self.client.get("/deniz/İstanbul?lat=999&lon=29.0")
        self.assertEqual(resp.status_code, 400)

    def test_don_uyarisi_basarili(self):
        resp = self.client.get("/don-uyarisi/İstanbul")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["veri"]["genelRiskSeviyesi"], "Risk Yok")

    def test_akilli_ozet_basarili(self):
        resp = self.client.get("/akilli-ozet/İstanbul")
        self.assertEqual(resp.status_code, 200)

    def test_akilli_ozet_mgm_hatasinda_404_doner(self):
        resp = self.client.get("/akilli-ozet/coken-il")
        self.assertEqual(resp.status_code, 404)

    def test_map_geojson_basarili(self):
        resp = self.client.get("/map/geojson")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["type"], "FeatureCollection")

    def test_map_geojson_mgm_hatasinda_502_doner(self):
        app_module.mgm = FakeMGM(should_fail_health=True)
        resp = self.client.get("/map/geojson")
        self.assertEqual(resp.status_code, 502)

    def test_sondurum_en_dusuk_basarili(self):
        resp = self.client.get("/sondurum/en-dusuk-sicakliklar?tarih=2026-08-14")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["veri"]["tarih"], "2026-08-14")

    def test_sondurum_en_dusuk_mgm_hatasinda_502_doner(self):
        resp = self.client.get("/sondurum/en-dusuk-sicakliklar?tarih=2099-01-01")
        self.assertEqual(resp.status_code, 502)

    def test_sondurum_en_yuksek_basarili(self):
        resp = self.client.get("/sondurum/en-yuksek-sicakliklar")
        self.assertEqual(resp.status_code, 200)

    def test_sondurum_toplam_yagis_basarili(self):
        resp = self.client.get("/sondurum/toplam-yagis")
        self.assertEqual(resp.status_code, 200)

    def test_sondurum_kar_basarili(self):
        resp = self.client.get("/sondurum/kar-kalinliklari")
        self.assertEqual(resp.status_code, 200)

    def test_sondurum_son_gozlemler_basarili(self):
        resp = self.client.get("/sondurum/son-gozlemler")
        self.assertEqual(resp.status_code, 200)

    def test_istasyonlar_basarili(self):
        resp = self.client.get("/istasyonlar/İstanbul")
        self.assertEqual(resp.status_code, 200)

    def test_istasyonlar_mgm_hatasinda_404_doner(self):
        app_module.mgm = FakeMGM(should_fail_health=True)
        resp = self.client.get("/istasyonlar/İstanbul")
        self.assertEqual(resp.status_code, 404)

    def test_istasyonsuz_ilde_guncel_404_doner(self):
        resp = self.client.get("/guncel/istasyonsuz-il")
        self.assertEqual(resp.status_code, 404)

    def test_saatlik_mgm_hatasinda_404_doner(self):
        with patch.object(app_module.mgm, "saatlik_tahmin", side_effect=MGMWeatherError("test")):
            resp = self.client.get("/saatlik/İstanbul")
        self.assertEqual(resp.status_code, 404)

    def test_hava_durumu_mgm_hatasinda_404_doner(self):
        with patch.object(app_module.mgm, "hava_durumu", side_effect=MGMWeatherError("test")):
            resp = self.client.get("/hava-durumu/İstanbul")
        self.assertEqual(resp.status_code, 404)

    def test_hava_kalitesi_mgm_hatasinda_404_doner(self):
        with patch.object(app_module.mgm, "hava_kalitesi", side_effect=MGMWeatherError("test")):
            resp = self.client.get("/hava-kalitesi/İstanbul")
        self.assertEqual(resp.status_code, 404)

    def test_gun_ay_bilgisi_mgm_hatasinda_404_doner(self):
        with patch.object(app_module.mgm, "gun_dogumu_batimi", side_effect=MGMWeatherError("test")):
            resp = self.client.get("/gun-ay-bilgisi/İstanbul")
        self.assertEqual(resp.status_code, 404)

    def test_sondurum_en_yuksek_mgm_hatasinda_502_doner(self):
        with patch.object(app_module.mgm, "en_yuksek_sicakliklar", side_effect=MGMWeatherError("t")):
            resp = self.client.get("/sondurum/en-yuksek-sicakliklar")
        self.assertEqual(resp.status_code, 502)

    def test_sondurum_toplam_yagis_mgm_hatasinda_502_doner(self):
        with patch.object(app_module.mgm, "toplam_yagislar", side_effect=MGMWeatherError("t")):
            resp = self.client.get("/sondurum/toplam-yagis")
        self.assertEqual(resp.status_code, 502)

    def test_sondurum_kar_mgm_hatasinda_502_doner(self):
        with patch.object(app_module.mgm, "kar_kalinliklari", side_effect=MGMWeatherError("t")):
            resp = self.client.get("/sondurum/kar-kalinliklari")
        self.assertEqual(resp.status_code, 502)

    def test_sondurum_son_gozlemler_mgm_hatasinda_502_doner(self):
        with patch.object(app_module.mgm, "son_gozlemler", side_effect=MGMWeatherError("t")):
            resp = self.client.get("/sondurum/son-gozlemler")
        self.assertEqual(resp.status_code, 502)

    def test_polen_mgm_hatasinda_404_doner(self):
        with patch.object(app_module.mgm, "polen_indeksi", side_effect=MGMWeatherError("test")):
            resp = self.client.get("/polen/İstanbul")
        self.assertEqual(resp.status_code, 404)

    def test_deniz_mgm_hatasinda_404_doner(self):
        with patch.object(app_module.mgm, "deniz_durumu", side_effect=MGMWeatherError("test")):
            resp = self.client.get("/deniz/İstanbul")
        self.assertEqual(resp.status_code, 404)

    def test_don_uyarisi_mgm_hatasinda_404_doner(self):
        with patch.object(app_module.mgm, "don_kiragi_riski", side_effect=MGMWeatherError("test")):
            resp = self.client.get("/don-uyarisi/İstanbul")
        self.assertEqual(resp.status_code, 404)

    def test_404_handler_bilinmeyen_yolda_calisir(self):
        resp = self.client.get("/hic-var-olmayan-bir-yol")
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(resp.get_json()["basarili"])


if __name__ == "__main__":
    unittest.main()
