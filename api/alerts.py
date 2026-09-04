"""Alert route compatibility facade.

Alert implementation remains in app.py while the route/service boundary is
migrated incrementally.
"""

from app import _alert_degerlendir, _alert_ekle, _alert_kontrol_calistir, _alert_webhook_gonder

__all__ = [
    "_alert_degerlendir",
    "_alert_ekle",
    "_alert_kontrol_calistir",
    "_alert_webhook_gonder",
]
