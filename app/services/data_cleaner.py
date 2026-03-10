"""Data cleaning and standardization for VLM-extracted FA case data."""

import re

from app.schemas.fa_case import VLMExtractedData


def clean_date(raw: str | None) -> str | None:
    """Remove noise from date strings. E.g. '2026/03/02[13829]' → '2026/03/02'."""
    if not raw:
        return None
    # Remove bracketed numbers like [13829]
    cleaned = re.sub(r"\[.*?\]", "", raw).strip()
    # Try to match common date patterns
    match = re.search(r"(\d{4}[/\-\.]\d{1,2}[/\-\.]\d{1,2})", cleaned)
    if match:
        return match.group(1)
    return cleaned


def parse_lots(raw: str | None) -> list[str]:
    """Split lot string into list. 'LOT-A, LOT-B, LOT-C' → ['LOT-A', 'LOT-B', 'LOT-C']."""
    if not raw:
        return []
    # Split by comma, semicolon, or newline
    lots = re.split(r"[,;\n]+", raw)
    return [lot.strip() for lot in lots if lot.strip()]


def clean_extracted_data(data: VLMExtractedData) -> dict:
    """Clean and standardize VLM-extracted data into DB-ready dict.

    Returns dict matching FACase column names.
    """
    return {
        "date": clean_date(data.date),
        "customer": (data.customer or "").strip() or None,
        "device": (data.device or "").strip() or None,
        "model": (data.model or "").strip() or None,
        "defect_mode": (data.defect_mode or "").strip() or None,
        "defect_rate_raw": (data.defect_rate or "").strip() or None,
        "defect_lots": parse_lots(data.defect_lots),
        "fab_assembly": (data.fab_assembly or "").strip() or None,
        "fa_status": (data.fa_status or "").strip() or None,
        "follow_up": (data.follow_up or "").strip() or None,
    }
