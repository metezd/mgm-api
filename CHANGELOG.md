# Changelog

Bu proje [Semantik Sürümleme](https://semver.org/lang/tr/) kullanır.
`0.x` sürümlerde API henüz kararlı kabul edilmez, küçük sürümler
(0.1 → 0.2) arasında bile geriye dönük uyumsuz değişiklik olabilir.

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
