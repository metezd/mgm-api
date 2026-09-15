class MGMWeatherError(Exception):
    """MGM istemcisiyle ilgili tüm hatalar için temel sınıf."""


class MGMCircuitOpenError(MGMWeatherError):
    """Circuit breaker açıkken MGM'ye istek atlanınca fırlatılır."""

