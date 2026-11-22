"""Einrichtung ueber die HA-Oberflaeche (Zugangsdaten bleiben in HA)."""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EdaPortalAuthError, EdaPortalClient, EdaPortalError
from .const import (
    CONF_COMMUNITY_ID,
    CONF_EMAIL,
    CONF_LOOKBACK_DAYS,
    CONF_METERING_POINT,
    CONF_PASSWORD,
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
)

SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_COMMUNITY_ID): str,
        vol.Required(CONF_METERING_POINT): str,
    }
)


class EdaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            user_input[CONF_METERING_POINT] = user_input[CONF_METERING_POINT].strip().upper()
            await self.async_set_unique_id(user_input[CONF_METERING_POINT])
            self._abort_if_unique_id_configured()
            client = EdaPortalClient(
                async_get_clientsession(self.hass),
                user_input[CONF_EMAIL],
                user_input[CONF_PASSWORD],
                user_input[CONF_COMMUNITY_ID].strip(),
            )
            try:
                await client.login()
            except EdaPortalAuthError:
                errors["base"] = "invalid_auth"
            except EdaPortalError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title=user_input[CONF_METERING_POINT], data=user_input)
        return self.async_show_form(step_id="user", data_schema=SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            client = EdaPortalClient(
                async_get_clientsession(self.hass),
                entry.data[CONF_EMAIL],
                user_input[CONF_PASSWORD],
                entry.data[CONF_COMMUNITY_ID],
            )
            try:
                await client.login()
            except EdaPortalAuthError:
                errors["base"] = "invalid_auth"
            except EdaPortalError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]})
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return EdaOptionsFlow()


class EdaOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        o = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LOOKBACK_DAYS, default=o.get(CONF_LOOKBACK_DAYS, DEFAULT_LOOKBACK_DAYS)): vol.All(int, vol.Range(min=1, max=120)),
                    vol.Required(CONF_PRELIM_DAYS, default=o.get(CONF_PRELIM_DAYS, DEFAULT_PRELIM_DAYS)): vol.All(int, vol.Range(min=0, max=30)),
                    vol.Optional(CONF_PARTICIPATION, default=o.get(CONF_PARTICIPATION, DEFAULT_PARTICIPATION)): str,
                    vol.Optional(CONF_PRODUCER_MARITTA, default=o.get(CONF_PRODUCER_MARITTA, DEFAULT_PRODUCER_MARITTA)): str,
                    vol.Optional(CONF_PRODUCER_OLIVER, default=o.get(CONF_PRODUCER_OLIVER, DEFAULT_PRODUCER_OLIVER)): str,
                }
            ),
        )
