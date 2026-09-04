"""Parity: yfinance .info/.fast_info vs bfinance for RELIANCE.NS.

OFFLINE: exchange mapping, taxonomy->sector/industry mapping, MA on synthetic series.
LIVE (@pytest.mark.live): key-coverage yfinance vs bfinance, no fabricated constants,
sector/industry present, fiftyDayAverage/twoHundredDayAverage/previousClose real-or-None.
"""

import pytest

from bfinance.market.fast_info import FastInfo
from bfinance.market.quotes import QuoteEngine
from bfinance.models.company import CompanyProfile, TopRatios


def _profile(symbol="RELIANCE.NS", **kw):
    base = {
        "symbol": symbol,
        "company_id": 1,
        "name": "Reliance Industries Ltd",
        "sector": "Energy",
        "industry_group": "Oil, Gas & Consumable Fuels",
        "industry": "Petroleum Products",
        "sub_industry": "Refineries & Marketing",
        "ratios": TopRatios(market_cap=1762546.0, current_price=1302.0),
    }
    base.update(kw)
    return CompanyProfile(**base)


# ---------------- OFFLINE ----------------

def test_exchange_mapping_ns_bo_bare():
    from bfinance.market.quotes import resolve_exchange

    assert resolve_exchange("RELIANCE.NS") == "NSE"
    assert resolve_exchange("RELIANCE.BO") == "BSE"
    assert resolve_exchange("RELIANCE") == "NSE"


def test_quote_engine_exchange_mapping():
    assert QuoteEngine.build_info_dict(_profile("RELIANCE.NS"))["exchange"] == "NSE"
    assert QuoteEngine.build_info_dict(_profile("500112.BO"))["exchange"] == "BSE"
    assert QuoteEngine.build_info_dict(_profile("RELIANCE"))["exchange"] == "NSE"


def test_fast_info_exchange_mapping():
    assert FastInfo(_profile("RELIANCE.NS")).exchange == "NSE"
    assert FastInfo(_profile("RELIANCE.BO")).exchange == "BSE"
    assert FastInfo(_profile("RELIANCE")).exchange == "NSE"


def test_taxonomy_to_sector_industry_mapping():
    from bfinance.market.quotes import map_sector_industry

    out = map_sector_industry(
        sector="Energy",
        industry_group="Oil, Gas & Consumable Fuels",
        industry="Petroleum Products",
        sub_industry="Refineries & Marketing",
    )
    assert out["sector"] == "Energy"
    assert out["industry"] in (
        "Refineries & Marketing",
        "Petroleum Products",
        "Oil, Gas & Consumable Fuels",
    )
    # empty taxonomy -> None, never fabricated
    empty = map_sector_industry(sector=None, industry_group=None, industry=None, sub_industry=None)
    assert empty["sector"] is None
    assert empty["industry"] is None


def test_moving_average_synthetic_series():
    from bfinance.market.quotes import moving_average, previous_close_from_history

    closes = [float(x) for x in range(1, 251)]  # 1..250
    assert moving_average(closes, 50) == pytest.approx(sum(range(201, 251)) / 50)
    assert moving_average(closes, 200) == pytest.approx(sum(range(51, 251)) / 200)
    assert moving_average(closes[:10], 50) is None  # insufficient -> None
    assert previous_close_from_history([10.0, 11.0, 12.0]) == pytest.approx(11.0)
    assert previous_close_from_history([10.0]) is None or previous_close_from_history([10.0]) == pytest.approx(10.0)
    assert previous_close_from_history([]) is None


def test_quote_engine_history_derived_averages_synthetic():
    import pandas as pd

    closes = [100.0 + i for i in range(250)]
    idx = pd.date_range("2024-01-01", periods=250, freq="D")
    hist = pd.DataFrame({"Close": closes}, index=idx)
    info = QuoteEngine.build_info_dict(_profile("RELIANCE.NS"), history=hist)
    assert info["fiftyDayAverage"] == pytest.approx(sum(closes[-50:]) / 50)
    assert info["twoHundredDayAverage"] == pytest.approx(sum(closes[-200:]) / 200)
    assert info["previousClose"] == pytest.approx(closes[-2])
    assert info["regularMarketPreviousClose"] == pytest.approx(closes[-2])
    # intraday unavailable -> must stay None
    assert info["regularMarketDayHigh"] is None
    assert info["regularMarketDayLow"] is None


def test_fast_info_history_derived_averages_synthetic():
    closes = [100.0 + i for i in range(250)]
    fi = FastInfo(_profile("RELIANCE.NS"), latest_price=closes[-1], history=closes)
    assert fi.fifty_day_average == pytest.approx(sum(closes[-50:]) / 50)
    assert fi.two_hundred_day_average == pytest.approx(sum(closes[-200:]) / 200)
    assert fi.previous_close == pytest.approx(closes[-2])


def test_quote_engine_no_fabricated_ohlc_offline():
    info = QuoteEngine.build_info_dict(_profile("RELIANCE.NS"))
    cmp_ = 1302.0
    # exact cmp*factor fabrications must be gone; intraday stays None
    assert info["regularMarketDayHigh"] is None
    assert info["regularMarketDayLow"] is None
    assert info["regularMarketOpen"] is None
    for k in ("regularMarketDayHigh", "regularMarketDayLow", "regularMarketOpen"):
        if info[k] is not None:
            assert abs(info[k] / cmp_ - 1.0) > 0.001
    # forwardPE honest: None when no source
    assert info["forwardPE"] is None
    assert info.get("returnOnAssets") != 0.08


# ---------------- LIVE ----------------

@pytest.mark.live
def test_live_key_coverage_reliance():
    import yfinance as yf

    yf_info = yf.Ticker("RELIANCE.NS").info
    from bfinance import Ticker

    bt = Ticker("RELIANCE.NS")
    bf_info = bt.info
    bf_fi = bt.fast_info.to_dict()
    print(f"\nyfinance keys={len(yf_info)} bfinance info keys={len(bf_info)}")
    print(f"fast_info keys={sorted(bf_fi)}")
    overlap = set(yf_info) & set(bf_info)
    print(f"overlap={len(overlap)} yf_only={len(set(yf_info) - set(bf_info))}")
    # sector/industry present if profile taxonomy exists
    prof = bt._ensure_profile()
    if prof.sector:
        assert bf_info.get("sector"), "sector missing despite taxonomy"
    if prof.industry or prof.sub_industry or prof.industry_group:
        assert bf_info.get("industry"), "industry missing despite taxonomy"


@pytest.mark.live
def test_live_no_fabricated_constants():
    import yfinance as yf

    from bfinance import Ticker

    bt = Ticker("RELIANCE.NS")
    info = bt.info
    yf_info = yf.Ticker("RELIANCE.NS").info
    # ROA: never hardcoded 0.08 unless genuinely 8%
    roa = info.get("returnOnAssets")
    assert roa is None or abs(roa - 0.08) > 1e-9 or yf_info.get("returnOnAssets") == pytest.approx(0.08, abs=1e-4)
    # forwardPE: None-or-real positive
    fpe = info.get("forwardPE")
    assert fpe is None or (isinstance(fpe, (int, float)) and fpe > 0)
    # intraday DayHigh/Low honestly None
    assert info.get("regularMarketDayHigh") is None
    assert info.get("regularMarketDayLow") is None


@pytest.mark.live
def test_live_averages_real_or_none():
    from bfinance import Ticker

    bt = Ticker("RELIANCE.NS")
    hist = bt.history(period="1y")
    closes = hist["Close"].dropna().tolist()
    assert len(closes) > 200, f"need 200+ closes, got {len(closes)}"
    exp50 = sum(closes[-50:]) / 50
    exp200 = sum(closes[-200:]) / 200
    exp_prev = closes[-2]
    info = QuoteEngine.build_info_dict(bt._ensure_profile(), history=hist)
    for k, exp in (("fiftyDayAverage", exp50), ("twoHundredDayAverage", exp200), ("previousClose", exp_prev)):
        v = info.get(k)
        assert v is None or v == pytest.approx(exp, rel=1e-6), f"{k}={v} expected {exp}"
    assert info["fiftyDayAverage"] == pytest.approx(exp50, rel=1e-6)
    assert info["twoHundredDayAverage"] == pytest.approx(exp200, rel=1e-6)
    assert info["previousClose"] == pytest.approx(exp_prev, rel=1e-6)
    fi = FastInfo(bt._ensure_profile(), history=hist)
    assert fi.fifty_day_average == pytest.approx(exp50, rel=1e-6)
    assert fi.two_hundred_day_average == pytest.approx(exp200, rel=1e-6)
    assert fi.previous_close == pytest.approx(exp_prev, rel=1e-6)
