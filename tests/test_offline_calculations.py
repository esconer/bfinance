"""Offline TDD tests for market/ratios.py correctness (no network)."""
import inspect
import math
import pathlib

import pandas as pd

import bfinance.market.ratios as ratios_mod
from bfinance.market.ratios import CustomRatiosCalculator


def _df(rows, cols):
    # rows: dict row -> list aligned to cols
    return pd.DataFrame({c: {r: rows[r][i] for r in rows} for i, c in enumerate(cols)})


def _healthy_frames():
    cols = ["2023", "2024"]
    pnl = _df({
        "Net Profit": [100.0, 200.0],
        "Sales": [1000.0, 1500.0],
        "Expenses": [800.0, 1000.0],
        "Operating Profit": [150.0, 300.0],
        "Other Income": [10.0, 10.0],
        "Interest": [20.0, 20.0],
        "EPS in Rs": [5.0, 10.0],
        "Depreciation": [50.0, 60.0],
    }, cols)
    bs = _df({
        "Total Assets": [1000.0, 1500.0],
        "Fixed Assets": [600.0, 900.0],
        "Other Assets": [400.0, 600.0],
        "Borrowings": [400.0, 300.0],
        "Current Assets": [500.0, 800.0],
        "Current Liabilities": [250.0, 300.0],
        "Equity Capital": [100.0, 100.0],
        "Reserves": [500.0, 800.0],
        "Cash Equivalents": [50.0, 100.0],
    }, cols)
    cf = _df({
        "Cash from Operating Activity": [250.0, 300.0],
        "Cash from Investing Activity": [-100.0, -80.0],
    }, cols)
    return pnl, bs, cf


# 1. _safe_float
def test_safe_float_handles_none_nan_dash():
    fn = getattr(ratios_mod, "_safe_float", None)
    assert callable(fn), "missing module-level _safe_float"
    assert fn(None) == 0.0
    assert fn(float("nan")) == 0.0
    assert fn(float("inf")) == 0.0
    assert fn("—") == 0.0
    assert fn("abc") == 0.0
    assert fn("", default=1.5) == 1.5
    assert fn(None, default=2.5) == 2.5


def test_safe_float_numeric_passthrough():
    fn = getattr(ratios_mod, "_safe_float", None)
    assert callable(fn), "missing _safe_float"
    assert fn(5) == 5.0
    assert fn(3.14) == 3.14
    assert fn("123.45") == 123.45
    assert fn(" 12 ", default=0.0) == 12.0


def test_no_float_or_pattern_remains():
    src = pathlib.Path(ratios_mod.__file__).read_text(encoding="utf-8")
    import re
    assert not re.search(r"float\s*\([^)]*or\s+0\.0", src), "float(x or 0.0) still present (NaN passes through or)"
    assert "or 1.0" not in src, "or 1.0 fabrication still present"


# 2. has / latest_pair
def test_has_present_and_not_nan():
    fn = getattr(ratios_mod, "has", None)
    assert callable(fn), "missing has(df,row,col)"
    df = _df({"A": [1.0, 2.0]}, ["2023", "2024"])
    assert fn(df, "A", "2024") is True
    assert fn(df, "Missing", "2024") is False
    assert fn(df, "A", "2099") is False
    df2 = _df({"A": [1.0, float("nan")]}, ["2023", "2024"])
    assert fn(df2, "A", "2024") is False
    df3 = _df({"A": [1.0, None]}, ["2023", "2024"])
    assert fn(df3, "A", "2024") is False
    assert fn(pd.DataFrame(), "A", "2024") is False


def test_latest_pair_normal_and_ttm():
    fn = getattr(ratios_mod, "latest_pair", None)
    assert callable(fn), "missing latest_pair(df)"
    df = _df({"A": [1, 2]}, ["2022", "2023"])
    assert fn(df) == ("2023", "2022")
    df_ttm = _df({"A": [1, 2, 3]}, ["2022", "2023", "TTM"])
    assert fn(df_ttm) == ("2023", "2022")
    # per-frame: each statement resolves its own pair
    df2 = _df({"A": [1, 2, 3, 4]}, ["2021", "2022", "2023", "TTM"])
    assert fn(df2) == ("2023", "2022")


# 3. piotroski guard
def test_piotroski_requires_all_statements():
    pnl, bs, cf = _healthy_frames()
    # each frame with <2 cols must yield zero-score with notes
    pnl1 = pnl.iloc[:, :1]
    r = CustomRatiosCalculator.calculate_piotroski_score(pnl1, bs, cf)
    assert r["score"] == 0 and r["max_score"] == 9 and r["breakdown"] == {}
    assert isinstance(r.get("notes"), list) and len(r["notes"]) > 0
    bs1 = bs.iloc[:, :1]
    r2 = CustomRatiosCalculator.calculate_piotroski_score(pnl, bs1, cf)
    assert r2["score"] == 0 and r2["breakdown"] == {}
    assert isinstance(r2.get("notes"), list) and len(r2["notes"]) > 0
    cf1 = cf.iloc[:, :1]
    r3 = CustomRatiosCalculator.calculate_piotroski_score(pnl, bs, cf1)
    assert r3["score"] == 0 and r3["breakdown"] == {}
    assert isinstance(r3.get("notes"), list) and len(r3["notes"]) > 0


def test_piotroski_missing_assets_no_fabrication():
    pnl, bs, cf = _healthy_frames()
    bs2 = bs.drop(index=["Total Assets", "Fixed Assets", "Other Assets"], errors="ignore")
    r = CustomRatiosCalculator.calculate_piotroski_score(pnl, bs2, cf)
    assert r["breakdown"]["positive_roa"] is False
    assert r["breakdown"]["higher_asset_turnover"] is False
    assert r["breakdown"]["lower_leverage"] is False
    assert isinstance(r.get("notes"), list) and len(r["notes"]) > 0
    assert any("asset" in str(n).lower() for n in r["notes"])


def test_piotroski_total_assets_fallback():
    pnl, bs, cf = _healthy_frames()
    # drop Total Assets but keep Fixed+Other -> should still compute ROA/turnover
    bs2 = bs.drop(index=["Total Assets"])
    r = CustomRatiosCalculator.calculate_piotroski_score(pnl, bs2, cf)
    # Fixed+Other: 600+400=1000 prev, 900+600=1500 curr -> same as before -> True
    assert r["breakdown"]["positive_roa"] is True
    assert r["breakdown"]["higher_asset_turnover"] is True


def test_piotroski_f2_uses_beginning_assets():
    cols = ["2023", "2024"]
    pnl = _df({"Net Profit": [50.0, 100.0], "Sales": [1000.0, 1500.0],
               "Expenses": [800.0, 1200.0], "Operating Profit": [150.0, 300.0]}, cols)
    # prev assets zero -> beginning-assets basis cannot award F2
    bs = _df({"Total Assets": [0.0, 1000.0], "Borrowings": [100.0, 100.0],
              "Current Assets": [500.0, 800.0], "Current Liabilities": [250.0, 300.0],
              "Equity Capital": [100.0, 100.0]}, cols)
    cf = _df({"Cash from Operating Activity": [10.0, 20.0],
              "Cash from Investing Activity": [-5.0, -5.0]}, cols)
    r = CustomRatiosCalculator.calculate_piotroski_score(pnl, bs, cf)
    assert r["breakdown"]["positive_roa"] is False


def test_piotroski_f5_uses_leverage_ratio():
    cols = ["2023", "2024"]
    pnl = _df({"Net Profit": [10.0, 10.0], "Sales": [500.0, 500.0],
               "Expenses": [400.0, 400.0]}, cols)
    # absolute debt rises 200->300 but leverage falls 0.4->0.15 -> F5 True under ratio logic
    bs = _df({"Total Assets": [500.0, 2000.0], "Borrowings": [200.0, 300.0],
              "Current Assets": [100.0, 100.0], "Current Liabilities": [50.0, 50.0],
              "Equity Capital": [50.0, 50.0]}, cols)
    cf = _df({"Cash from Operating Activity": [5.0, 5.0],
              "Cash from Investing Activity": [0.0, 0.0]}, cols)
    r = CustomRatiosCalculator.calculate_piotroski_score(pnl, bs, cf)
    assert r["breakdown"]["lower_leverage"] is True


def test_piotroski_f5_missing_data_false():
    pnl, bs, cf = _healthy_frames()
    bs2 = bs.drop(index=["Borrowings"])
    r = CustomRatiosCalculator.calculate_piotroski_score(pnl, bs2, cf)
    assert r["breakdown"]["lower_leverage"] is False
    assert len(r.get("notes", [])) > 0


def test_piotroski_f6_uses_current_ratio():
    cols = ["2023", "2024"]
    pnl = _df({"Net Profit": [10.0, 10.0], "Sales": [500.0, 500.0],
               "Expenses": [400.0, 400.0]}, cols)
    bs = _df({
        "Total Assets": [1000.0, 1000.0],
        "Borrowings": [100.0, 100.0],
        "Current Assets": [500.0, 800.0],
        "Current Liabilities": [250.0, 300.0],
        # Other proxy worsens to trap old code: 600/200=3.0 -> 400/200=2.0
        "Other Assets": [600.0, 400.0],
        "Other Liabilities": [200.0, 200.0],
        "Equity Capital": [50.0, 50.0],
    }, cols)
    cf = _df({"Cash from Operating Activity": [5.0, 5.0],
              "Cash from Investing Activity": [0.0, 0.0]}, cols)
    r = CustomRatiosCalculator.calculate_piotroski_score(pnl, bs, cf)
    # current ratio 2.0 -> 2.67 improves => True despite Other proxy worsening
    assert r["breakdown"]["higher_current_ratio"] is True


def test_piotroski_f6_missing_current_false():
    pnl, bs, cf = _healthy_frames()
    bs2 = bs.drop(index=["Current Assets", "Current Liabilities"], errors="ignore")
    r = CustomRatiosCalculator.calculate_piotroski_score(pnl, bs2, cf)
    assert r["breakdown"]["higher_current_ratio"] is False
    assert len(r.get("notes", [])) > 0


def test_piotroski_f7_missing_equity_false():
    pnl, bs, cf = _healthy_frames()
    bs2 = bs.drop(index=["Equity Capital"])
    r = CustomRatiosCalculator.calculate_piotroski_score(pnl, bs2, cf)
    assert r["breakdown"]["no_equity_dilution"] is False
    assert len(r.get("notes", [])) > 0


def test_piotroski_f8_prefers_operating_profit():
    cols = ["2023", "2024"]
    # (Sales-Expenses)/Sales worsens 25%->10% but OP/Sales improves 12.5%->20%
    pnl = _df({"Net Profit": [10.0, 10.0], "Sales": [800.0, 1000.0],
               "Expenses": [600.0, 900.0], "Operating Profit": [100.0, 200.0]}, cols)
    bs = _df({"Total Assets": [1000.0, 1000.0], "Borrowings": [100.0, 100.0],
              "Current Assets": [200.0, 200.0], "Current Liabilities": [100.0, 100.0],
              "Equity Capital": [50.0, 50.0]}, cols)
    cf = _df({"Cash from Operating Activity": [5.0, 5.0],
              "Cash from Investing Activity": [0.0, 0.0]}, cols)
    r = CustomRatiosCalculator.calculate_piotroski_score(pnl, bs, cf)
    assert r["breakdown"]["higher_gross_margin"] is True


def test_piotroski_f8_no_sales_false():
    cols = ["2023", "2024"]
    pnl = _df({"Net Profit": [0.0, 0.0], "Sales": [0.0, 0.0],
               "Expenses": [0.0, 0.0]}, cols)
    bs = _df({"Total Assets": [1000.0, 1000.0], "Borrowings": [100.0, 100.0],
              "Current Assets": [200.0, 200.0], "Current Liabilities": [100.0, 100.0],
              "Equity Capital": [50.0, 50.0]}, cols)
    cf = _df({"Cash from Operating Activity": [0.0, 0.0],
              "Cash from Investing Activity": [0.0, 0.0]}, cols)
    r = CustomRatiosCalculator.calculate_piotroski_score(pnl, bs, cf)
    assert r["breakdown"]["higher_gross_margin"] is False


def test_piotroski_f8_fallback_without_op():
    cols = ["2023", "2024"]
    pnl = _df({"Net Profit": [10.0, 10.0], "Sales": [1000.0, 2000.0],
               "Expenses": [900.0, 1500.0]}, cols)  # 10% -> 25% improves
    bs = _df({"Total Assets": [1000.0, 1000.0], "Borrowings": [100.0, 100.0],
              "Current Assets": [200.0, 200.0], "Current Liabilities": [100.0, 100.0],
              "Equity Capital": [50.0, 50.0]}, cols)
    cf = _df({"Cash from Operating Activity": [5.0, 5.0],
              "Cash from Investing Activity": [0.0, 0.0]}, cols)
    r = CustomRatiosCalculator.calculate_piotroski_score(pnl, bs, cf)
    assert r["breakdown"]["higher_gross_margin"] is True


def test_piotroski_shape_and_notes():
    pnl, bs, cf = _healthy_frames()
    r = CustomRatiosCalculator.calculate_piotroski_score(pnl, bs, cf)
    assert r["max_score"] == 9
    assert isinstance(r.get("notes"), list)
    bd = r["breakdown"]
    for k in ["positive_net_income", "positive_roa", "positive_cfo", "cfo_exceeds_pat",
              "lower_leverage", "higher_current_ratio", "no_equity_dilution",
              "higher_gross_margin", "higher_asset_turnover"]:
        assert k in bd and isinstance(bd[k], bool)
    assert "notes" in bd and isinstance(bd["notes"], list)
    assert r["score"] == 9


# 4. custom ratios
def test_ev_subtracts_cash():
    pnl, bs, cf = _healthy_frames()
    out = CustomRatiosCalculator.calculate_all_custom_ratios(
        1000.0, 100.0, 10.0, 50.0, 10.0, pnl, bs, cf)
    # mcap 1000 + debt 300 - cash 100 = 1200
    assert out["enterprise_value_cr"] == 1200.0


def test_ev_cash_bank_balance_fallback_and_missing():
    pnl, bs, cf = _healthy_frames()
    bs2 = bs.drop(index=["Cash Equivalents"])
    bs2.loc["Cash and Bank Balance"] = [20.0, 40.0]
    out = CustomRatiosCalculator.calculate_all_custom_ratios(
        1000.0, 100.0, 10.0, 50.0, 10.0, pnl, bs2, cf)
    assert out["enterprise_value_cr"] == 1000.0 + 300.0 - 40.0
    bs3 = bs.drop(index=["Cash Equivalents"])
    out3 = CustomRatiosCalculator.calculate_all_custom_ratios(
        1000.0, 100.0, 10.0, 50.0, 10.0, pnl, bs3, cf)
    assert out3["enterprise_value_cr"] == 1300.0


def test_ebitda_adds_depreciation():
    pnl, bs, cf = _healthy_frames()
    out = CustomRatiosCalculator.calculate_all_custom_ratios(
        1000.0, 100.0, 10.0, 50.0, 10.0, pnl, bs, cf)
    # EV 1200, EBITDA 300+60=360
    assert out["ev_to_ebitda"] == round(1200.0 / 360.0, 2)


def test_interest_coverage_includes_other_income():
    pnl, bs, cf = _healthy_frames()
    # OP 300 + Other 10 = 310 / 20 = 15.5
    out = CustomRatiosCalculator.calculate_all_custom_ratios(
        1000.0, 100.0, 10.0, 50.0, 10.0, pnl, bs, cf)
    assert out["interest_coverage"] == round(310.0 / 20.0, 2)
    # omit when interest <= 0
    pnl2 = pnl.copy()
    pnl2.loc["Interest"] = [0.0, 0.0]
    out2 = CustomRatiosCalculator.calculate_all_custom_ratios(
        1000.0, 100.0, 10.0, 50.0, 10.0, pnl2, bs, cf)
    assert "interest_coverage" not in out2


def test_fcf_ignores_positive_cfi():
    pnl, bs, _cf = _healthy_frames()
    cf2 = _df({"Cash from Operating Activity": [300.0, 300.0],
               "Cash from Investing Activity": [-50.0, 100.0]}, ["2023", "2024"])
    out = CustomRatiosCalculator.calculate_all_custom_ratios(
        1000.0, 100.0, 10.0, 50.0, 10.0, pnl, bs, cf2)
    assert out["free_cash_flow_cr"] == 300.0
    cf3 = _df({"Cash from Operating Activity": [300.0, 300.0],
               "Cash from Investing Activity": [-50.0, -100.0]}, ["2023", "2024"])
    out3 = CustomRatiosCalculator.calculate_all_custom_ratios(
        1000.0, 100.0, 10.0, 50.0, 10.0, pnl, bs, cf3)
    assert out3["free_cash_flow_cr"] == 200.0


def test_debt_equity_and_cfo_pat_nan_safe():
    pnl, bs, cf = _healthy_frames()
    bs2 = bs.copy()
    bs2.loc["Borrowings", "2024"] = float("nan")
    out = CustomRatiosCalculator.calculate_all_custom_ratios(
        1000.0, 100.0, 10.0, 50.0, 10.0, pnl, bs2, cf)
    v = out.get("debt_to_equity")
    assert v is None or (isinstance(v, float) and not math.isnan(v))
    cf2 = cf.astype(object)
    cf2.loc["Cash from Operating Activity", "2024"] = "—"
    out2 = CustomRatiosCalculator.calculate_all_custom_ratios(
        1000.0, 100.0, 10.0, 50.0, 10.0, pnl, bs, cf2)
    v2 = out2.get("cfo_to_pat")
    assert v2 is None or (isinstance(v2, float) and not math.isnan(v2))


def test_graham_eps_fallback_safe():
    pnl, bs, cf = _healthy_frames()
    pnl2 = pnl.astype(object)
    pnl2.loc["EPS in Rs", "2024"] = "—"
    out = CustomRatiosCalculator.calculate_all_custom_ratios(
        None, 100.0, 10.0, 50.0, None, pnl2, bs, cf)
    # fallback EPS = 100/10 = 10 -> Graham = sqrt(22.5*10*50)
    import math as _m
    assert out["graham_number"] == _m.sqrt(22.5 * 10.0 * 50.0)
    assert not (isinstance(out["graham_number"], float) and _m.isnan(out["graham_number"]))


def test_signatures_and_keys_unchanged():
    assert [p.name for p in inspect.signature(
        CustomRatiosCalculator.calculate_piotroski_score).parameters.values()] == ["pnl", "bs", "cf"]
    assert [p.name for p in inspect.signature(
        CustomRatiosCalculator.calculate_all_custom_ratios).parameters.values()] == [
        "market_cap_cr", "current_price", "trailing_pe", "book_value", "eps", "pnl", "bs", "cf"]
    pnl, bs, cf = _healthy_frames()
    out = CustomRatiosCalculator.calculate_all_custom_ratios(
        1000.0, 100.0, 10.0, 50.0, 10.0, pnl, bs, cf)
    for k in ["piotroski_score", "piotroski_breakdown", "graham_number",
              "enterprise_value_cr", "ev_to_ebitda", "interest_coverage",
              "debt_to_equity", "cfo_to_pat", "free_cash_flow_cr"]:
        assert k in out, f"missing key {k}"
