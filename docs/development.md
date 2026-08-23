Hızlı başlangıç için [README](../README.md)'ye bakın. Burada Docker'ın tüm
seçenekleri, test/lint/CI ve bağımlılık yönetimi detayları var.

## Docker Uygulaması

Uygulamayı tüm bağımlılıklarıyla birlikte ayağa kaldırmak için aşağıdaki komutları kullanabilirsiniz:

```bash
cp .env.example .env   # İsteğe bağlı olarak değişkenler düzenlenebilir
docker compose up --build
```

`MGM_REDIS_URL` otomatik olarak bundled Redis'e yönlendirilir. `.env` yoksa da
çalışır, varsayılanlarla devam eder.

Sadece uygulamayı (Redis'siz, in-memory cache ile) çalıştırmak için:

```bash
docker build -t mgm-api .
docker run -p 5000:5000 mgm-api
```

> **Not:** Docker imajı, sistemin durumunu izlemek üzere `/health` uç noktasını kullanan yapılandırılmış bir `HEALTHCHECK` barındırır. Konteynerin güncel sağlığı `docker ps` çıktısında `healthy` veya `unhealthy` olarak gözlemlenebilir.

## Test ve Lint

```bash
python -m unittest discover -s tests -v
pip install ruff && ruff check .
```

İkisi de CI'da her push/PR'da otomatik çalışır (`.github/workflows/main.yml`).

## Bağımlılık Yönetimi

Projedeki Python bağımlılıkları iki aşamalı bir yapıyla yönetilmektedir: 
* `requirements.txt` dosyası esnek sürüm aralıklarını (`>=`) barındırır.
* `requirements-lock.txt` dosyası ise bu gereksinimlerden üretilmiş, kesin olarak sabitlenmiş (*pinned*) sürümleri içerir. 

Sürekli entegrasyon (CI) süreçleri ve Docker build adımları, tekrarlanabilir yapılar elde etmek amacıyla her zaman `lock` dosyasını baz alır. Bağımlılıkların güncellenmesine dair talimatlar ilgili dosyanın üst kısmında yorum satırı olarak yer almaktadır.

Ayrıca projede yer alan Dependabot yapılandırması (`.github/dependabot.yml`), pip, Docker ve GitHub Actions bağımlılıklarını haftalık olarak tarar ve gerekli güncellemeler için otomatik PR oluşturur.

## Rate Limit

Uygulama, IP tabanlı kayan pencere (*sliding-window*) algoritması kullanarak istekleri sınırlandırmaktadır. Bu sınırlandırmalar `APP_RATE_LIMIT_WINDOW_SECONDS` ve `APP_RATE_LIMIT_MAX_REQUESTS` ortam değişkenleri ile yapılandırılabilir.

API tarafından döndürülen her HTTP yanıtı, istemciyi bilgilendirmek amacıyla aşağıdaki başlıkları (*header*) içerir:
* `X-RateLimit-Limit`: İzin verilen maksimum istek sayısı.
* `X-RateLimit-Remaining`: Kalan istek hakkı.
* `X-RateLimit-Reset`: Kayan pencere algoritması kullanıldığı için tek bir sabit sıfırlanma anı bulunmamaktadır. En eski isteğin zaman aşımına uğrayıp istemcinin en az bir yeni istek hakkı daha kazanacağı UNIX timestampini işaret eder.

> **İstisnalar:** `/health`, `/docs` ve `/openapi.yaml` uç noktaları bu sınırlandırmalardan muaftır

**Toplu İstek (Batch) Koruması:**
İstek sınırlandırma mekanizması, paket içerisindeki öge sayısını değil, toplam HTTP isteği sayısını baz alır. Bu nedenle, tek bir istek içerisine çok sayıda sorgu paketlenerek limitlerin fiilen aşılmasını (bypass) engellemek amacıyla `POST /toplu` uç noktasına yapılan talepler `APP_TOPLU_MAX_SORGU` (varsayılan: `20`) ortam değişkeni ile ayrıca sınırlandırılmıştır.

## Response Compression

İstemci tarafından HTTP isteklerinde `Accept-Encoding: gzip` başlığı iletildiğinde; JSON, HTML ve YAML formatındaki yanıtlar `Flask-Compress` eklentisi kullanılarak otomatik olarak sıkıştırılır. Bu optimizasyon standart olarak aktiftir ve ek bir yapılandırma gerektirmez. `br` (Brotli) da desteklenir `Accept-Encoding` içinde `br` geçen istemciler otomatik brotli alır

## Gözlemlenebilirlik (Prometheus metrikleri)

`GET /metrics`, Prometheus text formatında metrik döner. Rate limitten muaf, `/health` gibi.

- `http_requests_total{method,endpoint,status}` — endpoint bazlı istek sayacı. Etiket olarak ham path değil Flask'ın eşleştirdiği route adı (`request.endpoint`, ör. `guncel`) kullanılır `/guncel/<il>` gibi path'lerde `il` değerini etikete koymak sınırsız kardinaliteye (her farklı il için ayrı zaman serisi) yol açardı.
- `http_request_duration_seconds{method,endpoint}` histogram, aynı etiketleme mantığıyla.
- `mgm_cache_result_total{sonuc}` `hit`/`stale_hit`/`miss` sayaçları (`mgm_client.py`, `_cached_get()` içinde artırılır).
- `mgm_circuit_breaker_state` 0=kapalı, 1=yarı-açık, 2=açık; her scrape'te `mgm.circuit_breaker_saglik_ozeti()`'nden okunur.
- `mgm_rate_limit_rejected_total` 429 ile reddedilen istek sayısı.

## Deploy (Render)

Repo'daki `render.yaml` Blueprint'i, web servisini ve Redis uyumlu bir Key
Value servisini tek seferde kurar. Render Dashboard'da **New +** → **Blueprint** → bu repoyu seçin.