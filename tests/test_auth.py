import unittest
import uuid

from flask import Flask

from api.auth import ListeYetkiService

# authorize() Flask'ın `request`/`jsonify` global proxy'lerini kullanır,
# bu yüzden çağrılırken aktif bir request context gerekir. app.py'yi
# bütünüyle import etmeden (yavaş, gereksiz bağımlılık) minimal bir
# Flask uygulaması yeterlidir.
_test_app = Flask(__name__)


class _SahteRedisClient:
    """redis-py'nin bu testler için gereken küçük bir alt kümesini
    taklit eden bellek içi sahte istemci. Gerçek redis-py gibi
    hash alanlarını bytes döner (authorize()'daki decode dalını
    gerçekçi şekilde tetiklemek için)."""

    def __init__(self):
        self.store: dict[str, dict[bytes, bytes]] = {}
        self.expire_calls: list[tuple[str, int]] = []

    def exists(self, key):
        return key in self.store

    def hset(self, key, mapping):
        kayit = self.store.setdefault(key, {})
        for k, v in mapping.items():
            kayit[k.encode() if isinstance(k, str) else k] = (
                v.encode() if isinstance(v, str) else v
            )

    def expire(self, key, ttl):
        self.expire_calls.append((key, ttl))

    def hgetall(self, key):
        return self.store.get(key, {})


class _PatlayanRedisClient:
    """Her işlemde hata fırlatan sahte Redis — bellek fallback'ini
    tetiklemek için kullanılır."""

    class RedisHatasi(Exception):
        pass

    def exists(self, key):
        raise self.RedisHatasi("redis çöktü")

    def hgetall(self, key):
        raise self.RedisHatasi("redis çöktü")


class _RedisBaglantiHatasi(Exception):
    """Gerçek `redis.RedisError` gibi, uygulama mantığının fırlattığı
    ValueError'la ASLA çakışmayan, yalnızca Redis bağlantı/işlem
    hatalarını temsil eden ayrı bir sınıf. Testlerde `error_class`
    olarak düz `Exception` kullanmak, `create()`/`authorize()` içinde
    kasıtlı fırlatılan ValueError'ları da (yanlışlıkla) yakalardı."""


def _her_zaman_gecerli(_liste_id: str) -> bool:
    return True


def _asla_gecerli_degil(_liste_id: str) -> bool:
    return False


class TestListeYetkiServiceCreate(unittest.TestCase):
    """ListeYetkiService.create(): liste_id üretimi/doğrulama, Redis ve
    bellek içi depolama, çakışma tespiti, Redis hatasında fallback."""

    def test_listeid_verilmezse_uuid_v4_uretilir(self):
        servis = ListeYetkiService(
            redis_state=lambda: (False, None, "mgm-cache:", Exception),
            liste_id_validator=_her_zaman_gecerli,
            favori_ttl=100,
            alert_ttl=100,
        )
        sonuc = servis.create()
        self.assertEqual(uuid.UUID(sonuc["listeId"]).version, 4)
        self.assertTrue(sonuc["manage_token"])
        self.assertTrue(sonuc["read_token"])
        self.assertNotEqual(sonuc["manage_token"], sonuc["read_token"])

    def test_gecersiz_liste_id_value_error_firlatir(self):
        servis = ListeYetkiService(
            redis_state=lambda: (False, None, "mgm-cache:", Exception),
            liste_id_validator=_asla_gecerli_degil,
            favori_ttl=100,
            alert_ttl=100,
        )
        with self.assertRaises(ValueError):
            servis.create("herhangi-bir-id")

    def test_redis_yoksa_bellekte_saklanir(self):
        servis = ListeYetkiService(
            redis_state=lambda: (False, None, "mgm-cache:", Exception),
            liste_id_validator=_her_zaman_gecerli,
            favori_ttl=100,
            alert_ttl=100,
        )
        servis.create("test-liste")
        self.assertIn("test-liste", servis.memory)
        self.assertIn("manage_token_hash", servis.memory["test-liste"])

    def test_bellekte_ayni_liste_id_tekrar_olusturulamaz(self):
        servis = ListeYetkiService(
            redis_state=lambda: (False, None, "mgm-cache:", Exception),
            liste_id_validator=_her_zaman_gecerli,
            favori_ttl=100,
            alert_ttl=100,
        )
        servis.create("test-liste")
        with self.assertRaises(ValueError):
            servis.create("test-liste")

    def test_redis_varsa_redise_yazilir_ve_expire_cagrilir(self):
        redis_client = _SahteRedisClient()
        servis = ListeYetkiService(
            redis_state=lambda: (True, redis_client, "mgm-cache:", _RedisBaglantiHatasi),
            liste_id_validator=_her_zaman_gecerli,
            favori_ttl=100,
            alert_ttl=200,
        )
        servis.create("test-liste")

        anahtar = ListeYetkiService.redis_key("test-liste", "mgm-cache:")
        self.assertIn(anahtar, redis_client.store)
        self.assertEqual(redis_client.expire_calls, [(anahtar, 200)])  # max(100,200)
        # Redis'e yazılan; bellekte AYRICA tutulmaz
        self.assertNotIn("test-liste", servis.memory)

    def test_redis_de_ayni_liste_id_varsa_value_error_firlatir(self):
        redis_client = _SahteRedisClient()
        servis = ListeYetkiService(
            redis_state=lambda: (True, redis_client, "mgm-cache:", _RedisBaglantiHatasi),
            liste_id_validator=_her_zaman_gecerli,
            favori_ttl=100,
            alert_ttl=100,
        )
        servis.create("test-liste")
        with self.assertRaises(ValueError):
            servis.create("test-liste")

    def test_redis_hata_verirse_bellek_fallbackine_duser(self):
        servis = ListeYetkiService(
            redis_state=lambda: (
                True, _PatlayanRedisClient(), "mgm-cache:", _PatlayanRedisClient.RedisHatasi,
            ),
            liste_id_validator=_her_zaman_gecerli,
            favori_ttl=100,
            alert_ttl=100,
        )
        sonuc = servis.create("test-liste")
        self.assertEqual(sonuc["listeId"], "test-liste")
        self.assertIn("test-liste", servis.memory)  # Redis başarısız -> bellek kullanıldı


class TestListeYetkiServiceAuthorize(unittest.TestCase):
    """ListeYetkiService.authorize(): Bearer token doğrulama, hash
    karşılaştırma, Redis/bellek kaynak seçimi, hatalı/eksik header'lar."""

    def test_authorization_header_yoksa_401_doner(self):
        servis = ListeYetkiService(
            redis_state=lambda: (False, None, "mgm-cache:", Exception),
            liste_id_validator=_her_zaman_gecerli,
            favori_ttl=100,
            alert_ttl=100,
        )
        with _test_app.test_request_context("/"):
            sonuc = servis.authorize("herhangi-liste", "manage")

        self.assertIsNotNone(sonuc)
        _, status = sonuc
        self.assertEqual(status, 401)

    def test_bearer_onekisiz_header_401_doner(self):
        servis = ListeYetkiService(
            redis_state=lambda: (False, None, "mgm-cache:", Exception),
            liste_id_validator=_her_zaman_gecerli,
            favori_ttl=100,
            alert_ttl=100,
        )
        with _test_app.test_request_context("/", headers={"Authorization": "Token abc"}):
            sonuc = servis.authorize("herhangi-liste", "manage")

        self.assertIsNotNone(sonuc)
        self.assertEqual(sonuc[1], 401)

    def test_bellekteki_dogru_token_ile_yetkilendirme_basarili(self):
        servis = ListeYetkiService(
            redis_state=lambda: (False, None, "mgm-cache:", Exception),
            liste_id_validator=_her_zaman_gecerli,
            favori_ttl=100,
            alert_ttl=100,
        )
        veri = servis.create("test-liste")

        with _test_app.test_request_context(
            "/", headers={"Authorization": f"Bearer {veri['manage_token']}"}
        ):
            sonuc = servis.authorize("test-liste", "manage")

        self.assertIsNone(sonuc)  # None == yetkilendirme başarılı

    def test_yanlis_token_ile_401_doner(self):
        servis = ListeYetkiService(
            redis_state=lambda: (False, None, "mgm-cache:", Exception),
            liste_id_validator=_her_zaman_gecerli,
            favori_ttl=100,
            alert_ttl=100,
        )
        servis.create("test-liste")

        with _test_app.test_request_context(
            "/", headers={"Authorization": "Bearer yanlis-token-degeri"}
        ):
            sonuc = servis.authorize("test-liste", "manage")

        self.assertIsNotNone(sonuc)
        self.assertEqual(sonuc[1], 401)

    def test_read_token_ile_manage_yetkisi_istenirse_401_doner(self):
        # Scope karışıklığı yaşanmamalı: read_token yalnızca "read" için geçerli.
        servis = ListeYetkiService(
            redis_state=lambda: (False, None, "mgm-cache:", Exception),
            liste_id_validator=_her_zaman_gecerli,
            favori_ttl=100,
            alert_ttl=100,
        )
        veri = servis.create("test-liste")

        with _test_app.test_request_context(
            "/", headers={"Authorization": f"Bearer {veri['read_token']}"}
        ):
            sonuc = servis.authorize("test-liste", "manage")

        self.assertIsNotNone(sonuc)
        self.assertEqual(sonuc[1], 401)

    def test_redisden_bytes_donse_bile_dogru_yetkilendirilir(self):
        redis_client = _SahteRedisClient()
        servis = ListeYetkiService(
            redis_state=lambda: (True, redis_client, "mgm-cache:", _RedisBaglantiHatasi),
            liste_id_validator=_her_zaman_gecerli,
            favori_ttl=100,
            alert_ttl=100,
        )
        veri = servis.create("test-liste")  # Redis'e hash'ler bytes olarak yazıldı

        with _test_app.test_request_context(
            "/", headers={"Authorization": f"Bearer {veri['manage_token']}"}
        ):
            sonuc = servis.authorize("test-liste", "manage")

        self.assertIsNone(sonuc)

    def test_redis_hata_verirse_bellek_fallbackinden_yetkilendirilir(self):
        # create() bellekte saklandı (Redis yok), authorize() de aynı
        # şekilde Redis hatasında belleğe düşmeli.
        servis = ListeYetkiService(
            redis_state=lambda: (False, None, "mgm-cache:", Exception),
            liste_id_validator=_her_zaman_gecerli,
            favori_ttl=100,
            alert_ttl=100,
        )
        veri = servis.create("test-liste")

        # authorize() sırasında Redis'i "mevcut ama patlıyor" gibi göster
        servis.redis_state = lambda: (
            True, _PatlayanRedisClient(), "mgm-cache:", _PatlayanRedisClient.RedisHatasi,
        )

        with _test_app.test_request_context(
            "/", headers={"Authorization": f"Bearer {veri['manage_token']}"}
        ):
            sonuc = servis.authorize("test-liste", "manage")

        self.assertIsNone(sonuc)

    def test_var_olmayan_liste_icin_401_doner(self):
        servis = ListeYetkiService(
            redis_state=lambda: (False, None, "mgm-cache:", Exception),
            liste_id_validator=_her_zaman_gecerli,
            favori_ttl=100,
            alert_ttl=100,
        )
        with _test_app.test_request_context(
            "/", headers={"Authorization": "Bearer herhangi-bir-token"}
        ):
            sonuc = servis.authorize("hic-olusturulmamis-liste", "manage")

        self.assertIsNotNone(sonuc)
        self.assertEqual(sonuc[1], 401)


if __name__ == "__main__":
    unittest.main()
