"""
Agent Function Calling & Tool Specifications for OpenAI, Gemini, Anthropic, LangChain, and LiteLLM.
"""

from typing import Any, Callable, Dict, List, Optional
import json
import math
from bfinance.ticker import Ticker
from bfinance.screens import screens
from bfinance.ai.context import AIContextBuilder


def _sanitize_non_finite(obj: Any) -> Any:
    """Replace NaN/Inf floats with None for valid JSON."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanitize_non_finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_non_finite(v) for v in obj]
    return obj


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
    def execute_tool(cls, name: str, arguments: Dict[str, Any]) -> Any:
        """
        Execute an AI tool call and return serialized string output.
        """
        try:
            if name == "get_stock_dossier":
                symbol = arguments.get("symbol", "")
                if not isinstance(symbol, str) or not symbol.strip():
                    raise ValueError("symbol must be a non-empty string")
                out_fmt = arguments.get("format", "markdown")
                if out_fmt not in ("json", "markdown"):
                    raise ValueError("format must be 'json' or 'markdown'")
                try:
                    t = Ticker(symbol.strip())
                    if out_fmt == "json":
                        data = _sanitize_non_finite(t.to_ai_context(format="json"))
                        return json.dumps(data, indent=2, allow_nan=False)
                    return t.to_ai_context(format="markdown")
                except ValueError:
                    raise
                except Exception as exc:
                    return {"error": str(exc)}

            elif name == "run_institutional_screen":
                screen_name = arguments.get("screen_name", "coffee_can")
                max_stocks = arguments.get("max_stocks", 10)
                if isinstance(max_stocks, bool) or not isinstance(max_stocks, int):
                    raise ValueError("max_stocks must be a positive int")
                if max_stocks <= 0 or max_stocks > 500:
                    raise ValueError("max_stocks must be between 1 and 500")
                screen_obj = getattr(screens, screen_name, None)
                if not screen_obj:
                    return {"error": f"Screen '{screen_name}' not found."}
                try:
                    df = screen_obj.run(max_stocks=max_stocks)
                except Exception as exc:
                    return {"error": str(exc)}
                try:
                    return df.to_markdown(index=False)
                except ImportError:
                    return f"```\n{df.to_string(index=False)}\n```"
                except Exception as exc:
                    return {"error": str(exc)}

            return {"error": f"Unknown tool function '{name}'."}
        except ValueError:
            raise
        except Exception as exc:
            return {"error": str(exc)}
