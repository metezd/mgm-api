from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --- girdi doğrulama/sanitization deseni -----------------------------
#
# Kullanıcıdan alınan yer adı metinlerini XSS ve
# enjeksiyon saldırılarına (<script>, ", ', ;, {}, ``, $() vb.) karşı
# "izin verilenler listesi" (allow-list) mantığıyla sınırlar: yalnızca
# burada açıkça izin verilen karakterler kabul edilir
#
# _YER_ADI_DESENI: il/ilçe gibi kesin yer adları için (örn. "Kadıköy",
# "Afşin-Elbistan"). Yalnızca harf (Türkçe karakterler dahil), tek
# boşluk, tire ve kesme işaretiyle ayrılmış harf öbekleri.
_YER_ADI_DESENI = r"^[A-Za-zÇĞİIıÖŞÜçğıöşü]+(?:[ '\-][A-Za-zÇĞİIıÖŞÜçğıöşü]+)*$"

# _ARAMA_DESENI: serbest metin arama (/ara?q=, toplu sorgu, favori
# sorgusu) için — "kadikoy, istanbul", "kadikoy/istanbul" gibi ayraçlı
# girdilere de izin verir. Harf, rakam, boşluk, virgül, '/', '-', '.'
# (ardışık ayraçlar da dahil, örn. ", ").
_ARAMA_DESENI = r"^[A-Za-z0-9ÇĞİIıÖŞÜçğıöşü]+(?:[ .,/\-]+[A-Za-z0-9ÇĞİIıÖŞÜçğıöşü]+)*$"


class KonumSorguModel(BaseModel):
    """`<il>` path parametresi + `ilce` query parametresi için sıkı
    doğrulama. Yalnızca harf/boşluk/tire/kesme işareti, 1-80 karakter;
    XSS/enjeksiyon için kullanılabilecek karakterlere (< > " ' ; { } |
    & ` $ ( ) [ ] \\ vb.) izin vermez."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    il: str | None = Field(default=None, min_length=1, max_length=80, pattern=_YER_ADI_DESENI)
    ilce: str | None = Field(default=None, min_length=1, max_length=80, pattern=_YER_ADI_DESENI)


class SerbestAramaModel(BaseModel):
    """Serbest metin arama parametresi (`/ara?q=` vb.) için sıkı
    doğrulama. Harf/rakam/boşluk/virgül/'/'/'-'/'.' , 1-150 karakter;
    aynı şekilde XSS/enjeksiyon karakterlerine izin vermez."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    sorgu: str = Field(min_length=1, max_length=150, pattern=_ARAMA_DESENI)


class FavoriGovdeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sorgu: str = Field(min_length=1, max_length=150, pattern=_ARAMA_DESENI)


class FavoriListeEkleModel(FavoriGovdeModel):
    listeId: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class ListeOlusturModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    listeId: str | None = Field(
        default=None,
        min_length=3,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class TopluGovdeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sorgular: list[str] = Field(min_length=1)

    @field_validator("sorgular")
    @classmethod
    def sorgular_gecerli(cls, value: list[str]) -> list[str]:
        for sorgu in value:
            if not sorgu.strip():
                raise ValueError("listedeki her sorgu boş olmayan bir metin olmalıdır")
            SerbestAramaModel(sorgu=sorgu)  # XSS/enjeksiyon deseni + uzunluk kontrolü
        return value


class AlertGovdeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tur: str
    il: str = Field(min_length=1, max_length=80, pattern=_YER_ADI_DESENI)
    ilce: str | None = Field(default=None, min_length=1, max_length=80, pattern=_YER_ADI_DESENI)
    webhookUrl: str = Field(min_length=1, max_length=2048)
    esik: float | str | None = None
    yon: str = "ustunde"

    @field_validator("tur")
    @classmethod
    def tur_gecerli(cls, value: str) -> str:
        if value not in {
            "weather.temp_threshold",
            "weather.wind_gust_exceeded",
            "weather.rain_threshold",
            "weather.rain_started",
            "weather.rain_stopped",
            "weather.frost_risk",
            "weather.warning_issued",
        }:
            raise ValueError("geçersiz alert türü")
        return value

    @field_validator("yon")
    @classmethod
    def yon_gecerli(cls, value: str) -> str:
        if value not in {"ustunde", "altinda"}:
            raise ValueError("'yon' ustunde veya altinda olmalıdır")
        return value


class WebhookPayloadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str
    eventId: str
    alertId: str
    il: str
    ilce: str | None = None
    esik: float | str | None = None
    olcum: dict
    tetiklenmeZamani: str
