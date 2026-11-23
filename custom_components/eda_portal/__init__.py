"""EDA-Portal: Viertelstundenwerte des eigenen Zaehlpunkts als Langzeitstatistik."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import voluptuous as vol

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EdaPortalAuthError, EdaPortalClient, EdaPortalError, parse_producers
from .api import parse_participation as parse_portal_participation
from .const import (
    CONF_COMMUNITY_ID,
    CONF_EMAIL,
    CONF_LOOKBACK_DAYS,
    CONF_METERING_POINT,
    CONF_PASSWORD,
    CONF_COMMUNITY_OVERRIDE,
    CONF_PARTICIPATION,
    DEFAULT_PARTICIPATION,
    CONF_PRELIM_DAYS,
    CONF_PRODUCER_MARITTA,
    CONF_PRODUCER_OLIVER,
    DEFAULT_PRODUCER_MARITTA,
    DEFAULT_PRODUCER_OLIVER,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_PRELIM_DAYS,
    DOMAIN,
    REQUEST_PAUSE_SECONDS,
    SERVICE_BACKFILL,
    TIMEZONE,
    UPDATE_INTERVAL_HOURS,
)
from .importer import build_hours, days_range, parse_participation, slug, stat_id

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor"]
TZ = ZoneInfo(TIMEZONE)

_STATS = (("verbrauch", "kWh", "energy", "Verbrauch"),
          ("eigendeckung", "kWh", "energy", "Eigendeckung"),
          ("vorlaeufig", "Flag", None, "Vorlaeufig"))


@dataclass
class EdaResult:
    last_data_day: date | None = None
    last_hour_preliminary: bool | None = None
    written_hours: int = 0
    missing_days: list[str] = field(default_factory=list)
    last_run: datetime | None = None


def _metadata(zp: str, key: str, unit: str, unit_class: str | None, label: str) -> StatisticMetaData:
    """Baut die Metadaten passend zur installierten HA-Version (Felder werden abgeglichen)."""
    wanted = {
        "has_mean": True,
        "has_sum": False,
        "name": f"EEG {zp} {label}",
        "source": "sensor",
        "statistic_id": stat_id(zp, key),
        "unit_of_measurement": unit,
        "unit_class": unit_class,
    }
    try:
        from homeassistant.components.recorder.models import StatisticMeanType  # type: ignore

        wanted["mean_type"] = StatisticMeanType.ARITHMETIC
        wanted.pop("has_mean", None)
    except ImportError:
        pass
    allowed = set(getattr(StatisticMetaData, "__annotations__", wanted))
    return {k: v for k, v in wanted.items() if k in allowed}  # type: ignore[return-value]


class EdaCoordinator(DataUpdateCoordinator[EdaResult]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=timedelta(hours=UPDATE_INTERVAL_HOURS))
        self.entry = entry
        self.zp: str = entry.data[CONF_METERING_POINT]
        self.client = EdaPortalClient(
            async_get_clientsession(hass),
            entry.data[CONF_EMAIL],
            entry.data[CONF_PASSWORD],
            entry.data[CONF_COMMUNITY_ID],
        )
        self._lock = asyncio.Lock()

    @property
    def _prelim_days(self) -> int:
        return int(self.entry.options.get(CONF_PRELIM_DAYS, DEFAULT_PRELIM_DAYS))

    @property
    def _lookback(self) -> int:
        return int(self.entry.options.get(CONF_LOOKBACK_DAYS, DEFAULT_LOOKBACK_DAYS))

    @property
    def _participation_fallback(self):
        return parse_participation(self.entry.options.get(CONF_PARTICIPATION, DEFAULT_PARTICIPATION))

    @property
    def _configured_community(self) -> str:
        return str(self.entry.options.get(CONF_COMMUNITY_OVERRIDE) or self.entry.data[CONF_COMMUNITY_ID]).strip()

    @property
    def _fallback_producers(self) -> list[dict]:
        o = self.entry.options
        out = []
        for key, opt, default in (
            ("maritta", CONF_PRODUCER_MARITTA, DEFAULT_PRODUCER_MARITTA),
            ("oliver", CONF_PRODUCER_OLIVER, DEFAULT_PRODUCER_OLIVER),
        ):
            zp = str(o.get(opt, default)).strip().upper()
            if zp:
                out.append({"zp": zp, "key": key, "since": None, "till": None})
        return out

    async def _prepare(self) -> tuple[list, list[dict]]:
        """Interne EEG-ID aufloesen (jeden Lauf, falls sie sich aendert), dann Teilnahmefaktor-Verlauf und
        Erzeuger aus dem Portal lesen. Klappt das nicht, gelten die Einstellungen als Rueckfall."""
        participation = None
        producers = None
        try:
            internal = await self.client.resolve_community(self._configured_community)
            self.client.set_community(internal)
        except EdaPortalAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except EdaPortalError as err:
            internal = None
            _LOGGER.warning("EEG-ID konnte nicht aufgeloest werden, es wird der gespeicherte Wert benutzt: %s", err)
            self.client.set_community(self._configured_community)
        if internal:
            try:
                info = await self.client.community_info(internal)
                participation = parse_portal_participation(info, self.zp) or None
                plist = parse_producers(info)
                if plist:
                    used: set[str] = set()
                    producers = []
                    known = {f["zp"]: f["key"] for f in self._fallback_producers}  # bisherige Schluessel bleiben erhalten
                    for pr in plist:
                        key = known.get(pr["zp"].upper()) or slug(pr["alias"]) or slug(pr["zp"])
                        if key in used:
                            key = slug(pr["zp"])
                        used.add(key)
                        producers.append({**pr, "key": key})
            except EdaPortalAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except EdaPortalError as err:
                _LOGGER.warning("EEG-Daten nicht lesbar, Einstellungen werden als Rueckfall benutzt: %s", err)
        if participation is None:
            participation = self._participation_fallback
        if producers is None:
            producers = self._fallback_producers
        _LOGGER.info("Erzeuger: %s", [(p_["zp"], p_["key"]) for p_ in producers])
        return participation, producers

    async def import_range(self, start: date, end: date) -> EdaResult:
        """Holt g/p fuer jeden Tag und schreibt die Statistiken."""
        async with self._lock:
            today = datetime.now(TZ).date()
            participation, producers = await self._prepare()
            hours: list[dict] = []
            missing: list[str] = []
            for day in days_range(start, end):
                try:
                    g = await self.client.day("g", self.zp, day)
                    await asyncio.sleep(REQUEST_PAUSE_SECONDS)
                    p = await self.client.day("p", self.zp, day)
                    await asyncio.sleep(REQUEST_PAUSE_SECONDS)
                except EdaPortalAuthError as err:
                    raise ConfigEntryAuthFailed(str(err)) from err
                except EdaPortalError as err:
                    raise UpdateFailed(str(err)) from err
                # Erzeuger, die an diesem Tag Teil der EEG waren (Aenderungen im Portal wirken so rueckwirkend richtig)
                active = [
                    pr for pr in producers
                    if (pr["since"] is None or pr["since"] <= day) and (pr["till"] is None or day <= pr["till"])
                ]
                generation: dict[str, list] = {}
                for pr in active:
                    try:
                        generation[pr["key"]] = await self.client.day("g", pr["zp"], day)
                        await asyncio.sleep(REQUEST_PAUSE_SECONDS)
                    except EdaPortalAuthError as err:
                        raise ConfigEntryAuthFailed(str(err)) from err
                    except EdaPortalError as err:
                        _LOGGER.warning("Erzeugerdaten %s fuer %s nicht abrufbar: %s", pr["zp"], day, err)
                        generation = {}
                        break
                day_hours = build_hours(g, p, TZ, today, self._prelim_days, generation, participation)
                if len(day_hours) < 23:
                    missing.append(day.isoformat())
                hours.extend(day_hours)
            hours.sort(key=lambda h: h["start"])
            series = list(_STATS)
            for key in sorted({k for h in hours for k in h.get("split", {})}):
                series.append((f"eigendeckung_{key}", "kWh", "energy", f"Eigendeckung PV {key.title()}"))
            for key, unit, unit_class, label in series:
                if key.startswith("eigendeckung_"):
                    sub = key[len("eigendeckung_"):]
                    data = [
                        StatisticData(start=h["start"], mean=h["split"][sub], min=h["split"][sub], max=h["split"][sub])
                        for h in hours
                        if sub in h.get("split", {})
                    ]
                else:
                    data = [
                        StatisticData(start=h["start"], mean=h[key], min=h[key], max=h[key])
                        for h in hours
                        if key in h
                    ]
                if data:
                    async_add_external_statistics(self.hass, _metadata(self.zp, key, unit, unit_class, label), data)
            result = EdaResult(written_hours=len(hours), missing_days=missing, last_run=datetime.now(TZ))
            if hours:
                last = hours[-1]
                result.last_data_day = last["start"].astimezone(TZ).date()
                result.last_hour_preliminary = bool(last["vorlaeufig"])
            return result

    async def _async_update_data(self) -> EdaResult:
        today = datetime.now(TZ).date()
        result = await self.import_range(today - timedelta(days=self._lookback), today)
        if result.last_data_day is None and self.data:
            result.last_data_day = self.data.last_data_day
            result.last_hour_preliminary = self.data.last_hour_preliminary
        return result


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = EdaCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_options_updated))

    if not hass.services.has_service(DOMAIN, SERVICE_BACKFILL):

        async def _backfill(call: ServiceCall) -> None:
            start: date = call.data["start_date"]
            end: date = call.data.get("end_date") or datetime.now(TZ).date()
            if end < start:
                raise ServiceValidationError("end_date liegt vor start_date")
            if (end - start).days > 400:
                raise ServiceValidationError("Zeitraum auf 400 Tage begrenzt")
            for coord in hass.data.get(DOMAIN, {}).values():
                res = await coord.import_range(start, end)
                _LOGGER.info("Backfill %s..%s: %s Stunden, unvollstaendige Tage: %s",
                             start, end, res.written_hours, res.missing_days)
            for coord in hass.data.get(DOMAIN, {}).values():
                await coord.async_request_refresh()

        hass.services.async_register(
            DOMAIN,
            SERVICE_BACKFILL,
            _backfill,
            schema=vol.Schema({vol.Required("start_date"): cv.date, vol.Optional("end_date"): cv.date}),
        )
    return True


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_BACKFILL)
    return ok
