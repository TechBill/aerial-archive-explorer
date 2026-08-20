#!/usr/bin/env python3
"""Aerial Archive Explorer: search and download historic USGS aerial photos."""

from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import math
import os
import platform
import queue
import random
import re
import shutil
import ssl
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable, Iterable, Mapping

import certifi
import keyring
from keyring.errors import KeyringError, PasswordDeleteError
from PIL import Image, ImageTk, UnidentifiedImageError


# Configuration and constants

APP_NAME = "Aerial Archive Explorer"
APP_VERSION = "2.0.2"
APP_SUBTITLE = "Search and download historic USGS aerial photographs by location."
API_BASE = "https://m2m.cr.usgs.gov/api/api/json/stable/"
USER_AGENT = f"AerialArchiveExplorer/{APP_VERSION} (+independent USGS catalog client)"
REQUEST_TIMEOUT = 45
PAGE_SIZE = 100
SEARCH_CAP = 500
DEFAULT_RADIUS = "1.0"
RETRY_LIMIT = 3
PREVIEW_LIMIT = 20 * 1024 * 1024
MAX_UNPACKED_IMAGE = 4 * 1024 * 1024 * 1024
POLL_INTERVAL = 5.0
POLL_LIMIT = 60
EARTH_EXPLORER_URL = "https://earthexplorer.usgs.gov/"
M2M_ACCESS_URL = "https://ers.cr.usgs.gov/profile/access"
M2M_TOKEN_URL = "https://ers.cr.usgs.gov/profile/access"
DONATE_URL = "https://www.paypal.com/paypalme/techbill"
CREDENTIAL_SERVICE = "com.techbill.aerialarchiveexplorer.m2m"
CREDENTIAL_USERNAME_KEY = "saved-usgs-username"
MISSING = "—"

COLUMNS = (
    ("year", "Year", 64), ("date", "Date", 100),
    ("agency", "Agency", 130), ("project", "Project", 120),
    ("roll", "Roll", 80), ("frame", "Frame", 80),
    ("scale", "Scale", 90), ("image_type", "Type", 110),
    ("preview", "Preview", 74), ("download", "Download", 88),
    ("display_id", "ID", 180),
)

LOG = logging.getLogger(APP_NAME)


# Models and exceptions

class ApiError(Exception):
    """Safe, categorized error from the USGS API or transport."""

    def __init__(self, category: str, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.detail = redact(detail)


class DiagnosticsBuffer(logging.Handler):
    """Thread-safe, bounded in-memory diagnostics for the current app run."""

    def __init__(self, capacity: int = 1000) -> None:
        super().__init__(logging.INFO)
        self._lines: deque[str] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                                            datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = redact(self.format(record))
        except (TypeError, ValueError):
            line = "Diagnostics formatting failed."
        with self._lock:
            self._lines.append(line)

    def snapshot(self) -> str:
        with self._lock:
            return "\n".join(self._lines)

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()


class CredentialStore:
    """Persist the M2M application token only in the operating-system vault."""

    def __init__(self, backend: Any = keyring) -> None:
        self._backend = backend

    def load(self) -> tuple[str, str] | None:
        try:
            username = self._backend.get_password(
                CREDENTIAL_SERVICE, CREDENTIAL_USERNAME_KEY
            )
            if not username:
                return None
            token = self._backend.get_password(CREDENTIAL_SERVICE, username)
        except KeyringError as exc:
            raise ApiError(
                "Credential store",
                "The operating-system credential store could not be read.",
                str(exc),
            ) from exc
        return (username, token) if token else None

    def save(self, username: str, token: str) -> None:
        previous = self.load()
        try:
            if previous and previous[0] != username:
                self._delete(previous[0])
            self._backend.set_password(CREDENTIAL_SERVICE, username, token)
            self._backend.set_password(
                CREDENTIAL_SERVICE, CREDENTIAL_USERNAME_KEY, username
            )
        except KeyringError as exc:
            try:
                self._delete(username)
            except ApiError:
                pass
            raise ApiError(
                "Credential store",
                "The credential could not be saved securely.",
                str(exc),
            ) from exc

    def clear(self) -> None:
        saved = self.load()
        if saved:
            self._delete(saved[0])
        self._delete(CREDENTIAL_USERNAME_KEY)

    def _delete(self, account: str) -> None:
        try:
            self._backend.delete_password(CREDENTIAL_SERVICE, account)
        except PasswordDeleteError:
            pass
        except KeyringError as exc:
            raise ApiError(
                "Credential store",
                "The saved credential could not be cleared.",
                str(exc),
            ) from exc


def local_config_path() -> Path:
    """Per-OS location for the optional local credential fallback file."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / APP_NAME
    return base / "config.json"


class LocalTokenStore:
    """Explicitly opt-in fallback that stores the M2M username/token in a
    plain local file instead of the OS credential store.

    This is a deliberately lower-security choice, not a replacement for
    ``CredentialStore``: the file is not encrypted, only restricted to the
    owning user where the OS supports it (``0600``/``0700`` on POSIX; NTFS
    already limits the per-user profile directory on Windows). It exists
    for a single-user, non-shared machine where repeated OS keychain
    re-authorization prompts (e.g. after every unsigned development
    rebuild) are more disruptive than the residual risk of a plaintext
    file under the user's own profile. Never make this the default store;
    only construct/use it when the user has explicitly opted in via the
    access prompt, and prefer ``CredentialStore`` whenever both have a
    saved credential.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or local_config_path()

    def load(self) -> tuple[str, str] | None:
        try:
            if not self.path.exists():
                return None
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ApiError(
                "Credential store",
                "The local saved-access file could not be read.",
                str(exc),
            ) from exc
        if not isinstance(data, Mapping):
            return None
        username, token = data.get("username"), data.get("token")
        return (username, token) if username and token else None

    def save(self, username: str, token: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        partial = self.path.with_name(self.path.name + ".part")
        try:
            partial.write_text(
                json.dumps({"username": username, "token": token}), encoding="utf-8",
            )
            try:
                os.chmod(partial, 0o600)
            except OSError:
                pass
            partial.replace(self.path)
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise ApiError(
                "Credential store",
                "The access could not be saved to the local file.",
                str(exc),
            ) from exc

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise ApiError(
                "Credential store",
                "The local saved-access file could not be removed.",
                str(exc),
            ) from exc


@dataclass(frozen=True)
class SearchQuery:
    latitude: float
    longitude: float
    radius_km: float


@dataclass
class AerialFrame:
    entity_id: str
    display_id: str
    acquisition_date: dt.date | None = None
    agency: str = ""
    project: str = ""
    roll: str = ""
    frame: str = ""
    scale: str = ""
    image_type: str = ""
    quality: str = ""
    coordinates: str = ""
    browse_url: str = ""
    download_hint: str = ""
    footprint: tuple[tuple[float, float], ...] | None = None
    details: dict[str, str] = field(default_factory=dict)

    @property
    def year(self) -> str:
        return str(self.acquisition_date.year) if self.acquisition_date else MISSING

    @property
    def date_text(self) -> str:
        return self.acquisition_date.isoformat() if self.acquisition_date else MISSING


@dataclass(frozen=True)
class DownloadProduct:
    product_id: str
    entity_id: str
    name: str
    size: int | None
    state: str
    available: bool
    order_only: bool
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False)

    @property
    def size_text(self) -> str:
        return format_bytes(self.size)


@dataclass(frozen=True)
class SearchResult:
    frames: list[AerialFrame]
    total_hits: int
    capped: bool
    dataset_alias: str
    candidate_count: int = 0
    invalid_footprints: int = 0


# Pure helpers

NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"


def parse_finite_number(text: str, label: str, low: float, high: float) -> float:
    try:
        value = float(text.strip())
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number between {low:g} and {high:g}.") from exc
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{label} must be a finite number between {low:g} and {high:g}.")
    return value


def parse_radius(text: str, unit: str) -> float:
    value = parse_finite_number(text, "Radius", 0.01, 100.0)
    return value * 1.609344 if unit == "miles" else value


def parse_clipboard_coordinates(text: str) -> tuple[str, str]:
    """Return display-preserving latitude/longitude strings from one pair."""
    value = (text or "").strip()
    if not value:
        raise ValueError("The clipboard is empty.")
    if re.search(r"https?://|[°'\"]", value, re.IGNORECASE):
        raise ValueError("Clipboard text is not a supported decimal coordinate pair.")

    tags = re.findall(r"<coordinates\b[^>]*>(.*?)</coordinates>", value,
                      flags=re.IGNORECASE | re.DOTALL)
    if tags:
        if len(tags) != 1:
            raise ValueError("The clipboard contains multiple coordinate sets.")
        tuples = tags[0].strip().split()
        if len(tuples) != 1:
            raise ValueError("The KML contains multiple coordinate tuples.")
        match = re.fullmatch(rf"\s*({NUMBER})\s*,\s*({NUMBER})(?:\s*,\s*({NUMBER}))?\s*", tuples[0])
        if not match:
            raise ValueError("The KML coordinate tuple is malformed.")
        lon_text, lat_text = match.group(1), match.group(2)
    else:
        if "<coordinates" in value.lower() or "</coordinates>" in value.lower():
            raise ValueError("The KML coordinate element is malformed.")
        simple = re.fullmatch(rf"\s*({NUMBER})\s*(?:,|\s+)\s*({NUMBER})\s*", value)
        if not simple:
            # Allow harmless labels only when exactly one comma-separated pair exists.
            found = re.findall(rf"({NUMBER})\s*,\s*({NUMBER})", value)
            if len(found) != 1:
                raise ValueError("Expected one pair such as 37.123456, -93.654321.")
            remainder = re.sub(rf"{NUMBER}\s*,\s*{NUMBER}", "", value, count=1)
            if re.search(r"\d|<|>|/", remainder):
                raise ValueError("The clipboard contains ambiguous coordinate text.")
            lat_text, lon_text = found[0]
        else:
            lat_text, lon_text = simple.group(1), simple.group(2)

    parse_finite_number(lat_text, "Latitude", -90, 90)
    parse_finite_number(lon_text, "Longitude", -180, 180)
    return lat_text, lon_text


def coordinate_boxes(query: SearchQuery) -> list[tuple[float, float, float, float]]:
    """Build one or two (south, west, north, east) WGS84 MBRs."""
    lat_delta = query.radius_km / 111.32
    south = max(-90.0, query.latitude - lat_delta)
    north = min(90.0, query.latitude + lat_delta)
    cosine = abs(math.cos(math.radians(query.latitude)))
    if cosine < 1e-6 or south <= -90 or north >= 90:
        return [(south, -180.0, north, 180.0)]
    lon_delta = query.radius_km / (111.32 * cosine)
    if lon_delta >= 180:
        return [(south, -180.0, north, 180.0)]
    west, east = query.longitude - lon_delta, query.longitude + lon_delta
    if west < -180:
        return [(south, west + 360, north, 180.0), (south, -180.0, north, east)]
    if east > 180:
        return [(south, west, north, 180.0), (south, -180.0, north, east - 360)]
    return [(south, west, north, east)]


def parse_date(value: Any) -> dt.date | None:
    if not value:
        return None
    text = str(value).strip().replace("/", "-")
    for candidate in (text[:10], text):
        try:
            return dt.date.fromisoformat(candidate)
        except ValueError:
            pass
    for fmt in ("%Y-%m", "%Y", "%m-%d-%Y"):
        try:
            parsed = dt.datetime.strptime(text, fmt)
            return parsed.date()
        except ValueError:
            pass
    return None


def metadata_map(scene: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    raw = scene.get("metadata") or []
    if isinstance(raw, Mapping):
        raw = raw.items()
    for item in raw:
        if isinstance(item, tuple):
            key, value = item
        elif isinstance(item, Mapping):
            key = item.get("fieldName") or item.get("label") or item.get("name")
            value = item.get("value")
        else:
            continue
        if key and value not in (None, ""):
            result[str(key).strip()] = str(value).strip()
    return result


def _lookup(values: Mapping[str, str], *names: str) -> str:
    normalized = {re.sub(r"\W", "", key).lower(): value for key, value in values.items()}
    for name in names:
        value = normalized.get(re.sub(r"\W", "", name).lower())
        if value:
            return value
    return ""


def polygon_area(footprint: tuple[tuple[float, float], ...]) -> float:
    """Signed area of a footprint polygon via the shoelace formula."""
    total = 0.0
    count = len(footprint)
    for index in range(count):
        x1, y1 = footprint[index]
        x2, y2 = footprint[(index + 1) % count]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def point_in_footprint(
    latitude: float, longitude: float,
    footprint: tuple[tuple[float, float], ...] | None,
) -> bool:
    """Return True when (latitude, longitude) is inside or on the boundary
    of a valid four-corner footprint polygon."""
    if not footprint or len(footprint) != 4 or abs(polygon_area(footprint)) < 1e-12:
        return False
    point_x, point_y = longitude, latitude
    count = len(footprint)
    for index in range(count):
        x1, y1 = footprint[index]
        x2, y2 = footprint[(index + 1) % count]
        cross = (x2 - x1) * (point_y - y1) - (y2 - y1) * (point_x - x1)
        if (abs(cross) < 1e-9
                and min(x1, x2) - 1e-9 <= point_x <= max(x1, x2) + 1e-9
                and min(y1, y2) - 1e-9 <= point_y <= max(y1, y2) + 1e-9):
            return True
    inside = False
    for index in range(count):
        x1, y1 = footprint[index]
        x2, y2 = footprint[(index + 1) % count]
        if (y1 > point_y) != (y2 > point_y):
            intersect_x = x1 + (point_y - y1) * (x2 - x1) / (y2 - y1)
            if point_x < intersect_x:
                inside = not inside
    return inside


def _corner_label_matches(label: str, corner: str, axis: str) -> bool:
    lowered = label.casefold()
    compact = re.sub(r"[^a-z0-9]", "", lowered)
    corner_names = {
        "nw": ("northwest", "nw"), "ne": ("northeast", "ne"),
        "se": ("southeast", "se"), "sw": ("southwest", "sw"),
    }[corner]
    has_corner = corner_names[0] in compact or bool(
        re.search(rf"\b{corner_names[1]}\b", lowered)
    )
    if axis == "lat":
        has_axis = "latitude" in compact or bool(re.search(r"\blat\b", lowered))
    else:
        has_axis = any(word in compact for word in ("longitude", "long", "lon"))
    return has_corner and has_axis


def extract_frame_footprint(
    metadata: Mapping[str, str],
) -> tuple[tuple[float, float], ...] | None:
    """Extract the USGS frame's four corners (NW, NE, SE, SW) as a footprint
    polygon of (longitude, latitude) tuples, or None when incomplete/invalid."""
    corners: list[tuple[float, float]] = []
    for corner in ("nw", "ne", "se", "sw"):
        latitude: float | None = None
        longitude: float | None = None
        for label, raw_value in metadata.items():
            try:
                value = float(str(raw_value).strip())
            except (TypeError, ValueError):
                continue
            if _corner_label_matches(label, corner, "lat"):
                latitude = value
            elif _corner_label_matches(label, corner, "lon"):
                longitude = value
        if latitude is None or longitude is None:
            return None
        if not (math.isfinite(latitude) and math.isfinite(longitude)):
            return None
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return None
        corners.append((longitude, latitude))
    footprint = tuple(corners)
    if len(set(footprint)) < 4 or abs(polygon_area(footprint)) < 1e-12:
        return None
    return footprint


def normalize_scene(scene: Mapping[str, Any]) -> AerialFrame:
    meta = metadata_map(scene)
    entity_id = str(scene.get("entityId") or scene.get("entityID") or "").strip()
    display_id = str(scene.get("displayId") or scene.get("displayID") or entity_id).strip()
    date_value = scene.get("acquisitionDate") or _lookup(meta, "Acquisition Date", "Date Acquired")
    browse = ""
    for candidate in scene.get("browse") or scene.get("browseUrls") or []:
        if isinstance(candidate, str):
            browse = candidate
        elif isinstance(candidate, Mapping):
            browse = str(candidate.get("browsePath") or candidate.get("url") or "")
        if browse:
            break
    spatial = scene.get("spatialCoverage") or scene.get("spatialBounds") or ""
    return AerialFrame(
        entity_id=entity_id, display_id=display_id,
        acquisition_date=parse_date(date_value),
        agency=_lookup(meta, "Agency", "Source Agency"),
        project=_lookup(meta, "Project", "Project Name"),
        roll=_lookup(meta, "Roll Number", "Roll"),
        frame=_lookup(meta, "Frame Number", "Frame"),
        scale=_lookup(meta, "Scale", "Nominal Scale"),
        image_type=_lookup(meta, "Image Type", "Film Type"),
        quality=_lookup(meta, "Quality", "Image Quality"),
        coordinates=_lookup(meta, "Coordinates - Decimal Degrees", "Center Coordinate") or str(spatial or ""),
        browse_url=browse,
        download_hint=_lookup(meta, "High Resolution Download Available", "Download Available"),
        footprint=extract_frame_footprint(meta),
        details=meta,
    )


def frame_sort_key(frame: AerialFrame) -> tuple[Any, str]:
    return (frame.acquisition_date or dt.date.max, frame.display_id.casefold())


def match_aerial_dataset(datasets: Iterable[Mapping[str, Any]]) -> Mapping[str, Any]:
    exact: list[Mapping[str, Any]] = []
    plausible: list[Mapping[str, Any]] = []
    for dataset in datasets:
        text = " ".join(str(dataset.get(key) or "") for key in
                        ("collectionName", "datasetName", "datasetAlias", "fullName")).casefold()
        compact = re.sub(r"[^a-z0-9]", "", text)
        if "aerialphotosingleframes" in compact or "aerialphotographysingleframerecords" in compact:
            exact.append(dataset)
        elif "aerial" in text and "single" in text and "frame" in text:
            plausible.append(dataset)
    candidates = exact or plausible
    if not candidates:
        raise ApiError("API/schema", "USGS did not return the Aerial Photo Single Frames collection.")
    aliases = {str(item.get("datasetAlias") or item.get("datasetName") or "") for item in candidates}
    if len(candidates) != 1 and len(aliases) != 1:
        raise ApiError("API/schema", "USGS returned multiple possible aerial single-frame collections.")
    return candidates[0]


def classify_product(raw: Mapping[str, Any]) -> DownloadProduct:
    name = str(raw.get("productName") or raw.get("displayName") or raw.get("name") or "Unnamed product")
    available = bool(raw.get("available") or raw.get("bulkAvailable"))
    text = " ".join(str(raw.get(key) or "") for key in
                    ("productName", "downloadSystem", "downloadCode", "available", "bulkAvailable")).casefold()
    order_only = any(word in text for word in ("order", "purchase", "fee", "on-demand", "ondemand")) and not available
    state = "immediate" if available else ("order-only" if order_only else "unavailable")
    size_value = raw.get("filesize") or raw.get("fileSize") or raw.get("size")
    try:
        size = int(size_value) if size_value not in (None, "") else None
    except (TypeError, ValueError):
        size = None
    return DownloadProduct(str(raw.get("id") or raw.get("productId") or ""),
                           str(raw.get("entityId") or ""), name, size,
                           state, available, order_only, raw)


def product_rank(product: DownloadProduct) -> tuple[int, int, str]:
    name = product.name.casefold()
    resolution = 0
    if "1000" in name or "25 micron" in name or "high resolution" in name:
        resolution = 1000
    elif "400" in name or "63 micron" in name or "medium resolution" in name:
        resolution = 400
    availability = 2 if product.available and not product.order_only else 0
    return availability, resolution, product.name.casefold()


def best_product(products: Iterable[DownloadProduct]) -> DownloadProduct | None:
    eligible = [product for product in products if product.available and not product.order_only]
    return max(eligible, key=product_rank, default=None)


def sanitize_filename(name: str, fallback: str = "aerial_image.tif") -> str:
    value = Path(urllib.parse.unquote(name or "")).name
    value = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", value).strip(" .")
    if value in ("", ".", ".."):
        value = fallback
    return value[:200]


def format_bytes(size: int | None) -> str:
    if size is None:
        return "size unknown"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def redact(text: Any) -> str:
    value = str(text or "")
    value = re.sub(
        r"(?i)\b(token|api[_ -]?key|x-auth-token)\b(\s*(?:=|:)\s*['\"]?)[^\s,}\"]+",
        r"\1\2[REDACTED]",
        value,
    )
    value = re.sub(r"https://[^\s]+\?[^\s]+", "[REDACTED SIGNED URL]", value)
    return value


def tls_context() -> ssl.SSLContext:
    """Return a verifying TLS context with a bundled, current CA trust store."""
    return ssl.create_default_context(cafile=certifi.where())


def parse_envelope(payload: Any) -> Any:
    if not isinstance(payload, Mapping):
        raise ApiError("API/schema", "USGS returned an unexpected response.")
    error_code = payload.get("errorCode")
    error_message = payload.get("errorMessage")
    if error_code or error_message:
        message = str(error_message or "USGS rejected the request.")
        lowered = message.casefold()
        category = "Authentication/access" if any(x in lowered for x in ("auth", "token", "permission", "login")) else "API/schema"
        raise ApiError(category, message, f"Code: {error_code}")
    if "data" not in payload:
        raise ApiError("API/schema", "USGS returned a response without data.")
    return payload["data"]


# USGS M2M API client

Transport = Callable[[str, Mapping[str, Any], str | None], Any]


class UsgsM2MClient:
    def __init__(self, transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.api_key: str | None = None
        self.dataset_alias: str | None = None
        self._transport = transport or self._http_transport
        self._sleep = sleep

    def _http_transport(self, endpoint: str, payload: Mapping[str, Any], api_key: str | None) -> Any:
        encoded = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if api_key:
            headers["X-Auth-Token"] = api_key
        request = urllib.request.Request(API_BASE + endpoint, encoded, headers, method="POST")
        for attempt in range(RETRY_LIMIT):
            started = time.monotonic()
            LOG.info("USGS request: endpoint=%s attempt=%d/%d authenticated=%s",
                     endpoint, attempt + 1, RETRY_LIMIT, bool(api_key))
            try:
                with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT,
                                            context=tls_context()) as response:
                    body = response.read().decode("utf-8")
                    LOG.info("USGS response: endpoint=%s status=%s elapsed=%.2fs bytes=%d",
                             endpoint, getattr(response, "status", "unknown"),
                             time.monotonic() - started, len(body))
                    return json.loads(body)
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt + 1 < RETRY_LIMIT:
                    delay = float(exc.headers.get("Retry-After", 0) or 0) or (2 ** attempt + random.random())
                    LOG.warning("USGS transient HTTP error: endpoint=%s status=%d; retrying in %.1fs",
                                endpoint, exc.code, min(delay, 15))
                    self._sleep(min(delay, 15))
                    continue
                category = "Authentication/access" if exc.code in (401, 403) else "Network"
                LOG.error("USGS HTTP error: endpoint=%s status=%d elapsed=%.2fs",
                          endpoint, exc.code, time.monotonic() - started)
                raise ApiError(category, f"USGS request failed (HTTP {exc.code}).") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                reason = redact(getattr(exc, "reason", exc))
                if attempt + 1 < RETRY_LIMIT:
                    LOG.warning("USGS connection error: endpoint=%s type=%s reason=%s; retrying",
                                endpoint, type(exc).__name__, reason)
                    self._sleep(2 ** attempt + random.random())
                    continue
                LOG.error("USGS connection failed: endpoint=%s type=%s reason=%s elapsed=%.2fs",
                          endpoint, type(exc).__name__, reason,
                          time.monotonic() - started)
                raise ApiError("Network", "Could not reach the USGS service. Check the connection and try again.", str(exc)) from exc
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                LOG.error("USGS response decode failed: endpoint=%s type=%s",
                          endpoint, type(exc).__name__)
                raise ApiError("API/schema", "USGS returned an unreadable response.") from exc
        raise ApiError("Network", "USGS did not respond after several attempts.")

    def request(self, endpoint: str, payload: Mapping[str, Any], authenticated: bool = True) -> Any:
        if authenticated and not self.api_key:
            raise ApiError("Authentication/access", "Sign in before searching or downloading.")
        try:
            return parse_envelope(
                self._transport(endpoint, payload, self.api_key if authenticated else None)
            )
        except ApiError as exc:
            if authenticated and exc.category == "Authentication/access":
                self.api_key = None
            raise

    def login(self, username: str, application_token: str) -> None:
        if not username.strip() or not application_token:
            raise ApiError("Input", "Enter a USGS username and application token.")
        key = self.request("login-token", {"username": username.strip(), "token": application_token}, authenticated=False)
        if not isinstance(key, str) or not key:
            raise ApiError("Authentication/access", "USGS did not return a valid session key.")
        self.api_key = key
        LOG.info("USGS sign-in succeeded for the current session.")

    def logout(self, api_key: str | None = None) -> None:
        key = api_key or self.api_key
        if key:
            try:
                parse_envelope(self._transport("logout", {}, key))
            finally:
                if self.api_key == key:
                    self.api_key = None

    def discover_dataset(self) -> str:
        if self.dataset_alias:
            return self.dataset_alias
        data = self.request("dataset-search", {"datasetName": "Aerial Photo Single Frames"})
        if isinstance(data, Mapping):
            data = data.get("results") or data.get("datasets") or []
        dataset = match_aerial_dataset(data or [])
        alias = str(dataset.get("datasetAlias") or dataset.get("datasetName") or "")
        if not alias:
            raise ApiError("API/schema", "The aerial dataset did not include a usable alias.")
        self.dataset_alias = alias
        LOG.info("Discovered Aerial Photo Single Frames dataset alias: %s", alias)
        return alias

    def search(self, query: SearchQuery, cancel: threading.Event,
               cap: int = SEARCH_CAP) -> SearchResult:
        alias = self.discover_dataset()
        unique: dict[str, AerialFrame] = {}
        total_hits = 0
        capped = False
        for south, west, north, east in coordinate_boxes(query):
            start = 1
            while len(unique) < cap:
                if cancel.is_set():
                    raise ApiError("Cancelled", "Search cancelled.")
                page_limit = min(PAGE_SIZE, cap - len(unique))
                payload = {
                    "datasetName": alias, "maxResults": page_limit,
                    "startingNumber": start,
                    "sceneFilter": {"spatialFilter": {"filterType": "mbr",
                        "lowerLeft": {"latitude": south, "longitude": west},
                        "upperRight": {"latitude": north, "longitude": east}}},
                }
                data = self.request("scene-search", payload)
                if not isinstance(data, Mapping):
                    raise ApiError("API/schema", "USGS scene search returned unexpected data.")
                results = data.get("results") or []
                hits = int(data.get("totalHits") or len(results))
                total_hits += hits
                for raw in results:
                    frame = normalize_scene(raw)
                    if frame.entity_id:
                        unique[frame.entity_id] = frame
                returned = int(data.get("recordsReturned") or len(results))
                LOG.info("Scene page received: start=%d returned=%d total_hits=%d",
                         start, returned, hits)
                start += returned
                if returned <= 0 or start > hits:
                    break
            if len(unique) >= cap:
                capped = True
                break
        candidates = list(unique.values())
        covering = [
            frame for frame in candidates
            if point_in_footprint(query.latitude, query.longitude, frame.footprint)
        ]
        invalid_footprints = sum(1 for frame in candidates if not frame.footprint)
        frames = sorted(covering, key=frame_sort_key)
        LOG.info(
            "Footprint coverage filter: candidates=%d retained=%d invalid_geometry=%d",
            len(candidates), len(frames), invalid_footprints,
        )
        return SearchResult(
            frames, total_hits, capped or total_hits > len(candidates), alias,
            candidate_count=len(candidates), invalid_footprints=invalid_footprints,
        )

    def download_options(self, dataset: str, entity_id: str) -> list[DownloadProduct]:
        data = self.request("download-options", {"datasetName": dataset, "entityIds": [entity_id]})
        if isinstance(data, Mapping):
            data = data.get("results") or data.get("downloadOptions") or []
        products = [classify_product(item) for item in (data or []) if isinstance(item, Mapping)]
        LOG.info("Download options received: entity=%s products=%d", entity_id, len(products))
        return products

    def request_download_url(self, product: DownloadProduct, cancel: threading.Event) -> str:
        label = f"aerial-archive-{uuid.uuid4().hex[:12]}"
        payload = {"downloads": [{"entityId": product.entity_id,
                                  "productId": product.product_id}], "label": label}
        data = self.request("download-request", payload)
        url = _download_url(data)
        if url:
            return url
        for _ in range(POLL_LIMIT):
            if cancel.wait(POLL_INTERVAL):
                raise ApiError("Cancelled", "Download cancelled.")
            retrieved = self.request("download-retrieve", {"label": label})
            url = _download_url(retrieved)
            if url:
                return url
            if _download_failed(retrieved):
                break
        raise ApiError("Download preparation", "USGS did not prepare the image in time. Try again or open it in EarthExplorer.")


def _download_url(data: Any) -> str:
    if not isinstance(data, Mapping):
        return ""
    for key in ("availableDownloads", "available", "requested", "preparingDownloads"):
        for item in data.get(key) or []:
            if isinstance(item, Mapping) and item.get("url"):
                return str(item["url"])
    return str(data.get("url") or "")


def _download_failed(data: Any) -> bool:
    return isinstance(data, Mapping) and bool(data.get("failed") or data.get("duplicateDownloads"))


# Preview, download, and session cache

def fetch_bytes(url: str, cancel: threading.Event, limit: int = PREVIEW_LIMIT) -> bytes:
    if not url.lower().startswith("https://"):
        raise ApiError("Preview", "USGS supplied an invalid browse address.")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT,
                                    context=tls_context()) as response:
            total = int(response.headers.get("Content-Length") or 0)
            if total > limit:
                raise ApiError("Preview", "The browse image is too large for Quick Preview.")
            chunks: list[bytes] = []
            size = 0
            while True:
                if cancel.is_set():
                    raise ApiError("Cancelled", "Preview cancelled.")
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise ApiError("Preview", "The browse image is too large for Quick Preview.")
                chunks.append(chunk)
            return b"".join(chunks)
    except ApiError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ApiError("Preview", "The browse image could not be loaded.", str(exc)) from exc


def stream_download(url: str, destination: Path, cancel: threading.Event,
                    progress: Callable[[int, int | None], None] | None = None) -> Path:
    if not url.lower().startswith("https://"):
        raise ApiError("Network", "USGS supplied an invalid download address.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    if destination.exists():
        raise ApiError("Filesystem", "The destination file already exists.")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(
            request, timeout=REQUEST_TIMEOUT, context=tls_context()
        ) as response:
            LOG.info(
                "Download response: content_type=%s content_encoding=%s content_length=%s",
                response.headers.get("Content-Type", "unknown"),
                response.headers.get("Content-Encoding", "none"),
                response.headers.get("Content-Length", "unknown"),
            )
            with partial.open("xb") as output:
                total_value = response.headers.get("Content-Length")
                total = int(total_value) if total_value else None
                received = 0
                last_update = 0.0
                while True:
                    if cancel.is_set():
                        raise ApiError("Cancelled", "Download cancelled.")
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    received += len(chunk)
                    now = time.monotonic()
                    if progress and now - last_update >= 0.1:
                        progress(received, total)
                        last_update = now
                output.flush()
                os.fsync(output.fileno())
        partial.replace(destination)
        if progress:
            progress(destination.stat().st_size, total)
        return destination
    except ApiError:
        partial.unlink(missing_ok=True)
        raise
    except FileExistsError as exc:
        raise ApiError("Filesystem", "A partial download already exists at the destination.") from exc
    except OSError as exc:
        partial.unlink(missing_ok=True)
        message = "Could not write the image. Check free space and folder permissions."
        raise ApiError("Filesystem", message, str(exc)) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        partial.unlink(missing_ok=True)
        raise ApiError("Network", "The image transfer failed. Existing search results are unchanged.", str(exc)) from exc


def identify_image_suffix(path: Path) -> str:
    """Validate an image and return a conventional filename suffix."""
    try:
        with Image.open(path) as image:
            image.verify()
            image_format = (image.format or "").upper()
    except (OSError, UnidentifiedImageError) as exc:
        raise ApiError(
            "Download preparation",
            "The downloaded product is not a supported aerial image.",
            str(exc),
        ) from exc
    return {
        "TIFF": ".tif", "JPEG": ".jpg", "PNG": ".png",
        "BMP": ".bmp", "WEBP": ".webp",
    }.get(image_format, f".{image_format.lower()}" if image_format else ".img")


def prepare_viewable_image(path: Path, cancel: threading.Event) -> Path:
    """Expand a gzip-wrapped USGS scan and return a validated image path."""
    try:
        with path.open("rb") as source:
            magic = source.read(4)
    except OSError as exc:
        raise ApiError("Filesystem", "The downloaded image could not be inspected.", str(exc)) from exc

    if magic[:2] != b"\x1f\x8b":
        suffix = identify_image_suffix(path)
        if path.suffix.casefold() == suffix:
            return path
        renamed = path.with_name(path.name + suffix)
        if renamed.exists():
            raise ApiError("Filesystem", "A prepared image already exists in the session cache.")
        try:
            path.replace(renamed)
        except OSError as exc:
            raise ApiError(
                "Filesystem", "The prepared image could not be renamed.", str(exc)
            ) from exc
        LOG.info("Validated downloaded image: format=%s bytes=%d", suffix, renamed.stat().st_size)
        return renamed

    try:
        with path.open("rb") as source:
            source.seek(-4, os.SEEK_END)
            expected_size = int.from_bytes(source.read(4), "little")
    except OSError as exc:
        raise ApiError("Download preparation", "The gzip image size could not be read.", str(exc)) from exc
    if expected_size <= 0 or expected_size > MAX_UNPACKED_IMAGE:
        raise ApiError(
            "Download preparation",
            "The compressed image reports an unsupported expanded size.",
        )
    free_space = shutil.disk_usage(path.parent).free
    if free_space < expected_size + 64 * 1024 * 1024:
        raise ApiError(
            "Filesystem",
            f"The image needs about {format_bytes(expected_size)} to unpack, but there is not enough free space.",
        )

    unpacked = path.with_name(path.name + ".unpacked")
    partial = unpacked.with_name(unpacked.name + ".part")
    written = 0
    LOG.info("Preparing gzip-compressed image: compressed=%d expected_unpacked=%d",
             path.stat().st_size, expected_size)
    try:
        with gzip.open(path, "rb") as source, partial.open("xb") as output:
            while True:
                if cancel.is_set():
                    raise ApiError("Cancelled", "Image preparation cancelled.")
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UNPACKED_IMAGE:
                    raise ApiError(
                        "Download preparation",
                        "The expanded image exceeded the safe session limit.",
                    )
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        suffix = identify_image_suffix(partial)
        final = path.with_name(path.name + suffix)
        if final.exists():
            raise ApiError("Filesystem", "A prepared image already exists in the session cache.")
        partial.replace(final)
        path.unlink()
        LOG.info("Prepared compressed image: format=%s bytes=%d", suffix, written)
        return final
    except ApiError:
        partial.unlink(missing_ok=True)
        raise
    except (gzip.BadGzipFile, EOFError, OSError) as exc:
        partial.unlink(missing_ok=True)
        raise ApiError(
            "Download preparation",
            "The compressed USGS image could not be unpacked.",
            str(exc),
        ) from exc


def build_metadata_block(frame: AerialFrame) -> str:
    """Render a fixed-format USGS identity/footprint block for embedding in
    a saved TIFF's ImageDescription tag (270).

    Includes the raw four-corner footprint and center point flat as
    decimal-degree longitude/latitude pairs -- not just the combined
    "Coordinates / footprint" summary -- plus the archival identity
    fields (entity ID, acquisition date, project/roll/frame, scale) a
    researcher needs to trace the source. A downstream GDAL/rasterio
    tool converting this into a GeoTIFF or KMZ needs the explicit CRS
    and corner order stated (both included below) and should not assume
    the raster's pixel orientation matches these corners without
    checking -- USGS does not record the scan's physical orientation.
    """
    corners = frame.footprint  # (NW, NE, SE, SW), each (longitude, latitude)
    corner_labels = ("NW_CORNER", "NE_CORNER", "SE_CORNER", "SW_CORNER")
    if corners:
        corner_lines = [
            f"{label}: {longitude:.6f}, {latitude:.6f}"
            for label, (longitude, latitude) in zip(corner_labels, corners, strict=True)
        ]
        center_lon = sum(longitude for longitude, _ in corners) / len(corners)
        center_lat = sum(latitude for _, latitude in corners) / len(corners)
        center_line = f"CENTER: {center_lon:.6f}, {center_lat:.6f}"
    else:
        corner_lines = [f"{label}: unknown" for label in corner_labels]
        center_line = "CENTER: unknown"
    acquisition = frame.acquisition_date.isoformat() if frame.acquisition_date else "unknown"
    lines = [
        "---USGS HISTORICAL METADATA---",
        f"ENTITY_ID: {frame.entity_id or 'unknown'}",
        f"DISPLAY_ID: {frame.display_id or 'unknown'}",
        f"ACQUISITION_DATE: {acquisition}",
        f"AGENCY: {frame.agency or 'unknown'}",
        f"PROJECT: {frame.project or 'unknown'}",
        f"ROLL: {frame.roll or 'unknown'}",
        f"FRAME: {frame.frame or 'unknown'}",
        f"SCALE: {frame.scale or 'unknown'}",
        f"IMAGE_TYPE: {frame.image_type or 'unknown'}",
        f"QUALITY: {frame.quality or 'unknown'}",
        *corner_lines,
        center_line,
        "CORNER_ORDER: NW, NE, SE, SW (longitude, latitude)",
        "SOURCE_CRS: EPSG:4326 (WGS 84)",
        "NOTE: Footprint corners are USGS's nominal photographed-ground-area "
        "coordinates, not a verified pixel-to-ground mapping. Confirm scan "
        "orientation (rotation/flip) before using these corners as GCPs.",
        f"SOURCE: {APP_NAME}",
        "------------------------------",
    ]
    return "\n".join(lines)


def embed_tiff_metadata(
    source: Path, destination: Path, frame: AerialFrame, cancel: threading.Event,
) -> Path:
    """Write ``source`` to ``destination`` with the USGS metadata block
    embedded in the TIFF ImageDescription tag. Raises ApiError (without
    touching ``source``) if the image cannot be decoded/re-encoded; the
    caller should fall back to a plain byte copy rather than fail the
    whole save over a missing metadata tag."""
    if cancel.is_set():
        raise ApiError("Cancelled", "Save cancelled.")
    partial = destination.with_name(destination.name + ".part")
    try:
        with Image.open(source) as image:
            image.load()
            if cancel.is_set():
                raise ApiError("Cancelled", "Save cancelled.")
            save_kwargs: dict[str, Any] = {}
            compression = image.info.get("compression")
            if compression and compression != "raw":
                save_kwargs["compression"] = compression
            image.save(
                partial, format="TIFF",
                tiffinfo={270: build_metadata_block(frame)},
                **save_kwargs,
            )
        partial.replace(destination)
        LOG.info("Embedded USGS metadata in saved TIFF: entity=%s path=%s",
                 frame.entity_id, destination.name)
        return destination
    except ApiError:
        partial.unlink(missing_ok=True)
        raise
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        partial.unlink(missing_ok=True)
        raise ApiError(
            "Filesystem",
            "The USGS metadata could not be embedded in the saved TIFF.",
            str(exc),
        ) from exc


class ViewerCache:
    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="aerial-archive-")
        self.root = Path(self._temporary.name)
        self._paths: dict[tuple[str, str, str], Path] = {}
        self._saved: dict[tuple[str, str, str], Path] = {}

    @staticmethod
    def key(dataset: str, entity: str, product: str) -> tuple[str, str, str]:
        return dataset, entity, product

    def get(self, key: tuple[str, str, str]) -> Path | None:
        saved = self._saved.get(key)
        if saved and saved.exists():
            return saved
        cached = self._paths.get(key)
        return cached if cached and cached.exists() else None

    def destination(self, key: tuple[str, str, str], name: str) -> Path:
        digest = uuid.uuid5(uuid.NAMESPACE_URL, "|".join(key)).hex[:12]
        return self.root / f"{digest}_{sanitize_filename(name)}"

    def record_cache(self, key: tuple[str, str, str], path: Path) -> None:
        self._paths[key] = path

    def record_saved(self, key: tuple[str, str, str], path: Path) -> None:
        self._saved[key] = path

    def copy_to(self, key: tuple[str, str, str], destination: Path) -> Path:
        source = self.get(key)
        if not source:
            raise ApiError("Filesystem", "The cached image is no longer available.")
        if destination.exists():
            raise ApiError("Filesystem", "The destination file already exists.")
        temp = destination.with_name(destination.name + ".part")
        try:
            shutil.copy2(source, temp)
            temp.replace(destination)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise ApiError("Filesystem", "Could not save the cached image.", str(exc)) from exc
        self.record_saved(key, destination)
        return destination

    def close(self) -> None:
        self._temporary.cleanup()


# Interactive viewer

class ImageViewer:
    def __init__(self, parent: tk.Misc, image_path: Path, frame: AerialFrame,
                 product: DownloadProduct, cache: ViewerCache,
                 cache_key: tuple[str, str, str], browse_quality: bool = False) -> None:
        self.window = tk.Toplevel(parent)
        label = f"{frame.year} {frame.display_id}".strip()
        self.window.title(f"{APP_NAME} — Viewer — {label}")
        self.window.geometry("1100x760")
        self.window.minsize(650, 450)
        self.frame, self.path, self.product = frame, image_path, product
        self.cache, self.cache_key = cache, cache_key
        self._source_image = Image.open(image_path)
        self._source_image.load()
        self.rotation = 0  # degrees counter-clockwise applied for display: 0/90/180/270
        self.image = self._source_image
        self.photo: ImageTk.PhotoImage | None = None
        self.scale, self.min_scale, self.max_scale = 1.0, 0.01, 10.0
        self.offset_x = self.offset_y = 0.0
        self._pan: tuple[int, int] | None = None
        self._redraw_id: str | None = None
        self._quality_id: str | None = None
        self._save_cancel: threading.Event | None = None

        bar = ttk.Frame(self.window, padding=(8, 6))
        bar.pack(fill=tk.X)
        prefix = "Browse quality — " if browse_quality else ""
        ttk.Label(bar, text=f"{prefix}{product.name} • {product.size_text}").pack(side=tk.LEFT, padx=(0, 12))
        self.save_button: ttk.Button | None = None
        for text, command in (("Zoom +", lambda: self.zoom_center(1.2)),
                              ("Zoom −", lambda: self.zoom_center(1 / 1.2)),
                              ("Fit to Window", self.fit),
                              ("Rotate Left 90°", lambda: self.rotate(90)),
                              ("Rotate 180°", lambda: self.rotate(180)),
                              ("Rotate Right 90°", lambda: self.rotate(-90)),
                              ("Save Image", self.save),
                              ("Close", self.close)):
            button = ttk.Button(bar, text=text, command=command)
            if text == "Save Image":
                self.save_button = button
                if browse_quality:
                    button.configure(state=tk.DISABLED)
            button.pack(side=tk.LEFT, padx=3)
        self.zoom_var = tk.StringVar(value="100%")
        ttk.Label(bar, textvariable=self.zoom_var).pack(side=tk.RIGHT)
        self.canvas = tk.Canvas(self.window, bg="#202124", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        ttk.Label(self.window, text="Drag to pan. Wheel to zoom.  +/− zoom • 0 fits • Esc closes",
                  anchor=tk.CENTER).pack(fill=tk.X, pady=(3, 6))
        self.canvas.bind("<Configure>", self._configure)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Button-4>", lambda event: self.zoom_at(event.x, event.y, 1.12))
        self.canvas.bind("<Button-5>", lambda event: self.zoom_at(event.x, event.y, 1 / 1.12))
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.window.bind("+", lambda event: self.zoom_center(1.2))
        self.window.bind("=", lambda event: self.zoom_center(1.2))
        self.window.bind("-", lambda event: self.zoom_center(1 / 1.2))
        self.window.bind("0", lambda event: self.fit())
        self.window.bind("<Escape>", lambda event: self.close())
        self.window.bind("<FocusOut>", self._cancel_pan)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.after(80, self.fit)

    def fit(self) -> None:
        width, height = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        iw, ih = self.image.size
        self.scale = min(1.0, width / iw, height / ih) * 0.96
        self.min_scale = max(0.005, self.scale / 4)
        self.offset_x = (width - iw * self.scale) / 2
        self.offset_y = (height - ih * self.scale) / 2
        self.redraw()

    def rotate(self, delta: int) -> None:
        """Rotate the displayed image by ``delta`` degrees (positive =
        counter-clockwise "left", negative = clockwise "right").

        View-only: re-derived from the untouched source image each time
        (so repeated rotation never drifts or loses quality), and does
        not affect what Save Image writes -- that always saves the
        original downloaded bytes, matching every other saved output in
        this app.
        """
        self.rotation = (self.rotation + delta) % 360
        transpose = {
            90: Image.Transpose.ROTATE_90,
            180: Image.Transpose.ROTATE_180,
            270: Image.Transpose.ROTATE_270,
        }.get(self.rotation)
        self.image = self._source_image if transpose is None else self._source_image.transpose(transpose)
        self.fit()

    def zoom_center(self, factor: float) -> None:
        self.zoom_at(self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2, factor)

    def zoom_at(self, x: float, y: float, factor: float) -> None:
        old = self.scale
        new = max(self.min_scale, min(self.max_scale, old * factor))
        if abs(new - old) < 1e-12:
            return
        source_x, source_y = (x - self.offset_x) / old, (y - self.offset_y) / old
        self.scale = new
        self.offset_x, self.offset_y = x - source_x * new, y - source_y * new
        self.schedule_redraw()

    def _wheel(self, event: tk.Event) -> None:
        if event.delta:
            self.zoom_at(event.x, event.y, 1.12 if event.delta > 0 else 1 / 1.12)

    def _press(self, event: tk.Event) -> None:
        self._pan = event.x, event.y
        self.canvas.configure(cursor="fleur")

    def _drag(self, event: tk.Event) -> None:
        if self._pan:
            dx, dy = event.x - self._pan[0], event.y - self._pan[1]
            self.offset_x += dx
            self.offset_y += dy
            self._pan = event.x, event.y
            self.canvas.move("image", dx, dy)
            self.schedule_redraw(30)

    def _release(self, _event: tk.Event | None = None) -> None:
        self._cancel_pan()
        self.redraw(Image.Resampling.LANCZOS)

    def _cancel_pan(self, _event: tk.Event | None = None) -> None:
        self._pan = None
        if self.canvas.winfo_exists():
            self.canvas.configure(cursor="")

    def _configure(self, _event: tk.Event) -> None:
        self.schedule_redraw(80)

    def schedule_redraw(self, delay: int = 16) -> None:
        if self._redraw_id is None:
            self._redraw_id = self.window.after(delay, self._interactive_redraw)
        if self._quality_id:
            self.window.after_cancel(self._quality_id)
        self._quality_id = self.window.after(160, self._quality_redraw)

    def _interactive_redraw(self) -> None:
        self._redraw_id = None
        self.redraw(Image.Resampling.BILINEAR)

    def _quality_redraw(self) -> None:
        self._quality_id = None
        if not self._pan:
            self.redraw(Image.Resampling.LANCZOS)

    def redraw(self, resample: Image.Resampling = Image.Resampling.BILINEAR) -> None:
        if not self.window.winfo_exists():
            return
        self.canvas.delete("image")
        iw, ih = self.image.size
        cw, ch = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        left = max(0, math.floor(-self.offset_x / self.scale) - 1)
        top = max(0, math.floor(-self.offset_y / self.scale) - 1)
        right = min(iw, math.ceil((cw - self.offset_x) / self.scale) + 1)
        bottom = min(ih, math.ceil((ch - self.offset_y) / self.scale) + 1)
        if right <= left or bottom <= top:
            self.photo = None
            return
        width = max(1, round((right - left) * self.scale))
        height = max(1, round((bottom - top) * self.scale))
        visible = self.image.resize((width, height), resample=resample,
                                    box=(left, top, right, bottom))
        self.photo = ImageTk.PhotoImage(visible)
        self.canvas.create_image(self.offset_x + left * self.scale,
                                 self.offset_y + top * self.scale,
                                 image=self.photo, anchor="nw", tags="image")
        self.zoom_var.set(f"{self.scale * 100:.0f}%")

    def save(self) -> None:
        filename = sanitize_filename(f"{self.frame.entity_id}.tif")
        selected = filedialog.asksaveasfilename(
            parent=self.window, initialfile=filename, defaultextension=".tif",
            filetypes=(("TIFF image", "*.tif"), ("All files", "*.*")),
        )
        if not selected:
            return
        destination = Path(selected)
        if destination.exists() and not messagebox.askyesno(APP_NAME, "Replace the existing file?", parent=self.window):
            return
        if destination.exists():
            destination.unlink()
        if self.save_button:
            self.save_button.configure(state=tk.DISABLED)
        self._save_cancel = threading.Event()
        threading.Thread(
            target=self._save_worker, args=(destination, self._save_cancel), daemon=True,
        ).start()

    def _save_worker(self, destination: Path, cancel: threading.Event) -> None:
        embedded = True
        error: ApiError | None = None
        try:
            cached = self.cache.get(self.cache_key)
            if not cached:
                raise ApiError("Filesystem", "The cached image is no longer available.")
            try:
                embed_tiff_metadata(cached, destination, self.frame, cancel)
                self.cache.record_saved(self.cache_key, destination)
            except ApiError as exc:
                if exc.category == "Cancelled":
                    raise
                embedded = False
                LOG.warning(
                    "Metadata embedding failed; saving original TIFF unchanged: %s",
                    exc.message,
                )
                self.cache.copy_to(self.cache_key, destination)
        except ApiError as exc:
            error = exc
        except (OSError, ValueError) as exc:
            error = ApiError("Filesystem", "The image could not be saved.", str(exc))
        if self.window.winfo_exists():
            self.window.after(0, self._save_finished, destination, embedded, error)

    def _save_finished(self, destination: Path, embedded: bool, error: ApiError | None) -> None:
        if self.save_button and self.save_button.winfo_exists():
            self.save_button.configure(state=tk.NORMAL)
        if error:
            if error.category != "Cancelled":
                messagebox.showerror(error.category, error.message, parent=self.window)
            return
        suffix = "" if embedded else "\n\n(USGS metadata could not be embedded; original TIFF saved unchanged.)"
        messagebox.showinfo(APP_NAME, f"Image saved to:\n{destination}{suffix}", parent=self.window)

    def close(self) -> None:
        if self._redraw_id:
            self.window.after_cancel(self._redraw_id)
        if self._quality_id:
            self.window.after_cancel(self._quality_id)
        if self._save_cancel:
            self._save_cancel.set()
        self.photo = None
        self._source_image.close()
        self.window.destroy()


# Tkinter application controller

class AerialArchiveExplorerApp:
    def __init__(self, root: tk.Tk, client: UsgsM2MClient | None = None,
                 credential_store: CredentialStore | None = None,
                 local_credential_store: LocalTokenStore | None = None) -> None:
        self.root = root
        self.diagnostics = DiagnosticsBuffer()
        LOG.setLevel(logging.INFO)
        LOG.addHandler(self.diagnostics)
        self.log_window: tk.Toplevel | None = None
        self.log_text: tk.Text | None = None
        self._log_refresh_id: str | None = None
        self.client = client or UsgsM2MClient()
        self.credential_store = credential_store or CredentialStore()
        self.local_credential_store = local_credential_store or LocalTokenStore()
        self.username_value = ""
        self.token_value = ""
        self.access_window: tk.Toplevel | None = None
        self.cache = ViewerCache()
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.cancel = threading.Event()
        self.generation = 0
        self.dataset_alias = ""
        self.frames: list[AerialFrame] = []
        self.products: list[DownloadProduct] = []
        self.selected: AerialFrame | None = None
        self._sort_column, self._sort_reverse = "date", False
        self._busy = False
        self._build()
        LOG.info("%s started: version=%s Python=%s OS=%s frozen=%s",
                 APP_NAME, APP_VERSION, platform.python_version(), platform.platform(),
                 bool(getattr(sys, "frozen", False)))
        LOG.info("API base: %s", API_BASE)
        self.root.after(80, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after_idle(self._initialize_access)

    def _build(self) -> None:
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self._set_window_icon()
        self.root.geometry("1280x820")
        self.root.minsize(980, 650)
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=3)
        outer.rowconfigure(3, weight=2)
        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text=f"{APP_NAME}  v{APP_VERSION}",
                  font=("TkDefaultFont", 18, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text=APP_SUBTITLE).grid(row=0, column=1, padx=18)
        ttk.Button(header, text="Sign Out", command=self.sign_out).grid(
            row=0, column=2, sticky="e"
        )

        form = ttk.LabelFrame(outer, text="Search location", padding=10)
        form.grid(row=1, column=0, sticky="ew", pady=(8, 8))
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)
        form.columnconfigure(8, weight=1)
        self.latitude = tk.StringVar()
        self.longitude = tk.StringVar()
        self.radius = tk.StringVar(value=DEFAULT_RADIUS)
        self.unit = tk.StringVar(value="miles")
        labels = (("Latitude", self.latitude, 0), ("Longitude", self.longitude, 2))
        self.entries: dict[str, ttk.Entry] = {}
        for text, variable, column in labels:
            ttk.Label(form, text=text).grid(row=0, column=column, padx=(4, 3), sticky="e")
            entry = ttk.Entry(form, textvariable=variable, width=22)
            entry.grid(row=0, column=column + 1, padx=(0, 8), sticky="ew")
            entry.bind("<Return>", lambda _event: self.search())
            self.entries[text] = entry
        ttk.Label(form, text="Search radius").grid(row=0, column=4, sticky="e")
        ttk.Entry(form, textvariable=self.radius, width=8).grid(row=0, column=5, padx=(3, 3))
        ttk.Combobox(form, textvariable=self.unit, values=("miles", "kilometers"),
                     state="readonly", width=11).grid(row=0, column=6, padx=(0, 8))
        self.search_button = ttk.Button(form, text="Search USGS", command=self.search)
        self.search_button.grid(row=0, column=7, padx=(0, 8))
        self.paste_button = ttk.Button(
            form, text="Paste Coordinates", underline=0,
            command=self.paste_coordinates,
        )
        self.paste_button.grid(row=1, column=0, columnspan=4, pady=(8, 2))
        self.root.bind("<Alt-p>", lambda _event: self.paste_coordinates())
        ttk.Label(form, text="Radius is a catalog tolerance, not a guarantee that the point appears in the scan.",
                  foreground="#555555").grid(row=2, column=0, columnspan=9, pady=(7, 0))

        results = ttk.LabelFrame(outer, text="Aerial frames covering this coordinate", padding=6)
        results.grid(row=2, column=0, sticky="nsew")
        results.rowconfigure(0, weight=1)
        results.columnconfigure(0, weight=1)
        ids = [item[0] for item in COLUMNS]
        self.tree = ttk.Treeview(results, columns=ids, show="headings", selectmode="browse")
        for key, title, width in COLUMNS:
            self.tree.heading(key, text=title, command=lambda value=key: self.sort(value))
            self.tree.column(key, width=width, minwidth=50, stretch=key in ("agency", "project", "display_id"))
        yscroll = ttk.Scrollbar(results, orient=tk.VERTICAL, command=self.tree.yview)
        xscroll = ttk.Scrollbar(results, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<<TreeviewSelect>>", self.select_frame)
        self.count_var = tk.StringVar(value="No search yet.")
        ttk.Label(results, textvariable=self.count_var).grid(row=2, column=0, sticky="w", pady=(4, 0))

        lower = ttk.Panedwindow(outer, orient=tk.HORIZONTAL)
        lower.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        details_frame = ttk.LabelFrame(lower, text="Selected frame details", padding=7)
        actions_frame = ttk.LabelFrame(lower, text="View & download", padding=7)
        lower.add(details_frame, weight=3)
        lower.add(actions_frame, weight=2)
        self.details = tk.Text(details_frame, height=9, wrap=tk.WORD, state=tk.DISABLED)
        self.details.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            actions_frame,
            text="Every listed frame's footprint covers the searched coordinate.",
            foreground="#555555", wraplength=430, justify=tk.LEFT,
        ).pack(fill=tk.X, anchor=tk.W)
        self.product_var = tk.StringVar(value="Select a frame to check download products.")
        ttk.Label(actions_frame, textvariable=self.product_var, wraplength=430, justify=tk.LEFT).pack(
            fill=tk.X, anchor=tk.W, pady=(6, 10),
        )
        actions = ttk.Frame(actions_frame)
        actions.pack(fill=tk.X)
        self.viewer_button = ttk.Button(actions, text="View Aerial", command=self.open_best, state=tk.DISABLED)
        self.save_button = ttk.Button(actions, text="Download", command=self.save_as, state=tk.DISABLED)
        for button in (self.viewer_button, self.save_button):
            button.pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(actions, text="Open in EarthExplorer", command=lambda: webbrowser.open(EARTH_EXPLORER_URL)).pack(side=tk.RIGHT)
        self._build_donate_section(actions_frame)

        status = ttk.Frame(outer)
        status.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        status.columnconfigure(1, weight=1)
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(status, textvariable=self.status_var, width=28).grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(status, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=1, sticky="ew", padx=8)
        self.cancel_button = ttk.Button(status, text="Cancel", command=self.cancel.set, state=tk.DISABLED)
        self.cancel_button.grid(row=0, column=2)
        ttk.Button(status, text="View Logs", command=self.show_logs).grid(
            row=0, column=3, padx=(8, 0)
        )

    def _build_donate_section(self, parent: tk.Misc) -> None:
        """A centered, optional PayPal donation prompt below the action
        buttons. Plain tk.Frame/tk.Label (not ttk.Button) so the PayPal
        blue background renders reliably on macOS."""
        donate = ttk.Frame(parent)
        donate.pack(fill=tk.X, pady=(14, 0))
        ttk.Label(
            donate,
            text="If you enjoy this application, please consider donating!",
            justify=tk.CENTER, anchor=tk.CENTER,
        ).pack(fill=tk.X)
        button = tk.Frame(donate, bg="#0070BA", bd=1, relief="raised", cursor="hand2")
        button.pack(pady=(8, 0))
        label = tk.Label(
            button, text="Donate on PayPal", bg="#0070BA", fg="white",
            font=("TkDefaultFont", 10, "bold"), padx=20, pady=8,
        )
        label.pack()

        def open_donate(_event: tk.Event | None = None) -> None:
            webbrowser.open(DONATE_URL)

        def on_enter(_event: tk.Event | None = None) -> None:
            button.configure(bg="#005EA6")
            label.configure(bg="#005EA6")

        def on_leave(_event: tk.Event | None = None) -> None:
            button.configure(bg="#0070BA")
            label.configure(bg="#0070BA")

        for widget in (button, label):
            widget.bind("<Button-1>", open_donate)
            widget.bind("<Enter>", on_enter)
            widget.bind("<Leave>", on_leave)

    def _initialize_access(self) -> None:
        saved = None
        source = ""
        try:
            saved = self.credential_store.load()
            source = "the operating-system credential store"
        except ApiError as exc:
            LOG.error("Credential-store read failed: %s", exc.detail or exc.message)
            messagebox.showwarning(
                exc.category,
                f"{exc.message}\n\nYou can continue without saved access.",
                parent=self.root,
            )
        if not saved:
            try:
                saved = self.local_credential_store.load()
                source = "a local file on this machine"
            except ApiError as exc:
                LOG.error("Local credential file read failed: %s", exc.detail or exc.message)
        if saved:
            self.username_value, self.token_value = saved
            LOG.info("Loaded saved M2M access from %s.", source)
            self.root.deiconify()
            self.root.lift()
        else:
            self.show_access_prompt(hide_main=True)

    def show_access_prompt(self, hide_main: bool = False) -> None:
        if self.access_window and self.access_window.winfo_exists():
            self.access_window.lift()
            self.access_window.focus_force()
            return
        window = tk.Toplevel(self.root)
        window.title(f"{APP_NAME} — USGS Access")
        window.resizable(False, False)
        window.transient(self.root)
        frame = ttk.Frame(window, padding=22)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text="Connect to USGS M2M",
                  font=("TkDefaultFont", 15, "bold")).grid(
            row=0, column=0, columnspan=2, pady=(0, 8)
        )
        ttk.Label(
            frame,
            text=("Enter your USGS username and application token. By default they "
                  "are saved in the operating-system credential store, not in a file."),
            wraplength=440, justify=tk.LEFT,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 14))
        username_var = tk.StringVar(value=self.username_value)
        token_var = tk.StringVar()
        ttk.Label(frame, text="Username").grid(row=2, column=0, sticky="e", padx=(0, 8), pady=5)
        username_entry = ttk.Entry(frame, textvariable=username_var, width=38)
        username_entry.grid(row=2, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="Application token").grid(row=3, column=0, sticky="e", padx=(0, 8), pady=5)
        token_entry = ttk.Entry(frame, textvariable=token_var, show="•", width=38)
        token_entry.grid(row=3, column=1, sticky="ew", pady=5)
        local_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame,
            text="Save in a local file on this machine instead of the OS keychain (lower security)",
            variable=local_var,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(
            frame,
            text=("Only choose this on a personal, non-shared computer. The file is not "
                  "encrypted; it relies on this OS account's normal file permissions."),
            wraplength=440, justify=tk.LEFT, foreground="#555555",
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(2, 14))
        ttk.Button(
            frame, text="M2M Access / Token Help",
            command=lambda: webbrowser.open(M2M_ACCESS_URL),
        ).grid(row=6, column=0, columnspan=2, pady=(0, 14))
        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=2, sticky="ew")
        ttk.Button(
            buttons, text="Skip for Now",
            command=self._skip_access,
        ).pack(side=tk.LEFT)
        ttk.Button(
            buttons, text="Save & Continue",
            command=lambda: self._save_access(
                username_var.get(), token_var.get(), local_var.get(),
                username_entry, token_entry,
            ),
        ).pack(side=tk.RIGHT)
        window.bind(
            "<Return>",
            lambda _event: self._save_access(
                username_var.get(), token_var.get(), local_var.get(),
                username_entry, token_entry,
            ),
        )
        window.bind("<Escape>", lambda _event: self._skip_access())
        window.protocol("WM_DELETE_WINDOW", self._skip_access)
        window.update_idletasks()
        width, height = window.winfo_reqwidth(), window.winfo_reqheight()
        x = (window.winfo_screenwidth() - width) // 2
        y = (window.winfo_screenheight() - height) // 3
        window.geometry(f"+{x}+{y}")
        self.access_window = window
        window.grab_set()
        username_entry.focus_set()

    def _save_access(self, username: str, token: str, use_local: bool,
                     username_entry: ttk.Entry, token_entry: ttk.Entry) -> None:
        username = username.strip()
        if not username:
            messagebox.showerror("USGS Access", "Enter your USGS username.",
                                 parent=self.access_window)
            username_entry.focus_set()
            return
        if not token:
            messagebox.showerror("USGS Access", "Enter your M2M application token.",
                                 parent=self.access_window)
            token_entry.focus_set()
            return
        store = self.local_credential_store if use_local else self.credential_store
        destination = "a local file on this machine" if use_local else "the operating-system credential store"
        try:
            store.save(username, token)
        except ApiError as exc:
            LOG.error("Credential-store save failed: %s", exc.detail or exc.message)
            messagebox.showerror(exc.category, exc.message, parent=self.access_window)
            return
        # Only one store should hold the credential at a time so sign-out and
        # a later reload behave predictably; clear the store not chosen this
        # time, best-effort, without failing the save over it.
        other = self.credential_store if use_local else self.local_credential_store
        try:
            other.clear()
        except ApiError as exc:
            LOG.warning("Could not clear the unused credential store: %s", exc.detail or exc.message)
        self.username_value, self.token_value = username, token
        LOG.info("M2M access saved in %s.", destination)
        self._finish_access_prompt("Saved access loaded.")

    def _skip_access(self) -> None:
        self.username_value = ""
        self.token_value = ""
        LOG.info("USGS access prompt skipped; app opened in design/troubleshooting mode.")
        self._finish_access_prompt("Opened without USGS access.")

    def _finish_access_prompt(self, status: str) -> None:
        if self.access_window and self.access_window.winfo_exists():
            try:
                self.access_window.grab_release()
            except tk.TclError:
                pass
            self.access_window.destroy()
        self.access_window = None
        self.root.deiconify()
        self.root.lift()
        self.status_var.set(status)

    def sign_out(self) -> None:
        try:
            self.credential_store.clear()
            self.local_credential_store.clear()
        except ApiError as exc:
            LOG.error("Credential-store clear failed: %s", exc.detail or exc.message)
            messagebox.showerror(exc.category, exc.message, parent=self.root)
            return
        self.cancel.set()
        self.generation += 1
        active_key = self.client.api_key
        self.client.api_key = None
        if active_key:
            threading.Thread(
                target=self.client.logout, args=(active_key,), daemon=True
            ).start()
        self.client.dataset_alias = None
        self.username_value = ""
        self.token_value = ""
        self._clear_session_display()
        LOG.info("Signed out and cleared saved M2M access.")
        self.show_access_prompt(hide_main=True)

    def _clear_session_display(self) -> None:
        self.frames = []
        self.products = []
        self.selected = None
        self.dataset_alias = ""
        self.tree.delete(*self.tree.get_children())
        self.count_var.set("No search yet.")
        self._details("")
        self.viewer_button.configure(state=tk.DISABLED, text="View Aerial")
        self.save_button.configure(state=tk.DISABLED)
        self.product_var.set("Select a frame to check download products.")
        self._set_ready("Signed out")

    def show_logs(self) -> None:
        """Open a copyable, redacted diagnostics window for this app run."""
        if self.log_window and self.log_window.winfo_exists():
            self.log_window.deiconify()
            self.log_window.lift()
            self.log_window.focus_force()
            return
        window = tk.Toplevel(self.root)
        window.title(f"{APP_NAME} — Diagnostics")
        window.geometry("900x520")
        window.minsize(600, 320)
        window.transient(self.root)
        frame = ttk.Frame(window, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            frame,
            text=("Current-session diagnostics. Credentials, authentication keys, and "
                  "signed URLs are redacted."),
            wraplength=850,
        ).pack(fill=tk.X, pady=(0, 7))
        text_frame = ttk.Frame(frame)
        text_frame.pack(fill=tk.BOTH, expand=True)
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)
        log_text = tk.Text(text_frame, wrap=tk.NONE, state=tk.DISABLED,
                           font=("TkFixedFont", 11))
        yscroll = ttk.Scrollbar(text_frame, orient=tk.VERTICAL,
                                command=log_text.yview)
        xscroll = ttk.Scrollbar(text_frame, orient=tk.HORIZONTAL,
                                command=log_text.xview)
        log_text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        log_text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(buttons, text="Copy Logs", command=self.copy_logs).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Clear", command=self.clear_logs).pack(side=tk.LEFT, padx=6)
        ttk.Button(buttons, text="Close", command=self._close_logs).pack(side=tk.RIGHT)
        self.log_window = window
        self.log_text = log_text
        window.protocol("WM_DELETE_WINDOW", self._close_logs)
        self._refresh_logs()

    def _refresh_logs(self) -> None:
        if not self.log_window or not self.log_window.winfo_exists() or not self.log_text:
            return
        content = self.diagnostics.snapshot() or "No diagnostics have been recorded yet."
        current = self.log_text.get("1.0", "end-1c")
        if current != content:
            at_end = self.log_text.yview()[1] >= 0.98
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.delete("1.0", tk.END)
            self.log_text.insert("1.0", content)
            self.log_text.configure(state=tk.DISABLED)
            if at_end:
                self.log_text.see(tk.END)
        self._log_refresh_id = self.log_window.after(500, self._refresh_logs)

    def copy_logs(self) -> None:
        content = self.diagnostics.snapshot()
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self.status_var.set("Diagnostics copied.")

    def clear_logs(self) -> None:
        self.diagnostics.clear()
        LOG.info("Diagnostics cleared by user.")

    def _close_logs(self) -> None:
        if self.log_window and self._log_refresh_id:
            try:
                self.log_window.after_cancel(self._log_refresh_id)
            except tk.TclError:
                pass
        self._log_refresh_id = None
        if self.log_window and self.log_window.winfo_exists():
            self.log_window.destroy()
        self.log_window = None
        self.log_text = None

    def _set_window_icon(self) -> None:
        """Use the supplied platform icon when Tk supports its native format."""
        root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        icon = root / "assets" / ("icon.ico" if sys.platform == "win32" else "icon.icns")
        try:
            if icon.exists():
                self.root.iconbitmap(str(icon))
        except tk.TclError:
            # Some Tk builds cannot decode ICNS; packaging still uses this asset.
            pass

    def paste_coordinates(self) -> None:
        try:
            lat, lon = parse_clipboard_coordinates(self.root.clipboard_get())
        except (tk.TclError, ValueError) as exc:
            messagebox.showerror("Paste Coordinates", f"{exc}\n\nExpected: 37.123456, -93.654321",
                                 parent=self.root)
            return
        self.latitude.set(lat)
        self.longitude.set(lon)
        self.status_var.set("Coordinates pasted.")
        self.search_button.focus_set()

    def _query(self) -> SearchQuery:
        lat = parse_finite_number(self.latitude.get(), "Latitude", -90, 90)
        lon = parse_finite_number(self.longitude.get(), "Longitude", -180, 180)
        radius = parse_radius(self.radius.get(), self.unit.get())
        return SearchQuery(lat, lon, radius)

    def search(self) -> None:
        try:
            query = self._query()
        except ValueError as exc:
            messagebox.showerror("Input", str(exc), parent=self.root)
            return
        if not self.username_value or not self.token_value:
            self.status_var.set("USGS access is required to search.")
            self.show_access_prompt()
            return
        self.generation += 1
        generation = self.generation
        self.cancel.set()
        self.cancel = threading.Event()
        self._set_busy("Signing in…")
        credentials = (self.username_value, self.token_value)
        threading.Thread(
            target=self._search_worker,
            args=(query, credentials, generation, self.cancel),
            daemon=True,
        ).start()

    def _search_worker(self, query: SearchQuery, credentials: tuple[str, str],
                       generation: int, cancel: threading.Event) -> None:
        try:
            if not self.client.api_key:
                self.client.login(*credentials)
            self.events.put(("status", (generation, "Searching…")))
            result = self.client.search(query, cancel)
            self.events.put(("search", (generation, result)))
        except Exception as exc:  # worker boundary
            self.events.put(("error", (generation, self._safe_error(exc))))

    @staticmethod
    def _safe_error(exc: Exception) -> ApiError:
        if isinstance(exc, ApiError):
            return exc
        LOG.exception("Unexpected worker failure: %s", redact(exc))
        return ApiError("Error", "An unexpected error occurred. Existing results are unchanged.", str(exc))

    def _set_busy(self, status: str) -> None:
        self._busy = True
        self.status_var.set(status)
        self.search_button.configure(state=tk.DISABLED)
        self.cancel_button.configure(state=tk.NORMAL)
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)

    def _set_ready(self, status: str = "Ready") -> None:
        self._busy = False
        self.status_var.set(status)
        self.search_button.configure(state=tk.NORMAL)
        self.cancel_button.configure(state=tk.DISABLED)
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                try:
                    self._handle_event(kind, payload)
                except Exception as exc:  # main-thread event boundary
                    error = self._safe_error(exc)
                    self._set_ready("Error")
                    messagebox.showerror(error.category, error.message, parent=self.root)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(80, self._poll)

    def _handle_event(self, kind: str, payload: Any) -> None:
        generation = payload[0] if isinstance(payload, tuple) else self.generation
        if generation != self.generation:
            return
        value = payload[1] if isinstance(payload, tuple) else payload
        if kind == "status":
            self.status_var.set(value)
        elif kind == "search":
            self.frames, self.dataset_alias = value.frames, value.dataset_alias
            self._populate(value)
            self._set_ready("Complete" if value.frames else "No matches")
        elif kind == "products":
            entity_id, products = value
            if not self.selected or self.selected.entity_id != entity_id:
                return
            self.products = products
            chosen = best_product(products)
            self.product_var.set(f"Best available: {chosen.name} • {chosen.size_text}" if chosen else
                                 "No immediately downloadable scan. Use EarthExplorer to review options.")
            browse_fallback = bool(self.selected.browse_url)
            self.viewer_button.configure(
                text="View Aerial" if chosen else "View Browse Image Instead",
                state=tk.NORMAL if chosen or browse_fallback else tk.DISABLED,
            )
            self.save_button.configure(state=tk.NORMAL if chosen else tk.DISABLED)
            self._set_ready("Ready")
        elif kind == "downloaded":
            path, frame, product, key, open_viewer = value
            self.cache.record_cache(key, path)
            self._set_ready("Complete")
            if open_viewer:
                try:
                    ImageViewer(self.root, path, frame, product, self.cache, key)
                except (OSError, UnidentifiedImageError) as exc:
                    messagebox.showerror(APP_NAME, f"The downloaded image could not be opened:\n{exc}", parent=self.root)
            else:
                self._choose_and_copy(key, frame)
        elif kind == "saved_copy":
            _destination, embedded = value
            self._set_ready("Complete" if embedded else "Complete — saved without embedded metadata")
        elif kind == "browse_viewer":
            path, frame, product, key = value
            self.cache.record_cache(key, path)
            self._set_ready("Complete")
            try:
                ImageViewer(
                    self.root, path, frame, product, self.cache, key,
                    browse_quality=True,
                )
            except (OSError, UnidentifiedImageError) as exc:
                messagebox.showerror(
                    APP_NAME, f"The browse image could not be opened:\n{exc}",
                    parent=self.root,
                )
        elif kind == "progress":
            received, total = value
            if total:
                self.progress.stop()
                self.progress.configure(mode="determinate", value=min(100, received * 100 / total))
            self.status_var.set(f"Downloading… {format_bytes(received)} of {format_bytes(total)}")
        elif kind == "error":
            LOG.error("Operation failed: category=%s message=%s detail=%s",
                      value.category, value.message, value.detail or "none")
            self._set_ready("Cancelled" if value.category == "Cancelled" else "Error")
            if value.category != "Cancelled":
                messagebox.showerror(
                    value.category,
                    f"{value.message}\n\nUse View Logs for diagnostic details.",
                    parent=self.root,
                )

    def _populate(self, result: SearchResult) -> None:
        self.tree.delete(*self.tree.get_children())
        for index, frame in enumerate(self.frames):
            values = (frame.year, frame.date_text, frame.agency or MISSING, frame.project or MISSING,
                      frame.roll or MISSING, frame.frame or MISSING, frame.scale or MISSING,
                      frame.image_type or MISSING, "Yes" if frame.browse_url else "No",
                      frame.download_hint or "Check", frame.display_id or frame.entity_id)
            self.tree.insert("", tk.END, iid=str(index), values=values)
        shown = len(self.frames)
        suffix = "; list capped—narrow the radius" if result.capped else ""
        if result.candidate_count > shown:
            self.count_var.set(
                f"{shown} frame(s) cover this exact coordinate "
                f"({result.candidate_count} candidate scene(s) checked){suffix}"
            )
        else:
            self.count_var.set(f"{shown} frame(s) cover this exact coordinate{suffix}")
        self.selected = None
        self.products = []
        self._details("")

    def sort(self, column: str) -> None:
        selected_entity = self.selected.entity_id if self.selected else ""
        reverse = not self._sort_reverse if column == self._sort_column else False
        self._sort_column, self._sort_reverse = column, reverse
        def key(frame: AerialFrame) -> Any:
            if column == "date":
                return frame.acquisition_date or (dt.date.min if reverse else dt.date.max)
            return str(getattr(frame, column, "") or "").casefold()
        self.frames.sort(key=lambda frame: (key(frame), frame.display_id.casefold()), reverse=reverse)
        self._populate(SearchResult(
            self.frames, len(self.frames), False, self.dataset_alias,
            candidate_count=len(self.frames),
        ))
        for iid, frame in enumerate(self.frames):
            if frame.entity_id == selected_entity:
                self.tree.selection_set(str(iid))
                self.tree.see(str(iid))
                break

    def select_frame(self, _event: tk.Event | None = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        self.selected = self.frames[int(selection[0])]
        frame = self.selected
        lines = [f"{label}: {value or MISSING}" for label, value in (
            ("ID", frame.display_id), ("Entity ID", frame.entity_id), ("Acquisition date", frame.date_text),
            ("Agency", frame.agency), ("Project", frame.project), ("Roll / frame", f"{frame.roll or MISSING} / {frame.frame or MISSING}"),
            ("Scale", frame.scale), ("Image type", frame.image_type), ("Quality", frame.quality),
            ("Coordinates / footprint", frame.coordinates))]
        known = {line.split(":", 1)[0].casefold() for line in lines}
        for label, value in sorted(frame.details.items()):
            if label.casefold() not in known:
                lines.append(f"{label}: {value}")
        self._details("\n".join(lines))
        self.viewer_button.configure(state=tk.DISABLED, text="View Aerial")
        self.save_button.configure(state=tk.DISABLED)
        self.product_var.set("Checking available products…")
        generation = self.generation
        threading.Thread(target=self._products_worker, args=(frame, generation), daemon=True).start()

    def _details(self, text: str) -> None:
        self.details.configure(state=tk.NORMAL)
        self.details.delete("1.0", tk.END)
        self.details.insert("1.0", text)
        self.details.configure(state=tk.DISABLED)

    def _products_worker(self, frame: AerialFrame, generation: int) -> None:
        try:
            products = self.client.download_options(self.dataset_alias, frame.entity_id)
            self.events.put(("products", (generation, (frame.entity_id, products))))
        except Exception as exc:
            self.events.put(("error", (generation, self._safe_error(exc))))

    def open_best(self) -> None:
        if best_product(self.products):
            self._begin_product_download(True)
        elif self.selected and self.selected.browse_url:
            self._open_browse_fallback()

    def _open_browse_fallback(self) -> None:
        frame = self.selected
        if not frame:
            return
        key = self.cache.key(self.dataset_alias, frame.entity_id, "__browse__")
        cached = self.cache.get(key)
        product = DownloadProduct(
            "__browse__", frame.entity_id, "USGS catalog browse", None,
            "browse", False, False,
        )
        if cached:
            ImageViewer(
                self.root, cached, frame, product, self.cache, key,
                browse_quality=True,
            )
            return
        if not messagebox.askokcancel(
            APP_NAME,
            "No downloadable scan is immediately available. Open the lower-resolution "
            "USGS browse image in the viewer instead?",
            parent=self.root,
        ):
            return
        self.cancel = threading.Event()
        self._set_busy("Loading browse-quality image…")
        threading.Thread(
            target=self._browse_viewer_worker,
            args=(frame, product, key, self.generation, self.cancel),
            daemon=True,
        ).start()

    def _browse_viewer_worker(self, frame: AerialFrame, product: DownloadProduct,
                              key: tuple[str, str, str], generation: int,
                              cancel: threading.Event) -> None:
        partial: Path | None = None
        try:
            data = fetch_bytes(frame.browse_url, cancel)
            url_name = Path(urllib.parse.urlparse(frame.browse_url).path).name
            destination = self.cache.destination(
                key, sanitize_filename(url_name, f"{frame.display_id}_browse.jpg")
            )
            partial = destination.with_name(destination.name + ".part")
            with partial.open("xb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            partial.replace(destination)
            self.events.put(
                ("browse_viewer", (generation, (destination, frame, product, key)))
            )
        except Exception as exc:
            try:
                if partial:
                    partial.unlink(missing_ok=True)
            except OSError:
                pass
            self.events.put(("error", (generation, self._safe_error(exc))))

    def save_as(self) -> None:
        eligible = [item for item in self.products if item.available and not item.order_only]
        if not eligible:
            return
        product = best_product(eligible)
        if len(eligible) > 1:
            ordered = sorted(eligible, key=product_rank, reverse=True)
            choices = "\n".join(
                f"{index}. {item.name} ({item.size_text})"
                for index, item in enumerate(ordered, 1)
            )
            number = simpledialog.askinteger(
                "Choose download product",
                f"Select a product number:\n\n{choices}",
                parent=self.root, minvalue=1, maxvalue=len(ordered),
            )
            if number is None:
                return
            product = ordered[number - 1]
        self._begin_product_download(False, product)

    def _begin_product_download(self, open_viewer: bool,
                                product: DownloadProduct | None = None) -> None:
        frame = self.selected
        product = product or best_product(self.products)
        if not frame or not product:
            return
        key = self.cache.key(self.dataset_alias, frame.entity_id, product.product_id)
        cached = self.cache.get(key)
        if cached:
            if open_viewer:
                try:
                    ImageViewer(self.root, cached, frame, product, self.cache, key)
                except (OSError, UnidentifiedImageError) as exc:
                    messagebox.showerror(APP_NAME, str(exc), parent=self.root)
            else:
                self._choose_and_copy(key, frame)
            return
        if not messagebox.askokcancel(APP_NAME,
                f"Download {product.name} ({product.size_text})?\n\nThe file will be cached for this app session.", parent=self.root):
            return
        self.cancel = threading.Event()
        self._set_busy("Preparing download…")
        generation = self.generation
        threading.Thread(target=self._download_worker,
                         args=(frame, product, key, open_viewer, generation, self.cancel), daemon=True).start()

    def _download_worker(self, frame: AerialFrame, product: DownloadProduct,
                         key: tuple[str, str, str], open_viewer: bool,
                         generation: int, cancel: threading.Event) -> None:
        try:
            url = self.client.request_download_url(product, cancel)
            parsed_name = sanitize_filename(Path(urllib.parse.urlparse(url).path).name,
                                            f"{frame.display_id}.tif")
            destination = self.cache.destination(key, parsed_name)
            def report(received: int, total: int | None) -> None:
                self.events.put(("progress", (generation, (received, total))))
            stream_download(url, destination, cancel, report)
            self.events.put(("status", (generation, "Preparing image…")))
            prepared = prepare_viewable_image(destination, cancel)
            self.events.put(("downloaded", (generation, (prepared, frame, product, key, open_viewer))))
        except Exception as exc:
            self.events.put(("error", (generation, self._safe_error(exc))))

    def _choose_and_copy(self, key: tuple[str, str, str], frame: AerialFrame) -> None:
        filename = sanitize_filename(f"{frame.entity_id}.tif")
        selected = filedialog.asksaveasfilename(
            parent=self.root, initialfile=filename, defaultextension=".tif",
            filetypes=(("TIFF image", "*.tif"), ("All files", "*.*")),
        )
        if not selected:
            return
        destination = Path(selected)
        if destination.exists() and not messagebox.askyesno(APP_NAME, "Replace the existing file?", parent=self.root):
            return
        if destination.exists():
            destination.unlink()
        self.cancel = threading.Event()
        self._set_busy("Saving…")
        generation = self.generation
        threading.Thread(
            target=self._save_copy_worker, args=(key, frame, destination, generation, self.cancel),
            daemon=True,
        ).start()

    def _save_copy_worker(self, key: tuple[str, str, str], frame: AerialFrame,
                          destination: Path, generation: int, cancel: threading.Event) -> None:
        try:
            cached = self.cache.get(key)
            if not cached:
                raise ApiError("Filesystem", "The cached image is no longer available.")
            embedded = True
            try:
                embed_tiff_metadata(cached, destination, frame, cancel)
                self.cache.record_saved(key, destination)
            except ApiError as exc:
                if exc.category == "Cancelled":
                    raise
                embedded = False
                LOG.warning(
                    "Metadata embedding failed; saving original TIFF unchanged: %s",
                    exc.message,
                )
                self.cache.copy_to(key, destination)
            self.events.put(("saved_copy", (generation, (destination, embedded))))
        except Exception as exc:
            self.events.put(("error", (generation, self._safe_error(exc))))

    def close(self) -> None:
        LOG.info("Application shutdown requested.")
        self.cancel.set()
        self._close_logs()
        if self.client.api_key:
            threading.Thread(target=self.client.logout, daemon=True).start()
        self.cache.close()
        LOG.removeHandler(self.diagnostics)
        self.root.destroy()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", max(1.0, root.winfo_fpixels("1i") / 72.0))
    except tk.TclError:
        pass
    AerialArchiveExplorerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
