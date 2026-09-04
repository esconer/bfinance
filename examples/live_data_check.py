"""Live data proof: exercise every bfinance surface against real tickers.

Usage:  uv run --extra dev python examples/live_data_check.py [TICKER]
Default ticker: RELIANCE.NS. Requires network (screener.in / yfinance fallback).
Prints a PASS/FAIL evidence line per surface with shapes + sample values.
"""

import asyncio
import inspect
import sys
import traceback

TICKER = sys.argv[1] if len(sys.argv) > 1 else "RELIANCE.NS"

import bfinance as bf

PASS, FAIL = "PASS", "FAIL"
results = []


async def maybe(awaitable_or_value):
    if inspect.isawaitable(awaitable_or_value):
        return await awaitable_or_value
    return awaitable_or_value


def show(name, fn):
    try:
        out = fn()
        if inspect.iscoroutine(out):
            out = asyncio.get_event_loop().run_until_complete(out)
        print(f"[{PASS}] {name}: {out}", flush=True)
        results.append(True)
    except Exception as e:  # noqa: BLE001 — probe must never die early
        print(f"[{FAIL}] {name}: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc(limit=2)
        results.append(False)


def main():
    print(f"bfinance {bf.__version__} — live check for {TICKER}\n", flush=True)
    t = bf.Ticker(TICKER)

    show("history 1mo", lambda: _hist(t, "1mo"))
    show("history 1y", lambda: _hist(t, "1y"))
    show("info keys", lambda: _info(t))
    show("fast_info", lambda: _fast(t))
    show("income annual", lambda: _stmt(t.get_income_stmt(), "Sales"))
    show("balance annual", lambda: _stmt(t.get_balance_sheet(), "Borrowings"))
    show("cashflow annual", lambda: _stmt(t.get_cash_flow(), "Cash from Operating Activity"))
    show("income quarterly", lambda: _stmt(t.get_income_stmt(freq="quarterly"), "Sales"))
    show("dividends/splits", lambda: _actions(t))
    show("custom_ratios", lambda: _ratios(t))
    show("profile/sector", lambda: _profile(t))
    show("shareholding", lambda: _sh(t))
    show("concalls", lambda: _concalls(t))
    show("analyst/calendar/holders", lambda: _misc(t))
    show("download 2 tickers", lambda: _dl())
    show("screener search", lambda: _search())
    show("screen run (tiny)", lambda: _screen())
    show("ai dossier", lambda: _dossier(t))

    n_ok = sum(results)
    print(f"\n{n_ok}/{len(results)} surfaces returned live data", flush=True)
    sys.exit(0 if n_ok == len(results) else 1)


def _hist(t, period):
    df = t.history(period=period)
    last = df["Close"].iloc[-1]
    return f"rows={len(df)} cols={list(df.columns)[:6]} last_close={last:.2f} tz={df.index.tz}"


def _info(t):
    info = t.info
    return f"keys={len(info)} price={info.get('currentPrice')} mcap_cr={info.get('marketCapInCr')} pe={info.get('trailingPE')}"


def _fast(t):
    f = t.fast_info
    return f"exchange={f.exchange} prev_close={f.previous_close} ma50={f.fifty_day_average} ma200={f.two_hundred_day_average} mcap={f.market_cap}"


def _stmt(df, probe_row):
    rows = list(df.index)
    return f"shape={df.shape} has[{probe_row}]={probe_row in df.index} e.g_rows={rows[:3]}"


def _actions(t):
    d, s = t.dividends, t.splits
    return f"dividends={len(d)} splits={len(s)} last_div={d.iloc[-1] if len(d) else None}"


def _ratios(t):
    r = t.custom_ratios
    p = r.get("piotroski_score")
    return f"piotroski={p} graham={r.get('graham_number')} ev_cr={r.get('enterprise_value_cr')} keys={len(r)}"


def _profile(t):
    p = t._ensure_profile()
    return f"sector={t.sector} industry={t.industry} name={p.name}"


def _sh(t):
    df = t.get_shareholding() if hasattr(t, "get_shareholding") else t.shareholding
    return f"shape={df.shape if hasattr(df, 'shape') else type(df)}"


def _concalls(t):
    cc = t.concalls if not callable(getattr(t, "concalls", None)) else t.get_concalls()
    n = len(cc) if hasattr(cc, "__len__") else "?"
    first = cc[0] if n != "?" and n else None
    return f"count={n} first={str(first)[:100]}"


def _misc(t):
    at = t.analyst_price_targets
    cal = t.calendar
    mh = t.major_holders
    return f"targets={at} calendar_keys={list(cal)[:3] if isinstance(cal, dict) else type(cal)} holders_rows={len(mh) if hasattr(mh, '__len__') else '?'}"


def _dl():
    df = bf.download(f"{TICKER},TCS.NS", period="5d")
    return f"shape={df.shape} cols_top={list(df.columns.get_level_values(0).unique()[:4]) if hasattr(df.columns, 'get_level_values') else list(df.columns)[:4]}"


def _search():
    from bfinance.screener.client import ScreenerClient

    async def go():
        c = ScreenerClient()
        return await c.search("reliance")

    res = asyncio.run(go())
    return f"hits={len(res)} first={str(res[0])[:120] if res else None}"


def _screen():
    from bfinance.screens import screens

    screen = screens.undervalued_growth
    out = screen.run(universe=["RELIANCE", "TCS", "INFY"])
    return f"screen={screen.name} rows={len(out)} cols={list(out.columns)[:5]}"


def _dossier(t):
    d = t.to_ai_context(format="markdown")
    return f"chars={len(d)}"


if __name__ == "__main__":
    main()
