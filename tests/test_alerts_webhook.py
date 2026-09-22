import unittest
from unittest.mock import patch

import requests

from api.alerts import _webhook_url_maskele, send_webhook
from api.models import WebhookPayloadModel


class TestWebhookUrlMaskele(unittest.TestCase):
    """_webhook_url_maskele(): loglara yazılan webhook URL'sinden path/
    query/fragment'ı (secret/token barındırabilecek kısımlar) tamamen
    kaldırır, yalnızca scheme+host(+port) bırakır."""

    def test_slacktarzi_path_icindeki_token_gizlenir(self):
        maskeli = _webhook_url_maskele("https://hooks.slack.com/services/T000/B000/SUPER-GIZLI-TOKEN")
        self.assertNotIn("SUPER-GIZLI-TOKEN", maskeli)
        self.assertNotIn("T000", maskeli)
        self.assertEqual(maskeli, "https://hooks.slack.com/***")

    def test_query_stringdeki_secret_gizlenir(self):
        maskeli = _webhook_url_maskele("https://example.com/webhook?api_key=cok-gizli-anahtar")
        self.assertNotIn("cok-gizli-anahtar", maskeli)
        self.assertEqual(maskeli, "https://example.com/***")

    def test_port_korunur(self):
        maskeli = _webhook_url_maskele("https://example.com:8443/hook?token=x")
        self.assertEqual(maskeli, "https://example.com:8443/***")
        self.assertNotIn("token", maskeli)

    def test_bozuk_url_secret_sizdirmadan_geri_doner(self):
        # Girdi ne olursa olsun (bozuk/beklenmedik) sonuçta secret
        # içerebilecek orijinal path/query asla döndürülmemeli.
        maskeli = _webhook_url_maskele("tamamen-gecersiz-bir-deger?secret=123")
        self.assertNotIn("secret=123", maskeli)


class TestWebhookLoglamaSizintisiYok(unittest.TestCase):
    """send_webhook() başarısız olduğunda gerçek log satırlarında ham
    webhook URL'sinin (ve içindeki secret'ın) hiç görünmediğini
    doğrular — yalnızca _webhook_url_maskele() ile maskelenmiş hali."""

    def test_baglanti_hatasinda_loglarda_ham_url_gorunmez(self):
        alert = {
            "id": "alert-1",
            "tur": "weather.rain_started",
            "il": "İstanbul",
            "ilce": None,
            "esik": None,
            "webhookUrl": "https://hooks.slack.com/services/T000/B000/SUPER-GIZLI-TOKEN",
        }

        with (
            patch("api.alerts.requests.post", side_effect=requests.ConnectionError("çöktü")),
            self.assertLogs("api.alerts", level="WARNING") as kayit,
        ):
            sonuc = send_webhook(
                alert=alert,
                measurement={"durumKodu": "Y"},
                payload_model=WebhookPayloadModel,
                max_url_length=2048,
                allowed_ports={443},
                timeout=1,
                retry_max=1,
                retry_backoff=0.01,
                signing_secret="",
                max_response_bytes=1024,
            )

        self.assertFalse(sonuc)
        tum_loglar = "\n".join(kayit.output)
        self.assertNotIn("SUPER-GIZLI-TOKEN", tum_loglar)
        self.assertNotIn("T000", tum_loglar)
        self.assertIn("hooks.slack.com", tum_loglar)  # host bilgisi hâlâ faydalı olarak kalır


if __name__ == "__main__":
    unittest.main()
