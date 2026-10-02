"""Control-host disk policy shared by scheduled maintenance and doctor.

The configured data directory identifies the filesystem doctor measures. A maintenance run
expires unused build cache after seven days; doctor raises a finding below 10 GiB free.
"""

BUILD_CACHE_MAX_AGE_HOURS = 7 * 24
ROOT_FREE_MIN_BYTES = 10 * 1024**3
