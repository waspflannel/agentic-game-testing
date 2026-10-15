"""TypeSafe Jev client: one POST /v1/systemone per decision, carrying every question about the same state.
The key is TYPESAFE_API_KEY if set, else the one saved in Settings (the OS credential store); it is never logged or sent to the browser."""
from __future__ import annotations
import asyncio
import os
import re
import time
import httpx
import keyring.errors
from .models import JEV_MODEL

URL = "https://api.typesafe.ai/v1/systemone"
TIMEOUT_SECONDS = 10.0
ATTEMPTS = 3
FAKE_ENV = "ASHAD_JEV_FAKE"
FAKE_NOUL_ENV = "ASHAD_JEV_FAKE_NOUL"  # Set it to 0.5 / 0.1 to exercise the unsure and no bands.
RETRY_STATUSES = {429: "rate_limited", 529: "overloaded"}
KEY_ENV = "TYPESAFE_API_KEY"
KEY_REFERENCE = "typesafe:jev"


class JevError(Exception):
    """status is one of unavailable, rate_limited, overloaded, timeout, connection, invalid_answer,
    bad_request:local (validate_request refused the request before sending), or bad_request:<code> for any other non-2xx,
    with :<field> appended when the JSON error body names the offending field (e.g. bad_request:422:questions.action.criteria).
    The message and status are fixed text plus that sanitized field name: never the key, headers or a body."""

    def __init__(self, status: str, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


def choice(instructions: str | dict, criteria: dict[str, str]) -> dict:
    if not 1 <= len(criteria) <= 255:
        raise ValueError("A Jev choice needs 1-255 options.")
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def noul(instructions: str | dict, criteria: dict[str, str] | None = None) -> dict:
    """criteria, when given, says what a yes and a no mean: {"true": str, "false": str}."""
    question = {"type": "noul", "instructions": instructions}
    if criteria is not None:
        if not (isinstance(criteria, dict) and set(criteria) == {"true", "false"} and all(isinstance(v, str) for v in criteria.values())):
            raise ValueError('Jev noul criteria must be {"true": str, "false": str}.')
        question["criteria"] = criteria
    return question


def score(instructions: str | dict, levels: list[str]) -> dict:
    if not 2 <= len(levels) <= 10:
        raise ValueError("A Jev score needs 2-10 levels.")
    return {"type": "score", "instructions": instructions, "criteria": levels}


def _criteria_ok(kind: str, criteria) -> bool:
    if kind == "choice":
        return (isinstance(criteria, dict) and 1 <= len(criteria) <= 255
                and all(isinstance(k, str) and (v is None or isinstance(v, str)) for k, v in criteria.items()))
    if kind == "noul":
        return isinstance(criteria, dict) and set(criteria) <= {"true", "false"} and all(isinstance(v, str) for v in criteria.values())
    return isinstance(criteria, list) and 2 <= len(criteria) <= 10 and all(isinstance(level, str) for level in criteria)


def validate_request(state, questions) -> None:
    """Checks the documented request shape before anything is sent, so the fake and real paths refuse the same requests."""
    if not isinstance(state, (str, dict, list)):
        raise JevError("bad_request:local", "Jev state must be a string, object or array.")
    if not isinstance(questions, dict) or not questions:
        raise JevError("bad_request:local", "Jev questions must be a non-empty object.")
    for question_id, question in questions.items():
        if not isinstance(question, dict):
            raise JevError("bad_request:local", f"Jev question {question_id!r} is not an object.")
        unknown = set(question) - {"type", "instructions", "criteria"}
        if unknown:
            raise JevError("bad_request:local", f"Jev question {question_id!r} has unknown field {sorted(unknown)[0]!r}.")
        kind = question.get("type")
        if kind not in ("choice", "noul", "score"):
            raise JevError("bad_request:local", f"Jev question {question_id!r} has an invalid type.")
        if not isinstance(question.get("instructions"), (str, dict, list)):
            raise JevError("bad_request:local", f"Jev question {question_id!r} has invalid instructions.")
        if (kind != "noul" or "criteria" in question) and not _criteria_ok(kind, question.get("criteria")):
            raise JevError("bad_request:local", f"Jev question {question_id!r} has invalid criteria.")


async def box(instructions, image=None):
    """Box locates a region in an image. It needs jev-1.14.0 and image input; games without a bridge would need it, so it is a stub until then."""
    return {"status": "not_available", "reason": "Box needs jev-1.14.0 and image input; ASHAD sends text only."}


def _fake() -> bool:
    return os.environ.get(FAKE_ENV) == "1"


def _saved_key(credentials) -> str | None:
    try:
        return credentials.get(KEY_REFERENCE)
    except keyring.errors.KeyringError:
        return None


def api_key(credentials) -> str | None:
    """TYPESAFE_API_KEY wins over the saved key. Keyring lookups may block, so async callers run this in a thread."""
    return os.environ.get(KEY_ENV) or _saved_key(credentials)


def status(credentials) -> dict:
    """Booleans and words only: reports whether a key is set, never the key itself. Keyring lookups may block."""
    result = {"available": True, "fake": False, "key_set": bool(_saved_key(credentials)), "env_set": bool(os.environ.get(KEY_ENV))}
    if _fake():
        return {**result, "fake": True, "message": "Fake Jev (ASHAD_JEV_FAKE=1): local answers, no API calls."}
    if result["env_set"]:
        return {**result, "message": "TYPESAFE_API_KEY is set and takes precedence over a saved key."}
    if result["key_set"]:
        return {**result, "message": "Using the TypeSafe key saved in Settings."}
    return {**result, "available": False, "message": "No TypeSafe key is saved and TYPESAFE_API_KEY is not set, so Jev hybrid runs use Today's flow."}


async def ask(state: str, questions: dict[str, dict], *, credentials, model: str = JEV_MODEL) -> dict:
    """Sends every question in one request. Retries only 429/529, which the API rejected before answering."""
    validate_request(state, questions)
    if _fake():
        return {"model": model, "answers": fake_answers(questions), "usage": {"input_tokens": 0, "output_tokens": 0}, "latency_ms": 0}
    key = await asyncio.to_thread(api_key, credentials)
    if not key:
        raise JevError("unavailable", "No TypeSafe key is saved and TYPESAFE_API_KEY is not set.")
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        for attempt in range(ATTEMPTS):
            try:
                response = await client.post(
                    URL, json={"state": state, "model": model, "questions": questions},
                    headers={"Authorization": "Bearer " + key})
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
        raise JevError(f"bad_request:{response.status_code}{_error_field(response)}", f"Jev returned {response.status_code}.")
    try:
        body = response.json()
    except ValueError:
        raise JevError("invalid_answer", "Jev returned a body that is not JSON.") from None
    check_answers(questions, body.get("answers") if isinstance(body, dict) else None)
    return {"model": body.get("model", model), "answers": body["answers"], "usage": body.get("usage"),
            "latency_ms": int((time.monotonic() - started) * 1000)}


def _error_field(response) -> str:
    """":<field>" when the JSON error body names the offending field in a loc/field/param key, else "". Only that name is kept, sanitized."""
    try:
        body = response.json()
    except ValueError:
        return ""
    nodes = [body, body.get("error"), body.get("detail")] if isinstance(body, dict) else [body]
    for node in nodes:
        if isinstance(node, list) and node:
            node = node[0]
        if not isinstance(node, dict):
            continue
        for name in ("loc", "field", "param"):
            value = node.get(name)
            if isinstance(value, list):
                value = ".".join(str(part) for part in value if isinstance(part, (str, int)))
            field = re.sub(r"[^A-Za-z0-9_.]", "", value)[:40] if isinstance(value, str) else ""
            if field:
                return ":" + field
    return ""


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
                                    "legend": {str(index): level for index, level in enumerate(question["criteria"])},
                                    "confidence": 1.0}
    return answers
