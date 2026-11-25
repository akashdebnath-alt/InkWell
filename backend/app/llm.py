import json
import logging
import re
import string

from fastapi import HTTPException, status
from google import genai
from google.genai import errors, types

from .config import settings

logger = logging.getLogger(__name__)

GHOST_SYSTEM = """You are an inline suggestion engine for a creative writing editor.
Continue the author's text in their voice with only 1-8 next words. Never write a
sentence, paragraph, story, poem, or chapter. Do not repeat words already at the end
of the author's text. Return only the suggestion, without quotes or explanation."""

WRITING_TYPE_GUIDANCE = {
    "general": "Support the author's chosen form and preserve their voice.",
    "poem": "Treat this as a poem. Attend to sound, imagery, line breaks, and rhythm; do not force a fixed meter.",
    "story": "Treat this as fiction. Preserve the established point of view, tense, character voice, and continuity.",
    "essay": "Treat this as an essay. Favor clear claims, logical transitions, and precise language.",
}

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
    "next_line": """You are a collaborative writing partner. Continue from the selected
passage with exactly three distinct possible next lines. Keep each option to one line,
match the author's voice and point of view, and do not continue the whole work. Return
only a JSON array of three strings.""",
    "rhythm": """You are a poetry and prose rhythm editor. Revise only the selected
passage, preserving its meaning and voice while improving cadence, stress, and sound.
Offer exactly three concise alternatives as a JSON array of three strings. Do not impose
a fixed meter unless the author asks for one.""",
    "story_ideas": """You are a fiction story-idea partner. Based on the author's draft,
notes, and request, offer exactly five distinct, concise story directions, conflicts, or
turns the author could develop. Do not write a scene or the whole story. Return only a
JSON array of five strings.""",
}


def _client() -> genai.Client:
    if not settings.has_gemini_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI features are unavailable. Configure GEMINI_API_KEY in backend/.env.",
        )
    return genai.Client(api_key=settings.gemini_api_key)


def _gemini_error(error: errors.APIError) -> HTTPException:
    code = getattr(error, "code", None)
    if code == 400:
        detail = "Gemini rejected the request. Check the configured model and try again."
    elif code == 401:
        detail = "Gemini rejected the API key. Check GEMINI_API_KEY in backend/.env."
    elif code == 403:
        detail = "Gemini denied access. Check the API key's project and API access."
    elif code == 404:
        detail = "The configured Gemini model was not found. Check GEMINI_MODEL in backend/.env."
    elif code == 429:
        detail = "Gemini's rate limit or usage quota was reached. Check your account usage and try again."
    elif isinstance(code, int) and code >= 500:
        detail = "Gemini is temporarily unavailable. Please try again shortly."
    else:
        detail = "The writing assistant could not complete that request. Please try again."
    logger.error("Gemini API request failed with status %s", code)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=detail,
    )


def _generate_text(
    system_instruction: str,
    prompt: str,
    *,
    max_output_tokens: int,
    json_output: bool = False,
) -> str:
    client = _client()
    config = {
        "system_instruction": system_instruction,
        "max_output_tokens": max_output_tokens,
    }
    if json_output:
        config["response_mime_type"] = "application/json"
    try:
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(**config),
        )
    except errors.APIError as exc:
        raise _gemini_error(exc) from exc
    finally:
        client.close()
    return _response_text(response)


def _response_text(response) -> str:
    text = (getattr(response, "text", None) or "").strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gemini returned no text. Try again with a shorter or different prompt.",
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


def ghost(
    text_before: str,
    context: list[str],
    writing_type: str = "general",
) -> str:
    context_text = "\n---\n".join(context)
    message = (
        f"Author's related notes and earlier passages:\n{context_text}\n\n"
        f"Text before cursor:\n{text_before}"
    )
    guidance = WRITING_TYPE_GUIDANCE.get(
        writing_type,
        WRITING_TYPE_GUIDANCE["general"],
    )
    response_text = _generate_text(
        f"{GHOST_SYSTEM}\n\n{guidance}",
        message,
        max_output_tokens=20,
    )
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
            detail=(
                f"The writing assistant could not provide {exact_count} valid options. "
                "Please try again."
            ),
        )
    return values


def _limit_sentences(text: str, limit: int) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(sentences[:limit]).strip()


def assist(
    mode: str,
    request: str,
    selection: str,
    context: list[str],
    writing_type: str = "general",
) -> dict:
    if mode not in ASSIST_SYSTEMS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Choose a supported writing-assistant mode.",
        )
    if not selection.strip() and mode != "story_ideas":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Select a word or passage in your document first.",
        )

    context_text = "\n---\n".join(context)
    selected_text = selection.strip() or "(No text selected.)"
    message = (
        f"Author's related notes and earlier passages:\n{context_text}\n\n"
        f"Draft or selected text:\n{selected_text}\n\nRequest:\n{request.strip()}"
    )
    if mode == "rewrite_short":
        max_tokens = 120
    elif mode == "story_ideas":
        max_tokens = 280
    elif mode in {"next_line", "rhythm"}:
        max_tokens = 220
    else:
        max_tokens = 450
    guidance = WRITING_TYPE_GUIDANCE.get(
        writing_type,
        WRITING_TYPE_GUIDANCE["general"],
    )
    structured_modes = {
        "synonyms",
        "rhymes",
        "imagery",
        "feedback",
        "next_line",
        "rhythm",
        "story_ideas",
    }
    response_text = _generate_text(
        f"{ASSIST_SYSTEMS[mode]}\n\n{guidance}",
        message,
        max_output_tokens=max_tokens,
        json_output=mode in structured_modes,
    )
    if mode in {
        "synonyms",
        "rhymes",
        "imagery",
        "next_line",
        "rhythm",
        "story_ideas",
    }:
        exact_count = 5 if mode in {"synonyms", "rhymes", "imagery", "story_ideas"} else 3
        options = _parse_string_array(response_text, exact_count=exact_count)
        if mode in {"synonyms", "rhymes", "imagery"} and any(
            len(option.split()) > 8 or len(option) > 120 for option in options
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The writing assistant returned options that were too long. Please try again.",
            )
        if mode == "story_ideas" and any(
            len(option.split()) > 50 or len(option) > 300 for option in options
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The writing assistant returned story ideas that were too long. Please try again.",
            )
        if mode in {"next_line", "rhythm"} and any(
            len(option.split()) > 35 or len(option) > 240 for option in options
        ):
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
