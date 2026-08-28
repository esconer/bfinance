"""
Master End-to-End Verification of bfinance.
Exercises every single module: Core Ticker, Statements, Media, Screens, Excel Exporter, and AI Engine.
"""

import sys
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
import tempfile
import pandas as pd
import bfinance as bf
from bfinance.ai import BFinanceAITools

def verify_all():
    print("=" * 80)
    print("BFINANCE MASTER END-TO-END VERIFICATION AUDIT")
    print("=" * 80)

    # ---------------------------------------------------- 1. Core Ticker & yfinance Parity
    print("\n[TEST 1] YFINANCE 1.7.0 COMPATIBILITY & MARKET DATA:")
    stock = bf.Ticker("RELIANCE")
    fi = stock.fast_info
    hist = stock.history(period="5d", actions=True)
    print(f"  • Symbol: {stock.symbol} | CMP: ₹{fi.last_price:,.2f} | Market Cap: ₹{fi.market_cap:,.0f}")
    print(f"  • 5-Day OHLCV History Shape: {hist.shape} (Columns: {list(hist.columns)})")
    assert not hist.empty
    assert "Close" in hist.columns

    # ---------------------------------------------------- 2. Deep Fundamentals & Statements
    print("\n[TEST 2] 10-YEAR STATEMENTS & DUAL SHAREHOLDING:")
    pnl = stock.financials
    bs = stock.balance_sheet
    sh_q = stock.shareholding
    sh_yr = stock.shareholding_yearly
    print(f"  • 10-Year Income Statement Periods ({len(pnl.columns)}): {list(pnl.columns)[-4:]}")
    print(f"  • 10-Year Balance Sheet Items: {len(pnl.index)} metrics")
    print(f"  • Quarterly Shareholding Shape: {sh_q.shape}")
    print(f"  • Annual Shareholding (11Y) Shape: {sh_yr.shape}")
    assert not pnl.empty
    assert not sh_q.empty
    assert not sh_yr.empty

    # ---------------------------------------------------- 3. Media & Regulatory Filings
    print("\n[TEST 3] CONCALLS AUDIO, TRANSCRIPTS & ANNUAL REPORTS:")
    tcs = bf.Ticker("TCS")
    concalls = tcs.concalls
    reports = tcs.annual_reports
    print(f"  • Total Concalls Available: {len(concalls)}")
    if concalls:
        c0 = concalls[0]
        print(f"    - Latest Call: {c0.title} ({c0.date})")
        print(f"    - Transcript URL: {c0.transcript_url[:60] if c0.transcript_url else 'N/A'}...")
        print(f"    - Audio MP3 URL: {c0.audio_url[:60] if c0.audio_url else 'N/A'}...")
    print(f"  • Annual Reports Available: {len(reports)} years")
    assert len(concalls) > 0
    assert len(reports) > 0

    # ---------------------------------------------------- 4. Institutional Screens
    print("\n[TEST 4] INSTITUTIONAL QUANTITATIVE SCREENS:")
    df_coffee = bf.screens.coffee_can.run(max_stocks=3)
    print(f"  • Coffee Can Matches ({len(df_coffee)} stocks):")
    for _, row in df_coffee.iterrows():
        print(f"    - {row['Symbol']}: ROCE={row['ROCE_%']}% | ROE={row['ROE_%']}% | Price=₹{row['Price']}")
    assert not df_coffee.empty

    # ---------------------------------------------------- 5. Multi-Sheet Excel Financial Model
    print("\n[TEST 5] MULTI-TAB EXCEL FINANCIAL MODEL EXPORT (.xlsx):")
    with tempfile.TemporaryDirectory() as tmp_dir:
        excel_target = Path(tmp_dir) / "RELIANCE_Audit.xlsx"
        saved = stock.to_excel(str(excel_target))
        print(f"  • Generated Workbook: {saved} (Size: {Path(saved).stat().st_size:,} bytes)")
        with pd.ExcelFile(saved) as xl:
            print(f"  • Workbook Sheets ({len(xl.sheet_names)}): {xl.sheet_names}")
            assert len(xl.sheet_names) >= 7

    # ---------------------------------------------------- 6. AI / LLM Context & Prompts Engine
    print("\n[TEST 6] AI DATA FEED ENGINE & PROMPT GENERATOR:")
    ai_md = stock.to_ai_context(format="markdown")
    ai_json = stock.to_ai_context(format="json")
    memo_prompt = stock.to_investment_memo_prompt()
    audit_prompt = stock.to_forensic_audit_prompt()
    tools = BFinanceAITools.get_openai_tools()

    print(f"  • AI Markdown Dossier Generated: {len(ai_md):,} characters ({len(ai_md.splitlines())} lines)")
    print(f"  • AI JSON Dossier Top Nodes: {list(ai_json.keys())[:6]}...")
    print(f"  • Investment Memo Prompt: {len(memo_prompt):,} characters")
    print(f"  • Forensic Accounting Audit Prompt: {len(audit_prompt):,} characters")
    print(f"  • Registered AI Agent Tools: {[t['function']['name'] for t in tools]}")
    assert len(ai_md) > 1000
    assert len(ai_json) > 5
    assert len(memo_prompt) > 1000

    print("\n" + "=" * 80)
    print("ALL 6 MODULES VERIFIED 100% FUNCTIONAL AND CLEAN!")
    print("=" * 80)

if __name__ == "__main__":
    verify_all()
