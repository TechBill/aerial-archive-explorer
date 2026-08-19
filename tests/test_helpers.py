import datetime as dt
import logging
import ssl
import threading

import pytest

from aerial_archive_explorer import (
    ApiError,
    DownloadProduct,
    DiagnosticsBuffer,
    CredentialStore,
    SearchQuery,
    best_product,
    classify_product,
    coordinate_boxes,
    frame_sort_key,
    match_aerial_dataset,
    normalize_scene,
    parse_clipboard_coordinates,
    parse_envelope,
    parse_finite_number,
    parse_radius,
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
