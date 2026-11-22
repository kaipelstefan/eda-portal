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

from .api import EdaPortalAuthError, EdaPortalClient, EdaPortalError
from .const import (
    CONF_COMMUNITY_ID,
    CONF_EMAIL,
    CONF_LOOKBACK_DAYS,
    CONF_METERING_POINT,
    CONF_PASSWORD,
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
from .importer import build_hours, days_range, stat_id

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor"]
TZ = ZoneInfo(TIMEZONE)

_STATS = (("verbrauch", "kWh", "energy", "Verbrauch"),
          ("eigendeckung", "kWh", "energy", "Eigendeckung"),
          ("vorlaeufig", "Flag", None, "Vorlaeufig"),
          ("eigendeckung_maritta", "kWh", "energy", "Eigendeckung PV Maritta"),
          ("eigendeckung_oliver", "kWh", "energy", "Eigendeckung PV Oliver"))


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
    def _producers(self) -> tuple[str, str]:
        o = self.entry.options
        return (
            str(o.get(CONF_PRODUCER_MARITTA, DEFAULT_PRODUCER_MARITTA)).strip().upper(),
            str(o.get(CONF_PRODUCER_OLIVER, DEFAULT_PRODUCER_OLIVER)).strip().upper(),
        )

    async def import_range(self, start: date, end: date) -> EdaResult:
        """Holt g/p fuer jeden Tag und schreibt die Statistiken."""
        async with self._lock:
            today = datetime.now(TZ).date()
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
                pm, po = self._producers
                gen_m: list = []
                gen_o: list = []
                if pm and po:
                    try:
                        gen_m = await self.client.day("g", pm, day)
                        await asyncio.sleep(REQUEST_PAUSE_SECONDS)
                        gen_o = await self.client.day("g", po, day)
                        await asyncio.sleep(REQUEST_PAUSE_SECONDS)
                    except EdaPortalAuthError as err:
                        raise ConfigEntryAuthFailed(str(err)) from err
                    except EdaPortalError as err:
                        _LOGGER.warning("Erzeugerdaten fuer %s nicht abrufbar: %s", day, err)
                        gen_m, gen_o = [], []
                day_hours = build_hours(g, p, TZ, today, self._prelim_days, gen_m, gen_o)
                if len(day_hours) < 23:
                    missing.append(day.isoformat())
                hours.extend(day_hours)
            hours.sort(key=lambda h: h["start"])
            for key, unit, unit_class, label in _STATS:
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
