from __future__ import annotations

import datetime as _dt
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from ._constants import CONDITION_CODES, WMO_CONDITION_CODES, _tr_normalize, logger
from ._errors import MGMWeatherError


class _SondurumMixin:
    """Uyarilar, istasyon cozumleme, anlik durum."""

    def uyarilar(self, il: str | None = None) -> dict[str, Any]:
        """
        MGM'nin meteorolojik uyarı (MeteoUYARI) verisini döndürür.

        Önemli: bu metod diğerlerinden farklı çalışır. Bu kodun yazıldığı
        sırada aktif bir uyarı olmadığı için `alarmlar` uç noktasının ham
        JSON şeması doğrulanamadı, bu yüzden veri dönüştürülmeden MGM'den
        geldiği gibi döner. Alan adlarını tahmin ederek Türkçeleştirmeye
        çalışmaz. Sebep: bu projede daha önce böyle bir tahmin
        (ilce_istasyonu'nun eski filtreleme mantığı) yanlış çıkıp gerçek
        veriyi "bulunamadı" göstermişti, aynı hata tekrarlanmasın diye
        ham geçiş tercih edildi.

        `il` verilirse MGM'ye doğrudan parametre olarak iletilir. Bunun
        gerçekten filtrelediği doğrulanamadı, zararsız bir passthrough'tur.

        Gerçek bir uyarı aktifken bu metod tekrar çalıştırılıp MGM'nin
        döndürdüğü gerçek alan adları görülmeli, ancak o zaman anlamlı
        bir dönüşüm katmanı eklemek güvenli olur.
        """
        params: dict[str, Any] = {}
        if il:
            params["il"] = _tr_normalize(il)
        data = self._get("alarmlar", params or None)
        return {
            "ham": data if data is not None else [],
            "not": (
                "MGM'nin şu anki (bu kodun yazıldığı sıradaki) yanıtı boştu "
                "çünkü aktif bir uyarı yoktu, alan adları bu yüzden ham "
                "olarak geçiliyor, doğrulanmış bir şema/dönüşüm henüz yok."
            ),
        }



    def il_istasyonlari(self, il: str) -> list[dict[str, Any]]:
        """
        Bir il adına göre MGM'nin döndürdüğü istasyon(lar)ı döndürür.

        Önemli sınır: MGM'nin `merkezler` uç noktası, yalnızca `il` verilip
        `ilce` verilmediğinde o ilin tüm ilçelerini değil, genelde tek bir
        varsayılan istasyonu döner (İstanbul için `il=istanbul` yalnız
        Bakırköy döner, oysa `il=istanbul&ilce=kadikoy` ayrı ve doğru bir
        sonuç döner). Yani bu yöntem "ilin tüm istasyonlarının listesi"
        değil, "ilin varsayılan istasyonu" olarak okunmalı. Belirli bir
        ilçeye ulaşmak için ilce_istasyonu(il, ilce) kullanın, o MGM'ye
        ilçeyi doğrudan parametre olarak gönderir (bkz. docs/resilience.md).
        """
        data = self._get("merkezler", {"il": _tr_normalize(il)})
        if not data:
            raise MGMWeatherError(f"'{il}' için istasyon bulunamadı.")
        return data

    def _il_ilce_istasyonlari(self, il: str, ilce: str) -> list[dict[str, Any]]:
        """MGM'ye hem `il` hem `ilce` parametresini birlikte gönderir.

        MGM'nin merkezler uç noktası bu iki parametre birlikte verildiğinde
        o ilçeye ait istasyonu doğrudan döner. il_istasyonlari()'nin
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
                    f"göremiyoruz. '{il}' için varsayılan istasyon: "
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
           sıklığı muhtemelen düşer. Bu da doğrulanmamış bir varsayım,
           bu yüzden env ile kapatılabilir/ayarlanabilir tutuldu.
        2. Saat başı sıcak/soğuk pencere: sıcak
           pencere içinde kısa TTL kullanılır ki yeni düşen ölçüm hızlı
           yakalansın, dışında TTL uzatılır

        `cache_ttl_seconds<=0` ya da `guncel_dinamik_ttl_aktif=False` ise
        devre dışı kalır, statik `cache_ttl_seconds` aynen döner.
        """
        if self.cache_ttl_seconds <= 0 or not self.guncel_dinamik_ttl_aktif:
            return self.cache_ttl_seconds
        try:
            simdi = _dt.datetime.now(ZoneInfo(self.guncel_zaman_dilimi))
        except (ZoneInfoNotFoundError, OSError) as exc:
            logger.warning(
                "Dinamik TTL için saat dilimi çözümlenemedi (%s), statik TTL kullanılıyor.",
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
            "yagis": kayit.get("yagis00Now"),
            "olcumZamani": kayit.get("veriZamani"),
        }

    def _open_meteo_guncel_durum(self, enlem: float, boylam: float) -> dict[str, Any]:
        """
        MGM'ye ulaşılamadığında (circuit breaker açık ya da MGM hata verdi)
        kullanılan yedek kaynak. Open-Meteo key gerektirmeyen, ücretsiz bir
        hava durumu API'sidir (bkz. https://open-meteo.com). Alan adları
        MGM'ninkiyle birebir aynı tutulur ki tüketici tarafında ayrı bir
        dallanma gerekmesin. `guncel_durum_yedekli` çağrısı yanıta hangi
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
                "wind_direction_10m,surface_pressure,pressure_msl,weather_code,"
                "precipitation"
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
                "yagis": veri.get("precipitation"),
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
        guncel_durum()'u dener. MGM hata verirse (circuit breaker açık dahil)
        ve enlem/boylam biliniyorsa Open-Meteo'ya düşer. Döndürülen sözlükte
        her zaman bir `kaynak` alanı olur: "mgm" ya da "open-meteo", hangi
        servisten geldiği tüketici tarafında hep belli olsun diye.

        Not: il/ilçe → istasyon çözümlemesi (enlem/boylam'ın kendisi) de
        MGM'den geliyor. MGM'nin istasyon listesi ("merkezler") ile anlık
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
