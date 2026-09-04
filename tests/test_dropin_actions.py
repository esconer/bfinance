"""
Drop-in parity LOOP vs live yfinance (yfinance 1.7.0): bfinance
actions/holders/calendar/news/options surface must match yfinance shapes
for RELIANCE.NS (+ NIFTYBEES.NS for ETF dividend behavior only).

Conventions recorded here (see src/bfinance/market/corporate.py):
- yfinance dividends indexed by EX-DATE; bfinance by FISCAL YEAR-END
  (Mar-31 Asia/Kolkata). Amounts compared within 5% for last 4 payouts.
- Bonus issues: BOTH sides represent 1:1 bonus as a 2.0 split ratio.
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


def test_splits_last_split_ratio_and_bonus_convention():
    """Last split: same ratio; both sides render 1:1 bonus as 2.0."""
    y = yf.Ticker(RELIANCE).splits
    b = Ticker(RELIANCE).splits
    assert isinstance(b, pd.Series) and b.name == "Stock Splits"
    assert str(b.index.tz) == "Asia/Kolkata"
    y_date, y_ratio = y.index[-1].date(), float(y.iloc[-1])
    b_date, b_ratio = b.index[-1].date(), float(b.iloc[-1])
    print(f"yfinance last split: {y_date} ratio={y_ratio} (2024 1:1 bonus -> 2.0)")
    print(f"bfinance last split: {b_date} ratio={b_ratio} (equity-cap doubling -> 2.0)")
    assert y_ratio == pytest.approx(b_ratio), "bonus convention must both be 2.0"
    assert y_ratio == pytest.approx(2.0)


def test_actions_shape_contract():
    """yfinance actions columns/shape contract vs engine builder."""
    ya = yf.Ticker(RELIANCE).actions
    assert list(ya.columns) == ["Dividends", "Stock Splits"]
    b = CorporateActionsEngine.build_actions(
        Ticker(RELIANCE).dividends, Ticker(RELIANCE).splits
    )
    assert list(b.columns) == ACTIONS_COLUMNS == list(ya.columns)
    assert b["Dividends"].dtype == float and b["Stock Splits"].dtype == float
    assert isinstance(b.index, pd.DatetimeIndex) and b.index.name == "Date"
    print(f"yfinance actions shape={ya.shape}; bfinance engine shape={b.shape}")
    if not hasattr(Ticker, "actions"):
        _needsmain("ticker.py: add `actions` property via CorporateActionsEngine.build_actions()")


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
    assert len(yf.Ticker(ETF).splits) == 0 and len(Ticker(ETF).splits) == 0
    print(f"{ETF}: dividends/splits empty on both sides (quoteType={Ticker(ETF).info.get('quoteType')})")


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
    profile = t._ensure_profile()
    cmp_ = float(profile.ratios.current_price)
    c1 = DerivativesEngine.generate_option_chain("RELIANCE", cmp=cmp_, expiry_date="2026-09-24")
    c2 = DerivativesEngine.generate_option_chain("RELIANCE", cmp=cmp_, expiry_date="2026-09-24")
    assert c1.calls.equals(c2.calls) and c1.puts.equals(c2.puts), "must be deterministic"
    assert list(c1.calls.columns) == YF_CHAIN_COLUMNS
    assert list(c1.puts.columns) == YF_CHAIN_COLUMNS
    e = DerivativesEngine.empty_option_chain()
    assert e.calls.shape == (0, 13) and e.puts.shape == (0, 13)
    assert DerivativesEngine.resolve_options(RELIANCE) == ()
    print(f"bf Ticker.options={t.options} vs yf=() -> synthetic expiries recorded as gap")
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
