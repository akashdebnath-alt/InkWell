import json
import logging
import re
import string

from anthropic import (
    Anthropic,
    AnthropicError,
    APIConnectionError,
    APIStatusError,
    BadRequestError,
    RateLimitError,
)
from fastapi import HTTPException, status

from .config import settings

logger = logging.getLogger(__name__)

GHOST_SYSTEM = """You are an inline suggestion engine for a creative writing editor.
Continue the author's text in their voice with only 1-8 next words. Never write a
sentence, paragraph, story, poem, or chapter. Do not repeat words already at the end
of the author's text. Return only the suggestion, without quotes or explanation."""

ASSIST_SYSTEMS = {
    "synonyms": """You are a creative-writing word assistant. Help the author choose words;
never write their whole story, poem, or chapter. Return exactly five short synonym or
phrase options for the selected text as a JSON array of five strings, with no other text.""",
    "rhymes": """You are a creative-writing rhyme assistant. Help the author choose words;
never write their whole story, poem, or chapter. Return exactly five short rhyming word
or phrase options for the selected text as a JSON array of five strings, with no other text.""",
    "imagery": """You are a creative-writing imagery assistant. Offer concise sensory image
ideas the author can adapt; never write their whole story, poem, or chapter. Return
exactly five short imagery options as a JSON array of five strings, with no other text.""",
    "feedback": """You are a concise creative-writing coach. Never write the author's
whole story, poem, or chapter. Give constructive feedback as a JSON array of at most
five short bullet-point strings, with no other text.""",
    "rewrite_short": """You are a creative-writing editor. Never write the author's whole
story, poem, or chapter. Rewrite only the selected text, preserving its intent and voice,
using at most two sentences. Return only the rewrite, with no explanation.""",
}


def _client() -> Anthropic:
    if not settings.has_anthropic_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI features are unavailable. Configure ANTHROPIC_API_KEY in backend/.env.",
        )
    return Anthropic(api_key=settings.anthropic_api_key)


def _anthropic_error(error: AnthropicError) -> HTTPException:
    if isinstance(error, BadRequestError):
        logger.warning("Anthropic rejected an Inkwell request")
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The AI request was invalid. Please check the selected text and try again.",
        )
    if isinstance(error, (RateLimitError, APIConnectionError, APIStatusError)):
        logger.exception("Anthropic request is temporarily unavailable")
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The writing assistant is temporarily unavailable. Please try again shortly.",
        )
    logger.exception("Anthropic request failed")
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="The writing assistant could not complete that request. Please try again.",
    )


def _response_text(response) -> str:
    text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The writing assistant returned an empty response. Please try again.",
        )
    return text


def _word_key(word: str) -> str:
    return word.strip(string.punctuation + "“”‘’").casefold()


def clean_ghost(suggestion: str, text_before: str) -> str:
    suggestion = re.sub(
        r"""(?<!\w)["'“”‘’]|["'“”‘’](?!\w)""",
        "",
        suggestion,
    ).strip()
    words = suggestion.split()
    previous_words = text_before.split()
    overlap = min(len(words), len(previous_words))
    while overlap:
        if all(
            _word_key(words[index]) == _word_key(previous_words[-overlap + index])
            for index in range(overlap)
        ):
            words = words[overlap:]
            break
        overlap -= 1
    return " ".join(words[:8])


def ghost(text_before: str, context: list[str]) -> str:
    context_text = "\n---\n".join(context)
    message = (
        f"Author's related notes and earlier passages:\n{context_text}\n\n"
        f"Text before cursor:\n{text_before}"
    )
    try:
        with _client().messages.stream(
            model=settings.fast_model,
            max_tokens=20,
            system=GHOST_SYSTEM,
            messages=[{"role": "user", "content": message}],
        ) as stream:
            response_text = stream.get_final_text()
    except AnthropicError as exc:
        raise _anthropic_error(exc) from exc
    return clean_ghost(response_text, text_before)


def _parse_string_array(response_text: str, *, exact_count: int | None = None) -> list[str]:
    text = response_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        values = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The writing assistant returned an invalid response. Please try again.",
        ) from exc
    if not isinstance(values, list) or not all(
        isinstance(value, str) and value.strip() for value in values
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The writing assistant returned an invalid response. Please try again.",
        )
    values = [value.strip().lstrip("-* ").strip() for value in values]
    if exact_count is not None and len(values) != exact_count:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The writing assistant could not provide five valid options. Please try again.",
        )
    return values


def _limit_sentences(text: str, limit: int) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(sentences[:limit]).strip()


def assist(mode: str, request: str, selection: str, context: list[str]) -> dict:
    if mode not in ASSIST_SYSTEMS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Choose a supported writing-assistant mode.",
        )
    if not selection.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Select a word or passage in your document first.",
        )

    context_text = "\n---\n".join(context)
    message = (
        f"Author's related notes and earlier passages:\n{context_text}\n\n"
        f"Selected text:\n{selection}\n\nRequest:\n{request.strip()}"
    )
    max_tokens = 120 if mode == "rewrite_short" else 450
    try:
        response = _client().messages.create(
            model=settings.smart_model,
            max_tokens=max_tokens,
            system=ASSIST_SYSTEMS[mode],
            messages=[{"role": "user", "content": message}],
        )
    except AnthropicError as exc:
        raise _anthropic_error(exc) from exc

    response_text = _response_text(response)
    if mode in {"synonyms", "rhymes", "imagery"}:
        options = _parse_string_array(response_text, exact_count=5)
        if any(len(option.split()) > 8 or len(option) > 120 for option in options):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The writing assistant returned options that were too long. Please try again.",
            )
        return {"options": options}
    if mode == "feedback":
        feedback = _parse_string_array(response_text)
        if any(len(point) > 240 or len(point.split()) > 40 for point in feedback):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The writing assistant returned feedback that was too long. Please try again.",
            )
        return {"feedback": feedback[:5]}
    return {"rewrite": _limit_sentences(response_text, 2)}
