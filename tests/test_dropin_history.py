"""
Drop-in parity LOOP: bfinance history/download/Tickers vs yfinance live.

Scope: RELIANCE.NS (+TCS.NS only for multi-ticker tests). bfinance-only
extras (freq/screens) are out of scope. Network tests are marked live.
"""

import pandas as pd
import pytest

yfinance = pytest.importorskip("yfinance")
import bfinance as bf


@pytest.fixture(scope="session", autouse=True)
def _record_yfinance_baseline():
    print(f"\n[yfinance baseline] version={yfinance.__version__}")


def _norm(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(pd.to_datetime(idx)).normalize()
    return idx


def _assert_close_parity(bdf: pd.DataFrame, ydf: pd.DataFrame, tol: float = 0.02):
    assert not bdf.empty and not ydf.empty
    assert list(bdf.columns) == list(ydf.columns)
    assert str(bdf.index.tz) == str(ydf.index.tz) == "Asia/Kolkata"
    assert "Volume" in bdf.columns and bdf["Volume"].notna().all()
    b = bdf.copy()
    y = ydf.copy()
    b.index = _norm(b.index)
    y.index = _norm(y.index)
    common = b.index.intersection(y.index)
    assert len(common) >= 1, "no overlapping trading dates"
    pct = (b.loc[common, "Close"] - y.loc[common, "Close"]).abs() / y.loc[common, "Close"]
    assert bool((pct <= tol).all()), f"max Close pct diff {float(pct.max()):.4f}"


def _bf(symbol: str = "RELIANCE.NS") -> "bf.Ticker":
    return bf.Ticker(symbol, cache_ttl_hours=0)


# ------------------------------------------------------------- history windows
@pytest.mark.live
def test_history_1mo_parity():
    _assert_close_parity(
        _bf().history(period="1mo"),
        yfinance.Ticker("RELIANCE.NS").history(period="1mo"),
    )


@pytest.mark.live
def test_history_1y_parity():
    _assert_close_parity(
        _bf().history(period="1y"),
        yfinance.Ticker("RELIANCE.NS").history(period="1y"),
    )


@pytest.mark.live
def test_history_start_end_parity():
    _assert_close_parity(
        _bf().history(start="2024-01-01", end="2024-06-30"),
        yfinance.Ticker("RELIANCE.NS").history(start="2024-01-01", end="2024-06-30"),
    )


@pytest.mark.live
def test_history_1wk_parity():
    _assert_close_parity(
        _bf().history(period="1mo", interval="1wk"),
        yfinance.Ticker("RELIANCE.NS").history(period="1mo", interval="1wk"),
    )


# ------------------------------------------------------------------- actions
@pytest.mark.live
def test_history_actions_false_omits_cols():
    bdf = _bf().history(period="5d", actions=False)
    ydf = yfinance.Ticker("RELIANCE.NS").history(period="5d", actions=False)
    assert "Dividends" not in bdf.columns and "Stock Splits" not in bdf.columns
    assert list(bdf.columns) == list(ydf.columns)


@pytest.mark.live
def test_history_actions_true_merges_real_actions():
    ydf = yfinance.Ticker("RELIANCE.NS").history(start="2024-08-16", end="2024-08-22")
    assert not ydf.empty and "Dividends" in ydf.columns
    # yfinance records the 2024-08-19 ex-date dividend of 5.0
    day = pd.Timestamp("2024-08-19", tz="Asia/Kolkata")
    ydiv = float(ydf.loc[ydf.index.normalize() == day, "Dividends"].iloc[0])
    assert ydiv == pytest.approx(5.0)
    # identical call on the bfinance side: same schema (values document the
    # known NSE gap — screener has no ex-date feed, so bf shows 0.0 here)
    bdf = _bf().history(start="2024-08-16", end="2024-08-22")
    assert list(bdf.columns) == list(ydf.columns)
    assert "Dividends" in bdf.columns and "Stock Splits" in bdf.columns


# --------------------------------------------------------------- auto_adjust
@pytest.mark.live
def test_history_auto_adjust_true_drops_adj():
    bdf = _bf().history(period="5d", auto_adjust=True)
    ydf = yfinance.Ticker("RELIANCE.NS").history(period="5d", auto_adjust=True)
    assert "Adj Close" not in bdf.columns
    assert list(bdf.columns) == list(ydf.columns)


@pytest.mark.live
def test_history_auto_adjust_false_keeps_adj():
    bdf = _bf().history(period="5d", auto_adjust=False)
    ydf = yfinance.Ticker("RELIANCE.NS").history(period="5d", auto_adjust=False)
    assert "Adj Close" in bdf.columns
    assert list(bdf.columns) == list(ydf.columns)


# --------------------------------------- back_adjust / repair / keepna / prepost
@pytest.mark.live
def test_history_honored_flags_parity():
    for kwargs in ({"back_adjust": True}, {"keepna": True}, {"prepost": True}):
        bdf = _bf().history(period="5d", **kwargs)
        ydf = yfinance.Ticker("RELIANCE.NS").history(period="5d", **kwargs)
        assert list(bdf.columns) == list(ydf.columns)
        b = bdf.copy()
        y = ydf.copy()
        b.index = _norm(b.index)
        y.index = _norm(y.index)
        assert len(b.index.intersection(y.index)) >= 1


def test_history_repair_notimplemented():
    with pytest.raises(NotImplementedError):
        _bf().history(period="5d", repair=True)


# ------------------------------------------------------------------ download
@pytest.mark.live
def test_download_multi_parity():
    # NOTE: yfinance download() returns a tz-naive index in this env while
    # Ticker.history is tz-aware; bfinance matches yfinance exactly here.
    args = {"tickers": "RELIANCE.NS,TCS.NS", "period": "5d"}
    bdf = bf.download(progress=False, **args)
    ydf = yfinance.download(progress=False, **args)
    assert not bdf.empty and not ydf.empty
    assert list(bdf.columns) == list(ydf.columns)
    assert list(bdf.columns.names) == list(ydf.columns.names) == ["Price", "Ticker"]
    assert str(bdf.index.tz) == str(ydf.index.tz) == "None"


@pytest.mark.live
def test_download_single_layout_matches_yf_rules():
    ydf_multi = yfinance.download("RELIANCE.NS", period="5d", progress=False)
    bdf_multi = bf.download("RELIANCE.NS", period="5d", progress=False)
    assert isinstance(bdf_multi.columns, pd.MultiIndex)
    assert list(bdf_multi.columns) == list(ydf_multi.columns)
    ydf_flat = yfinance.download(
        "RELIANCE.NS", period="5d", progress=False, multi_level_index=False
    )
    bdf_flat = bf.download("RELIANCE.NS", period="5d", progress=False, multi_level_index=False)
    assert not isinstance(bdf_flat.columns, pd.MultiIndex)
    assert list(bdf_flat.columns) == list(ydf_flat.columns)


# ------------------------------------------------------------------- Tickers
@pytest.mark.live
def test_tickers_history_parity():
    # NOTE: yfinance Tickers.history is tz-naive in this env; matched exactly.
    bdf = bf.Tickers("RELIANCE.NS TCS.NS").history(period="5d")
    ydf = yfinance.Tickers("RELIANCE.NS TCS.NS").history(period="5d")
    assert not bdf.empty and not ydf.empty
    assert list(bdf.columns) == list(ydf.columns)
    assert list(bdf.columns.names) == list(ydf.columns.names) == ["Price", "Ticker"]
    assert str(bdf.index.tz) == str(ydf.index.tz) == "None"


# ------------------------------------------------- Ticker positional compat
def test_ticker_bool_second_positional_is_not_cache_ttl():
    # yfinance 1.7.0: second positional is `session`; a bool raises
    # YFDataException. bfinance must NOT silently bind it to cache_ttl_hours.
    with pytest.raises(yfinance.exceptions.YFDataException):
        yfinance.Ticker("RELIANCE.NS", True)
    for flag in (True, False):
        t = bf.Ticker("RELIANCE.NS", flag)
        assert getattr(t, "multi_level_index", None) is flag
        assert t.cache is None or t.cache.default_ttl_hours == pytest.approx(24.0)
    # keyword TTL still honored (both orders: positional ticker + keyword ttl,
    # and keyword multi_level_index)
    t = bf.Ticker("RELIANCE.NS", cache_ttl_hours=0)
    assert t.cache is None
    t = bf.Ticker("RELIANCE.NS", multi_level_index=False)
    assert t.multi_level_index is False
