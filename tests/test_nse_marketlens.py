"""
MarketLens client tests — fully offline, driven by recorded fixtures.

Fixtures under ``tests/fixtures/marketlens/`` were captured from the live NSE endpoint on
2026-09-26. Each carries ``_fixture_symbol`` / ``_fixture_period`` provenance keys so a fixture
cannot be silently paired with the wrong request.

The two empty-result fixtures are regression guards for the endpoint's most dangerous behaviour:
an unresolvable symbol returns ``{"success": true, "data": [], "error": null}`` — the same shape a
bad period or an outage would produce.
"""

import json
from pathlib import Path

import pandas as pd
import pytest

from bfinance.nse import marketlens as ml
from bfinance.utils.exceptions import (
    NotAnNseSymbolError,
    TickerNotFoundError,
    UpstreamServiceError,
)

FIXTURES = Path(__file__).parent / "fixtures" / "marketlens"


def load(name: str) -> dict:
    with open(FIXTURES / name, encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def client():
    return ml.MarketLensClient()


# --------------------------------------------------------------------- period validation


def test_periods_are_uppercase_only_constants():
    """The endpoint silently returns zero rows for a lowercase period. Guard the constant."""
    assert ml.PERIODS == ("1D", "1W", "1M", "3M", "6M", "1Y",
                          "5Y", "10Y", "15Y", "20Y", "25Y", "30Y")
    assert all(p == p.upper() for p in ml.PERIODS)


@pytest.mark.parametrize("given,expected", [
    ("1y", "1Y"), ("30Y", "30Y"), (" 5y ", "5Y"), ("1d", "1D"), ("25y", "25Y"),
])
def test_validate_period_uppercases(given, expected):
    assert ml.validate_period(given) == expected


@pytest.mark.parametrize("bad", ["1z", "bogus", "MAX", "20year", "", None, 5, "1dd", "y1"])
def test_validate_period_rejects_before_any_request(bad):
    """A bad period must raise locally. Relying on the server to reject it is what produces
    a silent zero-row result."""
    with pytest.raises(ValueError):
        ml.validate_period(bad)


def test_lowercase_period_is_normalised_not_rejected(client):
    """Deliberate deviation: the client uppercases rather than raising on a lowercase period.

    Both satisfy the ticket's intent (a lowercase period can never silently return zero rows),
    but normalising is strictly safer — a typo in a config value or a UI dropdown gets data
    instead of an exception, and the silent-failure mode is closed either way.
    """
    assert ml.validate_period("30y") == "30Y"
    assert ml.validate_period("1d") == "1D"
    # A genuinely unsupported period still fails loudly, before any request.
    with pytest.raises(ValueError, match="not supported"):
        ml.validate_period("30yy")


# --------------------------------------------------------------------- symbol validation


@pytest.mark.parametrize("given,expected", [
    ("RELIANCE", "RELIANCE"),
    ("reliance", "RELIANCE"),
    ("RELIANCE.NS", "RELIANCE"),      # .NS stripped — the endpoint does not understand it
    ("niftybees.ns", "NIFTYBEES"),
    ("M&M", "M&M"),                    # verified live: ampersand in the path is fine
    ("BAJAJ-AUTO", "BAJAJ-AUTO"),
    ("  TCS  ", "TCS"),
])
def test_validate_nse_symbol_accepts(given, expected):
    assert ml.validate_nse_symbol(given) == expected


@pytest.mark.parametrize("bad", [
    "RELIANCE.BO", "RELIANCE.BSE", "RELIANCE-BO", "500112", "500325",
    "^NIFTY", "", "   ", ".NS", None, 42, "RELIANCE NS",
])
def test_validate_nse_symbol_rejects(bad):
    with pytest.raises(NotAnNseSymbolError):
        ml.validate_nse_symbol(bad)


def test_syntactically_valid_but_nonexistent_symbol_passes_validation():
    """Two-layer defence. RELIANCEEQ is well-formed, so validation lets it through; it is the
    fetch that fails, with TickerNotFoundError. Only *shape* errors are caught locally."""
    assert ml.validate_nse_symbol("RELIANCEEQ") == "RELIANCEEQ"

    with pytest.raises(TickerNotFoundError):
        ml.MarketLensClient._parse(load("empty_bad_symbol.json"), "RELIANCEEQ", "1Y")


def test_bse_ticker_error_names_the_reason():
    with pytest.raises(NotAnNseSymbolError, match="BSE"):
        ml.validate_nse_symbol("RELIANCE.BO")


def test_scrip_code_error_is_distinct_from_bse_suffix():
    """Both are BSE, but the messages should tell the caller which form they passed."""
    with pytest.raises(NotAnNseSymbolError, match="scrip code"):
        ml.validate_nse_symbol("500112")


# --------------------------------------------------------------------- parsing


def test_parse_daily_frame_shape(client):
    frame = ml.MarketLensClient._parse(load("reliance_1y.json"), "RELIANCE", "1Y")

    assert list(frame.columns) == ["Close", "Volume"]
    assert isinstance(frame.index, pd.DatetimeIndex)
    assert frame.index.name == "Date"
    assert frame.index.tz is None, "daily index should be naive"
    assert len(frame) == 245
    assert frame.index.is_monotonic_increasing
    assert str(frame.index[0].date()) == "2025-09-29"
    assert str(frame.index[-1].date()) == "2026-09-24"


def test_parse_marks_partial_coverage(client):
    """The RC1 guard. A consumer must be able to detect that O/H/L are absent by design."""
    frame = ml.MarketLensClient._parse(load("reliance_1y.json"), "RELIANCE", "1Y")
    assert frame.attrs[ml.PARTIAL_COVERAGE_ATTR] is True
    assert frame.attrs["bfinance_provenance"] == "nse_marketlens"
    assert frame.attrs["bfinance_symbol"] == "RELIANCE"
    assert frame.attrs["bfinance_period"] == "1Y"


def test_parsed_frame_has_no_ohlc_columns(client):
    """Filling these in from a close-only frame is the exact defect this module exists to prevent."""
    frame = ml.MarketLensClient._parse(load("reliance_1y.json"), "RELIANCE", "1Y")
    for column in ("Open", "High", "Low", "Adj Close"):
        assert column not in frame.columns


def test_parse_intraday_index_is_tz_aware_ist(client):
    frame = ml.MarketLensClient._parse(load("reliance_1d_intraday.json"), "RELIANCE", "1D")

    assert str(frame.index.tz) == "Asia/Kolkata"
    assert len(frame) == 372
    session_start = frame.index[0].strftime("%H:%M")
    session_end = frame.index[-1].strftime("%H:%M")
    assert session_start == "09:15", f"expected a 09:15 IST open, got {session_start}"
    assert session_end == "15:59", f"expected a 15:59 IST close, got {session_end}"


def test_parse_intraday_bars_are_nominally_one_minute(client):
    """Nominally 1-minute, but coverage thins in the closing session.

    Measured on the 2026-09-25 RELIANCE session: median spacing 60s, p95 66s, and two gaps of
    ~18 min after 15:15 and 15:32 — consistent with NSE's closing session, where prints are sparse.
    An earlier assertion of a flat 60s spacing was wrong and was corrected here rather than
    papered over.
    """
    frame = ml.MarketLensClient._parse(load("reliance_1d_intraday.json"), "RELIANCE", "1D")
    deltas = pd.Series(frame.index, index=frame.index).diff().dt.total_seconds().dropna()

    assert 55 <= deltas.median() <= 65, f"expected ~60s median spacing, got {deltas.median()}"
    assert deltas.quantile(0.95) <= 120, "95% of bars should be within two minutes"
    sparse = int((deltas > 120).sum())
    assert sparse <= 5, f"expected only a few sparse gaps, found {sparse}"


def test_intraday_sparse_bars_are_confined_to_the_closing_session(client):
    """The sparse gaps must be late-session, not scattered through the day. If this regresses,
    the feed has changed shape and the docstring caveat needs revisiting."""
    frame = ml.MarketLensClient._parse(load("reliance_1d_intraday.json"), "RELIANCE", "1D")
    deltas = pd.Series(frame.index, index=frame.index).diff()
    gaps = deltas[deltas.dt.total_seconds() > 120].dropna()
    assert len(gaps) > 0, "fixture is expected to contain the known closing-session gaps"
    for timestamp in gaps.index:
        assert timestamp.hour == 15, f"unexpected intraday gap at {timestamp}"


def test_parse_volume_is_integer_and_close_is_float(client):
    frame = ml.MarketLensClient._parse(load("reliance_1w.json"), "RELIANCE", "1W")
    assert pd.api.types.is_integer_dtype(frame["Volume"])
    assert pd.api.types.is_float_dtype(frame["Close"])


def test_parse_1w_fixture(client):
    frame = ml.MarketLensClient._parse(load("reliance_1w.json"), "RELIANCE", "1W")
    assert len(frame) == 4
    assert list(frame.columns) == ["Close", "Volume"]


def test_parse_1m_fixture(client):
    frame = ml.MarketLensClient._parse(load("reliance_1m.json"), "RELIANCE", "1M")
    assert len(frame) == 19
    assert str(frame.index[-1].date()) == "2026-09-24"


def test_parse_etf_fixture(client):
    """ETFs are covered — NIFTYBEES resolves identically to a large-cap."""
    frame = ml.MarketLensClient._parse(load("niftybees_1y.json"), "NIFTYBEES", "1Y")
    assert len(frame) == 245
    assert frame["Close"].gt(0).all()


def test_parse_30y_depth_is_recorded(client):
    """30Y depth is the headline claim. The trimmed fixture records the real count."""
    payload = load("reliance_30y_trimmed.json")
    assert payload["_fixture_full_row_count"] == 8069
    frame = ml.MarketLensClient._parse(payload, "RELIANCE", "30Y")
    assert len(frame) == 10, "trimmed fixture holds head+tail only"
    assert str(frame.index[0].date()) == "1996-09-30", "30Y series must start in 1996"


# --------------------------------------------------------------------- silent-failure guards


def test_empty_result_raises_rather_than_returning_empty_frame(client):
    """A bad symbol returns success/[]/null. That must not be indistinguishable from an outage."""
    with pytest.raises(TickerNotFoundError, match="no rows"):
        ml.MarketLensClient._parse(load("empty_bad_symbol.json"), "RELIANCEEQ", "1Y")


def test_bse_scrip_code_empty_result_also_raises(client):
    with pytest.raises(TickerNotFoundError):
        ml.MarketLensClient._parse(load("empty_bse_scripcode.json"), "500112", "1Y")


def test_empty_error_message_explains_the_ambiguity(client):
    """The message must tell the caller this is ambiguous, not just 'no data'."""
    with pytest.raises(TickerNotFoundError) as excinfo:
        ml.MarketLensClient._parse(load("empty_bad_symbol.json"), "X", "1Y")
    message = str(excinfo.value)
    assert "unresolvable" in message and "outage" in message


def test_parse_rejects_changed_schema(client):
    """A column rename upstream must raise, not produce a frame of NaN."""
    payload = load("reliance_1w.json")
    payload["data"] = [{"when": "2026-09-21", "px": 1.0, "vol": 2}]
    with pytest.raises(UpstreamServiceError, match="missing expected field"):
        ml.MarketLensClient._parse(payload, "RELIANCE", "1W")


def test_parse_rejects_null_closes(client):
    """A fully-null price column is not a usable series."""
    payload = load("reliance_1w.json")
    payload["data"] = [{"date": "2026-09-21", "price": None, "volume": 5}] * 2
    with pytest.raises(TickerNotFoundError):
        ml.MarketLensClient._parse(payload, "RELIANCE", "1W")


# --------------------------------------------------------------------- exactness


def test_close_and_volume_match_recorded_values_exactly(client):
    """No tolerance. MarketLens and yfinance agreed to the rupee and the share on all 8
    sessions cross-checked on 2026-09-26, so anything looser would hide a real regression."""
    frame = ml.MarketLensClient._parse(load("reliance_1y.json"), "RELIANCE", "1Y")
    last = frame.iloc[-1]
    assert last["Close"] == 1219.2
    assert int(last["Volume"]) == 13923795


def test_known_series_is_exact_end_to_end(client):
    frame = ml.MarketLensClient._parse(load("reliance_1m.json"), "RELIANCE", "1M")
    expected_first_close = 1287.0
    expected_last_close = 1219.2
    assert frame["Close"].iloc[0] == expected_first_close
    assert frame["Close"].iloc[-1] == expected_last_close


# --------------------------------------------------------------------- no-OHLC invariant


def test_module_exposes_no_ohlc_builder(client):
    """Guard against a future helper that fills O/H/L from a close-only frame."""
    forbidden = {"synthesize_ohlc", "to_ohlcv", "build_ohlc", "fill_ohlc", "fake_ohlc"}
    public = {name for name in dir(ml) if not name.startswith("_")}
    assert not (forbidden & public)


def test_negative_volume_is_clamped_and_counted(client):
    """Live probing on 2026-09-25 found 100 of 372 intraday bars carrying a NEGATIVE volume
    (-1 to -4). Negative volume is impossible, so it is clamped to 0 and counted — never
    silently passed through into an aggregate.
    """
    frame = ml.MarketLensClient._parse(load("reliance_1d_intraday.json"), "RELIANCE", "1D")

    assert frame["Volume"].ge(0).all(), "no bar may carry a negative volume after parsing"
    assert frame.attrs["bfinance_negative_volume_bars"] == 100, "the defect count must be recorded"
    assert frame.attrs["bfinance_nonpositive_close_bars"] == 0


def test_intraday_volume_is_known_unusable(client):
    """Pin the measured damage so a future feed change is noticed rather than assumed away."""
    frame = ml.MarketLensClient._parse(load("reliance_1d_intraday.json"), "RELIANCE", "1D")
    unusable = int((frame["Volume"] == 0).sum()) + frame.attrs["bfinance_negative_volume_bars"]
    assert unusable >= 175, f"expected >=175 unusable volume bars, got {unusable}"


def test_daily_volume_has_no_anomalies(client):
    """The daily series is clean. This is what makes MarketLens valuable and must not regress."""
    frame = ml.MarketLensClient._parse(load("reliance_1y.json"), "RELIANCE", "1Y")
    assert frame.attrs["bfinance_negative_volume_bars"] == 0
    assert frame["Volume"].gt(0).all(), "every daily bar should carry a positive volume"


def test_intraday_volume_caveat_is_documented():
    """The intraday weakness is measured, not assumed. Assert on the substance of the caveat —
    the measured figures and the redirection — rather than on a keyword, so rewording the
    docstring cannot silently drop the warning.
    """
    doc = ml.MarketLensClient.intraday_async.__doc__ or ""
    assert "100" in doc and "372" in doc, "the measured negative and total bar counts must be stated"
    assert "negative" in doc.lower(), "the negative-volume defect must be named explicitly"
    assert "price_history_async" in doc, "must redirect callers to the daily series for volume"
    assert "unusable" in doc.lower()
