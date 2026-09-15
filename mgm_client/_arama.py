from __future__ import annotations

import contextlib
import difflib
import re
from typing import Any

import requests

from ._constants import _tr_normalize
from ._errors import MGMWeatherError
from .iller import TURKIYE_ILLERI


class _AramaMixin:
    """Serbest metin/koordinat cozumleme, birlesik hava_durumu()."""

    def _il_yakin_eslesme(self, token: str) -> str | None:
        """
        Verilen kelimeyi 81 il listesindeki en yakın ile eşler (typo
        toleranslı). Önce tam normalize eşleşmeye bakar, olmazsa stdlib
        difflib ile yakın eşleşme dener. 81 sabit string üzerinde
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

        1. **Tam eşleşme.** Sorgunun tamamı 81 ilden biriyle (typo
           toleranslı) eşleşiyorsa, o ilin varsayılan istasyonu kullanılır.
           Ağ isteği yok.
        2. **Parçalama.** Sorgu '/', ',' ya da boşlukla ayrılmış
           parçalara bölünür. Parçalardan biri bilinen bir ile (typo
           toleranslı) yakınsa, geri kalan parça(lar) ilçe adayı olarak
           doğrudan MGM'ye sorulur (bkz. ilce_istasyonu, MGM'nin il+ilçe
           birlikte verildiğinde doğru sonucu döndüğü ayrıca doğrulanmış
           bir davranıştır). Tek bir MGM isteği.
        3. **Geocoding.** İlk iki katman sonuç vermezse, sorgu
           Open-Meteo'nun (key gerektirmeyen) geocoding servisine
           gönderilir. Dönen en iyi aday tekrar MGM'de (il+ilçe olarak)
           denenir. MGM'de de bulunamazsa (örn. "Maslak" resmi bir ilçe
           değil, bir mahalle) doğrudan o koordinatla Open-Meteo'dan hava
           durumu döndürülür.

        Dönüş sözlüğü her zaman bir `durum` alanı içerir:
        - `"cozuldu"`: `il`/`ilce` (ya da doğrudan `enlem`/`boylam`) ve
          `yontem` doludur.
        - `"belirsiz"`: geocoding, farklı illerde birden fazla makul aday
          döndürdü. `secenekler` doludur, tahmin yürütülmedi.
        - `"bulunamadi"`: hiçbir katman bir sonuç üretemedi.
        """
        sorgu = (sorgu or "").strip()
        if not sorgu:
            return {"durum": "bulunamadi", "sorgu": sorgu}

        # Katman 1: sorgunun tamamı doğrudan bir il mi?
        il = self._il_yakin_eslesme(sorgu)
        if il:
            return {"durum": "cozuldu", "yontem": "il-eslesme", "il": il, "ilce": None}

        # Katman 2: parçalama, bir parça il, kalan(lar) ilçe adayı
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
        with contextlib.suppress(Exception):
            sonuc["ayEvresi"] = self.ay_evresi()

        return sonuc

    def hava_durumu_akilli(self, sorgu: str) -> dict[str, Any]:
        """
        `/ara` uç noktasının üst düzey yardımcı fonksiyonu: akilli_yer_bul()
        ile serbest metin sorguyu çözüp hava durumunu döner.

        - "cozuldu" ise hava_durumu()'nun döndürdüğü sözlüğe `durum`,
          `sorgu`, `yontem` alanları eklenerek döner (MGM istasyonuna
          çözüldüyse tam hava_durumu() yanıtı, sadece koordinat çözüldüyse
          (örn. "Maslak" gibi resmi ilçe olmayan bir yer) Open-Meteo'dan
          yalnızca güncel durum, tahmin boş liste).
        - "belirsiz" ise hava durumu getirmeden seçenek listesini döner.
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
        kılıyor. Bu proje ölçeğinde sorun değil, yüksek trafikli bir deploy'da kendi Nominatim
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


