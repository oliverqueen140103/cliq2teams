"""Shared HTTP client: token-bucket rate limiting + throttle-aware retry.

Graph's import endpoints throttle hard and the published limits move around, so
the only durable strategy is: stay conservative, always honour `Retry-After`,
and back off exponentially on anything transient.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: float | None = None):
        self.rate = rate_per_sec
        self.capacity = burst if burst is not None else max(1.0, rate_per_sec)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def take(self, n: float = 1.0) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                deficit = n - self._tokens
                wait = deficit / self.rate
            time.sleep(wait)


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} {url}: {body[:800]}")
        self.status = status
        self.body = body
        self.url = url


class HttpClient:
    """Thin wrapper. `auth_header` is a callable so tokens can refresh mid-run."""

    def __init__(
        self,
        base_url: str,
        auth_header: Callable[[], dict[str, str]],
        rate_per_sec: float = 4.0,
        max_retries: int = 8,
        timeout: int = 90,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth_header = auth_header
        self.bucket = TokenBucket(rate_per_sec)
        self.max_retries = max_retries
        self.timeout = timeout
        self.session = requests.Session()

    def request(self, method: str, path: str, **kw: Any) -> requests.Response:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        headers = dict(kw.pop("headers", {}))
        attempt = 0

        while True:
            headers.update(self.auth_header())
            self.bucket.take()
            try:
                resp = self.session.request(
                    method, url, headers=headers, timeout=self.timeout, **kw
                )
            except requests.RequestException as e:
                if attempt >= self.max_retries:
                    raise ApiError(0, str(e), url) from e
                self._sleep(attempt, None)
                attempt += 1
                continue

            if resp.status_code < 400:
                return resp

            if resp.status_code in RETRYABLE_STATUS and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After")
                log.warning(
                    "throttled/transient %s on %s (attempt %d/%d, Retry-After=%s)",
                    resp.status_code, url, attempt + 1, self.max_retries, retry_after,
                )
                self._sleep(attempt, retry_after)
                attempt += 1
                continue

            raise ApiError(resp.status_code, resp.text, url)

    def _sleep(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                time.sleep(min(float(retry_after), 300.0))
                return
            except ValueError:
                pass
        # full jitter exponential backoff
        time.sleep(random.uniform(0, min(2 ** attempt, 120)))

    # convenience
    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw).json()

    def post(self, path: str, json: Any = None, **kw: Any) -> requests.Response:
        return self.request("POST", path, json=json, **kw)

    def get_raw(self, path: str, **kw: Any) -> requests.Response:
        return self.request("GET", path, **kw)
