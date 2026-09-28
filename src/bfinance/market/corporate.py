"""
Corporate actions extractor (dividends, stock splits, bonus issues).

Two sources, and the difference between them is the whole point
-------------------------------------------------------------
This engine has two ways to answer "what happened to this share count, and when".

**Primary — the NSE corporate-actions feed**
(:mod:`bfinance.nse.corporate_actions`). Real events on their **true ex-dates**: the amount in
rupees actually payable on that date, and the split/bonus ratio as filed. Verified live
2026-09-27 against yfinance 1.7.0: 27 of 27 ex-dates matched to the day across RELIANCE, LICI,
SHRIRAMFIN, BAJAJ-AUTO and HDFCBANK, and all 8 bonus / face-value-sub-division ratios checked
agreed exactly. This is the source that makes ``Adj Close`` correct.

**Fallback — Screener.in P&L** (the historical path, retained behind an explicit
``fallback="screener"``). Derives annual payouts from ``Dividend Payout % x EPS in Rs`` and dates
them at the **fiscal year end**, which is 4-5 months before the actual ex-date. Amounts track
yfinance within ~5%; dates do not. This is an *approximation of a date*, so it is never used
silently: :func:`resolve_actions` stamps the degraded source into ``df.attrs`` and warns, and the
OHLCV path only reaches for it when the NSE feed is **unreachable** — never when NSE answers
"no action", which is a real answer and must not be overwritten by a guess.

The fallback is **dividends only**. It carries no share-count information at all, so it can never
supply a split or bonus, and it does not try: :meth:`CorporateActionsEngine.extract_splits` used to
infer one from the balance sheet's ``Equity Capital`` row, which turned every capital raise into a
"split" and silently rescaled the price history by the fabricated ratio. That inference is deleted
— an equity-capital change is ambiguous by construction (bonus, rights, fresh issue, conversion and
capital reduction all move it, and a face-value sub-division moves the share count without moving
it at all), so no threshold could separate the cases. A result from this path therefore carries
``extra["bfinance_actions_share_count_observable"] = False``: the dividend column is an
approximation, and ``Stock Splits`` is 0.0 meaning "not known", not "none happened".

yfinance parity notes that still hold for both paths:
- yfinance ``ticker.dividends`` is indexed by **ex-date**; only the NSE path matches that.
- yfinance represents NSE 1:1 **bonus issues as a 2.0 split** (e.g. RELIANCE 2017-09-07,
  2024-10-28). Both paths do the same.
- yfinance normalises every past dividend by all *subsequent* splits; the NSE feed does not. NSE's
  unadjusted figure is the correct input for ``1 - D / prev_close``, which is defined in pre-action
  rupees. Do not "fix" this by rescaling.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import pandas as pd

from bfinance.models.company import CompanyProfile

logger = logging.getLogger("bfinance")

YF_TZ = "Asia/Kolkata"
ACTIONS_COLUMNS = ["Dividends", "Stock Splits"]

#: Provenance tags. Exactly one of these describes the events in a returned frame; the tag is
#: recorded in ``df.attrs["bfinance_actions_source"]`` so a consumer can tell a real ex-dated series
#: from the fiscal-year-end approximation without inspecting the values.
ACTIONS_SOURCE_NSE = "nse_corporate_actions"
ACTIONS_SOURCE_SCREENER = "screener_fy_end_approximation"
ACTIONS_SOURCE_NONE = "none"

YF_MAJOR_HOLDERS_INDEX = [
    "insidersPercentHeld",
    "institutionsPercentHeld",
    "institutionsFloatPercentHeld",
    "institutionsCount",
]
YF_CALENDAR_KEYS = [
    "Ex-Dividend Date",
    "Earnings Date",
    "Earnings High",
    "Earnings Low",
    "Earnings Average",
    "Revenue High",
    "Revenue Low",
    "Revenue Average",
]


@dataclass
class ResolvedActions:
    """The outcome of resolving one symbol's corporate actions, with its provenance attached.

    ``source`` is the single tag from :data:`ACTIONS_SOURCE_NSE`, :data:`ACTIONS_SOURCE_SCREENER`
    or :data:`ACTIONS_SOURCE_NONE`. There is deliberately no combined value: two series are never
    merged, averaged or reconciled, because a blended dividend series is synthetic in exactly the
    same way a blended price series is, and it would be indistinguishable from a real one.
    """

    dividends: pd.Series
    splits: pd.Series
    source: str = ACTIONS_SOURCE_NONE
    detail: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


class CorporateActionsEngine:
    """
    Extracts structured dividends and stock splits series from corporate history.
    """

    @classmethod
    def _tz_dated(cls, records: Dict[Any, float], name: str) -> pd.Series:
        """Build tz-aware (Asia/Kolkata) Series matching yfinance index tz."""
        if not records:
            return pd.Series(dtype=float, name=name)
        s = pd.Series(records).sort_index()
        s.index = pd.DatetimeIndex(pd.to_datetime(s.index))
        if s.index.tz is None:
            s.index = s.index.tz_localize(YF_TZ)
        else:
            s.index = s.index.tz_convert(YF_TZ)
        s.index.name = "Date"
        s.name = name
        return s

    @classmethod
    def extract_dividends(cls, profile: CompanyProfile) -> pd.Series:
        """
        Extract dividend payout series matching yfinance `ticker.dividends`.
        Returns pd.Series indexed by DatetimeIndex with dividend amounts.
        Dated at fiscal year-end (see module docstring for ex-date gap).
        """
        pnl = profile.profit_loss
        if not pnl.headers:
            return pd.Series(dtype=float, name="Dividends")

        payout_map = pnl.get_metric("Dividend Payout %")
        eps_map = pnl.get_metric("EPS in Rs") or pnl.get_metric("EPS")

        div_records = {}
        for period in pnl.headers:
            if period.upper() == "TTM":
                continue
            payout_pct = payout_map.get(period)
            eps_val = eps_map.get(period)

            if payout_pct is not None and eps_val is not None and payout_pct > 0:
                div_per_share = (payout_pct / 100.0) * eps_val
                # The identity above is only defined for a profitable year. A payout ratio left
                # positive from an earlier profitable period, paired with a loss year's EPS,
                # inverts the sign: 0.40 * -5.0 == -2.0. No company pays a negative dividend, so
                # that is not a noisy estimate, it is not a dividend at all -- the same standard
                # that removed the equity-capital split inference. Unguarded it reached the
                # published Dividends column as -2.0, and _dividend_adjustment_factor turned it
                # into a step of 1 - (-2.0)/prev_close > 1, inflating the adjusted price history.
                if div_per_share <= 0:
                    continue
                # Parse period like 'Mar 2024' -> date '2024-03-31'
                try:
                    dt = pd.to_datetime(f"01 {period}", format="%d %b %Y") + pd.offsets.MonthEnd(1)
                    div_records[dt] = round(div_per_share, 2)
                except Exception:
                    pass

        if not div_records:
            return pd.Series(dtype=float, name="Dividends")

        return cls._tz_dated(div_records, "Dividends")

    @classmethod
    def extract_splits(cls, profile: CompanyProfile) -> pd.Series:
        """
        Split / bonus series from a Screener.in profile: **always empty, by construction.**

        Returned rather than deleted because :mod:`bfinance.ticker` calls this directly for
        ``Ticker.splits``. It used to infer splits from growth in the balance sheet's
        ``Equity Capital`` row, with no split filing required, and that inference is deleted. This
        docstring is here so that it is not re-added.

        Why the inference cannot be tuned, only removed
        -------------------------------------------------
        ``Equity Capital`` is ``shares issued x face value``, so its change is produced by at least
        six different corporate events, and the balance sheet does not say which one happened:

        * **1:1 bonus** -- capitalised reserves become share capital. Share count rises. *A split.*
        * **Rights issue** -- new shares at a premium. Share count rises. *Not a split.*
        * **Fresh issue / preferential allotment / QIP** -- share count rises. *Not a split.*
        * **Warrant, ESOP or convertible-bond conversion** -- share count rises. *Not a split.*
        * **Capital reduction, buy-back cancellation, demerger** -- capital falls. *Not a split.*
        * **Face-value sub-division** -- share count rises by 10x while equity capital is
          **unchanged**. *Not even visible in this field.*

        Only the first is a share-count event for price-adjustment purposes, and for that one alone
        the capital ratio happens to equal the share ratio. A 1:1 bonus and a rights issue that
        raised the same rupee amount are the *same number in the same field*. So the signal is not
        noisy, it is **ambiguous**, and no threshold separates the cases. Raising the old
        ``ratio >= 1.5`` cliff would only have chosen which wrong answer to return -- calibrating a
        fabrication is still a fabrication.

        The measured consequence, on a real balance sheet: equity capital 4505 -> 23700 Cr, a
        capital raise at a company that has never had a share-count event, produced
        ``{2026-03-31: 5.26}`` as a "split". That put a fabricated number in the published
        ``Stock Splits`` column and, because the back-adjustment factor is the cumulative product
        over such ratios, silently rescaled every earlier bar by 1/5.26 (10.0 -> 1.901141) while the
        frame's provenance flags reported real bars and a complete adjustment. When the fabricated
        date fell outside the bar window the column read 0.0 and the rescaling happened anyway.
        This is the historical "Vodafone Idea reports four fabricated splits, never having had one"
        bug. The NSE feed path was fixed earlier; this fallback was not.

        What is left instead
        --------------------
        A split is a *declared* fact, not an inferred one, and the NSE feed already carries it as a
        filing: :func:`bfinance.nse.corporate_actions.split_ratio` reads the ratio out of the subject
        string and returns ``None`` for anything that does not declare one, which is why that path
        is not vulnerable to any of the six cases above. That is the only source a split is taken
        from here. An empty series is the honest absence: downstream it reads as 0.0, which
        :meth:`merge_actions_into_history` documents as meaning "not known", not "none happened",
        and :meth:`resolve_actions_async` records as
        ``extra["bfinance_actions_share_count_observable"] = False``.
        """
        return pd.Series(dtype=float, name="Stock Splits")

    @classmethod
    def build_actions(
        cls,
        dividends: Optional[pd.Series] = None,
        splits: Optional[pd.Series] = None,
    ) -> pd.DataFrame:
        """
        Build yfinance `ticker.actions` shape contract: DataFrame indexed by
        Date with float columns ["Dividends", "Stock Splits"] (union of dates).
        """
        frames = []
        for series, col in ((dividends, "Dividends"), (splits, "Stock Splits")):
            if series is not None and not series.empty:
                s = series.dropna()
                if not s.empty:
                    frames.append(s.rename(col).to_frame())
        if not frames:
            return pd.DataFrame(
                {c: pd.Series(dtype=float) for c in ACTIONS_COLUMNS},
                index=pd.DatetimeIndex([], name="Date", tz=YF_TZ),
            )
        df = pd.concat(frames, axis=1).sort_index()
        for col in ACTIONS_COLUMNS:
            if col not in df.columns:
                df[col] = 0.0
        df = df[ACTIONS_COLUMNS].fillna(0.0).astype(float)
        df.index = pd.DatetimeIndex(pd.to_datetime(df.index))
        if df.index.tz is None:
            df.index = df.index.tz_localize(YF_TZ)
        else:
            df.index = df.index.tz_convert(YF_TZ)
        df.index.name = "Date"
        return df

    @classmethod
    def build_capital_gains(cls) -> pd.Series:
        """
        yfinance `ticker.capital_gains` for Indian equities is an empty
        Series (dtype object, name None). Match it; never fabricate.
        """
        return pd.Series([], dtype=object)

    @classmethod
    def build_major_holders(cls, profile: CompanyProfile) -> pd.DataFrame:
        """
        yfinance `ticker.major_holders` shape: single "Value" float column
        indexed by [insidersPercentHeld, institutionsPercentHeld,
        institutionsFloatPercentHeld, institutionsCount]. Fractions, never
        percent-strings. institutionsCount has no screener source (-> NaN).
        """
        empty = pd.DataFrame(
            {"Value": pd.Series(dtype=float)}, index=pd.Index([], dtype=object)
        )
        sh = profile.shareholding
        if not sh.headers:
            return empty
        latest = sh.headers[-1]
        prom = sh.get_metric("Promoters").get(latest)
        fii = sh.get_metric("FIIs").get(latest) or 0.0
        dii = sh.get_metric("DIIs").get(latest) or 0.0
        if prom is None:
            return empty
        insiders = float(prom) / 100.0
        inst = (float(fii) + float(dii)) / 100.0
        denom = 1.0 - insiders
        float_held = (inst / denom) if denom > 0 else float("nan")
        return pd.DataFrame(
            {"Value": [insiders, inst, float_held, float("nan")]},
            index=pd.Index(YF_MAJOR_HOLDERS_INDEX),
        )

    @classmethod
    def build_institutional_holders(cls) -> pd.DataFrame:
        """yfinance returns empty (0,0) DataFrame for RELIANCE.NS; match it."""
        return pd.DataFrame()

    @classmethod
    def build_mutualfund_holders(cls) -> pd.DataFrame:
        """yfinance returns empty (0,0) DataFrame for RELIANCE.NS; match it."""
        return pd.DataFrame()

    @classmethod
    def build_calendar(
        cls,
        profile: CompanyProfile,
        dividends: Optional[pd.Series] = None,
    ) -> Dict[str, Any]:
        """
        yfinance `ticker.calendar` key contract. No analyst-estimates feed
        exists upstream, so forward estimates stay None (never fabricated).
        """
        ex_div = None
        if dividends is not None and not dividends.empty:
            last = dividends.dropna().index.max()
            if isinstance(last, pd.Timestamp):
                ex_div = last.date()
        return {
            "Ex-Dividend Date": ex_div,
            "Earnings Date": ["N/A"],
            "Earnings High": None,
            "Earnings Low": None,
            "Earnings Average": None,
            "Revenue High": None,
            "Revenue Low": None,
            "Revenue Average": None,
        }

    @classmethod
    def build_news(cls, announcements: Optional[List[Dict[str, str]]] = None) -> List[Dict[str, Any]]:
        """
        Adapt screener announcements to the yfinance 1.7 `ticker.news`
        shape: list of {"id": ..., "content": {...}} where content carries
        title/link/publisher/time keys. Empty in -> empty list (never None).
        """
        items: List[Dict[str, Any]] = []
        for i, a in enumerate(announcements or []):
            title = (a or {}).get("title", "")
            url = (a or {}).get("url", "")
            items.append(
                {
                    "id": f"bfinance-{i}",
                    "content": {
                        "title": title,
                        "link": url,
                        "publisher": {"displayName": "Screener.in"},
                        "provider": {"displayName": "Screener.in"},
                        "time": None,
                        "pubDate": None,
                        "canonicalUrl": {"url": url},
                    },
                }
            )
        return items

    @classmethod
    def merge_actions_into_history(
        cls,
        df: pd.DataFrame,
        dividends: Optional[pd.Series] = None,
        splits: Optional[pd.Series] = None,
    ) -> pd.DataFrame:
        """
        Left-join dividend/split series onto a history frame by calendar date.

        Non-action dates stay 0.0; zeros therefore mean genuinely no action
        on that date (or empty/unavailable input series). Tz-aware and
        tz-naive indexes are compared on normalized tz-stripped dates.
        """
        df = df.copy()
        df["Dividends"] = 0.0
        df["Stock Splits"] = 0.0
        if df.empty:
            return df

        def _date_keys(idx) -> pd.DatetimeIndex:
            keys = pd.DatetimeIndex(pd.to_datetime(idx).normalize())
            if getattr(keys, "tz", None) is not None:
                keys = keys.tz_localize(None)
            return keys

        df_keys = _date_keys(df.index)
        for col, series in (("Dividends", dividends), ("Stock Splits", splits)):
            if series is None or series.empty:
                continue
            s = series.dropna()
            if s.empty:
                continue
            action_map = dict(zip(_date_keys(s.index), s.values))
            df[col] = [float(action_map.get(k, 0.0)) for k in df_keys]
        return df

    # ------------------------------------------------------------ NSE-backed resolution
    #
    # Everything below supersedes the two extractors above for the Adj Close path. The extractors
    # stay because ``bfinance.ticker`` calls them directly for ``Ticker.dividends`` /
    # ``Ticker.splits``, which is outside this change.

    @classmethod
    def from_actions_frame(
        cls, actions: Optional[pd.DataFrame]
    ) -> tuple:
        """Split an :func:`bfinance.nse.corporate_actions.actions_frame` into two Series.

        Returns ``(dividends, splits)``, each a tz-aware ``Asia/Kolkata`` Series named exactly as
        the matching column, or an empty Series when the feed had nothing. The two are read from
        their own columns and never cross-contaminated, because a rupee amount and a share ratio
        are different quantities.
        """
        empty_d = pd.Series(dtype=float, name="Dividends")
        empty_s = pd.Series(dtype=float, name="Stock Splits")
        if actions is None or getattr(actions, "empty", True):
            return empty_d, empty_s
        dividends = pd.to_numeric(actions.get("Dividends"), errors="coerce").fillna(0.0)
        splits = pd.to_numeric(actions.get("Stock Splits"), errors="coerce").fillna(0.0)
        dividends.name, splits.name = "Dividends", "Stock Splits"
        return dividends.astype(float), splits.astype(float)

    @classmethod
    async def resolve_actions_async(
        cls,
        symbol: str,
        start: Optional[pd.Timestamp] = None,
        end: Optional[pd.Timestamp] = None,
        *,
        client: Optional[Any] = None,
        profile: Optional[CompanyProfile] = None,
        fallback: str = "screener",
        load_profile: Optional[Any] = None,
    ) -> ResolvedActions:
        """Resolve one symbol's dividends and splits, preferring the NSE ex-dated feed.

        Called with no range it asks for the feed's **whole** history for the symbol — one request,
        a few kilobytes — rather than the caller's window. That is not an optimisation choice, it is
        what makes the empty answer interpretable:

        * **The feed returns rows for this symbol.** It indexes this instrument and has an opinion
          about the window. An empty result *within the window* is then a real statement — nothing
          happened — and it is honoured. ``Adj Close == Close`` is then a fact.
        * **The feed returns nothing at all for this symbol.** It does not index this instrument
          (the feed is demonstrably incomplete at the tail: AAKASH has one row in thirty years, and
          a stubbed or delisted name has none). That is *not* a statement about the window, so the
          legacy approximation is consulted — loudly.

        Conflating those two cases is the bug this method exists to avoid. Both produce an empty
        list, and treating the second as the first would silently report "no dividend ever" for an
        instrument the source simply does not cover.

        On top of that, ``fallback="screener"`` also engages when the feed *raised* (network,
        schema, rate limit). Either way the result is labelled: ``source`` is one of
        :data:`ACTIONS_SOURCE_NSE`, :data:`ACTIONS_SOURCE_SCREENER` or :data:`ACTIONS_SOURCE_NONE`,
        a warning is logged, and the caller records it in ``df.attrs``. The two sources are never
        merged — a combined dividend series would be synthetic in the same way a blended price
        series is, and just as hard to detect.
        """
        if fallback not in ("screener", "none"):
            raise ValueError(f"fallback must be 'screener' or 'none', got {fallback!r}")

        covers_symbol = False
        try:
            if client is None:
                from bfinance.nse import corporate_actions as nse_ca

                client = nse_ca.CorporateActionsClient()
            actions = await client.actions_async(symbol, start=start, end=end)
            covers_symbol = not getattr(actions, "empty", True)
            if covers_symbol:
                dividends, splits = cls.from_actions_frame(actions)
                if start is not None or end is not None:
                    dividends, splits = cls._clip(dividends, splits, start, end)
                counts = dict(actions.attrs.get("bfinance_action_counts") or {})
                return ResolvedActions(
                    dividends=dividends,
                    splits=splits,
                    source=ACTIONS_SOURCE_NSE,
                    detail=(
                        f"NSE corporate-actions feed, {len(actions)} event date(s) "
                        f"{counts or 'none'} in [{start}, {end}]"
                    ),
                    extra={
                        "bfinance_actions_source_url": actions.attrs.get("bfinance_source_url"),
                        "bfinance_actions_event_counts": counts,
                        # Events NSE recorded that this module refuses to turn into a factor (buy
                        # backs, rights, demergers, AGM notices, REIT/SML "Per Unit"
                        # distributions). Surfaced so a consumer knows the feed was read and what
                        # it declined.
                        "bfinance_actions_unconverted_rows": int(
                            actions.attrs.get("bfinance_unconverted_action_rows", 0) or 0
                        ),
                        "bfinance_actions_dividend_basis": actions.attrs.get(
                            "bfinance_dividend_basis"
                        ),
                    },
                )
            reason = (
                f"the NSE corporate-actions feed returned no rows at all for {symbol!r}, so it "
                f"does not index this instrument"
            )
        except Exception as exc:  # noqa: BLE001 - any failure degrades, none propagates silently
            reason = f"the NSE corporate-actions feed was unreachable ({exc})"
            logger.debug("%s: NSE corporate actions unavailable (%s).", symbol, exc)

        if fallback == "screener":
            profile = await cls._maybe_load_profile(symbol, profile, load_profile)
            if profile is not None:
                dividends = cls.extract_dividends(profile)
                # Always empty, and that is the point: a Screener.in P&L carries no share-count
                # field, so there is nothing here to derive a split from. See extract_splits for
                # why the balance sheet's 'Equity Capital' row cannot stand in for one.
                splits = cls.extract_splits(profile)
                logger.warning(
                    "%s: %s. Falling back to the Screener.in fiscal-year-end approximation. "
                    "Dividend DATES are ~4-5 months off the true ex-date, and SPLITS AND BONUSES "
                    "ARE NOT OBSERVABLE FROM THIS SOURCE AT ALL, so 'Stock Splits' is 0.0 meaning "
                    "'not known' rather than 'none happened' and no share-count factor is derived "
                    "from it. Provenance is recorded in df.attrs['bfinance_actions_source'].",
                    symbol, reason,
                )
                return ResolvedActions(
                    dividends=dividends,
                    splits=splits,
                    source=ACTIONS_SOURCE_SCREENER,
                    detail=(
                        "Screener.in P&L approximation: dividends dated at fiscal year end "
                        "(~4-5 months after the true ex-date) and no share-count data of any kind, "
                        f"so no split factor is derived; {reason}"
                    ),
                    extra={
                        "bfinance_actions_fallback_reason": reason,
                        # The one fact the attrs vocabulary could not previously express: a NAMED
                        # source that cannot underwrite a share-count factor. Emitted rather than
                        # left absent so a consumer can branch on one key, and False rather than
                        # omitted so "cannot see splits" is distinguishable from "not consulted".
                        # It reaches the frame through OHLCVEngine's `df.attrs.update(extra)`.
                        #
                        # Deliberately NOT the `bfinance_actions_unconverted_rows` key that drives
                        # _incomplete_adjustment: that one means "the feed wrote a row down and this
                        # engine declined to convert it", and here the feed wrote down *nothing*.
                        # Reporting 1 would forge a feed record, which is the same defect in a
                        # different column. See the note in market/ohlcv.py::_incomplete_adjustment.
                        "bfinance_actions_share_count_observable": False,
                        "bfinance_actions_share_count_source": "not_observable_from_screener_pnl",
                    },
                )

        # Either the fallback is disabled, or there was no profile to approximate from. Both are
        # honest absences, and saying so beats emitting a fabricated ex-date.
        logger.warning(
            "%s: no corporate actions available because %s. Dividends and Stock Splits will be "
            "0.0, which means 'not known', not 'none happened'.",
            symbol, reason,
        )
        return ResolvedActions(
            dividends=pd.Series(dtype=float, name="Dividends"),
            splits=pd.Series(dtype=float, name="Stock Splits"),
            source=ACTIONS_SOURCE_NONE,
            detail=f"no corporate-action source available because {reason}",
            extra={"bfinance_actions_fallback_reason": reason},
        )

    @staticmethod
    def _clip(dividends, splits, start, end):
        """Restrict both series to ``[start, end)`` when a caller asked for a window.

        The OHLCV path deliberately does **not** use this: it takes the whole history so the
        "does the feed index this symbol" question stays answerable. ``merge_actions_into_history``
        drops out-of-window dates by construction anyway, so clipping here is a convenience for
        direct callers of this method, not a correctness requirement.

        The action series is ``Asia/Kolkata``-aware, so a naive ``start``/``end`` is localised before
        the comparison rather than allowed to raise — a tz-aware/naive comparison is a
        ``TypeError`` in pandas, and a window argument should not be able to crash the call.
        """
        def _bound(value, end_of_day: bool) -> Optional[pd.Timestamp]:
            if value is None:
                return None
            stamp = pd.Timestamp(value)
            if stamp.tzinfo is None:
                stamp = stamp.tz_localize(YF_TZ)
            else:
                stamp = stamp.tz_convert(YF_TZ)
            return stamp + pd.Timedelta(days=1) if end_of_day else stamp

        def _mask(series):
            keep = pd.Series(True, index=series.index)
            lower = _bound(start, end_of_day=False)
            upper = _bound(end, end_of_day=True)
            if lower is not None:
                keep &= series.index >= lower
            if upper is not None:
                keep &= series.index < upper
            return series[keep.to_numpy()]

        return _mask(dividends), _mask(splits)

    @staticmethod
    async def _maybe_load_profile(
        symbol: str, profile: Optional[CompanyProfile], load_profile: Optional[Any]
    ) -> Optional[CompanyProfile]:
        """Return a profile for the approximation path, or ``None``.

        ``load_profile`` is awaited when supplied. It is the caller's own loader so this module
        keeps no client of its own; when it is absent the approximation simply cannot run, which
        is the correct outcome rather than a reason to invent one.
        """
        if profile is not None or load_profile is None:
            return profile
        try:
            return await load_profile(symbol)
        except Exception as exc:  # noqa: BLE001 - an approximation is never worth an exception
            logger.debug("%s: profile unavailable for the approximation fallback (%s).", symbol, exc)
            return None
