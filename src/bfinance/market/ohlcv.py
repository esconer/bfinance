import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence, Union
import numpy as np
import pandas as pd

try:  # real-OHLC source; the opt-in synthetic path must keep working without it
    from bfinance.nse.bhavcopy import bhavcopy_range
except ImportError:  # pragma: no cover - depends on whether that module is installed
    bhavcopy_range = None

logger = logging.getLogger("bfinance")

from bfinance.market.corporate import (
    ACTIONS_SOURCE_NONE,
    ACTIONS_SOURCE_NSE,
    ACTIONS_SOURCE_SCREENER,
    CorporateActionsEngine,
)
from bfinance.screener.client import ScreenerClient
from bfinance.utils.exceptions import BFinanceError, TickerNotFoundError
from bfinance.utils.symbols import normalize_symbol

IST = "Asia/Kolkata"

# First date the cookie-free NSE archive publishes a full bhavcopy carrying real O/H/L
# (verified live 2026-09-26). The real-OHLC path cannot serve anything older.
BHAVCOPY_FIRST_DATE = "2024-01-01"

OHLC_SOURCES = ("bhavcopy", "synthetic")

# --------------------------------------------------------------------------------------
# Where the BARS came from, as opposed to what they are.
#
# ``attrs["bfinance_ohlc_source"]`` answers "bhavcopy print or derived O/H/L". This answers a
# different question: did these numbers leave the local SQLite mirror, leave NSE, or both. A
# frame can be real bhavcopy and still have been read off disk a week ago, and a consumer
# deciding how much to trust a number's freshness cannot read that off the feed label.
SOURCE_NETWORK = "network"
SOURCE_MIRROR = "mirror"
SOURCE_MIRROR_NETWORK = "mirror+network"

#: Set this to opt the bhavcopy path into reading :mod:`bfinance.mirror`. A bare truthy value
#: means the default location (``~/.bfinance/mirror.db``); any other non-empty value is read as
#: a path. Unset or empty -- the state of every existing installation -- means OFF, and the
#: call path is byte-identical to before the mirror existed: no :class:`bfinance.mirror.Mirror`
#: is constructed, no file is opened, no query runs.
#:
#: The env var exists because ``Ticker.history()`` and ``download()`` cannot be given a new
#: keyword argument without changing signatures this module does not own, and the engine is
#: constructed for them in ``Ticker.__init__`` with a single positional argument. A per-call
#: ``fetch_history(mirror=...)`` is the other door in; this is the one a caller of the public
#: yfinance-shaped API can actually reach.
MIRROR_ENV_VAR = "BFINANCE_MIRROR"

_TRUTHY_ENV = frozenset({"1", "true", "on", "yes", "y"})


def _mirror_state(**overrides) -> dict:
    """
    The per-call record of what the mirror was asked and what it answered.

    Published verbatim on ``df.attrs["bfinance_mirror"]`` on EVERY frame, mirror or not, so a
    consumer reads one key instead of probing for the absence of one. ``days_missing`` is the
    load-bearing field: it is the list of trading days the mirror does not hold, which is the
    difference between a complete frame and one that would have been a silent lie.
    """
    state = {
        "enabled": False,      # was a mirror consulted at all
        "used": False,         # did the store contribute to the ANSWER -- rows, or a definitive
                               # empty. False when it was opened and could not contribute, which
                               # is the negative control's whole claim.
        "path": None,          # which database, so a stale-mirror bug is traceable
        "reason": None,        # why not, when it was not used
        "days_considered": 0,  # calendar days the coverage query looked at
        "days_missing": [],    # trading days it does not hold -> a real gap, never silent
        "non_trading_days": [],  # weekends and seeded holidays -> absence here is an ANSWER
        "network_days": [],    # which days this call had to fetch from NSE to close the gaps
        "calendar_seeded": None,  # False means a weekday gap could not be proven either way
        "source": SOURCE_NETWORK,
    }
    state.update(overrides)
    return state

#: The columns a corporate-action back-adjustment restates *as prices*. ``Volume`` is deliberately
#: absent: it is a share count, not a rupee price, and it is restated separately by the rule below.
ADJUSTED_PRICE_COLUMNS = ("Open", "High", "Low", "Close")

#: The one share-count column, and the only column scaled by the **inverse** of the price factor.
#:
#: A share-count back-adjustment restates prices onto today's share count, so
#: ``price_adjusted = price / F`` for share-count factor ``F``. The same restatement has to be
#: applied to the share count, in the opposite direction, or the frame ends up describing two bars
#: in two different units: ``volume_adjusted = volume * F``. One then-share became ``F`` now-shares,
#: so a bar that traded ``V`` then-shares traded ``V * F`` now-shares' worth of stock. That is what
#: makes ``price * volume`` -- the rupee value traded -- invariant across the boundary, which is the
#: whole point of putting a bar on one share count.
#:
#: Measured on the real feed (RELIANCE, 2024-09-01..2024-11-16, 39 bars before and 14 after its
#: 1:1 bonus of ex-date 2024-10-28; run
#: ``uv run python <scratch>/measure_reliance_wide.py`` against ``nse.bhavcopy.bhavcopy_range``):
#:
#:     median Close   2924.90 -> 1305.47   ratio 0.446331   (the bonus, plus a soft session)
#:     median Volume  8132084 -> 11996392  ratio 1.475193   <- volume did NOT halve
#:     mean   Volume  8388982 -> 12883285  ratio 1.535739
#:
#: The direction is the finding. Had the published volume already been on the post-bonus share
#: count it would have fallen by ~2.2x, matching the halved price. It rose ~1.5x instead, so the
#: column mixes two share-count bases: pre-bonus bars are counted in then-shares, post-bonus bars
#: in now-shares. Leaving it alone while halving the price would understate the traded value of
#: every pre-bonus bar by exactly ``F`` -- a 2.0x step at the bonus, on a series where no other
#: event produces one. The correct test is therefore the invariant ``price * volume``, not
#: "did volume change".
VOLUME_COLUMN = "Volume"

#: Machine-readable price bases, recorded on every frame as ``attrs["price_basis"]``.
#:
#: Five sources on four different bases for one stock is the problem this vocabulary exists to
#: make visible: the values are self-describing and a consumer can branch on them without parsing
#: prose. Deliberately *not* called ``"yfinance_adjusted"``, because it is not yfinance parity --
#: no demerger, buy-back, rights issue or REIT per-unit distribution is applied, and no dividend
#: reaches O/H/L/C. The name states what was actually done.
PRICE_BASIS_AS_TRADED = "as_traded"
PRICE_BASIS_SHARE_COUNT_ADJUSTED = "as_traded_share_count_adjusted"
#: A vendor's own split/bonus-adjusted series, used whole. The spelling is the vendor's, copied
#: verbatim from ``bfinance.sources.price_provider.PRICE_BASIS``, so a frame bfinance built carries
#: the same basis label the source published rather than a paraphrase of it. It is a distinct
#: member because it is a *different promise* from :data:`PRICE_BASIS_SHARE_COUNT_ADJUSTED`: the
#: one says bfinance applied the share-count factor over the whole action history, the other says
#: the source applied splits and bonuses but NOT demergers and bfinance applied nothing further.
PRICE_BASIS_VENDOR_SPLIT_ADJUSTED = "as_traded_split_bonus_adjusted"
#: The synthetic path is not a price basis at all: Close and Volume are real Screener.in values and
#: Open/High/Low are *derived* from Close (previous close open, +/-0.2% envelope). It is named as
#: what it is rather than dressed up as an as-traded series.
PRICE_BASIS_SYNTHETIC = "screener_close_real_derived_ohlc_unadjusted"

# Key names a whole-market bhavcopy frame may carry the scrip identity under. The daily
# file lists every NSE symbol, so a range fetch is sliced down to one symbol by these.
_SYMBOL_KEYS = ("SYMBOL", "TICKER", "SECURITY", "SCIP_CODE", "SCIP CODE")


class RealOHLCUnavailableError(BFinanceError):
    """The real-OHLC source could not serve the request, so no bars are returned.

    Raised instead of falling back to fabricated Open/High/Low. An empty frame here would
    be indistinguishable from "this instrument has no data" and a fabricated frame would be
    indistinguishable from real data, so the default source fails loudly instead.
    """


class AdjustmentNotAvailableError(BFinanceError):
    """auto_adjust=True was requested but no authoritative adjustment factors exist."""


def _localize_ist(df: pd.DataFrame) -> pd.DataFrame:
    """Return ``df`` sorted by a tz-aware Asia/Kolkata DatetimeIndex (yfinance NSE convention)."""
    df = df.copy()
    df.index = pd.to_datetime(df.index)
    if df.index.tz is None:
        df.index = df.index.tz_localize(IST)
    else:
        df.index = df.index.tz_convert(IST)
    df.index.name = "Date"
    df.sort_index(inplace=True)
    return df


def _select_symbol_rows(market: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Slice a whole-market bhavcopy frame down to one symbol.

    A daily bhavcopy file is ~3,600 rows covering the entire exchange, so the range is fetched
    once and filtered here rather than issuing one request per symbol. Tolerates a symbol
    column (``SYMBOL``/``TICKER``/...) or a symbol level on a MultiIndex; a frame carrying no
    symbol key at all is already scoped to a single instrument and is returned unchanged.
    """
    if isinstance(market.index, pd.MultiIndex):
        for level in market.index.names:
            if level is not None and str(level).strip().upper() in _SYMBOL_KEYS:
                market = market.reset_index(level=level)
                break
    key = next((c for c in market.columns if str(c).strip().upper() in _SYMBOL_KEYS), None)
    if key is None:
        return market
    return market.loc[market[key].astype(str).str.strip().str.upper().eq(symbol)]


def _dividend_adjustment_factor(df: pd.DataFrame) -> pd.Series:
    """Cumulative DIVIDEND factor per bar, from the actions already merged into ``df``.

    factor(t) = product over dividends dated *after* t of (1 - D / prev_close).

    Two properties are deliberate, both measured against yfinance 1.7.0 rather than assumed:

    * **Only ex-dates inside the returned window can appear.** ``1 - D / prev_close`` needs the
      close on the trading day before the ex-date, and a bhavcopy range fetch stops at the
      requested ``end``. An ex-date past that boundary has no prev_close to divide by, and
      substituting a stale close from the end of the window would fabricate a factor -- so those
      dividends are left out and the omission is documented in the docstring of
      :meth:`OHLCVEngine.fetch_history` rather than papered over.
    * **It is read from the AS-TRADED ``Close``.** The step is a ratio of two rupee amounts in the
      same basis, (prev_close - D) / prev_close, so it is scale-invariant: NSE's unadjusted rupee
      dividend against an as-traded prev_close gives the same number as yfinance's adjusted
      dividend against its split-adjusted prev_close. Feeding an already-adjusted close back in
      would double-count instead.
    """
    factor = pd.Series(1.0, index=df.index)
    dividends = df.get("Dividends")
    if dividends is None or float(dividends.abs().sum()) == 0:
        return factor
    close = df["Close"].astype(float)
    prev_close = close.shift(1)
    prev_close = prev_close.where(prev_close > 0, close)
    step = 1.0 - dividends.astype(float) / prev_close
    return (factor * step.where(step > 0, 1.0))[::-1].cumprod()[::-1].shift(-1).fillna(1.0)


def _split_adjustment_factor(index: pd.DatetimeIndex, splits) -> pd.Series:
    """Cumulative SHARE-COUNT factor per bar, from the instrument's WHOLE action history.

    factor(t) = product over splits/bonuses dated *after* t of 1/S.

    This is the factor that belongs on the price columns, and it is the whole-history product
    rather than only the events that land inside the bar window. That distinction is the entire
    HDFCBANK case: its 1:1 bonus has an ex-date of 2025-08-26, so for a 2024-09..2024-11 request
    no event ever appears in the frame, and a window-only product is 1.0 for every bar while
    yfinance -- which serves split-adjusted prices for the whole series -- halves all 53 of them.
    A share-count event restates every earlier bar, so it must be read from the full history,
    which is exactly why :meth:`CorporateActionsEngine.resolve_actions_async` asks for the whole
    history rather than the caller's window.

    Note the sign: this is the factor prices are *multiplied* by, so a 1:1 bonus (ratio 2.0) gives
    0.5 on every earlier bar. Volume therefore takes its **reciprocal**; see :data:`VOLUME_COLUMN`.

    No price is needed to build it, so unlike the dividend factor this one is exact for ex-dates
    outside the returned window.
    """
    factor = pd.Series(1.0, index=index)
    if splits is None or getattr(splits, "empty", True) or not isinstance(index, pd.DatetimeIndex):
        return factor
    ratios = pd.to_numeric(splits, errors="coerce").dropna()
    ratios = ratios[ratios > 0]
    if ratios.empty:
        return factor
    dates = pd.DatetimeIndex(ratios.index)
    dates = (
        dates.tz_localize(index.tz) if dates.tz is None else dates.tz_convert(index.tz)
    )
    # side="left" -> first bar at or after the ex-date, so the prefix strictly BEFORE it (the
    # bars the event restates) is the part that carries 1/S. The ex-date bar itself does not.
    positions = np.searchsorted(index.asi8, dates.asi8, side="left")
    multipliers = np.ones(len(index) + 1, dtype=float)
    for position, ratio in zip(positions, ratios.to_numpy(dtype=float)):
        if position > 0:
            multipliers[:position] *= 1.0 / ratio
    return pd.Series(multipliers[: len(index)], index=index)


def _incomplete_adjustment(symbol: str, extra: Optional[dict]) -> Optional[dict]:
    """Describe the action classes the feed recorded that this engine declined to convert.

    Returns ``None`` when nothing was declined, else a dict carrying a machine-readable ``reason``,
    the symbol, the declined-row count, the full action-kind histogram, and a human ``detail``.
    The key lands on ``df.attrs["bfinance_adjustment_incomplete"]`` so a caller that asked for
    ``adjust_corporate_actions=True`` can detect an incomplete adjustment from the frame alone.

    Why a count is the signal, and not a price test
    ------------------------------------------------
    The obvious alternative -- look for a price move no applied factor explains -- was measured and
    does not work. On ITC's real 2025-01-06 demerger the as-traded close fell 8.0876%
    (481.60 -> 442.65) with the factor at exactly 1.000000, which looks like a clean detection.
    It is not: an 8% single-day fall is ordinary volatility, and
    :func:`bfinance.nse.corporate_actions.unexplained_price_jumps` at its documented 0.25 tolerance
    reports **no** jump for those bars. Catching the demerger that way needs a tolerance tight
    enough to false-positive on every earnings drop, and then the flag stops meaning anything. A
    price move cannot tell a demerger from a bad day; the feed's own record can, because the feed
    wrote the row down and :mod:`bfinance.nse.corporate_actions` classified it ``other`` and
    refused to make a factor of it.

    So the signal is the feed's own ``bfinance_actions_unconverted_rows``. The cost is precision,
    and it is a deliberate trade: that bucket holds declared-but-unpriceable rows that are not
    events at all (8,649 ``Annual General Meeting`` notices on the full 1995-2026 pull) alongside
    real skipped ones (90 demergers, 177 rights, 159 buy-backs, 843 interest payments). A symbol
    whose only declined rows are AGM notices is therefore reported incomplete too. That direction of
    error is the safe one -- a frame that says "possibly incomplete" when it was complete costs a
    consumer one attrs check, whereas the current behaviour costs them a silently wrong factor.
    Exposing the count and the histogram lets a consumer see how much of it is AGM noise.

    Ex-dates of the declined rows are **not** in here, because the feed does not pass them through
    :func:`~bfinance.market.corporate.CorporateActionsEngine.resolve_actions_async`: it forwards only
    the count. Naming the ex-date and the subject (ITC, 2025-01-06, "Demerger") needs one added key
    on the resolved-actions ``extra`` in ``bfinance/nse/corporate_actions.py``.

    A precise signal now exists upstream and would remove the AGM over-reporting: the parsed-events
    frame carries ``attrs["bfinance_structural_action_rows"]``, which counts demergers, buy-backs
    and rights issues rather than every declined row. It cannot be used yet, because
    :func:`~bfinance.nse.corporate_actions.actions_frame` does not propagate that key onto the
    collapsed frame that ``resolve_actions_async`` receives. Two lines -- propagate in
    ``actions_frame``, forward in the ``extra`` -- would let this report a demerger without
    condemning a symbol whose only declined rows are AGM notices.
    """
    if not extra:
        return None
    declined = int(extra.get("bfinance_actions_unconverted_rows") or 0)
    if declined <= 0:
        return None
    counts = dict(extra.get("bfinance_actions_event_counts") or {})
    return {
        "reason": "unconverted_action_classes",
        "symbol": (symbol or "").upper() or None,
        "unconverted_rows": declined,
        "action_counts": counts,
        # The feed collapses every declined class into one ``other`` bucket, so this is the only
        # class name available. Reported next to the count so a consumer can see it is a bucket and
        # not a specific finding, rather than mistaking "other" for a diagnosis.
        "declined_action_classes": ["other"],
        "detail": (
            f"the corporate-actions feed recorded {declined} row(s) for "
            f"{(symbol or '').upper()} in a class this engine never converts into a factor "
            f"(buy-backs, rights, demergers, AGM notices, REIT/SML per-unit distributions), so "
            f"the share-count factor is incomplete for the window's prices. Measured on ITC's "
            f"real 2025-01-06 demerger: bhavcopy fell 8.0876% across the ex-date (481.60 -> "
            f"442.65) and the correct factor is 1.047332, but the factor applied is 1.000000. The "
            f"count cannot separate a demerger from an AGM notice, so this is conservative: it "
            f"over-reports rather than under-reports."
        ),
    }


def _resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample daily bars to weekly (Monday-labeled) or monthly bars."""
    if rule == "weekly":
        keys = df.index.normalize() - pd.to_timedelta(df.index.weekday, unit="D")
    else:
        keys = df.index.normalize().to_period("M").to_timestamp()
        if getattr(df.index, "tz", None) is not None:
            keys = keys.tz_localize("Asia/Kolkata")
    agg = {}
    for col in df.columns:
        if col == "Open":
            agg[col] = "first"
        elif col == "High":
            agg[col] = "max"
        elif col == "Low":
            agg[col] = "min"
        elif col in ("Close", "Adj Close"):
            agg[col] = "last"
        elif col == "Volume":
            agg[col] = "sum"
        elif col == "Dividends":
            agg[col] = "sum"
        else:
            agg[col] = "max"
    out = df.groupby(keys).agg(agg)
    out.index.name = "Date"
    out.sort_index(inplace=True)
    if "Volume" in out.columns:
        out["Volume"] = out["Volume"].fillna(0).astype(np.int64)
    return out


#: Coarseness order, so "delivered is coarser than requested" is a comparison and not a
#: guess. ``None`` is not in here: an interval that cannot be determined is a third thing,
#: neither finer nor coarser, and it must not be silently treated as the finest.
_INTERVAL_RANK: Dict[str, int] = {"1d": 0, "1wk": 1, "1mo": 2}


def _infer_bar_interval(index) -> Optional[str]:
    """
    The bar spacing a frame actually carries, read off its own index, or ``None``.

    Why this exists: the close-only Screener feed changes granularity with window length
    and nothing in the request says which you got. Measured on RELIANCE, same
    ``interval="1d"`` call, screener.in route:

        period=1mo    20 rows   median gap 1 day    daily
        period=6mo   122 rows   median gap 1 day    daily
        period=1y    247 rows   median gap 1 day    daily
        period=2y    105 rows   median gap 7 days   weekly
        period=5y    262 rows   median gap 7 days   weekly

    So a caller asking for daily bars silently receives weekly ones past roughly eighteen
    months, with no column, attribute or warning to say so. Any indicator, volatility
    estimate or backtest then changes meaning at that boundary without changing shape.

    The median is used rather than the minimum because a daily equity series legitimately
    contains 3-day gaps across weekends and holidays; the mode of trading-day spacing is
    the signal, and the minimum would read a Friday-to-Monday pair as weekly.

    Fewer than three bars cannot establish a spacing, and ``None`` is returned rather than
    guessing from one gap, which would classify a two-bar frame by noise.
    """
    idx = pd.DatetimeIndex(index)
    if idx.hasnans or len(idx) < 3:
        return None
    gaps = pd.Series(idx).diff().dt.days.dropna()
    if gaps.empty:
        return None
    median_gap = float(gaps.median())
    if median_gap <= 1.5:
        return "1d"
    if median_gap <= 9.0:
        return "1wk"
    if median_gap <= 40.0:
        return "1mo"
    return None


def _interval_mismatch_detail(requested: str, delivered: Optional[str]) -> Optional[str]:
    """
    A sentence naming the coarser-than-requested case, or ``None`` when there is none.

    The published reason this does not raise: the values are real, only the granularity
    is coarser than asked, so a caller who genuinely wants five years at weekly
    resolution is served correctly by the same call. Refusing would break that legitimate
    use to prevent a misuse the caller can now see. What it must never be is silent, so
    the frame carries the key, the sentence, and a logged warning.
    """
    if delivered is None or requested not in _INTERVAL_RANK or delivered not in _INTERVAL_RANK:
        return None
    if _INTERVAL_RANK[delivered] <= _INTERVAL_RANK[requested]:
        return None
    return (
        f"the feed delivered {delivered} bars while interval={requested!r} was requested. "
        f"The Screener.in chart route coarsens with window length, so a longer request "
        f"silently yields fewer, wider bars. This frame is REAL but is not the resolution "
        f"you asked for: any per-bar indicator, ATR, or backtest computed on it changes "
        f"meaning. Ask for interval={delivered!r} if that is what you wanted, or use "
        f"ohlc='bhavcopy' for real daily bars (from 2024-01-01, the archive floor)."
    )


# --------------------------------------------------------------------------------------
# The price-source cascade: the seam, the request vocabulary, and the four attrs keys.
#
# WHAT IS HERE AND WHY IT IS OPT-IN
# --------------------------------------------------------------------------------------
# :mod:`bfinance.sources.cascade` is a complete, tested, multi-source failover engine for price
# history: a try-order, a health ledger, a price-basis check that never spends a request on a
# source that cannot serve the requested basis, and an attempt list. It was not on this path, so a
# ``history()`` call could not fail over even in principle, and the machinery was unreachable from
# the public API.
#
# It is wired in here **opt-in**, and the reason is a measurement rather than caution. The four
# price sources are on three different bases, and they are not interchangeable: HDFCBANK's close
# is exactly 0.5x the bhavcopy's on all 62 sessions of 2024-12-16..2025-03-13, a 1:1 bonus
# back-applied, because the one vendor back-adjusts and the exchange print does not. A default that
# failed over between them would produce a series containing a move that never happened, so the
# try-order is chosen per call by ``source=``/``sources=`` rather than baked in here.
#
# What the cascade changes is the *vendor*, never the bar shaping and never the basis of the
# answer. The winner's frame is used whole, and its real basis is published. Nothing is blended
# and nothing is invented: :func:`bfinance.sources.cascade.is_empty_payload` already treats an
# empty frame as a failure rather than as data, and this module refuses that frame again on its
# own terms before touching it.
#
# FOUR ATTRS KEYS, and why they are four and not one
# --------------------------------------------------------------------------------------
# Failover is **loud in attrs, quiet in the return type**: the caller gets an ordinary DataFrame
# and reads the provenance off it. The keys are separate because the four questions are separate,
# and one packed dict would make a consumer parse prose to find a boolean it branches on.
#
# * ``bfinance_price_source`` -- the winning **vendor**, read off the registry row that served.
#   Distinct from ``bfinance_source``, which is the TRANSPORT (``network``/``mirror``/
#   ``mirror+network``) and whose vocabulary is a published contract that this change does not
#   touch. A frame can be real bhavcopy and have been read off disk a week ago; "which vendor
#   printed this" and "how stale can it be" are different questions.
# * ``bfinance_price_source_attempts`` -- every source considered, asked or not, with its outcome.
# * ``bfinance_price_source_degraded`` -- ``True`` only when a source ahead of the winner was
#   **asked and failed**. A source skipped because it cannot serve the requested basis is the
#   system working, and counting it here would train callers to ignore the flag.
# * ``bfinance_price_source_detail`` -- one sentence naming what was tried and what failed.
#
# All four are set on the populated frame AND the empty one, because a frame with no bars is
# exactly the frame a consumer is most likely to treat as "nothing went wrong".

#: The capability the registry publishes daily prices under. Spelled once because it appears in
#: the routing call, in the validation error, and in whatever refusal the cascade raises.
PRICE_CAPABILITY = "price_history"

#: The registry row whose values are the exchange print. Named here rather than left as a bare
#: string in the comparison that routes a mirror-free frame, so the assertion in
#: :meth:`OHLCVEngine._price_source_record` cannot drift from the row it is asserting about.
BHAVCOPY_SOURCE = "nse-bhavcopy"

#: The basis an unqualified request is routed on, as the registry spells it. bfinance post-processes
#: the bars it is handed -- it applies the share-count factor itself -- so it needs an UNADJUSTED
#: series to start from. It is also the only price basis in the default registry with real
#: redundancy (``nse-bhavcopy`` and ``marketlens-close``), so it is the one a failsafe can fail
#: over on at all. A bare string rather than a :class:`~bfinance.sources.model.Basis` because this
#: module must not import :mod:`bfinance.sources` at module level
#: (``tests/test_public_surface.py`` requires ``import bfinance`` not to pull that layer in), and
#: because the comparison that needs it is a string comparison on a value the cascade returned.
DEFAULT_PRICE_BASIS = "as_traded"

#: The four attrs keys. Module constants so a consumer, a test and this module all spell them
#: identically, and so a rename is a one-line edit that the compiler then forces everywhere.
PRICE_SOURCE_ATTR = "bfinance_price_source"
PRICE_SOURCE_ATTEMPTS_ATTR = "bfinance_price_source_attempts"
PRICE_SOURCE_DEGRADED_ATTR = "bfinance_price_source_degraded"
PRICE_SOURCE_DETAIL_ATTR = "bfinance_price_source_detail"


@dataclass(frozen=True)
class PriceSourceRouter:
    """
    The injection seam: how :class:`OHLCVEngine` reaches the source cascade, and what it may
    substitute for it.

    Exists because of a concrete measurement, not a preference. Roughly 130 offline call sites
    across ten test files construct ``OHLCVEngine(screener_client, corporate_actions_client=...)``
    and stub those two objects directly, because that is the only door into this engine today.
    :func:`bfinance.sources.cascade.load_provider` does ``importlib.import_module(spec.module)`` at
    **call time**, so the moment the cascade is on the path every one of those stubs is bypassed
    and the offline suite starts reaching the real NSE. The seam is added before the cascade is
    switched on for exactly that reason: it is what keeps "no regression" checkable instead of
    aspirational.

    Every field is optional and the default is the real thing, so ``PriceSourceRouter()`` and
    ``None`` behave identically. What the router does **not** do is change the shipped default:
    with no router and no ``source=``/``sources=``, :meth:`OHLCVEngine.fetch_history` still calls
    :meth:`_fetch_bhavcopy_bars` directly and no registry is consulted at all.

    Attributes:
        registry: A :class:`bfinance.sources.registry.Registry` to route against. ``None`` uses
            :data:`bfinance.sources.registry.DEFAULT`, so the real try-order and the real
            basis-eligibility rules are what a test exercises by default.
        fetch: ``(spec, request) -> value``, the same contract as
            :func:`bfinance.sources.cascade.resolve`'s ``fetch``. It may raise, and a raise is a
            fall-through rather than a failure of the call, which is what makes a stub the
            failover case. ``None`` lets the cascade import each row's module at call time.
        health: A :class:`bfinance.sources.health.HealthLedger`. ``None`` uses the process-wide
            one, which learns from real failures -- and which a test must therefore override, or
            one test's recorded failure reorders a later test's try-order.
    """

    registry: Optional[Any] = None
    fetch: Optional[Any] = None
    health: Optional[Any] = None


def resolve_price_source_names(
    source: Optional[str] = None,
    sources: Optional[Sequence[str]] = None,
    registry: Optional[Any] = None,
) -> tuple:
    """
    The ordered, de-duplicated price-source names a call asked for, validated up front.

    Refused **before any request is spent**, for the same reason an unrecognised ``ohlc`` or
    ``period`` is: a source name is knowable locally, so discovering it is invalid by asking a
    vendor. ``tests/test_silent_fallbacks.py::test_download_rejects_before_doing_any_work`` pins
    that *timing* for ``download()``, and this is the same guarantee for a price source -- one
    typo must not cost one request per symbol in a 5,000-symbol batch.

    ``source=`` and ``sources=`` are merged rather than treated as alternatives, in that order, with
    duplicates collapsed. ``source="nse-bhavcopy", sources=["nse-bhavcopy"]`` is redundant rather
    than contradictory, and raising on it would be refusing a call with one unambiguous meaning.
    Two *different* names is not redundant, and the merged order is the try-order, so it is
    accepted and honoured.

    Returns:
        The names in the order they would be tried. Empty when the caller named none, which is
        the signal that the direct path is in force and the registry must not even be imported.

    Raises:
        ValueError: a name is not registered for ``"price_history"``. A
            :class:`bfinance.sources.errors.SourceCapabilityError` would also be defensible, but
            this is a caller bug in a caller-facing argument, and :class:`ValueError` is what the
            neighbouring ``ohlc`` and ``period`` refusals raise, so one ``except`` covers all three.
    """
    ordered: List[str] = []
    for candidate in ((source,) if source is not None else ()) + tuple(sources or ()):
        name = str(candidate).strip()
        if name and name not in ordered:
            ordered.append(name)
    if not ordered:
        # Nothing named. Returning here rather than importing the registry is deliberate: with no
        # source selected the direct path runs, and `tests/test_public_surface.py` requires that
        # `import bfinance` not pull this layer in.
        return ()

    from bfinance.sources.registry import DEFAULT  # lazy: opt-in isolation

    reg = DEFAULT if registry is None else registry
    registered = tuple(reg.names_for(PRICE_CAPABILITY))
    unknown = [name for name in ordered if name not in registered]
    if unknown:
        raise ValueError(
            f"price source(s) {unknown} are not registered for {PRICE_CAPABILITY!r}. "
            f"Registered, in try-order: {list(registered)}.\n\n"
            f"Named here, before any request: a source name is a local vocabulary, so spending a "
            f"request to discover it is invalid is the one outcome this package will not produce. "
            f"Note that a name is not a promise of availability -- pass several with sources= and "
            f"the cascade tries them in order, and attrs['{PRICE_SOURCE_ATTR}'] on the returned "
            f"frame names the one that actually answered."
        )
    return tuple(ordered)


class OHLCVEngine:
    """
    Historical OHLCV data builder converting deep price feeds into yfinance-identical DataFrames.

    Provenance is per-source and recorded in ``df.attrs["bfinance_synthetic_ohlc"]``:

    - ``ohlc="bhavcopy"`` (default): Open/High/Low/Close/Volume are real, published daily
      values from the NSE bhavcopy archive. Flag is ``False``.
    - ``ohlc="synthetic"`` (explicit opt-in): Close + Volume are real values from the
      close-only Screener chart API, but Open/High/Low are synthesized from Close
      (previous close open, +/-0.2% envelope) and the flag is ``True``.

    Adjusted vs as-traded
    ---------------------
    The NSE bhavcopy is *as traded*. yfinance's own ``auto_adjust=False`` series is not: Yahoo
    serves OHLC already restated onto the current share count, so for RELIANCE over
    2024-09-01..2024-11-15 the two disagreed by the full action factor (14/53 bars matched; the
    39 before the 28-Oct-2024 1:1 bonus were off by exactly 2.0x) -- see
    ``tests/test_bhavcopy_parity_adjust.py``. Trendlyne, an independent third source, agreed with
    yfinance on 7 of 8 symbols, so yfinance's basis is the reference and a drop-in must reproduce
    it. So the default path multiplies Open/High/Low/Close by the cumulative **share-count**
    factor, and that factor is the product over the instrument's WHOLE
    corporate-action history, not just the events that land inside the window. It has to be:
    HDFCBANK's 1:1 bonus has a 2025-08-26 ex-date, so for a 2024-09..2024-11 request nothing in
    the frame shows it, and a window-only product is 1.0 for every bar where yfinance halves all
    53. ``adjust_corporate_actions=False`` returns the as-traded series, for anyone reconstructing
    what actually traded.

    Volume is the one column that is **not** a price, so it is not scaled by the same factor -- it
    is scaled by the factor's *inverse*, which is what keeps ``price * volume`` (the rupee value
    traded) continuous across a share-count event. The as-traded bhavcopy mixes two share-count
    bases in that one column; see :data:`VOLUME_COLUMN` for the measurement that establishes the
    direction, and ``attrs["price_basis"]`` for the resulting label.

    ``Adj Close`` is the full back-adjusted series: the share-count factor *and* the dividend
    factor. Dividends are deliberately NOT on the price columns, because yfinance's raw OHLC are
    not -- TCS, with Rs 238 of dividends after the window, matches bhavcopy to 5e-8. yfinance's
    own code says the same thing: ``utils.auto_adjust`` derives its scale factor from
    ``Adj Close / Close``, so its raw OHLC carry splits and not dividends. The two-factor shape
    is not a detail; folding the dividend factor into O/H/L/C puts TCS 7.6% from yfinance.

    The factors come from NSE's own corporate-actions feed
    (:mod:`bfinance.nse.corporate_actions`) -- true ex-dates, rupee amounts as actually payable,
    and bonus / face-value-sub-division ratios. With no action at all the factors are 1.0
    everywhere, so ``Adj Close == Close`` is then a fact about the instrument rather than an
    unfinished calculation. ``df.attrs["bfinance_actions_source"]`` always names the feed that
    produced the ``Dividends`` / ``Stock Splits`` columns, so a frame built on the legacy
    fiscal-year-end approximation can never be mistaken for one carrying real ex-dates.

    Degradation
    -----------
    If the actions feed is unreachable, errors, or does not index the instrument, the frame
    cannot honestly claim yfinance parity, so it is returned **as-traded and flagged**
    (``attrs["bfinance_corporate_actions_applied"] = False``) with a warning, rather than
    silently unadjusted. This package's convention is lenient degradation -- an answer plus a
    truthful label -- so the third option (unadjusted and unlabelled) is the one thing that is
    never done. ``attrs["bfinance_corporate_actions_applied_detail"]`` says which of the four
    cases applies: explicit opt-out, actions not requested, synthetic path, or no source.

    A fifth case exists and is the reason the flag alone was not enough
    ---------------------------------------------------------------
    ``bfinance_corporate_actions_applied`` used to read ``True`` whenever a real factor was derived
    from a real feed, which is a *different* claim from "this frame is fully adjusted".
    :mod:`bfinance.nse.corporate_actions` deliberately never turns a ``Demerger``, buy-back, rights
    issue, AGM notice or REIT per-unit distribution into a factor -- folding those in would corrupt
    the arithmetic -- so the feed can hold known price-affecting events that the factor does not
    cover, and the frame had no way to say so. Measured on ITC, ex-date 2025-01-06:

        bhavcopy as-traded   481.60 (2025-01-03) -> 442.65 (2025-01-06)   -8.0876%
        factor applied       1.000000   (the feed's splits series has nothing on that date)
        factor that is right 1.047332   (= P_pre / (P_ex + 0.1 x 171.85), the stub listing close)

    So the frame was claiming to have applied an adjustment that was 4.7332% short. The flag is now
    ``False`` in that state, and ``attrs["bfinance_adjustment_incomplete"]`` carries a
    machine-readable ``reason``, the symbol, the declined-row count and the action-kind histogram.
    That key is present on **every** frame (``None`` when complete), so the check is
    ``df.attrs["bfinance_adjustment_incomplete"] is not None`` rather than a probe for absence.
    The factor that *was* derived is still applied to the columns -- it is right about what it
    covers, and discarding it would regress parity -- so the frame delivers an adjustment and
    reports it as incomplete rather than quietly delivering less than it promises.

    Volume and the share-count basis
    --------------------------------
    The share-count factor restates prices onto today's share count (``price / F``), and Volume is
    restated by its inverse (``volume * F``) so that ``price * volume`` -- the rupee value traded --
    is one quantity across the boundary instead of stepping by exactly ``F`` there. The as-traded
    bhavcopy mixes the two share-count bases in a single column, which the RELIANCE measurement in
    :data:`VOLUME_COLUMN` demonstrates. ``attrs["price_basis"]`` states the result on every frame.

    The local mirror, and what it is allowed to do
    -----------------------------------------------
    :mod:`bfinance.mirror` is a SQLite copy of the bhavcopy tape. It is **opt-in**
    (``fetch_history(mirror=...)``, or :data:`MIRROR_ENV_VAR` for callers of the yfinance-shaped
    API), and with it off the path here is unchanged: no store is constructed, no file is opened,
    no query runs.

    The reason it needs a section rather than a paragraph is that it introduces a way to be wrong
    this module could not previously be. A mirror read returns zero rows for a security that did
    not list, for a day before it listed, and for a day nobody has downloaded yet, and all three
    look identical. So the store's coverage watermark, not its row count, decides whether it may
    answer; a day it does not hold is fetched from NSE or the call raises, and is never returned
    as a short frame. ``attrs["bfinance_source"]`` (``"network"`` / ``"mirror"`` /
    ``"mirror+network"``) and ``attrs["bfinance_mirror"]`` (the full record, including
    ``days_missing``) are published on every frame, the empty one included. The decision table
    is in :meth:`_fetch_bhavcopy_bars`.

    Failing over between price sources
    ----------------------------------
    :mod:`bfinance.sources` is a complete, tested failover engine over the price sources -- a
    try-order, a health ledger, a basis check, and an attempt ledger. It is reachable from
    ``history()`` through ``source=`` (one vendor) and ``sources=`` (an ordered list to fail over
    through), and it is reached ONLY through one of those or through an injected
    :class:`PriceSourceRouter`. **With neither, the direct bhavcopy reader runs and the registry
    is never imported**, so the shipped default is unchanged and every existing two-argument
    ``OHLCVEngine(...)`` construction behaves exactly as it did.

    It is opt-in for a measured reason, not a cautious one. The price sources are on three
    different bases and are not interchangeable: HDFCBANK's close on the second vendor's feed is
    exactly 0.5x the bhavcopy's on all 62 sessions of 2024-12-16..2025-03-13, a 1:1 bonus
    back-applied, so an as-traded request served from it -- or spliced with it -- would carry a
    move that never happened. The cascade refuses to spend a request on a source that cannot
    serve the requested basis at all, and the winner's frame is used whole with its own basis
    published. The section above, on the four price-source attrs keys, is the full contract.
    """

    #: The closed vocabulary of ``period`` values, and their lookback in calendar days.
    #:
    #: Keys are matched case-insensitively, and the vocabulary is deliberately closed: anything
    #: not in here raises rather than falling back to the default, because a period that silently
    #: becomes "five years" is indistinguishable, in the returned frame, from a period that
    #: silently became the bhavcopy archive floor (2024-01-01) -- both are "all the data there is".
    #: A caller asking for ``'2w'`` or ``'1M'`` used to get the whole archive; they now get a
    #: ``ValueError`` naming the valid values. ``None`` / ``''`` is the one non-value that is
    #: accepted, and it means "no period given, use the default lookback" (1825 days, widened by
    #: ``start`` if one is passed). ``'1mo'`` is one month; ``'1m'`` is deliberately absent, since
    #: yfinance reads it as one *minute* and a human reads it as one month, and this module's bar
    #: source is daily.
    PERIOD_DAYS_MAP = {
        "1d": 2,
        "5d": 7,
        "1mo": 30,
        "3mo": 90,
        "6mo": 180,
        "1y": 365,
        "2y": 730,
        "3y": 1095,
        "5y": 1825,
        "10y": 3652,
        "ytd": 365,
        "max": 10000,
    }

    def __init__(self, screener_client: ScreenerClient, corporate_actions_client=None,
                 price_sources: Optional[PriceSourceRouter] = None):
        """Args:
        screener_client: the Screener.in client, used by the legacy close-only path and by the
            labelled fiscal-year-end fallback for corporate actions.
        corporate_actions_client: optional
            :class:`bfinance.nse.corporate_actions.CorporateActionsClient`. Injected by tests and by
            a caller who wants one configured client (proxy, timeout, pacing) shared across a
            batch. When ``None`` the real client is constructed per history call, so the default
            behaviour is unchanged and no new dependency is introduced.
        price_sources: optional :class:`PriceSourceRouter` -- the seam onto
            :mod:`bfinance.sources.cascade`, which is the package's failover engine for price
            history. It replaces nothing: with ``None`` (the default) a history call still goes
            straight to :meth:`_fetch_bhavcopy_bars` and the registry is never imported, so every
            existing two-argument construction is byte-identical. Supplying one -- or naming
            ``source=``/``sources=`` on a call -- routes the vendor choice through the cascade,
            and a supplied ``fetch`` is how an offline caller keeps that route from reaching a
            real vendor.
        """
        self.screener = screener_client
        self.corporate_actions = corporate_actions_client
        self.price_sources = price_sources

    @staticmethod
    def _history_columns(auto_adjust: bool, back_adjust: bool, actions: bool) -> list:
        cols = ["Open", "High", "Low", "Close", "Volume"]
        if not auto_adjust and not back_adjust:
            cols.insert(4, "Adj Close")
        if actions:
            cols.extend(["Dividends", "Stock Splits"])
        return cols

    async def fetch_history(
        self,
        symbol: str,
        period: str = "1mo",
        interval: str = "1d",
        start: Optional[Union[str, datetime]] = None,
        end: Optional[Union[str, datetime]] = None,
        prepost: bool = False,
        actions: bool = True,
        auto_adjust: bool = False,
        back_adjust: bool = False,
        repair: bool = False,
        keepna: bool = False,
        rounding: bool = False,
        timeout: int = 10,
        raise_errors: bool = False,
        ohlc: Literal["bhavcopy", "synthetic"] = "bhavcopy",
        adjust_corporate_actions: bool = True,
        mirror=None,
        source: Optional[str] = None,
        sources: Optional[Sequence[str]] = None,
    ) -> pd.DataFrame:
        """
        Fetch historical OHLCV DataFrame matching exact yfinance schema:
        Columns: ['Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume'] (+ ['Dividends', 'Stock Splits'] if actions=True)
        Index: pd.DatetimeIndex localized to Asia/Kolkata (yfinance NSE convention).

        Args:
            period: Lookback window, from the closed vocabulary in :data:`PERIOD_DAYS_MAP`
                (``'1d'``, ``'5d'``, ``'1mo'``, ``'3mo'``, ``'6mo'``, ``'1y'``, ``'2y'``,
                ``'3y'``, ``'5y'``, ``'10y'``, ``'ytd'``, ``'max'``), case-insensitive.
                ``''``/``None`` means "no period given" and takes the 5-year default lookback.
                Anything else raises -- see the note on the map for why an unrecognised period
                must not quietly become the longest window bfinance has.
            ohlc: Bar source. ``"bhavcopy"`` (default) returns real published O/H/L from the
                NSE bhavcopy archive and reports ``attrs["bfinance_synthetic_ohlc"] = False``.
                ``"synthetic"`` is the explicit opt-in for the legacy close-only Screener path
                that derives Open/High/Low from Close; it reports the flag as ``True``.

                **Kept, not replaced.** It is the legacy spelling of "which feed", and it is the
                only one of the two that can ask for a feed which is not in the source registry
                at all. How the two interact is fixed, and deliberately so:

                * ``ohlc=`` chooses the **bar shaping** (published O/H/L, or O/H/L derived from
                  Close). ``source=``/``sources=`` choose the **vendor**. They are orthogonal, and
                  neither implies the other.
                * Naming neither is the shipped default and does not engage the cascade at all --
                  the direct bhavcopy reader runs and the registry is never imported.
                * ``ohlc="synthetic"`` and a named source are **mutually exclusive** and the
                  combination raises. The synthetic path is not a registry row: it derives
                  Open/High/Low from Close with a +/-0.2% envelope, and no registry source does
                  that. Routing it through the cascade would either invent a row that does not
                  exist or silently swap in a different vendor's real O/H/L, which is the one
                  thing ``attrs["bfinance_synthetic_ohlc"]`` exists to prevent.
            source / sources: **one** price source, or an **ordered list to fail over through**,
                named by its :mod:`bfinance.sources.registry` name (``"nse-bhavcopy"``,
                ``"marketlens-close"``, ``"trendlyne-ohlc"``, ``"screener-chart"``, ...). This is
                the opt-in that puts :mod:`bfinance.sources.cascade` on the path; with neither
                named the call behaves exactly as it did before that layer existed.

                The list is the failsafe: providers are tried in the order given and the first
                one that answers wins, so ``sources=["nse-bhavcopy", "marketlens-close"]`` is a
                genuine second attempt on the *same* price basis, and the call raises only if
                every one of them fails. An unknown name raises :class:`ValueError` **before any
                request is spent** -- see :func:`resolve_price_source_names`.

                Basis routing is automatic and it is not cosmetic. The registry declares which
                basis each source publishes, and the cascade **never calls** a source that cannot
                serve the requested one: asking for as-traded prices skips
                ``"trendlyne-ohlc"`` on basis, spends zero requests on it, and records the skip
                in ``attrs["bfinance_price_source_attempts"]``. That is measured, not stylistic --
                HDFCBANK's close on the second vendor's feed is exactly 0.5x the bhavcopy's on all
                62 sessions of 2024-12-16..2025-03-13 (the 1:1 bonus back-applied), so an
                as-traded request served from it would carry a move that never happened. When
                the winner is a split/bonus-adjusted source, its bars are used **whole**: this
                engine applies no further share-count factor on top, and
                ``attrs["price_basis"]`` reads :data:`PRICE_BASIS_VENDOR_SPLIT_ADJUSTED` rather
                than claiming an adjustment bfinance did not make.

                Four attrs keys report what the cascade did, on the populated frame and the empty
                one alike -- ``bfinance_price_source`` (the winning **vendor**, not the
                transport), ``bfinance_price_source_attempts``,
                ``bfinance_price_source_degraded`` and ``bfinance_price_source_detail``. The
                return type does not change: a failover is an ordinary DataFrame.
            adjust_corporate_actions: ``True`` (default) back-adjusts Open/High/Low/Close by the
                cumulative SHARE-COUNT factor, which is what yfinance's own ``auto_adjust=False``
                series carries -- bhavcopy is as-traded and Yahoo's raw OHLC are not, so without
                this the two disagree by the full factor over entire date ranges (RELIANCE,
                2024-09-01..2024-11-15: 14/53 bars matched, the other 39 off by 2.0x across the
                28-Oct 1:1 bonus). The factor is the product over the instrument's whole
                corporate-action history, so an ex-date outside the window still restates the
                window (HDFCBANK's 2025-08-26 bonus halves all 53 bars of a 2024-09..2024-11
                request). ``False`` returns the as-traded series, which is what the bhavcopy
                natively is and what trade reconstruction needs. Volume is restated on this path by
                the factor's inverse rather than by the factor itself, so that ``price * volume``
                stays the rupee value traded; see the class docstring. Dividends never touch
                O/H/L/C -- see the class docstring.
                ``attrs["bfinance_corporate_actions_applied"]`` records which basis came back, in
                every combination, and reads ``False`` whenever a known action class was left
                unconverted. ``attrs["bfinance_adjustment_incomplete"]`` is then a dict naming why
                (``None`` when the adjustment is complete), and ``attrs["price_basis"]`` names the
                basis in a machine-readable form.
            auto_adjust: Refused when True (:class:`AdjustmentNotAvailableError`) -- see below.
            mirror: opt in to reading the local :mod:`bfinance.mirror` SQLite tape before the
                network. ``None`` (the default) consults :data:`MIRROR_ENV_VAR` instead and, with
                that unset, is **off**: no ``Mirror`` is constructed, no file is opened, no query
                runs, and the returned values are unchanged. Pass a path, or ``True`` for
                ``~/.bfinance/mirror.db``, or a ``Mirror`` you already hold. Applies to
                ``ohlc="bhavcopy"`` only -- the mirror holds the bhavcopy tape, not the
                close-only Screener series, so the synthetic path never consults it.

                A mirror that HOLDS the window answers from disk and sets
                ``attrs["bfinance_source"] = "mirror"``. A mirror that does not hold it does
                **not** get to answer with a short frame: :meth:`Mirror.coverage` is consulted
                first, and the missing trading days are fetched from NSE, so the frame is
                complete or the call raises. ``attrs["bfinance_mirror"]["days_missing"]`` names
                them either way. See :meth:`_fetch_bhavcopy_bars` for the full decision table.

        yfinance 1.7.0 parity notes (measured live, bhavcopy / yfinance over
        2024-09-01..2024-11-15):
        - auto_adjust defaults to False and True is refused -- but for a *column-set* reason now,
          not a missing-input one. The default path already returns yfinance's adjusted basis, so
          the only thing auto_adjust=True would change is dropping "Adj Close" and the action
          columns, which bfinance keeps.
        - The default path is back-adjusted and matches yfinance bar-for-bar: RELIANCE and
          HDFCBANK were 14/53 and 0/53 before the fix and 53/53 (all four of O/H/L/C, to within
          1e-4) after. With no dividend ex-date inside the window -- which is the case for both --
          "Adj Close" equals "Close", as it must for a share-count-adjusted series.
        - **``Adj Close`` is not always equal to ``Close``, and must not be.** A dividend ex-date
          inside the window is what separates them, because yfinance's raw OHLC do not carry
          dividends (TCS: factor 1.000000 despite Rs 238 paid after the window) while its
          "Adj Close" does (TCS Adj Close / Close = 0.929060 on 2024-09-02).
        - Known gap, measured and not papered over: a dividend with an ex-date AFTER the window
          does not reach bfinance's ``Adj Close``, because 1 - D / prev_close needs the close on
          the day before that ex-date and the bhavcopy fetch stops at ``end``. Substituting the
          last close in the window would fabricate a factor, so those are left out. The measured
          size: RELIANCE 0.86%, HDFCBANK 3.03% on 2024-09-02. O/H/L/C are unaffected (dividends
          are not on them), and the share-count factor has no such gap because it needs no price.
        - back_adjust=True drops "Adj Close" (no-op scale, same reason).
        - end dates are exclusive (start inclusive), matching yfinance.
        - interval="1wk" resamples daily bars into Monday-labeled weekly
          bars (Open=first, High=max, Low=min, Close=last, Volume=sum,
          Dividends=sum, Stock Splits=max). interval="1mo" resamples to
          month-start bars with the same aggregation.
        - repair=True is not implemented (yfinance itself needs scipy for it,
          missing in this env) -> NotImplementedError instead of silently
          returning unrepaired data. Unsupported intraday intervals also raise.
        - prepost/keepna/back_adjust are honored as explicit no-ops for NSE
          daily bars (yfinance returns identical frames for them here).

        Raises:
            AdjustmentNotAvailableError: ``auto_adjust=True``.
            RealOHLCUnavailableError: ``ohlc="bhavcopy"`` and the archive could not serve the
                window. Never answered with fabricated bars.
            TickerNotFoundError: the archive answered but holds no rows for this symbol and
                ``raise_errors=True``.
            bfinance.sources.errors.SourceUnavailableError: the cascade ran and **every** named
                or eligible source failed. The message names each one and the reason it gave.
                Note this is an ``UpstreamServiceError`` subclass, so a caller already writing
                ``except UpstreamServiceError`` around a data fetch needs no new except clause.
            bfinance.sources.errors.SourceBasisError: the basis the named sources publish is not
                one bfinance can serve for this request, or no registered source publishes it.
                Deliberately a ``ValueError``, not an unavailability: nothing is broken, and
                retrying cannot help.
            ValueError: unknown ``ohlc`` source, a ``period`` that is not in
                :data:`PERIOD_DAYS_MAP`, or a ``source=``/``sources=`` name that is not
                registered for price history. All three are caller bugs, decided locally, before
                any request is spent learning something knowable.
        """
        if repair:
            raise NotImplementedError("repair=True is not supported by bfinance")
        if ohlc not in OHLC_SOURCES:
            raise ValueError(f"ohlc must be one of {OHLC_SOURCES}, got {ohlc!r}")
        # Refused here, next to the other "which feed" checks, for the same reason they are: a
        # source name is a local vocabulary. Validating after the first fetch would spend a
        # request per symbol discovering a typo, and tests/test_silent_fallbacks.py pins exactly
        # that timing for download().
        price_names = resolve_price_source_names(
            source, sources, registry=getattr(self.price_sources, "registry", None)
        )
        if price_names and ohlc == "synthetic":
            raise ValueError(
                f"ohlc='synthetic' cannot be combined with source=/sources={list(price_names)}. "
                f"The synthetic path reads the close-only Screener chart feed and DERIVES "
                f"Open/High/Low from Close, and no source in the price registry does that -- so "
                f"there is no row to route it to. Pick one: keep ohlc='synthetic' with its own "
                f"feed, or keep ohlc='bhavcopy' (the default) and choose the vendor with "
                f"source=/sources=."
            )
        if auto_adjust:
            raise AdjustmentNotAvailableError(
                "auto_adjust=True is still refused by bfinance, but the reason is now a "
                "column-set difference rather than a missing input. Adjustment factors DO "
                "exist and are real: bfinance reads dividends (true ex-date, rupees actually "
                "payable) and bonus / face-value-sub-division ratios from NSE's own "
                "corporate-actions feed (bfinance.nse.corporate_actions), and it already "
                "applies them -- the default auto_adjust=False path returns a back-adjusted "
                "series, with Open/High/Low/Close carrying the cumulative share-count factor "
                "over the instrument's whole history and 'Adj Close' carrying that plus the "
                "dividend factor, which is the yfinance basis. yfinance's "
                "auto_adjust=True answers the same question with a DIFFERENT column set: it "
                "drops 'Adj Close' and the corporate-action columns. bfinance keeps them, so "
                "accepting True would silently change which columns come back -- a worse "
                "surprise than a refusal. For the as-traded series (what actually printed on "
                "the exchange) pass adjust_corporate_actions=False; either way "
                "df.attrs['bfinance_corporate_actions_applied'] records the basis of the "
                "prices you got, and df.attrs['bfinance_actions_source'] records where the "
                "factor came from."
            )
        norm_interval = (interval or "1d").lower()
        if norm_interval in ("1d", "1day", "daily"):
            resample = None
        elif norm_interval in ("1wk", "1w", "wk", "weekly"):
            resample = "weekly"
        elif norm_interval in ("1mo", "1m", "monthly"):
            resample = "monthly"
        else:
            raise NotImplementedError(f"interval={interval!r} is not supported by bfinance")

        # A period the caller NAMED but bfinance does not know is a caller bug, and it used to be
        # invisible. `days` defaulted to 1825 and anything not in the map was skipped, so every
        # unrecognised period requested five years, which the archive clamp then pinned to
        # 2024-01-01. Measured through to the requested window before this fix (today = 2026-09-28):
        # '1mo' -> 2026-08-29, '1d' -> 2026-09-26, and 'BANANA', '2w', '1M' and '' all -> 2024-01-01
        # -- four different questions, one answer, no raise and no warning. '1M' is the sharpest:
        # a human types it meaning one month, it lowercases to '1m', and the map key is '1mo'.
        if period:
            key = str(period).lower()
            if key not in self.PERIOD_DAYS_MAP:
                raise ValueError(
                    f"period={period!r} is not a period bfinance knows, and answering it with a "
                    f"different window would be a silent lie. Valid periods (case-insensitive): "
                    f"{', '.join(sorted(self.PERIOD_DAYS_MAP))}. One month is '1mo' -- '1m' is not "
                    f"accepted here, because it reads as one minute to yfinance and one month to a "
                    f"human while bfinance's bars are daily. For a window of your own, pass "
                    f"start=/end=; for the default lookback, pass period='' or period=None."
                )

        if prepost:
            logger.debug("%s: prepost=True is a no-op for NSE daily bars.", symbol)
        if back_adjust:
            logger.debug("%s: back_adjust=True applied as no-op (feed is unadjusted).", symbol)

        clean_symbol = normalize_symbol(symbol)

        # No period given (None or ""): 1825 days, widened below by `start` if one was passed.
        # That is a stated default, not a fallback for a value we failed to understand -- an
        # unrecognised value raised above and never reaches this line.
        days = 1825
        if period:
            days = self.PERIOD_DAYS_MAP[str(period).lower()]

        # If start date provided, calculate days from today
        if start:
            start_dt = pd.to_datetime(start)
            now_dt = pd.to_datetime(datetime.now())
            delta_days = (now_dt - start_dt).days + 10
            days = max(days, delta_days)

        synthetic = ohlc == "synthetic"
        # Two independent reasons the cascade can be in force, and both must hold: the caller
        # named a source, or the engine was built with a PriceSourceRouter. Neither alone is a
        # default, and with neither the direct bhavcopy reader below runs exactly as it did
        # before the cascade existed.
        cascade_on = bool(price_names) or self.price_sources is not None
        if synthetic:
            bars = await self._fetch_synthetic_bars(clean_symbol, days, raise_errors)
            # The mirror holds the bhavcopy tape, not the close-only Screener series, so there is
            # nothing for it to answer. Recorded rather than left blank, so a consumer reading one
            # key on every frame does not have to know which of the two feeds was used.
            mirror_state = _mirror_state(
                reason="ohlc='synthetic' reads the close-only Screener feed, which the "
                       "bhavcopy mirror does not hold"
            )
            price_record = self._direct_price_source_record(
                "synthetic", router_present=self.price_sources is not None
            )
        elif cascade_on:
            bars, mirror_state, price_record = await self._fetch_cascade_bars(
                symbol, clean_symbol, start, end, days, price_names, mirror
            )
            if price_record["degraded"]:
                # One line for whoever is watching the log now; the frame carries the same
                # sentence in attrs. Loud in the log, quiet in the return type.
                logger.warning(
                    "%s: price source failover. %s", symbol, price_record["detail"]
                )
        else:
            bars, mirror_state = await self._fetch_bhavcopy_bars(
                symbol, clean_symbol, start, end, days, raise_errors, mirror
            )
            price_record = self._direct_price_source_record("bhavcopy", router_present=False)
        source = mirror_state.get("source") or SOURCE_NETWORK

        if bars.empty:
            cols = self._history_columns(auto_adjust, back_adjust, actions)
            empty = pd.DataFrame(
                columns=cols,
                index=pd.DatetimeIndex([], name="Date", tz=IST),
            )
            empty.attrs["bfinance_synthetic_ohlc"] = synthetic
            # No bars means no adjustment could be performed. Say so, rather than leaving the key
            # absent and letting a consumer assume the default.
            empty.attrs["bfinance_ohlc_source"] = ohlc
            empty.attrs["bfinance_actions_source"] = ACTIONS_SOURCE_NONE
            empty.attrs["bfinance_actions_source_detail"] = (
                "no bars in the requested window, so no corporate actions were resolved"
            )
            empty.attrs["bfinance_corporate_actions_applied"] = False
            empty.attrs["bfinance_corporate_actions_applied_detail"] = (
                "no bars in the requested window, so there is nothing to adjust"
            )
            # Present on every frame, complete or not, so a consumer can branch on one key instead
            # of probing for the absence of one.
            empty.attrs["bfinance_adjustment_incomplete"] = None
            empty.attrs["price_basis"] = (
                PRICE_BASIS_SYNTHETIC if synthetic else PRICE_BASIS_AS_TRADED
            )
            # Present on the empty frame too, and None rather than a guess: there are no
            # bars, so the spacing is genuinely unknown. A consumer that reads one key on
            # every frame must not have to special-case the empty one.
            empty.attrs["bfinance_bar_interval"] = None
            empty.attrs["bfinance_bar_interval_mismatch"] = None
            # Provenance, on the empty frame too. An empty frame is precisely the frame a caller
            # is most likely to treat as "nothing went wrong", so it is the worst place to leave
            # the question of where the answer came from unanswered.
            empty.attrs["bfinance_source"] = source
            empty.attrs["bfinance_mirror"] = mirror_state
            # The four price-source keys, on the EMPTY frame too. An empty frame is exactly the
            # frame a consumer is most likely to treat as "nothing went wrong", so it is the
            # worst place to leave "which vendor, and did anything fail on the way" unanswered.
            # A consumer reads one key on every frame instead of probing for the absence of one.
            self._publish_price_source(empty, price_record)
            return empty

        df = bars.copy()

        actions_source = None
        actions_detail = ""
        actions_extra: dict = {}
        splits_history = None
        dividends_history = None
        if actions:
            # Real dividends/splits with true ex-dates from NSE's own corporate-actions feed.
            #
            # The whole history is requested, not the bar window, and that is deliberate: the feed
            # answers with an empty list both for "nothing happened in this window" and for "I do
            # not index this instrument", and only a full-history answer tells the two apart. The
            # window is applied on merge (merge_actions_into_history joins by date), so the wider
            # request costs a few kilobytes and buys a meaningful empty answer.
            #
            # It is also what makes the share-count adjustment possible at all: a bonus restates
            # every bar before its ex-date, so the product has to run over events the window never
            # shows (HDFCBANK's 2025-08-26 1:1 bonus halves all 53 bars of a 2024-09..2024-11
            # request). The full series is therefore kept here, not just the in-window merge.
            #
            # One request per symbol, and a range is always sent -- the per-symbol feed is capped
            # at 20 rows without one, which would silently truncate the dividend history.
            try:
                resolved = await CorporateActionsEngine.resolve_actions_async(
                    clean_symbol,
                    client=self.corporate_actions,
                    load_profile=self._load_profile,
                )
                df = CorporateActionsEngine.merge_actions_into_history(
                    df, dividends=resolved.dividends, splits=resolved.splits
                )
                # The full-history share-count series is what makes the price adjustment possible
                # at all -- see the note where the factor is built.
                splits_history = resolved.splits
                dividends_history = resolved.dividends
                actions_source = resolved.source
                actions_detail = resolved.detail
                actions_extra = dict(resolved.extra or {})
                df.attrs.update(resolved.extra)
            except Exception as e:  # noqa: BLE001 - resolve_actions_async already degrades
                logger.warning(
                    "%s: corporate actions could not be merged (%s); using zeros, which mean "
                    "'not known' rather than 'none happened'.",
                    symbol, e,
                )
                df["Dividends"] = 0.0
                df["Stock Splits"] = 0.0
                actions_source = ACTIONS_SOURCE_NONE
                actions_detail = f"merge failed: {e}"

        # Two factors, because yfinance uses two, and using one for both is the defect this
        # replaces. Measured live against yfinance 1.7.0 `auto_adjust=False` (bhavcopy / yfinance):
        #
        #   RELIANCE  2024-09..2024-11   2.000000   <- 1:1 bonus 2024-10-28
        #   HDFCBANK  2024-09..2024-11   2.000000   <- 1:1 bonus 2025-08-26 (outside the window)
        #   TCS       2024-09..2024-11   1.000000   <- Rs 238 of later dividends, factor 1.0
        #
        # TCS is the load-bearing row: its raw OHLC are NOT restated for dividends, which is
        # exactly what yfinance's own code does (`utils.auto_adjust` scales by Adj Close / Close,
        # and Yahoo's raw OHLC are already split-adjusted). So the SHARE-COUNT factor goes on the
        # price columns and the DIVIDEND factor goes on Adj Close alone. Applying the dividend
        # factor to O/H/L/C would put TCS 7.6% away from yfinance.
        #
        # The share-count factor is the product over the WHOLE history, not over the window, and
        # that is the whole HDFCBANK case: its bonus ex-date is 2025-08-26, so for a
        # 2024-09..2024-11 request no bar in the frame mentions it while yfinance halves all 53.
        # The synthetic path keeps its legacy in-window factor instead, byte for byte, because
        # that path is an explicit opt-in that this change does not touch.
        #
        # Order matters: the dividend step divides by the previous AS-TRADED close, so both
        # factors are derived from the un-scaled frame before any column is multiplied.
        #
        # A vendor-adjusted winner is the one case where the share-count factor must NOT be built
        # at all. `trendlyne-ohlc` publishes as_traded_split_bonus_adjusted, so its bars already
        # carry the cumulative share-count restatement; multiplying by the same factor again
        # halves every bar a second time. It is 1.0 here rather than skipped downstream so every
        # later step -- the column scaling, Adj Close, Volume's inverse -- runs unchanged and the
        # winner's data is used whole. Measured: HDFCBANK, 2024-12-16..2025-03-13, the vendor
        # close is exactly 0.5x the bhavcopy's on all 62 sessions.
        vendor_adjusted = bool(price_record.get("vendor_adjusted"))
        split_source = df.get("Stock Splits") if synthetic else splits_history
        if vendor_adjusted:
            split_factor = pd.Series(1.0, index=df.index)
        else:
            split_factor = _split_adjustment_factor(df.index, split_source)
        dividend_factor = _dividend_adjustment_factor(df)
        adjusted_close = df["Close"].astype(float) * split_factor * dividend_factor

        factor_applied, applied_detail = self._resolve_adjustment(
            synthetic=synthetic,
            actions=actions,
            adjust_corporate_actions=adjust_corporate_actions,
            actions_source=actions_source,
            actions_detail=actions_detail,
        )
        # A factor was derived -- but is it the whole story? The feed may have recorded action classes
        # that are deliberately never converted into a factor (ITC's real 2025-01-06 demerger is
        # one: the as-traded close fell 8.0876% across the ex-date and the correct factor is
        # 1.047332, while the one derived here is exactly 1.000000). Claiming success in that state
        # is the false flag this closes.
        #
        # `scaled` and `applied` are deliberately two different facts, because the caller asked for an
        # adjustment and should still GET one. The bonus and sub-division factors that *were*
        # derived are real and correct for what they cover, and dropping them because an unrelated
        # demerger is unconverted would regress the yfinance parity this module exists to reach.
        # So the columns are scaled, and the frame says it did not finish:
        #   scaled  -> were the columns restated (what the data did)
        #   applied -> does the frame claim a COMPLETE adjustment (what attrs reports)
        # Only meaningful when a factor was actually derived. On the opt-out path there is no
        # adjustment to be incomplete, and reporting a declined row there would conflate "you asked
        # for no adjustment" with "we could not finish one" -- two different reasons for the same
        # False, already distinguished by bfinance_corporate_actions_applied_detail.
        incomplete = (
            _incomplete_adjustment(clean_symbol, actions_extra) if factor_applied else None
        )
        scaled = factor_applied
        applied = factor_applied
        if vendor_adjusted and applied:
            # _resolve_adjustment cannot know this: it is asked about the actions feed, and the
            # reason the factor is 1.0 is a property of the WINNING VENDOR, not of the feed. Left
            # alone it would write "back-adjusted by the cumulative share-count factor from
            # nse-corporate-actions" on a frame whose prices were not touched here at all.
            applied_detail = (
                f"the winning price source ({price_record.get('source')}) already publishes these "
                f"bars restated for splits and bonuses, with demergers NOT applied, so bfinance "
                f"applied no further share-count factor -- the two do not compose, and applying "
                f"both would halve every bar a second time. Adj Close additionally carries the "
                f"dividend factor for ex-dates inside the window. attrs['price_basis'] reads "
                f"{PRICE_BASIS_VENDOR_SPLIT_ADJUSTED!r} rather than claiming an adjustment bfinance "
                f"did not make."
            )
        if incomplete is not None and applied:
            applied = False
            applied_detail = (
                f"{applied_detail} -- but the adjustment is INCOMPLETE and this frame does not "
                f"claim parity. The share-count factor WAS applied to O/H/L/C and Volume; it is "
                f"just not the whole factor. {incomplete['detail']}"
            )
        if adjust_corporate_actions and not synthetic and actions and not applied:
            # The one degradation that is not the caller's own choice. attrs carries the label for
            # whoever inspects the frame later; this is for whoever is watching the log now.
            # Once per frame, not per row, and non-fatal: a consumer that wants to fail hard reads
            # attrs["bfinance_adjustment_incomplete"] / ["bfinance_corporate_actions_applied"]
            # instead of parsing this line. Deliberately a logger call, not warnings.warn -- the
            # module's degradation channel is logger + attrs, and raising a Python warning as well
            # would duplicate one fact through two channels that can drift apart.
            logger.warning(
                "%s: yfinance parity NOT established. %s. attrs"
                "['bfinance_corporate_actions_applied'] is False and "
                "attrs['bfinance_adjustment_incomplete'] names the reason.",
                symbol, applied_detail,
            )
        if scaled:
            for column in ADJUSTED_PRICE_COLUMNS:
                df[column] = df[column].astype(float) * split_factor
            # Volume takes the INVERSE factor, so that price * volume -- the rupee value traded --
            # is the same quantity on both sides of a share-count event instead of stepping by
            # exactly F there. See :data:`VOLUME_COLUMN` for the measurement that establishes the
            # direction. Rounded back to int64 because a share count is a whole number; the
            # rounding error is under one share, so the invariant holds to ~1e-7 on a real bar.
            if VOLUME_COLUMN in df.columns:
                scaled_volume = df[VOLUME_COLUMN].astype(float) / split_factor
                df[VOLUME_COLUMN] = np.rint(scaled_volume).astype(np.int64)
        # Adj Close always carries BOTH factors, on either path. That is what makes it usable: on
        # the adjusted path it is Close x the dividend factor, and on the as-traded path it is the
        # full adjusted series, recoverable from the published prices. The flag, not the column,
        # tells the two apart.
        df["Adj Close"] = adjusted_close

        # The one part of the adjustment this call cannot make, named rather than buried in a
        # docstring: a dividend with an ex-date after the last bar needs the close on the day
        # before it, and that bar is outside the fetch. Recorded so a consumer measuring against
        # yfinance can see the size of the gap instead of discovering it. (Measured: RELIANCE
        # 0.86%, HDFCBANK 3.03% on Adj Close, uniform across the window, zero effect on O/H/L/C.)
        if dividends_history is not None and not getattr(dividends_history, "empty", True):
            pending = dividends_history[
                (dividends_history.index > df.index[-1]) & (dividends_history > 0)
            ]
            if len(pending):
                df.attrs["bfinance_actions_unapplied_dividends"] = [
                    {"ex_date": str(stamp.date()), "amount": float(amount)}
                    for stamp, amount in pending.items()
                ]

        if rounding:
            df["Open"] = df["Open"].round(2)
            df["High"] = df["High"].round(2)
            df["Low"] = df["Low"].round(2)
            df["Close"] = df["Close"].round(2)
            df["Adj Close"] = df["Adj Close"].round(2)

        df = df[self._history_columns(auto_adjust, back_adjust, actions)]
        df.index.name = "Date"

        # Filter by start (inclusive) / end (exclusive) matching yfinance
        if start:
            start_ts = pd.to_datetime(start)
            if df.index.tz is not None and start_ts.tzinfo is None:
                start_ts = start_ts.tz_localize("Asia/Kolkata")
            df = df[df.index >= start_ts]
        if end:
            end_ts = pd.to_datetime(end)
            if df.index.tz is not None and end_ts.tzinfo is None:
                end_ts = end_ts.tz_localize("Asia/Kolkata")
            df = df[df.index < end_ts]

        if not keepna:
            df.dropna(how="all", inplace=True)

        if resample is not None and not df.empty:
            df = _resample_ohlcv(df, resample)
            # The resample rule IS the delivered spacing, so it is read off the request
            # rather than inferred back out of the index. Inference is only needed when
            # no resampling happened, which is the daily-request case and the one where
            # the feed can hand back something coarser than was asked for.
            delivered_interval = "1wk" if resample == "weekly" else "1mo"
        else:
            delivered_interval = _infer_bar_interval(df.index)

        interval_detail = _interval_mismatch_detail(norm_interval, delivered_interval)
        if interval_detail:
            # Logged as well as published. The package's degradation channel is
            # logger + attrs, and the two carry the same sentence so they cannot drift.
            logger.warning("%s: %s", symbol, interval_detail)

        if synthetic:
            df.attrs["bfinance_synthetic_ohlc"] = True
        else:
            df.attrs["bfinance_synthetic_ohlc"] = False
        # Two independent facts, both required, because either one alone lets a consumer believe
        # the wrong thing: WHERE the bars came from, and WHETHER those bars are restated onto
        # today's share count. A frame can be real-but-unadjusted (as-traded bhavcopy) and a
        # frame can be synthetic; neither implies the other.
        df.attrs["bfinance_ohlc_source"] = ohlc
        # WHERE the rows were read from, as distinct from what they are: "network" on the
        # default path, "mirror" when the local tape held the whole window, "mirror+network"
        # when it held part of it and NSE supplied the rest. A consumer asking "how stale can
        # this be?" reads this key and not the feed label.
        df.attrs["bfinance_source"] = source
        # The full mirror record, on every frame. ``days_missing`` is the honest-gap list, and
        # it is empty on every frame this method returns -- a non-empty one alongside rows would
        # mean a short frame dressed as a complete answer, which is the defect this whole
        # integration exists to make impossible.
        df.attrs["bfinance_mirror"] = mirror_state
        df.attrs["bfinance_corporate_actions_applied"] = applied
        df.attrs["bfinance_corporate_actions_applied_detail"] = applied_detail
        # The one key a consumer needs to detect a half-done adjustment without reading logs. It is
        # present on EVERY frame -- None when the adjustment is complete -- so the check is
        # ``df.attrs["bfinance_adjustment_incomplete"] is not None`` and never a probe for absence.
        df.attrs["bfinance_adjustment_incomplete"] = incomplete
        # Which basis the rupee prices in this frame actually sit on. Set on every path, including
        # the empty one, because a frame with no bars is exactly the frame a consumer is most
        # likely to treat as "nothing to worry about".
        df.attrs["price_basis"] = self._price_basis(
            synthetic=synthetic, scaled=scaled, vendor_adjusted=vendor_adjusted
        )
        # What the bars actually are spaced at, which is NOT the interval that was
        # requested. Present on every frame, including the empty one (where it is None,
        # because no bars exist to measure), so a consumer reads one key instead of
        # probing for its absence.
        df.attrs["bfinance_bar_interval"] = delivered_interval
        # None when the delivered spacing is what was asked for, or finer, or
        # undeterminable. A sentence naming both when it is coarser. The check is
        # ``df.attrs["bfinance_bar_interval_mismatch"] is not None``.
        df.attrs["bfinance_bar_interval_mismatch"] = interval_detail
        # Which feed produced the Dividends / Stock Splits columns, so a frame built on the
        # fiscal-year-end approximation is never mistaken for one carrying true ex-dates.
        df.attrs["bfinance_actions_source"] = actions_source or ACTIONS_SOURCE_NONE
        df.attrs["bfinance_actions_source_detail"] = actions_detail
        if actions and actions_source == ACTIONS_SOURCE_NSE:
            df.attrs["bfinance_adj_close_source"] = (
                "back-adjusted Close by NSE ex-dated corporate actions: O/H/L/C carry the "
                "share-count factor over the whole history, Volume carries its inverse, and "
                "Adj Close carries the share-count factor plus the dividend factor for ex-dates "
                "inside the window"
                if scaled
                else "back-adjusted Close by NSE ex-dated corporate actions (prices as traded; "
                "only Adj Close is restated)"
            )
        if actions and actions_source == ACTIONS_SOURCE_SCREENER and applied:
            df.attrs["bfinance_adj_close_source"] = (
                "back-adjusted Close by the labelled Screener.in fiscal-year-end approximation, "
                "whose ex-dates are ~4-5 months late; O/H/L/C carry the share-count factor and "
                "Adj Close adds the in-window dividend factor"
            )
        # The four price-source keys, last so nothing above can overwrite them. Set on the empty
        # frame too (see the early return), because a consumer must be able to read one key on
        # every frame this method returns rather than probing for the absence of one.
        self._publish_price_source(df, price_record)
        return df

    # ------------------------------------------------------------------ the price-source cascade
    #
    # Everything below is reached only when `source=`/`sources=` was named or a PriceSourceRouter
    # was injected. With neither, fetch_history still calls _fetch_bhavcopy_bars directly and the
    # registry is never imported -- which is the whole reason the shipped default is unchanged
    # and the reason the ~130 existing two-argument OHLCVEngine(...) constructions keep working.

    async def _fetch_cascade_bars(
        self,
        symbol: str,
        clean_symbol: str,
        start: Optional[Union[str, datetime]],
        end: Optional[Union[str, datetime]],
        days: int,
        names: Sequence[str],
        mirror,
    ) -> tuple:
        """Bars from the source cascade, plus the transport record and the provenance record.

        Returns ``(rows, mirror_state, price_record)``.

        The whole-market-to-one-symbol narrowing happens here rather than in each source, because
        the registry rows are deliberately inconsistent about it: ``nse-bhavcopy`` is a whole-market
        archive with no symbol argument, while the other three are per-symbol. Narrowing in one
        place is what lets the same post-processing run over any of them.

        The window is clamped to :data:`BHAVCOPY_FIRST_DATE` with the same log line the direct path
        uses, and deliberately so: the request the cascade carries is a *date range*, and a source
        that costs one request per day in the span it is handed must never be asked for 25 years
        because a caller wrote ``period="max"``. Clamping can only ever ask for less, and the
        per-source window filtering downstream (the ``start``/``end`` filter, and
        :func:`bfinance.sources.price_provider.narrow` on that vendor's own route) is unaffected.
        """
        # Lazy: tests/test_public_surface.py requires that `import bfinance` does not pull this
        # layer in, and this is the only place in the package that needs it.
        from bfinance.sources import cascade as sources_cascade

        router = self.price_sources
        registry = getattr(router, "registry", None) if router is not None else None
        health = getattr(router, "health", None) if router is not None else None
        fetch = getattr(router, "fetch", None) if router is not None else None

        start_ts = self._window_start(start, days)
        end_ts = pd.to_datetime(end) if end is not None else pd.Timestamp.now(tz=IST)
        floor = pd.Timestamp(BHAVCOPY_FIRST_DATE, tz=IST)
        if start_ts < floor:
            logger.warning(
                "%s: requested window starts %s, before the bhavcopy archive begins (%s). "
                "Returning only the covered range; use ohlc='synthetic' for older history.",
                symbol, start_ts.date(), BHAVCOPY_FIRST_DATE,
            )
            start_ts = floor

        request = {
            "symbol": clean_symbol,
            # The screener chart adapter reads `ticker`; every other adapter reads `symbol`.
            # Both are sent rather than one, because the adapters are the layer that knows each
            # entry point's parameter name and a request is a mapping, not a signature.
            "ticker": clean_symbol,
            "start": start_ts.strftime("%Y-%m-%d"),
            "end": end_ts.strftime("%Y-%m-%d"),
            "days": days,
            "capability": PRICE_CAPABILITY,
        }
        basis = self._requested_price_basis(names, registry)
        only = tuple(names) or None
        # The plan is computed here, not only inside resolve(), and that is the whole reason
        # `bfinance_price_source_attempts` can be a complete account. resolve() returns the moment
        # one provider answers, so its own `attempts` stops there -- which is right for a caller
        # asking "what happened" and wrong for a consumer reading the routing picture off a frame,
        # because the sources that were passed over AHEAD of the winner would be missing. plan()
        # computes every exclusion without calling anything, so merging the two gives the whole
        # try-order in the order it was actually considered.
        planned = sources_cascade.plan(
            PRICE_CAPABILITY, basis=basis, registry=registry, health=health, only=only
        )
        resolved = sources_cascade.resolve(
            PRICE_CAPABILITY,
            request=request,
            basis=basis,
            registry=registry,
            health=health,
            fetch=fetch,
            only=only,
        )
        # The winner's frame, used WHOLE. Not concatenated with anything, not re-based, not
        # blended with a fallback's bars: Basis.comparable() already refused the only two
        # sources that disagree, and stitching a second vendor's bars onto this one would put a
        # move in the series that never happened.
        value = resolved.value
        if not hasattr(value, "empty") or not hasattr(value, "columns"):
            raise RealOHLCUnavailableError(
                f"{symbol}: price source {resolved.source!r} answered a {PRICE_CAPABILITY!r} "
                f"request with {type(value).__name__}, which is not a frame. bfinance builds a "
                f"history out of bars and will not derive them from a value it does not "
                f"understand; a real feed serving an unexpected shape is a bug worth raising."
            )
        missing = [c for c in ("Open", "High", "Low", "Close") if c not in value.columns]
        if missing:
            raise RealOHLCUnavailableError(
                f"{symbol}: price source {resolved.source!r} answered a {PRICE_CAPABILITY!r} "
                f"request with a frame missing {missing}. Refusing to synthesize them: a "
                f"fabricated Open/High/Low is indistinguishable from a real one, which is the "
                f"defect this package exists to not produce."
            )
        bars = self._tidy_bars(
            symbol,
            _select_symbol_rows(value, clean_symbol),
            dict(getattr(value, "attrs", {}) or {}),
            label=f"{resolved.source} bars",
        )
        # The mirror is a transport for ONE vendor, and the cascade's whole job is choosing the
        # vendor. Recorded rather than silently skipped, because a caller who enabled the mirror
        # and then named a source would otherwise have no way to know their local tape stopped
        # being consulted -- and logged, because a silently-ignored cache is the same defect
        # shape as a silently-ignored cache hit.
        mirror_state = _mirror_state(
            reason=(
                "the price-source cascade was engaged (source=/sources= or an injected "
                "PriceSourceRouter), which routes the bhavcopy through bfinance.sources rather "
                "than through this module's mirror-aware reader; the local mirror is not consulted "
                "on that path"
            )
        )
        if self._mirror_spec(mirror) is not None:
            logger.warning(
                "%s: a local mirror is configured but the price-source cascade is in force, so the "
                "mirror is not consulted on this call. Drop source=/sources= to keep reading it, "
                "or accept that the vendor attrs['%s'] names is being fetched over the network.",
                symbol, PRICE_SOURCE_ATTR,
            )
        return bars, mirror_state, self._price_source_record(resolved, planned)

    @staticmethod
    def _requested_price_basis(names: Sequence[str], registry) -> Any:
        """The vendor basis this call must be served on, or a refusal naming the conflict.

        Derived from the sources the caller NAMED, not from the registry's first row, and that
        difference is the whole design. ``plan()``'s own default is "the first registered source's
        basis", which is right for a caller who named nothing and wrong for one who named
        ``sources=["trendlyne-ohlc"]``: taking it there would request as-traded prices from a
        source that publishes only split-adjusted ones, and the honest answer to that is a refusal
        rather than a silent substitution.

        Two named sources that publish different bases are refused for the same reason, with the
        conflict spelled out -- a failsafe list has to be on ONE basis to be a failsafe, and the
        error says which bases are actually on offer rather than making the caller read the
        registry.
        """
        from bfinance.sources.model import basis_from  # lazy: opt-in isolation

        reg = registry
        if not names:
            # The engine post-processes what it is given -- it applies the share-count factor
            # itself -- so it needs an UNADJUSTED series to start from. as_traded is also the only
            # price basis in the default registry with genuine redundancy (bhavcopy and
            # MarketLens), so it is the basis a failsafe can actually fail over on.
            return basis_from(DEFAULT_PRICE_BASIS)
        if reg is None:
            from bfinance.sources.registry import DEFAULT

            reg = DEFAULT
        shared: Optional[set] = None
        published: Dict[str, list] = {}
        for name in names:
            spec = reg.source(name)
            if spec is None:  # pragma: no cover - resolve_price_source_names already refused it
                continue
            published[name] = [b.value for b in spec.bases]
            available = {b.value for b in spec.bases}
            shared = available if shared is None else (shared & available)
        if not shared:
            raise ValueError(
                f"the price sources named publish bases that do not overlap, so they are not a "
                f"failsafe for one another and cannot be tried in order: {published}. A failsafe "
                f"list has to be on a single price basis, because bfinance will not splice two of "
                f"them into one series -- a 1:2 split would then read as a -50% move that never "
                f"happened. Name the sources for one basis, or run two calls."
            )
        if len(shared) > 1:
            raise ValueError(
                f"the price sources named publish several bases ({sorted(shared)}), so the basis "
                f"of this request is ambiguous: {published}. Name a single source, or name sources "
                f"that agree."
            )
        return basis_from(next(iter(shared)))

    @staticmethod
    def _price_source_record(resolved, planned=None) -> dict:
        """The four attrs values, as one dict the caller can inspect and the engine can spread.

        Built in one place so a frame cannot claim ``degraded=False`` while its own attempt list
        contains a failure -- the same rule :func:`bfinance.sources.cascade._finish` applies, for
        the same reason.

        ``degraded`` is taken from the cascade rather than recomputed, because the cascade's rule is
        the careful one: a source SKIPPED for being on the wrong basis is not degradation (the
        system did the only thing available) while a source ASKED and failed is (the caller wanted
        that answer and did not get it). Recomputing it here as "was anything skipped?" would make
        every as-traded request look degraded, because the adjusted sources are always skipped.

        ``planned`` is the cascade's own pre-computed exclusion list, merged in so the published
        ``attempts`` covers the WHOLE try-order rather than the prefix up to the winner. The
        resolution's own record wins on any source both know about, so this can add rows and never
        overwrite one; the order is the try-order either way, which is the order a reader needs.
        """
        records: Dict[str, dict] = {}

        def _payload(attempt) -> dict:
            return {
                "source": attempt.provider,
                "outcome": attempt.outcome,
                "called": attempt.called,
                "reason": attempt.reason,
                "basis": attempt.basis.value if attempt.basis is not None else None,
            }

        for attempt in resolved.attempts:
            records[attempt.provider] = _payload(attempt)
        for attempt in getattr(planned, "attempts", ()) or ():
            records.setdefault(attempt.provider, _payload(attempt))
        rank = {
            spec.name: index
            for index, spec in enumerate(getattr(planned, "ordered", ()) or ())
        }
        attempts = sorted(records.values(), key=lambda row: rank.get(row["source"], len(rank)))
        return {
            "source": resolved.source,
            "attempts": attempts,
            "degraded": bool(resolved.degraded),
            "detail": OHLCVEngine._price_source_detail(resolved, attempts),
            # Read by the adjustment step, not published as a key: it decides whether a
            # share-count factor may be applied at all. True when the winner published bars that
            # already carry one.
            "vendor_adjusted": resolved.basis.value != DEFAULT_PRICE_BASIS,
        }

    @staticmethod
    def _price_source_detail(resolved, attempts: Sequence[Mapping]) -> str:
        """One sentence naming what was tried and what failed, in the order it happened.

        A sentence and not a list, because the list is already on the frame under
        ``bfinance_price_source_attempts`` and this key exists for the reader who will not parse
        that. It distinguishes the three cases that must never be confused: answered first time,
        answered after a failure, and answered after everything eligible was passed over. The
        MERGED attempt list is passed in rather than ``resolved.attempts``, because the cascade
        stops recording the moment a provider answers and a sentence that omitted the sources
        passed over would describe a routing decision the frame's own list contradicts.

        A source passed over contributes only the FIRST sentence of its reason, and that is not
        cosmetics. A basis skip's reason is "<one authored sentence>. <the row's whole registry
        note>", and a registry note is a measured essay by design -- the one for the split-adjusted
        row is 900 characters of arithmetic about HDFCBANK. Pasting that into a key documented as
        a sentence produces a 4 KB "sentence" nobody reads, on every as-traded call. The ledger
        under ``bfinance_price_source_attempts`` keeps the whole reason, so the measurement still
        travels with the frame; this key is the summary.
        """
        failed = [a for a in attempts if a["outcome"] == "failed"]
        skipped = [a for a in attempts if not a["called"]]
        if not failed:
            passed = (
                "nothing ahead of it failed, so this is first choice and the frame is not "
                "degraded"
            )
        else:
            names = ", ".join(
                f"{a['source']} ({OHLCVEngine._brief(a['reason'])})" if a["reason"] else a["source"]
                for a in failed
            )
            passed = (
                f"it was asked first, failed ({names}), and the frame is therefore DEGRADED -- "
                f"second choice, not first"
            )
        tail = ""
        if skipped:
            tail = " " + "; ".join(
                f"{a['source']} was not asked: {OHLCVEngine._first_sentence(a['reason'])}"
                for a in skipped
            )
        return (
            f"{resolved.source} answered the {resolved.basis.value} price history on the "
            f"{resolved.basis_requested.value} basis, {passed}.{tail}"
        )

    @staticmethod
    def _brief(reason: Optional[str]) -> str:
        """A failure reason on one line, whole.

        :func:`bfinance.sources.cascade._short` already collapses a traceback into
        ``"ClassName: message"`` and caps it at 300 characters, so a failure needs no trimming --
        only whitespace, since these strings travel into ``attrs``.
        """
        return " ".join((reason or "").split())

    @staticmethod
    def _first_sentence(reason: Optional[str]) -> str:
        """The leading sentence of a skip reason, with the registry's note taken back off it.

        A basis skip reads ``"publishes only the split_adjusted basis, and the as_traded basis was
        requested. <the row's whole note>"``. The note is measured evidence and it is worth
        carrying -- in ``bfinance_price_source_attempts``, where a machine reads it. It is not
        worth carrying twice, in a key documented as a sentence.
        """
        text = OHLCVEngine._brief(reason)
        head, separator, _tail = text.partition(". ")
        if separator and head:
            return f"{head}."
        return text

    @staticmethod
    def _direct_price_source_record(ohlc: str, router_present: bool) -> dict:
        """The four attrs values for a frame the cascade did not build.

        Every value is the honest one rather than a plausible one. ``source`` is ``None`` and
        ``attempts`` is empty because no registry row was asked -- publishing a vendor name here
        would be a claim about a routing decision that was never made, and the feed is already
        named, truthfully, by ``attrs["bfinance_ohlc_source"]``. ``degraded`` is ``False``
        because there is no alternative to have been denied: the caller got the only thing this
        path can produce.
        """
        note = (
            " A PriceSourceRouter was injected on this engine, so the cascade WOULD have run; "
            "ohlc='synthetic' is the legacy close-only path and is deliberately not routed through "
            "it."
            if ohlc == "synthetic" and router_present
            else (
                " No PriceSourceRouter was injected and no source= / sources= was named, so the "
                "shipped direct path ran and no source was tried and no fallback was available."
                if ohlc != "synthetic"
                else ""
            )
        )
        return {
            "source": None,
            "attempts": [],
            "degraded": False,
            "detail": (
                f"the direct ohlc={ohlc!r} reader answered; no price-source cascade ran, so there "
                f"is no try-order, no attempt list and nothing to be degraded from.{note} The feed "
                f"is attrs['bfinance_ohlc_source'] and the transport is attrs['bfinance_source']; "
                f"name a vendor with source= / sources= to engage the cascade."
            ),
            "vendor_adjusted": False,
        }

    @staticmethod
    def _publish_price_source(frame: pd.DataFrame, record: Mapping) -> pd.DataFrame:
        """Write the four price-source keys onto a frame, populated or empty, and return it.

        All four are always present. A consumer reads ``attrs["bfinance_price_source_degraded"]``
        rather than probing for the key's absence, and an empty frame is precisely the frame a
        consumer is most likely to treat as "nothing went wrong".
        """
        frame.attrs[PRICE_SOURCE_ATTR] = record.get("source")
        frame.attrs[PRICE_SOURCE_ATTEMPTS_ATTR] = list(record.get("attempts") or ())
        frame.attrs[PRICE_SOURCE_DEGRADED_ATTR] = bool(record.get("degraded"))
        frame.attrs[PRICE_SOURCE_DETAIL_ATTR] = record.get("detail")
        return frame

    @staticmethod
    def _price_basis(synthetic: bool, scaled: bool, vendor_adjusted: bool = False) -> str:
        """Name the rupee-price basis of this frame, as one of the ``PRICE_BASIS_*`` constants.

        This is the attribute a consumer should branch on instead of inferring the basis from the
        values. Five sources on four different bases for one stock is a real hazard -- bhavcopy is
        the exchange print, yfinance's raw OHLC are already split-adjusted, and the two differ by
        the full action factor over whole date ranges -- so the package that resolves them is the
        one place that should say which one it used.

        Deliberately *not* named ``"yfinance_adjusted"``. It is not yfinance parity: no demerger,
        buy-back, rights issue or per-unit distribution is applied, and no dividend reaches
        O/H/L/C. ``as_traded_share_count_adjusted`` says what was done instead of claiming parity
        that the unconverted-row gap makes untrue.

        ``vendor_adjusted`` is the third case and is checked BEFORE ``scaled``, because the two
        would otherwise report the same string. When the cascade's winner published split/bonus
        restated bars, ``scaled`` can legitimately be ``True`` -- bfinance did derive a factor --
        while having multiplied by 1.0, so ``as_traded_share_count_adjusted`` would be a false
        claim about this frame. The winner's own basis is published instead.
        """
        if synthetic:
            return PRICE_BASIS_SYNTHETIC
        if vendor_adjusted:
            return PRICE_BASIS_VENDOR_SPLIT_ADJUSTED
        return (
            PRICE_BASIS_SHARE_COUNT_ADJUSTED if scaled else PRICE_BASIS_AS_TRADED
        )

    @staticmethod
    def _resolve_adjustment(
        synthetic: bool,
        actions: bool,
        adjust_corporate_actions: bool,
        actions_source: Optional[str],
        actions_detail: str,
    ) -> tuple:
        """Decide whether the factor is applied to the price columns, and say why either way.

        Returns ``(applied, detail)``. The detail string is not decoration: it is the only place a
        consumer learns WHICH of the four not-adjusted cases they are in, and the rule is that
        there is no silent fifth case. Every path out of here either applies the factor or names the
        reason it did not.

        There IS a fifth case -- a factor that was applied but is not the whole factor, because the
        feed recorded action classes this engine declines to convert. It cannot be decided here,
        because it needs the resolved actions' ``extra``, so it is layered on top by
        :func:`_incomplete_adjustment` in the caller. What matters is that the layering happens
        *before* the columns are scaled, so the flag and the data can never disagree: this method
        answers "was a factor derived", the incompleteness check answers "is it complete", and
        attrs reports the second.

        A requested-but-unresolvable factor degrades to as-traded plus this label rather than
        raising. bfinance's convention is lenient degradation -- an answer with a truthful label --
        and the alternative (a raise) would take out a price feed that is otherwise perfectly
        good, because a corporate-actions endpoint had a bad minute. What is never acceptable is
        the third option, which is silently unadjusted.
        """
        if synthetic:
            # The close-only Screener feed has no corporate-action series of its own, and this
            # path is already flagged as fabricated OHLC. Touching it would be a second change
            # to a frame whose contract is "unchanged legacy behaviour".
            return False, (
                "synthetic path: close-only Screener feed with derived Open/High/Low is never "
                "back-adjusted"
            )
        if not adjust_corporate_actions:
            return False, (
                "as-traded bhavcopy prices, requested explicitly (adjust_corporate_actions=False)"
            )
        if not actions:
            # The factor is derived from the actions, so asking for no actions means there is
            # nothing to apply. Recorded rather than quietly assumed, because the caller did ask
            # for the default basis and did not get it.
            return False, (
                "corporate actions were not requested (actions=False), so no factor exists to "
                "apply; pass actions=True for the yfinance-parity adjusted basis"
            )
        if actions_source in (None, ACTIONS_SOURCE_NONE):
            return False, (
                "as-traded bhavcopy prices: no corporate-action source could be resolved, so "
                f"yfinance parity is NOT established. {actions_detail}"
            )
        if actions_source == ACTIONS_SOURCE_SCREENER:
            return True, (
                "O/H/L/C back-adjusted by the labelled Screener.in fiscal-year-end approximation; "
                "amounts track yfinance but ex-dates run ~4-5 months late, so this basis is "
                "approximate. See bfinance_actions_source_detail"
            )
        return True, (
            f"O/H/L/C back-adjusted by the cumulative share-count factor from {actions_source} "
            f"(over the instrument's whole history); Adj Close adds the dividend factor for "
            f"ex-dates inside the window"
        )

    async def _load_profile(self, symbol: str):
        """Load a Screener.in company profile — the *approximation* fallback only.

        Deliberately a thin delegation to the injected screener client. It is reached solely when the
        NSE corporate-actions feed is unreachable, and the caller labels the result accordingly, so
        the approximation can never quietly become the primary source.
        """
        loader = getattr(self.screener, "get_company_profile", None)
        if loader is None:
            return None
        return await loader(symbol)

    async def _fetch_synthetic_bars(
        self, clean_symbol: str, days: int, raise_errors: bool
    ) -> pd.DataFrame:
        """
        Opt-in legacy path: Close + Volume from the close-only Screener chart API, with
        Open/High/Low *invented* from Close. Kept only behind ``ohlc="synthetic"``.
        """
        try:
            chart_df = await self.screener.get_chart_timeseries(clean_symbol, metric="price", days=days)
        except Exception as e:
            if raise_errors:
                raise
            logger.warning("%s: No price data found (%s). Returning empty DataFrame.", clean_symbol, e)
            return pd.DataFrame()

        if chart_df.empty or "Price" not in chart_df.columns:
            return pd.DataFrame()

        chart_df = _localize_ist(chart_df)
        close_series = chart_df["Price"].astype(float)
        volume_series = (
            chart_df["Volume"].astype(float)
            if "Volume" in chart_df.columns
            else pd.Series(0.0, index=chart_df.index)
        )

        df = pd.DataFrame(index=chart_df.index)
        df["Close"] = close_series

        # Previous close for daily open reference
        prev_close = close_series.shift(1).bfill()
        df["Open"] = prev_close

        # High and Low envelope
        df["High"] = np.maximum(df["Open"], df["Close"]) * 1.002
        df["Low"] = np.minimum(df["Open"], df["Close"]) * 0.998

        df["Volume"] = volume_series.fillna(0).astype(np.int64)
        return df

    async def _fetch_bhavcopy_bars(
        self, symbol: str, clean_symbol: str,
        start: Optional[Union[str, datetime]], end: Optional[Union[str, datetime]],
        days: int, raise_errors: bool = False, mirror=None,
    ) -> tuple:
        """
        Real published Open/High/Low/Close/Volume for one symbol, and where they were read from.

        Returns ``(rows, state)``; ``state["source"]`` is one of :data:`SOURCE_NETWORK`,
        :data:`SOURCE_MIRROR` or :data:`SOURCE_MIRROR_NETWORK`, and the whole dict is published
        on ``df.attrs["bfinance_mirror"]``.

        The whole-market range is fetched ONCE and sliced to ``clean_symbol`` here, because a
        single daily file already covers every NSE scrip: one request per symbol would
        multiply traffic for no extra data. A window older than the archive is clamped with a
        warning (logged, not silent); an archive that cannot answer at all raises rather than
        returning fabricated bars, since an empty frame would be indistinguishable from
        "this instrument has no data".

        THE MIRROR DECISION, which is the whole reason this method has a mirror argument
        ---------------------------------------------------------------------------
        :meth:`bfinance.mirror.Mirror.read` returns zero rows for three different situations and
        cannot tell them apart: the security did not list that day, the day is before the
        security existed, and the day has not been downloaded. Handing any of those back as an
        empty (or short) frame would relocate the silent-empty-frame defect from the network
        into the database and call it a cache. So ``read()`` is never the thing that decides
        whether the mirror may answer -- :meth:`~bfinance.mirror.Mirror.coverage` is, and it
        distinguishes four states rather than two:

            mirror not consulted (opt-out)            -> network, source "network"
            consulted, file absent                     -> network, source "network", reason logged
            consulted, holds EVERY requested day      -> mirror, source "mirror"
            consulted, missing only a short span       -> mirror for the held days, NSE for the
                                                        gaps, source "mirror+network"
            consulted, gaps span the whole window     -> network for the whole window, source
                                                        "network", gaps named in attrs
            consulted, NSE fails on the gaps          -> RAISES. Never the mirror's partial
                                                        window presented as a complete answer.

        Two of those rows are the load-bearing ones. The third is the only case where the mirror
        is trusted, and it is trusted precisely because the coverage watermark -- not the row
        count -- established it. The last is the refusal: a frame with a day silently missing
        is the exact artefact this package exists to not produce, and a caller cannot tell it
        from a complete one without reading attrs nobody reads.

        Why the "short span" test rather than "always top up the gaps"
        ----------------------------------------------------------------
        ``bhavcopy_range`` costs one HTTP request per DAY in the span it is given, so topping
        up the gaps of a 25-year request against a calendar that only covers 2026 would ask for
        roughly 6,000 files to fix 6,000 files. So the gap span is compared against the window
        span: topping up happens only when it is strictly cheaper, and otherwise the whole
        window is read from the network exactly as it would have been with no mirror at all.
        That bound is what keeps enabling this strictly non-regressive in traffic.
        """
        if bhavcopy_range is None:
            raise RealOHLCUnavailableError(
                f"{symbol}: the NSE bhavcopy source (bfinance.nse.bhavcopy) is not available in "
                f"this build, and ohlc='bhavcopy' will not substitute invented bars. Install that "
                f"module, or pass ohlc='synthetic' to opt in to the legacy close-only path "
                f"(whose Open/High/Low are derived from Close)."
            )

        start_ts = self._window_start(start, days)
        end_ts = pd.to_datetime(end) if end is not None else pd.Timestamp.now(tz=IST)
        floor = pd.Timestamp(BHAVCOPY_FIRST_DATE, tz=IST)
        if start_ts < floor:
            logger.warning(
                "%s: requested window starts %s, before the bhavcopy archive begins (%s). "
                "Returning only the covered range; use ohlc='synthetic' for older history.",
                symbol, start_ts.date(), BHAVCOPY_FIRST_DATE,
            )
            start_ts = floor

        start_iso = start_ts.strftime("%Y-%m-%d")
        end_iso = end_ts.strftime("%Y-%m-%d")

        rows, state = await self._bhavcopy_bars_from(
            symbol, clean_symbol, start_iso, end_iso, mirror
        )
        if rows.empty:
            # Lenient by default, matching the package convention for a genuinely absent
            # instrument -- but empty, never filled with invented bars, and never short.
            if raise_errors:
                raise TickerNotFoundError(
                    f"{symbol}: not present in the NSE bhavcopy for "
                    f"{start_ts.date()}..{end_ts.date()}."
                )
            logger.warning(
                "%s: no bhavcopy rows in %s..%s. Returning empty DataFrame.",
                symbol, start_ts.date(), end_ts.date(),
            )
        return rows, state

    # ------------------------------------------------------------------ the mirror decision
    @staticmethod
    def _mirror_spec(mirror):
        """
        Resolve the opt-in into what to open, or ``None`` for OFF.

        The very first question, and the one that keeps the default path byte-identical: with
        this ``None`` nothing below constructs a ``Mirror``, opens a file, or runs a query. The
        return is the spec itself rather than a boolean because the two doors answer with
        different things -- a caller-supplied path or ``Mirror`` versus the environment's
        string -- and resolving the string here is what stops the env var from being honoured
        with the DEFAULT path when the caller set neither.
        """
        if mirror is not None:
            return True if mirror is True else mirror
        raw = os.environ.get(MIRROR_ENV_VAR)
        if not raw or not str(raw).strip():
            return None
        value = str(raw).strip()
        return True if value.lower() in _TRUTHY_ENV else value

    def _open_mirror(self, mirror) -> tuple:
        """
        Turn the opt-in into a usable store, or say plainly why there is not one.

        ``mirror`` is a path, a ``True``/``None`` meaning "the default location", or a ``Mirror``
        the caller already holds. Returns ``(store_or_None, state)`` and never raises: a broken
        or absent mirror must degrade to the network with the reason recorded, not take down a
        price feed that is otherwise fine. The one thing it refuses to do is CREATE the database
        -- a read must not leave a 0-byte file behind, and a caller who enabled the mirror
        before backfilling it needs to be told that, not to have an empty database silently
        created and then reported as "the mirror had no rows for your window".

        Imported inside the function, like ``bhavcopy`` at the top of the module and for the
        same reason: ``import bfinance`` must not execute an opt-in module, and
        ``tests/test_public_surface.py`` asks that in a fresh interpreter.
        """
        from bfinance.mirror import Mirror, default_mirror_path  # lazy: opt-in isolation

        if isinstance(mirror, Mirror):
            store = mirror
        elif mirror is True:
            store = Mirror(default_mirror_path())
        else:
            store = Mirror(mirror)
        path = Path(store.path)
        state = _mirror_state(enabled=True, path=str(path))
        if not path.exists():
            state["enabled"] = False
            state["reason"] = (
                f"no mirror database at {path}. A history() call never creates one: run "
                f"Mirror({str(path)!r}).sync(start, end) first, or point {MIRROR_ENV_VAR} at a "
                f"database that exists."
            )
            logger.warning("%s: %s", MIRROR_ENV_VAR, state["reason"])
            return None, state
        return store, state

    @staticmethod
    def _mirror_plan(store, start_iso: str, end_iso: str) -> dict:
        """
        Ask the coverage watermark what the mirror actually holds for the window.

        This is the only thing allowed to decide that the mirror may answer. ``read()`` is not,
        because ``read()`` cannot tell an un-downloaded day from a day this security did not
        trade -- it returns zero rows for both, and for a day before the listing as well.

        ``missing_trading_days`` deliberately includes weekdays the calendar cannot classify
        (``is_trading_day is None``, e.g. a year the seeded holiday payload does not cover).
        That is the safe direction: an unclassifiable day is fetched rather than assumed
        absent, so a mirror with no calendar at all simply never serves anything rather than
        serving a hole. Weekends and seeded holidays come back as holidays instead, which is
        what makes test "a holiday is not a gap" pass without any special-casing here.
        """
        coverage = store.coverage(start_iso, end_iso)
        return {
            "days_considered": len(coverage),
            "days_missing": list(coverage.missing_trading_days),
            "non_trading_days": list(coverage.holidays),
            "calendar_seeded": store.get_holidays() is not None,
        }

    async def _bhavcopy_bars_from(
        self, symbol: str, clean_symbol: str, start_iso: str, end_iso: str, mirror,
    ) -> tuple:
        """The mirror-first reader, with the network as the fallback in every direction."""
        state = _mirror_state()
        store = None
        spec = self._mirror_spec(mirror)
        if spec is not None:
            store, state = self._open_mirror(spec)
        if store is not None:
            try:
                state.update(
                    await asyncio.to_thread(self._mirror_plan, store, start_iso, end_iso)
                )
            except Exception as e:  # noqa: BLE001 - a broken store must not break a price read
                state.update(
                    enabled=False,
                    reason=f"mirror coverage query failed ({e}); read the network instead",
                )
                logger.warning("%s: %s: %s", symbol, MIRROR_ENV_VAR, state["reason"])
                store = None

        try:
            if store is None:
                rows = await self._bhavcopy_network_rows(
                    symbol, clean_symbol, start_iso, end_iso
                )
                return rows, dict(state, used=False, source=SOURCE_NETWORK)

            gaps = list(state["days_missing"])

            if gaps and not self._gap_top_up_pays(gaps, start_iso, end_iso):
                # The mirror is missing as much as the window is wide -- a stale or empty store
                # on a long request. One whole-window fetch is the same single call the
                # un-mirrored path makes, so this cannot be worse than having no mirror, and the
                # frame is complete. The gaps are still named, so a user can see their mirror
                # is not keeping up instead of wondering why it was ignored.
                logger.warning(
                    "%s: the local mirror is missing %d trading day(s) spanning %s..%s, which is "
                    "no better than the requested window itself; reading the whole window from "
                    "NSE. attrs['bfinance_mirror']['days_missing'] lists them.",
                    symbol, len(gaps), gaps[0], gaps[-1],
                )
                rows = await self._bhavcopy_network_rows(
                    symbol, clean_symbol, start_iso, end_iso
                )
                # used=False and deliberately not "not held.empty": NOTHING from the store
                # reached this frame, so a truthy `used` here would be the provenance key lying
                # about the frame's own source.
                return rows, dict(
                    state, used=False, source=SOURCE_NETWORK,
                    network_days=[start_iso, end_iso],
                )

            held = await asyncio.to_thread(store.read, clean_symbol, start_iso, end_iso)
            held = self._tidy_bars(symbol, held, {
                "bfinance_provenance": f"bfinance mirror (SQLite) {state['path']}",
            })

            if not gaps:
                # Full coverage. The mirror is authoritative, so this is the one case where
                # "no rows for this symbol" is a real answer rather than a missing day: the
                # watermark says the file was downloaded, so the security genuinely had no bar.
                return held, dict(
                    state, used=True, source=SOURCE_MIRROR, network_days=[],
                )

            logger.warning(
                "%s: the local mirror is missing %d trading day(s) (%s..%s) of the requested "
                "window; fetching exactly those from NSE so the frame is not short. "
                "attrs['bfinance_source'] is %r and attrs['bfinance_mirror']['days_missing'] "
                "lists them.",
                symbol, len(gaps), gaps[0], gaps[-1], SOURCE_MIRROR_NETWORK,
            )
            try:
                fetched = await self._bhavcopy_network_rows(
                    symbol, clean_symbol, gaps[0], gaps[-1]
                )
            except RealOHLCUnavailableError as e:
                # THE refusal. Returning `held` here would be a frame missing real trading
                # days, wearing the same schema and the same attrs as a complete one, and the
                # caller would have no way to tell. An empty frame is no better -- it is the
                # defect in its purest form. So the call fails loudly, exactly as it does when
                # the network is the only source.
                raise RealOHLCUnavailableError(
                    f"{symbol}: the local mirror is missing {len(gaps)} trading day(s) of the "
                    f"requested window ({start_iso}..{end_iso}) and NSE could not supply them "
                    f"({e}). The mirror's partial window is deliberately NOT returned: a frame "
                    f"with real days missing is indistinguishable from a complete one. Fill the "
                    f"gap with Mirror(...).sync({gaps[0]!r}, {gaps[-1]!r}) and retry."
                ) from e

            rows = self._merge_mirror_and_network(symbol, held, fetched)
            return rows, dict(
                # used tracks whether a single stored row reached the frame, not whether the
                # store was opened. A window the mirror shares in name but not in days has
                # `enabled=True` and `used=False`, and that pair is the whole negative control.
                state, used=not held.empty,
                source=SOURCE_MIRROR_NETWORK if not held.empty else SOURCE_NETWORK,
                network_days=list(gaps),
            )
        finally:
            # One short-lived connection per call. download() fans out across symbols, and a
            # leaked handle per fan-out member is a real fd leak on a 5,000-symbol request.
            if store is not None:
                store.close()

    @staticmethod
    def _gap_top_up_pays(gaps: list, start_iso: str, end_iso: str) -> bool:
        """
        Is closing the gaps cheaper than ignoring the mirror?

        ``bhavcopy_range`` spends one HTTP request per day of the SPAN it is handed, not per
        missing day, so topping up a sparse set of gaps in a long window can cost exactly as
        much as the window itself. Comparing the two spans keeps the decision honest instead of
        assuming the mirror is always worth it, and makes the answer False whenever topping up
        would fetch the whole window anyway.
        """
        gap_span = (pd.Timestamp(gaps[-1]) - pd.Timestamp(gaps[0])).days + 1
        window_span = (pd.Timestamp(end_iso) - pd.Timestamp(start_iso)).days + 1
        return gap_span < window_span

    async def _bhavcopy_network_rows(
        self, symbol: str, clean_symbol: str, start_iso: str, end_iso: str,
    ) -> pd.DataFrame:
        """
        One whole-market range fetch, sliced to one symbol and tidied. Raises if it cannot.

        Split out of the caller so the gap-closing path can ask NSE for a sub-range through the
        identical contract -- same errors, same messages -- instead of growing a second, laxer
        copy of the fetch.
        """
        try:
            # sync/blocking client -> off the event loop so concurrent fetches stay concurrent
            market = await asyncio.to_thread(bhavcopy_range, start_iso, end_iso)
        except BFinanceError:
            # Already a typed, specific answer (NotAnNseSymbolError for a BSE scrip code,
            # ParsingError for a schema change, ...). Do not blur it into a generic one.
            raise
        except Exception as e:
            raise RealOHLCUnavailableError(
                f"{symbol}: NSE bhavcopy unavailable for {start_iso}..{end_iso} ({e}). "
                f"No bars returned; bfinance does not invent them."
            ) from e

        if market is None or getattr(market, "empty", True):
            raise RealOHLCUnavailableError(
                f"{symbol}: NSE bhavcopy returned no data for {start_iso}..{end_iso}. "
                f"The archive covers {BHAVCOPY_FIRST_DATE} onward; an earlier window cannot be "
                f"served with real OHLC. No bars returned."
            )

        missing = [c for c in ("Open", "High", "Low", "Close", "Volume") if c not in market.columns]
        if missing:
            raise RealOHLCUnavailableError(
                f"{symbol}: NSE bhavcopy frame is missing {missing}; expected real OHLCV columns. "
                f"Refusing to synthesize them."
            )

        return self._tidy_bars(
            symbol, _select_symbol_rows(market, clean_symbol), dict(market.attrs)
        )

    @staticmethod
    def _tidy_bars(
        symbol: str, rows: pd.DataFrame, attrs: Optional[dict] = None, label: str = "bhavcopy",
    ) -> pd.DataFrame:
        """
        Localise, coerce and provenance-stamp a per-symbol slice, from either source.

        Shared by the network, mirror and source-cascade paths on purpose: the three must be
        indistinguishable once they are in, or "the mirror serves the same bars" -- and "the
        second vendor's bars drop into the same consumers" -- would be a claim about values and a
        difference in dtypes. ``label`` only names the feed in the one log line that can fire, so
        a drop is attributable to the source that lost the rows rather than to "bhavcopy" whatever
        actually answered.
        """
        rows = _localize_ist(rows)
        # Carry the source's own provenance record (provenance string, schema, dates the NSE
        # published no archive for) onto the per-symbol frame, so a gap in the returned bars
        # is explainable rather than mysterious.
        rows.attrs = {k: v for k, v in (attrs or {}).items() if k != "bfinance_synthetic_ohlc"}
        for col in ("Open", "High", "Low", "Close"):
            rows[col] = pd.to_numeric(rows[col], errors="coerce")
        if VOLUME_COLUMN in rows.columns:
            rows[VOLUME_COLUMN] = pd.to_numeric(
                rows[VOLUME_COLUMN], errors="coerce"
            ).fillna(0).astype(np.int64)
        unpriced = int(rows["Close"].isna().sum())
        if unpriced:
            logger.warning("%s: dropped %d %s row(s) with no usable close.", symbol, unpriced, label)
            rows = rows[rows["Close"].notna()]
        return rows

    @staticmethod
    def _merge_mirror_and_network(
        symbol: str, held: pd.DataFrame, fetched: pd.DataFrame,
    ) -> pd.DataFrame:
        """
        Union the mirror's held days with the days NSE just supplied.

        The mirror wins on any day it holds, deliberately: it is the authority for a day whose
        file is already in the store, and the network rows for those days were transferred only
        because a range fetch cannot be sparse. The drop is therefore a no-op on the values and
        the point is that the PROVENANCE is exact -- ``bfinance_source`` says which days came
        from where, and a day cannot be both.
        """
        if fetched.empty:
            # The archive had no rows for this symbol on the gap days. That is a real answer
            # (no listing, no trade), not a failure, so the mirror's days stand on their own.
            return held
        fetched = fetched[~fetched.index.isin(held.index)]
        if fetched.empty:
            return held
        merged = pd.concat([held, fetched]).sort_index()
        merged.index.name = "Date"
        # pd.concat drops attrs, so the source's own provenance record is reattached here.
        merged.attrs = {**held.attrs, **{k: v for k, v in fetched.attrs.items()
                                         if k not in held.attrs}}
        return merged

    @staticmethod
    def _window_start(start: Optional[Union[str, datetime]], days: int) -> pd.Timestamp:
        """Inclusive window start, tz-aware IST, derived from ``start`` or the period length."""
        if start is not None:
            start_ts = pd.to_datetime(start)
        else:
            start_ts = pd.Timestamp.now(tz=IST) - pd.Timedelta(days=days)
        if start_ts.tzinfo is None:
            start_ts = start_ts.tz_localize(IST)
        else:
            start_ts = start_ts.tz_convert(IST)
        return start_ts
