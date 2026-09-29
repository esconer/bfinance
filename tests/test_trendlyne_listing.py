"""
Pre-listing rows: the count must be measurable, and the claim that it was zero must be gone.

Background, measured live 2026-09-29 against trendlyne.com
----------------------------------------------------------
``eodData`` returns bars dated **before** a security's own NSE listing date. On SBIN (trendlyne
stock id 1193) 2,222 of 5,114 bars, carrying 45,561,075,413 shares of volume, are dated before
its 2015-01-15 NSE listing. The pattern is not general -- PAYTM starts on its 2021-11-18 listing
date, IREDA (a 2023 IPO) starts 2023-11-29, and RELIANCE starts at the 2006-01-02 floor -- which
is exactly why the generalisation survived: on three of four symbols it looked true.

Two claims in ``client.py`` asserted the opposite, and there was no implementation for either:
there is no ``LISTING`` constant, no clipping function, and no gate. The module docstring said the
history *is* truncated at listing, and the ``attrs`` note told every caller the bar count was
symbol-dependent *because* it was truncated at the instrument's listing. A package that states a
property of its data the data does not have, and invites downstream code to rely on it, is worse
than one that says nothing.

There is no listing-date source to gate on. The OHLC payload's ``stockData`` carries the full
name, the NSE code, the BSE code, the ISIN and three scores, and no date; NSE's ``/public-past-issues``
answers an empty list for historical windows. So the route taken here is **expose, do not decide**:
``parse_ohlc`` takes an optional caller-supplied ``listing_date`` and publishes
``attrs["bfinance_trendlyne_rows_before_listing"]`` plus the volume those rows carry. It never
clips. On a split- and bonus-adjusted series the pre-listing bars sit on the same adjusted scale as
the rest, so dropping them would leave a discontinuity rather than remove one -- and which rows a
caller wants is not the parser's decision.

Fully offline, driven by the recorded ``ohlc_motherson.json`` capture, through
``httpx.MockTransport`` where the client is involved. **No test touches the network.**
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

import httpx
import pandas as pd
import pytest

from bfinance.trendlyne import client as tl

FIXTURES = Path(__file__).parent / "fixtures" / "trendlyne"
PACKAGE_DIR = Path(tl.__file__).parent
CLIENT_SOURCE = (PACKAGE_DIR / "client.py").read_text(encoding="utf-8")
MODULE_DOC = tl.__doc__ or ""

#: MOTHERSON's NSE listing date. The recorded capture is 60 pre-2006 rows plus 60 recent ones, so
#: this date puts 60 bars on the wrong side of it and leaves the negative control (the capture's own
#: first bar, 2006-01-02) at zero.
MOTHERSON_LISTED = "2021-08-18"
#: The first bar of the recorded capture. Supplying it as a listing date is the negative control:
#: a frame genuinely has no rows before its own first bar.
FLOOR = "2006-01-02"
#: Measured on the recorded fixture, not assumed -- :class:`TestNegativeControl` re-derives it.
PRE_LISTING_ROWS = 60

#: The claims that shipped, verbatim, each one a sentence the data does not support. Every one is
#: pure ASCII, so the "is it still here" assertions below are exact substring checks rather than a
#: paraphrase that could drift.
FALSE_CLAIMS = (
    # The attrs note handed to every caller of ohlc().
    "truncated at the instrument's listing",
    "2006-01-02 is the floor for old listings",
    # The module docstring's section heading claim and its conclusion.
    "The history is truncated at listing",
    "so the floor is the listing date, not a window edge",
)


def text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def payload(name: str = "ohlc_motherson.json") -> dict:
    return json.loads(text(name))


def mock_client(handler, **kwargs) -> tl.TrendlyneClient:
    """A client whose every request is served by ``handler`` -- no network, no sockets."""
    kwargs.setdefault("min_request_interval_ms", 0.0)
    kwargs.setdefault("max_retries", 2)
    client = tl.TrendlyneClient(transport=httpx.MockTransport(handler), **kwargs)
    client.load_symbol_index(text("sitemap_excerpt.xml"))
    return client


def live_shaped_handler(calls: list | None = None):
    """Serve the recorded sitemap, page and OHLC payload, in that order."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if calls is not None:
            calls.append(url)
        if "equity-sitemap" in url:
            return httpx.Response(200, text=text("sitemap_excerpt.xml"))
        if "/equity/878/" in request.url.path:
            return httpx.Response(200, text=text("stock_page_motherson.html"))
        return httpx.Response(200, json=payload())
    return handler


# =========================================================== the defect is measurable


class TestPreListingRowsAreDetectable:
    """The replacement for the claim: a number, on the frame, that a caller can act on."""

    def test_a_frame_with_rows_before_the_listing_date_reports_them(self):
        frame = tl.parse_ohlc(payload(), "MOTHERSON", listing_date=MOTHERSON_LISTED)

        assert frame.attrs[tl.ROWS_BEFORE_LISTING_ATTR] == PRE_LISTING_ROWS
        assert frame.attrs[tl.LISTING_DATE_ATTR] == MOTHERSON_LISTED

        # Cross-checked against the frame, so the count cannot drift away from the rows it counts.
        pre = frame[frame.index < pd.Timestamp(MOTHERSON_LISTED)]
        assert len(pre) == PRE_LISTING_ROWS
        assert frame.attrs[tl.VOLUME_BEFORE_LISTING_ATTR] == int(pre["Volume"].sum())
        assert frame.attrs[tl.VOLUME_BEFORE_LISTING_ATTR] > 0

    def test_the_count_is_published_on_every_frame_even_when_nothing_was_measured(self):
        """The key is always written, -1 included, so a consumer can rely on it existing.

        ``-1`` is *not measured*; ``0`` is *measured, none found*. Collapsing the two is how the
        old claim was able to stand: nothing on the frame ever contradicted it.
        """
        frame = tl.parse_ohlc(payload(), "MOTHERSON")
        assert frame.attrs[tl.ROWS_BEFORE_LISTING_ATTR] == -1
        assert frame.attrs[tl.VOLUME_BEFORE_LISTING_ATTR] == -1
        assert frame.attrs[tl.LISTING_DATE_ATTR] is None
        assert tl.ROWS_BEFORE_LISTING_ATTR in frame.attrs
        assert tl.VOLUME_BEFORE_LISTING_ATTR in frame.attrs
        assert tl.LISTING_DATE_ATTR in frame.attrs

    def test_the_rows_are_never_dropped(self):
        """Exposing the count is the whole fix. Silently clipping would be a different package.

        It would also be wrong on the merits: the series is back-adjusted for splits and bonuses, so
        the pre-listing bars are on the same adjusted scale as the rest. Cutting them leaves a
        discontinuity at the cut instead of removing one.
        """
        plain = tl.parse_ohlc(payload(), "MOTHERSON")
        checked = tl.parse_ohlc(payload(), "MOTHERSON", listing_date=MOTHERSON_LISTED)
        assert len(plain) == len(checked) == 125
        assert plain.index.equals(checked.index)
        assert plain["Close"].equals(checked["Close"])

    def test_a_non_zero_count_is_logged_and_not_only_written_to_attrs(self, caplog):
        """A caller who never reads attrs still has to be able to notice."""
        with caplog.at_level(logging.WARNING, logger="bfinance"):
            tl.parse_ohlc(payload(), "MOTHERSON", listing_date=MOTHERSON_LISTED)
        messages = " ".join(record.getMessage() for record in caplog.records)
        assert str(PRE_LISTING_ROWS) in messages
        assert MOTHERSON_LISTED in messages
        assert "96,351,247" in messages, "the volume travels with the count in the warning too"

    @pytest.mark.parametrize("given,expected", [
        (MOTHERSON_LISTED, MOTHERSON_LISTED),
        (date(2021, 8, 18), MOTHERSON_LISTED),
        (datetime(2021, 8, 18), MOTHERSON_LISTED),
        (pd.Timestamp("2021-08-18"), MOTHERSON_LISTED),
        # A timestamp carrying an offset keeps its wall-clock date. A listing is a calendar day,
        # and shifting it across midnight would move the boundary a whole bar.
        (pd.Timestamp("2021-08-18T23:30:00+05:30"), MOTHERSON_LISTED),
        ("2021-08-18T09:15:00", MOTHERSON_LISTED),
    ])
    def test_the_date_forms_a_caller_would_actually_type_all_agree(self, given, expected):
        frame = tl.parse_ohlc(payload(), "MOTHERSON", listing_date=given)
        assert frame.attrs[tl.LISTING_DATE_ATTR] == expected
        assert frame.attrs[tl.ROWS_BEFORE_LISTING_ATTR] == PRE_LISTING_ROWS

    def test_the_first_bar_itself_is_not_counted_as_pre_listing(self):
        """The boundary is inclusive of the listing date: a bar ON the listing day is not before it."""
        frame = tl.parse_ohlc(payload(), "MOTHERSON", listing_date=FLOOR)
        assert frame.index[0] == pd.Timestamp(FLOOR)
        assert frame.attrs[tl.ROWS_BEFORE_LISTING_ATTR] == 0

    @pytest.mark.parametrize("bad", ["not-a-date", "2021-13-45", pd.NaT, None.__class__])
    def test_a_listing_date_that_is_not_a_date_raises_rather_than_counting_zero(self, bad):
        """Counting against a NaT would report 0 rows before a date the caller believes in.

        That is the exact false all-clear this measurement exists to remove, so it is an error
        instead. ``None`` is the one input that is allowed through, and it means *do not check*.
        """
        with pytest.raises(ValueError, match="listing_date"):
            tl.parse_ohlc(payload(), "MOTHERSON", listing_date=bad)


# =========================================================== the false claim is gone


class TestTheFalseClaimIsGone:
    """Fails if the claim is reinstated, in the docstring or in the note a caller receives."""

    @pytest.mark.parametrize("claim", FALSE_CLAIMS)
    def test_the_claim_is_not_in_the_module_source(self, claim):
        assert claim not in CLIENT_SOURCE, (
            f"the note or the docstring says {claim!r} again. There is no listing-date source on "
            f"this route and no clipping code, so the statement is unsupported either way. State "
            f"the measured boundary instead, or publish the count."
        )

    @pytest.mark.parametrize("claim", FALSE_CLAIMS)
    def test_the_claim_is_not_in_the_note_a_caller_receives(self, claim):
        note = tl.parse_ohlc(payload(), "MOTHERSON").attrs[tl.NOTE_ATTR]
        assert claim not in note

    @pytest.mark.parametrize("claim", FALSE_CLAIMS)
    def test_the_claim_is_not_in_the_module_docstring(self, claim):
        assert claim not in MODULE_DOC

    def test_the_needles_are_the_wording_that_actually_shipped(self):
        """Non-vacuity. An "assert not in" test whose needle is a typo passes forever.

        This reconstructs the shipped note and docstring sentences in ASCII and requires every
        needle to be a substring of one of them. If a needle is ever mistyped, it stops matching
        here, which is a failure rather than a silently dead guard.
        """
        shipped = (
            "({bars} bars) truncated at the instrument's listing, so the bar count is "
            "symbol-dependent (2006-01-02 is the floor for old listings; a 2023 IPO returns "
            "a few hundred). Basis: AS-TRADED, back-adjusted for splits and bonuses only; "
            "DEMERGERS ARE NOT APPLIED, so these bars equal the NSE as-traded print. The history "
            "is truncated at listing -- it is not a fixed vendor window. Measured "
            "counter-examples: a 2023 listing cannot have twenty years of history, so the floor "
            "is the listing date, not a window edge."
        )
        assert shipped.isascii()
        for claim in FALSE_CLAIMS:
            assert claim in shipped, f"needle is not the shipped wording: {claim!r}"

    def test_the_note_now_states_the_measured_boundary_instead(self):
        note = tl.parse_ohlc(payload(), "MOTHERSON").attrs[tl.NOTE_ATTR]
        assert "NOT the listing date" in note
        assert tl.ROWS_BEFORE_LISTING_ATTR in note, "the note must name the key the caller reads"
        assert "SBIN" in note and "2,222" in note
        assert "45,561,075,413" in note, (
            "the volume figure is the evidence; without it the claim looks like a rounding "
            "difference in bar counts"
        )
        assert "2015-01-15" in note

    def test_the_note_still_carries_everything_it_carried_before(self):
        """The correction must not be bought by deleting the price-basis warnings."""
        note = tl.parse_ohlc(payload(), "MOTHERSON").attrs[tl.NOTE_ATTR]
        for phrase in (
            "AS-TRADED",
            "DEMERGERS ARE NOT APPLIED",
            "split or bonus",
            "bhavcopy",
            "yfinance",
            "as-traded",
            "symbol-dependent",
        ):
            assert phrase in note, f"the note lost {phrase!r} to the listing-date edit"

    def test_the_module_docstring_keeps_the_measurement_and_its_caveat(self):
        assert "vendor floor" in MODULE_DOC
        assert "45,561,075,413" in MODULE_DOC
        assert "2015-01-15" in MODULE_DOC
        assert "2222" in MODULE_DOC or "2,222" in MODULE_DOC
        assert "no listing-date source" in MODULE_DOC, (
            "the reason there is no gate has to stay in the docstring, or the next agent adds a "
            "hardcoded listing table and re-breaks this"
        )
        assert tl.ROWS_BEFORE_LISTING_ATTR in MODULE_DOC
        assert "BSEcode" in MODULE_DOC, (
            "the plausible-but-unverified explanation for SBIN is the thing that stops a future "
            "agent from presenting the pre-listing bars as either real or fabricated"
        )

    def test_the_listing_date_is_the_callers_claim_and_is_echoed_as_such(self):
        """The frame says whose date it is measuring against, so nobody reads it as a lookup."""
        assert "CALLER" in CLIENT_SOURCE or "caller" in CLIENT_SOURCE
        frame = tl.parse_ohlc(payload(), "MOTHERSON", listing_date=MOTHERSON_LISTED)
        assert frame.attrs[tl.LISTING_DATE_ATTR] == MOTHERSON_LISTED


# =========================================================== negative control


class TestNegativeControl:
    """A frame with no pre-listing rows must report zero -- and must not raise."""

    def test_zero_is_reported_and_nothing_raises(self):
        frame = tl.parse_ohlc(payload(), "MOTHERSON", listing_date=FLOOR)
        assert frame.attrs[tl.ROWS_BEFORE_LISTING_ATTR] == 0
        assert frame.attrs[tl.VOLUME_BEFORE_LISTING_ATTR] == 0
        assert frame.attrs[tl.LISTING_DATE_ATTR] == FLOOR
        assert len(frame) == 125, "a clean frame is still a frame"

    def test_a_zero_count_is_not_an_omission(self):
        """0 and -1 must be distinguishable, and 0 must be an ``int`` from a real count."""
        zero = tl.parse_ohlc(payload(), "MOTHERSON", listing_date=FLOOR)
        unchecked = tl.parse_ohlc(payload(), "MOTHERSON")
        assert zero.attrs[tl.ROWS_BEFORE_LISTING_ATTR] != unchecked.attrs[tl.ROWS_BEFORE_LISTING_ATTR]
        assert isinstance(zero.attrs[tl.ROWS_BEFORE_LISTING_ATTR], int)
        assert zero.attrs[tl.ROWS_BEFORE_LISTING_ATTR] == 0
        assert unchecked.attrs[tl.ROWS_BEFORE_LISTING_ATTR] == -1

    def test_the_zero_is_measured_and_not_a_coincidence(self):
        """The same frame, the same code path, a different date: a non-zero count.

        This is what makes the zero above a measurement rather than a constant that always comes
        out 0, which would have satisfied both the old claim and this test.
        """
        frame = tl.parse_ohlc(payload(), "MOTHERSON", listing_date=MOTHERSON_LISTED)
        assert frame.attrs[tl.ROWS_BEFORE_LISTING_ATTR] == PRE_LISTING_ROWS > 0

    def test_a_listing_date_after_the_whole_series_counts_every_row(self):
        frame = tl.parse_ohlc(payload(), "MOTHERSON", listing_date="2099-01-01")
        assert frame.attrs[tl.ROWS_BEFORE_LISTING_ATTR] == len(frame) == 125
        assert frame.attrs[tl.VOLUME_BEFORE_LISTING_ATTR] == int(frame["Volume"].sum())

    def test_the_negative_control_emits_no_warning(self, caplog):
        """Non-vacuous: the same caplog block proves the warning fires when it should.

        An assertion that a message is absent passes just as happily when nothing was captured at
        all, so the positive case runs first in the same block and has to be there.
        """
        with caplog.at_level(logging.WARNING, logger="bfinance"):
            tl.parse_ohlc(payload(), "MOTHERSON", listing_date=MOTHERSON_LISTED)
            dirty = len(caplog.records)
            tl.parse_ohlc(payload(), "MOTHERSON", listing_date=FLOOR)
        joined = " ".join(record.getMessage() for record in caplog.records)
        assert dirty >= 1, "the positive control produced no warning, so the check below is vacuous"
        assert "listing date" in joined
        # Only the dirty frame warned. The clean frame added no second record.
        assert len(caplog.records) == dirty, (
            f"a frame with no pre-listing rows emitted {[r.getMessage() for r in caplog.records[dirty:]]}"
        )


# =========================================================== threaded through the client


class TestClientThreading:
    """``ohlc``/``ohlc_async`` are the only public door to a frame, so the argument has to reach
    :func:`parse_ohlc` through both of them and both their module-level shortcuts."""

    async def test_ohlc_async_publishes_the_count(self):
        calls: list = []
        client = mock_client(live_shaped_handler(calls))
        frame = await client.ohlc_async("MOTHERSON", listing_date=MOTHERSON_LISTED)
        assert frame.attrs[tl.ROWS_BEFORE_LISTING_ATTR] == PRE_LISTING_ROWS
        assert frame.attrs[tl.LISTING_DATE_ATTR] == MOTHERSON_LISTED
        assert len(frame) == 125
        # The sitemap is pre-loaded by the fixture, so a listing date costing a request would show
        # up here as a third call. Counting rows is not a network operation.
        assert len(calls) == 2, f"page + api only, got {calls}"

    async def test_ohlc_async_without_a_listing_date_still_writes_the_keys(self):
        client = mock_client(live_shaped_handler())
        frame = await client.ohlc_async("MOTHERSON")
        assert frame.attrs[tl.ROWS_BEFORE_LISTING_ATTR] == -1
        assert frame.attrs[tl.LISTING_DATE_ATTR] is None

    async def test_a_rejected_token_retry_keeps_the_listing_measurement(self):
        """The retry path rebuilds the frame from a second payload and must not drop the argument.

        A keyword that is threaded through the happy path only is the ordinary way a measurement
        silently disappears from half the frames a caller receives.
        """
        # Cached-but-wrong token: the api rejects it, the page yields a different one, the retry
        # with the new one succeeds. The retry rebuilds the frame from a second parse_ohlc call.
        state = {"valid": "TOKENB======", "pages": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "equity-sitemap" in url:
                return httpx.Response(200, text=text("sitemap_excerpt.xml"))
            if "/equity/878/" in request.url.path:
                state["pages"] += 1
                page = text("stock_page_motherson.html").replace(
                    "TQZITAIRHO5FCC7SYAKGO6M7QA======", "TOKENB======"
                )
                return httpx.Response(200, text=page)
            if state["valid"] in url:
                return httpx.Response(200, json=payload())
            return httpx.Response(200, json={
                "head": {"status": "2", "statusDescription": "Not Authorized !"}, "body": {}})

        client = mock_client(handler)
        import time as _time
        client._tokens[878] = (_time.monotonic(), "TOKEN_A======", {})

        frame = await client.ohlc_async("MOTHERSON", listing_date=MOTHERSON_LISTED)
        assert state["pages"] == 1, "the retry path is what this test is about"
        assert frame.attrs[tl.ROWS_BEFORE_LISTING_ATTR] == PRE_LISTING_ROWS
        assert frame.attrs[tl.LISTING_DATE_ATTR] == MOTHERSON_LISTED

    def test_the_sync_wrapper_forwards_the_listing_date(self):
        client = mock_client(live_shaped_handler())
        frame = client.ohlc("MOTHERSON", listing_date=MOTHERSON_LISTED)
        assert frame.attrs[tl.ROWS_BEFORE_LISTING_ATTR] == PRE_LISTING_ROWS

    def test_the_module_level_shortcuts_accept_the_keyword(self):
        """Signature check only -- the module-level shortcuts use a shared live client, so calling
        them here would be a network request."""
        import inspect

        for fn in (tl.ohlc, tl.ohlc_async, tl.TrendlyneClient.ohlc, tl.TrendlyneClient.ohlc_async):
            assert "listing_date" in inspect.signature(fn).parameters, fn.__qualname__

    def test_ohlc_async_is_still_a_coroutine_function(self):
        """Unrelated to listing dates, but the async entry point is load-bearing for adapter code
        that awaits it, and this file is the one that touches both signatures."""
        import inspect

        assert inspect.iscoroutinefunction(tl.TrendlyneClient.ohlc_async)


# =========================================================== the shape of the fix


class TestNoSilentGate:
    """What the package must NOT have grown while fixing the claim."""

    def test_there_is_no_listing_table_to_hardcode(self):
        """A module-level *table* of listing dates would be the same defect with a worse smell: a
        hardcoded table that is wrong is a false claim that a caller also cannot correct, because
        the package would keep answering with its own copy. The caller supplies the date, so there
        is nothing here to be wrong. The attr-name constants are string keys, not tables, and are
        allowed."""
        import ast

        tree = ast.parse(CLIENT_SOURCE)
        tables = []
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if not isinstance(value, (ast.Dict, ast.List, ast.Set, ast.Tuple)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and "LISTING" in target.id.upper():
                    tables.append(target.id)
        assert tables == [], (
            f"a module-level listing table appeared: {tables}. This package has no listing-date "
            f"source, so a table can only be an unverified guess."
        )

    def test_no_public_symbol_claims_to_know_the_listing_date(self):
        assert not hasattr(tl, "LISTING")
        assert not hasattr(tl, "LISTING_DATES")
        assert not hasattr(tl, "LISTING_DATE")

    def test_the_attr_names_are_stable_and_prefixed_like_every_other_one(self):
        for name in (tl.LISTING_DATE_ATTR, tl.ROWS_BEFORE_LISTING_ATTR,
                     tl.VOLUME_BEFORE_LISTING_ATTR):
            assert name.startswith("bfinance_trendlyne_")
            assert name == name.lower()
        assert tl.ROWS_BEFORE_LISTING_ATTR == "bfinance_trendlyne_rows_before_listing"

    def test_the_count_survives_a_column_slice_like_every_other_attr(self):
        """Consumers slice frames constantly; a measurement that vanishes on the first slice is not
        a measurement."""
        frame = tl.parse_ohlc(payload(), "MOTHERSON", listing_date=MOTHERSON_LISTED)
        assert frame[["Close"]].attrs[tl.ROWS_BEFORE_LISTING_ATTR] == PRE_LISTING_ROWS
        assert frame.head(3).attrs[tl.ROWS_BEFORE_LISTING_ATTR] == PRE_LISTING_ROWS
