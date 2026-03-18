"""Generate concise weekly summary from FA cases using LLM."""

from loguru import logger
from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.core.config import settings
from app.models.fa_case import FACase, FAReport, FAWeeklyPeriod

SUMMARY_PROMPT = """你是一個失效分析(FA)周報摘要助手。
請根據以下本周所有 FA 案例資料，產生一段精簡的重點摘要。

要求：
- 用繁體中文撰寫
- 用 3~5 個重點條列（bullet points）
- 涵蓋：主要缺陷模式、受影響的客戶/Device、整體 FA 進度
- 如有需關注的異常（如高 defect rate 或多筆相同缺陷模式），請特別標示
- 總字數控制在 200 字以內
- 不要加標題，直接列點

本周案例資料：
{cases_text}"""


def _format_cases_for_prompt(cases: list[FACase]) -> str:
    """Format case list into text block for LLM prompt."""
    lines = []
    for c in cases:
        parts = [
            f"Customer: {c.customer or '—'}",
            f"Device: {c.device or '—'}",
            f"Model: {c.model or '—'}",
            f"Defect Mode: {c.defect_mode or '—'}",
            f"Defect Rate: {c.defect_rate_raw or '—'}",
            f"FAB/Assy: {c.fab_assembly or '—'}",
            f"FA Status: {c.fa_status or '—'}",
            f"Follow Up: {c.follow_up or '—'}",
        ]
        lines.append(" | ".join(parts))
    return "\n".join(lines)


async def generate_weekly_summary(
    client: AsyncOpenAI,
    db: AsyncSession,
    period_id: int,
) -> str | None:
    """Generate or regenerate the weekly summary for a given period.

    Fetches all cases from reports in this period, sends to LLM,
    and saves the result to FAWeeklyPeriod.summary.
    """
    # Get the period
    result = await db.execute(
        select(FAWeeklyPeriod).where(FAWeeklyPeriod.id == period_id)
    )
    period = result.scalar_one_or_none()
    if not period:
        logger.warning("Weekly period {} not found, skipping summary", period_id)
        return None

    # Get all cases from this period's reports
    cases_result = await db.execute(
        select(FACase)
        .options(
            load_only(
                FACase.customer,
                FACase.device,
                FACase.model,
                FACase.defect_mode,
                FACase.defect_rate_raw,
                FACase.fab_assembly,
                FACase.fa_status,
                FACase.follow_up,
            )
        )
        .join(FAReport, FACase.report_id == FAReport.id)
        .where(FAReport.weekly_period_id == period_id)
        .order_by(FACase.id)
    )
    cases = list(cases_result.scalars().all())

    if not cases:
        period.summary = None
        await db.commit()
        return None

    cases_text = _format_cases_for_prompt(cases)
    prompt = SUMMARY_PROMPT.format(cases_text=cases_text)

    try:
        resp = await client.chat.completions.create(
            model=settings.vlm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=512,
        )
        summary = resp.choices[0].message.content.strip()
        period.summary = summary
        await db.commit()
        logger.info(
            "Generated weekly summary for {}-W{:02d} ({} cases)",
            period.year,
            period.week_number,
            len(cases),
        )
        return summary
    except Exception as e:
        logger.warning(
            "Weekly summary generation failed for period {}: {}", period_id, e
        )
        return None
