"""Embedding generation using Qwen3-VL-Embedding via vLLM Chat Embeddings API.

Reference: https://github.com/vllm-project/vllm/blob/main/examples/pooling/embed/vision_embedding_online.py
"""

import asyncio
from pathlib import Path

from loguru import logger
from openai import AsyncOpenAI
from openai.types.create_embedding_response import CreateEmbeddingResponse

from app.core.config import settings
from app.models.fa_case import FACase
from app.services.image_utils import image_to_base64

DEFAULT_INSTRUCTION = "Represent the user's input."


async def _chat_embeddings(
    client: AsyncOpenAI,
    messages: list[dict],
) -> CreateEmbeddingResponse:
    """Call vLLM's Chat Embeddings API (extension of OpenAI Embeddings API).

    Qwen3-VL-Embedding requires messages format with
    continue_final_message=True and add_special_tokens=True.
    """
    return await client.post(
        "/embeddings",
        cast_to=CreateEmbeddingResponse,
        body={
            "messages": messages,
            "model": settings.vlm_embedding_model,
            "encoding_format": "float",
            "continue_final_message": True,
            "add_special_tokens": True,
        },
    )


def build_case_text(case: FACase) -> str:
    """Build text representation of a case for embedding."""
    parts = []
    if case.customer:
        parts.append(f"Customer: {case.customer}")
    if case.device:
        parts.append(f"Device: {case.device}")
    if case.model:
        parts.append(f"Model: {case.model}")
    if case.defect_mode:
        parts.append(f"Defect Mode: {case.defect_mode}")
    if case.defect_rate_raw:
        parts.append(f"Defect Rate: {case.defect_rate_raw}")
    if case.fab_assembly:
        parts.append(f"FAB/Assembly: {case.fab_assembly}")
    if case.fa_status:
        parts.append(f"FA Status: {case.fa_status}")
    if case.follow_up:
        parts.append(f"Follow Up: {case.follow_up}")
    return " | ".join(parts)


async def generate_text_embedding(client: AsyncOpenAI, text: str) -> list[float]:
    """Generate text embedding using Qwen3-VL Chat Embeddings API."""
    response = await _chat_embeddings(client, messages=[
        {"role": "system", "content": [{"type": "text", "text": DEFAULT_INSTRUCTION}]},
        {"role": "user", "content": [{"type": "text", "text": text}]},
        {"role": "assistant", "content": [{"type": "text", "text": ""}]},
    ])
    return response.data[0].embedding


async def generate_image_embedding(client: AsyncOpenAI, image_path: str | Path) -> list[float]:
    """Generate image embedding using Qwen3-VL Chat Embeddings API."""
    try:
        b64 = image_to_base64(Path(image_path))

        response = await _chat_embeddings(client, messages=[
            {"role": "system", "content": [{"type": "text", "text": DEFAULT_INSTRUCTION}]},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": ""},
                ],
            },
            {"role": "assistant", "content": [{"type": "text", "text": ""}]},
        ])
        return response.data[0].embedding
    except Exception as e:
        logger.warning("Image embedding failed: {}", e)
        return []


async def generate_embeddings_for_case(
    client: AsyncOpenAI, case: FACase
) -> tuple[list[float], list[float]]:
    """Generate both text and image embeddings for a case.

    Returns (text_embedding, image_embedding).
    """
    text = build_case_text(case)
    text_coro = generate_text_embedding(client, text) if text else asyncio.sleep(0, result=[])
    image_coro = (
        generate_image_embedding(client, case.slide_image_path)
        if case.slide_image_path
        else asyncio.sleep(0, result=[])
    )
    text_emb, image_emb = await asyncio.gather(text_coro, image_coro)
    return text_emb, image_emb
