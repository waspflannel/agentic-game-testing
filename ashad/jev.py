"""TypeSafe Jev client: one POST /v1/systemone per decision, carrying every question about the same state. The key is TYPESAFE_API_KEY only; it is never stored or logged."""
from __future__ import annotations
import asyncio
import os
import time
import httpx
from .models import JEV_MODEL

URL = "https://api.typesafe.ai/v1/systemone"
TIMEOUT_SECONDS = 10.0
ATTEMPTS = 3
FAKE_ENV = "ASHAD_JEV_FAKE"
FAKE_NOUL_ENV = "ASHAD_JEV_FAKE_NOUL"  # Set it to 0.5 / 0.1 to exercise the unsure and no bands.
RETRY_STATUSES = {429: "rate_limited", 529: "overloaded"}


class JevError(Exception):
    """status is one of unavailable, rate_limited, overloaded, timeout, connection, bad_request, invalid_answer.
    The message is fixed text: never the key, headers or request body."""

    def __init__(self, status: str, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


def choice(instructions: str, criteria: dict[str, str]) -> dict:
    if not 1 <= len(criteria) <= 255:
        raise ValueError("A Jev choice needs 1-255 options.")
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def noul(instructions: str, criteria: str | None = None) -> dict:
    question = {"type": "noul", "instructions": instructions}
    if criteria is not None:
        question["criteria"] = criteria
    return question


def score(instructions: str, levels: list[str]) -> dict:
    if not 2 <= len(levels) <= 10:
        raise ValueError("A Jev score needs 2-10 levels.")
    return {"type": "score", "instructions": instructions, "levels": levels}


async def box(instructions, image=None):
    """Box locates a region in an image. It needs jev-1.14.0 and image input; games without a bridge would need it, so it is a stub until then."""
    return {"status": "not_available", "reason": "Box needs jev-1.14.0 and image input; ASHAD sends text only."}


def _fake() -> bool:
    return os.environ.get(FAKE_ENV) == "1"


def status() -> dict:
    """Words only: reports whether the key is set, never the key itself."""
    if _fake():
        return {"available": True, "fake": True, "message": "Fake Jev (ASHAD_JEV_FAKE=1): local answers, no API calls."}
    if os.environ.get("TYPESAFE_API_KEY"):
        return {"available": True, "fake": False, "message": "TYPESAFE_API_KEY is set."}
    return {"available": False, "fake": False, "message": "TYPESAFE_API_KEY is not set, so Jev hybrid runs use Today's flow."}


async def ask(state: str, questions: dict[str, dict], *, model: str = JEV_MODEL) -> dict:
    """Sends every question in one request. Retries only 429/529, which the API rejected before answering."""
    if _fake():
        return {"model": model, "answers": fake_answers(questions), "usage": {"input_tokens": 0, "output_tokens": 0}, "latency_ms": 0}
    if not os.environ.get("TYPESAFE_API_KEY"):
        raise JevError("unavailable", "TYPESAFE_API_KEY is not set.")
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        for attempt in range(ATTEMPTS):
            try:
                response = await client.post(
                    URL, json={"state": state, "model": model, "questions": questions},
                    headers={"Authorization": "Bearer " + os.environ["TYPESAFE_API_KEY"]})
            except httpx.TimeoutException:
                raise JevError("timeout", f"Jev did not answer within {TIMEOUT_SECONDS:g} s.") from None
            except httpx.TransportError:
                raise JevError("connection", "Could not reach the Jev API.") from None
            if response.status_code not in RETRY_STATUSES:
                break
            if attempt < ATTEMPTS - 1:
                try:
                    delay = min(5.0, float(response.headers["retry-after"]))
                except (KeyError, ValueError):
                    delay = 0.5 * 2 ** attempt
                await asyncio.sleep(delay)
    if response.status_code in RETRY_STATUSES:
        raise JevError(RETRY_STATUSES[response.status_code], f"Jev returned {response.status_code} after {ATTEMPTS} attempts.")
    if not response.is_success:
        raise JevError("bad_request", f"Jev returned {response.status_code}.")
    try:
        body = response.json()
    except ValueError:
        raise JevError("invalid_answer", "Jev returned a body that is not JSON.") from None
    check_answers(questions, body.get("answers") if isinstance(body, dict) else None)
    return {"model": body.get("model", model), "answers": body["answers"], "usage": body.get("usage"),
            "latency_ms": int((time.monotonic() - started) * 1000)}


def check_answers(questions: dict[str, dict], answers) -> None:
    if not isinstance(answers, dict):
        raise JevError("invalid_answer", "Jev answers are not an object.")
    for question_id, question in questions.items():
        answer = answers.get(question_id)
        if not isinstance(answer, dict) or answer.get("type") != question["type"]:
            raise JevError("invalid_answer", f"Jev answer {question_id!r} is missing or has the wrong type.")
        if question["type"] == "choice":
            valid = answer.get("choice") in question["criteria"] and "probabilities" in answer and "confidence" in answer
        elif question["type"] == "noul":
            value = answer.get("noul")
            # JSON 0 and 1 decode as int; true is an int too but is not a probability.
            valid = isinstance(value, (int, float)) and not isinstance(value, bool) and 0.0 <= value <= 1.0
        else:
            valid = all(field in answer for field in ("score", "probabilities", "confidence"))
        if not valid:
            raise JevError("invalid_answer", f"Jev answer {question_id!r} does not match its question.")


def fake_answers(questions: dict[str, dict]) -> dict:
    """Deterministic answers in the documented shapes: the first option, a 0.9 yes (or ASHAD_JEV_FAKE_NOUL), and the lowest level."""
    answers = {}
    for question_id, question in questions.items():
        if question["type"] == "choice":
            options = list(question["criteria"])
            n = len(options)
            probabilities = {option: 0.9 if index == 0 else 0.1 / (n - 1) for index, option in enumerate(options)}
            confidence = 1.0 if n == 1 else (0.9 - 1 / n) / (1 - 1 / n)
            answers[question_id] = {"type": "choice", "choice": options[0], "probabilities": probabilities, "confidence": confidence}
        elif question["type"] == "noul":
            noul_value = min(1.0, max(0.0, float(os.environ.get(FAKE_NOUL_ENV, "0.9"))))
            answers[question_id] = {"type": "noul", "noul": noul_value}
        else:
            answers[question_id] = {"type": "score", "score": 0, "probabilities": {"0": 1.0},
                                    "legend": {str(index): level for index, level in enumerate(question["levels"])},
                                    "confidence": 1.0}
    return answers
