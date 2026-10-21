"""Regression tests: the Accept-Encoding header must not advertise brotli.

Measured live 2026-10-11: screener.in answers `Accept-Encoding: gzip, deflate, br`
with `Content-Encoding: br`. httpx decodes gzip/deflate natively but decodes brotli
only when the optional `brotli` (or `brotlicffi`) package is installed; without it
`resp.text` is the raw compressed bytes, so the parser finds no tables and every
screener-backed member silently degrades to empty -- sector, industry, financials,
shareholding and peers all came back empty on a live run while the HTTP status was
200. The fix is to not advertise br, which makes the server answer gzip.

These tests are about the header and about the silent-failure mode, not about the
vendor's current encoding preferences.
"""
from __future__ import annotations

import gzip
import zlib

import httpx
import pytest

import httpx as _httpx

from bfinance.screener.client import ScreenerClient


def _stub_transport(monkeypatch, handler):
    """Make ScreenerClient's internal httpx.AsyncClient serve ``handler``.

    The client builds a fresh AsyncClient per attempt inside its retry loop, so the
    seam is the class itself: capture the real class first, then replace it with a
    factory that returns an AsyncClient carrying a MockTransport and the kwargs the
    client passes (dropping the ones MockTransport does not want).
    """
    real_async_client = httpx.AsyncClient  # captured before the patch

    def factory(**kwargs):
        kwargs.pop("headers", None)
        kwargs.pop("timeout", None)
        kwargs.pop("follow_redirects", None)
        kwargs.pop("proxy", None)
        return real_async_client(transport=httpx.MockTransport(handler), **{})

    monkeypatch.setattr(httpx, "AsyncClient", factory)


class TestAcceptEncodingNeverAdvertisesBrotli:
    def test_the_header_does_not_ask_for_brotli(self):
        """A brotli advertisement is paid for with a package we do not depend on."""
        headers = ScreenerClient()._get_browser_headers()
        encodings = [e.strip() for e in headers["Accept-Encoding"].lower().split(",")]
        assert "br" not in encodings, (
            "the Accept-Encoding header advertises brotli, but httpx can only decode it "
            "when the optional brotli/brotlicffi package is installed and the package does "
            "not depend on it; the response then reaches the parser as compressed bytes"
        )

    def test_the_header_still_asks_for_at_least_one_decodable_encoding(self):
        """Dropping br must not turn into refusing compression entirely."""
        encodings = [
            e.strip()
            for e in ScreenerClient()._get_browser_headers()["Accept-Encoding"].lower().split(",")
        ]
        assert {"gzip", "deflate"} & set(encodings), (
            "the header asks for no encoding the client can actually decode"
        )

    @pytest.mark.parametrize("encoding", ["gzip", "deflate"])
    async def test_a_decodable_compressed_body_reaches_the_caller_as_text(
        self, encoding, monkeypatch
    ):
        """Whatever the server answers must decode to the document it sent.

        A 200 whose body the client cannot decode is exactly the silent-empty failure
        this guards, so the proxy replays it in both encodings httpx handles natively.
        """
        marker = "<html><body><h1>company</h1></body></html>"
        raw = (
            gzip.compress(marker.encode())
            if encoding == "gzip"
            else zlib.compress(marker.encode())
        )
        _stub_transport(
            monkeypatch,
            lambda r: httpx.Response(200, headers={"Content-Encoding": encoding}, content=raw),
        )
        client = ScreenerClient(min_request_interval_ms=0.0)
        resp = await client._send_with_retry("https://www.screener.in/company/TCS/consolidated/")
        assert resp.text == marker, (
            f"a {encoding}-encoded body did not decode to the document the server sent"
        )

    async def test_a_body_that_is_not_a_document_is_detectable(self, monkeypatch):
        """Control for the test above: the undecodable answer is visibly not a document.

        This is the signature of the brotli failure, stated as bytes, so the guard can be
        checked without the brotli package installed to produce the real thing.
        """
        undecodable = b"\x1b\x00\x00\x04\x00\x00\x00\x00"  # not any document
        _stub_transport(
            monkeypatch, lambda r: httpx.Response(200, content=undecodable)
        )
        client = ScreenerClient(min_request_interval_ms=0.0)
        resp = await client._send_with_retry("https://www.screener.in/company/TCS/consolidated/")
        assert not resp.text.lstrip().lower().startswith(("<!doctype", "<html")), (
            "control expected a non-document body; if this fails the control was mis-specified"
        )
