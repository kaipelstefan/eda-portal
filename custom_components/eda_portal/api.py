"""Schlanker Client fuer die (undokumentierte) Web-Schnittstelle des EDA-Portals.

Anmeldung: POST /api/login {email, password} -> {token, exp, user}
Daten:     POST /api/consumptionsurya/{g|p}/<Zaehlpunkt>
           Body {"time": {"in": {"min": "YYYY-MM-DDT00:00", "max": "YYYY-MM-DDT23:45"}},
                 "energyCommunityId": "...", "name": "<Zaehlpunkt>"}
           Antwort {"success": true, "data": [[iso_zeit, wert, qualitaet], ...]}
g = Verbrauch nach Teilnahmefaktor, p = Restbezug; Eigendeckung = g - p.
"""
from __future__ import annotations

from datetime import date, datetime
import logging
import re

import aiohttp

from .const import BASE_URL, API_URL

_LOGGER = logging.getLogger(__name__)
_EEG_ID = re.compile(r"^RC\d+$", re.I)

_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
    "Accept-Language": "de-AT,de;q=0.9",
}


class EdaPortalError(Exception):
    """Allgemeiner Fehler."""


class EdaPortalAuthError(EdaPortalError):
    """Anmeldung fehlgeschlagen oder Token ungueltig."""


class EdaPortalClient:
    def __init__(self, session: aiohttp.ClientSession, email: str, password: str, community_id: str) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._community_id = community_id
        self._token: str | None = None

    async def login(self) -> None:
        try:
            async with self._session.post(
                f"{BASE_URL}/api/login",
                json={"email": self._email, "password": self._password},
                headers={"Accept": "application/json", **_BROWSER_HEADERS},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status in (400, 401, 403):
                    raise EdaPortalAuthError("Anmeldung abgelehnt")
                if resp.status != 200:
                    raise EdaPortalError(f"Login HTTP {resp.status}: {(await resp.text())[:200]!r}")
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise EdaPortalError(f"Verbindungsfehler: {err}") from err
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise EdaPortalAuthError("Kein Token in der Login-Antwort")
        self._token = token

    async def _post(self, path: str, body: dict, retry: bool = True) -> dict:
        if not self._token:
            await self.login()
        try:
            async with self._session.post(
                f"{BASE_URL}{path}",
                json=body,
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json", **_BROWSER_HEADERS},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                text = await resp.text()
                if resp.status in (401, 403) or (resp.status == 500 and "INVALID_HEADER" in text):
                    if retry:
                        self._token = None
                        await self.login()
                        return await self._post(path, body, retry=False)
                    raise EdaPortalAuthError("Token abgelehnt")
                if resp.status != 200:
                    raise EdaPortalError(f"HTTP {resp.status} bei {path}: {text[:200]!r}")
                return await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise EdaPortalError(f"Verbindungsfehler: {err}") from err

    def set_community(self, internal_id: str) -> None:
        self._community_id = internal_id

    async def _get_json(self, path: str, retry: bool = True):
        """GET gegen die PWA-Schnittstelle; versucht beide Hosts des Portals."""
        if not self._token:
            await self.login()
        last: Exception | None = None
        for host in (API_URL, BASE_URL):
            try:
                async with self._session.get(
                    f"{host}{path}",
                    headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json", **_BROWSER_HEADERS},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    text = await resp.text()
                    if resp.status in (401, 403) and retry:
                        self._token = None
                        await self.login()
                        return await self._get_json(path, retry=False)
                    if resp.status != 200:
                        last = EdaPortalError(f"HTTP {resp.status} bei {host}{path}: {text[:120]!r}")
                        continue
                    return await resp.json(content_type=None)
            except aiohttp.ClientError as err:
                last = EdaPortalError(f"Verbindungsfehler: {err}")
        raise last or EdaPortalError("Abruf fehlgeschlagen")

    async def resolve_community(self, configured: str) -> str:
        """EEG-ID (z. B. RC100767) -> interne Portal-ID. Andere Werte gelten als interne ID."""
        configured = configured.strip()
        if not _EEG_ID.match(configured):
            return configured
        wanted = configured.upper()
        for path in ("/api/pwa/energycommunities", "/api/users"):
            try:
                data = await self._get_json(path)
            except EdaPortalAuthError:
                raise
            except EdaPortalError as err:
                _LOGGER.debug("Suche der EEG ueber %s fehlgeschlagen: %s", path, err)
                continue
            found = _find_community(data, wanted)
            if found:
                return found
        raise EdaPortalError(f"EEG {configured} im Portal nicht gefunden")

    async def participation_history(self, internal_id: str, metering_point: str) -> list[tuple[date, float]]:
        """Teilnahmefaktor-Verlauf des Zaehlpunkts aus dem Portal: [(gueltig ab, Prozent)]."""
        data = await self._get_json(f"/api/pwa/energycommunities/{internal_id}")
        mps = (data.get("data") or {}).get("meterpoints") or []
        for mp in mps:
            if str(mp.get("name", "")).upper() == metering_point.upper():
                out = []
                for h in mp.get("history") or []:
                    try:
                        f = float(h["participationFactor"])
                        d = datetime.fromisoformat(str(h["dateFrom"]).replace(" ", "T")).date()
                    except (KeyError, TypeError, ValueError):
                        continue
                    if f > 0:
                        out.append((d, f))
                if not out and mp.get("initialParticipationFactor"):
                    out.append((date(2000, 1, 1), float(mp["initialParticipationFactor"])))
                return sorted(out)
        raise EdaPortalError(f"Zaehlpunkt {metering_point} nicht in der EEG gefunden")

    async def day(self, kind: str, metering_point: str, day: date) -> list[list]:
        """Viertelstundenwerte eines Tages. kind: 'g' oder 'p'."""
        body = {
            "time": {"in": {"min": f"{day.isoformat()}T00:00", "max": f"{day.isoformat()}T23:45"}},
            "energyCommunityId": self._community_id,
            "name": metering_point,
        }
        res = await self._post(f"/api/consumptionsurya/{kind}/{metering_point}", body)
        if not isinstance(res, dict) or not res.get("success", False):
            raise EdaPortalError(f"Unerwartete Antwort fuer {kind} {day}")
        return res.get("data") or []


def _find_community(data, wanted: str) -> str | None:
    """Sucht rekursiv ein Objekt mit energy_community_id == wanted und gibt dessen id zurueck."""
    if isinstance(data, dict):
        if str(data.get("energy_community_id", "")).upper() == wanted and data.get("id"):
            return str(data["id"])
        for v in data.values():
            r = _find_community(v, wanted)
            if r:
                return r
    elif isinstance(data, list):
        for v in data:
            r = _find_community(v, wanted)
            if r:
                return r
    return None
