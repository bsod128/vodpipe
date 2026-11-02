"""Small stdlib HTTP client for the queue API.

Kept in the core package (not the server package) on purpose: the same client a
test uses, a load script uses, and `vodpipe job` uses. Depending on `requests`
just to POST JSON would drag a dependency into a library whose core promise is
"standard library only".
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body[:300]}")


class Client:
    def __init__(self, base_url: str = "http://127.0.0.1:8000", timeout: int = 30):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode()
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code, exc.read().decode(errors="replace")) from None
        return json.loads(raw) if raw.strip()[:1] in "{[" else raw

    def health(self) -> dict:
        return self._call("GET", "/health")

    def submit(self, spec: dict) -> dict:
        return self._call("POST", "/jobs", spec)

    def status(self, job_id: str) -> dict:
        return self._call("GET", f"/jobs/{job_id}")

    def cancel(self, job_id: str) -> dict:
        return self._call("POST", f"/jobs/{job_id}/cancel")

    def wait(self, job_id: str, timeout: float = 900.0, poll: float = 2.0,
             on_update=None) -> dict:
        """Poll to a terminal state. `on_update(state_dict)` drives progress UI
        and is the hook tests use to assert the stage sequence."""
        import time

        deadline = time.time() + timeout
        seen = None
        while time.time() < deadline:
            st = self.status(job_id)
            sig = (st.get("state"), st.get("stage"))
            if sig != seen:
                seen = sig
                if on_update:
                    on_update(st)
            if st.get("state") in ("SUCCESS", "CANCELLED", "FAILURE"):
                return st
            time.sleep(poll)
        raise TimeoutError(f"job {job_id} not terminal after {timeout}s")
