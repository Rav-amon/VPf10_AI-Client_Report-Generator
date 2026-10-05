"""Структурный журнал: JSON в консоль и в файл logs/report_generator.log."""

from __future__ import annotations

import json
import logging
import re
import sys
import uuid
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Корень проекта: utils/ -> project_root/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "report_generator.log"

LOGGER_NAME = "report_generator"
MAX_LOG_BYTES = 1_048_576
LOG_BACKUP_COUNT = 3

# Имена полей, которые нельзя писать даже если их передали в context.
SENSITIVE_KEYS = frozenset(
    {"api_key", "authorization", "token", "secret", "password", "openai_api_key", "anthropic_api_key"}
)
SECRET_TEXT = re.compile(
    r"(?i)(?:sk-ant-[a-z0-9_\-]{8,}|sk-[a-z0-9_\-]{8,}|bearer\s+[a-z0-9\-._]+)"
)

# Общий идентификатор прогона CLI или HTTP-запроса. В журнал попадает сам, без ручной передачи.
correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")

# Сторонние библиотеки шумят на каждом PDF. Их сообщения в наш журнал не берём.
QUIET_LOGGERS = (
    "weasyprint",
    "fontTools",
    "fontTools.subset",
    "werkzeug",
    "httpx",
    "httpx2",
    "httpcore",
    "httpcore2",
)


class JsonLogFormatter(logging.Formatter):
    """Пишет запись одной JSON-строкой: level, message, context."""

    def format(self, record: logging.LogRecord) -> str:
        context = _sanitize(getattr(record, "context", {}))
        if not isinstance(context, dict):
            context = {"value": context}
        payload_context: dict[str, object] = {
            "logger": record.name,
            "correlation_id": correlation_id.get(),
        }
        payload_context.update(context)
        if record.exc_info:
            payload_context["exception"] = _redact_text(self.formatException(record.exc_info))
        payload = {
            "level": record.levelname,
            "message": _redact_text(record.getMessage()),
            "context": payload_context,
        }
        return json.dumps(payload, ensure_ascii=False, default=str)


def new_correlation_id() -> str:
    """Создаёт идентификатор текущего прогона и сохраняет его для всех следующих записей."""
    value = uuid.uuid4().hex
    correlation_id.set(value)
    return value


def configure_logging(verbose: bool) -> None:
    """Включает журнал приложения. --verbose добавляет сообщения уровня DEBUG.

    Обработчики висят только на logger report_generator, чтобы в файл
    не попадали внутренние сообщения OpenAI, Flask и WeasyPrint.
    """
    level = logging.DEBUG if verbose else logging.INFO
    app_logger = logging.getLogger(LOGGER_NAME)
    _close_handlers(app_logger)
    app_logger.setLevel(level)
    app_logger.propagate = False

    formatter = JsonLogFormatter()
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)
    app_logger.addHandler(stream_handler)
    _attach_file_handler(app_logger, formatter, level)

    for logger_name in QUIET_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.ERROR)


def _attach_file_handler(
    app_logger: logging.Logger,
    formatter: logging.Formatter,
    level: int,
) -> None:
    """Добавляет вращающийся файл журнала. Сбой диска не останавливает программу."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=MAX_LOG_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"Не удалось открыть файл журнала: {exc}", file=sys.stderr)
        return
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    app_logger.addHandler(file_handler)


def _close_handlers(app_logger: logging.Logger) -> None:
    """Закрывает старые обработчики, чтобы повторная настройка не дублировала строки."""
    for handler in list(app_logger.handlers):
        app_logger.removeHandler(handler)
        handler.close()


def _sanitize(value: object, key: str | None = None) -> object:
    """Убирает секреты из контекста, если они туда попали по ошибке."""
    if key is not None and key.lower() in SENSITIVE_KEYS:
        return "[скрыто]"
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        cleaned: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            item_key = str(raw_key)
            cleaned[item_key] = _sanitize(raw_value, item_key)
        return cleaned
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _redact_text(text: str) -> str:
    """Маскирует похожие на ключи API фрагменты внутри строки."""
    return SECRET_TEXT.sub("[скрыто]", text)
