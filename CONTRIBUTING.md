# Contributing to `bfinance`

Thank you for your interest in contributing to `bfinance`! We welcome bug reports, feature suggestions, documentation improvements, and pull requests.

---

## 🛠️ Development Setup

`bfinance` uses [`uv`](https://github.com/astral-sh/uv) as its fast package and project manager.

### 1. Clone the Repository
```bash
git clone https://github.com/bfinance-org/bfinance.git
cd bfinance
```

### 2. Install Dependencies
```bash
# Sync dependencies and development tools
uv sync --extra dev
```

---

## 🧪 Running Tests

`bfinance` has an automated test suite covering yfinance parity, Screener ingestion, custom ratios, and AI engines:

```bash
# Run all tests
uv run pytest

# Run a specific test suite with verbose output
uv run pytest tests/test_ticker_yfinance_compat.py -v

# Run with coverage report
uv run pytest --cov=bfinance
```

---

## 📐 Coding Conventions & Guidelines

1. **yfinance 1.7.0+ Compatibility**:
   - Never break API signatures of existing `yfinance` methods (`history()`, `fast_info`, `info`, `download()`, `Tickers`, `Sector`).
   - If adding a parameter, ensure it matches `yfinance` defaults or is backward-compatible.
2. **Indian Market Invariants**:
   - Currency is always Indian Rupee (`INR` / `₹`).
   - Market caps and financial statement totals should support Indian numeral formatting (Crores `Cr`, Lakhs `L`).
   - Timezone for market hours is strictly `Asia/Kolkata`.
3. **Anti-Blocking & Network Hygiene**:
   - Always route web requests through `ScreenerClient` to preserve adaptive request pacing (`150ms`), jitter, User-Agent rotation, and SQLite caching.
4. **Zero-Crash Graceful Degradation**:
   - Core properties must handle missing data and invalid tickers gracefully when `raise_errors=False`.
   - Log warnings using `logging.getLogger("bfinance")`.

---

## 🚀 Submitting Pull Requests

1. Fork the repo and create a feature branch (`git checkout -b feature/my-enhancement`).
2. Add permanent unit tests in `tests/` for any new feature or bug fix.
3. Ensure all tests pass (`uv run pytest`).
4. Commit your changes and open a Pull Request.
