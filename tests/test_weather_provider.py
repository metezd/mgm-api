import unittest
from unittest.mock import MagicMock

from weather_provider import MGMAdapter, WeatherProvider

# WeatherProvider'daki tüm soyut metod adları — MGMAdapter'ın hepsini
# gerçekten delege ettiğini ve MGMWeather'a doğru argümanlarla ilettiğini
# doğrulamak için kullanılır.
_METOD_ARGUMANLARI = {
    "il_istasyonlari": (("İstanbul",), {}),
    "ilce_istasyonu": (("İstanbul", "Kadıköy"), {}),
    "guncel_durum_yedekli": ((12345, 41.0, 29.0), {}),
    "gunluk_tahmin": ((12345,), {}),
    "saatlik_tahmin": ((12345,), {}),
    "hava_durumu": (("İstanbul", "Kadıköy"), {}),
    "hava_durumu_akilli": (("kadikoy istanbul",), {}),
    "hava_durumu_konum": ((41.0, 29.0), {}),
    "hava_kalitesi": ((41.0, 29.0), {}),
    "polen_indeksi": ((41.0, 29.0), {}),
    "deniz_durumu": ((41.0, 29.0), {}),
    "gun_dogumu_batimi": ((41.0, 29.0), {}),
    "ay_evresi": ((), {}),
    "don_kiragi_riski": ((12345,), {"il": "İstanbul", "ilce": None}),
    "akilli_ozet": (("İstanbul", None), {}),
    "uyarilar": ((None,), {}),
    "en_dusuk_sicakliklar": ((None,), {}),
    "en_yuksek_sicakliklar": ((None,), {}),
    "toplam_yagislar": ((None,), {}),
    "kar_kalinliklari": ((), {}),
    "son_gozlemler": ((), {}),
    "harita_geojson": ((), {}),
    "circuit_breaker_saglik_ozeti": ((), {}),
    "redis_saglik_ozeti": ((), {}),
}


class TestWeatherProviderArayuzu(unittest.TestCase):
    """WeatherProvider soyut arayüzünün temel kuralları."""

    def test_dogrudan_ornek_olusturulamaz(self):
        with self.assertRaises(TypeError):
            WeatherProvider()  # soyut sınıf, örneklenemez

    def test_tum_metodlari_implemente_etmeyen_alt_sinif_olusturulamaz(self):
        class EksikSaglayici(WeatherProvider):
            def il_istasyonlari(self, il):
                return []
            # geri kalan ~22 metod implemente edilmedi

        with self.assertRaises(TypeError):
            EksikSaglayici()

    def test_arayuzun_tum_metodlarini_implemente_eden_sinif_olusturulabilir(self):
        # Her soyut metod için sabit bir gövde içeren namespace'i class
        # OLUŞTURULMADAN ÖNCE hazırlıyoruz — ABCMeta, __abstractmethods__'ı
        # sınıf oluşturma anında hesaplar; sonradan setattr ile eklenen
        # metodlar bunu güncellemez.
        namespace = {isim: (lambda self, *a, **k: {}) for isim in _METOD_ARGUMANLARI}
        TamSaglayici = type("TamSaglayici", (WeatherProvider,), namespace)

        try:
            TamSaglayici()  # artık hata vermemeli
        except TypeError as exc:  # pragma: no cover - hata ayıklamaya yardımcı
            self.fail(f"Tüm metodlar tanımlıyken örnekleme başarısız oldu: {exc}")


class TestMGMAdapter(unittest.TestCase):
    """MGMAdapter'ın WeatherProvider arayüzünü MGMWeather'a doğru
    biçimde delege ettiğini doğrular."""

    def setUp(self):
        self.sahte_mgm = MagicMock()
        self.adapter = MGMAdapter(self.sahte_mgm)

    def test_mgmadapter_weatherprovider_alt_sinifidir(self):
        self.assertIsInstance(self.adapter, WeatherProvider)

    def test_her_metod_sarilan_istemciye_delege_eder(self):
        for isim, (args, kwargs) in _METOD_ARGUMANLARI.items():
            with self.subTest(metod=isim):
                sahte_donus = {"metod": isim}
                getattr(self.sahte_mgm, isim).return_value = sahte_donus
                sonuc = getattr(self.adapter, isim)(*args, **kwargs)
                self.assertEqual(sonuc, sahte_donus)
                getattr(self.sahte_mgm, isim).assert_called_once()

    def test_arayuz_disi_ozelliğe_erisim_sarilan_istemciye_duser(self):
        # __getattr__ kaçış kapısı: WeatherProvider'da olmayan bir
        # özellik istenirse sarılan MGMWeather'a düşer.
        self.sahte_mgm.http_pool_maxsize = 20
        self.assertEqual(self.adapter.http_pool_maxsize, 20)

    def test_gercek_mgmweather_ile_uyumludur(self):
        # Sahte değil gerçek MGMWeather örneğiyle sarma — mgm_client'ın
        # public API'sinin WeatherProvider'ın beklediği 23 metodun
        # hepsini (doğru isim/imza ile) sağladığını kanıtlar. Ağ
        # çağrısı yapılmaz, yalnızca metodların var olduğu doğrulanır.
        from mgm_client import MGMWeather

        gercek_mgm = MGMWeather()
        adapter = MGMAdapter(gercek_mgm)
        self.assertIsInstance(adapter, WeatherProvider)
        for isim in _METOD_ARGUMANLARI:
            self.assertTrue(callable(getattr(adapter, isim)))


if __name__ == "__main__":
    unittest.main()
