"""
Regression tests for what the AI-context renderers actually hand to an LLM.

`AIContextBuilder.build_markdown_context` is embedded verbatim in
`AIPromptFactory.investment_memo`, and `build_json_context` is shipped to models by
`BFinanceAITools.execute_tool`. A malformed cell ("nan", "1.05578e+06", "10 Years::")
is therefore a data-quality defect, not cosmetics: each one is a token an LLM can
read as a number.

Every test here is offline — a synthetic CompanyProfile, no network. The real
screener.in markup for the CAGR tables is embedded as a literal (SCREENER_CAGR_HTML)
so the parser can be checked without a fetch.
"""

import json
import re

import pandas as pd
import pytest
from bs4 import BeautifulSoup

from bfinance.ai import AIContextBuilder, AIPromptFactory
from bfinance.ai.context import _safe_markdown
from bfinance.ai.tools import _sanitize_non_finite
from bfinance.models.company import (
    AnalysisInsights,
    CompanyProfile,
    PeerStock,
    TopRatios,
)
from bfinance.models.statements import FinancialStatement
from bfinance.screener.parser import ScreenerHTMLParser

#: The label a missing cell must carry. Spelled out here (not imported from the
#: renderer) so this file states the expected output independently of the code.
MISSING_CELL = "N/A"

#: Verbatim `.ranges-table` markup from
#: https://www.screener.in/company/RELIANCE/consolidated/ (captured 2026-09-27; TCS
#: and HDFCBANK have the identical shape). The metric label is the
#: `<th colspan="2">`; every remaining row is a period/value pair.
SCREENER_CAGR_HTML = """
<html><body>
<table class="ranges-table">
 <tr><th colspan="2">Compounded Sales Growth</th></tr>
 <tr><td>10 Years:</td><td>15%</td></tr>
 <tr><td>5 Years:</td><td>18%</td></tr>
 <tr><td>3 Years:</td><td>6%</td></tr>
 <tr><td>TTM:</td><td>15%</td></tr>
</table>
<table class="ranges-table">
 <tr><th colspan="2">Stock Price CAGR</th></tr>
 <tr><td>10 Years:</td><td>18%</td></tr>
 <tr><td>1 Year:</td><td>-11%</td></tr>
</table>
</body></html>
"""

#: Every key `build_json_context` emitted before period labels were added. The fix is
#: additive: this set must keep working unchanged for existing consumers.
LEGACY_JSON_KEYS = {
    "symbol", "company_name", "bse_code", "nse_symbol", "sector", "industry",
    "sub_industry", "indices", "about", "valuation", "cagrs", "pros_and_cons",
    "annual_pnl", "quarterly_pnl", "balance_sheet", "cash_flow",
    "shareholding_quarterly", "shareholding_yearly", "concalls", "peers",
}

#: (json key, CompanyProfile attribute) for every statement the dossier ships.
STATEMENTS = (
    ("annual_pnl", "profit_loss"),
    ("quarterly_pnl", "quarters"),
    ("balance_sheet", "balance_sheet"),
    ("cash_flow", "cash_flow"),
    ("shareholding_quarterly", "shareholding"),
    ("shareholding_yearly", "shareholding_yearly"),
)


def _synthetic_profile() -> CompanyProfile:
    """A profile shaped like the real one: Rs Cr magnitudes, ratios, and real gaps."""
    return CompanyProfile(
        symbol="TESTCO",
        company_id=1,
        name="Test Industries Ltd",
        nse_symbol="TESTCO",
        bse_code="500001",
        sector="Energy",
        industry="Petroleum",
        sub_industry="Refineries",
        about="A synthetic company used to render the dossier offline.",
        indices=["Nifty 50"],
        ratios=TopRatios(
            current_price=1287.0, market_cap=1742179.0, stock_pe=23.3, roce=10.3, roe=8.91
        ),
        analysis=AnalysisInsights(cons=["low ROE"]),
        cagrs={
            "Compounded Sales Growth": {
                "10 Years:": "15%", "5 Years:": "18%", "3 Years:": "6%", "TTM:": "15%",
            },
            "Stock Price CAGR": {"10 Years:": "18%", "1 Year:": "-11%"},
        },
        profit_loss=FinancialStatement(
            headers=["Mar 2025", "Mar 2026", "TTM"],
            rows={
                "Sales": [962820.0, 1055780.0, 1123060.0],
                "OPM %": [17.33, 16.96, 16.35],
                "Tax %": [24.0, 22.0, None],  # screener has no tax % for TTM
                "EPS in Rs": [51.47, 59.69, 55.22],
            },
        ),
        balance_sheet=FinancialStatement(
            headers=["Mar 2025", "Mar 2026"],
            rows={
                "Reserves": [829668.0, 890498.0],
                "Total Assets": [1949710.0, 2177550.0],
            },
        ),
        cash_flow=FinancialStatement(
            headers=["Mar 2025", "Mar 2026"],
            rows={
                "Cash from Operating Activity": [178703.0, 192113.0],
                "Cash from Investing Activity": [-137535.0, -101089.0],
            },
        ),
        quarters=FinancialStatement(
            headers=["Mar 2025", "Jun 2026"],
            rows={
                "Sales": [261388.0, 309468.0],
                "Raw PDF": [None, None],  # screener emits a label-only row
            },
        ),
        ratios_history=FinancialStatement(
            headers=["Mar 2025", "Mar 2026"],
            rows={"Debtor Days": [16.0, 20.0], "ROCE %": [10.35, 10.11]},
        ),
        shareholding=FinancialStatement(
            headers=["Mar 2025", "Jun 2026"],
            rows={
                "Promoters": [50.1, 50.48],
                "No. of Shareholders": [4765730.0, 4651860.0],
            },
        ),
        peers=[
            PeerStock(
                rank=1, name="Peer One", cmp=239.1, pe=76.38,
                market_cap_cr=157881.0, roce=1.86, dividend_yield=0.25,
            ),
            PeerStock(
                rank=2, name="Peer Two", cmp=407.85, pe=None,
                market_cap_cr=111644.0, roce=8.72, dividend_yield=0.0,
            ),
        ],
    )


def _cells(md: str, label: str, occurrence: int = 0) -> list:
    """Cells of the `occurrence`-th markdown table row whose first cell is `label`."""
    found = []
    for line in md.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and cells[0] == label:
            found.append(cells)
    assert found, f"no table row labelled {label!r} in dossier"
    return found[occurrence]


def _row_containing(md: str, text: str) -> list:
    """Cells of the single markdown table row that contains `text`."""
    for line in md.splitlines():
        if text in line and line.strip().startswith("|"):
            return [c.strip() for c in line.strip().strip("|").split("|")]
    raise AssertionError(f"no table row containing {text!r} in dossier")


# --------------------------------------------------------------------------- #
# Defect 1 — a missing value rendered as the bare token "nan"
# --------------------------------------------------------------------------- #

def test_dossier_never_prints_a_bare_nan_token():
    """`nan` inside a numeric column reads as a value; missing must be labelled."""
    md = AIContextBuilder.build_markdown_context(_synthetic_profile())

    assert re.findall(r"(?i)\bnan\b", md) == []
    # The three real gap shapes from the committed dossiers:
    assert _cells(md, "Tax %")[-1] == MISSING_CELL           # income statement cell
    assert _cells(md, "Raw PDF") == ["Raw PDF", MISSING_CELL, MISSING_CELL]
    # peer row: rank, name, cmp, pe, market_cap_cr, roce, dividend_yield
    assert _row_containing(md, "Peer Two")[3] == MISSING_CELL


def test_safe_markdown_labels_missing_cells_and_keeps_ratios_readable():
    md = _safe_markdown(pd.DataFrame({"Mar 2026": [1123060.0, None], "Promoters": [50.1, 0.17]}))

    assert re.findall(r"(?i)\bnan\b", md) == []
    assert MISSING_CELL in md
    # a column with no large values keeps its native precision (not "50.100000")
    assert "0.17" in md and "50.1" in md


def test_safe_markdown_falls_back_to_plain_text_without_tabulate(monkeypatch):
    """The ImportError fallback must survive, and must not print 'nan' either."""

    def _no_tabulate(self, *args, **kwargs):
        raise ImportError("Missing optional dependency 'tabulate'")

    monkeypatch.setattr(pd.DataFrame, "to_markdown", _no_tabulate)
    out = _safe_markdown(pd.DataFrame({"Mar 2026": [1123060.0, None]}))

    assert out.startswith("```") and out.rstrip().endswith("```")
    assert MISSING_CELL in out
    assert re.findall(r"(?i)\bnan\b", out) == []


# --------------------------------------------------------------------------- #
# Defect 2 — scientific notation in a table headed "in Rs Cr"
# --------------------------------------------------------------------------- #

def test_dossier_never_uses_scientific_notation():
    md = AIContextBuilder.build_markdown_context(_synthetic_profile())

    assert re.findall(r"\d(?:\.\d+)?e[+-]\d+", md) == []
    assert _cells(md, "Total Assets")[1:] == ["1,949,710", "2,177,550"]
    assert _cells(md, "No. of Shareholders")[1] == "4,765,730.00"
    # annual income statement: that Rs Cr column also holds EPS/%, so 2 decimals
    assert _cells(md, "Sales")[2] == "1,055,780.00"
    assert _cells(md, "EPS in Rs")[1:] == ["51.47", "59.69", "55.22"]


def test_safe_markdown_never_uses_scientific_notation():
    md = _safe_markdown(pd.DataFrame({"Mar 2026": [1123060.0, 2177550.0]}))

    assert re.findall(r"\d(?:\.\d+)?e[+-]\d+", md) == []
    assert "1,123,060" in md and "2,177,550" in md


# --------------------------------------------------------------------------- #
# Defect 3 — the JSON dossier dropped period labels
# --------------------------------------------------------------------------- #

def test_json_context_carries_period_labels_for_every_statement():
    profile = _synthetic_profile()
    data = AIContextBuilder.build_json_context(profile)

    assert data["annual_pnl_periods"] == ["Mar 2025", "Mar 2026", "TTM"]
    for key, attribute in STATEMENTS:
        statement = getattr(profile, attribute)
        assert data[f"{key}_periods"] == statement.headers
        for metric, values in data[key].items():
            # value i belongs to period i: the mapping is recoverable again
            assert len(values) == len(data[f"{key}_periods"]), metric


def test_json_context_keeps_legacy_keys_unchanged():
    """The period labels are additive: no existing key renamed, moved or restructured."""
    data = AIContextBuilder.build_json_context(_synthetic_profile())

    assert LEGACY_JSON_KEYS <= set(data)
    assert data["annual_pnl"] == {
        "Sales": [962820.0, 1055780.0, 1123060.0],
        "OPM %": [17.33, 16.96, 16.35],
        "Tax %": [24.0, 22.0, None],
        "EPS in Rs": [51.47, 59.69, 55.22],
    }


def test_json_tool_payload_labels_periods_end_to_end():
    """Mirrors the json branch of BFinanceAITools.execute_tool: sanitize + dumps."""
    data = _sanitize_non_finite(AIContextBuilder.build_json_context(_synthetic_profile()))
    payload = json.dumps(data, indent=2, allow_nan=False)

    assert '"annual_pnl_periods"' in payload
    assert "Mar 2026" in payload
    assert "TTM" in payload


# --------------------------------------------------------------------------- #
# Defect 4 — doubled colon in the CAGR block
# --------------------------------------------------------------------------- #

def test_cagr_period_labels_are_not_double_coloned():
    md = AIContextBuilder.build_markdown_context(_synthetic_profile())

    assert re.findall(r"\w+::", md) == []
    assert (
        "- **Compounded Sales Growth**: 10 Years: 15% | 5 Years: 18% | "
        "3 Years: 6% | TTM: 15%"
    ) in md


def test_parsed_cagr_keys_render_without_doubling_colons():
    """End to end: real screener markup -> parser -> dossier."""
    cagrs = ScreenerHTMLParser()._parse_cagrs(BeautifulSoup(SCREENER_CAGR_HTML, "lxml"))
    profile = _synthetic_profile()
    profile.cagrs = cagrs

    md = AIContextBuilder.build_markdown_context(profile)

    assert re.findall(r"\w+::", md) == []
    assert (
        "- **Compounded Sales Growth**: 10 Years: 15% | 5 Years: 18% | 3 Years: 6% | TTM: 15%"
    ) in md
    assert "- **Stock Price CAGR**: 10 Years: 18% | 1 Year: -11%" in md


def test_parse_cagrs_keeps_the_metric_label_and_reads_period_rows():
    """Structural guard (passes before and after): what `_parse_cagrs` must keep doing.

    In the live markup the metric label ("Compounded Sales Growth", "Stock Price
    CAGR", ...) is the `<th colspan="2">` header and every remaining row is a
    period/value pair, so the label is the dict key and is not lost, and the period
    cells are data rows rather than a misread header. If screener.in ever ships a
    different shape, this is the test that should fail first.
    """
    parsed = ScreenerHTMLParser()._parse_cagrs(BeautifulSoup(SCREENER_CAGR_HTML, "lxml"))

    assert parsed == {
        "Compounded Sales Growth": {
            "10 Years:": "15%", "5 Years:": "18%", "3 Years:": "6%", "TTM:": "15%",
        },
        "Stock Price CAGR": {"10 Years:": "18%", "1 Year:": "-11%"},
    }


# --------------------------------------------------------------------------- #
# The prompt embeds the dossier verbatim, so the defects shipped into the model
# --------------------------------------------------------------------------- #

def test_investment_memo_prompt_embeds_a_clean_dossier():
    prompt = AIPromptFactory.investment_memo(_synthetic_profile())
    dossier = prompt.split("<COMPANY_FINANCIAL_DOSSIER>")[1]

    assert re.findall(r"(?i)\bnan\b", dossier) == []
    assert re.findall(r"\d(?:\.\d+)?e[+-]\d+", dossier) == []
    assert re.findall(r"\w+::", dossier) == []


@pytest.mark.parametrize(
    "section", ["income_stmt", "balance_sheet", "cash_flow", "quarters", "cagrs"]
)
def test_section_scoped_dossiers_are_also_clean(section):
    md = AIContextBuilder.build_markdown_context(_synthetic_profile(), include_sections={section})

    assert re.findall(r"(?i)\bnan\b", md) == []
    assert re.findall(r"\d(?:\.\d+)?e[+-]\d+", md) == []
    assert re.findall(r"\w+::", md) == []
