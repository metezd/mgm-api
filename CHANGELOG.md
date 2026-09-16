# Changelog

Bu proje [Semantik Sürümleme](https://semver.org/lang/tr/) kullanır.
`0.x` sürümlerde API henüz kararlı kabul edilmez, küçük sürümler
(0.1 → 0.2) arasında bile geriye dönük uyumsuz değişiklik olabilir.

## [Yayınlanmadı]

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
