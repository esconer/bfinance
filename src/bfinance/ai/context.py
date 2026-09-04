"""
AI Context Engine: Formats and serializes 100% of bfinance corporate data into
token-optimized, highly structured Markdown, JSON, and text representations for LLMs.
"""

from typing import Any, Dict, List, Literal, Optional, Set
import json
import pandas as pd

from bfinance.models.company import CompanyProfile


def _safe_markdown(df: Any, **kwargs: Any) -> str:
    """Render DataFrame as markdown, falling back to plain text if tabulate is missing."""
    try:
        return df.to_markdown(**kwargs)
    except ImportError:
        try:
            text = df.to_string(index=kwargs.get("index", True))
        except Exception:
            text = str(df)
        return f"```\n{text}\n```"


class AIContextBuilder:
    """
    Constructs comprehensive AI-ready context dossiers from CompanyProfile.
    Supports token-efficient Markdown, JSON Schema, and plain dense text.
    """

    @classmethod
    def build_markdown_context(
        cls,
        profile: CompanyProfile,
        include_sections: Optional[Set[str]] = None,
        max_quarters: int = 8,
        max_years: int = 7,
        max_concalls: int = 5,
    ) -> str:
        """
        Generate dense, professional Markdown formatted financial context for LLMs.
        """
        all_sections = {
            "metadata", "valuation", "income_stmt", "quarters",
            "balance_sheet", "cash_flow", "shareholding", "ratios",
            "cagrs", "analysis", "concalls", "peers", "documents"
        }
        if include_sections is not None:
            unknown = set(include_sections) - all_sections
            if unknown:
                raise ValueError(f"Unknown sections {sorted(unknown)}; valid: {sorted(all_sections)}")
        active = include_sections or all_sections

        lines: List[str] = []
        r = profile.ratios

        # 1. Metadata
        if "metadata" in active:
            lines.append(f"# FINANCIAL DOSSIER: {profile.name} (NSE: {profile.nse_symbol or profile.symbol} | BSE: {profile.bse_code or 'N/A'})")
            lines.append(f"**Sector**: {profile.sector or 'N/A'} | **Industry**: {profile.industry or 'N/A'} | **Sub-Industry**: {profile.sub_industry or 'N/A'}")
            if profile.indices:
                lines.append(f"**Index Memberships**: {', '.join(profile.indices[:6])}")
            if profile.about:
                lines.append(f"\n### Business Description\n{profile.about}")

        # 2. Key Valuation & Financial Ratios
        if "valuation" in active:
            lines.append("\n## Current Valuation & Quality Metrics")
            lines.append(f"- **Current Market Price (CMP)**: ₹{r.current_price:,.2f}" if r.current_price else "- **CMP**: N/A")
            lines.append(f"- **Market Capitalization**: ₹{r.market_cap:,.2f} Cr" if r.market_cap else "- **Market Cap**: N/A")
            lines.append(f"- **52-Week Range**: ₹{r.low_52w:,.2f} - ₹{r.high_52w:,.2f}" if r.low_52w and r.high_52w else "")
            lines.append(f"- **Stock P/E**: {r.stock_pe:.2f}x | **Book Value**: ₹{r.book_value:,.2f}" if r.stock_pe and r.book_value else "")
            lines.append(f"- **ROCE**: {r.roce:.2f}% | **ROE**: {r.roe:.2f}%" if r.roce and r.roe else "")
            face_str = f"₹{r.face_value}" if r.face_value is not None else "N/A"
            lines.append(f"- **Dividend Yield**: {r.dividend_yield:.2f}% | **Face Value**: {face_str}" if r.dividend_yield is not None else "")
            if r.debt_to_equity is not None:
                lines.append(f"- **Debt to Equity**: {r.debt_to_equity:.2f}")

        # 3. Annual Income Statement
        if "income_stmt" in active and not profile.profit_loss.empty:
            lines.append(f"\n## Annual Income Statement (Last {max_years} Years in ₹ Cr)")
            df_pnl = profile.profit_loss.to_dataframe(orient="columns")
            if not df_pnl.empty:
                cols = list(df_pnl.columns)[-max_years:]
                lines.append(_safe_markdown(df_pnl[cols]))

        # 4. Quarterly Results
        if "quarters" in active and not profile.quarters.empty:
            lines.append(f"\n## Quarterly Financial Results (Last {max_quarters} Quarters in ₹ Cr)")
            df_q = profile.quarters.to_dataframe(orient="columns")
            if not df_q.empty:
                cols = list(df_q.columns)[-max_quarters:]
                lines.append(_safe_markdown(df_q[cols]))

        # 5. Balance Sheet
        if "balance_sheet" in active and not profile.balance_sheet.empty:
            lines.append(f"\n## Annual Balance Sheet (Last {max_years} Years in ₹ Cr)")
            df_bs = profile.balance_sheet.to_dataframe(orient="columns")
            if not df_bs.empty:
                cols = list(df_bs.columns)[-max_years:]
                lines.append(_safe_markdown(df_bs[cols]))

        # 6. Cash Flow Statement
        if "cash_flow" in active and not profile.cash_flow.empty:
            lines.append(f"\n## Cash Flow Statement (Last {max_years} Years in ₹ Cr)")
            df_cf = profile.cash_flow.to_dataframe(orient="columns")
            if not df_cf.empty:
                cols = list(df_cf.columns)[-max_years:]
                lines.append(_safe_markdown(df_cf[cols]))

        # 7. Shareholding Pattern Trends
        if "shareholding" in active:
            lines.append("\n## Institutional Shareholding Distribution (%)")
            df_sh = profile.shareholding.to_dataframe(orient="columns")
            if not df_sh.empty:
                lines.append("### Quarterly Trend (Recent Quarters)")
                lines.append(_safe_markdown(df_sh.iloc[:, -6:]))

            df_shy = profile.shareholding_yearly.to_dataframe(orient="columns")
            if not df_shy.empty:
                lines.append("\n### 10-Year Annual Trend")
                lines.append(_safe_markdown(df_shy.iloc[:, -6:]))

        # 8. Operating Ratios History
        if "ratios" in active and not profile.ratios_history.empty:
            lines.append("\n## Historical Operating Ratios & Efficiency Metrics")
            df_rh = profile.ratios_history.to_dataframe(orient="columns")
            if not df_rh.empty:
                lines.append(_safe_markdown(df_rh.iloc[:, -max_years:]))

        # 9. Compounded CAGRs
        if "cagrs" in active and profile.cagrs:
            lines.append("\n## Compounded Annual Growth Rates (CAGR %)")
            for k, v in profile.cagrs.items():
                vals = " | ".join([f"{time_k}: {val}" for time_k, val in v.items()])
                lines.append(f"- **{k}**: {vals}")

        # 10. Qualitative Insights (Pros & Cons)
        if "analysis" in active and (profile.analysis.pros or profile.analysis.cons):
            lines.append("\n## Qualitative Analysis (Pros & Cons)")
            if profile.analysis.pros:
                lines.append("### Key Strengths (Pros)")
                for p in profile.analysis.pros:
                    lines.append(f"- [PRO] {p}")
            if profile.analysis.cons:
                lines.append("### Key Risks & Concerns (Cons)")
                for c in profile.analysis.cons:
                    lines.append(f"- [CON] {c}")

        # 11. Earnings Conference Calls
        if "concalls" in active and profile.concalls:
            lines.append(f"\n## Recent Conference Calls & Transcripts (Latest {max_concalls})")
            for call in profile.concalls[:max_concalls]:
                audio_tag = f" | [Audio MP3]({call.audio_url})" if call.audio_url else ""
                transcript_tag = f" | [PDF Transcript]({call.transcript_url})" if call.transcript_url else ""
                lines.append(f"- **{call.date}**: {call.title}{transcript_tag}{audio_tag}")

        # 12. Peer Benchmarking
        if "peers" in active and profile.peers:
            lines.append("\n## Industry Peer Comparison Matrix")
            df_peers = profile.peers_dataframe()
            if not df_peers.empty:
                peer_cols = [c for c in ["rank", "name", "cmp", "pe", "market_cap_cr", "roce", "dividend_yield"] if c in df_peers.columns]
                lines.append(_safe_markdown(df_peers[peer_cols].head(8), index=False))

        return "\n".join([line for line in lines if line.strip() != ""])

    @classmethod
    def build_json_context(cls, profile: CompanyProfile) -> Dict[str, Any]:
        """
        Generate structured nested JSON dictionary format for tool outputs and function calls.
        """
        r = profile.ratios
        return {
            "symbol": profile.symbol,
            "company_name": profile.name,
            "bse_code": profile.bse_code,
            "nse_symbol": profile.nse_symbol,
            "sector": profile.sector,
            "industry": profile.industry,
            "sub_industry": profile.sub_industry,
            "indices": profile.indices,
            "about": profile.about,
            "valuation": {
                "current_price": r.current_price,
                "market_cap_cr": r.market_cap,
                "high_52w": r.high_52w,
                "low_52w": r.low_52w,
                "pe_ratio": r.stock_pe,
                "book_value": r.book_value,
                "dividend_yield_pct": r.dividend_yield,
                "roce_pct": r.roce,
                "roe_pct": r.roe,
                "face_value": r.face_value,
                "debt_to_equity": r.debt_to_equity,
                "peg_ratio": r.peg_ratio,
            },
            "cagrs": profile.cagrs,
            "pros_and_cons": {
                "pros": profile.analysis.pros,
                "cons": profile.analysis.cons,
            },
            "annual_pnl": profile.profit_loss.rows,
            "quarterly_pnl": profile.quarters.rows,
            "balance_sheet": profile.balance_sheet.rows,
            "cash_flow": profile.cash_flow.rows,
            "shareholding_quarterly": profile.shareholding.rows,
            "shareholding_yearly": profile.shareholding_yearly.rows,
            "concalls": [c.model_dump() for c in profile.concalls[:10]],
            "peers": [p.model_dump() for p in profile.peers[:8]],
        }
