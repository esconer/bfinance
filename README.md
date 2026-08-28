# 🇮🇳 `bfinance` (Bharat Finance)

<p align="center">
  <b>The High-Performance Python SDK for Indian Equities (NSE & BSE)</b><br>
  <i>A 1:1 drop-in replacement for <code>yfinance</code> (1.7.0+ compatible) supercharged with 10-year audited Ind AS financials, concalls with streamable audio MP3s, institutional stock screeners, multi-tab Excel models, and native AI/LLM data feeds.</i>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue?style=flat-square" alt="Python Versions">
  <img src="https://img.shields.io/badge/yfinance%20Parity-1.7.0%20Compatible-green?style=flat-square" alt="yfinance 1.7.0 Compatible">
  <img src="https://img.shields.io/badge/Coverage-NSE%20%26%20BSE-orange?style=flat-square" alt="NSE and BSE">
  <img src="https://img.shields.io/badge/Tests-54%2F54%20Passing-brightgreen?style=flat-square" alt="Tests Passing">
  <img src="https://img.shields.io/badge/License-MIT-purple?style=flat-square" alt="MIT License">
</p>

---

## 🚀 Why `bfinance`?

Standard financial libraries like `yfinance` often fail Indian equity investors: Yahoo Finance truncates Indian financial statements to just **4–5 years**, frequently experiences **missing fundamental data**, lacks **conference calls and shareholding patterns**, and breaks with upstream API changes.

`bfinance` solves this completely. It gives you the **exact same API as `yfinance` 1.7.0+** so your existing code, charting tools, and backtesters work with **zero code changes**, while giving you access to deep Indian corporate data:

| Dimension | `yfinance` (Standard) | `bfinance` (Supercharged) |
| :--- | :---: | :---: |
| **API Syntax Parity** | 1.7.0+ Standard | **100% Drop-in Equivalent** (`yf.Ticker` $\leftrightarrow$ `bf.Ticker`) |
| **Financial Statements Depth** | 4 to 5 Years (Standard Yahoo) | **10 to 13+ Years** (Audited Ind AS Statements) |
| **Quarterly Statement History**| 4 to 5 Quarters | **12 to 16+ Historical Quarters** |
| **Institutional Shareholding** | Basic / Often Empty | **12Q Quarterly & 11Y Annual Trends** (Promoter, FII, DII, Govt, Public) |
| **Conference Calls & Transcripts**| ❌ None | **40+ Concalls** with **Direct Audio MP3s**, BSE PDFs & Presentations |
| **Annual Reports & Credit Ratings**| ❌ None | **15+ Years Annual Report PDFs** & CRISIL/ICRA Credit Rationales |
| **Sector & Industry Hierarchy**| Generic Global Taxonomy | **4-Level Indian Taxonomy** + Index Memberships (`Nifty 50`, `Sensex`) |
| **Institutional Stock Screens**| ❌ None | **Built-in Screens**: Coffee Can, Magic Formula, Debt-Free, High Yield |
| **Custom Ratios & Scoring** | ❌ None | **Piotroski 9-Point Score**, **Graham Number**, **EV/EBITDA**, Screener search |
| **Excel Financial Modeling** | ❌ None | **1-Line Multi-Tab `.xlsx` Export** matching Screener "Export to Excel" |
| **AI / LLM Context Engine** | ❌ None | **Native Token-Dense Markdown/JSON Dossiers** & Prompt Factories |
| **Anti-Blocking Architecture** | ❌ None | **Persistent SQLite Cache** (24h TTL), User-Agent pool, Jitter pacing |

---

## 📦 Installation

```bash
# Using pip
pip install bfinance

# Using uv (recommended)
uv add bfinance
```

---

## ⚡ 30-Second Quickstart (Zero-Change Migration from `yfinance`)

Simply replace `import yfinance as yf` with `import bfinance as yf`:

```python
import bfinance as yf

# 1. Initialize any NSE or BSE ticker (supports RELIANCE, RELIANCE.NS, 500325.BO)
ticker = yf.Ticker("RELIANCE")

# 2. Exact yfinance 1.7.0 APIs work out of the box!
print("Live CMP:", ticker.fast_info.last_price)
print("Market Cap:", ticker.fast_info.market_cap)

# Historical OHLCV (Exact DataFrame schema with Dividends & Splits)
hist = ticker.history(period="1mo", actions=True)
print(hist.tail())

# 3. Access 10-Year Audited Financial Statements
print(ticker.financials)         # 10+ Years Ind AS Annual Income Statement
print(ticker.balance_sheet)      # 10+ Years Annual Balance Sheet
print(ticker.cashflow)           # 10+ Years Cash Flow Statement
print(ticker.quarterly_income_stmt) # 12+ Quarters Results

# 4. Multi-ticker downloading (yf.download equivalent)
data = yf.download(["TCS", "INFY", "HDFCBANK"], period="5d")
```

---

## 💎 Indian Market Superpowers

### 1. Conference Calls with Direct Audio MP3 Streaming
Access over 40+ historical quarterly earnings calls, download PDFs, and stream raw audio:

```python
import bfinance as bf

stock = bf.Ticker("TCS")

for call in stock.concalls[:3]:
    print(f"[{call.date}] {call.title}")
    print("  • PDF Transcript:", call.transcript_url)
    print("  • Audio MP3 Link:", call.audio_url)

# Download the latest concall audio MP3 straight to disk
stock.download_concall_audio(index=0, dest_path="./tcs_concall.mp3")

# Download the latest transcript PDF
stock.download_concall_transcript(index=0, dest_path="./tcs_transcript.pdf")
```

---

### 2. Dual Cadence Institutional Shareholding Trends
Track institutional accumulation across FIIs, Mutual Funds, and Promoters:

```python
stock = bf.Ticker("RELIANCE")

# 12+ Quarters Distribution
print("Quarterly Shareholding (12Q):\n", stock.shareholding)

# 11+ Years Historical Long-Term Trend
print("Annual Shareholding (11Y):\n", stock.shareholding_yearly)
```

---

### 3. Complete 4-Level Sector Hierarchy & Index Memberships
```python
stock = bf.Ticker("RELIANCE")

print("Sector:", stock.sector)                  # Energy
print("Industry Group:", stock.industry_group)  # Oil, Gas & Consumable Fuels
print("Industry:", stock.industry)              # Petroleum Products
print("Sub-Industry:", stock.sub_industry)      # Refineries & Marketing
print("Indices:", stock.indices)                # ['BSE Sensex', 'Nifty 50', 'BSE 500', ...]
```

---

### 4. Custom Ratios & Quantitative Scoring Engine
Search Screener's 500+ ratio directory and compute advanced investment metrics:

```python
stock = bf.Ticker("RELIANCE")

# 1. Joseph Piotroski 9-Point F-Score
print("Piotroski Score:", stock.piotroski_score, "/ 9")

# 2. Benjamin Graham Maximum Fair Value
print("Graham Number: ₹", stock.graham_number)

# 3. Enterprise Value & Multiples
print("Enterprise Value: ₹", stock.enterprise_value, "Cr")
print("EV / EBITDA:", stock.ev_to_ebitda, "x")
print("Interest Coverage Ratio:", stock.interest_coverage, "x")

# 4. Search Screener's 500+ ratio catalog
results = bf.ratios.search("graham")
for r in results:
    print(r["name"], "->", r["description"])
```

---

### 5. Institutional Stock Screeners
Run pre-built institutional strategies or custom filters:

```python
import bfinance as bf

# 1. Saurabh Mukherjea Coffee Can (10Y ROCE > 15% & ROE > 15%)
coffee_df = bf.screens.coffee_can.run(max_stocks=10)
print(coffee_df[['Symbol', 'Name', 'Price', 'ROCE_%', 'ROE_%']])

# 2. Joel Greenblatt Magic Formula (High ROCE + Attractive P/E)
magic_df = bf.screens.magic_formula.run(max_stocks=10)

# 3. Other built-in screens
bf.screens.debt_free_compounders.run()
bf.screens.high_dividend_yield.run()
bf.screens.undervalued_growth.run()

# 4. Custom Screener Predicate
growth_screen = bf.screens.custom(
    name="High ROE Midcaps",
    filter_fn=lambda t: (t.info.get("marketCapInCr") or 0) > 10000 and ((t.info.get("returnOnEquity") or 0) * 100) > 20
)
print(growth_screen.run(max_stocks=5))
```

---

### 6. 1-Line Multi-Tab Excel Financial Model Exporter
Export complete 10+ year statements and valuation ratios into an 8-sheet `.xlsx` workbook matching Screener's "Export to Excel":

```python
stock = bf.Ticker("RELIANCE")
stock.to_excel("Reliance_10Y_Financial_Model.xlsx")
```
*Generates sheets: `Overview`, `Profit & Loss`, `Quarters`, `Balance Sheet`, `Cash Flow`, `Shareholding`, `Ratios History`, and `Peers`.*

---

## 🤖 Native AI / LLM Data Feed Engine

Feed 100% of corporate data directly to AI agents (Gemini, Claude, GPT, DeepSeek, LangChain, LlamaIndex, CrewAI):

```python
stock = bf.Ticker("RELIANCE")

# 1. Generate token-dense Markdown financial dossier
markdown_dossier = stock.to_ai_context(format="markdown")

# 2. Or generate structured JSON dictionary
json_dossier = stock.to_ai_context(format="json")

# 3. Ready-to-run prompt templates
memo_prompt = stock.to_investment_memo_prompt()      # Initiation Coverage Note
audit_prompt = stock.to_forensic_audit_prompt()      # Forensic Accounting Check
concall_prompt = stock.to_concall_analyst_prompt()   # Earnings Call Analysis
```

### AI Agent Function Calling Tools
```python
from bfinance.ai import BFinanceAITools

# Get native OpenAI / Gemini tool calling schemas
tools = BFinanceAITools.get_openai_tools()

# Execute tool call returned by LLM
result = BFinanceAITools.execute_tool(
    name="get_stock_dossier",
    arguments={"symbol": "TCS", "format": "markdown"}
)
```

---

## 🛡️ Anti-Blocking & Zero-Crash Architecture

`bfinance` is engineered for production environments where reliability and uptime are paramount:

1. **Persistent SQLite Caching (`~/.cache/bfinance/cache.db`)**:
   - Fundamentals cached for **24 hours**, search queries for **7 days**, chart timeseries for **6 hours**.
   - **99% of requests resolve in < 1ms from disk cache** without touching remote servers.
2. **Adaptive Request Pacing & Jitter**:
   - Automatic `150ms` pacing + `10–50ms` Gaussian micro-jitter to prevent WAF burst detection.
3. **Desktop Browser Pool Rotation**:
   - Automatically rotates realistic Chrome, Safari, Firefox, and Edge user-agents with complete browser headers.
4. **Exponential Backoff**:
   - Recovers gracefully from HTTP 429 rate limits without aggressive retry spam.
5. **Zero-Crash Graceful Degradation (`raise_errors=False` default)**:
   - Missing data, invalid symbols, or delisted companies log structured warnings via standard Python `logging` and return safe empty DataFrames/dictionaries instead of throwing uncaught exceptions.

---

## 📚 Detailed Documentation & Guides

* 📖 **[API Reference Manual](docs/API_REFERENCE.md)**: Exhaustive function-by-function guide.
* 🔄 **[yfinance Migration Guide](docs/YFINANCE_MIGRATION_GUIDE.md)**: Step-by-step migration notes.
* 🤖 **[AI Agent Integration Guide](docs/AI_AGENT_INTEGRATION.md)**: Using `bfinance` with LangChain, LlamaIndex, CrewAI, and OpenAI/Gemini.
* 🧪 **[Contributing & Development](CONTRIBUTING.md)**: Testing and development setup.

---

## 🧪 Test Suite

`bfinance` maintains an exhaustive test suite with **54/54 tests passing**:

```bash
# Run full test suite with uv
uv run pytest
```

---

## 📄 License & Legal Disclaimers

### License
This project is licensed under the **MIT License**. See [`LICENSE`](LICENSE) for complete terms.

### ⚠️ Legal, Financial & Non-Affiliation Disclaimer
1. **Educational & Academic Purpose Only**: `bfinance` is an open-source software library developed strictly for academic, educational, and research purposes. It is **NOT** financial, investment, accounting, tax, or legal advice.
2. **No Investment Liability**: The developers, maintainers, and contributors are not SEBI-registered Research Analysts (RA) or Investment Advisors (RIA). No output from this library constitutes a recommendation or solicitation to buy, sell, or hold any security or financial derivative. Users assume **100% full responsibility** for their own financial decisions and trading operations. In no event shall the authors be liable for any direct, indirect, or consequential financial losses.
3. **Independent Project & Non-Affiliation**: `bfinance` is an independent open-source project and is **not affiliated, endorsed, authorized, or certified by** Yahoo! Inc., `yfinance`, Screener.in (Mittal Analytics), NSE India, BSE India, or any of their parent entities or subsidiaries. All product names, trademarks, ticker symbols, and brand logos belong to their respective owners.
4. **Data Verification & Fair Usage**: Data is fetched from publicly accessible endpoints. Users are responsible for complying with the Terms of Service and rate limits of any upstream platforms. For mission-critical commercial applications, users should subscribe to official authorized market data providers.
