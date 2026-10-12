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
# A binary entity telling whether the heating system supplies heat (catalogue
# §2.7 HeatAvailable), e.g. the ECL Comfort 310's "Heat available". Stored by
# entity registry ID.
CONF_HEAT_SOURCE = "heat_source"
# Floor-heating (or other) thermostats whose hvac_action "heating" counts as room
# demand in the house-wide demand signals. Stored by entity registry ID.
CONF_DEMAND_CLIMATES = "demand_climates"
# Catalogue §1.6: run the adaptation only while heat is available.
CONF_ADAPTATION_WITH_HEAT = "adaptation_with_heat"
# Catalogue §3.8: weekly valve exercise, staggered by EXERCISE_STAGGER_MINUTES.
CONF_EXERCISE_DAY = "exercise_day"
CONF_EXERCISE_TIME = "exercise_time"

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
DEFAULT_ADAPTATION_WITH_HEAT = True
DEFAULT_EXERCISE_DAY = "Wednesday"
DEFAULT_EXERCISE_TIME = "10:00:00"
EXERCISE_OFF = "off"
MAX_STARTUP_DELAY_MINUTES = 60
MIN_SENSOR_AGE_MINUTES = 15
MAX_SENSOR_AGE_MINUTES = 720

STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = f"{DOMAIN}.sync_state"

# The 5-minute evaluation is local. Vendor-aware write suppression happens in
# planner.py, so it does not translate into a five-minute radio write.
LOCAL_EVALUATION_INTERVAL = timedelta(minutes=5)

# HeatAvailable is re-sent even when unchanged, so a valve that missed a write
# or rejoined still knows. "False" keeps the valve in boiler mode for 48 h.
HEAT_AVAILABLE_REFRESH = timedelta(hours=3)

# Catalogue §1.2: write the clock at least weekly, and at once when a valve
# reports it lost (retried at most hourly).
TIME_SYNC_INTERVAL = timedelta(days=7)
MAINTENANCE_RETRY = timedelta(hours=1)
# Catalogue §1.6: start an adaptation run only after heat has been available
# this long, and at most once a day per valve.
ADAPTATION_HEAT_DELAY = timedelta(minutes=30)
ADAPTATION_RETRY = timedelta(hours=24)
EXERCISE_STAGGER_MINUTES = 10
