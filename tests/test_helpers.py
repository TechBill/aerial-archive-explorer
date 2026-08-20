import datetime as dt
import logging
import ssl
import threading

import pytest

from aerial_archive_explorer import (
    AerialFrame,
    ApiError,
    DownloadProduct,
    DiagnosticsBuffer,
    CredentialStore,
    SearchQuery,
    best_product,
    build_metadata_block,
    classify_product,
    coordinate_boxes,
    extract_frame_footprint,
    frame_sort_key,
    match_aerial_dataset,
    normalize_scene,
    parse_clipboard_coordinates,
    parse_envelope,
    parse_finite_number,
    parse_radius,
    point_in_footprint,
    polygon_area,
    redact,
    sanitize_filename,
    tls_context,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("37.123456, -93.654321", ("37.123456", "-93.654321")),
        (" 37.123456   -93.654321 ", ("37.123456", "-93.654321")),
        ("<coordinates>-93.654321,37.123456,250</coordinates>",
         ("37.123456", "-93.654321")),
        ("<Placemark> <coordinates> -93.5,37.2 </coordinates> </Placemark>",
         ("37.2", "-93.5")),
        ("Location: 37.1, -93.2", ("37.1", "-93.2")),
    ],
)
def test_clipboard_valid(text, expected):
    assert parse_clipboard_coordinates(text) == expected


@pytest.mark.parametrize("text", [
    "", "not coordinates", "37", "37,-93 38,-94",
    "https://maps.example/37,-93", "37° 2'", "91,-93", "37,-181",
    "<coordinates>-93,37 -94,38</coordinates>",
    "<coordinates>-93,37</coordinates><coordinates>-94,38</coordinates>",
    "NaN, 2", "inf, 2",
])
def test_clipboard_invalid(text):
    with pytest.raises(ValueError):
        parse_clipboard_coordinates(text)


def test_finite_numbers_and_radius():
    assert parse_finite_number("-90", "Latitude", -90, 90) == -90
    assert parse_radius("1", "miles") == pytest.approx(1.609344)
    assert parse_radius("1", "kilometers") == 1
    for value in ("", "nan", "inf", "101"):
        with pytest.raises(ValueError):
            parse_radius(value, "kilometers")


def test_bounding_box_equator():
    boxes = coordinate_boxes(SearchQuery(0, 0, 1))
    assert len(boxes) == 1
    south, west, north, east = boxes[0]
    assert south == pytest.approx(-1 / 111.32)
    assert north == pytest.approx(1 / 111.32)
    assert west == pytest.approx(-1 / 111.32)
    assert east == pytest.approx(1 / 111.32)


def test_bounding_box_high_latitude_and_pole():
    high = coordinate_boxes(SearchQuery(80, 10, 1))[0]
    assert high[3] - high[1] > 0.09
    pole = coordinate_boxes(SearchQuery(90, 10, 1))[0]
    assert pole[1:] == (-180.0, 90.0, 180.0)


def test_bounding_box_splits_antimeridian():
    boxes = coordinate_boxes(SearchQuery(0, 179.999, 10))
    assert len(boxes) == 2
    assert boxes[0][3] == 180
    assert boxes[1][1] == -180


def test_dataset_match_zero_one_ambiguous():
    selected = match_aerial_dataset([{
        "collectionName": "Aerial Photo Single Frames", "datasetAlias": "alias"
    }])
    assert selected["datasetAlias"] == "alias"
    with pytest.raises(ApiError):
        match_aerial_dataset([])
    with pytest.raises(ApiError):
        match_aerial_dataset([
            {"collectionName": "Aerial Photo Single Frames", "datasetAlias": "one"},
            {"collectionName": "Aerial Photo Single Frames archive", "datasetAlias": "two"},
        ])


def test_response_envelope():
    assert parse_envelope({"data": {"ok": True}, "errorCode": None}) == {"ok": True}
    with pytest.raises(ApiError, match="denied"):
        parse_envelope({"data": None, "errorCode": "AUTH", "errorMessage": "Token denied"})
    with pytest.raises(ApiError):
        parse_envelope({"unexpected": True})


def test_normalization_complete_sparse_and_unknown():
    scene = {
        "entityId": "E1", "displayId": "PHOTO-1", "acquisitionDate": "1952-06-03",
        "browse": [{"browsePath": "https://example.test/browse.jpg"}],
        "metadata": [
            {"fieldName": "Agency", "value": "USDA"},
            {"fieldName": "Roll Number", "value": "R12"},
            {"fieldName": "Frame", "value": "4"},
            {"fieldName": "Unfamiliar Field", "value": "preserved"},
        ],
    }
    frame = normalize_scene(scene)
    assert frame.entity_id == "E1"
    assert frame.acquisition_date == dt.date(1952, 6, 3)
    assert frame.agency == "USDA"
    assert frame.details["Unfamiliar Field"] == "preserved"
    sparse = normalize_scene({"entityId": "E2", "metadata": []})
    assert sparse.acquisition_date is None
    assert sorted([sparse, frame], key=frame_sort_key) == [frame, sparse]


def test_extract_footprint_from_usgs_corner_metadata_and_reject_invalid():
    metadata = {
        "NW Corner Lat dec": "38.5", "NW Corner Long dec": "-94.5",
        "NE Corner Lat dec": "38.5", "NE Corner Long dec": "-93.5",
        "SE Corner Lat dec": "37.5", "SE Corner Long dec": "-93.5",
        "SW Corner Lat dec": "37.5", "SW Corner Long dec": "-94.5",
    }
    assert extract_frame_footprint(metadata) == (
        (-94.5, 38.5), (-93.5, 38.5), (-93.5, 37.5), (-94.5, 37.5),
    )
    incomplete = {"NW Corner Lat dec": "38.5", "NW Corner Long dec": "-94.5"}
    assert extract_frame_footprint(incomplete) is None
    out_of_range = {**metadata, "NW Corner Lat dec": "138.5"}
    assert extract_frame_footprint(out_of_range) is None
    degenerate = {name: "0" for name in metadata}
    assert extract_frame_footprint(degenerate) is None


def test_normalize_scene_extracts_footprint():
    scene = {
        "entityId": "E1", "metadata": [
            {"fieldName": "NW Corner Lat dec", "value": "38.5"},
            {"fieldName": "NW Corner Long dec", "value": "-94.5"},
            {"fieldName": "NE Corner Lat dec", "value": "38.5"},
            {"fieldName": "NE Corner Long dec", "value": "-93.5"},
            {"fieldName": "SE Corner Lat dec", "value": "37.5"},
            {"fieldName": "SE Corner Long dec", "value": "-93.5"},
            {"fieldName": "SW Corner Lat dec", "value": "37.5"},
            {"fieldName": "SW Corner Long dec", "value": "-94.5"},
        ],
    }
    assert normalize_scene(scene).footprint == (
        (-94.5, 38.5), (-93.5, 38.5), (-93.5, 37.5), (-94.5, 37.5),
    )
    assert normalize_scene({"entityId": "E2", "metadata": []}).footprint is None


def test_point_in_footprint_inside_outside_boundary_and_invalid():
    footprint = ((-94.0, 38.0), (-92.0, 38.0), (-92.0, 36.0), (-94.0, 36.0))
    assert point_in_footprint(37.0, -93.0, footprint)  # interior
    assert point_in_footprint(38.0, -93.0, footprint)  # on an edge (north)
    assert point_in_footprint(38.0, -94.0, footprint)  # exactly on a corner
    assert not point_in_footprint(39.0, -93.0, footprint)  # north of it
    assert not point_in_footprint(37.0, -93.0, None)
    degenerate = ((-94.0, 38.0), (-94.0, 38.0), (-94.0, 38.0), (-94.0, 38.0))
    assert not point_in_footprint(37.0, -93.0, degenerate)
    # A neighboring frame's footprint that never reaches the searched point.
    neighbor = ((-92.0, 38.0), (-90.0, 38.0), (-90.0, 36.0), (-92.0, 36.0))
    assert not point_in_footprint(37.0, -93.0, neighbor)


def test_polygon_area_signed():
    footprint = ((-94.0, 38.0), (-92.0, 38.0), (-92.0, 36.0), (-94.0, 36.0))
    assert abs(polygon_area(footprint)) == pytest.approx(4.0)


def test_build_metadata_block_includes_identity_and_corners():
    frame = AerialFrame(
        entity_id="AR1VXA000010011", display_id="1VXA000010011",
        acquisition_date=dt.date(1959, 3, 22), agency="1", project="VXA00",
        roll="000001", frame="11", scale="18000", image_type="24", quality="8",
        footprint=((-93.302812, 37.233226), (-93.257739, 37.232884),
                   (-93.258178, 37.196844), (-93.30323, 37.197187)),
    )
    block = build_metadata_block(frame)
    assert "ENTITY_ID: AR1VXA000010011" in block
    assert "ACQUISITION_DATE: 1959-03-22" in block
    assert "PROJECT: VXA00" in block
    assert "ROLL: 000001" in block
    assert "FRAME: 11" in block
    assert "SCALE: 18000" in block
    assert "NW_CORNER: -93.302812, 37.233226" in block
    assert "NE_CORNER: -93.257739, 37.232884" in block
    assert "SE_CORNER: -93.258178, 37.196844" in block
    assert "SW_CORNER: -93.303230, 37.197187" in block
    assert "CENTER: -93.280490, 37.215035" in block
    assert "SOURCE_CRS: EPSG:4326" in block
    assert "CORNER_ORDER: NW, NE, SE, SW" in block


def test_build_metadata_block_handles_missing_footprint():
    frame = AerialFrame(entity_id="E1", display_id="E1")
    block = build_metadata_block(frame)
    assert "ENTITY_ID: E1" in block
    assert "ACQUISITION_DATE: unknown" in block
    assert "NW_CORNER: unknown" in block
    assert "CENTER: unknown" in block


def product(name, available=True, order=False):
    return DownloadProduct(name, "E", name, None,
                           "order-only" if order else "immediate",
                           available, order)


def test_product_classification_and_ranking():
    high = classify_product({"id": "H", "entityId": "E", "productName": "High Resolution 1000 dpi", "available": True})
    medium = classify_product({"id": "M", "entityId": "E", "productName": "Medium Resolution 400 dpi", "available": True})
    order = classify_product({"id": "O", "entityId": "E", "productName": "On-demand order scan", "available": False})
    assert order.order_only
    assert best_product([medium, order, high]) == high
    assert best_product([order]) is None


@pytest.mark.parametrize(("name", "expected"), [
    ("../../secret.tif", "secret.tif"),
    ("a:b?.tif", "a_b_.tif"),
    ("", "aerial_image.tif"),
])
def test_filename_sanitization(name, expected):
    assert sanitize_filename(name) == expected


def test_redaction():
    text = redact(
        "endpoint=login-token attempt=1/3 token: abc123 "
        "X-Auth-Token='secret' https://host/file?signature=secret"
    )
    assert "abc123" not in text
    assert "signature=secret" not in text
    assert "endpoint=login-token attempt=1/3" in text


def test_tls_context_requires_verification_and_hostname_check():
    context = tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.cert_store_stats()["x509_ca"] > 0


def test_diagnostics_buffer_is_bounded_and_redacted():
    buffer = DiagnosticsBuffer(capacity=2)
    logger = logging.getLogger("aerial-diagnostics-test")
    logger.addHandler(buffer)
    logger.setLevel(logging.INFO)
    try:
        logger.info("first")
        logger.info("token: super-secret")
        logger.info("last")
        snapshot = buffer.snapshot()
        assert "first" not in snapshot
        assert "super-secret" not in snapshot
        assert "[REDACTED]" in snapshot
        assert "last" in snapshot
        buffer.clear()
        assert buffer.snapshot() == ""
    finally:
        logger.removeHandler(buffer)


class FakeCredentialBackend:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, password):
        self.values[(service, account)] = password

    def delete_password(self, service, account):
        from keyring.errors import PasswordDeleteError

        try:
            del self.values[(service, account)]
        except KeyError as exc:
            raise PasswordDeleteError("missing") from exc


def test_credential_store_round_trip_update_and_clear():
    backend = FakeCredentialBackend()
    store = CredentialStore(backend)
    assert store.load() is None
    store.save("first-user", "first-token")
    assert store.load() == ("first-user", "first-token")
    store.save("second-user", "second-token")
    assert store.load() == ("second-user", "second-token")
    assert "first-token" not in backend.values.values()
    store.clear()
    assert store.load() is None
    assert not backend.values
