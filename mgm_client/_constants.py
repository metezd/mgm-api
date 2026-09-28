"""Cache/redis/circuit-breaker sabitleri, hadise kodu eslemeleri, Turkce normalizasyon."""

import logging
import os

try:
    from prometheus_client import Counter, Gauge, Histogram
except ImportError:  # pragma: no cover
    class _NoopMetric:
        def __init__(self, *args, **kwargs):
            pass

        def labels(self, *args, **kwargs):
            return self

        def inc(self, *args, **kwargs):
            pass

        def set(self, *args, **kwargs):
            pass

        def observe(self, *args, **kwargs):
            pass

    Counter = Gauge = Histogram = _NoopMetric  # type: ignore[misc,assignment]

logger = logging.getLogger("mgm_client")

REDIS_CONNECT_TIMEOUT = 2.0
REDIS_SOCKET_TIMEOUT = 2.0
REDIS_HEALTH_CHECK_INTERVAL = 30
REDIS_STARTUP_RETRY_ATTEMPTS = 5
REDIS_STARTUP_RETRY_DELAY_SECONDS = 2.0

# Redis cache kayıt sarmalayıcısındaki anahtarlar
_CACHED_AT_KEY = "_cachedAt"
_VALUE_KEY = "_value"

CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5
CIRCUIT_BREAKER_WINDOW_SECONDS = 30.0
CIRCUIT_BREAKER_OPEN_SECONDS = 60.0

CACHE_SONUC_SAYAC = Counter(
    "mgm_cache_result_total",
    "Cache sorgu sonucu (hit: taze, stale_hit: bayat ama sunuldu, "
    "miss: hiç yok, lkg_fallback: gerçek istek+SWR başarısız olunca "
    "son-bilinen-iyi-değerden sunuldu)",
    ["sonuc"],
)
# Kaynak sağlığı: her dış servis ayrı izlenir
KAYNAKLAR = ("mgm", "open-meteo", "nominatim", "ibb", "sunrise-sunset", "piri-reis")
KAYNAK_SAGLIK_HATA_ESIGI = 3  # bu kadar ardışık hatadan sonra kaynak "hata" sayılır

KAYNAK_ISTEK_SAYAC = Counter(
    "mgm_source_requests_total",
    "Dış kaynağa atılan gerçek (cache dışı) isteklerin sonucu",
    ["kaynak", "sonuc"],  # sonuc: ok | hata
)
KAYNAK_ISTEK_SURESI = Histogram(
    "mgm_source_request_duration_seconds",
    "Dış kaynağa atılan gerçek isteklerin süresi",
    ["kaynak"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
KAYNAK_UP_GAUGE = Gauge(
    "mgm_source_up",
    "Kaynağa atılan SON isteğin sonucu (1: başarılı, 0: hata)",
    ["kaynak"],
)
KAYNAK_SON_BASARI_GAUGE = Gauge(
    "mgm_source_last_success_timestamp_seconds",
    "Kaynağa atılan son başarılı isteğin unix zamanı",
    ["kaynak"],
)

CACHE_KEY_NAMESPACE = "mgm-api"
CACHE_KEY_VERSION = "v2"
CACHE_KEY_MAX_LENGTH = 180
CACHE_RESPONSE_MAX_BYTES = int(os.getenv("MGM_CACHE_MAX_RESPONSE_BYTES", str(2 * 1024 * 1024)))



# Durum kodları
CONDITION_CODES: dict[str, str] = {
    "PB": "Parçalı Bulutlu",
    "GSY": "Gökgürültülü Sağanak Yağışlı",
    "HSY": "Hafif Sağanak Yağışlı",
    "SY": "Sağanak Yağışlı",
    "A": "Açık",
    "AB": "Az Bulutlu",
    "CB": "Çok Bulutlu",
    "D": "Duman",
    "HY": "Hafif Yağmurlu",
    "HKY": "Hafif Kar Yağışlı",
    "MSY": "Yer Yer Sağanak Yağışlı",
    "KKY": "Karla Karışık Yağmurlu",
    "GKR": "Güneyli Kuvvetli Rüzgar",
    "SCK": "Sıcak",
    "PUS": "Puslu",
    "Y": "Yağmurlu",
    "K": "Kar Yağışlı",
    "DY": "Dolu",
    "R": "Rüzgarlı",
    "KKR": "Kuzeyli Kuvvetli Rüzgar",
    "SGK": "Soğuk",
    "SIS": "Sisli",
    "KY": "Kuvvetli Yağmurlu",
    "KSY": "Kuvvetli Sağanak Yağışlı",
    "YKY": "Yoğun Kar Yağışlı",
    "KF": "Toz veya Kum Fırtınası",
    "KGY": "Kuvvetli Gökgürültülü Sağanak Yağışlı",
}

# Open-Meteo WMO hava durumu kodları (https://open-meteo.com/en/docs)
# Sadece fallback yanıtlarında kullanılır
WMO_CONDITION_CODES: dict[int, str] = {
    0: "Açık",
    1: "Genel Olarak Açık",
    2: "Parçalı Bulutlu",
    3: "Çok Bulutlu",
    45: "Sisli",
    48: "Kırağı Sisi",
    51: "Hafif Çisenti",
    53: "Çisenti",
    55: "Yoğun Çisenti",
    56: "Hafif Donan Çisenti",
    57: "Yoğun Donan Çisenti",
    61: "Hafif Yağmurlu",
    63: "Yağmurlu",
    65: "Şiddetli Yağmurlu",
    66: "Hafif Donan Yağmur",
    67: "Şiddetli Donan Yağmur",
    71: "Hafif Kar Yağışlı",
    73: "Kar Yağışlı",
    75: "Yoğun Kar Yağışlı",
    77: "Kar Taneli",
    80: "Hafif Sağanak",
    81: "Sağanak",
    82: "Şiddetli Sağanak",
    85: "Hafif Kar Sağanağı",
    86: "Yoğun Kar Sağanağı",
    95: "Gök Gürültülü Fırtına",
    96: "Dolu ile Gök Gürültülü Fırtına",
    99: "Şiddetli Dolu ile Gök Gürültülü Fırtına",
}


_TR_MAP = str.maketrans("ıİüÜğĞşŞöÖçÇ", "iIuUgGsSoOcC")


def _tr_normalize(text: str) -> str:
    """Şehir ve ilçe adlarını MGM servisinin beklediği sadeleştirilmiş forma çevirir."""
    return text.translate(_TR_MAP).lower().strip()
