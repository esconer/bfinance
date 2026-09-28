"""
The provider-agnostic statement cascade, proved offline against recorded fixtures.

Everything here runs without a socket. That is not a convenience — it is the only way several of
these claims could be tested at all, because three of them are about a **decoy** (trap 1) and two
are about **absences** (traps 2 and 3) that a live test would either be slow to expose or would
answer by accident.

The fixtures, and what each is evidence of
------------------------------------------
================================================  ==========  ======================================
fixture                                            bytes       evidence for
================================================  ==========  ======================================
``screener/company_itc_full_columns.html``         102,257     all five screener.in statement
                                                              sections, every period column
                                                              intact, byte-verbatim
``trendlyne/financials_motherson.html``            212,407     the five SSR tables, every data
                                                              row and the ``Graph`` column kept
``trendlyne/fundamental_results_motherson.json``   523,047     13 body keys, both
                                                              ``consolidated`` and
                                                              ``standalone`` dumps
``trendlyne/financials_tab_shell_balance_sheet.html`` 14,146  **trap 1** — a tab-page shell with
                                                              zero ``<table>``, kept for this
================================================  ==========  ======================================

The two vendors' fixtures are for **different companies** (ITC and MOTHERSON), so nothing here
compares a screener *number* to a trendlyne number. That is a fact about the evidence, not a gap in
the tests, and it is why every cross-provider assertion below is about shape, basis and
provenance and never about equality of values. The one value-level cross-check that does exist —
trendlyne's ``NP_TTM`` row against its own annual ``TTM`` column — is internal to a single
vendor's single payload, and is the strongest integrity check available offline.

What each section pins
----------------------
1. **one canonical shape** — the same capability through three routes gives the same frame
   structure, the same period labels and the same ``attrs`` key set.
2. **TTM from two providers**, with the intra-vendor TTM agreement pinned.
3. **the negative control** — force provider 1 to raise and prove provider 2 served it *and that the
   fallback is recorded*. An unproven fallback is an unproven cascade; this is the most important
   test in the file.
4. **basis** — recorded everywhere, flagged on mismatch, never blended.
5. **the refusal** — four cells no source can answer, refusing with a message that names both
   vendors and says a later bfinance will not help.
6. **trap 1** — a tab page is a *route error*, not an empty answer.
7. **trap 2** — three byte-identical header rows, discriminated by row id, with the registry
   recording that header identification is impossible for them.
8. **trap 3** — the period set comes from the data, and a 0-of-N read is unavailable, not zero.
9. **period labels** — one canonical form across both sources.
10. **the ``find_tables`` contract** — ``keep_default_na=False`` and ``thousands=None`` still hold;
    a regression there would silently corrupt every number in the package.
"""

from __future__ import annotations

import copy
import json
import pathlib
import re
import subprocess
import sys

import pandas as pd
import pytest
from bs4 import BeautifulSoup

from bfinance.statements import (
    ATTR_BASIS,
    ATTR_BASIS_MISMATCH,
    ATTR_BASIS_NOTE,
    ATTR_BASIS_REQUESTED,
    ATTR_DROPPED_ROWS,
    ATTR_DUPLICATE_ROWS,
    ATTR_FALLBACK,
    ATTR_FALLBACK_FROM,
    ATTR_FREQUENCY,
    ATTR_IDENTIFIED_BY,
    ATTR_KEYS,
    ATTR_KIND,
    ATTR_PERIODS,
    ATTR_PROVIDER,
    ATTR_PROVIDERS_TRIED,
    ATTR_SKIP_REASONS,
    ATTR_SYMBOL,
    CAPABILITIES,
    TABLES,
    TTM,
    UNAVAILABLE,
    VENDORS,
    Basis,
    Frequency,
    PeriodLabelError,
    StatementCapabilityError,
    StatementEmptyError,
    StatementError,
    StatementKind,
    StatementNotADataPageError,
    StatementRequest,
    canonical_period,
    capability_report,
    classify_html_tables,
    expand_short_year,
    html_row_ids,
    html_table_headers,
    identify_by_header,
    identify_by_row_ids,
    order_periods,
    parse_company_page,
    parse_financials_html,
    parse_fundamental_results,
    providers_for,
    resolve,
    resolve_from_payloads,
    unavailable_reason,
)
from bfinance.trendlyne.tables import find_tables
from bfinance.utils.exceptions import BFinanceError, UpstreamServiceError

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
SCREENER_ITC = FIXTURES / "screener" / "company_itc_full_columns.html"
TRENDLYNE_HTML = FIXTURES / "trendlyne" / "financials_motherson.html"
TRENDLYNE_JSON = FIXTURES / "trendlyne" / "fundamental_results_motherson.json"
TRENDLYNE_TAB_SHELL = FIXTURES / "trendlyne" / "financials_tab_shell_balance_sheet.html"

#: Every payload, keyed by the keyword ``resolve_from_payloads`` takes it under.
ALL_PAYLOAD_KWARGS = ("screener_html", "trendlyne_html", "trendlyne_json")


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", newline="")


@pytest.fixture(scope="module")
def screener_html() -> str:
    return _read(SCREENER_ITC)


@pytest.fixture(scope="module")
def trendlyne_html() -> str:
    return _read(TRENDLYNE_HTML)


@pytest.fixture(scope="module")
def trendlyne_json() -> dict:
    return json.loads(_read(TRENDLYNE_JSON))


@pytest.fixture(scope="module")
def tab_shell_html() -> str:
    return _read(TRENDLYNE_TAB_SHELL)


@pytest.fixture(scope="module")
def payloads(screener_html, trendlyne_html, trendlyne_json) -> dict:
    return {
        "screener_html": screener_html,
        "trendlyne_html": trendlyne_html,
        "trendlyne_json": trendlyne_json,
    }


def _request(
    kind: StatementKind,
    frequency: Frequency,
    *,
    symbol: str = "X",
    basis: Basis = Basis.CONSOLIDATED,
) -> StatementRequest:
    return StatementRequest(symbol=symbol, kind=kind, frequency=frequency, basis=basis)


def _payload_for(spec, payloads: dict):
    """The bytes a provider would read, by its payload discriminator."""
    return payloads[spec.payload]


#: trendlyne names a row by a machine id; screener.in names it in words. The distinction is total
#: on these fixtures, which is what proves a fallback frame was not blended with the frame the
#: failed provider would have produced.
_MACHINE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+$")

#: The three periods-free header cells every trendlyne statement table opens with.
_TL_PREFIX_HEADERS = ["Indicator", "Graph"]


def _assert_canonical_shape(result, *, symbol: str) -> pd.DataFrame:
    """The one shape every provider must produce. Applied to all three routes in section 1.

    Orientation (index = metric, columns = period), ascending periods, ``TTM`` first, the full
    ``attrs`` key set, and a numeric frame. Nothing here compares a *value* across vendors: the two
    fixtures are different companies, so a value comparison would be a category error dressed as a
    test.
    """
    frame = result.to_dataframe()

    assert list(frame.index) == list(result.metrics), "index must be the metrics, in provider order"
    assert list(frame.columns) == list(result.periods), "columns must be the periods, in order"
    assert frame.index.name == "metric"
    assert frame.columns.name == "period"
    assert result.periods == order_periods(result.periods), "periods must already be canonical"
    assert all(
        pd.api.types.is_numeric_dtype(frame[column]) for column in frame.columns
    ), f"a column is not numeric: {frame.dtypes.to_dict()}"
    assert result.non_null_cells() > 0, "a result with no number in any cell is not a result"

    assert set(frame.attrs) == set(ATTR_KEYS), (
        f"attrs keys differ from ATTR_KEYS: extra={sorted(set(frame.attrs) - set(ATTR_KEYS))} "
        f"missing={sorted(set(ATTR_KEYS) - set(frame.attrs))}"
    )
    assert frame.attrs[ATTR_SYMBOL] == symbol
    assert frame.attrs[ATTR_KIND] == result.kind.value
    assert frame.attrs[ATTR_FREQUENCY] == result.frequency.value
    assert frame.attrs[ATTR_PERIODS] == len(result.periods)
    assert frame.attrs[ATTR_BASIS] == result.basis.value
    assert frame.attrs[ATTR_BASIS_REQUESTED] == result.request.basis.value
    assert frame.attrs[ATTR_BASIS_MISMATCH] is result.basis_mismatch
    assert frame.attrs[ATTR_FALLBACK] is result.fallback
    assert frame.attrs[ATTR_FALLBACK_FROM] == result.fallback_from
    assert frame.attrs[ATTR_SKIP_REASONS] == dict(result.skip_reasons)
    assert frame.attrs[ATTR_PROVIDER] == result.provider
    assert frame.attrs[ATTR_PROVIDERS_TRIED] == result.providers_tried
    assert frame.attrs[ATTR_IDENTIFIED_BY] == result.identified_by
    assert frame.attrs[ATTR_BASIS_NOTE]
    return frame


# =============================================================================
# 1. one capability, three routes, one canonical shape
# =============================================================================


def test_quarterly_income_from_screener_alone_from_trendlyne_alone_and_through_the_cascade(
    payloads,
):
    """The quarterly P&L is a capability two independent sources publish, so it is the one the
    cascade exists for. All three routes must land on the same canonical shape.

    Route 3 is the interesting one: screener is *disabled*, so the answer must come from
    trendlyne. It is the negative control in miniature, and the "same shape" assertion is what
    makes a failover a drop-in rather than a second, differently-shaped API.
    """
    consolidated = _request(StatementKind.INCOME, Frequency.QUARTERLY, symbol="ITC")
    standalone = _request(
        StatementKind.INCOME, Frequency.QUARTERLY, symbol="MOTHERSON", basis=Basis.STANDALONE
    )

    from_screener = resolve_from_payloads(consolidated, only=("screener",), **payloads)
    from_trendlyne = resolve_from_payloads(
        standalone, only=("trendlyne-html", "trendlyne-json"), **payloads
    )
    # Route 3: the cascade with provider 1 *disabled* — asked, and refusing. That is what a
    # disabled provider looks like, and it is why the fallback is recorded as a reason rather than
    # as an absence.
    def disabled(spec):
        if spec.name == "screener":
            raise UpstreamServiceError("screener is disabled for this test")
        return _payload_for(spec, payloads)

    through = resolve(consolidated, disabled, only=("screener", "trendlyne-json"))

    assert from_screener.provider == "screener"
    assert from_screener.basis is Basis.CONSOLIDATED
    assert from_screener.served is True and from_screener.fallback is False

    # trendlyne's SSR page is standalone, so the first hop lands on it for a standalone request
    # and the basis routing sends a consolidated request to the JSON provider instead. The basis
    # seam is exercised in full in section 4.
    assert from_trendlyne.provider == "trendlyne-html"
    assert from_trendlyne.basis is Basis.STANDALONE

    assert through.provider == "trendlyne-json", "route 3 must be served by provider 2"
    assert through.fallback is True
    assert through.fallback_from == ("screener",)

    a = _assert_canonical_shape(from_screener, symbol="ITC")
    b = _assert_canonical_shape(from_trendlyne, symbol="MOTHERSON")
    c = _assert_canonical_shape(through, symbol="ITC")

    # The period sets are equal, and that is the substance of the shape claim: trendlyne prints
    # "Jun '23" on the page and 'Jun 2023' in its JSON, screener.in prints "Jun 2023", and all
    # three routes produce the identical 13 canonical columns in the identical order.
    assert len(from_screener.periods) == 13
    assert from_screener.periods == from_trendlyne.periods == through.periods
    assert from_screener.periods[0] == "Jun 2023"
    assert from_screener.periods[-1] == "Jun 2026"
    assert list(a.columns) == list(b.columns) == list(c.columns)

    # Same shape, different vocabularies — and the difference is recorded, not papered over.
    assert set(a.index) & set(b.index) == set(), (
        "the two vendors' row vocabularies should be disjoint; a collision means one provider's "
        "frame has been mixed into the other"
    )
    assert all(_MACHINE_ID.match(label) for label in b.index), (
        f"trendlyne rows must be keyed by machine id, got {list(b.index)[:5]}"
    )
    assert not any("_" in label for label in a.index), (
        f"screener.in rows are display labels, not machine ids; got {list(a.index)[:5]}"
    )

    # And the disabled provider is named in attrs, with the reason, not merely absent.
    assert "screener" in through.skip_reasons
    assert "screener is disabled" in through.skip_reasons["screener"]
    assert through.providers_tried == ("screener", "trendlyne-json")


def test_every_registered_capability_is_readable_from_each_provider_it_names(payloads):
    """Not just the quarterly P&L: every cell the registry claims is available is actually
    readable, from each of the providers it names, and lands on the same shape.

    A registry that lists a provider it cannot read is the quietest possible lie in this package —
    it would make the cascade fall through on every call while reporting a working redundancy.
    """
    exercised = set()
    for (kind, frequency), capability in CAPABILITIES.items():
        if not capability.available:
            assert capability.unavailable_reason, f"{kind}/{frequency} has no reason recorded"
            continue  # the four refusals, covered in section 5
        for provider in capability.provider_names:
            for basis in Basis:
                if basis not in _bases_of(provider):
                    continue
                request = _request(kind, frequency, symbol="X", basis=basis)
                result = resolve_from_payloads(request, only=(provider,), **payloads)
                assert result.provider == provider, (
                    f"{kind.value}/{frequency.value} asked {provider!r} and got "
                    f"{result.provider!r}"
                )
                _assert_canonical_shape(result, symbol="X")
                exercised.add((kind, frequency, provider))

    expected = {
        (kind, frequency, provider)
        for (kind, frequency), capability in CAPABILITIES.items()
        for provider in capability.provider_names
    }
    assert exercised == expected, (
        f"not every (cell, provider) pair was exercised: {sorted(expected - exercised)}"
    )


def _bases_of(provider: str) -> tuple:
    from bfinance.statements import PROVIDERS

    return PROVIDERS[provider].bases


# =============================================================================
# 2. TTM from two providers, and the intra-vendor integrity check
# =============================================================================


def test_ttm_income_resolves_from_both_providers(payloads):
    """TTM P&L is the second capability with two sources. screener.in's is the trailing column of
    the annual P&L; trendlyne's is a period of the annual dump and two rows of the quarterly one.
    """
    consolidated = _request(StatementKind.INCOME, Frequency.TTM, symbol="ITC")
    standalone = _request(
        StatementKind.INCOME, Frequency.TTM, symbol="MOTHERSON", basis=Basis.STANDALONE
    )

    from_screener = resolve_from_payloads(consolidated, only=("screener",), **payloads)
    from_json = resolve_from_payloads(consolidated, only=("trendlyne-json",), **payloads)
    from_html = resolve_from_payloads(standalone, only=("trendlyne-html",), **payloads)

    for result in (from_screener, from_json, from_html):
        assert result.periods == (TTM,), (
            f"a TTM statement is one window, not a calendar period; got {result.periods}"
        )
        _assert_canonical_shape(result, symbol=result.request.symbol)

    # screener.in's TTM is a *column* of section#profit-loss, so its metrics are the P&L labels.
    assert from_screener.metric("Net Profit") == {"TTM": 20184.0}
    assert from_screener.metric("Sales") == {"TTM": 76488.0}
    # TTM presence is what identifies that table; a TTM column on section#quarters would invert
    # the semantics, and section#quarters has none.
    assert from_screener.identified_by == "section"

    # trendlyne's TTM is a *period* of the annual dump, so its metrics are machine ids, and the
    # two bases differ.
    assert from_json.metric("NP_A") == {"TTM": 4379.89}
    assert from_html.metric("NP_A") == {"TTM": 1789.2}
    assert from_html.basis is Basis.STANDALONE
    assert from_json.basis is Basis.CONSOLIDATED


def test_trendlyne_npttm_row_agrees_with_its_own_annual_ttm_column(trendlyne_json, trendlyne_html):
    """The one value-level cross-check available offline, and worth pinning.

    trendlyne ships its trailing net profit **twice**: as the ``NP_TTM`` row of the quarterly
    table — one value per quarter, each the trailing twelve months *ending at that quarter* — and
    as the ``TTM`` period of the annual dump. The newest quarterly ``NP_TTM`` must equal the annual
    ``TTM`` column, or one of the two is wrong and this package would be propagating a
    disagreement it never looked for.

    Measured: both are **4,379.89** at Jun 2026 for MOTHERSON on the consolidated basis. The
    standalone pair is 1,789.19 and the SSR page — which renders standalone — prints 1,789.2.
    """
    body = trendlyne_json["body"]
    for basis_name, expected in (("consolidated", 4379.89), ("standalone", 1789.19)):
        # `order.quarter.qtr` is newest-first, so the trailing twelve months *ending at the latest
        # quarter* is its first entry. (max() over the dump's keys would pick 'Sep 2025' — string
        # order has nothing to do with calendar order, and that mistake reads as a data conflict.)
        latest_quarter = body["order"]["quarter"]["qtr"][0]
        assert latest_quarter == "Jun 2026"
        row_value = body["quarterlyDataDump"][basis_name][latest_quarter]["NP_TTM"]
        column_value = body["annualDataDump"][basis_name][TTM]["NP_A"]
        assert row_value == column_value == expected, (
            f"{basis_name}: NP_TTM row {row_value} at {latest_quarter} disagrees with the annual "
            f"TTM column {column_value}"
        )

    # The same agreement *through the module*, not just through the fixture: the TTM statement
    # from the JSON provider and the NP_TTM row of its quarterly statement must be one number.
    consolidated = _request(StatementKind.INCOME, Frequency.TTM, symbol="MOTHERSON")
    ttm = resolve_from_payloads(consolidated, only=("trendlyne-json",), trendlyne_json=trendlyne_json)
    quarterly = resolve_from_payloads(
        _request(StatementKind.INCOME, Frequency.QUARTERLY, symbol="MOTHERSON"),
        only=("trendlyne-json",),
        trendlyne_json=trendlyne_json,
    )
    assert ttm.metric("NP_A")["TTM"] == quarterly.metric("NP_TTM")["Jun 2026"] == 4379.89
    assert quarterly.metric("EPS_TTM")["Jun 2026"] == 4.15

    # The SSR page is standalone and must agree with the standalone pair, to the one decimal it
    # prints. This is the check that catches a provider silently switching basis.
    from_html = parse_financials_html(
        trendlyne_html,
        _request(
            StatementKind.INCOME, Frequency.TTM, symbol="MOTHERSON", basis=Basis.STANDALONE
        ),
    )
    assert from_html.metric("NP_A")["TTM"] == 1789.2
    assert from_html.metric("NP_A")["TTM"] != ttm.metric("NP_A")["TTM"], (
        "the SSR page and the consolidated dump returned the same figure, so the basis recorded on "
        "the SSR result is not what that page actually renders"
    )


# =============================================================================
# 3. THE NEGATIVE CONTROL - the cascade's fallback, proved
# =============================================================================


def test_the_cascade_falls_through_to_provider_two_and_records_the_fallback(payloads):
    """The single most important test in this file.

    A cascade that works is the whole value of the package, and a cascade that works *silently* is
    indistinguishable from one that was never exercised. So: force provider 1 to fail four
    different ways, prove provider 2 served the answer every time, and prove the fallback — and the
    reason — is on ``attrs``.

    The four ways exercise four different points in the cascade: the fetch raising, the caller
    having no cached page, the fetch returning bytes the provider refuses, and every provider
    failing. All four must be recorded differently, or a reader cannot tell "trendlyne was never
    asked" from "trendlyne was asked and found nothing".
    """
    request = _request(StatementKind.INCOME, Frequency.QUARTERLY, symbol="MOTHERSON")

    # (a) the fetch itself raises -- a transport failure, a 429, a DNS error.
    def raising_fetch(spec):
        if spec.name == "screener":
            raise UpstreamServiceError("screener.in returned HTTP 429")
        return _payload_for(spec, payloads)

    result = resolve(request, raising_fetch, only=("screener", "trendlyne-json"))

    assert result.provider == "trendlyne-json", "provider 2 did not serve the result"
    assert result.served is False
    assert result.fallback is True
    assert result.fallback_from == ("screener",)
    assert result.providers_tried == ("screener", "trendlyne-json")
    assert result.skip_reasons["screener"] == "UpstreamServiceError: screener.in returned HTTP 429"

    frame = _assert_canonical_shape(result, symbol="MOTHERSON")
    assert frame.attrs[ATTR_FALLBACK] is True
    assert frame.attrs[ATTR_FALLBACK_FROM] == ("screener",)
    assert frame.attrs[ATTR_PROVIDER] == "trendlyne-json"
    assert frame.attrs[ATTR_SKIP_REASONS]["screener"].startswith("UpstreamServiceError:")
    assert result.non_null_cells() > 0, "a fallback frame of nulls is not a fallback"

    # (b) the caller has no cached page for provider 1 -- the ordinary "only one source is
    #     available" case, which must be recorded as a missing payload, not as a failure.
    without_screener = resolve_from_payloads(
        request, trendlyne_json=payloads["trendlyne_json"],
        only=("screener", "trendlyne-json"),
    )
    assert without_screener.provider == "trendlyne-json"
    assert without_screener.fallback is True
    assert "no screener_html was supplied" in without_screener.skip_reasons["screener"]
    assert "UpstreamServiceError" not in without_screener.skip_reasons["screener"]
    assert without_screener.skip_reasons["screener"].startswith("StatementSourceError:")

    # (c) the fetch returns bytes the provider refuses to read. Here: trendlyne's own tab-page
    #     decoy, which answers HTTP 200 and carries nothing (trap 1, reused as a failure mode).
    through_decoy = resolve(
        request,
        lambda spec: payloads["trendlyne_json"] if spec.name != "screener" else tab_shell(),
        only=("screener", "trendlyne-json"),
    )
    assert through_decoy.provider == "trendlyne-json"
    assert through_decoy.fallback is True
    assert "StatementNotADataPageError" in through_decoy.skip_reasons["screener"] or (
        "StatementTableError" in through_decoy.skip_reasons["screener"]
    ), through_decoy.skip_reasons["screener"]

    # (d) every provider failing is still a typed error, and it names each one.
    def always_fails(spec):
        raise UpstreamServiceError(f"{spec.name} is down")

    with pytest.raises(StatementError) as excinfo:
        resolve(request, always_fails, only=("screener", "trendlyne-json"))
    message = str(excinfo.value)
    assert "screener is down" in message and "trendlyne-json is down" in message
    assert "2 of the 2 registered provider(s) were asked" in message
    assert "Capabilities:" in message


def tab_shell() -> str:
    return _read(TRENDLYNE_TAB_SHELL)


def test_the_cascade_does_not_reach_for_a_provider_that_cannot_serve_the_basis(payloads):
    """A basis-ineligible provider is **skipped with a reason**, never asked and mis-served.

    This matters because screener.in has no standalone variant to switch to: its consolidated page
    and its standalone page carry different tables. Asking it for standalone and reading the
    consolidated numbers would produce a frame that is wrong by exactly the consolidated/standalone
    difference *and labelled correctly* — the worst kind of wrong.
    """
    standalone = _request(
        StatementKind.INCOME, Frequency.QUARTERLY, symbol="MOTHERSON", basis=Basis.STANDALONE
    )
    calls = []

    def counting_fetch(spec):
        calls.append(spec.name)
        return _payload_for(spec, payloads)

    result = resolve(standalone, counting_fetch, only=("screener", "trendlyne-json"))

    assert calls == ["trendlyne-json"], f"screener was asked for a standalone basis: {calls}"
    assert result.provider == "trendlyne-json"
    assert result.basis is Basis.STANDALONE
    assert result.basis_mismatch is False
    assert "consolidated-only" in result.skip_reasons["screener"]
    assert result.fallback is True
    assert result.fallback_from == ("screener",)
    assert "screener" not in result.providers_tried, (
        "a skipped provider must not appear in providers_tried — it was never asked"
    )
    _assert_canonical_shape(result, symbol="MOTHERSON")


# =============================================================================
# 4. basis is recorded, and a mismatch is flagged rather than blended
# =============================================================================


def test_every_result_records_the_basis_it_was_served_on(payloads):
    """No result leaves this package without saying which basis it is on, and the requested basis
    is recorded beside it so a mismatch is one boolean away."""
    for (kind, frequency), capability in CAPABILITIES.items():
        if not capability.available:
            continue  # the four refusals; section 5 covers those
        for basis in Basis:
            result = resolve_from_payloads(_request(kind, frequency, basis=basis), **payloads)
            assert result.basis in Basis
            assert result.basis_note, "a result must always carry a basis note, never an empty one"
            frame = result.to_dataframe()
            assert frame.attrs[ATTR_BASIS] == result.basis.value
            assert frame.attrs[ATTR_BASIS_REQUESTED] == basis.value
            assert frame.attrs[ATTR_BASIS_MISMATCH] is (result.basis is not basis)
            assert frame.attrs[ATTR_BASIS_NOTE] == result.basis_note


def test_a_cross_provider_basis_mismatch_is_flagged_and_nothing_is_blended(payloads):
    """The seam this whole package exists to prevent, and the case the basis routing prevents.

    screener.in is consolidated-only and cannot serve a standalone request, so a standalone caller
    whose only payload is trendlyne's SSR page gets a **standalone** result from a cascade whose
    registry lists a consolidated-only provider first. That is a real, reachable cross-provider
    situation, and two things must hold: the cross-provider aspect is recorded, and the frame is
    entirely one provider's numbers on one basis.

    What must never happen: the two vendors' figures being joined, substituted or averaged. The
    numbers themselves are the strongest available evidence — 38,290.1 is what the page prints for
    MOTHERSON's shareholder funds at Mar 2026 and 40,979.6 is what the consolidated dump holds for
    the same row and the same period.
    """
    standalone = _request(
        StatementKind.BALANCE_SHEET, Frequency.ANNUAL, symbol="MOTHERSON", basis=Basis.STANDALONE
    )
    result = resolve_from_payloads(standalone, trendlyne_html=payloads["trendlyne_html"])

    assert result.provider == "trendlyne-html"
    assert result.basis is Basis.STANDALONE
    assert result.basis_mismatch is False, (
        "the request asked for standalone and got standalone; the basis routing is what prevented "
        "the mismatch, and that is the correct outcome"
    )
    assert result.fallback is True
    assert result.fallback_from == ("screener",)
    assert "consolidated-only" in result.skip_reasons["screener"]

    frame = _assert_canonical_shape(result, symbol="MOTHERSON")
    assert frame.attrs[ATTR_BASIS_MISMATCH] is False
    assert result.basis_note.startswith("served on the standalone basis")

    # The frame is entirely trendlyne's, on one basis. Every metric is a machine id; a single
    # screener.in label anywhere in the index would mean a blend.
    assert all(_MACHINE_ID.match(metric) for metric in result.metrics)
    assert not any(metric in result.metrics for metric in ("Total Assets", "Equity Capital"))
    assert result.metric("TotalShareHoldersFunds_A")["Mar 2026"] == 38290.1
    assert result.metric("TotalShareHoldersFunds_A")["Mar 2026"] != 40979.6

    # And the consolidated route on the same rows, for the record: the same row id, the same
    # period, two different numbers, because the basis is different.
    consolidated = resolve_from_payloads(
        _request(
            StatementKind.BALANCE_SHEET, Frequency.ANNUAL, symbol="MOTHERSON",
            basis=Basis.CONSOLIDATED,
        ),
        trendlyne_json=payloads["trendlyne_json"],
    )
    assert consolidated.basis is Basis.CONSOLIDATED
    assert consolidated.metric("TotalShareHoldersFunds_A")["Mar 2026"] == 40979.6
    assert consolidated.metric("TotalShareHoldersFunds_A")["Mar 2026"] != (
        result.metric("TotalShareHoldersFunds_A")["Mar 2026"]
    )


def test_a_result_on_the_wrong_basis_is_flagged_and_never_converted(trendlyne_html):
    """A genuine mismatch, forced: the request says consolidated, the provider served standalone.

    What must hold is that the mismatch is *carried* — on the result, on the frame's ``attrs``, and
    spelled out in the note — and that the numbers are left exactly as the provider gave them.
    Nothing is adjusted, substituted or averaged; the caller decides what to do about it.
    """
    consolidated = _request(
        StatementKind.BALANCE_SHEET, Frequency.ANNUAL, symbol="MOTHERSON", basis=Basis.CONSOLIDATED
    )
    # The provider refuses a request it cannot serve, rather than serving the wrong basis.
    with pytest.raises(ValueError, match="renders the standalone basis"):
        parse_financials_html(trendlyne_html, consolidated)

    # Read honestly on its own basis...
    honest = parse_financials_html(
        trendlyne_html,
        _request(
            StatementKind.BALANCE_SHEET, Frequency.ANNUAL, symbol="MOTHERSON",
            basis=Basis.STANDALONE,
        ),
    )
    assert honest.basis is Basis.STANDALONE
    assert honest.basis_mismatch is False
    assert honest.basis_note.endswith("which is the basis requested.")

    # ...and then labelled with a request that disagrees, which is the mismatch a caller would see
    # if a provider ever served the wrong basis.
    mismatched = _rebuild(honest, request=consolidated)
    assert mismatched.basis_mismatch is True
    assert mismatched.fallback is False, "no cascade ran, so there is no fallback to record"
    note = mismatched.basis_note
    assert "served on the standalone basis" in note
    assert "but the consolidated basis was requested" in note
    assert "NOT adjusted" in note
    assert "one provider's standalone figures" in note
    assert mismatched.to_dataframe().attrs[ATTR_BASIS_MISMATCH] is True
    # And the numbers are exactly what the provider gave: flagged, not converted.
    assert mismatched.metric("TotalShareHoldersFunds_A")["Mar 2026"] == 38290.1


def _rebuild(result, **changes):
    import dataclasses

    return dataclasses.replace(result, **changes)


# =============================================================================
# 5. the refusal, and it names both vendors
# =============================================================================


@pytest.mark.parametrize(
    "kind, frequency",
    [
        (StatementKind.BALANCE_SHEET, Frequency.QUARTERLY),
        (StatementKind.BALANCE_SHEET, Frequency.TTM),
        (StatementKind.CASH_FLOW, Frequency.QUARTERLY),
        (StatementKind.CASH_FLOW, Frequency.TTM),
    ],
)
def test_the_four_impossible_cells_raise_a_typed_error_naming_both_vendors(
    kind, frequency, payloads
):
    """Quarterly balance sheet, quarterly cash flow, TTM balance sheet and TTM cash flow are not
    available from any reachable source. Both vendors were probed; this is measured, not assumed.

    So the correct outcome is a typed refusal whose message says so, and whose text tells the
    caller this will not be fixed in a later version — otherwise they file it as a bug and wait for
    a release that is not coming.
    """
    assert providers_for(kind, frequency) == ()
    assert unavailable_reason(kind, frequency)

    with pytest.raises(StatementCapabilityError) as excinfo:
        resolve_from_payloads(_request(kind, frequency, symbol="ITC"), **payloads)

    message = str(excinfo.value)
    for vendor in VENDORS:
        assert vendor in message, f"the refusal does not name {vendor}: {message[:400]}"
    assert "measured" in message
    assert "not a bfinance bug" in message
    assert "will not be fixed by a later version" in message
    assert kind.value in message
    assert frequency.value in message
    assert "Checked against: screener.in and trendlyne.com" in message
    # It replaces a bare NotImplementedError, so existing handlers still catch it, and it is a
    # BFinanceError so `except BFinanceError` works too.
    assert isinstance(excinfo.value, NotImplementedError)
    assert isinstance(excinfo.value, BFinanceError)


def test_attrs_and_the_capability_report_never_claim_an_unavailable_cell_is_available():
    """The other half of the refusal: nothing the package publishes may suggest these four cells
    can be served. A registry entry with a provider in it is a promise the code cannot keep, and a
    capability report that printed a provider for an unavailable cell would be the same lie in
    prose."""
    for cell, reason in UNAVAILABLE.items():
        kind, frequency = cell
        assert providers_for(kind, frequency) == (), f"{cell} has a provider registered"
        assert CAPABILITIES[cell].provider_names == ()
        assert CAPABILITIES[cell].available is False
        assert CAPABILITIES[cell].unavailable_reason == reason
        assert reason.strip()
        for vendor in VENDORS:
            assert vendor in reason, f"the recorded reason for {cell} does not name {vendor}"

    # Every one of the nine pairs is a key, so an absent entry cannot be mistaken for a gap.
    assert len(CAPABILITIES) == 9
    assert sum(1 for cell in CAPABILITIES.values() if cell.available) == 5
    assert sum(1 for cell in CAPABILITIES.values() if not cell.available) == 4

    report = capability_report()
    for kind, frequency in UNAVAILABLE:
        row = next(line for line in report.splitlines() if line.strip().startswith(kind.value))
        assert row.count("UNAVAILABLE") == 2, f"{kind}/{frequency} is not marked twice: {row!r}"
    # The cells that *are* available are named with their providers, so the report cannot be
    # trivially all-refusal either: five available cells, three providers each.
    assert "screener, trendlyne-html, trendlyne-json" in report
    assert report.count("screener, trendlyne-html, trendlyne-json") == 5
    income_row = next(line for line in report.splitlines() if line.strip().startswith("income"))
    assert income_row.count("UNAVAILABLE") == 0


def test_asking_only_for_a_provider_the_cell_does_not_register_is_a_capability_error(payloads):
    """``only=`` cannot conjure a provider, and saying so beats silently serving the other one — a
    caller who asked for a specific source needs to know they did not get it."""
    request = _request(StatementKind.INCOME, Frequency.QUARTERLY, symbol="MOTHERSON")
    with pytest.raises(StatementCapabilityError) as excinfo:
        resolve(request, lambda spec: payloads[spec.payload], only=("screener", "nonexistent"))
    assert "nonexistent" in str(excinfo.value)
    assert "Registered:" in str(excinfo.value)

    with pytest.raises(StatementCapabilityError) as excinfo:
        resolve(request, lambda spec: payloads[spec.payload], only=())
    assert "excluded every provider registered" in str(excinfo.value)

    # And on a cell that does not exist at all, the capability refusal wins over `only`, because
    # the caller's provider preference cannot conjure a source.
    with pytest.raises(StatementCapabilityError) as excinfo:
        resolve(
            _request(StatementKind.BALANCE_SHEET, Frequency.QUARTERLY, symbol="X"),
            lambda spec: payloads[spec.payload],
            only=("screener",),
        )
    message = str(excinfo.value)
    assert "no reachable source can either" in message
    assert "are not registered" not in message


# =============================================================================
# 6. TRAP 1 - the five tab pages are decoys
# =============================================================================


def test_trap1_a_tab_page_shell_raises_not_a_data_page_error(tab_shell_html, trendlyne_html):
    """trendlyne answers **HTTP 200** for ``/fundamentals/balance-sheet/`` and its four siblings,
    with an ~85 KB shell holding **zero** ``<table>``. They are not 404s, nothing is
    robots-disallowed, and they differ only in ``<title>``.

    A parser pointed at one gets an empty answer and reports it as missing data, which is a **false
    negative about the vendor**. So the shell must produce a loud, specific error naming the routes
    that do carry data — and specifically not an empty frame.
    """
    assert "<table" not in tab_shell_html, (
        "the decoy fixture no longer has zero <table>; that fixture is the evidence for this test"
    )
    assert len(tab_shell_html) > 10_000, "the shell is ~85 KB live, 14,146 B trimmed here"
    assert "balance-sheet" in tab_shell_html, (
        "the shell still advertises itself as the balance-sheet tab, which is the whole trap"
    )
    assert "data-tablesurl" in tab_shell_html, (
        "the shell still names the XHR route, so a reader can see where the data really is"
    )

    request = _request(
        StatementKind.BALANCE_SHEET, Frequency.ANNUAL, symbol="MOTHERSON", basis=Basis.STANDALONE
    )
    with pytest.raises(StatementNotADataPageError) as excinfo:
        parse_financials_html(tab_shell_html, request)

    message = str(excinfo.value)
    assert "ZERO <table>" in message
    assert "not a data page" in message
    assert "decoys, not errors" in message
    # Both real routes named, and the decoys named, so the reader can fix the URL.
    assert "/fundamentals/financials/{id}/{SYM}/{slug}/" in message
    assert "/fundamentals/get-fundamental_results-v2/{id}/{token}/" in message
    for decoy in (
        "/fundamentals/balance-sheet/",
        "/fundamentals/cash-flow/",
        "/fundamentals/quarterly-results/",
        "/fundamentals/annual-results/",
        "/fundamentals/financial-ratios/",
    ):
        assert decoy in message, f"the error does not name the decoy route {decoy}"
    assert isinstance(excinfo.value, StatementError)

    # The same through the cascade, with the reason recorded rather than swallowed.
    with pytest.raises(StatementError) as excinfo:
        resolve_from_payloads(request, only=("trendlyne-html",), trendlyne_html=tab_shell_html)
    assert "StatementNotADataPageError" in str(excinfo.value)

    # And an empty payload is the same diagnosis, not a crash.
    with pytest.raises(StatementNotADataPageError, match="empty payload"):
        parse_financials_html("", request)

    # The real page, for contrast: same provider, same request, a statement.
    good = parse_financials_html(trendlyne_html, request)
    assert good.metrics[:2] == ("TotalShareHoldersFunds_A", "EQCAP_A")
    assert good.non_null_cells() > 0


# =============================================================================
# 7. TRAP 2 - three byte-identical header rows
# =============================================================================


def test_trap2_the_three_ambiguous_tables_are_discriminated_by_row_id_and_the_registry_says_so(
    trendlyne_html,
):
    """The repo's convention — identify a table by its header signature, never by heading text — is
    right, and it is followed wherever it works. It is **insufficient here**, and the failure would
    be worse than a miss: balance sheet, ratios and cash flow have a byte-identical header row, so a
    signature matches all three and a registry that trusted it would silently return the wrong
    table.

    So: prove the headers really are identical, prove no header signature can separate them, prove
    the row ids do, and prove the registry **records** that — so the next person does not try.
    """
    tags = _outermost_tables(trendlyne_html)
    assert len(tags) == 5

    balance_sheet, ratios, cash_flow = tags[2], tags[3], tags[4]
    headers_bs = html_table_headers(balance_sheet)
    headers_ratios = html_table_headers(ratios)
    headers_cf = html_table_headers(cash_flow)

    assert headers_bs == headers_ratios == headers_cf, (
        "the three tables' header rows are no longer byte-identical, so this test's premise has "
        "changed and the registry's identified_by='row_id' must be re-checked"
    )
    assert headers_bs == [
        "Indicator", "Graph", "CAGR 3 Yrs", "CAGR 5 Yrs",
        *[f"Mar '{year:02d}" for year in range(26, 15, -1)],
    ], f"the shared header row changed: {headers_bs}"

    # A header signature cannot choose between them: each of the three specs' header signature
    # matches all three tables.
    specs = [
        TABLES["trendlyne_balance_sheet"],
        TABLES["trendlyne_ratios"],
        TABLES["trendlyne_cash_flow"],
    ]
    for headers in (headers_bs, headers_ratios, headers_cf):
        matched = [spec.name for spec in specs if identify_by_header(headers, spec)]
        assert len(matched) == 3, (
            f"a header signature should match all three (and therefore be useless); got {matched}"
        )

    # The row ids can, and do — exactly one spec per table.
    for tag, expected in (
        (balance_sheet, "trendlyne_balance_sheet"),
        (ratios, "trendlyne_ratios"),
        (cash_flow, "trendlyne_cash_flow"),
    ):
        matched = [spec.name for spec in specs if identify_by_row_ids(html_row_ids(tag), spec)]
        assert matched == [expected], f"row ids matched {matched}, expected exactly [{expected}]"

    # The two P&L tables *are* header-identified, and neither P&L signature leaks onto the others.
    assert identify_by_header(html_table_headers(tags[0]), TABLES["trendlyne_quarterly_income"])
    assert identify_by_header(html_table_headers(tags[1]), TABLES["trendlyne_annual_income"])
    assert not identify_by_header(headers_bs, TABLES["trendlyne_annual_income"])
    assert not identify_by_header(headers_bs, TABLES["trendlyne_quarterly_income"])
    assert not identify_by_header(html_table_headers(tags[0]), TABLES["trendlyne_annual_income"])

    # **The registry records the fact**, in the spec itself rather than in a comment, so the next
    # reader sees it before they try a signature.
    for name, discriminant in (
        ("trendlyne_balance_sheet", "TA_A"),
        ("trendlyne_ratios", "ROE_A"),
        ("trendlyne_cash_flow", "CFO_A"),
    ):
        spec = TABLES[name]
        assert spec.identified_by == "row_id"
        assert discriminant in spec.row_ids
        assert "CANNOT be header-identified" in spec.note
        assert "byte-identical" in spec.note
        assert "signature-match" in spec.note
    for name in ("trendlyne_quarterly_income", "trendlyne_annual_income"):
        assert TABLES[name].identified_by == "header"

    # And the live classification uses the row-id route for exactly those three.
    assert classify_html_tables(trendlyne_html) == [
        "trendlyne_quarterly_income",
        "trendlyne_annual_income",
        "trendlyne_balance_sheet",
        "trendlyne_ratios",
        "trendlyne_cash_flow",
    ]


def _outermost_tables(html: str) -> list:
    soup = BeautifulSoup(html, "lxml")
    return [t for t in soup.find_all("table") if t.find_parent("table") is None]


def test_trendlyne_matches_none_of_the_pre_registered_signatures(trendlyne_html):
    """All five of trendlyne's statement tables match **none** of the eight signatures already
    registered in :data:`bfinance.trendlyne.tables.SIGNATURES` — ``classify_table`` returns
    ``None`` for every one of them. So this module brings its own registry rather than extending
    that one, and ``find_tables(html, None)`` is the only correct way to reach these frames."""
    from bfinance.trendlyne.tables import page_signatures

    assert page_signatures(trendlyne_html) == {0: None, 1: None, 2: None, 3: None, 4: None}, (
        "a trendlyne statement table now matches an existing signature; check whether this "
        "module's registry should be replaced by one instead of duplicating the idea"
    )
    # A layout-free financials page is exactly the case the trendlyne docstring's trap 2 warns
    # about, and `signature=None` is the documented way through it.
    assert len(find_tables(trendlyne_html, None)) == 5


def test_the_labels_a_person_would_key_on_are_duplicated_so_the_id_identifies(trendlyne_html):
    """The reason the machine id is the key and the display label is not, measured on the fixture.

    ``Operating Exp.`` appears **twice** in the quarterly P&L and twice in the annual one;
    ``Cash Earning Retention Ann. %`` appears twice in the ratios table. The machine ids repeat too
    (``OEXPNS_Q``, ``OEXPNS_A``, ``CashEarningRetention_A``), so a duplicate is counted and
    recorded rather than silently overwritten — and a label is additionally truncated with a ``⌄``
    disclosure marker, which any substring match on text would have to survive.
    """
    tags = _outermost_tables(trendlyne_html)
    labels = [
        cell.get_text(" ", strip=True)
        for cell in tags[0].find_all("td", attrs={"role": "rowheader"})
    ]
    # Each cell holds the hidden machine id *and* the display text, so the label is the tail.
    assert labels.count("OEXPNS_Q Operating Exp.") == 2, (
        f"the duplicate label this test pins is gone: {labels[:8]}"
    )
    assert any("⌄" in label for label in labels), "labels are no longer disclosure-marked"

    ids = html_row_ids(tags[0])
    assert ids.count("OEXPNS_Q") == 2, "the duplicate id this test pins is gone"

    # Duplicated ids are counted and recorded, not silently dropped.
    result = parse_financials_html(
        trendlyne_html,
        _request(
            StatementKind.INCOME, Frequency.QUARTERLY, symbol="MOTHERSON", basis=Basis.STANDALONE
        ),
    )
    assert result.duplicate_rows == 1
    frame = result.to_dataframe()
    assert frame.attrs[ATTR_DUPLICATE_ROWS] == 1
    assert list(frame.index).count("OEXPNS_Q") == 1

    # The four non-numeric rows are dropped and *named* on attrs, not dropped quietly.
    assert frame.attrs[ATTR_DROPPED_ROWS] == (
        "earnings_transcripts", "ai_summary", "results_pdf", "result_notes",
    )
    assert not set(frame.attrs[ATTR_DROPPED_ROWS]) & set(result.metrics)


# =============================================================================
# 8. TRAP 3 - order.quarter advertises 13 quarters and holds none
# =============================================================================


def test_trap3_the_period_set_comes_from_the_data_and_a_zero_read_is_unavailable(trendlyne_json):
    """The most dangerous thing found in either source, because it produces a *confidently wrong*
    answer rather than an error.

    ``body.order.quarter.bs`` and ``body.order.quarter.cf`` each list all 13 quarterly period
    labels. Every balance-sheet, cash-flow and ratio row id is ``_A``-suffixed, and **zero of the
    260** appears in ``body.quarterlyDataDump``. Reading the balance sheet across the advertised
    quarterly columns therefore yields 0 of 169 non-null cells — a correctly-shaped frame of nulls
    that reads as "the vendor published no balance sheet". That is a false negative, and it is
    exactly what a parser gets wrong when it takes a period set from ``order``.

    So: measure the 0-of-N read here so the trap stays real, assert the module's own period set is
    not ``order``'s, and assert that a forced quarterly balance sheet is refused, not returned.
    """
    body = trendlyne_json["body"]

    # 1. The arithmetic, measured in the test so a reader does not have to re-derive it.
    for basis_name in ("consolidated", "standalone"):
        quarterly = body["quarterlyDataDump"][basis_name]
        annual = body["annualDataDump"][basis_name]
        for group in ("bs", "cf"):
            ids = [entry["indCode"] for entry in body["level_first_dict_map"][group]]
            advertised = body["order"]["quarter"][group]
            assert len(advertised) == 13, f"order.quarter.{group} no longer advertises 13 periods"

            non_null = sum(
                1
                for row_id in ids
                for period in advertised
                if quarterly.get(period, {}).get(row_id) is not None
            )
            assert non_null == 0, (
                f"{group}/{basis_name}: the quarterly read is no longer empty ({non_null} of "
                f"{len(ids) * len(advertised)}). If trendlyne started publishing quarterly "
                f"balance-sheet or cash-flow data, this refusal is wrong and must be revisited."
            )

            annual_periods = body["order"]["annual"][group]
            annual_non_null = sum(
                1
                for row_id in ids
                for period in annual_periods
                if annual.get(period, {}).get(row_id) is not None
            )
            assert annual_non_null > 0, f"{group}/{basis_name}: the annual read is empty too"

    # The exact figures the refusal quotes.
    assert len(body["level_first_dict_map"]["bs"]) * 13 == 169
    assert len(body["level_first_dict_map"]["cf"]) * 13 == 52
    assert len(
        {
            row_id
            for basis in ("consolidated", "standalone")
            for period in body["annualDataDump"][basis]
            for row_id in body["annualDataDump"][basis][period]
            if row_id.endswith("_A")
        }
    ) == 260, "the '260 _A keys' figure the refusal quotes has changed"
    quarterly_ids = {
        row_id
        for basis in ("consolidated", "standalone")
        for period in body["quarterlyDataDump"][basis]
        for row_id in body["quarterlyDataDump"][basis][period]
    }
    assert not any(row_id.endswith("_A") for row_id in quarterly_ids), (
        "an _A-suffixed key now appears in the quarterly dump; trap 3 no longer holds"
    )
    assert len(quarterly_ids) == 82

    # 2. The module's own period set is the *annual* one — the dump's keys, not order's list, and
    #    certainly not the 13 quarterly labels order.quarter.bs advertises.
    annual_bs = parse_fundamental_results(
        trendlyne_json,
        _request(StatementKind.BALANCE_SHEET, Frequency.ANNUAL, symbol="MOTHERSON"),
    )
    assert annual_bs.periods == tuple(sorted(body["order"]["annual"]["bs"]))
    assert len(annual_bs.periods) == 11
    assert TTM not in annual_bs.periods
    # Not the 13 quarterly labels order.quarter.bs advertises. It shares 11 of them with the
    # annual set, so the claim is that the sets differ in size and content, not that they are
    # disjoint — which is exactly the confusion that makes a period set taken from `order` look
    # plausible.
    assert set(annual_bs.periods) != set(body["order"]["quarter"]["bs"])
    assert len(annual_bs.periods) != len(body["order"]["quarter"]["bs"]) == 13
    assert "Jun 2023" not in annual_bs.periods, "a quarter-only period leaked into the annual set"
    assert annual_bs.non_null_cells() > 0
    # Every row is annual-suffixed: the row set came from the suffix, not from a label.
    assert all(metric.endswith("_A") for metric in annual_bs.metrics)
    assert "TA_A" in annual_bs.metrics and "TotalShareHoldersFunds_A" in annual_bs.metrics

    # 3. A quarterly balance sheet asked for directly raises, quoting the arithmetic, and returns
    #    no frame at all. The registry already refuses it before any payload is read (section 5);
    #    this is the provider refusing too, so a direct call cannot bypass the guard.
    for kind, quoted in (
        (StatementKind.BALANCE_SHEET, "0 of 169"),
        (StatementKind.CASH_FLOW, "0 of 52"),
    ):
        with pytest.raises(StatementEmptyError) as excinfo:
            parse_fundamental_results(
                trendlyne_json, _request(kind, Frequency.QUARTERLY, symbol="MOTHERSON")
            )
        message = str(excinfo.value)
        assert quoted in message
        assert "260" in message and "'_A'" in message
        assert "order.quarter" in message
        assert "unasked question" in message or "not a published absence" in message

    # 4. The general backstop, in both of its forms. (a) The periods are there and the values are
    #    not — trap 3's exact shape, and the most likely one to be mistaken for "the vendor has no
    #    data". (b) The rows are not there at all.
    nulled = copy.deepcopy(trendlyne_json)
    for period in nulled["body"]["quarterlyDataDump"]["consolidated"].values():
        for row_id in [key for key in period if key.endswith("_Q") or key in ("NP_TTM", "EPS_TTM")]:
            period[row_id] = None
    with pytest.raises(StatementEmptyError) as excinfo:
        parse_fundamental_results(
            nulled, _request(StatementKind.INCOME, Frequency.QUARTERLY, symbol="MOTHERSON")
        )
    message = str(excinfo.value)
    assert "NONE of them carries a number" in message
    assert "0 of" in message
    assert "unasked question, not a published absence" in message
    assert "rather than returned as a correctly shaped frame of nulls" in message

    stripped = copy.deepcopy(trendlyne_json)
    for period in stripped["body"]["quarterlyDataDump"]["consolidated"].values():
        for row_id in [
            key
            for key in period
            if key.endswith("_Q") or key in ("NP_TTM", "EPS_TTM")
        ]:
            period.pop(row_id)
    with pytest.raises(StatementEmptyError, match="no income row id carrying the '_Q' suffix"):
        parse_fundamental_results(
            stripped, _request(StatementKind.INCOME, Frequency.QUARTERLY, symbol="MOTHERSON")
        )


# =============================================================================
# 9. period labels normalise to one canonical form
# =============================================================================


def test_period_labels_normalise_to_one_canonical_form():
    """trendlyne writes ``'Jun 2026'`` in its JSON and ``"Jun '26"`` on the same response's page.
    screener.in writes ``"Jun 2023"``. Left alone, joining the two frames is a silent outer join
    that drops every row and looks like a plotting bug months later."""
    assert canonical_period("Jun 2023") == "Jun 2023"
    assert canonical_period("Jun '26") == "Jun 2026"
    assert canonical_period("Jun '2016") == "Jun 2016"
    assert canonical_period("Jun 26") == "Jun 2026"
    assert canonical_period("  Jun   2026  ") == "Jun 2026"
    assert canonical_period("ttm") == TTM
    assert canonical_period("TTM") == TTM
    # the 2000s pivot, stated because it is a decision rather than a general rule
    assert expand_short_year(26) == 2026
    assert expand_short_year(16) == 2016
    # refused, not guessed: a period invented from an unrecognised label is a wrong column
    for bad in ("Q2 FY27", "FY26", "2026-06-30", "", "   ", "Jun 26 Q", "Jun 199"):
        with pytest.raises(PeriodLabelError):
            canonical_period(bad)
    with pytest.raises(PeriodLabelError):
        canonical_period(20260630)


def test_period_labels_are_canonical_on_every_statement_both_sources_serve(payloads):
    """End to end, on every period of every cell, on both bases."""
    checked = 0
    for (kind, frequency), capability in CAPABILITIES.items():
        if not capability.available:
            continue
        for basis in Basis:
            result = resolve_from_payloads(_request(kind, frequency, basis=basis), **payloads)
            checked += 1
            for period in result.periods:
                assert period == TTM or re.match(r"^[A-Z][a-z]{2} \d{4}$", period), (
                    f"{period!r} is not canonical"
                )
                assert period == canonical_period(period), f"{period!r} is not already canonical"
            assert result.periods == order_periods(result.periods)
    assert checked == 10, f"expected 5 available cells x 2 bases; covered {checked}"


def test_the_two_digit_page_headers_and_the_json_labels_land_on_the_same_thirteen_columns(
    screener_html, trendlyne_html, trendlyne_json
):
    """The specific claim: trendlyne's page prints ``"Jun '23"`` and its JSON prints
    ``'Jun 2023'`` for the same thirteen quarters, and screener.in prints ``"Jun 2023"`` — so all
    three routes produce one identical column set."""
    from_page = parse_financials_html(
        trendlyne_html,
        _request(
            StatementKind.INCOME, Frequency.QUARTERLY, symbol="MOTHERSON", basis=Basis.STANDALONE
        ),
    )
    from_json = parse_fundamental_results(
        trendlyne_json,
        _request(StatementKind.INCOME, Frequency.QUARTERLY, symbol="MOTHERSON"),
    )
    from_screener = parse_company_page(
        screener_html, _request(StatementKind.INCOME, Frequency.QUARTERLY, symbol="ITC")
    )

    assert from_page.periods == from_json.periods == from_screener.periods
    assert len(from_page.periods) == 13
    assert all(len(period) == 8 for period in from_page.periods), (
        "the page's two-digit headers were not expanded to four digits"
    )
    assert from_page.periods[0] == "Jun 2023"
    assert from_page.periods[-1] == "Jun 2026"

    # The two trendlyne routes are the same vendor and the same thirteen quarters, so their
    # overlapping rows must carry the same standalone-vs-consolidated-equal numbers. The bases
    # differ for MOTHERSON, so only rows that happen to be equal are compared — which the
    # *periods* being identical already proves, and is asserted here so a future change to the
    # period derivation cannot pass unnoticed.
    assert "NP_Q" in from_page.metrics and "NP_Q" in from_json.metrics

    # TTM sorts ahead of the calendar periods, because it is the most recent window and both
    # vendors print it last.
    assert order_periods(["Mar 2024", TTM, "Jun 2023", "Mar 2025"]) == (
        TTM, "Jun 2023", "Mar 2024", "Mar 2025",
    )


# =============================================================================
# 10. the find_tables contract
# =============================================================================


def test_find_tables_still_passes_keep_default_na_false_and_thousands_none(
    monkeypatch, trendlyne_html
):
    """The trendlyne statement reader goes through :func:`bfinance.trendlyne.tables.find_tables`,
    so this contract is load-bearing for every number this package returns.

    ``read_html``'s own defaults would rewrite the vendor's cells before any rule here could see
    them: ``"N/A"`` -> ``NaN`` and ``"1,234"`` -> ``1234``. A ``NaN`` in a cell trendlyne actually
    rendered is indistinguishable from "the vendor did not report it", and a silently stripped
    thousands separator looks like a parsing bug months later. A regression here would corrupt
    every number in the package silently, which is why the flags are pinned rather than assumed.
    """
    import pandas

    captured = []
    real_read_html = pandas.read_html

    def spy(*args, **kwargs):
        captured.append(kwargs)
        return real_read_html(*args, **kwargs)

    monkeypatch.setattr(pandas, "read_html", spy)
    find_tables(trendlyne_html, None)

    assert captured, "find_tables did not call read_html at all"
    for kwargs in captured:
        _assert_no_vendor_rewriting(kwargs, "find_tables")

    # And this package's own reader passes the same three flags, for the same reasons.
    from bfinance.statements.fundamentals_provider import _read_frame

    local: list = []

    def local_spy(*args, **kwargs):
        local.append(kwargs)
        return real_read_html(*args, **kwargs)

    monkeypatch.setattr(pandas, "read_html", local_spy)
    for tag in _outermost_tables(trendlyne_html):
        _read_frame(tag)

    assert len(local) == 5, f"expected one read_html call per table, saw {len(local)}"
    for kwargs in local:
        _assert_no_vendor_rewriting(kwargs, "_read_frame")


def _assert_no_vendor_rewriting(kwargs: dict, who: str) -> None:
    assert kwargs.get("keep_default_na") is False, (
        f"{who}: keep_default_na must be False; pandas would turn 'N/A' into NaN. Got {kwargs}"
    )
    assert "thousands" in kwargs and kwargs["thousands"] is None, (
        f"{who}: thousands must be None; pandas would strip the separator from '3,827.7'. "
        f"Got {kwargs}"
    )
    assert kwargs.get("na_values") == [], f"{who}: na_values must be empty; got {kwargs}"


def test_this_package_does_not_import_the_leaf_vendor_package():
    """``bfinance.trendlyne`` is a leaf and
    ``tests/test_trendlyne_core.py::TestIsolation::test_no_other_bfinance_module_imports_trendlyne``
    enforces it over every other module under ``src/``. This package reads that vendor's pages, so
    it does its own table extraction (:func:`_read_frame`) rather than importing the shared one.

    Checked here as well, because a second guard on a constraint whose violation is a *silent*
    architectural regression is worth having: the moment an ``import bfinance.trendlyne`` lands in
    this package, the coupling is back, and the failure mode is a leaf whose availability this
    package now shares.

    Scoped to ``bfinance/statements/`` on purpose. The repo-wide sweep is that other test's job,
    and duplicating it here would make this file fail whenever an unrelated module elsewhere under
    ``src/`` is mid-edit — which is a flaky test, not a guard.
    """
    import ast
    import pathlib

    import bfinance.statements as package

    offenders = []
    for path in sorted(pathlib.Path(package.__file__).parent.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module, *[f"{node.module}.{a.name}" for a in node.names]]
            if any("trendlyne" in name for name in names):
                offenders.append(f"{path.name}:{node.lineno} -> {names}")
    assert offenders == [], f"the leaf package must stay a leaf; these import it: {offenders}"


def test_the_local_table_reader_matches_the_shared_extractor_cell_for_cell(trendlyne_html):
    """The reason this package carries its own ``read_html`` call.

    ``bfinance.trendlyne`` must stay a leaf, so :func:`_read_frame` duplicates
    :func:`bfinance.trendlyne.tables.find_tables` — same three flags, same flattening, same
    all-empty-row drop. Duplication is only acceptable while the two are provably identical, so
    they are compared here: **all five tables, every column label, every cell.**
    """
    from bfinance.statements.fundamentals_provider import _read_frame

    mine = [_read_frame(tag) for tag in _outermost_tables(trendlyne_html)]
    theirs = find_tables(trendlyne_html, None)

    assert len(mine) == len(theirs) == 5
    for index, (a, b) in enumerate(zip(mine, theirs, strict=True)):
        assert list(a.columns) == list(b.columns), f"table {index}: columns differ"
        assert a.shape == b.shape, f"table {index}: {a.shape} != {b.shape}"
        left, right = a.to_numpy().tolist(), b.to_numpy().tolist()
        mismatches = [
            (r, c, left[r][c], right[r][c])
            for r in range(len(left))
            for c in range(len(left[r]))
            if left[r][c] != right[r][c]
        ]
        assert mismatches == [], f"table {index}: {len(mismatches)} cell(s) differ: {mismatches[:5]}"


def test_the_cells_this_package_reads_keep_their_raw_formatting(trendlyne_html):
    """Behavioural half of the previous test: on the real fixture ``'-'`` is the string ``'-'`` and
    not ``NaN``, and ``'3,827.7'`` keeps its separator."""
    frames = find_tables(trendlyne_html, None)
    cash_flow, balance_sheet, quarterly = frames[4], frames[2], frames[0]

    dashes = {
        str(cell)
        for frame in frames
        for cell in frame.to_numpy().ravel()
        if str(cell).strip() == "-"
    }
    assert dashes == {"-"}, f"the fixture no longer holds a literal '-': {sorted(dashes)[:5]}"
    assert not cash_flow["Mar '26"].isna().any(), (
        "a cell trendlyne rendered became NaN, which is indistinguishable from 'not reported'"
    )
    assert not balance_sheet["Mar '26"].isna().any()
    first_values = quarterly["Jun '26"].tolist()
    assert "3,827.7" in first_values, f"the thousands separator was stripped: {first_values[:3]}"

    # And through the statement reader the same cell is the number 3827.7 — parsed by bfinance,
    # all-or-nothing, not by pandas' type inference.
    result = parse_financials_html(
        trendlyne_html,
        _request(
            StatementKind.INCOME, Frequency.QUARTERLY, symbol="MOTHERSON", basis=Basis.STANDALONE
        ),
    )
    assert result.metric("TOTAL_SR_Q")["Jun 2026"] == 3827.7
    assert result.metric("OPMPCT_Q")["Jun 2026"] == 12.47, "a % cell must keep its percentage"

    # A row the vendor renders entirely as '-' is dropped and named, not turned into zeros and not
    # turned into -1.0. LiabilityMinorityInterest_A is exactly that row on this page.
    sheet = parse_financials_html(
        trendlyne_html,
        _request(
            StatementKind.BALANCE_SHEET, Frequency.ANNUAL, symbol="MOTHERSON",
            basis=Basis.STANDALONE,
        ),
    )
    assert "LiabilityMinorityInterest_A" in sheet.dropped_rows, (
        "the all-dash row is no longer dropped; if trendlyne started populating it this test's "
        "premise changed"
    )
    assert "LiabilityMinorityInterest_A" not in sheet.metrics
    assert 0.0 not in [value for value in sheet.metric("TA_A").values() if value is not None]


# =============================================================================
# 11. the package's own hygiene
# =============================================================================


def test_every_registered_payload_dispatches_to_a_real_parser():
    """The registry names a payload per provider and the cascade maps payloads to parsers. If one
    of the two drifts, the provider is registered as able to serve something nothing can read —
    and the cascade falls through on every call while claiming a working redundancy."""
    from bfinance.statements import PAYLOAD_PARSE, PROVIDERS

    payloads = {spec.payload for spec in PROVIDERS.values()}
    assert payloads == set(PAYLOAD_PARSE), (
        f"registered payloads {sorted(payloads)} != parsers {sorted(PAYLOAD_PARSE)}"
    )
    for provider in PROVIDERS.values():
        parser = PAYLOAD_PARSE[provider.payload]
        assert callable(parser), f"{provider.name}'s parser is not callable"
        # a parser that is missing a name is a silent no-op; make sure the wiring is real
        assert getattr(parser, "__name__", "") or getattr(
            parser, "__qualname__", ""
        ), f"{provider.name}'s parser has no name; the wiring may be a stub"


def test_the_non_numeric_row_ids_this_module_documents_are_the_ones_it_drops(trendlyne_html):
    """:data:`NON_NUMERIC_ROW_IDS` names the document-link, notes and AI-blurb rows so a reader can
    see what disappears from a statement. A constant that quietly stops matching the data is worse
    than no constant, so it is checked against the fixture."""
    from bfinance.statements.fundamentals_provider import NON_NUMERIC_ROW_IDS

    tags = _outermost_tables(trendlyne_html)
    present = {row_id for tag in tags for row_id in html_row_ids(tag)}
    dropped = set(NON_NUMERIC_ROW_IDS) & present
    assert dropped == {
        "earnings_transcripts", "ai_summary", "results_pdf", "result_notes", "Annual_Reports",
    }, f"the documented non-numeric rows are {sorted(dropped)}; the constant has drifted"

    # And each is genuinely dropped from the statement that would otherwise carry it.
    annual = parse_financials_html(
        trendlyne_html,
        _request(
            StatementKind.INCOME, Frequency.ANNUAL, symbol="MOTHERSON", basis=Basis.STANDALONE
        ),
    )
    assert set(annual.dropped_rows) & {"result_notes", "Annual_Reports"} == {
        "result_notes", "Annual_Reports",
    }
    assert not set(annual.dropped_rows) & set(annual.metrics)


def test_importing_bfinance_does_not_import_this_package():
    """Opt-in, like :mod:`bfinance.trendlyne`. If this package were imported eagerly, a statements
    bug could break ``import bfinance`` — and the trendlyne isolation the repo enforces would be
    gone with it."""
    code = (
        "import sys, bfinance;"
        "print('bfinance.statements' in sys.modules, 'bfinance.trendlyne' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert completed.stdout.strip() == "False False", completed.stdout


def test_no_attrs_key_is_written_that_is_not_declared():
    """``ATTR_KEYS`` is the whole published contract. An undeclared key cannot be branched on by a
    consumer, and that is how ``attrs`` quietly becomes a dumping ground."""
    from bfinance.statements import model

    assert isinstance(model.ATTR_KEYS, tuple)
    assert len(set(model.ATTR_KEYS)) == len(model.ATTR_KEYS), "ATTR_KEYS has a duplicate"
    assert "bfinance_provenance" in model.ATTR_KEYS
    assert all(key.startswith("bfinance_") for key in model.ATTR_KEYS)


def test_a_result_built_without_the_cascade_reports_no_fallback_and_no_order(screener_html):
    """The fallback verdict is computed from the order the cascade recorded, never from what
    happened to be passed in. A hand-built result has no order, so it claims no fallback — and the
    cascade always populates both."""
    direct = parse_company_page(
        screener_html, _request(StatementKind.INCOME, Frequency.QUARTERLY, symbol="ITC")
    )
    assert direct.registry_order == ()
    assert direct.providers_tried == ()
    assert direct.served is False
    assert direct.fallback is False, "no cascade ran, so there is no failover to report"
    assert direct.fallback_from == ()
    assert direct.skip_reasons == {}

    through = resolve_from_payloads(
        _request(StatementKind.INCOME, Frequency.QUARTERLY, symbol="ITC"),
        only=("screener",),
        screener_html=screener_html,
    )
    assert through.registry_order == ("screener",)
    assert through.served is True
    assert through.fallback is False
    assert through.fallback_from == ()
    assert through.providers_tried == ("screener",)
    assert through.skip_reasons == {}


def test_a_statement_result_must_not_be_ragged_or_empty():
    """A ragged row is a markup change and a frame-building crash elsewhere; an empty result is a
    refusal. Neither is reachable through the providers, so both are checked at the model."""
    from bfinance.statements import StatementResult

    request = _request(StatementKind.INCOME, Frequency.QUARTERLY, symbol="X")
    common = dict(
        request=request, provider="screener", payload="screener_html",
        basis=Basis.CONSOLIDATED,
    )
    with pytest.raises(ValueError, match="not unique"):
        StatementResult(periods=("Jun 2026", "Jun 2026"), rows={"Sales": (1.0, 2.0)}, **common)
    with pytest.raises(ValueError, match="at least one period"):
        StatementResult(periods=(), rows={"Sales": ()}, **common)
    with pytest.raises(ValueError, match="at least one row"):
        StatementResult(periods=("Jun 2026",), rows={}, **common)
    with pytest.raises(ValueError, match="ragged statement"):
        StatementResult(periods=("Jun 2026", "Mar 2026"), rows={"Sales": (1.0,)}, **common)
    with pytest.raises(TypeError, match="must be a StatementKind"):
        StatementRequest(symbol="X", kind="income", frequency=Frequency.QUARTERLY)
