"""Sensor platform for Linkcat integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTR_CHECKOUTS, ATTR_HOLDS, ATTR_READY_HOLDS, DOMAIN
from .coordinator import LinkcatDataCoordinator
from .models import LinkcatAccountData


@dataclass(frozen=True, kw_only=True)
class LinkcatSensorEntityDescription(SensorEntityDescription):
    """Describes Linkcat sensor entity."""

    value_key: str


SENSORS: tuple[LinkcatSensorEntityDescription, ...] = (
    LinkcatSensorEntityDescription(
        key="checkout_count",
        name="Linkcat Checkouts",
        icon="mdi:book-open-page-variant",
        value_key="checkout_count",
    ),
    LinkcatSensorEntityDescription(
        key="hold_count",
        name="Linkcat Holds",
        icon="mdi:bookmark",
        value_key="hold_count",
    ),
    LinkcatSensorEntityDescription(
        key="ready_hold_count",
        name="Linkcat Ready Holds",
        icon="mdi:bookmark-check",
        value_key="ready_hold_count",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: LinkcatDataCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        LinkcatSensor(coordinator=coordinator, entry=entry, description=description)
        for description in SENSORS
    )


class LinkcatSensor(CoordinatorEntity[LinkcatDataCoordinator], SensorEntity):
    """A Linkcat sensor."""

    entity_description: LinkcatSensorEntityDescription

    def __init__(
        self,
        coordinator: LinkcatDataCoordinator,
        entry: ConfigEntry,
        description: LinkcatSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"

    @property
    def native_value(self) -> int | None:
        data: LinkcatAccountData | None = self.coordinator.data
        if data is None:
            return None
        return getattr(data, self.entity_description.value_key)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        data: LinkcatAccountData | None = self.coordinator.data
        if data is None:
            return {}

        if self.entity_description.key == "checkout_count":
            return {
                ATTR_CHECKOUTS: [
                    {
                        "title": item.title,
                        "author": item.author,
                        "image_url": item.image_url,
                        "due_date": item.due_date,
                    }
                    for item in data.checkouts
                ]
            }

        if self.entity_description.key in {"hold_count", "ready_hold_count"}:
            return {
                ATTR_HOLDS: [
                    {
                        "title": item.title,
                        "author": item.author,
                        "image_url": item.image_url,
                        "status": item.status,
                        "ready": item.ready,
                    }
                    for item in data.holds
                ],
                ATTR_READY_HOLDS: data.ready_hold_count,
            }

        return {}
