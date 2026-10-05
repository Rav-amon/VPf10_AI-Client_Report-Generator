"""Разбор транскрибации диалога через OpenAI или Claude."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, TypedDict

from dotenv import load_dotenv

from .errors import AppError

logger = logging.getLogger("report_generator.ai")

# Корень проекта: utils/ -> project_root/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

# Контракт JSON, который уходит в PDF-шаблон.
REPORT_FIELDS: tuple[str, ...] = (
    "client_name",
    "topic",
    "main_request",
    "mood",
    "desired_timeline",
    "desired_cost",
    "product_requirements",
    "next_steps",
)

EMPTY_FIELD_PLACEHOLDER = "Не указано"
DEFAULT_PROVIDER = "openai"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"
SUPPORTED_PROVIDERS = frozenset({"openai", "anthropic"})

# Ограничение текста, чтобы не отправлять в модель случайный огромный файл.
MAX_DIALOG_CHARS = 80_000
MODEL_TEMPERATURE = 0.2
# Ответ стал длиннее: в нём сроки, стоимость и список пожеланий к продукту.
MODEL_MAX_TOKENS = 1200
REQUEST_TIMEOUT_SECONDS = 60.0

MAX_RETRY_COUNT = 3
RETRY_BACKOFF_SECONDS = 1.0
TRANSIENT_STATUS_CODES = frozenset({408, 409, 429, 500, 502, 503, 504})
TRANSIENT_ERROR_NAMES = frozenset({
    "APIConnectionError",
    "APITimeoutError",
    "InternalServerError",
    "RateLimitError",
})
AUTH_ERROR_NAMES = frozenset({"AuthenticationError", "PermissionDeniedError"})

SYSTEM_PROMPT = """
Ты аналитик клиентских диалогов.
По транскрибации верни один JSON-объект и ничего больше: без markdown и без пояснений.
Обязательные ключи и только они:
- client_name: имя клиента строкой; если имени нет, напиши «Не указано»
- topic: тема разговора одной короткой фразой
- main_request: суть запроса клиента
- mood: краткая оценка настроения клиента
- desired_timeline: желаемый срок результата словами клиента; если срока нет, напиши «Не указано»
- desired_cost: бюджет или цена, которую назвал клиент, с валютой, если она прозвучала; если цены нет, напиши «Не указано». Сумму не выдумывай
- product_requirements: что обязательно должно войти в итоговый продукт. Это основные пожелания клиента, каждый пункт с новой строки. Не подменяй их следующими шагами менеджера. Если пожеланий нет, напиши «Не указано»
- next_steps: конкретные следующие шаги; каждый пункт с новой строки
Все значения — строки. Пиши на языке диалога.
Пример формы ответа:
{"client_name":"Игорь","topic":"Корпоративная телефония","main_request":"Подключить АТС на 25 сотрудников","mood":"Рабочее","desired_timeline":"Запуск через три недели","desired_cost":"До 180 тысяч рублей","product_requirements":"Очередь звонков\\nЗапись разговоров на 90 дней","next_steps":"Прислать смету сегодня\\nСозвон в четверг"}
""".strip()


class ReportData(TypedDict):
    """Структурированные поля отчёта после разбора диалога."""

    client_name: str
    topic: str
    main_request: str
    mood: str
    desired_timeline: str
    desired_cost: str
    product_requirements: str
    next_steps: str


def process_dialog_with_ai(text: str) -> ReportData:
    """Передаёт транскрибацию в модель и возвращает поля отчёта.

    Текст диалога в журнал не пишется: в нём могут быть персональные данные.
    """
    dialog = _validate_dialog(text)
    _load_environment()
    provider = _resolve_provider()
    model = _resolve_model(provider)
    started = time.perf_counter()
    # В журнал попадают только служебные поля. Текст диалога и ответ модели не пишем.
    logger.info(
        "Запрос к модели",
        extra={
            "context": {
                "provider": provider,
                "model": model,
                "dialog_chars": len(dialog),
            }
        },
    )
    raw_answer = _call_with_retry(lambda: _request_completion(provider, dialog))
    payload = _extract_json_object(raw_answer)
    report = _normalize_report(payload)
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    logger.info(
        "Ответ модели получен",
        extra={
            "context": {
                "provider": provider,
                "model": model,
                "elapsed_ms": elapsed_ms,
            }
        },
    )
    return report


def _validate_dialog(text: str) -> str:
    """Проверяет, что на вход пришла непустая транскрибация разумной длины."""
    if not isinstance(text, str):
        raise AppError(
            "EMPTY_DIALOG",
            "Транскрибация должна быть строкой.",
        )
    dialog = text.strip()
    if not dialog:
        raise AppError("EMPTY_DIALOG", "Транскрибация пуста.")
    if len(dialog) > MAX_DIALOG_CHARS:
        raise AppError(
            "DIALOG_TOO_LONG",
            "Транскрибация слишком длинная для одного отчёта.",
            {"max_chars": MAX_DIALOG_CHARS, "actual_chars": len(dialog)},
        )
    return dialog


def _load_environment() -> None:
    """Подхватывает ключи из .env в корне проекта, не затирая уже заданные переменные."""
    load_dotenv(ENV_PATH)


def _resolve_provider() -> str:
    """Читает AI_PROVIDER и проверяет, что выбран поддерживаемый сервис."""
    provider = _setting("AI_PROVIDER", DEFAULT_PROVIDER).lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise AppError(
            "UNSUPPORTED_PROVIDER",
            "Провайдер ИИ не поддерживается. Допустимы openai и anthropic.",
            {"provider": provider},
        )
    return provider


def _setting(name: str, default: str = "") -> str:
    """Возвращает переменную окружения без крайних пробелов."""
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip()
    if not value:
        return default
    return value


def _require_secret(name: str) -> str:
    """Возвращает API-ключ или понятную ошибку, если ключ не задан."""
    secret = os.getenv(name, "").strip()
    if not secret:
        raise AppError(
            "AI_AUTH",
            f"Не задан {name}. Добавьте ключ в файл .env.",
            {"env": name},
        )
    return secret


def _resolve_model(provider: str) -> str:
    """Возвращает имя модели выбранного провайдера."""
    if provider == "anthropic":
        return _setting("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)
    return _setting("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)


def _request_completion(provider: str, dialog: str) -> str:
    """Выбирает клиент OpenAI или Anthropic и возвращает сырой текст ответа."""
    if provider == "anthropic":
        return _request_anthropic(dialog)
    return _request_openai(dialog)


def _request_openai(dialog: str) -> str:
    """Запрашивает JSON-ответ у Chat Completions API OpenAI."""
    from openai import OpenAI

    api_key = _require_secret("OPENAI_API_KEY")
    model = _resolve_model("openai")
    # Свои повторы уже есть в _call_with_retry, поэтому у клиента max_retries=0.
    client = OpenAI(
        api_key=api_key,
        timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=0,
    )
    completion = client.chat.completions.create(
        model=model,
        temperature=MODEL_TEMPERATURE,
        max_tokens=MODEL_MAX_TOKENS,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Транскрибация диалога:\n{dialog}"},
        ],
    )
    content = completion.choices[0].message.content
    if not isinstance(content, str) or not content.strip():
        raise AppError("AI_INVALID_RESPONSE", "Модель OpenAI вернула пустой ответ.")
    return content


def _request_anthropic(dialog: str) -> str:
    """Запрашивает JSON-ответ у Messages API Anthropic."""
    from anthropic import Anthropic

    api_key = _require_secret("ANTHROPIC_API_KEY")
    model = _resolve_model("anthropic")
    client = Anthropic(
        api_key=api_key,
        timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=0,
    )
    # У messages.create в актуальном SDK нет аргумента temperature.
    message = client.messages.create(
        model=model,
        max_tokens=MODEL_MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": f"Транскрибация диалога:\n{dialog}"},
        ],
    )
    parts: list[str] = []
    for block in message.content:
        text_part = getattr(block, "text", None)
        if isinstance(text_part, str):
            parts.append(text_part)
    content = "".join(parts).strip()
    if not content:
        raise AppError("AI_INVALID_RESPONSE", "Модель Claude вернула пустой ответ.")
    return content


def _call_with_retry(operation: Callable[[], str]) -> str:
    """Повторяет временные сбои сети и лимитов с экспоненциальной паузой."""
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRY_COUNT + 1):
        try:
            return operation()
        except AppError:
            raise
        except Exception as exc:
            last_error = exc
            can_retry = attempt < MAX_RETRY_COUNT and _is_transient(exc)
            if not can_retry:
                break
            delay = RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "Повтор запроса к модели",
                extra={
                    "context": {
                        "attempt": attempt,
                        "delay_seconds": delay,
                        "error_type": type(exc).__name__,
                    }
                },
            )
            time.sleep(delay)
    raise _map_provider_error(last_error)


def _is_transient(exc: Exception) -> bool:
    """Отличает временный сбой, который стоит повторить, от ошибки настроек."""
    if type(exc).__name__ in TRANSIENT_ERROR_NAMES:
        return True
    status_code = getattr(exc, "status_code", None)
    return isinstance(status_code, int) and status_code in TRANSIENT_STATUS_CODES


def _map_provider_error(exc: Exception | None) -> AppError:
    """Переводит исключение SDK в код, сообщение и детали без секретов."""
    if exc is None:
        return AppError("AI_UPSTREAM", "Модель не вернула ответ.")
    status_code = getattr(exc, "status_code", None)
    error_name = type(exc).__name__
    details: dict[str, object] = {"error_type": error_name}
    if isinstance(status_code, int):
        details["status_code"] = status_code
    if error_name in AUTH_ERROR_NAMES or status_code in {401, 403}:
        return AppError(
            "AI_AUTH",
            "Ключ API отклонён. Проверьте значение в файле .env.",
            details,
        )
    if error_name == "RateLimitError" or status_code == 429:
        return AppError(
            "AI_RATE_LIMIT",
            "Превышен лимит запросов к модели. Повторите попытку позже.",
            details,
        )
    logger.error(
        "Ошибка провайдера модели",
        extra={"context": details},
    )
    return AppError(
        "AI_UPSTREAM",
        "Не удалось получить ответ модели.",
        details,
    )


def _extract_json_object(raw: str) -> dict[str, object]:
    """Достаёт JSON-объект из ответа, даже если модель обернула его в ```."""
    cleaned = _strip_code_fence(raw.strip())
    parsed = _loads_object(cleaned)
    if parsed is None:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end > start:
            parsed = _loads_object(cleaned[start : end + 1])
    if parsed is None:
        raise AppError(
            "AI_INVALID_RESPONSE",
            "Модель вернула ответ не в формате JSON.",
        )
    return parsed


def _strip_code_fence(text: str) -> str:
    """Убирает markdown-ограду ```json ... ``` вокруг объекта."""
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    without_open = lines[1:]
    if without_open and without_open[-1].strip().startswith("```"):
        without_open = without_open[:-1]
    return "\n".join(without_open).strip()


def _loads_object(text: str) -> dict[str, object] | None:
    """Парсит JSON и оставляет только объект со строковыми ключами."""
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    result: dict[str, object] = {}
    for key, value in parsed.items():
        if not isinstance(key, str):
            return None
        result[key] = value
    return result


def _normalize_report(payload: dict[str, object]) -> ReportData:
    """Приводит ответ модели к непустым строкам полей отчёта."""
    normalized: dict[str, str] = {}
    for field in REPORT_FIELDS:
        normalized[field] = _as_report_string(payload.get(field), field)
    return ReportData(
        client_name=normalized["client_name"],
        topic=normalized["topic"],
        main_request=normalized["main_request"],
        mood=normalized["mood"],
        desired_timeline=normalized["desired_timeline"],
        desired_cost=normalized["desired_cost"],
        product_requirements=normalized["product_requirements"],
        next_steps=normalized["next_steps"],
    )


def _as_report_string(value: object, field: str) -> str:
    """Превращает строку или список модели в текст поля отчёта."""
    if isinstance(value, list):
        lines = [str(item).strip() for item in value if str(item).strip()]
        text = "\n".join(lines)
    elif isinstance(value, str):
        text = value.strip()
    elif value is None:
        text = ""
    else:
        text = str(value).strip()
    if text:
        return text
    logger.warning(
        "Модель не заполнила поле отчёта",
        extra={"context": {"field": field}},
    )
    return EMPTY_FIELD_PLACEHOLDER
