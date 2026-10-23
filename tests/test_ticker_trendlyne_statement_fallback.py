"""The opt-in second-vendor statement fallback on ``Ticker``.

The contract this file pins is narrow on purpose, because the failure modes of a fallback are
worse than the failure it fixes:

* **off by default.** ``Ticker(symbol)`` must open no second-vendor socket. Proved by asserting
  the transport is never called, not by reading a docstring.
* **never blended.** When the primary source has a statement, it is returned whole and the
  fallback transport is never asked. A frame half from each vendor is indistinguishable from
  either, and the two vendors disagree in a way that matters (basis).
* **degrade, don't crash.** A failing fallback returns an empty frame with the reason on
  ``data_warnings`` unless ``raise_errors=True``, matching ``_read_statement``'s contract.
* **a served statement is not a degradation.** ``data_warnings`` says "what is unavailable"; a
  complete frame is not that. Provenance rides on the frame's ``attrs``, the same convention
  ``history()`` uses for a price failover (``bfinance_price_source*``). This file pins both
  halves: a success records nothing, and a failure still records.
* **the frame never narrates a vendor failure that did not happen.** Screener.in is not consulted
  on this path at all -- the fallback holds one payload, the cascade is restricted to the
  providers that read it, and its fetch refuses anything else. So no screener.in entry may appear
  in ``skip_reasons``, ``providers_tried`` or ``fallback_from``, and ``fallback`` is ``False``:
  within this call there was no failover to record. The previous fetch handed this fallback's JSON
  to *every* provider, so screener.in's HTML parser was fed a dict and its resulting
  ``KeyError`` was published on the frame as a screener.in outage.
* **a caller bug is not a degraded vendor.** ``except (ValueError, KeyError)`` turned a
  misspelled ``StatementKind`` into a "vendor degraded" warning. Only the package's typed errors
  degrade; builtins propagate.
* **one payload per ``Ticker``.** The fallback vendor's JSON carries every statement and both
  frequencies, so four accessors must not cost four fetches. Counted, not asserted by comment.
* **an unsupported flag raises rather than being ignored.** All three accessors take yfinance's
  ``pretty`` keyword and neither vendor path can honour it, so ``pretty=True`` is refused with
  the reason. See the ``pretty`` section at the foot of this file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from bfinance.statements.fundamentals_fetch import FundamentalsFetchError
from bfinance.ticker import Ticker

FIXTURE = Path(__file__).parent / "fixtures" / "trendlyne" / "fundamental_results_ntpc.json"


def _payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class _FakeTransport:
    """Records every call and returns the recorded payload."""

    def __init__(self, payload=None, error=None):
        self.calls = []
        self.payload = payload if payload is not None else _payload()
        self.error = error

    def __call__(self, symbol, **kwargs):
        self.calls.append(symbol)
        if self.error is not None:
            raise self.error
        return self.payload


@pytest.fixture
def empty_ticker(monkeypatch):
    """A Ticker whose primary source is degraded and carries no statement at all.

    The primary source is stubbed rather than fetched: this file is offline, and a live
    screener.in response would make every assertion below depend on a network condition.
    """
    profile = empty_profile()
    monkeypatch.setattr(Ticker, "_ensure_profile", lambda self: profile)
    return Ticker("NTPC", raise_errors=True)


def empty_profile():
    """A ``CompanyProfile`` carrying no statement of any kind."""
    from bfinance.models.company import CompanyProfile, FinancialStatement

    return CompanyProfile(
        symbol="NTPC",
        company_id=0,
        name="NTPC Ltd.",
        profit_loss=FinancialStatement(headers=[], rows={}),
        balance_sheet=FinancialStatement(headers=[], rows={}),
        cash_flow=FinancialStatement(headers=[], rows={}),
        quarters=FinancialStatement(headers=[], rows={}),
    )


# --------------------------------------------------------------------------- #
# the default: no second-vendor request at all
# --------------------------------------------------------------------------- #


def test_the_default_makes_no_second_vendor_request(empty_ticker, monkeypatch):
    transport = _FakeTransport()
    monkeypatch.setattr(
        "bfinance.statements.fundamentals_fetch.fetch_fundamentals_json_sync", transport
    )
    frame = empty_ticker.get_income_stmt()
    assert transport.calls == [], "the fallback ran without being asked for"
    assert frame.empty


def test_the_flag_is_off_by_default_on_the_constructor():
    assert Ticker("NTPC").statement_fallback is False


# --------------------------------------------------------------------------- #
# the fallback, when there is nothing to fall back from
# --------------------------------------------------------------------------- #


def test_the_fallback_serves_the_statements_when_the_primary_is_empty(empty_ticker, monkeypatch):
    transport = _FakeTransport()
    monkeypatch.setattr(
        "bfinance.statements.fundamentals_fetch.fetch_fundamentals_json_sync", transport
    )
    ticker = Ticker("NTPC", raise_errors=True, statement_fallback=True)
    ticker._ensure_profile = lambda: empty_ticker._ensure_profile()

    frame = ticker.get_income_stmt()
    assert transport.calls == ["NTPC"]
    assert not frame.empty
    assert frame.attrs["bfinance_statements_provider"] == "trendlyne-json"
    assert frame.attrs["bfinance_statements_basis"] == "consolidated"
    assert frame.attrs["bfinance_statements_basis_mismatch"] is False
    # The yfinance shape, not the vendor's: newest-first DatetimeIndex columns.
    assert frame.columns[0] == pd.Timestamp("2026-03-31")
    assert "Total Rev. Ann." in frame.index

    balance = ticker.get_balance_sheet()
    assert not balance.empty
    assert "Total Assets Ann." in balance.index

    cash = ticker.get_cash_flow()
    assert not cash.empty
    assert cash.attrs["bfinance_statements_provider"] == "trendlyne-json"


def test_the_quarterly_income_statement_falls_back_rather_than_raising(
    empty_ticker, monkeypatch
):
    """The primary source raises ``NotImplementedError`` for a quarterly table it does not carry.
    The fallback vendor publishes 13 quarters, so with the flag on the raise must not pre-empt
    it."""
    transport = _FakeTransport()
    monkeypatch.setattr(
        "bfinance.statements.fundamentals_fetch.fetch_fundamentals_json_sync", transport
    )
    ticker = Ticker("NTPC", raise_errors=True, statement_fallback=True)
    ticker._ensure_profile = lambda: empty_ticker._ensure_profile()

    frame = ticker.get_income_stmt(freq="quarterly")
    assert not frame.empty
    assert len(frame.columns) == 13
    assert frame.columns[0] == pd.Timestamp("2026-06-30")


def test_quarterly_still_raises_without_the_flag(empty_ticker, monkeypatch):
    """The opt-out path keeps its documented behaviour, including the raise."""
    monkeypatch.setattr(
        "bfinance.statements.fundamentals_fetch.fetch_fundamentals_json_sync", _FakeTransport()
    )
    ticker = Ticker("NTPC", raise_errors=True)
    ticker._ensure_profile = lambda: empty_ticker._ensure_profile()
    with pytest.raises(NotImplementedError):
        ticker.get_income_stmt(freq="quarterly")


def test_a_failing_quarterly_fallback_degrades_and_then_raises(empty_ticker, monkeypatch):
    """Both halves of the quarterly-empty path, in that order, and the flag changes neither.

    The fallback is attempted before the raise because it publishes 13 quarters where the primary
    source may carry none -- but when it cannot answer, the reason belongs on ``data_warnings``
    *and* the caller's quarterly request still ends in the documented ``NotImplementedError``
    rather than in a silent empty frame. There is one raise on this path, and the accessor has
    exactly one exit that can produce it.
    """
    transport = _FakeTransport(error=FundamentalsFetchError("the vendor did not answer"))
    monkeypatch.setattr(
        "bfinance.statements.fundamentals_fetch.fetch_fundamentals_json_sync", transport
    )
    ticker = Ticker("NTPC", raise_errors=False, statement_fallback=True)
    ticker._ensure_profile = lambda: empty_ticker._ensure_profile()

    with pytest.raises(NotImplementedError, match="quarterly statements not supported"):
        ticker.get_income_stmt(freq="quarterly")

    assert transport.calls == ["NTPC"], "the quarterly fallback was never attempted"
    assert any("fallback vendor" in w for w in ticker.data_warnings)
    assert any("FundamentalsFetchError" in w for w in ticker.data_warnings)


def test_the_properties_delegate_to_the_fallback(empty_ticker, monkeypatch):
    """``financials`` / ``balance_sheet`` / ``cashflow`` / ``quarterly_income_stmt`` all
    reach it."""
    monkeypatch.setattr(
        "bfinance.statements.fundamentals_fetch.fetch_fundamentals_json_sync", _FakeTransport()
    )
    ticker = Ticker("NTPC", raise_errors=True, statement_fallback=True)
    ticker._ensure_profile = lambda: empty_ticker._ensure_profile()
    assert not ticker.financials.empty
    assert not ticker.income_stmt.empty
    assert not ticker.quarterly_income_stmt.empty
    assert not ticker.balance_sheet.empty
    assert not ticker.cashflow.empty


# --------------------------------------------------------------------------- #
# no blending
# --------------------------------------------------------------------------- #


def test_the_fallback_is_not_used_when_the_primary_has_a_statement(monkeypatch):
    """The primary source answers, so the second vendor is never asked. This is the assertion
    that keeps a fallback from becoming a silent second source."""
    from bfinance.models.company import CompanyProfile, FinancialStatement

    populated = FinancialStatement(
        headers=["Mar 2025"],
        rows={"Sales+": [100.0]},
    )
    profile = CompanyProfile(
        symbol="NTPC", company_id=1, name="NTPC Ltd.", profit_loss=populated
    )
    transport = _FakeTransport()
    monkeypatch.setattr(
        "bfinance.statements.fundamentals_fetch.fetch_fundamentals_json_sync", transport
    )
    ticker = Ticker("NTPC", raise_errors=True, statement_fallback=True)
    monkeypatch.setattr(Ticker, "_ensure_profile", lambda self: profile)

    frame = ticker.get_income_stmt()
    assert transport.calls == [], "the fallback ran even though the primary source answered"
    assert not frame.empty
    # One vendor's labels, not a mixture of both vocabularies.
    assert "Sales+" in frame.index or "Total Revenue" in frame.index
    assert "Total Rev. Ann." not in frame.index


# --------------------------------------------------------------------------- #
# degradation
# --------------------------------------------------------------------------- #


def test_a_failing_fallback_degrades_to_an_empty_frame_with_a_reason(empty_ticker, monkeypatch):
    transport = _FakeTransport(error=FundamentalsFetchError("the vendor did not answer"))
    monkeypatch.setattr(
        "bfinance.statements.fundamentals_fetch.fetch_fundamentals_json_sync", transport
    )
    ticker = Ticker("NTPC", raise_errors=False, statement_fallback=True)
    ticker._ensure_profile = lambda: empty_ticker._ensure_profile()

    frame = ticker.get_income_stmt()
    assert frame.empty
    assert any("fallback vendor" in w for w in ticker.data_warnings)
    assert any("FundamentalsFetchError" in w for w in ticker.data_warnings)


def test_a_failing_fallback_raises_when_raise_errors(empty_ticker, monkeypatch):
    transport = _FakeTransport(error=FundamentalsFetchError("the vendor did not answer"))
    monkeypatch.setattr(
        "bfinance.statements.fundamentals_fetch.fetch_fundamentals_json_sync", transport
    )
    ticker = Ticker("NTPC", raise_errors=True, statement_fallback=True)
    ticker._ensure_profile = lambda: empty_ticker._ensure_profile()
    with pytest.raises(FundamentalsFetchError):
        ticker.get_income_stmt()


# --------------------------------------------------------------------------- #
# a served statement is not a degradation
# --------------------------------------------------------------------------- #


def test_a_served_statement_records_nothing_on_data_warnings(empty_ticker, monkeypatch):
    """``_degrade`` says "what is **unavailable**". A complete frame is not that.

    Reporting a healthy failover here told every caller to distrust a frame that was in fact
    complete, and collapsed the one distinction ``data_warnings`` exists to keep: "genuinely not
    published" versus "failed to fetch".
    """
    monkeypatch.setattr(
        "bfinance.statements.fundamentals_fetch.fetch_fundamentals_json_sync", _FakeTransport()
    )
    ticker = Ticker("NTPC", raise_errors=False, statement_fallback=True)
    ticker._ensure_profile = lambda: empty_ticker._ensure_profile()

    for frame in (ticker.financials, ticker.balance_sheet, ticker.cashflow):
        assert not frame.empty
    assert ticker.data_warnings == (), ticker.data_warnings
    assert not [w for w in ticker.data_warnings if "unavailable" in w]


def test_a_served_statement_still_names_its_provenance_on_attrs(empty_ticker, monkeypatch):
    """Dropping the success-path ``_degrade`` must not drop the answer to "which vendor, on what
    basis" -- the same reason this file's ``history()`` reports a price failover on attrs."""
    monkeypatch.setattr(
        "bfinance.statements.fundamentals_fetch.fetch_fundamentals_json_sync", _FakeTransport()
    )
    ticker = Ticker("NTPC", raise_errors=False, statement_fallback=True)
    ticker._ensure_profile = lambda: empty_ticker._ensure_profile()

    frame = ticker.financials
    assert frame.attrs["bfinance_statements_provider"] == "trendlyne-json"
    assert frame.attrs["bfinance_statements_basis"] == "consolidated"
    assert frame.attrs["bfinance_statements_basis_requested"] == "consolidated"
    assert frame.attrs["bfinance_statements_basis_mismatch"] is False
    # "Which vendor answered" is answerable from the frame alone rather than inferred from the
    # absence of a warning. What it did *not* answer is pinned separately, below: the fallback
    # claims no in-package failover, because inside this call there was none.
    assert frame.attrs["bfinance_statements_providers_tried"] == ("trendlyne-json",)


# --------------------------------------------------------------------------- #
# the provenance record must not narrate a vendor failure that never happened
# --------------------------------------------------------------------------- #
#
# The fetch callable handed this fallback's JSON payload to *every* provider the cascade offered
# it to, so screener.in's HTML parser was handed a dict and raised
# ``KeyError: slice(None, 500, None)``. The cascade recorded that faithfully and the frame
# published it as
#
#   skip_reasons    = {'screener': 'KeyError: slice(None, 500, None)', ...}
#   providers_tried = ('screener', 'trendlyne-json')
#   fallback_from   = ('screener', 'trendlyne-html')
#   fallback        = True
#
# Every word of which was false: screener.in was never consulted, so it did not fail. A frame that
# narrates an outage it caused itself is the same defect `_read_statement` records as the
# confident 80%-wrong EnterpriseValue — a fabricated fact, in the one shape a reader cannot check.

_FABRICATED_RECORD_ATTRS = (
    "bfinance_statements_skip_reasons",
    "bfinance_statements_providers_tried",
    "bfinance_statements_fallback_from",
)


def test_the_fallback_never_claims_a_screener_attempt(empty_ticker, monkeypatch):
    """The BLOCKING fix, asserted on the frame a caller actually receives.

    Screener.in is not consulted on this path at all: the fallback holds one payload, so the
    cascade is restricted to the providers that read it and its fetch refuses anything else.
    Nothing about screener.in may appear on the frame -- not as a skip reason, not as a provider
    that was tried, not as one that was fallen back from.
    """
    monkeypatch.setattr(
        "bfinance.statements.fundamentals_fetch.fetch_fundamentals_json_sync", _FakeTransport()
    )
    ticker = Ticker("NTPC", raise_errors=True, statement_fallback=True)
    ticker._ensure_profile = lambda: empty_ticker._ensure_profile()

    frames = (
        ticker.financials,
        ticker.balance_sheet,
        ticker.cashflow,
        ticker.quarterly_income_stmt,
    )
    for frame in frames:
        assert not frame.empty
        for attr in _FABRICATED_RECORD_ATTRS:
            recorded = frame.attrs[attr]
            names = recorded if isinstance(recorded, (tuple, list)) else tuple(recorded)
            assert not any("screener" in name for name in names), (
                f"attrs[{attr!r}] narrates a screener.in attempt that never happened: {recorded}"
            )
        # The positive half, so the assertions above cannot pass because the record was emptied
        # rather than made true: the provider that actually answered is still named.
        assert frame.attrs["bfinance_statements_provider"] == "trendlyne-json"
        assert frame.attrs["bfinance_statements_providers_tried"] == ("trendlyne-json",)
        assert frame.attrs["bfinance_statements_skip_reasons"] == {}


def test_no_fallback_failure_is_recorded_as_a_screener_skip(empty_ticker, monkeypatch):
    """Even when the fallback cannot answer, the reason is the *fallback's*, not screener.in's.

    The cascade names every provider it **asked** on its own ``name: reason`` line, so that is
    what is checked. The capability matrix it also prints lists screener.in as a provider for
    these cells, which is a true statement about the registry and not a claim that it failed --
    so a blanket "screener must not appear in the message" would be wrong.
    """
    monkeypatch.setattr(
        "bfinance.statements.fundamentals_fetch.fetch_fundamentals_json_sync", _FakeTransport()
    )
    ticker = Ticker("NTPC", raise_errors=False, statement_fallback=True)
    ticker._fundamentals_payload = lambda fetch: {"head": {"status": "0"}, "body": {}}
    ticker._ensure_profile = lambda: empty_ticker._ensure_profile()

    assert ticker.get_balance_sheet().empty
    warnings = "\n".join(ticker.data_warnings)
    assert "fallback vendor" in warnings
    blamed = [
        line.strip()
        for line in warnings.splitlines()
        if line.strip().startswith(("screener:", "screener_html:", "trendlyne-html:"))
    ]
    assert blamed == [], f"a fallback failure was blamed on a vendor that was never asked: {blamed}"


def test_the_fetch_refuses_a_provider_whose_payload_it_does_not_carry():
    """The rule itself, at the callable, so it cannot be quietly reverted to a blanket return.

    This is the fix the provenance defect turned on: a fetch that answers for a provider whose
    bytes it never received will hand them over again the moment the cascade asks, and the frame
    will narrate whatever the parser does with them.
    """
    from bfinance.statements import JSON_PAYLOAD, PROVIDERS, SCREENER_PAYLOAD, StatementSourceError
    from bfinance.ticker import _fallback_fetch

    fetch = _fallback_fetch(JSON_PAYLOAD, {"head": {"status": "0"}})

    carrier = next(
        spec for spec in PROVIDERS.values() if spec.payload == JSON_PAYLOAD
    )
    assert fetch(carrier) == {"head": {"status": "0"}}

    foreign = next(
        spec for spec in PROVIDERS.values() if spec.payload == SCREENER_PAYLOAD
    )
    with pytest.raises(StatementSourceError):
        fetch(foreign)


def test_a_caller_bug_is_not_reported_as_a_degraded_vendor(empty_ticker, monkeypatch):
    """``kind`` and ``frequency`` come from this package's own accessors; a misspelling is a bug
    here, not bad upstream data.

    ``except (ValueError, KeyError)`` turned ``StatementKind("inccome")`` into "the vendor is
    degraded" on ``data_warnings`` — the quiet bug-into-empty-frame antipattern
    ``_read_statement`` exists to refuse, and it hid the provenance defect behind a plausible
    degradation message.
    """
    monkeypatch.setattr(
        "bfinance.statements.fundamentals_fetch.fetch_fundamentals_json_sync", _FakeTransport()
    )
    ticker = Ticker("NTPC", raise_errors=False, statement_fallback=True)
    ticker._ensure_profile = lambda: empty_ticker._ensure_profile()

    with pytest.raises(ValueError):
        ticker._fallback_statement_frame("inccome", "annual")
    assert ticker.data_warnings == (), (
        "a caller bug was recorded as a vendor degradation instead of propagating"
    )


def test_a_fallback_that_serves_nothing_still_records(empty_ticker, monkeypatch):
    """The other half of the pair: when no statement is served there IS something to report."""
    monkeypatch.setattr(
        "bfinance.statements.fundamentals_fetch.fetch_fundamentals_json_sync", _FakeTransport()
    )
    ticker = Ticker("NTPC", raise_errors=False, statement_fallback=True)
    # A payload no provider can read a statement out of: served nothing, but did not fail.
    ticker._fundamentals_payload = lambda fetch: {"head": {"status": "0"}, "body": {}}
    ticker._ensure_profile = lambda: empty_ticker._ensure_profile()

    frame = ticker.get_balance_sheet()
    assert frame.empty
    assert any("fallback vendor" in w for w in ticker.data_warnings)


# --------------------------------------------------------------------------- #
# request volume: one payload per Ticker, fetched lazily
# --------------------------------------------------------------------------- #


def test_four_statement_accessors_cost_one_payload_fetch(empty_ticker, monkeypatch):
    """The JSON route carries every statement and both frequencies, so four accessors on one
    ``Ticker`` must not cost four fetches. Counted on the transport, not asserted by comment."""
    transport = _FakeTransport()
    monkeypatch.setattr(
        "bfinance.statements.fundamentals_fetch.fetch_fundamentals_json_sync", transport
    )
    ticker = Ticker("NTPC", raise_errors=True, statement_fallback=True)
    ticker._ensure_profile = lambda: empty_ticker._ensure_profile()

    frames = (
        ticker.financials,
        ticker.balance_sheet,
        ticker.cashflow,
        ticker.quarterly_income_stmt,
    )
    assert all(not frame.empty for frame in frames)
    assert transport.calls == ["NTPC"], f"the payload was fetched {len(transport.calls)} times"


def test_the_payload_cache_is_lazy_and_scoped_to_one_ticker(empty_ticker, monkeypatch):
    """Nothing at construction, and a second ``Ticker`` re-fetches.

    The fetcher's own documentation records that the per-symbol token rotates and is never
    stored, so this cache is scoped to a single object's lifetime on purpose. A process-wide
    payload cache would outlive that boundary; the scope is pinned here rather than wished for.
    """
    transport = _FakeTransport()
    monkeypatch.setattr(
        "bfinance.statements.fundamentals_fetch.fetch_fundamentals_json_sync", transport
    )

    first = Ticker("NTPC", raise_errors=True, statement_fallback=True)
    assert transport.calls == [], "construction fetched the payload"
    first._ensure_profile = lambda: empty_ticker._ensure_profile()
    assert not first.financials.empty
    assert transport.calls == ["NTPC"]

    second = Ticker("NTPC", raise_errors=True, statement_fallback=True)
    second._ensure_profile = lambda: empty_ticker._ensure_profile()
    assert not second.financials.empty
    assert transport.calls == ["NTPC", "NTPC"], "the cache leaked past one Ticker's lifetime"


def test_a_failed_fetch_is_not_cached(empty_ticker, monkeypatch):
    """A transient failure must not be replayed for the life of the object.

    Mirrors ``_ensure_profile``'s own rule -- the fallback is returned, never stored -- because a
    single blocked request otherwise becomes a permanently empty statement.
    """
    calls = []

    def flaky(symbol, **kwargs):
        calls.append(symbol)
        if len(calls) == 1:
            raise FundamentalsFetchError("the vendor did not answer")
        return _payload()

    monkeypatch.setattr(
        "bfinance.statements.fundamentals_fetch.fetch_fundamentals_json_sync", flaky
    )
    ticker = Ticker("NTPC", raise_errors=False, statement_fallback=True)
    ticker._ensure_profile = lambda: empty_ticker._ensure_profile()

    assert ticker.get_balance_sheet().empty
    assert not ticker.get_balance_sheet().empty
    assert calls == ["NTPC", "NTPC"]


# --------------------------------------------------------------------------- #
# the Tickers collection can set the flag
# --------------------------------------------------------------------------- #


def test_tickers_forwards_the_flag_to_every_ticker():
    """``Tickers([...]).tickers["NTPC"].financials`` could never fall back without it.

    The keyword reaches the real constructor as a keyword, which is what the constructor's
    signature requires: only ``ticker`` is positional there and ``session`` is the second
    positional. Asserted on the constructed objects, not on the call.
    """
    from bfinance.tickers import Tickers

    collection = Tickers(["NTPC", "RELIANCE"], statement_fallback=True)
    assert {sym: t.statement_fallback for sym, t in collection.tickers.items()} == {
        "NTPC": True,
        "RELIANCE": True,
    }


def test_tickers_default_is_unchanged():
    """The flag is off by default, so a collection opens no second-vendor socket unless asked."""
    from bfinance.tickers import Tickers

    collection = Tickers("NTPC,RELIANCE")
    assert all(t.statement_fallback is False for t in collection.tickers.values())
    # ``Tickers``' own bool handling is untouched by the new keyword: on the collection -- not
    # on ``Ticker``, whose second positional is ``session`` -- a bool is still multi_level_index.
    assert collection.multi_level_index is True
    assert Tickers(["NTPC"], False).multi_level_index is False


def test_a_tickers_collection_with_the_flag_on_reaches_the_fallback(empty_ticker, monkeypatch):
    """End to end through the collection, not just on the attribute."""
    from bfinance.tickers import Tickers

    monkeypatch.setattr(
        "bfinance.statements.fundamentals_fetch.fetch_fundamentals_json_sync", _FakeTransport()
    )
    ticker = Tickers(["NTPC"], statement_fallback=True)["NTPC"]
    ticker._ensure_profile = lambda: empty_ticker._ensure_profile()

    frame = ticker.financials
    assert not frame.empty
    assert frame.attrs["bfinance_statements_provider"] == "trendlyne-json"
    assert ticker.multi_level_index is True
    assert ticker.data_warnings == ()


def test_the_keyword_reaches_the_constructor_and_a_bool_second_positional_is_refused():
    """``Ticker.__init__`` is a real signature now: ``(ticker, session=None, *, ...)``.

    The statement-fallback keyword is appended and keyword-reachable, so every existing call site
    is unaffected. The old yfinance-compat shim in ``tickers.py`` that read a bool second
    positional as ``multi_level_index`` is gone, so every non-``None`` second positional binds to
    ``session`` and is refused with ``NotImplementedError`` -- yfinance 1.7.0 refuses the same
    call. ``multi_level_index`` is therefore settable by keyword only, which is asserted here.
    """
    ticker = Ticker("NTPC", statement_fallback=True)
    assert ticker.statement_fallback is True
    assert Ticker("NTPC").statement_fallback is False
    for value in (True, False):
        with pytest.raises(NotImplementedError):
            Ticker("NTPC", value)
    assert Ticker("NTPC", multi_level_index=True).multi_level_index is True


# --------------------------------------------------------------------------- #
# ``pretty`` is refused, not ignored
# --------------------------------------------------------------------------- #
#
# All three accessors accepted ``pretty`` and read it nowhere. It was not cosmetic: against
# yfinance 1.7.0 (tag ``3d9d2f0``) ``Ticker.income_stmt`` -- the property -- forces
# ``pretty=True`` and returns Title Case ("Total Revenue"), while ``Ticker.get_income_stmt()``
# with its default returns the same rows CamelCase ("TotalRevenue"). The flag is the whole
# difference between two yfinance surfaces a drop-in caller could have copied from.
#
# Neither path here reads it. ``FinancialStatement.to_yfinance()`` has no ``pretty`` parameter
# and applies ``INDIAN_TO_YFINANCE_LABELS`` unconditionally, so the screener.in frame is Title
# Case by the label map's doing rather than the flag's; the fallback frame carries the vendor's
# own ``sstr``. Honouring the flag would need an unmeasured label map in one direction or the
# other, which ``bfinance.statements.model`` refuses in its module docstring. So it raises.

_REASON_FRAGMENTS = ("pretty=True", "label map")


@pytest.mark.parametrize(
    "accessor", ["get_income_stmt", "get_balance_sheet", "get_cash_flow"]
)
def test_pretty_true_raises_on_every_accessor(accessor, empty_ticker, monkeypatch):
    """Both vendor paths are covered: the flag raises before either can serve a frame."""
    monkeypatch.setattr(
        "bfinance.statements.fundamentals_fetch.fetch_fundamentals_json_sync", _FakeTransport()
    )
    with pytest.raises(NotImplementedError) as excinfo:
        getattr(empty_ticker, accessor)(pretty=True)
    message = str(excinfo.value)
    assert all(fragment in message for fragment in _REASON_FRAGMENTS), message


def test_pretty_true_raises_on_the_screener_path_too(monkeypatch):
    """Not scoped to the fallback. The primary path does not read the flag either -- so raising
    there is not removing a capability, and a caller cannot get a different answer depending on
    which vendor happened to answer."""
    from bfinance.models.company import CompanyProfile, FinancialStatement

    populated = FinancialStatement(headers=["Mar 2025"], rows={"Sales+": [100.0]})
    profile = CompanyProfile(
        symbol="NTPC", company_id=1, name="NTPC Ltd.", profit_loss=populated
    )
    monkeypatch.setattr(Ticker, "_ensure_profile", lambda self: profile)
    ticker = Ticker("NTPC", raise_errors=True)
    with pytest.raises(NotImplementedError, match="label map"):
        ticker.get_income_stmt(pretty=True)


def test_the_alias_raises_too(empty_ticker):
    """``get_cashflow`` forwards ``pretty``, so it must not be a way around the refusal."""
    with pytest.raises(NotImplementedError):
        empty_ticker.get_cashflow(pretty=True)


def test_the_default_still_returns_the_vendors_labels_unchanged(empty_ticker, monkeypatch):
    """The refusal is scoped to ``pretty=True``. ``pretty=False`` -- the default, and what every
    existing caller passes implicitly -- keeps returning what the serving path publishes, which
    is asserted here on a real recorded label rather than on a row count."""
    monkeypatch.setattr(
        "bfinance.statements.fundamentals_fetch.fetch_fundamentals_json_sync", _FakeTransport()
    )
    ticker = Ticker("NTPC", raise_errors=True, statement_fallback=True)
    ticker._ensure_profile = lambda: empty_ticker._ensure_profile()

    frame = ticker.get_income_stmt()
    assert "Total Rev. Ann." in frame.index
    # ...and neither vocabulary yfinance would produce under either setting of the flag, which is
    # the point: the caller gets the vendor's own label, not a silently relabelled one.
    assert "TotalRevenue" not in frame.index
    assert "Total Revenue" not in frame.index

    balance = ticker.get_balance_sheet()
    assert "Total Assets Ann." in balance.index


def test_pretty_false_is_still_the_default_and_is_accepted(empty_ticker, monkeypatch):
    """Explicit ``pretty=False`` is the documented default and must behave identically to passing
    nothing, on both vendor paths."""
    from bfinance.models.company import CompanyProfile, FinancialStatement

    populated = FinancialStatement(headers=["Mar 2025"], rows={"Sales+": [100.0]})
    profile = CompanyProfile(
        symbol="NTPC", company_id=1, name="NTPC Ltd.", profit_loss=populated
    )
    ticker = Ticker("NTPC", raise_errors=True)
    monkeypatch.setattr(Ticker, "_ensure_profile", lambda self: profile)

    implicit = ticker.get_income_stmt()
    explicit = ticker.get_income_stmt(pretty=False)
    assert list(explicit.index) == list(implicit.index)
    assert not explicit.empty


def test_the_refusal_costs_no_request(empty_ticker, monkeypatch):
    """The raise is decided before any work, so a refused call opens no socket -- the same rule
    ``history()`` follows for an unknown source name."""
    transport = _FakeTransport()
    monkeypatch.setattr(
        "bfinance.statements.fundamentals_fetch.fetch_fundamentals_json_sync", transport
    )
    ticker = Ticker("NTPC", raise_errors=True, statement_fallback=True)
    ticker._ensure_profile = lambda: empty_ticker._ensure_profile()
    with pytest.raises(NotImplementedError):
        ticker.get_income_stmt(pretty=True)
    assert transport.calls == []
    assert ticker.data_warnings == (), "a refused flag was recorded as a degradation"
