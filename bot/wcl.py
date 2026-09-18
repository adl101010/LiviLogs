"""Warcraft Logs API client.

Retail and every Classic flavour run the same GraphQL API on their own host (www., classic.,
fresh., vanilla.), so the host is taken from the posted link and used for every call.
"""

import logging
import re
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from .config import RecapSettings, wcl_credentials

log = logging.getLogger(__name__)

# The subdomain is letters only, so the host we call is always somewhere under warcraftlogs.com.
_REPORT_URL = re.compile(
    r"https?://(?:([a-z]+)\.)?warcraftlogs\.com/reports/([A-Za-z0-9]{8,32})(?![A-Za-z0-9])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReportRef:
    host: str
    code: str

    @property
    def url(self) -> str:
        return f"https://{self.host}/reports/{self.code}"

    @property
    def is_retail(self) -> bool:
        return self.host.startswith("www.")


def find_report_links(text: str) -> list[ReportRef]:
    refs: list[ReportRef] = []
    for sub, code in _REPORT_URL.findall(text or ""):
        ref = ReportRef(host=f"{(sub or 'www').lower()}.warcraftlogs.com", code=code)
        if ref not in refs:
            refs.append(ref)
    return refs


class WCLError(Exception):
    pass


class ReportUnavailable(WCLError):
    """The report doesn't exist or is private. Retrying won't help."""


_STATUS_QUERY = """
query Status($code: String!) {
  reportData {
    report(code: $code) {
      code title startTime endTime segments exportedSegments
    }
  }
}
"""

_FULL_QUERY = """
query Recap($code: String!, $deathsKillType: KillType) {
  reportData {
    report(code: $code) {
      code title startTime endTime segments exportedSegments visibility
      zone { id name }
      fights(killType: Encounters) { id encounterID name kill difficulty size startTime endTime }
      masterData { actors(type: "Player") { id name server subType } }
      rankings%(rankings_args)s
      deaths: table(dataType: Deaths, killType: $deathsKillType)
    }
  }
}
"""


def _rankings_args(settings: RecapSettings) -> str:
    # Enum values go in as literals; leaving an argument out means "same as the report page".
    args = []
    if settings.compare:
        args.append(f"compare: {settings.compare}")
    if settings.timeframe:
        args.append(f"timeframe: {settings.timeframe}")
    return f"({', '.join(args)})" if args else ""


class WCLClient:
    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        credentials: Callable[[str], tuple[str, str]] = wcl_credentials,
    ):
        self._http = http or httpx.AsyncClient(timeout=60)
        self._credentials = credentials
        self._tokens: dict[str, tuple[str, float]] = {}

    async def close(self) -> None:
        await self._http.aclose()

    async def _token(self, host: str, refresh: bool = False) -> str:
        cached = self._tokens.get(host)
        if cached and not refresh and cached[1] > time.time() + 60:
            return cached[0]
        client_id, secret = self._credentials(host)
        resp = await self._http.post(
            f"https://{host}/oauth/token",
            data={"grant_type": "client_credentials"},
            auth=(client_id, secret),
        )
        if resp.status_code != 200:
            raise WCLError(f"WCL login failed on {host} (HTTP {resp.status_code}). Check the client ID/secret.")
        body = resp.json()
        self._tokens[host] = (body["access_token"], time.time() + float(body.get("expires_in", 3600)))
        return body["access_token"]

    async def query(self, host: str, query: str, variables: dict) -> dict:
        for attempt in range(2):
            token = await self._token(host, refresh=attempt > 0)
            resp = await self._http.post(
                f"https://{host}/api/v2/client",
                json={"query": query, "variables": variables},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 401 and attempt == 0:
                continue  # token expired early; get a new one and retry once
            if resp.status_code == 429:
                raise WCLError("WCL rate limit hit. Try again later.")
            if resp.status_code != 200:
                raise WCLError(f"WCL returned HTTP {resp.status_code}")
            body = resp.json()
            return self._unwrap(body)
        raise WCLError("WCL rejected the login token twice")

    @staticmethod
    def _unwrap(body: dict) -> dict:
        report = ((body.get("data") or {}).get("reportData") or {}).get("report")
        errors = body.get("errors") or []
        if report is None:
            message = "; ".join(e.get("message", "") for e in errors) or "report not found"
            if not errors or any(
                word in message.lower() for word in ("permission", "not exist", "not found", "private")
            ):
                raise ReportUnavailable(message)
            raise WCLError(message)
        if errors:
            log.warning("WCL returned partial data: %s", errors)
        return report

    async def report_status(self, ref: ReportRef) -> dict:
        return await self.query(ref.host, _STATUS_QUERY, {"code": ref.code})

    async def report_full(self, ref: ReportRef, settings: RecapSettings) -> dict:
        query = _FULL_QUERY % {"rankings_args": _rankings_args(settings)}
        kill_type = "All" if settings.deaths_include_trash else "Encounters"
        return await self.query(ref.host, query, {"code": ref.code, "deathsKillType": kill_type})
