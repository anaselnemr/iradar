"""Tests for asset_watch - the file-fingerprint logic that catches a document
or image republished at the SAME URL (invisible to the page compare; the one
class of real update the offline mutation battery proved is missed).

Pure logic only: URL classification and the changed-bytes decision. No network.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "iradar"))
import asset_watch as aw   # noqa: E402


def _row(etag="", last_modified="", content_length=None):
    return {"etag": etag, "last_modified": last_modified,
            "content_length": content_length, "kind": "document"}


def _fp(status=200, etag="", last_modified="", content_length=None):
    return {"status": status, "etag": etag, "last_modified": last_modified,
            "content_length": content_length}


# --- what counts as a file asset -------------------------------------------

def test_documents_and_media_are_assets():
    assert aw.is_asset_url("https://www.infineon.com/assets/row/public/documents/10/49/guide.pdf")
    assert aw.is_asset_url("https://assets.infineon.com/is/image/infineon/photo")
    assert aw.is_asset_url("https://assets.infineon.com/is/content/infineon/video-tour")
    assert aw.is_asset_url("https://www.infineon.com/x/jcr:content/renditions/img.web.1280.720.png")
    assert aw.is_asset_url("https://www.infineon.com/tools/setup.zip")


def test_plain_pages_and_foreign_hosts_are_not_assets():
    # HTML pages are what the page watch itself covers - and their ETag moves
    # on every deploy without the content changing
    assert not aw.is_asset_url("https://www.infineon.com/part/CY8C624ABZI-S2D44")
    assert not aw.is_asset_url("https://www.infineon.com/products/microcontroller")
    assert not aw.is_asset_url("https://community.infineon.com/thread/123.pdf")
    assert not aw.is_asset_url("")
    assert not aw.is_asset_url(None)


# --- the changed-bytes decision --------------------------------------------

def test_same_validators_is_no_change():
    old = _row(etag="abc", last_modified="Mon, 01 Jul 2026", content_length=500)
    assert aw.bytes_changed(old, _fp(etag="abc", last_modified="Mon, 01 Jul 2026", content_length=500)) is False


def test_size_change_wins():
    old = _row(etag="abc", content_length=500)
    assert aw.bytes_changed(old, _fp(etag="abc", content_length=777)) is True


def test_etag_change_flags():
    old = _row(etag="abc", content_length=500)
    assert aw.bytes_changed(old, _fp(etag="def", content_length=500)) is True


def test_last_modified_only_used_without_etags():
    # both sides carry etags that MATCH -> a moved date must not flag
    old = _row(etag="abc", last_modified="Mon, 01 Jul 2026")
    assert aw.bytes_changed(old, _fp(etag="abc", last_modified="Tue, 02 Jul 2026")) is False
    # no etags anywhere -> the date is the only validator and does flag
    old = _row(last_modified="Mon, 01 Jul 2026")
    assert aw.bytes_changed(old, _fp(last_modified="Tue, 02 Jul 2026")) is True


def test_missing_validator_on_either_side_never_judges():
    # a CDN that stops sending ETags must not fake an update
    old = _row(etag="abc")
    assert aw.bytes_changed(old, _fp(etag="")) is False
    old = _row()
    assert aw.bytes_changed(old, _fp(etag="new")) is False


def test_unreachable_now_is_not_a_change():
    old = _row(etag="abc", content_length=500)
    assert aw.bytes_changed(old, _fp(status=None)) is False
    assert aw.bytes_changed(old, _fp(status=503, etag="zzz")) is False
