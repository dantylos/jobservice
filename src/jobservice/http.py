"""Polite HTTP client: fixed delay between requests and retries on transient errors."""

import logging
import time

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "jobservice/0.1 (personal job-search collector)"
_RETRY_STATUS = {429, 500, 502, 503, 504}


class HttpClient:
    def __init__(
        self,
        delay: float = 1.0,
        retries: int = 3,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.delay = delay
        self.retries = retries
        self._last = 0.0
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "cs,en;q=0.8"},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self._client.close()

    def get(self, url: str, **kw) -> httpx.Response:
        return self._request("GET", url, **kw)

    def post(self, url: str, **kw) -> httpx.Response:
        return self._request("POST", url, **kw)

    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        for attempt in range(1, self.retries + 1):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self._client.request(method, url, **kw)
            except httpx.TransportError as e:
                if attempt == self.retries:
                    raise
                log.warning("%s %s failed (%s), retry %d", method, url, e, attempt)
            else:
                if resp.status_code not in _RETRY_STATUS or attempt == self.retries:
                    resp.raise_for_status()
                    return resp
                log.warning("%s %s -> %d, retry %d", method, url, resp.status_code, attempt)
            time.sleep(self.delay * 2**attempt)
        raise AssertionError("unreachable")
