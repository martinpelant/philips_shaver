# custom_components/philips_shaver/entity.py
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.components.bluetooth import async_last_service_info
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .coordinator import PhilipsShaverCoordinator
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class PhilipsShaverEntity(CoordinatorEntity[PhilipsShaverCoordinator]):
    """Base class for all Philips Shaver entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PhilipsShaverCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.entry = entry
        self._address = entry.data["address"]

        # Device-Info wird beim ersten Mal gesetzt
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, self._address)},
            connections={(dr.CONNECTION_BLUETOOTH, self._address)},
            manufacturer="Philips",
            name="Philips Shaver",
            # Model und Firmware kommen später → werden in _handle_coordinator_update aktualisiert
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""

        # dynamic icon update
        if hasattr(self, "icon"):
            try:
                new_icon = self.icon
                if getattr(self, "_attr_icon", None) != new_icon:
                    self._attr_icon = new_icon
            except Exception as err:
                _LOGGER.debug(
                    "Failed to update dynamic icon for %s: %s",
                    self.entity_id or self.__class__.__name__,
                    err,
                )

        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        """Return True if the device is currently in Bluetooth range."""

        # Checking if the device is currently in range
        service_info = async_last_service_info(self.hass, self._address)
        return service_info is not None
