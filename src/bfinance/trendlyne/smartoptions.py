"""
Trendlyne SmartOptions — the option-chain JSON client, on a second host, behind a second opt-in.

What this module is
-------------------
``smartoptions.trendlyne.com`` is a **different host** from ``trendlyne.com``, reached by a
different client with a different base URL, and it answers exactly one thing this package wants:
the full option chain for one symbol at one expiry —

    GET https://smartoptions.trendlyne.com/phoenix/api/fno/option/chain/
        ?stockCode={SYMBOL}&expDate={YYYY-MM-DD}

That response is the only free, no-auth source in the Indian public web for strike-wise open
interest, IV, delta/gamma/vega/rho/theta and the day's max pain, ATM and MWPL for a scrip. It is
also the only place bfinance can get them *without* fabricating them, which is the standard this
whole package holds itself to (see the "never fabricate" rule in
:mod:`bfinance.trendlyne.client`). :class:`SmartOptionsClient` therefore reads the route and
reports what it carries, and reports nothing it does not.

Robots — and why the gate here is **not** a robots gate
-------------------------------------------------------
Read live on **2026-10-10** from ``https://smartoptions.trendlyne.com/robots.txt``. Its
``User-agent: *`` group carries exactly **two** ``Disallow`` rules::

    Disallow: */my-alerts/*
    Disallow: */strategy-builder/user/
    Crawl-delay: 1

Neither rule can match ``/phoenix/api/fno/option/chain/``: the first needs the literal segment
``my-alerts`` and the second needs ``strategy-builder/user/``, and the chain route contains
neither. There is no trailing ``$`` on either, so a prefix match would not help — the substrings
simply are not present. **This route is robots-clean**, and this module states that rather than
implying a conflict that does not exist. :data:`DEFAULT_MIN_REQUEST_INTERVAL_MS` is 1000 ms for
the same reason :mod:`bfinance.trendlyne.client`'s is: the ``Crawl-delay: 1`` in the group this
client falls under.

So the opt-in here, :data:`SMART_OPTIONS_FLAG`, is **not** :class:`TrendlyneRobotsDisallowedError`
territory, and it is deliberately not implemented as one. The reason it is off by default is an
**operator decision recorded 2026-10-10**: SmartOptions ships as a *secondary* F&O source, not as
part of the main trendlyne.com client. That buys three things a default-on route could not:

1. the package's core promise stays what it is — Indian equity data with traceable provenance —
   and a derivatives vendor is a different promise with a different provenance tag
   (``bfinance_provenance = "smartoptions"``, never ``"trendlyne"``);
2. a caller who wants the main path is never silently paying for a host it did not choose, or
   being rate-limited against a crawl-delay it never agreed to;
3. the opt-in is one named word in a constructor call, which is the cheapest possible record that
   somebody meant it.

The refusal is raised **before any socket is opened** and names :data:`SMART_OPTIONS_FLAG`, so
finding out the gate is closed costs the vendor nothing — the same shape as
:meth:`bfinance.trendlyne.client.TrendlyneClient._require_member_search`, for a different reason.

What the payload actually looks like
------------------------------------
The envelope is the one this vendor family uses everywhere: ``{"head": {...}, "body": {...}}``,
and success is ``head.status == "0"`` — **never the HTTP status alone**. :func:`parse_envelope`
from the sibling module is reused unchanged for that, rather than a second unwrapper.

Measured on the recorded capture (TCS, ``2026-10-27``, 276 KB, 2026-10-09)::

    head            {"status": "0", "statusDescription": "success", "responseCode": null}
    body            35 tableHeaders, 102 tableData rows x 35 columns, tableDataV2,
                    maxPain, atTheMoney, MWPL, stockLevelData, expiryLevelPCRValues,
                    futureData, groupData, contractLastUpdated, stockPageType, IV

(The committed fixture ``tests/fixtures/trendlyne/option_chain_tcs.json`` is a trimmed window of
that same response — 35 headers and every row shape above are unchanged, but a subset of the
strikes. The counts here are the raw capture's, so a reader counting the fixture's rows is not
looking at a contradiction.)

Three things in that shape are load-bearing, and each one is a place where the obvious reading is
wrong:

**Rows are positional, and the header is the only key.** ``body.tableData`` is a list of 35-element
**arrays**. There is no ``data`` key, and there are no per-row dicts. Each cell's meaning comes
from ``body.tableHeaders[i]["unique_name"]`` at the *same index*, so the mapping is built from the
header list and applied positionally — never the reverse, and never by hard-coding an index.
:class:`OptionChainResult` then **melts** the wide rows into twice-as-many long ones, one per
``strike x side`` (204 on the raw capture), because a wide frame whose ``current_price_call`` and
``current_price_put`` sit
side by side is the wrong shape for every consumer: a Greeks screen wants one row per contract.
The frame carries **all 35** ``unique_name`` columns, so a call row's ``current_price_put`` is
``NaN`` and a put row's ``current_price_call`` is ``NaN``. That ``NaN`` means "this column does not
apply to this row's side"; it is never a substituted zero.

**``body.IV`` is ``null`` by design.** It was ``None`` on the recorded capture and nothing in this
module treats that as an error, because it is not one — the implied volatility that matters is
per-contract, and it arrives in ``implied_volatility_call`` / ``implied_volatility_put``. A payload
whose top-level ``IV`` is populated is accepted exactly as one whose is not.

**``expDate`` must be ISO ``YYYY-MM-DD``.** Verified 2026-10-10: the same request with
``expDate=27-Oct-2026`` returns **HTTP 500**, not a 4xx and not a validation message. So
:func:`validate_expiry` refuses anything that is not exactly ``YYYY-MM-DD`` — including
``2026-10-7``, which :func:`datetime.date.fromisoformat` would otherwise accept on Python 3.11 —
**locally, before a request is made**, turning a guaranteed server error into a message that names
the accepted form.

The row shape, column mapping and envelope checks live in :func:`parse_option_chain`, which takes
no I/O: it can be pointed at a recorded capture offline, and :class:`SmartOptionsClient` is the
thin transport around it. Every number in :attr:`OptionChainResult.summary` is copied from the
response; a key the vendor did not send is ``None``, never zero.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlencode

import httpx
import pandas as pd

from bfinance.utils.symbols import normalize_symbol

# ``USER_AGENTS`` and ``parse_envelope`` are public in :mod:`bfinance.trendlyne.client` and are
# **imported, not copied**. The browser UA strings and the envelope unwrapper are host-independent,
# and a second copy of either is a second thing to keep correct — the same argument the package
# makes about its pacing algorithm. ``_run_async``, ``_parse_retry_after`` and the pacing lock are
# private in ``client.py``; each is replicated below as a named twin rather than imported across a
# privacy boundary, which is this repo's own convention.
from .client import USER_AGENTS, parse_envelope
from .errors import (
    TrendlyneError,
    TrendlyneHTTPError,
    TrendlyneNotFoundError,
    TrendlyneRateLimitedError,
    TrendlyneSchemaError,
    TrendlyneServerError,
    TrendlyneSmartOptionsError,
    TrendlyneTransportError,
)

__all__ = [
    "BASE_URL",
    "DEFAULT_MIN_REQUEST_INTERVAL_MS",
    "OPTION_CHAIN_PATH",
    "SMART_OPTIONS_FLAG",
    "OptionChainResult",
    "SmartOptionsClient",
    "parse_option_chain",
    "validate_expiry",
]
logger = logging.getLogger("bfinance")

#: The one route this module addresses. A query-string endpoint, so it is kept out of
#: :data:`bfinance.trendlyne.client.ENDPOINTS` — those are path templates validated by
#: :func:`bfinance.trendlyne.client.build_path`, and this one carries its two parameters in the
#: query. ``tableHeaders``/``tableData`` were confirmed on this exact path on 2026-10-10.
BASE_URL = "https://smartoptions.trendlyne.com"
OPTION_CHAIN_PATH = "/phoenix/api/fno/option/chain/"

#: The opt-in that permits a request to this host. ``False`` — off — is the default, and the only
#: way to turn it on is to name it in the constructor or in the call. See the module docstring for
#: why this gate exists at all when the route is robots-clean: it is the operator's 2026-10-10
#: decision to ship SmartOptions as a **secondary** source, not a robots conflict.
SMART_OPTIONS_FLAG = "allow_smart_options"

#: The pacing floor in milliseconds, and a **compliance** number rather than a tuning knob:
#: ``smartoptions.trendlyne.com/robots.txt``, read live on 2026-10-10, carries ``Crawl-delay: 1``
#: in its ``User-agent: *`` group — the group this client falls under. One second is 1000 ms, one
#: request per second, so that is the value.
#:
#: This is a **separate** floor from
#: :data:`bfinance.trendlyne.client.DEFAULT_MIN_REQUEST_INTERVAL_MS`, and deliberately so. The two
#: hosts are different operators publishing different robots documents, and the honest reading of
#: a per-host ``Crawl-delay`` is one slot per host, not one slot shared across both: sharing would
#: make a burst against one of them throttle the other for no compliance reason, and it would let
#: this client silently inherit a floor it never measured. A caller who wants a different posture
#: — tests, a mirror, a private quota — passes ``min_request_interval_ms=``, and ``0.0`` disables
#: pacing outright, which is how the offline tests run.
DEFAULT_MIN_REQUEST_INTERVAL_MS = 1000.0

#: Global pacing state for **this host only**, on the algorithm
#: ``bfinance.trendlyne.client`` already uses (``_GLOBAL_PACING_LOCK`` + a monotonic clock +
#: 10-50 ms of jitter). It is a deliberate twin, not a second algorithm: the module-level
#: ``_throttle_pacing`` below is the same reservation step as ``TrendlyneClient._throttle_pacing``,
#: with one difference — it reads this module's own slot. See
#: :data:`DEFAULT_MIN_REQUEST_INTERVAL_MS` for why the slot is not shared across the two hosts.
_GLOBAL_PACING_LOCK = threading.Lock()
_GLOBAL_LAST_REQUEST_TIME = 0.0

#: ``Retry-After`` cap in seconds, so a hostile or accidental header cannot stall us. Twin of
#: ``client._MAX_RETRY_AFTER_SEC`` — the value is a politeness bound, not a host property.
_MAX_RETRY_AFTER_SEC = 30.0

#: Header AWS WAF attaches to a challenged response, independently of the status code it returns.
#: Twin of ``client._WAF_ACTION_HEADER``; spelled out rather than imported because it is private
#: in :mod:`bfinance.trendlyne.client` and the header itself is the thing, not a client detail.
_WAF_ACTION_HEADER = "x-amzn-waf-action"


def _parse_retry_after(response: Optional[httpx.Response]) -> Optional[float]:
    """Parse a delta-seconds ``Retry-After`` header, capped. Twin of ``client._parse_retry_after``.

    ``None`` when the header is absent, unparseable or negative. Reproduced here rather than
    imported because it is private in ``client.py`` and it is twelve lines with no host-specific
    content; the shape is kept identical so a change to one is visibly a change to both.
    """
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
    """Bridge to sync, matching ``client._run_async`` and every other twin in this repo."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(coro)).result()
    return asyncio.run(coro)


#: ``expDate`` must be exactly this and nothing else. ``YYYY-MM-DD``, four/two/two digits, anchored.
#: The route answers **HTTP 500** for anything else (verified 2026-10-10 with ``27-Oct-2026``), and
#: ``date.fromisoformat`` alone is not a sufficient check: Python 3.11 widened it to accept
#: ``20261027`` and ``2026-W01-1``, both of which this route would 500 on.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: The ``unique_name`` values this module requires on ``tableHeaders``, and refuses to guess
#: without. Chosen from the recorded 2026-10-10 capture (all 35 present there): the strike itself,
#: the two prices — without them the frame is not an option chain — and the three Greeks this
#: module's own contract promises per side. **Additions are fine and are carried through**; only a
#: *removal* trips this, which is the direction worth a loud failure, because a renamed or
#: dropped column would otherwise become a quietly mislabelled one.
_REQUIRED_UNIQUE_NAMES: Tuple[str, ...] = (
    "strike_price",
    "current_price_call",
    "current_price_put",
    "delta_calc_call",
    "delta_calc_put",
    "gamma_calc_call",
    "gamma_calc_put",
    "vega_calc_call",
    "vega_calc_put",
)

#: ``body`` key -> ``OptionChainResult.summary`` key for the totals ``body.stockLevelData``
#: publishes inside its ``PCR_metrcs`` sub-object. Measured names on 2026-10-10 (TCS):
#: ``total_call_OI``, ``total_put_OI``, ``total_OI``, ``total_call_vol``, ``total_put_vol`` and
#: ``FNO_TOTAL_VOL``. A key the response does not carry is written as ``None``, never zero and
#: never omitted silently.
_STOCK_LEVEL_TOTALS: Tuple[Tuple[str, str], ...] = (
    ("total_call_OI", "total_call_oi"),
    ("total_put_OI", "total_put_oi"),
    ("total_OI", "total_oi"),
    ("total_call_vol", "total_call_volume"),
    ("total_put_vol", "total_put_volume"),
    ("FNO_TOTAL_VOL", "total_volume"),
)


def validate_expiry(expiry: str) -> str:
    """Validate an ``expDate`` before any request, and return it cleaned.

    Args:
        expiry: The expiry as the caller has it — ``2026-10-27``, ``"2026-10-27"`` or
            ``" 2026-10-27 "``.

    Returns:
        The ``YYYY-MM-DD`` string, whitespace stripped. **Echoed back** by
        :attr:`OptionChainResult.expiry`, so it is exactly what was sent.

    Raises:
        ValueError: Anything that is not an ISO calendar date in the one form this route accepts.
            Raised before a socket is opened, so an argument mistake costs the vendor nothing.

    Note:
        The route is verified to answer **HTTP 500** for ``27-Oct-2026`` (2026-10-10), which is a
        server error with no body explaining the mistake — the most expensive possible answer to a
        formatting question. That is the whole reason this function exists. It is also stricter
        than :meth:`datetime.date.fromisoformat`, deliberately: on Python 3.11 that accepts
        ``20261027`` and ``2026-W01-1``, both of which this route would reject with the same 500.
    """
    if not isinstance(expiry, str):
        raise ValueError(f"expiry must be a string, got {type(expiry).__name__}")
    clean = expiry.strip()
    if not _ISO_DATE_RE.match(clean):
        raise ValueError(
            f"expiry={expiry!r} is not an ISO date. Use 'YYYY-MM-DD' (for example "
            f"'2026-10-27'). The expDate parameter accepts nothing else: the same request with "
            f"'27-Oct-2026' answers HTTP 500 with no explanation, which is why the form is "
            f"checked here rather than sent and diagnosed."
        )
    try:
        date.fromisoformat(clean)
    except ValueError as exc:  # right shape, not a real day: '2026-02-30'
        # The accepted form is restated here rather than only in the branch above. Both refusals
        # are "that is not a date this route takes", and a caller who typed a plausible day in a
        # plausible month should not have to guess which rule they broke.
        raise ValueError(
            f"expiry={expiry!r} has the right shape but is not a calendar date: {exc}. "
            f"Use 'YYYY-MM-DD' and a day that exists (for example '2026-10-27')."
        ) from exc
    return clean


@dataclass(frozen=True)
class OptionChainResult:
    """One option chain, as the response carried it.

    Attributes:
        chain: The long frame — **one row per ``strike x side``**, so ``2 x len(unique strikes)``
            rows (204 on the raw 2026-10-27 TCS capture: 102 strikes). Columns are every
            ``tableHeaders[i].unique_name`` in wire order, prefixed by ``symbol`` and ``side``,
            then ``symbol`` and ``side`` themselves. A row's own side is populated and the other
            side's columns are ``NaN``, which means *not applicable to this row*, never *zero* —
            a call row must not report the put's open interest as if it were its own. No cell is
            filled, smoothed or dropped: a value the vendor sent as ``null`` is ``NaN``, and a
            whole row of ``null``s is still a row, because the strike exists.

            **Zero rows is an answer, and it carries the same columns.** An expiry the vendor has
            not populated yet, and a scrip with no F&O chain, both answer ``tableHeaders`` with an
            empty ``tableData``; the frame is then empty with every column present, so a consumer's
            own ``frame["side"]`` or ``frame.sort_values(["strike_price", "side"])`` cannot raise
            on it. :attr:`attrs` says so in ``bfinance_trendlyne_smartoptions_empty``.
        summary: The headline numbers, copied verbatim. Always carries ``maxPain``, ``atTheMoney``,
            ``MWPL``, ``pcr_oi``, ``pcr_volume``, the six totals from
            ``stockLevelData.PCR_metrcs`` and ``expiry_level_pcr`` (the whole
            ``expiryLevelPCRValues`` object). Any key the response did not carry is ``None``.
            ``pcr_oi``/``pcr_volume`` are the **expiry-level** ratios from
            ``expiryLevelPCRValues``; the all-expiry equivalents the vendor publishes in
            ``stockLevelData`` are left inside ``stock_level_pcr`` rather than silently
            overwriting them, because the two are different measurements. On an empty chain this
            is whatever the response itself carried and nothing more: no number is inferred from a
            chain that has no rows.
        expiry: The ISO ``YYYY-MM-DD`` date, echoed back exactly as validated and sent.
        contract_last_updated: The vendor's own timestamp string for the chain, verbatim (on the
            recorded capture: ``'09 Oct,2026  04:00 PM (IST)'``). It is **not** parsed into a
            ``datetime``: the format carries a double space and a bare ``IST`` with no offset,
            and guessing an offset would be a fabricated fact. ``None`` when the response omits it.
        attrs: The same mapping as :attr:`pandas.DataFrame.attrs` on :attr:`chain`, held here so a
            caller holding the result does not have to reach through to the frame for provenance.
    """

    chain: pd.DataFrame
    summary: Dict[str, Any] = field(default_factory=dict)
    expiry: str = ""
    contract_last_updated: Optional[str] = None
    attrs: Dict[str, Any] = field(default_factory=dict)


def _unique_names(headers: Any) -> List[str]:
    """The ``unique_name`` of each header, in wire order.

    Raises:
        TrendlyneSchemaError: ``tableHeaders`` is absent, is not a list, holds a non-object, or
            holds a header with no ``unique_name``. Any of those means the positional mapping
            cannot be built, and a frame built anyway would have every cell mislabelled.
    """
    if not isinstance(headers, list):
        raise TrendlyneSchemaError(
            f"SmartOptions option chain: body.tableHeaders is {type(headers).__name__}, "
            f"expected a list. Upstream schema may have changed."
        )
    names: List[str] = []
    for position, header in enumerate(headers):
        if not isinstance(header, dict):
            raise TrendlyneSchemaError(
                f"SmartOptions option chain: tableHeaders[{position}] is "
                f"{type(header).__name__}, expected an object with 'unique_name'."
            )
        name = header.get("unique_name")
        if not isinstance(name, str) or not name:
            raise TrendlyneSchemaError(
                f"SmartOptions option chain: tableHeaders[{position}] carries no usable "
                f"'unique_name' (got {name!r}). Without it the positional row arrays cannot be "
                f"mapped to columns."
            )
        names.append(name)
    return names


def _long_rows(
    rows: Sequence[Sequence[Any]], names: Sequence[str], symbol: str
) -> pd.DataFrame:
    """Melt positional wide rows into one row per ``strike x side``, mapped by header name.

    ``body.tableData`` is a list of positional arrays, so ``rows[i][j]`` means
    ``names[j]``. That mapping is applied here and nowhere else. The frame then splits into a
    ``call`` row and a ``put`` row per wire row, so a consumer filtering ``side == "put"`` gets a
    frame of put contracts rather than one with 17 dead columns.

    No cell is coerced, and no string the vendor sent is turned into a number: a ``null`` becomes
    ``NaN`` through pandas' own inference, and a build-up string stays a string. Numeric coercion
    here would turn a value of the wrong type into a ``NaN`` that reads downstream as "not
    measured" rather than "not what you assumed", which is the substitution this package exists to
    refuse.

    **An empty ``tableData`` returns the full column set with zero rows**, ``symbol`` and ``side``
    included and in the same leading positions as a populated chain. The early return used to hand
    back the bare ``names`` frame, so :func:`parse_option_chain`'s own
    ``sort_values(["strike_price", "side"])`` raised ``KeyError: 'side'`` on exactly the answer a
    caller most needs to be able to read -- an expiry the vendor has not populated, or a scrip that
    is not F&O. Zero rows is a measurement; a missing column is a defect in this module.

    One dtype consequence is worth stating rather than discovering. A column that holds integers on
    the wire (``pk_call`` and ``pk_put``, the contract ids) becomes ``float64`` in the long frame,
    because the other side's rows carry no value for it and only a float column can hold a missing
    integer. The ids themselves are exact — well inside float64's 2**53 — so nothing is rounded;
    the dtype simply stops advertising "this column never has a hole", which after the melt is no
    longer true.
    """
    width = len(names)
    for position, row in enumerate(rows):
        if not isinstance(row, (list, tuple)):
            raise TrendlyneSchemaError(
                f"SmartOptions option chain: tableData[{position}] is {type(row).__name__}, "
                f"expected a positional array of {width} values. Upstream schema may have "
                f"changed — this route used to be described as returning per-row objects."
            )
        if len(row) != width:
            raise TrendlyneSchemaError(
                f"SmartOptions option chain: tableData[{position}] has {len(row)} values but "
                f"tableHeaders declares {width}. A row of the wrong width cannot be mapped to "
                f"columns without guessing which value belongs where."
            )

    wide = pd.DataFrame([list(row) for row in rows], columns=list(names))
    if wide.empty:
        # An empty chain is a real answer, not a missing one: an expiry the vendor has not
        # populated yet, or a scrip that is not F&O, both answer with ``tableHeaders`` and zero
        # ``tableData`` rows. The frame still carries **every** column -- ``symbol`` and ``side``
        # included, in the same leading position as a populated chain -- so a consumer's
        # ``frame.sort_values(["strike_price", "side"])`` or ``frame["side"]`` cannot raise
        # KeyError on the one answer that is most likely to be handed to a screen.
        empty = pd.DataFrame(columns=["symbol", "side", *names])
        return empty

    frames = []
    for side, other in (("call", "put"), ("put", "call")):
        # A row belongs to ONE side, so the other side's columns are *nulled on it* rather than
        # left carrying that side's numbers. Copying the whole wide row onto both sides would be
        # the worst possible fabrication: a call row would report the put's open interest as if
        # it were the call's, at the exact strikes where a screen sorts by it.
        #
        # The columns themselves stay in the frame — as NaN — rather than being dropped, so
        # `delta_calc_put` is a real column on both halves and a consumer never has to re-join
        # two frames. `strike_price` is the one column shared by both sides and is never nulled.
        side_frame = wide.copy()
        opposite = [
            name
            for name in names
            if name != "strike_price" and name.endswith(f"_{other}")
        ]
        if opposite:
            # ``float("nan")`` rather than ``pd.NA``: pandas 2 upcasts an int64 column to object
            # on ``pd.NA``, which would leave a numeric column non-numeric for the rest of the
            # frame's life. A float NaN is how pandas itself spells "missing in a numeric column".
            side_frame[opposite] = float("nan")
        side_frame.insert(0, "side", side)
        frames.append(side_frame)
    melted = pd.concat(frames, ignore_index=True)
    melted.insert(0, "symbol", symbol)
    return melted[["symbol", "side", *names]]


def parse_option_chain(
    payload: Any, *, symbol: str, expiry: str, source: Optional[str] = None
) -> OptionChainResult:
    """Turn a recorded or live SmartOptions chain payload into an :class:`OptionChainResult`.

    Pure: no I/O, no clock, no randomness. The offline tests drive it straight from a fixture, and
    :meth:`SmartOptionsClient.option_chain_async` is the transport wrapped around it.

    Args:
        payload: The decoded JSON, envelope and all. The envelope is checked by
            :func:`bfinance.trendlyne.client.parse_envelope` — success is ``head.status == "0"``,
            never the HTTP status alone, because this vendor family answers **200** for a failed
            request.
        symbol: The symbol the request was for, recorded on every row.
        expiry: The ISO expiry, recorded on the result and in ``attrs``.
        source: The URL this payload actually came from, recorded in ``attrs``. The client passes
            the URL it requested rather than letting this function rebuild the default, because a
            caller who set ``base_url=`` pointed the request somewhere else and an ``attrs`` entry
            naming the stock host would be a claim about a request that never went there.
            ``None`` (the default) rebuilds the canonical :data:`BASE_URL` form.

    Returns:
        One :class:`OptionChainResult`. Every value is copied from ``body``; a key the response
        did not carry is ``None``. An empty ``tableData`` is **not** an error: it yields an empty
        ``chain`` carrying every column (``symbol`` and ``side`` included) and a ``summary`` of
        whatever the response itself carried. See :class:`OptionChainResult`.

    Raises:
        TrendlyneNotAuthorizedError: ``head.status`` is not ``"0"``.
        TrendlyneSchemaError: The envelope or ``body`` has the wrong shape, ``tableHeaders`` has
            no usable ``unique_name``, a required column is missing, or a row is not a positional
            array of the declared width.
    """
    ticker = normalize_symbol(symbol)
    context = f"the SmartOptions option chain for {ticker} expiring {expiry}"
    body = parse_envelope(payload, context=context)

    if "tableHeaders" not in body:
        raise TrendlyneSchemaError(
            f"{context}: body carries no 'tableHeaders'. Body keys: {sorted(body)[:12]}. "
            f"Upstream schema may have changed — without the header list the positional "
            f"tableData arrays cannot be mapped to columns."
        )
    names = _unique_names(body.get("tableHeaders"))
    missing = [name for name in _REQUIRED_UNIQUE_NAMES if name not in names]
    if missing:
        raise TrendlyneSchemaError(
            f"{context}: tableHeaders is missing required column(s) {missing}. This module maps "
            f"by unique_name and will not guess a substitute for a column the vendor stopped "
            f"sending — a renamed column read positionally is a mislabelled frame. Headers "
            f"present: {names[:40]}."
        )

    rows = body.get("tableData")
    if not isinstance(rows, list):
        raise TrendlyneSchemaError(
            f"{context}: body.tableData is {type(rows).__name__}, expected a list of positional "
            f"arrays. There is no 'data' key on this route; the rows are in 'tableData'."
        )
    chain = _long_rows(rows, names, ticker)
    chain = chain.sort_values(["strike_price", "side"], kind="stable", ignore_index=True)

    expiry_pcr = body.get("expiryLevelPCRValues")
    expiry_pcr = dict(expiry_pcr) if isinstance(expiry_pcr, Mapping) else {}
    stock_level = body.get("stockLevelData")
    stock_level = dict(stock_level) if isinstance(stock_level, Mapping) else {}
    pcr_metrics = stock_level.get("PCR_metrcs")
    pcr_metrics = dict(pcr_metrics) if isinstance(pcr_metrics, Mapping) else {}

    summary: Dict[str, Any] = {
        # Top-level headline numbers. None when absent, never 0.0 — a max pain the vendor did not
        # send is not a max pain of zero, and the two are indistinguishable to a downstream screen.
        "maxPain": body.get("maxPain"),
        "atTheMoney": body.get("atTheMoney"),
        "MWPL": body.get("MWPL"),
        # Expiry-level PCR, NOT the all-expiry FNO_* ratios in stockLevelData. Both are real and
        # they disagree (0.9103 vs 0.9 on the recorded capture); collapsing them would lose the
        # distinction the caller asked a chain question in order to make.
        "pcr_oi": expiry_pcr.get("pcr_oi"),
        "pcr_volume": expiry_pcr.get("pcr_volume"),
        "expiry_level_pcr": expiry_pcr,
        # The all-expiry equivalents, verbatim and unflattened, so the contrast above is visible.
        "stock_level_pcr": pcr_metrics,
        "underlying_price": stock_level.get("currentPrice"),
        "underlying_updated": stock_level.get("updated"),
    }
    for wire_key, summary_key in _STOCK_LEVEL_TOTALS:
        summary[summary_key] = pcr_metrics.get(wire_key)

    contract_last_updated = body.get("contractLastUpdated")
    if not isinstance(contract_last_updated, str) or not contract_last_updated:
        contract_last_updated = None

    attrs: Dict[str, Any] = {
        # The provenance tag this module exists to be distinct on. Never "trendlyne": a different
        # host, a different crawl-delay and a different contract. The KEY is the cross-package one
        # every other producer in bfinance publishes (nse/bhavcopy.py, ownership.py, tables.py), so
        # a caller filtering frames by source reads one vocabulary; the VALUE stays this host's own.
        "bfinance_provenance": "smartoptions",
        "bfinance_symbol": ticker,
        "bfinance_trendlyne_smartoptions_expiry": expiry,
        "bfinance_trendlyne_smartoptions_rows": int(len(chain)),
        "bfinance_trendlyne_smartoptions_strikes": int(len(rows)),
        # Whether the vendor sent a populated chain. An expiry before the chain is published and a
        # non-F&O scrip are different questions with the same honest answer -- zero rows -- and this
        # is what tells a caller to ask "is this expiry live" rather than "did the fetch work".
        "bfinance_trendlyne_smartoptions_empty": bool(chain.empty),
        "bfinance_trendlyne_smartoptions_source": source
        if source is not None
        else f"{BASE_URL}{OPTION_CHAIN_PATH}?stockCode={ticker}&expDate={expiry}",
        "bfinance_trendlyne_note": (
            "One row per strike x side; the other side's columns are NaN on each row, which means "
            "'not applicable here' and never 'zero'. A null cell is the vendor's null. "
            "body.IV is null by design and is not carried."
        )
        + (
            " This chain is EMPTY: the response carried the header list and no rows, so the frame "
            "has every column and no values and the summary holds only what the response itself "
            "carried. That is the answer for an expiry the vendor has not populated and for a "
            "scrip with no F&O chain -- it is not a failure and nothing was invented to fill it."
            if chain.empty
            else ""
        ),
    }
    chain.attrs.update(attrs)
    return OptionChainResult(
        chain=chain,
        summary=summary,
        expiry=expiry,
        contract_last_updated=contract_last_updated,
        attrs=attrs,
    )


async def _throttle_pacing(min_request_interval_ms: float) -> None:
    """Reserve this host's pacing slot, on ``TrendlyneClient``'s algorithm.

    Deliberate twin of :meth:`bfinance.trendlyne.client.TrendlyneClient._throttle_pacing` — the
    same reservation under the same lock, with the same 10-50 ms of jitter — differing only in
    which module's slot it reads. See :data:`DEFAULT_MIN_REQUEST_INTERVAL_MS` for why the slot is
    per host.

    ``0.0`` returns without sleeping, which is how the offline tests run.
    """
    global _GLOBAL_LAST_REQUEST_TIME
    if min_request_interval_ms <= 0:
        return
    wait_sec = 0.0
    with _GLOBAL_PACING_LOCK:
        now = time.monotonic()
        elapsed_ms = (now - _GLOBAL_LAST_REQUEST_TIME) * 1000.0
        if elapsed_ms < min_request_interval_ms:
            wait_sec = (min_request_interval_ms - elapsed_ms) / 1000.0 + random.uniform(0.01, 0.05)
            _GLOBAL_LAST_REQUEST_TIME = now + wait_sec
        else:
            _GLOBAL_LAST_REQUEST_TIME = now
    if wait_sec > 0.0:
        await asyncio.sleep(wait_sec)


class SmartOptionsClient:
    """Client for ``smartoptions.trendlyne.com``. Constructing it performs **no I/O**.

    A deliberately small surface: one route, one symbol, one expiry. The sitemap, the symbol
    index, the per-symbol token and the whole HTML layer belong to
    :class:`bfinance.trendlyne.client.TrendlyneClient` and are **not** duplicated here — this
    module is a second host reached over a second, separately-paced connection, not a fork of the
    first.

    There are no cookies, no API key and no login. A browser ``User-Agent`` plus the two
    same-origin headers below is the whole requirement; the vendor states no authentication on
    this route and none is invented.

    Args:
        timeout: Per-request timeout, seconds.
        max_retries: Attempts for 429, 5xx and transport failures. Every 4xx is terminal — an
            unchanged request cannot turn a rejection into a success, and retrying it only spends
            the vendor's patience.
        proxy: Proxy URL. Falls back to ``HTTPS_PROXY``/``HTTP_PROXY``, as elsewhere in bfinance.
        min_request_interval_ms: This host's pacing floor. See
            :data:`DEFAULT_MIN_REQUEST_INTERVAL_MS`; ``0.0`` disables pacing, which is how the
            offline tests run.
        base_url: Override for the host. A mirror or a recorded-replay server. The request path
            and query are unchanged, so this cannot silently point the client at a different API.
        transport: Optional ``httpx`` transport. ``httpx.MockTransport`` is how the test suite
            exercises the 429/5xx/gate paths with no network; production callers leave it ``None``.
        allow_smart_options: Opt in to the route, via :data:`SMART_OPTIONS_FLAG`. **Off by
            default**; see the module docstring for the operator decision behind that.
    """

    #: Absolute base for every request, exposed as a class attribute so a subclass can point at a
    #: mirror without touching the module-level helpers — the convention
    #: :class:`bfinance.trendlyne.client.TrendlyneClient` uses.
    BASE_URL = BASE_URL

    def __init__(
        self,
        timeout: float = 20.0,
        max_retries: int = 3,
        proxy: Optional[str] = None,
        min_request_interval_ms: float = DEFAULT_MIN_REQUEST_INTERVAL_MS,
        base_url: Optional[str] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        allow_smart_options: bool = False,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self.proxy = proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        self.min_request_interval_ms = float(min_request_interval_ms)
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.transport = transport
        self.allow_smart_options = bool(allow_smart_options)
        #: The URL the most recent successful request resolved to, as the response saw it. Read
        #: it; it exists so ``attrs`` can name where the data actually came from rather than where
        #: the client meant to look. ``None`` until a request has succeeded.
        self.last_url: Optional[str] = None

    def _require_smart_options(self, allowed: Optional[bool]) -> None:
        """Refuse the request when the opt-in is closed, naming the constant that opens it.

        Mirrors :meth:`bfinance.trendlyne.client.TrendlyneClient._require_member_search` in shape
        — check first, raise before any socket, name the flag — but **not** in kind. That method
        raises :class:`~bfinance.trendlyne.errors.TrendlyneRobotsDisallowedError` because the
        route it guards is covered by a ``robots.txt`` ``Disallow``. This route is robots-clean
        (module docstring, robots section), so claiming a robots conflict here would be a false
        statement about a site the package has read. The refusal is therefore
        :class:`~bfinance.trendlyne.errors.TrendlyneSmartOptionsError`: an operator policy
        decision, not a policy conflict.

        Args:
            allowed: Per-call override of :data:`SMART_OPTIONS_FLAG`, or ``None`` to use the
                constructor's setting.

        Raises:
            TrendlyneSmartOptionsError: The gate is closed. Raised before a socket is opened.
        """
        if allowed if allowed is not None else self.allow_smart_options:
            return
        raise TrendlyneSmartOptionsError(
            f"smartoptions.trendlyne.com was not contacted: {SMART_OPTIONS_FLAG} is False, which "
            f"is this client's default. Pass {SMART_OPTIONS_FLAG}=True to SmartOptionsClient(...) "
            f"or to option_chain(...) to fetch the option chain. This is not a robots refusal — "
            f"the chain route is robots-clean (robots.txt read 2026-10-10 disallows only "
            f"*/my-alerts/* and */strategy-builder/user/, with Crawl-delay: 1 for everybody "
            f"else). The gate is off because SmartOptions ships as a secondary F&O source beside "
            f"trendlyne.com, on its own host and its own crawl-delay, and a caller should opt "
            f"into that rather than inherit it."
        )

    def _headers(self) -> Dict[str, str]:
        """Browser headers plus the two same-origin headers this host expects.

        The route answers a phoenix JSON feed rather than a page, so ``Accept`` is
        ``application/json`` alone rather than the HTML-capable string the trendlyne.com client
        sends — asking for JSON and only JSON is what this endpoint is, and a negotiation failure
        would arrive as a 200 carrying HTML, which is the hardest kind to diagnose.
        """
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/",
        }

    async def _send(self, path: str) -> httpx.Response:
        """GET with pacing and retry. Returns the 200 response or raises a typed error.

        Retry covers 429, 5xx and transport failures, with exponential backoff honouring
        ``Retry-After`` (capped at 30 s). Every other status is terminal.

        **The WAF guard, and what is assumed rather than measured about it.** HTTP 405, or any
        status carrying :data:`_WAF_ACTION_HEADER`, raises
        :class:`~bfinance.trendlyne.errors.TrendlyneSmartOptionsError` and is **not** retried —
        the same discrimination :meth:`bfinance.trendlyne.client.TrendlyneClient._send` makes,
        for the same reason: WAF attaches the header independently of the code it returns, so a
        challenge served as a 200 is a captcha page rather than a JSON feed, and a challenged
        session is spent rather than momentarily throttled.

        **Whether ``smartoptions.trendlyne.com`` sits behind that same WAF is UNMEASURED.** Every
        capture in this repository is from ``trendlyne.com``; no request to this host has been
        observed returning either form. The guard is here as an **assumption** that the operator's
        front door is shared across the vendor's subdomains — cheap, directionally right, and
        wrong in at most the harmless direction (an over-cautious refusal on a 405 from a route
        that simply does not answer GET). It is stated as an assumption rather than dressed as a
        measurement because the alternative — asserting this host is behind a WAF nobody has seen
        challenge it — is a false claim about a site this package has not read.

        **No 444 special case**, and the omission is a measurement rather than an oversight:
        the 444 bot gate in :mod:`bfinance.trendlyne.client` was measured on trendlyne.com's
        ``/mapp/v1/`` family on 2026-09-27 and has not been observed on this host. Treating it
        here would be claiming a behaviour nobody has seen on this route; a 404, a 401 or a 403
        arrive as the specific typed errors they are, and anything else non-200 arrives as
        :class:`~bfinance.trendlyne.errors.TrendlyneHTTPError` with the code attached, which is
        where a caller looks first.
        """
        url = f"{self.base_url}{path}"
        last_error: Optional[Exception] = None
        last_response_error: Optional[TrendlyneError] = None

        for attempt in range(self.max_retries):
            await _throttle_pacing(self.min_request_interval_ms)
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
                    response = await client.get(url, headers=self._headers())
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(min(2.0 * (2**attempt), 8.0) + random.uniform(0, 0.5))
                continue

            status = response.status_code

            # The WAF is checked before every other status, 200 included: the header is attached
            # independently of the code the edge returns, and a challenge served as a 200 would
            # otherwise be handed to ``response.json()`` as a parse failure. Terminal and not
            # retried, for the reason in the docstring.
            waf_action = response.headers.get(_WAF_ACTION_HEADER)
            if status == 405 or waf_action:
                seen = (
                    f"{_WAF_ACTION_HEADER}={waf_action}"
                    if waf_action
                    else f"no {_WAF_ACTION_HEADER} header"
                )
                raise TrendlyneSmartOptionsError(
                    f"smartoptions.trendlyne.com's edge challenged this request: HTTP {status} "
                    f"with {seen} for {url}. This is terminal and was not retried, because a "
                    f"challenge spends the session's budget rather than momentarily exceeding "
                    f"it -- the same request would be refused for the rest of the session. "
                    f"Note what is assumed here rather than measured: whether this host is "
                    f"behind the same AWS WAF as trendlyne.com has NOT been observed on this "
                    f"route, so this guard is an assumption that the operator is shared across "
                    f"the vendor's subdomains. If it fires on a 405 that was never a challenge, "
                    f"the response here is the specific one and the route is worth reporting."
                )

            if status == 200:
                # The final URL, not the requested one: follow_redirects is on, so these can
                # differ, and the caller records this string in ``attrs``. Recording the one that
                # was asked for would be a claim about where the data came from.
                self.last_url = str(response.url)
                return response

            if status == 429:
                if attempt < self.max_retries - 1:
                    backoff = min(2.0 * (2**attempt), 8.0) + random.uniform(0.5, 1.5)
                    retry_after = _parse_retry_after(response)
                    if retry_after is not None:
                        backoff = max(backoff, retry_after)
                    logger.warning(
                        "smartoptions rate limited (%d); backing off %.1fs (attempt %d/%d).",
                        status, backoff, attempt + 1, self.max_retries,
                    )
                    await asyncio.sleep(backoff)
                    continue
                raise TrendlyneRateLimitedError(
                    f"smartoptions rate limited this client (HTTP 429) after {self.max_retries} "
                    f"attempts. No state is cached by this client, so a later call may succeed.",
                    status_code=status,
                    url=url,
                )

            if status in (401, 403):
                raise TrendlyneHTTPError(
                    f"smartoptions returned HTTP {status}. The chain route is public and this "
                    f"client sends no credentials, so this is a request-shape or access change "
                    f"rather than bad credentials — and it is not retried.",
                    status_code=status,
                    url=url,
                )

            if status == 404:
                raise TrendlyneNotFoundError(
                    f"smartoptions has no route or resource at {url} (HTTP 404). This is also "
                    f"what an unknown stockCode earns — the route exists, the scrip under it may "
                    f"not. Terminal: an unchanged request cannot find it a second time.",
                    status_code=status,
                    url=url,
                )

            if status >= 500:
                last_response_error = TrendlyneServerError(
                    f"smartoptions returned HTTP {status} for {url}. A 500 here is also what a "
                    f"malformed expDate earns — see validate_expiry, which is why the format is "
                    f"checked before the request rather than diagnosed after it.",
                    status_code=status,
                    url=url,
                )
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(min(2.0 * (2**attempt), 8.0) + random.uniform(0, 0.5))
                continue

            raise TrendlyneHTTPError(
                f"smartoptions returned unexpected HTTP {status} for {url}.",
                status_code=status,
                url=url,
            )

        # A response that arrived and said "5xx" must surface as a server error, not as a
        # transport error: the two send the reader to completely different places.
        if last_response_error is not None:
            raise last_response_error
        raise TrendlyneTransportError(
            f"smartoptions request failed after {self.max_retries} attempts for {url}: "
            f"{last_error}"
        ) from last_error

    async def option_chain_async(
        self,
        symbol: str,
        expiry: str,
        *,
        allow_smart_options: Optional[bool] = None,
    ) -> OptionChainResult:
        """Fetch one option chain. The async half of the package's every-other-client twin.

        Args:
            symbol: The scrip, e.g. ``"TCS"``. Normalised through
                :func:`bfinance.utils.symbols.normalize_symbol`, so ``"tcs"`` and ``"TCS.NS"``
                both address the chain for ``TCS``.
            expiry: The expiry as ``YYYY-MM-DD``. See :func:`validate_expiry`; anything else is
                refused before a request, because the route answers HTTP 500 for it.
            allow_smart_options: Per-call override of :data:`SMART_OPTIONS_FLAG`, or ``None`` for
                the constructor's setting.

        Returns:
            One :class:`OptionChainResult`.

        Raises:
            TrendlyneSmartOptionsError: :data:`SMART_OPTIONS_FLAG` is closed. **Before** the
                expiry is even looked at, and before any socket — the standing policy decision
                comes first. Also raised for a WAF challenge (HTTP 405, or any status carrying
                ``x-amzn-waf-action``); see :meth:`_send` for what is assumed rather than
                measured about that host's edge.
            ValueError: ``expiry`` is not an ISO ``YYYY-MM-DD`` date. Before any socket.
            TrendlyneNotAuthorizedError: HTTP 200 whose envelope reports ``head.status != "0"``.
            TrendlyneRateLimitedError: HTTP 429 that survived every retry.
            TrendlyneServerError: HTTP 5xx that survived every retry.
            TrendlyneTransportError: The request never completed.
            TrendlyneHTTPError: Any other non-200 status.
            TrendlyneSchemaError: The payload parsed but did not have the shape this module
                requires.
        """
        self._require_smart_options(allow_smart_options)
        clean_expiry = validate_expiry(expiry)
        ticker = normalize_symbol(symbol)
        if not ticker:
            raise ValueError(f"symbol={symbol!r} normalises to an empty string.")

        query = urlencode({"stockCode": ticker, "expDate": clean_expiry})
        response = await self._send(f"{OPTION_CHAIN_PATH}?{query}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise TrendlyneSchemaError(
                f"smartoptions returned a non-JSON body (HTTP {response.status_code}) for "
                f"{ticker} {clean_expiry}. Content-Type: "
                f"{response.headers.get('Content-Type', '<none>')!r}. First 160 chars: "
                f"{response.text[:160]!r}."
            ) from exc
        return parse_option_chain(
            payload, symbol=ticker, expiry=clean_expiry, source=self.last_url
        )

    def option_chain(
        self,
        symbol: str,
        expiry: str,
        *,
        allow_smart_options: Optional[bool] = None,
    ) -> OptionChainResult:
        """Sync variant of :meth:`option_chain_async`, on the repo's shared bridge."""
        return _run_async(
            self.option_chain_async(
                symbol, expiry, allow_smart_options=allow_smart_options
            )
        )