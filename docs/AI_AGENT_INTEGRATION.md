# AI Agent Integration Guide for `bfinance`

`bfinance` provides native tools and context formatters designed to connect Indian financial data directly to Large Language Models (LLMs) and autonomous AI Agent frameworks.

---

## 1. Quick Integration Patterns

### Pattern A: Direct Context Injection (Gemini / Claude / OpenAI)
Inject complete, token-optimized company financial dossiers directly into your prompts:

```python
import bfinance as bf
import google.generativeai as genai

# 1. Fetch stock and generate AI dossier
stock = bf.Ticker("RELIANCE")
ai_dossier = stock.to_ai_context(format="markdown")

# 2. Feed to Gemini / Claude / GPT
prompt = f"""
You are a Senior Equity Analyst. Analyze the following Indian company financial dossier:

{ai_dossier}

Provide an evaluation of its return on capital employed (ROCE) trend and capital allocation quality.
"""

model = genai.GenerativeModel("gemini-2.5-pro")
response = model.generate_content(prompt)
print(response.text)
```

---

### Pattern B: Ready-to-Run Prompt Factories
Use pre-built institutional prompts:

```python
import bfinance as bf

stock = bf.Ticker("TCS")

# 1. Investment Initiation Memo
memo_prompt = stock.to_investment_memo_prompt()

# 2. Forensic Accounting Check
forensic_prompt = stock.to_forensic_audit_prompt()

# 3. Earnings Concall Takeaways & Guidance
concall_prompt = stock.to_concall_analyst_prompt()
```

---

## 2. OpenAI / LiteLLM Function Calling & Tools

`bfinance` ships with pre-configured Function Calling schemas:

```python
from openai import OpenAI
import bfinance as bf
from bfinance.ai import BFinanceAITools

client = OpenAI()

# 1. Provide bfinance tool schemas to the model
messages = [
    {"role": "system", "content": "You are an Indian financial assistant with access to real-time company tools."},
    {"role": "user", "content": "Analyze the financial health of Infosys and check if it passes the Coffee Can screen."}
]

response = client.chat.completions.create(
    model="gpt-4o",
    messages=messages,
    tools=BFinanceAITools.get_openai_tools(),
    tool_choice="auto"
)

# 2. Execute tool calls automatically
for tool_call in response.choices[0].message.tool_calls or []:
    function_name = tool_call.function.name
    arguments = json.loads(tool_call.function.arguments)
    
    # Execute using bfinance dispatcher
    tool_result = BFinanceAITools.execute_tool(function_name, arguments)
    
    # Send result back to LLM
    messages.append(response.choices[0].message)
    messages.append({
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": tool_result
    })

final_response = client.chat.completions.create(
    model="gpt-4o",
    messages=messages
)
print(final_response.choices[0].message.content)
```

---

## 3. LangChain & LangGraph Integration

Integrate `bfinance` tools into LangChain agents:

```python
from langchain.tools import tool
import bfinance as bf

@tool
def get_indian_stock_dossier(symbol: str) -> str:
    """Fetch 10-year financials, valuation ratios, CAGRs, and concalls for an Indian stock (NSE/BSE)."""
    t = bf.Ticker(symbol)
    return t.to_ai_context(format="markdown")

@tool
def run_stock_screener(strategy: str) -> str:
    """Run institutional screen (coffee_can, magic_formula, debt_free_compounders, high_dividend_yield)."""
    screen = getattr(bf.screens, strategy, None)
    if not screen:
        return f"Strategy {strategy} not found."
    df = screen.run(max_stocks=10)
    return df.to_markdown(index=False)

tools = [get_indian_stock_dossier, run_stock_screener]
```

---

## 4. CrewAI Integration

```python
from crewai.tools import tool
import bfinance as bf

@tool("Indian Stock Financial Dossier")
def stock_dossier_tool(symbol: str) -> str:
    """Fetches complete financial statement and concall history for an Indian company."""
    return bf.Ticker(symbol).to_ai_context(format="markdown")
```

---

## 5. Token Efficiency & Output Sizing

`bfinance` Markdown dossiers are structured with clean markdown tables and bulleted highlights:
* **Standard Dossier Size**: ~10,000 to 13,000 characters (~2,500 to 3,200 tokens).
* Fits comfortably within standard 8k, 32k, 128k, and 1M+ context windows.
* Covers 10 years of annual P&L, 12 quarters, Balance Sheet, Cash Flow, Shareholding, CAGRs, and Concalls in a single dense prompt.
