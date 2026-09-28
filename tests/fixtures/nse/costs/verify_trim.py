"""
Prove the trimmed circular fixtures are behaviour-neutral for rate extraction.

The two circulars are 358,718 and 163,365 bytes of PDF, of which 4,087 and 3,083 characters are
text. The stored fixtures are 3,504 and 1,799 bytes of that text, trimmed. This script is the
evidence that trimming changed nothing the parsers read: it builds a schedule from the FULL
pypdf extraction and from the TRIMMED excerpts, and compares a 16-field snapshot plus a full
round-trip quote.

Run it only when a fixture is regenerated — it needs the original PDFs and ``pypdf``, which are
not test dependencies::

    uv run --with pypdf python tests/fixtures/nse/costs/verify_trim.py

The offline test suite proves the same neutrality on the trimmed side alone
(``test_the_trimmed_fixture_reproduces_every_transcribed_rate``), because that is all it has.
"""

from __future__ import annotations

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "src"))

from bfinance import costs  # noqa: E402

#: Point these at the downloaded originals to re-run the proof.
ORIGINALS = {
    "NSE_FATAX/73524": pathlib.Path("/path/to/FATAX73524.pdf"),
    "NSE/FA/73061": pathlib.Path("/path/to/FA73061.pdf"),
}
EXCERPTS = {
    "NSE/FATAX/73524": HERE / "NSE_FATAX_73524_stt_rates.txt",
    "NSE/FA/73061": HERE / "NSE_FA_73061_exchange_charges.txt",
}

FIELDS = 16


def snapshot(schedule: costs.CostSchedule) -> dict:
    """The 16 values a trim must not change. Every one is a rate, a date, or a citation."""
    return {
        "stt_new_rates": {k: (v.pct, v.payer, v.previous_pct, v.marked_no_change)
                          for k, v in sorted(schedule.stt_regimes[-1].rates.items())},
        "stt_previous_rates": {k: (v.pct, v.payer, v.previous_pct, v.marked_no_change)
                               for k, v in sorted(schedule.stt_regimes[0].rates.items())},
        "stt_circular_ref": schedule.stt_circular.circular.ref,
        "stt_circular_reference": schedule.stt_circular.circular.circular_ref,
        "stt_issued": schedule.stt_circular.circular.issued.isoformat(),
        "stt_effective_from": schedule.stt_circular.effective_from.isoformat(),
        "stt_lineage": schedule.stt_circular.lineage,
        "stt_statute": schedule.stt_circular.statute,
        "exchange_circular_ref": schedule.exchange_circular.circular.ref,
        "exchange_effective_from": schedule.exchange_circular.effective_from.isoformat(),
        "sebi_directive": schedule.exchange_circular.sebi_directive,
        "exchange_predecessors": schedule.exchange_circular.predecessors,
        "exchange_segments": {k: v.to_dict()
                              for k, v in sorted(schedule.exchange_circular.existing.items())},
        "exchange_derived_prior": {k: v.to_dict()
                                   for k, v in sorted(schedule.exchange_circular.prior.items())},
        "exchange_regime_bounds": [(r.effective_from.isoformat(),
                                    r.effective_to.isoformat() if r.effective_to else None)
                                   for r in schedule.exchange_regimes],
        "stt_regime_bounds": [(r.effective_from.isoformat(),
                               r.effective_to.isoformat() if r.effective_to else None)
                              for r in schedule.stt_regimes],
    }


def main() -> int:
    missing = [p for p in ORIGINALS.values() if not p.is_file()]
    if missing:
        print("Cannot prove neutrality without the original PDFs. Missing:")
        for path in missing:
            print(f"  {path}")
        print("\nSet ORIGINALS above, then re-run with pypdf installed.")
        return 2

    from pypdf import PdfReader  # type: ignore

    full_texts = {
        ref: "\n".join((page.extract_text() or "") for page in PdfReader(str(p)).pages)
        for ref, p in ORIGINALS.items()
    }
    excerpt_texts = {ref: p.read_text(encoding="utf-8") for ref, p in EXCERPTS.items()}

    full = costs.schedule_from_circulars(
        full_texts["NSE/FATAX/73524"], full_texts["NSE/FA/73061"]
    )
    trimmed = costs.schedule_from_circulars(
        excerpt_texts["NSE/FATAX/73524"], excerpt_texts["NSE/FA/73061"]
    )

    a, b = snapshot(full), snapshot(trimmed)
    failures = [key for key in a if a[key] != b[key]]
    for key in a:
        print(f"  {'OK  ' if a[key] == b[key] else 'DIFF'} {key}")
        if a[key] != b[key]:
            print(f"       full:  {a[key]}")
            print(f"       trim:  {b[key]}")

    quote_args = dict(buy_shape="delivery_buy", sell_shape="delivery_sell",
                      trade_date="2026-05-01", buy_amount=1e7)
    qa = costs.quote(**quote_args, schedule=full)
    qb = costs.quote(**quote_args, schedule=trimmed)
    quote_same = qa.to_dict() == qb.to_dict()
    print(f"\n  full-text chars : "
          f"{ {k: len(v) for k, v in full_texts.items()} }")
    print(f"  excerpt bytes   : "
          f"{ {k: len(v.encode('utf-8')) for k, v in excerpt_texts.items()} }")
    print(f"  fields compared : {len(a)}")
    print(f"  quote identical : {quote_same}  -> {qb.round_trip_bps:.5f} bps")

    if failures or not quote_same:
        print("\nFAILED: the trim is not behaviour-neutral.")
        return 1
    print("\nPASS: the trim changed nothing the rate parsers read.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
