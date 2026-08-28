"""
Number formatting and parsing utilities with Indian numbering localization (₹, Cr, Lakh).
"""

import re
from typing import Optional, Union


def parse_indian_number(text: Optional[Union[str, int, float]]) -> Optional[float]:
    """
    Parse a numeric string formatted in Indian notation into a float.
    Handles '₹', 'Cr', 'L', '%', commas, negative signs, brackets for negative '(123.4)'.
    Examples:
        '₹17,42,991Cr.' -> 1742991.0
        '1,288' -> 1288.0
        '23.3%' -> 23.3
        '(45.6)' -> -45.6
        '-' or '--' -> None
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)

    cleaned = str(text).strip()
    if not cleaned or cleaned in ("-", "--", "None", "null", "N/A", "NA", ""):
        return None

    # Handle parenthesized negative numbers e.g. (1,234) -> -1234
    is_negative = False
    if cleaned.startswith("(") and cleaned.endswith(")"):
        is_negative = True
        cleaned = cleaned[1:-1].strip()
    elif cleaned.startswith("-"):
        is_negative = True
        cleaned = cleaned[1:].strip()

    # Remove currency, units, commas, %
    cleaned = (
        cleaned.replace("₹", "")
        .replace("Rs.", "")
        .replace("Rs", "")
        .replace("Cr.", "")
        .replace("Cr", "")
        .replace("Lakh", "")
        .replace("L", "")
        .replace("%", "")
        .replace(",", "")
        .strip()
    )

    try:
        val = float(cleaned)
        return -val if is_negative else val
    except ValueError:
        return None


def format_inr(
    amount: Optional[Union[int, float]],
    unit: str = "auto",
    decimals: int = 2,
) -> str:
    """
    Format a number in Indian Rupee notation (₹, Cr, Lakh).
    """
    if amount is None:
        return "N/A"

    sign = "-" if amount < 0 else ""
    abs_amt = abs(amount)

    if unit == "Cr" or (unit == "auto" and abs_amt >= 1e7):
        cr_val = abs_amt / 1e7
        return f"{sign}₹{cr_val:,.{decimals}f} Cr"
    elif unit == "Lakh" or (unit == "auto" and abs_amt >= 1e5):
        lakh_val = abs_amt / 1e5
        return f"{sign}₹{lakh_val:,.{decimals}f} L"
    else:
        return f"{sign}₹{abs_amt:,.{decimals}f}"
