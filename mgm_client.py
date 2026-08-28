"""
mgm_client.py
-------------
Not: MGM'nin eski "web servis" (SOAP/REST) API'sinin yerini alan,
www.mgm.gov.tr sitesinin kendisinin kullandığı iç JSON servislerine dayanır.
Resmi belgelenmiş bir API değildir; MGM bu uç noktaların yapısını
değiştirirse istemcinin de güncellenmesi gerekir.

Kullanım:
    from mgm_client import MGMWeather

    mgm = MGMWeather()
    istasyonlar = mgm.il_istasyonlari("İstanbul")
    guncel = mgm.guncel_durum(istasyonlar[0]["istasyonId"])
    tahmin = mgm.gunluk_tahmin(istasyonlar[0]["istasyonId"])
"""

from __future__ import annotations

import copy
import datetime as _dt
import difflib
import json
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from prometheus_client import Counter
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("mgm_client")

# Cache altyapısı ana istek akışını asla uzun süre bloklamamalı
# bu nedenle değerler bilinçli olarak düşük tutulur.
REDIS_CONNECT_TIMEOUT = 2.0
REDIS_SOCKET_TIMEOUT = 2.0
REDIS_HEALTH_CHECK_INTERVAL = 30
REDIS_STARTUP_RETRY_ATTEMPTS = 5
REDIS_STARTUP_RETRY_DELAY_SECONDS = 2.0

# Redis cache kayıt sarmalayıcısındaki anahtarlar
_CACHED_AT_KEY = "_cachedAt"
_VALUE_KEY = "_value"

# Circuit breaker varsayılanları: MGM art arda hata verirse
# devre açılır ve bu süre boyunca MGM'ye hiç istek atılmadan 
# doğrudan hata dönülür. Cache katmanı ayrı çalışır
CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5
CIRCUIT_BREAKER_WINDOW_SECONDS = 30.0
CIRCUIT_BREAKER_OPEN_SECONDS = 60.0

# Prometheus metrikleri modül seviyesinde tanımlı, global default
# registry'de yaşıyor. app.py'deki /metrics endpoint'i generate_latest
# çağırdığında bunlar otomatik dahil olur
CACHE_SONUC_SAYAC = Counter(
    "mgm_cache_result_total",
    "Cache sorgu sonucu (hit: taze, stale_hit: bayat ama sunuldu, miss: hiç yok)",
    ["sonuc"],
)


@dataclass
class _InFlight:
    """Cache miss'te aynı anahtarı aynı anda tek isteğin yüklemesini sağlar.

    Lider istek loader'ı çalıştırır, sonucu/hastayı kaydeder ve event'i set
    eder bekleyenler event'i izleyip aynı sonucu kullanır.
    """

    event: threading.Event = field(default_factory=threading.Event)
    sonuc: Any = None
    hata: BaseException | None = None


@dataclass
class _CircuitBreaker:
    """Kayan pencereli, üç durumlu (kapalı/açık/yarı açık) circuit breaker.

    - **Kapalı**: her istek MGM'ye normal şekilde gider.
    - Pencere (`window_seconds`) içinde `failure_threshold` sayıda hata
      birikirse devre **açılır**: `open_seconds` boyunca hiçbir istek MGM'ye
      gitmez, doğrudan `MGMWeatherError` fırlatılır.
    - `open_seconds` dolunca devre **yarı açık** olur: tek bir deneme
      isteğine izin verilir. Başarılı olursa devre kapanır ve sayaçlar
      sıfırlanır ve başarısız olursa devre tekrar `open_seconds` için açılır.

    Thread-safe'tir; birden çok iş parçacığı aynı anda `basarisiz()` /
    `izin_var_mi()` çağırabilir.
    """

    failure_threshold: int
    window_seconds: float
    open_seconds: float
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _hatalar: list[float] = field(default_factory=list, init=False)
    _acilma_zamani: float | None = field(default=None, init=False)
    _yari_acik_deneme_suruyor: bool = field(default=False, init=False)

    def izin_var_mi(self) -> bool:
        """Şu an bir MGM isteğine izin verilip verilmeyeceğini döndürür.

        Devre yarı açıkken True dönen tek çağrı, deneme isteğini yapma
        hakkını da üstlenmiş olur (diğer eşzamanlı çağrılar False alır).
        """
        with self._lock:
            if self._acilma_zamani is None:
                return True
            gecen = time.monotonic() - self._acilma_zamani
            if gecen < self.open_seconds:
                return False
            if self._yari_acik_deneme_suruyor:
                return False
            self._yari_acik_deneme_suruyor = True
            return True

    def basarili(self) -> None:
        """Bir MGM isteği başarıyla tamamlandığında çağrılır; devreyi kapatır."""
        with self._lock:
            self._hatalar.clear()
            self._acilma_zamani = None
            self._yari_acik_deneme_suruyor = False

    def basarisiz(self) -> None:
        """Bir MGM isteği hatayla sonuçlandığında çağrılır."""
        with self._lock:
            simdi = time.monotonic()
            if self._yari_acik_deneme_suruyor:
                # Yarı açık deneme de başarısız oldu: devreyi tekrar aç.
                self._yari_acik_deneme_suruyor = False
                self._acilma_zamani = simdi
                self._hatalar = [simdi]
                return
            self._hatalar = [t for t in self._hatalar if simdi - t <= self.window_seconds]
            self._hatalar.append(simdi)
            if len(self._hatalar) >= self.failure_threshold:
                self._acilma_zamani = simdi

    def durum(self) -> str:
        """Gözlemlenebilirlik için mevcut durumu döndürür: kapali|acik|yari-acik."""
        with self._lock:
            if self._acilma_zamani is None:
                return "kapali"
            if time.monotonic() - self._acilma_zamani < self.open_seconds:
                return "acik"
            return "yari-acik"


class MGMWeatherError(Exception):
    """MGM istemcisiyle ilgili tüm hatalar için temel sınıf."""


class MGMCircuitOpenError(MGMWeatherError):
    """Circuit breaker açıkken MGM'ye istek atlanınca fırlatılır."""


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

#`GET /iller` doğrudan bu sabitten döner.
TURKIYE_ILLERI: list[dict[str, Any]] = [
    {"plakaKodu": 1, "il": "Adana"},
    {"plakaKodu": 2, "il": "Adıyaman"},
    {"plakaKodu": 3, "il": "Afyonkarahisar"},
    {"plakaKodu": 4, "il": "Ağrı"},
    {"plakaKodu": 5, "il": "Amasya"},
    {"plakaKodu": 6, "il": "Ankara"},
    {"plakaKodu": 7, "il": "Antalya"},
    {"plakaKodu": 8, "il": "Artvin"},
    {"plakaKodu": 9, "il": "Aydın"},
    {"plakaKodu": 10, "il": "Balıkesir"},
    {"plakaKodu": 11, "il": "Bilecik"},
    {"plakaKodu": 12, "il": "Bingöl"},
    {"plakaKodu": 13, "il": "Bitlis"},
    {"plakaKodu": 14, "il": "Bolu"},
    {"plakaKodu": 15, "il": "Burdur"},
    {"plakaKodu": 16, "il": "Bursa"},
    {"plakaKodu": 17, "il": "Çanakkale"},
    {"plakaKodu": 18, "il": "Çankırı"},
    {"plakaKodu": 19, "il": "Çorum"},
    {"plakaKodu": 20, "il": "Denizli"},
    {"plakaKodu": 21, "il": "Diyarbakır"},
    {"plakaKodu": 22, "il": "Edirne"},
    {"plakaKodu": 23, "il": "Elazığ"},
    {"plakaKodu": 24, "il": "Erzincan"},
    {"plakaKodu": 25, "il": "Erzurum"},
    {"plakaKodu": 26, "il": "Eskişehir"},
    {"plakaKodu": 27, "il": "Gaziantep"},
    {"plakaKodu": 28, "il": "Giresun"},
    {"plakaKodu": 29, "il": "Gümüşhane"},
    {"plakaKodu": 30, "il": "Hakkâri"},
    {"plakaKodu": 31, "il": "Hatay"},
    {"plakaKodu": 32, "il": "Isparta"},
    {"plakaKodu": 33, "il": "Mersin"},
    {"plakaKodu": 34, "il": "İstanbul"},
    {"plakaKodu": 35, "il": "İzmir"},
    {"plakaKodu": 36, "il": "Kars"},
    {"plakaKodu": 37, "il": "Kastamonu"},
    {"plakaKodu": 38, "il": "Kayseri"},
    {"plakaKodu": 39, "il": "Kırklareli"},
    {"plakaKodu": 40, "il": "Kırşehir"},
    {"plakaKodu": 41, "il": "Kocaeli"},
    {"plakaKodu": 42, "il": "Konya"},
    {"plakaKodu": 43, "il": "Kütahya"},
    {"plakaKodu": 44, "il": "Malatya"},
    {"plakaKodu": 45, "il": "Manisa"},
    {"plakaKodu": 46, "il": "Kahramanmaraş"},
    {"plakaKodu": 47, "il": "Mardin"},
    {"plakaKodu": 48, "il": "Muğla"},
    {"plakaKodu": 49, "il": "Muş"},
    {"plakaKodu": 50, "il": "Nevşehir"},
    {"plakaKodu": 51, "il": "Niğde"},
    {"plakaKodu": 52, "il": "Ordu"},
    {"plakaKodu": 53, "il": "Rize"},
    {"plakaKodu": 54, "il": "Sakarya"},
    {"plakaKodu": 55, "il": "Samsun"},
    {"plakaKodu": 56, "il": "Siirt"},
    {"plakaKodu": 57, "il": "Sinop"},
    {"plakaKodu": 58, "il": "Sivas"},
    {"plakaKodu": 59, "il": "Tekirdağ"},
    {"plakaKodu": 60, "il": "Tokat"},
    {"plakaKodu": 61, "il": "Trabzon"},
    {"plakaKodu": 62, "il": "Tunceli"},
    {"plakaKodu": 63, "il": "Şanlıurfa"},
    {"plakaKodu": 64, "il": "Uşak"},
    {"plakaKodu": 65, "il": "Van"},
    {"plakaKodu": 66, "il": "Yozgat"},
    {"plakaKodu": 67, "il": "Zonguldak"},
    {"plakaKodu": 68, "il": "Aksaray"},
    {"plakaKodu": 69, "il": "Bayburt"},
    {"plakaKodu": 70, "il": "Karaman"},
    {"plakaKodu": 71, "il": "Kırıkkale"},
    {"plakaKodu": 72, "il": "Batman"},
    {"plakaKodu": 73, "il": "Şırnak"},
    {"plakaKodu": 74, "il": "Bartın"},
    {"plakaKodu": 75, "il": "Ardahan"},
    {"plakaKodu": 76, "il": "Iğdır"},
    {"plakaKodu": 77, "il": "Yalova"},
    {"plakaKodu": 78, "il": "Karabük"},
    {"plakaKodu": 79, "il": "Kilis"},
    {"plakaKodu": 80, "il": "Osmaniye"},
    {"plakaKodu": 81, "il": "Düzce"},
]


def turkiye_illeri() -> list[dict[str, Any]]:
    """Türkiye'nin 81 ilini plaka kodu sırasıyla döndürür.

    Sabit veridir; MGM'ye istek atmaz. Çağıranın listeyi kazara mutasyona
    uğratmaması için her çağrıda yeni bir kopya döner.
    """
    return copy.deepcopy(TURKIYE_ILLERI)


@dataclass
class MGMWeather:
    """servis.mgm.gov.tr uç noktalarına istek atan basit istemci."""

    timeout: int = 10
    retry_total: int = 3
    retry_backoff: float = 0.3
    cache_ttl_seconds: int = 60
    cache_max_entries: int = 512
    stale_while_revalidate_seconds: int = 300
    circuit_breaker_failure_threshold: int = CIRCUIT_BREAKER_FAILURE_THRESHOLD
    circuit_breaker_window_seconds: float = CIRCUIT_BREAKER_WINDOW_SECONDS
    circuit_breaker_open_seconds: float = CIRCUIT_BREAKER_OPEN_SECONDS
    guncel_dinamik_ttl_aktif: bool = True
    guncel_sicak_pencere_baslangic_dk: int = 5
    guncel_sicak_pencere_bitis_dk: int = 15
    guncel_sicak_ttl_saniye: int = 120
    guncel_soguk_ttl_saniye: int = 1800
    guncel_zaman_dilimi: str = "Europe/Istanbul"
    guncel_gece_baslangic_saat: int = 0
    guncel_gece_bitis_saat: int = 6
    guncel_gece_ttl_saniye: int = 3600
    # gunluk_tahmin/saatlik_tahmin, guncel_durum'dan ayrı ve daha uzun bir TTL kullanır
    tahmin_ttl_saniye: int = 10800
    hava_kalitesi_ttl_saniye: int = 600
    ibb_istasyon_ttl_saniye: int = 21600
    ibb_max_mesafe_km: float = 40.0
    geojson_sinir_ttl_saniye: int = 2_592_000  # 30 gün
    harita_sicaklik_ttl_saniye: int = 600
    # Deniz durumu hava sıcaklığından daha yavaş değişir = daha uzun TTL
    deniz_ttl_saniye: int = 1800
    # Türkiye geneli en yüksek/en düşük sıcaklık tabloları günde birkaç
    # kez güncellenir, uzunca bir TTL yeterli
    sondurum_ttl_saniye: int = 1800
    piri_reis_ttl_saniye: int = 1800
    # verilen koordinata en yakın istasyon mesafeden uzaksa çalışmıyo kabul edilip Open-Meteo'ya düşülür
    piri_reis_max_mesafe_km: float = 60.0
    redis_url: str | None = None
    redis_prefix: str = "mgm-cache:"
    redis_client: Any | None = None
    header_provider: Callable[[], dict[str, str]] | None = None
    session: requests.Session = field(default_factory=requests.Session)
    _cache: dict[str, tuple[float, float, Any]] = field(default_factory=dict, init=False)
    _cache_lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _redis_available: bool = field(default=False, init=False)
    _redis_error_cls: type[BaseException] | None = field(default=None, init=False)
    _renewing: set[str] = field(default_factory=set, init=False)
    _renew_lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _in_flight: dict[str, _InFlight] = field(default_factory=dict, init=False)
    _in_flight_lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _lock_ttl: float = field(default=0.0, init=False)
    _circuit_breaker: _CircuitBreaker = field(default=None, init=False)  # type: ignore[assignment]

    BASE_URL = "https://servis.mgm.gov.tr/web"
    SUNRISE_URL = "https://api.sunrise-sunset.org/json"
    OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
    OPEN_METEO_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
    OPEN_METEO_AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
    # Deniz suyu sıcaklığı ve dalga yüksekliği: 
    # MGM'nin Piri Reis denizcilik sayfaları bu veriyi sağlıyor ama yalnızca
    # tarayıcı üzerinden görüntülenen HTML sayfaları olarak. 
    # Bu yüzden Open-Meteo Marine API kullanılıyor.
    OPEN_METEO_MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
    # Next.js SSR sayfası, veri <script id="__NEXT_DATA__"> içine gömülü JSON olarak gelir.
    # Belgeli bir REST API değil bu yüzden yalnızca sıcaklık için BİRİNCİL kaynak olarak kullanılır
    # sayfa yapısı değişirse (DOM/anahtar) MGMWeatherError fırlatılır ve 
    # deniz_durumu() otomatik olarak Open-Meteo'ya düşer.
    PIRI_REIS_DENIZ_SUYU_URL = "https://pirireis.mgm.gov.tr/deniz-suyu-sicakliklari"
    PIRI_REIS_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like "
            "Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }
    NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
    NOMINATIM_USER_AGENT = "mgm-hava-durumu-api/1.0 (https://github.com/metezd/hava-durumu)"
    # İBB Açık Veri Portalı — Çevre Koruma ve Kontrol Dairesi Başkanlığı
    # hava kalitesi web servisleri (resmi, dokümante edilmiş; bkz.
    # data.ibb.gov.tr "Hava Kalitesi İstasyon Bilgileri/Ölçüm Sonuçları
    # Web Servisi"). Yalnızca İstanbul'u kapsar ve PM2.5 döndürmez —
    # bu yüzden PM2.5 ve UV indeksi her zaman Open-Meteo'dan tamamlanır.
    IBB_HAVA_KALITESI_ISTASYONLAR_URL = (
        "https://api.ibb.gov.tr/havakalitesi/OpenDataPortalHandler/GetAQIStations"
    )
    IBB_HAVA_KALITESI_OLCUM_URL = (
        "https://api.ibb.gov.tr/havakalitesi/OpenDataPortalHandler/GetAQIByStationId"
    )
    # İl sınırları GeoJSON — statik/açık kaynak (OSM türevi), il bazında
    # MultiPolygon/Polygon sınırları. Neredeyse hiç değişmediği için
    # (idari sınır değişmediği sürece) uzun TTL ile önbelleklenir.
    GEOJSON_IL_SINIRLARI_URL = (
        "https://raw.githubusercontent.com/alpers/Turkey-Maps-GeoJSON/master/tr-cities.json"
    )
    # GeoJSON kaynağındaki bazı il adları MGM/81-il listesiyle birebir
    # eşleşmez (kısaltma/varyant); difflib toleransı bunları yakalayamayabilir.
    GEOJSON_IL_ALIASLARI = {
        "afyon": "Afyonkarahisar",
        "k. maras": "Kahramanmaraş",
        "kahramanmaras": "Kahramanmaraş",
        "k.maras": "Kahramanmaraş",
    }

    HEADERS = {
        "Host": "servis.mgm.gov.tr",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Origin": "https://www.mgm.gov.tr",
        "Referer": "https://www.mgm.gov.tr/",
    }

    def __post_init__(self) -> None:
        # Arka plan yenileme görevi en fazla timeout*(retry+1) sürebilir
        # Kilit TTL'i bu sınıra marj eklenerek belirlenir.
        self._lock_ttl = float(self.timeout * (self.retry_total + 1) + 5)

        self._circuit_breaker = _CircuitBreaker(
            failure_threshold=self.circuit_breaker_failure_threshold,
            window_seconds=self.circuit_breaker_window_seconds,
            open_seconds=self.circuit_breaker_open_seconds,
        )

        retry = Retry(
            total=self.retry_total,
            connect=self.retry_total,
            read=self.retry_total,
            status=self.retry_total,
            backoff_factor=self.retry_backoff,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset({"GET"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        if self.redis_client is not None:
            self._redis_available = True
            return

        if self.redis_url:
            try:
                import redis
            except ImportError as exc:
                raise MGMWeatherError(
                    "Redis cache için 'redis' paketi kurulu değil. "
                    "`pip install -r requirements.txt` çalıştırın."
                ) from exc

            self.redis_client = redis.Redis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=REDIS_CONNECT_TIMEOUT,
                socket_timeout=REDIS_SOCKET_TIMEOUT,
                health_check_interval=REDIS_HEALTH_CHECK_INTERVAL,
            )
            son_hata: Exception | None = None
            for deneme in range(1, REDIS_STARTUP_RETRY_ATTEMPTS + 1):
                try:
                    self.redis_client.ping()
                    son_hata = None
                    break
                except redis.RedisError as exc:
                    son_hata = exc
                    if deneme < REDIS_STARTUP_RETRY_ATTEMPTS:
                        logger.warning(
                            "Redis'e başlangıç bağlantısı başarısız (deneme %d/%d): %s "
                            "— %.1f sn sonra tekrar denenecek",
                            deneme,
                            REDIS_STARTUP_RETRY_ATTEMPTS,
                            exc,
                            REDIS_STARTUP_RETRY_DELAY_SECONDS,
                        )
                        time.sleep(REDIS_STARTUP_RETRY_DELAY_SECONDS)
            if son_hata is not None:
                raise MGMWeatherError(
                    f"Redis bağlantısı {REDIS_STARTUP_RETRY_ATTEMPTS} denemeden "
                    f"sonra kurulamadı: {son_hata}"
                ) from son_hata

            self._redis_error_cls = redis.RedisError
            self._redis_available = True

    # Düşük seviye yardımcılar
    def _cache_omru(self) -> int:
        """Bir cache kaydının diskte/ bellekte tutulacağı toplam süre (TTL + SWR)."""
        swr = self.stale_while_revalidate_seconds if self._swr_aktif() else 0
        return self.cache_ttl_seconds + swr

    def _cached_get(
        self, key: str, loader: Callable[[], Any], ttl_override: float | None = None
    ) -> Any:
        """Stale-while-revalidate cache akışı.

        Kayıt tazeyse (TTL içinde) doğrudan döner. TTL geçmiş ama stale
        penceresi içindeyse eski veriyi anında döner ve arka planda yeniler.
        Pencere de geçtiyse bloklayıcı şekilde yeniden yükler.

        `ttl_override` verilirse `self.cache_ttl_seconds` yerine kullanılır
        örn. guncel_durum() saat başına göre dinamik TTL uygulamak için
        bunu kullanır. Diğer tüm çağrılar statik `cache_ttl_seconds`'ta kalır.
        """
        ttl = self.cache_ttl_seconds if ttl_override is None else ttl_override
        kayit = self._kayit_sec(key)
        if kayit is not None:
            payload, yazilma_zamani = kayit
            yas = time.time() - yazilma_zamani
            if yas <= ttl:
                logger.info("Cache hit (taze): %s", key)
                CACHE_SONUC_SAYAC.labels(sonuc="hit").inc()
                return payload
            if self._swr_aktif() and yas <= ttl + self.stale_while_revalidate_seconds:
                logger.info("Cache hit (stale, arka planda yenileniyor): %s", key)
                CACHE_SONUC_SAYAC.labels(sonuc="stale_hit").inc()
                if self._renew_try_lock(key):
                    self._arka_planda_yenile(key, loader)
                return payload

        logger.info("Cache miss: %s", key)
        CACHE_SONUC_SAYAC.labels(sonuc="miss").inc()
        return self._yukle_singleton(key, loader)

    def _swr_aktif(self) -> bool:
        return self.stale_while_revalidate_seconds > 0

    def _kayit_sec(self, key: str) -> tuple[Any, float] | None:
        """Redis'ten, yoksa bellekten (veri, yazılma zamanı) kaydını döndürür."""
        redis_kayit = self._redis_get(key)
        if redis_kayit is not None:
            return redis_kayit
        return self._cache_get(key)

    def _yukle_singleton(self, key: str, loader: Callable[[], Any]) -> Any:
        """Cache miss'te aynı anahtar için tek yükleme garantisi (single-flight).

        Ilk istek lider olur ve loader'ı çalıştırıp eşzamanlı istekler aynı
        _InFlight kaydında bekleyip sonucu paylaşır. Hata durumunda hata da
        paylaşılır; kayıtlar her sonuçta temizlenir.
        """
        with self._in_flight_lock:
            kayit = self._in_flight.get(key)
            if kayit is None:
                kayit = _InFlight()
                self._in_flight[key] = kayit
                lider = True
            else:
                lider = False

        if lider:
            try:
                sonuc = loader()
                self._cache_set(key, sonuc)
                self._redis_set(key, sonuc)
            except BaseException as exc:  # noqa: BLE001 - bekleyenlere her hata paylaşılır
                kayit.hata = exc
                kayit.event.set()
                with self._in_flight_lock:
                    self._in_flight.pop(key, None)
                raise
            kayit.sonuc = sonuc
            kayit.event.set()
            with self._in_flight_lock:
                self._in_flight.pop(key, None)
            return sonuc

        logger.info("Cache miss'te lider istek bekleniyor: %s", key)
        self._in_flight_sonucu_bekle(kayit)
        assert kayit.hata is None, "Bekleyen istek hata almadan dönmemeli"
        return copy.deepcopy(kayit.sonuc)

    def _in_flight_sonucu_bekle(self, kayit: _InFlight) -> None:
        """Lider isteğin tamamlanmasını bekler; hata girerse yeniden fırlatır."""
        kayit.event.wait()
        if kayit.hata is not None:
            raise kayit.hata

    def _renew_try_lock(self, key: str) -> bool:
        """Aynı anahtarı aynı anda tek yenileyenin yüklemesini sağlar.

        Önce işlem içi kilit, Redis varsa ardından SET NX EX ile çalışanlar
        arası kilit alınır. Redis kilidi kilitliyse görev atlanır.
        """
        with self._renew_lock:
            if key in self._renewing:
                return False
            self._renewing.add(key)

        if self._redis_available:
            kilit_anahtari = self._redis_key(key) + ":swr-lock"
            lock_ttl = max(1.0, self._lock_ttl)
            try:
                kazanildi = self._redis_islem(
                    lambda: self.redis_client.set(
                        kilit_anahtari, "1", nx=True, ex=lock_ttl
                    ),
                    "Redis yenileme kilidi hatası",
                )
            except MGMWeatherError:
                logger.warning("Redis yenileme kilidi alınamadı: %s", key)
                with self._renew_lock:
                    self._renewing.discard(key)
                return False
            if not kazanildi:
                with self._renew_lock:
                    self._renewing.discard(key)
                return False
        return True

    def _renew_release(self, key: str) -> None:
        if self._redis_available:
            kilit_anahtari = self._redis_key(key) + ":swr-lock"
            try:
                self._redis_islem(
                    lambda: self.redis_client.delete(kilit_anahtari),
                    "Redis yenileme kilidi bırakma hatası",
                )
            except MGMWeatherError:
                logger.debug("Redis yenileme kilidi bırakılamadı: %s", key)
        with self._renew_lock:
            self._renewing.discard(key)

    def _arka_planda_yenile(self, key: str, loader: Callable[[], Any]) -> None:
        """Stale veri döndükten sonra cache'i arka planda günceller"""

        def gorev() -> None:
            try:
                yeni = loader()
                self._cache_set(key, yeni)
                self._redis_set(key, yeni)
                logger.info("Arka plan cache yenileme tamamlandı: %s", key)
            except MGMWeatherError as exc:
                logger.warning("Arka plan cache yenileme başarısız: %s (%s)", key, exc)
            finally:
                self._renew_release(key)

        thread = threading.Thread(
            target=gorev, daemon=True, name=f"swr-{key[:24]}"
        )
        thread.start()

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        ttl_override: float | None = None,
    ) -> Any:
        cache_key = self._cache_key(path, params)
        url = f"{self.BASE_URL}/{path}"

        def loader() -> Any:
            # Circuit breaker açıkken MGM'ye hiç istek atılmaz; hemen hata
            # dönülür. Not: bu yalnızca asıl ağ isteğini engeller ve çağıran
            # `_cached_get` zaten stale veri varsa onu döndürmüş olabilir
            # (SWR akışı, arka planda bu loader'ı tetikler). Yani MGM
            # kesintisi sırasında elde stale veri varsa kullanıcı bundan
            # etkilenmez sadece breaker gereksiz/bekletici ağ isteklerini keser
            if not self._circuit_breaker.izin_var_mi():
                logger.warning(
                    "Circuit breaker açık, MGM isteği atlanıyor: %s, params=%s",
                    url,
                    params,
                )
                raise MGMCircuitOpenError(
                    "MGM servisi art arda hata verdiği için circuit breaker "
                    f"açık; istek atlandı ({url})."
                )

            headers = self.HEADERS.copy()
            if self.header_provider:
                extra_headers = self.header_provider()
                if extra_headers:
                    headers.update(extra_headers)

            logger.info("İstek atılıyor: %s, params=%s", url, params)
            try:
                resp = self.session.get(
                    url, headers=headers, params=params, timeout=self.timeout
                )
            except requests.RequestException as exc:
                self._circuit_breaker.basarisiz()
                logger.error("MGM bağlantı hatası: %s", exc)
                raise MGMWeatherError(f"MGM servisine bağlanılamadı: {exc}") from exc

            if resp.status_code != 200:
                self._circuit_breaker.basarisiz()
                logger.warning("MGM servisinden hata kodu: %d (%s)", resp.status_code, url)
                raise MGMWeatherError(
                    f"MGM servisi beklenmeyen durum kodu döndürdü: {resp.status_code} "
                    f"({url})"
                )
            try:
                sonuc = resp.json()
            except ValueError as exc:
                self._circuit_breaker.basarisiz()
                logger.error("MGM JSON çözümleme hatası: %s", exc)
                raise MGMWeatherError(
                    f"MGM servisinden geçerli JSON alınamadı ({url})"
                ) from exc

            self._circuit_breaker.basarili()
            return sonuc

        return self._cached_get(cache_key, loader, ttl_override=ttl_override)

    def _cache_key(self, path: str, params: dict[str, Any] | None = None) -> str:
        serialized = json.dumps(params or {}, sort_keys=True, ensure_ascii=False)
        return f"{path}?{serialized}"

    def _cache_get(self, key: str) -> tuple[Any, float] | None:
        if self.cache_ttl_seconds <= 0:
            return None
        now = time.monotonic()
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            expires_at, yazilma_zamani, payload = entry
            if expires_at <= now:
                # Kayıt tamamen öldü (TTL + SWR penceresi doldu)
                del self._cache[key]
                return None
            return copy.deepcopy(payload), yazilma_zamani

    def _cache_set(self, key: str, payload: Any) -> None:
        if self.cache_ttl_seconds <= 0:
            return
        expires_at = time.monotonic() + self._cache_omru()
        yazilma_zamani = time.time()
        with self._cache_lock:
            if len(self._cache) >= self.cache_max_entries:
                oldest_key = min(self._cache.items(), key=lambda item: item[1][0])[0]
                del self._cache[oldest_key]
            self._cache[key] = (expires_at, yazilma_zamani, copy.deepcopy(payload))

    def _redis_key(self, key: str) -> str:
        return f"{self.redis_prefix}{key}"

    def _redis_islem(self, islem: Callable[[], Any], hata_mesaji: str) -> Any:
        """Redis çağrısını yürütür ve gerçek istemcide hataları MGMWeatherError'a sarar.

        Testlerde enjekte edilen sahte istemciler için hata sarmalama yapılmaz.
        """
        if self._redis_error_cls is None:
            return islem()
        try:
            return islem()
        except self._redis_error_cls as exc:
            raise MGMWeatherError(f"{hata_mesaji}: {exc}") from exc

    def _redis_get(self, key: str) -> tuple[Any, float] | None:
        if not self._redis_available or self.cache_ttl_seconds <= 0:
            return None
        assert self.redis_client is not None

        value = self._redis_islem(
            lambda: self.redis_client.get(self._redis_key(key)),
            "Redis cache okuma hatası",
        )

        if value is None:
            return None
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8")
        if not isinstance(value, str):
            raise MGMWeatherError("Redis cache verisi beklenen formatta değil.")
        try:
            kayit = json.loads(value)
        except ValueError as exc:
            raise MGMWeatherError(f"Redis cache verisi çözümlenemedi: {exc}") from exc
        if not isinstance(kayit, dict) or _VALUE_KEY not in kayit or _CACHED_AT_KEY not in kayit:
            raise MGMWeatherError("Redis cache verisi beklenen formatta değil.")
        return kayit[_VALUE_KEY], float(kayit[_CACHED_AT_KEY])

    def _redis_set(self, key: str, payload: Any) -> None:
        if not self._redis_available or self.cache_ttl_seconds <= 0:
            return
        assert self.redis_client is not None
        kayit = {_CACHED_AT_KEY: time.time(), _VALUE_KEY: payload}
        serialized = json.dumps(kayit, ensure_ascii=False)
        # SWR aktifken kayanın erken silinmemesi için TTL + SWR penceresi kadar tut
        omur = self._cache_omru()

        self._redis_islem(
            lambda: self.redis_client.setex(
                self._redis_key(key), omur, serialized
            ),
            "Redis cache yazma hatası",
        )

    def redis_saglik_ozeti(self) -> dict[str, str]:
        """Redis sağlık özeti: durum ok|hata|skip ve varsa hata mesajı."""
        if not self._redis_available:
            return {"durum": "skip"}
        try:
            assert self.redis_client is not None
            self._redis_islem(
                lambda: self.redis_client.ping(),
                "Redis sağlık kontrolü hatası",
            )
            return {"durum": "ok"}
        except MGMWeatherError as exc:
            return {"durum": "hata", "hata": str(exc)}

    def circuit_breaker_saglik_ozeti(self) -> dict[str, str]:
        """Circuit breaker durumunu döndürür: kapali|acik|yari-acik."""
        return {"durum": self._circuit_breaker.durum()}

    def uyarilar(self, il: str | None = None) -> dict[str, Any]:
        """
        MGM'nin meteorolojik uyarı (MeteoUYARI) verisini döndürür.

        ÖNEMLİ sbu metod diğerlerinden (guncel_durum, gunluk_tahmin vb.)
        farklı çalışır: MGM'nin `alarmlar` uç noktasının ham JSON
        şemasını, bu kodun yazıldığı sırada aktif bir uyarı bulunmadığı
        için sDOĞRULAYAMADIK. Bu yüzden veriyi
        dönüştürmeden, MGM'den geldiği gibi ham olarak döndürür — alan
        adlarını tahmin ederek Türkçeleştirmeye/yeniden şekillendirmeye
        ÇALIŞMAZ. Sebep: bu projede daha önce tam da bu tür bir tahmin
        (ilce_istasyonu'nun eski client-side filtreleme mantığı) yanlış
        çıkıp gerçek verileri "bulunamadı" göstermişti — aynı hatayı
        görmediğimiz bir şema üzerinde tekrarlamamak için ham geçiş
        tercih edildi.

        `il` verilirse MGM'ye doğrudan parametre olarak iletilir (MGM'nin
        bunu destekleyip desteklemediği, filtrenin gerçekte çalışıp
        çalışmadığı doğrulanamadı — zararsız bir passthrough'tur, MGM
        parametreyi yok sayarsa en kötü ihtimalle filtresiz sonuçla aynı
        şeyi alırsınız).

        Gerçek bir uyarı aktifken bu metod tekrar çalıştırılıp MGM'nin
        döndürdüğü gerçek alan adları görülmeli; ancak o zaman anlamlı
        bir Türkçe şema/dönüşüm katmanı eklemek güvenli olur.
        """
        params: dict[str, Any] = {}
        if il:
            params["il"] = _tr_normalize(il)
        data = self._get("alarmlar", params or None)
        return {
            "ham": data if data is not None else [],
            "not": (
                "MGM'nin şu anki (bu kodun yazıldığı sıradaki) yanıtı boştu "
                "çünkü aktif bir uyarı yoktu; alan adları bu yüzden ham "
                "olarak geçiliyor, doğrulanmış bir şema/dönüşüm henüz yok."
            ),
        }

    def il_istasyonlari(self, il: str) -> list[dict[str, Any]]:
        """
        Bir il adına göre MGM'nin döndürdüğü istasyon(lar)ı döndürür.

        Önemli sınır: MGM'nin `merkezler` uç noktası, yalnızca `il` verilip
        `ilce` verilmediğinde o ilin TÜM ilçelerini değil, genelde tek bir
        varsayılan istasyonu döner (İstanbul için bu davranış
        `il=istanbul` sadece Bakırköy döner, oysa
        `il=istanbul&ilce=kadikoy` ayrı ve doğru bir sonuç döner) Yani bu
        yöntem "ilin tüm istasyonlarının listesi" değil, "ilin varsayılan
        istasyonu" olarak okunmalı ve belirli bir ilçeye ulaşmak için
        ilce_istasyonu(il, ilce) kullanın, o MGM'ye ilce'yi doğrudan
        parametre olarak gönderir (bkz. docs/resilience.md)
        """
        data = self._get("merkezler", {"il": _tr_normalize(il)})
        if not data:
            raise MGMWeatherError(f"'{il}' için istasyon bulunamadı.")
        return data

    def _il_ilce_istasyonlari(self, il: str, ilce: str) -> list[dict[str, Any]]:
        """MGM'ye hem `il` hem `ilce` parametresini birlikte gönderir.

        MGM'nin merkezler uç noktası bu iki parametre birlikte verildiğinde
        o ilçeye ait istasyonu doğrudan döner — il_istasyonlari()'nin
        döndürdüğü listede o ilçe olmasa bile bu sorgu genelde bulur. 
        `_get` zaten kendi cache/SWR/circuit-breaker
        mantığını çağıran ilce_istasyonu() bunu anlamlı bir hata mesajına çevirir.
        """
        return (
            self._get("merkezler", {"il": _tr_normalize(il), "ilce": _tr_normalize(ilce)})
            or []
        )

    def ilce_istasyonu(self, il: str, ilce: str | None = None) -> dict[str, Any]:
        """
        İl adına göre tek bir istasyon kaydı döndürür.
        ilce verilmezse ilin varsayılan istasyonu döndürülür.

        ilce verildiğinde MGM'ye `il`+`ilce` doğrudan parametre olarak
        gönderilir çünkü MGM'nin il-only sorgusu genelde
        o ilin sadece bir istasyonunu döner, tam liste değil. Bu yüzden
        önceki bir sürümde "ilçe bulunamadı" hatası MGM'de aslında var olan
        birçok ilçe için yanlışlıkla dönüyordu (bkz. docs/resilience.md)
        """
        if ilce:
            istasyonlar = self._il_ilce_istasyonlari(il, ilce)
            if istasyonlar:
                return istasyonlar[0]
            varsayilan_ilce = None
            try:
                varsayilan = self.il_istasyonlari(il)
                if varsayilan:
                    varsayilan_ilce = str(varsayilan[0].get("ilce", "")).strip() or None
            except MGMWeatherError:
                pass
            if varsayilan_ilce:
                raise MGMWeatherError(
                    f"'{il}' ilinde '{ilce}' ilçesi MGM'de bulunamadı (yazım "
                    f"hatası olabilir). MGM bir ilin tüm ilçelerini listelemeyi "
                    f"desteklemediğinden başka geçerli ilçe adlarını burada "
                    f"göremiyoruz; '{il}' için varsayılan istasyon: "
                    f"{varsayilan_ilce}."
                )
            raise MGMWeatherError(f"'{il}' ilinde '{ilce}' ilçesi bulunamadı.")
        istasyonlar = self.il_istasyonlari(il)
        return istasyonlar[0]

    # Güncel durum
    @staticmethod
    def _saat_araliginda_mi(saat: int, baslangic: int, bitis: int) -> bool:
        """Gece yarısını saran aralıkları da (örn. 22-06) doğru ele alır."""
        if baslangic <= bitis:
            return baslangic <= saat < bitis
        return saat >= baslangic or saat < bitis

    def _guncel_durum_dinamik_ttl(self) -> float:
        """
        guncel_durum() cache'i için saat başına göre değişen TTL

        İki katman var:
        1. Gece penceresi: en uzun TTL. Hem gerçek kullanıcı trafiği hem MGM'nin bazı istasyonlarının ölçüm
           sıklığı muhtemelen düşer — bu da doğrulanmamış bir varsayım,
           bu yüzden env ile kapatılabilir/ayarlanabilir tutuldu.
        2. Saat başı sıcak/soğuk pencere: "sıcak
           pencere" içinde kısa TTL kullanılır ki yeni düşen ölçüm hızlı 
           yakalansın dışında TTL uzatılır

        `cache_ttl_seconds<=0` ya da `guncel_dinamik_ttl_aktif=False` ise
        devre dışı kalır, statik `cache_ttl_seconds` aynen döner.
        """
        if self.cache_ttl_seconds <= 0 or not self.guncel_dinamik_ttl_aktif:
            return self.cache_ttl_seconds
        try:
            simdi = _dt.datetime.now(ZoneInfo(self.guncel_zaman_dilimi))
        except (ZoneInfoNotFoundError, OSError) as exc:
            logger.warning(
                "Dinamik TTL için saat dilimi çözümlenemedi (%s); statik TTL kullanılıyor.",
                exc,
            )
            return self.cache_ttl_seconds

        if self._saat_araliginda_mi(
            simdi.hour, self.guncel_gece_baslangic_saat, self.guncel_gece_bitis_saat
        ):
            return self.guncel_gece_ttl_saniye
        if self.guncel_sicak_pencere_baslangic_dk <= simdi.minute <= self.guncel_sicak_pencere_bitis_dk:
            return self.guncel_sicak_ttl_saniye
        return self.guncel_soguk_ttl_saniye

    def guncel_durum(self, istasyon_id: int | str) -> dict[str, Any]:
        """Bir istasyon için anlık (güncel) hava durumu verisini döndürür."""
        data = self._get(
            "sondurumlar",
            {"merkezid": istasyon_id},
            ttl_override=self._guncel_durum_dinamik_ttl(),
        )
        if not data:
            raise MGMWeatherError(
                f"{istasyon_id} numaralı istasyon için güncel veri bulunamadı."
            )
        kayit = data[0]
        kod = kayit.get("hadiseKodu")
        return {
            "istasyonId": istasyon_id,
            "sicaklik": kayit.get("sicaklik"),
            "nem": kayit.get("nem"),
            "ruzgarHizi": kayit.get("ruzgarHiz"),
            "ruzgarYonu": kayit.get("ruzgarYon"),
            "basinc": kayit.get("aktuelBasinc"),
            "denizSeviyesiBasinc": kayit.get("denizeIndirgenmisBasinc"),
            "durumKodu": kod,
            "durum": CONDITION_CODES.get(kod, kod),
            "olcumZamani": kayit.get("veriZamani"),
        }

    def _open_meteo_guncel_durum(self, enlem: float, boylam: float) -> dict[str, Any]:
        """
        MGM'ye ulaşılamadığında (circuit breaker açık ya da MGM hata verdi)
        kullanılan yedek kaynak. Open-Meteo key gerektirmeyen, ücretsiz bir
        hava durumu API'sidir (bkz. https://open-meteo.com). Alan adları
        MGM'ninkiyle birebir aynı tutulur ki tüketici tarafında ayrı bir
        dallanma gerekmesin; `guncel_durum_yedekli` çağrısı yanıta hangi
        kaynaktan geldiğini belirten bir `kaynak` alanı ekler.

        Kapsam bilinçli olarak dar tutuldu: yalnızca anlık durum için
        fallback var, 5 günlük/saatlik tahmin için yok (bkz.
        docs/resilience.md).
        """
        params = {
            "latitude": enlem,
            "longitude": boylam,
            "current": (
                "temperature_2m,relative_humidity_2m,wind_speed_10m,"
                "wind_direction_10m,surface_pressure,pressure_msl,weather_code"
            ),
            "timezone": "Europe/Istanbul",
        }
        cache_key = self._cache_key("open-meteo-guncel", params)

        def loader() -> dict[str, Any]:
            try:
                resp = self.session.get(
                    self.OPEN_METEO_URL, params=params, timeout=self.timeout
                )
                resp.raise_for_status()
                veri = resp.json()["current"]
            except (requests.RequestException, KeyError, ValueError) as exc:
                raise MGMWeatherError(
                    f"Open-Meteo yedek servisinden veri alınamadı: {exc}"
                ) from exc

            kod = veri.get("weather_code")
            return {
                "sicaklik": veri.get("temperature_2m"),
                "nem": veri.get("relative_humidity_2m"),
                "ruzgarHizi": veri.get("wind_speed_10m"),
                "ruzgarYonu": veri.get("wind_direction_10m"),
                "basinc": veri.get("surface_pressure"),
                "denizSeviyesiBasinc": veri.get("pressure_msl"),
                "durumKodu": kod,
                "durum": WMO_CONDITION_CODES.get(kod, kod),
                "olcumZamani": veri.get("time"),
            }

        return self._cached_get(cache_key, loader)

    def guncel_durum_yedekli(
        self,
        istasyon_id: int | str,
        enlem: float | None = None,
        boylam: float | None = None,
    ) -> dict[str, Any]:
        """
        guncel_durum()'u dener; MGM hata verirse (circuit breaker açık dahil)
        ve enlem/boylam biliniyorsa Open-Meteo'ya düşer. Döndürülen sözlükte
        her zaman bir `kaynak` alanı olur: "mgm" ya da "open-meteo" — hangi
        servisten geldiği tüketici tarafında hep belli olsun diye.

        Not: il/ilçe → istasyon çözümlemesi (enlem/boylam'ın kendisi) de
        MGM'den geliyor; MGM'nin istasyon listesi ("merkezler") ile anlık
        durum ("sondurumlar") uçları ayrı cache/SWR girdileri kullandığından
        genelde biri çökükken diğeri hâlâ cache'te taze olur, ama ikisi de
        aynı anda ve hiç cache'siz düşerse (soğuk anahtar + tam MGM kesintisi)
        enlem/boylam da elde olmayacağından bu fallback devreye giremez.
        """
        try:
            veri = self.guncel_durum(istasyon_id)
            veri["kaynak"] = "mgm"
            return veri
        except MGMWeatherError as mgm_hata:
            if enlem is None or boylam is None:
                raise
            try:
                veri = self._open_meteo_guncel_durum(enlem, boylam)
            except MGMWeatherError as om_hata:
                raise MGMWeatherError(
                    "MGM ve Open-Meteo (yedek) servislerinin ikisinden de "
                    f"veri alınamadı. MGM: {mgm_hata} | Open-Meteo: {om_hata}"
                ) from om_hata
            veri["istasyonId"] = istasyon_id
            veri["kaynak"] = "open-meteo"
            return veri

    # Hava kalitesi + UV indeksi
    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """İki koordinat arası büyük daire mesafesi (km). En yakın İBB
        istasyonunu bulmak için kullanılır."""
        yaricap = 6371.0088
        f1, f2 = math.radians(lat1), math.radians(lat2)
        dfi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = (
            math.sin(dfi / 2) ** 2
            + math.cos(f1) * math.cos(f2) * math.sin(dlambda / 2) ** 2
        )
        return 2 * yaricap * math.asin(math.sqrt(a))

    def _ibb_istasyonlari(self) -> list[dict[str, Any]]:
        """İBB hava kalitesi istasyon listesini getirir ve uzun süre cacheler.
        """
        cache_key = self._cache_key("ibb-hk-istasyonlar")

        def loader() -> list[dict[str, Any]]:
            try:
                resp = self.session.get(
                    self.IBB_HAVA_KALITESI_ISTASYONLAR_URL, timeout=self.timeout
                )
                resp.raise_for_status()
                ham = resp.json()
            except (requests.RequestException, ValueError) as exc:
                raise MGMWeatherError(
                    f"İBB hava kalitesi istasyon listesi alınamadı: {exc}"
                ) from exc

            istasyonlar: list[dict[str, Any]] = []
            for kayit in ham if isinstance(ham, list) else []:
                konum = str(kayit.get("Location") or "")
                parcalar = [p.strip() for p in konum.split(",")]
                if len(parcalar) != 2:
                    continue
                try:
                    enlem, boylam = float(parcalar[0]), float(parcalar[1])
                except ValueError:
                    continue
                istasyonlar.append(
                    {
                        "id": kayit.get("Id"),
                        "ad": kayit.get("Name"),
                        "adres": kayit.get("Adress") or kayit.get("Address"),
                        "enlem": enlem,
                        "boylam": boylam,
                    }
                )
            if not istasyonlar:
                raise MGMWeatherError(
                    "İBB hava kalitesi istasyon listesi boş ya da beklenmeyen "
                    "formatta döndü."
                )
            return istasyonlar

        return self._cached_get(
            cache_key, loader, ttl_override=self.ibb_istasyon_ttl_saniye
        )

    def _ibb_en_yakin_istasyon(
        self, enlem: float, boylam: float
    ) -> dict[str, Any] | None:
        """Verilen koordinata en yakın İBB istasyonunu döner; en yakını
        `ibb_max_mesafe_km`'den uzaksa (İstanbul dışı) None döner."""
        istasyonlar = self._ibb_istasyonlari()
        en_yakin = min(
            istasyonlar,
            key=lambda s: self._haversine_km(enlem, boylam, s["enlem"], s["boylam"]),
        )
        mesafe = self._haversine_km(
            enlem, boylam, en_yakin["enlem"], en_yakin["boylam"]
        )
        if mesafe > self.ibb_max_mesafe_km:
            return None
        return en_yakin

    def _piri_reis_deniz_istasyonlari(self) -> list[dict[str, Any]]:
        """
        MGM'nin Piri Reis "Deniz Suyu Sıcaklıkları" sayfasını çekip <script id="__NEXT_DATA__"> ,
        içine gömülü JSON'dan istasyon listesi ve anlık sıcaklıkları ayıklar. 
        Bu resmi bir REST API DEĞİLDİR Bu yüzden sadece birincil kaynak olarak kullanılır
        herhangi bir adımda başarısız olursa Open-Meteo'ya düşer.
        """
        cache_key = self._cache_key("piri-reis-deniz-suyu")

        def loader() -> list[dict[str, Any]]:
            try:
                resp = self.session.get(
                    self.PIRI_REIS_DENIZ_SUYU_URL,
                    headers=self.PIRI_REIS_HEADERS,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
            except requests.RequestException as exc:
                raise MGMWeatherError(
                    f"Piri Reis deniz suyu sıcaklığı sayfasına ulaşılamadı: {exc}"
                ) from exc

            eslesme = re.search(
                r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                resp.text,
                re.DOTALL,
            )
            if not eslesme:
                raise MGMWeatherError(
                    "Piri Reis sayfa yapısı değişmiş görünüyor "
                    "(__NEXT_DATA__ bulunamadı)."
                )
            try:
                yuk = json.loads(eslesme.group(1))
                ham_veri = yuk["props"]["pageProps"]["data"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise MGMWeatherError(
                    f"Piri Reis JSON yapısı beklenmedik: {exc}"
                ) from exc

            istasyonlar: list[dict[str, Any]] = []
            for kayit in ham_veri if isinstance(ham_veri, list) else []:
                sicaklik = kayit.get("denizSicaklik")
                if sicaklik is None:
                    continue
                try:
                    enlem = float(kayit["enlem"])
                    boylam = float(kayit["boylam"])
                except (KeyError, TypeError, ValueError):
                    continue
                istasyonlar.append(
                    {
                        "istasyonId": kayit.get("istNo"),
                        "ad": kayit.get("istAd"),
                        "il": kayit.get("il"),
                        "ilce": kayit.get("ilce"),
                        "enlem": enlem,
                        "boylam": boylam,
                        "sicaklik": float(sicaklik),
                        "olcumZamani": kayit.get("denizVeriZamani"),
                    }
                )
            if not istasyonlar:
                raise MGMWeatherError(
                    "Piri Reis deniz suyu sıcaklığı verisi boş ya da "
                    "beklenmeyen formatta döndü."
                )
            return istasyonlar

        return self._cached_get(
            cache_key, loader, ttl_override=self.piri_reis_ttl_saniye
        )

    def _piri_reis_en_yakin_istasyon(
        self, enlem: float, boylam: float
    ) -> dict[str, Any] | None:
        """Verilen koordinata en yakın Piri Reis deniz istasyonunu döner;
        en yakını `piri_reis_max_mesafe_km`'den uzaksa None döner."""
        istasyonlar = self._piri_reis_deniz_istasyonlari()
        en_yakin = min(
            istasyonlar,
            key=lambda s: self._haversine_km(enlem, boylam, s["enlem"], s["boylam"]),
        )
        mesafe = self._haversine_km(
            enlem, boylam, en_yakin["enlem"], en_yakin["boylam"]
        )
        if mesafe > self.piri_reis_max_mesafe_km:
            return None
        return en_yakin

    @staticmethod
    def _sozlukten_kirletici_deger(veri: dict[str, Any], *adaylar: str) -> float | None:
        """İBB'nin Concentration/AQI nesnelerindeki alan adı casing'i resmi
        dokümanda net belirtilmediğinden, olası varyasyonları (PM10, Pm10,
        pm10 vb.) büyük/küçük harf duyarsız arar."""
        kucuk_harfli = {str(k).lower(): v for k, v in veri.items()}
        for aday in adaylar:
            if aday.lower() in kucuk_harfli:
                try:
                    return float(kucuk_harfli[aday.lower()])
                except (TypeError, ValueError):
                    return None
        return None

    def _ibb_olcum(self, istasyon_id: str) -> dict[str, Any]:
        """Verilen İBB istasyon id'si için en güncel PM10/NO2 ölçümünü döner.

        Not: bu servisin tam JSON şeması resmi dokümanda örnek yanıt olarak
        verilmemiştir. bu yöntem hem liste hem tekil kayıt biçimini kabul
        eder ve alan adlarını harf duyarsız arar. Servis yanıtı beklenmedik
        şekilde değişirse MGMWeatherError fırlatılır ve çağıran taraf
        `hava_kalitesi` Open-Meteo'ya düşer.
        """
        simdi = _dt.datetime.now()
        baslangic = simdi - _dt.timedelta(hours=3)
        params = {
            "StationId": istasyon_id,
            "StartDate": baslangic.strftime("%d.%m.%Y %H:%M:%S"),
            "EndDate": simdi.strftime("%d.%m.%Y %H:%M:%S"),
        }
        cache_key = self._cache_key("ibb-hk-olcum", params)

        def loader() -> dict[str, Any]:
            try:
                resp = self.session.get(
                    self.IBB_HAVA_KALITESI_OLCUM_URL, params=params, timeout=self.timeout
                )
                resp.raise_for_status()
                ham = resp.json()
            except (requests.RequestException, ValueError) as exc:
                raise MGMWeatherError(
                    f"İBB hava kalitesi ölçüm servisinden veri alınamadı: {exc}"
                ) from exc

            kayitlar = ham if isinstance(ham, list) else [ham] if isinstance(ham, dict) else []
            if not kayitlar:
                raise MGMWeatherError(
                    f"İBB istasyon {istasyon_id} için ölçüm kaydı bulunamadı."
                )

            def _zaman(kayit: dict[str, Any]) -> str:
                return str(kayit.get("ReadTime") or "")

            en_guncel = max(kayitlar, key=_zaman)
            konsantrasyon = en_guncel.get("Concentration") or {}
            aqi = en_guncel.get("AQI") or {}
            if not isinstance(konsantrasyon, dict):
                konsantrasyon = {}
            if not isinstance(aqi, dict):
                aqi = {}

            pm10 = self._sozlukten_kirletici_deger(konsantrasyon, "PM10")
            no2 = self._sozlukten_kirletici_deger(konsantrasyon, "NO2", "NO_2")
            if pm10 is None and no2 is None:
                raise MGMWeatherError(
                    f"İBB istasyon {istasyon_id} yanıtında beklenen kirletici "
                    "alanları bulunamadı."
                )
            return {
                "pm10": pm10,
                "no2": no2,
                "so2": self._sozlukten_kirletici_deger(konsantrasyon, "SO2"),
                "o3": self._sozlukten_kirletici_deger(konsantrasyon, "O3"),
                "co": self._sozlukten_kirletici_deger(konsantrasyon, "CO"),
                "hki": self._sozlukten_kirletici_deger(aqi, "AQI", "HKI"),
                "olcumZamani": en_guncel.get("ReadTime"),
            }

        return self._cached_get(
            cache_key, loader, ttl_override=self.hava_kalitesi_ttl_saniye
        )

    def _open_meteo_hava_kalitesi(self, enlem: float, boylam: float) -> dict[str, Any]:
        """Open-Meteo Air Quality API'sinden (key gerektirmez, CAMS verisine
        dayanır) anlık PM10, PM2.5, NO2 ve UV indeksini döner. İBB'nin
        kapsamadığı bölgeler için tam fallback, İstanbul için ise PM2.5 ve
        UV indeksinin tamamlayıcı kaynağıdır
        """
        params = {
            "latitude": enlem,
            "longitude": boylam,
            "current": "pm10,pm2_5,nitrogen_dioxide,uv_index,european_aqi",
            "timezone": "Europe/Istanbul",
        }
        cache_key = self._cache_key("open-meteo-hava-kalitesi", params)

        def loader() -> dict[str, Any]:
            try:
                resp = self.session.get(
                    self.OPEN_METEO_AIR_QUALITY_URL, params=params, timeout=self.timeout
                )
                resp.raise_for_status()
                veri = resp.json()["current"]
            except (requests.RequestException, KeyError, ValueError) as exc:
                raise MGMWeatherError(
                    f"Open-Meteo hava kalitesi servisinden veri alınamadı: {exc}"
                ) from exc

            return {
                "pm10": veri.get("pm10"),
                "pm25": veri.get("pm2_5"),
                "no2": veri.get("nitrogen_dioxide"),
                "uvIndeksi": veri.get("uv_index"),
                "avrupaHKI": veri.get("european_aqi"),
                "olcumZamani": veri.get("time"),
            }

        return self._cached_get(
            cache_key, loader, ttl_override=self.hava_kalitesi_ttl_saniye
        )

    def _sondurum_tarihler(self, tarih_path: str) -> list[str]:
        # tarih seçenekleri (son ~5 gün), en güncel data[0]
        return self._cached_get(
            self._cache_key(f"sondurum-tarih-{tarih_path}"),
            lambda: self._get(tarih_path),
            ttl_override=self.sondurum_ttl_saniye,
        )

    def _sondurum_sicaklik(
        self, veri_path: str, tarih_path: str, tarih: str | None
    ) -> dict[str, Any]:
        if not tarih:
            tarihler = self._sondurum_tarihler(tarih_path)
            tarih = tarihler[0][:10]
        veri = self._cached_get(
            self._cache_key(f"sondurum-{veri_path}", {"tarih": tarih}),
            lambda: self._get(veri_path, {"tarih": tarih}),
            ttl_override=self.sondurum_ttl_saniye,
        )
        kayitlar = [k for k in veri if k.get("istAd") is not None]
        return {"tarih": tarih, "kayitlar": kayitlar}

    def en_dusuk_sicakliklar(self, tarih: str | None = None) -> dict[str, Any]:
        return self._sondurum_sicaklik(
            "sondurumlar/endusuk", "sondurumlar/minimumMaxTarih", tarih
        )

    def en_yuksek_sicakliklar(self, tarih: str | None = None) -> dict[str, Any]:
        # min ile ayrı tarih servisi kullanır (maximumMaxTarih)
        return self._sondurum_sicaklik(
            "sondurumlar/enyuksek", "sondurumlar/maximumMaxTarih", tarih
        )

    def hava_kalitesi(self, enlem: float, boylam: float) -> dict[str, Any]:
        """Anlık UV indeksi ve hava kalitesi (PM10, PM2.5, NO2) döner.

        Öncelik İBB'nin resmi hava kalitesi ölçüm ağındadır (yalnızca
        İstanbul'u, `ibb_max_mesafe_km` yarıçapında kapsar); PM2.5 ve UV
        indeksi İBB servisinde bulunmadığından her zaman Open-Meteo Air
        Quality API'siyle (key gerektirmez) tamamlanır. İBB'ye hiç
        ulaşılamazsa (İstanbul dışı konum, istasyon/servis hatası vb.)
        tüm alanlar Open-Meteo'dan gelir. Döndürülen sözlükte her zaman
        `kaynaklar` alanı bulunur: hangi alanın hangi servisten geldiğini
        gösterir (örn. {"pm10": "ibb", "pm25": "open-meteo", ...}).
        """
        acik_meteo = self._open_meteo_hava_kalitesi(enlem, boylam)
        sonuc: dict[str, Any] = {
            "pm10": acik_meteo["pm10"],
            "pm25": acik_meteo["pm25"],
            "no2": acik_meteo["no2"],
            "uvIndeksi": acik_meteo["uvIndeksi"],
            "avrupaHKI": acik_meteo["avrupaHKI"],
            "olcumZamani": acik_meteo["olcumZamani"],
            "istasyon": None,
            "kaynaklar": {
                "pm10": "open-meteo",
                "pm25": "open-meteo",
                "no2": "open-meteo",
                "uvIndeksi": "open-meteo",
            },
        }

        try:
            istasyon = self._ibb_en_yakin_istasyon(enlem, boylam)
            if istasyon is None:
                return sonuc
            ibb_olcum = self._ibb_olcum(istasyon["id"])
        except MGMWeatherError as exc:
            logger.warning("İBB hava kalitesi alınamadı, Open-Meteo kullanılıyor: %s", exc)
            return sonuc

        if ibb_olcum.get("pm10") is not None:
            sonuc["pm10"] = ibb_olcum["pm10"]
            sonuc["kaynaklar"]["pm10"] = "ibb"
            sonuc["olcumZamani"] = ibb_olcum.get("olcumZamani") or sonuc["olcumZamani"]
        if ibb_olcum.get("no2") is not None:
            sonuc["no2"] = ibb_olcum["no2"]
            sonuc["kaynaklar"]["no2"] = "ibb"
        sonuc["istasyon"] = {
            "ad": istasyon["ad"],
            "adres": istasyon["adres"],
            "enlem": istasyon["enlem"],
            "boylam": istasyon["boylam"],
        }
        return sonuc

    # Polen ve alerji indeksi (tur_esik_dusuk, tur_esik_orta, tur_esik_yuksek) 
    # grains/m³ EAN/CAMS tabanlı yaygın kullanılan yaklaşık eşikler
    # kesin klinik eşikler türe ve bölgeye göre değişebilir, 
    # bu sınıflandırma genel bir yönlendirme amaçlıdır, tıbbi tavsiye değildir.
    POLEN_TURLERI = {
        "grass_pollen": ("cimen", 20, 50, 150),
        "birch_pollen": ("huş", 30, 90, 500),
        "alder_pollen": ("kızılağaç", 30, 90, 300),
        "mugwort_pollen": ("pelin otu", 20, 50, 150),
        "olive_pollen": ("zeytin", 10, 50, 200),
        "ragweed_pollen": ("ambrosia", 10, 30, 100),
    }

    def _polen_seviyesi(self, tur_anahtari: str, deger: float | None) -> str:
        if deger is None:
            return "Veri Yok"
        if deger <= 0:
            return "Yok"
        _, dusuk, orta, yuksek = self.POLEN_TURLERI[tur_anahtari]
        if deger <= dusuk:
            return "Düşük"
        if deger <= orta:
            return "Orta"
        if deger <= yuksek:
            return "Yüksek"
        return "Çok Yüksek"

    def polen_indeksi(self, enlem: float, boylam: float) -> dict[str, Any]:
        """
        Anlık polen konsantrasyonlarını (Grains/m³) ve her tür için
        basitleştirilmiş risk seviyesini (Yok/Düşük/Orta/Yüksek/Çok
        Yüksek) döner. Open-Meteo Air Quality API'sinden (CAMS Avrupa
        modeli, key gerektirmez) beslenir.

        Kapsam notu: CAMS polen verisi yalnızca Avrupa bölgesini ve
        yalnızca ilgili türün polen sezonunu kapsar; sezon dışında veya
        modelin kapsamadığı konumlarda tür bazında değer null döner
        (`seviye: "Veri Yok"` olarak işaretlenir), bu bir hata değildir.

        Not: Seviyeler kesin klinik/tıbbi eşikler değildir — genel
        yönlendirme amaçlı, EAN/CAMS tabanlı yaklaşık sınıflandırmadır.
        """
        params = {
            "latitude": enlem,
            "longitude": boylam,
            "current": ",".join(self.POLEN_TURLERI.keys()),
            "timezone": "Europe/Istanbul",
        }
        cache_key = self._cache_key("open-meteo-polen", params)

        def loader() -> dict[str, Any]:
            try:
                resp = self.session.get(
                    self.OPEN_METEO_AIR_QUALITY_URL, params=params, timeout=self.timeout
                )
                resp.raise_for_status()
                veri = resp.json()["current"]
            except (requests.RequestException, KeyError, ValueError) as exc:
                raise MGMWeatherError(
                    f"Open-Meteo polen servisinden veri alınamadı: {exc}"
                ) from exc

            turler: dict[str, Any] = {}
            en_yuksek_seviye_sirasi = 0
            baskin_tur = None
            seviye_sirasi = {
                "Veri Yok": 0,
                "Yok": 0,
                "Düşük": 1,
                "Orta": 2,
                "Yüksek": 3,
                "Çok Yüksek": 4,
            }
            for anahtar, (tr_adi, _, _, _) in self.POLEN_TURLERI.items():
                deger = veri.get(anahtar)
                seviye = self._polen_seviyesi(anahtar, deger)
                turler[anahtar] = {
                    "ad": tr_adi,
                    "deger": deger,
                    "birim": "Grains/m³",
                    "seviye": seviye,
                }
                sira = seviye_sirasi.get(seviye, 0)
                if sira > en_yuksek_seviye_sirasi:
                    en_yuksek_seviye_sirasi = sira
                    baskin_tur = tr_adi

            return {
                "turler": turler,
                "baskinTur": baskin_tur,
                "genelRiskSeviyesi": next(
                    (k for k, v in seviye_sirasi.items() if v == en_yuksek_seviye_sirasi),
                    "Veri Yok",
                ),
                "olcumZamani": veri.get("time"),
                "kaynak": "open-meteo (CAMS Avrupa)",
                "aciklama": (
                    "Seviyeler EAN/CAMS tabanlı yaklaşık sınıflandırmadır, "
                    "kesin klinik eşik değildir. CAMS yalnızca Avrupa "
                    "bölgesini ve ilgili türün sezonunu kapsar."
                ),
            }

        return self._cached_get(
            cache_key, loader, ttl_override=self.hava_kalitesi_ttl_saniye
        )

    def _open_meteo_deniz(self, enlem: float, boylam: float) -> dict[str, Any]:
        """Open-Meteo Marine Weather API'sinden anlık dalga
        durumu ve deniz suyu sıcaklığını döner.
        """
        params = {
            "latitude": enlem,
            "longitude": boylam,
            "current": (
                "wave_height,wave_period,wave_direction,sea_surface_temperature"
            ),
            "timezone": "Europe/Istanbul",
        }
        cache_key = self._cache_key("open-meteo-deniz", params)

        def loader() -> dict[str, Any]:
            try:
                resp = self.session.get(
                    self.OPEN_METEO_MARINE_URL, params=params, timeout=self.timeout
                )
                resp.raise_for_status()
                veri = resp.json()["current"]
            except (requests.RequestException, KeyError, ValueError) as exc:
                raise MGMWeatherError(
                    f"Open-Meteo deniz durumu servisinden veri alınamadı: {exc}"
                ) from exc

            return {
                "denizSuyuSicakligi": veri.get("sea_surface_temperature"),
                "dalgaYuksekligi": veri.get("wave_height"),
                "dalgaPeriyodu": veri.get("wave_period"),
                "dalgaYonu": veri.get("wave_direction"),
                "olcumZamani": veri.get("time"),
            }

        return self._cached_get(cache_key, loader, ttl_override=self.deniz_ttl_saniye)

    def deniz_durumu(self, enlem: float, boylam: float) -> dict[str, Any]:
        """
        Anlık deniz suyu sıcaklığı ve dalga durumu (yükseklik, periyot,
        yön) döner.

        Kaynak önceliği: deniz suyu sıcaklığı için ÖNCELİK MGM'nin Piri
        Reis sayfasındaki (pirireis.mgm.gov.tr) gerçek istasyon
        ölçümlerindedir. Dalga yüksekliği/periyodu/yönü Piri Reis kaynağında bulunmadığından 
        Open-Meteo'dan gelir. Döndürülen sözlükte `kaynaklar` alanı hangi verinin nereden geldiğini gösterir.

        Kapsam notu: Open-Meteo'nun deniz modeli yalnızca deniz
        grid hücrelerini kapsar; Piri Reis istasyonu da bulunamazsa
        (ikisi de kapsam dışıysa) tüm alanlar null döner ve
        `kapsamDisi: true` ile işaretlenir
        """
        acik_meteo = self._open_meteo_deniz(enlem, boylam)
        sonuc: dict[str, Any] = {
            "denizSuyuSicakligi": acik_meteo["denizSuyuSicakligi"],
            "dalgaYuksekligi": acik_meteo["dalgaYuksekligi"],
            "dalgaPeriyodu": acik_meteo["dalgaPeriyodu"],
            "dalgaYonu": acik_meteo["dalgaYonu"],
            "olcumZamani": acik_meteo["olcumZamani"],
            "istasyon": None,
            "kaynaklar": {
                "denizSuyuSicakligi": "open-meteo",
                "dalga": "open-meteo",
            },
        }

        try:
            istasyon = self._piri_reis_en_yakin_istasyon(enlem, boylam)
            if istasyon is not None:
                sonuc["denizSuyuSicakligi"] = istasyon["sicaklik"]
                sonuc["olcumZamani"] = istasyon.get("olcumZamani") or sonuc["olcumZamani"]
                sonuc["kaynaklar"]["denizSuyuSicakligi"] = "piri-reis"
                sonuc["istasyon"] = {
                    "ad": istasyon["ad"],
                    "il": istasyon.get("il"),
                    "ilce": istasyon.get("ilce"),
                    "enlem": istasyon["enlem"],
                    "boylam": istasyon["boylam"],
                }
        except MGMWeatherError as exc:
            logger.warning(
                "Piri Reis deniz suyu sıcaklığı alınamadı, Open-Meteo kullanılıyor: %s",
                exc,
            )

        kapsam_disi = (
            sonuc["denizSuyuSicakligi"] is None and sonuc["dalgaYuksekligi"] is None
        )
        sonuc["kapsamDisi"] = kapsam_disi
        sonuc["aciklama"] = (
            "Kıyıya/açık denize yakın koordinat gerektirir; karasal "
            "konumlarda ve Piri Reis'in kapsamadığı kıyılarda tüm "
            "alanlar null döner."
            if kapsam_disi
            else None
        )
        return sonuc

    # Günlük tahmin (5 günlük)
    def _tahmin_ttl(self) -> float:
        if self.cache_ttl_seconds <= 0:
            return self.cache_ttl_seconds
        return self.tahmin_ttl_saniye

    def gunluk_tahmin(self, istasyon_id: int | str) -> list[dict[str, Any]]:
        """Bir istasyon için 5 günlük tahmini gün gün liste olarak döndürür."""
        data = self._get(
            "tahminler/gunluk", {"istno": istasyon_id}, ttl_override=self._tahmin_ttl()
        )
        if not data:
            raise MGMWeatherError(
                f"{istasyon_id} numaralı istasyon için tahmin verisi bulunamadı."
            )
        kayit = data[0]
        bugun = _dt.date.today()
        sonuc = []
        for gun in range(1, 6):
            kod = kayit.get(f"hadiseGun{gun}")
            sonuc.append(
                {
                    "tarih": (bugun + _dt.timedelta(days=gun - 1)).isoformat(),
                    "enDusuk": kayit.get(f"enDusukGun{gun}"),
                    "enYuksek": kayit.get(f"enYuksekGun{gun}"),
                    "enDusukNem": kayit.get(f"enDusukNemGun{gun}"),
                    "enYuksekNem": kayit.get(f"enYuksekNemGun{gun}"),
                    "ruzgarHizi": kayit.get(f"ruzgarHizGun{gun}"),
                    "durumKodu": kod,
                    "durum": CONDITION_CODES.get(kod, kod),
                }
            )
        return sonuc

    # Tarımsal don / kırağı riski — MGM'nin ayrı bir resmi don uç noktası
    # yok; bu yüzden 5 günlük tahminin (enDusuk, nem, rüzgar) üzerinden
    # ziraat meteorolojisinde yaygın kullanılan eşiklerle sezgisel bir
    # risk sınıflandırması yapılır. RESMİ MGM DON UYARISI DEĞİLDİR.
    _DON_ESIKLERI = (
        # (üst_sinir_C, seviye, aciklama)
        (-10.0, "Çok Kuvvetli Don", "Hassas tüm bitkiler için ciddi zarar riski."),
        (-5.0, "Kuvvetli Don", "Çoğu bitki türü için zarar riski yüksek."),
        (-2.0, "Orta Don", "Soğuğa hassas bitkilerde zarar görülebilir."),
        (0.0, "Hafif Don", "Hassas fide ve çiçeklerde zarar olasılığı var."),
        (4.0, "Kırağı Riski", "Açık/durgun gecelerde yüzey kırağısı olasılığı var."),
    )

    def _don_seviyesi(self, en_dusuk: float | None) -> tuple[str, str]:
        if en_dusuk is None:
            return "Bilinmiyor", "Tahmin verisi eksik."
        for esik, seviye, aciklama in self._DON_ESIKLERI:
            if en_dusuk <= esik:
                return seviye, aciklama
        return "Risk Yok", "Don/kırağı beklenmiyor."

    def don_kiragi_riski(self, istasyon_id: int | str, il: str = "", ilce: str | None = None) -> dict[str, Any]:
        """
        Verilen istasyon için 5 günlük tarımsal don/kırağı riski tahmini.

        Yöntem: gunluk_tahmin()'den gelen günlük en düşük sıcaklık (°C)
        eşik tablosuna göre sınıflandırılır (Kırağı Riski / Hafif /
        Orta / Kuvvetli / Çok Kuvvetli Don). Ayrıca düşük rüzgar
        (<10 km/h) + yüksek nem (>%60) birlikte görüldüğünde radyatif
        kırağı oluşumu için elverişli koşul olduğu ayrıca işaretlenir
        (`kiragiKosuluUygun`) — bu, çıplak gökyüzü/durgun gece gibi
        klasik kırağı oluşum koşullarının sıcaklık-dışı bir yaklaşımıdır.

        Not: Bu, MGM'nin resmi bir don uyarı ürünü DEĞİLDİR; 5 günlük
        sıcaklık tahmininden türetilmiş bir risk göstergesidir. Kritik
        tarımsal kararlar için MGM'nin resmi tarım meteorolojisi
        bültenleri esas alınmalıdır.
        """
        gunler = self.gunluk_tahmin(istasyon_id)

        sonuc_gunler = []
        seviye_sirasi = {
            "Risk Yok": 0,
            "Bilinmiyor": 0,
            "Kırağı Riski": 1,
            "Hafif Don": 2,
            "Orta Don": 3,
            "Kuvvetli Don": 4,
            "Çok Kuvvetli Don": 5,
        }
        en_yuksek_risk_sirasi = 0
        for gun in gunler:
            en_dusuk = gun.get("enDusuk")
            nem = gun.get("enYuksekNem")
            ruzgar = gun.get("ruzgarHizi")
            seviye, aciklama = self._don_seviyesi(
                float(en_dusuk) if en_dusuk is not None else None
            )
            kiragi_kosulu_uygun = (
                en_dusuk is not None
                and float(en_dusuk) <= 4.0
                and nem is not None
                and float(nem) >= 60.0
                and ruzgar is not None
                and float(ruzgar) < 10.0
            )
            sonuc_gunler.append(
                {
                    "tarih": gun.get("tarih"),
                    "enDusukSicaklik": en_dusuk,
                    "seviye": seviye,
                    "aciklama": aciklama,
                    "kiragiKosuluUygun": kiragi_kosulu_uygun,
                }
            )
            en_yuksek_risk_sirasi = max(
                en_yuksek_risk_sirasi, seviye_sirasi.get(seviye, 0)
            )

        genel_seviye = next(
            (k for k, v in seviye_sirasi.items() if v == en_yuksek_risk_sirasi),
            "Risk Yok",
        )
        return {
            "il": il,
            "ilce": ilce,
            "genelRiskSeviyesi": genel_seviye,
            "gunler": sonuc_gunler,
            "aciklama": (
                "5 günlük sıcaklık tahmininden türetilmiş sezgisel risk "
                "göstergesidir; MGM'nin resmi don uyarı ürünü değildir."
            ),
        }

    # Saatlik tahmin
    def saatlik_tahmin(self, istasyon_id: int | str) -> list[dict[str, Any]]:
        """Bir istasyon için saatlik tahmin verisini döndürür (mevcutsa).

        gunluk_tahmin() gibi _tahmin_ttl() kullanır (bkz. orada).
        """
        data = self._get(
            "tahminler/saatlik", {"istno": istasyon_id}, ttl_override=self._tahmin_ttl()
        )
        return data or []

    # Gün doğumu ve batımı (sunrise-sunset.org üzerinden)
    def gun_dogumu_batimi(self, enlem: float, boylam: float) -> dict[str, str]:
        params = {"lat": enlem, "lng": boylam, "formatted": 0}
        cache_key = self._cache_key("gun-dogumu-batimi", params)

        def loader() -> dict[str, str]:
            try:
                resp = self.session.get(
                    self.SUNRISE_URL, params=params, timeout=self.timeout
                )
                resp.raise_for_status()
                sonuc = resp.json()["results"]
            except (requests.RequestException, KeyError, ValueError) as exc:
                raise MGMWeatherError(
                    f"Gün doğumu/batımı verisi alınamadı: {exc}"
                ) from exc

            tz = _dt.timezone(_dt.timedelta(hours=3))  # Bizim saat (UTC+3)
            dogum = _dt.datetime.fromisoformat(sonuc["sunrise"]).astimezone(tz)
            batim = _dt.datetime.fromisoformat(sonuc["sunset"]).astimezone(tz)
            return {
                "gunDogumu": dogum.strftime("%H:%M"),
                "gunBatimi": batim.strftime("%H:%M"),
            }

        return self._cached_get(cache_key, loader)

    _AY_EVRE_ADLARI = (
        "Yeni Ay",
        "Hilal (Büyüyen)",
        "İlk Dördün",
        "Şişkin Ay (Büyüyen)",
        "Dolunay",
        "Şişkin Ay (Küçülen)",
        "Son Dördün",
        "Hilal (Küçülen)",
    )
    _SINODIK_AY_GUN = 29.530588861
    # 2000-01-06 18:14 UTC referans yeni ay (bilinen astronomik epoch)
    _AY_EPOKU = _dt.datetime(2000, 1, 6, 18, 14, tzinfo=_dt.timezone.utc)

    def ay_evresi(self, tarih: _dt.date | None = None) -> dict[str, Any]:
        """
        Verilen tarih (UTC gün ortası referans alınarak) için ay evresini
        yerel olarak hesaplar; dış servis/bağımlılık gerektirmez.

        Döner: evreAdi, yasGunu (0-29.53), aydinlanmaOrani (0-1),
        buyuyorMu (bir sonraki dolunaya mı yaklaşıyor).
        """
        an = _dt.datetime.combine(
            tarih or _dt.datetime.now(_dt.timezone.utc).date(),
            _dt.time(hour=12),
            tzinfo=_dt.timezone.utc,
        )
        gecen_gun = (an - self._AY_EPOKU).total_seconds() / 86400.0
        yas = gecen_gun % self._SINODIK_AY_GUN

        evre_orani = yas / self._SINODIK_AY_GUN  # 0-1
        evre_indeksi = int((evre_orani * 8) + 0.5) % 8
        aydinlanma = (1 - math.cos(2 * math.pi * evre_orani)) / 2

        return {
            "evreAdi": self._AY_EVRE_ADLARI[evre_indeksi],
            "yasGunu": round(yas, 2),
            "aydinlanmaOrani": round(aydinlanma, 4),
            "buyuyorMu": evre_orani < 0.5,
        }

    def il_sinirlari_geojson(self) -> dict[str, Any]:
        """
        Türkiye'nin 81 ilinin sınır poligonlarını GeoJSON FeatureCollection
        olarak döner. Uzun TTL ile önbelleklenir çünkü idari sınırlar pratikte değişmez.
        """
        cache_key = self._cache_key("il-sinirlari-geojson")

        def loader() -> dict[str, Any]:
            try:
                resp = self.session.get(
                    self.GEOJSON_IL_SINIRLARI_URL, timeout=self.timeout
                )
                resp.raise_for_status()
                veri = resp.json()
            except (requests.RequestException, ValueError) as exc:
                raise MGMWeatherError(
                    f"İl sınırları GeoJSON verisi alınamadı: {exc}"
                ) from exc
            if veri.get("type") != "FeatureCollection" or not veri.get("features"):
                raise MGMWeatherError("İl sınırları GeoJSON verisi beklenen formatta değil.")
            return veri

        return self._cached_get(
            cache_key, loader, ttl_override=self.geojson_sinir_ttl_saniye
        )

    def _geojson_il_adini_coz(self, ham_ad: str) -> str | None:
        """GeoJSON kaynağındaki il adını 81-il listesindeki kanonik ada
        çözer; önce bilinen alias sözlüğüne, sonra difflib yakın eşleşmeye
        bakar."""
        alias = self.GEOJSON_IL_ALIASLARI.get(_tr_normalize(ham_ad))
        if alias:
            return alias
        return self._il_yakin_eslesme(ham_ad)

    def harita_geojson(self) -> dict[str, Any]:
        """
        `/map/geojson` uç noktasının üst düzey yardımcı fonksiyonu:
        il sınırları GeoJSON'unu MGM son durum sıcaklıklarıyla birleştirip
        doğrudan Leaflet/Mapbox'a beslenebilecek tek bir FeatureCollection
        döner. Her feature'ın `properties` alanına şunlar eklenir:
        `il`, `sicaklik` (°C, bulunamazsa null), `durum`, `guncellemeZamani`.

        Sınır verisi bulunamayan/çözülemeyen il adları `sicaklik: null`
        ile birlikte yine de haritada kalır
        """
        sinirlar = self.il_sinirlari_geojson()

        def _il_sicakligi(il_adi: str) -> dict[str, Any]:
            try:
                istasyon = self.ilce_istasyonu(il_adi)
                istasyon_id = istasyon.get("istasyonId") or istasyon.get("merkezId")
                enlem = istasyon.get("enlem") or istasyon.get("lat")
                boylam = istasyon.get("boylam") or istasyon.get("lon")
                cache_key = self._cache_key("harita-sicaklik", {"il": il_adi})

                def loader() -> dict[str, Any]:
                    guncel = self.guncel_durum_yedekli(istasyon_id, enlem, boylam)
                    return {
                        "sicaklik": guncel.get("sicaklik"),
                        "durum": guncel.get("durum"),
                        "guncellemeZamani": guncel.get("olcumZamani"),
                    }

                return self._cached_get(
                    cache_key, loader, ttl_override=self.harita_sicaklik_ttl_saniye
                )
            except MGMWeatherError:
                return {"sicaklik": None, "durum": None, "guncellemeZamani": None}

        il_adlari = sorted({kayit["il"] for kayit in TURKIYE_ILLERI})
        with ThreadPoolExecutor(max_workers=min(len(il_adlari), 16)) as havuz:
            sicakliklar = dict(
                zip(il_adlari, havuz.map(_il_sicakligi, il_adlari))
            )

        features_out = []
        for feature in sinirlar["features"]:
            ham_ad = (feature.get("properties") or {}).get("name", "")
            kanonik_il = self._geojson_il_adini_coz(ham_ad)
            veri = sicakliklar.get(kanonik_il, {}) if kanonik_il else {}

            yeni_feature = copy.deepcopy(feature)
            yeni_feature.setdefault("properties", {})
            yeni_feature["properties"]["il"] = kanonik_il or ham_ad
            yeni_feature["properties"]["sicaklik"] = veri.get("sicaklik")
            yeni_feature["properties"]["durum"] = veri.get("durum")
            yeni_feature["properties"]["guncellemeZamani"] = veri.get(
                "guncellemeZamani"
            )
            features_out.append(yeni_feature)

        return {"type": "FeatureCollection", "features": features_out}

    def _il_yakin_eslesme(self, token: str) -> str | None:
        """
        Verilen kelimeyi 81 il listesindeki en yakın ile eşler (typo
        toleranslı). Önce tam normalize eşleşmeye bakar, olmazsa stdlib
        difflib ile yakın eşleşme dener — 81 sabit string üzerinde
        çalıştığı için ağır bir NLP/ML kütüphanesi gerekmez, difflib
        fazlasıyla yeterli. Eşleşme yeterince güçlü değilse None döner.
        """
        hedef = _tr_normalize(token)
        il_map = {_tr_normalize(kayit["il"]): kayit["il"] for kayit in TURKIYE_ILLERI}
        if hedef in il_map:
            return il_map[hedef]
        yakinlar = difflib.get_close_matches(hedef, il_map.keys(), n=1, cutoff=0.75)
        return il_map[yakinlar[0]] if yakinlar else None

    @staticmethod
    def _sorguyu_parcala(sorgu: str) -> list[str]:
        """'kadikoy/istanbul', 'kadikoy, istanbul', 'istanbul kadikoy' gibi
        serbest metin girdilerini parçalara ayırır."""
        parcalar = re.split(r"[/,]+|\s+", sorgu.strip())
        return [p for p in parcalar if p]

    def _open_meteo_geocode(self, sorgu: str, adet: int = 5) -> list[dict[str, Any]]:
        """
        Serbest metin bir yer adını (mahalle, semt, önemli bina/kurum adı
        dahil) key gerektirmeyen Open-Meteo Geocoding API'siyle koordinata
        çözer. akilli_yer_bul()'un ilk iki katmanı (tam eşleşme, il+ilçe
        parçalama) sonuç veremediğinde son çare olarak kullanılır.
        """
        params = {"name": sorgu, "count": adet, "language": "tr", "format": "json"}
        cache_key = self._cache_key("open-meteo-geocode", params)

        def loader() -> list[dict[str, Any]]:
            try:
                resp = self.session.get(
                    self.OPEN_METEO_GEOCODE_URL, params=params, timeout=self.timeout
                )
                resp.raise_for_status()
                return resp.json().get("results") or []
            except (requests.RequestException, ValueError) as exc:
                raise MGMWeatherError(
                    f"Open-Meteo geocoding servisinden veri alınamadı: {exc}"
                ) from exc

        return self._cached_get(cache_key, loader)

    def akilli_yer_bul(self, sorgu: str) -> dict[str, Any]:
        """
        Serbest metin bir sorguyu ("kadıköy", "kadikoy/istanbul",
        "maslak itü" gibi) bir yere çözümlemeye çalışır. Katmanlı çalışır,
        her katman bir öncekinin çözemediği durumda devreye girer:

        1. **Tam eşleşme** — sorgunun tamamı 81 ilden biriyle (typo
           toleranslı) eşleşiyorsa, o ilin varsayılan istasyonu kullanılır.
           Ağ isteği yok.
        2. **Parçalama** — sorgu '/', ',' ya da boşlukla ayrılmış
           parçalara bölünür; parçalardan biri bilinen bir ile (typo
           toleranslı) yakınsa, geri kalan parça(lar) ilçe adayı olarak
           doğrudan MGM'ye sorulur (bkz. ilce_istasyonu — MGM'nin il+ilçe
           birlikte verildiğinde doğru sonucu döndüğü ayrıca doğrulanmış
           bir davranıştır). Tek bir MGM isteği.
        3. **Geocoding** — ilk iki katman sonuç vermezse, sorgu
           Open-Meteo'nun (key gerektirmeyen) geocoding servisine
           gönderilir. Dönen en iyi aday tekrar MGM'de (il+ilçe olarak)
           denenir; MGM'de de bulunamazsa (örn. "Maslak" resmi bir ilçe
           değil, bir mahalle) doğrudan o koordinatla Open-Meteo'dan hava
           durumu döndürülür.

        Dönüş sözlüğü her zaman bir `durum` alanı içerir:
        - `"cozuldu"`: `il`/`ilce` (ya da doğrudan `enlem`/`boylam`) ve
          `yontem` doludur.
        - `"belirsiz"`: geocoding, farklı illerde birden fazla makul aday
          döndürdü — `secenekler` doludur, tahmin yürütülmedi.
        - `"bulunamadi"`: hiçbir katman bir sonuç üretemedi.
        """
        sorgu = (sorgu or "").strip()
        if not sorgu:
            return {"durum": "bulunamadi", "sorgu": sorgu}

        # Katman 1: sorgunun tamamı doğrudan bir il mi?
        il = self._il_yakin_eslesme(sorgu)
        if il:
            return {"durum": "cozuldu", "yontem": "il-eslesme", "il": il, "ilce": None}

        # Katman 2: parçalama — bir parça il, kalan(lar) ilçe adayı
        parcalar = self._sorguyu_parcala(sorgu)
        if len(parcalar) >= 2:
            for i, parca in enumerate(parcalar):
                il = self._il_yakin_eslesme(parca)
                if not il:
                    continue
                ilce_adayi = " ".join(p for j, p in enumerate(parcalar) if j != i).strip()
                if not ilce_adayi:
                    continue
                try:
                    istasyon = self.ilce_istasyonu(il, ilce_adayi)
                except MGMWeatherError:
                    continue
                return {
                    "durum": "cozuldu",
                    "yontem": "il-ilce-parcalama",
                    "il": il,
                    "ilce": istasyon.get("ilce", ilce_adayi),
                }

        # Katman 3: geocoding (typo/semantik "maslak itü" gibi girdiler)
        try:
            adaylar = self._open_meteo_geocode(sorgu)
        except MGMWeatherError:
            adaylar = []

        if not adaylar:
            # GeoNames'te "maslak itü" gibi birleşik bir kayıt yoktur. 
            # Kelimeleri tek tek deneyin sonra ilk sonuç veren
            # kelimeyi kullanın. 2 karakterden kısa kelimeler atlanır.
            for parca in self._sorguyu_parcala(sorgu):
                if len(parca) < 3:
                    continue
                try:
                    parca_adaylari = self._open_meteo_geocode(parca)
                except MGMWeatherError:
                    parca_adaylari = []
                if parca_adaylari:
                    adaylar = parca_adaylari
                    break

        tr_adaylar = [a for a in adaylar if a.get("country_code") == "TR"]
        adaylar = tr_adaylar or adaylar
        if not adaylar:
            return {"durum": "bulunamadi", "sorgu": sorgu}

        # İlk birkaç aday farklı illere yayılıyorsa gerçekten belirsiz
        # demektir, tahmin yürütmek yerine seçenek sunuyoruz.
        farkli_iller = {a.get("admin1") for a in adaylar[:3] if a.get("admin1")}
        if len(farkli_iller) > 1:
            return {
                "durum": "belirsiz",
                "sorgu": sorgu,
                "secenekler": [
                    {
                        "yer": a.get("name"),
                        "il": a.get("admin1"),
                        "ulke": a.get("country"),
                        "enlem": a.get("latitude"),
                        "boylam": a.get("longitude"),
                    }
                    for a in adaylar[:5]
                ],
            }

        en_iyi = adaylar[0]
        il_adayi = en_iyi.get("admin1")
        yer_adi = en_iyi.get("name")
        if il_adayi and yer_adi:
            il = self._il_yakin_eslesme(il_adayi)
            if il:
                try:
                    istasyon = self.ilce_istasyonu(il, yer_adi)
                    return {
                        "durum": "cozuldu",
                        "yontem": "geocoding-mgm",
                        "il": il,
                        "ilce": istasyon.get("ilce", yer_adi),
                    }
                except MGMWeatherError:
                    pass

        # MGM'de çözülemedi ama geocoding bir koordinat verdi ise doğrudan Open-Meteo'ya düşer.
        enlem, boylam = en_iyi.get("latitude"), en_iyi.get("longitude")
        if enlem is not None and boylam is not None:
            return {
                "durum": "cozuldu",
                "yontem": "geocoding-dogrudan",
                "il": il_adayi,
                "ilce": yer_adi,
                "enlem": enlem,
                "boylam": boylam,
            }

        return {"durum": "bulunamadi", "sorgu": sorgu}

    def hava_durumu(self, il: str, ilce: str | None = None) -> dict[str, Any]:
        """
        Verilen il/ilçe için güncel durum + 5 günlük tahmini tek seferde
        toplayıp döndüren üst düzey yardımcı fonksiyon.
        """
        istasyon = self.ilce_istasyonu(il, ilce)
        istasyon_id = istasyon.get("istasyonId") or istasyon.get("merkezId")

        sonuc: dict[str, Any] = {
            "il": istasyon.get("il", il),
            "ilce": istasyon.get("ilce"),
            "istasyonId": istasyon_id,
            "enlem": istasyon.get("enlem") or istasyon.get("lat"),
            "boylam": istasyon.get("boylam") or istasyon.get("lon"),
        }
        sonuc["guncel"] = self.guncel_durum_yedekli(istasyon_id, sonuc["enlem"], sonuc["boylam"])

        # Tahmin için fallback yok (bilinçli kapsam dışı, bkz.
        # guncel_durum_yedekli docstring'i) ama MGM çökükken en azından
        # "guncel" alanının (fallback ile) döndüğü bir yanıtı MGM'nin
        # tahmin uç noktası tek başına çökertmesin diye tolere ediyoruz.
        try:
            sonuc["tahmin"] = self.gunluk_tahmin(istasyon_id)
        except MGMWeatherError:
            sonuc["tahmin"] = []

        try:
            if sonuc["enlem"] and sonuc["boylam"]:
                sonuc.update(self.gun_dogumu_batimi(sonuc["enlem"], sonuc["boylam"]))
        except MGMWeatherError:
            pass

        # Ay evresi yerel hesaplandığı için harici servise bağımlı değil
        try:
            sonuc["ayEvresi"] = self.ay_evresi()
        except Exception:  # noqa: BLE001 - yerel hesap, beklenmedik durum korumas
            pass

        return sonuc

    def hava_durumu_akilli(self, sorgu: str) -> dict[str, Any]:
        """
        `/ara` uç noktasının üst düzey yardımcı fonksiyonu: akilli_yer_bul()
        ile serbest metin sorguyu çözüp hava durumunu döner.

        - "cozuldu" ise hava_durumu()'nun döndürdüğü sözlüğe `durum`,
          `sorgu`, `yontem` alanları eklenerek döner (MGM istasyonuna
          çözüldüyse tam hava_durumu() yanıtı; sadece koordinat çözüldüyse
          — örn. "Maslak" gibi resmi ilçe olmayan bir yer — Open-Meteo'dan
          yalnızca güncel durum, tahmin boş liste).
        - "belirsiz" ise hava durumu getirmeden seçenek listesini döner;
          çağıran kullanıcıya seçim yaptırmalı.
        - Hiçbir şey çözülemezse MGMWeatherError fırlatır.
        """
        sonuc = self.akilli_yer_bul(sorgu)
        if sonuc["durum"] == "bulunamadi":
            raise MGMWeatherError(f"'{sorgu}' herhangi bir yere çözümlenemedi.")
        if sonuc["durum"] == "belirsiz":
            return sonuc

        if sonuc["yontem"] == "geocoding-dogrudan":
            guncel = self._open_meteo_guncel_durum(sonuc["enlem"], sonuc["boylam"])
            guncel["kaynak"] = "open-meteo"
            return {
                "durum": "cozuldu",
                "sorgu": sorgu,
                "yontem": sonuc["yontem"],
                "il": sonuc.get("il"),
                "ilce": sonuc.get("ilce"),
                "enlem": sonuc["enlem"],
                "boylam": sonuc["boylam"],
                "guncel": guncel,
                "tahmin": [],
            }

        veri = self.hava_durumu(sonuc["il"], sonuc.get("ilce"))
        veri["durum"] = "cozuldu"
        veri["sorgu"] = sorgu
        veri["yontem"] = sonuc["yontem"]
        return veri

    def _nominatim_ters_geocode(self, enlem: float, boylam: float) -> dict[str, Any] | None:
        """
        Koordinatı bir adres bileşenine çözer: OpenStreetMap'in
        ücretsiz Nominatim servisi. Kullanım politikası
        saniyede 1 istekle sınırlı ve tanımlayıcı bir User-Agent zorunlu
        kılıyor. Bu proje ölçeğinde sorun değil; yüksek trafikli bir deploy'da kendi Nominatim
        instance'ınızı barındırmanız ya da ücretli bir alternatif kullanmanız gerekir.

        Adres bulunamazsa None döner
        """
        params = {
            "lat": enlem,
            "lon": boylam,
            "format": "jsonv2",
            "addressdetails": 1,
            "accept-language": "tr",
            "zoom": 10,  # il/ilçe seviyesi yeterli
        }
        cache_key = self._cache_key("nominatim-reverse", params)

        def loader() -> dict[str, Any] | None:
            try:
                resp = self.session.get(
                    self.NOMINATIM_REVERSE_URL,
                    params=params,
                    headers={"User-Agent": self.NOMINATIM_USER_AGENT},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                veri = resp.json()
            except (requests.RequestException, ValueError) as exc:
                raise MGMWeatherError(
                    f"Nominatim ters geocoding servisinden veri alınamadı: {exc}"
                ) from exc
            return veri.get("address")

        return self._cached_get(cache_key, loader)

    @staticmethod
    def _nominatim_il_ilce_adaylari(adres: dict[str, Any]) -> tuple[str | None, str | None]:
        """
        Nominatim "address" objesinden il ve ilçe adaylarını çıkarır.
        
        Türkiye OSM verilerinde ilçe etiketleri standart olmadığından
        (county, city_district, town, suburb veya district olabilmektedir)
        sistem sırayla tarama yapar ve ilk dolu değeri kullanır.
        Tek bir alan adına bağımlı kalınarak yaşanabilecek veri kayıpları engellenir.
        """
        il_adayi = adres.get("state")
        ilce_adayi = None
        for anahtar in ("county", "city_district", "town", "suburb", "district", "city"):
            deger = adres.get(anahtar)
            if deger:
                ilce_adayi = deger
                break
        return il_adayi, ilce_adayi

    def hava_durumu_konum(self, enlem: float, boylam: float) -> dict[str, Any]:
        """
        Koordinatları Nominatim ile ters geocoding yaparak il/ilçe adına çevirir 
        ve MGM'de arar. MGM'de bulunamazsa (veya geocoding başarısız olursa) 
        Open-Meteo üzerinden anlık durumu döner (fallback).
        """
        il_adayi: str | None = None
        ilce_adayi: str | None = None
        try:
            adres = self._nominatim_ters_geocode(enlem, boylam)
            if adres:
                il_adayi, ilce_adayi = self._nominatim_il_ilce_adaylari(adres)
        except MGMWeatherError:
            pass  # ters geocoding çökerse MGM denemeden Open-Meteo'ya düş

        if il_adayi:
            il = self._il_yakin_eslesme(il_adayi)
            if il:
                try:
                    veri = self.hava_durumu(il, ilce_adayi)
                    veri["durum"] = "cozuldu"
                    veri["yontem"] = "nominatim-mgm" if ilce_adayi else "nominatim-mgm-il-varsayilan"
                    return veri
                except MGMWeatherError:
                    pass

        guncel = self._open_meteo_guncel_durum(enlem, boylam)
        guncel["kaynak"] = "open-meteo"
        return {
            "durum": "cozuldu",
            "yontem": "nominatim-open-meteo" if il_adayi else "open-meteo-dogrudan",
            "il": il_adayi,
            "ilce": ilce_adayi,
            "enlem": enlem,
            "boylam": boylam,
            "guncel": guncel,
            "tahmin": [],
        }


if __name__ == "__main__":
    import json
    import sys

    il_adi = sys.argv[1] if len(sys.argv) > 1 else "İstanbul"
    ilce_adi = sys.argv[2] if len(sys.argv) > 2 else None

    client = MGMWeather()
    print(json.dumps(client.hava_durumu(il_adi, ilce_adi), ensure_ascii=False, indent=2))
