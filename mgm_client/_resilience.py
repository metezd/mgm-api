from __future__ import annotations

import datetime as _dt
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ._constants import (
    KAYNAK_ISTEK_SAYAC,
    KAYNAK_ISTEK_SURESI,
    KAYNAK_SAGLIK_HATA_ESIGI,
    KAYNAK_SON_BASARI_GAUGE,
    KAYNAK_UP_GAUGE,
    KAYNAKLAR,
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

    Thread-safe'tir. Birden çok iş parçacığı aynı anda `basarisiz()` /
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
        """Bir MGM isteği başarıyla tamamlandığında çağrılır, devreyi kapatır."""
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


@dataclass
class _KaynakDurumu:
    basarili_toplam: int = 0
    hata_toplam: int = 0
    ardisik_hata: int = 0
    son_basarili: float | None = None  # unix zamanı
    son_hata: float | None = None
    son_hata_turu: str | None = None
    son_gecikme_ms: float | None = None
    ortalama_gecikme_ms: float | None = None  # üstel hareketli ortalama


@dataclass
class _KaynakSagligi:
    """Dış kaynakların (MGM, Open-Meteo, Nominatim, ...) pasif sağlık izleyicisi.

    Aktif yoklama yapmaz: yalnızca gerçek trafiğin sonucunu kaydeder.
    Bu yüzden hiç istek atılmamış bir kaynak "bilinmiyor" görünür.

    Kaynak durumu:
    - **bilinmiyor**: henüz hiç gerçek istek atılmadı.
    - **ok**: son istek başarılı (ardışık hata yok).
    - **kararsiz**: 1..(eşik-1) ardışık hata var.
    - **hata**: `hata_esigi` veya daha fazla ardışık hata.

    Devre kesiciden (`_CircuitBreaker`) bağımsızdır: o yalnızca MGM'yi korur
    ve istek akışını keser, bu sınıf ise her kaynağı ayrı ayrı gözlemler ve
    hiçbir isteği engellemez. Thread-safe'tir.

    Hata mesajı SAKLANMAZ; yalnızca hata türü (istisna sınıfı adı ya da HTTP
    kodu). Mesajlar URL/parametre (ör. kullanıcının koordinatları) içerebilir
    ve bu bilgi kimlik doğrulamasız `/health/kaynaklar` ile dışarı çıkar.
    """

    hata_esigi: int = KAYNAK_SAGLIK_HATA_ESIGI
    _EMA_ALFA = 0.2
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _durumlar: dict[str, _KaynakDurumu] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for kaynak in KAYNAKLAR:
            self._durumlar[kaynak] = _KaynakDurumu()

    @staticmethod
    def hata_turu(exc: BaseException) -> str:
        """Hata mesajını sızdırmadan sınıflandırır: `http_503`, `ConnectTimeout`..."""
        neden = exc.__cause__ or exc
        kod = getattr(exc, "durum_kodu", None) or getattr(
            getattr(neden, "response", None), "status_code", None
        )
        if kod:
            return f"http_{kod}"
        return type(neden).__name__

    def basari(self, kaynak: str, sure_saniye: float) -> None:
        simdi = time.time()
        ms = sure_saniye * 1000.0
        with self._lock:
            d = self._durumlar.setdefault(kaynak, _KaynakDurumu())
            d.basarili_toplam += 1
            d.ardisik_hata = 0
            d.son_basarili = simdi
            self._gecikme_guncelle(d, ms)
        KAYNAK_ISTEK_SAYAC.labels(kaynak=kaynak, sonuc="ok").inc()
        KAYNAK_ISTEK_SURESI.labels(kaynak=kaynak).observe(sure_saniye)
        KAYNAK_UP_GAUGE.labels(kaynak=kaynak).set(1)
        KAYNAK_SON_BASARI_GAUGE.labels(kaynak=kaynak).set(simdi)

    def hata(self, kaynak: str, sure_saniye: float, hata_turu: str) -> None:
        simdi = time.time()
        ms = sure_saniye * 1000.0
        with self._lock:
            d = self._durumlar.setdefault(kaynak, _KaynakDurumu())
            d.hata_toplam += 1
            d.ardisik_hata += 1
            d.son_hata = simdi
            d.son_hata_turu = hata_turu
            self._gecikme_guncelle(d, ms)
        KAYNAK_ISTEK_SAYAC.labels(kaynak=kaynak, sonuc="hata").inc()
        KAYNAK_ISTEK_SURESI.labels(kaynak=kaynak).observe(sure_saniye)
        KAYNAK_UP_GAUGE.labels(kaynak=kaynak).set(0)

    def _gecikme_guncelle(self, d: _KaynakDurumu, ms: float) -> None:
        d.son_gecikme_ms = round(ms, 1)
        if d.ortalama_gecikme_ms is None:
            d.ortalama_gecikme_ms = round(ms, 1)
        else:
            d.ortalama_gecikme_ms = round(
                self._EMA_ALFA * ms + (1 - self._EMA_ALFA) * d.ortalama_gecikme_ms, 1
            )

    def _durum(self, d: _KaynakDurumu) -> str:
        if d.basarili_toplam + d.hata_toplam == 0:
            return "bilinmiyor"
        if d.ardisik_hata == 0:
            return "ok"
        if d.ardisik_hata < self.hata_esigi:
            return "kararsiz"
        return "hata"

    @staticmethod
    def _iso(zaman: float | None) -> str | None:
        if zaman is None:
            return None
        return (
            _dt.datetime.fromtimestamp(zaman, tz=_dt.UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

    def ozet(self) -> dict[str, dict[str, Any]]:
        simdi = time.time()
        with self._lock:
            sonuc: dict[str, dict[str, Any]] = {}
            for kaynak, d in self._durumlar.items():
                sonuc[kaynak] = {
                    "durum": self._durum(d),
                    "son_basarili": self._iso(d.son_basarili),
                    "son_basarili_yas_saniye": (
                        None if d.son_basarili is None else int(simdi - d.son_basarili)
                    ),
                    "son_hata": self._iso(d.son_hata),
                    "son_hata_turu": d.son_hata_turu,
                    "ardisik_hata": d.ardisik_hata,
                    "basarili_toplam": d.basarili_toplam,
                    "hata_toplam": d.hata_toplam,
                    "son_gecikme_ms": d.son_gecikme_ms,
                    "ortalama_gecikme_ms": d.ortalama_gecikme_ms,
                }
            return sonuc
