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

Endpoints — every template has a fetch method behind it
------------------------------------------------------
Base ``https://trendlyne.com``. No login, no API key, no cookie: a browser ``User-Agent`` is the
whole requirement. :data:`ENDPOINTS` holds **only the routes this package can address today**,
and :func:`build_path` is the supported way to build one, so the token rule is enforced locally.

That dict used to be a dishonest one. It listed 17 templates, of which **14 no method ever
built** — the section tables (deals, insider trades, share and mutual-fund holdings, financials,
research reports, board meetings, buy-sell-zone), the four ``latest-news`` routes, and ``rights``.
None had a fetch method, and five of the 14 were never even confirmed to answer 200. A template
with no method behind it is a promise about unwritten code, and ``build_path`` would return a
well-formed URL for a route nobody had ever requested. The 14 were deleted; ``docs/UPSTREAM_SOURCES.md``
keeps the full scraping study, so nothing measured was lost.

**They are wired now, measured rather than assumed.** A 2026-10-10 reconnaissance pass against
trendlyne.com re-read ``robots.txt`` and fetched each candidate route for TCS (id 1372) and NTPC
(id 959), and this dict grew by 14: eleven section pages and three JSON siblings. Every one of
them is **robots-clean** — the verdict is recorded as a comment on the key itself in
:data:`ENDPOINTS`, so the claim is auditable from the file rather than from prose. The one equity
rule in the ``User-agent: *`` group is ``*/equity/technical-analysis-*``, and it does **not** match
``/equity/api/stock/adv-technical-analysis/…``: the rule needs that literal substring, and this
route has ``/api/stock/`` in the middle. The ``technical-analysis`` **page** stays unwired and
off-limits; the JSON sibling is what :meth:`TrendlyneClient.adv_technical_analysis_async` reads.
``/alerts/stock-alerts/`` is likewise allowed — only ``/alerts/addStockAlert/`` and
``/alerts/modal/stockAlert/`` are disallowed, and neither is a route this package builds.

**A test now fails if a template is added without a method behind it**
(``tests/test_trendlyne_core.py::TestNoDeadScaffolding``), and one request per section call is
part of the contract those tests pin. The symbol resolves through the sitemap index, which is
cached, and the section route is fetched once — no stock-page fetch, no token fetch, because a
section route needs only the id, the symbol and the slug the index already holds.

Three guards belong to this layer rather than to the parsers, and all three are measurements:

* **WAF** — HTTP 405, or an ``x-amzn-waf-action`` header, is
  :class:`~bfinance.trendlyne.errors.TrendlyneWafChallengeError`, terminal and never retried. See
  the "Pacing, retries and caching" section.
* **Snapshot-only** — ``/equity/share-holding/`` answers **HTTP 500** for any period but
  ``latest``, so :meth:`TrendlyneClient.share_holding_async` refuses a non-``latest`` period
  locally and spends no request on a guaranteed error. The mutual-fund route is the measured
  counter-example and deliberately does **not** carry that guard.
* **Row cap** — ``/equity/insider-trading-sast/all/`` is capped at **exactly 500** rows
  server-side, which is indistinguishable from a complete history unless it is said out loud.
  :data:`INSIDER_ROW_CAP` and the note on that frame carry it.

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

The richer search route, and why it categorises instead of filtering
-------------------------------------------------------------------
``GET /member/api/ac_snames/all/?term={q}&all-results=true`` is a **second** autocomplete, and it
returns **22 fields** where the plain one at :data:`_SYMBOL_SEARCH_PATH` returns 4. One real record,
verbatim::

    {"id": "NIFTYBEES", "label": "Nippon India ETF Nifty 50 BeES(NIFTYBEES)",
     "value": "NIFTYBEES", "k": 1588, "slugname": "nippon-india-etf-nifty-50-bees",
     "country": "IND", "defaultExchange": "NSE", "stock_code": "NIFTYBEES",
     "UIcode": "NIFTYBEES", "category": "ETF", "pageType": 10, "NSEcode": "NIFTYBEES",
     "BSEcode": "590103", "isCfd": false, "currentPrice": 259.47, "day_change": -1.0,
     "day_changeP": -0.38, "day_high": 260.49, "day_low": 257.9, "sectorName": "Others",
     "industry": {}, "nexturl": "https://trendlyne.com/equity/1588/NIFTYBEES/...",
     "urls": [["Overview", "https://trendlyne.com/equity/1588/NIFTYBEES/..."]]}

The top level is a **bare JSON list**, not an object with a ``data`` key, so the unwrapping is the
same ``payload.get("data") or payload.get("results") or []`` the plain route does, applied only when
the payload is a dict. Measured across **136 distinct (category, symbol) pairs from 8 queries** on
2026-09-30, the vendor's category vocabulary is exactly six strings, and they are not decoration:

    ============  ===  ===========  ===========================================
    category        n   pageType    what resolving to it would mean
    ============  ===  ===========  ===========================================
    Equity          73           2  a normal listed share, OHLC available
    Index           26          24  an index series, OHLC available
    ETF             12          10  an exchange-traded fund, OHLC available
    Mutual Funds    11          12  **no OHLC route exists at all**
    Not Traded       9           2  delisted or suspended
    Superstars        5     absent  a vendor-side grouping, not an instrument type
    ============  ===  ===========  ===========================================

**A ``Mutual Funds`` hit has no OHLC route whatsoever** -- mutual funds are a separate namespace at
``/mutual-fund/nav/{id}/``, which this package already says so in its own not-found message -- and a
``Not Traded`` hit is delisted. Either one handed back as a price history would be a **wrong** answer
rather than a thin one. So the category is carried on the result (:attr:`SymbolCategory`,
:func:`parse_symbol_category`, :meth:`SymbolCategory.has_ohlc`) and **never used to filter
silently**: dropping the 20 unfetchable rows out of a ranked list would make the list look
exhaustive when it is not, and the caller is the only one who knows whether a fund or a delisted
line is what they asked for. An unrecognised vendor string maps to
:attr:`SymbolCategory.OTHER` and is reported as unknown, never guessed into ``equity``.

``label`` is the display name with the ticker appended in parentheses, and the parentheses are the
trap: a company name may legitimately contain its own, so only a **trailing** group that equals the
ticker is stripped. See :func:`_display_name`.

This route is **not** wired into :meth:`TrendlyneClient.resolve_any_async`, and the reason is the
one recorded in commit ``06f6c7c``: trendlyne's search is fuzzy and ranked, so it answers a question
with 20 candidates and the first is the vendor's guess rather than an answer. ``ohlc("nifty bees")``
returning BANKBEES -- a plausible frame for the wrong security, indistinguishable downstream -- is the
worst outcome this package can produce. Discovery and resolution are different questions, so they get
different methods. It is also gated **twice**: it needs :data:`MEMBER_SEARCH_FLAG` *and*
:data:`FULL_SEARCH_FLAG`, because it is a second route into the same robots-disallowed namespace, and
a second opt-in is the cheapest honest signal that a caller meant it.

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

A route that only serves a snapshot, and the route that looks like one but is not
----------------------------------------------------------------------------------
:class:`~bfinance.trendlyne.errors.TrendlyneSnapshotOnlyError` and
:data:`SNAPSHOT_ONLY_ENDPOINTS` were removed as dead public API: the set had to be a subset of
:data:`ENDPOINTS` to be enforceable, nothing fetched a snapshot-only route, so it was an empty
``frozenset()`` and the guard could never fire. A public exception that cannot be raised is the
same category of problem as the 14 dead endpoint templates — it makes the package look bigger than
the code that runs.

``share-holding`` serves **only** the literal path segment ``latest``. Verified 2026-09-27 on
MOTHERSON and re-verified 2026-10-10 on TCS: ``.../Jun-2024/...``, ``.../2024/...`` and
``/equity/share-holding/1372/TCS/Jun-2026/…`` all return **HTTP 500**, not 404 and not an empty
table. So the share-holding route cannot address a period at all. The single ``latest`` page is
still informative — it embeds a wide multi-period grid (44 distinct period labels on MOTHERSON,
from Dec 2015 to Jun 2026) — but it is one request, not a series you can walk.

``monthly-mutual-fund-share-holding`` is **not** snapshot-only, contrary to what the same test
suggests for share-holding. Verified on MOTHERSON: ``latest`` -> 200 (355 rows, top period
Aug-2026), ``2024`` -> 200 (313 rows, top period Sep-2024), ``Jun-2024`` -> 200 (260 rows, top
period Jun-2024), ``2023`` -> 200 (179 rows). Re-verified on TCS 2026-10-10 for the two forms the
share-holding route rejects: ``Jun-2026`` -> 200 (1,577,098 B) and ``2026`` -> 200 (1,402,262 B).
The segment genuinely filters.

**So the guard is on one route and not the other**, and the difference is the measurement rather
than a preference. :meth:`TrendlyneClient.share_holding_async` and
:meth:`TrendlyneClient.share_holding_holders_async` refuse a non-``latest`` period *before* the
request, with a :class:`~bfinance.trendlyne.errors.TrendlyneServerError` naming the snapshot rule —
a deterministic 500 is not worth two requests and a retry storm to rediscover.
:meth:`TrendlyneClient.monthly_mf_holdings_async` validates the period with :func:`validate_period`
and forwards it into the template, because on that route a period is a filter that works. A blanket
snapshot guard would have broken the one route where the segment means something.
:data:`SNAPSHOT_PERIOD` survives as the only period segment the share-holding route accepts.

``eodData`` is newest-first, and its start is not the instrument's listing date
-------------------------------------------------------------------------------
**The start of the series is a vendor floor that this package has never verified against an exchange
listing date.** This section has been wrong twice and both versions shipped. The first claim was a
uniform vendor window, generalised from a large-cap measurement to every symbol. The correction,
which is the version that shipped until this commit, was the opposite one: that the floor IS the
listing date, because a 2023 IPO cannot have twenty years of bars. Both are false, and the second
is the more dangerous one, because it tells a caller the data is clean when it is not.

Measured live 2026-09-29, four symbols, the full frame including the last-week merge:

========================  ===========  ==============  ======  =================================
symbol                    first bar   NSE listing     bars   bars dated before that listing
========================  ===========  ==============  ======  =================================
RELIANCE (id 1127)        2006-01-02   long before     5114   0
SBIN (id 1193)            2006-01-02   2015-01-15      5114   **2222** (45,561,075,413 shares)
PAYTM (id 712312)         2021-11-18   2021-11-18      1206   0
IREDA (id 1791996)        2023-11-29   2023-10-13      703    0
========================  ===========  ==============  ======  =================================

What survives of the old claim is only that the bar count is symbol-dependent: a recent listing
reaches far fewer bars than a mature large cap. For three of the four symbols above the first bar is
at or after the listing date, which is what made the listing-date claim look right. **SBIN is the
counter-example that falsifies the rule.** 2,222 of its 5,114 bars, carrying 45,561,075,413 shares
of volume, are dated before its 2015-01-15 NSE listing. Its own payload carries a ``BSEcode`` of
500112 next to the NSE symbol, so those bars are most likely the BSE history of the same company
filed under the NSE ticker -- plausible, not verified, and not this package's call to make. A frame
whose ``exchange`` attr reads ``"NSE"`` while holding pre-NSE-listing bars is exactly why the
decision belongs to the caller.

**There is no listing-date source here to gate on.** The OHLC payload's ``stockData`` holds the full
name, the NSE code, the BSE code, the ISIN and three scores, and no listing date; NSE's
``/public-past-issues`` answers an empty list for historical windows. So this package neither clips
nor pretends to. :func:`parse_ohlc` takes an optional ``listing_date`` and publishes
``attrs["bfinance_trendlyne_rows_before_listing"]`` plus the volume those rows carry, so the caller
can see the number and decide for itself. The key reads ``-1`` when no date was supplied, which
means "not measured" and must never be read as "none".

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
one. The default interval is **1000 ms**, and that number is a **compliance floor** rather than a
tuning choice: trendlyne's ``robots.txt`` states ``Crawl-delay: 1`` in its ``User-agent: *`` group,
read live on 2026-09-30 alongside 28 ``Disallow`` rules in the same group. The 750 ms this package
used to ship was 1.33 requests per second, a 250 ms per-request violation of a published floor. The
``Crawl-delay: 10`` that also appears in that file is scoped to ``GPTBot``/``ClaudeBot`` and does
not apply to this client. Retry covers 429, 5xx and transport failures only, with exponential
backoff that honours ``Retry-After`` (capped at 30 s); every 4xx is terminal.

The WAF, and why 405 is not just another 4xx
--------------------------------------------
trendlyne is fronted by AWS WAF on CloudFront, and a challenged session is answered with **HTTP
405** and an ``x-amzn-waf-action: captcha`` header rather than with 429. Both forms are terminal
here: :class:`~bfinance.trendlyne.errors.TrendlyneWafChallengeError` is raised before the retry
loop, because the budget is spent rather than momentarily exceeded — a retry would not be refused
for a moment, it would be refused for the rest of the session.

The ~130-150 requests per session that the message quotes is a **third-party measurement** of
anonymous access, recorded on 2026-10-10 alongside 22 clean requests in the same session, and it
is cited as that — an outside figure — rather than as a promise bfinance makes. ``robots.txt``
publishes a crawl-delay and 28 disallow rules and says nothing about a per-session count, so the
budget this package can actually vouch for is the one it enforces: one request per second, serially,
through :meth:`TrendlyneClient._throttle_pacing`'s global lock. That is also why a section call
costs exactly one request — with fourteen new routes, the cheapest thing this package can do for a
caller who wants several of them is to make each one a single fetch.

Caching is opt-in on disk. The sitemap index and the tokens live in memory with a TTL. Passing
``cache=SQLiteCache(...)`` additionally persists the index; the default is ``None``, so importing
this package never creates a file in the user's home directory.
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import quote_plus, unquote

import httpx
import numpy as np
import pandas as pd
from bs4 import BeautifulSoup

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
    TrendlyneTableNotFoundError,
    TrendlyneTransportError,
    TrendlyneWafChallengeError,
)
from .sectors import (
    TrendlyneGroup,
    constituent_attrs,
    parse_classification,
    parse_constituents,
    parse_industry_aggregate,
    parse_taxonomy,
)
from .tables import (
    PROVENANCE,
    buy_sell_zone_tables,
    extract_ohlc_token,
    find_table,
    find_tables,
    monthly_mf_holding_table,
    page_signatures,
    share_holding_tables,
)


#: Public surface of this module. Without it, ``from bfinance.trendlyne.client import *`` binds
#: the module's own imports as well as its API -- ``asyncio``, ``httpx``, ``logging``,
#: ``dataclasses``, ``datetime``, ``html`` and ``re`` all land in the caller's namespace. The
#: package __init__ curates a subset of these for ``bfinance.trendlyne`` itself; this list
#: describes the module. ``logger`` is deliberately absent: it is a getLogger() assignment, not
#: an API anyone should be reaching for.
__all__ = [
    "BASE_URL",
    "COLUMNS",
    "DEFAULT_MIN_REQUEST_INTERVAL_MS",
    "DEMERGER_APPLIED",
    "DEMERGER_APPLIED_ATTR",
    "ENDPOINTS",
    "FULL_SEARCH_FLAG",
    "GROUP_ENDPOINTS",
    "INSIDER_ROW_CAP",
    "LISTING_DATE_ATTR",
    "MEMBER_SEARCH_FLAG",
    "METRICS",
    "NOTE_ATTR",
    "PRICE_BASIS",
    "PRICE_BASIS_ATTR",
    "RETRIEVED_AT_ATTR",
    "ROBOTS_DISALLOWED_ROUTES",
    "ROWS_BEFORE_LISTING_ATTR",
    "SITEMAP_PATH",
    "SNAPSHOT_ONLY_ATTR",
    "SNAPSHOT_PERIOD",
    "SOURCE_ATTR",
    "STOCK_PAGE_SECTIONS",
    "SymbolCategory",
    "TrendlyneClient",
    "TrendlyneSymbol",
    "USER_AGENTS",
    "VOLUME_BEFORE_LISTING_ATTR",
    "WINDOW_ATTR",
    "build_attrs",
    "build_path",
    "ohlc",
    "ohlc_async",
    "parse_company_overview",
    "parse_corporate_actions",
    "parse_envelope",
    "parse_event_tables",
    "parse_latest_news",
    "parse_ohlc",
    "parse_returns_comparison",
    "parse_sitemap",
    "parse_stock_alerts",
    "parse_stock_page_checklists",
    "parse_stock_page_consensus",
    "parse_stock_page_metrics",
    "parse_stock_page_performance",
    "parse_stock_page_recommendations",
    "parse_stock_page_valuation",
    "parse_swot_tables",
    "parse_symbol_category",
    "parse_technical_signals",
    "redact",
    "resolve",
    "symbol_index",
    "validate_period",
]
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
def _normalise_symbol_query(value: str) -> str:
    """
    Reduce a typed symbol to the form a match can be judged on: upper case, alphanumerics only.

    A user types ``nifty bees`` and ``NIFTYBEES``; both must reduce to the same string, or the
    one legitimate reading of that query is indistinguishable from a typo. Separators go
    because a space, a dot and a hyphen are all things people type inside a name and none of
    them is part of a ticker.
    """
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


_SYMBOL_SEARCH_PATH = "/member/api/ac_snames/?term={query}"

#: The same autocomplete, told to return everything. 22 fields instead of 4 -- it carries ``label``,
#: ``category``, ``pageType``, ``defaultExchange`` and a live price, which is why it can say what an
#: instrument *is* rather than only what it is called. Same robots rule, same namespace, and therefore
#: a second opt-in (:data:`FULL_SEARCH_FLAG`) on top of :data:`MEMBER_SEARCH_FLAG`. Discovery only:
#: it is deliberately not a fallback for :meth:`TrendlyneClient.resolve_any_async`, because it is
#: fuzzy and ranked, which is the defect commit ``06f6c7c`` fixed. See the module docstring.
_SYMBOL_SEARCH_FULL_PATH = "/member/api/ac_snames/all/?term={query}&all-results=true"

#: Paths this package will not request unless a caller opts in, mapped to the robots.txt rule that
#: forbids them. **Read from the live ``robots.txt`` on 2026-09-27**, whose ``User-agent: *`` block
#: carries 28 ``Disallow`` rules and a ``Crawl-delay: 1``. (The 10 s figure that circulates for
#: this site is scoped to ``GPTBot``/``ClaudeBot``, not to everybody.)
#:
#: Two entries, both in the one namespace this package used to violate in shipped code:
#:
#: ===============================  =============================================================
#: path                            covering rule
#: ===============================  =============================================================
#: ``/member/api/ac_snames/``       ``Disallow: */member/``
#: ``/member/api/ac_snames/all/``   ``Disallow: */member/``
#: ===============================  =============================================================
#:
#: The rule is the **broad** ``*/member/``. robots.txt has a second, narrower member rule
#: (``*/member/api/ac_snames/stock/``) which this package's path does *not* match — the specific
#: one covers the ``/stock/`` sibling endpoint, not the ``?term=`` search this package called. The
#: broad rule is what forbids it, and it was forbidding it while ``search_remote`` was a default
#: part of :meth:`TrendlyneClient.resolve_any`. That is now opt-in and off.
#:
#: The ``/all/`` sub-path is covered by the same rule and is listed separately only so the table
#: stays complete: a path is absent from this dict only when this package cannot build it. A
#: robots matcher was run against the rule to check that, rather than assumed: ``*`` is a leading
#: wildcard with no trailing ``$``, so ``*/member/`` matches any path containing ``/member/``, and
#: ``/member/api/ac_snames/all/?term=`` contains it.
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
    "/member/api/ac_snames/all/": "Disallow: */member/",
}

#: The opt-in that permits a robots-disallowed route. ``False`` — off — is the default, and the
#: only way to turn it on is to name it in the constructor or in the call. The name says which
#: conflict is being overridden, because "allow_disallowed" would not.
MEMBER_SEARCH_FLAG = "allow_member_search"

#: The **second** opt-in, required by :meth:`TrendlyneClient.search_full` on top of
#: :data:`MEMBER_SEARCH_FLAG`. ``False``, off, is the default, and it is checked *second*, so a
#: caller who opens :data:`MEMBER_SEARCH_FLAG` and forgets this one still cannot reach the route.
#:
#: Why two switches rather than one. :data:`MEMBER_SEARCH_FLAG` is the standing decision that this
#: package may ask a robots-disallowed path at all; the rich route is a *second* path into the same
#: forbidden namespace, returns 20 ranked fuzzy candidates where the plain one resolves a ticker
#: cleanly, and is the kind of call a caller reaches for by habit rather than by need. Each opt-in
#: costs a second, explicit word at the call site, and the refusal names whichever of the two is
#: actually missing. What this does **not** do is make the route compliant: both flags open a
#: robots-disallowed path, and the default posture stays that neither is open.
FULL_SEARCH_FLAG = "allow_full_search"

#: Every route this package can address **today**, as ``str.format`` templates over
#: ``stock_id``, ``symbol``, ``slug``, ``group_id``, ``group_slug``, ``period``, ``metric`` and
#: ``token``. Use :func:`build_path` instead of formatting these directly.
#:
#: This held 17 templates once and 14 of them were dead strings: no method built them, so
#: ``build_path`` would mint a plausible URL for a route nobody had requested and five of them
#: (``rights`` and the four ``latest-news`` routes) had never been seen to answer 200 at all. The 14
#: were deleted; when a route *is* wired, its name and its fetch method land in the same commit and
#: a test enforces that pairing (``tests/test_trendlyne_core.py::TestNoDeadScaffolding``).
#:
#: The section block below is the 2026-10-10 rewiring of those 14. **Each key carries its own
#: robots verdict, because "all clean" is not a thing a reader can check.** The file was read live
#: that day: ``User-agent: *``, ``Crawl-delay: 1``, 28 ``Disallow`` rules. Of the equity rules,
#: ``*/equity/technical-analysis-*`` is the only one, and it needs that literal substring -- so
#: ``/equity/api/stock/adv-technical-analysis/`` is allowed (there is ``/api/stock/`` in the middle)
#: while the ``technical-analysis`` page it shadows is not. ``/alerts/stock-alerts/`` is allowed;
#: only ``*/alerts/addStockAlert/`` and ``*/alerts/modal/stockAlert/`` are disallowed, and neither is
#: a route here. ``*/member/`` -- the one this package gates behind a flag -- matches none of them.
#: Measurements and the raw captures are in ``.scratch/trendlyne-e2e-recon.md``.
#:
#: **Note the segment order.** It is not uniform and cannot be made uniform: the stock page puts the
#: id first, while six of these routes put the symbol first. One ``.format`` template per shape is
#: the only way a wrong route is refused by :func:`build_path` rather than by a 404.
ENDPOINTS: Dict[str, str] = {
    "stock_page": "/equity/{stock_id}/{symbol}/{slug}/",
    "ohlc": "/mapp/v1/stock/web/ohlc/{stock_id}/{token}/",
    "chart_metric": "/mapp/v1/stock/chart-data/{stock_id}/{metric}/",
    # Group pages. Note the shape: the id comes FIRST and there is no symbol segment, which is the
    # opposite of stock_page above. One template with an optional branch would match a group URL
    # as a stock without complaining, so the two shapes get two entries.
    "sector_page": "/equity/sector/{group_id}/{group_slug}/",
    "industry_page": "/equity/industry/{group_id}/{group_slug}/",
    # ---------------------------------------------------------- section pages (robots-clean
    # 2026-10-10: all eleven match none of the 28 `User-agent: *` Disallow rules).
    "corporate_actions_page": "/equity/corporate-actions/{symbol}/{stock_id}/{slug}/",
    "bulk_block_deals_page": "/equity/bulk-block-deals/{symbol}/{stock_id}/{slug}/",
    # Snapshot-only: answers HTTP 500 for every period segment but `latest`. Re-verified 2026-10-10
    # on TCS. The guard is in share_holding_async, not here -- see the module docstring.
    "share_holding_page": "/equity/share-holding/{stock_id}/{symbol}/{period}/{slug}/",
    "board_meeting_page": "/equity/board-meeting/{symbol}/{stock_id}/{slug}-board_meeting/",
    "research_reports_page": "/research-reports/stock/{stock_id}/{symbol}/{slug}/",
    "latest_news_page": "/latest-news/{stock_id}/{symbol}/{slug}/",
    # Allowed: only `*/alerts/addStockAlert/` and `*/alerts/modal/stockAlert/` are disallowed, and
    # those are the two write routes a logged-in member uses. Reading the list needs no member.
    "stock_alerts_page": "/alerts/stock-alerts/{symbol}/{stock_id}/{slug}/",
    "insider_trading_page": "/equity/insider-trading-sast/all/{symbol}/{stock_id}/{slug}/",
    # NOT snapshot-only -- the measured counter-example to the route above. `Jun-2026` and `2026`
    # both answer 200 here (verified 2026-10-10, TCS) and filter the grid.
    "monthly_mf_holding_page": (
        "/equity/monthly-mutual-fund-share-holding/{stock_id}/{symbol}/{period}/{slug}/"
    ),
    "buy_sell_zone_page": "/tools/buy-sell-zone/{stock_id}/{symbol}/{slug}/",
    "swot_page": "/equity/swot-buy-or-sell/{stock_id}/{symbol}/{slug}/",
    # ------------------------------------------------------------- JSON siblings (robots-clean
    # 2026-10-10; all three carry `?format=json` in the template so the query cannot be forgotten).
    # The first is the sibling of the robots-DISALLOWED `/equity/technical-analysis-*` page: the rule
    # needs that literal substring and this path has `/equity/api/stock/` in front of it.
    "adv_technical_analysis": (
        "/equity/api/stock/adv-technical-analysis/{stock_id}/24/?format=json"
    ),
    "overview_second_part": "/equity/overview-second-part/{stock_id}/?format=json",
    "price_performance_analysis": (
        "/share-price/price-performance-analysis/{stock_id}/?format=json"
    ),
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

#: The insider-trading route's **server-side row cap**: 500. Recorded in the repo scraping study as
#: exactly 500 rows on another symbol, and re-measured on TCS on 2026-10-10 at 175 rows — which is
#: the point of keeping the number rather than the *conclusion*. A frame that comes back holding
#: exactly this many rows may be truncated, and a truncated history is indistinguishable from a
#: complete one unless it is said out loud; 175 rows may equally be the whole of what exists.
#:
#: So the guard is not "this is all of it" and not "this is partial" — it is the row count in
#: ``attrs["bfinance_trendlyne_insider_rows"]`` plus, when the count equals this cap, a note saying
#: the cap was hit and that the frame is therefore **not** the full history. Never the reverse: a
#: frame below the cap is never described as complete, because nothing measured says it is.
INSIDER_ROW_CAP = 500

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

#: The listing date the CALLER supplied to :func:`parse_ohlc`, echoed back on the frame.
#: ``None`` when none was supplied. This package has no listing-date source of its own: the OHLC
#: ``stockData`` carries name, NSE code, BSE code, stock_id, instrument type, ISIN and three scores,
    # and no date of any kind. Observed verbatim on SBIN:
    #   [0] 'State Bank of India' [1] 'SBIN' [2] '500112' [3] 1193 [4] 'Equity'
    #   [5] 'INE062A01020' [6] 40 [7] 48 [8] 34 The value is
#: therefore only ever what a caller asserted, never something this module looked up.
LISTING_DATE_ATTR = "bfinance_trendlyne_listing_date"
#: How many bars are dated before :data:`LISTING_DATE_ATTR`. This is the number the module used to
#: *assert away*: the note and the docstring both claimed the history was clipped at the listing
#: date, and SBIN (2,222 of 5,114 bars, 45,561,075,413 shares of volume, before its 2015-01-15 NSE
#: listing, measured 2026-09-29) shows the claim was false. ``-1`` means *not measured* -- no
#: listing date was supplied -- and is deliberately distinct from ``0``, which means *measured,
#: none found*. Same convention as ``bfinance_trendlyne_last_week_rows_added``.
ROWS_BEFORE_LISTING_ATTR = "bfinance_trendlyne_rows_before_listing"
#: Total volume of those same rows, so "how much of this is pre-listing" is one dict lookup rather
#: than a second pass. ``-1`` when not measured, on the same terms as the row count.
VOLUME_BEFORE_LISTING_ATTR = "bfinance_trendlyne_volume_before_listing"
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

#: The pacing floor in milliseconds, and a **compliance** number rather than a tuning knob.
#: trendlyne's ``robots.txt``, fetched live on 2026-09-30, carries ``Crawl-delay: 1`` in its
#: ``User-agent: *`` group (the group this client falls under) and comments that line "Wait 1
#: second between successive requests". One second is 1000 ms, which is one request per second, so
#: that is the value. It was 750 ms: 1.33 requests per second, 250 ms per request under a
#: published floor, bought for no reason and in contradiction of the measurement this module had
#: already recorded. The 28 ``Disallow`` rules in that same ``*`` group are what
#: :data:`ROBOTS_DISALLOWED_ROUTES` encodes; the ``Crawl-delay`` is what this constant is.
#:
#: The 10 s figure that circulates for this site is **not** the number to use: it sits in the
#: ``GPTBot``/``ClaudeBot`` group, which is scoped to those named crawlers rather than to ``*``,
#: and adopting it would make bulk scraping ~10x slower for no compliance reason. The old
#: justification for 750 ms was page weight (a 292 KB page per symbol, against 100-150 ms for
#: screener.in and MarketLens), which is a real observation but is not a robots-derived argument:
#: a heavy page does not lower a stated crawl-delay, so it is not the reason for this value.
#:
#: A caller who wants a different posture -- tests, a mirror, a private quota -- passes
#: ``min_request_interval_ms=``, and ``0.0`` disables pacing outright. Anything *below* this
#: default is a decision to outrun the site's stated crawl-delay, and
#: ``tests/test_trendlyne_pacing.py`` fails if this number is quietly tidied.
DEFAULT_MIN_REQUEST_INTERVAL_MS = 1000.0

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

#: The header AWS WAF attaches when it challenges a request. Checked on **every** response, whatever
#: the status: the challenge and the code are separate, and a 200 carrying this header is a
#: captcha page rather than data.
_WAF_ACTION_HEADER = "x-amzn-waf-action"

_SITEMAP_URL_RE = re.compile(r"/equity/(\d+)/([^/]+)/([^/?#]+)/?")
# The month form is ``Mon-YYYY`` with the capitalisation trendlyne actually serves. Case matters
# in a path segment, and an unrecognised segment on the share-holding route costs a 500, so this
# is matched strictly rather than case-insensitively.
_PERIOD_RE = re.compile(r"^(?:latest|\d{4}|[A-Z][a-z]{2}-\d{4})$")
_TOKEN_IN_URL_RE = re.compile(r"(/ohlc/\d+/)[A-Za-z0-9=]+/")
#: A **trailing** parenthesised group at the end of a vendor ``label``, and only that. ``[^()]*``
#: cannot cross a parenthesis, so the match is the last balanced group on the line and never a
#: nested or internal one -- which is what keeps a company name's own parentheses intact.
_LABEL_TICKER_RE = re.compile(r"\(([^()]*)\)\s*$")


# ------------------------------------------------------- what a search hit actually is


class SymbolCategory(str, Enum):
    """What a search hit **is**, and whether this package can fetch a price history for it.

    ``str``-valued on purpose: a category then serialises as its own value (``"etf"``, not
    ``"SymbolCategory.ETF"``), so it drops into a frame's ``attrs`` or a JSON body without a
    conversion step. :meth:`has_ohlc` is the reason the enumeration exists at all.

    The vocabulary is **closed and measured**: :data:`_VENDOR_CATEGORIES` holds the six strings
    trendlyne actually returned, counted across 136 distinct ``(category, symbol)`` pairs from 8
    queries on 2026-09-30. Anything else is :attr:`OTHER`, which means *this package does not know
    what this is*, not *it is an equity*. A guess there would be the wrong-instrument defect
    commit ``06f6c7c`` fixed, wearing a category label.
    """

    #: A listed share. ``pageType`` 2 on every measured hit. OHLC is available.
    EQUITY = "equity"
    #: An exchange-traded fund. ``pageType`` 10. OHLC is available, and ETFs are absent from the
    #: equity sitemap entirely, so this is the only route to them.
    ETF = "etf"
    #: An index series. ``pageType`` 24. OHLC is available.
    INDEX = "index"
    #: A mutual-fund scheme. ``pageType`` 12. **No OHLC route exists**: mutual funds live in a
    #: separate namespace at ``/mutual-fund/nav/{id}/`` and carry NAV, not a price history.
    MUTUAL_FUND = "mutual_fund"
    #: Delisted or suspended. ``pageType`` 2, same as equity, which is why the *category* and not
    #: the page type is the thing worth reading. **A price history is not the answer here.**
    NOT_TRADED = "not_traded"
    #: A category this package has not measured. See :func:`parse_symbol_category`.
    OTHER = "other"

    def has_ohlc(self) -> bool:
        """Whether this package can fetch a price history for an instrument in this category.

        ``True`` for :attr:`EQUITY`, :attr:`ETF` and :attr:`INDEX`: all three were measured
        serving OHLC on this package's own route, with a per-symbol token.

        ``False`` for :attr:`MUTUAL_FUND` and :attr:`NOT_TRADED`, and that is the assertion that
        matters. A mutual fund has no OHLC route to fetch and a delisted line has no price, so a
        caller that resolves to either and returns bars has returned a **wrong** answer rather than
        an empty one. That is the reason the category is carried on the result at all.

        ``False`` for :attr:`OTHER` too, and it is ``False`` rather than a third answer because this
        method answers "can you fetch it", not "what is it": an unmeasured category has not earned
        a ``True``. Read :attr:`OTHER` as unknown, and go and look: it is not evidence of an
        equity.
        """
        return self in (SymbolCategory.EQUITY, SymbolCategory.ETF, SymbolCategory.INDEX)


#: The vendor's own strings, verbatim, and nothing else. A key absent here maps to
#: :attr:`SymbolCategory.OTHER`; it is never coerced to a neighbouring category, because a wrong
#: category is worse than a missing one -- a caller reading ``OTHER`` knows to ask, while a caller
#: reading ``EQUITY`` believes the vendor said so. ``"Superstars"`` is deliberately absent: it
#: appeared on 5 measured pairs with no ``pageType``, and it describes a vendor-side grouping
#: rather than an instrument class, so it is unmeasured here.
_VENDOR_CATEGORIES: Dict[str, SymbolCategory] = {
    "Equity": SymbolCategory.EQUITY,
    "ETF": SymbolCategory.ETF,
    "Index": SymbolCategory.INDEX,
    "Mutual Funds": SymbolCategory.MUTUAL_FUND,
    "Not Traded": SymbolCategory.NOT_TRADED,
}


def parse_symbol_category(value: Any) -> SymbolCategory:
    """Map a vendor ``category`` string onto :class:`SymbolCategory`.

    Args:
        value: The ``category`` field off a search record. ``None``, a missing key and any
            unrecognised string all return :attr:`SymbolCategory.OTHER`.

    Returns:
        The matching category, or :attr:`SymbolCategory.OTHER`. Never raises and never guesses:
        the six measured strings are the whole vocabulary, so an eighth string is a vendor change
        this package has not measured, and reporting it as unknown is the only honest answer. It
        is a reported outcome, not a silent drop -- the caller can read
        :attr:`SymbolCategory.OTHER` and decide.

    Note:
        Case-sensitive on purpose. ``"Mutual fund"`` and ``"mutual funds"`` are not in
        :data:`_VENDOR_CATEGORIES`, so they return :attr:`SymbolCategory.OTHER` -- which is
        visible rather than wrong, and is the direction a schema drift should fail in. A
        case-insensitive lookup would be a guess about a vocabulary nobody has measured.
    """
    return _VENDOR_CATEGORIES.get(str(value or "").strip(), SymbolCategory.OTHER)


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
        name: The vendor's display name with the trailing ticker removed: ``Nippon India ETF
            Nifty 50 BeES`` for a record labelled ``"Nippon India ETF Nifty 50 BeES(NIFTYBEES)"``.
            ``""`` when the route that built this record carries no name, which is every sitemap
            record; see :func:`_display_name` for the one rule that keeps a name containing its own
            parentheses intact.
        category: What the instrument is, from :func:`parse_symbol_category`. Defaults to
            :attr:`SymbolCategory.OTHER` because a sitemap entry carries no category field; an
            unstated category is not an equity, and a default of ``EQUITY`` would be a claim about
            5,922 records that was never measured.
        vendor_category: The vendor's raw ``category`` string, kept verbatim beside the mapped
            :attr:`category`. It exists so :attr:`SymbolCategory.OTHER` is auditable: the caller can
            see what trendlyne actually said instead of only this package's verdict on it.
    """

    symbol: str
    stock_id: int
    slug: str
    # Appended after the original three, all defaulted: this dataclass is constructed positionally
    # as TrendlyneSymbol(symbol, stock_id, slug) in parse_sitemap, in search_remote_async and in the
    # tests. Adding a required field would break every one of those call sites, and the three above
    # are the identity of the record while the three below are what one particular route knows
    # extra about it.
    name: str = ""
    category: SymbolCategory = SymbolCategory.OTHER
    vendor_category: str = ""

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


def _display_name(label: str, symbol: str, slug: str = "") -> str:
    """The vendor's display name, with the trailing ticker removed **only when it is the ticker**.

    A search record carries ``"Nippon India ETF Nifty 50 BeES(NIFTYBEES)"``: the display name with
    the ticker appended in parentheses, which is how the site renders it in its own dropdown. The
    ticker is the part already in :attr:`TrendlyneSymbol.symbol`, so what is wanted is the name.

    A blind ``label.rsplit("(", 1)[0]`` is the obvious implementation and it is wrong, because a
    company name may legitimately contain its own parentheses. Real ones do: ``"Tata Elxsi
    (India) Ltd"`` is a name, not a wrapper. Two rules make it safe, and both are needed:

    1. **Trailing only.** :data:`_LABEL_TICKER_RE` is anchored to the end of the string, so a
       parenthetical that is part of the name is never a candidate.
    2. **The group must be the ticker.** The captured text is compared to the symbol through
       :func:`_normalise_symbol_query` before anything is removed, so a label whose last group is
       something else is left whole. Nothing is stripped on a guess.

    Args:
        label: The vendor's ``label``, which may be absent or empty.
        symbol: The record's symbol, used to decide whether the trailing group is the ticker.
        slug: The record's slug, used when there is no usable label. **Not** title-cased or
            otherwise prettified: a fallback is reported as the vendor wrote it, because a
            fabricated name reads exactly like a measured one.

    Returns:
        The display name, or the slug, or the symbol -- whichever is the best thing actually on the
        record. Never raises and never returns ``""`` for a record that has any of the three, so a
        missing ``label`` degrades the field rather than the call.
    """
    text = (label or "").strip()
    match = _LABEL_TICKER_RE.search(text)
    if match and _normalise_symbol_query(match.group(1)) == _normalise_symbol_query(symbol):
        # The group is the ticker, so it is the one that was added for the dropdown. Remove it and
        # the name it was appended to is what remains.
        text = text[: match.start()].strip()
    if text:
        return text
    return (slug or symbol or "").strip()


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
        record: The resolved symbol. Required by every route **except** the group routes in
            :data:`GROUP_ENDPOINTS`, which have no symbol segment — they are the only ones that can
            be built without one, and asking for those with a record is an error in the other
            direction.
        period: Period segment, for the routes that take one. See :func:`validate_period`. Only
            ``share_holding_page`` (which accepts ``latest`` alone, and is guarded by its fetch
            method rather than here) and ``monthly_mf_holding_page`` carry it in the URL; the rest
            ignore it, and :func:`validate_period` still runs so a malformed value is rejected
            consistently rather than depending on which route was asked for.
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
            ``group`` whose kind does not match the route, a non-group route given a ``group``, or a
            stock route with no ``record``.
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
    if name not in GROUP_ENDPOINTS and record is None:
        # Every remaining template formats at least one of stock_id/symbol/slug, so a missing
        # record is a caller mistake -- and saying so beats a KeyError out of str.format, which
        # would read as a bug in the template rather than in the call.
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


def _listing_cutoff(listing_date: Any) -> Optional[pd.Timestamp]:
    """Normalise a caller-supplied listing date to a midnight, tz-naive ``Timestamp``.

    The date component is taken as given, exactly as :func:`_rows_to_frame` takes it from the wire:
    a listing is a calendar day, so an offset carried on the argument is dropped rather than shifted
    the date across a boundary. ``None`` in, ``None`` out, because "no listing date was supplied" is
    a first-class answer here and not an error -- it is what makes the attrs read ``-1``.

    Raises:
        ValueError: The argument is not a date at all. Silently counting against a ``NaT`` would
            report ``0`` rows before a listing date the caller believes in, which is the exact false
            all-clear this measurement exists to remove.
    """
    if listing_date is None:
        return None
    try:
        stamp = pd.Timestamp(listing_date)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"listing_date={listing_date!r} is not a date. Pass an ISO string ('2015-01-15'), a "
            f"datetime.date or a pandas Timestamp, or None to skip the check."
        ) from exc
    if pd.isna(stamp):
        raise ValueError(
            f"listing_date={listing_date!r} is NaT, which is not a listing date. Pass a real date, "
            f"or None to skip the check."
        )
    if stamp.tzinfo is not None:
        stamp = stamp.tz_localize(None)
    return stamp.normalize()


def parse_ohlc(
    payload: Any,
    symbol: str,
    *,
    source_url: Optional[str] = None,
    include_last_week: bool = True,
    retrieved_at: Optional[datetime] = None,
    stock_id: Optional[int] = None,
    listing_date: Any = None,
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
        listing_date: The security's listing date, as ``'YYYY-MM-DD'``, a ``date`` or a
            ``Timestamp``. **The caller supplies it; this module does not know it.** Supplying it
            publishes :data:`ROWS_BEFORE_LISTING_ATTR` (and :data:`VOLUME_BEFORE_LISTING_ATTR`) on
            the frame; omitting it leaves both at ``-1``, meaning *not measured*.

    Returns:
        Columns :data:`COLUMNS`, ascending ``Date`` index, with ``attrs`` recording provenance,
        the price basis, the vendor window, the merge, the invariant check and every missing value.

    Note:
        ``listing_date`` exists because a stronger claim used to live here. The module asserted
        that the history was clipped at the instrument's listing date, and there is no such
        clipping in the code -- there was never a listing-date lookup to clip against. SBIN
        returned 2,222 of 5,114 bars, carrying 45,561,075,413 shares of volume, dated before its
        2015-01-15 NSE listing (measured live 2026-09-29). The package cannot decide for the
        caller which of those bars are wanted, so it counts them and says so. **The rows are never
        dropped**: on a split- or bonus-adjusted series the pre-listing bars are on the same
        adjusted scale as the rest, so clipping them would leave a discontinuity rather than
        remove one.

    Raises:
        TrendlyneNotAuthorizedError: The envelope reports a failed request (rejected token).
        TrendlyneSchemaError: Missing ``eodHeaders``/``eodData``, a renamed column, a row of the
            wrong width, an unparseable date, or an all-missing close.
        ValueError: ``listing_date`` is neither a date nor ``None``.
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

    # The listing date is the CALLER's claim about the security, never a lookup this module
    # made -- there is no listing-date source on this route, and the payload's stockData does
    # not carry one. Without it the count stays -1 ("not measured"), which is deliberately not
    # 0: a frame nobody has checked must not read as a frame that was checked and came back
    # clean. That distinction is the whole point -- this count is the replacement for a claim
    # about the data that the data did not support.
    cutoff = _listing_cutoff(listing_date)
    if cutoff is None:
        listing_iso = None
        rows_before = -1
        volume_before = -1
    else:
        pre = frame.index < cutoff
        rows_before = int(pre.sum())
        volume_before = frame.loc[pre, "Volume"].sum()
        if pd.api.types.is_integer_dtype(frame["Volume"]):
            volume_before = int(volume_before)
        listing_iso = cutoff.date().isoformat()
        if rows_before:
            logger.warning(
                "trendlyne eodData for %s carries %d bar(s) dated before the caller-supplied "
                "listing date %s (%s shares of volume). The rows are kept and the count is in "
                "attrs; this package has no listing-date source of its own to gate on.",
                symbol, rows_before, listing_iso, f"{volume_before:,}",
            )

    note = (
        f"Daily EOD series from trendlyne.com, spanning {first_date}..{last_date} "
        f"({len(frame)} bars). The bar count is symbol-dependent, but the START of the series is a "
        f"vendor floor this package has NOT verified against any exchange listing date, and it is "
        f"NOT the listing date: measured live 2026-09-29, SBIN returned 2,222 bars and "
        f"45,561,075,413 shares of volume dated before its 2015-01-15 NSE listing, on a frame "
        f"whose exchange reads NSE. Do not treat index[0] as the listing, and do not read a short "
        f"bar count as a young listing. Pass listing_date= to have "
        f"attrs['{ROWS_BEFORE_LISTING_ATTR}'] measured for your instrument; it reads -1 when it "
        f"has not been measured. Basis: "
        f"AS-TRADED, back-adjusted for splits and bonuses only; DEMERGERS ARE NOT APPLIED, so "
        f"these bars equal the NSE as-traded print wherever no split or bonus falls inside the "
        f"joined span. Do not splice them with bfinance.nse.bhavcopy bars across a split or bonus "
        f"date (the older bars are scaled there and the join carries a corporate-action-sized "
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
            listing_date=listing_iso,
            rows_before_listing=rows_before,
            volume_before_listing=volume_before,
        )
    )
    frame.attrs["bfinance_synthetic_ohlc"] = False
    frame.attrs[PRICE_BASIS_ATTR] = PRICE_BASIS
    frame.attrs[DEMERGER_APPLIED_ATTR] = DEMERGER_APPLIED
    return frame[list(COLUMNS)]


# --------------------------------------------------------- section-page parsing
#
# The pages below need two different kinds of parser, and the split is not stylistic.
#
# **Tables** are read by :mod:`bfinance.trendlyne.tables`, which owns the header-signature model and
# the structural extractors, and this module does not re-implement it. ``find_table``,
# ``share_holding_tables``, ``buy_sell_zone_tables`` and ``monthly_mf_holding_table`` are called
# here and nothing is parsed twice.
#
# **Card lists** — news and alerts — are not tables at all, and there is no shared helper to reach
# for. Two of these functions exist because the page has no table to classify.


def _paged(path: str, page: int) -> str:
    """Append ``?page=N`` to a section path, leaving page 1 as the page's own canonical URL.

    The two paginated section routes answer ``?page=N`` — measured 200 on ``/latest-news/…`` on
    2026-10-10 — and page 1 is the bare path the site links to, so a first page is not sent as
    ``?page=1``: a request this package did not need to shape differently should not be.

    Raises:
        ValueError: ``page`` is not a positive integer. Locally, because the alternative is a URL
            that silently asks for page zero or for something that is not a page.
    """
    try:
        number = int(page)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"page must be a positive integer, got {page!r}") from exc
    if number < 1:
        raise ValueError(f"page={page!r} is not a positive page number")
    return path if number == 1 else f"{path}?page={number}"


def _stamped(frame: pd.DataFrame, symbol: str, source_url: Optional[str], note: str,
             retrieved_at: Optional[datetime], *, snapshot_only: bool = False,
             **extra: Any) -> pd.DataFrame:
    """Put the package's standard attrs on a section frame.

    One call site for every section frame, so ``bfinance_provenance`` / ``bfinance_symbol`` /
    ``bfinance_trendlyne_snapshot_only`` cannot be present on one route and missing on the next,
    and so no section frame inherits the OHLC-only keys — see :func:`build_attrs` on why a table
    frame must not carry a price basis.

    ``snapshot_only`` is a keyword-only flag rather than a positional one because it is the one
    argument a caller must think about: every section route is a single page with no period axis,
    so it reads ``True`` there, and the default is the conservative ``False`` for a caller that
    has not said.
    """
    frame.attrs.update(
        build_attrs(
            symbol,
            source=source_url,
            snapshot_only=snapshot_only,
            note=note,
            retrieved_at=retrieved_at,
            **extra,
        )
    )
    return frame


def parse_latest_news(html: str, *, symbol: str = "", source_url: Optional[str] = None,
                      retrieved_at: Optional[datetime] = None) -> pd.DataFrame:
    """The server-rendered news list of ``/latest-news/`` as a frame of items.

    **This route has no ``<table>``.** Verified 2026-10-10: the list is a card column, one
    ``div.panel-post`` per item, and the trimming that made it a 139 KB fixture kept all ten.

    Args:
        html: The page, or a verbatim excerpt of the news column.
        symbol: Recorded in ``attrs`` only; the page is already one symbol's news.
        source_url: The URL fetched, recorded in ``attrs``.
        retrieved_at: When the response arrived.

    Returns:
        Columns ``date``, ``kind``, ``title``, ``url``, ``source``, one row per card in page order.

    Note:
        Two columns are carried verbatim rather than normalised, and it is worth knowing why
        before "fixing" them.

        * ``date`` is the vendor's own rendered text, which is **not** one format. The recorded
          page carries ``10 Oct, 2026 at 09:49 AM`` in a ``title`` attribute, ``09 Oct 2026,
          06:35PM`` in the body, and ``Oct. 9, 2026, 11:09 a.m.`` on the alerts route. Reading one
          of them as *the* timestamp and parsing the others through it would be a guess about
          which element is authoritative; the string is published instead.
        * ``url`` is whatever href the card carries, **including a login wall**. A research-report
          card links to ``/visitor/loginmodal.html?next=…`` for anonymous readers. Rewriting that
          to the page behind the wall would be inventing an address the response did not contain,
          so the wall is what a caller sees.

        ``kind`` is the card's ``<label>`` text (an empty string when the card carries none) and
        ``source`` the ``div.rsssource`` publisher, which trendlyne-authored posts do not have.

    Note:
        An empty result is returned as an **empty frame with these columns**, not as an error and
        not as rows of blanks: a page with no news is a real answer, and one shaped like a parse
        failure is not.
    """
    soup = BeautifulSoup(html or "", "lxml")
    rows: List[Dict[str, Any]] = []
    for panel in soup.select("div.panel-post"):
        body = panel.select_one("div.post-body")
        scope = body if body is not None else panel

        date_node = panel.select_one("div.post-head-subtext span[title]")
        if date_node is None:
            date_node = panel.select_one("div.post-head-subtext span")
        date_text = date_node.get("title") if date_node is not None and date_node.get("title") \
            else (date_node.get_text(" ", strip=True) if date_node is not None else "")

        heading = scope.select_one("h2.card-title") or scope.select_one("div.card-title") \
            or scope.select_one("h6")
        newslink = scope.select_one("a.newslink")
        if heading is not None:
            title = heading.get_text(" ", strip=True)
        elif newslink is not None:
            title = newslink.get_text(" ", strip=True)
        else:
            title = ""

        # The newslink first, because a card's other /posts/ links are chrome: an "Alert" button
        # points at /posts/alert-modal/{id}/, and the stock-insight strip above the body repeats
        # links to unrelated posts. Only a /posts/<digits>/ href is the item's own page.
        url = newslink.get("href", "") if newslink is not None else ""
        if not url:
            own = scope.select_one('a[href*="/posts/"]')
            href = own.get("href", "") if own is not None else ""
            url = href if re.search(r"/posts/\d+/", href or "") else ""

        source_node = panel.select_one("div.rsssource")
        rows.append(
            {
                "date": date_text,
                "kind": "; ".join(
                    label.get_text(" ", strip=True) for label in panel.select("label")
                ),
                "title": title,
                "url": url,
                "source": source_node.get_text(" ", strip=True) if source_node is not None else "",
            }
        )

    frame = pd.DataFrame(rows, columns=["date", "kind", "title", "url", "source"])
    note = (
        f"{len(rows)} server-rendered news item(s) from trendlyne.com's /latest-news/ page, in "
        f"page order. This route publishes no table: the list is a card column. The date is the "
        f"vendor's rendered text and is not normalised, because the page renders three formats for "
        f"the same kind of value. A url that points at /visitor/loginmodal.html is the vendor's "
        f"own paywall for anonymous readers and is reported as sent rather than rewritten. This "
        f"frame is ONE page of a paginated list: ask for page=2 for the next, and note that an "
        f"empty page means the page number is past the end, not that the symbol has no news."
    )
    return _stamped(
        frame, symbol, source_url, note, retrieved_at, rows=len(rows),
        row_selector="div.panel-post",
    )


def parse_stock_alerts(html: str, *, symbol: str = "", source_url: Optional[str] = None,
                       retrieved_at: Optional[datetime] = None) -> pd.DataFrame:
    """The server-rendered alert list of ``/alerts/stock-alerts/`` as a frame.

    **Also no ``<table>``**: the alerts are cards too, one ``div.tr-alert-body`` per alert inside
    ``aside#allposts``. The recorded page carries ten of them — a research-report alert, four SMA
    crossovers and five price/volume threshold alerts.

    Args:
        html: The page, or a verbatim excerpt of the alert column.

    Returns:
        Columns ``date``, ``kind``, ``headline``, ``detail``, ``values``, one row per alert in page
        order (newest first as the page serves them).

    Note:
        ``values`` is a dict, because the alert cards carry a different number of key/value tiles
        each and their labels are the vendor's own ("Day Price", "SMA 30", "Crossover",
        "Target Price"). A fixed column set would either invent columns or drop the tiles that did
        not fit; the dict is what the card carried. If one card ever repeats a label, that key
        holds a list of every value seen under it rather than the last one winning silently.

        ``headline`` is the card's ``h5.card-title``, which on this page renders the **symbol**
        rather than a sentence — the alert's own wording is in ``detail``, the first list-group
        item ("Stock Price (2399.3) crossed above 30 Day SMA (2343.86) on 31 August, 2026").

    Note:
        Only the ``aside#allposts`` card list is parsed. The sidebar's research-report teaser block
        is a **different** alert family with a different shape, and its content is served in full
        by ``/equity/overview-second-part/{id}/?format=json`` and, on the wire, by
        :meth:`TrendlyneClient.research_reports`. Merging the two here would produce rows with a
        different set of columns, which is a frame whose missing values are a parsing decision
        rather than a fact about the vendor.
    """
    soup = BeautifulSoup(html or "", "lxml")
    rows: List[Dict[str, Any]] = []
    for card in soup.select("div.tr-alert-body"):
        date_node = card.select_one("div.gray555.fs09rem")
        title_node = card.select_one("h5.card-title")
        items = card.select("ul.list-group li.list-group-item")

        values: Dict[str, Any] = {}
        for item in items[1:]:
            value_node = item.select_one("h3")
            label_node = item.select_one("p")
            if value_node is None or label_node is None:
                continue
            label = label_node.get_text(" ", strip=True)
            if not label:
                continue
            value = value_node.get_text(" ", strip=True)
            if label in values:
                # Repeated label: keep every value rather than letting the last one win.
                previous = values[label]
                values[label] = list(previous) if isinstance(previous, list) else [previous, value]
            else:
                values[label] = value

        rows.append(
            {
                "date": date_node.get_text(" ", strip=True) if date_node is not None else "",
                "kind": " / ".join(
                    item.get_text(" ", strip=True) for item in card.select("ul.nav-alert-full li")
                ),
                "headline": title_node.get_text(" ", strip=True) if title_node is not None else "",
                "detail": items[0].get_text(" ", strip=True) if items else "",
                "values": values,
            }
        )

    frame = pd.DataFrame(rows, columns=["date", "kind", "headline", "detail", "values"])
    note = (
        f"{len(rows)} server-rendered alert(s) from trendlyne.com's /alerts/stock-alerts/ page, "
        f"newest first as served. This route publishes no table: the alerts are cards. 'headline' "
        f"is the card's own title element, which renders the symbol; the alert's wording is in "
        f"'detail'. 'values' holds the card's labelled tiles (Day Price, SMA 30, Crossover, "
        f"Target Price …) and its labels differ per alert family, which is why it is a dict rather "
        f"than a fixed set of columns. Only the alert list is parsed; the sidebar research-report "
        f"block is a different family with a different shape and is served in full by the "
        f"overview-second-part JSON route. An alert list that comes back empty means the page "
        f"carried no cards, which is a fact about the page and not a parse failure."
    )
    return _stamped(
        frame, symbol, source_url, note, retrieved_at, rows=len(rows),
        row_selector="div.tr-alert-body",
    )


def parse_swot_tables(html: str, *, symbol: str = "", source_url: Optional[str] = None,
                      retrieved_at: Optional[datetime] = None) -> Dict[str, pd.DataFrame]:
    """The SWOT page's fact tables, keyed by the ``div.tab-pane`` each one sits in.

    Five outermost tables on the recorded page, each **single-column** and each with the fact
    itself as its header cell ("Companies with no Debt", "Broker price or reco upgrades in last
    month", …). Measured on TCS 2026-10-10: 19, 6, 3, 3 and 3 fact rows.

    Args:
        html: The page, or a verbatim excerpt of the widget.

    Returns:
        ``{"tag_strengths": frame, "tag_weakness": frame, ...}`` — the ``div.tab-pane`` ids in page
        order.

    Note:
        The key is the pane id, not the heading, and the difference is the whole point: the
        headings are free text that rewrites itself when the vendor rewords a screen ("Stocks -
        DVM Select Long Term (subscription)" is one of them), while ``#tag_strengths`` is the
        widget's own structure. Each frame publishes its heading in
        ``attrs["bfinance_trendlyne_swot_heading"]`` so both are readable.

        A table with no enclosing tab pane is keyed ``table_<index>`` rather than dropped, because
        dropping it would report a shorter list than the page carries.
    """
    soup = BeautifulSoup(html or "", "lxml")
    outermost = [tag for tag in soup.find_all("table") if tag.find_parent("table") is None]
    frames = find_tables(html or "")

    results: Dict[str, pd.DataFrame] = {}
    for frame in frames:
        index = frame.attrs.get("bfinance_trendlyne_table_index")
        tag = outermost[index] if isinstance(index, int) and index < len(outermost) else None
        pane = tag.find_parent("div", class_="tab-pane") if tag is not None else None
        key = (pane.get("id") if pane is not None else "") or f"table_{index}"
        if key in results:
            key = f"{key}_{index}"
        heading = str(frame.columns[0]) if len(frame.columns) else ""
        frame.attrs["bfinance_trendlyne_table"] = key
        frame.attrs["bfinance_trendlyne_swot_heading"] = heading
        results[key] = _stamped(
            frame,
            symbol,
            source_url,
            f"One SWOT fact table, keyed by its div.tab-pane id ({key!r}) rather than by its "
            f"heading, which is free text the vendor rewords. Each of these tables is a single "
            f"column whose header cell IS the fact, so the column name is that fact: "
            f"{heading!r}. {len(frame)} fact row(s).",
            retrieved_at,
            swot_table=key,
        )
    if not results:
        raise TrendlyneTableNotFoundError(
            "the SWOT page carried no outermost <table>. The five fact tables are the page's whole "
            "content, so an empty sweep means the widget is gone or its markup changed -- not that "
            "the stock has no strengths or threats."
        )
    return results


#: Column carrying which page section a merged corporate-actions row came from. Named with the
#: package prefix on purpose: it is this package's annotation, not a column trendlyne rendered, and
#: a reader must be able to tell the two apart at a glance.
_SECTION_COLUMN = "bfinance_trendlyne_section"


def parse_corporate_actions(html: str, *, symbol: str = "", source_url: Optional[str] = None,
                            retrieved_at: Optional[datetime] = None) -> Dict[str, pd.DataFrame]:
    """The corporate-actions page's tables, bucketed into exactly three keys.

    Args:
        html: The page, or a verbatim excerpt of its tables.

    Returns:
        ``{"bonus": frame, "dividend": frame, "other": frame}`` — always all three keys, in that
        order. Every frame carries ``bfinance_trendlyne_table`` set to its own key, and a
        ``bfinance_trendlyne_section`` column naming the section each row came from.

    Note:
        **Why three fixed keys for a page whose section count is not fixed.** The contract this
        method serves is three names, and TCS's page does have exactly three sections (Bonus,
        Dividend, Other). MOTHERSON's, recorded the same day, has **five** — bonus, split,
        dividend, a purpose/notes table, rights — so "keys exactly ``bonus``, ``dividend``,
        ``other``" cannot mean "one key per section" without losing two tables on some symbols.

        So ``other`` is the honest bucket: **every section that is neither a bonus nor a
        dividend**, concatenated in page order, each row tagged in
        ``bfinance_trendlyne_section`` with the signature that selected it. Nothing is dropped and
        nothing is silently reclassified — a rights row is still a rights row, it is simply in the
        bucket the three-key contract provides. The section names are the registry's, which means
        ``board_meeting`` appears here for the page's catch-all Ex-Date/Purpose/Notes table,
        because that header row is identical to the board-meeting one and the registry cannot tell
        them apart. That is a known collision, published rather than papered over: the column says
        which signature matched, not what the section means.

        A section absent from the page is an **empty frame** with the same columns, not a missing
        key: a caller iterating the three names must be able to tell "this symbol had no bonus" from
        "the parser lost the bonus table", and a missing key answers neither.

        The same holds for a section that is **present and empty** — a header row the vendor
        renders above "no rows", which is the ordinary shape for a split or a rights issue on a
        symbol that has never had one (TCS's own ``eventsData`` records ``splitTableHeader`` and
        ``rightTableHeader`` with zero rows beneath them, 2026-10-10). Such a table still
        classifies, so it still has a section name; the name comes from the registry signature
        rather than from a row, because there is no row to read one off.
    """
    signatures = page_signatures(html or "")
    frames = find_tables(html or "")

    #: Each bucket holds ``(section name, frame)`` pairs rather than bare frames, so the section
    #: list can be built for a frame with zero rows as well as one with rows. Reading the name back
    #: off a row (``.iloc[0]``) is what raised :class:`IndexError` on a header-only table: an empty
    #: frame has the column and no value in it.
    buckets: Dict[str, List[Tuple[str, pd.DataFrame]]] = {"bonus": [], "dividend": [], "other": []}
    for frame in frames:
        index = frame.attrs.get("bfinance_trendlyne_table_index")
        signature = signatures.get(index) if isinstance(index, int) else None
        key = signature if signature in ("bonus", "dividend") else "other"
        tagged = frame.copy()
        tagged[_SECTION_COLUMN] = signature or "unclassified"
        buckets[key].append((signature or "unclassified", tagged))

    results: Dict[str, pd.DataFrame] = {}
    for key, parts in buckets.items():
        sections = [name for name, _ in parts]
        if parts:
            merged = pd.concat([frame for _, frame in parts], ignore_index=True, sort=False)
            rows = len(merged)
        else:
            merged = pd.DataFrame(columns=[_SECTION_COLUMN])
            rows = 0
        merged.attrs["bfinance_trendlyne_table"] = key
        results[key] = _stamped(
            merged,
            symbol,
            source_url,
            f"The {key!r} section(s) of trendlyne.com's corporate-actions page: {rows} row(s) "
            f"from section(s) {sections or ['<none: this symbol has none>']}. Sections are "
            f"identified by header signature and each row carries the one that matched in its "
            f"{_SECTION_COLUMN!r} column, because the page's section count varies by symbol and "
            f"every section that is neither a bonus nor a dividend shares the 'other' bucket.",
            retrieved_at,
            rows=rows,
            sections=sections,
        )
    return results


def _wire_table_frame(headers: Any, rows: Any, *, label: str) -> pd.DataFrame:
    """Build a frame from a JSON table's ``tableHeaders`` + ``tableData`` pair.

    trendlyne serves these two ways and both are measured: the ``overview-second-part`` events
    blocks carry **mappings** keyed by ``unique_name``, while ``price-performance-analysis`` carries
    **positional arrays** with the names only in the header list. Either way the column name is the
    header's own ``unique_name`` (falling back to ``name``, which is all some of these blocks
    publish) and never an index — mapping by position is how a vendor reordering a table turns into
    a frame full of confidently wrong numbers.

    Args:
        headers: The header list, or ``None``.
        rows: The data list, or ``None``.
        label: Human name of the block, used in the error messages.

    Raises:
        TrendlyneSchemaError: The block is not a header list plus a data list, a header entry has
            no usable name, or a positional row is not ``len(headers)`` values wide. Never repaired.
    """
    if not isinstance(headers, list) or not headers:
        raise TrendlyneSchemaError(
            f"{label}: no usable header list (got {type(headers).__name__}). Column names come "
            f"from the header's own unique_name/name, so without it the frame would have to be "
            f"built by index. Upstream schema may have changed."
        )
    if not isinstance(rows, list):
        raise TrendlyneSchemaError(
            f"{label}: table data is {type(rows).__name__}, expected a list. Upstream schema may "
            f"have changed."
        )

    names: List[str] = []
    for header in headers:
        if not isinstance(header, dict):
            raise TrendlyneSchemaError(
                f"{label}: header entry {header!r} is not an object. Upstream schema may have "
                f"changed."
            )
        name = str(header.get("unique_name") or header.get("name") or "").strip()
        if not name:
            raise TrendlyneSchemaError(
                f"{label}: a header entry has neither 'unique_name' nor 'name': {header!r}. "
                f"Upstream schema may have changed."
            )
        names.append(name)

    out: List[Dict[str, Any]] = []
    for position, row in enumerate(rows):
        if isinstance(row, dict):
            out.append({name: row.get(name) for name in names})
            continue
        if not isinstance(row, (list, tuple)) or len(row) != len(names):
            width = len(row) if isinstance(row, (list, tuple)) else type(row).__name__
            raise TrendlyneSchemaError(
                f"{label}: data row {position} has {width} value(s), expected {len(names)}. "
                f"Upstream schema may have changed."
            )
        out.append(dict(zip(names, row)))
    return pd.DataFrame(out, columns=names)


#: Wire key -> published name for the five event tables inside ``eventsData``. Explicit rather than
#: derived from camelCase, because ``rightTableData`` is the rights table and no rule that turns
#: "right" into "rights" is worth having. A sixth block, if the vendor adds one, is **skipped and
#: published in attrs** rather than guessed at.
_EVENT_TABLES: Dict[str, str] = {
    "boardMeeting": "board_meeting",
    "dividend": "dividend",
    "bonus": "bonus",
    "split": "split",
    "right": "rights",
}


def parse_event_tables(events_data: Any, *, symbol: str = "", source_url: Optional[str] = None,
                       retrieved_at: Optional[datetime] = None) -> Dict[str, pd.DataFrame]:
    """The corporate-event tables inside ``overview-second-part``'s ``eventsData``.

    Args:
        events_data: The ``body["eventsData"]`` mapping, verbatim.

    Returns:
        ``{"board_meeting": frame, "dividend": frame, "bonus": frame, "split": frame,
        "rights": frame}`` — every key of :data:`_EVENT_TABLES` that the block carries, in that
        order.

    Note:
        **An empty event type is an empty frame, not a missing key.** TCS has no split and no
        rights issue, so both blocks are ``[]`` and both frames come back with their columns and
        no rows. Dropping them would make "this symbol had no rights issue" indistinguishable from
        "the parser found no rights table", and only the caller knows which of those they asked
        about.

        Columns come from each block's own ``unique_name`` (``boardMeetDate``, ``exDate``,
        ``dividendAmount``, …), never from position: these blocks carry different keys from each
        other and a positional mapping would put a bonus ratio under a dividend column.

        Each frame carries ``bfinance_trendlyne_event`` — its own name — and the dict's frames
        together record which blocks were present in ``bfinance_trendlyne_events_absent``.
    """
    if not isinstance(events_data, dict):
        raise TrendlyneSchemaError(
            f"eventsData is {type(events_data).__name__}, expected an object. Upstream schema may "
            f"have changed."
        )

    results: Dict[str, pd.DataFrame] = {}
    absent: List[str] = []
    pending: List[Tuple[str, pd.DataFrame]] = []
    for wire_key, name in _EVENT_TABLES.items():
        header_key, data_key = f"{wire_key}TableHeader", f"{wire_key}TableData"
        headers = events_data.get(header_key)
        rows = events_data.get(data_key)
        if headers is None and rows is None:
            absent.append(name)
            continue
        frame = _wire_table_frame(headers, rows, label=f"eventsData.{data_key}")
        frame.attrs["bfinance_trendlyne_event"] = name
        pending.append((name, frame))

    # Stamped after the sweep so every frame publishes the SAME absent-list. Filling it in as the
    # loop went would make the first frame's attrs a shorter prefix of the last one's, and a caller
    # reading two frames would see two different answers to "what was missing".
    for name, frame in pending:
        results[name] = _stamped(
            frame,
            symbol,
            source_url,
            f"The {name!r} corporate-event table from trendlyne.com's overview-second-part JSON: "
            f"{len(frame)} row(s). Columns are the block's own unique_name keys. An empty frame "
            f"means this symbol has no such event on the page, which is an answer rather than a "
            f"missing value.",
            retrieved_at,
            event=name,
            rows=len(frame),
            events_absent=absent,
        )
    return results


def parse_company_overview(
    company_profile: Any, *, symbol: str = "", source_url: Optional[str] = None,
    retrieved_at: Optional[datetime] = None,
) -> pd.DataFrame:
    """Directors and top executives, plus the business description, as one frame.

    The JSON behind ``/equity/about/`` — a page that renders client-side and carries **zero** SSR
    tables. ``companyProfileData`` holds both people tables, the description and the website.

    Args:
        company_profile: The ``body["companyProfileData"]`` mapping, verbatim.

    Returns:
        One row per person, ``role`` first, then the union of both blocks' ``unique_name`` columns.
        A person listed as both (TCS's CEO is, on the recorded capture, a director and an
        executive) appears **twice**, once per role, because collapsing them would assert that the
        vendor lists them once — and it does not.

        The description and the website are **attrs**, not columns:
        ``bfinance_trendlyne_company_description`` and ``bfinance_trendlyne_website``. They are
        prose about the company rather than rows about people, and widening the frame to carry them
        would put a paragraph in every cell.

    Note:
        The two blocks publish different keys — executives carry ``year_end``, directors carry
        ``date_str`` — so a person missing a key gets ``None``/``NaN`` in that column. That is the
        honest reading: the vendor did not publish it for that role, and filling it from the other
        block would be this package's inference. A key that appears in the records but in neither
        block's header list has no column to go under, so it is listed in
        ``attrs["bfinance_trendlyne_unheaded_keys"]`` rather than dropped unnoticed. On the
        recorded capture that list is empty.
    """
    if not isinstance(company_profile, dict):
        raise TrendlyneSchemaError(
            f"companyProfileData is {type(company_profile).__name__}, expected an object. Upstream "
            f"schema may have changed."
        )

    blocks = (
        ("director", company_profile.get("directorsTableHeader"),
         company_profile.get("directorsTableData")),
        ("executive", company_profile.get("topExecutivesTableHeader"),
         company_profile.get("topExecutivesTableData")),
    )
    names: List[str] = ["role"]
    for _, headers, _ in blocks:
        for header in headers or []:
            entry = header or {}
            name = str(entry.get("unique_name") or entry.get("name") or "").strip()
            if name and name not in names:
                names.append(name)

    rows: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}
    unheaded: List[str] = []
    for role, _, data in blocks:
        if data is None:
            counts[role] = 0
            continue
        if not isinstance(data, list):
            raise TrendlyneSchemaError(
                f"companyProfileData {role} table is {type(data).__name__}, expected a list. "
                f"Upstream schema may have changed."
            )
        counts[role] = len(data)
        for person in data:
            row = {"role": role}
            row.update({name: person.get(name) for name in names if name != "role"})
            for key in person:
                if key not in names:
                    unheaded.append(str(key))
            rows.append(row)

    frame = pd.DataFrame(rows, columns=names)
    return _stamped(
        frame,
        symbol,
        source_url,
        f"Directors ({counts.get('director', 0)}) and top executives "
        f"({counts.get('executive', 0)}) from trendlyne.com's overview-second-part JSON — "
        f"the data behind the /equity/about/ page, "
        f"which renders client-side and carries no server-rendered table. A person who holds both "
        f"roles appears once per role. The business description and the company website are attrs, "
        f"not columns. A person missing a key the other role publishes reads missing here; it is "
        f"not filled from the other block.",
        retrieved_at,
        company_description=company_profile.get("companyDescription"),
        website=company_profile.get("websiteUrl"),
        directors=counts.get("director", 0),
        top_executives=counts.get("executive", 0),
        rows=len(frame),
        unheaded_keys=sorted(set(unheaded)),
    )


def parse_technical_signals(parameters: Any, *, symbol: str = "",
                            source_url: Optional[str] = None,
                            retrieved_at: Optional[datetime] = None) -> pd.DataFrame:
    """``body.parameters`` of the advanced-technical-analysis route as a tidy frame.

    The JSON is a **nested** object: 56 top-level keys on the recorded TCS capture, of which some
    values are a single ``{name, value, insight, color}`` record (``rsi``, ``macd``, ``atr``,
    ``adx``, ``momentum``, …), some are a **list** of them (``sma_parameters``, ``ema_parameters``,
    ``oscillator_parameter``), some are a dict of them (``pivot_level``, whose values are ``pivot``,
    ``R1``..``S3``), and some are plain data with no signal in them at all (``current_price``,
    ``day_low``, ``available_frequency``, ``spot_data``).

    Args:
        parameters: ``body["parameters"]``, verbatim.

    Returns:
        Columns ``name``, ``value``, ``insight``, ``color``, ``parameter`` — one row per signal
        record, in the order the JSON presents them. ``parameter`` is the JSON key the record came
        from, so two records called "5 Day" (one SMA, one EMA) stay distinguishable.

    Note:
        ``insight`` is the insight's ``shorttext``; ``longtext`` stays in the raw mapping that
        :meth:`TrendlyneClient.adv_technical_analysis_async` returns, because a prose paragraph per
        row is not what "tidy" should mean. ``value`` is verbatim — a float, a bool or a string as
        the vendor sent it, never coerced.

        **A non-dict ``insight`` is carried, not discarded.** On the recorded capture every
        ``insight`` is either a ``{shorttext, longtext}`` object or ``None``, so the dict branch is
        the one that runs — and a branch that is never exercised by the fixture is a branch that
        fails silently. A plain string insight is one of the shapes this vendor family uses, and
        turning it into ``None`` would put a real signal in the frame with no value in its own
        column. So a string is carried verbatim, which is also what "``value`` is verbatim" means
        applied to this column.

        Keys that carry no signal record are **published, not hidden**:
        ``attrs["bfinance_trendlyne_unread_parameters"]`` lists them. That is the difference
        between "this frame has 60 rows" and "this frame has 60 of the 56 top-level keys, and here
        are the ones it does not speak for".
    """
    if not isinstance(parameters, dict):
        raise TrendlyneSchemaError(
            f"technical-analysis parameters is {type(parameters).__name__}, expected an object. "
            f"Upstream schema may have changed."
        )

    def _record(value: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(value, dict) or "name" not in value or "value" not in value:
            return None
        insight = value.get("insight")
        if isinstance(insight, dict):
            short = insight.get("shorttext")
        elif isinstance(insight, str):
            # Carried verbatim. See the Note above.
            short = insight
        else:
            short = None
        return {
            "name": value.get("name"),
            "value": value.get("value"),
            "insight": short,
            "color": value.get("color"),
        }

    rows: List[Dict[str, Any]] = []
    unread: List[str] = []
    for key, value in parameters.items():
        found: List[Dict[str, Any]] = []
        single = _record(value)
        if single is not None:
            found.append(single)
        elif isinstance(value, list):
            found = [record for record in (_record(item) for item in value) if record is not None]
        elif isinstance(value, dict):
            found = [
                record
                for record in (_record(item) for item in value.values())
                if record is not None
            ]
        if not found:
            unread.append(str(key))
        for record in found:
            record["parameter"] = key
            rows.append(record)

    frame = pd.DataFrame(
        rows, columns=["name", "value", "insight", "color", "parameter"]
    )
    return _stamped(
        frame,
        symbol,
        source_url,
        f"{len(rows)} technical signal record(s) from trendlyne.com's adv-technical-analysis JSON, "
        f"which is the robots-clean sibling of the /equity/technical-analysis-* page that "
        f"robots.txt disallows. 'insight' is the vendor's own shorttext (or the string it sent "
        f"instead of one) and its longtext remains in the raw mapping. 'value' is verbatim and "
        f"never coerced. Parameter keys that carry no "
        f"name/value record ({len(unread)}) are listed in attrs rather than dropped silently.",
        retrieved_at,
        rows=len(frame),
        parameter_keys=len(parameters),
        unread_parameters=unread,
    )


def parse_returns_comparison(block: Any, *, symbol: str = "",
                             source_url: Optional[str] = None,
                             retrieved_at: Optional[datetime] = None) -> pd.DataFrame:
    """The ``returnsComparison`` block of ``price-performance-analysis`` as a frame.

    The block is ``{"tableHeaders": [...], "tableData": [[...], ...]}`` — positional rows, with the
    column names only in the header list. Measured on TCS 2026-10-10: seven periods (``1 Day`` …
    ``3 Yrs``) against five comparators (the stock itself, ``Nifty50``, ``Sensex``, the industry
    and the sector), as **strings**, and the header entries carry no ``unique_name``.

    Returns:
        One row per period, columns named by the header list's own ``name``.

    Note:
        Values are **not** coerced. The vendor mixes ``-2.36`` and ``"-3.89"`` in one column and a
        rule that typed a column would have to pick which of those it was correcting; both are kept
        exactly as sent, and a caller that wants numbers parses the column itself.
    """
    if not isinstance(block, dict):
        raise TrendlyneSchemaError(
            f"returnsComparison is {type(block).__name__}, expected an object. Upstream schema may "
            f"have changed."
        )
    frame = _wire_table_frame(
        block.get("tableHeaders"),
        block.get("tableData"),
        label="price-performance returnsComparison",
    )
    return _stamped(
        frame,
        symbol,
        source_url,
        f"Period-over-period returns from trendlyne.com's price-performance-analysis JSON: "
        f"{len(frame)} period(s) against the vendor's comparators (the stock, Nifty 50, Sensex, "
        f"industry and sector). Values are the vendor's own and are not coerced — the block mixes "
        f"floats and strings in one column. This is a snapshot: one request, no period axis.",
        retrieved_at,
        rows=len(frame),
    )


# ------------------------------------------------- the symbol page's own sections
#
# Six blocks of data live in the HTML this package already downloads for every symbol: the OHLC
# token sits in the page's ``data-eodurl`` attribute, so the first :meth:`TrendlyneClient.ohlc`
# per symbol pays for the document and every later call reads the cached token. Reading these
# six blocks therefore costs **no request of its own** -- they are parsed out of the same bytes
# the token comes from and cached beside it (see
# :meth:`TrendlyneClient.stock_page_snapshot_async`).
#
# Every block is addressed by the ``id`` of the element that carries it, never by a CSS class or
# a heading: the surrounding markup is Bootstrap-era div soup whose classes change with the
# theme, while the six ids are the handles the page's own JavaScript binds to.
#
# Three things on that page are deliberately **not** read here, and the reasons are measurements:
#
# * ``data-companyinsights`` is a Python ``repr`` containing ``Decimal('...')`` calls, not JSON --
#   ``ast.literal_eval`` refuses it on the live page. Reading it needs a bespoke evaluator, and
#   every number it carries is already published by the key-metrics block above and by
#   :mod:`bfinance.statements`.
# * the DVM ``data-score`` attributes hold the durability and valuation scores while the page
#   renders ``-`` for a non-subscriber. That is a paywalled number and this package does not
#   launder one.
# * the schema.org ``AggregateRating`` is a reader poll, not a market fact.

#: Columns of each symbol-page section. Held here rather than in the parsers because a section
#: that cannot be read is returned as an **empty frame with the right shape** plus a status attr,
#: never as a missing key: a dict with a hole in it reads as "the vendor had nothing", which is a
#: different claim from "this package could not read what the vendor sent".
_SECTION_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "key_metrics": ("metric", "value", "unit", "param", "industry_rank", "insight", "color",
                    "chartable"),
    "price_performance": ("period", "low", "high", "change_pct", "last_price", "label", "color"),
    "consensus": ("statement", "period_type", "period", "period_end", "actual", "average",
                  "high", "low", "median", "analysts", "surprise_pct", "growth_pct",
                  "is_past", "is_current", "forecast"),
    "recommendations": ("as_of", "strong_buy", "buy", "hold", "sell", "strong_sell",
                        "no_opinion", "analysts", "reco_value", "reco_text", "average_reco_text",
                        "insight"),
    "valuation": ("basis", "heading", "title", "fair_price", "fair_price_basis", "upside_pct",
                  "upside_basis", "state"),
    "checklists": ("checklist", "title", "passed", "failed", "total", "pass_pct", "rank",
                   "insight"),
}


def _section_element(html: Union[str, BeautifulSoup], element_id: str, *, label: str) -> Any:
    """The element carrying a section, or a typed error naming what is missing.

    ``html`` may already be a parsed document. Parsing the 294 KB MOTHERSON page measured 57 ms,
    and :func:`_stock_page_snapshot` reads six sections out of that one page, so it hands each
    parser the soup it built instead of paying 57 ms six times for identical bytes. Public parsers
    still take text; this is the seam that lets the batch caller share the parse, not a second
    public input type.
    """
    soup = html if isinstance(html, BeautifulSoup) else BeautifulSoup(html or "", "lxml")
    node = soup.find(id=element_id)
    if node is None:
        raise TrendlyneSchemaError(
            f"{label}: the page carries no element with id={element_id!r}. That id is the only "
            f"handle on this block -- there is no heading or class to fall back on -- so a "
            f"missing one is a markup change, not an empty section."
        )
    return node


def _section_json(node: Any, attribute: str, *, label: str) -> Any:
    """Parse one ``data-*`` attribute as JSON, or raise a schema error naming it."""
    raw = node.get(attribute)
    if raw is None:
        raise TrendlyneSchemaError(
            f"{label}: element id={node.get('id')!r} carries no {attribute!r} attribute. "
            f"Upstream schema may have changed."
        )
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise TrendlyneSchemaError(
            f"{label}: {attribute!r} is not JSON ({exc}). Upstream schema may have changed."
        ) from exc


def _bool_attr(value: Any) -> Optional[bool]:
    """``"true"``/``"false"`` as the page spells them, or ``None`` when it does not say.

    ``None`` is not ``False`` here: an absent basis flag means *not measured*, and a consumer
    reading it as standalone would be asserting something the page never claimed.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def parse_stock_page_metrics(
    html: str, *, symbol: str = "", source_url: Optional[str] = None,
    retrieved_at: Optional[datetime] = None,
) -> pd.DataFrame:
    """The ``Key Metrics`` grid of a symbol page: the vendor's own ``info`` block.

    Fifteen records on the recorded MOTHERSON page, each ``{title, value, unit, param_name,
    shorttext, longtext, color, allowed_param_chart}``, in a ``data-metrics`` attribute on
    ``div#stock_key_metrics``. These are the fields a yfinance consumer reaches for first --
    market capitalisation, P/E, PEG, price-to-book, institutional holding, revenue and profit
    growth, operating margin, Piotroski score and ROE -- and because they are in the page the
    OHLC token comes from, reading them costs no request.

    Args:
        html: A trendlyne symbol page, or a document carrying the same element.
        symbol: Recorded in ``attrs`` only; the page is already one symbol's.
        source_url: The URL fetched, recorded in ``attrs``.
        retrieved_at: When the response arrived.

    Returns:
        Columns ``metric``, ``value``, ``unit``, ``param``, ``industry_rank``, ``insight``,
        ``color``, ``chartable`` -- one row per record, in page order.

    Note:
        ``value`` is the vendor's number in the vendor's unit and is **never converted**.
        ``unit`` reads ``"Cr."`` on market capitalisation and is empty on the ratios, and
        ``attrs["bfinance_trendlyne_units"]`` carries the page's own sentence (*"All financials
        are in INR Cr and price data in INR"*) rather than a conversion this package invented.

        The page also states the accounting basis, carried as
        ``bfinance_trendlyne_consolidated`` and ``bfinance_trendlyne_bonus_adjusted``. A consumer
        comparing these numbers with a standalone statement is comparing two different things,
        and the flags are the difference.

        ``industry_rank`` and ``insight`` are the vendor's short and long comparison text
        (``"Above industry Median"`` / ``"PE TTM is above industry Median"``). The two ``relp_*``
        relative-performance records publish neither, and read ``None`` here rather than being
        filled with a comparison the vendor did not make.

    Raises:
        TrendlyneSchemaError: The element or its ``data-metrics`` attribute is missing, the
            attribute is not JSON, a record is not an object, or the list is empty. All four are
            a markup change rather than an empty grid.
    """
    label = "stock page key metrics"
    node = _section_element(html, "stock_key_metrics", label=label)
    records = _section_json(node, "data-metrics", label=label)
    if not isinstance(records, list) or not records:
        raise TrendlyneSchemaError(
            f"{label}: data-metrics is {type(records).__name__} with "
            f"{len(records) if isinstance(records, list) else 0} record(s), expected a non-empty "
            f"list. A stock page always publishes this grid, so an empty one is a markup change."
        )
    rows: List[Dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            raise TrendlyneSchemaError(
                f"{label}: record {record!r} is not an object. Upstream schema may have changed."
            )
        rows.append(
            {
                "metric": record.get("title"),
                "value": record.get("value"),
                "unit": record.get("unit") or "",
                "param": record.get("param_name"),
                "industry_rank": record.get("shorttext"),
                "insight": record.get("longtext"),
                "color": record.get("color"),
                "chartable": record.get("allowed_param_chart"),
            }
        )
    frame = pd.DataFrame(rows, columns=list(_SECTION_COLUMNS["key_metrics"]))
    return _stamped(
        frame,
        symbol,
        source_url,
        f"{len(frame)} key metric(s) from the symbol page's own data-metrics grid. Values are the "
        f"vendor's, in the vendor's unit, and are not converted: attrs carries the page's units "
        f"sentence and its accounting basis. Two relative-performance records publish no industry "
        f"comparison and read missing here rather than filled.",
        retrieved_at,
        snapshot_only=True,
        page_section="key_metrics",
        section_status="parsed",
        rows=len(frame),
        units=node.get("data-metrics-text"),
        consolidated=_bool_attr(node.get("data-consolidated")),
        bonus_adjusted=_bool_attr(node.get("data-bonus-adjusted")),
        stock_name=node.get("data-stock-name"),
        stock_short_name=node.get("data-stock-short-name"),
    )


def parse_stock_page_performance(
    html: str, *, symbol: str = "", source_url: Optional[str] = None,
    retrieved_at: Optional[datetime] = None,
) -> pd.DataFrame:
    """The four price ranges of a symbol page, with the vendor's own period returns.

    ``div#stock_performance_parameters`` carries a ``data-metrics`` list of four records --
    ``Day``, ``Week``, ``Month`` and ``52 Week`` -- each with that period's ``high`` and ``low``,
    the change over the period in percent, and the last traded price the range was measured
    against.

    Args:
        html: A trendlyne symbol page, or a document carrying the same element.
        symbol: Recorded in ``attrs`` only.
        source_url: The URL fetched, recorded in ``attrs``.
        retrieved_at: When the response arrived.

    Returns:
        Columns ``period``, ``low``, ``high``, ``change_pct``, ``last_price``, ``label``,
        ``color``.

    Note:
        This is the zero-request version of the ``52 Week Price Range`` half of
        :meth:`TrendlyneClient.price_performance`, which is a second route and a second request.
        The ``52 Week`` row is ``fiftyTwoWeekHigh`` / ``fiftyTwoWeekLow`` and its ``change_pct``
        is the one-year return.

        **``last_price`` is the vendor's rounded print, not the quote.** On the recorded page all
        four rows read ``157.3`` while the same page's ``data-currentprice`` -- and its
        ``<meta name="description">`` -- read ``157.27``. It is carried verbatim because that is
        what the block says; a caller that needs the quote takes it from the price series.

    Raises:
        TrendlyneSchemaError: The element or its ``data-metrics`` attribute is missing, the
            attribute is not JSON, a record is not an object, or the list is empty.
    """
    label = "stock page price performance"
    node = _section_element(html, "stock_performance_parameters", label=label)
    records = _section_json(node, "data-metrics", label=label)
    if not isinstance(records, list) or not records:
        raise TrendlyneSchemaError(
            f"{label}: data-metrics is {type(records).__name__} with "
            f"{len(records) if isinstance(records, list) else 0} record(s), expected a non-empty "
            f"list. Every symbol page publishes these ranges, so an empty one is a markup change."
        )
    rows: List[Dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            raise TrendlyneSchemaError(
                f"{label}: record {record!r} is not an object. Upstream schema may have changed."
            )
        rows.append(
            {
                "period": record.get("title"),
                "low": record.get("low"),
                "high": record.get("high"),
                "change_pct": record.get("change_val"),
                "last_price": record.get("ltp"),
                "label": record.get("shorttext"),
                "color": record.get("color"),
            }
        )
    frame = pd.DataFrame(rows, columns=list(_SECTION_COLUMNS["price_performance"]))
    return _stamped(
        frame,
        symbol,
        source_url,
        f"{len(frame)} price range(s) from the symbol page's own data-metrics block: day, week, "
        f"month and 52 week high/low with the vendor's change over each period. 'last_price' is "
        f"the vendor's rounded print and differs from the same page's quote -- read the series "
        f"for a price.",
        retrieved_at,
        snapshot_only=True,
        page_section="price_performance",
        section_status="parsed",
        rows=len(frame),
        stock_name=node.get("data-stock-name"),
        stock_short_name=node.get("data-stock-short-name"),
    )


def parse_stock_page_consensus(
    html: str, *, symbol: str = "", source_url: Optional[str] = None,
    retrieved_at: Optional[datetime] = None,
) -> pd.DataFrame:
    """The analyst estimate series embedded in a symbol page, as one long frame.

    ``section#consensus-results`` carries ``data-consensusjson``: a 66 KB object of **fifteen**
    statements -- ``EPS``, ``CASH_EPS``, ``REVENUE``, ``EBIT``, ``EBITDA``, ``NET_INCOME``,
    ``TARGET_PRICE`` and the rest -- each holding a ``QUARTER`` and/or ``ANNUAL`` list of
    ``{ACTUAL, AVG, HIGH, LOW, MEDIAN, NUMBER_OF_ANALYSTS, SURPRISES, GROWTH, periodtype,
    qtr_end_date, is_past, is_current, ...}``.

    Args:
        html: A trendlyne symbol page, or a document carrying the same element.
        symbol: Recorded in ``attrs`` only.
        source_url: The URL fetched, recorded in ``attrs``.
        retrieved_at: When the response arrived.

    Returns:
        Columns ``statement``, ``period_type``, ``period``, ``period_end``, ``actual``,
        ``average``, ``high``, ``low``, ``median``, ``analysts``, ``surprise_pct``,
        ``growth_pct``, ``is_past``, ``is_current``, ``forecast``.

    Note:
        ``statement`` is the vendor's own key and is **not** renamed: ``EPS`` and ``CASH_EPS``
        are two different series on this page, and collapsing them would pick one. ``period`` is
        the vendor's label (``FY26``, ``Q2FY27``) and ``period_end`` its ``qtr_end_date``.

        ``is_past`` is carried rather than used to split the frame. A row with ``is_past`` false
        and an ``average`` is a forecast; a row with ``is_past`` true and an ``actual`` is a
        print; a row that is neither reads ``None`` in both columns. Nothing is inferred from a
        date, and the vendor's ``resulttype`` is not surfaced because ``period_type`` already
        says which list the row came from.

        ``TARGET_PRICE`` additionally carries ``TARGET_PRICE_FORECAST`` -- the forward target
        block, one annual and one quarterly record -- and those two rows are appended with
        ``forecast = True``. No other statement publishes that key on the recorded page.

        ``RECOMMENDATION`` is a **list** on this page rather than a series object, so it has no
        rows here; it is :func:`parse_stock_page_recommendations`, and it is named in
        ``attrs["bfinance_trendlyne_unread_statements"]`` so the omission is visible rather than
        silent.

        The vendor's per-statement prose (``INSIGHT``, e.g. *"EPS was higher than average
        estimate 2 times in past 3 years"*) is carried as short text in
        ``attrs["bfinance_trendlyne_insights"]``, keyed by statement and period type, rather than
        repeated on every row.

    Raises:
        TrendlyneSchemaError: The element or its ``data-consensusjson`` attribute is missing, the
            attribute is not JSON, or it yields no series row at all.
    """
    label = "stock page consensus estimates"
    node = _section_element(html, "consensus-results", label=label)
    payload = _section_json(node, "data-consensusjson", label=label)
    if not isinstance(payload, dict):
        raise TrendlyneSchemaError(
            f"{label}: data-consensusjson is {type(payload).__name__}, expected an object keyed "
            f"by statement. Upstream schema may have changed."
        )

    rows: List[Dict[str, Any]] = []
    unread: List[str] = []
    insights: Dict[str, Dict[str, str]] = {}
    for statement, block in payload.items():
        if not isinstance(block, dict):
            unread.append(str(statement))
            continue
        found = False
        for period_type in ("QUARTER", "ANNUAL"):
            series = block.get(period_type)
            if not isinstance(series, list):
                continue
            for record in series:
                if not isinstance(record, dict):
                    raise TrendlyneSchemaError(
                        f"{label}: {statement}.{period_type} holds {record!r}, expected an "
                        f"object. Upstream schema may have changed."
                    )
                found = True
                rows.append(_consensus_row(statement, period_type, record, forecast=False))
        if statement == "TARGET_PRICE":
            # The one statement that publishes a forward block beside its history. Measured on
            # the recorded page: no other key carries a ``*_FORECAST`` sibling.
            for period_type in ("ANNUAL", "QUARTER"):
                record = (block.get("TARGET_PRICE_FORECAST") or {}).get(period_type)
                if isinstance(record, dict):
                    found = True
                    rows.append(_consensus_row(statement, period_type, record, forecast=True))
        if not found:
            unread.append(str(statement))
        for period_type, text in _consensus_insights(block.get("INSIGHT")).items():
            insights.setdefault(str(statement), {}).update({period_type: text})

    if not rows:
        raise TrendlyneSchemaError(
            f"{label}: data-consensusjson yielded no series row. The page publishes fifteen "
            f"statements with quarter and annual estimates, so an empty parse is a schema change."
        )

    frame = pd.DataFrame(rows, columns=list(_SECTION_COLUMNS["consensus"]))
    return _stamped(
        frame,
        symbol,
        source_url,
        f"{len(frame)} analyst estimate row(s) from the symbol page's own data-consensusjson, "
        f"which is the estimate grid the /equity/consensus-estimates/ page renders client-side. "
        f"One row per statement and period, with the vendor's own is_past/is_current flags; "
        f"'actual' is a print and 'average' a forecast, and neither is inferred from a date. "
        f"RECOMMENDATION is a list rather than a series and is not in this frame.",
        retrieved_at,
        snapshot_only=True,
        page_section="consensus",
        section_status="parsed",
        rows=len(frame),
        statements=len({row["statement"] for row in rows}),
        unread_statements=sorted(set(unread)),
        insights=insights,
        current_price=_consensus_current_price(node),
    )


def _consensus_row(statement: Any, period_type: str, record: Mapping[str, Any], *,
                   forecast: bool) -> Dict[str, Any]:
    """One wire record of ``data-consensusjson`` as a flat row, with the vendor's names mapped."""
    return {
        "statement": statement,
        "period_type": period_type,
        "period": record.get("periodtype"),
        "period_end": record.get("qtr_end_date"),
        "actual": record.get("ACTUAL"),
        "average": record.get("AVG"),
        "high": record.get("HIGH"),
        "low": record.get("LOW"),
        "median": record.get("MEDIAN"),
        "analysts": record.get("NUMBER_OF_ANALYSTS"),
        "surprise_pct": record.get("SURPRISES"),
        "growth_pct": record.get("GROWTH"),
        "is_past": record.get("is_past"),
        "is_current": record.get("is_current"),
        "forecast": forecast,
    }


def _consensus_insights(block: Any) -> Dict[str, str]:
    """The short insight sentence per period type, or ``{}`` when the statement has none.

    Only the ``st`` text is kept. ``lt`` is the same sentence on every measured record and
    ``value`` is null on every one, so a frame column would be a constant and a dict entry would
    be a paragraph per row.
    """
    if not isinstance(block, dict):
        return {}
    texts: Dict[str, str] = {}
    for period_type, record in block.items():
        if not isinstance(record, dict):
            continue
        parts = [
            str(record[key].get("st"))
            for key in ("performanceInsight", "forwardInsight")
            if isinstance(record.get(key), dict) and record[key].get("st")
        ]
        if parts:
            texts[str(period_type)] = " ".join(parts)
    return texts


def _consensus_current_price(node: Any) -> Optional[float]:
    """The page's own ``data-currentprice``, or ``None`` when it is absent or unreadable.

    Kept separate from the estimate rows because it is the *quote* the upside in
    ``TARGET_PRICE_FORECAST`` is measured against, not an estimate. ``None`` rather than ``0.0``:
    a missing quote is not a free share.
    """
    raw = node.get("data-currentprice")
    if raw is None:
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _recommendation_count(value: Any) -> Any:
    """A recommendation count, unwrapped from the ``{"value": n, "class": "buy"}`` box.

    ``RECOMMENDATION``'s five count fields are boxes where ``ANALYST_RECOMMENDATION``'s same five
    are plain numbers: the page puts a colour class beside each count for its own bar chart, and
    the count is the ``value``. Returning the box would put a dict in a column whose name says
    "count", so the box is opened here and the colour is dropped -- presentation, not data.
    """
    if isinstance(value, dict):
        return value.get("value")
    return value


def parse_stock_page_recommendations(
    html: str, *, symbol: str = "", source_url: Optional[str] = None,
    retrieved_at: Optional[datetime] = None,
) -> pd.DataFrame:
    """The analyst recommendation history embedded in a symbol page.

    ``data-consensusjson``'s ``RECOMMENDATION`` key is a **list**, not a series object like its
    fifteen siblings: six monthly snapshots on the recorded page, each with the buy/hold/sell
    counts, the number of analysts, the vendor's ``RECO_VALUE`` and its ``RECO_TEXT``.

    Args:
        html: A trendlyne symbol page, or a document carrying the same element.
        symbol: Recorded in ``attrs`` only.
        source_url: The URL fetched, recorded in ``attrs``.
        retrieved_at: When the response arrived.

    Returns:
        Columns ``as_of``, ``strong_buy``, ``buy``, ``hold``, ``sell``, ``strong_sell``,
        ``no_opinion``, ``analysts``, ``reco_value``, ``reco_text``, ``average_reco_text``,
        ``insight``.

    Note:
        The five buy/sell count fields arrive **boxed** on this block --
        ``{"value": 12.0, "class": "d_buy"}`` -- where the same five on ``ANALYST_RECOMMENDATION``
        are plain numbers. The box is opened and its ``value`` published, because a column named
        ``strong_buy`` holding a dict is not a count; the ``class`` is the vendor's bar-chart
        colour and is dropped.

        ``reco_value`` is the vendor's own scale and is carried without a legend: the recorded
        page reads ``1.82`` with ``reco_text`` ``"BUY"``, and this package does not publish a
        mapping from one to the other that trendlyne never stated. ``as_of`` is the vendor's
        ``asofdate`` (``YYYY-MM-DD``), which is a snapshot date rather than a period end. The
        counts do **not** sum to ``analysts`` on the recorded page (18 buy-side + 3 hold/sell + 1
        no-opinion = 22 against 21 analysts), so no total is derived from them.

    Raises:
        TrendlyneSchemaError: The element or its ``data-consensusjson`` attribute is missing, the
            attribute is not JSON, ``RECOMMENDATION`` is absent, or it is empty.
    """
    label = "stock page analyst recommendations"
    node = _section_element(html, "consensus-results", label=label)
    payload = _section_json(node, "data-consensusjson", label=label)
    records = payload.get("RECOMMENDATION") if isinstance(payload, dict) else None
    if not isinstance(records, list) or not records:
        raise TrendlyneSchemaError(
            f"{label}: data-consensusjson carries no non-empty RECOMMENDATION list (got "
            f"{type(records).__name__}). Upstream schema may have changed."
        )
    rows: List[Dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            raise TrendlyneSchemaError(
                f"{label}: record {record!r} is not an object. Upstream schema may have changed."
            )
        reco_text = record.get("RECO_TEXT")
        insight = record.get("INSIGHT")
        rows.append(
            {
                "as_of": record.get("asofdate"),
                "strong_buy": _recommendation_count(record.get("STRONG_BUY")),
                "buy": _recommendation_count(record.get("BUY")),
                "hold": _recommendation_count(record.get("HOLD")),
                "sell": _recommendation_count(record.get("SELL")),
                "strong_sell": _recommendation_count(record.get("STRONG_SELL")),
                "no_opinion": record.get("NO_OPINION"),
                "analysts": record.get("NUMBER_OF_ANALYSTS"),
                "reco_value": record.get("RECO_VALUE"),
                "reco_text": reco_text.get("value") if isinstance(reco_text, dict) else reco_text,
                "average_reco_text": record.get("AVG_RECO_TEXT"),
                "insight": insight.get("st") if isinstance(insight, dict) else insight,
            }
        )
    frame = pd.DataFrame(rows, columns=list(_SECTION_COLUMNS["recommendations"]))
    return _stamped(
        frame,
        symbol,
        source_url,
        f"{len(frame)} monthly analyst-recommendation snapshot(s) from the symbol page's own "
        f"data-consensusjson. Counts and the vendor's RECO_VALUE are carried verbatim; this "
        f"package does not publish a scale the vendor never stated. 'insight' is the vendor's "
        f"short sentence, repeated on every row because it describes the whole series.",
        retrieved_at,
        snapshot_only=True,
        page_section="recommendations",
        section_status="parsed",
        rows=len(frame),
    )


def parse_stock_page_valuation(
    html: str, *, symbol: str = "", source_url: Optional[str] = None,
    retrieved_at: Optional[datetime] = None,
) -> pd.DataFrame:
    """The PE-based fair-price block of a symbol page.

    ``div#stock_pe_valuation_check`` carries two JSON attributes -- ``data-pe-current-value`` and
    ``data-pe-future-value`` -- each holding ``{heading, title, attr1_heading, attr1_title,
    attr1_val, attr2_heading, attr2_title, attr2_val, st}``: a fair price on a stated basis, the
    upside to it, and the vendor's one-word verdict.

    Args:
        html: A trendlyne symbol page, or a document carrying the same element.
        symbol: Recorded in ``attrs`` only.
        source_url: The URL fetched, recorded in ``attrs``.
        retrieved_at: When the response arrived.

    Returns:
        Columns ``basis``, ``heading``, ``title``, ``fair_price``, ``fair_price_basis``,
        ``upside_pct``, ``upside_basis``, ``state`` -- two rows, ``current`` and ``future``.

    Note:
        ``fair_price`` is **the vendor's derivation, not a price this package computed**, and the
        basis is in the same row: ``"Based on 5Yr Average PE"`` for the current row and
        ``"Based on 1Yr Forward EPS"`` for the forward one. The recorded page reads 184.3 and
        236.3 against a quote of 157.27, which is the vendor's ``Undervalued`` verdict and is
        carried as ``state``.

        ``attrs["bfinance_trendlyne_insight"]`` is the vendor's sentence about the pair
        (*"… is undervalued at both current PE and future earnings estimates."*), with its own
        ``insight_color``. ``attrs["bfinance_trendlyne_buy_sell_zone_url"]`` is the Tier-2 route
        the same element points at, recorded so a caller knows where the fuller page lives
        without this function fetching it.

    Raises:
        TrendlyneSchemaError: The element, either JSON attribute, or a required field is missing,
            or an attribute is not JSON.
    """
    label = "stock page PE valuation"
    node = _section_element(html, "stock_pe_valuation_check", label=label)
    rows: List[Dict[str, Any]] = []
    for basis, attribute in (("current", "data-pe-current-value"),
                             ("future", "data-pe-future-value")):
        record = _section_json(node, attribute, label=label)
        if not isinstance(record, dict):
            raise TrendlyneSchemaError(
                f"{label}: {attribute} is {type(record).__name__}, expected an object. Upstream "
                f"schema may have changed."
            )
        rows.append(
            {
                "basis": basis,
                "heading": record.get("heading"),
                "title": record.get("title"),
                "fair_price": record.get("attr1_val"),
                "fair_price_basis": record.get("attr1_title"),
                "upside_pct": record.get("attr2_val"),
                "upside_basis": record.get("attr2_title"),
                "state": record.get("st"),
            }
        )
    frame = pd.DataFrame(rows, columns=list(_SECTION_COLUMNS["valuation"]))
    return _stamped(
        frame,
        symbol,
        source_url,
        "PE-based fair price and upside on two bases, from the symbol page's own "
        "data-pe-current-value / data-pe-future-value attributes. The fair price is the "
        "vendor's derivation and its basis is in the same row; nothing here is computed by "
        "bfinance. 'state' is the vendor's verdict verbatim.",
        retrieved_at,
        snapshot_only=True,
        page_section="valuation",
        section_status="parsed",
        rows=len(frame),
        insight=node.get("data-pe-insight"),
        insight_color=node.get("data-pe-insight-color"),
        buy_sell_zone_url=node.get("data-buy-sell-url"),
    )


def parse_stock_page_checklists(
    html: str, *, symbol: str = "", source_url: Optional[str] = None,
    retrieved_at: Optional[datetime] = None,
) -> pd.DataFrame:
    """The two pass/fail checklists of a symbol page, as counts.

    ``div#trendlyne_checklist`` carries ``data-checklistdict``: a JSON object keyed by checklist
    (``trendlyne``, ``graham``), each with ``pos``, ``neg``, ``total``, a title, an insight
    sentence and, for one of the two, a numeric ``checklistP``.

    Args:
        html: A trendlyne symbol page, or a document carrying the same element.
        symbol: Recorded in ``attrs`` only.
        source_url: The URL fetched, recorded in ``attrs``.
        retrieved_at: When the response arrived.

    Returns:
        Columns ``checklist``, ``title``, ``passed``, ``failed``, ``total``, ``pass_pct``,
        ``rank``, ``insight``.

    Note:
        **``pass_pct`` is missing for the Graham checklist on the recorded page while its
        ``rank`` sentence still reads "High rank - 60.0% pass in checklist".** The number is only
        published as prose for that checklist, and it is left missing rather than parsed out of
        the sentence: ``6 / 10`` is the same fact and needs no regex, while a regex over a
        sentence is a parser that breaks when the wording changes. ``None`` here means *the
        vendor did not publish it in this field*, not *zero*.

    Raises:
        TrendlyneSchemaError: The element or its ``data-checklistdict`` attribute is missing, the
            attribute is not JSON, or it carries no checklist.
    """
    label = "stock page checklists"
    node = _section_element(html, "trendlyne_checklist", label=label)
    payload = _section_json(node, "data-checklistdict", label=label)
    if not isinstance(payload, dict) or not payload:
        raise TrendlyneSchemaError(
            f"{label}: data-checklistdict is {type(payload).__name__} with "
            f"{len(payload) if isinstance(payload, dict) else 0} checklist(s), expected a "
            f"non-empty object. Upstream schema may have changed."
        )
    rows: List[Dict[str, Any]] = []
    for key, record in payload.items():
        if not isinstance(record, dict):
            raise TrendlyneSchemaError(
                f"{label}: checklist {key!r} is {type(record).__name__}, expected an object. "
                f"Upstream schema may have changed."
            )
        rows.append(
            {
                "checklist": key,
                "title": record.get("title"),
                "passed": record.get("pos"),
                "failed": record.get("neg"),
                "total": record.get("total"),
                "pass_pct": record.get("checklistP"),
                "rank": record.get("insightp"),
                "insight": record.get("insight"),
            }
        )
    frame = pd.DataFrame(rows, columns=list(_SECTION_COLUMNS["checklists"]))
    return _stamped(
        frame,
        symbol,
        source_url,
        f"{len(frame)} pass/fail checklist(s) from the symbol page's own data-checklistdict. "
        f"'passed'/'failed' are the vendor's pos/neg counts. 'pass_pct' is published numerically "
        f"for only one checklist on the recorded page and reads missing for the other, whose "
        f"percentage exists only inside its 'rank' sentence -- missing here means not published "
        f"in that field, never zero.",
        retrieved_at,
        snapshot_only=True,
        page_section="checklists",
        section_status="parsed",
        rows=len(frame),
    )


#: Fixed keys of :meth:`TrendlyneClient.stock_page_snapshot`, in page order. Declared as a tuple
#: so a caller can iterate the contract and a test can assert it, and so a missing key is a
#: contract change rather than a surprise.
STOCK_PAGE_SECTIONS: Tuple[str, ...] = tuple(_SECTION_COLUMNS)

#: The six parsers, in the same order. A dict rather than six sequential calls so the failure of
#: one section can be caught, recorded and returned as an unreadable frame instead of taking the
#: other five with it.
_STOCK_PAGE_PARSERS = {
    "key_metrics": parse_stock_page_metrics,
    "price_performance": parse_stock_page_performance,
    "consensus": parse_stock_page_consensus,
    "recommendations": parse_stock_page_recommendations,
    "valuation": parse_stock_page_valuation,
    "checklists": parse_stock_page_checklists,
}


def _stock_page_snapshot(
    html: str, *, symbol: str, source_url: Optional[str], retrieved_at: Optional[datetime],
) -> Dict[str, pd.DataFrame]:
    """Parse every symbol-page section, turning a failure into a stated one.

    The six sections come from one document, so one of them changing shape must not cost a
    caller the other five. A section that raises is returned as an **empty frame with its
    columns** whose ``bfinance_trendlyne_section_status`` reads ``"unreadable"`` and whose
    ``bfinance_trendlyne_section_error`` names the exception; every frame in the dict also
    carries ``bfinance_trendlyne_sections_failed``, so the failure travels with the data rather
    than being swallowed here.

    Only :class:`~bfinance.trendlyne.errors.TrendlyneError` and ``ValueError`` are caught: those
    are what the parsers raise, and anything else is a bug in this module rather than a vendor
    change, so it must not be relabelled as one.

    The document is parsed **once** here and the resulting soup handed to all six parsers. Six
    independent ``BeautifulSoup`` builds of the recorded 294 KB page measured 340 ms against 57 ms
    for one, and this function runs on the cold path of every :meth:`TrendlyneClient.ohlc`.
    """
    soup = BeautifulSoup(html or "", "lxml")
    frames: Dict[str, pd.DataFrame] = {}
    failures: Dict[str, str] = {}
    for key, parser in _STOCK_PAGE_PARSERS.items():
        try:
            frames[key] = parser(
                soup, symbol=symbol, source_url=source_url, retrieved_at=retrieved_at
            )
        except (TrendlyneError, ValueError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
            failures[key] = reason
            empty = pd.DataFrame(columns=list(_SECTION_COLUMNS[key]))
            frames[key] = _stamped(
                empty,
                symbol,
                source_url,
                f"This section could not be read from the symbol page ({reason}). It is returned "
                f"as an empty frame **with its columns** so the shape is stable; "
                f"attrs['bfinance_trendlyne_section_status'] reads 'unreadable'. An empty frame "
                f"here does not mean the vendor published no rows.",
                retrieved_at,
                snapshot_only=True,
                page_section=key,
                section_status="unreadable",
                section_error=reason,
                rows=0,
            )
    for frame in frames.values():
        frame.attrs["bfinance_trendlyne_sections_failed"] = dict(failures)
    return frames


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
            :data:`DEFAULT_MIN_REQUEST_INTERVAL_MS` for the robots-derived 1 s floor this default
            matches. ``0.0`` disables pacing, which is how the offline tests run.
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
        allow_member_search: Whether to use trendlyne's own autocomplete route, which its
            ``robots.txt`` disallows via the broad ``Disallow: */member/``. See
            :data:`ROBOTS_DISALLOWED_ROUTES` and :data:`MEMBER_SEARCH_FLAG`.

            **On by default**, which is a decision this package's operator made and which a
            reader should be able to judge rather than inherit. The reasoning was coverage, and
            it is worth stating plainly: the equity sitemap carries 5,925 instruments and
            **no ETFs at all**, so with this off the route is the *only* way to reach an ETF.
            Measured on a mixed list of six real symbols on 2026-09-30 -- ``ntpc``, ``cipla``,
            ``wipro``, ``indgn``, ``mafang``, ``hngsngbees`` -- the strict default resolved four
            and refused two, and both refusals were ETFs that resolve fine once this is on
            (``MAFANG`` 1,334 bars, ``HNGSNGBEES`` 2,602 bars). A default that fails a third of
            a realistic portfolio is not a default anyone can ship.

            So: this package knowingly requests a path the site asks crawlers not to fetch, on
            any lookup for a symbol the sitemap omits. That is a standing compliance choice and
            it is the operator's to make, which is why it is a named flag with the default
            stated in one place rather than buried in a resolution order. Pass
            ``allow_member_search=False`` for the strict, sitemap-only behaviour, in which ETFs
            are unreachable and the refusal says so.
        allow_full_search: Opt in to the **richer** search route
            (``/member/api/ac_snames/all/``, 22 fields, categorised) used by :meth:`search_full`.
            **Off by default, and a second gate rather than a wider one:** this route needs
            ``allow_member_search=True`` *as well*, so opening one without the other still
            refuses. See :data:`FULL_SEARCH_FLAG`. It stays shut even though the flag beneath it
            is now open, because the two are separate decisions: one resolves a ticker, the
            other lists ranked candidates for a human to choose from, and only the first is
            needed to fetch a price history.
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
        allow_member_search: bool = True,
        allow_full_search: bool = False,
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
        #: Second opt-in for :meth:`search_full`, and it is checked *after* ``allow_member_search``,
        #: never instead of it. Read it; set it in the constructor or per call.
        self.allow_full_search = bool(allow_full_search)
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
        # (fetched_at, {section key: frame}). The six symbol-page sections are parsed out of the
        # same page the token comes from, so they are cached beside it and cost no request of their
        # own -- see :meth:`stock_page_snapshot_async`. Kept in its own dict rather than in the
        # tuple above because ``_tokens``' three-element contract is what five other methods
        # unpack, and a fourth element would break them for a reader that never wants sections.
        self._page_snapshots: Dict[int, Tuple[float, Dict[str, pd.DataFrame]]] = {}
        #: How the most recent :meth:`resolve_any` resolved its symbol — ``"sitemap"``,
        #: ``"member_search"`` or ``"skipped_robots_disallowed"``. Written on every call so the
        #: degradation is recorded rather than inferred from a missing result. Read it; do not set
        #: it.
        self.symbol_resolution: Optional[str] = None
        #: Every HTTP request this client has actually attempted, including retries and including
        #: the ones a section method spends resolving a symbol. It exists because a section frame's
        #: note used to claim "fetched in one request" as a constant, which is only true for a warm
        #: sitemap: a cold index on a symbol the sitemap omits costs three, two of them on the
        #: robots-gated ``/member/`` route. A caller counting their WAF budget needs the number, not
        #: the claim.
        self._request_count: int = 0

    @property
    def request_count(self) -> int:
        """How many HTTP requests this client has attempted since it was built.

        Counts **attempts**, not responses: a retried 429 costs two, because that is what the
        vendor was asked for. Section frames publish the delta across their own call as
        ``attrs["bfinance_trendlyne_requests"]``, so the cost of one call is checkable without
        holding a client.
        """
        return self._request_count

    def _require_member_search(
        self,
        allowed: Optional[bool],
        *,
        full: bool = False,
        allow_full: Optional[bool] = None,
    ) -> None:
        """Refuse a robots-disallowed request, naming the conflict and the switch.

        One check for both search routes, because the reasoning is identical and the difference is
        only **how many** flags it takes. :meth:`search_remote_async` calls it with ``full=False``
        and needs :data:`MEMBER_SEARCH_FLAG`; :meth:`search_full_async` calls it with ``full=True``
        and needs that flag **and** :data:`FULL_SEARCH_FLAG`. Duplicating the raise would have
        produced two messages to keep in step, and they are the same message with a different number
        of switches named.

        Args:
            allowed: The per-call override of :data:`MEMBER_SEARCH_FLAG`, or ``None`` to use the
                constructor's setting.
            full: ``True`` to apply the two-flag rule for :data:`_SYMBOL_SEARCH_FULL_PATH`. It is
                checked **first**, so a caller who has not opened :data:`MEMBER_SEARCH_FLAG` is
                told that, rather than being walked through a second flag they cannot use yet.
            allow_full: The per-call override of :data:`FULL_SEARCH_FLAG`, or ``None``. Only read
                when ``full`` is ``True``.

        Raises:
            TrendlyneRobotsDisallowedError: The route is disallowed and the gate it needs is
                closed. Raised **before** any socket is opened, so honouring robots costs this
                package nothing. The message names the **specific** missing flag by its constant
                name, and for the two-flag route it says which of the two is missing rather than
                listing both as if either would do.
        """
        if allowed if allowed is not None else self.allow_member_search:
            if not full:
                return
            if allow_full if allow_full is not None else self.allow_full_search:
                return
            path = _SYMBOL_SEARCH_FULL_PATH.split("?")[0]
            rule = ROBOTS_DISALLOWED_ROUTES.get(path, "Disallow")
            raise TrendlyneRobotsDisallowedError(
                f"{path} is disallowed by trendlyne's robots.txt ({rule}), so this request was "
                f"not made. This route needs BOTH {MEMBER_SEARCH_FLAG}=True (which you have set) "
                f"AND {FULL_SEARCH_FLAG}=True, and {FULL_SEARCH_FLAG} is the one that is closed. "
                f"Pass {FULL_SEARCH_FLAG}=True to TrendlyneClient(...) or to search_full(...). "
                f"Note that opening it changes nothing about the site's policy: this is still a "
                f"robots-disallowed path, and the default stays that both flags are off."
            )
        path = _SYMBOL_SEARCH_PATH.split("?")[0]
        rule = ROBOTS_DISALLOWED_ROUTES.get(path, "Disallow")
        raise TrendlyneRobotsDisallowedError(
            f"{path} is disallowed by trendlyne's robots.txt ({rule}), so this request was "
            f"not made. It is the only route to instruments the equity sitemap omits, which "
            f"is every ETF: NIFTYBEES, BANKBEES, GOLDBEES, JUNIORBEES, HNGSNGBEES, MAFANG and "
            f"SETFNIF50 all resolve here and appear in no sitemap entry, so while this "
            f"client has the route closed those symbols are unreachable. That is a coverage "
            f"gap, not a claim that they do not exist. It is closed because "
            f"{MEMBER_SEARCH_FLAG}=False was passed, and that flag **defaults to True** -- so "
            f"reaching this refusal means the strict setting was chosen deliberately. Drop the "
            f"argument, or pass {MEMBER_SEARCH_FLAG}=True, to reach those instruments, at the "
            f"cost of requesting a path the site asks crawlers not to fetch. Note robots.txt's "
            f"'User-agent: *' block carries Crawl-delay: 1; the 10 s figure quoted elsewhere is "
            f"scoped to GPTBot/ClaudeBot."
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
        Every 4xx — **including 444 and the WAF's 405** — is terminal, because retrying an
        unchanged request cannot change its outcome and does cost the vendor a request. The WAF
        header form is checked on every response whatever its status, because WAF attaches it
        independently of the code it returns.
        """
        url = self._absolute(path)
        last_error: Optional[Exception] = None
        last_response_error: Optional[TrendlyneError] = None

        for attempt in range(self.max_retries):
            await self._throttle_pacing()
            self._request_count += 1
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

            # The WAF is checked before every other status, 200 included: WAF attaches the header
            # independently of the code it returns, and a challenge served as a 200 is a captcha
            # page, not data. A challenged session is also not "slow down" but "this session is
            # over", so backing off inside it would spend three attempts proving that.
            waf_action = response.headers.get(_WAF_ACTION_HEADER)
            if status == 405 or waf_action:
                seen = (
                    f"{_WAF_ACTION_HEADER}={waf_action}"
                    if waf_action
                    else f"no {_WAF_ACTION_HEADER} header"
                )
                raise TrendlyneWafChallengeError(
                    f"trendlyne's edge (AWS WAF on CloudFront) challenged this request: HTTP "
                    f"{status} with {seen} for {redact(url)}. This is terminal and was not "
                    f"retried, because the budget is spent rather than momentarily exceeded -- "
                    f"the same request would be refused for the rest of the session, and retrying "
                    f"only spends more of it. The allowance usually quoted for anonymous access "
                    f"is roughly 130-150 requests per session; that is a third-party measurement "
                    f"recorded on 2026-10-10, not a limit bfinance publishes or can promise. What "
                    f"this package enforces is its own: one request per second, serially, through "
                    f"a global pacing lock. Expect a cooldown of roughly ten minutes before the "
                    f"session is usable again.",
                    status_code=status,
                    url=redact(url),
                )

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
        """Forget the sitemap index, every cached token and every cached page section."""
        with self._state_lock:
            self._index = None
            self._index_at = 0.0
            self._index_source_bytes = 0
            self._tokens.clear()
            self._page_snapshots.clear()

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

    # -------------------------------------------- full search: the categorised route

    async def search_full_async(
        self,
        query: str,
        *,
        limit: int = 10,
        allow_member_search: Optional[bool] = None,
        allow_full_search: Optional[bool] = None,
    ) -> List[TrendlyneSymbol]:
        """Search trendlyne's **richer** autocomplete, which says what each hit *is*.
        **Still opt-in twice**, even though the plain route beneath it is now open by default.

        :data:`_SYMBOL_SEARCH_FULL_PATH` is a second route beside :meth:`search_remote_async`, and
        it returns **22 fields** where that one returns 4. A real record, verbatim::

            {"id": "NIFTYBEES", "label": "Nippon India ETF Nifty 50 BeES(NIFTYBEES)",
             "value": "NIFTYBEES", "k": 1588, "slugname": "nippon-india-etf-nifty-50-bees",
             "country": "IND", "defaultExchange": "NSE", "stock_code": "NIFTYBEES",
             "UIcode": "NIFTYBEES", "category": "ETF", "pageType": 10, "NSEcode": "NIFTYBEES",
             "BSEcode": "590103", "isCfd": false, "currentPrice": 259.47, "day_change": -1.0,
             "day_changeP": -0.38, "day_high": 260.49, "day_low": 257.9, "sectorName": "Others",
             "industry": {}, "nexturl": "https://trendlyne.com/equity/1588/NIFTYBEES/...",
             "urls": [["Overview", "https://trendlyne.com/equity/1588/NIFTYBEES/..."]]}

        Only three of those are surfaced, because only three belong on a resolver record: the symbol
        and id (addressing), the slug (the URL), and the name and category (what it is). The rest --
        a live price, a day's change -- are a snapshot with no date on them, and this package does
        not publish an undated price.

        **The category is the point of the method, and it is carried rather than filtered.** The
        measured vocabulary across 136 distinct ``(category, symbol)`` pairs from 8 queries on
        2026-09-30 is six strings: ``Equity`` (73, pageType 2), ``Index`` (26, pageType 24), ``ETF``
        (12, pageType 10), ``Mutual Funds`` (11, pageType 12), ``Not Traded`` (9, pageType 2) and
        ``Superstars`` (5, no pageType). Two of the six cannot be resolved to a price history at
        all -- a mutual fund lives in a separate namespace at ``/mutual-fund/nav/{id}/`` and carries
        NAV rather than OHLC, and a ``Not Traded`` line is delisted or suspended. Handing either
        back with bars would be a **wrong** answer rather than a thin one, so
        :attr:`TrendlyneSymbol.category` is set and :meth:`SymbolCategory.has_ohlc` is exposed for
        the caller to branch on. Dropping them from the list instead would be wrong in a different
        way: a 20-row ranked list with 9 rows missing looks like an exhaustive answer, and only the
        caller knows whether they wanted a fund. ``Superstars`` and any string outside the six map
        to :attr:`SymbolCategory.OTHER` and are reported as unknown rather than guessed into
        ``equity``.

        **This is not wired into :meth:`resolve_any_async`, deliberately.** That route is fuzzy and
        ranked -- ``"nifty bees"`` returns five BEES ETFs with BANKBEES first and NIFTYBEES fifth --
        and resolving to the first hit is what made ``ohlc("nifty bees")`` return a different
        security's bars with nothing to notice it by (commit ``06f6c7c``). This method returns up to
        20 ranked candidates for a human to choose from, and the plain route already resolves a
        ticker correctly; feeding the fuzzy one into resolution would recreate that defect. Use it
        to *discover* what exists, then resolve deliberately.

        Args:
            query: Symbol or company-name fragment, as for :meth:`search_remote_async`.
            limit: Maximum records to return. The vendor's order is preserved, because the ranking
                is the vendor's only claim about which candidate is most likely meant, and
                re-sorting it here would assert a preference this package has not measured.
            allow_member_search: Per-call override of :data:`MEMBER_SEARCH_FLAG`.
            allow_full_search: Per-call override of :data:`FULL_SEARCH_FLAG`. **Both** this and
                ``allow_member_search`` are required.

        Returns:
            Categorised records, best match first. Empty when trendlyne has no match, which is an
            expected outcome of a fuzzy search rather than an error.

        Raises:
            TrendlyneRobotsDisallowedError: Either gate is closed. The message names the specific
                missing flag by its constant name and the disallowed path with its covering rule.
                Raised **before** the request, so a refusal costs zero requests.
            TrendlyneSchemaError: The response was neither a list nor an object carrying one under
                ``data``/``results``.
            ValueError: ``query`` is empty.
        """
        clean = (query or "").strip()
        if not clean:
            raise ValueError("query must be a non-empty string")
        self._require_member_search(
            allow_member_search, full=True, allow_full=allow_full_search
        )

        path = _SYMBOL_SEARCH_FULL_PATH.format(query=quote_plus(clean))
        payload = await self.fetch_json_async(path, xhr=True)
        # The top level of this route is a BARE LIST, measured, while the plain route can answer
        # with an object. Both are handled, the same way search_remote_async handles them, rather
        # than one shape being assumed correct.
        if isinstance(payload, dict):
            payload = payload.get("data") or payload.get("results") or []
        if not isinstance(payload, list):
            raise TrendlyneSchemaError(
                f"full symbol search returned {type(payload).__name__}, expected a list. "
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
            vendor_category = str(item.get("category") or "").strip()
            out.append(
                TrendlyneSymbol(
                    symbol=symbol,
                    stock_id=stock_id,
                    slug=slug,
                    name=_display_name(
                        str(item.get("label") or ""), symbol, slug
                    ),
                    category=parse_symbol_category(vendor_category),
                    vendor_category=vendor_category,
                )
            )
            if len(out) >= max(1, int(limit)):
                break
        return out

    def search_full(
        self,
        query: str,
        *,
        limit: int = 10,
        allow_member_search: Optional[bool] = None,
        allow_full_search: Optional[bool] = None,
    ) -> List[TrendlyneSymbol]:
        """Sync variant of :meth:`search_full_async`."""
        return _run_async(
            self.search_full_async(
                query,
                limit=limit,
                allow_member_search=allow_member_search,
                allow_full_search=allow_full_search,
            )
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
                f"({ROBOTS_DISALLOWED_ROUTES[disallowed]}), and this client has the route "
                f"closed. {symbol!r} may well exist on trendlyne: the sitemap does not carry "
                f"it, and it is exactly the instruments the sitemap omits - every ETF - that "
                f"the disallowed route would have resolved. {MEMBER_SEARCH_FLAG} **defaults to "
                f"True**, so reaching this refusal means the strict setting was passed "
                f"deliberately; drop the argument to reach those instruments, at the cost of "
                f"requesting a path the site asks crawlers not to fetch. Mutual funds are a "
                f"separate namespace (/mutual-fund/nav/{{id}}/) and stay unreachable even with "
                f"the route open."
            )

        matches = await self.search_remote_async(symbol, limit=5, allow_member_search=True)
        if matches:
            # trendlyne's search is FUZZY and RANKED, so `matches[0]` is not an answer to the
            # question that was asked -- it is the vendor's best guess. Taking it blind is
            # how `nifty bees` came to return BANKBEES: the spaced query matches five BEES
            # ETFs and the vendor ranks BANKBEES first, while the instrument the user meant,
            # NIFTYBEES, is fifth in the same list. A plausible-looking frame for the wrong
            # instrument is the worst outcome this package can produce, because nothing about
            # it looks wrong downstream.
            #
            # So a hit is accepted only when it IS the thing asked for, judged after
            # normalisation. `nifty bees` -> NIFTYBEES is defensible; `nifty bees` ->
            # BANKBEES is not, and no ranking can make it so.
            wanted = _normalise_symbol_query(symbol)
            exact = [m for m in matches if _normalise_symbol_query(m.symbol) == wanted]
            if len(exact) == 1:
                self.symbol_resolution = "member_search"
                return exact[0]
            offered = ", ".join(f"{m.symbol} ({m.stock_id})" for m in matches)
            self.symbol_resolution = "member_search_ambiguous"
            raise TrendlyneSymbolNotFoundError(
                f"{symbol!r} is not in trendlyne's equity sitemap ({self.index_size} entries), "
                f"and trendlyne's own search returned {len(matches)} candidate(s) of which NONE is "
                f"the instrument asked for: {offered}. Choosing one of those would hand you a "
                f"different security's prices with no error, so this package refuses instead. "
                f"If one of them is what you meant, pass its exact ticker. A bare prefix such as "
                f"{symbol!r} is not enough to pick between them."
            )
        self.symbol_resolution = "sitemap"
        raise TrendlyneSymbolNotFoundError(
            f"{symbol!r} resolved by neither route: not in the equity sitemap "
            f"({self.index_size} entries) and trendlyne's own search returned no match, which is "
            f"what a misspelling looks like -- this search is not typo-tolerant. "
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

        **The six symbol-page sections ride along the same way**, in their own cache entry — see
        :meth:`stock_page_snapshot_async`. They are parsed out of this page too, so they also cost
        no request; the measured price is one shared soup build plus the 66 KB estimate JSON, about
        70 ms once per cold symbol, against the ~1.3 s the fetch itself takes.

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
        # The sections come out of these very bytes, so they are parsed now and cached beside the
        # token: a later :meth:`stock_page_snapshot_async` then costs no request at all. Measured
        # cost of the batch parse on the recorded 294 KB page is ~100 ms once per cold symbol
        # (one soup build shared by six parsers, plus the 66 KB consensus JSON), against ~1.3 s
        # for the fetch itself.
        snapshot = _stock_page_snapshot(
            html,
            symbol=record.symbol,
            source_url=self._absolute(record.page_path),
            retrieved_at=datetime.now(timezone.utc),
        )
        with self._state_lock:
            self._tokens[record.stock_id] = (time.monotonic(), token, classification)
            self._page_snapshots[record.stock_id] = (time.monotonic(), snapshot)
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
        listing_date: Any = None,
    ) -> pd.DataFrame:
        """Daily OHLCV, as-traded and split/bonus-adjusted. Two requests the first time, one after.

        Args:
            symbol: Anything :meth:`resolve_async` accepts.
            include_last_week: Merge ``eodDataLastWeek`` — the five most recent sessions, which
                the main ``eodData`` key does not contain. See the module docstring.
            listing_date: The security's listing date, as ``'YYYY-MM-DD'``, a ``date`` or a
                ``Timestamp``. **The caller supplies it; this package does not look it up** -- the
                OHLC payload carries no listing date and this route has no other source for one.
                Passing it publishes ``attrs["bfinance_trendlyne_rows_before_listing"]`` and its
                volume companion; the bars themselves are never dropped. Without it both read
                ``-1``, meaning *not measured*, which is not the same as zero.

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
            ValueError: ``listing_date`` is neither a date nor ``None``.
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
                listing_date=listing_date,
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
                listing_date=listing_date,
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

    def ohlc(
        self,
        symbol: str,
        *,
        include_last_week: bool = True,
        listing_date: Any = None,
    ) -> pd.DataFrame:
        """Sync variant of :meth:`ohlc_async`."""
        return _run_async(
            self.ohlc_async(
                symbol,
                include_last_week=include_last_week,
                listing_date=listing_date,
            )
        )

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

    # ------------------------------------------------------- section pages
    #
    # One request each, on purpose. The symbol resolves through the cached sitemap index and the
    # section route needs nothing else — no stock page, no OHLC token — so the cold cost of a
    # section call is one request over the sitemap and one over the section, and one after that.
    # With fourteen routes now reachable, a caller wanting three of them pays three requests, and
    # the WAF budget above is the reason that number is worth pinning (tests assert it per method).

    def _section_note(self, page: str, what: str, requests: int = 1) -> str:
        """The provenance note every section frame carries. One sentence of what it is, one of what
        it is not, because a frame that only says where it came from invites a caller to read the
        absence of something as an absence in the data.

        Args:
            page: The route, as the vendor spells it.
            what: What the frame is.
            requests: How many requests the call actually cost, **measured** rather than assumed.
                The old wording hard-coded "fetched in one request", which is true for a warm
                sitemap and false three times over for a cold index on a symbol the sitemap omits:
                the sitemap fetch, the member-search fetch, and the section itself. Those extra
                requests go to ``/member/api/ac_snames/`` — the robots-gated route — so a caller
                who is counting their budget needs the real number. See
                :attr:`TrendlyneClient.request_count`.
        """
        detail = (
            "one request"
            if requests == 1
            else f"{requests} requests ({requests - 1} of them resolving the symbol)"
        )
        return (
            f"{what} from trendlyne.com's {page} page, fetched in {detail}. This is a snapshot: "
            f"one page, one snapshot, and no period axis to walk backwards over. A frame that "
            f"comes back empty means the page carried no such rows for this symbol, not that "
            f"this package failed to look."
        )

    async def corporate_actions_async(self, symbol: str) -> Dict[str, pd.DataFrame]:
        """Bonus, dividend and every other corporate action, in one page fetch.

        Args:
            symbol: Anything :meth:`resolve_async` accepts.

        Returns:
            ``{"bonus": frame, "dividend": frame, "other": frame}`` — always all three keys. See
            :func:`parse_corporate_actions` for why the third key is a bucket rather than a fourth
            section, and for what the ``bfinance_trendlyne_section`` column is for.

        Raises:
            TrendlyneSymbolNotFoundError: The symbol resolves by neither route.
        """
        record = await self.resolve_any_async(symbol)
        path = build_path("corporate_actions_page", record)
        html = await self._get_text(path)
        return parse_corporate_actions(
            html,
            symbol=record.symbol,
            source_url=self._absolute(path),
            retrieved_at=datetime.now(timezone.utc),
        )

    def corporate_actions(self, symbol: str) -> Dict[str, pd.DataFrame]:
        """Sync variant of :meth:`corporate_actions_async`."""
        return _run_async(self.corporate_actions_async(symbol))

    async def bulk_block_deals_async(self, symbol: str) -> pd.DataFrame:
        """Bulk and block deals, as the ``deals`` table.

        One request. The live page carries a decade or more in a single response — 82 rows on
        MOTHERSON (the recorded fixture keeps the first 40) and 182 on TCS — which is why this
        route is **not** snapshot-guarded: there is no period to ask for, the page is the history.

        Returns:
            A frame whose ``attrs["bfinance_trendlyne_table"]`` is ``"deals"``.
        """
        # Counted from *before* the resolve: a cold index on a symbol the sitemap omits costs three
        # requests here, two of them on the robots-gated /member/ route, and the note has to say so
        # rather than claim one.
        before = self._request_count
        record = await self.resolve_any_async(symbol)
        path = build_path("bulk_block_deals_page", record)
        html = await self._get_text(path)
        spent = self._request_count - before
        frame = find_table(html, "deals", symbol=record.symbol)
        frame.attrs.update(
            build_attrs(
                record.symbol,
                source=self._absolute(path),
                snapshot_only=False,
                note=self._section_note(
                    "/equity/bulk-block-deals/", "Bulk and block deals", spent
                ),
                retrieved_at=datetime.now(timezone.utc),
                rows=len(frame),
                requests=spent,
            )
        )
        return frame

    def bulk_block_deals(self, symbol: str) -> pd.DataFrame:
        """Sync variant of :meth:`bulk_block_deals_async`."""
        return _run_async(self.bulk_block_deals_async(symbol))

    async def _share_holding_html_async(
        self, record: TrendlyneSymbol, period: str
    ) -> Tuple[str, str]:
        """The one share-holding fetch, behind the snapshot-only guard.

        The guard lives here rather than in :func:`build_path` because it is a property of the
        route and not of the path string: measured 2026-09-27 on MOTHERSON and 2026-10-10 on TCS,
        ``/equity/share-holding/…`` answers **HTTP 500** for every period segment but ``latest``.
        Spending a request (or three, with the retry) to be told that is the thing this package
        exists not to do, so the refusal is local, costs zero requests, and says why.

        The mutual-fund route is the measured counter-example and deliberately has none of this:
        there the period segment is a filter that works.

        Returns:
            ``(html, path)``. The path comes back with the body because the callers stamp it into
            ``attrs`` and the page's own URL is the provenance — not the stock page this method
            never fetched.
        """
        checked = validate_period(period)
        if checked != SNAPSHOT_PERIOD:
            raise TrendlyneServerError(
                f"the trendlyne share-holding route serves only the period "
                f"{SNAPSHOT_PERIOD!r}, not {checked!r}. Measured: /equity/share-holding/1372/TCS/"
                f"Jun-2026/... returns HTTP 500 (re-verified 2026-10-10; the same period on "
                f"monthly-mutual-fund-share-holding returns 200 and filters), as do /2024/ and "
                f"/2023/. This was refused locally, so no request was spent. Use "
                f"monthly_mf_holdings_async() for a period-filtered holding table, or read the "
                f"multi-period grid the single 'latest' page already embeds via share_holding()",
                status_code=500,
            )
        path = build_path("share_holding_page", record, period=SNAPSHOT_PERIOD)
        return await self._get_text(path), path

    async def share_holding_async(self, symbol: str, period: str = SNAPSHOT_PERIOD) -> pd.DataFrame:
        """The share-holding **summary grid**: holder categories down, quarters across.

        Args:
            symbol: Anything :meth:`resolve_async` accepts.
            period: Must be ``latest``. Any other value is refused **before** the request — see
                :meth:`_share_holding_html_async` for the measurement. Note the contrast with
                :meth:`monthly_mf_holdings_async`, where a period really does filter.

        Returns:
            One frame, carrying the ``latest`` page's summary grid. On the recorded TCS capture
            that is 13 columns — a ``Row Type`` marker plus twelve quarters, Jun 2026 to Sep 2023 —
            against **12 rows**: the five top-level categories (Promoter, FII, DII, Public, Others)
            and the seven sub-rows the page renders collapsed beneath them (Promoter's Holding /
            Pledged / Locked and DII's Mutual Funds / Banks / Insurance / Others). The frame is
            indexed by the vendor's own row label, so ``frame.loc["Pledged", "Jun 2026"]`` reads a
            cell directly. On MOTHERSON the same page carries 44 distinct quarter labels. That grid
            is why one ``latest`` request is worth having even though the route cannot be walked.

        Note:
            **The page hides its sub-category rows with ``style="display:none"``, and this frame
            carries them anyway.** They used not to: ``pandas.read_html`` honours the CSS
            ``display`` property by default, so 8 of the grid's 13 rows — including ``Pledged``,
            which is a distinct holding figure and not a restatement of the promoter's own — were
            silently dropped. The extractor now reads this one table with
            ``displayed_only=False``; every other table this package reads is unaffected. Because
            the rows are now visible, the frame says which is which: ``frame["Row Type"]`` reads
            ``"category"`` for the five top-level rows and ``"subgroup"`` for the seven beneath
            them. Filter on it — the index is deliberately not unique, since ``"Others"`` is both a
            DII sub-row and a top-level category, and this package does not invent a label to make
            it so.

        Note:
            ``attrs["bfinance_trendlyne_snapshot_only"]`` is **True** here, and it is the only
            section frame that carries it: this is the one route whose answer is a single
            unaddressable point in time.
        """
        record = await self.resolve_any_async(symbol)
        html, path = await self._share_holding_html_async(record, period)
        tables = share_holding_tables(html)
        frame = tables.summary
        frame.attrs.update(
            build_attrs(
                record.symbol,
                source=self._absolute(path),
                snapshot_only=True,
                note="Share-holding summary grid (holder categories down, quarters across) from "
                     "trendlyne's /equity/share-holding/ page. This route serves ONLY the period "
                     "'latest' -- every other segment answers HTTP 500 -- so this frame is one "
                     "unaddressable snapshot and is flagged snapshot_only. It is still worth one "
                     "request: the page embeds every quarter it holds. The page's own sub-category "
                     "rows are style='display:none' and pandas.read_html skips what the page hides "
                     "by default, so this table is read with displayed_only=False and the frame "
                     "carries them too -- including 'Pledged', a distinct holding figure. The "
                     "index is the vendor's own row label and the 'Row Type' column reads "
                     "'category' or 'subgroup'; the index is not unique ('Others' is both a DII "
                     "sub-row and a top-level category) and no label was invented to make it so. "
                     "Use share_holding_holders_async() for the per-category holder tables and "
                     "monthly_mf_holdings_async() for a period that actually filters.",
                retrieved_at=datetime.now(timezone.utc),
                rows=len(frame),
                holder_categories=list(tables.categories),
            )
        )
        return frame

    def share_holding(self, symbol: str, period: str = SNAPSHOT_PERIOD) -> pd.DataFrame:
        """Sync variant of :meth:`share_holding_async`."""
        return _run_async(self.share_holding_async(symbol, period=period))

    async def share_holding_holders_async(self, symbol: str) -> List[pd.DataFrame]:
        """The per-category holder tables from the same ``latest`` page.

        Args:
            symbol: Anything :meth:`resolve_async` accepts. No ``period`` argument, deliberately:
                the route does not have one (see :meth:`share_holding_async`), and a parameter that
                can only take a single value is a way of inviting a caller to pass another.

        Returns:
            One frame per category in page order — three on the recorded page (promoter,
            institutional, public non-institutional) — each carrying
            ``attrs["bfinance_trendlyne_holder_category"]``. An unnamed section gets ``None`` there
            rather than a guessed category.

        Note:
            This is a **separate request** from :meth:`share_holding_async`, not a shared one: two
            callers asking for the summary and the holders pay two requests rather than one method
            guessing which of the two they wanted. Each frame is stamped ``snapshot_only`` for the
            same reason the summary grid is.
        """
        record = await self.resolve_any_async(symbol)
        html, path = await self._share_holding_html_async(record, SNAPSHOT_PERIOD)
        tables = share_holding_tables(html)
        retrieved_at = datetime.now(timezone.utc)
        holders: List[pd.DataFrame] = []
        for frame, category in zip(tables.holders, tables.categories):
            frame.attrs.update(
                build_attrs(
                    record.symbol,
                    source=self._absolute(path),
                    snapshot_only=True,
                    note=f"The {category or '<unnamed section>'} holder table from trendlyne's "
                         f"share-holding page: {len(frame)} holder(s). The route serves only the "
                         f"period 'latest', so this is one unaddressable snapshot. The category "
                         f"comes from the tab label of the section wrapper, because all three "
                         f"tables repeat one header row verbatim; an unnamed section reads None "
                         f"here rather than a guess.",
                    retrieved_at=retrieved_at,
                    rows=len(frame),
                    holder_category=category,
                )
            )
            holders.append(frame)
        return holders

    def share_holding_holders(self, symbol: str) -> List[pd.DataFrame]:
        """Sync variant of :meth:`share_holding_holders_async`."""
        return _run_async(self.share_holding_holders_async(symbol))

    async def board_meetings_async(self, symbol: str) -> pd.DataFrame:
        """Scheduled and completed board meetings, as the page's own table.

        Returns:
            One frame with the vendor's ``Ex-date`` / ``Purpose`` / ``Notes`` columns, in the order
            the page serves them — which is **not** chronological: the recorded TCS capture leads
            with a forward-dated 08 Oct 2026 quarterly result and runs back years. Sorting is left
            to the caller because the page's order is the vendor's only claim about what it
            considers current, and re-sorting it here would assert a preference nobody measured.
        """
        before = self._request_count
        record = await self.resolve_any_async(symbol)
        path = build_path("board_meeting_page", record)
        html = await self._get_text(path)
        spent = self._request_count - before
        frame = find_table(html, "board_meeting", symbol=record.symbol)
        frame.attrs.update(
            build_attrs(
                record.symbol,
                source=self._absolute(path),
                snapshot_only=False,
                note=self._section_note(
                    "/equity/board-meeting/", "Board meetings (ex-date, purpose, notes)", spent
                ) + " Rows are in the page's own order, which leads with forward-dated meetings "
                  "and is not chronological; nothing is re-sorted.",
                retrieved_at=datetime.now(timezone.utc),
                rows=len(frame),
                requests=spent,
            )
        )
        return frame

    def board_meetings(self, symbol: str) -> pd.DataFrame:
        """Sync variant of :meth:`board_meetings_async`."""
        return _run_async(self.board_meetings_async(symbol))

    async def research_reports_async(self, symbol: str, page: int = 1) -> pd.DataFrame:
        """Broker research reports, as the page's one table.

        Args:
            symbol: Anything :meth:`resolve_async` accepts.
            page: Page number. ``1`` is the page's own first page and carries no query string;
                anything higher is sent as ``?page=N``.

        Returns:
            One frame carrying the vendor's own columns — ``Summary``, ``Date``, ``Stock``,
            ``Author``, ``LTP``, ``Target``, ``Price at reco (Change since reco%)``, ``Upside(%)``,
            ``Type``, ``Report``, ``Discuss``. The recorded TCS capture holds 21 data rows, the
            first of which is the vendor's **aggregate** "Consensus Share Price Target" row, so a
            consumer deriving a consensus must not average the column and also read that row.

        Note:
            The aggregate row is called out rather than filtered: removing it would be this package
            deciding what the caller wanted, and it is the row that carries the consensus.
        """
        before = self._request_count
        record = await self.resolve_any_async(symbol)
        path = build_path("research_reports_page", record)
        path = _paged(path, page)
        html = await self._get_text(path)
        spent = self._request_count - before
        frame = find_table(html, "research_report", symbol=record.symbol)
        frame.attrs.update(
            build_attrs(
                record.symbol,
                source=self._absolute(path),
                snapshot_only=False,
                note=self._section_note(
                    "/research-reports/stock/", "Broker research reports", spent
                ) + f" This frame is page {page} only. The vendor's aggregate 'Consensus Share "
                  f"Price Target' row is kept, not filtered: it is the consensus, and a consumer "
                  f"that also averages the Target column must exclude it deliberately.",
                retrieved_at=datetime.now(timezone.utc),
                rows=len(frame),
                page=page,
                requests=spent,
            )
        )
        return frame

    def research_reports(self, symbol: str, page: int = 1) -> pd.DataFrame:
        """Sync variant of :meth:`research_reports_async`."""
        return _run_async(self.research_reports_async(symbol, page=page))

    async def latest_news_async(self, symbol: str, page: int = 1) -> pd.DataFrame:
        """The symbol's news list, as the page renders it.

        Args:
            symbol: Anything :meth:`resolve_async` accepts.
            page: Page number; ``1`` is the bare page and anything higher is sent as ``?page=N``.
                ``?page=2`` was measured answering 200 on 2026-10-10.

        Returns:
            Columns ``date``, ``kind``, ``title``, ``url``, ``source``. See
            :func:`parse_latest_news` for what is carried verbatim and why — the short version is
            that the date string is the vendor's own (three formats on one page) and a
            login-modal ``url`` is the vendor's own paywall, reported rather than rewritten.
        """
        record = await self.resolve_any_async(symbol)
        path = build_path("latest_news_page", record)
        path = _paged(path, page)
        html = await self._get_text(path)
        frame = parse_latest_news(
            html,
            symbol=record.symbol,
            source_url=self._absolute(path),
            retrieved_at=datetime.now(timezone.utc),
        )
        frame.attrs["bfinance_trendlyne_page"] = page
        return frame

    def latest_news(self, symbol: str, page: int = 1) -> pd.DataFrame:
        """Sync variant of :meth:`latest_news_async`."""
        return _run_async(self.latest_news_async(symbol, page=page))

    async def stock_alerts_async(self, symbol: str) -> pd.DataFrame:
        """The symbol's alert history — research-report alerts, SMA crossovers, price thresholds.

        Returns:
            Columns ``date``, ``kind``, ``headline``, ``detail``, ``values``; see
            :func:`parse_stock_alerts` for what each is, including why ``values`` is a dict.

        Note:
            Reading this list needs no member account. Creating an alert does, and the two write
            routes that do (``/alerts/addStockAlert/``, ``/alerts/modal/stockAlert/``) are the two
            this site's robots.txt disallows in this namespace — neither is a route this package
            builds.
        """
        record = await self.resolve_any_async(symbol)
        path = build_path("stock_alerts_page", record)
        html = await self._get_text(path)
        return parse_stock_alerts(
            html,
            symbol=record.symbol,
            source_url=self._absolute(path),
            retrieved_at=datetime.now(timezone.utc),
        )

    def stock_alerts(self, symbol: str) -> pd.DataFrame:
        """Sync variant of :meth:`stock_alerts_async`."""
        return _run_async(self.stock_alerts_async(symbol))

    async def insider_trading_async(self, symbol: str) -> pd.DataFrame:
        """Insider and SAST trades, as the page's own table.

        The route's ``all`` list is long and **capped server-side at exactly
        :data:`INSIDER_ROW_CAP` rows**, so this method publishes the row count and, when the count
        equals the cap, a note saying the cap was hit and the frame is therefore not the full
        history. A frame below the cap is never described as complete: the recorded TCS capture
        holds 174 data rows and nothing measured says that is all of them.

        Returns:
            One frame with the vendor's ``Client Name`` / ``Client Category`` / ``Action*`` /
            ``Reported To/By Exchange`` / ``Quantity`` / ``Post Transaction Holding`` / ``Traded %``
            / ``Avg. Price`` / ``Value`` / ``Period`` columns. It is **not** the ``deals`` table and
            is not read by that signature — there is no ``Deal Type`` column here, and borrowing the
            deals header would classify an insider frame as a bulk-deal frame with no error.
        """
        record = await self.resolve_any_async(symbol)
        path = build_path("insider_trading_page", record)
        html = await self._get_text(path)
        frame = find_table(html, "insider_trade", symbol=record.symbol)
        rows = len(frame)
        capped = rows >= INSIDER_ROW_CAP
        note = (
            f"Insider / SAST trades from trendlyne.com's insider-trading-sast page: {rows} row(s), "
            f"served in a single page with no period axis. The observed server-side cap on this "
            f"route is exactly {INSIDER_ROW_CAP} rows."
        )
        if capped:
            note += (
                " This frame holds exactly that many, so it is TRUNCATED and is NOT the full "
                "history; attrs['bfinance_trendlyne_insider_rows'] is the number to read before "
                "treating it as one."
            )
        else:
            note += (
                " This frame is below the cap, which does NOT mean it is complete -- only that it "
                "was not cut at the cap. Nothing measured says what the vendor holds beyond "
                "these rows."
            )
        frame.attrs.update(
            build_attrs(
                record.symbol,
                source=self._absolute(path),
                snapshot_only=False,
                note=note,
                retrieved_at=datetime.now(timezone.utc),
                insider_rows=rows,
                insider_row_cap=INSIDER_ROW_CAP,
                insider_rows_capped=capped,
            )
        )
        return frame

    def insider_trading(self, symbol: str) -> pd.DataFrame:
        """Sync variant of :meth:`insider_trading_async`."""
        return _run_async(self.insider_trading_async(symbol))

    async def monthly_mf_holdings_async(self, symbol: str,
                                        period: str = SNAPSHOT_PERIOD) -> pd.DataFrame:
        """Mutual funds' monthly holding of one stock, on a three-month ragged grid.

        Args:
            symbol: Anything :meth:`resolve_async` accepts.
            period: ``latest``, a year (``2024``) or a month (``Jun-2024``) — validated by
                :func:`validate_period`, so a malformed segment costs zero requests. **This route
                genuinely filters by period**: measured on TCS 2026-10-10, ``Jun-2026`` answers 200
                with 1,577,098 bytes and ``2026`` answers 200 with 1,402,262. It is therefore the
                deliberate counter-example to :meth:`share_holding_async`, which must not carry this
                guard.

        Returns:
            One row per fund. The grid is **ragged** — 26 of the 30 rows on the recorded capture
            carry ``-`` for a month in which that fund held nothing — so no month column may be
            coerced to numeric, and the extractor's all-or-nothing rule keeps every one verbatim.
        """
        record = await self.resolve_any_async(symbol)
        checked = validate_period(period)
        path = build_path("monthly_mf_holding_page", record, period=checked)
        html = await self._get_text(path)
        frame = monthly_mf_holding_table(html)
        frame.attrs.update(
            build_attrs(
                record.symbol,
                source=self._absolute(path),
                snapshot_only=False,
                note=f"Mutual-fund holdings of {record.symbol} for period {checked!r}, from "
                     f"trendlyne's /equity/monthly-mutual-fund-share-holding/ page. The period "
                     f"segment on THIS route filters (verified 2026-10-10: 'Jun-2026' and '2026' "
                     f"both answer 200 with real data), which is why it carries no snapshot guard "
                     f"while share_holding_async does. The grid is ragged -- a '-' is a month in "
                     f"which that fund held nothing, not a missing measurement -- so no month "
                     f"column is coerced to numeric.",
                retrieved_at=datetime.now(timezone.utc),
                rows=len(frame),
                period=checked,
            )
        )
        return frame

    def monthly_mf_holdings(self, symbol: str, period: str = SNAPSHOT_PERIOD) -> pd.DataFrame:
        """Sync variant of :meth:`monthly_mf_holdings_async`."""
        return _run_async(self.monthly_mf_holdings_async(symbol, period=period))

    async def buy_sell_zone_async(self, symbol: str) -> Dict[str, pd.DataFrame]:
        """The PE-band grid on both bases: standalone and consolidated.

        Returns:
            ``{"standalone": frame, "consolidated": frame}``. The two grids carry **byte-identical
            header rows** on the live page, so nothing but the enclosing ``div.tab-pane`` tells them
            apart — and their numbers differ (recorded TCS: current PE 14.9 vs 15.1, Strong Buy
            Zone 1.0% vs 1.8%). A swap would be invisible, which is why the key is the pane id.

            Each frame carries the page's summary statistics in ``attrs`` — the strong-buy-zone
            share, current / average / median PE and the raw tile text for the forward trio, all
            read off the tile beside each grid. They are ``None`` when the page shows no such tile,
            which on the recorded capture is the measured answer for one of them.
        """
        record = await self.resolve_any_async(symbol)
        path = build_path("buy_sell_zone_page", record)
        html = await self._get_text(path)
        retrieved_at = datetime.now(timezone.utc)
        results: Dict[str, pd.DataFrame] = {}
        for basis, table in buy_sell_zone_tables(html).items():
            frame = table.frame
            frame.attrs.update(
                build_attrs(
                    record.symbol,
                    source=self._absolute(path),
                    snapshot_only=False,
                    note=f"Buy-sell-zone PE-band grid on the {basis} basis, from trendlyne's "
                         f"/tools/buy-sell-zone/ page: {len(frame)} band row(s). The standalone "
                         f"and consolidated grids carry byte-identical header rows, so this frame "
                         f"is keyed by its div.tab-pane id ({basis!r}) and not by its headers. "
                         f"The page's summary statistics (strong buy zone, current / average / "
                         f"median PE and the raw tile text) are in attrs; an attr reads None when "
                         f"the page shows no such tile.",
                    retrieved_at=retrieved_at,
                    basis=basis,
                    rows=len(frame),
                )
            )
            results[basis] = frame
        return results

    def buy_sell_zone(self, symbol: str) -> Dict[str, pd.DataFrame]:
        """Sync variant of :meth:`buy_sell_zone_async`."""
        return _run_async(self.buy_sell_zone_async(symbol))

    async def swot_async(self, symbol: str) -> Dict[str, pd.DataFrame]:
        """The SWOT widget's fact tables, keyed by their tab pane.

        Returns:
            ``{"tag_strengths": frame, "tag_weakness": frame, "tag_opportunity": frame,
            "tag_threats": frame, "tag_others": frame}`` on the recorded page — five single-column
            tables whose header cell *is* the fact. Each frame publishes that heading in
            ``attrs["bfinance_trendlyne_swot_heading"]``.

        Note:
            The ``tag_others`` pane is not a fourth quadrant. It is trendlyne's own extra tab
            (on the recorded capture, a subscription-gated screen) and it is published under its own
            name rather than folded into "threats" or dropped.
        """
        record = await self.resolve_any_async(symbol)
        path = build_path("swot_page", record)
        html = await self._get_text(path)
        return parse_swot_tables(
            html,
            symbol=record.symbol,
            source_url=self._absolute(path),
            retrieved_at=datetime.now(timezone.utc),
        )

    def swot(self, symbol: str) -> Dict[str, pd.DataFrame]:
        """Sync variant of :meth:`swot_async`."""
        return _run_async(self.swot_async(symbol))

    # ------------------------------------------------- JSON sibling routes
    #
    # Three routes that end in `?format=json`, all of which answer the standard trendlyne envelope.
    # `parse_envelope` is what decides success here: head.status == "0", never the HTTP status,
    # because a failed request on these routes arrives as a 200 with an empty body.

    async def adv_technical_analysis_async(
        self, symbol: str, *, _record: Optional[TrendlyneSymbol] = None
    ) -> Dict[str, Any]:
        """The advanced technical analysis, as the vendor's own parameter mapping.

        This is the robots-clean sibling of the ``/equity/technical-analysis-*`` **page**, which
        ``robots.txt`` disallows: the rule needs that literal substring and this path has
        ``/equity/api/stock/`` in front of it. The page route stays unwired.

        Args:
            symbol: Anything :meth:`resolve_async` accepts.
            _record: An **already-resolved** record, passed down by :meth:`technical_signals_async`
                rather than resolved again. ``None`` (the default, and the only thing a caller
                ever passes) resolves here as before. See
                :meth:`technical_signals_async` for why the second resolve was not free.

        Returns:
            ``body["parameters"]`` **as sent** — 56 top-level keys on the recorded TCS capture,
            nested differently by key (a record, a list of records, a dict of records). Nothing is
            flattened or renamed here; :meth:`technical_signals_async` is the tidy view of the
            same request and :func:`parse_technical_signals` says exactly what it leaves out.

        Raises:
            TrendlyneNotAuthorizedError: ``head.status != "0"`` — which arrives as HTTP 200.
            TrendlyneSchemaError: The envelope carried no ``parameters``.
            TrendlyneWafChallengeError: The edge challenged the session. Terminal.
        """
        record = _record if _record is not None else await self.resolve_any_async(symbol)
        path = build_path("adv_technical_analysis", record)
        payload = await self._get_json(path, referer=record.page_url)
        body = parse_envelope(
            payload, context=f"the technical-analysis payload for {record.symbol}"
        )
        if "parameters" not in body:
            raise TrendlyneSchemaError(
                f"the technical-analysis payload for {record.symbol} has no 'parameters'. Body "
                f"keys: {sorted(body)[:12]}. Upstream schema may have changed."
            )
        return body["parameters"]

    def adv_technical_analysis(self, symbol: str) -> Dict[str, Any]:
        """Sync variant of :meth:`adv_technical_analysis_async`."""
        return _run_async(self.adv_technical_analysis_async(symbol))

    async def technical_signals_async(self, symbol: str) -> pd.DataFrame:
        """The same request as :meth:`adv_technical_analysis_async`, as a tidy frame.

        One request, columns ``name``, ``value``, ``insight``, ``color``, ``parameter``. Use this
        to read indicators and the raw method to read everything else the payload carries.

        Note:
            **The symbol is resolved once, here.** This used to resolve it and then call
            :meth:`adv_technical_analysis_async`, which resolved it *again*. For a symbol the
            sitemap carries the second resolve is a cache hit and costs nothing — which is why the
            request-count tests never saw it. For a symbol the sitemap **omits**, which is every
            ETF, the second resolve re-requests ``/member/api/ac_snames/``: the one route this
            package's robots gate exists for. Measured: ``NIFTYBEES`` cost two member requests
            where one would do. So the resolved record is handed down instead of re-derived.
        """
        record = await self.resolve_any_async(symbol)
        parameters = await self.adv_technical_analysis_async(record.symbol, _record=record)
        return parse_technical_signals(
            parameters,
            symbol=record.symbol,
            source_url=self._absolute(build_path("adv_technical_analysis", record)),
            retrieved_at=datetime.now(timezone.utc),
        )

    def technical_signals(self, symbol: str) -> pd.DataFrame:
        """Sync variant of :meth:`technical_signals_async`."""
        return _run_async(self.technical_signals_async(symbol))

    async def overview_second_part_async(
        self, symbol: str, *, _record: Optional[TrendlyneSymbol] = None
    ) -> Dict[str, Any]:
        """The company and corporate-events JSON behind ``/equity/about/``.

        The about page itself renders client-side and carries **zero** server-rendered tables;
        this route is where its content lives. Measured on TCS 2026-10-10: nine body keys —
        ``eventsData``, ``researchReports``, ``corporateFiling`` and four document lists,
        ``companyProfileData`` and ``faq``.

        Args:
            symbol: Anything :meth:`resolve_async` accepts.
            _record: An already-resolved record, handed down by :meth:`events_async` and
                :meth:`company_overview_async` so the symbol is resolved once per call rather than
                twice. ``None`` (the default) resolves here. See :meth:`technical_signals_async`
                for what the second resolve costs on a symbol the sitemap omits.

        Returns:
            The envelope's **body** mapping, verbatim, with ``head.status == "0"`` already checked
            by :func:`parse_envelope`. Not the whole envelope, and not a subset: the raw mapping is
            what a caller who wants ``faq`` or the document lists needs, and
            :meth:`events_async` / :meth:`company_overview_async` are shaped views of two of its
            keys.
        """
        record = _record if _record is not None else await self.resolve_any_async(symbol)
        path = build_path("overview_second_part", record)
        payload = await self._get_json(path, referer=record.page_url)
        return parse_envelope(
            payload, context=f"the overview-second-part payload for {record.symbol}"
        )

    def overview_second_part(self, symbol: str) -> Dict[str, Any]:
        """Sync variant of :meth:`overview_second_part_async`."""
        return _run_async(self.overview_second_part_async(symbol))

    async def events_async(self, symbol: str) -> Dict[str, pd.DataFrame]:
        """Board meetings, dividends, bonuses, splits and rights, as five frames.

        One request. Each frame's columns are its block's own ``unique_name`` keys, and a block the
        symbol has none of (TCS has neither a split nor a rights issue on the recorded capture)
        comes back as an **empty frame with its columns**, not as a missing key.
        """
        record = await self.resolve_any_async(symbol)
        body = await self.overview_second_part_async(record.symbol, _record=record)
        return parse_event_tables(
            body.get("eventsData"),
            symbol=record.symbol,
            source_url=self._absolute(build_path("overview_second_part", record)),
            retrieved_at=datetime.now(timezone.utc),
        )

    def events(self, symbol: str) -> Dict[str, pd.DataFrame]:
        """Sync variant of :meth:`events_async`."""
        return _run_async(self.events_async(symbol))

    async def company_overview_async(self, symbol: str) -> pd.DataFrame:
        """Directors and top executives, with the business description in ``attrs``.

        One request. See :func:`parse_company_overview` for why the description is not a column and
        why a person holding both roles appears twice.
        """
        record = await self.resolve_any_async(symbol)
        body = await self.overview_second_part_async(record.symbol, _record=record)
        return parse_company_overview(
            body.get("companyProfileData"),
            symbol=record.symbol,
            source_url=self._absolute(build_path("overview_second_part", record)),
            retrieved_at=datetime.now(timezone.utc),
        )

    def company_overview(self, symbol: str) -> pd.DataFrame:
        """Sync variant of :meth:`company_overview_async`."""
        return _run_async(self.company_overview_async(symbol))

    async def price_performance_async(
        self, symbol: str, *, _record: Optional[TrendlyneSymbol] = None
    ) -> Dict[str, Any]:
        """Returns comparison, seasonality and daily bars, as the vendor's own body mapping.

        Args:
            symbol: Anything :meth:`resolve_async` accepts.
            _record: An already-resolved record, handed down by :meth:`returns_comparison_async` so
                the symbol is resolved once per call rather than twice. ``None`` (the default)
                resolves here. See :meth:`technical_signals_async` for what the second resolve
                costs on a symbol the sitemap omits.

        Returns:
            The envelope's **body**: ``lastModified``, ``returnsComparison``, ``returnsDeepDive``,
            ``returnsPatternData`` and ``dailyData`` on the recorded capture.
            :meth:`returns_comparison_async` is the shaped view of one of those five keys.

        Note:
            This route's ``head.statusDescription`` reads ``"success"`` in lower case where every
            other trendlyne route sends ``"Success"``. That is the concrete reason success is
            decided on ``head.status == "0"`` and never on the description string.
        """
        record = _record if _record is not None else await self.resolve_any_async(symbol)
        path = build_path("price_performance_analysis", record)
        payload = await self._get_json(path, referer=record.page_url)
        return parse_envelope(
            payload, context=f"the price-performance payload for {record.symbol}"
        )

    def price_performance(self, symbol: str) -> Dict[str, Any]:
        """Sync variant of :meth:`price_performance_async`."""
        return _run_async(self.price_performance_async(symbol))

    async def returns_comparison_async(self, symbol: str) -> pd.DataFrame:
        """Period-over-period returns against the vendor's comparators, as a frame.

        One request. Seven periods against five comparators on the recorded capture, with values
        left exactly as sent — the block mixes floats and strings in one column.
        """
        record = await self.resolve_any_async(symbol)
        body = await self.price_performance_async(record.symbol, _record=record)
        return parse_returns_comparison(
            body.get("returnsComparison"),
            symbol=record.symbol,
            source_url=self._absolute(build_path("price_performance_analysis", record)),
            retrieved_at=datetime.now(timezone.utc),
        )

    def returns_comparison(self, symbol: str) -> pd.DataFrame:
        """Sync variant of :meth:`returns_comparison_async`."""
        return _run_async(self.returns_comparison_async(symbol))

    async def stock_page_snapshot_async(
        self, symbol: str, *, force_refresh: bool = False
    ) -> Dict[str, pd.DataFrame]:
        """Six sections of the symbol page, for **no request of their own**.

        The OHLC token lives in the symbol page, so :meth:`ohlc` already downloads that page and
        :meth:`ohlc_token_async` parses these six sections out of the same bytes and caches them.
        This method reads that cache: after any OHLC or token call for the symbol it costs
        **zero** requests, and on a cold client it costs the one page fetch the token needs anyway.

        Args:
            symbol: Anything :meth:`resolve_async` accepts.
            force_refresh: Re-fetch the page and re-parse, even if a cached snapshot is fresh.

        Returns:
            ``{section: frame}``, always all six keys in :data:`STOCK_PAGE_SECTIONS` order:
            ``key_metrics``, ``price_performance``, ``consensus``, ``recommendations``,
            ``valuation``, ``checklists``. Copies, so mutating one does not poison the cache. Each
            carries its parser's ``attrs`` plus ``bfinance_trendlyne_requests``, the **measured**
            delta across this call.

        Raises:
            TrendlyneSchemaError: The cache is empty after a fetch that reported success, which
                would mean the page parsed but stored nothing -- a bug here rather than a vendor
                change, and never reported as six empty sections.

        Note:
            A section whose markup changed is **not** an error here: it comes back as an empty frame
            with its columns, ``bfinance_trendlyne_section_status="unreadable"``, the exception in
            ``bfinance_trendlyne_section_error``, and its name in
            ``bfinance_trendlyne_sections_failed`` on every frame. One section changing shape must
            not cost a caller the other five, and an empty frame must never be read as "the vendor
            published nothing".
        """
        before = self._request_count
        record = await self.resolve_any_async(symbol)
        with self._state_lock:
            cached = self._page_snapshots.get(record.stock_id)
        if (
            not force_refresh
            and cached is not None
            and self.token_ttl_seconds > 0.0
            and (time.monotonic() - cached[0]) <= self.token_ttl_seconds
        ):
            frames = cached[1]
        else:
            await self.ohlc_token_async(record.symbol, force_refresh=force_refresh)
            with self._state_lock:
                entry = self._page_snapshots.get(record.stock_id)
            if entry is None:
                raise TrendlyneSchemaError(
                    f"the page fetched for {record.symbol!r} produced no symbol-page sections. "
                    f"The token and the sections come out of one document, so a token with no "
                    f"sections beside it is a bug in this package, not a vendor change."
                )
            frames = entry[1]
        spent = self._request_count - before
        out: Dict[str, pd.DataFrame] = {}
        for key, frame in frames.items():
            copy = frame.copy()
            copy.attrs = dict(frame.attrs)
            copy.attrs["bfinance_trendlyne_requests"] = spent
            out[key] = copy
        return out

    def stock_page_snapshot(
        self, symbol: str, *, force_refresh: bool = False
    ) -> Dict[str, pd.DataFrame]:
        """Sync variant of :meth:`stock_page_snapshot_async`."""
        return _run_async(
            self.stock_page_snapshot_async(symbol, force_refresh=force_refresh)
        )


#: Shared client for the module-level shortcuts. Created on first use, never at import.
_DEFAULT_CLIENT: Optional[TrendlyneClient] = None


def _default_client() -> TrendlyneClient:
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = TrendlyneClient()
    return _DEFAULT_CLIENT


def ohlc(
    symbol: str,
    *,
    include_last_week: bool = True,
    listing_date: Any = None,
) -> pd.DataFrame:
    """Module-level shortcut for :meth:`TrendlyneClient.ohlc`.

    Pass ``listing_date`` to have ``attrs["bfinance_trendlyne_rows_before_listing"]`` measured
    against your own listing date. The package cannot supply one and will not guess one.

    >>> from bfinance.trendlyne import ohlc
    >>> ohlc("RELIANCE").tail()          # doctest: +SKIP
    """
    return _default_client().ohlc(
        symbol, include_last_week=include_last_week, listing_date=listing_date
    )


def ohlc_async(
    symbol: str,
    *,
    include_last_week: bool = True,
    listing_date: Any = None,
) -> pd.DataFrame:
    """Async variant of :func:`ohlc`."""
    return _default_client().ohlc_async(
        symbol, include_last_week=include_last_week, listing_date=listing_date
    )


def resolve(symbol: str) -> TrendlyneSymbol:
    """Module-level shortcut for :meth:`TrendlyneClient.resolve`."""
    return _default_client().resolve(symbol)


def symbol_index(*, force_refresh: bool = False) -> Dict[str, TrendlyneSymbol]:
    """Module-level shortcut for :meth:`TrendlyneClient.symbol_index`."""
    return _default_client().symbol_index(force_refresh=force_refresh)
