"""Constants for Danfoss Ally Coordinator."""

from datetime import timedelta

DOMAIN = "danfoss_ally_coordinator"
NAME = "Danfoss Ally Coordinator"

CONF_AREAS = "areas"
CONF_ENABLED = "enabled"
CONF_MAX_SENSOR_AGE_MINUTES = "max_sensor_age_minutes"
CONF_PRIORITIZE_EXTERNAL_SENSOR = "prioritize_external_sensor"
CONF_SYNC_TEMPERATURE = "sync_temperature"
CONF_SYNC_WINDOWS = "sync_windows"

DEFAULT_ENABLED = False
DEFAULT_MAX_SENSOR_AGE_MINUTES = 120
DEFAULT_PRIORITIZE_EXTERNAL_SENSOR = False
DEFAULT_SYNC_TEMPERATURE = True
DEFAULT_SYNC_WINDOWS = True
MIN_SENSOR_AGE_MINUTES = 15
MAX_SENSOR_AGE_MINUTES = 720

STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = f"{DOMAIN}.sync_state"

# The 5-minute evaluation is local. Vendor-aware write suppression happens in
# planner.py, so it does not translate into a five-minute radio write.
LOCAL_EVALUATION_INTERVAL = timedelta(minutes=5)
