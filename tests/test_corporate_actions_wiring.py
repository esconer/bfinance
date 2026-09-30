"""
Wiring tests: the corporate-actions feed reaches ``Ticker.history()`` and ``Adj Close``.

Fully offline. The NSE feed is replaced by a stub client that returns a recorded payload, and the
price bars by a whole-market bhavcopy stub, so the back-adjustment arithmetic can be checked against
hand-computable numbers.

Three contracts are under test, and the third is the one that is easy to get wrong:

1. **Provenance.** A frame built on the NSE feed says so, in ``df.attrs``; a frame built on the
   legacy fiscal-year-end approximation says *that* instead. The two are never merged.
2. **Arithmetic.** ``Adj Close`` is ``Close`` times a factor derived only from the merged actions.
   Ex-date bars and everything after them are unadjusted.
3. **No price blending.** ``Close`` is bit-identical to the bhavcopy value, and the *only* thing
   that touches it is the action factor. There is no second price to average in, and the corporate
   actions feed is not asked for one.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bfinance.market import corporate as corporate_mod
from bfinance.market import ohlcv as ohlcv_mod
from bfinance.market.corporate import (
    ACTIONS_SOURCE_NONE,
    ACTIONS_SOURCE_NSE,
    ACTIONS_SOURCE_SCREENER,
    CorporateActionsEngine,
)
from bfinance.market.ohlcv import OHLCVEngine
from bfinance.models.company import CompanyProfile
from bfinance.models.statements import FinancialStatement
from bfinance.nse import corporate_actions as ca

FIXTURES = Path(__file__).parent / "fixtures" / "corporate_actions"


def load(name: str):
    with open(FIXTURES / name, encoding="utf-8") as handle:
        return json.load(handle)


# --------------------------------------------------------------------- stubs


class _StubActionsClient:
    """Stands in for ``CorporateActionsClient``. Returns a recorded payload, or raises.

    Records what it was asked for so a test can assert the request shape without a transport.
    """

    def __init__(self, payload=None, error=None):
        self.payload = payload if payload is not None else load("nse_reliance_2020_2026.json")
        self.error = error
        self.calls = []

    async def actions_async(self, symbol, start=None, end=None, index="equities"):
        self.calls.append({"symbol": symbol, "start": start, "end": end, "index": index})
        if self.error is not None:
            raise self.error
        events = ca.parse_corporate_actions(
            self.payload, symbol=symbol, url="fixture"
        )
        return ca.actions_frame(events, symbol=symbol, url="fixture")


class _StubScreener:
    """Close-only screener stub with an optional profile, no network."""

    def __init__(self, profile=None):
        self.profile = profile
        self.profile_calls = []

    async def get_chart_timeseries(self, ticker, metric="price", days=1825):
        return pd.DataFrame()

    async def get_company_profile(self, ticker, force_refresh=False):
        self.profile_calls.append(ticker)
        if self.profile is None:
            raise ValueError("no profile")
        return self.profile


def _screener_profile():
    """The legacy approximation's input: 50% of EPS 20 = Rs 10, dated at the fiscal year end."""
    return CompanyProfile(
        symbol="RELIANCE", company_id=1, name="Reliance",
        profit_loss=FinancialStatement(
            headers=["Jun 2024"], rows={"Dividend Payout %": [50.0], "EPS in Rs": [20.0]},
        ),
    )


def _market_frame(rows):
    """Whole-market bhavcopy-shaped frame, exactly as ``nse.bhavcopy`` returns it."""
    dates, symbols, values = [], [], []
    for symbol, bars in rows.items():
        for date, o, h, low, close, volume in bars:
            dates.append(date)
            symbols.append(symbol)
            values.append((o, h, low, close, volume))
    index = pd.MultiIndex.from_arrays(
        [pd.DatetimeIndex(dates), pd.Index(symbols, dtype=object, name="Symbol")],
        names=["Date", "Symbol"],
    )
    frame = pd.DataFrame(
        values, columns=["Open", "High", "Low", "Close", "Volume"], index=index
    )
    frame["Volume"] = frame["Volume"].astype("int64")
    frame = frame.sort_index()
    frame.attrs["bfinance_synthetic_ohlc"] = False
    return frame


# Three bars around RELIANCE's real 2024-08-19 ex-date (Rs 10 dividend, prior close 2956.40).
# The dividend lands on the third bar so the factor must touch only the two before it.
BARS = {
    "RELIANCE": [
        ("2024-08-16", 2900.0, 2970.0, 2890.0, 2956.40, 1_000),
        ("2024-08-19", 2956.40, 2990.0, 2940.0, 2976.80, 2_000),
        ("2024-08-20", 2976.80, 3010.0, 2960.0, 3000.00, 3_000),
    ]
}

# A window spanning the real 28-Oct-2024 1:1 bonus (-> 2.0) and the 19-Aug-2024 dividend.
BONUS_BARS = {
    "RELIANCE": [
        ("2024-08-16", 2900.0, 2970.0, 2890.0, 2956.40, 1_000),
        ("2024-08-19", 2956.40, 2990.0, 2940.0, 2976.80, 2_000),
        ("2024-10-25", 2600.0, 2670.0, 2590.0, 2655.70, 4_000),
        ("2024-10-28", 2655.70, 2700.0, 1320.0, 1334.35, 5_000),
    ]
}

# A window in which RELIANCE genuinely has no corporate action. NSE indexes this symbol and has
# rows for it, so "nothing in this window" is a real answer, not a coverage gap.
QUIET_BARS = {
    "RELIANCE": [
        ("2024-05-02", 2400.0, 2450.0, 2390.0, 2440.00, 1_000),
        ("2024-05-03", 2440.00, 2470.0, 2420.0, 2460.00, 2_000),
    ]
}


def _use_bars(monkeypatch, bars):
    monkeypatch.setattr(
        ohlcv_mod, "bhavcopy_range", lambda start, end, **kw: _market_frame(bars)
    )
    return bars


@pytest.fixture
def bars(monkeypatch):
    """Install the bhavcopy stub. The corporate-actions client is passed in per-test."""
    return _use_bars(monkeypatch, BARS)


def _engine(actions_client, profile=None):
    return OHLCVEngine(
        _StubScreener(profile=profile), corporate_actions_client=actions_client
    )


async def _history(actions_client, profile=None, **kwargs):
    return await _engine(actions_client, profile=profile).fetch_history(
        symbol="RELIANCE.NS", period="5d", actions=True, auto_adjust=False, **kwargs
    )


async def _fetch(actions_client, profile=None, **kwargs):
    """``_history`` with ``auto_adjust`` left to the caller, for the two auto_adjust=True tests.

    The two are separate rather than one with a default, because every other caller in this file
    is asserting the ``auto_adjust=False`` contract and a shared default would let a future test
    inherit it without meaning to.
    """
    return await _engine(actions_client, profile=profile).fetch_history(
        symbol="RELIANCE.NS", period="5d", actions=True, **kwargs
    )


# --------------------------------------------------------------------- 1. provenance


async def test_nse_feed_is_the_source_and_is_recorded(bars):
    df = await _history(_StubActionsClient())
    assert df.attrs["bfinance_actions_source"] == ACTIONS_SOURCE_NSE
    assert "NSE corporate-actions feed" in df.attrs["bfinance_actions_source_detail"]
    assert df.attrs["bfinance_adj_close_source"].startswith("back-adjusted Close by NSE")
    assert df.attrs["bfinance_actions_source_url"] == "fixture"
    assert df.attrs["bfinance_actions_dividend_basis"] == "unadjusted_rupees_at_ex_date"
    # The real event count, so a consumer can see what the feed actually returned.
    assert df.attrs["bfinance_actions_event_counts"]["dividend"] == 7


async def test_dividend_lands_on_the_true_ex_date(bars):
    """The whole point. 2024-08-19 is NSE's ex-date; the legacy path dated it 2024-06-30.

    The three bars are 16, 19 and 20 Aug 2024, so an ex-date at fiscal year end (30 Jun) would
    touch nothing here and ``Adj Close`` would be a copy of ``Close``.
    """
    client = _StubActionsClient()
    df = await _history(client)

    by_date = {str(idx.date()): float(val) for idx, val in df["Dividends"].items()}
    assert by_date["2024-08-19"] == pytest.approx(10.0)
    assert by_date["2024-08-16"] == 0.0
    assert by_date["2024-08-20"] == 0.0
    # And the request carried the symbol, not a period-limited guess.
    assert client.calls[0]["symbol"] == "RELIANCE"
    assert client.calls[0]["index"] == "equities"


async def test_provenance_survives_a_window_filter(bars):
    """Slicing must not launder the provenance away."""
    df = await _history(_StubActionsClient(), start="2024-08-19", end="2024-08-21")
    assert not df.empty
    assert df.attrs["bfinance_actions_source"] == ACTIONS_SOURCE_NSE
    assert float(df["Dividends"].sum()) == pytest.approx(10.0)


# --------------------------------------------------------------------- 2. arithmetic


async def test_adj_close_is_close_times_the_action_factor(bars):
    """factor(t) = prod over actions dated after t of (1 - D/prev_close) / S.

    The recorded payload carries RELIANCE's real 2024-10-28 1:1 bonus as well as the 19-Aug Rs 10
    dividend, and the bonus is dated AFTER all three bars -- so ``Adj Close`` carries 1/2 on every
    one of them. That is not a new opinion about the dividend; it is the correction of the same
    defect this work removed from the price columns: a share-count event restates every earlier
    bar whether or not the window shows it, and yfinance's own "Adj Close" does the same
    (RELIANCE 2024-08-16: yfinance Close 1477.60, Adj Close 1464.85 -- both on the halved basis).

    ``adjust_corporate_actions=False`` opts out of the default basis, so this per-column view of
    the as-traded prices stays observable. The adjusted-path arithmetic is pinned in
    tests/test_bhavcopy_parity_adjust.py.
    """
    df = await _history(_StubActionsClient(), adjust_corporate_actions=False)
    close = df["Close"].astype(float)

    # 1 - 10/2956.40 = 0.996617...  from the dividend, times 1/2 from the later bonus.
    expected_pre = close.iloc[0] * (1.0 - 10.0 / close.iloc[0]) * 0.5
    assert float(df["Adj Close"].iloc[0]) == pytest.approx(expected_pre)
    # The dividend ex-date bar and everything after it: still halved by the bonus, and no dividend.
    assert float(df["Adj Close"].iloc[1]) == pytest.approx(close.iloc[1] * 0.5)
    assert float(df["Adj Close"].iloc[2]) == pytest.approx(close.iloc[2] * 0.5)
    assert not df["Adj Close"].equals(df["Close"])


async def test_adj_close_equals_close_when_the_feed_finds_nothing_in_the_window(monkeypatch):
    """An empty *in-window* answer from a symbol the feed DOES index is a real answer.

    This is the distinction the whole resolution order turns on. The feed returns rows for
    RELIANCE, so it has an opinion about May 2024, and that opinion is that nothing happened.
    ``Adj Close == Close`` is then a fact about the instrument -- not a gap, and emphatically not
    a licence to substitute the fiscal-year-end approximation.
    """
    _use_bars(monkeypatch, QUIET_BARS)
    client = _StubActionsClient()          # the real recorded RELIANCE payload
    df = await _history(client, start="2024-05-02", end="2024-05-04")

    assert client.calls, "the feed was consulted"
    assert df.attrs["bfinance_actions_source"] == ACTIONS_SOURCE_NSE
    assert bool((df["Dividends"] == 0.0).all())
    assert bool((df["Stock Splits"] == 0.0).all())
    pd.testing.assert_series_equal(df["Adj Close"], df["Close"], check_names=False)


async def test_bonus_ratio_drives_the_split_factor(monkeypatch):
    """RELIANCE's real 28-Oct-2024 1:1 bonus -> 2.0, halving every earlier bar.

    Read off the as-traded series (``adjust_corporate_actions=False``); on the default path this
    same factor is already inside ``Close``, which is the point -- see
    tests/test_bhavcopy_parity_adjust.py.
    """
    _use_bars(monkeypatch, BONUS_BARS)
    df = await _history(_StubActionsClient(), adjust_corporate_actions=False)

    assert float(df.loc[df["Dividends"] != 0.0, "Dividends"].iloc[0]) == pytest.approx(10.0)
    assert float(df["Stock Splits"].sum()) == pytest.approx(2.0)
    # Bars before the bonus carry (dividend factor) x (1/2); the bonus bar itself carries 1.0.
    factor = df["Adj Close"] / df["Close"]
    expected = 0.5 * (1.0 - 10.0 / 2956.40)
    assert float(factor.iloc[0]) == pytest.approx(expected)
    assert float(factor.iloc[1]) == pytest.approx(0.5)
    assert float(factor.iloc[2]) == pytest.approx(0.5)
    assert float(factor.iloc[3]) == pytest.approx(1.0)


async def test_a_feed_the_instrument_is_absent_from_falls_back_loudly(bars):
    """Zero rows for the whole symbol means the feed does not index it, not "nothing happened".

    That is the one case where the legacy approximation is consulted, and it is labelled: the
    approximation's fiscal-year-end date (30 Jun 2024) touches none of these bars, so the frame
    records an approximation rather than a real event.
    """
    df = await _history(_StubActionsClient(payload=[]), profile=_screener_profile())
    assert df.attrs["bfinance_actions_source"] == ACTIONS_SOURCE_SCREENER
    assert "fiscal year end" in df.attrs["bfinance_actions_source_detail"]
    assert "does not index this instrument" in df.attrs["bfinance_actions_fallback_reason"]
    assert bool((df["Dividends"] == 0.0).all())


async def test_an_unreachable_feed_falls_back_loudly(bars):
    df = await _history(
        _StubActionsClient(error=RuntimeError("nse blocked")),
        profile=_screener_profile(),
    )
    assert df.attrs["bfinance_actions_source"] == ACTIONS_SOURCE_SCREENER
    assert "unreachable" in df.attrs["bfinance_actions_fallback_reason"]
    assert "nse blocked" in df.attrs["bfinance_actions_fallback_reason"]


async def test_fallback_can_be_switched_off(bars):
    """With no fallback, an absent feed yields an honest absence, not a fabricated ex-date."""
    resolved = await CorporateActionsEngine.resolve_actions_async(
        "RELIANCE", client=_StubActionsClient(payload=[]), fallback="none"
    )
    assert resolved.source == ACTIONS_SOURCE_NONE
    assert resolved.dividends.empty and resolved.splits.empty


async def test_unknown_fallback_is_rejected():
    with pytest.raises(ValueError):
        await CorporateActionsEngine.resolve_actions_async(
            "RELIANCE", client=_StubActionsClient(), fallback="average"
        )


async def test_no_source_at_all_yields_zeros_not_a_guess(bars):
    """Unreachable feed and no profile -> zeros, and the detail says why."""
    df = await _history(_StubActionsClient(error=RuntimeError("boom")))
    assert df.attrs["bfinance_actions_source"] == ACTIONS_SOURCE_NONE
    assert bool((df["Dividends"] == 0.0).all())
    assert bool((df["Stock Splits"] == 0.0).all())
    assert "not known" in df.attrs["bfinance_actions_source_detail"] or "boom" in (
        df.attrs["bfinance_actions_source_detail"]
    )


# --------------------------------------------------------------------- 3. no price blending


async def test_close_is_bit_identical_to_the_bhavcopy_value(bars):
    """A blended or averaged price would move. This one does not move at all.

    Asserted on the as-traded series (``adjust_corporate_actions=False``). The only transformation
    applied to the published price anywhere on this path is multiplication by an action factor --
    which on the default path lands in ``Close`` itself, and is pinned as exactly
    ``published * factor`` (never a blend) in tests/test_bhavcopy_parity_adjust.py.
    """
    df = await _history(_StubActionsClient(), adjust_corporate_actions=False)
    published = _market_frame(BARS).xs("RELIANCE", level="Symbol")["Close"]
    np.testing.assert_array_equal(df["Close"].to_numpy(), published.to_numpy())
    assert df["Close"].tolist() == [2956.40, 2976.80, 3000.00]


async def test_the_two_sources_are_never_merged(bars):
    """A symbol the feed DOES index never reaches the approximation, even with a profile loaded.

    The stub profile declares a Rs 10 dividend dated 30 Jun 2024. If the two sources were combined,
    or if the approximation were consulted for an indexed symbol, that phantom would appear.
    """
    df = await _history(_StubActionsClient(), profile=_screener_profile())
    assert df.attrs["bfinance_actions_source"] == ACTIONS_SOURCE_NSE
    # Exactly one dividend, dated on the NSE ex-date. Not two, and not a second one in June.
    nonzero = df.index[df["Dividends"] != 0.0]
    assert list(nonzero) == [pd.Timestamp("2024-08-19", tz="Asia/Kolkata")]
    assert float(df["Dividends"].sum()) == pytest.approx(10.0)


async def test_the_actions_feed_is_never_asked_for_a_price(bars):
    """One request, for actions, over the whole history.

    The window is *not* sent: the feed answers with an empty list both for "nothing in this window"
    and for "I do not index this symbol", and only a full-history answer tells them apart.
    """
    client = _StubActionsClient()
    await _history(client)
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["start"] is None and call["end"] is None
    # The screener profile is never touched on the primary path.
    assert engine_profile_calls == []


# A module-level recorder so the last test can assert the screener was not consulted at all.
engine_profile_calls: list = []


@pytest.fixture(autouse=True)
def _reset_profile_recorder():
    engine_profile_calls.clear()
    yield
    engine_profile_calls.clear()


async def test_screener_is_only_consulted_for_the_labelled_fallback(bars, monkeypatch):
    """The legacy P&L path is reachable, but only behind the fallback and only when labelled."""
    engine = _engine(_StubActionsClient())
    monkeypatch.setattr(
        engine, "_load_profile", lambda symbol: _record(symbol)
    )
    df = await engine.fetch_history(
        symbol="RELIANCE.NS", period="5d", actions=True, auto_adjust=False
    )
    assert df.attrs["bfinance_actions_source"] == ACTIONS_SOURCE_NSE
    assert engine_profile_calls == [], "the primary path must not touch Screener.in"


async def _record(symbol):
    engine_profile_calls.append(symbol)
    return _screener_profile()


# --------------------------------------------------------------------- 4. auto_adjust


async def test_auto_adjust_true_applies_the_real_factor_rather_than_refusing(bars):
    """
    This test asserted the opposite, and the reason it gave is no longer true.

    The old docstring said the refusal "stayed" because *coverage* was unproven -- an Indian split
    is published as a bonus issue or a face-value sub-division, so recall was sampled rather than
    censused, and rescaling every OHLC column was not yet a promise bfinance should make. That is a
    real and still-published caveat, and it is carried by
    ``attrs["bfinance_adjustment_incomplete"]`` -- it was never a reason to refuse the *keyword*,
    because yfinance 1.7 defaults ``auto_adjust`` to ``True`` and a drop-in that dies on the value
    its closest competitor reaches by saying nothing is not a drop-in.

    So the call now does what yfinance does, with the factors this feed already supplies: scale
    Open/High/Low/Close by ``Adj Close / Close`` and drop ``Adj Close``. The fixture's three bars
    straddle RELIANCE's real 19-Aug-2024 Rs 10 dividend, so the ratio is 0.99661764 on the bar
    before the ex-date and 1.0 after -- a column drop that scaled nothing cannot pass this.
    """
    adjusted = await _fetch(_StubActionsClient(), auto_adjust=False)
    auto = await _fetch(_StubActionsClient(), auto_adjust=True)

    assert "Adj Close" not in auto.columns
    assert list(auto.columns) == [
        "Open", "High", "Low", "Close", "Volume", "Dividends", "Stock Splits"
    ]
    assert auto["Close"].tolist() == pytest.approx(adjusted["Adj Close"].tolist(), rel=1e-12)
    ratio = (adjusted["Adj Close"] / adjusted["Close"]).tolist()
    assert ratio == pytest.approx([1.0 - 10.0 / 2956.40, 1.0, 1.0], rel=1e-12)
    assert auto["Volume"].tolist() == adjusted["Volume"].tolist()


async def test_auto_adjust_true_is_still_refused_when_the_feed_is_not_ex_dated(bars):
    """
    The refusal this change KEEPS, on the case the old docstring was really about.

    The "coverage" caveat above has a concrete face: when the feed cannot be reached, or does not
    index the instrument, ``Adj Close`` is not an adjusted series and scaling four price columns
    by it would restate history with a number that never existed. That is the one output this
    package exists to not produce, so ``True`` still refuses -- but the message now names WHICH
    case it is, instead of claiming there are no factors at all. There are: they are real when the
    feed answers, and absent when it does not, and the message has to be able to tell the two.
    """
    from bfinance.market.ohlcv import AdjustmentNotAvailableError

    with pytest.raises(AdjustmentNotAvailableError) as exc:
        await _fetch(_StubActionsClient(error=RuntimeError("feed down")), auto_adjust=True)
    message = str(exc.value)
    # And it must not still claim the input is missing: a real factor DOES exist, it just was not
    # fetched on this call.
    assert "carries no dividend or split series" not in message
    assert "no corporate-actions feed with real" in message
    assert "would fabricate history" in message
    # The approximation is a refusal too, and for a different reason -- the two are not the same
    # defect and a single "it raises" test could not tell them apart.
    with pytest.raises(AdjustmentNotAvailableError) as approx_exc:
        await _fetch(
            _StubActionsClient(payload=[]), profile=_screener_profile(), auto_adjust=True
        )
    approx_message = str(approx_exc.value)
    assert "fiscal-year-end approximation" in approx_message
    assert "corporate-actions" in approx_message


# --------------------------------------------------------------------- 5. engine surface


def test_history_signature_and_dispatch_are_untouched():
    """The actions path must not have moved the public contract, and the price-source seam did
    exactly one thing to it: appended two optional parameters at the end.

    The previous version of this docstring said a change here "would be an unrequested contract
    edit" and that ``fetch_history``'s signature, the ``ohlc`` parameter and the bhavcopy dispatch
    "belong to another agent's work". That is no longer true, and the reason is worth recording
    rather than quietly overwriting:

    * ``source=`` (one price source) and ``sources=`` (an ordered list to fail over through) were
      **requested**, by plans/trendlyne-default-failsafe-yfinance.md task T3a. They put the
      existing :mod:`bfinance.sources.cascade` on the price path so ``history()`` can fail over
      instead of having exactly one vendor and one failure mode.
    * Everything this test pins about the *actions* contract is unchanged, and is still asserted:
      ``ohlc`` still defaults to ``"bhavcopy"``, ``OHLC_SOURCES`` is still
      ``("bhavcopy", "synthetic")``, the bhavcopy dispatch is still the default, and the real
      archive reader is still wired in. The new parameters are **appended, optional, and default
      to ``None``**, so every existing call -- positional or keyword -- resolves to what it did.
      That is asserted below by position as well as by default, because a default that is right
      while the parameter has been inserted in the middle would break every positional caller and
      no default-value assertion would notice.
    """
    import inspect

    names = list(inspect.signature(ohlcv_mod.OHLCVEngine.fetch_history).parameters)
    assert names[1:] == [
        "symbol", "period", "interval", "start", "end", "prepost", "actions", "auto_adjust",
        "back_adjust", "repair", "keepna", "rounding", "timeout", "raise_errors", "ohlc",
        "adjust_corporate_actions", "mirror", "source", "sources",
    ], (
        f"the existing parameters must keep their positions and order; the source parameters are "
        f"appended, never inserted: {names}"
    )

    params = inspect.signature(ohlcv_mod.OHLCVEngine.fetch_history).parameters
    assert params["ohlc"].default == "bhavcopy"
    assert params["actions"].default is True
    assert params["auto_adjust"].default is False
    assert params["source"].default is None, (
        "a default other than None would silently put every existing caller on the source cascade, "
        "which is a contract change and not a seam"
    )
    assert params["sources"].default is None
    assert ohlcv_mod.OHLC_SOURCES == ("bhavcopy", "synthetic")
    assert ohlcv_mod.bhavcopy_range is not None


def test_legacy_extractors_are_still_available_for_ticker():
    """``bfinance.ticker`` calls these directly, so the fallback cannot be deleted."""
    profile = _screener_profile()
    dividends = CorporateActionsEngine.extract_dividends(profile)
    assert float(dividends.iloc[0]) == pytest.approx(10.0)
    assert str(dividends.index.tz) == "Asia/Kolkata"
    assert corporate_mod.ACTIONS_SOURCE_SCREENER != corporate_mod.ACTIONS_SOURCE_NSE
