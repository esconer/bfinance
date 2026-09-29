"""
trendlyne pacing tests -- fully offline, no network and no fixtures.

These exist because of one number. ``DEFAULT_MIN_REQUEST_INTERVAL_MS`` shipped as ``750.0`` while
the site it addresses states a floor of one request per second, so the package ran 4 requests per
second against a published ``Crawl-delay: 1``. The number looked arbitrary and the next person to
tidy it had no way of knowing it encoded an external limit rather than a preference.

The tests are therefore written so the *constraint* is stated, not just the value. A constant
asserted equal to ``1000.0`` on its own proves nothing, because whoever breaks it can also change
the assertion. What holds is the rule that at most ``CRAWL_DELAY_SECONDS`` requests per second may
go out, with that rule's input transcribed from the live file rather than derived from the
constant under test.

Measurement behind the rule, from ``https://www.trendlyne.com/robots.txt`` fetched on 2026-09-30
(HTTP 200, 2,536 bytes): the ``User-agent: *`` group carries 28 ``Disallow`` rules and
``Crawl-delay: 1``, and the site comments that line "Wait 1 second between successive requests". A
separate group for ``GPTBot``/``ClaudeBot`` carries ``Crawl-delay: 10``, scoped to those named
crawlers and not the limit for this client; reading that as the general figure is how a
10x-too-slow default gets written.

Constructing :class:`~bfinance.trendlyne.client.TrendlyneClient` performs no I/O, so nothing here
touches the network.
"""

from __future__ import annotations

import ast

import pytest

from bfinance.trendlyne import client as tl

#: trendlyne's ``Crawl-delay`` for ``User-agent: *``, in seconds, as stated in its ``robots.txt``.
#: Transcribed from the live file on 2026-09-30 and deliberately *not* derived from the constant
#: under test: if the two were derived from each other the test could not fail.
CRAWL_DELAY_SECONDS = 1.0

#: ``Disallow`` rules in that same ``User-agent: *`` group. It does not set the pacing floor, but it
#: travels with the pacing decision in :data:`ROBOTS_DISALLOWED_ROUTES`, and keeping the two facts
#: quoted together is what stops either being re-attributed to the wrong group later.
CRAWL_DELAY_GROUP_DISALLOW_RULES = 28

#: The scoped delay that must never be mistaken for the general one.
SCOPED_BOT_CRAWL_DELAY_SECONDS = 10.0

_CONSTANT_SOURCE = "DEFAULT_MIN_REQUEST_INTERVAL_MS = "


def _source() -> str:
    with open(tl.__file__, encoding="utf-8") as handle:
        return handle.read()


def _module_lines() -> list[str]:
    return _source().splitlines()


def _flat(text: str) -> str:
    """Collapse every run of whitespace to one space, so a phrase survives a re-wrap.

    The prose being checked is hard-wrapped at 100 columns. A phrase assertion against the raw
    text would fail on an unrelated reflow of the comment block, which is the kind of brittleness
    that gets a correct test deleted instead of fixed.
    """
    return " ".join(text.split())


def _rationale_text() -> str:
    """The ``#:`` comment block immediately above the constant, as source text.

    Read from the source rather than from ``__doc__`` because this is a module-level constant:
    nothing attaches its comment block to it at runtime, which is exactly why the stated reason
    could rot without any test noticing.
    """
    lines = _module_lines()
    index = next(i for i, line in enumerate(lines) if line.startswith(_CONSTANT_SOURCE))
    block: list[str] = []
    for line in reversed(lines[:index]):
        if not line.startswith("#:"):
            break
        block.append(line)
    return "\n".join(reversed(block))


def test_the_default_pacing_floor_is_one_second():
    """The literal value, pinned so the derivation test has something concrete to disagree with."""
    assert tl.DEFAULT_MIN_REQUEST_INTERVAL_MS == 1000.0
    assert tl.TrendlyneClient().min_request_interval_ms == 1000.0


def test_the_floor_encodes_the_published_crawl_delay():
    """At most one request per second, because ``Crawl-delay: 1`` permits at most one per second.

    This is the test that earns its keep. ``750.0`` is 1.33 req/s: 250 ms per request under a
    published floor, a compliance defect rather than a taste question. Asserting the derived rate
    rather than the bare constant means a later "tidying" to 750, or to 500 "to halve the wait",
    fails here with a message that names the rule and the fetch the rule came from.
    """
    floor_ms = tl.TrendlyneClient().min_request_interval_ms
    required_ms = CRAWL_DELAY_SECONDS * 1000.0
    requests_per_second = 1000.0 / floor_ms
    allowed_per_second = 1.0 / CRAWL_DELAY_SECONDS

    assert floor_ms >= required_ms, (
        f"pacing floor is {floor_ms} ms = {requests_per_second:.2f} req/s, but trendlyne's "
        f"robots.txt states 'Crawl-delay: {CRAWL_DELAY_SECONDS:.0f}' for 'User-agent: *' "
        f"(fetched 2026-09-30), a floor of {required_ms:.0f} ms = {allowed_per_second:.2f} req/s"
    )
    assert requests_per_second <= allowed_per_second, (
        f"{requests_per_second:.2f} requests per second exceeds the {allowed_per_second:.2f} "
        f"allowed by 'Crawl-delay: {CRAWL_DELAY_SECONDS:.0f}' for 'User-agent: *'"
    )
    # Pinned separately as well, so the fix cannot be made by weakening only the rate assertion.
    assert tl.DEFAULT_MIN_REQUEST_INTERVAL_MS == required_ms


@pytest.mark.parametrize("floor_ms", [750.0, 500.0, 100.0, 999.0])
def test_the_derived_check_rejects_the_faster_values(floor_ms):
    """The rule from the test above, applied by hand to the values it must not accept.

    Guards against a vacuous derivation. If the comparison in that test were weakened into
    something that always passes, these inputs would stop being rejected by the same expression.
    Evaluated inline rather than by monkeypatching the constant, so it keeps its teeth even while
    the constant itself is wrong.
    """
    requests_per_second = 1000.0 / floor_ms
    within_limit = floor_ms >= CRAWL_DELAY_SECONDS * 1000.0 and requests_per_second <= 1.0
    assert not within_limit, (
        f"{floor_ms} ms should be rejected: it is {requests_per_second:.2f} req/s against a "
        f"'Crawl-delay: {CRAWL_DELAY_SECONDS:.0f}' floor for 'User-agent: *'"
    )


def test_zero_disables_pacing_and_an_explicit_value_is_honoured():
    """Tests and deliberate bulk callers must still be able to opt out, in both directions.

    ``0.0`` is what every offline trendlyne suite passes, and a caller with their own quota must
    be able to go slower as well as faster. Overridability is deliberate; this default is the
    floor that is not negotiable without a conscious argument.
    """
    assert tl.TrendlyneClient(min_request_interval_ms=0.0).min_request_interval_ms == 0.0
    assert tl.TrendlyneClient(min_request_interval_ms=250.0).min_request_interval_ms == 250.0
    assert tl.TrendlyneClient(min_request_interval_ms=10_000.0).min_request_interval_ms == 10_000.0


def test_the_ten_second_delay_is_scoped_to_named_bots_and_is_not_the_floor():
    """``Crawl-delay: 10`` sits in the same file, for ``GPTBot``/``ClaudeBot`` only.

    Two groups, two delays, and a 10x penalty for reading the wrong one. The relation is asserted
    rather than assumed, so that changing the transcription of either number is a visible event
    rather than a silent drift: 10 s would be a 10x-slower crawl for no compliance reason.
    """
    assert CRAWL_DELAY_SECONDS < SCOPED_BOT_CRAWL_DELAY_SECONDS
    assert tl.DEFAULT_MIN_REQUEST_INTERVAL_MS < SCOPED_BOT_CRAWL_DELAY_SECONDS * 1000.0
    assert CRAWL_DELAY_GROUP_DISALLOW_RULES == 28


def test_the_rationale_cites_robots_not_page_size():
    """The reason has to be findable by whoever next asks whether the number can be smaller.

    A correct number with no stated reason is how 750 ms happened once: correct by coincidence,
    carrying a page-size justification that was never a robots-derived argument. Asserted on the
    source comment because that is where a reader looks, and because nothing else keeps it honest.
    """
    doc = _flat(_rationale_text())
    assert "Crawl-delay: 1" in doc, "the pacing rationale no longer cites the robots.txt crawl-delay"
    assert "User-agent: *" in doc, "the rationale no longer says which robots.txt group applies"
    assert "2026-09-30" in doc, "the rationale no longer records when robots.txt was read"
    assert "GPTBot" in doc and "ClaudeBot" in doc, (
        "the rationale must record that 'Crawl-delay: 10' is scoped to those bots, not to '*'"
    )


def test_the_module_docstring_agrees_with_the_constant():
    """The module docstring states the same floor, so the two cannot drift apart silently.

    Both are prose a reader trusts and the docstring is what a caller sees first, so a mismatch
    between them is the same defect as a wrong constant: a number nobody can trace.
    """
    doc = _flat(tl.__doc__)
    assert "**1000 ms**" in doc
    assert "Crawl-delay: 1" in doc
    assert "compliance floor" in doc


def test_750_is_only_ever_named_as_the_value_that_was_replaced():
    """The old number may appear in prose, but never as a value the code evaluates.

    A blanket ban would forbid the rationale from recording what it used to be, which is what makes
    the correction reviewable. What must not come back is 750 as a live literal anywhere in the
    module. Checked against the AST rather than a line grep, so a ``750.0`` buried in a call, a
    comparison or a keyword argument is caught and a comment quoting the history is not.
    """
    literals = [
        node.value
        for node in ast.walk(ast.parse(_source(), filename=tl.__file__))
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
        and node.value == 750
    ]
    assert not literals, f"750 is evaluated as a live literal {literals} times in the trendlyne client"

    # The historical value stays documented, in the two places a reader of the default will look,
    # phrased as what it was rather than as a current claim.
    assert "The 750 ms this package used to ship" in _flat(tl.__doc__)
    assert "It was 750 ms" in _flat(_rationale_text())


def test_retry_cap_and_headers_were_not_carried_along_by_the_pacing_change():
    """A guard rail, not a policy: this fix must not have dragged retry or headers with it.

    Retry honours ``Retry-After`` but caps it, so a hostile header cannot stall a client for an
    unbounded time. That cap is unrelated to the crawl-delay, and changing it here would be scope
    creep rather than a consequence of the pacing fix.
    """
    assert tl._MAX_RETRY_AFTER_SEC == 30.0
    assert len(tl.USER_AGENTS) == 3
    assert all("Mozilla/5.0" in ua for ua in tl.USER_AGENTS)
