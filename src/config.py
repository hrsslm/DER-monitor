"""Project-wide configuration for the Ontario DER Monitor.

Every assumption that affects the numbers lives here, in one place, so a
future reader (or the owner) can see and change it without hunting through
the pipeline code. Simple values only — no clever code.
"""

from pathlib import Path
import datetime as dt

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"            # downloaded upstream files (git-ignored)
PROCESSED_DIR = DATA_DIR / "processed"  # committed outputs + manifest.json
INPUTS_DIR = DATA_DIR / "inputs"      # user-supplied files (committed)
DOCS_DIR = ROOT / "docs"

# ---------------------------------------------------------------------------
# Timezone
# ---------------------------------------------------------------------------
# IESO market reports use fixed EST (UTC-5) all year, no daylight saving.
# Verified 2026-09-29: IESO docs say hour-ending 1-24 uses EST year-round, and
# the HourlyLFDA feed had exactly 24 rows on 2026-03-08 (Toronto spring-forward).
# NOTE: the sibling iesometeo project assumes America/Toronto for IESO hours.
# Timestamps are never shared between the two projects.
TIMEZONE_MODE = "EST_fixed"
EST_OFFSET = dt.timedelta(hours=-5)   # fixed UTC-5
TORONTO_TZ = "America/Toronto"        # used only for RPP tariff classification

# Coordinate systems
EPSG_WGS84 = "EPSG:4326"      # store and plot in this
EPSG_ONTARIO = "EPSG:3161"    # area and distance work in this (metres)

# ---------------------------------------------------------------------------
# LFDA column rename (owner decision 2026-09-29)
# ---------------------------------------------------------------------------
# The IESO's HourlyLFDA report labels its rate column "LFDC Rate ($/MWh)".
# LFDA and LFDC are the same quantity; the column is renamed to
# lfda_rate_mwh when the file is parsed (Phase 2). The mapping is defined
# here so the rename is pinned by a fixture regression test.
LFDA_COLUMN_MAP = {
    "Date": "date",
    "Hour": "hour",
    "Status": "status",
    "LFDC Rate ($/MWh)": "lfda_rate_mwh",
}

# ---------------------------------------------------------------------------
# RPP holidays (assumption — configurable)
# ---------------------------------------------------------------------------
# OEB RPP prices treat weekends AND statutory holidays as off-peak all day.
# The OEB definition lists the nine holidays below. Two things sources
# disagree on: whether the August Civic Holiday counts, and whether a holiday
# falling on a weekend shifts to the next weekday. Defaults here: no Civic
# Holiday, no shift. Sensitivity: a handful of weekdays per year.
HOLIDAY_NAMES = [
    "New Year's Day",      # January 1
    "Family Day",          # third Monday of February (Ontario)
    "Good Friday",         # Friday before Easter Sunday
    "Victoria Day",        # Monday before May 25
    "Canada Day",          # July 1
    "Labour Day",          # first Monday of September
    "Thanksgiving Day",    # second Monday of October
    "Christmas Day",       # December 25
    "Boxing Day",          # December 26
]
INCLUDE_CIVIC_HOLIDAY = False  # August Civic Holiday is not an RPP holiday
SHIFT_WEEKEND_HOLIDAYS = False  # if a holiday falls on a weekend, do NOT move it


def ontario_rpp_holidays(year):
    """Return the set of date objects treated as RPP holidays for a year."""
    holidays = set()

    def add(month, day):
        holidays.add(dt.date(year, month, day))

    def nth_weekday(month, weekday, n):
        # weekday: Monday=0 .. Sunday=6. n=1 means first such weekday.
        first = dt.date(year, month, 1)
        delta = (weekday - first.weekday()) % 7
        return first + dt.timedelta(days=delta + 7 * (n - 1))

    add(1, 1)                                        # New Year's Day
    holidays.add(nth_weekday(2, 0, 3))                # Family Day
    # Good Friday: two days before Easter Sunday (Anonymous Gregorian algorithm)
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    easter = dt.date(year, month, day)
    holidays.add(easter - dt.timedelta(days=2))      # Good Friday
    # Victoria Day: the Monday strictly before May 25
    may25 = dt.date(year, 5, 25)
    holidays.add(may25 - dt.timedelta(days=may25.weekday() % 7 or 7))
    add(7, 1)                                        # Canada Day
    holidays.add(nth_weekday(9, 0, 1))                # Labour Day
    holidays.add(nth_weekday(10, 0, 2))                # Thanksgiving
    add(12, 25)                                      # Christmas Day
    add(12, 26)                                      # Boxing Day

    if not SHIFT_WEEKEND_HOLIDAYS:
        return holidays
    # Shift weekend holidays to the next weekday (currently disabled).
    shifted = set()
    for h in holidays:
        while h.weekday() >= 5:
            h += dt.timedelta(days=1)
        shifted.add(h)
    return shifted


# ---------------------------------------------------------------------------
# IESO public reports server
# ---------------------------------------------------------------------------
IESO_REPORTS_ROOT = "https://reports-public.ieso.ca/public"

# Feed folder names on the reports server (Phase 0 verified, 2026-09-29).
IESO_FEEDS = {
    # feed key: (folder, filename pattern hint)
    "da_zonal": ("DAHourlyOntarioZonalPrice", "daily XML"),
    "lfda": ("HourlyLFDA", "yearly/YTD CSV"),
    "da_lmp": ("DAHourlyEnergyLMP", "daily CSV"),
    "vg_forecast": ("VGForecastSummary", "XML"),
}

# Active Generation Contract List — use the power-data URL (verified 2026-09-29).
# Validation-only: never summed with OEB embedded-generation capacity.
CONTRACT_LIST_URL = (
    "https://www.ieso.ca/-/media/Files/IESO/Document-Library/power-data/supply/"
    "IESO-Active-Contracted-Generation-List.xlsx"
)

# ---------------------------------------------------------------------------
# OEB open data
# ---------------------------------------------------------------------------
OEB_OPEN_DATA_PAGE = "https://www.oeb.ca/ontarios-energy-sector/open-data"
# Vintage folders change (e.g. "2024-2"); the fetcher parses the landing page
# for current file links at fetch time and never guesses a next-year path.

# ---------------------------------------------------------------------------
# HTTP behaviour
# ---------------------------------------------------------------------------
HTTP_TIMEOUT = 60            # seconds per request
HTTP_CHUNK_SIZE = 1 << 20    # 1 MiB download chunks

# ---------------------------------------------------------------------------
# Release gate
# ---------------------------------------------------------------------------
# The repo contains third-party data whose licence the owner has NOT
# confirmed (LDC polygons and the user-supplied node coordinates file).
# While False, every page carries a "DRAFT: not for public release" banner
# and a noindex meta tag. The owner flips this to True only after every
# item in RELEASE_CHECKLIST.md is satisfied.
PUBLIC_RELEASE_APPROVED = False
