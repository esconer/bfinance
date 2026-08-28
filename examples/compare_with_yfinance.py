"""
Direct Live Side-by-Side Comparison between yfinance 1.7.0 and bfinance.
Fetches real data from both tools simultaneously and prints direct value comparisons.
"""

import sys
sys.stdout.reconfigure(encoding='utf-8')
import pandas as pd
import yfinance as yf
import bfinance as bf

pd.set_option('display.max_columns', 10)
pd.set_option('display.width', 1000)

print("=" * 80)
print("LIVE SIDE-BY-SIDE COMPARISON: yfinance 1.7.0 vs bfinance")
print("=" * 80)

for symbol_yf, symbol_bf in [("RELIANCE.NS", "RELIANCE"), ("TCS.NS", "TCS"), ("INFY.NS", "INFY")]:
    print(f"\n{'#' * 30} {symbol_bf} {'#' * 30}")
    
    # 1. Initialize tickers
    yf_t = yf.Ticker(symbol_yf)
    bf_t = bf.Ticker(symbol_bf)

    # ------------------------------------------------------------- FastInfo
    print("\n--- 1. FAST_INFO COMPARISON ---")
    try:
        yf_price = yf_t.fast_info.last_price
        yf_mcap = yf_t.fast_info.market_cap
        yf_52h = yf_t.fast_info.year_high
        yf_52l = yf_t.fast_info.year_low
    except Exception as e:
        yf_price, yf_mcap, yf_52h, yf_52l = f"Error: {e}", "N/A", "N/A", "N/A"

    bf_fi = bf_t.fast_info
    bf_price = bf_fi.last_price
    bf_mcap = bf_fi.market_cap
    bf_52h = bf_fi.year_high
    bf_52l = bf_fi.year_low

    comparison_fast_info = pd.DataFrame({
        "Metric": ["Last Price (₹)", "Market Cap (₹)", "52W High (₹)", "52W Low (₹)"],
        "yfinance": [yf_price, f"{yf_mcap:,.0f}" if isinstance(yf_mcap, (int, float)) else yf_mcap, yf_52h, yf_52l],
        "bfinance": [bf_price, f"{bf_mcap:,.0f}" if isinstance(bf_mcap, (int, float)) else bf_mcap, bf_52h, bf_52l],
    })
    print(comparison_fast_info.to_string(index=False))

    # ------------------------------------------------------------- Info Dict
    print("\n--- 2. INFO DICT CORE VALUES ---")
    try:
        yf_info = yf_t.info
    except Exception:
        yf_info = {}
    bf_info = bf_t.info

    keys_to_compare = [
        ("shortName", "Company Name"),
        ("currentPrice", "Current Price"),
        ("trailingPE", "Trailing P/E"),
        ("marketCap", "Market Cap"),
        ("bookValue", "Book Value"),
        ("dividendYield", "Dividend Yield"),
    ]
    
    info_rows = []
    for k, label in keys_to_compare:
        info_rows.append({
            "Metric": label,
            "yfinance": yf_info.get(k, "N/A"),
            "bfinance": bf_info.get(k, "N/A"),
        })
    print(pd.DataFrame(info_rows).to_string(index=False))

    # ------------------------------------------------------------- Statement Depth
    print("\n--- 3. FINANCIAL STATEMENT DEPTH ---")
    print(f"yfinance Annual Income Statement shape : {yf_t.financials.shape}")
    print(f"bfinance Annual Income Statement shape : {bf_t.financials.shape}")
    print(f"yfinance Balance Sheet shape          : {yf_t.balance_sheet.shape}")
    print(f"bfinance Balance Sheet shape          : {bf_t.balance_sheet.shape}")

    # ------------------------------------------------------------- Indian Superpowers
    print("\n--- 4. INDIAN MARKET SUPERPOWERS (bfinance ONLY) ---")
    print(f"Sector / Industry  : {bf_t.sector} -> {bf_t.industry}")
    print(f"Index Memberships  : {', '.join(bf_t.indices[:3])}")
    print(f"Piotroski Score    : {bf_t.piotroski_score}/9")
    print(f"Graham Fair Value  : ₹{bf_t.graham_number:,.2f}" if bf_t.graham_number else "Graham Value: N/A")
    print(f"Earnings Concalls  : {len(bf_t.concalls)} conference calls (with streamable MP3s & transcripts)")
    print(f"Quarterly Shareholding: {bf_t.shareholding.shape[1]} quarters available")

print("\n" + "=" * 80)
print("COMPARISON AUDIT COMPLETED!")
print("=" * 80)
