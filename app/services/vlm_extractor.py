"""VLM-based slide classification and structured data extraction.

Two-stage pipeline:
  Stage 1 — classify_single_slide / classify_slides_batch
             Lightweight call to determine if a slide is an FA case page.
  Stage 2 — extract_single_slide / extract_slides_batch
             Full extraction of 10 structured fields from confirmed case pages.
"""

import asyncio
from collections.abc import Callable
from pathlib import Path

from loguru import logger
from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)

from app.core.config import settings
from app.schemas.fa_case import VLMClassificationResult, VLMSlideResult
from app.services.image_utils import image_to_base64

# Errors worth retrying (transient); everything else fails immediately
_RETRYABLE = (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)

# Global semaphore — shared across all concurrent uploads/extractions
_vlm_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _vlm_semaphore
    if _vlm_semaphore is None:
        _vlm_semaphore = asyncio.Semaphore(settings.vlm_max_concurrency)
    return _vlm_semaphore


def reset_semaphore() -> None:
    """Re-create the semaphore — call on startup to bind to the current event loop."""
    global _vlm_semaphore
    _vlm_semaphore = asyncio.Semaphore(settings.vlm_max_concurrency)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

CLASSIFY_PROMPT = """你是一個失效分析(FA)周報投影片的分類助手。
請判斷這張投影片是否為 FA 案例頁。

判斷標準：案例頁會包含 Date、Customer、Device、
Defect Mode(或 Defect Phenomenon)等結構化欄位。
非案例頁包括：首頁、目錄、摘要頁、圖表總覽等。

請回傳：
- is_case_page: 是否為案例頁
- confidence: 信心度 (0.0 ~ 1.0)
- reason: 簡短說明判斷理由（一句話）
"""

EXTRACT_PROMPT = """你是一個失效分析(FA)周報投影片的資料提取助手。
這張投影片已確認為 FA 案例頁，請提取 data 中的所有欄位。

注意：
- Defect Model、Defect Phenomenon、Defect Mode 是同一欄位
- Defect Rate 和 Fail Rate 是同一欄位
- 日期格式如 "2026/03/02[13829]"，只需保留 "2026/03/02"
- 設 is_case_page=true
"""

# ---------------------------------------------------------------------------
# Stage 1: Classification
# ---------------------------------------------------------------------------


async def classify_single_slide(
    client: AsyncOpenAI,
    image_path: Path,
    slide_number: int,
) -> VLMClassificationResult:
    """Classify a single slide as case or non-case (lightweight VLM call)."""
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
                    {"type": "text", "text": CLASSIFY_PROMPT},
                ],
            }
        ],
        response_format=VLMClassificationResult,
        temperature=0.3,
        top_p=0.8,
        max_tokens=128,
        extra_body={
            "top_k": settings.vlm_top_k,
            "min_p": settings.vlm_min_p,
            "repetition_penalty": settings.vlm_repetition_penalty,
        },
    )

    if response.usage:
        logger.debug(
            "Slide {} classify: {} prompt + {} completion tokens",
            slide_number,
            response.usage.prompt_tokens,
            response.usage.completion_tokens,
        )

    result = response.choices[0].message.parsed
    if result is None:
        raw = response.choices[0].message.content or ""
        raise ValueError(
            f"Slide {slide_number}: classification parse failed — {raw[:200]}"
        )

    return result


async def classify_slides_batch(
    client: AsyncOpenAI,
    image_paths: list[Path],
    slide_numbers: list[int],
    on_progress: Callable | None = None,
) -> list[tuple[int, VLMClassificationResult | None, str | None, str | None]]:
    """Classify multiple slides with concurrency control and retry.

    Returns:
        List of (slide_number, result_or_none, raw_json_or_none, error_or_none).
    """
    semaphore = _get_semaphore()
    total = len(image_paths)
    completed = 0

    async def _process_one(
        image_path: Path, slide_num: int
    ) -> tuple[int, VLMClassificationResult | None, str | None, str | None]:
        nonlocal completed
        async with semaphore:
            last_error = None
            for attempt in range(settings.vlm_retry_count + 1):
                try:
                    result = await classify_single_slide(client, image_path, slide_num)
                    completed += 1
                    if on_progress:
                        await on_progress(completed, total, slide_num)
                    raw = result.model_dump_json()
                    return (slide_num, result, raw, None)
                except _RETRYABLE as e:
                    last_error = str(e)
                    if attempt < settings.vlm_retry_count:
                        logger.warning(
                            "Slide {} classify attempt {} failed (retryable): {}, retrying...",
                            slide_num,
                            attempt + 1,
                            e,
                        )
                        await asyncio.sleep(1 * (attempt + 1))
                    else:
                        break
                except Exception as e:
                    # Non-retryable error (parse failure, file error, etc.)
                    last_error = str(e)
                    logger.error(
                        "Slide {} classification failed (non-retryable): {}",
                        slide_num,
                        e,
                    )
                    break

            completed += 1
            if on_progress:
                await on_progress(completed, total, slide_num)
            return (slide_num, None, None, last_error)

    tasks = [_process_one(img, num) for img, num in zip(image_paths, slide_numbers)]
    results = await asyncio.gather(*tasks)
    return sorted(results, key=lambda x: x[0])


# ---------------------------------------------------------------------------
# Stage 2: Field extraction
# ---------------------------------------------------------------------------


async def extract_single_slide(
    client: AsyncOpenAI,
    image_path: Path,
    slide_number: int,
) -> VLMSlideResult:
    """Extract structured FA case fields from a confirmed case slide."""
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
                    {"type": "text", "text": EXTRACT_PROMPT},
                ],
            }
        ],
        response_format=VLMSlideResult,
        temperature=settings.vlm_temperature,
        top_p=settings.vlm_top_p,
        presence_penalty=settings.vlm_presence_penalty,
        max_tokens=2048,
        extra_body={
            "top_k": settings.vlm_top_k,
            "min_p": settings.vlm_min_p,
            "repetition_penalty": settings.vlm_repetition_penalty,
        },
    )

    if response.usage:
        logger.debug(
            "Slide {} extract: {} prompt + {} completion tokens",
            slide_number,
            response.usage.prompt_tokens,
            response.usage.completion_tokens,
        )

    result = response.choices[0].message.parsed
    if result is None:
        raw = response.choices[0].message.content or ""
        raise ValueError(f"Slide {slide_number}: extraction parse failed — {raw[:200]}")

    return result


async def extract_slides_batch(
    client: AsyncOpenAI,
    image_paths: list[Path],
    slide_numbers: list[int],
    on_progress: Callable | None = None,
) -> list[tuple[int, VLMSlideResult | None, str | None, str | None]]:
    """Extract data from multiple slides with concurrency control and retry.

    Returns:
        List of (slide_number, result_or_none, raw_response_or_none, error_or_none).
    """
    semaphore = _get_semaphore()
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
                except _RETRYABLE as e:
                    last_error = str(e)
                    if attempt < settings.vlm_retry_count:
                        logger.warning(
                            "Slide {} extract attempt {} failed (retryable): {}, retrying...",
                            slide_num,
                            attempt + 1,
                            e,
                        )
                        await asyncio.sleep(1 * (attempt + 1))
                    else:
                        break
                except Exception as e:
                    last_error = str(e)
                    logger.error(
                        "Slide {} extraction failed (non-retryable): {}",
                        slide_num,
                        e,
                    )
                    break

            completed += 1
            if on_progress:
                await on_progress(completed, total, slide_num)
            return (slide_num, None, None, last_error)

    tasks = [_process_one(img, num) for img, num in zip(image_paths, slide_numbers)]
    results = await asyncio.gather(*tasks)
    return sorted(results, key=lambda x: x[0])
