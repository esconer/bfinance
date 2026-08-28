"""
Test suite validating AI Context Generation, Prompt Factories, and Agent Function Calling Tools.
"""

import json
import pytest
import bfinance as bf
from bfinance.ai import AIContextBuilder, AIPromptFactory, BFinanceAITools


def test_ai_markdown_context():
    """Verify to_ai_context(format='markdown') produces comprehensive markdown dossier."""
    t = bf.Ticker("RELIANCE")
    md = t.to_ai_context(format="markdown")

    assert isinstance(md, str)
    assert "# FINANCIAL DOSSIER: Reliance Industries Ltd" in md
    assert "Current Valuation & Quality Metrics" in md
    assert "Annual Income Statement" in md
    assert "Annual Balance Sheet" in md
    assert "Institutional Shareholding Distribution" in md
    assert "Qualitative Analysis (Pros & Cons)" in md


def test_ai_json_context():
    """Verify to_ai_context(format='json') produces valid structured dictionary."""
    t = bf.Ticker("TCS")
    data = t.to_ai_context(format="json")

    assert isinstance(data, dict)
    assert data["symbol"] == "TCS"
    assert "Tata Consultancy Services" in data["company_name"]
    assert "valuation" in data
    assert "current_price" in data["valuation"]
    assert "annual_pnl" in data
    assert "cagrs" in data
    assert "concalls" in data


def test_ai_prompt_factory():
    """Verify prompt builders generate actionable, context-rich prompts for LLMs."""
    t = bf.Ticker("RELIANCE")

    # 1. Investment memo prompt
    memo_prompt = t.to_investment_memo_prompt()
    assert "<INSTRUCTIONS>" in memo_prompt
    assert "<COMPANY_FINANCIAL_DOSSIER>" in memo_prompt
    assert "Investment Initiation Note" in memo_prompt

    # 2. Forensic audit prompt
    audit_prompt = t.to_forensic_audit_prompt()
    assert "Forensic Accounting Auditor" in audit_prompt
    assert "Working Capital Stress" in audit_prompt

    # 3. Concall summary prompt
    concall_prompt = t.to_concall_analyst_prompt()
    assert "Forward Revenue, Margin, and Capex Guidance" in concall_prompt


def test_ai_tool_schemas_and_execution():
    """Verify agent tool calling schemas and tool execution."""
    # 1. OpenAI function calling schemas
    schemas = BFinanceAITools.get_openai_tools()
    assert isinstance(schemas, list)
    assert len(schemas) == 2
    assert schemas[0]["function"]["name"] == "get_stock_dossier"

    # 2. Execute get_stock_dossier tool
    result_md = BFinanceAITools.execute_tool("get_stock_dossier", {"symbol": "INFY", "format": "markdown"})
    assert isinstance(result_md, str)
    assert "Infosys" in result_md

    # 3. Execute run_institutional_screen tool
    result_screen = BFinanceAITools.execute_tool("run_institutional_screen", {"screen_name": "coffee_can", "max_stocks": 3})
    assert isinstance(result_screen, str)
    assert "Symbol" in result_screen or "Name" in result_screen
