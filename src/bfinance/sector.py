"""
Sector and Industry explorer classes matching yfinance 1.4.0+ Sector and Industry API.
"""

from typing import Dict, List, Optional
import pandas as pd


INDIAN_SECTOR_MAP = {
    "technology": {
        "name": "Information Technology",
        "key": "technology",
        "top_companies": [
            {"symbol": "TCS", "name": "Tata Consultancy Services", "cmp": 3950.0, "market_cap_cr": 1420000.0, "pe": 29.5},
            {"symbol": "INFY", "name": "Infosys Ltd", "cmp": 1820.0, "market_cap_cr": 750000.0, "pe": 27.2},
            {"symbol": "HCLTECH", "name": "HCL Technologies Ltd", "cmp": 1780.0, "market_cap_cr": 480000.0, "pe": 26.0},
            {"symbol": "WIPRO", "name": "Wipro Ltd", "cmp": 540.0, "market_cap_cr": 280000.0, "pe": 24.5},
            {"symbol": "TECHM", "name": "Tech Mahindra Ltd", "cmp": 1620.0, "market_cap_cr": 158000.0, "pe": 38.0},
        ],
    },
    "financials": {
        "name": "Financial Services",
        "key": "financials",
        "top_companies": [
            {"symbol": "HDFCBANK", "name": "HDFC Bank Ltd", "cmp": 1640.0, "market_cap_cr": 1250000.0, "pe": 18.5},
            {"symbol": "ICICIBANK", "name": "ICICI Bank Ltd", "cmp": 1220.0, "market_cap_cr": 860000.0, "pe": 17.8},
            {"symbol": "SBIN", "name": "State Bank of India", "cmp": 810.0, "market_cap_cr": 720000.0, "pe": 10.2},
            {"symbol": "KOTAKBANK", "name": "Kotak Mahindra Bank Ltd", "cmp": 1780.0, "market_cap_cr": 350000.0, "pe": 21.0},
            {"symbol": "AXISBANK", "name": "Axis Bank Ltd", "cmp": 1180.0, "market_cap_cr": 365000.0, "pe": 14.5},
        ],
    },
    "energy": {
        "name": "Energy & Oil & Gas",
        "key": "energy",
        "top_companies": [
            {"symbol": "RELIANCE", "name": "Reliance Industries Ltd", "cmp": 1288.0, "market_cap_cr": 1742000.0, "pe": 23.3},
            {"symbol": "ONGC", "name": "Oil and Natural Gas Corporation", "cmp": 310.0, "market_cap_cr": 390000.0, "pe": 8.5},
            {"symbol": "NTPC", "name": "NTPC Ltd", "cmp": 410.0, "market_cap_cr": 395000.0, "pe": 18.2},
            {"symbol": "POWERGRID", "name": "Power Grid Corporation", "cmp": 330.0, "market_cap_cr": 305000.0, "pe": 19.0},
            {"symbol": "IOC", "name": "Indian Oil Corporation", "cmp": 175.0, "market_cap_cr": 245000.0, "pe": 11.5},
        ],
    },
    "auto": {
        "name": "Automobile & Auto Components",
        "key": "auto",
        "top_companies": [
            {"symbol": "TATAMOTORS", "name": "Tata Motors Ltd", "cmp": 1050.0, "market_cap_cr": 385000.0, "pe": 11.2},
            {"symbol": "MARUTI", "name": "Maruti Suzuki India Ltd", "cmp": 12400.0, "market_cap_cr": 390000.0, "pe": 28.0},
            {"symbol": "M&M", "name": "Mahindra & Mahindra Ltd", "cmp": 2850.0, "market_cap_cr": 350000.0, "pe": 31.5},
            {"symbol": "BAJAJ-AUTO", "name": "Bajaj Auto Ltd", "cmp": 9800.0, "market_cap_cr": 275000.0, "pe": 34.0},
            {"symbol": "MOTHERSON", "name": "Samvardhana Motherson International", "cmp": 180.0, "market_cap_cr": 125000.0, "pe": 42.0},
        ],
    },
}


class Sector:
    """
    Sector representation matching yfinance 1.4.0+ `yf.Sector`.
    """

    def __init__(self, key: str):
        clean_key = key.lower().replace(" ", "-").replace("_", "-")
        self.key = clean_key
        sector_info = INDIAN_SECTOR_MAP.get(clean_key, {
            "name": key.title(),
            "key": clean_key,
            "top_companies": []
        })
        self.name = sector_info["name"]
        self._top_companies_raw = sector_info["top_companies"]

    @property
    def overview(self) -> Dict[str, str]:
        return {"name": self.name, "key": self.key, "region": "IN"}

    @property
    def top_companies(self) -> pd.DataFrame:
        """Return top sector constituent companies as a DataFrame."""
        if not self._top_companies_raw:
            return pd.DataFrame()
        return pd.DataFrame(self._top_companies_raw)

    def __repr__(self) -> str:
        return f"<bfinance.Sector name='{self.name}' key='{self.key}'>"


class Industry:
    """
    Industry representation matching yfinance 1.4.0+ `yf.Industry`.
    """

    def __init__(self, key: str):
        self.key = key.lower().replace(" ", "-")
        self.name = key.replace("-", " ").title()

    @property
    def overview(self) -> Dict[str, str]:
        return {"name": self.name, "key": self.key, "region": "IN"}

    @property
    def top_companies(self) -> pd.DataFrame:
        # Defaults to sector matching
        sec = Sector(self.key)
        return sec.top_companies

    def __repr__(self) -> str:
        return f"<bfinance.Industry name='{self.name}' key='{self.key}'>"
