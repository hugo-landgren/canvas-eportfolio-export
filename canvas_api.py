"""Minimal Canvas API-klient -- allt exportverktygen behöver, inget mer.

Modulen finns för att verktygen ska gå att köra fristående. De växte fram
inuti en större admin-app, och det enda de lånade därifrån var en HTTP-klient
med sidbrytning och en läsning av miljövariabler. Båda ryms här.

Två saker är värda att känna till om Canvas API:

  * Sidbrytningen sker med RFC5988 `Link`-huvuden, inte med sidnummer i svaret.
    `paginate()` följer `rel="next"` tills det tar slut.
  * Ett `403` betyder oftast "du får inte", men Canvas svarar *också* 403 vid
    strypning, med ordet "throttle" i kroppen. Det senare ska man backa av
    och försöka igen på, inte ge upp för. `_request()` skiljer på dem.
"""
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional

import requests

TIMEOUT = 60
MAX_RETRIES = 3
PER_PAGE = 100

_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


def next_link(link_header: Optional[str]) -> Optional[str]:
    if not link_header:
        return None
    m = _NEXT_RE.search(link_header)
    return m.group(1) if m else None


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__("Canvas API error {}: {}".format(status, body[:300]))
        self.status = status
        self.body = body

    @property
    def is_auth(self) -> bool:
        """401/403 betyder att token är död eller saknar rätt -- vid ett anrop
        ser det identiskt ut med att just den posten inte gick att läsa."""
        return self.status in (401, 403)


class CanvasClient:
    def __init__(self, host: str, token: str, account_id: str = "1"):
        self.base_url = "https://{}/api/v1".format(host.strip().rstrip("/"))
        self.account_id = account_id
        self.session = requests.Session()
        self.session.headers.update({"Authorization": "Bearer {}".format(token)})

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        if not url.startswith("http"):
            url = self.base_url + url
        last_exc = None
        for attempt in range(MAX_RETRIES):
            try:
                r = self.session.request(method, url, timeout=TIMEOUT, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 502, 503, 504) or (
                r.status_code == 403 and "throttle" in r.text.lower()
            ):
                time.sleep(2 ** attempt + 1)
                continue
            if r.status_code >= 400:
                raise ApiError(r.status_code, r.text)
            return r
        raise ApiError(0, "request failed after retries: {}".format(last_exc))

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        return self._request("GET", path, params=params).json()

    def paginate(self, path: str, params: Optional[dict] = None) -> Iterator[dict]:
        """Ge varje post över alla sidor av ett Canvas index-endpoint."""
        params = dict(params or {})
        params.setdefault("per_page", PER_PAGE)
        url: Optional[str] = path
        while url:
            r = self._request("GET", url, params=params)
            batch = r.json()
            if not isinstance(batch, list):
                yield batch
                return
            if not batch:
                return
            for item in batch:
                yield item
            url = next_link(r.headers.get("link"))
            params = None  # nästa länk bär redan hela query-strängen

    def account_users(self) -> Iterator[dict]:
        """Alla användare i kontot. Sorterade på id, inte på last_login:
        dokumentationen varnar för strypning vid djup sidbrytning på andra
        sorteringskolumner, och vi vill åt varje sida ändå."""
        return self.paginate(
            "/accounts/{}/users".format(self.account_id),
            {"sort": "id", "order": "asc", "include[]": "last_login"},
        )

    def ping(self) -> None:
        """Billigaste anropet som bevisar att token fungerar."""
        self.get("/users/self")


@dataclass(frozen=True)
class Settings:
    canvas_host: str
    canvas_token: Optional[str]
    canvas_account_id: str

    @property
    def is_beta(self) -> bool:
        return ".beta." in self.canvas_host

    @property
    def environment(self) -> str:
        return "beta" if self.is_beta else "production"

    @property
    def canvas_configured(self) -> bool:
        return bool(self.canvas_token and self.canvas_host)


def settings() -> Settings:
    return Settings(
        canvas_host=os.environ.get("CANVAS_HOST", "").strip(),
        canvas_token=os.environ.get("CANVAS_API_TOKEN"),
        canvas_account_id=os.environ.get("CANVAS_ACCOUNT_ID", "1").strip(),
    )
