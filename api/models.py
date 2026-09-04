from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FavoriGovdeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sorgu: str = Field(min_length=1, max_length=100)


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
    def sorgular_bos_olmamalı(cls, value: list[str]) -> list[str]:
        if any(not sorgu.strip() for sorgu in value):
            raise ValueError("listedeki her sorgu boş olmayan bir metin olmalıdır")
        return value


class AlertGovdeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tur: str
    il: str = Field(min_length=1, max_length=100)
    ilce: str | None = Field(default=None, max_length=100)
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
