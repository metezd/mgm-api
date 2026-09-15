from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


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
