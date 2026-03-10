"""VLM-based structured data extraction from slide images."""

import asyncio
from collections.abc import Callable
from pathlib import Path

from loguru import logger
from openai import AsyncOpenAI

from app.core.config import settings
from app.schemas.fa_case import VLMSlideResult
from app.services.image_utils import image_to_base64

VLM_PROMPT = """你是一個失效分析(FA)周報投影片的資料提取助手。
請分析這張投影片圖片，判斷是否為 FA 案例頁。

判斷標準：案例頁會包含 Date、Customer、Device、
Defect Mode(或Defect Phenomenon)等結構化欄位。

如果是案例頁，請提取 data 中的所有欄位。
如果不是案例頁（首頁、摘要頁、圖表頁等），設 is_case_page=false, data=null。

注意：
- Defect Model、Defect Phenomenon、Defect Mode 是同一欄位
- Defect Rate 和 Fail Rate 是同一欄位
- 日期格式如 "2026/03/02[13829]"，只需保留 "2026/03/02"
"""



async def extract_single_slide(
    client: AsyncOpenAI,
    image_path: Path,
    slide_number: int,
) -> VLMSlideResult:
    """Send a single slide image to VLM and parse structured response."""
    b64 = image_to_base64(image_path)

    response = await client.beta.chat.completions.parse(
        model=settings.vlm_model,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                    {"type": "text", "text": VLM_PROMPT},
                ],
            }
        ],
        response_format=VLMSlideResult,
        temperature=settings.vlm_temperature,
        top_p=settings.vlm_top_p,
        presence_penalty=settings.vlm_presence_penalty,
        max_tokens=1024,
        extra_body={
            "top_k": settings.vlm_top_k,
            "min_p": settings.vlm_min_p,
            "repetition_penalty": settings.vlm_repetition_penalty,
        },
    )

    result = response.choices[0].message.parsed
    if result is None:
        raw = response.choices[0].message.content or ""
        raise ValueError(f"Slide {slide_number}: structured output parse failed — {raw[:200]}")

    return result


async def extract_slides_batch(
    client: AsyncOpenAI,
    image_paths: list[Path],
    slide_numbers: list[int],
    on_progress: Callable | None = None,
) -> list[tuple[int, VLMSlideResult | None, str | None, str | None]]:
    """Extract data from multiple slides with concurrency control and retry.

    Args:
        client: Shared AsyncOpenAI client from app.state.
        image_paths: List of slide image file paths.
        slide_numbers: Corresponding slide numbers.
        on_progress: Optional callback(completed, total, slide_number) for SSE.

    Returns:
        List of (slide_number, result_or_none, raw_response_or_none, error_or_none).
    """
    semaphore = asyncio.Semaphore(settings.vlm_max_concurrency)
    total = len(image_paths)
    completed = 0

    async def _process_one(
        image_path: Path, slide_num: int
    ) -> tuple[int, VLMSlideResult | None, str | None, str | None]:
        nonlocal completed
        async with semaphore:
            last_error = None
            for attempt in range(settings.vlm_retry_count + 1):
                try:
                    result = await extract_single_slide(client, image_path, slide_num)
                    completed += 1
                    if on_progress:
                        await on_progress(completed, total, slide_num)
                    raw = result.model_dump_json()
                    return (slide_num, result, raw, None)
                except Exception as e:
                    last_error = str(e)
                    if attempt < settings.vlm_retry_count:
                        logger.warning(
                            "Slide {} attempt {} failed: {}, retrying...",
                            slide_num, attempt + 1, e,
                        )
                        await asyncio.sleep(1 * (attempt + 1))

            completed += 1
            if on_progress:
                await on_progress(completed, total, slide_num)
            logger.error("Slide {} failed after retries: {}", slide_num, last_error)
            return (slide_num, None, None, last_error)

    tasks = [
        _process_one(img, num)
        for img, num in zip(image_paths, slide_numbers)
    ]
    results = await asyncio.gather(*tasks)
    return sorted(results, key=lambda x: x[0])
