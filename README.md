<h1 align="center">MGM Unofficial Weather API</h1>

<p align="center">
  <a href="https://github.com/metezd/hava-durumu/actions/workflows/main.yml">
    <img src="https://github.com/metezd/hava-durumu/actions/workflows/main.yml/badge.svg" alt="CI Status" />
  </a>
  <img src="https://img.shields.io/badge/python-3.13-blue.svg" alt="Python 3.13" />
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License" />
</p>

<p align="center">
  Resmi bir API değildir, veriyi <a href="https://www.mgm.gov.tr">mgm.gov.tr</a>'den çeker.
</p>

## Başlatma

```bash
docker compose up --build
```

Docker'sız çalıştırmak, Redis'siz kullanmak, Render ile deploy etmek için
[docs/development.md](docs/development.md)

## Kullanım

```bash
curl "http://127.0.0.1:5000/hava-durumu/Istanbul?ilce=Bakirkoy"
```

```json
{
  "basarili": true,
  "veri": {
    "il": "İSTANBUL",
    "ilce": "Bakırköy",
    "guncel": { "sicaklik": 29.4, "durum": "Açık" },
    "tahmin": [{ "tarih": "2026-08-14", "enDusuk": 23, "enYuksek": 31 }]
  }
}
```

Tüm endpoint'ler, parametreler ve şema: **`/docs`** (Swagger UI)

Yazım hatası/karmaşık girdi toleranslı arama için `/ara?q=...` de var —
`"kadikoy/istanbul"`, `"maslak itü"` gibi serbest metinleri çözer.

`/uyarilar?il=...` MGM'nin meteorolojik uyarı
verisini geçirir.

`/konum?lat=...&lon=...` GPS koordinatından önce MGM'yi
dener, çalışmazsa Open-Meteo denenir

`POST /toplu` tek istekte birden çok yer (`{"sorgular": ["istanbul",
"kadikoy/istanbul"]}`) `/ara` ile aynı akıllı çözümleyiciyi kullanır,
kısmi başarısızlığa toleranslıdır, paralel işlenir.

`GET /metrics` Prometheus formatında metrik. HTTP istek sayısı/süresi,
cache hit/miss/stale oranı, circuit breaker durumu.

Türkçe karakter problemi olursa Türkçe karakter kullanmayın. Örn: (`Istanbul`, `Bakirkoy`)

## Daha fazlası

- [docs/development.md](docs/development.md)
- [docs/resilience.md](docs/resilience.md)
