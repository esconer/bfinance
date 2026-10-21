"""
The trendlyne layer's three guards, each tied to recorded evidence, with a control per verdict.

The gap this file closes
-----------------------
``tests/test_trendlyne_sections.py`` pins all three guards **behaviourally**: that
``TrendlyneWafChallengeError`` is raised and not retried, that a non-``latest`` share-holding
period raises ``TrendlyneServerError`` with zero requests spent, and that an insider frame at the
cap is labelled ``TRUNCATED``. Those are real tests and they are not duplicated here. But every
response they are handed is built inside the test file, so what they establish is that the shipped
code does what it does — never that what it does is *right*.

That is the same unfailable assertion ``tests/test_refusal_evidence.py`` was written to remove:
a test asserting a refusal is true is as weak as a test asserting a constant, and a guard whose
trigger was invented by the test asserting it cannot be wrong about the trigger.

The shape here is that file's, applied to the trendlyne layer. Every verdict is **derived** from a
committed artifact — bytes on disk plus the URL ``tests/fixtures/trendlyne/META.json`` records for
them — by a function that takes the artifact as its only input, and every derivation is paired
with a control that mutates the recorded artifact in memory and requires the verdict to **change**.

What is tied, and to what
=========================
============================  =====================================================
guard                        the committed artifact behind it
============================  =====================================================
WAF, terminal                **none.** See the stated limitation below.
share-holding snapshot-only  ``shareholding_tcs.html`` + its recorded ``latest`` URL
insider row cap              ``insider_trading_tcs.html``, 175 ``<tr>`` on disk
============================  =====================================================

One honest limitation, stated rather than papered over
------------------------------------------------------
**No trendlyne 405 response is committed anywhere in this repository.** The WAF guard's trigger is
a measurement recorded in ``src/bfinance/trendlyne/client.py``'s docstring and in
``TrendlyneWafChallengeError``'s message, and that prose is not bytes on disk. So the WAF verdict
here is tied to the strongest thing that *is* committed — the recorded vendor payloads, which
demonstrate the guard is a discrimination and not a blanket refusal, and which it must therefore
let through — rather than to the 405 itself. What a 405 would add, and what its absence costs, is
said at the point of use rather than left for a reader to discover. Closing the gap means
committing the response; that is recorded work, not a test failure.

The absence is asserted, not assumed: a committed capture of the share-holding route at a
non-``latest`` period would be the artifact that decides that guard the other way, and the test
that looks for one says so when it finds nothing.

No network, no live route. Every artifact read here is committed, the only module behaviour
exercised runs through ``httpx.MockTransport``, and nothing in this file is ``live``-marked.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from typing import Any, Dict, List, NamedTuple, Tuple

import httpx

from bfinance.trendlyne import client as tl

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "trendlyne"
META = json.loads((FIXTURES / "META.json").read_text(encoding="utf-8-sig"))["files"]


# --------------------------------------------------------------------------- #
# reading the recorded artifacts
# --------------------------------------------------------------------------- #

def _recorded_bytes(name: str) -> bytes:
    """The committed bytes for a recorded capture, with the recording checked against them.

    The digest where ``META.json`` carries one, and the byte count always. The older MOTHERSON-era
    entries record a 16-hex digest of the *original* response plus a stored byte count rather than a
    digest of the stored bytes, so checking the half each entry actually recorded is stronger than
    checking neither.
    """
    raw = (FIXTURES / name).read_bytes()
    record = META[name]
    if "stored_sha256" in record:
        assert hashlib.sha256(raw).hexdigest() == record["stored_sha256"], (
            f"{name} on disk is not the bytes META.json records; every verdict below is void"
        )
    assert len(raw) == record["stored_bytes"], (
        f"{name} is {len(raw)} bytes, META.json records {record['stored_bytes']}"
    )
    return raw


def _recorded_url(name: str) -> str:
    """The exact URL ``META.json`` says this capture came from."""
    record = META[name]
    assert record["http_status"] == 200, (
        f"{name} was not a 200 capture, so it cannot carry the vendor's own answer: "
        f"{record['url']}"
    )
    return record["url"]


class Capture(NamedTuple):
    """A capture as the recorder left it: the URL it came from and the bytes it was.

    This pair is the only input the snapshot verdict below takes, and both halves are committed.
    """

    url: str
    body: bytes


def _capture(name: str) -> Capture:
    return Capture(_recorded_url(name), _recorded_bytes(name))


# --------------------------------------------------------------------------- #
# 1. the WAF guard
# --------------------------------------------------------------------------- #

class RecordedResponse(NamedTuple):
    """A recorded response: a status, its headers, and whatever body came with it.

    Named ``RecordedResponse`` so the gap is visible at every call site — and ``body`` defaults to
    empty, because on the 405 that is not a placeholder, it is the fact: the response was not kept.
    """

    status: int
    headers: Dict[str, str]
    body: bytes = b""


def _waf_verdict(recorded: RecordedResponse) -> str:
    """Re-derive what the edge did to this response, from the response alone.

    ``waf-challenged``
        HTTP 405, or any status carrying ``x-amzn-waf-action``. The header is checked before the
        status because WAF attaches it independently: a challenge served as a 200 is a captcha
        page, and reading that page as data is how a challenge becomes a parse error.
    ``refused-not-waf``
        a 4xx/5xx with no such header — an ordinary block or an outage, which the layer maps to a
        different exception and is free to retry.
    ``data``
        a 2xx with no such header. The state every committed capture in this repository is in, and
        the one a blanket refusal would destroy.
    """
    action = recorded.headers.get("x-amzn-waf-action")
    if recorded.status == 405 or action:
        return "waf-challenged"
    if not 200 <= recorded.status < 300:
        return "refused-not-waf"
    return "data"


#: A route the shipped guard is exercised through. Any path will do — the guard reads the status and
#: the header and never the URL, which is one of the things the controls below lean on.
_PROBE_PATH = "/equity/bulk-block-deals/MOTHERSON/878/samvardhana-motherson-international-ltd/"


def _shipped_waf_verdict(recorded: RecordedResponse) -> str:
    """The same question, asked of the shipped guard rather than of the derivation.

    :meth:`bfinance.trendlyne.client.TrendlyneClient.fetch_text` over ``httpx.MockTransport``,
    which performs no I/O of any kind. ``max_retries=2``, so a guard that *did* retry would be
    visible as more than one handler call; the call count is therefore asserted alongside.
    """
    calls: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            recorded.status, content=recorded.body, headers=dict(recorded.headers)
        )

    client = tl.TrendlyneClient(
        transport=httpx.MockTransport(handler), min_request_interval_ms=0.0, max_retries=2
    )
    try:
        client.fetch_text(_PROBE_PATH)
    except tl.TrendlyneError as exc:
        # Every input here is a *terminal* status, so one call is the contract: a guard that
        # retried would spend more of a budget it has already been told is gone. (429 is
        # deliberately absent from the table — it is the one status the layer does retry, and
        # proving that here would cost a real backoff sleep for nothing.)
        assert len(calls) == 1, f"the shipped guard retried {len(calls)} times on {recorded.status}"
        return type(exc).__name__
    return "data"


class TestTheWafGuardIsTiedToTheStatusAndTheHeader:
    """Two recorded forms of the challenge, and the derived verdict must move when either does."""

    def test_a_recorded_four_oh_five_is_a_challenge(self) -> None:
        assert _waf_verdict(RecordedResponse(405, {})) == "waf-challenged"

    def test_a_recorded_waf_header_is_a_challenge_at_any_status(self) -> None:
        """Including on a 200, which is the form that silently returns a captcha page as data."""
        for status in (200, 403, 404, 500):
            assert _waf_verdict(
                RecordedResponse(status, {"x-amzn-waf-action": "captcha"})
            ) == "waf-challenged", status

    def test_every_committed_capture_is_in_the_data_state(self) -> None:
        """The half that *can* be tied to bytes: each committed capture is a 200 with no header.

        This is what shows the guard is a discrimination and not a blanket refusal. It reads the
        artifacts rather than asserting a constant, so it fails if the fixture set is ever replaced
        by something the guard would have refused.
        """
        for name in sorted(META):
            recorded = RecordedResponse(META[name]["http_status"], {}, _recorded_bytes(name))
            assert _waf_verdict(recorded) == "data", name

    def test_control_flipping_the_status_flips_the_verdict(self) -> None:
        """Negative control 1: drop 405 to 404, remove the header, and it is an ordinary block."""
        challenged = RecordedResponse(405, {})
        assert _waf_verdict(challenged) == "waf-challenged"

        not_challenged = RecordedResponse(404, {})
        assert _waf_verdict(not_challenged) == "refused-not-waf"
        assert _waf_verdict(not_challenged) != _waf_verdict(challenged)

    def test_control_flipping_the_header_flips_the_verdict(self) -> None:
        """Negative control 2: the same 200, with and without the header, on the *same* bytes.

        The body is held constant on purpose. If the verdict moved because the body changed, the
        guard would be reading the captcha page rather than the response's own status — the exact
        failure mode the header check exists to catch.
        """
        body = _recorded_bytes("bulk_deals_motherson.html")
        challenged = RecordedResponse(200, {"x-amzn-waf-action": "challenge"}, body)
        served = RecordedResponse(200, {}, body)

        assert _waf_verdict(challenged) == "waf-challenged"
        assert _waf_verdict(served) == "data"
        assert _waf_verdict(served) != _waf_verdict(challenged)
        assert challenged.body == served.body

    def test_control_the_derivation_and_the_shipped_guard_agree_on_every_input(self) -> None:
        """The tie. A derivation nobody compares against the code would agree with anything.

        Six recorded inputs — both challenge forms, three ordinary refusals, and the committed 200
        — run through the derivation and through the shipped guard, which must classify each
        identically. The negatives carry the weight: an equivalence that holds only on the
        positives is a derivation fitted to the shipped code rather than checked against it.
        """
        body = _recorded_bytes("bulk_deals_motherson.html")
        inputs: Tuple[RecordedResponse, ...] = (
            RecordedResponse(405, {}),
            RecordedResponse(200, {"x-amzn-waf-action": "challenge"}, body),
            RecordedResponse(403, {}),
            RecordedResponse(444, {}),
            RecordedResponse(404, {}),
            RecordedResponse(200, {}, body),
        )
        shipped: Tuple[str, ...] = (
            "TrendlyneWafChallengeError",
            "TrendlyneWafChallengeError",
            "TrendlyneAccessDeniedError",
            "TrendlyneAccessDeniedError",
            "TrendlyneNotFoundError",
            "data",
        )
        for recorded, want in zip(inputs, shipped):
            got = _shipped_waf_verdict(recorded)
            assert got == want, (
                f"the shipped guard classifies {recorded.status} {recorded.headers} as {got}, "
                f"not {want}; the verdict derived here would then be about a guard that changed"
            )
            # The derivation is asserted *against the shipped classification*, not against the
            # literal above. That is what makes it a second derivation and not a restatement.
            derived = _waf_verdict(recorded)
            if want == "TrendlyneWafChallengeError":
                assert derived == "waf-challenged", recorded
            elif want == "data":
                assert derived == "data", recorded
            else:
                assert derived == "refused-not-waf", recorded

    def test_the_stated_limitation_is_still_true(self) -> None:
        """No committed capture is a non-200, so the gap is asserted, not just written down.

        The day one is — a 405 above all — this goes red and the verdict above can be tied to the
        real response instead of to the recorded payloads. That is the moment to do the work.
        """
        non_200 = {name: r["http_status"] for name, r in META.items() if r["http_status"] != 200}
        assert non_200 == {}, (
            f"a non-200 trendlyne capture now exists ({non_200}); tie the WAF guard to it"
        )

    def test_a_plain_four_oh_three_is_not_this_guard_s_business(self) -> None:
        """Why the above matters. Not every block is the WAF, and the two are not retryable alike.

        A WAF verdict on a plain 403 would make the client treat a block it could retry as a spent
        session, and spend a cooldown on it.
        """
        assert _waf_verdict(RecordedResponse(403, {})) == "refused-not-waf"
        assert _waf_verdict(
            RecordedResponse(403, {"x-amzn-waf-action": "captcha"})
        ) == "waf-challenged"


# --------------------------------------------------------------------------- #
# 2. the share-holding snapshot-only guard
# --------------------------------------------------------------------------- #

_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
#: A period label as trendlyne spells it in a table header: ``Jun 2026`` (quarter columns) or
#: ``Sep-2026`` (month spans). Anchored on the cell, so ``Holding Jun 2026`` — a holder-category
#: header on the very same page — is not counted as a period.
_PERIOD_LABEL = re.compile(rf"^{_MONTH}[- ]\d{{4}}$")
_CELL = re.compile(r"<th\b[^>]*>(.*?)</th>", re.IGNORECASE | re.DOTALL)


def _period_labels(body: bytes) -> List[str]:
    """The period labels a recorded page publishes in its own header cells."""
    text = body.decode("utf-8-sig", errors="replace")
    return [
        label
        for label in (re.sub(r"\s+", " ", cell).strip() for cell in _CELL.findall(text))
        if _PERIOD_LABEL.match(label)
    ]


def _period_segment(url: str, endpoint: str) -> str:
    """The ``{period}`` segment of a recorded URL, located through the route template.

    Not pinned to an index: ``ENDPOINTS[endpoint].path`` says which segment is the period, so a
    route whose template gains or loses a segment cannot leave this quietly reading the wrong one.
    """
    template = tl.ENDPOINTS[endpoint].strip("/").split("/")
    recorded = url.split("://", 1)[-1].split("/", 1)[-1].strip("/").split("/")
    if "{period}" not in template:
        raise AssertionError(f"{endpoint} has no period segment to read: {template}")
    slot = template.index("{period}")
    if slot >= len(recorded):
        raise AssertionError(f"{url} is shorter than the {endpoint} template: {template}")
    return recorded[slot]


def _one_period_page(body: bytes) -> bytes:
    """The committed page with every period-labelled header cell but the first blanked out.

    The in-memory mutation for control 2: same path, same page, one period left. Everything else —
    the row labels, the table, the page chrome — is untouched, so a verdict that moved would have
    moved because of the period columns and nothing else.
    """
    seen = 0

    def blank(match: "re.Match[str]") -> str:
        nonlocal seen
        label = re.sub(r"\s+", " ", match.group(1)).strip()
        if not _PERIOD_LABEL.match(label):
            return match.group(0)
        seen += 1
        return match.group(0) if seen == 1 else "<th>dropped</th>"

    return re.sub(
        r"<th\b[^>]*>(.*?)</th>", blank, body.decode("utf-8-sig"), flags=re.DOTALL
    ).encode("utf-8")


def _snapshot_verdict(capture: Capture) -> str:
    """Re-derive what one request on this route is worth, from the capture alone.

    ``period-segment-refused``
        the recorded path asks for a period other than ``latest``. Measured: this route answers
        HTTP 500 for every such segment, deterministically, so the client refuses it locally and
        spends no request. Note what this verdict is *not* derived from — a 500 body, because none
        was kept. See the module docstring.
    ``one-request-is-the-whole-history``
        the ``latest`` page, and its own header cells publish **more than one** period. That is
        what makes refusing to walk the route honest: the single unaddressable snapshot already
        carries every quarter the page holds.
    ``one-request-is-a-single-point``
        ``latest``, but the page publishes one period. The snapshot is then worth exactly what a
        snapshot is, and the claim above would be false.
    """
    if _period_segment(capture.url, "share_holding_page") != tl.SNAPSHOT_PERIOD:
        return "period-segment-refused"
    if len(set(_period_labels(capture.body))) >= 2:
        return "one-request-is-the-whole-history"
    return "one-request-is-a-single-point"


class TestTheShareHoldingRouteIsTiedToItsLatestCapture:
    """One committed capture, read for the period it was taken at and for the periods it holds."""

    def test_the_recorded_capture_is_a_latest_page_publishing_many_periods(self) -> None:
        capture = _capture("shareholding_tcs.html")
        assert _snapshot_verdict(capture) == "one-request-is-the-whole-history"

        labels = set(_period_labels(capture.body))
        assert len(labels) >= 4, f"the recorded grid published only {sorted(labels)}"
        # The two halves pinned apart, so a re-recorded capture that lost its history is red here
        # rather than silently flipping the verdict above.
        assert _period_segment(capture.url, "share_holding_page") == tl.SNAPSHOT_PERIOD
        assert _period_segment(capture.url, "monthly_mf_holding_page") == tl.SNAPSHOT_PERIOD

    def test_the_older_capture_agrees(self) -> None:
        """Two independent captures of the same route, a symbol and two months apart.

        One capture of one symbol on one day is an observation. Two are a property of the route.
        """
        assert _snapshot_verdict(
            _capture("shareholding_motherson.html")
        ) == "one-request-is-the-whole-history"

    def test_control_a_non_latest_path_flips_the_verdict(self) -> None:
        """Negative control 1: the recorded path, with its period segment changed in memory.

        Same bytes, different segment. The guard exists because the vendor answers 500 for that
        segment, so the route's *value* and its *walkability* are two different questions and the
        verdict has to answer the second one.
        """
        capture = _capture("shareholding_tcs.html")
        assert _snapshot_verdict(capture) == "one-request-is-the-whole-history"

        for period in ("Jun-2026", "2024", "2023"):
            moved = Capture(
                capture.url.replace(f"/{tl.SNAPSHOT_PERIOD}/", f"/{period}/"), capture.body
            )
            assert _snapshot_verdict(moved) == "period-segment-refused", period
            assert _snapshot_verdict(moved) != _snapshot_verdict(capture)

    def test_control_a_page_with_one_period_flips_the_verdict(self) -> None:
        """Negative control 2: the bytes are an input too, not only the path.

        Every period-labelled header cell but the first is blanked, in memory. The path still says
        ``latest``; the claim the guard's note rests on — one request is worth having because the
        page embeds every quarter — no longer holds on these bytes.
        """
        capture = _capture("shareholding_tcs.html")
        thinned = _one_period_page(capture.body)
        assert len(set(_period_labels(thinned))) == 1

        assert _snapshot_verdict(Capture(capture.url, thinned)) == "one-request-is-a-single-point"
        assert _snapshot_verdict(Capture(capture.url, thinned)) != _snapshot_verdict(capture)

    def test_no_committed_capture_asks_this_route_for_a_period(self) -> None:
        """The absence, which is the other half of the refusal: no evidence the route can be walked.

        Scanned over the URLs ``META.json`` records, not over a hardcoded list, so a capture added
        tomorrow at ``Jun-2026`` turns this red — the moment the guard must be re-decided rather
        than merely re-measured.
        """
        asked = {
            name: _period_segment(record["url"], "share_holding_page")
            for name, record in META.items()
            if "/equity/share-holding/" in record["url"]
        }
        assert asked, "the fixture set no longer holds a share-holding capture at all"
        assert set(asked.values()) == {tl.SNAPSHOT_PERIOD}, (
            f"a committed capture asks the snapshot-only route for a period: {asked}"
        )

    def test_control_the_capture_scan_is_not_fooled_by_the_mutual_fund_route(self) -> None:
        """Non-vacuity for the scan above, in the one direction that has already fooled this repo.

        ``/equity/monthly-mutual-fund-share-holding/`` **contains** ``share-holding``, and a
        committed capture of it exists. A substring match would collect that capture as evidence
        about the wrong route and conclude the segment means something there too — which is the
        precise thing that makes this guard route-scoped.
        """
        mf_url = _recorded_url("monthly_mf_holding_tcs.html")
        assert "share-holding" in mf_url, "the counter-example URL no longer looks like the trap"
        assert "/equity/share-holding/" not in mf_url

        collected = [r["url"] for r in META.values() if "/equity/share-holding/" in r["url"]]
        assert mf_url not in collected

        # And the segment reader finds a real period on both routes, which is what makes the
        # verdict above a question about the *segment* rather than about route identification.
        assert _period_segment(mf_url, "monthly_mf_holding_page") == tl.SNAPSHOT_PERIOD
        assert _period_segment(
            mf_url.replace(f"/{tl.SNAPSHOT_PERIOD}/", "/Jun-2026/"), "monthly_mf_holding_page"
        ) == "Jun-2026"

    def test_the_mutual_fund_capture_publishes_month_spans_the_latest_page_cannot(self) -> None:
        """Why the guard is scoped to one route, read off the committed counter-example bytes.

        The mutual-fund capture's header cells carry explicit ``Sep-2026`` / ``Aug-2026`` /
        ``Jul-2026`` month spans — a ragged three-month grid the page can walk, because the period
        segment there filters. A blanket snapshot guard would have broken the one route where the
        segment means something.
        """
        mf_labels = set(_period_labels(_recorded_bytes("monthly_mf_holding_tcs.html")))
        assert {"Sep-2026", "Aug-2026", "Jul-2026"} <= mf_labels, sorted(mf_labels)

        share_labels = set(_period_labels(_recorded_bytes("shareholding_tcs.html")))
        assert share_labels, "the share-holding capture publishes no period at all now"
        assert not any("-" in label for label in share_labels), (
            "the share-holding grid is quarters; a month-spelled label means the two routes' "
            "grids have converged and the counter-example above is no longer one"
        )


# --------------------------------------------------------------------------- #
# 3. the insider row cap
# --------------------------------------------------------------------------- #

_ROW = re.compile(r"<tr\b.*?</tr>", re.IGNORECASE | re.DOTALL)
_HEADER_CELL = re.compile(r"<th\b", re.IGNORECASE)


def _insider_rows(body: bytes) -> int:
    """Data rows in a recorded insider-trading page, counted from the bytes.

    Header rows are the ones carrying ``<th>``, excluded by that mark rather than by subtracting a
    literal — so a capture with no header at all counts its rows honestly instead of coming out
    one short of the truth.
    """
    rows = _ROW.findall(body.decode("utf-8-sig", errors="replace"))
    return sum(1 for row in rows if not _HEADER_CELL.search(row))


def _insider_verdict(body: bytes) -> str:
    """Re-derive whether this frame can be called the whole history, from the page alone.

    ``truncated-at-the-cap``
        the page holds exactly :data:`INSIDER_ROW_CAP` data rows, which is the measured
        server-side cap — so it was cut, and saying so is the only honest description.
    ``below-cap-completeness-unverified``
        fewer rows than the cap. This is **not** a statement that the frame is complete: nothing
        measured says what the vendor holds beyond them, and the verdict is named to stop that
        distinction collapsing.
    """
    return "truncated-at-the-cap" if _insider_rows(body) >= tl.INSIDER_ROW_CAP else (
        "below-cap-completeness-unverified"
    )


def _rebuilt_with_rows(body: bytes, total: int) -> bytes:
    """The committed page, rebuilt in memory to hold exactly ``total`` data rows.

    Built by repeating the capture's *own* data rows rather than by synthesising markup, so the
    controls below vary the row count and nothing else: the header row is byte-identical, and a
    verdict that moved because the table's signature changed would show up as a flip here.
    """
    text = body.decode("utf-8-sig")
    rows = _ROW.findall(text)
    headers = [row for row in rows if _HEADER_CELL.search(row)]
    data = [row for row in rows if not _HEADER_CELL.search(row)]
    assert len(headers) == 1, f"expected one header row, found {len(headers)}"
    assert data, "the capture carries no data row to repeat"
    rebuilt = headers[0] + "".join((data * (total // len(data) + 1))[:total])
    head = text[: text.index(headers[0])]
    tail = text[text.rindex("</tr>") + len("</tr>"):]
    return (head + rebuilt + tail).encode("utf-8")


class TestTheInsiderRowCapIsTiedToTheRecordedRowCount:
    """The cap is a number, the frame publishes a count, and this reads both off the same bytes."""

    def test_the_recorded_capture_is_below_the_cap(self) -> None:
        body = _recorded_bytes("insider_trading_tcs.html")
        assert _insider_verdict(body) == "below-cap-completeness-unverified"

        # The META note writes "175 <tr> = 1 header + 174 data rows" in prose; the count above is
        # derived from the bytes. Two independent readings of one artifact.
        assert _insider_rows(body) == 174
        assert len(_ROW.findall(body.decode("utf-8-sig"))) == 175
        assert "1 header + 174 data rows" in META["insider_trading_tcs.html"]["notes"]
        assert tl.INSIDER_ROW_CAP == 500

    def test_control_a_frame_at_exactly_the_cap_flips_the_verdict(self) -> None:
        """Negative control 1: the recorded page, grown in memory to exactly the cap."""
        body = _recorded_bytes("insider_trading_tcs.html")
        at_cap = _rebuilt_with_rows(body, tl.INSIDER_ROW_CAP)

        assert _insider_rows(at_cap) == tl.INSIDER_ROW_CAP
        assert _insider_verdict(at_cap) == "truncated-at-the-cap"
        assert _insider_verdict(at_cap) != _insider_verdict(body)

    def test_control_one_row_short_of_the_cap_does_not_trip_it(self) -> None:
        """Negative control 2: the off-by-one. A cap guard that fires early is as wrong as one that
        never fires — 499 is a real page, not a truncated one, and calling it truncated tells a
        caller to distrust a frame that was cut at nothing.
        """
        body = _recorded_bytes("insider_trading_tcs.html")
        nearly = _rebuilt_with_rows(body, tl.INSIDER_ROW_CAP - 1)

        assert _insider_rows(nearly) == tl.INSIDER_ROW_CAP - 1
        assert _insider_verdict(nearly) == "below-cap-completeness-unverified"
        assert _insider_verdict(nearly) == _insider_verdict(body)

    def test_control_the_row_count_is_a_count_and_not_a_constant(self) -> None:
        """Non-vacuity for every verdict above: the counter moves when the bytes move."""
        body = _recorded_bytes("insider_trading_tcs.html")
        assert {_insider_rows(_rebuilt_with_rows(body, n)) for n in (0, 1, 7, 499, 500, 501)} == {
            0, 1, 7, 499, 500, 501
        }

        # And one data row removed from the *committed* capture changes the count, so the recorded
        # 174 is a reading of the artifact rather than a number the file was born knowing.
        text = body.decode("utf-8-sig")
        first_data = next(row for row in _ROW.findall(text) if not _HEADER_CELL.search(row))
        start = text.index(first_data)
        cut = (text[:start] + text[start + len(first_data):]).encode("utf-8")
        assert _insider_rows(cut) == _insider_rows(body) - 1

    def test_the_shipped_frame_publishes_the_count_derived_from_these_bytes(self) -> None:
        """The tie. The number the guard reasons about is the number this file derives.

        The count is computed here from the committed capture and compared against what the client
        stamps on the frame built from those same bytes, so a change to either side alone — the
        counter here or the extractor there — makes the two disagree.
        """
        body = _recorded_bytes("insider_trading_tcs.html")
        recorded_rows = _insider_rows(body)
        frame = _insider_frame_from(body)

        assert len(frame) == recorded_rows
        assert frame.attrs["bfinance_trendlyne_insider_rows"] == recorded_rows
        assert frame.attrs["bfinance_trendlyne_insider_row_cap"] == tl.INSIDER_ROW_CAP
        assert frame.attrs["bfinance_trendlyne_insider_rows_capped"] is False
        assert "does NOT mean it is complete" in frame.attrs["bfinance_trendlyne_note"]


def _insider_frame_from(body: bytes) -> Any:
    """The frame the shipped client builds from these bytes, over ``httpx.MockTransport``."""
    from test_trendlyne_core import mock_client

    sitemap = (FIXTURES / "sitemap_excerpt.xml").read_text(encoding="utf-8-sig")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "sitemap" in url:
            return httpx.Response(200, text=sitemap)
        if "insider-trading-sast" in url:
            return httpx.Response(200, content=body)
        return httpx.Response(404, text="Not Found")

    client = mock_client(handler, allow_member_search=False)
    client.load_symbol_index(sitemap)
    return client.insider_trading("MOTHERSON")


# --------------------------------------------------------------------------- #
# 4. the frame around all three
# --------------------------------------------------------------------------- #


def test_each_verdict_is_a_function_of_its_artifact_and_not_a_constant() -> None:
    """Every guard's verdict, shown to move when the recorded artifact moves.

    One table, one row per guard: the recorded artifact and two in-memory mutations of it, each of
    which must produce a verdict different from the recorded one. A derivation that could not do
    this is the unfailable assertion this file exists to prevent, and it is cheaper to check all
    three here than to trust that each was written correctly.
    """
    share = _capture("shareholding_tcs.html")
    insider = _recorded_bytes("insider_trading_tcs.html")
    deals = _recorded_bytes("bulk_deals_motherson.html")

    rows: Tuple[Tuple[str, str, List[str]], ...] = (
        (
            "waf",
            # The recorded artifact is the committed payload: a 200 with no WAF header. Both
            # mutations below are the challenge arriving instead — once as a status, once as a
            # header on those same bytes.
            _waf_verdict(RecordedResponse(200, {}, deals)),
            [
                _waf_verdict(RecordedResponse(405, {})),
                _waf_verdict(RecordedResponse(200, {"x-amzn-waf-action": "challenge"}, deals)),
            ],
        ),
        (
            "share-holding snapshot-only",
            _snapshot_verdict(share),
            [
                _snapshot_verdict(
                    Capture(share.url.replace(f"/{tl.SNAPSHOT_PERIOD}/", "/2024/"), share.body)
                ),
                _snapshot_verdict(Capture(share.url, _one_period_page(share.body))),
            ],
        ),
        (
            "insider row cap",
            _insider_verdict(insider),
            [_insider_verdict(_rebuilt_with_rows(insider, tl.INSIDER_ROW_CAP))],
        ),
    )

    for name, recorded, mutated in rows:
        assert recorded, f"{name}: no verdict was derived at all"
        for other in mutated:
            assert other != recorded, (
                f"{name}: mutating the recorded artifact left the verdict at {recorded!r}"
            )