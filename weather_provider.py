"""
weather_provider.py
--------------------
`app.py`'nin hava durumu verisine ihtiyaç duyduğu HER yerde bağımlı
olduğu soyut arayüz (Adapter Pattern / Dependency Inversion). `app.py`
"MGM'den veri al" demek yerine "WeatherProvider'dan veri al" der —
`mgm` global'i `MGMWeather` değil, `WeatherProvider` tipindedir.

Yarın MGM sistemi tamamen değişirse (ya da ikinci bir kaynak eklenirse)
yapılması gereken tek şey: bu arayüzü implemente eden yeni bir Adapter
sınıfı yazmak ve `app.py`'de `mgm = MGMAdapter(...)` satırını
`mgm = YeniAdapter(...)` ile değiştirmek. `app.py`'deki ~25 çağrı
noktasının HİÇBİRİNE dokunmaya gerek kalmaz, çünkü hepsi zaten somut
`MGMWeather` sınıfına değil bu arayüze göre yazılmıştır.

Arayüzdeki metod seti, `mgm_client.MGMWeather`'ın TÜM yeteneklerini
değil, yalnızca `app.py`'nin gerçekten çağırdığı alt kümeyi kapsar
("ports and adapters" mantığı: arayüz, sağlayıcının değil,
TÜKETİCİNİN ihtiyacına göre şekillenir). MGMWeather'ın arayüzde
olmayan başka metodları da olabilir, bunlar kütüphanenin kendi
public API'si olarak kalmaya devam eder.

`MGMAdapter`, bu arayüzü var olan `MGMWeather` istemcisi üzerinden
sağlayan somut adaptördür — ince bir delege (delegation) katmanıdır;
iş mantığının hiçbiri burada değil, `mgm_client` paketinde yaşar.
"""

from __future__ import annotations

import abc
import datetime as _dt
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mgm_client import MGMWeather


class WeatherProvider(abc.ABC):
    """app.py'nin bağımlı olduğu soyut hava durumu sağlayıcısı arayüzü."""

    @abc.abstractmethod
    def il_istasyonlari(self, il: str) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    def ilce_istasyonu(self, il: str, ilce: str | None = None) -> dict[str, Any]: ...

    @abc.abstractmethod
    def guncel_durum_yedekli(
        self,
        istasyon_id: int | str,
        enlem: float | None = None,
        boylam: float | None = None,
    ) -> dict[str, Any]: ...

    @abc.abstractmethod
    def gunluk_tahmin(self, istasyon_id: int | str) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    def saatlik_tahmin(self, istasyon_id: int | str) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    def hava_durumu(self, il: str, ilce: str | None = None) -> dict[str, Any]: ...

    @abc.abstractmethod
    def hava_durumu_akilli(self, sorgu: str) -> dict[str, Any]: ...

    @abc.abstractmethod
    def hava_durumu_konum(self, enlem: float, boylam: float) -> dict[str, Any]: ...

    @abc.abstractmethod
    def hava_kalitesi(self, enlem: float, boylam: float) -> dict[str, Any]: ...

    @abc.abstractmethod
    def polen_indeksi(self, enlem: float, boylam: float) -> dict[str, Any]: ...

    @abc.abstractmethod
    def deniz_durumu(self, enlem: float, boylam: float) -> dict[str, Any]: ...

    @abc.abstractmethod
    def gun_dogumu_batimi(self, enlem: float, boylam: float) -> dict[str, str]: ...

    @abc.abstractmethod
    def ay_evresi(self, tarih: _dt.date | None = None) -> dict[str, Any]: ...

    @abc.abstractmethod
    def don_kiragi_riski(
        self, istasyon_id: int | str, il: str = "", ilce: str | None = None
    ) -> dict[str, Any]: ...

    @abc.abstractmethod
    def akilli_ozet(self, il: str, ilce: str | None = None) -> dict[str, Any]: ...

    @abc.abstractmethod
    def uyarilar(self, il: str | None = None) -> dict[str, Any]: ...

    @abc.abstractmethod
    def en_dusuk_sicakliklar(self, tarih: str | None = None) -> dict[str, Any]: ...

    @abc.abstractmethod
    def en_yuksek_sicakliklar(self, tarih: str | None = None) -> dict[str, Any]: ...

    @abc.abstractmethod
    def toplam_yagislar(self, tarih: str | None = None) -> dict[str, Any]: ...

    @abc.abstractmethod
    def kar_kalinliklari(self) -> dict[str, Any]: ...

    @abc.abstractmethod
    def son_gozlemler(self) -> dict[str, Any]: ...

    @abc.abstractmethod
    def harita_geojson(self) -> dict[str, Any]: ...

    @abc.abstractmethod
    def circuit_breaker_saglik_ozeti(self) -> dict[str, str]: ...

    @abc.abstractmethod
    def redis_saglik_ozeti(self) -> dict[str, str]: ...


class MGMAdapter(WeatherProvider):
    """`WeatherProvider` arayüzünü `MGMWeather` istemcisi üzerinden
    sağlayan somut adaptör. Her metod, sarılan `MGMWeather` örneğine
    bire bir delege eder; davranış/iş mantığı değişmez."""

    def __init__(self, mgm_client: MGMWeather) -> None:
        self._mgm = mgm_client

    def il_istasyonlari(self, il):
        return self._mgm.il_istasyonlari(il)

    def ilce_istasyonu(self, il, ilce=None):
        return self._mgm.ilce_istasyonu(il, ilce)

    def guncel_durum_yedekli(self, istasyon_id, enlem=None, boylam=None):
        return self._mgm.guncel_durum_yedekli(istasyon_id, enlem, boylam)

    def gunluk_tahmin(self, istasyon_id):
        return self._mgm.gunluk_tahmin(istasyon_id)

    def saatlik_tahmin(self, istasyon_id):
        return self._mgm.saatlik_tahmin(istasyon_id)

    def hava_durumu(self, il, ilce=None):
        return self._mgm.hava_durumu(il, ilce)

    def hava_durumu_akilli(self, sorgu):
        return self._mgm.hava_durumu_akilli(sorgu)

    def hava_durumu_konum(self, enlem, boylam):
        return self._mgm.hava_durumu_konum(enlem, boylam)

    def hava_kalitesi(self, enlem, boylam):
        return self._mgm.hava_kalitesi(enlem, boylam)

    def polen_indeksi(self, enlem, boylam):
        return self._mgm.polen_indeksi(enlem, boylam)

    def deniz_durumu(self, enlem, boylam):
        return self._mgm.deniz_durumu(enlem, boylam)

    def gun_dogumu_batimi(self, enlem, boylam):
        return self._mgm.gun_dogumu_batimi(enlem, boylam)

    def ay_evresi(self, tarih=None):
        return self._mgm.ay_evresi(tarih)

    def don_kiragi_riski(self, istasyon_id, il="", ilce=None):
        return self._mgm.don_kiragi_riski(istasyon_id, il=il, ilce=ilce)

    def akilli_ozet(self, il, ilce=None):
        return self._mgm.akilli_ozet(il, ilce)

    def uyarilar(self, il=None):
        return self._mgm.uyarilar(il)

    def en_dusuk_sicakliklar(self, tarih=None):
        return self._mgm.en_dusuk_sicakliklar(tarih)

    def en_yuksek_sicakliklar(self, tarih=None):
        return self._mgm.en_yuksek_sicakliklar(tarih)

    def toplam_yagislar(self, tarih=None):
        return self._mgm.toplam_yagislar(tarih)

    def kar_kalinliklari(self):
        return self._mgm.kar_kalinliklari()

    def son_gozlemler(self):
        return self._mgm.son_gozlemler()

    def harita_geojson(self):
        return self._mgm.harita_geojson()

    def circuit_breaker_saglik_ozeti(self):
        return self._mgm.circuit_breaker_saglik_ozeti()

    def redis_saglik_ozeti(self):
        return self._mgm.redis_saglik_ozeti()

    def __getattr__(self, name: str) -> Any:
        # WeatherProvider arayüzünde henüz yer almayan (ör. testlerde ya
        # da ileride eklenecek) bir MGMWeather özelliğine erişilirse,
        # sert bir AttributeError yerine sarılan istemciye düşer. Bu bir
        # kaçış kapısıdır (escape hatch) — app.py'nin normal akışı hep
        # yukarıdaki arayüz metodlarını kullanmalı, buraya düşmemelidir.
        return getattr(self._mgm, name)
