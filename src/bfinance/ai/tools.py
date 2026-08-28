"""
Agent Function Calling & Tool Specifications for OpenAI, Gemini, Anthropic, LangChain, and LiteLLM.
"""

from typing import Any, Callable, Dict, List, Optional
import json
from bfinance.ticker import Ticker
from bfinance.screens import screens
from bfinance.ai.context import AIContextBuilder


class BFinanceAITools:
    """
    Standard Function Calling / Tool specifications for AI Agents.
    """

    @classmethod
    def get_openai_tools(cls) -> List[Dict[str, Any]]:
        """
        Return OpenAI / Gemini / LiteLLM function calling schemas.
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_stock_dossier",
                    "description": "Fetch 100% of financial statements, valuation ratios, 10Y CAGRs, shareholding, and concall media for an Indian stock (NSE/BSE).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "symbol": {
                                "type": "string",
                                "description": "NSE or BSE stock symbol, e.g. 'RELIANCE', 'TCS', 'HDFCBANK', 'BAJAJ-AUTO'."
                            },
                            "format": {
                                "type": "string",
                                "enum": ["markdown", "json"],
                                "description": "Output format ('markdown' for direct LLM reasoning, 'json' for structured processing).",
                                "default": "markdown"
                            }
                        },
                        "required": ["symbol"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "run_institutional_screen",
                    "description": "Run institutional quantitative stock screener across Indian equities (Coffee Can, Magic Formula, Debt Free Compounders, High Dividend, Undervalued Growth).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "screen_name": {
                                "type": "string",
                                "enum": ["coffee_can", "magic_formula", "debt_free_compounders", "high_dividend_yield", "undervalued_growth"],
                                "description": "The institutional strategy to execute."
                            },
                            "max_stocks": {
                                "type": "integer",
                                "description": "Maximum matching stocks to return.",
                                "default": 10
                            }
                        },
                        "required": ["screen_name"]
                    }
                }
            }
        ]

    @classmethod
    def execute_tool(cls, name: str, arguments: Dict[str, Any]) -> str:
        """
        Execute an AI tool call and return serialized string output.
        """
        if name == "get_stock_dossier":
            symbol = arguments.get("symbol", "")
            out_fmt = arguments.get("format", "markdown")
            t = Ticker(symbol)
            if out_fmt == "json":
                return json.dumps(t.to_ai_context(format="json"), indent=2)
            return t.to_ai_context(format="markdown")

        elif name == "run_institutional_screen":
            screen_name = arguments.get("screen_name", "coffee_can")
            max_stocks = arguments.get("max_stocks", 10)
            screen_obj = getattr(screens, screen_name, None)
            if not screen_obj:
                return f"Error: Screen '{screen_name}' not found."
            df = screen_obj.run(max_stocks=max_stocks)
            return df.to_markdown(index=False)

        return f"Error: Unknown tool function '{name}'."
