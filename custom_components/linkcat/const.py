"""Constants for the Linkcat integration."""

DOMAIN = "linkcat"
DEFAULT_NAME = "Linkcat"
BASE_URL = "https://mpl.linkcat.info/client/en_US/mpl/"
DEFAULT_SCAN_INTERVAL_HOURS = 24
MIN_SCAN_INTERVAL_HOURS = 1
MAX_SCAN_INTERVAL_HOURS = 168

CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_SCAN_INTERVAL_HOURS = "scan_interval_hours"

ATTR_CHECKOUTS = "checkouts"
ATTR_HOLDS = "holds"
ATTR_READY_HOLDS = "ready_holds"
