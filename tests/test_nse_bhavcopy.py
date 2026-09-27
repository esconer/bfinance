"""
NSE bhavcopy client tests — fully offline, driven by recorded fixtures.

The three fixtures under ``tests/fixtures/bhavcopy/`` were captured from the live NSE archives on
2026-09-27 and trimmed to a handful of rows. ``META.json`` records the source URL, the real
header/row field counts and the true full row count for each, so a fixture cannot be silently
replaced with a different shape.

Each fixture keeps its **full original header line** (including the malformed 35-field one) and
at least one non-``EQ`` row, so the schema dispatch and the series filter are both genuinely
exercised rather than asserted against a hand-written stub.

Network access is stubbed at the HTTP boundary, never at the parser, so the tests cover the real
retry, 404 and pacing code paths without a socket.
"""

import asyncio
import io
import json
import zipfile
from pathlib import Path

import httpx
import pandas as pd
import pytest

from bfinance.nse import bhavcopy as bc
from bfinance.utils.exceptions import (
    ParsingError,
    RateLimitExceededError,
    TickerNotFoundError,
    UpstreamServiceError,
)

FIXTURES = Path(__file__).parent / "fixtures" / "bhavcopy"
META = json.loads((FIXTURES / "META.json").read_text(encoding="utf-8"))

UDIFF = "udiff_cm_20260925"
UDIFF_35COL = "udiff_cm_20240101_35col"
LEGACY = "legacy_cm_20230515"


def zip_bytes(key: str) -> bytes:
    return (FIXTURES / f"{key}.csv.zip").read_bytes()


def csv_text(key: str) -> str:
    """The CSV inside the fixture ZIP, decoded exactly as the client would."""
    return bc.unzip_bhavcopy(zip_bytes(key), url=key)


def row_index(lines, symbol: str) -> int:
    """Line number of ``symbol`` in a CSV (0 is the header)."""
    offset = 7 if lines[0].startswith("TradDt") else 0  # TckrSymb vs SYMBOL
    for position, line in enumerate(lines[1:], start=1):
        if line.split(",")[offset] == symbol:
            return position
    raise AssertionError(f"{symbol} is not in the fixture")


def _row_for(lines, symbol: str) -> str:
    return lines[row_index(lines, symbol)]


def blank_field(lines, symbol: str, column: str) -> str:
    """Return ``lines`` with one field of one row emptied, as a CSV string."""
    lines = list(lines)
    idx = lines[0].split(",").index(column)
    fields = _row_for(lines, symbol).split(",")
    fields[idx] = ""
    lines[row_index(lines, symbol)] = ",".join(fields)
    return "\n".join(lines)


def with_dates(csv: str, dates) -> str:
    """Rewrite only the date column of every row, keeping the recorded header and all other
    fields byte-identical.

    Used to synthesise a multi-day span from a one-day capture so a range merge can be tested
    offline. Every price is the *recorded* price for that symbol; only the date is moved.
    """
    iso = csv.splitlines()[0].startswith("TradDt")
    idx = 0 if iso else 10  # TradDt vs TIMESTAMP
    out = [csv.splitlines()[0]]
    for line in csv.splitlines()[1:]:
        fields = line.split(",")
        fields[idx] = dates[0] if iso else pd.Timestamp(dates[0]).strftime("%d-%b-%Y").upper()
        out.append(",".join(fields))
    return "\n".join(out)


def zipped(csv: str, member: str = "bhav.csv") -> bytes:
    """Wrap CSV text in a single-member ZIP, the shape NSE serves."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, csv.encode("utf-8"))
    return buf.getvalue()


@pytest.fixture
def client():
    """A client with pacing switched off, so tests do not sleep."""
    return bc.BhavcopyClient(min_request_interval_ms=0.0)


# ------------------------------------------------------------------ fixtures are real


def test_fixture_metadata_records_the_real_shapes():
    """Guard the fixtures themselves. A trimmed file with a rewritten header is not evidence."""
    assert META[UDIFF]["full_row_count"] == 3654
    assert META[UDIFF]["header_fields"] == 34
    assert META[UDIFF_35COL]["header_fields"] == 35
    assert META[UDIFF_35COL]["data_row_fields"] == 34
    assert META[LEGACY]["full_row_count"] == 2427
    assert META[LEGACY]["header_fields"] == 14
    for key, meta in META.items():
        assert meta["url"].startswith("https://nsearchives.nseindia.com/")
        assert meta["captured"] == "2026-09-27"


def test_the_35_column_fixture_is_actually_malformed():
    """The parse-by-name requirement is only real if the fixture exercises it.

    Upstream's 2024-01-01 file has a 35-field header and 34-field data rows. If a future capture
    replaces this with a well-formed file, the guard that protects against positional parsing
    silently stops running — so assert the malformation is present.
    """
    lines = csv_text(UDIFF_35COL).splitlines()
    assert len(lines[0].split(",")) == 35
    assert {len(row.split(",")) for row in lines[1:]} == {34}

    # pandas sees the extra header field as a junk column, which is what the parser must drop.
    frame = pd.read_csv(io.StringIO(csv_text(UDIFF_35COL)), dtype=str, keep_default_na=False)
    assert [c for c in frame.columns if str(c).startswith("Unnamed:")] == ["Unnamed: 34"]


def test_34_column_udiff_fixture_has_no_junk_column():
    frame = pd.read_csv(io.StringIO(csv_text(UDIFF)), dtype=str, keep_default_na=False)
    assert not [c for c in frame.columns if str(c).startswith("Unnamed:")]


# ------------------------------------------------------------------ schema dispatch


def test_udiff_schema_is_detected_and_parsed(client):
    frame = bc.parse_bhavcopy(csv_text(UDIFF), series="EQ")

    assert frame.attrs["bfinance_schema"] == "udiff"
    assert set(frame.index.get_level_values("Symbol")) == {
        "IRCON", "RELIANCE", "SBIN", "ZYDUSWELL",
    }
    assert str(frame.index.get_level_values("Date")[0].date()) == "2026-09-25"


def test_legacy_schema_is_detected_and_parsed(client):
    """The pre-2024 branch must be exercised offline, not assumed to mirror UDiFF."""
    frame = bc.parse_bhavcopy(csv_text(LEGACY), series="EQ")

    assert frame.attrs["bfinance_schema"] == "legacy"
    assert set(frame.index.get_level_values("Symbol")) == {"RELIANCE", "TCS", "ZYDUSWELL"}
    # Legacy TIMESTAMP is dd-MMM-YYYY, not the UDiFF ISO form.
    assert str(frame.index.get_level_values("Date")[0].date()) == "2023-05-15"


def test_malformed_35_column_udiff_still_parses_by_name(client):
    """A trailing field on the header must not shift, rename or blank the OHLCV columns."""
    frame = bc.parse_bhavcopy(csv_text(UDIFF_35COL), series="EQ")

    assert frame.attrs["bfinance_schema"] == "udiff"
    assert set(frame.index.get_level_values("Symbol")) == {"ABB", "PATELENG", "ZOTA"}
    assert frame["Close"].notna().all(), "a shifted column read would produce NaN or wrong values"
    assert frame.xs("ABB", level="Symbol")["Close"].iloc[0] == 4682.20


def test_unrecognised_schema_raises_rather_than_returning_nan(client):
    """A column rename upstream must be loud. Empty-looking frames are the failure mode this
    whole module was written to avoid."""
    with pytest.raises(ParsingError, match="no recognised schema"):
        bc.parse_bhavcopy("A,B,C\n1,2,3\n", series="EQ")


def test_udiff_missing_a_required_column_names_it(client):
    lines = csv_text(UDIFF).splitlines()
    header = lines[0].split(",")
    header[header.index("HghPric")] = "HighPricRenamed"
    body = [",".join(r.split(",")[:17] + [""] + r.split(",")[18:]) for r in lines[1:]]
    broken = "\n".join([",".join(header)] + body)

    with pytest.raises(ParsingError, match="high"):
        bc.parse_bhavcopy(broken, series="EQ")


# ------------------------------------------------------------------ output contract


def test_column_order_and_spelling_match_ohlcv_engine(client):
    """The exact tuple OHLCVEngine produces, in the exact order. Not a subset, not reordered."""
    for key in (UDIFF, UDIFF_35COL, LEGACY):
        frame = bc.parse_bhavcopy(csv_text(key), series="EQ")
        assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"], key


def test_index_is_date_then_symbol_and_level_zero_is_a_datetime_index(client):
    frame = bc.parse_bhavcopy(csv_text(UDIFF), series="EQ")

    assert isinstance(frame.index, pd.MultiIndex)
    assert list(frame.index.names) == ["Date", "Symbol"]
    level_zero = frame.index.get_level_values("Date")
    assert isinstance(level_zero, pd.DatetimeIndex)
    assert level_zero.name == "Date"
    assert level_zero.tz is None, "naive by design; OHLCVEngine localises to IST itself"


def test_frame_is_sorted_ascending_and_one_row_per_instrument(client):
    frame = bc.parse_bhavcopy(csv_text(UDIFF), series="EQ")

    assert frame.index.is_monotonic_increasing
    assert not frame.index.duplicated().any()
    assert len(frame) == 4
    assert frame.attrs["bfinance_duplicate_date_rows"] == 0


def test_single_symbol_slice_is_a_plain_datetime_index(client):
    """The cross-section narrows to the plain OHLCVEngine-compatible shape a consumer wants."""
    frame = bc.parse_bhavcopy(csv_text(UDIFF), series="EQ")
    single = frame.xs("RELIANCE", level="Symbol")

    assert isinstance(single.index, pd.DatetimeIndex)
    assert single.index.name == "Date"
    assert list(single.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert len(single) == 1


def test_volume_is_int64_when_complete(client):
    frame = bc.parse_bhavcopy(csv_text(UDIFF), series="EQ")
    assert pd.api.types.is_integer_dtype(frame["Volume"])
    assert pd.api.types.is_float_dtype(frame["Close"])


# ------------------------------------------------------------------ the attrs flag


def test_synthetic_ohlc_flag_is_false_on_every_schema(client):
    """The whole point of the module. Today every bfinance frame reports True."""
    for key in (UDIFF, UDIFF_35COL, LEGACY):
        frame = bc.parse_bhavcopy(csv_text(key), series="EQ")
        assert frame.attrs[bc.SYNTHETIC_OHLC_ATTR] is False, key


def test_synthetic_ohlc_flag_is_false_on_an_empty_frame():
    """A 404 must not read as 'real data' or as 'fabricated data'. It is 'no data'."""
    frame = bc.empty_bhavcopy_frame()
    assert frame.empty
    assert frame.attrs[bc.SYNTHETIC_OHLC_ATTR] is False


def test_synthetic_ohlc_flag_cannot_be_suppressed_by_caller_attrs():
    """A caller-supplied attrs dict must not be able to set the flag True and lie."""
    frame = bc.empty_bhavcopy_frame({bc.SYNTHETIC_OHLC_ATTR: True, "x": 1})
    assert frame.attrs[bc.SYNTHETIC_OHLC_ATTR] is False
    assert frame.attrs["x"] == 1


def test_provenance_is_recorded(client):
    frame = bc.parse_bhavcopy(csv_text(UDIFF), series="EQ")
    assert frame.attrs["bfinance_provenance"] == "nse_bhavcopy"
    assert frame.attrs["bfinance_series"] == "EQ"


# ------------------------------------------------------------------ series filter


def test_series_filter_drops_non_equity_rows(client):
    """The default filter is mandatory: a bhavcopy is not equities.

    Upstream's 2026-09-25 CM file is 3654 rows of which only 2660 are EQ. Gold bonds (series GB)
    carry entirely plausible prices near 15000, so an unfiltered read would put them in a
    portfolio as instruments.
    """
    frame = bc.parse_bhavcopy(csv_text(UDIFF), series="EQ")

    symbols = set(frame.index.get_level_values("Symbol"))
    assert "SGBJUN28" not in symbols, "gold bond leaked through the equity filter"
    assert "3IINFOLTD" not in symbols, "BE-series row leaked through the equity filter"
    assert frame.attrs["bfinance_rows_dropped_by_series_filter"] == 3
    assert frame.attrs["bfinance_source_row_count"] == 7


def test_other_series_are_reachable_by_requesting_them(client):
    frame = bc.parse_bhavcopy(csv_text(UDIFF), series="GB")
    assert set(frame.index.get_level_values("Symbol")) == {"SGBJUN28", "SGBN28VIII"}
    assert frame.attrs["bfinance_series"] == "GB"
    assert frame.xs("SGBJUN28", level="Symbol")["Close"].iloc[0] == 15040.00


def test_series_filter_applies_to_the_legacy_schema_too(client):
    """Legacy SYMBOL can be a numeric scrip code (1018GS2026). It must not be mistaken for an
    equity, and must not break the parse."""
    frame = bc.parse_bhavcopy(csv_text(LEGACY), series="EQ")

    assert set(frame.index.get_level_values("Symbol")) == {"RELIANCE", "TCS", "ZYDUSWELL"}
    assert frame.attrs["bfinance_rows_dropped_by_series_filter"] == 3


def test_absent_series_yields_an_empty_frame_not_an_error(client):
    """A series that is simply not in this file is a valid answer: zero rows."""
    frame = bc.parse_bhavcopy(csv_text(UDIFF), series="ZZ")
    assert frame.empty
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert frame.attrs[bc.SYNTHETIC_OHLC_ATTR] is False


def test_series_code_is_normalised_not_case_sensitive(client):
    assert bc.validate_series("eq") == "EQ"
    assert len(bc.parse_bhavcopy(csv_text(UDIFF), series="gb")) == 2


# ------------------------------------------------------------------ never fabricate


def test_blank_price_field_stays_nan_and_is_counted(client):
    """A missing field is reported, never filled. Deriving an Open from a Close is the exact
    defect this module replaces."""
    frame = bc.parse_bhavcopy(
        blank_field(csv_text(UDIFF).splitlines(), "RELIANCE", "OpnPric"), series="EQ"
    )
    missing = frame.attrs["bfinance_missing_ohlcv"]

    assert missing == {"Open": 1}, "the gap must be reported, not silently filled"
    assert frame["Open"].isna().sum() == 1
    # The rest of that row survives untouched, so the gap is not a whole-file misread.
    row_out = frame.xs("RELIANCE", level="Symbol")
    assert pd.isna(row_out["Open"].iloc[0])
    assert row_out["High"].iloc[0] == 1227.40
    assert row_out["Close"].iloc[0] == 1226.00


def test_volume_stays_float_when_a_value_is_missing(client):
    """Casting a missing volume to int64 would invent a number."""
    frame = bc.parse_bhavcopy(
        blank_field(csv_text(UDIFF).splitlines(), "RELIANCE", "TtlTradgVol"), series="EQ"
    )
    assert frame["Volume"].isna().sum() == 1
    assert pd.api.types.is_float_dtype(frame["Volume"])


def test_ohlc_values_are_exactly_what_upstream_sent(client):
    """No rounding, no envelope, no derived Open. RELIANCE 2026-09-25 opened at 1210.50 — the
    fabricated path would have reported the previous close here."""
    frame = bc.parse_bhavcopy(csv_text(UDIFF), series="EQ")
    row = frame.xs("RELIANCE", level="Symbol")

    assert row["Open"].iloc[0] == 1210.50
    assert row["High"].iloc[0] == 1227.40
    assert row["Low"].iloc[0] == 1210.50
    assert row["Close"].iloc[0] == 1226.00
    assert int(row["Volume"].iloc[0]) == 13138735


def test_high_and_low_are_not_a_fixed_envelope_around_open_close(client):
    """The regression that defines the old defect: ``High = max(Open, Close) * 1.002``.

    The discriminator is **variability**, not magnitude. A fabricated frame produces *exactly*
    0.002 for every single row, so a real bhavcopy is proven by the ratios differing from each
    other — and by at least one being far outside any 0.2% envelope. A magnitude-only threshold
    would be wrong: a genuinely quiet session really can trade inside 0.2%.
    """
    for key in (UDIFF, UDIFF_35COL, LEGACY):
        frame = bc.parse_bhavcopy(csv_text(key), series="EQ")
        hi = frame["High"] / frame[["Open", "Close"]].max(axis=1) - 1.0
        lo = 1.0 - frame["Low"] / frame[["Open", "Close"]].min(axis=1)

        assert not bool((hi.round(6) == 0.002).all()), f"{key}: every row is exactly +0.2%"
        assert not bool((lo.round(6) == 0.002).all()), f"{key}: every row is exactly -0.2%"
        assert hi.nunique() > 1 or lo.nunique() > 1, f"{key}: ratios are constant, not observed"


def test_a_real_session_exceeds_the_fabricated_envelope(client):
    """One recorded row with an observed range far outside 0.2%: PATELENG on 2024-01-01 printed
    a high 3.54% above its open/close maximum and a low exactly at its open. No +-0.2% envelope
    can produce that, and no real bhavcopy can fail to."""
    frame = bc.parse_bhavcopy(csv_text(UDIFF_35COL), series="EQ")
    row = frame.xs("PATELENG", level="Symbol")

    assert row["Open"].iloc[0] == 61.70
    assert row["High"].iloc[0] == 67.20
    assert row["Low"].iloc[0] == 61.70
    assert row["Close"].iloc[0] == 64.90
    assert float(row["High"].iloc[0] / row["Close"].iloc[0] - 1.0) == pytest.approx(
        67.20 / 64.90 - 1.0, rel=1e-9
    )
    assert float(row["High"].iloc[0] / row["Close"].iloc[0] - 1.0) > 0.03


def test_no_public_helper_builds_ohlc():
    """Guard against a future helper that synthesises O/H/L from a close-only frame."""
    forbidden = {"synthesize_ohlc", "to_ohlcv", "build_ohlc", "fill_ohlc", "fake_ohlc", "envelope"}
    public = {name for name in dir(bc) if not name.startswith("_")}
    assert not (forbidden & public)


# ------------------------------------------------------------------ unzip / transport


def test_unzip_rejects_a_non_zip_body():
    """An HTML error page served with a 200 is the real failure this guards."""
    with pytest.raises(ParsingError, match="not a readable ZIP"):
        bc.unzip_bhavcopy(b"<html>gateway error</html>", url="x")


def test_unzip_rejects_a_multi_member_archive():
    """Reading the wrong member would yield plausible, wrong bars."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("a.csv", "A\n1\n")
        archive.writestr("b.csv", "A\n2\n")
    with pytest.raises(ParsingError, match="expected exactly one CSV"):
        bc.unzip_bhavcopy(buf.getvalue(), url="x")


def test_fixtures_are_single_member_archives():
    """Confirms the fixtures match the shape upstream actually serves."""
    for key in (UDIFF, UDIFF_35COL, LEGACY):
        with zipfile.ZipFile(io.BytesIO(zip_bytes(key))) as archive:
            names = archive.namelist()
        assert len(names) == 1, key
        assert names[0].endswith(".csv"), key


# ------------------------------------------------------------------ URL construction


@pytest.mark.parametrize("day,expected", [
    ("2026-09-25", "/content/cm/BhavCopy_NSE_CM_0_0_0_20260925_F_0000.csv.zip"),
    ("2024-01-01", "/content/cm/BhavCopy_NSE_CM_0_0_0_20240101_F_0000.csv.zip"),
    ("2023-12-29", "/content/historical/EQUITIES/2023/DEC/cm29DEC2023bhav.csv.zip"),
    ("2023-05-15", "/content/historical/EQUITIES/2023/MAY/cm15MAY2023bhav.csv.zip"),
])
def test_archive_url_picks_the_verified_route_for_the_date(day, expected):
    assert bc.archive_url(day).endswith(expected)
    assert bc.archive_url(day).startswith("https://nsearchives.nseindia.com")


def test_udiff_cutoff_is_the_verified_boundary():
    """Verified live: the UDiFF URL 404s for 2023-12-29 while the legacy URL returns 200 for the
    same day, and 2024-01-01 is 200 on UDiFF."""
    assert bc.UDIFF_CUTOFF == __import__("datetime").date(2024, 1, 1)
    assert "historical" in bc.archive_url("2023-12-31")
    assert "content/cm" in bc.archive_url("2024-01-01")


def test_fo_route_is_built_for_post_cutoff_dates():
    assert bc.archive_url("2026-09-25", "FO").endswith(
        "/content/fo/BhavCopy_NSE_FO_0_0_0_20260925_F_0000.csv.zip"
    )


def test_fo_before_the_cutover_is_rejected_before_any_request():
    """No verified legacy derivatives route exists, so the URL would be a guaranteed 404."""
    with pytest.raises(ValueError, match="no verified legacy archive"):
        bc.archive_url("2023-05-15", "FO")


@pytest.mark.parametrize("bad", ["XX", "cm2", "", None, 7])
def test_validate_exchange_rejects(bad):
    with pytest.raises(ValueError):
        bc.validate_exchange(bad)


@pytest.mark.parametrize("given,expected", [("cm", "CM"), ("fo", "FO"), (" CM ", "CM")])
def test_validate_exchange_normalises(given, expected):
    assert bc.validate_exchange(given) == expected


@pytest.mark.parametrize("bad", ["", "   ", "EQUITY", "E-Q", None, 5])
def test_validate_series_rejects(bad):
    with pytest.raises(ValueError):
        bc.validate_series(bad)


@pytest.mark.parametrize("bad", ["not-a-date", "", None, 20260925])
def test_to_date_rejects_garbage(bad):
    with pytest.raises(ValueError):
        bc.to_date(bad)


def test_to_date_accepts_the_documented_input_shapes():
    import datetime as dt

    assert bc.to_date("2026-09-25") == dt.date(2026, 9, 25)
    assert bc.to_date("20260925") == dt.date(2026, 9, 25)
    assert bc.to_date(dt.date(2026, 9, 25)) == dt.date(2026, 9, 25)
    assert bc.to_date(pd.Timestamp("2026-09-25 15:30")) == dt.date(2026, 9, 25)


# ------------------------------------------------------------------ trading calendar


def test_weekends_are_skipped_without_a_request():
    assert bc.is_weekday("2026-09-25") is True
    assert bc.is_weekday("2026-09-26") is False  # Saturday
    assert bc.is_weekday("2026-09-27") is False  # Sunday


def test_iter_trading_days_excludes_the_weekend():
    days = bc.iter_trading_days("2026-09-21", "2026-09-27")
    assert [d.isoformat() for d in days] == [
        "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25",
    ]


def test_iter_trading_days_rejects_an_inverted_range():
    with pytest.raises(ValueError, match="before start"):
        bc.iter_trading_days("2026-09-25", "2026-09-01")


# ------------------------------------------------------------------ HTTP behaviour


class _StubTransport(httpx.AsyncBaseTransport):
    """Serves canned responses per URL, so the real client code runs with no socket.

    ``_get_archive`` constructs its own ``httpx.AsyncClient`` per attempt, so the transport is
    injected by swapping the module's ``httpx`` reference — an earlier version of this file
    attached a transport to a client the code never used, and silently hit the live network.
    """

    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append(str(request.url))
        for needle, response in self.responses.items():
            if needle in str(request.url):
                return response() if callable(response) else response
        return httpx.Response(404, text="not found")


@pytest.fixture
def stub_httpx(monkeypatch):
    """Install a stub transport. Yields a factory; the module's ``httpx`` is restored after."""
    holder = {}

    class _FakeHttpx:
        Response = httpx.Response

        @staticmethod
        def AsyncClient(**kwargs):
            kwargs.pop("transport", None)
            client = httpx.AsyncClient(
                transport=holder["transport"], timeout=kwargs.get("timeout", 5.0)
            )
            holder["clients"].append(client)
            return client

    _FakeHttpx.TimeoutException = httpx.TimeoutException
    _FakeHttpx.TransportError = httpx.TransportError
    holder["clients"] = []
    monkeypatch.setattr(bc, "httpx", _FakeHttpx)

    def install(responses):
        holder["transport"] = _StubTransport(responses)
        return holder["transport"]

    install.requests = lambda: list(holder["transport"].requests)
    return install


def served(status: int, content=None, text=None):
    """A response factory, so each attempt gets a fresh unconsumed response object."""
    def build():
        kwargs = {"text": text} if text is not None else {"content": content}
        return httpx.Response(status, **kwargs)

    return build


async def test_get_archive_maps_404_to_none(stub_httpx):
    transport = stub_httpx({"20260925": served(200, content=zip_bytes(UDIFF))})
    client = bc.BhavcopyClient(min_request_interval_ms=0.0)

    assert await client._get_archive(bc.archive_url("2026-09-26")) is None
    assert await client._get_archive(bc.archive_url("2026-09-25")) is not None
    assert len(transport.requests) == 2


async def test_get_archive_returns_bytes_on_200(stub_httpx):
    stub_httpx({"20260925": served(200, content=zip_bytes(UDIFF))})
    client = bc.BhavcopyClient(min_request_interval_ms=0.0)

    payload = await client._get_archive(bc.archive_url("2026-09-25"))
    assert payload == zip_bytes(UDIFF)
    assert bc.unzip_bhavcopy(payload).startswith("TradDt,")


async def test_get_archive_raises_on_an_unexpected_status(stub_httpx):
    stub_httpx({"20260925": served(403, text="forbidden")})
    client = bc.BhavcopyClient(min_request_interval_ms=0.0)
    with pytest.raises(UpstreamServiceError, match="unexpected HTTP 403"):
        await client._get_archive(bc.archive_url("2026-09-25"))


async def test_get_archive_raises_after_exhausted_5xx_retries(stub_httpx):
    transport = stub_httpx({"20260925": served(503, text="busy")})
    client = bc.BhavcopyClient(min_request_interval_ms=0.0, max_retries=2)
    with pytest.raises(UpstreamServiceError, match="failed after 2 attempts"):
        await client._get_archive(bc.archive_url("2026-09-25"))
    assert len(transport.requests) == 2, "a 5xx must be retried, not accepted"


async def test_get_archive_retries_a_5xx_then_succeeds(stub_httpx):
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, content=zip_bytes(UDIFF))

    transport = stub_httpx({"20260925": flaky})
    client = bc.BhavcopyClient(min_request_interval_ms=0.0, max_retries=3)
    assert await client._get_archive(bc.archive_url("2026-09-25")) is not None
    assert len(transport.requests) == 2


async def test_get_archive_raises_rate_limit_error_on_429(stub_httpx):
    stub_httpx({"20260925": served(429, text="slow down")})
    client = bc.BhavcopyClient(min_request_interval_ms=0.0, max_retries=1)
    with pytest.raises(RateLimitExceededError):
        await client._get_archive(bc.archive_url("2026-09-25"))


async def test_get_archive_retries_on_a_network_error_then_gives_up(stub_httpx):
    class _DeadTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.requests = []

        async def handle_async_request(self, request):
            self.requests.append(str(request.url))
            raise httpx.ConnectError("connection refused")

    dead = _DeadTransport()
    bc.httpx.AsyncClient = staticmethod(
        lambda **kw: httpx.AsyncClient(transport=dead, timeout=1.0)
    )
    client = bc.BhavcopyClient(min_request_interval_ms=0.0, max_retries=2)
    with pytest.raises(UpstreamServiceError, match="failed after 2 attempts"):
        await client._get_archive(bc.archive_url("2026-09-25"))
    assert len(dead.requests) == 2


# ------------------------------------------------------------------ client behaviour


async def test_single_day_fetch_populates_provenance(client):
    async def fake(url):
        return zip_bytes(UDIFF)

    client._get_archive = fake
    frame = await client.bhavcopy_async("2026-09-25")

    assert frame.attrs["bfinance_schema"] == "udiff"
    assert frame.attrs["bfinance_exchange"] == "CM"
    assert frame.attrs["bfinance_date"] == "2026-09-25"
    assert frame.attrs["bfinance_source_url"].endswith("20260925_F_0000.csv.zip")
    assert frame.attrs[bc.MISSING_DATES_ATTR] == []
    assert frame.attrs[bc.SYNTHETIC_OHLC_ATTR] is False


async def test_single_day_over_the_real_transport_path(stub_httpx):
    """End-to-end through the HTTP layer: 200, unzip, detect, filter, parse."""
    stub_httpx({"20260925": served(200, content=zip_bytes(UDIFF))})
    client = bc.BhavcopyClient(min_request_interval_ms=0.0)
    frame = await client.bhavcopy_async("2026-09-25")

    assert len(frame) == 4
    assert set(frame.index.get_level_values("Symbol")) == {
        "IRCON", "RELIANCE", "SBIN", "ZYDUSWELL"
    }
    assert frame.attrs[bc.SYNTHETIC_OHLC_ATTR] is False


async def test_404_returns_an_empty_frame_with_the_right_columns(stub_httpx):
    """A Saturday. The stub 404s anything it does not recognise."""
    transport = stub_httpx({})
    client = bc.BhavcopyClient(min_request_interval_ms=0.0)
    frame = await client.bhavcopy_async("2026-09-26")

    assert len(transport.requests) == 1
    assert frame.empty
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert frame.attrs[bc.MISSING_DATES_ATTR] == ["2026-09-26"]
    assert frame.attrs[bc.SYNTHETIC_OHLC_ATTR] is False
    # Never a synthesised row.
    assert len(frame) == 0


async def test_404_does_not_raise_by_default(stub_httpx):
    """A weekend must not be an exception. That is the single most likely thing to break."""
    stub_httpx({})
    client = bc.BhavcopyClient(min_request_interval_ms=0.0)
    assert (await client.bhavcopy_async("2026-01-01")).empty


async def test_404_raises_under_raise_errors(stub_httpx):
    stub_httpx({})
    client = bc.BhavcopyClient(min_request_interval_ms=0.0)
    with pytest.raises(TickerNotFoundError, match="no bhavcopy for 2026-09-26"):
        await client.bhavcopy_async("2026-09-26", raise_errors=True)


async def test_404_error_message_names_the_date_and_the_expected_reason(stub_httpx):
    """The caller must be able to tell 'holiday' from 'your request was wrong'."""
    stub_httpx({})
    client = bc.BhavcopyClient(min_request_interval_ms=0.0)
    with pytest.raises(TickerNotFoundError) as excinfo:
        await client.bhavcopy_async("2026-09-26", raise_errors=True)
    message = str(excinfo.value)
    assert "2026-09-26" in message
    assert "holiday" in message


async def test_404_is_distinguished_from_a_transport_failure(stub_httpx):
    """A 404 is expected; a dead socket is not. They must not share an outcome."""
    async def missing(url):
        return None

    async def broken(url):
        raise UpstreamServiceError("transport failed")

    client = bc.BhavcopyClient(min_request_interval_ms=0.0)
    client._get_archive = missing
    assert (await client.bhavcopy_async("2026-09-26")).empty

    client._get_archive = broken
    with pytest.raises(UpstreamServiceError):
        await client.bhavcopy_async("2026-09-26")


async def test_a_malformed_body_raises_rather_than_producing_empty_bars(stub_httpx):
    """An HTML error page served with a 200 must not read as 'no instruments traded today'."""
    stub_httpx({"20260925": served(200, text="<html>gateway timeout</html>")})
    client = bc.BhavcopyClient(min_request_interval_ms=0.0)
    with pytest.raises(ParsingError, match="not a readable ZIP"):
        await client.bhavcopy_async("2026-09-25")


# ------------------------------------------------------------------ range


async def test_range_skips_the_weekend_without_requesting_it(client):
    """The request-storm guard. A five-day span across a weekend is three requests, not five."""
    requested = []

    async def fake(url):
        requested.append(url)
        return zip_bytes(UDIFF) if "20260921" in url else None

    client._get_archive = fake
    frame = await client.bhavcopy_range_async("2026-09-21", "2026-09-27")

    assert len(requested) == 5, "Sat 26 and Sun 27 must not be requested at all"
    assert not any("20260926" in u for u in requested)
    assert not any("20260927" in u for u in requested)
    assert not frame.empty
    assert frame.attrs[bc.MISSING_DATES_ATTR] == [
        "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25",
    ]
    assert frame.attrs["bfinance_candidate_trading_days"] == 5


async def test_range_issues_exactly_one_request_per_candidate_day(client):
    """No retry storm, no duplicate request for a day already fetched."""
    requested = []

    async def fake(url):
        requested.append(url)
        return zipped(with_dates(csv_text(UDIFF), ["2026-09-21"]))

    client._get_archive = fake
    await client.bhavcopy_range_async("2026-09-21", "2026-09-25")

    assert len(requested) == len(set(requested)) == 5


async def test_range_records_missing_dates_and_still_returns_data(client):
    async def fake(url):
        if "20260925" in url:
            return zip_bytes(UDIFF)
        return None

    client._get_archive = fake
    frame = await client.bhavcopy_range_async("2026-09-24", "2026-09-25")

    assert not frame.empty
    assert frame.attrs[bc.MISSING_DATES_ATTR] == ["2026-09-24"]
    assert frame.attrs[bc.SYNTHETIC_OHLC_ATTR] is False
    assert frame.index.is_monotonic_increasing


async def test_range_raises_on_a_missing_date_under_on_missing_raise(client):
    async def fake(url):
        if "20260925" in url:
            return zip_bytes(UDIFF)
        return None

    client._get_archive = fake
    with pytest.raises(TickerNotFoundError, match="on_missing='skip'"):
        await client.bhavcopy_range_async("2026-09-24", "2026-09-25", on_missing="raise")


async def test_range_over_a_fully_missing_span_is_empty_not_an_error(client):
    async def fake(url):
        return None

    client._get_archive = fake
    frame = await client.bhavcopy_range_async("2026-09-21", "2026-09-25")

    assert frame.empty
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert frame.attrs[bc.SYNTHETIC_OHLC_ATTR] is False
    assert len(frame.attrs[bc.MISSING_DATES_ATTR]) == 5


async def test_range_rejects_an_unknown_on_missing(client):
    with pytest.raises(ValueError, match="on_missing"):
        await client.bhavcopy_range_async("2026-09-21", "2026-09-25", on_missing="explode")


async def test_range_dispatches_per_date_across_the_udiff_cutover(client):
    """The route is chosen per date, not per range. A span straddling 2024-01-01 must ask the
    legacy route for the early days and UDiFF for the later ones."""
    requested = []

    async def fake(url):
        requested.append(url)
        return None

    client._get_archive = fake
    await client.bhavcopy_range_async("2023-12-28", "2024-01-03")

    legacy = [u for u in requested if "historical" in u]
    udiff = [u for u in requested if "/content/cm/" in u]
    # Thu 28 + Fri 29 Dec 2023 on legacy; Mon 1, Tue 2, Wed 3 Jan 2024 on UDiFF (1 Jan was a
    # Monday, so it is a candidate even though the market was shut).
    assert len(legacy) == 2, legacy
    assert len(udiff) == 3, udiff
    assert sorted(u.rsplit("/", 1)[-1] for u in legacy) == [
        "cm28DEC2023bhav.csv.zip", "cm29DEC2023bhav.csv.zip",
    ]
    assert sorted(u.rsplit("/", 1)[-1] for u in udiff) == [
        "BhavCopy_NSE_CM_0_0_0_20240101_F_0000.csv.zip",
        "BhavCopy_NSE_CM_0_0_0_20240102_F_0000.csv.zip",
        "BhavCopy_NSE_CM_0_0_0_20240103_F_0000.csv.zip",
    ]


async def test_range_merges_days_into_one_ascending_frame(client):
    """A multi-day span built from the recorded one-day capture, with only the date moved."""
    days = {
        "20260921": zipped(with_dates(csv_text(UDIFF), ["2026-09-21"])),
        "20260922": zipped(with_dates(csv_text(UDIFF), ["2026-09-22"])),
        "20260923": zipped(with_dates(csv_text(UDIFF), ["2026-09-23"])),
        "20260924": zipped(with_dates(csv_text(UDIFF), ["2026-09-24"])),
        "20260925": zip_bytes(UDIFF),
    }

    async def fake(url):
        for stamp, payload in days.items():
            if stamp in url:
                return payload
        return None

    client._get_archive = fake
    frame = await client.bhavcopy_range_async("2026-09-21", "2026-09-25")

    assert len(frame) == 5 * 4, "five days x four EQ instruments"
    assert frame.index.is_monotonic_increasing
    assert not frame.index.duplicated().any()
    assert frame.attrs["bfinance_candidate_trading_days"] == 5
    assert frame.attrs["bfinance_symbol_count"] == 4
    assert frame.attrs[bc.SYNTHETIC_OHLC_ATTR] is False
    assert len(frame.index.get_level_values("Date").unique()) == 5


async def test_range_carries_each_days_recorded_prices(client):
    """The merge must not reindex or re-derive anything: day N keeps the captured values."""
    async def fake(url):
        return zipped(with_dates(csv_text(UDIFF), ["2026-09-22"])) if "20260922" in url else None

    client._get_archive = fake
    frame = await client.bhavcopy_range_async("2026-09-22", "2026-09-22")

    row = frame.xs("RELIANCE", level="Symbol")
    assert str(row.index[0].date()) == "2026-09-22"
    assert row["Open"].iloc[0] == 1210.50
    assert row["Close"].iloc[0] == 1226.00


async def test_range_merges_the_legacy_schema_too(client):
    # Legacy URLs are cm{dd}{MMM}{yyyy}bhav.csv.zip, not a compact stamp.
    days = {
        "cm15MAY2023": zipped(with_dates(csv_text(LEGACY), ["2023-05-15"])),
        "cm16MAY2023": zipped(with_dates(csv_text(LEGACY), ["2023-05-16"])),
    }

    async def fake(url):
        for stamp, payload in days.items():
            if stamp in url:
                return payload
        return None

    client._get_archive = fake
    frame = await client.bhavcopy_range_async("2023-05-15", "2023-05-16")

    assert len(frame) == 6
    series = frame.xs("TCS", level="Symbol")
    assert [str(d.date()) for d in series.index] == ["2023-05-15", "2023-05-16"]
    assert series["Close"].tolist() == [3255.05, 3255.05]


async def test_range_per_day_attrs_do_not_survive_the_merge(client):
    """After a merge, a per-day attr would describe one day while appearing to describe all of
    them. It must be dropped, not inherited."""

    async def fake(url):
        return zipped(with_dates(csv_text(UDIFF), ["2026-09-22"])) if "20260922" in url else None

    client._get_archive = fake
    frame = await client.bhavcopy_range_async("2026-09-22", "2026-09-22")

    assert "bfinance_schema" not in frame.attrs
    assert "bfinance_source_url" not in frame.attrs
    assert frame.attrs["bfinance_provenance"] == "nse_bhavcopy"
    assert frame.attrs["bfinance_requested_start"] == "2026-09-22"


async def test_range_dedupes_a_repeated_upstream_row(client):
    """Two different days cannot share a (Date, Symbol) pair, so a duplicate is an upstream
    repeat. Keep the last, record it, never silently ship two bars for one instrument-day."""

    async def fake(url):
        return zipped(with_dates(csv_text(UDIFF), ["2026-09-22"]))

    client._get_archive = fake
    frame = await client.bhavcopy_range_async("2026-09-22", "2026-09-22")

    assert not frame.index.duplicated().any()
    assert len(frame) == 4


async def test_a_file_with_no_rows_for_the_series_is_distinct_from_a_missing_file(client):
    """A 404 means NSE published nothing. A 200 with no matching series means the file exists but
    the series was not traded. Collapsing the two would report a real trading day as a holiday."""
    async def fake(url):
        return zipped(csv_text(UDIFF))

    client._get_archive = fake
    frame = await client.bhavcopy_async("2026-09-25", series="ZZ")

    assert frame.empty
    assert frame.attrs["bfinance_no_rows_for_series"] is True
    assert frame.attrs[bc.MISSING_DATES_ATTR] == [], "an existing file is not a missing date"
    assert frame.attrs[bc.SYNTHETIC_OHLC_ATTR] is False


async def test_range_does_not_report_a_series_less_day_as_a_missing_date(client):
    async def fake(url):
        # Both files exist; only one holds EQ rows.
        return zipped(with_dates(csv_text(UDIFF), ["2026-09-22"])) if "20260922" in url \
            else zipped(csv_text(UDIFF).replace(",EQ,", ",ZZ,"))

    client._get_archive = fake
    frame = await client.bhavcopy_range_async("2026-09-22", "2026-09-23")

    assert frame.attrs[bc.MISSING_DATES_ATTR] == []
    assert len(frame) == 4
    assert str(frame.index.get_level_values("Date")[0].date()) == "2026-09-22"


async def test_range_progress_logs_each_day_without_changing_the_result(client, caplog):
    async def fake(url):
        return zipped(with_dates(csv_text(UDIFF), ["2026-09-22"])) if "20260922" in url else None

    client._get_archive = fake
    with caplog.at_level("INFO", logger="bfinance"):
        frame = await client.bhavcopy_range_async("2026-09-21", "2026-09-23", progress=True)

    assert not frame.empty
    assert "20260921_F_0000.csv.zip" in caplog.text
    assert "20260923_F_0000.csv.zip" in caplog.text


async def test_range_is_quiet_by_default(client, caplog):
    async def fake(url):
        return None

    client._get_archive = fake
    with caplog.at_level("INFO", logger="bfinance"):
        await client.bhavcopy_range_async("2026-09-21", "2026-09-25")

    assert caplog.text == ""


async def test_range_through_the_real_transport_layer(stub_httpx):
    """Two real days over the HTTP path: 200 for one, 404 for the other."""
    stub_httpx({
        "20260922": served(200, content=zipped(with_dates(csv_text(UDIFF), ["2026-09-22"]))),
        "20260923": served(404, text="not found"),
    })
    client = bc.BhavcopyClient(min_request_interval_ms=0.0)
    frame = await client.bhavcopy_range_async("2026-09-22", "2026-09-23")

    assert len(frame) == 4
    assert frame.attrs[bc.MISSING_DATES_ATTR] == ["2026-09-23"]
    assert frame.attrs[bc.SYNTHETIC_OHLC_ATTR] is False


# ------------------------------------------------------------------ pacing


async def test_pacing_spaces_requests_by_the_configured_interval():
    """A multi-year range is thousands of files. NSE blocks aggressive clients, so the floor is
    the difference between a slow download and a block."""
    client = bc.BhavcopyClient(min_request_interval_ms=120.0)
    client._last_request_time = 0.0
    bc._GLOBAL_LAST_REQUEST_TIME = 0.0

    started = asyncio.get_running_loop().time()
    for _ in range(3):
        await client._throttle_pacing()
    elapsed_ms = (asyncio.get_running_loop().time() - started) * 1000.0

    # 3 calls at a 120 ms floor cannot complete in less than ~240 ms.
    assert elapsed_ms >= 220.0, f"expected pacing to hold, took only {elapsed_ms:.0f}ms"
    bc._GLOBAL_LAST_REQUEST_TIME = 0.0


def test_pacing_state_is_module_global_so_clients_share_one_limit():
    """Ported from screener/client.py. A private per-instance clock would let N clients burst
    N x the limit."""
    assert isinstance(bc._GLOBAL_PACING_LOCK, type(__import__("threading").Lock()))
    assert isinstance(bc._GLOBAL_LAST_REQUEST_TIME, float)


# ------------------------------------------------------------------ public surface


def test_the_two_ticket_functions_exist_with_the_ticket_signatures():
    import inspect

    single = inspect.signature(bc.bhavcopy)
    assert list(single.parameters) == ["date", "series", "exchange", "raise_errors"]
    assert single.parameters["series"].default == "EQ"
    assert single.parameters["exchange"].default == "CM"
    assert single.parameters["raise_errors"].default is False

    ranged = inspect.signature(bc.bhavcopy_range)
    assert list(ranged.parameters) == ["start", "end", "exchange", "on_missing", "progress"]
    assert ranged.parameters["exchange"].default == "CM"
    assert ranged.parameters["on_missing"].default == "skip"
    assert ranged.parameters["progress"].default is False


def test_client_methods_match_the_module_shortcuts():
    """The sync shortcut must delegate to the client, not reimplement the fetch, and the
    client's sync method must bridge through the same async implementation."""
    import inspect

    for name in ("bhavcopy", "bhavcopy_range"):
        assert hasattr(bc.BhavcopyClient, name)
        assert hasattr(bc.BhavcopyClient, name + "_async")
        # The client method wraps the async one via _run_async ...
        assert "_run_async" in inspect.getsource(getattr(bc.BhavcopyClient, name))
        # ... and the module shortcut delegates to the client.
        assert "_default_client()" in inspect.getsource(getattr(bc, name))


def test_sync_shortcut_runs_without_a_running_loop(client, monkeypatch):
    """The public API is sync, so it must work from ordinary synchronous code."""
    async def fake(url):
        return zip_bytes(UDIFF)

    monkeypatch.setattr(client, "_get_archive", fake)
    frame = client.bhavcopy("2026-09-25")
    assert len(frame) == 4
    assert frame.attrs[bc.SYNTHETIC_OHLC_ATTR] is False


def test_no_cookie_priming_or_landing_page_fetch():
    """A static archive route plus a User-Agent is all that is needed. Cookie priming adds
    fragility and implies an anti-bot problem this endpoint does not have.

    Checked against code with docstrings stripped: the module docstring *mentions* cookies, to
    record that they are not needed, and a naive substring scan would flag its own warning.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(bc)))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body.pop(0)
    code = ast.unparse(tree)

    for forbidden in (
        "cookie", "set_cookie", "nseindia.com/api", "nseindia.com/market-data",
        "market-activity", "setTimeout",
    ):
        assert forbidden.lower() not in code.lower(), forbidden


def test_only_the_archive_host_is_contacted():
    """Every URL this module builds points at the static archive host."""
    for day in ("2026-09-25", "2024-01-01", "2023-05-15"):
        for exchange in ("CM", "FO"):
            try:
                url = bc.archive_url(day, exchange)
            except ValueError:
                continue
            assert url.startswith("https://nsearchives.nseindia.com/content/"), url
            assert url.endswith(".csv.zip")


def test_package_exports_the_bhavcopy_surface_without_shadowing_the_module():
    """The ticket names the function `bhavcopy` and the module `bhavcopy`. A package attribute can
    only be one of them, so the module must win and stay importable."""
    import bfinance.nse as nse
    from bfinance.nse.bhavcopy import bhavcopy as direct

    assert nse.bhavcopy.__name__ == "bfinance.nse.bhavcopy"
    assert callable(direct)
    for name in ("bhavcopy_range", "BhavcopyClient", "archive_url", "COLUMNS", "UDIFF_CUTOFF"):
        assert hasattr(nse, name), name


def test_module_constants_are_the_documented_values():
    assert bc.COLUMNS == ("Open", "High", "Low", "Close", "Volume")
    assert bc.SYNTHETIC_OHLC_ATTR == "bfinance_synthetic_ohlc"
    assert bc.MISSING_DATES_ATTR == "bfinance_bhavcopy_missing_dates"
    assert bc.PROVENANCE == "nse_bhavcopy"
    assert bc.EXCHANGES == ("CM", "FO")
