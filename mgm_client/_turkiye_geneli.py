from __future__ import annotations

from typing import Any

from .iller import TURKIYE_ILLERI


class _TurkiyeGeneliMixin:
    """En dusuk/yuksek sicaklik, toplam yagis, kar, son gozlemler."""

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

    def toplam_yagislar(self, tarih: str | None = None) -> dict[str, Any]:
        # deger burada mm yağış, ayrı tarih servisi (yagisMaxTarih)
        return self._sondurum_sicaklik(
            "sondurumlar/toplamyagis", "sondurumlar/yagisMaxTarih", tarih
        )

    def kar_kalinliklari(self) -> dict[str, Any]:
        # anlık, tarih parametresi yok. deger alanı yerine karYukseklik (cm)
        veri = self._cached_get(
            self._cache_key("sondurum-kar"),
            lambda: self._get("sondurumlar/kar"),
            ttl_override=self.sondurum_ttl_saniye,
        )
        return {"kayitlar": veri}

    def son_gozlemler(self) -> dict[str, Any]:
        # sondurumlar/ilmerkezleri sadece istNo döner. il adı için
        # merkezler/iller ile eşleştirip TURKIYE_ILLERI plaka->il çözülür.
        # merkezler/iller'in alan adları (sondurumIstNo, ilPlaka) JS
        # kaynağından çıkarım, gerçek bir yanıt örneği görülmedi. Bu
        # yüzden eşleştirme oranı düşükse (alan adları değişmiş/yanlış
        # tahmin edilmiş olabilir) sessizce geçmek yerine yanıta bir
        # uyarı ekleniyor.
        iller = self._cached_get(
            self._cache_key("merkezler-iller"),
            lambda: self._get("merkezler/iller"),
            ttl_override=self.sondurum_ttl_saniye,
        )
        istno_plaka = {
            i.get("sondurumIstNo"): i.get("ilPlaka")
            for i in iller
            if i.get("sondurumIstNo")
        }
        plaka_il = {k["plakaKodu"]: k["il"] for k in TURKIYE_ILLERI}
        veri = self._cached_get(
            self._cache_key("sondurum-ilmerkezleri"),
            lambda: self._get("sondurumlar/ilmerkezleri"),
            ttl_override=self.sondurum_ttl_saniye,
        )
        kayitlar = []
        for k in veri:
            plaka = istno_plaka.get(k.get("istNo"))
            kayitlar.append({**k, "il": plaka_il.get(plaka)})

        sonuc: dict[str, Any] = {"kayitlar": kayitlar}
        cozulen = sum(1 for k in kayitlar if k.get("il"))
        if kayitlar and cozulen / len(kayitlar) < 0.5:
            sonuc["_uyari"] = (
                f"İl adı eşleştirmesi düşük oranda başarılı oldu "
                f"({cozulen}/{len(kayitlar)}). merkezler/iller uç noktasının "
                "alan adları (sondurumIstNo, ilPlaka) değişmiş/yanlış "
                "tahmin edilmiş olabilir, doğrulanmalı."
            )
        return sonuc

