import logging
from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry

from homeassistant.core import Event, HomeAssistant

from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import UpdateFailed

from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    Platform,
)

from .const import DOMAIN, CONF_STATION_ID, DEFAULT_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

_MODE_TO_OPTION: dict[int, str] = {
    0: "Fast",
    1: "PV priority",
    2: "PV & battery",
}

_OPTION_TO_MODE: dict[str, int] = {
    value: key for key, value in _MODE_TO_OPTION.items()
}

OPERATION_MODE = SelectEntityDescription(
    key="charge_mode",
    entity_category=EntityCategory.CONFIG,
    translation_key="charge_mode",
)


async def async_setup_entry(
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the inverter select entities from a config entry."""

    semsApi = hass.data[DOMAIN][config_entry.entry_id]
    stationId = config_entry.data[CONF_STATION_ID]

    try:
        result = await hass.async_add_executor_job(semsApi.getData, stationId)
        inverter = result
        active_mode = inverter["chargeMode"]
        current_charge_power = inverter["max_charge_power"]

    except Exception as err:
        # logging.exception("Something awful happened!")
        raise UpdateFailed(f"Error communicating with API: {err}")
    else:
        _LOGGER.debug(f"InverterOperationModeEntity args: {semsApi}, {inverter["sn"]}, {OPERATION_MODE}, {inverter},"
                      f" {[v for k, v in _MODE_TO_OPTION.items()]}, {_MODE_TO_OPTION.get(active_mode)}, {current_charge_power} ")
        entity = InverterOperationModeEntity(
            semsApi,
            inverter["sn"],
            OPERATION_MODE,
            inverter,
            [v for k, v in _MODE_TO_OPTION.items()],
            _MODE_TO_OPTION.get(active_mode),
            current_charge_power,
        )

    async_add_entities([entity])

    # eco_mode_power_entity_id = er.async_get(hass).async_get_entity_id(
    #     Platform.NUMBER,
    #     DOMAIN,
    #     f"{DOMAIN}-eco_mode_power-{inverter.serial_number}",
    # )
    # if eco_mode_power_entity_id:
    #     async_track_state_change_event(
    #         hass,
    #         eco_mode_power_entity_id,
    #         entity.update_eco_mode_power,
    #     )


class InverterOperationModeEntity(SelectEntity):
    """Entity representing the inverter operation mode."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
            self,
            api,
            sn,
            description: SelectEntityDescription,
            inverter,
            supported_options: list[str],
            current_mode: str,
            current_charge_power: int,
    ) -> None:
        super().__init__()
        """Initialize the inverter operation mode setting entity."""
        self.api = api
        self.sn = sn
        self.entity_description = description
        self._attr_unique_id = f"{self.sn}-select-charge-mode"
        self._attr_options = supported_options
        self._attr_current_option = str(current_mode)
        self._inverter = inverter
        self._current_charge_power = current_charge_power

        _LOGGER.debug(f"Creating SelectEntity for Wallbox {self.sn}")

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return f"Wallbox {self._inverter['model']}"

    @property
    def device_info(self):
        # _LOGGER.debug("self.device_state_attributes: %s", self.device_state_attributes)
        return {
            "identifiers": {
                # Serial numbers are unique identifiers within a specific domain
                (DOMAIN, self.sn)
            },
            "name": self.name,
            "manufacturer": "GoodWe",
        }

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        _LOGGER.debug(
            "Setting operation mode to %s, power %d",
            option,
            self._current_charge_power,
        )

        await self.hass.async_add_executor_job(self.api.set_charge_mode, self.sn, _OPTION_TO_MODE[option], self._current_charge_power)

        self._attr_current_option = option
        self.async_write_ha_state()

    async def async_update(self) -> None:
        try:
            # Note: asyncio.TimeoutError and aiohttp.ClientError are already
            # handled by the data update coordinator.
            # async with async_timeout.timeout(10):
            result = await self.api.getData(self.sn)
            _LOGGER.debug("Resulting result: %s", result)

            inverter = result

            if inverter is None:
                # something went wrong, probably token could not be fetched
                raise UpdateFailed(
                    "Error communicating with API, probably token could not be fetched, see debug logs"
                )

            mode = inverter["chargeMode"]
            charge_power = inverter["max_charge_power"]
            _LOGGER.debug("Got wallbox charge mode %s and power %s", mode, charge_power)

        # except ApiError as err:
        except Exception as err:
            # logging.exception("Something awful happened!")
            raise UpdateFailed(f"Error communicating with API: {err}")

        self._attr_current_option = _MODE_TO_OPTION[mode]
        self._current_charge_power = charge_power

    # async def update_eco_mode_power(self, event: Event) -> None:
    #     """Update eco mode power value in inverter (when in eco mode)."""
    #     state = event.data.get("new_state")
    #     if state is None or state.state in (STATE_UNKNOWN, "", STATE_UNAVAILABLE):
    #         return
    #
    #     self._eco_mode_power = int(float(state.state))
    #     if event.data.get("old_state"):
    #         operation_mode = _OPTION_TO_MODE[self.current_option]
    #         if operation_mode in (
    #             OperationMode.ECO_CHARGE,
    #             OperationMode.ECO_DISCHARGE,
    #         ):
    #             _LOGGER.debug("Setting eco mode power to %d", self._eco_mode_power)
    #             await self._inverter.set_operation_mode(
    #                 operation_mode, self._eco_mode_power, self._eco_mode_soc
    #             )
