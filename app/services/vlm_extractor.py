"""VLM-based structured data extraction from slide images."""

import asyncio
import base64
import json
import logging
from pathlib import Path

from openai import AsyncOpenAI

from app.config import settings
from app.schemas.fa_case import VLMExtractedData, VLMSlideResult

logger = logging.getLogger(__name__)

VLM_PROMPT = """你是一個失效分析(FA)周報投影片的資料提取助手。
請分析這張投影片圖片，判斷是否為 FA 案例頁。

判斷標準：案例頁會包含 Date、Customer、Device、
Defect Mode(或Defect Phenomenon)等結構化欄位。

如果是案例頁，請提取以下欄位並以 JSON 格式回傳：
{
  "is_case_page": true,
  "data": {
    "date": "僅保留日期，去除編號等雜訊",
    "customer": "客戶名稱，保留原始格式",
    "device": "內部產品型號",
    "model": "客戶機種名稱",
    "defect_mode": "失效模式/現象描述",
    "defect_rate": "不良率，保留原始格式",
    "defect_lots": "批號列表，用逗號分隔",
    "fab_assembly": "工廠名稱/代碼",
    "fa_status": "分析狀態描述",
    "follow_up": "後續行動描述"
  }
}

如果不是案例頁（首頁、摘要頁、圖表頁等），回傳：
{"is_case_page": false, "data": null}

注意：
- Defect Model、Defect Phenomenon、Defect Mode 是同一欄位
- Defect Rate 和 Fail Rate 是同一欄位
- 日期格式如 "2026/03/02[13829]"，只需保留 "2026/03/02"
- 僅回傳 JSON，不要有其他文字"""


def _get_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=settings.vlm_base_url,
        api_key=settings.vlm_api_key,
    )


def _image_to_base64(image_path: Path) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


async def extract_single_slide(
    client: AsyncOpenAI,
    image_path: Path,
    slide_number: int,
) -> VLMSlideResult:
    """Send a single slide image to VLM and parse structured response."""
    b64 = _image_to_base64(image_path)

    response = await client.chat.completions.create(
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
        temperature=0.1,
        max_tokens=1024,
    )

    raw_text = response.choices[0].message.content.strip()

    # Try to parse JSON from response (handle markdown code blocks)
    json_text = raw_text
    if "```" in json_text:
        # Extract JSON from markdown code block
        start = json_text.find("{")
        end = json_text.rfind("}") + 1
        if start >= 0 and end > start:
            json_text = json_text[start:end]

    try:
        parsed = json.loads(json_text)
        return VLMSlideResult(**parsed)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Slide {slide_number}: Failed to parse VLM response: {e}")
        logger.debug(f"Raw response: {raw_text}")
        raise ValueError(f"VLM response parse error: {e}")


async def extract_slides_batch(
    image_paths: list[Path],
    slide_numbers: list[int],
    on_progress: callable | None = None,
) -> list[tuple[int, VLMSlideResult | None, str | None, str | None]]:
    """Extract data from multiple slides with concurrency control and retry.

    Args:
        image_paths: List of slide image file paths.
        slide_numbers: Corresponding slide numbers.
        on_progress: Optional callback(completed, total, slide_number) for SSE.

    Returns:
        List of (slide_number, result_or_none, raw_response_or_none, error_or_none).
    """
    client = _get_client()
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
                    # Store raw response for debugging
                    raw = result.model_dump_json()
                    return (slide_num, result, raw, None)
                except Exception as e:
                    last_error = str(e)
                    if attempt < settings.vlm_retry_count:
                        logger.warning(
                            f"Slide {slide_num} attempt {attempt + 1} failed: {e}, retrying..."
                        )
                        await asyncio.sleep(1 * (attempt + 1))

            completed += 1
            if on_progress:
                await on_progress(completed, total, slide_num)
            logger.error(f"Slide {slide_num} failed after retries: {last_error}")
            return (slide_num, None, None, last_error)

    tasks = [
        _process_one(img, num)
        for img, num in zip(image_paths, slide_numbers)
    ]
    results = await asyncio.gather(*tasks)
    return sorted(results, key=lambda x: x[0])
