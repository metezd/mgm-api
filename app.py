"""
app.py
------
mgm_client.MGMWeather sınıfını basit bir REST API olarak dışarı açan
Flask uygulaması.

Çalıştırma:
    pip install -r requirements.txt
    python app.py
    # varsayılan olarak http://127.0.0.1:5000 üzerinde çalışır

Uç noktalar:
    GET /istasyonlar/<il>
        -> İldeki tüm istasyonları listeler

    GET /guncel/<il>?ilce=<ilce>
        -> Anlık hava durumu

    GET /tahmin/<il>?ilce=<ilce>
        -> 5 günlük tahmin

    GET /hava-durumu/<il>?ilce=<ilce>
        -> Güncel durum + tahmin + gün doğumu ve batımı

    GET /hava-kalitesi/<il>?ilce=<ilce>
        -> Anlık UV indeksi ve hava kalitesi (PM10, PM2.5, NO2).
           İstanbul için öncelik İBB'nin resmi ölçüm ağındadır,
           PM2.5/UV ve İBB'nin kapsamadığı yerler Open-Meteo'dan gelir.

    GET /gun-ay-bilgisi/<il>?ilce=<ilce>
        -> Verilen il/ilçe (koordinat) için gün doğumu, gün batımı
           (sunrise-sunset.org) ve ay evresi. /hava-durumu yanıtına da
           "ayEvresi" alanı olarak otomatik eklenir.

Örnek:
    curl "http://127.0.0.1:5000/hava-durumu/Istanbul?ilce=Bakirkoy"
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, Response, g, jsonify, request
from flask_compress import Compress
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from mgm_client import MGMWeather, MGMWeatherError, turkiye_illeri

app = Flask(__name__)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# gzip/br response compression.
app.config["COMPRESS_MIMETYPES"] = [
    "application/json",
    "application/yaml",
    "text/html",
]
Compress(app)
mgm = MGMWeather(
    timeout=int(os.getenv("MGM_TIMEOUT", "10")),
    retry_total=int(os.getenv("MGM_RETRY_TOTAL", "3")),
    retry_backoff=float(os.getenv("MGM_RETRY_BACKOFF", "0.3")),
    cache_ttl_seconds=int(os.getenv("MGM_CACHE_TTL", "60")),
    cache_max_entries=int(os.getenv("MGM_CACHE_MAX_ENTRIES", "512")),
    stale_while_revalidate_seconds=int(
        os.getenv("MGM_STALE_WHILE_REVALIDATE", "300")
    ),
    circuit_breaker_failure_threshold=int(
        os.getenv("MGM_CIRCUIT_BREAKER_FAILURE_THRESHOLD", "5")
    ),
    circuit_breaker_window_seconds=float(
        os.getenv("MGM_CIRCUIT_BREAKER_WINDOW_SECONDS", "30")
    ),
    circuit_breaker_open_seconds=float(
        os.getenv("MGM_CIRCUIT_BREAKER_OPEN_SECONDS", "60")
    ),
    guncel_dinamik_ttl_aktif=os.getenv("MGM_GUNCEL_DINAMIK_TTL", "1") not in {"0", "false", "False"},
    guncel_sicak_pencere_baslangic_dk=int(
        os.getenv("MGM_GUNCEL_SICAK_PENCERE_BASLANGIC_DK", "5")
    ),
    guncel_sicak_pencere_bitis_dk=int(
        os.getenv("MGM_GUNCEL_SICAK_PENCERE_BITIS_DK", "15")
    ),
    guncel_sicak_ttl_saniye=int(os.getenv("MGM_GUNCEL_SICAK_TTL_SANIYE", "120")),
    guncel_soguk_ttl_saniye=int(os.getenv("MGM_GUNCEL_SOGUK_TTL_SANIYE", "1800")),
    guncel_gece_baslangic_saat=int(os.getenv("MGM_GUNCEL_GECE_BASLANGIC_SAAT", "0")),
    guncel_gece_bitis_saat=int(os.getenv("MGM_GUNCEL_GECE_BITIS_SAAT", "6")),
    guncel_gece_ttl_saniye=int(os.getenv("MGM_GUNCEL_GECE_TTL_SANIYE", "3600")),
    tahmin_ttl_saniye=int(os.getenv("MGM_TAHMIN_TTL_SANIYE", "10800")),
    hava_kalitesi_ttl_saniye=int(os.getenv("MGM_HAVA_KALITESI_TTL_SANIYE", "600")),
    ibb_istasyon_ttl_saniye=int(os.getenv("MGM_IBB_ISTASYON_TTL_SANIYE", "21600")),
    ibb_max_mesafe_km=float(os.getenv("MGM_IBB_MAX_MESAFE_KM", "40")),
    redis_url=os.getenv("REDIS_URL") or os.getenv("MGM_REDIS_URL") or None,
    redis_prefix=os.getenv("MGM_REDIS_PREFIX", "mgm-cache:"),
)
CORS_ALLOW_ORIGIN = os.getenv("APP_CORS_ALLOW_ORIGIN", "*")
RATE_LIMIT_WINDOW = int(os.getenv("APP_RATE_LIMIT_WINDOW_SECONDS", "60"))
RATE_LIMIT_MAX = int(os.getenv("APP_RATE_LIMIT_MAX_REQUESTS", "60"))
TOPLU_MAX_SORGU = int(os.getenv("APP_TOPLU_MAX_SORGU", "20"))

HTTP_ISTEK_SAYAC = Counter(
    "http_requests_total", "Toplam HTTP isteği", ["method", "endpoint", "status"]
)
HTTP_ISTEK_SURESI = Histogram(
    "http_request_duration_seconds", "İstek süresi (saniye)", ["method", "endpoint"]
)
RATE_LIMIT_RED_SAYAC = Counter(
    "mgm_rate_limit_rejected_total", "429 ile reddedilen istek sayısı"
)
CIRCUIT_BREAKER_DURUM_GAUGE = Gauge(
    "mgm_circuit_breaker_state", "0=kapali 1=yari-acik 2=acik (scrape anında okunur)"
)
RATE_LIMIT_BUCKETS = defaultdict(deque)


def _hata_yanit(exc: Exception, kod: int = 502):
    return jsonify({"basarili": False, "hata": str(exc)}), kod


def _istasyon_id_getir(il: str, ilce: str | None) -> int | str:
    istasyon = mgm.ilce_istasyonu(il, ilce)
    istasyon_id = istasyon.get("istasyonId") or istasyon.get("merkezId")
    if istasyon_id is None:
        raise MGMWeatherError(f"'{il}' için geçerli istasyon kimliği bulunamadı.")
    return istasyon_id


def _istasyon_ve_konum_getir(
    il: str, ilce: str | None
) -> tuple[int | str, float | None, float | None]:
    """
    _istasyon_id_getir'e ek olarak enlem/boylam da döner
    fallback'i (guncel_durum_yedekli) için gerekli.
    """
    istasyon = mgm.ilce_istasyonu(il, ilce)
    istasyon_id = istasyon.get("istasyonId") or istasyon.get("merkezId")
    if istasyon_id is None:
        raise MGMWeatherError(f"'{il}' için geçerli istasyon kimliği bulunamadı.")
    enlem = istasyon.get("enlem") or istasyon.get("lat")
    boylam = istasyon.get("boylam") or istasyon.get("lon")
    return istasyon_id, enlem, boylam


@app.before_request
def istek_zamanlayici():
    g.metrik_baslangic = time.monotonic()


@app.before_request
def rate_limit():
    if request.method == "OPTIONS":
        return None
    if request.path in {"/health", "/docs", "/openapi.yaml", "/metrics"}:
        return None

    ip = request.remote_addr or "unknown"
    now = time.monotonic()
    bucket = RATE_LIMIT_BUCKETS[ip]
    window_seconds = max(1, RATE_LIMIT_WINDOW)
    limit = max(1, RATE_LIMIT_MAX)
    while bucket and now - bucket[0] > window_seconds:
        bucket.popleft()

    if bucket:
        kalan_sure = max(0.0, window_seconds - (now - bucket[0]))
    else:
        kalan_sure = 0.0
    g.rl_limit = limit
    g.rl_reset_epoch = int(time.time() + kalan_sure)

    if len(bucket) >= limit:
        g.rl_remaining = 0
        RATE_LIMIT_RED_SAYAC.inc()
        retry_after = max(1, window_seconds)
        response = jsonify({
            "basarili": False,
            "hata": "Çok fazla istek gönderdiniz. Lütfen birkaç saniye sonra tekrar deneyin.",
        })
        response.status_code = 429
        response.headers["Retry-After"] = str(retry_after)
        return response
    bucket.append(now)
    g.rl_remaining = max(0, limit - len(bucket))


@app.after_request
def guvenlik_ve_cors_headerlari(response):
    response.headers["Access-Control-Allow-Origin"] = CORS_ALLOW_ORIGIN
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none';"

    if hasattr(g, "rl_limit"):
        response.headers["X-RateLimit-Limit"] = str(g.rl_limit)
        response.headers["X-RateLimit-Remaining"] = str(g.rl_remaining)
        response.headers["X-RateLimit-Reset"] = str(g.rl_reset_epoch)
    return response


@app.after_request
def metrik_kaydet(response):
    endpoint = request.endpoint or "eslesmedi"  # 404 gibi durumlarda route yok
    HTTP_ISTEK_SAYAC.labels(
        method=request.method, endpoint=endpoint, status=response.status_code
    ).inc()
    baslangic = getattr(g, "metrik_baslangic", None)
    if baslangic is not None:
        HTTP_ISTEK_SURESI.labels(method=request.method, endpoint=endpoint).observe(
            time.monotonic() - baslangic
        )
    return response


@app.get("/metrics")
def metrics():
    CIRCUIT_BREAKER_DURUM_GAUGE.set(
        {"kapali": 0, "yari-acik": 1, "acik": 2}.get(
            mgm.circuit_breaker_saglik_ozeti()["durum"], -1
        )
    )
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.get("/openapi.yaml")
def openapi_spec():
    with open(os.path.join(_BASE_DIR, "openapi.yaml"), encoding="utf-8") as f:
        return Response(f.read(), mimetype="application/yaml")


@app.get("/docs")
def docs():
    # Swagger UI'ı CDN'den yükleyen minimal bir HTML sayfası. Yeni bir
    # Python bağımlılığı (flask-smorest, apispec vb.) eklemeden, elle
    # yazılmış openapi.yaml'i görselleştirir.
    html = """<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="utf-8" />
  <title>Hava Durumu API — Dokümantasyon</title>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/5.17.14/swagger-ui.min.css" />
  <style>body { margin: 0; }</style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/5.17.14/swagger-ui-bundle.min.js"></script>
  <script>
    window.onload = () => {
      window.ui = SwaggerUIBundle({
        url: "/openapi.yaml",
        dom_id: "#swagger-ui",
        presets: [SwaggerUIBundle.presets.apis],
      });
    };
  </script>
</body>
</html>"""
    return Response(html, mimetype="text/html")


@app.get("/iller")
def iller():
    return jsonify({"basarili": True, "veri": turkiye_illeri()})


@app.get("/ara")
def ara():
    sorgu = request.args.get("q", "").strip()
    if not sorgu:
        return jsonify({"basarili": False, "hata": "'q' parametresi zorunludur."}), 400
    try:
        veri = mgm.hava_durumu_akilli(sorgu)
        return jsonify({"basarili": True, "veri": veri})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 404)


@app.get("/uyarilar")
def uyarilar():
    il = request.args.get("il")
    try:
        return jsonify({"basarili": True, "veri": mgm.uyarilar(il)})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 502)


@app.get("/konum")
def konum():
    lat_str = request.args.get("lat")
    lon_str = request.args.get("lon")
    if not lat_str or not lon_str:
        return jsonify(
            {"basarili": False, "hata": "'lat' ve 'lon' parametreleri zorunludur."}
        ), 400
    try:
        enlem = float(lat_str)
        boylam = float(lon_str)
    except ValueError:
        return jsonify(
            {"basarili": False, "hata": "'lat' ve 'lon' geçerli birer sayı olmalıdır."}
        ), 400
    if not (-90 <= enlem <= 90) or not (-180 <= boylam <= 180):
        return jsonify(
            {"basarili": False, "hata": "'lat' -90..90, 'lon' -180..180 aralığında olmalıdır."}
        ), 400
    try:
        veri = mgm.hava_durumu_konum(enlem, boylam)
        return jsonify({"basarili": True, "veri": veri})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 502)


@app.post("/toplu")
def toplu():
    """
    Tek istekte birden çok yer için hava durumu — her sorgu, /ara ile
    aynı akıllı çözümleyiciyi kullanır, yani "istanbul", "kadikoy/istanbul", 
    "maslak itü" gibi serbest metinler burada da geçerlidir.

    Kısmi başarısızlığa toleranslıdır: bir sorgu çözülemese bile diğerleri
    etkilenmez — yanıt her zaman 200 döner (batch'in kendisi işlendi),
    her öğe kendi basarili/hata durumunu taşır. Sonuç dizisi, istek
    sırasıyla birebir aynı sırada döner

    Batch boyutu TOPLU_MAX_SORGU ile sınırlıdır: aksi halde tek bir
    isteğe çok sayıda sorgu sıkıştırmak, rate limit'i fiilen bypass
    etmenin bir yolu olurdu
    """
    if not request.is_json:
        return jsonify(
            {"basarili": False, "hata": "İstek gövdesi JSON olmalıdır (Content-Type: application/json)."}
        ), 400
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "sorgular" not in body:
        return jsonify(
            {"basarili": False, "hata": "'sorgular' alanı zorunludur (bir dizi metin sorgusu)."}
        ), 400

    sorgular = body["sorgular"]
    if not isinstance(sorgular, list) or not sorgular:
        return jsonify(
            {"basarili": False, "hata": "'sorgular' boş olmayan bir dizi olmalıdır."}
        ), 400
    if len(sorgular) > TOPLU_MAX_SORGU:
        return jsonify(
            {
                "basarili": False,
                "hata": f"En fazla {TOPLU_MAX_SORGU} sorgu gönderebilirsiniz "
                f"(gönderilen: {len(sorgular)}).",
            }
        ), 400
    if not all(isinstance(s, str) and s.strip() for s in sorgular):
        return jsonify(
            {"basarili": False, "hata": "'sorgular' listesindeki her öğe boş olmayan bir metin olmalıdır."}
        ), 400

    def _tek_sorgu(sorgu: str) -> dict:
        sorgu = sorgu.strip()
        try:
            veri = mgm.hava_durumu_akilli(sorgu)
            return {"sorgu": sorgu, "basarili": True, "veri": veri}
        except MGMWeatherError as exc:
            return {"sorgu": sorgu, "basarili": False, "hata": str(exc)}

    # MGMWeather client thread safe yapıdadır
    # Paralel yürütme sayesinde N adet sorgunun toplam gecikmesi, 
    # sıralı toplam yerine en yavaş tekil sorgu süresine indirgenir.
    with ThreadPoolExecutor(max_workers=min(len(sorgular), 10)) as havuz:
        sonuclar = list(havuz.map(_tek_sorgu, sorgular))

    return jsonify({"basarili": True, "veri": sonuclar})


@app.get("/istasyonlar/<il>")
def istasyonlar(il: str):
    try:
        return jsonify({"basarili": True, "veri": mgm.il_istasyonlari(il)})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 404)


@app.get("/health")
def health():
    devre_durumu = mgm.circuit_breaker_saglik_ozeti()["durum"]

    deep = request.args.get("deep", "").strip().lower() in {"1", "true", "yes", "on"}
    if not deep:
        return jsonify(
            {
                "basarili": True,
                "durum": "ok",
                "servis": "hava-durumu",
                "mgm": "skip",
                "redis": "skip",
                "circuit_breaker": devre_durumu,
            }
        )

    redis_durum = mgm.redis_saglik_ozeti()
    if redis_durum["durum"] == "hata":
        return (
            jsonify(
                {
                    "basarili": False,
                    "durum": "degraded",
                    "servis": "hava-durumu",
                    "mgm": "skip",
                    "redis": "hata",
                    "circuit_breaker": devre_durumu,
                    "hata": redis_durum["hata"],
                }
            ),
            503,
        )

    try:
        mgm.il_istasyonlari("Ankara")
        return jsonify(
            {
                "basarili": True,
                "durum": "ok",
                "servis": "hava-durumu",
                "mgm": "ok",
                "redis": redis_durum["durum"],
                "circuit_breaker": mgm.circuit_breaker_saglik_ozeti()["durum"],
            }
        )
    except MGMWeatherError as exc:
        return (
            jsonify(
                {
                    "basarili": False,
                    "durum": "degraded",
                    "servis": "hava-durumu",
                    "mgm": "hata",
                    "redis": redis_durum["durum"],
                    "circuit_breaker": mgm.circuit_breaker_saglik_ozeti()["durum"],
                    "hata": str(exc),
                }
            ),
            503,
        )


@app.get("/guncel/<il>")
def guncel(il: str):
    ilce = request.args.get("ilce")
    try:
        istasyon_id, enlem, boylam = _istasyon_ve_konum_getir(il, ilce)
        veri = mgm.guncel_durum_yedekli(istasyon_id, enlem, boylam)
        return jsonify({"basarili": True, "veri": veri})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 404)


@app.get("/tahmin/<il>")
def tahmin(il: str):
    ilce = request.args.get("ilce")
    try:
        istasyon_id = _istasyon_id_getir(il, ilce)
        veri = mgm.gunluk_tahmin(istasyon_id)
        return jsonify({"basarili": True, "veri": veri})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 404)


@app.get("/saatlik/<il>")
def saatlik(il: str):
    ilce = request.args.get("ilce")
    try:
        istasyon_id = _istasyon_id_getir(il, ilce)
        veri = mgm.saatlik_tahmin(istasyon_id)
        return jsonify({"basarili": True, "veri": veri})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 404)


@app.get("/hava-durumu/<il>")
def hava_durumu(il: str):
    ilce = request.args.get("ilce")
    try:
        veri = mgm.hava_durumu(il, ilce)
        return jsonify({"basarili": True, "veri": veri})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 404)


@app.get("/hava-kalitesi/<il>")
def hava_kalitesi(il: str):
    ilce = request.args.get("ilce")
    try:
        _, enlem, boylam = _istasyon_ve_konum_getir(il, ilce)
        if enlem is None or boylam is None:
            raise MGMWeatherError(
                f"'{il}' için konum (enlem/boylam) bilgisi bulunamadı; "
                "hava kalitesi koordinat gerektirir."
            )
        veri = mgm.hava_kalitesi(float(enlem), float(boylam))
        return jsonify({"basarili": True, "veri": veri})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 404)


@app.get("/gun-ay-bilgisi/<il>")
def gun_ay_bilgisi(il: str):
    ilce = request.args.get("ilce")
    try:
        _, enlem, boylam = _istasyon_ve_konum_getir(il, ilce)
        if enlem is None or boylam is None:
            raise MGMWeatherError(
                f"'{il}' için konum (enlem/boylam) bilgisi bulunamadı; "
                "gün doğumu/batımı koordinat gerektirir."
            )
        veri: dict = {}
        veri.update(mgm.gun_dogumu_batimi(float(enlem), float(boylam)))
        veri["ayEvresi"] = mgm.ay_evresi()
        return jsonify({"basarili": True, "veri": veri})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 404)


@app.errorhandler(404)
def not_found(_exc):
    return jsonify({"basarili": False, "hata": "Uç nokta bulunamadı."}), 404


if __name__ == "__main__":
    host = os.getenv("APP_HOST", "127.0.0.1")
    port = int(os.getenv("APP_PORT", "5000"))
    server = os.getenv("APP_SERVER", "waitress").strip().lower()

    if server == "waitress":
        try:
            from waitress import serve
        except ImportError as exc:
            raise RuntimeError(
                "Waitress kurulu değil. `pip install -r requirements.txt` çalıştırın "
                "veya geçici olarak `APP_SERVER=flask` ile başlatın."
            ) from exc
        serve(app, host=host, port=port)
    elif server == "flask":
        debug = os.getenv("FLASK_DEBUG", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        app.run(host=host, port=port, debug=debug)
    else:
        raise RuntimeError(
            f"Geçersiz APP_SERVER değeri: '{server}'. Geçerli değerler: waitress, flask."
        )
