"""
Generate formatted Excel Workbooks (.xlsx) and AI Markdown Dossiers for Indian Equities.
"""

import sys
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
import bfinance as bf

export_dir = Path("exports")
export_dir.mkdir(parents=True, exist_ok=True)

symbols = ["RELIANCE", "TCS", "HDFCBANK", "BAJAJ-AUTO", "INFY"]

print("=" * 80)
print("GENERATING EXCEL FINANCIAL MODELS & AI MARKDOWN DOSSIERS")
print("=" * 80)

for sym in symbols:
    stock = bf.Ticker(sym)
    
    # 1. Generate 8-Tab Financial Model Excel (.xlsx)
    excel_path = export_dir / f"{sym}_10Y_Financial_Model.xlsx"
    stock.to_excel(str(excel_path))
    print(f"[EXCEL]    {sym:<10} -> {excel_path.name:<32} ({excel_path.stat().st_size:,} bytes)")
    
    # 2. Generate AI Financial Dossier (.md)
    md_path = export_dir / f"{sym}_AI_Dossier.md"
    md_content = stock.to_ai_context(format="markdown")
    md_path.write_text(md_content, encoding="utf-8")
    print(f"[MARKDOWN] {sym:<10} -> {md_path.name:<32} ({len(md_content):,} characters)")
    
    # 3. Generate Investment Memo Prompt (.md)
    prompt_path = export_dir / f"{sym}_Investment_Memo_Prompt.md"
    prompt_content = stock.to_investment_memo_prompt()
    prompt_path.write_text(prompt_content, encoding="utf-8")
    print(f"[PROMPT]   {sym:<10} -> {prompt_path.name:<32} ({len(prompt_content):,} characters)\n")

print("=" * 80)
print(f"ALL FILES SUCCESSFULLY SAVED IN: {export_dir.resolve()}")
print("=" * 80)
