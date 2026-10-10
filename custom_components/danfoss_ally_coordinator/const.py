"""Constants for Danfoss Ally Coordinator."""

from datetime import timedelta

DOMAIN = "danfoss_ally_coordinator"
NAME = "Danfoss Ally Coordinator"

CONF_AREAS = "areas"
CONF_ENABLED = "enabled"
CONF_MAX_SENSOR_AGE_MINUTES = "max_sensor_age_minutes"

CONF_SYNC_TEMPERATURE = "sync_temperature"
CONF_SYNC_WINDOWS = "sync_windows"
CONF_SYNC_VALVE_WINDOWS = "sync_valve_windows"
CONF_AGGREGATION = "aggregation"
CONF_STARTUP_DELAY_MINUTES = "startup_delay_minutes"

# Settings changed by the room switches; applied live without a reload.
CONF_EXCLUDED_ENTITIES = "excluded_entities"
CONF_SETPOINT_SYNC_DISABLED_AREAS = "setpoint_sync_disabled_areas"
CONF_LOAD_BALANCING_AREAS = "load_balancing_areas"
LIVE_OPTION_KEYS = (
    CONF_EXCLUDED_ENTITIES,
    CONF_SETPOINT_SYNC_DISABLED_AREAS,
    CONF_LOAD_BALANCING_AREAS,
)

AGGREGATION_MEDIAN = "median"
AGGREGATION_MEAN = "mean"

DEFAULT_ENABLED = False
DEFAULT_MAX_SENSOR_AGE_MINUTES = 120

DEFAULT_SYNC_TEMPERATURE = True
DEFAULT_SYNC_WINDOWS = True
DEFAULT_SYNC_VALVE_WINDOWS = False
DEFAULT_AGGREGATION = AGGREGATION_MEDIAN
# After a Home Assistant restart the Zigbee mesh needs time to re-form and
# sleepy devices need to check in before valves are written again.
DEFAULT_STARTUP_DELAY_MINUTES = 10
MAX_STARTUP_DELAY_MINUTES = 60
MIN_SENSOR_AGE_MINUTES = 15
MAX_SENSOR_AGE_MINUTES = 720

STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = f"{DOMAIN}.sync_state"

# The 5-minute evaluation is local. Vendor-aware write suppression happens in
# planner.py, so it does not translate into a five-minute radio write.
LOCAL_EVALUATION_INTERVAL = timedelta(minutes=5)
