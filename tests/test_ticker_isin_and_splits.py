"""``Ticker.splits``, ``Ticker.actions`` and ``Ticker.isin`` from NSE's corporate-actions feed.

These three members share one source and therefore one request. That is the point of putting them
in one file: the claim "the first of the three pays one request and the other two read the memo"
is only observable when they are asked together, and so is the failure contract that differs
between them.

**What each member is, in one line.**
- ``splits`` is :func:`bfinance.nse.corporate_actions.splits_from_actions` over the feed's parsed
  frame: the filing's own ``SplitRatio``, selected on the ratio rather than on ``Action`` because a
  composite subject is filed as a dividend while carrying a real factor. The index is localised to
  ``Asia/Kolkata`` here, matching ``dividends``, because the pure reader does not invent a market
  timezone.
- ``actions`` is :func:`~bfinance.nse.corporate_actions.actions_frame` over the same frame: one row
  per declared ex-date, both columns from one feed so the frame is internally consistent. It is
  deliberately **not** ``build_actions(self.dividends, self.splits)``: ``dividends`` is Screener's
  fiscal-year-end approximation and dating a split against it would mix two conventions.
- ``isin`` reads the feed's own ``ISIN`` column and refuses a frame whose rows disagree; when the
  feed has no row it falls back to the archived index memberships, which open no socket.

**The feed is faked at ``CorporateActionsClient``, which is the package's own seam.** No ``httpx``
transport is involved, so no test here can reach NSE even by mistake, and the recorded payloads in
``tests/fixtures/corporate_actions/`` are the same ones the parser tests use. The fake also records
the constructor's keyword arguments, so the transport settings a ``Ticker`` was built with can be
asserted rather than assumed.

**Absence is never a zero.** An empty feed is an empty Series/DataFrame with
``bfinance_actions_source == "nse_corporate_actions"`` and no warning; a failed feed is the same
empty shape with ``"none"``, a ``bfinance_actions_source_detail`` naming the exception, and a
``data_warnings`` entry. The distinction is asserted for each of the three members.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import pytest

from bfinance.nse import corporate_actions as ca
from bfinance.ticker import Ticker
from bfinance import universe as U
from bfinance.utils.exceptions import BFinanceError, UpstreamServiceError

FIXTURES = Path(__file__).parent / "fixtures" / "corporate_actions"

#: Where the fake reports its own URL; carried into ``actions_frame``'s ``bfinance_source_url``.
FEED_URL = "https://nse.test/corporate-actions"

RELIANCE_ISIN = "INE002A01018"
ITC_ISIN = "INE154A01025"


def payload(name: str) -> Any:
    """One recorded NSE payload, byte-for-byte as it was fetched."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeFeed:
    """The NSE client seam: one async method, its arguments logged, failures budgeted.

    ``failing_calls`` is a count rather than a flag so the "a failure is not memoised" test can
    fail the first call and answer the second from the same object. ``calls`` is the request
    counter -- ``len(feed.calls)`` is exactly what the docstrings mean by "one request".
    """

    def __init__(
        self,
        data: Any = None,
        *,
        error: Optional[BaseException] = None,
        failing_calls: int = 0,
    ) -> None:
        self.data = data
        self.error = error
        self.failing_calls = failing_calls
        self.calls: list = []
        #: The kwargs ``Ticker`` built this client with, recorded by the fixture's factory.
        self.client_kwargs: dict = {}

    async def corporate_actions_async(self, symbol=None, *args, **kwargs):
        self.calls.append(symbol)
        if len(self.calls) <= self.failing_calls:
            raise self.error
        return ca.parse_corporate_actions(self.data, symbol=symbol, url=FEED_URL)


@pytest.fixture
def feed(monkeypatch: pytest.MonkeyPatch):
    """Install a :class:`FakeFeed` as ``bfinance.nse.corporate_actions.CorporateActionsClient``.

    The factory form lets one test budget a failure and the next install a clean feed without
    patching the module twice. It also records the construction kwargs on the instance, so a test
    can assert the Ticker's timeout/proxy reached the client.
    """

    def install(
        data: Any = None,
        *,
        error: Optional[BaseException] = None,
        failing_calls: int = 0,
    ) -> FakeFeed:
        instance = FakeFeed(data, error=error, failing_calls=failing_calls)

        def build_client(**kwargs: Any) -> FakeFeed:
            instance.client_kwargs = kwargs
            return instance

        monkeypatch.setattr(ca, "CorporateActionsClient", build_client)
        return instance

    return install


@pytest.fixture
def ticker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Ticker:
    """A Ticker whose archive lookups read ``tmp_path`` and whose cache is off.

    ``BFINANCE_HOME`` is redirected so the ISIN fallback cannot read the machine's real universe
    archive: the answer under test must come from what the test wrote.
    """
    monkeypatch.setenv("BFINANCE_HOME", str(tmp_path))
    return Ticker("RELIANCE", cache_ttl_hours=0)


def _archive_with(tmp_path: Path, symbol: str, isin: str) -> None:
    """Write one archived index snapshot carrying ``symbol``'s recorded ISIN."""
    archive = U.UniverseArchive(tmp_path / "universe")
    frame = U.parse_constituents(
        "Company Name,Industry,Symbol,Series,ISIN Code\n"
        f"Test Company Ltd.,Misc,{symbol},EQ,{isin}\n",
        index="nifty50",
        url="fixture",
        retrieved_utc="2026-09-27T20:44:25+00:00",
    )
    archive.record(
        "nifty50",
        frame,
        retrieved_utc="2026-09-27T20:44:25+00:00",
        recorded_on="2026-09-27",
    )


LOCAL = "Asia/Kolkata"


# ================================================================ splits


def test_splits_are_the_feeds_published_factor(ticker, feed):
    feed(payload("nse_reliance_2020_2026.json"))

    series = ticker.splits

    assert isinstance(series, pd.Series)
    assert series.name == "Stock Splits"
    assert series.dtype == float
    assert series.to_dict() == {
        pd.Timestamp("2024-10-28", tz=LOCAL): pytest.approx(2.0),
    }
    assert isinstance(series.index, pd.DatetimeIndex)
    assert series.index.name == "Date"
    assert str(series.index.tz) == LOCAL
    assert series.attrs["bfinance_provenance"] == "nse_corporate_actions"
    assert series.attrs["bfinance_symbol"] == "RELIANCE"
    assert series.attrs["bfinance_actions_source"] == "nse_corporate_actions"
    assert "bfinance_actions_source_detail" not in series.attrs


def test_itc_splits_are_the_two_declared_bonuses(ticker, feed):
    feed(payload("nse_itc_2025_demerger.json"))

    series = ticker.splits

    assert series.to_dict() == {
        pd.Timestamp("2010-08-03", tz=LOCAL): pytest.approx(2.0),
        pd.Timestamp("2016-07-01", tz=LOCAL): pytest.approx(1.5),
    }
    assert series.index.is_monotonic_increasing


def test_the_split_index_is_the_ex_date_not_the_record_date(ticker, feed):
    """A composite filing: the two dates differ, and the index must be the ex-date.

    yfinance indexes splits by ex-date, and so does ``actions_frame``. Reading the record date
    would shift the factor by a day, which every downstream price adjustment would then apply to
    the wrong bar.
    """
    feed([
        {
            "symbol": "RELIANCE",
            "subject": (
                "Interim Dividend - Rs 1.50 Per Share / Face Value Split - From Rs 10 Per Share "
                "To Rs 5 Per Share"
            ),
            "exDate": "15-Jan-2025",
            "recDate": "16-Jan-2025",
            "isin": RELIANCE_ISIN,
            "faceVal": "10",
        }
    ])

    series = ticker.splits

    assert series.index.tolist() == [pd.Timestamp("2025-01-15", tz=LOCAL)]
    assert pd.Timestamp("2025-01-16", tz=LOCAL) not in series.index


def test_a_composite_subject_is_a_split_even_though_it_is_filed_as_a_dividend(ticker, feed):
    """Selection is on ``SplitRatio``, not on ``Action``.

    The filing's ``Action`` is ``dividend`` because that is what the subject leads with, and a
    filter on it would drop a real ``2.0`` factor that yfinance publishes for the same date. The
    control reads the parsed action, so this is a filter test and not an empty-frame test.
    """
    feed([
        {
            "symbol": "RELIANCE",
            "subject": (
                "Interim Dividend - Rs 1.50 Per Share / Face Value Split - From Rs 10 Per Share "
                "To Rs 5 Per Share"
            ),
            "exDate": "15-Jan-2025",
            "isin": RELIANCE_ISIN,
        }
    ])

    series = ticker.splits
    events = ticker._actions_events

    assert events.iloc[0]["Action"] == "dividend"
    assert events.iloc[0]["SplitRatio"] == pytest.approx(2.0)
    assert series.iloc[0] == pytest.approx(2.0)
    # The same filing is a dividend AND a split on ``actions``: both columns come from the one
    # parsed row, so the composite is one dated row carrying 1.5 rupees and a 2.0 factor.
    frame = ticker.actions
    assert frame.loc[series.index[0]].tolist() == pytest.approx([1.5, 2.0])


# ================================================================ actions


def test_actions_is_the_feed_frame_in_the_yfinance_shape(ticker, feed):
    feed(payload("nse_reliance_2020_2026.json"))

    frame = ticker.actions

    assert list(frame.columns) == ["Dividends", "Stock Splits"]
    assert frame.dtypes.tolist() == ["float64", "float64"]
    assert isinstance(frame.index, pd.DatetimeIndex)
    assert frame.index.name == "Date"
    assert str(frame.index.tz) == LOCAL
    assert len(frame) == 10
    assert frame.loc[pd.Timestamp("2024-10-28", tz=LOCAL), "Stock Splits"] == pytest.approx(2.0)
    assert frame.loc[pd.Timestamp("2024-10-28", tz=LOCAL), "Dividends"] == 0.0
    assert frame.loc[pd.Timestamp("2024-08-19", tz=LOCAL), "Dividends"] == pytest.approx(10.0)
    # The rights issue is a published event carrying neither a cash amount nor a factor. It is a
    # row the caller can see and not a row this package invented values for.
    assert frame.loc[pd.Timestamp("2020-05-13", tz=LOCAL)].tolist() == [0.0, 0.0]
    assert frame.attrs["bfinance_actions_source"] == "nse_corporate_actions"
    assert frame.attrs["bfinance_provenance"] == "nse_corporate_actions"
    assert frame.attrs["bfinance_symbol"] == "RELIANCE"
    assert frame.attrs["bfinance_actions_source_detail"] == (
        "NSE corporate-actions feed, 10 event row(s) "
        "{'dividend': 7, 'other': 2, 'bonus': 1}"
    )
    # ``actions`` and ``splits`` are the same feed, so the non-zero factor on one is the whole of
    # the other. A divergence here would mean one of them had started selecting differently.
    nonzero = frame["Stock Splits"][frame["Stock Splits"] != 0.0]
    assert nonzero.to_dict() == ticker.splits.to_dict()


def test_actions_does_not_read_the_screener_dividends_approximation(ticker, feed):
    """``actions["Dividends"]`` and ``dividends`` are different sources on purpose.

    Reading ``actions`` must not fetch the Screener profile, or the frame would silently acquire
    the fiscal-year-end basis the property is documented as not using.
    """
    feed(payload("nse_reliance_2020_2026.json"))

    ticker.actions

    assert ticker._profile is None, "actions fetched the Screener profile"
    assert ticker._actions_events is not None


# ================================================================ isin


@pytest.mark.parametrize(
    "fixture,expected",
    [
        ("nse_reliance_2020_2026.json", RELIANCE_ISIN),
        ("nse_itc_2025_demerger.json", ITC_ISIN),
    ],
)
def test_isin_is_read_from_the_feeds_own_column(ticker, feed, monkeypatch, fixture, expected):
    feed(payload(fixture))
    # Proves the answer did not come from the archive: that fallback would fail loudly here.
    monkeypatch.setattr(
        U, "isin_from_archive", lambda *a, **k: pytest.fail("the feed answered; no fallback")
    )

    assert ticker.isin == expected


def test_isin_is_none_never_an_empty_string(ticker, feed):
    """Neither source has anything, so the answer is ``None`` and the type is asserted."""
    feed([])

    found = ticker.isin

    assert found is None
    assert found != ""
    assert not isinstance(found, str)


def test_isin_falls_back_to_archived_memberships(ticker, feed, tmp_path):
    _archive_with(tmp_path, "RELIANCE", RELIANCE_ISIN)
    feed([])

    assert ticker.isin == RELIANCE_ISIN


def test_a_feed_with_disagreeing_isins_is_refused_rather_than_answered_from_one_row(
    ticker, feed, tmp_path
):
    """A market-wide or mis-joined frame is refused, not answered from its first row.

    The fallback is empty, so the observable answer is ``None``. The control below shows the same
    test would see a value if the rows agreed.
    """
    _archive_with(tmp_path, "OTHER", ITC_ISIN)
    feed([
        {"symbol": "RELIANCE", "subject": "Dividend - Rs 1 Per Share", "exDate": "01-Jan-2025",
         "isin": RELIANCE_ISIN},
        {"symbol": "ITC", "subject": "Dividend - Rs 1 Per Share", "exDate": "02-Jan-2025",
         "isin": ITC_ISIN},
    ])

    assert ticker.isin is None


# ================================================================ cost


def test_one_request_is_shared_by_splits_actions_and_isin(ticker, feed):
    fake = feed(payload("nse_reliance_2020_2026.json"))

    ticker.splits
    ticker.actions
    ticker.isin

    assert fake.calls == ["RELIANCE"], fake.calls


def test_a_fresh_ticker_pays_once_and_only_once(ticker, feed):
    fake = feed(payload("nse_reliance_2020_2026.json"))
    assert fake.calls == [], "constructing the Ticker touched NSE"

    ticker.actions
    assert len(fake.calls) == 1

    ticker.splits
    ticker.isin
    assert len(fake.calls) == 1


def test_the_feed_client_carries_the_tickers_timeout_and_proxy(
    monkeypatch: pytest.MonkeyPatch, feed, tmp_path: Path
):
    """A bounded Ticker must not fall back to the module-global client's 30 s timeout.

    ``_default_client()`` is process-global, with ``timeout=30.0`` and a proxy read from the
    environment only, so using it here made ``Ticker(timeout=3, proxy=...)`` wait 30 s and blame
    NSE. The class is stateless per call; the assertion is that the Ticker's settings reached it.
    """
    monkeypatch.setenv("BFINANCE_HOME", str(tmp_path))
    fake = feed([])

    Ticker("RELIANCE", cache_ttl_hours=0, timeout=3.0, proxy="http://proxy.test:8080").splits

    assert fake.client_kwargs == {"timeout": 3.0, "proxy": "http://proxy.test:8080"}


# ================================================================ failure and absence


def test_an_empty_feed_is_not_a_failure(ticker, feed):
    feed([])

    splits = ticker.splits
    actions = ticker.actions

    assert splits.empty
    assert actions.empty
    assert list(actions.columns) == ["Dividends", "Stock Splits"]
    assert splits.attrs["bfinance_actions_source"] == "nse_corporate_actions"
    assert actions.attrs["bfinance_actions_source"] == "nse_corporate_actions"
    assert "bfinance_empty_feed" not in actions.attrs, (
        "this property never sets the shape's empty-feed key; only empty_actions_frame does"
    )
    assert ticker.data_warnings == ()


def test_a_feed_failure_is_recorded_and_the_archive_still_answers(ticker, feed, tmp_path):
    _archive_with(tmp_path, "RELIANCE", RELIANCE_ISIN)
    fake = feed(error=UpstreamServiceError("NSE blocked the request"), failing_calls=99)

    assert ticker.isin == RELIANCE_ISIN
    splits = ticker.splits
    actions = ticker.actions

    assert splits.empty
    assert splits.attrs["bfinance_actions_source"] == "none"
    assert splits.attrs["bfinance_provenance"] == "none"
    assert "UpstreamServiceError" in splits.attrs["bfinance_actions_source_detail"]
    assert actions.empty
    assert list(actions.columns) == ["Dividends", "Stock Splits"]
    assert actions.attrs["bfinance_actions_source"] == "none"
    assert actions.attrs["bfinance_provenance"] == "none", (
        "a failed feed did not come from NSE; the empty shape's default provenance is overridden"
    )
    assert "bfinance_empty_feed" not in actions.attrs, (
        "a failed feed observed nothing, so it must not claim NSE answered with an empty feed; "
        "the key is absent here exactly as it is on splits"
    )
    assert "bfinance_empty_feed" not in splits.attrs
    assert "UpstreamServiceError" in actions.attrs["bfinance_actions_source_detail"]
    assert any("corporate actions" in w and "UpstreamServiceError" in w for w in ticker.data_warnings)
    # A failure is not memoised, so each of the three members retried the feed.
    assert len(fake.calls) == 3


def test_a_transient_failure_is_not_latched(ticker, feed):
    """The first call fails, the second answers: only a success fills the memo."""
    fake = feed(payload("nse_reliance_2020_2026.json"), error=UpstreamServiceError("once"), failing_calls=1)

    first = ticker.splits
    second = ticker.splits

    assert first.empty
    assert first.attrs["bfinance_actions_source"] == "none"
    assert not second.empty
    assert second.attrs["bfinance_actions_source"] == "nse_corporate_actions"
    assert len(fake.calls) == 2


def test_raise_errors_propagates_the_feed_failure(feed):
    """The same contract as the section members: a raising caller gets the exception, not a memo."""
    feed(error=UpstreamServiceError("NSE blocked the request"), failing_calls=99)
    raising = Ticker("RELIANCE", cache_ttl_hours=0, raise_errors=True)

    with pytest.raises(BFinanceError):
        raising.splits

    assert raising.data_warnings == ()


# ================================================================ capital gains


def test_capital_gains_is_empty_and_costs_no_request(ticker, feed):
    fake = feed(payload("nse_reliance_2020_2026.json"))

    series = ticker.capital_gains

    assert isinstance(series, pd.Series)
    assert series.empty
    assert series.name is None
    assert fake.calls == []
