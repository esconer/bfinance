"""
Historical valuation multiples and price time series engine from Screener.in.
"""

from typing import Any, Dict
import pandas as pd
from bfinance.utils.formatting import parse_indian_number

#: Provenance tag for every frame this module builds, named as a module constant (not a literal
#: at the call site) for the same reason :data:`bfinance.ownership.PROVENANCE_SHAREHOLDING` is:
#: a test asserts on the exact string, and a string typed twice is a string that can drift.
PROVENANCE = "screener_chart"

#: The capability name, in the vocabulary :mod:`bfinance.ownership` established for
#: ``bfinance_capability`` ("shareholding_patterns", "bulk_short_block_deals"). The client method
#: that serves this frame is ``ScreenerClient.get_chart_timeseries``.
CAPABILITY = "chart_timeseries"


def _publish(frame: pd.DataFrame) -> pd.DataFrame:
    """Stamp the four attrs every chart frame carries, on both return paths.

    This used to be nothing at all, which was the worst honesty gap in the screener client: the
    chart payload is the only one of its five that is cached **and** parsed into a frame, so a
    chart frame served from ``~/.bfinance/cache.db`` was byte-for-byte indistinguishable from one
    fetched five seconds ago — there was nothing on it to say so.

    ``bfinance_cache_status`` is deliberately **not** set here. This function is pure and cannot
    know where its argument came from; claiming a status it did not observe would be a guess
    wearing the same key name as a fact. :meth:`ScreenerClient.get_chart_timeseries` sets it from
    the real :class:`~bfinance.cache.sqlite_cache.CacheLookup`, which is the same division of
    labour :mod:`bfinance.ownership` uses (the parser publishes provenance, the client publishes
    the cache outcome).

    The empty payload is not an exception: an empty chart frame is exactly the shape a caller is
    most likely to mistake for "nothing was cached and there is nothing to show", so it carries the
    provenance too.
    """
    frame.attrs["bfinance_provenance"] = PROVENANCE
    frame.attrs["bfinance_capability"] = CAPABILITY
    frame.attrs["bfinance_source_row_count"] = len(frame)
    frame.attrs["bfinance_columns"] = tuple(str(col) for col in frame.columns)
    return frame


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

        The returned frame always carries ``attrs["bfinance_provenance"] = 'screener_chart'``,
        ``bfinance_capability``, ``bfinance_source_row_count`` and ``bfinance_columns`` — including
        on the empty-payload path. Pure: no network, no clock, and no claim about where the
        argument came from.
        """
        datasets = raw_json.get("datasets", [])
        if not datasets:
            return _publish(pd.DataFrame())

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
        return _publish(df)
