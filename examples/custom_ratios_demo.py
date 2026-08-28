"""
Demonstration of Screener.in Custom Ratios Catalog Search and Quantitative Ratio Calculations.
"""

import sys
sys.stdout.reconfigure(encoding='utf-8')
import bfinance as bf

print("=" * 80)
print("SCREENER CUSTOM RATIOS & QUANTITATIVE SCORING ENGINE")
print("=" * 80)

# 1. Search Screener's 500+ Ratio Dictionary
print("\n[1] SEARCHING SCREENER RATIO CATALOG FOR 'graham':")
graham_results = bf.ratios.search("graham")
for r in graham_results[:2]:
    print(f"  • Name: {r['name']} (Unit: {r['unit'] or 'ratio'})")
    print(f"    Formula/Description: {r['description']}\n")

print("[2] SEARCHING SCREENER RATIO CATALOG FOR 'piotroski':")
piot_results = bf.ratios.search("piotroski")
for r in piot_results[:1]:
    print(f"  • Name: {r['name']}")
    print(f"    Description: {r['description']}\n")

# 2. Extract & Compute Custom Ratios for Reliance and TCS
for sym in ["RELIANCE", "TCS"]:
    t = bf.Ticker(sym)
    cr = t.custom_ratios
    print("-" * 80)
    print(f"CUSTOM RATIOS FOR {sym}:")
    print(f"  • Piotroski F-Score (0-9)      : {t.piotroski_score}/9")
    print(f"  • Benjamin Graham Number       : ₹{t.graham_number:,.2f}" if t.graham_number else "  • Graham Number: N/A")
    print(f"  • Enterprise Value (EV)        : ₹{t.enterprise_value:,.2f} Cr")
    print(f"  • EV / EBITDA Multiple         : {t.ev_to_ebitda}x" if t.ev_to_ebitda else "  • EV / EBITDA: N/A")
    print(f"  • Interest Coverage Ratio      : {t.interest_coverage}x" if t.interest_coverage else "  • Interest Coverage: N/A")
    print(f"  • Quality of Earnings (CFO/PAT): {cr.get('cfo_to_pat')}x")
    print(f"  • Free Cash Flow (FCF)         : ₹{cr.get('free_cash_flow_cr', 0):,.2f} Cr")
print("=" * 80)
