"""Ein Status-Sensor: letzter Tag mit Daten."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([EdaLastDaySensor(hass.data[DOMAIN][entry.entry_id])])


class EdaLastDaySensor(CoordinatorEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.DATE
    _attr_icon = "mdi:database-clock"
    _attr_has_entity_name = True
    _attr_name = "Letzter Datentag"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.zp}_letzter_datentag"

    @property
    def native_value(self):
        return self.coordinator.data.last_data_day if self.coordinator.data else None

    @property
    def extra_state_attributes(self):
        d = self.coordinator.data
        if not d:
            return {}
        return {
            "zaehlpunkt": self.coordinator.zp,
            "letzte_stunde_vorlaeufig": d.last_hour_preliminary,
            "geschriebene_stunden": d.written_hours,
            "unvollstaendige_tage": d.missing_days,
            "letzter_lauf": d.last_run.isoformat() if d.last_run else None,
        }
