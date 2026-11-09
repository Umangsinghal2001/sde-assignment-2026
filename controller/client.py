"""Thin HTTP client for the simulator. Retries transport errors and 5xx; 4xx goes back to the caller."""
import time

import httpx


class SimError(Exception):
    def __init__(self, status, body):
        super().__init__(f"{status}: {body}")
        self.status, self.body = status, body

    @property
    def code(self):
        return self.body.get("error") if isinstance(self.body, dict) else None


class Sim:
    def __init__(self, url=None, http=None, retries=5):
        # http lets tests pass a FastAPI TestClient instead of a real socket
        self.http = http or httpx.Client(base_url=url, timeout=120)
        self.retries = retries

    def _send(self, method, path, retry=True, **kw):
        for i in range(self.retries + 1 if retry else 1):
            try:
                r = self.http.request(method, path, **kw)
            except httpx.TransportError:
                if not retry or i == self.retries:
                    raise
            else:
                if r.status_code < 500 or not retry or i == self.retries:
                    return r
            time.sleep(min(2 ** i * 0.2, 5))
        raise RuntimeError("unreachable")

    def call(self, method, path, retry=True, **kw):
        r = self._send(method, path, retry, **kw)
        try:
            body = r.json()
        except ValueError:
            body = r.text
        if r.status_code >= 400:
            raise SimError(r.status_code, body)
        return body

    def get(self, path, **params):
        return self.call("GET", path, params=params or None)

    def post(self, path, body, retry=True):
        return self.call("POST", path, retry=retry, json=body)

    def put(self, path, body):
        return self.call("PUT", path, json=body)

    def text(self, path):
        r = self._send("GET", path)
        r.raise_for_status()
        return r.text

    def lines(self, path):
        """Stream an NDJSON body line by line; a whole chunk as one string costs ~3x its size in memory."""
        with self.http.stream("GET", path) as r:
            r.raise_for_status()
            yield from r.iter_lines()
