"""Constants for the Vivosun GrowHub integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "vivosun_growhub"
INTEGRATION_NAME = "Vivosun GrowHub"
API_BASE_URL = "https://api-prod.next.vivosun.com"
API_LOGIN_PATH = "/user/login"
API_DEVICE_LIST_PATH = "/iot/device/getTotalList"
API_AWS_IDENTITY_PATH = "/iot/user/awsIdentity"
API_POINT_LOG_PATH = "/iot/data/getPointLog"
API_REQUEST_TIMEOUT_SECONDS = 15

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_CAMERA_IP = "camera_ip"
CONF_HAS_CAMERA = "has_camera"
DEFAULT_TEMP_UNIT = "celsius"

SHADOW_NAME = "GrowHub"
TOPIC_SHADOW_BASE = "$aws/things/{thing}/shadow"
TOPIC_SHADOW_GET = "$aws/things/{thing}/shadow/get"
TOPIC_SHADOW_GET_ACCEPTED = "$aws/things/{thing}/shadow/get/accepted"
TOPIC_SHADOW_UPDATE = "$aws/things/{thing}/shadow/update"
TOPIC_SHADOW_UPDATE_ACCEPTED = "$aws/things/{thing}/shadow/update/accepted"
TOPIC_SHADOW_UPDATE_DOCUMENTS = "$aws/things/{thing}/shadow/update/documents"
TOPIC_SHADOW_UPDATE_DELTA = "$aws/things/{thing}/shadow/update/delta"
TOPIC_CHANNEL_APP = "{topic_prefix}/channel/app"

SHADOW_KEY_LIGHT = "light"
SHADOW_KEY_CIRCULATOR_FAN = "cFan"
SHADOW_KEY_DUCT_FAN = "dFan"
SHADOW_KEY_HUMIDIFIER = "hmdf"
SHADOW_KEY_HEATER = "heat"
SHADOW_KEY_CONNECTED = "connected"
SHADOW_KEY_MODE = "mode"
SHADOW_KEY_MANU = "manu"
SHADOW_KEY_LEVEL = "lv"
SHADOW_KEY_OSC = "osc"
SHADOW_KEY_NW = "nw"
SHADOW_KEY_AUTO = "auto"
SHADOW_KEY_TUNIT = "tUnit"

SENSOR_KEY_INSIDE_TEMP = "inTemp"
SENSOR_KEY_INSIDE_HUMI = "inHumi"
SENSOR_KEY_INSIDE_VPD = "inVpd"
SENSOR_KEY_OUTSIDE_TEMP = "outTemp"
SENSOR_KEY_OUTSIDE_HUMI = "outHumi"
SENSOR_KEY_OUTSIDE_VPD = "outVpd"
SENSOR_KEY_CORE_TEMP = "coreTemp"
SENSOR_KEY_RSSI = "rssi"
SENSOR_KEY_PROBE_TEMP = "pTemp"
SENSOR_KEY_PROBE_HUMI = "pHumi"
SENSOR_KEY_PROBE_VPD = "pVpd"
SENSOR_KEY_WATER_LEVEL = "waterLv"

SENSOR_CHANNEL_KEYS = (
    SENSOR_KEY_INSIDE_TEMP,
    SENSOR_KEY_INSIDE_HUMI,
    SENSOR_KEY_INSIDE_VPD,
    SENSOR_KEY_OUTSIDE_TEMP,
    SENSOR_KEY_OUTSIDE_HUMI,
    SENSOR_KEY_OUTSIDE_VPD,
    SENSOR_KEY_CORE_TEMP,
    SENSOR_KEY_RSSI,
    SENSOR_KEY_PROBE_TEMP,
    SENSOR_KEY_PROBE_HUMI,
    SENSOR_KEY_PROBE_VPD,
    SENSOR_KEY_WATER_LEVEL,
)

SENSOR_UNAVAILABLE_SENTINEL = -6666
TEMP_SCALE_FACTOR = 100
WATER_LEVEL_SCALE_FACTOR = 1000
LIGHT_MIN_BRIGHTNESS = 25

CFAN_LEVEL_MAP: tuple[int, ...] = (0, 44, 51, 60, 64, 70, 75, 80, 85, 90, 100)
CFAN_NATURAL_WIND_VALUE = 200
DFAN_LEVEL_MAP: tuple[int, ...] = (0, 30, 35, 40, 50, 60, 70, 80, 85, 90, 100)

MODE_MANUAL = 0
MODE_AUTO = 1
MODE_CYCLE = 1
MODE_PLAN = 2

SHADOW_ROOT_STATE = "state"
SHADOW_ROOT_DESIRED = "desired"
SHADOW_ROOT_REPORTED = "reported"
# Write-path guardrail: direct control payloads should use desired.<key>.manu.lv.
DESIRED_LEVEL_PATH_NOTE = "desired.<key>.manu.lv"

PLATFORMS: list[Platform] = [
    Platform.LIGHT,
    Platform.FAN,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.HUMIDIFIER,
    Platform.CLIMATE,
    Platform.CAMERA,
]
