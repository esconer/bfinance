"""
Historical valuation multiples and price time series engine from Screener.in.
"""

from typing import Any, Dict, List, Optional
import pandas as pd
from bfinance.utils.formatting import parse_indian_number


class ScreenerChartEngine:
    """
    Parser and serializer for Screener.in's internal chart API datasets.
    """

    METRIC_QUERIES = {
        "price": "Price-DMA50-DMA200-Volume",
        "pe": "Price to Earning-Median PE-EPS",
        "margins": "GPM-OPM-NPM-Quarter Sales",
        "ev_ebitda": "EV Multiple-Median EV Multiple-EBITDA",
        "pb": "Price to book value-Median PBV-Book value",
        "mcap_sales": "Market Cap to Sales-Median Market Cap to Sales-Sales",
    }

    @classmethod
    def get_query_param(cls, metric: str) -> str:
        """Resolve friendly metric shorthand into Screener URL query param."""
        clean_key = metric.lower().replace("-", "_").replace(" ", "_")
        return cls.METRIC_QUERIES.get(clean_key, metric)

    @classmethod
    def parse_chart_json_to_dataframe(cls, raw_json: Dict[str, Any]) -> pd.DataFrame:
        """
        Convert Screener chart API JSON into a clean time-indexed Pandas DataFrame.
        """
        datasets = raw_json.get("datasets", [])
        if not datasets:
            return pd.DataFrame()

        series_dict: Dict[str, Dict[str, float]] = {}
        for ds in datasets:
            metric_name = ds.get("metric", "value")
            values = ds.get("values", [])
            data_map: Dict[str, float] = {}
            for item in values:
                if len(item) >= 2:
                    date_str = str(item[0])
                    num_val = parse_indian_number(item[1])
                    if num_val is not None:
                        data_map[date_str] = num_val
            series_dict[metric_name] = data_map

        df = pd.DataFrame(series_dict)
        if not df.empty:
            df.index = pd.to_datetime(df.index)
            df.sort_index(inplace=True)
            df.index.name = "Date"
        return df
