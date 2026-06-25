"""DataUpdateCoordinator for Linkcat integration."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_PASSWORD, CONF_SCAN_INTERVAL_HOURS, CONF_USERNAME, DEFAULT_SCAN_INTERVAL_HOURS, DOMAIN
from .linkcat_client import LinkcatAuthError, LinkcatClient
from .models import LinkcatAccountData

_LOGGER = logging.getLogger(__name__)


class LinkcatDataCoordinator(DataUpdateCoordinator[LinkcatAccountData]):
    """Coordinate Linkcat data fetches."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        scan_interval_hours = entry.options.get(CONF_SCAN_INTERVAL_HOURS, DEFAULT_SCAN_INTERVAL_HOURS)
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=scan_interval_hours),
        )
        self._entry = entry
        self._client = LinkcatClient(
            username=entry.data[CONF_USERNAME],
            password=entry.data[CONF_PASSWORD],
        )

    async def _async_update_data(self) -> LinkcatAccountData:
        try:
            return await self._client.fetch_account_data()
        except LinkcatAuthError as exc:
            raise UpdateFailed(f"Authentication failed: {exc}") from exc
        except Exception as exc:
            raise UpdateFailed(f"Failed to fetch Linkcat data: {exc}") from exc
