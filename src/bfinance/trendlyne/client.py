"""
trendlyne.com client — optional, isolated, and traceable to the byte.

What this package is
--------------------
trendlyne.com is a **third-party** Indian market analytics site. It is not an exchange, not NSE,
and not a broker feed. It republishes and derives data that bfinance already gets from better
sources, and its value is coverage breadth: share-holding patterns, mutual-fund holdings, bulk
and block deals, insider trades, board meetings, research reports and news, none of which any
official Indian source exposes as a free no-auth route.

That is exactly why it lives in its own top-level package instead of under ``bfinance/nse/``, and
why the isolation is a hard rule rather than a preference:

1. **No inbound edges.** Nothing in the main path imports this package. The only import site is a
   user's explicit ``from bfinance.trendlyne import ...``, and ``bfinance/__init__.py`` does not
   import it. If trendlyne is unreachable, rate-limits us, changes its markup or disappears,
   every other bfinance call behaves exactly as before.
2. **No import-time I/O.** Constructing :class:`TrendlyneClient` opens no socket and reads no
   file. The sitemap, the symbol index and the per-symbol tokens are all fetched on first use.
3. **A typed error, or a frame that says what it is.** Every public call raises a
   :class:`~bfinance.trendlyne.errors.TrendlyneError` subclass or returns a frame carrying
   ``attrs["bfinance_provenance"] == "trendlyne"`` plus a truthful note. Nothing is filled,
   interpolated or smoothed. A missing number stays missing.

The price-basis warning — read this before concatenating anything
----------------------------------------------------------------
**This route is not on a yfinance basis, and claiming it was is the bug this section exists to
prevent.** The measured basis is:

    as-traded  ·  back-adjusted for SPLITS and BONUSES only  ·  DEMERGERS NOT APPLIED

It is *not* ``yfinance_back_adjusted``, and it never was on any symbol measured. An earlier
version of this docstring and of :data:`PRICE_BASIS` asserted a blanket yfinance basis; that was
false, and :data:`PRICE_BASIS` was corrected rather than the claim being softened.

Measured live 2026-09-27 against :mod:`bfinance.nse.bhavcopy` (as-traded, freshly downloaded
archives), 8 symbols over two windows. The discriminating window is 2024-12-16..2025-03-13: it
sits **before** HDFCBANK's 1:1 bonus (2025-08-26) and MOTHERSON's 1:2 bonus (2025-07-18) and
**straddles** ITC's demerger (2025-01-06)::

    ITC 62/62 · RELIANCE 62/62 · TCS 62/62 · INFY 62/62 · SBIN 62/62 · AXISBANK 62/62  (all 5
    fields byte-identical, max|diff| 0.00)
    HDFCBANK 0/62 (max|price diff| 934.98) · MOTHERSON 0/62 (max|price diff| 56.47)

    2026-09-01..2026-09-25, all 8 symbols: 18/18 byte-identical

The two failures are **exactly** the two names with a share-count event after the window, and the
same two match bhavcopy 18/18 once the window is past their event. That is back-adjustment for
splits and bonuses, and nothing else. The 6 that match everywhere have no event, so "matches
bhavcopy" is what as-traded looks like, and "stops matching" is what an applied split factor looks
like — the two are distinguishable on the same data.

**Demergers are not applied at all, and this is the part that matters.** ITC 2025-01-03 is
trendlyne **481.60** and bhavcopy **481.60**, while yfinance says **463.74**. Across 2017 (246
sessions), 2018 (244), 2020 (250), 2022 (248) and 2024 (245) the trendlyne ÷ yfinance close ratio
is **1.038513 at every session of every year** — the exact reciprocal of Yahoo's 0.962915
constant. So on a demerger it is **the exchange and yfinance that rewrote history**, not this
source. The basis is uniform: one rule on all 8 symbols across both windows, so the label is a
constant, not a per-symbol derivation.

Practical consequence, and it is the opposite of what the old label implied:

* **May be blended with** :mod:`bfinance.nse.bhavcopy` **on demerger names** — they agree to the
  paisa, because neither rewrites them.
* **Must not be blended on a name with a split or bonus inside the joined span** — the older bars
  are scaled and the join carries a corporate-action-sized discontinuity.

``attrs`` tells you the *rule*, never whether *this* symbol has an event inside your window.
Measure it, or check the symbol's corporate actions; do not infer it from the basis attr.

A positive control on the as-traded half, because "as-traded" is a claim that should be testable
from the other side too: the 2026-09-24 RELIANCE bar is ``Close 1219.2, Volume 13923795`` here,
byte-for-byte what :mod:`bfinance.nse.marketlens` returns for the same session (see the recorded
``tests/fixtures/marketlens/reliance_1y.json``). Two independent sources agreeing exactly on a
recent bar is what an unadjusted series looks like; combined with the 62/62 bhavcopy agreement
above and the 0.991 ratio across RELIANCE's own 2024-10-28 bonus, it pins both halves of the label
from separate angles.

:mod:`bfinance.nse.bhavcopy` is as-traded and carries no split or bonus factor. Every OHLC frame
therefore carries::

    df.attrs["bfinance_trendlyne_price_basis"] = "as_traded_split_bonus_adjusted"
    df.attrs["bfinance_trendlyne_demerger_applied"] = False
    df.attrs["bfinance_synthetic_ohlc"] = False

``synthetic_ohlc = False`` says the O/H/L are real (they are: the invariant
``High >= max(Open, Close)`` and ``Low <= min(Open, Close)`` held on **51,070 of 51,070** rows
across 10 symbols, and on all 5,107 rows of this module's own recorded capture).

Endpoints — 3 of them, all with a fetch method
----------------------------------------------
Base ``https://trendlyne.com``. No login, no API key, no cookie: a browser ``User-Agent`` is the
whole requirement. :data:`ENDPOINTS` holds **only the routes this package can address today**,
and :func:`build_path` is the supported way to build one, so the token rule is enforced locally.

That is a deliberately small dict, and it used to be a dishonest one. It listed 17 templates, of
which **14 no method ever built** — the section tables (deals, insider trades, share and
mutual-fund holdings, financials, research reports, board meetings, buy-sell-zone), the four
``latest-news`` routes, and ``rights``. None had a fetch method, and five of the 14 were never even
confirmed to answer 200. A template with no method behind it is a promise about unwritten code, and
``build_path`` would return a well-formed URL for a route nobody had ever requested. The 14 were
deleted; ``docs/UPSTREAM_SOURCES.md`` keeps the full scraping study — row counts, depths, and the
two pages the header-signature model cannot yet discriminate — so nothing measured is lost. **A
test now fails if a template is added without a method behind it.**

Symbol resolution is a sitemap, because there is no search API
--------------------------------------------------------------
``GET /equity-sitemap-stocks.xml`` returns 5,922 ``<loc>`` entries of the form
``/equity/{ID}/{SYMBOL}/{slug}/``. Every guessed search route 404s, so this document is the only
resolver. It is 831 KB, hence the in-memory TTL cache (24 h default) and the opt-in
:class:`~bfinance.cache.sqlite_cache.SQLiteCache`.

**The symbol segment is not always a ticker.** The tail of the live sitemap holds
``/equity/3622447/1216/bse-midsmall-private-banks/`` — a BSE index family keyed by the number
``1216``. Lookup is exact-match on that segment; :meth:`TrendlyneClient.search` is the escape
hatch, because the slug carries the human-readable name.

The per-symbol token, and what 444 actually means
--------------------------------------------------
The OHLC route needs an opaque per-symbol token that exists **only** inside the symbol's own page,
in a ``data-eodurl`` attribute (``/mapp/v1/stock/web/ohlc/878/<token>/``). It is not a constant:
MOTHERSON's is ``TQZITAIRHO5FCC7SYAKGO6M7QA======``, RELIANCE's is
``4EL732ZR47YQ7OPFML7VCNHP6Y======``. Two requests per symbol — one HTML page, one JSON — after
which the token is cached.

The 444 is a bot gate, not a bad token. This corrects the most natural reading of the failure and
is the single most important thing to know about this API. Measured 2026-09-27 on the *same*
valid MOTHERSON URL:

=========================  =========================================================
request                    result
=========================  =========================================================
no ``Referer``             **HTTP 444, zero-length body**
``Referer: <the page>``    HTTP 200, 396 KB of JSON
``Referer``, wrong token   HTTP **200**, 92 bytes,
                           ``head.status == "2"`` / ``"Not Authorized !"``
=========================  =========================================================

Two consequences, and they need different types:

* **444 means the request did not look like it came from a page on the site.** It is terminal and
  not retried: retrying an unchanged request only spends the vendor's patience. It must never
  reach ``response.json()`` — a zero-length body would surface as a decode error, which is the
  most confusing possible symptom.
* **A rejected token is a 200.** Status-code checking alone reports success and hands the caller
  an empty body. :func:`parse_envelope` treats any ``head.status != "0"`` as
  :class:`~bfinance.trendlyne.errors.TrendlyneNotAuthorizedError`, and
  :meth:`TrendlyneClient.ohlc` re-scrapes the token **once** on that failure, in case it rotated.

``/mapp/v1/`` requests carry a ``Referer``; the HTML page routes do not need one (verified 200
without). HTML pages work with a bare browser ``User-Agent``.

A route that only serves a snapshot cannot address a period — measured, and kept as prose
-------------------------------------------------------------------------------------------------
There is **no policy table for this any more**, and the note to read before wiring a holding route
is this section. :class:`~bfinance.trendlyne.errors.TrendlyneSnapshotOnlyError` and
:data:`SNAPSHOT_ONLY_ENDPOINTS` were removed as dead public API: the set had to be a subset of
:data:`ENDPOINTS` to be enforceable, nothing fetched a snapshot-only route, so it was an empty
``frozenset()`` and the guard could never fire. A public exception that cannot be raised is the
same category of problem as the 14 dead endpoint templates — it makes the package look bigger than
the code that runs. The measured fact survives here, which is where whoever wires the route reads.

``share-holding`` serves **only** the literal path segment ``latest``. Verified 2026-09-27:
``.../Jun-2024/...`` and ``.../2024/...`` both return **HTTP 500**, not 404 and not an empty
table. So the share-holding route cannot address a period at all. The single ``latest`` page is
still informative — it embeds a wide multi-period grid (44 distinct period labels on MOTHERSON,
from Dec 2015 to Jun 2026) — but it is one request, not a series you can walk.

``monthly-mutual-fund-share-holding`` is **not** snapshot-only, contrary to what the same test
suggests for share-holding. Verified on MOTHERSON: ``latest`` -> 200 (355 rows, top period
Aug-2026), ``2024`` -> 200 (313 rows, top period Sep-2024), ``Jun-2024`` -> 200 (260 rows, top
period Jun-2024), ``2023`` -> 200 (179 rows). The segment genuinely filters.

**Wiring either route means adding its name to :data:`ENDPOINTS` in the same commit as its fetch
method — and for ``share_holding`` also restoring a local period guard**, because ``build_path``
no longer carries one: the 500 above is a fact, not an enforcement. The guard belongs in
``build_path`` as an explicit check against the 500, not in a table that can be empty. Deals and
insider trades are the opposite extreme when they are wired — 10-16 years in one page — so they
must **not** be guarded. :data:`SNAPSHOT_PERIOD` survives as the only period segment the
share-holding route accepts, and :func:`validate_period` already rejects anything else before a
request is spent.

``eodData`` is newest-first, and the freshest week is in a different key
-----------------------------------------------------------------------
**The history is truncated at listing — it is *not* a fixed vendor window.** This docstring used to
claim the opposite ("5,107 bars for every symbol measured … a fixed vendor window, *not* listing
history"), and that was wrong: it generalised a large-cap measurement to every symbol. Measured
counter-examples — **IREDA (2023 IPO) returns 696 bars**, and **PAYTM (delisted) 1,199**. A 2023
listing cannot have twenty years of history, so the floor is the listing date, not a window edge.

What *is* uniform is the **start** for old listings: 2006-01-02. So a mature large cap reaches
5,107 bars in ``eodData`` and 5,112 after the last-week merge, and a recent listing reaches far
fewer. Treat any bar count as symbol-dependent, and do not use a constant to reason about coverage.

The payload also carries ``eodDataLastWeek``, which on the same
capture holds 2026-09-21 to 2026-09-25 — i.e. the five most recent sessions that ``eodData`` does
**not** contain. A frame built from ``eodData`` alone is therefore a week behind the vendor's own
latest bar; ``ohlc(include_last_week=True)`` (the default) merges those five in, keeping
``eodData`` authoritative for any date both keys carry, and records the merge in ``attrs``.
Verified live 2026-09-27: RELIANCE returns 5,107 + 5 = **5,112** bars ending 2026-09-25, and TCS
the same. A frame built from ``eodData`` alone stops at 2026-09-18.

Two more measured details, both recorded in ``attrs`` rather than smoothed over:

* Rows arrive **newest-first**. The frame is sorted ascending; the wire order is not preserved.
* ``close_price`` and ``last_price`` are **different fields** and disagree on most bars — 5,024
  of 5,112 rows on a live RELIANCE pull, 82 of 120 on this module's recorded capture, typically
  by a few paisa. ``Close`` is ``close_price``, because that is the field consistent with the
  bar's O/H/L; the divergence count is recorded in
  ``attrs["bfinance_trendlyne_close_last_divergent_rows"]`` so the difference is visible rather
  than silently resolved.

A trap for whoever parses ``chart-data`` next
----------------------------------------------
``/mapp/v1/stock/chart-data/{ID}/{METRIC}/`` needs no token, and its ``eodData`` is
``[epoch_ms, value]`` pairs. The epochs are **midnight IST on quarter-end dates**
(2026-06-30, 2026-03-31, 2025-12-31, 2025-09-30 … all at 00:00 +05:30), so they must be converted
to ``Asia/Kolkata`` before the date is taken. Reading them as UTC dates lands every point one day
early. :meth:`TrendlyneClient.chart_metric` returns the raw payload and does **not** parse it, and
no module here does: the timestamp handling above is the whole reason, and a consumer that needs
quarterly points must do it explicitly rather than inherit a guess.

Pacing, retries and caching
---------------------------
Pacing is the package's existing global-slot scheme, ported verbatim from
``bfinance.screener.client`` and ``bfinance.nse.bhavcopy`` (``_GLOBAL_PACING_LOCK``,
``_GLOBAL_LAST_REQUEST_TIME``, ``_throttle_pacing``) — the same algorithm, not a second private
one. The default interval is **750 ms**, deliberately far above the 100-150 ms used for
screener.in and MarketLens: this is a commercial site serving a heavy page per symbol, two
requests to the chart, and nothing here is worth being impatient with. Retry covers 429, 5xx and
transport failures only, with exponential backoff that honours ``Retry-After`` (capped at 30 s);
every 4xx is terminal.

Caching is opt-in on disk. The sitemap index and the tokens live in memory with a TTL. Passing
``cache=SQLiteCache(...)`` additionally persists the index; the default is ``None``, so importing
this package never creates a file in the user's home directory.
"""

from __future__ import annotations

import asyncio
import html as _html
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote_plus, unquote

import httpx
import numpy as np
import pandas as pd

from bfinance.utils.symbols import normalize_symbol

from .errors import (
    TrendlyneAccessDeniedError,
    TrendlyneError,
    TrendlyneHTTPError,
    TrendlyneNotAuthorizedError,
    TrendlyneNotFoundError,
    TrendlyneRateLimitedError,
    TrendlyneRobotsDisallowedError,
    TrendlyneSchemaError,
    TrendlyneServerError,
    TrendlyneSymbolNotFoundError,
    TrendlyneTransportError,
)
from .sectors import (
    TrendlyneGroup,
    constituent_attrs,
    parse_classification,
    parse_constituents,
    parse_industry_aggregate,
    parse_taxonomy,
)
from .tables import PROVENANCE, extract_ohlc_token

logger = logging.getLogger("bfinance")

#: Provenance tag on every frame. Re-exported from :mod:`bfinance.trendlyne.tables`, which owns it.
BASE_URL = "https://trendlyne.com"

#: The only symbol resolver trendlyne has. 5,922 entries, 831 KB, refreshed daily.
SITEMAP_PATH = "/equity-sitemap-stocks.xml"

#: trendlyne's own autocomplete, used as the *fallback* resolver. **This path is disallowed by
#: trendlyne's robots.txt** and every call to it is gated behind
#: :data:`MEMBER_SEARCH_FLAG` — see :data:`ROBOTS_DISALLOWED_ROUTES` and
#: :meth:`TrendlyneClient.search_remote`. The sitemap does not list ETFs -- NIFTYBEES, BANKBEES,
#: GOLDBEES, JUNIORBEES and SETFNIF50 resolve *only* here and serve full OHLC, yet **none of them
#: appear among the 5,922 sitemap entries** (verified 2026-09-27 against the full 830,905-byte
#: document: zero entries contain "BEES" or "ETF", and none begins "SETF"). So with the gate off
#: — the default — **ETFs are unreachable through this package**, and
#: :meth:`TrendlyneClient.resolve_any` says so rather than reporting the instrument as unknown.
#: Query-string endpoint, so it is kept out of :data:`ENDPOINTS` (those are path templates
#: validated by :func:`build_path`). A miss answers ``[{"headline": "No matches found"}]``, which is
#: a result, not an error.
_SYMBOL_SEARCH_PATH = "/member/api/ac_snames/?term={query}"

#: Paths this package will not request unless a caller opts in, mapped to the robots.txt rule that
#: forbids them. **Read from the live ``robots.txt`` on 2026-09-27**, whose ``User-agent: *`` block
#: carries 28 ``Disallow`` rules and a ``Crawl-delay: 1``. (The 10 s figure that circulates for
#: this site is scoped to ``GPTBot``/``ClaudeBot``, not to everybody.)
#:
#: One entry, and it is the one this package used to violate in shipped code:
#:
#: ==========================  =============================================================
#: path                       covering rule
#: ==========================  =============================================================
#: ``/member/api/ac_snames/``  ``Disallow: */member/``
#: ==========================  =============================================================
#:
#: The rule is the **broad** ``*/member/``. robots.txt has a second, narrower member rule
#: (``*/member/api/ac_snames/stock/``) which this package's path does *not* match — the specific
#: one covers the ``/stock/`` sibling endpoint, not the ``?term=`` search this package called. The
#: broad rule is what forbids it, and it was forbidding it while ``search_remote`` was a default
#: part of :meth:`TrendlyneClient.resolve_any`. That is now opt-in and off.
#:
#: Also disallowed, recorded so nobody re-derives it: ``*/onwards/``, ``*/edithistory/``,
#: ``*/trendit/``, ``*/like/``, ``*/accounts/``, ``*/visitor/``, ``*/wholikes/``,
#: ``*/get-document/``, ``*/getUserNavBar/``, ``*/relationships/``, ``*/alerts/addStockAlert/``,
#: ``*/posts/comment/``, ``*/fundamentals/back-test/runhistory/``, ``*/fundamentals/chart/v2/``,
#: ``*/polls/getMemberVotes/``, ``*/rapid-results/all/``, ``*/alerts/modal/stockAlert/``,
#: ``*/create-basket/from/``, ``*/stock-screeners/by-type/?searchparam=``, and seven
#: ``*/portfolio|equity/.../custom/?query=%E|%F`` rules. None is reachable from
#: :data:`ENDPOINTS`, which is the point of the table being narrow rather than a deny-list.
#:
#: Checked against every route this package can build, the sector and industry group pages
#: included: ``/equity/sector/{id}/{slug}/`` and ``/equity/industry/{id}/{slug}/`` match **none** of
#: the 28. ``*/equity/technical-analysis-*`` is the only equity rule and it needs that literal
#: prefix, which a stock or group page never has.
ROBOTS_DISALLOWED_ROUTES: Dict[str, str] = {
    "/member/api/ac_snames/": "Disallow: */member/",
}

#: The opt-in that permits a robots-disallowed route. ``False`` — off — is the default, and the
#: only way to turn it on is to name it in the constructor or in the call. The name says which
#: conflict is being overridden, because "allow_disallowed" would not.
MEMBER_SEARCH_FLAG = "allow_member_search"

#: Every route this package can address **today**, as ``str.format`` templates over
#: ``stock_id``, ``symbol``, ``slug``, ``group_id``, ``group_slug``, ``period``, ``metric`` and
#: ``token``. Use :func:`build_path` instead of formatting these directly.
#:
#: This held 17 templates once and 14 of them were dead strings: no method built them, so
#: ``build_path`` would mint a plausible URL for a route nobody had requested and five of them
#: (``rights`` and the four ``latest-news`` routes) had never been seen to answer 200 at all. The
#: 14 were deleted rather than wired — building a fetch method is separate work, and a
#: header-signature model that cannot yet tell Buy Sell Zone's two tables apart, or that validated
#: ``board_meeting`` on the wrong page, would return confident nonsense if wired today. The full
#: scraping study survives in ``docs/UPSTREAM_SOURCES.md``. When a route *is* wired, add its name
#: here and a fetch method in the same commit; a test enforces that pairing.
ENDPOINTS: Dict[str, str] = {
    "stock_page": "/equity/{stock_id}/{symbol}/{slug}/",
    "ohlc": "/mapp/v1/stock/web/ohlc/{stock_id}/{token}/",
    "chart_metric": "/mapp/v1/stock/chart-data/{stock_id}/{metric}/",
    # Group pages. Note the shape: the id comes FIRST and there is no symbol segment, which is the
    # opposite of stock_page above. One template with an optional branch would match a group URL
    # as a stock without complaining, so the two shapes get two entries.
    "sector_page": "/equity/sector/{group_id}/{group_slug}/",
    "industry_page": "/equity/industry/{group_id}/{group_slug}/",
}

#: Routes that take no symbol, so :func:`build_path` needs a group id and slug rather than a
#: :class:`TrendlyneSymbol`. Derived from the keys above rather than restated, so adding a third
#: group route cannot leave this set behind.
GROUP_ENDPOINTS: frozenset = frozenset({"sector_page", "industry_page"})

#: The only period segment a snapshot-only route accepts — the literal ``latest`` that
#: ``/equity/share-holding/...`` answers for, and returns HTTP 500 for anything else. Kept as a
#: constant because that is a measured property of the route rather than a default. See the "A
#: route that only serves a snapshot" section of the module docstring.
SNAPSHOT_PERIOD = "latest"

#: Metrics the ``chart-data`` route accepts. Any other value returns 200 with a failed envelope,
#: so the set is enforced locally.
METRICS: Tuple[str, ...] = (
    "EPS_TTM",
    "PE_TTM_SHARE_NOW",
    "PBV_A_SHARE_NOW",
    "DIVIDEND_YIELD_TTM_Q",
)

#: Price basis of the OHLC route, recorded on every frame. Read the "price-basis warning" section
#: of the module docstring for the measurement behind it; the short form is
#: **as-traded, back-adjusted for splits and bonuses only, demergers NOT applied.**
#:
#: This constant used to say ``"yfinance_back_adjusted"``, a false blanket claim -- it named a
#: source's convention rather than this route's behaviour, and on the whole demerger class (ITC,
#: where the trendlyne/yfinance close ratio is 1.038513 at every session of 2017, 2018, 2020, 2022
#: and 2024) it described the exact opposite of what this route does. Measured 2026-09-27 against
#: bhavcopy over 2024-12-16..2025-03-13: 6 of 8 symbols byte-identical on all 5 fields for 62/62
#: sessions, and the 2 that diverge (HDFCBANK, MOTHERSON) are exactly the 2 with a split or bonus
#: after the window. It is a constant, not a per-symbol derivation: one rule on 8 symbols across 2
#: windows. :data:`DEMERGER_APPLIED` carries the same fact as a boolean so a consumer can branch on
#: it without parsing the label.
PRICE_BASIS = "as_traded_split_bonus_adjusted"

#: Whether this route applies demerger (demutualisation) adjustment. **False**, and it is stated
#: separately because it is the part of the basis a consumer is most likely to get wrong: the
#: demerger names are the ones where yfinance and this source disagree, and the direction of that
#: disagreement is the opposite of what the old label implied -- here it is the *exchange* print
#: that survives. See the module docstring.
DEMERGER_APPLIED = False

#: Output contract, identical to ``bfinance.nse.bhavcopy.COLUMNS`` so a frame from here drops
#: into the same consumers as a bhavcopy frame.
COLUMNS: Tuple[str, ...] = ("Open", "High", "Low", "Close", "Volume")

SNAPSHOT_ONLY_ATTR = "bfinance_trendlyne_snapshot_only"
PRICE_BASIS_ATTR = "bfinance_trendlyne_price_basis"
#: Machine-readable companion to :data:`PRICE_BASIS`: this route does **not** apply demerger
#: adjustment. Present so a consumer can branch on the fact instead of parsing the label string,
#: and present on every frame whether it is True or False.
DEMERGER_APPLIED_ATTR = "bfinance_trendlyne_demerger_applied"
SOURCE_ATTR = "bfinance_trendlyne_source"
NOTE_ATTR = "bfinance_trendlyne_note"
#: Written by ``build_attrs(window=...)``, which prefixes its keyword arguments. A one-line
#: summary of the same facts as ``bfinance_trendlyne_first_date`` / ``_last_date`` / ``_bars``.
WINDOW_ATTR = "bfinance_trendlyne_window"
RETRIEVED_AT_ATTR = "bfinance_trendlyne_retrieved_at"

#: Wire column names (``eodHeaders[].unique_name``) this module requires, in output order.
_OHLC_REQUIRED: Tuple[str, ...] = (
    "date", "open_price", "high_price", "low_price", "close_price", "volume",
)

#: Wire columns observed but not surfaced as a column. ``last_price`` differs from
#: ``close_price`` on most bars; the count of disagreeing rows is recorded in ``attrs``.
_OHLC_EXTRA: Tuple[str, ...] = ("last_price",)

#: Conservative by design. The 100-150 ms used for screener.in and MarketLens is too aggressive
#: for a commercial site serving a 292 KB page per symbol.
DEFAULT_MIN_REQUEST_INTERVAL_MS = 750.0

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
]

# Global pacing state shared across all client instances, ported from
# ``bfinance.screener.client`` so concurrent clients respect one rate limit instead of
# bursting N x. Deliberately not a second, private scheme.
_GLOBAL_PACING_LOCK = threading.Lock()
_GLOBAL_LAST_REQUEST_TIME = 0.0

# Retry-After cap (seconds) so a hostile or accidental header cannot stall us.
_MAX_RETRY_AFTER_SEC = 30.0

_SITEMAP_URL_RE = re.compile(r"/equity/(\d+)/([^/]+)/([^/?#]+)/?")
# The month form is ``Mon-YYYY`` with the capitalisation trendlyne actually serves. Case matters
# in a path segment, and an unrecognised segment on the share-holding route costs a 500, so this
# is matched strictly rather than case-insensitively.
_PERIOD_RE = re.compile(r"^(?:latest|\d{4}|[A-Z][a-z]{2}-\d{4})$")
_TOKEN_IN_URL_RE = re.compile(r"(/ohlc/\d+/)[A-Za-z0-9=]+/")


# ------------------------------------------------------------------ symbol + paths


@dataclass(frozen=True)
class TrendlyneSymbol:
    """One entry of the equity sitemap: everything needed to address a symbol on trendlyne.

    Attributes:
        symbol: The sitemap's own symbol key, verbatim (uppercase for equities, but **numeric**
            for some entries — ``/equity/3622447/1216/bse-midsmall-private-banks/``). It is
            trendlyne's key, not guaranteed to equal the NSE ticker.
        stock_id: The integer path segment that identifies the stock to trendlyne.
        slug: The lowercase, hyphenated company name used in every URL.
    """

    symbol: str
    stock_id: int
    slug: str

    @property
    def page_path(self) -> str:
        """The symbol's own page path — also the correct ``Referer`` for its ``/mapp/v1/`` calls."""
        return ENDPOINTS["stock_page"].format(
            stock_id=self.stock_id, symbol=self.symbol, slug=self.slug
        )

    @property
    def page_url(self) -> str:
        """Absolute URL of :attr:`page_path`."""
        return f"{BASE_URL}{self.page_path}"


def _decoded(value: str) -> str:
    """XML entity-decode then percent-decode a sitemap path segment.

    ``&amp;`` must be resolved before percent-decoding: the sitemap stores ``M&amp;M`` and the
    live symbol is ``M&M``. Doing it in the other order would leave a stray ``&amp;``.
    """
    return unquote(_html.unescape(value))


def parse_sitemap(xml_text: str) -> Dict[str, TrendlyneSymbol]:
    """Parse the equity sitemap into a ``symbol -> TrendlyneSymbol`` index.

    Args:
        xml_text: The body of ``/equity-sitemap-stocks.xml``.

    Returns:
        A dict keyed by the sitemap's symbol segment. Over the live document (5,922 entries) every
        ``<loc>`` matches :data:`_SITEMAP_URL_RE`.

    Raises:
        TrendlyneSchemaError: The document contained no recognisable entry. A sitemap that
            changed shape is reported, not returned as an empty index — an empty index would make
            every symbol look unresolvable.
    """
    if not xml_text:
        raise TrendlyneSchemaError("sitemap response was empty")

    index: Dict[str, TrendlyneSymbol] = {}
    for match in _SITEMAP_URL_RE.finditer(xml_text):
        stock_id, symbol, slug = match.group(1), match.group(2), match.group(3)
        # XML entity-decode FIRST, then percent-decode. The sitemap is XML, so a symbol
        # containing an ampersand is stored escaped: "M&amp;M", not "M&M". Sixteen entries
        # are affected -- including M&M (id 807) and M&MFIN (id 808), two of the largest
        # NSE names. Percent-decoding alone leaves the literal "M&amp;M" in the index, which
        # then 404s on lookup and makes a real, resolvable symbol look unresolvable.
        # Verified live: after this fix M&M, M&MFIN, J&KBANK, ARE&M, GVT&D and IL&FSENGG all
        # resolve, and the pipeline returns their bars.
        index[_decoded(symbol)] = TrendlyneSymbol(
            symbol=_decoded(symbol), stock_id=int(stock_id), slug=_decoded(slug)
        )
    if not index:
        raise TrendlyneSchemaError(
            "no /equity/{id}/{symbol}/{slug}/ entries found in the sitemap. First 300 chars: "
            f"{xml_text[:300]!r}. trendlyne may have changed the document, or the request may "
            "have been served an error page."
        )
    return index


def validate_period(period: str) -> str:
    """Validate a period path segment before any request.

    Accepts ``latest``, a year (``2024``) and a ``Mon-YYYY`` month (``Jun-2024``) — the three
    forms verified live. Rejecting anything else locally turns what would be an unexplained
    HTTP 500 on the share-holding route into a message naming the accepted forms.
    """
    if not isinstance(period, str):
        raise ValueError(f"period must be a string, got {type(period).__name__}")
    clean = period.strip()
    if not _PERIOD_RE.match(clean):
        raise ValueError(
            f"period={period!r} is not a valid period segment. Use 'latest', a year "
            f"('2024') or a month ('Jun-2024')."
        )
    return clean


def build_path(
    name: str,
    record: Optional[TrendlyneSymbol] = None,
    *,
    period: str = SNAPSHOT_PERIOD,
    metric: str = "",
    token: str = "",
    group: Optional["TrendlyneGroup"] = None,
) -> str:
    """Build a request path for a known endpoint, enforcing the rules that would otherwise
    produce a confusing server error.

    Args:
        name: A key of :data:`ENDPOINTS` — that is, a route this package has a fetch method for.
        record: The resolved symbol. Required for the stock routes, ignored for the group routes
            in :data:`GROUP_ENDPOINTS`, which have no symbol segment.
        period: Period segment, for the routes that take one. See :func:`validate_period`. No
            addressable route takes one today.
        metric: Metric name, required by ``chart_metric``. See :data:`METRICS`.
        token: Per-symbol OHLC token, required by ``ohlc``.
        group: The sector or industry, required by ``sector_page`` and ``industry_page``. Its
            ``kind`` must match the route, so a sector's id cannot be formatted into an industry
            URL.

    Returns:
        A path beginning with ``/``.

    Raises:
        ValueError: Unknown ``name`` — including a route that exists on trendlyne but is not
            implemented here, which is an explicit error rather than a minted URL — an
            unrecognised ``metric``, a missing ``token``, a group route with no ``group``, a
            ``group`` whose kind does not match the route, or a non-group route given a ``group``.
    """
    if name not in ENDPOINTS:
        raise ValueError(
            f"unknown endpoint {name!r}. This package addresses only "
            f"{', '.join(sorted(ENDPOINTS))} — one per method that fetches it. Routes that exist on "
            f"the site but have no fetch method here are not listed; see the 'Endpoints' section "
            f"of the module docstring and docs/UPSTREAM_SOURCES.md before adding one."
        )
    if name in GROUP_ENDPOINTS:
        if group is None:
            raise ValueError(
                f"endpoint {name!r} takes a sector or industry, not a symbol. Pass "
                f"group=TrendlyneGroup(kind={name.split('_')[0]!r}, group_id=..., slug=..., "
                f"name=...), which you get from parse_taxonomy() — no id is ever hardcoded."
            )
        if group.kind != name.split("_")[0]:
            raise ValueError(
                f"endpoint {name!r} requires kind={name.split('_')[0]!r}, got "
                f"kind={group.kind!r}. "
                f"Formatting one kind's id into the other's URL would address a different page "
                f"than the one whose taxonomy you read."
            )
    elif group is not None:
        raise ValueError(
            f"endpoint {name!r} takes a symbol; group= applies only to "
            f"{', '.join(sorted(GROUP_ENDPOINTS))}."
        )
    if name == "chart_metric":
        if metric not in METRICS:
            raise ValueError(f"metric={metric!r} is not supported. Must be one of {', '.join(METRICS)}.")
    if name in ("ohlc", "stock_page") and record is None:
        raise ValueError(
            f"endpoint {name!r} takes a symbol; pass record=TrendlyneSymbol from resolve()."
        )
    if name == "ohlc" and not token:
        raise ValueError(
            "the ohlc endpoint requires a per-symbol token. Call "
            "TrendlyneClient.ohlc_token(symbol) first — the token is not a constant."
        )
    validate_period(period)
    values: Dict[str, Any] = dict(period=period, metric=metric, token=token)
    if record is not None:
        values.update(
            stock_id=record.stock_id, symbol=record.symbol, slug=record.slug
        )
    if group is not None:
        values.update(group_id=group.group_id, group_slug=group.slug)
    return ENDPOINTS[name].format(**values)


def redact(url: str) -> str:
    """Replace a per-symbol token in a URL with ``<token>``.

    Tokens are per-symbol credentials for trendlyne's private API surface. They must not reach a
    log line, an exception message, a ``repr`` of a frame, or a CSV export via ``attrs``.
    """
    return _TOKEN_IN_URL_RE.sub(r"\1<token>/", url or "")


def parse_envelope(payload: Any, *, context: str) -> Dict[str, Any]:
    """Unwrap trendlyne's ``{"head": ..., "body": ...}`` envelope and verify it succeeded.

    Every JSON route on this site answers **200** for a failed request, with
    ``head.status != "0"`` and a small or empty ``body``. Verified: a rejected OHLC token returns
    92 bytes of ``{"head": {"status": "2", "statusDescription": "Not Authorized !"},
    "body": {}}``. Checking the status code is therefore not enough, and returning the body
    unchecked would hand the caller an empty frame that looks like a quiet market.

    Args:
        payload: The decoded JSON.
        context: Human description of the request, used in the error message.

    Returns:
        The ``body`` mapping.

    Raises:
        TrendlyneNotAuthorizedError: ``head.status`` is not ``"0"``.
        TrendlyneSchemaError: The envelope, or the body, is not the expected shape.
    """
    if not isinstance(payload, dict):
        raise TrendlyneSchemaError(
            f"{context}: expected a JSON object envelope, got {type(payload).__name__}."
        )
    head = payload.get("head")
    if not isinstance(head, dict):
        raise TrendlyneSchemaError(
            f"{context}: response has no 'head' object. Top-level keys: "
            f"{sorted(payload)[:8]}. Upstream schema may have changed."
        )
    status = str(head.get("status", "")).strip()
    description = str(head.get("statusDescription") or "").strip()
    if status != "0":
        raise TrendlyneNotAuthorizedError(
            f"trendlyne reported status={status!r} ({description or 'no description'}) for "
            f"{context}. Note this arrives as HTTP 200 with an empty body — for the OHLC route "
            f"it means the per-symbol token was rejected or has rotated.",
            status_code=200,
        )
    body = payload.get("body")
    if not isinstance(body, dict):
        raise TrendlyneSchemaError(
            f"{context}: 'body' is {type(body).__name__}, expected an object. "
            f"Upstream schema may have changed."
        )
    return body


def build_attrs(
    symbol: str,
    *,
    source: Optional[str] = None,
    snapshot_only: bool = False,
    note: Optional[str] = None,
    retrieved_at: Optional[datetime] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """Build the ``attrs`` mapping every frame from this package carries.

    Sibling modules should call this rather than assembling attrs by hand, so that the keys a
    consumer can rely on are the same whichever module produced the frame.

    Args:
        symbol: The symbol the frame describes.
        source: The URL it came from. Redacted automatically when it carries a token.
        snapshot_only: ``True`` when the frame is a single snapshot that cannot be walked
            backwards. Always written, ``False`` included, so the key is never missing.
        note: One plain sentence on what this frame does and does not contain.
        retrieved_at: When the response arrived.
        **extra: Additional ``bfinance_trendlyne_*`` measurements.

    Returns:
        A dict suitable for ``frame.attrs.update(...)``.

    Note:
        The price-basis keys are deliberately **not** written here. They are set by
        :func:`parse_ohlc`, which is the only function in this package that has measured a price
        basis, and :data:`PRICE_BASIS_ATTR` / :data:`DEMERGER_APPLIED_ATTR` describe *OHLC*. A
        table frame — a deals list, a holdings grid — has no price basis at all, and stamping one
        onto it would be the same kind of false claim this package was corrected for. A caller
        that really does have bars must set both keys itself.
    """
    attrs: Dict[str, Any] = {
        "bfinance_provenance": PROVENANCE,
        "bfinance_symbol": symbol,
        SNAPSHOT_ONLY_ATTR: bool(snapshot_only),
    }
    if source:
        attrs[SOURCE_ATTR] = redact(source)
    if note:
        attrs[NOTE_ATTR] = note
    if retrieved_at is not None:
        attrs[RETRIEVED_AT_ATTR] = retrieved_at.astimezone(timezone.utc).isoformat(
            timespec="seconds"
        )
    for key, value in extra.items():
        attrs[f"bfinance_trendlyne_{key}"] = value
    return attrs


# ------------------------------------------------------------------ OHLC parsing


def _rows_to_frame(
    rows: Sequence[Any],
    positions: Mapping[str, int],
    width: int,
    *,
    label: str,
) -> Tuple[pd.DataFrame, int, int]:
    """Turn wire rows into a typed frame.

    Returns:
        ``(frame, missing_close_rows, divergent_close_last_rows)``.

    Raises:
        TrendlyneSchemaError: A row is not a list of ``width`` values, a date will not parse, or
            every close is missing. Nothing is dropped or filled to recover from any of these.
    """
    if not rows:
        raise TrendlyneSchemaError(f"{label} is empty; nothing to parse.")
    if not isinstance(rows, (list, tuple)):
        raise TrendlyneSchemaError(f"{label} is {type(rows).__name__}, expected a list of rows.")

    bad_width = [i for i, row in enumerate(rows) if not isinstance(row, (list, tuple))
                 or len(row) != width]
    if bad_width:
        raise TrendlyneSchemaError(
            f"{label}: {len(bad_width)} of {len(rows)} rows do not have {width} values. "
            f"First offending row index: {bad_width[0]}. Upstream schema may have changed."
        )

    # The wire timestamp is a retrieval marker ('2026-09-18T10:01:00+00:00'), not a session time.
    # Only the literal date is taken: converting to a timezone first can move a bar onto the
    # wrong day (a 19:00 UTC bar is already the next morning in IST).
    raw_dates = [str(row[positions["date"]]) for row in rows]
    index = pd.to_datetime([d[:10] for d in raw_dates], format="%Y-%m-%d", errors="coerce")
    if index.isna().any():
        raise TrendlyneSchemaError(
            f"{label}: {int(index.isna().sum())} of {len(index)} dates are not YYYY-MM-DD "
            f"prefixed. First offending value: "
            f"{next(d for d, bad in zip(raw_dates, index.isna()) if bad)!r}"
        )

    data: Dict[str, Any] = {}
    for name in _OHLC_REQUIRED:
        if name == "date":
            continue
        # NOTE: values are passed as numpy arrays, not Series. A Series carries a RangeIndex
        # which pandas reindexes against the explicit DatetimeIndex below, silently producing an
        # all-NaN frame that still "succeeds". See the identical note in bfinance.nse.marketlens
        # and CONTEXT.md 9.22. ``np.asarray`` rather than ``.to_numpy()`` because pandas 3 returns
        # an ndarray from ``to_numeric`` where pandas 2 returned a Series.
        data[name] = np.asarray(pd.to_numeric([row[positions[name]] for row in rows],
                                             errors="coerce"))

    close = data["close_price"]
    if bool(pd.isna(close).all()):
        raise TrendlyneSchemaError(
            f"{label}: every close_price is missing or non-numeric. The series is unusable and "
            f"is not returned as an empty frame."
        )

    divergent = 0
    for name in _OHLC_EXTRA:
        if name not in positions:
            continue
        other = np.asarray(pd.to_numeric([row[positions[name]] for row in rows],
                                         errors="coerce"))
        both = ~pd.isna(close) & ~pd.isna(other)
        divergent += int((close[both] != other[both]).sum())

    frame = pd.DataFrame(
        {
            "Open": data["open_price"],
            "High": data["high_price"],
            "Low": data["low_price"],
            "Close": close,
            "Volume": data["volume"],
        },
        index=pd.DatetimeIndex(index, name="Date"),
    )
    return frame, int(pd.isna(close).sum()), divergent


def parse_ohlc(
    payload: Any,
    symbol: str,
    *,
    source_url: Optional[str] = None,
    include_last_week: bool = True,
    retrieved_at: Optional[datetime] = None,
    stock_id: Optional[int] = None,
) -> pd.DataFrame:
    """Parse an OHLC payload into a ``DatetimeIndex`` frame with truthful ``attrs``.

    Args:
        payload: The decoded ``/mapp/v1/stock/web/ohlc/{id}/{token}/`` response.
        symbol: The symbol, for ``attrs`` only. It is not cross-checked against the payload —
            the payload's own ``stockData`` is not part of the OHLC frame's contract.
        source_url: The URL used, for ``attrs``. The token is redacted.
        include_last_week: Merge ``eodDataLastWeek`` into the series. See the module docstring:
            the freshest five sessions live in that separate key, so a frame built from
            ``eodData`` alone stops a week short of the vendor's own latest bar. Dates already
            present in ``eodData`` are **never** overwritten — ``eodData`` is authoritative.
        retrieved_at: When the response arrived.
        stock_id: trendlyne's internal id for the symbol, recorded in ``attrs`` when known.

    Returns:
        Columns :data:`COLUMNS`, ascending ``Date`` index, with ``attrs`` recording provenance,
        the price basis, the vendor window, the merge, the invariant check and every missing value.

    Raises:
        TrendlyneNotAuthorizedError: The envelope reports a failed request (rejected token).
        TrendlyneSchemaError: Missing ``eodHeaders``/``eodData``, a renamed column, a row of the
            wrong width, an unparseable date, or an all-missing close.
    """
    body = parse_envelope(payload, context=f"the OHLC payload for {symbol}")
    for key in ("eodHeaders", "eodData"):
        if key not in body:
            raise TrendlyneSchemaError(
                f"the OHLC payload for {symbol} has no {key!r}. Body keys: "
                f"{sorted(body)[:12]}. Upstream schema may have changed."
            )

    headers = body["eodHeaders"]
    if not isinstance(headers, list) or not headers:
        raise TrendlyneSchemaError(
            f"the OHLC payload for {symbol} has an empty 'eodHeaders'; column order is "
            f"undefined, and guessing it is exactly the mistake this module refuses to make."
        )
    try:
        names = [
            str(header.get("unique_name") or header.get("name") or "").strip().lower()
            for header in headers
        ]
    except AttributeError as exc:
        raise TrendlyneSchemaError(
            f"the OHLC payload for {symbol} has a malformed 'eodHeaders' entry: {headers[0]!r}"
        ) from exc

    missing = [column for column in _OHLC_REQUIRED if column not in names]
    if missing:
        raise TrendlyneSchemaError(
            f"the OHLC payload for {symbol} is missing required column(s) {missing}. "
            f"Present: {names}. Upstream schema may have changed."
        )
    positions = {name: index for index, name in enumerate(names)}

    eod_frame, eod_missing_close, eod_divergent = _rows_to_frame(
        body["eodData"], positions, len(names), label=f"eodData for {symbol}"
    )

    eod_rows = len(eod_frame)
    last_week_added = 0
    frame = eod_frame
    if include_last_week:
        week_rows = body.get("eodDataLastWeek")
        if isinstance(week_rows, list) and week_rows:
            week_frame, _, week_divergent = _rows_to_frame(
                week_rows, positions, len(names), label=f"eodDataLastWeek for {symbol}"
            )
            # eodData wins on a shared date; the last-week key only contributes dates it has alone.
            fresh = week_frame[~week_frame.index.isin(eod_frame.index)]
            last_week_added = len(fresh)
            if last_week_added:
                frame = pd.concat([eod_frame, fresh])
            eod_divergent += week_divergent
    elif isinstance(body.get("eodDataLastWeek"), list) and body["eodDataLastWeek"]:
        last_week_added = -1  # -1 = present upstream, deliberately not merged

    # The wire is newest-first (verified: eodData[0] is 2026-09-18 and eodData[-1] is 2006-01-02),
    # so the frame is sorted here rather than inheriting the transmission order. Done
    # unconditionally: with include_last_week=False there is no concat to sort.
    frame = frame.sort_index()

    # Duplicate sessions are impossible in a correct series. Rather than silently dropping one,
    # keep the last occurrence and report the count.
    duplicated = int(frame.index.duplicated().sum())
    if duplicated:
        logger.warning(
            "trendlyne eodData for %s carries %d duplicate session date(s); keeping the last "
            "occurrence of each. attrs records the count.", symbol, duplicated,
        )
        frame = frame[~frame.index.duplicated(keep="last")]

    volume = frame["Volume"]
    integral = volume.dropna().map(lambda v: float(v).is_integer()).all()
    if bool(integral) and not volume.isna().any():
        frame["Volume"] = volume.astype("int64")
    else:
        non_integral = int((~volume.dropna().map(lambda v: float(v).is_integer())).sum())
        if non_integral:
            logger.warning(
                "trendlyne Volume for %s carries %d non-integral value(s); kept as float rather "
                "than truncated.", symbol, non_integral,
            )

    # The OHLC invariant, measured rather than assumed: 51,070/51,070 rows across 10 symbols, and
    # 5,107/5,107 on this module's own capture. Counted and reported, never repaired.
    upper = frame[["Open", "High", "Close"]].max(axis=1)
    lower = frame[["Open", "Low", "Close"]].min(axis=1)
    comparable = frame[["Open", "High", "Low", "Close"]].notna().all(axis=1)
    violations = int(((frame["High"] < upper) | (frame["Low"] > lower))[comparable].sum())
    if violations:
        logger.warning(
            "trendlyne OHLC for %s violates High>=max(O,C)/Low<=min(O,C) on %d of %d rows; "
            "reported in attrs, not repaired.", symbol, violations, int(comparable.sum()),
        )

    missing_values = {
        column: int(frame[column].isna().sum())
        for column in COLUMNS
        if int(frame[column].isna().sum())
    }
    first_date = frame.index[0].date().isoformat()
    last_date = frame.index[-1].date().isoformat()
    note = (
        f"Daily EOD series from trendlyne.com, spanning {first_date}..{last_date} "
        f"({len(frame)} bars) — truncated at the instrument's listing, so the bar count is "
        f"symbol-dependent (2006-01-02 is the floor for old listings; a 2023 IPO returns a few "
        f"hundred). Basis: "
        f"AS-TRADED, back-adjusted for splits and bonuses only; DEMERGERS ARE NOT APPLIED, so "
        f"these bars equal the NSE as-traded print wherever no split or bonus falls inside the "
        f"joined span. Do not splice them with bfinance.nse.bhavcopy bars across a split or bonus "
        f"date — the older bars are scaled there and the join carries a corporate-action-sized "
        f"step. Do not splice them with yfinance bars on a demerger name: yfinance rewrites that "
        f"history and this source does not."
    )
    frame.attrs.update(
        build_attrs(
            symbol,
            source=source_url,
            snapshot_only=False,
            note=note,
            retrieved_at=retrieved_at,
            stock_id=stock_id,
            exchange=body.get("exchange"),
            price_basis=PRICE_BASIS,
            window=f"{first_date}..{last_date} ({len(frame)} bars)",
            bars=len(frame),
            first_date=first_date,
            last_date=last_date,
            eod_rows=eod_rows,
            last_week_rows_added=last_week_added,
            missing_close_rows=eod_missing_close,
            close_last_divergent_rows=eod_divergent,
            ohlc_invariant_violations=violations,
            duplicate_dates=duplicated,
            missing_values=missing_values,
        )
    )
    frame.attrs["bfinance_synthetic_ohlc"] = False
    frame.attrs[PRICE_BASIS_ATTR] = PRICE_BASIS
    frame.attrs[DEMERGER_APPLIED_ATTR] = DEMERGER_APPLIED
    return frame[list(COLUMNS)]


def _parse_retry_after(response: httpx.Response) -> Optional[float]:
    """Parse a delta-seconds ``Retry-After`` header, capped. ``None`` if absent or unusable."""
    raw = response.headers.get("Retry-After") if response is not None else None
    if raw is None:
        return None
    try:
        delay = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if delay < 0:
        return None
    return min(delay, _MAX_RETRY_AFTER_SEC)


def _run_async(coro):
    """Bridge to sync, matching ``Ticker._run_async`` and ``MarketLensClient`` semantics."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(coro)).result()
    return asyncio.run(coro)


# ------------------------------------------------------------------ client


class TrendlyneClient:
    """Client for trendlyne.com. Constructing it performs **no I/O**.

    Everything — the 831 KB sitemap, the per-symbol token, every page — is fetched on first use
    and cached in memory with a TTL. A long-lived instance is the intended usage: it is what makes
    the second call for a symbol cost one request instead of two.

    Args:
        timeout: Per-request timeout, seconds.
        max_retries: Attempts for 429, 5xx and transport failures. Terminal statuses are never
            retried.
        proxy: Proxy URL. Falls back to ``HTTPS_PROXY``/``HTTP_PROXY``, as elsewhere in bfinance.
        min_request_interval_ms: Global pacing slot. See
            :data:`DEFAULT_MIN_REQUEST_INTERVAL_MS` for why the default is deliberately slow.
        index_ttl_hours: How long the parsed sitemap is reused.
        token_ttl_hours: How long a per-symbol token is reused. Tokens were observed to be stable
            across page loads, so this is a safety net rather than a necessity — and
            :meth:`ohlc` re-scrapes on rejection regardless of the TTL.
        cache: Optional duck-typed cache with ``get(key)``/``set(key, payload, category=, ttl_hours=)``
            — a :class:`~bfinance.cache.sqlite_cache.SQLiteCache` satisfies it. ``None`` (the
            default) keeps everything in memory, so importing this package never writes to the
            user's home directory. The type is not imported, keeping this module free of an
            import edge to ``bfinance.cache``.
        transport: Optional ``httpx`` transport. ``httpx.MockTransport`` is how the test suite
            exercises the 444/404/5xx paths with no network; production callers leave it ``None``.
        allow_member_search: Opt in to trendlyne's own autocomplete route, which its
            ``robots.txt`` disallows via the broad ``Disallow: */member/``. **Off by default, and
            that is the only shipped configuration.** See :data:`ROBOTS_DISALLOWED_ROUTES` and
            :data:`MEMBER_SEARCH_FLAG`. Turning it on is the one thing in this package that
            knowingly requests a path the site asks crawlers not to fetch, so it is named
            explicitly and never inferred.
    """

    #: Absolute base for every request. Exposed as a class attribute so a subclass can point at a
    #: mirror without having to touch the module-level helpers.
    BASE_URL = BASE_URL

    def __init__(
        self,
        timeout: float = 20.0,
        max_retries: int = 3,
        proxy: Optional[str] = None,
        min_request_interval_ms: float = DEFAULT_MIN_REQUEST_INTERVAL_MS,
        index_ttl_hours: float = 24.0,
        token_ttl_hours: float = 12.0,
        cache: Optional[Any] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        allow_member_search: bool = False,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self.proxy = proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        self.min_request_interval_ms = float(min_request_interval_ms)
        self.index_ttl_seconds = max(0.0, float(index_ttl_hours) * 3600.0)
        self.token_ttl_seconds = max(0.0, float(token_ttl_hours) * 3600.0)
        self.cache = cache
        self.transport = transport
        self.allow_member_search = bool(allow_member_search)
        self._last_request_time = 0.0
        # A threading.Lock, not asyncio.Lock: the sync wrappers call asyncio.run() per call, so
        # every call gets a fresh event loop and an asyncio primitive bound to an earlier one
        # would raise. The guarded sections hold no awaits.
        self._state_lock = threading.Lock()
        self._index: Optional[Dict[str, TrendlyneSymbol]] = None
        self._index_at: float = 0.0
        self._index_source_bytes: int = 0
        # (fetched_at, token, classification). The token and the sector/industry come out of the
        # same page fetch, so they are cached together: caching the token alone would mean a warm
        # token could not report a classification without spending a second request.
        self._tokens: Dict[int, Tuple[float, str, Dict[str, TrendlyneGroup]]] = {}
        #: How the most recent :meth:`resolve_any` resolved its symbol — ``"sitemap"``,
        #: ``"member_search"`` or ``"skipped_robots_disallowed"``. Written on every call so the
        #: degradation is recorded rather than inferred from a missing result. Read it; do not set
        #: it.
        self.symbol_resolution: Optional[str] = None

    def _require_member_search(self, allowed: Optional[bool]) -> None:
        """Refuse a robots-disallowed request, naming the conflict and the switch.

        Args:
            allowed: The per-call override, or ``None`` to use the constructor's setting.

        Raises:
            TrendlyneRobotsDisallowedError: The route is disallowed and the gate is closed. Raised
                **before** any socket is opened, so honouring robots costs this package nothing.
        """
        if allowed if allowed is not None else self.allow_member_search:
            return
        path = _SYMBOL_SEARCH_PATH.split("?")[0]
        rule = ROBOTS_DISALLOWED_ROUTES.get(path, "Disallow")
        raise TrendlyneRobotsDisallowedError(
            f"{path} is disallowed by trendlyne's robots.txt ({rule}), so this request was not "
            f"made. It is the only route to instruments the equity sitemap omits — every ETF "
            f"(NIFTYBEES, BANKBEES, GOLDBEES, JUNIORBEES and SETFNIF50 resolve here and appear in "
            f"none of the sitemap's 5,922 entries), so with the gate off those symbols are "
            f"unreachable through this package. That is a coverage gap, not a claim that they do "
            f"not exist. To request it anyway, pass {MEMBER_SEARCH_FLAG}=True to "
            f"TrendlyneClient(...) or to search_remote(...). Note robots.txt's 'User-agent: *' "
            f"block carries Crawl-delay: 1; the 10 s figure quoted elsewhere is scoped to "
            f"GPTBot/ClaudeBot."
        )

    # ---------------------------------------------------------------- internals

    def _headers(self, referer: Optional[str] = None, *, xhr: bool = False) -> Dict[str, str]:
        """
        Args:
            referer: Sent verbatim when given. Required by ``/mapp/v1/`` routes (see the module
                docstring's status matrix) and unnecessary on the HTML page routes.
            xhr: Adds ``X-Requested-With: XMLHttpRequest``. Needed by trendlyne's own
                autocomplete route, which answers **HTTP 200 with a non-JSON body** when the
                header is missing - a silent failure, not an error status. Not sent
                unconditionally, because a real browser does not send it on a navigation and
                adding it to the HTML page routes would misreport a page fetch as an XHR.
        """
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if referer:
            headers["Referer"] = referer
        if xhr:
            headers["X-Requested-With"] = "XMLHttpRequest"
        return headers

    async def _throttle_pacing(self) -> None:
        """Reserve a global pacing slot, so concurrent clients burst once rather than N times."""
        global _GLOBAL_LAST_REQUEST_TIME
        with _GLOBAL_PACING_LOCK:
            now = time.monotonic()
            last = max(self._last_request_time, _GLOBAL_LAST_REQUEST_TIME)
            elapsed_ms = (now - last) * 1000.0
            if elapsed_ms < self.min_request_interval_ms:
                wait_sec = (self.min_request_interval_ms - elapsed_ms) / 1000.0 + random.uniform(
                    0.01, 0.05
                )
                _GLOBAL_LAST_REQUEST_TIME = now + wait_sec
            else:
                wait_sec = 0.0
                _GLOBAL_LAST_REQUEST_TIME = now
        if wait_sec > 0.0:
            await asyncio.sleep(wait_sec)
        self._last_request_time = time.monotonic()
        with _GLOBAL_PACING_LOCK:
            _GLOBAL_LAST_REQUEST_TIME = max(_GLOBAL_LAST_REQUEST_TIME, self._last_request_time)

    def _absolute(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.BASE_URL}{path}"

    async def _send(
        self, path: str, *, referer: Optional[str] = None, xhr: bool = False
    ) -> httpx.Response:
        """GET with pacing and retry. Returns the 200 response or raises a typed error.

        Retry policy: 429 and 5xx and transport failures are retried with exponential backoff.
        Every 4xx — **including 444** — is terminal, because retrying an unchanged request cannot
        change its outcome and does cost the vendor a request.
        """
        url = self._absolute(path)
        last_error: Optional[Exception] = None
        last_response_error: Optional[TrendlyneError] = None

        for attempt in range(self.max_retries):
            await self._throttle_pacing()
            client_kwargs: Dict[str, Any] = {
                "timeout": self.timeout,
                "follow_redirects": True,
            }
            if self.proxy:
                client_kwargs["proxy"] = self.proxy
            if self.transport is not None:
                client_kwargs["transport"] = self.transport
            try:
                async with httpx.AsyncClient(**client_kwargs) as client:
                    response = await client.get(url, headers=self._headers(referer, xhr=xhr))
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(min(2.0 * (2**attempt), 8.0) + random.uniform(0, 0.5))
                continue

            status = response.status_code
            if status == 200:
                return response

            if status == 429:
                if attempt < self.max_retries - 1:
                    backoff = min(2.0 * (2**attempt), 8.0) + random.uniform(0.5, 1.5)
                    retry_after = _parse_retry_after(response)
                    if retry_after is not None:
                        backoff = max(backoff, retry_after)
                    logger.warning(
                        "trendlyne rate limited (%d) on %s; backing off %.1fs (attempt %d/%d).",
                        status, redact(url), backoff, attempt + 1, self.max_retries,
                    )
                    await asyncio.sleep(backoff)
                    continue
                raise TrendlyneRateLimitedError(
                    f"trendlyne rate limited this client (HTTP 429) after {self.max_retries} "
                    f"attempts for {redact(url)}. The index and token caches remain valid.",
                    status_code=status,
                    url=redact(url),
                )

            if status == 444:
                # Verified 2026-09-27: the /mapp/v1/ family answers 444 with a zero-length body
                # unless the request carries a Referer. Terminal, and never a decode error.
                raise TrendlyneAccessDeniedError(
                    f"trendlyne returned HTTP 444 for {redact(url)}. This is the site's request "
                    f"gate for /mapp/v1/, not a bad symbol: the identical URL returns 200 when "
                    f"the request carries a Referer. It is not retried, because retrying an "
                    f"unchanged request cannot succeed.",
                    status_code=status,
                    url=redact(url),
                )

            if status in (401, 403):
                raise TrendlyneAccessDeniedError(
                    f"trendlyne returned HTTP {status} for {redact(url)}. The route is public, so "
                    f"this is a request-shape or access change rather than bad credentials.",
                    status_code=status,
                    url=redact(url),
                )

            if status == 404:
                raise TrendlyneNotFoundError(
                    f"trendlyne has no route or resource at {redact(url)} (HTTP 404).",
                    status_code=status,
                    url=redact(url),
                )

            if status >= 500:
                last_response_error = TrendlyneServerError(
                    f"trendlyne returned HTTP {status} for {redact(url)}.",
                    status_code=status,
                    url=redact(url),
                )
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(min(2.0 * (2**attempt), 8.0) + random.uniform(0, 0.5))
                continue

            raise TrendlyneHTTPError(
                f"trendlyne returned unexpected HTTP {status} for {redact(url)}.",
                status_code=status,
                url=redact(url),
            )

        # A response that arrived and said "5xx" must surface as a server error, not as a
        # transport error: the two send the reader to completely different places.
        if last_response_error is not None:
            raise last_response_error
        raise TrendlyneTransportError(
            f"trendlyne request failed after {self.max_retries} attempts for {redact(url)}: "
            f"{last_error}"
        ) from last_error

    async def _get_text(self, path: str, *, referer: Optional[str] = None) -> str:
        response = await self._send(path, referer=referer)
        return response.text

    async def _get_json(
        self, path: str, *, referer: Optional[str] = None, xhr: bool = False
    ) -> Any:
        response = await self._send(path, referer=referer, xhr=xhr)
        try:
            return response.json()
        except ValueError as exc:
            # A 200 with an HTML or empty body: report the type, never a bare decode error.
            hint = ""
            if path.startswith(_SYMBOL_SEARCH_PATH.split("?")[0]):
                hint = (
                    " This is the autocomplete route, which answers HTTP 200 with a non-JSON "
                    "body when X-Requested-With: XMLHttpRequest is absent."
                )
            raise TrendlyneSchemaError(
                f"trendlyne returned a non-JSON body (HTTP {response.status_code}) for "
                f"{redact(self._absolute(path))}. Content-Type: "
                f"{response.headers.get('Content-Type', '<none>')!r}. First 160 chars: "
                f"{response.text[:160]!r}.{hint}"
            ) from exc

    # ------------------------------------------------------------------ public I/O

    async def fetch_text_async(
        self, path: str, *, referer: Optional[str] = None
    ) -> str:
        """GET a path and return the raw body. The primitive every sibling module builds on.

        Args:
            path: A path from :data:`ENDPOINTS` (built with :func:`build_path`), or an absolute
                URL.
            referer: Sent as the ``Referer`` header. Required by ``/mapp/v1/`` routes, optional
                elsewhere.

        Returns:
            The response body, unparsed.

        Raises:
            TrendlyneAccessDeniedError: HTTP 444/401/403.
            TrendlyneNotFoundError: HTTP 404.
            TrendlyneRateLimitedError: HTTP 429 after every retry.
            TrendlyneServerError: HTTP 5xx after every retry.
            TrendlyneTransportError: The request never completed.
            TrendlyneHTTPError: Any other unexpected status.
        """
        return await self._get_text(path, referer=referer)

    def fetch_text(self, path: str, *, referer: Optional[str] = None) -> str:
        """Sync variant of :meth:`fetch_text_async`."""
        return _run_async(self.fetch_text_async(path, referer=referer))

    async def fetch_json_async(
        self, path: str, *, referer: Optional[str] = None, xhr: bool = False
    ) -> Any:
        """GET a path and decode JSON, raising a typed error on a non-JSON body.

        Does **not** unwrap the ``head``/``body`` envelope — call :func:`parse_envelope` for that,
        so a failed-but-200 response can never be mistaken for data.

        Args:
            xhr: Send ``X-Requested-With: XMLHttpRequest``. Required by the autocomplete route,
                which otherwise answers 200 with a non-JSON body.
        """
        return await self._get_json(path, referer=referer, xhr=xhr)

    def fetch_json(self, path: str, *, referer: Optional[str] = None) -> Any:
        """Sync variant of :meth:`fetch_json_async`."""
        return _run_async(self.fetch_json_async(path, referer=referer))

    # ------------------------------------------------------------------ symbol index

    def load_symbol_index(self, xml_text: str) -> int:
        """Populate the symbol index from a sitemap document without any I/O.

        Exposed so a caller holding a cached sitemap — or a test holding a fixture — can resolve
        symbols offline. Returns the number of symbols indexed.

        Raises:
            TrendlyneSchemaError: The document held no recognisable entry.
        """
        index = parse_sitemap(xml_text)
        with self._state_lock:
            self._index = index
            self._index_at = time.monotonic()
            self._index_source_bytes = len(xml_text.encode("utf-8"))
        return len(index)

    def clear_caches(self) -> None:
        """Forget the sitemap index and every cached token. The next call re-fetches."""
        with self._state_lock:
            self._index = None
            self._index_at = 0.0
            self._index_source_bytes = 0
            self._tokens.clear()

    def _cached_index(self) -> Optional[Dict[str, TrendlyneSymbol]]:
        with self._state_lock:
            if self._index is None or self.index_ttl_seconds <= 0.0:
                return None
            if (time.monotonic() - self._index_at) > self.index_ttl_seconds:
                return None
            return self._index

    async def symbol_index_async(
        self, *, force_refresh: bool = False
    ) -> Dict[str, TrendlyneSymbol]:
        """The ``symbol -> TrendlyneSymbol`` index, from memory or from the sitemap.

        The live document holds 5,922 entries in 831 KB. A benign race can issue two GETs when
        two coroutines miss the cache simultaneously; that is left alone deliberately, because an
        ``asyncio.Lock`` would not survive the per-call event loop that the sync wrappers create.

        Args:
            force_refresh: Ignore the TTL and re-fetch.

        Returns:
            The index mapping. The returned dict is the client's own; treat it as read-only.
        """
        if not force_refresh:
            cached = self._cached_index()
            if cached is not None:
                return cached

        cache_key = "trendlyne:sitemap:v1"
        if not force_refresh and self.cache is not None:
            try:
                stored = self.cache.get(cache_key)
            except Exception:  # a broken cache must never break a data call
                logger.warning("trendlyne sitemap cache read failed; fetching instead.", exc_info=True)
                stored = None
            if isinstance(stored, str):
                index = parse_sitemap(stored)
                with self._state_lock:
                    self._index = index
                    self._index_at = time.monotonic()
                return index

        text = await self._get_text(SITEMAP_PATH)
        index = parse_sitemap(text)
        with self._state_lock:
            self._index = index
            self._index_at = time.monotonic()
            self._index_source_bytes = len(text.encode("utf-8"))
        if self.cache is not None:
            try:
                self.cache.set(cache_key, text, category="trendlyne", ttl_hours=24.0)
            except Exception:
                logger.warning("trendlyne sitemap cache write failed; continuing.", exc_info=True)
        return index

    def symbol_index(self, *, force_refresh: bool = False) -> Dict[str, TrendlyneSymbol]:
        """Sync variant of :meth:`symbol_index_async`."""
        return _run_async(self.symbol_index_async(force_refresh=force_refresh))

    @property
    def index_size(self) -> Optional[int]:
        """Number of symbols in the cached index, or ``None`` if it has not been loaded."""
        with self._state_lock:
            return None if self._index is None else len(self._index)

    @property
    def index_source_bytes(self) -> int:
        """Size of the sitemap document the cached index was parsed from.

        A diagnostic, not bookkeeping: the live document is ~831 KB for 5,922 symbols, so a much
        smaller value means the index came from an excerpt, a cached copy, or a truncated
        response — which is the first thing to check when a symbol "does not exist".
        """
        with self._state_lock:
            return self._index_source_bytes

    async def resolve_async(self, symbol: str) -> TrendlyneSymbol:
        """Resolve a symbol to its sitemap record.

        Uses :func:`bfinance.utils.symbols.normalize_symbol`, so ``reliance.ns``, ``RELIANCE``
        and ``RELIANCE-EQ`` all resolve to the same entry. Matching against the sitemap is exact:
        a symbol trendlyne does not carry raises rather than being guessed at.

        Raises:
            TrendlyneSymbolNotFoundError: Not in the sitemap. The message names the form that was
                tried, so a wrong ticker is distinguishable from a coverage gap.
            ValueError: ``symbol`` is empty or not a string.
        """
        cleaned = normalize_symbol(symbol or "")
        if not cleaned:
            raise ValueError("symbol must be a non-empty string")
        index = await self.symbol_index_async()
        record = index.get(cleaned)
        if record is None:
            hint = ""
            if len(index) > 1000:
                hint = (
                    f" The index holds {len(index)} symbols; use "
                    f"TrendlyneClient.search() to look one up by name."
                )
            raise TrendlyneSymbolNotFoundError(
                f"{cleaned!r} is not in trendlyne's equity sitemap (tried {cleaned!r}). "
                f"trendlyne has no symbol-search API, so the sitemap is the only resolver and "
                f"this package will not guess a URL.{hint}"
            )
        return record

    def resolve(self, symbol: str) -> TrendlyneSymbol:
        """Sync variant of :meth:`resolve_async`."""
        return _run_async(self.resolve_async(symbol))

    async def search_async(self, query: str, limit: int = 10) -> List[TrendlyneSymbol]:
        """Substring search over the **local** index — no request beyond loading the sitemap.

        The sitemap's symbol segment is trendlyne's own key and is not always a ticker: the live
        tail holds ``/equity/3622447/1216/bse-midsmall-private-banks/``. The slug carries the
        company name, so both are searched.

        Args:
            query: Matched case-insensitively against the symbol and the slug.
            limit: Maximum results. Exact symbol matches are ranked first.

        Returns:
            Up to ``limit`` records, best match first. Empty when nothing matches — a fuzzy miss
            is an expected outcome of a local search, not an error.
        """
        clean = (query or "").strip().lower()
        if not clean:
            raise ValueError("query must be a non-empty string")
        index = await self.symbol_index_async()
        exact, partial = [], []
        for symbol, record in index.items():
            if symbol.lower() == clean:
                exact.append(record)
            elif clean in symbol.lower() or clean in record.slug.lower():
                partial.append(record)
        return (exact + partial)[: max(1, int(limit))]

    def search(self, query: str, limit: int = 10) -> List[TrendlyneSymbol]:
        """Sync variant of :meth:`search_async`."""
        return _run_async(self.search_async(query, limit=limit))

    # ------------------------------------------------------- remote symbol search

    async def search_remote_async(
        self, query: str, *, limit: int = 10, allow_member_search: Optional[bool] = None
    ) -> List[TrendlyneSymbol]:
        """Resolve a symbol through trendlyne's own **remote** autocomplete. **Opt-in; off by
        default, because robots.txt disallows it.**

        :data:`_SYMBOL_SEARCH_PATH` sits under trendlyne's broad ``Disallow: */member/`` rule, so
        this method used to be a live robots violation in shipped code: it was the ETF fallback
        inside :meth:`resolve_any`, so any ``ohlc()`` call for a symbol the sitemap omitted quietly
        fetched a disallowed path. It is now gated.

        This route is the only way to instruments the equity sitemap does not carry. Verified
        2026-09-27: ``NIFTYBEES`` (id 1588), ``BANKBEES`` (1581), ``GOLDBEES`` (1582),
        ``JUNIORBEES`` (1584) and ``SETFNIF50`` (1753) all resolve here and serve full OHLC, yet
        **none of them appears in the sitemap's 5,922 entries** — checked against the full
        830,905-byte document, which contains no entry with ``BEES`` or ``ETF`` in the symbol
        segment and none beginning ``SETF``. The only non-ticker entries are BSE index families
        (``1216``, ``1214``) and ``NIFTYBOND2033``. **So with the gate off, no ETF is reachable
        through this package at all**, and that is stated here rather than discovered later as a
        confusing "symbol not found".

        Use this when :meth:`search_async` finds nothing; the two are complementary, not
        competing. Costs one request and needs no token.

        Args:
            query: Symbol or company-name fragment. trendlyne matches either.
            limit: Maximum records to return.
            allow_member_search: Per-call override of the constructor's setting. ``True`` sends the
                request anyway, accepting that it is robots-disallowed. ``None`` (the default)
                defers to the constructor.

        Returns:
            Matching records. Empty when trendlyne has no match - it answers a miss with
            ``[{"headline": "No matches found"}]``, which is an expected outcome, not an error.

        Raises:
            TrendlyneRobotsDisallowedError: The gate is closed. The message names the disallowed
                path, the covering ``Disallow`` rule, and the flag that opens the gate. **Raised
                before the request**, not answered with an empty list: an empty list is
                indistinguishable from "trendlyne has no such symbol", and a caller would conclude
                the instrument does not exist.
            TrendlyneSchemaError: The response was not the expected list shape.
            ValueError: ``query`` is empty.
        """
        clean = (query or "").strip()
        if not clean:
            raise ValueError("query must be a non-empty string")
        self._require_member_search(allow_member_search)

        path = _SYMBOL_SEARCH_PATH.format(query=quote_plus(clean))
        payload = await self.fetch_json_async(path, xhr=True)
        if isinstance(payload, dict):
            payload = payload.get("data") or payload.get("results") or []
        if not isinstance(payload, list):
            raise TrendlyneSchemaError(
                f"symbol search returned {type(payload).__name__}, expected a list. "
                f"First 200 chars: {str(payload)[:200]!r}"
            )

        out: List[TrendlyneSymbol] = []
        for item in payload:
            if not isinstance(item, dict) or "k" not in item:
                # The no-match sentinel, or a record without an id. Skip rather than invent one.
                continue
            symbol = str(item.get("id") or item.get("value") or "").strip()
            slug = str(item.get("slugname") or "").strip()
            if not symbol or not slug:
                continue
            try:
                stock_id = int(item["k"])
            except (TypeError, ValueError):
                continue
            out.append(TrendlyneSymbol(symbol=symbol, stock_id=stock_id, slug=slug))
            if len(out) >= max(1, int(limit)):
                break
        return out

    def search_remote(
        self, query: str, *, limit: int = 10, allow_member_search: Optional[bool] = None
    ) -> List[TrendlyneSymbol]:
        """Sync variant of :meth:`search_remote_async`."""
        return _run_async(
            self.search_remote_async(query, limit=limit, allow_member_search=allow_member_search)
        )

    async def resolve_any_async(
        self, symbol: str, *, allow_member_search: Optional[bool] = None
    ) -> TrendlyneSymbol:
        """Resolve via the sitemap index first, then fall back to the remote search — **if that is
        allowed**.

        The sitemap is one bulk request that covers 5,922 instruments, so it is tried first and is
        the cheaper path for equities. The remote search is the fallback that rescues ETFs and
        anything else trendlyne indexes but does not list — and it is the route robots.txt
        disallows, so the fallback is **gated** by :data:`MEMBER_SEARCH_FLAG` and is **off** in the
        default configuration.

        When the gate is closed the fallback is **skipped explicitly** — not attempted, not
        silently ignored — and the skip is recorded in two places: on
        :attr:`TrendlyneClient.symbol_resolution` as ``"skipped_robots_disallowed"``, and in the
        message of the :class:`TrendlyneSymbolNotFoundError` that follows. That distinction is the
        point. A caller who asks for ``NIFTYBEES`` has to be able to tell "this package will not
        ask" from "this instrument does not exist", and only the first is true. :meth:`ohlc`
        copies the recorded value onto the frame as
        ``attrs["bfinance_trendlyne_symbol_resolution"]``, so the degradation is visible on every
        OHLC frame and not only on the failure path.

        Args:
            symbol: Anything :meth:`resolve_async` accepts.
            allow_member_search: Per-call override of the constructor's setting, forwarded to
                :meth:`search_remote_async`.

        Returns:
            The symbol's record.

        Raises:
            TrendlyneSymbolNotFoundError: Neither route produced a record. The message separates the
                two reasons — the sitemap does not carry the symbol, and the remote route was
                either tried and found nothing or skipped because it is robots-disallowed.
            TrendlyneRobotsDisallowedError: Never from here. The skip is reported as a not-found,
                because the caller asked "resolve this symbol" and a robots-policy failure is not a
                different answer to that question, only a narrower way of giving the same one.
                Raising it here would make every ETF lookup fail with something the caller cannot
                act on. :meth:`search_remote` is where the policy error belongs.
        """
        try:
            record = await self.resolve_async(symbol)
        except TrendlyneSymbolNotFoundError:
            pass
        else:
            self.symbol_resolution = "sitemap"
            return record

        gate_open = (
            allow_member_search if allow_member_search is not None else self.allow_member_search
        )
        disallowed = _SYMBOL_SEARCH_PATH.split("?")[0]
        if not gate_open:
            self.symbol_resolution = "skipped_robots_disallowed"
            raise TrendlyneSymbolNotFoundError(
                f"{symbol!r} is not in trendlyne's equity sitemap "
                f"({self.index_size} entries). The remote route was NOT tried: {disallowed} is "
                f"disallowed by trendlyne's robots.txt "
                f"({ROBOTS_DISALLOWED_ROUTES[disallowed]}), so the gate is closed. {symbol!r} may "
                f"well exist on trendlyne — the sitemap does not carry it, and it is exactly the "
                f"instruments the sitemap omits (every ETF) that the disallowed route would have "
                f"resolved. Pass {MEMBER_SEARCH_FLAG}=True to use it anyway. Mutual funds are a "
                f"separate namespace (/mutual-fund/nav/{{id}}/) and are not reachable from here."
            )

        matches = await self.search_remote_async(symbol, limit=5, allow_member_search=True)
        if matches:
            self.symbol_resolution = "member_search"
            return matches[0]
        self.symbol_resolution = "sitemap"
        raise TrendlyneSymbolNotFoundError(
            f"{symbol!r} resolved by neither route: not in the equity sitemap "
            f"({self.index_size} entries) and trendlyne's own search returned no match. "
            f"Mutual funds are a separate namespace (/mutual-fund/nav/{{id}}/) and are not "
            f"reachable from here."
        )

    def resolve_any(
        self, symbol: str, *, allow_member_search: Optional[bool] = None
    ) -> TrendlyneSymbol:
        """Sync variant of :meth:`resolve_any_async`."""
        return _run_async(
            self.resolve_any_async(symbol, allow_member_search=allow_member_search)
        )

    # ------------------------------------------------------------------ token

    async def ohlc_token_async(
        self, symbol: str, *, force_refresh: bool = False
    ) -> Tuple[str, Dict[str, TrendlyneGroup]]:
        """The per-symbol OHLC token **and** the symbol's classification, cached together.

        Costs one HTML request the first time per symbol and none afterwards. The token lives only
        in the symbol page's ``data-eodurl`` attribute and differs per symbol.

        **The classification is free and rides along here.** :func:`parse_classification` reads the
        sector and industry out of the schema.org breadcrumb on the very same page, so attaching it
        costs **no request at all** — a second fetch would double the cold cost of every symbol for
        data the page already carried. It is cached in the same entry as the token for the same
        reason: caching the token alone would mean a warm token could not report a classification
        without a second request.

        Args:
            symbol: Anything :meth:`resolve_async` accepts.
            force_refresh: Re-scrape even if a cached token is still fresh.

        Returns:
            ``(token, classification)``. The token is verbatim, including any ``=`` padding. The
            classification is ``{"sector": ..., "industry": ...}`` and may be **empty** — see
            :func:`parse_classification`; an absent breadcrumb is a fact to report, not a reason to
            fail a call whose product is a price series.

        Raises:
            TrendlyneSymbolNotFoundError: The symbol resolves by neither the sitemap nor — when the
                gate is open — trendlyne's own search.
            TrendlyneTokenError: The page carried no token (a markup change).
            TrendlyneAccessDeniedError / TrendlyneNotFoundError: The page itself failed.
        """
        record = await self.resolve_any_async(symbol)
        if not force_refresh and self.token_ttl_seconds > 0.0:
            with self._state_lock:
                cached = self._tokens.get(record.stock_id)
            if cached and (time.monotonic() - cached[0]) <= self.token_ttl_seconds:
                return cached[1], cached[2]

        html = await self._get_text(record.page_path)
        token = extract_ohlc_token(html)
        classification = parse_classification(html)
        with self._state_lock:
            self._tokens[record.stock_id] = (time.monotonic(), token, classification)
        return token, classification

    def ohlc_token(self, symbol: str, *, force_refresh: bool = False) -> str:
        """Sync variant of :meth:`ohlc_token_async`, returning just the token."""
        return _run_async(self.ohlc_token_async(symbol, force_refresh=force_refresh))[0]

    async def classification_async(self, symbol: str) -> Dict[str, TrendlyneGroup]:
        """The symbol's sector and industry, off the page the token fetch already makes.

        The strict door onto :func:`parse_classification`: a page with no classification breadcrumb
        **raises** here instead of returning an empty mapping, because a caller who asked for the
        classification must not be handed ``{}`` and left to guess whether the stock is genuinely
        unclassified or the markup changed. :meth:`ohlc_async` uses the lenient parser instead —
        there the classification is a bonus and the price series is the product.

        Costs nothing beyond :meth:`ohlc_token_async`, whose page it shares.

        Raises:
            TrendlyneSchemaError: The page carried no ``SECTOR :`` or ``INDUSTRY :`` breadcrumb.
        """
        _, classification = await self.ohlc_token_async(symbol)
        if not classification:
            raise TrendlyneSchemaError(
                f"the page fetched for {symbol!r} carried no 'SECTOR :' or 'INDUSTRY :' "
                f"breadcrumb. That label plus the href is the only place the classification "
                f"exists — there is no data-* attribute and no embedded JSON for it — so a "
                f"missing one is a markup change, not an unclassified stock."
            )
        return classification

    def classification(self, symbol: str) -> Dict[str, TrendlyneGroup]:
        """Sync variant of :meth:`classification_async`."""
        return _run_async(self.classification_async(symbol))

    # ------------------------------------------------------------------ payloads

    async def ohlc_async(
        self,
        symbol: str,
        *,
        include_last_week: bool = True,
    ) -> pd.DataFrame:
        """Daily OHLCV, as-traded and split/bonus-adjusted. Two requests the first time, one after.

        Args:
            symbol: Anything :meth:`resolve_async` accepts.
            include_last_week: Merge ``eodDataLastWeek`` — the five most recent sessions, which
                the main ``eodData`` key does not contain. See the module docstring.

        Returns:
            A ``DatetimeIndex`` frame with columns :data:`COLUMNS` and ``attrs`` recording
            provenance, price basis, vendor window and every missing value. Never empty: a
            symbol with no usable bars raises instead. It also carries
            ``bfinance_trendlyne_sector`` and ``bfinance_trendlyne_industry`` — each a
            ``{"id", "slug", "name"}`` mapping, or ``None`` — and
            ``bfinance_trendlyne_symbol_resolution``, read off the page the OHLC token came from
            at no extra request cost. See
            :meth:`TrendlyneClient._resolution_and_classification_attrs`.

        Raises:
            TrendlyneSymbolNotFoundError: The symbol is not in the sitemap, and the remote route
                that would have found it was skipped because it is robots-disallowed. The message
                says which, so an ETF miss is distinguishable from a typo.
            TrendlyneTokenError: No token in the symbol page.
            TrendlyneAccessDeniedError: HTTP 444 — the ``/mapp/v1/`` gate.
            TrendlyneNotAuthorizedError: The envelope rejected the token, and re-scraping it once
                did not produce a different one.
            TrendlyneSchemaError: The payload did not have the expected shape.
        """
        record = await self.resolve_any_async(symbol)
        retrieved_at = datetime.now(timezone.utc)
        token, classification = await self.ohlc_token_async(record.symbol)
        path = build_path("ohlc", record, token=token)
        payload = await self._get_json(path, referer=record.page_url)
        extra = self._resolution_and_classification_attrs(classification)

        try:
            frame = parse_ohlc(
                payload,
                record.symbol,
                source_url=self._absolute(path),
                include_last_week=include_last_week,
                retrieved_at=retrieved_at,
                stock_id=record.stock_id,
            )
        except TrendlyneNotAuthorizedError:
            # The token may have rotated upstream. Re-scrape once — and only if it actually
            # changed, so a genuinely rejected token costs one page fetch, not a retry storm.
            fresh, _ = await self.ohlc_token_async(record.symbol, force_refresh=True)
            if fresh == token:
                raise
            logger.info(
                "trendlyne rotated the OHLC token for %s; retried once with the new one.",
                record.symbol,
            )
            retry_path = build_path("ohlc", record, token=fresh)
            payload = await self._get_json(retry_path, referer=record.page_url)
            frame = parse_ohlc(
                payload,
                record.symbol,
                source_url=self._absolute(retry_path),
                include_last_week=include_last_week,
                retrieved_at=retrieved_at,
                stock_id=record.stock_id,
            )
        frame.attrs.update(extra)
        return frame

    def _resolution_and_classification_attrs(
        self, classification: Mapping[str, "TrendlyneGroup"]
    ) -> Dict[str, Any]:
        """The sector/industry and resolver facts every OHLC frame carries.

        Two separate things travel here and they must not be conflated:

        * **Classification** — what the symbol *is*. ``bfinance_trendlyne_sector`` and
          ``bfinance_trendlyne_industry`` are each a ``(id, slug, name)`` triple, taken from the
          page the OHLC token was read out of, so they cost no request. When the page carried no
          breadcrumb, ``bfinance_trendlyne_classification`` is the string ``"absent"`` — stated
          rather than omitted, because a missing key reads as "not applicable" and this is a
          change to the page.
        * **Resolution** — how the symbol was found. ``bfinance_trendlyne_symbol_resolution`` is
          ``"sitemap"``, ``"member_search"``, or ``"skipped_robots_disallowed"``. The third is the
          one that matters: it is how a caller learns that a whole class of instrument (ETFs) is
          unreachable in this configuration, on a frame that otherwise succeeded.

        Written on every frame, values included, so a consumer can rely on the keys existing.
        """
        extra: Dict[str, Any] = {
            "bfinance_trendlyne_symbol_resolution": self.symbol_resolution,
            "bfinance_trendlyne_classification": "absent" if not classification else "present",
        }
        for kind in ("sector", "industry"):
            key = f"bfinance_trendlyne_{kind}"
            group = classification.get(kind)
            extra[key] = None if group is None else {
                "id": group.group_id, "slug": group.slug, "name": group.name,
            }
        return extra

    def ohlc(self, symbol: str, *, include_last_week: bool = True) -> pd.DataFrame:
        """Sync variant of :meth:`ohlc_async`."""
        return _run_async(self.ohlc_async(symbol, include_last_week=include_last_week))

    async def chart_metric_async(self, symbol: str, metric: str) -> Any:
        """The raw ``chart-data`` payload for a metric. No token needed.

        Args:
            symbol: Anything :meth:`resolve_async` accepts.
            metric: One of :data:`METRICS`; validated locally, because an unknown metric comes
                back as a 200 with a failed envelope rather than a 404.

        Returns:
            The decoded JSON, envelope included. Parsing is left to the sibling module that owns
            metrics, because the timestamp handling is a real trap: ``body["eodData"]`` is
            ``[epoch_ms, value]`` pairs stamped at **midnight IST on quarter-end dates**, so the
            epoch must be converted to ``Asia/Kolkata`` before the date is taken — read as UTC,
            every point lands one day early. Verified 2026-09-27 on MOTHERSON ``EPS_TTM``:
            1782757800000 is 2026-06-30 00:00 +05:30, not 2026-06-29.

        Raises:
            ValueError: ``metric`` is not in :data:`METRICS`.
            TrendlyneSymbolNotFoundError: The symbol resolves by neither route.
            TrendlyneAccessDeniedError: HTTP 444 from the ``/mapp/v1/`` gate.
        """
        record = await self.resolve_any_async(symbol)
        path = build_path("chart_metric", record, metric=metric)
        return await self._get_json(path, referer=record.page_url)

    def chart_metric(self, symbol: str, metric: str) -> Any:
        """Sync variant of :meth:`chart_metric_async`."""
        return _run_async(self.chart_metric_async(symbol, metric))

    # ------------------------------------------------------------------ sector & industry

    async def group_page_async(self, group: TrendlyneGroup) -> str:
        """The raw HTML of a sector or industry page.

        The one fetch every group helper below shares. Returned unparsed because the two products
        are independent: :func:`~bfinance.trendlyne.sectors.parse_constituents` and
        :func:`~bfinance.trendlyne.sectors.parse_taxonomy` read disjoint parts of the same
        document, and a caller who wants both should pay for one request.

        Checked against every ``User-agent: *`` ``Disallow`` rule in trendlyne's robots.txt
        (28 of them, read live 2026-09-27): ``/equity/sector/{id}/{slug}/`` and
        ``/equity/industry/{id}/{slug}/`` match **none**. The only equity rule is
        ``*/equity/technical-analysis-*``, which needs that literal prefix.

        Args:
            group: From :func:`~bfinance.trendlyne.sectors.parse_taxonomy` or
                :meth:`taxonomy_async`. No group id is ever hardcoded in this package.

        Raises:
            ValueError: ``group`` is not a :class:`~bfinance.trendlyne.sectors.TrendlyneGroup`.
            TrendlyneNotFoundError: HTTP 404 — no such group id.
        """
        if not isinstance(group, TrendlyneGroup):
            raise ValueError(
                f"group must be a TrendlyneGroup, got {type(group).__name__}. Build one from "
                f"parse_taxonomy() so the id, slug and name all come off a page."
            )
        return await self._get_text(build_path(f"{group.kind}_page", group=group))

    def group_page(self, group: TrendlyneGroup) -> str:
        """Sync variant of :meth:`group_page_async`."""
        return _run_async(self.group_page_async(group))

    async def constituents_async(self, group: TrendlyneGroup) -> pd.DataFrame:
        """Every constituent of a sector or industry, weighted by market cap.

        One request. The frame's ``attrs`` carry the row count and
        ``bfinance_trendlyne_rows_suspiciously_capped`` so a server-side truncation is visible —
        the site caps other pages at **exactly 500** rows, and a truncated list is otherwise
        indistinguishable from a complete one. The market-cap **unit is not asserted**; use
        ``Weight %``, which is exact and unit-free. See :mod:`bfinance.trendlyne.sectors`.
        """
        html = await self.group_page_async(group)
        result = parse_constituents(html)
        frame = result.frame
        frame.attrs.update(constituent_attrs(result, source_url=group.url))
        return frame

    def constituents(self, group: TrendlyneGroup) -> pd.DataFrame:
        """Sync variant of :meth:`constituents_async`."""
        return _run_async(self.constituents_async(group))

    async def taxonomy_async(self, group: TrendlyneGroup) -> Dict[str, TrendlyneGroup]:
        """The complete sector and industry vocabulary, from **one** page fetch.

        The select carries the same 164 options on every group page, so any one of them will do —
        which is what makes this cheap. The group is an explicit argument rather than a hardcoded
        probe page because **no group id is hardcoded anywhere in this package**: ids are read off
        a page or supplied by the caller, never invented. Once you have a
        :class:`~bfinance.trendlyne.sectors.TrendlyneGroup` — from here, or built yourself from
        an id you know — the rest of the vocabulary costs nothing more.

        Args:
            group: Which page to read the vocabulary off.

        Returns:
            ``{"sector:20": TrendlyneGroup, "industry:9": TrendlyneGroup, ...}``.

        Raises:
            ValueError: ``group`` is not a
                :class:`~bfinance.trendlyne.sectors.TrendlyneGroup`.
            TrendlyneSchemaError: The page carried no taxonomy select.
        """
        return parse_taxonomy(await self.group_page_async(group))

    def taxonomy(self, group: TrendlyneGroup) -> Dict[str, TrendlyneGroup]:
        """Sync variant of :meth:`taxonomy_async`."""
        return _run_async(self.taxonomy_async(group))

    async def industry_aggregate_async(self, sector: TrendlyneGroup) -> pd.DataFrame:
        """The per-industry aggregate block that sits beside a sector's constituent list.

        A second, **separately signed** table on the same page. It is what makes the sector
        constituent list trustworthy: the block's per-industry company counts must sum to the
        list's row count, which is a server-side cross-check that needs no second request.
        """
        if sector.kind != "sector":
            raise ValueError(
                f"the industry aggregate block only appears on a sector page; got kind="
                f"{sector.kind!r}. Its own constituent list is available via constituents()."
            )
        return parse_industry_aggregate(await self.group_page_async(sector))

    def industry_aggregate(self, sector: TrendlyneGroup) -> pd.DataFrame:
        """Sync variant of :meth:`industry_aggregate_async`."""
        return _run_async(self.industry_aggregate_async(sector))


#: Shared client for the module-level shortcuts. Created on first use, never at import.
_DEFAULT_CLIENT: Optional[TrendlyneClient] = None


def _default_client() -> TrendlyneClient:
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = TrendlyneClient()
    return _DEFAULT_CLIENT


def ohlc(symbol: str, *, include_last_week: bool = True) -> pd.DataFrame:
    """Module-level shortcut for :meth:`TrendlyneClient.ohlc`.

    >>> from bfinance.trendlyne import ohlc
    >>> ohlc("RELIANCE").tail()          # doctest: +SKIP
    """
    return _default_client().ohlc(symbol, include_last_week=include_last_week)


def ohlc_async(symbol: str, *, include_last_week: bool = True) -> pd.DataFrame:
    """Async variant of :func:`ohlc`."""
    return _default_client().ohlc_async(symbol, include_last_week=include_last_week)


def resolve(symbol: str) -> TrendlyneSymbol:
    """Module-level shortcut for :meth:`TrendlyneClient.resolve`."""
    return _default_client().resolve(symbol)


def symbol_index(*, force_refresh: bool = False) -> Dict[str, TrendlyneSymbol]:
    """Module-level shortcut for :meth:`TrendlyneClient.symbol_index`."""
    return _default_client().symbol_index(force_refresh=force_refresh)
