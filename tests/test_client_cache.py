import gzip
import io
import threading

import pytest
from PIL import Image

from aerial_archive_explorer import (
    AerialFrame,
    ApiError,
    SearchQuery,
    UsgsM2MClient,
    ViewerCache,
    embed_tiff_metadata,
    prepare_viewable_image,
    stream_download,
)


def footprint_metadata(latitude=37.0, longitude=-93.0, delta=0.1):
    """A four-corner footprint (as USGS metadata fields) that covers
    (latitude, longitude) with room to spare on every side."""
    return [
        {"fieldName": "NW Corner Latitude", "value": latitude + delta},
        {"fieldName": "NW Corner Longitude", "value": longitude - delta},
        {"fieldName": "NE Corner Latitude", "value": latitude + delta},
        {"fieldName": "NE Corner Longitude", "value": longitude + delta},
        {"fieldName": "SE Corner Latitude", "value": latitude - delta},
        {"fieldName": "SE Corner Longitude", "value": longitude + delta},
        {"fieldName": "SW Corner Latitude", "value": latitude - delta},
        {"fieldName": "SW Corner Longitude", "value": longitude - delta},
    ]


class FakeTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, endpoint, payload, key):
        self.calls.append((endpoint, payload, key))
        if endpoint == "login-token":
            return {"data": "session-key", "errorCode": None}
        if endpoint == "dataset-search":
            return {"data": [{"collectionName": "Aerial Photo Single Frames",
                               "datasetAlias": "aerial_alias"}], "errorCode": None}
        if endpoint == "scene-search":
            start = payload["startingNumber"]
            records = ([{"entityId": "E1", "displayId": "P1",
                         "acquisitionDate": "1940-01-01", "metadata": footprint_metadata()},
                        {"entityId": "E2", "displayId": "P2",
                         "acquisitionDate": "1950-01-01", "metadata": footprint_metadata()}]
                       if start == 1 else [])
            return {"data": {"results": records, "totalHits": 2,
                             "recordsReturned": len(records)}, "errorCode": None}
        if endpoint == "download-options":
            return {"data": [{"id": "P", "entityId": "E1",
                               "productName": "Medium Resolution 400 dpi",
                               "available": True}], "errorCode": None}
        if endpoint == "download-request":
            return {"data": {"availableDownloads": [{"url": "https://example.test/file.tif"}]},
                    "errorCode": None}
        if endpoint == "logout":
            return {"data": None, "errorCode": None}
        raise AssertionError(endpoint)


def test_api_contract_and_search_pagination_deduplication():
    transport = FakeTransport()
    client = UsgsM2MClient(transport=transport, sleep=lambda _: None)
    client.login("user", "app-token")
    result = client.search(SearchQuery(37, -93, 1), threading.Event())
    assert [frame.entity_id for frame in result.frames] == ["E1", "E2"]
    login = transport.calls[0]
    assert login[0] == "login-token"
    assert login[1] == {"username": "user", "token": "app-token"}
    search_call = next(call for call in transport.calls if call[0] == "scene-search")
    assert search_call[1]["datasetName"] == "aerial_alias"
    spatial = search_call[1]["sceneFilter"]["spatialFilter"]
    assert spatial["filterType"] == "mbr"
    assert spatial["lowerLeft"] != spatial["upperRight"]
    products = client.download_options("aerial_alias", "E1")
    assert products[0].product_id == "P"
    url = client.request_download_url(products[0], threading.Event())
    assert url.endswith("file.tif")
    request_call = next(call for call in transport.calls if call[0] == "download-request")
    assert request_call[1]["downloads"] == [{"entityId": "E1", "productId": "P"}]


def test_search_cancellation():
    client = UsgsM2MClient(transport=FakeTransport(), sleep=lambda _: None)
    client.api_key = "session"
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ApiError, match="cancelled"):
        client.search(SearchQuery(0, 0, 1), cancelled)


def test_search_cap():
    class ManyTransport(FakeTransport):
        def __call__(self, endpoint, payload, key):
            if endpoint == "dataset-search":
                return super().__call__(endpoint, payload, key)
            if endpoint == "scene-search":
                count = payload["maxResults"]
                return {"data": {"results": [
                    {"entityId": f"E{payload['startingNumber'] + i}",
                     "metadata": footprint_metadata(0.0, 0.0)}
                    for i in range(count)], "totalHits": 1000,
                    "recordsReturned": count}, "errorCode": None}
            return super().__call__(endpoint, payload, key)
    client = UsgsM2MClient(transport=ManyTransport(), sleep=lambda _: None)
    client.api_key = "session"
    result = client.search(SearchQuery(0, 0, 1), threading.Event(), cap=3)
    assert len(result.frames) == 3
    assert result.capped


def test_search_excludes_neighboring_and_footprintless_candidates():
    class MixedTransport(FakeTransport):
        def __call__(self, endpoint, payload, key):
            if endpoint == "scene-search":
                records = [
                    # Covers the searched point (37, -93).
                    {"entityId": "COVERS", "metadata": footprint_metadata(37.0, -93.0)},
                    # A real neighboring frame whose footprint does not
                    # reach the searched point at all.
                    {"entityId": "NEIGHBOR", "metadata": footprint_metadata(37.0, -90.0)},
                    # Matched the MBR radius search but has no usable
                    # corner metadata to confirm coverage either way.
                    {"entityId": "NO_FOOTPRINT", "metadata": []},
                ]
                return {"data": {"results": records, "totalHits": len(records),
                                 "recordsReturned": len(records)}, "errorCode": None}
            return super().__call__(endpoint, payload, key)

    client = UsgsM2MClient(transport=MixedTransport(), sleep=lambda _: None)
    client.api_key = "session"
    result = client.search(SearchQuery(37, -93, 1), threading.Event())
    assert [frame.entity_id for frame in result.frames] == ["COVERS"]
    assert result.candidate_count == 3
    assert result.invalid_footprints == 1


def test_prepared_download_retrieve_contract(monkeypatch):
    import aerial_archive_explorer as app

    monkeypatch.setattr(app, "POLL_INTERVAL", 0)

    class PreparedTransport(FakeTransport):
        def __call__(self, endpoint, payload, key):
            self.calls.append((endpoint, payload, key))
            if endpoint == "download-request":
                return {"data": {"preparingDownloads": [{"downloadId": "D1"}]},
                        "errorCode": None}
            if endpoint == "download-retrieve":
                return {"data": {"available": [{"downloadId": "D1",
                                                  "url": "https://example.test/ready.tif"}]},
                        "errorCode": None}
            return super().__call__(endpoint, payload, key)

    transport = PreparedTransport()
    client = UsgsM2MClient(transport=transport, sleep=lambda _: None)
    client.api_key = "session"
    product = client.download_options("aerial_alias", "E1")[0]
    assert client.request_download_url(product, threading.Event()).endswith("ready.tif")
    retrieve = next(call for call in transport.calls if call[0] == "download-retrieve")
    assert retrieve[1]["label"].startswith("aerial-archive-")


class FakeDownloadResponse:
    def __init__(self, content):
        self._stream = io.BytesIO(content)
        self.headers = {"Content-Length": str(len(content))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self._stream.read(size)


def test_stream_download_atomic_completion(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "aerial_archive_explorer.urllib.request.urlopen",
        lambda *_args, **_kwargs: FakeDownloadResponse(b"complete image"),
    )
    destination = tmp_path / "image.tif"
    updates = []
    stream_download("https://example.test/image.tif", destination,
                    threading.Event(), lambda done, total: updates.append((done, total)))
    assert destination.read_bytes() == b"complete image"
    assert not (tmp_path / "image.tif.part").exists()
    assert updates[-1] == (14, 14)


def test_stream_download_cancellation_cleans_partial(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "aerial_archive_explorer.urllib.request.urlopen",
        lambda *_args, **_kwargs: FakeDownloadResponse(b"unused"),
    )
    cancelled = threading.Event()
    cancelled.set()
    destination = tmp_path / "cancelled.tif"
    with pytest.raises(ApiError, match="cancelled"):
        stream_download("https://example.test/image.tif", destination, cancelled)
    assert not destination.exists()
    assert not (tmp_path / "cancelled.tif.part").exists()


def test_prepare_viewable_image_expands_gzip_tiff_atomically(tmp_path):
    image_bytes = io.BytesIO()
    Image.new("L", (12, 8), 127).save(image_bytes, format="TIFF")
    compressed = tmp_path / "opaque-download-id"
    with gzip.open(compressed, "wb") as output:
        output.write(image_bytes.getvalue())
    prepared = prepare_viewable_image(compressed, threading.Event())
    assert prepared.suffix == ".tif"
    assert not compressed.exists()
    assert not (tmp_path / "opaque-download-id.unpacked.part").exists()
    with Image.open(prepared) as image:
        assert image.format == "TIFF"
        assert image.size == (12, 8)


def test_prepare_viewable_image_adds_suffix_to_raw_image(tmp_path):
    opaque = tmp_path / "opaque-download-id"
    Image.new("RGB", (4, 3), "red").save(opaque, format="JPEG")
    prepared = prepare_viewable_image(opaque, threading.Event())
    assert prepared.suffix == ".jpg"
    assert prepared.exists()
    assert not opaque.exists()


def test_prepare_viewable_image_rejects_non_image(tmp_path):
    invalid = tmp_path / "not-an-image"
    invalid.write_bytes(b"plain text response")
    with pytest.raises(ApiError, match="not a supported"):
        prepare_viewable_image(invalid, threading.Event())


def test_viewer_cache_reuse_save_and_cleanup(tmp_path):
    cache = ViewerCache()
    key = cache.key("dataset", "entity", "product")
    source = cache.destination(key, "scan.tif")
    source.write_bytes(b"image bytes")
    cache.record_cache(key, source)
    assert cache.get(key) == source
    saved = tmp_path / "saved.tif"
    cache.copy_to(key, saved)
    assert saved.read_bytes() == b"image bytes"
    assert cache.get(key) == saved
    temporary_root = cache.root
    cache.close()
    assert saved.exists()
    assert not temporary_root.exists()


def test_embed_tiff_metadata_writes_description_and_preserves_pixels(tmp_path):
    import numpy as np

    source = tmp_path / "frame_original.tif"
    pixels = np.arange(12, dtype="uint8").reshape(3, 4)
    Image.fromarray(pixels, mode="L").save(source, format="TIFF")
    frame = AerialFrame(
        entity_id="AR1VXA000010011", display_id="1VXA000010011",
        acquisition_date=None,
        footprint=((-94.0, 38.0), (-92.0, 38.0), (-92.0, 36.0), (-94.0, 36.0)),
    )
    destination = tmp_path / "AR1VXA000010011.tif"
    embed_tiff_metadata(source, destination, frame, threading.Event())
    assert source.exists()  # source untouched
    with Image.open(destination) as saved:
        assert np.array_equal(np.array(saved), pixels)
        description = saved.tag_v2[270]
    assert "ENTITY_ID: AR1VXA000010011" in description
    assert "---USGS HISTORICAL METADATA---" in description


def test_embed_tiff_metadata_cancelled_leaves_no_partial(tmp_path):
    source = tmp_path / "frame_original.tif"
    Image.new("L", (4, 3), 100).save(source, format="TIFF")
    frame = AerialFrame(entity_id="E1", display_id="E1")
    destination = tmp_path / "E1.tif"
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ApiError, match="cancelled"):
        embed_tiff_metadata(source, destination, frame, cancelled)
    assert not destination.exists()
    assert not (tmp_path / (destination.name + ".part")).exists()


def test_embed_tiff_metadata_rejects_non_image_without_touching_source(tmp_path):
    source = tmp_path / "not-an-image.tif"
    source.write_bytes(b"plain text, not a TIFF")
    frame = AerialFrame(entity_id="E1", display_id="E1")
    destination = tmp_path / "E1.tif"
    with pytest.raises(ApiError, match="metadata could not be embedded"):
        embed_tiff_metadata(source, destination, frame, threading.Event())
    assert source.read_bytes() == b"plain text, not a TIFF"
    assert not destination.exists()


def test_cache_never_overwrites(tmp_path):
    cache = ViewerCache()
    key = cache.key("d", "e", "p")
    source = cache.destination(key, "scan.tif")
    source.write_bytes(b"source")
    cache.record_cache(key, source)
    destination = tmp_path / "exists.tif"
    destination.write_bytes(b"keep")
    with pytest.raises(ApiError):
        cache.copy_to(key, destination)
    assert destination.read_bytes() == b"keep"
    cache.close()
