"""Komut satiri arayuzu: `python -m mgm_client <il> [ilce]`."""

from ._errors import MGMWeatherError
from .client import MGMWeather


def _cli_calistir() -> None:
    """`python -m mgm_client <il> [ilce]` komut satırı arayüzü. Sunucu
    gerektirmez, sadece hava_durumu()'u çağırıp sonucu JSON basar."""
    import argparse
    import json
    import sys

    ayiklayici = argparse.ArgumentParser(
        prog="python -m mgm_client",
        description="MGM (Türkiye) hava durumu verisine komut satırından, HTTP sunucu olmadan erişir.",
    )
    ayiklayici.add_argument("il", help="İl adı (örn. İstanbul)")
    ayiklayici.add_argument("ilce", nargs="?", default=None, help="İlçe adı (opsiyonel)")
    ayiklayici.add_argument(
        "--indent", type=int, default=2, help="JSON çıktısının girinti genişliği (varsayılan: 2)"
    )
    args = ayiklayici.parse_args()

    client = MGMWeather()
    try:
        sonuc = client.hava_durumu(args.il, args.ilce)
    except MGMWeatherError as exc:
        print(f"Hata: {exc}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(sonuc, ensure_ascii=False, indent=args.indent))
