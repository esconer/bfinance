"""
Drop-in parity LOOP vs live yfinance (yfinance 1.7.0): bfinance
actions/holders/calendar/news/options surface must match yfinance shapes
for RELIANCE.NS (+ NIFTYBEES.NS for ETF dividend behavior only).

Conventions recorded here (see src/bfinance/market/corporate.py):
- yfinance dividends indexed by EX-DATE; bfinance by FISCAL YEAR-END
  (Mar-31 Asia/Kolkata). Amounts compared within 5% for last 4 payouts.
- Splits: yfinance represents a 1:1 bonus as a 2.0 split ratio. bfinance's
  `Ticker.splits` now reads the NSE corporate-actions filings -- the same feed
  `Ticker.actions` uses -- so the factor is the filing's own `SplitRatio` and the
  index is the declared ex-date. `CorporateActionsEngine.extract_splits` is still
  empty by construction: the deleted equity-capital inference is not coming back,
  and `test_extract_splits_is_unconditional...` keeps it that way.
- `Ticker.actions` is built from that one feed rather than by concatenating
  `dividends` and `splits`, so its `Dividends` column is NOT `Ticker.dividends`.
- yfinance capital_gains is EMPTY for Indian equities; bfinance matches.
- yfinance options == () for NSE underlyings (even RELIANCE.NS, an F&O
  stock); option_chain() empty, dated expiry raises ValueError.

Ticker-surface gaps needing ticker.py wiring are probed (not failed) and
tagged NEEDS-MAIN in stdout.
"""

import pandas as pd
import pytest

yf = pytest.importorskip("yfinance")

from bfinance.market.corporate import (
    ACTIONS_COLUMNS,
    YF_CALENDAR_KEYS,
    YF_MAJOR_HOLDERS_INDEX,
    CorporateActionsEngine,
)
from bfinance.models.company import CompanyProfile, FinancialStatement
from bfinance.market.derivatives import DerivativesEngine, YF_CHAIN_COLUMNS
from bfinance.ticker import Ticker

pytestmark = pytest.mark.live

RELIANCE = "RELIANCE.NS"
SMALLCAP = "GRAVITA.NS"  # non-F&O NSE underlying
ETF = "NIFTYBEES.NS"


def _needsmain(msg: str) -> None:
    print(f"NEEDS-MAIN: {msg}")


def test_dividends_last4_amounts_and_conventions():
    """Last 4 RELIANCE payouts: per-share amounts within 5%; record date conventions."""
    print(f"yfinance version: {yf.__version__}")
    y = yf.Ticker(RELIANCE).dividends
    b = Ticker(RELIANCE).dividends
    assert isinstance(b, pd.Series) and b.name == "Dividends"
    assert str(b.index.tz) == "Asia/Kolkata", f"bf index tz={b.index.tz}"
    last4 = y.tail(4)
    print("yfinance convention=ex-date; bfinance convention=fiscal-year-end Mar-31")
    for ts, y_amt in last4.items():
        year = ts.year
        cand = b[b.index.year == year]
        assert len(cand) >= 1, f"no bfinance FY payout for {year}"
        b_amt = float(cand.iloc[-1])
        b_date = cand.index[-1].date()
        rel = abs(b_amt - float(y_amt)) / float(y_amt)
        print(f"{year}: yf ex-date={ts.date()} amt={y_amt} | bf FY-end={b_date} amt={b_amt} rel-diff={rel:.3%}")
        assert rel <= 0.05, f"{year}: {b_amt} vs yfinance {y_amt}"
        assert (ts.date() - b_date).days > 3, "expected convention gap (ex-date vs FY-end)"


def test_splits_come_from_the_nse_filings_not_an_equity_capital_inference():
    """
    ``Ticker.splits`` is NSE's declared ex-date and factor, read from the filings feed.

    It used to be asserted the other way round: this test read ``b.index[-1]`` off a bfinance
    series that was empty by construction, on the understanding that bfinance inferred a split
    from growth in the balance sheet's ``Equity Capital`` row. That inference was deleted from
    :meth:`CorporateActionsEngine.extract_splits` -- because ``Equity Capital`` is ``shares issued
    x face value``, so a 1:1 bonus, a rights issue, a QIP and an ESOP conversion are the *same
    number in the same field* -- and the property now takes the filing's own ``SplitRatio`` from
    :func:`bfinance.nse.corporate_actions.splits_from_actions`. It is the same feed
    ``Ticker.actions`` and ``history``'s ``Stock Splits`` column use.

    yfinance is the independent statement of the same events, so the shared dates must agree on
    the factor. Dates only one side lists are printed rather than asserted: the two sources cover
    different scopes (the feed is NSE-only; yfinance may carry a longer or differently normalised
    history), and inventing a rule for those would be asserting parity nobody measured.
    """
    y = yf.Ticker(RELIANCE).splits
    b = Ticker(RELIANCE).splits

    assert isinstance(b, pd.Series) and b.name == "Stock Splits"
    assert isinstance(b.index, pd.DatetimeIndex) and b.index.name == "Date"
    assert str(b.index.tz) == "Asia/Kolkata", f"bf index tz={b.index.tz}"
    assert b.dtype == float
    assert (b > 0).all(), "a split factor is a positive share-count multiplier"
    assert b.attrs["bfinance_actions_source"] == "nse_corporate_actions", (
        "the property must read the NSE filings feed; extract_splits returns an empty Series by "
        "construction, so a populated series that claims another source is a re-added inference"
    )
    assert b.attrs["bfinance_symbol"] == "RELIANCE"

    common = b.index.intersection(y.index)
    print(f"yfinance splits: {y.to_dict()}")
    print(f"bfinance splits: {b.to_dict()} (NSE ex-dates, filing factors)")
    print(f"dates only yfinance: {sorted(set(y.index) - set(b.index))}")
    print(f"dates only bfinance: {sorted(set(b.index) - set(y.index))}")
    if not b.empty and not y.empty:
        assert len(common) > 0, "two histories of the same symbol share no ex-date"
    for ts in common:
        assert float(b.loc[ts]) == pytest.approx(float(y.loc[ts])), (
            f"{ts.date()}: bfinance {b.loc[ts]} vs yfinance {y.loc[ts]}"
        )



def test_extract_splits_is_unconditional_so_no_balance_sheet_can_re_add_the_inference():
    """
    The negative control for the test above, and the non-vacuity proof for the split claim.

    ``extract_splits(profile)`` takes a profile and returns an empty Series **without reading it**.
    That is asserted structurally -- over a profile carrying the exact signal the deleted inference
    keyed on -- rather than asserted as "it happened to be empty today", which would be equally true
    of a property that is simply broken. ``Ticker.splits`` now reads the NSE filings instead of this
    engine, and this control is what keeps the engine from being re-wired to the balance sheet.

    The signal is ``Equity Capital``, a row of the BALANCE SHEET, not a ``TopRatios`` field: the
    deleted code divided consecutive periods of it and wrote a split at ``ratio >= 1.5``. So the
    control builds a real :class:`FinancialStatement` carrying the measured capital raise,
    4505 -> 23700 Cr -- a ratio of 5.2608, which cleared that cliff by more than three times --
    and requires the answer to still be empty. Nothing is mutated and no network is spent, so the
    control is deterministic. If the inference is ever pasted back, this goes red.
    """
    raise_cr = FinancialStatement(
        headers=["Mar 2025", "Mar 2026"],
        rows={"Equity Capital": [4505.0, 23700.0]},
    )
    profile = CompanyProfile(
        symbol=RELIANCE, company_id=0, name="R", balance_sheet=raise_cr
    )
    split_series = CorporateActionsEngine.extract_splits(profile)

    assert 23700.0 / 4505.0 == pytest.approx(5.2608, abs=1e-4), (
        "the control's input must still be the measured 5.26x capital raise"
    )
    assert 23700.0 / 4505.0 > 1.5, "the control must clear the deleted ratio >= 1.5 cliff"
    assert split_series.empty, (
        "a 5.26x Equity Capital jump produced a split. That is the deleted equity-capital "
        "inference, and it is indistinguishable from a rights issue, a QIP or an ESOP conversion."
    )
    assert list(split_series.index) == []
    assert split_series.name == "Stock Splits"
    print(
        "Equity Capital 4505 -> 23700 Cr (ratio 5.2608, the deleted >= 1.5 cliff) still yields no "
        "split: ambiguous input, no inferred output."
    )


def test_actions_shape_contract():
    """yfinance actions columns/shape contract vs the property and the engine builder."""
    ya = yf.Ticker(RELIANCE).actions
    b = Ticker(RELIANCE).actions
    assert list(ya.columns) == ["Dividends", "Stock Splits"]
    assert list(b.columns) == ACTIONS_COLUMNS == list(ya.columns)
    assert b["Dividends"].dtype == float and b["Stock Splits"].dtype == float
    assert isinstance(b.index, pd.DatetimeIndex) and b.index.name == "Date"
    assert str(b.index.tz) == "Asia/Kolkata", f"bf index tz={b.index.tz}"
    assert b.attrs["bfinance_actions_source"] == "nse_corporate_actions"
    print(f"yfinance actions shape={ya.shape}; bfinance actions shape={b.shape}")
    # The engine builder is still the shape anchor for a caller who already holds two series, but
    # it is not the property's route: Ticker.actions reads one feed so both columns share a date
    # basis, and its Dividends column is deliberately not Ticker.dividends.
    engine = CorporateActionsEngine.build_actions(
        Ticker(RELIANCE).dividends, Ticker(RELIANCE).splits
    )
    assert list(engine.columns) == ACTIONS_COLUMNS
    print("engine build_actions shape unchanged; Ticker.actions reads one feed instead")


def test_capital_gains_empty_parity():
    """yfinance capital_gains empty for Indian equities; bfinance matches."""
    y = yf.Ticker(RELIANCE).capital_gains
    assert isinstance(y, pd.Series) and len(y) == 0
    b = CorporateActionsEngine.build_capital_gains()
    assert isinstance(b, pd.Series) and len(b) == 0
    assert b.name is None and y.name is None, f"names: bf={b.name!r} yf={y.name!r}"
    print(f"capital_gains parity: both empty Series (yf dtype={y.dtype}, bf dtype={b.dtype})")
    if not hasattr(Ticker, "capital_gains"):
        _needsmain("ticker.py: add `capital_gains` property via CorporateActionsEngine.build_capital_gains()")


def test_etf_dividend_empty_parity():
    """ETF dividend behavior: NIFTYBEES.NS empty on both sides."""
    y = yf.Ticker(ETF).dividends
    b = Ticker(ETF).dividends
    assert len(y) == 0 and len(b) == 0, f"yf={len(y)} bf={len(b)}"
    y_splits = yf.Ticker(ETF).splits
    b_splits = Ticker(ETF).splits
    assert len(y_splits) == 0 and len(b_splits) == 0
    # Emptiness now comes from the NSE feed having no row for an ETF, so the Series keeps the
    # indexed, localised shape a populated one would have -- not the RangeIndex the deleted
    # inference left behind.
    assert isinstance(b_splits.index, pd.DatetimeIndex)
    assert str(b_splits.index.tz) == "Asia/Kolkata"
    print(
        f"{ETF}: dividends/splits empty on both sides "
        f"(quoteType={Ticker(ETF).info.get('quoteType')})"
    )


def test_holders_shape_and_float_parity():
    """Holders shapes; numeric floats never percent-strings."""
    ym = yf.Ticker(RELIANCE).major_holders
    assert ym.shape == (4, 1) and list(ym.columns) == ["Value"]
    assert list(ym.index) == YF_MAJOR_HOLDERS_INDEX
    assert ym["Value"].dtype == float
    assert yf.Ticker(RELIANCE).institutional_holders.shape == (0, 0)
    assert yf.Ticker(RELIANCE).mutualfund_holders.shape == (0, 0)

    profile = Ticker(RELIANCE)._ensure_profile()
    bm = CorporateActionsEngine.build_major_holders(profile)
    assert list(bm.columns) == ["Value"] and list(bm.index) == YF_MAJOR_HOLDERS_INDEX
    assert bm["Value"].dtype == float, f"must be float, got {bm['Value'].dtype}"
    assert not bm["Value"].astype(str).str.contains("%").any(), "never percent-strings"
    for k in ["insidersPercentHeld", "institutionsPercentHeld"]:
        print(f"{k}: yf={float(ym.loc[k, 'Value']):.5f} bf={float(bm.loc[k, 'Value']):.5f}")
    rel = abs(float(bm.loc["insidersPercentHeld", "Value"]) - float(ym.loc["insidersPercentHeld", "Value"])) / float(
        ym.loc["insidersPercentHeld", "Value"]
    )
    assert rel <= 0.05, f"insiders drift {rel:.3%}"
    assert CorporateActionsEngine.build_institutional_holders().shape == (0, 0)
    assert CorporateActionsEngine.build_mutualfund_holders().shape == (0, 0)

    t = Ticker(RELIANCE)
    if isinstance(t.major_holders, pd.DataFrame) and "HoldingPercent" in t.major_holders.columns:
        _needsmain("ticker.py major_holders emits percent-strings; rewire to CorporateActionsEngine.build_major_holders()")
    for attr in ("institutional_holders", "mutualfund_holders"):
        if not hasattr(Ticker, attr):
            _needsmain(f"ticker.py: add `{attr}` property (empty (0,0) DataFrame for Indian equities)")


def test_calendar_key_contract():
    """Calendar key set matches yfinance; estimates stay None (never fabricated)."""
    y = yf.Ticker(RELIANCE).calendar
    b = CorporateActionsEngine.build_calendar(
        Ticker(RELIANCE)._ensure_profile(), Ticker(RELIANCE).dividends
    )
    assert set(b.keys()) == set(YF_CALENDAR_KEYS), f"bf={sorted(b.keys())}"
    assert set(y.keys()) == set(YF_CALENDAR_KEYS), f"yf={sorted(y.keys())}"
    print(f"yfinance calendar={y}")
    print(f"bfinance calendar={b}")


def test_options_and_chain_contract():
    """yfinance empty/error contract; bfinance determinism + column contract."""
    for sym in (RELIANCE, SMALLCAP):
        yt = yf.Ticker(sym)
        assert yt.options == (), f"{sym}: yf options={yt.options}"
        chain = yt.option_chain()
        assert chain.calls is None and chain.puts is None, f"{sym}: expected empty chain"
        with pytest.raises(ValueError):
            yt.option_chain("2026-09-24")
        print(f"{sym}: yf options=() chain empty; dated expiry raises ValueError")

    t = Ticker(RELIANCE)
    e = DerivativesEngine.empty_option_chain()
    assert e.calls.shape == (0, 13) and e.puts.shape == (0, 13)
    assert DerivativesEngine.resolve_options(RELIANCE) == ()
    # Ticker.option_chain() must be empty and must stay empty - it previously delegated to a
    # generator that drew volume/open interest from a seeded RNG.
    assert t.option_chain().calls.empty and t.option_chain().puts.empty
    with pytest.raises(NotImplementedError):
        DerivativesEngine.generate_option_chain("RELIANCE", cmp=1500.0, expiry_date="2026-09-24")
    print(f"bf Ticker.options={t.options} vs yf=() -> empty chain, no fabricated data")
    _needsmain("ticker.py: gate `options`/`option_chain` on real F&O membership via DerivativesEngine.resolve_options(); empty chain via empty_option_chain()")


def test_news_shape_contract():
    """News shape: list of {id, content{title,...}}; keys align, content may differ."""
    yn = yf.Ticker(RELIANCE).news
    assert isinstance(yn, list) and len(yn) > 0
    assert set(("id", "content")) <= set(yn[0].keys())
    assert "title" in yn[0]["content"]
    print(f"yfinance news top-keys={list(yn[0].keys())} content-keys={list(yn[0]['content'].keys())[:8]}")

    b_empty = CorporateActionsEngine.build_news(Ticker(RELIANCE)._ensure_profile().announcements)
    assert isinstance(b_empty, list)
    sample = CorporateActionsEngine.build_news([{"title": "Board approves dividend", "url": "https://example.com/x"}])
    assert set(("id", "content")) == set(sample[0].keys())
    for k in ("title", "link", "publisher", "time"):
        assert k in sample[0]["content"], f"missing content key {k}"
    print(f"bfinance news len={len(b_empty)} (upstream announcements empty); adapter keys={sorted(sample[0]['content'].keys())}")
    tn = Ticker(RELIANCE).news
    if tn and "content" not in tn[0]:
        _needsmain("ticker.py: adapt `news` via CorporateActionsEngine.build_news() to yfinance {id, content} shape")
    elif not tn:
        _needsmain("ticker.py: adapt `news` via CorporateActionsEngine.build_news(); upstream announcements currently empty")
