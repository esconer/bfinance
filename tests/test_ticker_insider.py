"""``Ticker.insider_transactions``: the vendor's insider/SAST table, verbatim.

The member is deliberately the least derived one on the surface. It hands back the page's own
thirteen columns, with the page's own formatted strings, and renames nothing: the recorded
``Quantity`` cell is ``"566,000"``, not ``566000``, because parsing it would publish a number the
vendor wrote as text and would make the column's unit this package's guess rather than the page's
statement. The one thing the member adds is a measured count and the route's cap, in ``attrs``.

**The route has no period axis.** It serves one page, server-side capped at exactly 500 rows
(``bfinance.trendlyne.client.INSIDER_ROW_CAP``); the recorded pull covered 2020-02-12 to
2025-06-16. A frame below the cap is not described as complete and one at the cap is described as
truncated. Both the count and the cap travel in ``attrs`` so a caller can see which case they hold
without a second request.

**One request, then a memo.** The frame is cached on the Ticker, and the memo is copied on read so
a caller who edits their copy cannot change what the next caller receives. A failure is not
memoised: the recorded error goes to ``data_warnings``, the member returns an empty frame, and the
next access retries.
"""

from __future__ import annotations

import httpx
import pytest

from bfinance.ticker import Ticker
from bfinance.trendlyne import client as tl

# The mock-transport seam and the shared recorder are imported rather than copied: two copies of a
# request stub drift into testing different things. `tests/` is not a package, so the modules are
# imported by bare name exactly as pytest imports them.
from test_trendlyne_core import text
from test_ticker_trendlyne_sections import SYMBOL, Recorder, _install_bridge

#: The route, and the recording of it. 174 data rows; the recorded page's own table is the only
#: source this member reads.
INSIDER = "/equity/insider-trading-sast/all/"
FIXTURE = "insider_trading_tcs.html"

COLUMNS = [
    "Client Name",
    "Client Category",
    "Action*",
    "Reported To/By Exchange",
    "Quantity",
    "Post Transaction Holding",
    "Traded %",
    "Avg. Price",
    "Value",
    "Period",
    "Regulation (Insider/SAST)",
    "Security Type",
    "Mode",
]


@pytest.fixture
def recorder() -> Recorder:
    return Recorder(
        overrides={INSIDER: lambda request: httpx.Response(200, text=text(FIXTURE))}
    )


@pytest.fixture
def seam(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> Recorder:
    _install_bridge(monkeypatch, recorder)
    return recorder


@pytest.fixture
def on(seam: Recorder) -> Ticker:
    return Ticker(SYMBOL, trendlyne_sections=True, cache_ttl_hours=0)


@pytest.fixture
def off(seam: Recorder) -> Ticker:
    return Ticker(SYMBOL, cache_ttl_hours=0)


# ================================================================ the frame


def test_insider_transactions_is_the_vendors_table_verbatim(on):
    frame = on.insider_transactions

    assert list(frame.columns) == COLUMNS
    assert len(frame) == 174
    # The first and last row, as the page wrote them. A rename, a filter or a numeric coercion
    # anywhere in the member shows up in one of these two dictionaries.
    assert frame.iloc[0].to_dict() == {
        "Client Name": "Life Insurance Corporation of India",
        "Client Category": "Other",
        "Action*": "Acquisition",
        "Reported To/By Exchange": "15 Oct 2025",
        "Quantity": "566,000",
        "Post Transaction Holding": "181,117,256  (5%)",
        "Traded %": "0.01%",
        "Avg. Price": "",
        "Value": "-",
        "Period": "29 Aug 2025 29 Aug 2025",
        "Regulation (Insider/SAST)": "SAST (29(1))",
        "Security Type": "-",
        "Mode": "Market",
    }
    assert frame.iloc[-1].to_dict() == {
        "Client Name": "Tata Sons Limited",
        "Client Category": "Promoter",
        "Action*": "Pledge",
        "Reported To/By Exchange": "24 Dec 2024",
        "Quantity": "3,650,000",
        "Post Transaction Holding": "30,100,000",
        "Traded %": "-",
        "Avg. Price": "",
        "Value": "-",
        "Period": "09 Jan 2014 09 Jan 2014",
        "Regulation (Insider/SAST)": "SAST (Reg31)",
        "Security Type": "-",
        "Mode": "Creation",
    }


def test_the_counts_are_the_pages_strings_not_numbers_this_package_parsed(on):
    """``"566,000"`` is one string cell; ``566000`` would be a number nobody published."""
    frame = on.insider_transactions

    assert isinstance(frame.loc[0, "Quantity"], str)
    assert frame.loc[0, "Quantity"] == "566,000"
    assert frame.loc[0, "Traded %"] == "0.01%"
    assert frame.loc[0, "Value"] == "-"
    # Every column is the page's text; no member of this frame is a numeric dtype.
    assert all(not str(dtype).startswith(("int", "float")) for dtype in frame.dtypes)


def test_the_count_cap_and_truncation_flag_are_measured_on_the_frame(on):
    frame = on.insider_transactions
    attrs = frame.attrs

    assert attrs["bfinance_provenance"] == "trendlyne"
    assert attrs["bfinance_symbol"] == SYMBOL
    assert attrs["bfinance_trendlyne_snapshot_only"] is False
    assert INSIDER in attrs["bfinance_trendlyne_source"]
    assert attrs["bfinance_trendlyne_retrieved_at"]
    assert attrs["bfinance_trendlyne_insider_rows"] == len(frame) == 174
    assert attrs["bfinance_trendlyne_insider_row_cap"] == 500 == tl.INSIDER_ROW_CAP
    assert attrs["bfinance_trendlyne_insider_rows_capped"] is False
    assert "174 row(s)" in attrs["bfinance_trendlyne_note"]
    assert "exactly 500 rows" in attrs["bfinance_trendlyne_note"]
    assert "does NOT mean it is complete" in attrs["bfinance_trendlyne_note"]


# ================================================================ cost and memo


def test_one_request_on_first_access_and_none_after(on, recorder):
    assert recorder.calls == [], "constructing the Ticker touched the vendor"

    on.insider_transactions
    assert len(recorder.calls) == 1
    on.insider_transactions
    on.insider_transactions
    assert len(recorder.calls) == 1


def test_the_request_is_the_bare_page_with_no_period_axis(on, recorder):
    on.insider_transactions

    assert recorder.paths == [
        tl.build_path("insider_trading_page", tl.parse_sitemap(text("sitemap_excerpt.xml"))[SYMBOL])
    ]
    assert "?" not in recorder.paths[0], (
        "the route publishes one page with no period parameter; a query string would be an "
        "invented axis"
    )


def test_the_caller_gets_a_copy_so_mutating_it_cannot_change_the_memo(on):
    first = on.insider_transactions
    first.loc[0, "Quantity"] = "mutated"

    assert on.insider_transactions.loc[0, "Quantity"] == "566,000"


# ================================================================ the flag


def test_the_flag_off_refuses_and_costs_nothing(off, recorder):
    with pytest.raises(NotImplementedError, match="trendlyne_sections=True"):
        off.insider_transactions

    assert recorder.calls == []
    assert off.data_warnings == ()


def test_the_same_property_answers_with_the_flag_on(on):
    """The control on the refusal above: the name is not simply unimplemented."""
    assert not on.insider_transactions.empty


# ================================================================ degradation


def test_a_failed_fetch_returns_an_empty_frame_and_records_the_cause(on, recorder):
    recorder.overrides[INSIDER] = httpx.Response(500, text="server error")

    frame = on.insider_transactions

    assert frame.empty
    assert any("TrendlyneServerError" in w for w in on.data_warnings), on.data_warnings
    spent = len(recorder.calls)
    assert spent >= 1
    # The failure did not fill the memo, so the next access retries instead of latching the block.
    recorder.overrides[INSIDER] = lambda request: httpx.Response(200, text=text(FIXTURE))
    assert len(on.insider_transactions) == 174
    assert len(recorder.calls) == spent + 1, "the second access did not retry the route"


def test_a_served_frame_records_no_warning(on):
    on.insider_transactions

    assert on.data_warnings == (), on.data_warnings
