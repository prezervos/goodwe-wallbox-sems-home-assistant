"""Constants for the sems integration."""

DOMAIN = "sems_wallbox"

import voluptuous as vol
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, CONF_SCAN_INTERVAL

CONF_STATION_ID = "wallbox_serial_No"
CONF_SCAN_INTERVAL_CHARGING = "scan_interval_charging"
CONF_PLANT_ID = "plant_id"         # Gen2: SEMS Plus plant/station ID
CONF_PRODUCT_MODEL = "product_model"  # Gen2: device model string e.g. GW7K-HCA-20

DEFAULT_SCAN_INTERVAL = 20  # timedelta(seconds=20)
DEFAULT_SCAN_INTERVAL_IDLE = 60       # seconds, when not charging
DEFAULT_SCAN_INTERVAL_CHARGING = 30   # seconds, when actively charging

# Connection type selector
CONF_CONNECTION_TYPE = "connection_type"
CONN_TYPE_CLOUD = "cloud"
CONN_TYPE_MODBUS = "modbus"

# Modbus TCP connection settings
CONF_MODBUS_HOST = "modbus_host"
CONF_MODBUS_PORT = "modbus_port"
CONF_MODBUS_DEVICE_ID = "modbus_device_id"
DEFAULT_MODBUS_PORT = 502
DEFAULT_MODBUS_DEVICE_ID = 247  # 0xF7 -- empirically discovered for GoodWe AC Wallbox Gen2

# Cloud capability config keys (stored in config_entry.data at setup time)
CONF_PILE_GENERATION = "pile_generation"          # "1" or "2"
CONF_RATED_POWER = "rated_power"                  # float kW
CONF_DASHBOARD_FUNCTIONS = "dashboard_functions"  # list[str]
CONF_MORE_DEVICE_CONTROLS = "more_device_controls"  # list[str]

# Capability string keys from dashboardFunctions
CAP_START_CHARGING = "startCharging"
CAP_CHARGING_MODE_SELECTION = "chargingModeSelection"
CAP_PLUG_AND_CHARGE = "plugAndCharge"

# Capability string keys from moreDeviceControls
CAP_OUTPUT_POWER_SETTING = "Output_Power_Setting"
CAP_DYNAMIC_LOAD_CONTROL = "Dynamic_Load_Control"
CAP_ENSURE_MIN_CHARGING_POWER = "Ensure_minimum_Charging_Power"
CAP_PHASE_SWITCH = "Phase_Switch"

# Validation of the user's configuration
SEMS_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_STATION_ID): str,
        vol.Optional(
            CONF_SCAN_INTERVAL, description={"suggested_value": 60}
        ): int,  # , default=DEFAULT_SCAN_INTERVAL
    }
)
