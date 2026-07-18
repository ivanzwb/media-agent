"""Central log-noise control.

The scraping stack (trafilatura + courlan + urllib3) emits a WARN/ERROR line for
almost every non-article page it touches during a deep crawl — "discarding
data", "empty HTML tree", "Discarding URL", "Connection pool is full",
per-URL DNS/timeout download errors, etc. During auto-discovery / a pipeline run
this floods the console and buries the app's own useful logs. These are benign
(the crawler simply skips those pages), so we raise their thresholds.
"""
from __future__ import annotations

import logging

# logger name -> minimum level we allow through
_NOISY_LOGGERS: dict[str, int] = {
    "trafilatura": logging.CRITICAL,   # discarding data / empty HTML tree / dl errors
    "courlan": logging.CRITICAL,       # Discarding URL: ...
    "urllib3": logging.ERROR,          # Connection pool is full / Retrying ...
    "urllib3.connectionpool": logging.ERROR,
}


def quiet_noisy_loggers() -> None:
    """Raise the threshold of noisy third-party crawl loggers. Idempotent."""
    for name, level in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(level)
