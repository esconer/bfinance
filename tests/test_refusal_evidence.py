"""
Each registered refusal, tied to a recorded measurement, and the test reads red if the fact changes.

The gap this file closes
-----------------------
``bfinance.sources.registry.UNAVAILABLE`` registers four measured refusals: ``tri_index_return``,
``rbi_reference_rate``, ``pledge_shares`` and ``fpi_ownership``. Each names a source that genuinely
does not publish the data. The layer's own test,
``test_a_capability_nobody_can_serve_is_a_measured_refusal_not_an_omission``, asserts that a
refusal **exists** and that its reason is non-empty — which is a test of a *constant*. It would
still be green the day ``pledge_shares`` became obtainable, because nothing in it connects the
refusal to the thing that justifies it.

A test asserting a refusal is true is as weak as a test asserting a constant, and this project has
removed three instances of exactly that mistake. So the shape here is different: every verdict in
this file is **derived** from a recorded artifact — bytes on disk, a JSON status, a CSV header — by
a function that takes the artifact as its only input, and every derivation is accompanied by a
control that mutates the recorded artifact in memory and requires the derivation to **change**.

Read a derivation and its control together: the control is what proves the verdict is a function of
the evidence rather than a restatement of it. A control that cannot flip the verdict is the same
unfailable assertion this file exists to prevent, and there is one per refusal.

The four measurements, and where each one is recorded
---------------------------------------------------
=========================================  ==================================================================
capability                                 the recorded fact, and the artifact holding it
=========================================  ==================================================================
``pledge_shares``                          HTTP 200 with a 28-byte empty envelope
                                           ``{"comNameList":[],"data":[]}``, byte-identical on two
                                           consecutive days. Recorded twice: the bytes in
                                           ``tests/fixtures/nse/ownership/pledge_yesbank_EMPTY.json``
                                           and the measurement in the same directory's
                                           ``META.json``.
``fpi_ownership``                          HTTP 403 Akamai ``Access Denied`` on
                                           ``/api/quote-equity``, recorded **twice independently** —
                                           in ``tests/fixtures/nse/ownership/META.json`` and in
                                           ``tests/fixtures/nse/optionchain/``'s META — and the BSE
                                           equivalent ``FpiLimit`` 403 in
                                           ``tests/fixtures/bse/ROBOTS_EVIDENCE.json``.
``rbi_reference_rate``                     HTTP 418 from a WAF on ``robots.txt`` **and** on every
                                           POST, body "You are not authorized to view this page."
                                           ``tests/fixtures/nse/ROBOTS_EVIDENCE.json``.
``tri_index_return``                       four candidate routes probed, every one HTTP 404, and no
                                           total-return field in the only NSE index payload ever
                                           recorded here. ``UNAVAILABLE_SOURCES`` in
                                           ``src/bfinance/nse/macro.py`` plus the two committed
                                           ``tests/fixtures/nse/macro/ind_close_all_*.csv``.
=========================================  ==================================================================

One honest limitation, stated rather than papered over
------------------------------------------------------
For ``tri_index_return`` the four probed **route URLs** are not recorded in any committed artifact —
only the count ``probe_count: 4`` and the sentence claiming all four 404'd. So that verdict is
tied to (a) the internal consistency of the recorded count against the number its own text spells
out and (b) the absence of a total-return field from the only two NSE index payloads committed to
this repository, which is a real absence over real bytes. It is not tied to the 404 responses
themselves, because those were not kept. Every other refusal is tied to an HTTP status that is on
disk. Closing the TRI gap means committing the probe log; that is recorded work, not a test failure.

No network, no live route. Every artifact read here is committed, and the only module behaviour
exercised is ``bfinance.ownership.probe_pledge_payload`` handed the recorded bytes, which does no
I/O.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import pathlib
import re
from typing import Any

import pytest

import bfinance.ownership as ownership
from bfinance.nse import macro
from bfinance.sources.registry import DEFAULT, UNAVAILABLE

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
NSE_ROBOTS = FIXTURES / "nse" / "ROBOTS_EVIDENCE.json"
BSE_ROBOTS = FIXTURES / "bse" / "ROBOTS_EVIDENCE.json"
OWNERSHIP_DIR = FIXTURES / "nse" / "ownership"
PLEDGE_BODY = OWNERSHIP_DIR / "pledge_yesbank_EMPTY.json"
OPTIONCHAIN_META = (
    FIXTURES / "nse" / "optionchain" / "optionchain_v3_indices_closed_market.META.json"
)
MACRO_DIR = FIXTURES / "nse" / "macro"

#: Files that *record* a refusal are not themselves a payload, so the "is there a payload for this
#: capability on disk?" scan below has to exclude them or it would find its own evidence and
#: conclude the opposite of what it is checking.
EVIDENCE_SUFFIXES = ("META.json", "EVIDENCE.json")

#: The four, named here rather than read out of the registry, so that a refusal silently removed
#: from ``UNAVAILABLE`` fails this file instead of quietly shrinking it.
REFUSALS = ("tri_index_return", "rbi_reference_rate", "pledge_shares", "fpi_ownership")


def _load(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _reason(capability: str) -> str:
    for name, text in UNAVAILABLE:
        if name == capability:
            return text
    raise AssertionError(f"{capability} is no longer in bfinance.sources.registry.UNAVAILABLE")


def _data_fixture_keys() -> list[tuple[pathlib.Path, set[str]]]:
    """``(path, {json keys})`` for every committed JSON **data** fixture.

    Keys, not text, and the evidence files excluded. Both restrictions are load-bearing and both
    were arrived at by a false positive rather than by taste:

    * text. ``tests/fixtures/trendlyne/fundamental_results_motherson.json`` contains the string
      ``"IntOnBalancesWithRBI_Q"`` and ``corporate_actions/...curated.json`` contains the symbol
      ``IRBINVIT``. A keyword scan for ``rbi`` finds both, concludes the RBI rate has been
      committed, and is wrong about a company's interest income and a REIT's ticker.
    * the evidence files. They *are* the refusal records; scanning them finds the refusal and
      concludes the opposite of what is being checked.
    """
    found: list[tuple[pathlib.Path, set[str]]] = []
    for path in sorted(FIXTURES.rglob("*.json")):
        if path.name.endswith(EVIDENCE_SUFFIXES):
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        found.append((path, set(re.findall(r'"([^"]{1,60})"\s*:', text))))
    return found


#: A JSON key that would mean the payload carries FPI ownership: ``fpi`` in any case, or "foreign"
#: next to an ownership word. Deliberately not "foreign" alone - a P&L carries
#: ``ForeignExchangeFOB_A`` and a dividend line carries ``...InForeignCurrency_A``, neither of which
#: is an ownership percentage.
_FPI_PAYLOAD_KEY = re.compile(
    r"fpi|foreign.*(own|hold|limit|invest)|(own|hold|limit|invest).*foreign", re.I
)

#: A JSON key that would mean the payload carries the RBI reference rate: the rate concept
#: alongside the bank, or one of the USD/INR spellings. ``IntOnBalancesWithRBI_Q`` is a company's
#: interest income and must not match.
_RBI_PAYLOAD_KEY = re.compile(
    r"(reference.*(rate|rbi)|(rate|rbi).*reference|usd.?inr|inr.?usd)", re.I
)


def _files_matching(predicate: re.Pattern[str]) -> list[str]:
    """Committed JSON data fixtures with at least one key satisfying ``predicate``."""
    return sorted(
        str(path.relative_to(FIXTURES))
        for path, keys in _data_fixture_keys()
        if any(predicate.search(key) for key in keys)
    )


# --------------------------------------------------------------------------- #
# 1. pledge_shares - HTTP 200, 28 bytes, zero rows
# --------------------------------------------------------------------------- #

#: A recorded key that would mean the payload *does* carry pledge data. Derived from the field names
#: NSE uses elsewhere rather than from the refusal's prose, so the verdict is about the bytes.
PLEDGE_SHAPED = re.compile(r"pledge|encumbr|sharesexchange", re.IGNORECASE)


def _pledge_verdict(record: dict[str, Any], raw: bytes) -> str:
    """Re-derive why ``pledge_shares`` is unavailable, from the recorded measurement alone.

    Returns one of:

    ``evidence-drifted``
        the bytes on disk are not the bytes the META records. Every other verdict is meaningless
        once this happens, so it is checked first and short-circuits.
    ``route-blocked`` / ``route-missing``
        the recorded status says the edge refused or the path is gone — a *different* state from the
        one recorded, and one the module's refusal wording does not describe.
    ``publishes-pledge-rows``
        the recorded payload carries rows, or a key that names pledge data. This is the state in
        which the registered refusal would be a lie.
    ``reachable-empty-schema-unverified``
        the recorded state: a 200, zero rows, and no key naming pledge data anywhere in the body.
    """
    recorded_digest = record.get("stored_sha256")
    if recorded_digest and hashlib.sha256(raw).hexdigest() != recorded_digest:
        return "evidence-drifted"

    status = int(record["measured"]["http_status"])
    if status == 404:
        return "route-missing"
    if status >= 400:
        return "route-blocked"

    payload = json.loads(raw.decode("utf-8"))
    if payload.get("data"):
        return "publishes-pledge-rows"
    for key in _all_keys(payload):
        if PLEDGE_SHAPED.search(str(key)):
            return "publishes-pledge-rows"
    return "reachable-empty-schema-unverified"


def _all_keys(node: Any) -> list[str]:
    keys: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            keys.append(key)
            keys.extend(_all_keys(value))
    elif isinstance(node, list):
        for item in node:
            keys.extend(_all_keys(item))
    return keys


class TestPledgeSharesIsTiedToTheEmptyTwoHundred:
    """``/api/corporate-pledgedata`` answers 200 with nothing in it, and that is why it is refused."""

    def test_the_recorded_bytes_really_are_the_empty_two_hundred(self) -> None:
        record = _load(OWNERSHIP_DIR / "META.json")["captures"]["pledge_yesbank_EMPTY.json"]
        raw = PLEDGE_BODY.read_bytes()
        assert _pledge_verdict(record, raw) == "reachable-empty-schema-unverified"
        # The derived verdict is only as good as the artifact, so the two halves are pinned
        # separately: the recorded byte count, and the recorded digest of the same bytes.
        assert len(raw) == record["stored_bytes"] == 28
        assert hashlib.sha256(raw).hexdigest() == record["stored_sha256"]
        assert record["measured"]["http_status"] == 200
        assert record["measured"]["is_404"] is False
        assert json.loads(raw.decode("utf-8")) == {"comNameList": [], "data": []}

    def test_the_module_own_reader_agrees_with_the_recorded_measurement(self) -> None:
        """The tie: the recorded claim and the shipped reader must say the same thing.

        ``META.json`` records ``rows_returned: 0``. ``probe_pledge_payload`` is the function
        ``bfinance.ownership`` actually ships for this payload, so running it on the recorded bytes
        and requiring ``row_count == 0`` and ``row_schema_verified is False`` is a comparison
        between the measurement and the code that depends on it. A payload that ever gains a row
        makes ``row_schema_verified`` true or ``row_count`` non-zero, and the registered refusal
        becomes a lie at exactly that moment.
        """
        record = _load(OWNERSHIP_DIR / "META.json")["captures"]["pledge_yesbank_EMPTY.json"]
        probe = ownership.probe_pledge_payload(
            json.loads(PLEDGE_BODY.read_text(encoding="utf-8-sig")), url="recorded"
        )
        assert probe.row_count == record["measured"]["rows_returned"] == 0
        assert probe.row_schema_verified is False
        assert probe.reachable is True, "a 200 is not a 404; the recorded state is reachable"
        assert tuple(probe.observed_keys) == tuple(record["measured"]["top_level_keys_verbatim"])

    def test_the_registered_refusal_names_the_module_that_enforces_it(self) -> None:
        """The layer's table and the module that owns the decision must not drift apart.

        ``UNAVAILABLE`` is described in its own comment as "recording a decision those modules
        already made". That is only true while the reason still names the module making it, so the
        name is asserted rather than assumed.
        """
        reason = _reason("pledge_shares")
        assert "bfinance.ownership" in reason, (
            f"the pledge_shares refusal no longer names the module that enforces it: {reason!r}"
        )
        assert "pledge" in reason.lower()
        # And no registered source claims the capability, on either axis of the registry.
        assert DEFAULT.specs_for("pledge_shares") == ()
        owners = [
            spec.name
            for spec in DEFAULT.all_specs()
            if any("pledge" in capability for capability in spec.capabilities)
        ]
        assert owners == [], f"a registered source now claims a pledge capability: {owners}"

    def test_control_mutating_the_recorded_status_flips_the_verdict(self) -> None:
        """Negative control 1: the recorded status is an input, not decoration.

        403 is the state the FPI refusal is in, and it is a *different* state from the one recorded:
        a blocked route cannot be "reachable and empty". If flipping the status left the verdict
        alone, the derivation would be a constant wearing a function's clothes.
        """
        record = _load(OWNERSHIP_DIR / "META.json")["captures"]["pledge_yesbank_EMPTY.json"]
        raw = PLEDGE_BODY.read_bytes()
        assert _pledge_verdict(record, raw) == "reachable-empty-schema-unverified"

        blocked = copy.deepcopy(record)
        blocked["measured"]["http_status"] = 403
        assert _pledge_verdict(blocked, raw) == "route-blocked"
        assert _pledge_verdict(blocked, raw) != _pledge_verdict(record, raw)

        missing = copy.deepcopy(record)
        missing["measured"]["http_status"] = 404
        assert _pledge_verdict(missing, raw) == "route-missing"

    def test_control_a_payload_with_a_row_flips_the_verdict(self) -> None:
        """Negative control 2: the state the refusal exists to prevent, injected in memory.

        One row, one pledge-shaped key. That is all it takes for "NSE publishes no pledge data" to
        stop being true, and the derivation has to notice. Without this control, control 1 alone
        would leave open the possibility that the verdict only ever reflects the status code.
        """
        record = _load(OWNERSHIP_DIR / "META.json")["captures"]["pledge_yesbank_EMPTY.json"]
        published = json.dumps(
            {"comNameList": ["YESBANK"], "data": [{"pledgeQnt": 0, "pledgeVal": 0.0}]}
        ).encode("utf-8")
        # The digest no longer matches, so re-record it: this control is about the *payload*, and
        # the drift check is control 3.
        republished = copy.deepcopy(record)
        republished["stored_sha256"] = hashlib.sha256(published).hexdigest()
        assert _pledge_verdict(republished, published) == "publishes-pledge-rows"
        assert _pledge_verdict(record, PLEDGE_BODY.read_bytes()) == "reachable-empty-schema-unverified"

    def test_control_editing_the_committed_bytes_flips_the_verdict(self) -> None:
        """Negative control 3: the digest check is load-bearing, not decoration."""
        record = _load(OWNERSHIP_DIR / "META.json")["captures"]["pledge_yesbank_EMPTY.json"]
        tampered = b'{"comNameList":[],"data":[{"pledgeQnt":1}]}'
        assert _pledge_verdict(record, tampered) == "evidence-drifted"
        assert _pledge_verdict(record, PLEDGE_BODY.read_bytes()) != "evidence-drifted"


# --------------------------------------------------------------------------- #
# 2. fpi_ownership - HTTP 403 Akamai, recorded twice
# --------------------------------------------------------------------------- #

AKAMAI_REFUSAL = re.compile(r"access denied|edgesuite|not authorized", re.IGNORECASE)


def _fpi_observations() -> list[dict[str, Any]]:
    """Every recorded attempt to read FPI ownership, from every artifact that records one.

    Three independent recordings, deliberately: a refusal that rests on one probe is a belief, and
    a refusal that rests on three probes in three files is a measurement.
    """
    observations: list[dict[str, Any]] = []

    owner_meta = _load(OWNERSHIP_DIR / "META.json")["not_captured"]["fpi_foreign_ownership"]
    observations.append({
        "source": "tests/fixtures/nse/ownership/META.json",
        "route": owner_meta["would_live_at"],
        "status": owner_meta["http_status_observed"],
        "body": owner_meta["body"],
    })

    # The option-chain META records the same route's 403 in a different session's prose, keyed by
    # the route and valued with the outcome. Read as dict entries, because the route is the *key*
    # there and the status lives in the value.
    def harvest(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and "quote-equity" in str(key):
                    found = re.search(r"\b(\d{3})\b", value)
                    if found is not None:
                        observations.append({
                            "source": OPTIONCHAIN_META.name,
                            "route": str(key),
                            "status": int(found.group(1)),
                            "body": value,
                        })
                harvest(value)
        elif isinstance(node, list):
            for item in node:
                harvest(item)

    harvest(_load(OPTIONCHAIN_META))

    # And the other exchange's FPI route, from the BSE probe.
    for entry in _load(BSE_ROBOTS)["_per_url_verdicts"]:
        if "FpiLimit" in entry["url"]:
            observations.append({
                "source": "tests/fixtures/bse/ROBOTS_EVIDENCE.json",
                "route": entry["url"],
                "status": entry["http_status"],
                "body": entry["verdict"],
            })
    return observations


def _fpi_verdict(observations: list[dict[str, Any]]) -> str:
    """Re-derive whether FPI ownership is obtainable, from the recorded statuses alone.

    ``no-observation`` when nothing was probed — a refusal with no measurement behind it is exactly
    the belief this file exists to catch. ``blocked-everywhere`` when every recorded attempt is a
    4xx whose body is a refusal document. ``served-somewhere`` the moment one of them is not.
    """
    if not observations:
        return "no-observation"
    for entry in observations:
        status = int(entry["status"])
        if 200 <= status < 300:
            return "served-somewhere"
        if status < 400 or status >= 500:
            return f"odd-status-{status}"
        if not AKAMAI_REFUSAL.search(str(entry["body"])):
            return f"not-a-refusal-document-{status}"
    return "blocked-everywhere"


class TestFpiOwnershipIsTiedToTheFourOhThree:
    """``/api/quote-equity`` answers 403 from the Akamai edge, and no FPI payload is on disk."""

    def test_every_recorded_attempt_was_a_refusal_and_at_least_three_were_made(self) -> None:
        observations = _fpi_observations()
        assert _fpi_verdict(observations) == "blocked-everywhere"
        assert len(observations) >= 3, (
            f"only {len(observations)} recorded FPI attempt(s); the refusal used to rest on one"
        )
        assert len({entry["source"] for entry in observations}) >= 2, (
            "the attempts all come from one artifact, so they are one observation, not several"
        )
        for entry in observations:
            assert int(entry["status"]) == 403, entry
            assert AKAMAI_REFUSAL.search(str(entry["body"])), entry

    def test_the_module_refuses_and_says_so_without_issuing_the_request(self) -> None:
        assert ownership.fpi_ownership_status().available is False
        with pytest.raises(ownership.FpiOwnershipUnavailableError) as excinfo:
            ownership.fpi_ownership("RELIANCE")
        message = str(excinfo.value)
        assert "403" in message
        assert "/api/quote-equity" in message

    def test_no_fpi_payload_is_committed_anywhere_in_the_fixture_tree(self) -> None:
        """The half of the gap the layer's own test cannot see.

        A refusal justified by a 403 is only as good as "nobody ever saw a payload". So the whole
        committed fixture tree is scanned for an FPI payload — not for the word "FPI", which appears
        in the refusal records themselves and must be excluded, but for a JSON *data* file carrying
        an FPI-shaped key. The day one exists, ``fpi_ownership`` stops being unavailable and this
        goes red, which is the moment the refusal must be re-decided.
        """
        hits = _files_matching(_FPI_PAYLOAD_KEY)
        assert hits == [], (
            f"a committed fixture carries FPI ownership data, so the fpi_ownership refusal is "
            f"stale: {hits}"
        )

    def test_control_the_fpi_payload_key_rule_fires_on_a_payload(self) -> None:
        """Non-vacuity for the scan above. Without this, "no hits" proves nothing.

        A keyword scan that matched nothing on a tree where nothing is expected is the exact
        unfailable assertion this project has deleted three times, so the predicate is shown to
        fire on a payload of the shape the refusal is about, and to *not* fire on the near-misses
        that really are in the tree. That is also why the scan is over JSON **keys** rather than
        over file text: ``IRBINVIT`` in a corporate-actions fixture and
        ``IntOnBalancesWithRBI_Q`` in a trendlyne fixture both match a text scan for ``rbi``.
        """
        for payload_key in ("fpiLimit", "FpiLimitDt", "foreignOwnership", "FpiHolding"):
            assert _FPI_PAYLOAD_KEY.search(payload_key), (
                f"the FPI key rule does not fire on {payload_key!r}, so the scan above is not "
                "looking for the right thing"
            )
        for not_a_payload in (
            "ForeignExchangeFOB_A",
            "DividendRemittanceInForeignCurrency_A",
            "IntOnBalancesWithRBI_Q",
        ):
            assert not _FPI_PAYLOAD_KEY.search(not_a_payload), (
                f"the FPI key rule fires on {not_a_payload!r}, which is a company income statement "
                "line and not an ownership percentage"
            )

    def test_control_flipping_one_recorded_status_flips_the_verdict(self) -> None:
        """Negative control. One status, changed in memory, has to be enough."""
        observations = _fpi_observations()
        assert _fpi_verdict(observations) == "blocked-everywhere"

        served = copy.deepcopy(observations)
        served[0]["status"] = 200
        served[0]["body"] = '{"fpiLimit": 12.5, "foreignOwnership": 4.1}'
        assert _fpi_verdict(served) == "served-somewhere"
        assert _fpi_verdict(served) != _fpi_verdict(observations)

        empty = _fpi_verdict([])
        assert empty == "no-observation"
        assert empty != _fpi_verdict(observations)


# --------------------------------------------------------------------------- #
# 3. rbi_reference_rate - HTTP 418 from a WAF, on robots.txt and on every POST
# --------------------------------------------------------------------------- #


def _rbi_observation() -> dict[str, Any]:
    for host in _load(NSE_ROBOTS)["hosts"]:
        if host["host"] == "www.rbi.org.in":
            return host
    raise AssertionError("www.rbi.org.in is no longer recorded in tests/fixtures/nse/ROBOTS_EVIDENCE.json")


def _rbi_verdict(observation: dict[str, Any]) -> str:
    """Re-derive the RBI refusal from the recorded status and body.

    ``served`` when the recorded status is a 2xx — a client could then establish what the site
    permits and read the rate. ``refused`` when it is not, and the recorded body is a refusal
    document naming authorisation. ``odd-body`` otherwise, because a 4xx with a data body is
    neither of the two states the refusal describes.
    """
    status = int(observation["http_status"])
    if 200 <= status < 300:
        return "served"
    if not 400 <= status < 500:
        return f"odd-status-{status}"
    body = str(observation.get("body_verbatim", ""))
    if re.search(r"not authorized|forbidden|access denied", body, re.IGNORECASE):
        return "refused"
    return "odd-body"


class TestRbiReferenceRateIsTiedToTheFourOneEight:
    """The WAF refuses ``robots.txt`` *and* every POST, so a client cannot even establish consent."""

    def test_the_recorded_status_is_a_refusal_and_the_code_quotes_the_same_number(self) -> None:
        observation = _rbi_observation()
        assert _rbi_verdict(observation) == "refused"
        recorded_status = int(observation["http_status"])
        # The tie: the number the module refuses *with* is the number the fixture records. Read
        # from the fixture, so a changed recording is a red test rather than two agreeing literals.
        assert macro.UNAVAILABLE_SOURCES["rbi_reference_rate"]["http_status"] == recorded_status
        assert recorded_status == 418, (
            f"www.rbi.org.in now answers {recorded_status}, not 418. Re-derive the refusal: this "
            "is the exact event that would make it stale"
        )
        assert "not authorized" in observation["body_verbatim"].lower()

    def test_the_refusal_covers_both_the_robots_probe_and_every_post(self) -> None:
        """The claim is stronger than "the site is down", and the record has to support both halves.

        A 418 on ``robots.txt`` alone would mean *unknown* permission; a 418 on a POST alone would
        mean a blocked request. Together they mean a client cannot establish what the site permits
        at all, which is the actual justification for not reading a rate a bank would apply to a
        trade. So the recorded verdict text has to name both, or the refusal has outgrown its
        evidence.
        """
        observation = _rbi_observation()
        recorded = observation["verdict"].lower()
        assert "robots.txt" in recorded
        assert "post" in recorded
        assert "418" in recorded
        # And the module's own reason repeats the same two halves, so the two cannot drift.
        why = macro.UNAVAILABLE_SOURCES["rbi_reference_rate"]["why"]
        assert "robots.txt" in why.lower() and "POST" in why
        assert macro.UNAVAILABLE_SOURCES["rbi_reference_rate"]["not_circumvented"] is True
        # __doPostBack/__VIEWSTATE is the reason a 200 form shell is not a payload.
        assert "__doPostBack" in why and "__VIEWSTATE" in why

    def test_the_accessor_raises_and_never_attempts_a_request(self) -> None:
        with pytest.raises(macro.SourceUnavailableError) as excinfo:
            macro.rbi_reference_rate()
        message = str(excinfo.value)
        assert "418" in message
        assert "www.rbi.org.in" in message
        # Any call shape lands on the same answer, which is the whole reason for *args/**kwargs.
        with pytest.raises(macro.SourceUnavailableError):
            macro.rbi_reference_rate("anything", year=2026)

    def test_no_rbi_rate_payload_is_committed(self) -> None:
        hits = _files_matching(_RBI_PAYLOAD_KEY)
        assert hits == [], (
            f"a committed fixture carries an RBI reference rate, so the refusal is stale: {hits}"
        )

    def test_control_the_rbi_key_rule_fires_on_a_rate_and_not_on_a_p_and_l_line(self) -> None:
        """Non-vacuity, in both directions, for the scan above.

        ``IntOnBalancesWithRBI_Q`` really is a key in ``tests/fixtures/trendlyne/``, and a keyword
        scan for ``rbi`` finds it. It is a company's interest income, not the reference rate, which
        is why the predicate is on the *key* and requires the rate concept alongside the bank.
        """
        for rate_key in ("rbiReferenceRate", "RBI Reference Rate", "referenceRate", "USDINR"):
            assert _RBI_PAYLOAD_KEY.search(rate_key), (
                f"the RBI key rule does not fire on {rate_key!r}, so the scan above is not looking "
                "for the right thing"
            )
        for not_a_rate in ("IntOnBalancesWithRBI_Q", "ForeignExchangeGainsLosses_A"):
            assert not _RBI_PAYLOAD_KEY.search(not_a_rate), (
                f"the RBI key rule fires on {not_a_rate!r}, which is a company income statement "
                "line and not the reference rate"
            )

    def test_control_flipping_the_recorded_status_flips_the_verdict(self) -> None:
        """Negative control. The WAF lifting would be visible here, which is the point of recording it."""
        observation = _rbi_observation()
        assert _rbi_verdict(observation) == "refused"

        served = copy.deepcopy(observation)
        served["http_status"] = 200
        assert _rbi_verdict(served) == "served"
        assert _rbi_verdict(served) != _rbi_verdict(observation)

        # And a 4xx that is not a refusal document is a third state, not a variant of "refused".
        odd = copy.deepcopy(observation)
        odd["http_status"] = 404
        odd["body_verbatim"] = "<html><head><title>Not Found</title></head></html>"
        assert _rbi_verdict(odd) == "odd-body"
        assert _rbi_verdict(odd) != _rbi_verdict(observation)


# --------------------------------------------------------------------------- #
# 4. tri_index_return - four routes probed, all 404, and no total-return field recorded
# --------------------------------------------------------------------------- #

#: A total-return field in an NSE index payload. NSE spells the concept several ways; the point is
#: that *any* of them appearing in a committed index-close CSV would contradict the refusal.
TOTAL_RETURN_FIELD = re.compile(r"total\s*return|\btri\b|div\s*yield\s*reinvest|net\s*tr", re.I)


def _observed_index_close_schema() -> set[str]:
    """Every column label in every committed NSE index-close payload, read from the CSVs."""
    labels: set[str] = set()
    for path in sorted(MACRO_DIR.glob("ind_close_all_*.csv")):
        with path.open(encoding="utf-8-sig", newline="") as handle:
            labels.update(next(csv.reader(handle)))
    return labels


def _tri_verdict(recorded: dict[str, Any], observed_labels: set[str]) -> str:
    """Re-derive the TRI refusal from the recorded probe claim and the observed schema.

    ``claim-inconsistent`` when the recorded integer count and the number its own sentence spells
    out disagree — the recorded fact contradicting itself is the cheapest possible signal that one
    of the two was edited without the other. ``total-return-published`` when any committed index
    payload carries a total-return field. ``no-route`` otherwise.
    """
    why = str(recorded.get("why", ""))
    spelled = re.search(r"\b(\w+)\s+candidate routes\b", why)
    if spelled is not None:
        words = {
            "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
            "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        }
        claimed = words.get(spelled.group(1).lower())
        if claimed is not None and claimed != int(recorded["probe_count"]):
            return "claim-inconsistent"
    for label in observed_labels:
        if TOTAL_RETURN_FIELD.search(label):
            return "total-return-published"
    return "no-route"


class TestTriIndexReturnIsTiedToTheFourFourOhFours:
    """No route exists, and the only NSE index payload committed here carries no total-return field."""

    def test_the_recorded_count_and_its_own_sentence_agree(self) -> None:
        recorded = macro.UNAVAILABLE_SOURCES["index_tri"]
        labels = _observed_index_close_schema()
        assert _tri_verdict(recorded, labels) == "no-route"
        assert recorded["available"] is False
        assert int(recorded["probe_count"]) == 4
        assert "404" in recorded["why"]
        # The spellings are checked against the payloads, not read off the refusal's prose.
        assert len(labels) == 13, f"the committed index-close schema changed: {sorted(labels)}"
        assert not any(TOTAL_RETURN_FIELD.search(label) for label in labels)
        assert "P/E" in labels and "Div Yield" in labels, (
            "the schema no longer carries the fields the refusal's reasoning is about"
        )

    def test_no_public_accessor_exists_for_a_total_return_series(self) -> None:
        """The refusal's other half: there is no ``tri``/``total_return`` function to call.

        An accessor for a series that does not exist is how an approximation gets written later —
        compounding a price index with a dividend yield, which is wrong by construction because it
        cannot know the reinvestment date. So the absence is asserted, not assumed.
        """
        import ast

        tree = ast.parse(pathlib.Path(macro.__file__).read_text(encoding="utf-8-sig"))
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        forbidden = sorted(
            name
            for name in defined
            if re.search(r"(^|_)(tri|total_?return)(_|$)", name, re.IGNORECASE)
        )
        assert forbidden == [], f"a total-return accessor appeared in macro.py: {forbidden}"

    def test_control_a_total_return_column_flips_the_verdict(self) -> None:
        """Negative control 1: a header row that answers the question the refusal denies."""
        recorded = macro.UNAVAILABLE_SOURCES["index_tri"]
        labels = _observed_index_close_schema()
        assert _tri_verdict(recorded, labels) == "no-route"
        with_tri = labels | {"Index Name", "Total Return Index Value"}
        assert _tri_verdict(recorded, with_tri) == "total-return-published"
        assert _tri_verdict(recorded, with_tri) != _tri_verdict(recorded, labels)

    def test_control_editing_one_side_of_the_recorded_claim_flips_the_verdict(self) -> None:
        """Negative control 2: the count and the sentence are cross-checked against each other.

        Change either alone and the two disagree, which is the signature of a fact that was edited
        without its own explanation being updated.
        """
        labels = _observed_index_close_schema()
        recorded = macro.UNAVAILABLE_SOURCES["index_tri"]

        fewer = copy.deepcopy(recorded)
        fewer["probe_count"] = 1
        assert _tri_verdict(fewer, labels) == "claim-inconsistent"

        reworded = copy.deepcopy(recorded)
        reworded["why"] = "Two candidate routes were probed and one of them returned HTTP 200."
        assert _tri_verdict(reworded, labels) == "claim-inconsistent"

        assert _tri_verdict(recorded, labels) != "claim-inconsistent"


# --------------------------------------------------------------------------- #
# 5. the frame around all four
# --------------------------------------------------------------------------- #


def test_all_four_refusals_are_registered_and_named_by_this_file() -> None:
    """If one is dropped from ``UNAVAILABLE`` this file fails, rather than quietly getting smaller."""
    assert tuple(name for name, _ in UNAVAILABLE) == REFUSALS
    for capability in REFUSALS:
        assert DEFAULT.is_registered(capability)
        assert DEFAULT.unavailable_reason(capability)


@pytest.mark.parametrize("capability", REFUSALS)
def test_each_refusal_is_decided_by_the_module_that_owns_the_data(capability: str) -> None:
    """A registered refusal must point at the module that enforces it, or it is only a note.

    ``UNAVAILABLE``'s own comment says it is "recording a decision those modules already made". Two
    of the four are enforced upstream (the cascade never runs) and two are enforced inside the
    owning module; the reason has to say which, and naming the module is how a reader can check.
    """
    reason = _reason(capability)
    owner = {
        "tri_index_return": "bfinance.nse.macro",
        "rbi_reference_rate": "bfinance.nse.macro",
        "pledge_shares": "bfinance.ownership",
        "fpi_ownership": "bfinance.ownership",
    }[capability]
    assert owner in reason, f"{capability}'s reason does not name {owner}: {reason!r}"


def test_the_four_refusals_are_distinguishable_from_one_another() -> None:
    """Four refusals with four reasons, or three of them are one refusal wearing four names.

    A caller who gets ``pledge_shares`` refused and ``fpi_ownership`` refused with the same sentence
    learns nothing, and the two have nothing to do with each other: one route is reachable and
    empty, the other is blocked at the edge.
    """
    reasons = {capability: _reason(capability) for capability in REFUSALS}
    assert len(set(reasons.values())) == len(REFUSALS)
    # And each reason is specific enough to act on, not a restatement of the capability's absence.
    for capability, reason in reasons.items():
        assert len(reason) > 100, f"{capability}'s reason is too short to be a measurement"
