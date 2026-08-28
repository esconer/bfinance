"""
Institutional LLM Prompt Templates for Financial Analysis and Decision-Making.
"""

from typing import Optional
from bfinance.models.company import CompanyProfile
from bfinance.ai.context import AIContextBuilder


class AIPromptFactory:
    """
    Constructs institutional-grade system and user prompts for AI equity analysis.
    """

    @classmethod
    def investment_memo(cls, profile: CompanyProfile, custom_instructions: str = "") -> str:
        """
        Generate an institutional initiation coverage / investment memo prompt.
        """
        context = AIContextBuilder.build_markdown_context(profile)
        instructions = custom_instructions or (
            "You are a Senior Principal at a top-tier Indian Long-Only Equity Fund.\n"
            "Analyze the provided financial dossier and draft a rigorous Investment Initiation Note.\n"
            "Include:\n"
            "1. Executive Summary & Investment Thesis (Key Moats, Catalysts, Risks)\n"
            "2. Business Model & Industry Dynamics (Competitive Positioning, Pricing Power)\n"
            "3. Financial Quality Breakdown (Revenue Quality, Margin Durability, Capital Allocation, ROCE trends)\n"
            "4. Capital Structure & Balance Sheet Health (Working capital, debt trajectory, Cash Conversion Cycle)\n"
            "5. Ownership & Institutional Flow Analysis (Promoter pledge, FII vs DII trajectory)\n"
            "6. Valuation Multiples & Fair Value Range (Historical PE vs Growth, DCF expectations)\n"
            "7. Key Downside Risks & Invalidation Triggers"
        )

        return (
            f"<INSTRUCTIONS>\n{instructions}\n</INSTRUCTIONS>\n\n"
            f"<COMPANY_FINANCIAL_DOSSIER>\n{context}\n</COMPANY_FINANCIAL_DOSSIER>\n\n"
            "Please write the comprehensive investment memo now."
        )

    @classmethod
    def forensic_audit(cls, profile: CompanyProfile) -> str:
        """
        Generate a forensic accounting audit prompt to identify red flags and earnings manipulation.
        """
        context = AIContextBuilder.build_markdown_context(profile)
        return (
            "<INSTRUCTIONS>\n"
            "You are an expert Forensic Accounting Auditor specializing in Indian listed equities.\n"
            "Perform a forensic quality check on the provided financial statements.\n"
            "Specifically examine and score (1 to 10):\n"
            "1. Cash Flow vs Reported PAT Divergence (Is Operating Cash Flow lagging Net Profit?)\n"
            "2. Working Capital Stress (Are Debtor Days or Inventory Days surging?)\n"
            "3. Other Income Dependency (Is Other Income inflating Operating PBT?)\n"
            "4. Depreciation & CWIP Capitalization Shenanigans (Is Capex stuck in CWIP?)\n"
            "5. Promoter Pledge & Stake Dilution\n"
            "6. Auditor Observations or Material Related Party Transactions (if indicated in notes)\n"
            "Provide a final 'Forensic Risk Rating' (Low, Medium, High, Extreme Red Flag) with detailed justification.\n"
            "</INSTRUCTIONS>\n\n"
            f"<COMPANY_FINANCIAL_DOSSIER>\n{context}\n</COMPANY_FINANCIAL_DOSSIER>\n\n"
            "Please conduct the forensic audit now."
        )

    @classmethod
    def concall_summary(cls, profile: CompanyProfile) -> str:
        """
        Generate prompt focusing on management commentary, concalls, and forward guidance.
        """
        context = AIContextBuilder.build_markdown_context(profile)
        return (
            "<INSTRUCTIONS>\n"
            "You are an Equity Research Analyst covering Indian Markets.\n"
            "Analyze the latest earnings conference calls, quarterly performance, and management commentary.\n"
            "Extract:\n"
            "1. Core Operational Highlights for the Latest Quarter\n"
            "2. Forward Revenue, Margin, and Capex Guidance given by Management\n"
            "3. Raw Q&A Analysis: Tough questions asked by institutional analysts and management's answers\n"
            "4. Headwinds vs Tailwinds summary\n"
            "</INSTRUCTIONS>\n\n"
            f"<COMPANY_FINANCIAL_DOSSIER>\n{context}\n</COMPANY_FINANCIAL_DOSSIER>\n\n"
            "Please summarize the management commentary and forward guidance now."
        )
