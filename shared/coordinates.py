"""Shared coordinates, haversine, address resolution, and Nominatim geocoding.

This module centralises all coordinate/distance logic used by:
  - The training worker (distance enrichment during model training)
  - The API at runtime (coordinates lookup, ``/profile`` distance calculation)
  - The profiling pipeline (distance enrichment + geocoding)

Coordinates are loaded from the SQL INSERT file at startup and kept
in-memory as a normalised lookup dict.
"""

import math
import os
import re
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Latvian diacritics stripping
# ---------------------------------------------------------------------------

_LV_DIACRITICS = str.maketrans(
    "āčēģīķļņōšūžĀČĒĢĪĶĻŅŌŠŪŽ",
    "acegiklnoszuACEGIKLNOSZU",
)


def strip_diacritics(text: str) -> str:
    """Strip Latvian diacritics: ā→a, č→c, ē→e, ģ→g, ī→i, etc."""
    return text.translate(_LV_DIACRITICS)


# ---------------------------------------------------------------------------
# City extraction (shared utility)
# ---------------------------------------------------------------------------

def extract_city(location: str) -> str:
    """Extract city name from Latvian address formats.

    Handles formats like:
      "Rīga" → "rīga"
      "Valmieras novads, Valmiera" → "valmiera"
      "Rīgas novads, Mārupe" → "mārupe"
      "Liepāja" → "liepāja"
    """
    if not isinstance(location, str) or not location.strip():
        return ""
    location = location.strip()
    if "," in location:
        parts = [p.strip() for p in location.split(",")]
        city = parts[-1]
    else:
        city = re.sub(r"\s+(novads|pagasts|pilsēta)$", "", location, flags=re.IGNORECASE)
    return city.strip().lower()


# ---------------------------------------------------------------------------
# Hospital ID → facility name mapping
# ---------------------------------------------------------------------------

HOSPITAL_MAP: dict[int, str] = {
    1: "Torņakalns",
    2: "Gaiļezers",
    4: "Rehabilitācija Gaiļezerā",
    6: "Daugavpils",
    8: "Valmiera",
}

# ---------------------------------------------------------------------------
# Facility coordinates (from novietne.sql / hardcoded)
# ---------------------------------------------------------------------------

FACILITY_COORDS: dict[str, tuple[float, float]] = {
    "Torņakalns": (56.92224388354279, 24.085035838798554),
    "Gaiļezers": (56.95995618673047, 24.253707605901496),
    "Rehabilitācija Gaiļezerā": (56.95995618673047, 24.253707605901496),
    "Valmiera": (57.538914, 25.426168),
    "Daugavpils": (55.871227, 26.515934),
}

# Build a diacritics-insensitive facility lookup at import time
FACILITY_COORDS_NORMALIZED: dict[str, tuple[float, float]] = {}
for _name, _coord in FACILITY_COORDS.items():
    FACILITY_COORDS_NORMALIZED[_name] = _coord
    _stripped = strip_diacritics(_name).lower()
    if _stripped not in FACILITY_COORDS_NORMALIZED:
        FACILITY_COORDS_NORMALIZED[_stripped] = _coord

# ---------------------------------------------------------------------------
# SQL parser
# ---------------------------------------------------------------------------

_INSERT_RE = re.compile(
    r"INSERT\s+INTO\s+\S+\s+\(name,\s*latitude,\s*longitude\)\s+VALUES\s*\("
    r"'(.+?)'\s*,\s*([\d.-]+)\s*,\s*([\d.-]+)\)",
    re.IGNORECASE,
)


def parse_coordinates_sql(sql_path: str) -> dict[str, tuple[float, float]]:
    """Parse INSERT statements from the coordinates SQL file.

    Returns ``{raw_name: (latitude, longitude)}``.
    """
    coords: dict[str, tuple[float, float]] = {}
    with open(sql_path, encoding="utf-8") as f:
        for line in f:
            m = _INSERT_RE.search(line)
            if m:
                name = m.group(1)
                lat, lon = float(m.group(2)), float(m.group(3))
                # Skip obviously invalid entries
                if 54 < lat < 59 and 20 < lon < 29:
                    coords[name] = (lat, lon)
    return coords


# ---------------------------------------------------------------------------
# Normalised lookup builder
# ---------------------------------------------------------------------------

def _normalize_aggressive(text: str) -> str:
    """Aggressively normalise an address to a core city/place name."""
    if not isinstance(text, str) or not text.strip():
        return ""
    t = text.strip().lower()

    # Rīga variants → rīga
    if t == "rīga" or t.startswith("rīga,") or t.startswith("rīga "):
        return "rīga"

    # Remove leading admin prefixes like "Konversijas rajons,"
    t = re.sub(r"^konversijas\s+rajons,\s*", "", t)

    # If comma-separated, take the last meaningful part
    if "," in t:
        parts = [p.strip() for p in t.split(",") if p.strip()]
        if parts:
            t = parts[-1]

    # Strip admin suffixes
    t = re.sub(
        r"\s+(novads|pagasts|pilsēta|rajons|lauku\s+teritorija|priekšpilsēta)$",
        "",
        t,
        flags=re.IGNORECASE,
    )

    return t.strip()


def build_lookup(
    raw_coords: dict[str, tuple[float, float]],
) -> dict[str, tuple[float, float]]:
    """Build a multi-level normalised address → (lat, lon) lookup.

    Four resolution levels (more specific entries take precedence):
      1. Direct lower-trim
      2. ``extract_city``
      3. Aggressive normalisation
      4. Diacritics-stripped variants of all above
    """
    lookup: dict[str, tuple[float, float]] = {}

    # Level 1: direct lower-trim
    for name, coord in raw_coords.items():
        key = name.strip().lower()
        if key not in lookup:
            lookup[key] = coord

    # Level 2: extract_city
    for name, coord in raw_coords.items():
        city = extract_city(name)
        if city and city not in lookup:
            lookup[city] = coord

    # Level 3: aggressive normalisation
    for name, coord in raw_coords.items():
        norm = _normalize_aggressive(name)
        if norm and norm not in lookup:
            lookup[norm] = coord

    # Level 4: diacritics-stripped variants of all existing keys
    stripped_additions: dict[str, tuple[float, float]] = {}
    for key, coord in lookup.items():
        stripped = strip_diacritics(key)
        if stripped != key and stripped not in lookup:
            stripped_additions[stripped] = coord
    lookup.update(stripped_additions)

    return lookup


# ---------------------------------------------------------------------------
# Address resolution
# ---------------------------------------------------------------------------

def resolve_address(
    address: str,
    lookup: dict[str, tuple[float, float]],
) -> tuple[float, float] | None:
    """Try to resolve a patient address to coordinates using multi-level lookup.

    Each level is also tried with diacritics stripped (e.g. Rīga → Riga).
    """
    if not isinstance(address, str) or not address.strip():
        return None

    # Level 1: direct lower-trim
    key1 = address.strip().lower()
    if key1 in lookup:
        return lookup[key1]
    key1s = strip_diacritics(key1)
    if key1s != key1 and key1s in lookup:
        return lookup[key1s]

    # Level 2: extract_city
    key2 = extract_city(address)
    if key2 and key2 in lookup:
        return lookup[key2]
    if key2:
        key2s = strip_diacritics(key2)
        if key2s != key2 and key2s in lookup:
            return lookup[key2s]

    # Level 3: aggressive normalisation
    key3 = _normalize_aggressive(address)
    if key3 and key3 in lookup:
        return lookup[key3]
    if key3:
        key3s = strip_diacritics(key3)
        if key3s != key3 and key3s in lookup:
            return lookup[key3s]

    return None


# ---------------------------------------------------------------------------
# Haversine
# ---------------------------------------------------------------------------

_EARTH_R_KM = 6371.0


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two (lat, lon) points."""
    lat1, lon1, lat2, lon2 = (math.radians(v) for v in (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return _EARTH_R_KM * 2 * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Distance calculation (for API runtime)
# ---------------------------------------------------------------------------

def calculate_distance(
    patient_address: str,
    facility_name: str,
    lookup: dict[str, tuple[float, float]],
) -> float | None:
    """Calculate distance between a patient address and a facility.

    Returns distance in km, or ``None`` if either side cannot be resolved.
    """
    patient_coords = resolve_address(patient_address, lookup)
    facility_coords = (
        FACILITY_COORDS.get(facility_name)
        or FACILITY_COORDS_NORMALIZED.get(facility_name.strip().lower())
        or FACILITY_COORDS_NORMALIZED.get(strip_diacritics(facility_name).lower())
    )

    if patient_coords is None or facility_coords is None:
        return None

    return round(
        haversine(patient_coords[0], patient_coords[1],
                  facility_coords[0], facility_coords[1]),
        3,
    )


# ---------------------------------------------------------------------------
# Nominatim geocoding (real-time fallback for unknown addresses)
# ---------------------------------------------------------------------------

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_NOMINATIM_HEADERS = {"User-Agent": "BKUS-PatientScheduling/1.0"}


def geocode_nominatim(address: str) -> tuple[float, float] | None:
    """Geocode an address via OpenStreetMap Nominatim with multi-strategy fallback.

    Strategies (tried in order):
      1. Reversed comma parts (``"novads, city"`` → ``"city, novads"``)
      2. Standard (as-is + ", Latvia")
      3. Last comma part only (suburb/city name)
      4. First comma part only (main place)

    Respects Nominatim usage policy: max 1 request per second.
    Returns ``(latitude, longitude)`` or ``None``.

    Requires the ``httpx`` package (lazy-imported).
    """
    import httpx as _httpx  # lazy import — not needed by training worker

    if not isinstance(address, str) or not address.strip():
        return None

    clean = address.strip()
    strategies = _build_search_strategies(clean)

    for query in strategies:
        try:
            resp = _httpx.get(
                _NOMINATIM_URL,
                params={
                    "q": query,
                    "format": "json",
                    "limit": 1,
                    "countrycodes": "lv",
                },
                headers=_NOMINATIM_HEADERS,
                timeout=10,
            )
            if resp.status_code == 200 and resp.json():
                result = resp.json()[0]
                lat, lon = float(result["lat"]), float(result["lon"])
                # Sanity check: must be in Latvia's bounding box
                if 55.5 < lat < 58.5 and 20.5 < lon < 28.5:
                    logger.info("Geocoded '%s' via query '%s' -> (%s, %s)",
                                address, query, lat, lon)
                    time.sleep(1.0)
                    return (lat, lon)
        except Exception:
            logger.debug("Nominatim request failed for query '%s'", query,
                         exc_info=True)
        # Rate limit: 1 req/sec
        time.sleep(1.0)

    logger.warning("Failed to geocode address: '%s'", address)
    return None


def _build_search_strategies(address: str) -> list[str]:
    """Build a list of search queries for Nominatim from a Latvian address."""
    strategies: list[str] = []
    parts = [p.strip() for p in address.split(",") if p.strip()]

    # Strategy 1: reversed comma parts + ", Latvia"
    if len(parts) > 1:
        strategies.append(", ".join(reversed(parts)) + ", Latvia")

    # Strategy 2: standard + ", Latvia"
    strategies.append(address.strip() + ", Latvia")

    # Strategy 3: last part only (city/suburb)
    if len(parts) > 1:
        strategies.append(parts[-1] + ", Latvia")

    # Strategy 4: first part only (main place)
    if len(parts) > 1:
        strategies.append(parts[0] + ", Latvia")

    return strategies


# ---------------------------------------------------------------------------
# Batch geocoding (for POST /geocode)
# ---------------------------------------------------------------------------

def geocode_addresses(
    addresses: list[str],
    known_lookup: dict[str, tuple[float, float]],
) -> list[dict]:
    """Batch-geocode a list of addresses, skipping already-known ones.

    Returns list of dicts with ``address``, ``lat``, ``lon``, ``status`` keys.
    """
    results: list[dict] = []
    for addr in addresses:
        addr_clean = addr.strip()
        if not addr_clean:
            continue

        # Skip if already known
        if resolve_address(addr_clean, known_lookup) is not None:
            results.append({"address": addr_clean, "lat": None, "lon": None, "status": "skipped"})
            continue

        coords = geocode_nominatim(addr_clean)
        if coords:
            results.append({"address": addr_clean, "lat": coords[0], "lon": coords[1], "status": "ok"})
        else:
            results.append({"address": addr_clean, "lat": None, "lon": None, "status": "failed"})

    return results


def append_to_sql_file(sql_path: str, new_entries: list[dict]) -> int:
    """Append new INSERT statements to the coordinates SQL file.

    Returns number of entries actually appended.
    """
    ok_entries = [e for e in new_entries if e.get("status") == "ok" and e.get("lat") is not None]
    if not ok_entries:
        return 0

    with open(sql_path, "a", encoding="utf-8") as f:
        for entry in ok_entries:
            name = entry["address"].replace("'", "''")
            f.write(
                f"INSERT INTO saule.latvian_places_with_coordinates_v2 "
                f"(name, latitude, longitude) VALUES('{name}', {entry['lat']}, {entry['lon']});\n"
            )

    return len(ok_entries)


# ---------------------------------------------------------------------------
# Convenience: default SQL path
# ---------------------------------------------------------------------------

def default_sql_path() -> Optional[str]:
    """Return the default coordinates SQL file path, or None if not found."""
    candidates = [
        # Shared volume (Docker): MODEL_DIR = /shared
        os.path.join(os.getenv("MODEL_DIR", "/app"),
                      "latvian_places_with_coordinates_v2.sql"),
        # Local dev: shared/ -> api/ -> src/ -> prediction_model/resources/...
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "resources",
                      "data", "input", "latvian_places_with_coordinates_v2.sql"),
    ]
    for c in candidates:
        c = os.path.normpath(c)
        if os.path.exists(c):
            return c
    return None
