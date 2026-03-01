# custom_components/philips_shaver/coordinator.py
from __future__ import annotations

import asyncio
from datetime import timedelta, datetime
import logging
from typing import Any

from bleak import BleakClient

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.config_entries import ConfigEntry

from homeassistant.helpers import device_registry as dr
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothScanningMode,
    async_last_service_info,
    async_register_callback,
)

from . import bluetooth as shaver_bluetooth
# ... (rest of imports are same)
from .const import (
    DOMAIN,
    CHAR_AMOUNT_OF_CHARGES,
    CHAR_AMOUNT_OF_OPERATIONAL_TURNS,
    CHAR_BATTERY_LEVEL,
    CHAR_CLEANING_CYCLES,
    CHAR_CLEANING_PROGRESS,
    CHAR_DAYS_SINCE_LAST_USED,
    CHAR_DEVICE_STATE,
    CHAR_FIRMWARE_REVISION,
    CHAR_HEAD_REMAINING,
    CHAR_HEAD_REMAINING_MINUTES,
    CHAR_LIGHTRING_COLOR_HIGH,
    CHAR_LIGHTRING_COLOR_LOW,
    CHAR_LIGHTRING_COLOR_MOTION,
    CHAR_LIGHTRING_COLOR_OK,
    CHAR_MODEL_NUMBER,
    CHAR_MOTOR_CURRENT,
    CHAR_MOTOR_CURRENT_MAX,
    CHAR_MOTOR_RPM,
    CHAR_PRESSURE,
    CHAR_SERIAL_NUMBER,
    CHAR_SHAVING_TIME,
    CHAR_TOTAL_AGE,
    CHAR_TRAVEL_LOCK,
    CHAR_SHAVING_MODE,
    CHAR_SHAVING_MODE_SETTINGS,
    CHAR_CUSTOM_SHAVING_MODE_SETTINGS,
    CONF_CAPABILITIES,
    CONF_POLL_INTERVAL,
    CONF_ENABLE_LIVE_UPDATES,
    DEFAULT_ENABLE_LIVE_UPDATES,
    DEFAULT_POLL_INTERVAL,
    POLL_READ_CHARS,
    LIVE_READ_CHARS,
    SHAVING_MODES,
)
from .utils import (
    parse_color,
    parse_shaving_settings_to_dict,
    parse_capabilities,
)

_LOGGER = logging.getLogger(__name__)


class PhilipsShaverCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Data update coordinator for Philips Shaver."""

    KEY_TO_UUID_MAPPING = {
        "device_state": CHAR_DEVICE_STATE,
        "travel_lock": CHAR_TRAVEL_LOCK,
        "battery": CHAR_BATTERY_LEVEL,
        "amount_of_charges": CHAR_AMOUNT_OF_CHARGES,
        "amount_of_operational_turns": CHAR_AMOUNT_OF_OPERATIONAL_TURNS,
        "cleaning_progress": CHAR_CLEANING_PROGRESS,
        "cleaning_cycles": CHAR_CLEANING_CYCLES,
        "motor_rpm": CHAR_MOTOR_RPM,
        "motor_current_ma": CHAR_MOTOR_CURRENT,
        "pressure": CHAR_PRESSURE,
        "head_remaining": CHAR_HEAD_REMAINING,
        "head_remaining_minutes": CHAR_HEAD_REMAINING_MINUTES,
        "shaving_time": CHAR_SHAVING_TIME,
        "shaving_settings": CHAR_SHAVING_MODE_SETTINGS,
        "total_age": CHAR_TOTAL_AGE,
    }

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        self.entry = entry
        self.address = entry.data["address"]

        # reading capabilities
        cap_int = entry.data.get(CONF_CAPABILITIES, 0)
        self.capabilities = parse_capabilities(cap_int)

        # read options
        options = entry.options
        poll_interval = options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        self.poll_interval_seconds = poll_interval
        self.enable_live_updates = options.get(
            CONF_ENABLE_LIVE_UPDATES, DEFAULT_ENABLE_LIVE_UPDATES
        )

        self.live_client: BleakClient | None = None
        self._connection_lock = asyncio.Lock()
        self._live_task: asyncio.Task | None = None

        # Pre-create live callbacks to avoid garbage collection churn
        self._live_callbacks = {
            key: self._make_live_callback(key)
            for key in self.KEY_TO_UUID_MAPPING
        }

        _LOGGER.debug(
            "Initializing coordinator for %s with poll interval %s seconds (live updates: %s)",
            self.address,
            self.poll_interval_seconds,
            self.enable_live_updates,
        )
        super().__init__(
            hass,
            _LOGGER,
            name=f"Philips Shaver {self.address}",
            update_interval=timedelta(seconds=self.poll_interval_seconds),
        )

        # Initial empty data set
        self.data = {
            "battery": None,
            "firmware": None,
            "model_number": None,
            "serial_number": None,
            "head_remaining": None,
            "days_since_last_used": None,
            "shaving_time": None,
            "device_state": None,
            "travel_lock": None,
            "cleaning_progress": 100,
            "cleaning_cycles": None,
            "motor_rpm": 0,
            "motor_current_ma": 0,
            "motor_current_max_ma": None,
            "amount_of_charges": None,
            "amount_of_operational_turns": None,
            "shaving_mode": None,
            "shaving_mode_value": None,
            "shaving_settings": None,
            "custom_shaving_settings": None,
            "pressure": 0,
            "pressure_state": None,
            "color_low": (255, 0, 0),
            "color_ok": (255, 0, 0),
            "color_high": (255, 0, 0),
            "color_motion": (255, 0, 0),
            "last_seen": None,
        }

    async def async_start(self) -> None:
        """Start initial refresh and live monitoring. Call after setup is complete."""
        self.hass.async_create_task(self.async_refresh())

        if self.enable_live_updates:
            self._live_task = self.hass.loop.create_task(self._start_live_monitoring())
        else:
            _LOGGER.info("Live updates disabled – polling only")

    def _start_advertisement_logging(self) -> None:
        """Logs every advertisement of the shaver (very helpful for debugging)."""

        @callback
        def _advertisement_debug_callback(service_info, change):
            adv = service_info.advertisement
            _LOGGER.debug(  # debug instead of warning -> less noisy
                "ADVERTISEMENT %s | Name: %s | RSSI: %s dBm | "
                "Mfr: %s | SvcData: %s | SvcUUIDs: %s",
                service_info.address,
                service_info.name or "unknown",
                service_info.rssi,
                (
                    {k: v.hex() for k, v in adv.manufacturer_data.items()}
                    if adv.manufacturer_data
                    else "none"
                ),
                (
                    {str(u): d.hex() for u, d in adv.service_data.items()}
                    if adv.service_data
                    else "none"
                ),
                adv.service_uuids or "none",
            )

        # Log only for this specific device
        self._unsub_adv_debug = async_register_callback(
            self.hass,
            _advertisement_debug_callback,
            BluetoothCallbackMatcher(address=self.address),
            BluetoothScanningMode.ACTIVE,
        )

    # ------------------------------------------------------------------
    # Called automatically by the coordinator (polling)
    # ------------------------------------------------------------------
    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data via polling fallback."""

        # 1. Live connection active -> skip immediately
        if self.live_client and self.live_client.is_connected:
            _LOGGER.debug("Live connection active – polling skipped")
            return self.data or {}

        # if data is null
        if self.data is None:
            # Initialize fallback if self.data is None
            self.data = {}

        # 2. We had live data recently -> also skip!
        last_seen = self.data.get("last_seen")
        if last_seen:
            age = (datetime.now() - last_seen).total_seconds()

            if age < self.poll_interval_seconds:
                _LOGGER.debug(
                    "Recent data (%ss < poll interval %ss) – polling skipped",
                    age,
                    self.poll_interval_seconds,
                )
                return self.data or {}

        async with self._connection_lock:
            try:
                results = await shaver_bluetooth.connect_and_read(
                    self.hass,
                    self.address,
                    POLL_READ_CHARS,
                )
                return self._process_results(results)
            except Exception as err:
                raise UpdateFailed(f"Error communicating with device: {err}") from err

    # ------------------------------------------------------------------
    # Common processing for poll + live
    # ------------------------------------------------------------------
    def _process_results(self, results: dict[str, bytes | None]) -> dict[str, Any]:
        """Process raw GATT values into coordinator data – using proper constants."""
        if not any(v is not None for v in results.values()):
            return self.data

        new_data = self.data.copy() if self.data else {}
        changed = False

        # === Standard GATT Characteristics ===
        if raw := results.get(CHAR_BATTERY_LEVEL):
            val = raw[0]
            if new_data.get("battery") != val:
                new_data["battery"] = val
                changed = True

        if raw := results.get(CHAR_FIRMWARE_REVISION):
            val = raw.decode("utf-8", "ignore").strip()
            if new_data.get("firmware") != val:
                new_data["firmware"] = val
                changed = True

        if raw := results.get(CHAR_MODEL_NUMBER):
            val = raw.decode("utf-8", "ignore").strip()
            if new_data.get("model_number") != val:
                new_data["model_number"] = val
                changed = True

        if raw := results.get(CHAR_SERIAL_NUMBER):
            val = raw.decode("utf-8", "ignore").strip()
            if new_data.get("serial_number") != val:
                new_data["serial_number"] = val
                changed = True

        # === Philips-specific Characteristics ===
        if raw := results.get(CHAR_HEAD_REMAINING):
            val = raw[0]
            if new_data.get("head_remaining") != val:
                new_data["head_remaining"] = val
                changed = True

        if raw := results.get(CHAR_HEAD_REMAINING_MINUTES):
            val = int.from_bytes(raw, "little")
            if new_data.get("head_remaining_minutes") != val:
                new_data["head_remaining_minutes"] = val
                changed = True

        if raw := results.get(CHAR_DAYS_SINCE_LAST_USED):
            val = int.from_bytes(raw, "little")
            if new_data.get("days_since_last_used") != val:
                new_data["days_since_last_used"] = val
                changed = True

        if raw := results.get(CHAR_SHAVING_TIME):
            val = int.from_bytes(raw, "little")
            if new_data.get("shaving_time") != val:
                new_data["shaving_time"] = val
                changed = True

        if raw := results.get(CHAR_DEVICE_STATE):
            state_byte = raw[0]
            val = {1: "off", 2: "shaving", 3: "charging"}.get(state_byte, "unknown")
            if new_data.get("device_state") != val:
                new_data["device_state"] = val
                changed = True

        if raw := results.get(CHAR_TRAVEL_LOCK):
            val = raw[0] == 1
            if new_data.get("travel_lock") != val:
                new_data["travel_lock"] = val
                changed = True

        if raw := results.get(CHAR_CLEANING_PROGRESS):
            val = raw[0]
            if new_data.get("cleaning_progress") != val:
                new_data["cleaning_progress"] = val
                changed = True

        if raw := results.get(CHAR_CLEANING_CYCLES):
            val = int.from_bytes(raw, "little")
            if new_data.get("cleaning_cycles") != val:
                new_data["cleaning_cycles"] = val
                changed = True

        if raw := results.get(CHAR_MOTOR_CURRENT):
            val = int.from_bytes(raw, "little")
            if new_data.get("motor_current_ma") != val:
                new_data["motor_current_ma"] = val
                changed = True

        if raw := results.get(CHAR_MOTOR_CURRENT_MAX):
            val = int.from_bytes(raw, "little")
            if new_data.get("motor_current_max_ma") != val:
                new_data["motor_current_max_ma"] = val
                changed = True

        if raw := results.get(CHAR_MOTOR_RPM):
            raw_val = int.from_bytes(raw, "little")
            val = int(round(raw_val / 3.036))
            if new_data.get("motor_rpm") != val:
                new_data["motor_rpm"] = val
                changed = True

        if raw := results.get(CHAR_AMOUNT_OF_CHARGES):
            val = int.from_bytes(raw, "little")
            if new_data.get("amount_of_charges") != val:
                new_data["amount_of_charges"] = val
                changed = True

        if raw := results.get(CHAR_AMOUNT_OF_OPERATIONAL_TURNS):
            val = int.from_bytes(raw, "little")
            if new_data.get("amount_of_operational_turns") != val:
                new_data["amount_of_operational_turns"] = val
                changed = True

        # === Colors ===
        color_map = {
            CHAR_LIGHTRING_COLOR_LOW: "color_low",
            CHAR_LIGHTRING_COLOR_OK: "color_ok",
            CHAR_LIGHTRING_COLOR_HIGH: "color_high",
            CHAR_LIGHTRING_COLOR_MOTION: "color_motion",
        }

        for char_uuid, key in color_map.items():
            if raw := results.get(char_uuid):
                if color := parse_color(raw):
                    if new_data.get(key) != color:
                        new_data[key] = color
                        changed = True

        # Shaving mode
        if raw := results.get(CHAR_SHAVING_MODE):
            mode_value = int.from_bytes(raw, "little")
            mode_name = SHAVING_MODES.get(mode_value, "unknown")
            if new_data.get("shaving_mode_value") != mode_value:
                new_data["shaving_mode_value"] = mode_value
                new_data["shaving_mode"] = mode_name
                changed = True

        # Shaving mode settings
        if raw := results.get(CHAR_SHAVING_MODE_SETTINGS):
            val = parse_shaving_settings_to_dict(raw)
            if new_data.get("shaving_settings") != val:
                new_data["shaving_settings"] = val
                changed = True

        # Custom shaving mode settings
        if raw := results.get(CHAR_CUSTOM_SHAVING_MODE_SETTINGS):
            val = parse_shaving_settings_to_dict(raw)
            if new_data.get("custom_shaving_settings") != val:
                new_data["custom_shaving_settings"] = val
                changed = True

        # Pressure
        if raw := results.get(CHAR_PRESSURE):
            val = int.from_bytes(raw, "little")
            if new_data.get("pressure") != val:
                new_data["pressure"] = val
                changed = True

        # Total Age
        if raw := results.get(CHAR_TOTAL_AGE):
            val = int.from_bytes(raw, "little")
            if new_data.get("total_age") != val:
                new_data["total_age"] = val
                changed = True

        # Device Registry Update (only if model or FW has changed)
        if changed and (new_data.get("model_number") or new_data.get("firmware")):
            dev_reg = dr.async_get(self.hass)
            device = dev_reg.async_get_device(identifiers={(DOMAIN, self.address)})
            if device:
                # Only update if there is actually newer data
                if (device.model != new_data.get("model_number") or 
                    device.sw_version != new_data.get("firmware")):
                    dev_reg.async_update_device(
                        device.id,
                        model=new_data.get("model_number") or "i9000 / XP9201",
                        sw_version=new_data.get("firmware"),
                    )

        # Always update – but only the internal timestamp
        new_data["last_seen"] = datetime.now()

        # ONLY if data has actually changed do we return the new dict
        # for DataUpdateCoordinator. Otherwise, we stay with the old state.
        if changed:
            return new_data
        
        # If nothing has changed but we want to update last_seen,
        # we do it in-place in the existing dict without triggering a listener update
        # (async_set_updated_data).
        if self.data:
            self.data["last_seen"] = new_data["last_seen"]
        
        return self.data

    @callback
    def _on_disconnect(self, _client: BleakClient) -> None:
        """Handle disconnected device."""
        _LOGGER.info("Live connection lost (remote disconnect)")
        self.live_client = None

    async def _start_live_monitoring(self) -> None:
        """Permanent live connection with notifications – exclusive and intelligent."""
        backoff = 5
        max_backoff = 300

        while True:
            # Only try if no one is currently connected
            async with self._connection_lock:
                try:
                    service_info = async_last_service_info(self.hass, self.address)
                    if not service_info:
                        _LOGGER.debug(
                            "Device %s not in range – retrying in %ds",
                            self.address,
                            backoff,
                        )
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, max_backoff)
                        continue

                    # Reset backoff when device is seen
                    backoff = 5

                    if self.live_client and self.live_client.is_connected:
                        # Should never happen – but just in case
                        await asyncio.sleep(5)
                        continue

                    _LOGGER.debug("Establishing live connection to %s...", self.address)
                    client = await shaver_bluetooth.establish_connection(
                        BleakClient,
                        service_info.device,
                        "philips_shaver",
                        disconnected_callback=self._on_disconnect,
                        timeout=15.0,
                    )

                    self.live_client = client

                    # Initially read all LIVE characteristics
                    results = {}
                    for uuid in LIVE_READ_CHARS:
                        try:
                            value = await client.read_gatt_char(uuid)
                            results[uuid] = bytes(value) if value else None
                        except Exception as e:
                            _LOGGER.debug(
                                "Live initial read failed for %s: %s", uuid, e
                            )

                    new_data = self._process_results(results)
                    self.async_set_updated_data(new_data)

                    # === Start notifications ===
                    await self._start_all_notifications()
                    _LOGGER.info("Live monitoring active – polling paused")

                except Exception as err:
                    _LOGGER.error(
                        "Live monitoring error: %s – retrying in %ds", err, backoff
                    )
                    if self.live_client:
                        try:
                            await self.live_client.disconnect()
                        except Exception:
                            pass
                    self.live_client = None
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, max_backoff)
                    continue

            # Outside the lock: wait until disconnect
            try:
                while self.live_client and self.live_client.is_connected:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                _LOGGER.debug("Live monitoring task cancelled")
                raise
            except Exception as err:
                _LOGGER.error("Unexpected error in live monitoring: %s", err)
            finally:
                # IMPORTANT: ensure we stop and disconnect
                await self._stop_all_notifications()
                if self.live_client:
                    try:
                        await self.live_client.disconnect()
                    except Exception:
                        pass
                    self.live_client = None
                _LOGGER.info("Live connection ended – polling will resume")
                await asyncio.sleep(5)  # short pause before reconnect

    def _make_live_callback(self, key: str):
        """Creates a live callback that works exactly like _process_results()."""

        @callback
        def _callback(_sender, data):
            if not data:
                return

            # We simulate a results dict with only this one characteristic
            # Use constant-time mapping lookup
            char_uuid = self.KEY_TO_UUID_MAPPING.get(key)
            if not char_uuid:
                return
            
            fake_results = {char_uuid: data}

            # _process_results() does everything: type conversion, mapping, etc.
            new_data = self._process_results(fake_results)

            if new_data == self.data:
                return  # nothing changed

            self.async_set_updated_data(new_data)

        return _callback

    async def _start_all_notifications(self) -> None:
        """Starts all GATT-Notifications for Live-Updates."""
        if not self.live_client or not self.live_client.is_connected:
            return

        for char_uuid, key in self.KEY_TO_UUID_MAPPING.items():
            try:
                # Re-use pre-created callback to avoid memory churn
                await self.live_client.start_notify(
                    char_uuid, self._live_callbacks[key]
                )
                _LOGGER.debug("Started notifications for %s", key)
            except Exception as e:
                _LOGGER.warning("Failed to start notify %s: %s", key, e)

    async def _stop_all_notifications(self) -> None:
        """Stops all GATT-Notifications."""
        if not self.live_client or not self.live_client.is_connected:
            return

        for char_uuid in self.KEY_TO_UUID_MAPPING.values():
            try:
                await self.live_client.stop_notify(char_uuid)
                _LOGGER.debug("Stopped notifications for %s", char_uuid)
            except Exception:
                pass  # ignore – will be disconnected anyway

    async def async_shutdown(self) -> None:
        """Called on unload – cleans everything up properly."""
        await self._stop_all_notifications()
        self._live_callbacks.clear()

        if hasattr(self, "_unsub_adv_debug") and self._unsub_adv_debug:
            self._unsub_adv_debug()
            self._unsub_adv_debug = None

        if self._live_task:
            self._live_task.cancel()
            try:
                await self._live_task
            except asyncio.CancelledError:
                pass

        if self.live_client and self.live_client.is_connected:
            try:
                await self.live_client.disconnect()
            except Exception:
                pass
