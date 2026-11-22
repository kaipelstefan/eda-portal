"""Schlanker Client fuer die (undokumentierte) Web-Schnittstelle des EDA-Portals.

Anmeldung: POST /api/login {email, password} -> {token, exp, user}
Daten:     POST /api/consumptionsurya/{g|p}/<Zaehlpunkt>
           Body {"time": {"in": {"min": "YYYY-MM-DDT00:00", "max": "YYYY-MM-DDT23:45"}},
                 "energyCommunityId": "...", "name": "<Zaehlpunkt>"}
           Antwort {"success": true, "data": [[iso_zeit, wert, qualitaet], ...]}
g = Verbrauch nach Teilnahmefaktor, p = Restbezug; Eigendeckung = g - p.
"""
from __future__ import annotations

from datetime import date

import aiohttp

from .const import BASE_URL


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
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status in (400, 401, 403):
                    raise EdaPortalAuthError("Anmeldung abgelehnt")
                if resp.status != 200:
                    raise EdaPortalError(f"Login HTTP {resp.status}")
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
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
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
                    raise EdaPortalError(f"HTTP {resp.status} bei {path}")
                return await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise EdaPortalError(f"Verbindungsfehler: {err}") from err

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
