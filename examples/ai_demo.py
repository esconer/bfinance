"""
AI Integration Demonstration: Feed 100% of bfinance corporate data to LLMs/AI Agents.
"""

import sys
sys.stdout.reconfigure(encoding='utf-8')
import bfinance as bf
from bfinance.ai import BFinanceAITools

print("=" * 80)
print("BFINANCE AI / LLM DATA FEED ENGINE DEMONSTRATION")
print("=" * 80)

stock = bf.Ticker("RELIANCE")

# 1. Generate Token-Optimized Markdown Dossier for AI
print("\n[1] AI MARKDOWN CONTEXT DOSSIER (Preview first 700 chars):")
md_context = stock.to_ai_context(format="markdown")
print(md_context[:700] + "\n...\n[TRUNCATED - Full dossier contains 10Y P&L, BS, CF, Shareholding, CAGRs, Pros/Cons, Concalls]")

# 2. Generate Structured JSON Context for LLM Function Calls
print("\n[2] AI STRUCTURED JSON DICTIONARY (Keys & Valuation Sample):")
json_context = stock.to_ai_context(format="json")
print("Top-level JSON keys:", list(json_context.keys()))
print("Valuation node:", json_context["valuation"])

# 3. Generate Ready-to-Run Investment Memo Prompt
print("\n[3] READY-TO-RUN INVESTMENT MEMO PROMPT (Preview):")
memo_prompt = stock.to_investment_memo_prompt()
print(memo_prompt[:450] + "\n...")

# 4. Generate Forensic Accounting Audit Prompt
print("\n[4] FORENSIC ACCOUNTING AUDIT PROMPT (Preview):")
audit_prompt = stock.to_forensic_audit_prompt()
print(audit_prompt[:450] + "\n...")

# 5. Agent Function Calling Tools
print("\n[5] OPENAI / GEMINI / ANTHROPIC TOOL SCHEMAS:")
tools = BFinanceAITools.get_openai_tools()
print(f"Registered {len(tools)} AI Tools:")
for t in tools:
    fn = t["function"]
    print(f"  • {fn['name']}: {fn['description'][:70]}...")

print("\n" + "=" * 80)
print("AI MODULE READY FOR PRODUCTION USE!")
print("=" * 80)
