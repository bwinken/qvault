"""Embedding generation using vLLM's OpenAI-compatible embedding API."""

import base64
import logging
from pathlib import Path

from openai import AsyncOpenAI

from app.config import settings
from app.models.fa_case import FACase

logger = logging.getLogger(__name__)


def _get_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=settings.vlm_base_url,
        api_key=settings.vlm_api_key,
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


async def generate_text_embedding(text: str) -> list[float]:
    """Generate text embedding using vLLM embedding endpoint."""
    client = _get_client()
    response = await client.embeddings.create(
        model=settings.vlm_embedding_model,
        input=text,
    )
    return response.data[0].embedding


async def generate_image_embedding(image_path: str | Path) -> list[float]:
    """Generate image embedding using vLLM embedding endpoint.

    Note: This depends on the vLLM model supporting image embeddings.
    If not supported, this will gracefully return an empty list.
    """
    try:
        image_path = Path(image_path)
        if not image_path.exists():
            logger.warning(f"Image not found for embedding: {image_path}")
            return []

        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")

        client = _get_client()
        response = await client.embeddings.create(
            model=settings.vlm_embedding_model,
            input=f"data:image/png;base64,{b64}",
        )
        return response.data[0].embedding
    except Exception as e:
        logger.warning(f"Image embedding failed (may not be supported): {e}")
        return []


async def generate_embeddings_for_case(case: FACase) -> tuple[list[float], list[float]]:
    """Generate both text and image embeddings for a case.

    Returns (text_embedding, image_embedding).
    """
    text = build_case_text(case)
    text_emb = await generate_text_embedding(text) if text else []
    image_emb = (
        await generate_image_embedding(case.slide_image_path)
        if case.slide_image_path
        else []
    )
    return text_emb, image_emb
