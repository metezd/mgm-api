from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import unquote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ._constants import (
    _CACHED_AT_KEY,
    _VALUE_KEY,
    CACHE_KEY_MAX_LENGTH,
    CACHE_KEY_NAMESPACE,
    CACHE_KEY_VERSION,
    CACHE_RESPONSE_MAX_BYTES,
    CACHE_SONUC_SAYAC,
    CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    CIRCUIT_BREAKER_OPEN_SECONDS,
    CIRCUIT_BREAKER_WINDOW_SECONDS,
    REDIS_CONNECT_TIMEOUT,
    REDIS_HEALTH_CHECK_INTERVAL,
    REDIS_SOCKET_TIMEOUT,
    _tr_normalize,
    logger,
)
from ._errors import MGMCircuitOpenError, MGMWeatherError
from ._resilience import _CircuitBreaker, _InFlight


@dataclass
class _MGMWeatherTemel:
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
    OPEN_METEO_MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
    PIRI_REIS_DENIZ_SUYU_URL = "https://pirireis.mgm.gov.tr/deniz-suyu-sicakliklari"
    PIRI_REIS_HEADERS: ClassVar[dict[str, str]] = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like "
            "Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }
    NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
    NOMINATIM_USER_AGENT = "mgm-hava-durumu-api/1.0 (https://github.com/metezd/hava-durumu)"
    IBB_HAVA_KALITESI_ISTASYONLAR_URL = (
        "https://api.ibb.gov.tr/havakalitesi/OpenDataPortalHandler/GetAQIStations"
    )
    IBB_HAVA_KALITESI_OLCUM_URL = (
        "https://api.ibb.gov.tr/havakalitesi/OpenDataPortalHandler/GetAQIByStationId"
    )
    GEOJSON_IL_SINIRLARI_URL = (
        "https://raw.githubusercontent.com/alpers/Turkey-Maps-GeoJSON/master/tr-cities.json"
    )
    GEOJSON_IL_ALIASLARI: ClassVar[dict[str, str]] = {
        "afyon": "Afyonkarahisar",
        "k. maras": "Kahramanmaraş",
        "kahramanmaras": "Kahramanmaraş",
        "k.maras": "Kahramanmaraş",
    }

    HEADERS: ClassVar[dict[str, str]] = {
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
            # mgm_client paketinden gecikmeli (lazy) import: testler
            # mgm_client.REDIS_STARTUP_RETRY_ATTEMPTS/DELAY'i patch'leyerek
            # yeniden deneme davranışını kontrol eder. Dosya başında
            # from ._constants import ... yapmak değeri import anında
            # kopyalardı, burada __post_init__ çalışma zamanında
            # mgm_client'ın mevcut değerini okuyor
            import mgm_client as _mgm_client_paketi

            deneme_sayisi = _mgm_client_paketi.REDIS_STARTUP_RETRY_ATTEMPTS
            gecikme_saniye = _mgm_client_paketi.REDIS_STARTUP_RETRY_DELAY_SECONDS
            for deneme in range(1, deneme_sayisi + 1):
                try:
                    self.redis_client.ping()
                    son_hata = None
                    break
                except redis.RedisError as exc:
                    son_hata = exc
                    if deneme < deneme_sayisi:
                        logger.warning(
                            "Redis'e başlangıç bağlantısı başarısız (deneme %d/%d): %s, "
                            "%.1f sn sonra tekrar denenecek",
                            deneme,
                            deneme_sayisi,
                            exc,
                            gecikme_saniye,
                        )
                        time.sleep(gecikme_saniye)
            if son_hata is not None:
                raise MGMWeatherError(
                    f"Redis bağlantısı {deneme_sayisi} denemeden "
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

        Kayıt tazeyse doğrudan döner. TTL geçmiş ama stale
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
        paylaşılır. Kayıtlar her sonuçta temizlenir.
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
            except BaseException as exc:
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
        """Lider isteğin tamamlanmasını bekler, hata girerse yeniden fırlatır."""
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
            # Circuit breaker açıkken MGM'ye hiç istek atılmaz, hemen hata
            # dönülür. Not: bu yalnızca asıl ağ isteğini engeller ve çağıran
            # `_cached_get` zaten stale veri varsa onu döndürmüş olabilir. 
            # Yani MGM kesintisi sırasında elde stale veri varsa kullanıcı bundan
            # etkilenmez sadece breaker gereksiz ağ isteklerini keser
            if not self._circuit_breaker.izin_var_mi():
                logger.warning(
                    "Circuit breaker açık, MGM isteği atlanıyor: %s, params=%s",
                    url,
                    params,
                )
                raise MGMCircuitOpenError(
                    "MGM servisi art arda hata verdiği için circuit breaker "
                    f"açık, istek atlandı ({url})."
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

    @staticmethod
    def _cache_key_normalize(value: Any) -> Any:
        if isinstance(value, str):
            value = unicodedata.normalize("NFKC", unquote(value))
            return _tr_normalize(value)
        if isinstance(value, dict):
            return {
                _tr_normalize(str(key)): _MGMWeatherTemel._cache_key_normalize(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, (list, tuple)):
            return [_MGMWeatherTemel._cache_key_normalize(item) for item in value]
        return value

    @staticmethod
    def _cache_source(path: str) -> str:
        if path.startswith("open-meteo-"):
            return "open-meteo"
        if path.startswith("ibb-"):
            return "ibb"
        if path.startswith("nominatim-"):
            return "nominatim"
        if path.startswith("gun-dogumu"):
            return "sunrise-sunset"
        return "mgm"

    def _cache_key(self, path: str, params: dict[str, Any] | None = None) -> str:
        canonical = {
            "source": self._cache_source(path),
            "path": self._cache_key_normalize(path),
            "params": self._cache_key_normalize(params or {}),
        }
        serialized = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        key = f"{CACHE_KEY_NAMESPACE}:{CACHE_KEY_VERSION}:{canonical['source']}:{digest}"
        return key[:CACHE_KEY_MAX_LENGTH]

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

    @staticmethod
    def _cache_response_schema_dogrula(payload: Any) -> None:
        if not isinstance(payload, (dict, list)):
            raise MGMWeatherError("Cache yanıtı JSON object veya array olmalıdır.")
        try:
            serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise MGMWeatherError(f"Cache yanıtı JSON schema doğrulamasından geçemedi: {exc}") from exc
        if len(serialized.encode("utf-8")) > CACHE_RESPONSE_MAX_BYTES:
            raise MGMWeatherError(
                f"Cache yanıtı {CACHE_RESPONSE_MAX_BYTES} byte sınırını aşıyor."
            )

    def _cache_set(self, key: str, payload: Any) -> None:
        if self.cache_ttl_seconds <= 0:
            return
        self._cache_response_schema_dogrula(payload)
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

