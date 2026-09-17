# Changelog

Bu proje [Semantik Sürümleme](https://semver.org/lang/tr/) kullanır.
`0.x` sürümlerde API henüz kararlı kabul edilmez, küçük sürümler
(0.1 → 0.2) arasında bile geriye dönük uyumsuz değişiklik olabilir.

## [Yayınlanmadı]

- ETag / 304 Not Modified desteği eklendi: başarılı (200), akışsız
  `GET` yanıtlarına `ETag` (Werkzeug `add_etag()`/`make_conditional()`)
  ve `Cache-Control: no-cache` header'ları eklenir; istemci aynı
  `ETag`'i `If-None-Match` ile gönderirse gövde tekrar üretilip
  gönderilmeden `304 Not Modified` döner. `/health` ve `/metrics`
  kapsam dışı. Prometheus metrikleri de 304'ü doğru sayar (hook,
  `metrik_kaydet`'ten önce çalışacak şekilde sıralandı). 8 yeni test
  (`TestEtagConditionalGet`). Detaylar: `docs/resilience.md`.

- Girdi doğrulama/sanitization: `<il>` path parametresi, `ilce`/`il`/`q`
  query parametreleri artık route'a girmeden Pydantic v2 katmanında
  sıkı regex + uzunluk validasyonundan geçiyor (`KonumSorguModel`,
  `SerbestAramaModel`). Yer adları yalnızca harf/boşluk/tire/kesme
  işareti (1-80 karakter), serbest arama harf/rakam/boşluk/virgül/`/`/
  `-`/`.` (1-150 karakter) — XSS/enjeksiyon karakterlerine (`< > " ' ;
  { } | & `` $` vb.) izin verilmiyor, geçersiz girdi 400 ile reddedilir.
  Aynı desenler mevcut POST gövdesi modellerine de uygulandı
  (`AlertGovdeModel.il/ilce`, `FavoriGovdeModel.sorgu`,
  `TopluGovdeModel.sorgular`). 14 yeni test eklendi
  (`TestKonumVeAramaDogrulama`).

- Prometheus + Grafana izleme örneği eklendi: `docker-compose.yml`'e
  `monitoring` profili altında (varsayılan `docker compose up`'ı
  etkilemez) `prometheus` ve `grafana` servisleri eklendi.
  `monitoring/prometheus.yml` `/metrics`'i scrape eder; Grafana veri
  kaynağı ve hazır "mgm-api" dashboard'u (istek hızı, 5xx oranı, p95
  gecikme, cache isabet oranı, circuit breaker durumu, rate-limit
  reddi) `monitoring/grafana/provisioning/` ile otomatik yüklenir.
  Detaylar: `docs/monitoring.md`. Etkinleştirmek için:
  `docker compose --profile monitoring up -d`.

- Dockerfile `python:3.14-slim`'den `python:3.13-slim`'e geri alındı.
  Dependabot'un Docker imaj bump'ı CI'da (main.yml yalnızca 3.13 ile
  lint/test çalıştırıyor, Docker build etmiyor) yakalanamayan bir hataya
  yol açtı. `.github/dependabot.yml`'a Python imajı için minör/majör
  sürüm güncellemelerini durduran bir `ignore` kuralı eklendi; patch/
  digest (güvenlik) güncellemeleri hâlâ otomatik önerilir. 3.14'e geçiş
  manuel doğrulamadan sonra tekrar denenmeli.

- Akıllı Özetleme (NLP) eklendi (`akilli_ozet()` / `GET /akilli-ozet/<il>`):
  güncel durum ve 5 günlük tahmini kural tabanlı (rule-based) doğal dil
  üretimiyle tek bir Türkçe özet paragrafına, öne çıkan noktalara
  (`anahtarNoktalar`), uyarılara (`uyarilar`: sıcak/soğuk/rüzgar eşiği
  aşımı) ve basit bir sıcaklık trendine (`trend`) çevirir. Bir dil
  modeli/ML kullanmaz; MGM'nin resmi bir metin ürünü değildir.

## [0.1.0] - İlk PyPI sürümü

`mgm_client.py`, `mgm-tr` adıyla bağımsız bir Python paketi olarak
PyPI'a yayınlandı. HTTP sunucu (`app.py`/Flask) olmadan, sadece
`requests` + `tzdata` ile çalışır.

İçerdiği başlıca özellikler (hepsi bu sürümden önce, `app.py` REST
API'sinin arka planında geliştirildi):

- Güncel durum, günlük/saatlik tahmin, akıllı il/ilçe arama
- Hava kalitesi, polen indeksi, deniz suyu sıcaklığı/dalga durumu
- Gün doğumu/batımı, ay evresi, tarımsal don/kırağı riski
- Türkiye geneli son durum uç noktaları
- Circuit breaker, Redis destekli cache/stale-while-revalidate

Not: Kullanılan uç noktaların büyük kısmı MGM'nin resmi
dokümante edilmiş bir API'si değil, mgm.gov.tr'nin kendi iç
servisleridir. MGM bunları değiştirirse istemcinin güncellenmesi
gerekir.
