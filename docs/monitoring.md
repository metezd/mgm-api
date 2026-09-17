Uygulama zaten Prometheus formatında metrik üretiyor (`GET /metrics`). Bu
belge, bu metrikleri toplayıp görselleştirmek için hazır bir
Prometheus + Grafana örneğini anlatır. Docker/test/CI detayları için
[development.md](development.md)'ye, dayanıklılık (cache/circuit breaker)
detayları için [resilience.md](resilience.md)'ye bakın.

## Hızlı Başlangıç

İzleme servisleri `docker-compose.yml`'de `monitoring` profili altında
tanımlıdır, yani normal `docker compose up` onları başlatmaz.
Etkinleştirmek için:

```bash
docker compose --profile monitoring up -d
```

Bu komut mevcut `redis` + `app` servislerinin yanına iki servis daha ekler:

* **Prometheus** → `http://localhost:9090` — `app:5000/metrics`'i 15
  saniyede bir çeker (`monitoring/prometheus.yml`).
* **Grafana** → `http://localhost:3000` — varsayılan giriş `admin` /
  `admin` (bkz. aşağıdaki ortam değişkenleri). Prometheus veri kaynağı ve
  "mgm-api" dashboard'u ilk açılışta otomatik olarak yüklenir
  (`monitoring/grafana/provisioning/`), elle ayar gerekmez.

Sadece Prometheus/Grafana'yı durdurmak için:

```bash
docker compose --profile monitoring down
```

## Ortam Değişkenleri

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `PROMETHEUS_PORT` | `9090` | Prometheus'un dışa açılan portu |
| `GRAFANA_PORT` | `3000` | Grafana'nın dışa açılan portu |
| `GRAFANA_ADMIN_USER` | `admin` | Grafana ilk yönetici kullanıcısı |
| `GRAFANA_ADMIN_PASSWORD` | `admin` | Grafana ilk yönetici parolası — üretimde mutlaka değiştirin |

Bunlar `.env` dosyasına eklenebilir (`.env.example`'a bakınız).

## Hazır Dashboard

`monitoring/grafana/dashboards/mgm-api.json`, `app.py`'de tanımlı gerçek
metriklerden türetilmiş şu panelleri içerir:

* İstek hızı (endpoint başına, `http_requests_total`)
* Hata oranı (5xx, `http_requests_total{status=~"5.."}`)
* p95 gecikme (`http_request_duration_seconds`)
* Cache isabet oranı ve hit/stale_hit/miss dağılımı (`mgm_cache_result_total`)
* Circuit breaker durumu (`mgm_circuit_breaker_state`: Kapalı/Yarı-Açık/Açık)
* Rate limit ile reddedilen istekler (`mgm_rate_limit_rejected_total`)

Dashboard'u değiştirip Grafana arayüzünden kaydettiğinizde değişiklik
konteyner içinde kalır; kalıcı olarak saklamak isterseniz Grafana'daki
"Export" ile JSON'u tekrar `monitoring/grafana/dashboards/mgm-api.json`
dosyasına yazın.

## Render/üretimde kullanma

Bu compose dosyası yerel geliştirme/demo amaçlıdır. Render gibi bir
platformda uygulamayı çalıştırıp Prometheus'u ayrı bir yerde (kendi
sunucunuzda, Grafana Cloud, vb.) barındırıyorsanız sadece
`monitoring/prometheus.yml`'deki `targets` değerini uygulamanızın genel
URL'siyle güncelleyip (`targets: ["mgm-api.onrender.com:443"]` gibi,
`scheme: https` ekleyerek) kendi Prometheus kurulumunuza scrape config
olarak verebilirsiniz; uygulama tarafında ek bir değişiklik gerekmez.
