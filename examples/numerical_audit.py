"""
Comprehensive Numerical Data Extraction Test across Indian Bluechips.
Extracts and prints exact numerical datasets from bfinance.
"""

import sys
sys.stdout.reconfigure(encoding='utf-8')
import pandas as pd
import bfinance as bf

pd.set_option('display.max_columns', 15)
pd.set_option('display.width', 1200)
pd.set_option('display.float_format', lambda x: f'{x:,.2f}' if isinstance(x, (int, float)) else str(x))

def run_numerical_test(symbol: str):
    stock = bf.Ticker(symbol)
    info = stock.info
    fi = stock.fast_info

    print("=" * 90)
    print(f"NUMERICAL DATA FOR: {info.get('shortName', symbol)} ({symbol})")
    print("=" * 90)

    # 1. Core Valuation & Quality Numbers
    print("\n[1] CORE VALUATION & QUALITY METRICS:")
    metrics = {
        "Current Market Price (CMP)": f"₹{fi.last_price:,.2f}",
        "Market Capitalization": f"₹{info.get('marketCapInCr', 0):,.2f} Cr (₹{fi.market_cap:,.0f})",
        "52-Week High / Low": f"₹{fi.year_high:,.2f} / ₹{fi.year_low:,.2f}",
        "Trailing P/E Ratio": f"{info.get('trailingPE', 0):.2f}",
        "Book Value per Share": f"₹{info.get('bookValue', 0):,.2f}",
        "Dividend Yield": f"{(info.get('dividendYield', 0) * 100):.2f}%",
        "ROCE (Return on Capital Employed)": f"{info.get('returnOnCapitalEmployed', 0):.2f}%",
        "ROE (Return on Equity)": f"{(info.get('returnOnEquity', 0) * 100):.2f}%",
        "Sector": f"{stock.sector} -> {stock.industry}",
        "Key Benchmark Indices": ", ".join(stock.indices[:4]),
    }
    for k, v in metrics.items():
        print(f"  • {k:<36}: {v}")

    # 2. 10-Year Annual Statements Numbers
    print("\n[2] 10-YEAR ANNUAL INCOME STATEMENT (Values in ₹ Crores):")
    pnl = stock.income_stmt
    display_rows = ["Sales", "Expenses", "Operating Profit", "OPM %", "Other Income", "Interest", "Depreciation", "Profit before tax", "Net Profit", "EPS in Rs"]
    filtered_rows = [r for r in display_rows if r in pnl.index]
    print(pnl.loc[filtered_rows].iloc[:, -6:])

    # 3. 12-Quarters Financial Numbers
    print("\n[3] 12-QUARTERS FINANCIAL PERFORMANCE (Values in ₹ Crores):")
    q_pnl = stock.quarterly_income_stmt
    q_rows = [r for r in ["Sales", "Operating Profit", "OPM %", "Net Profit", "EPS in Rs"] if r in q_pnl.index]
    print(q_pnl.loc[q_rows].iloc[:, -6:])

    # 4. Institutional Shareholding Trend Numbers (Quarterly & 11-Year Annual)
    print("\n[4A] QUARTERLY INSTITUTIONAL SHAREHOLDING (%):")
    print(stock.shareholding.iloc[:, -6:])

    print("\n[4B] 11-YEAR ANNUAL SHAREHOLDING TREND (%):")
    print(stock.shareholding_yearly.iloc[:, -6:])

    # 5. 10-Year Historical Corporate Ratios
    print("\n[5] 10-YEAR HISTORICAL OPERATING RATIOS:")
    print(stock.ratios_history.iloc[:, -6:])

    # 6. Compounded CAGRs
    print("\n[6] COMPOUNDED ANNUAL GROWTH RATES (CAGR %):")
    cagr_data = []
    for title, values in stock.cagrs.items():
        row = {"Metric": title}
        row.update(values)
        cagr_data.append(row)
    if cagr_data:
        print(pd.DataFrame(cagr_data).to_string(index=False))

    # 7. Multi-Year Historical Valuation Multiples (PE vs Median PE)
    print("\n[7] HISTORICAL VALUATION MULTIPLES (Last 5 Trading Days):")
    df_pe = stock.valuation_history(metric="pe", days=365)
    print(df_pe.tail(5))

    # 8. Sector Peer Group Numerical Comparison
    print("\n[8] LIVE SECTOR PEER GROUP NUMERICAL MATRIX:")
    peers = stock.peers
    if not peers.empty:
        peer_cols = [c for c in ["rank", "name", "cmp", "pe", "market_cap_cr", "roce", "dividend_yield"] if c in peers.columns]
        print(peers[peer_cols].head(5).to_string(index=False))

    # 9. Derivatives Option Chain Numbers
    print("\n[9] DERIVATIVES NSE OPTION CHAIN SAMPLE (Strikes, Premiums, OI, IV):")
    expiries = stock.options
    if expiries:
        chain = stock.option_chain(expiries[0])
        print(f"  Expiry: {expiries[0]}")
        print("  CALLS:")
        print(chain.calls[["contractSymbol", "strike", "lastPrice", "openInterest", "impliedVolatility"]].head(3).to_string(index=False))
        print("  PUTS:")
        print(chain.puts[["contractSymbol", "strike", "lastPrice", "openInterest", "impliedVolatility"]].head(3).to_string(index=False))


# Run numerical extraction on RELIANCE and TCS
run_numerical_test("RELIANCE")
print("\n" + "#" * 90 + "\n")
run_numerical_test("TCS")
