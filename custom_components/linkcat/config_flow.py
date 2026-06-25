"""Config flow for Linkcat integration."""

from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries

from .const import (
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL_HOURS,
    CONF_USERNAME,
    DEFAULT_NAME,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DOMAIN,
    MAX_SCAN_INTERVAL_HOURS,
    MIN_SCAN_INTERVAL_HOURS,
)
from .linkcat_client import LinkcatAuthError, LinkcatClient


class LinkcatConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Linkcat."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return LinkcatOptionsFlow(config_entry)

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_USERNAME].strip().lower())
            self._abort_if_unique_id_configured()

            client = LinkcatClient(
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
            )

            try:
                await client.validate_credentials()
            except LinkcatAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title=DEFAULT_NAME, data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )


class LinkcatOptionsFlow(config_entries.OptionsFlow):
    """Handle Linkcat options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_interval = self._config_entry.options.get(
            CONF_SCAN_INTERVAL_HOURS,
            DEFAULT_SCAN_INTERVAL_HOURS,
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL_HOURS, default=current_interval): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCAN_INTERVAL_HOURS, max=MAX_SCAN_INTERVAL_HOURS),
                    )
                }
            ),
        )
