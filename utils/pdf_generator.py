"""Сборка PDF-отчёта: данные -> HTML (Jinja2) -> PDF (WeasyPrint)."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, TemplateNotFound, select_autoescape

from .errors import AppError

logger = logging.getLogger("report_generator.pdf")

# Корень проекта нужен и для шаблона, и для папки reports.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = PROJECT_ROOT / "templates"
DEFAULT_REPORTS_DIR = PROJECT_ROOT / "reports"
TEMPLATE_NAME = "report_template.html"

# Те же ключи, что возвращает process_dialog_with_ai.
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

# Имя по заданию: report_2025-11-13_15-30.pdf
FILENAME_MINUTE = "report_%Y-%m-%d_%H-%M.pdf"
FILENAME_SECOND = "report_%Y-%m-%d_%H-%M-%S.pdf"
MAX_NAME_ATTEMPTS = 100

STEP_MARKER = re.compile(r"^(?:[-•–—]|\d+[.)])\s+")
POSITIVE_MARKERS = (
    "позитив",
    "доволь",
    "рад",
    "спокой",
    "благодар",
    "positive",
    "satisfied",
    "happy",
    "calm",
)
NEGATIVE_MARKERS = (
    "негатив",
    "раздраж",
    "зл",
    "недовол",
    "расстро",
    "negative",
    "angry",
    "upset",
    "frustrated",
)


def generate_pdf(
    report_data: Mapping[str, str],
    output_dir: Path | None = None,
    moment: datetime | None = None,
) -> Path:
    """Подставляет данные в HTML-шаблон, конвертирует страницу в PDF и сохраняет файл."""
    created_at = moment or datetime.now()
    directory = output_dir or DEFAULT_REPORTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Сборка PDF начата",
        extra={"context": {"template": TEMPLATE_NAME}},
    )
    context = build_template_context(report_data, created_at)
    html = render_report_html(context)
    destination = allocate_report_path(directory, created_at)
    _write_pdf_atomic(html, destination)
    logger.info(
        "PDF сохранён",
        extra={"context": {"pdf_path": to_project_relative(destination)}},
    )
    return destination


def build_template_context(
    report_data: Mapping[str, str],
    moment: datetime,
) -> dict[str, object]:
    """Чистая сборка контекста шаблона из полей отчёта и момента времени."""
    missing = [
        field
        for field in REPORT_FIELDS
        if not str(report_data.get(field, "")).strip()
    ]
    if missing:
        raise AppError(
            "PDF_TEMPLATE",
            "В данных отчёта не хватает полей для шаблона.",
            {"fields": missing},
        )
    mood = str(report_data["mood"]).strip()
    steps = split_steps(str(report_data["next_steps"]))
    requirements = split_steps(str(report_data["product_requirements"]))
    return {
        "client_name": str(report_data["client_name"]).strip(),
        "topic": str(report_data["topic"]).strip(),
        "main_request": str(report_data["main_request"]).strip(),
        "mood": mood,
        "mood_tone": mood_tone(mood),
        "desired_timeline": str(report_data["desired_timeline"]).strip(),
        "desired_cost": str(report_data["desired_cost"]).strip(),
        "requirements": requirements,
        "steps": steps,
        "generated_at": format_generated_at(moment),
    }


def render_report_html(context: Mapping[str, object]) -> str:
    """Рендерит HTML-строку отчёта через Jinja2."""
    environment = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
    )
    try:
        template = environment.get_template(TEMPLATE_NAME)
    except TemplateNotFound as exc:
        raise AppError(
            "PDF_TEMPLATE",
            "Не найден HTML-шаблон отчёта.",
            {"template": TEMPLATE_NAME},
        ) from exc
    return template.render(**context)


def format_generated_at(moment: datetime) -> str:
    """Форматирует дату отчёта для шапки PDF."""
    return moment.strftime("%d.%m.%Y, %H:%M")


def mood_tone(mood: str) -> str:
    """Возвращает тон настроения для цветовой метки в шаблоне."""
    lowered = mood.lower()
    # Негативные маркеры идут раньше: смешанная формулировка не должна стать зелёной.
    if any(marker in lowered for marker in NEGATIVE_MARKERS):
        return "negative"
    if any(marker in lowered for marker in POSITIVE_MARKERS):
        return "positive"
    return "neutral"


def split_steps(text: str) -> list[str]:
    """Делит следующие шаги на пункты по строкам, а если строка одна — по точке с запятой."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) <= 1 and ";" in text:
        lines = [part.strip() for part in text.split(";") if part.strip()]
    steps = [_clean_step(line) for line in lines]
    cleaned = [step for step in steps if step]
    if cleaned:
        return cleaned
    fallback = text.strip()
    if fallback:
        return [fallback]
    return ["Не указано"]


def _clean_step(line: str) -> str:
    """Снимает маркер списка в начале пункта: «1.» или «-»."""
    return STEP_MARKER.sub("", line.strip()).strip()


def allocate_report_path(directory: Path, moment: datetime) -> Path:
    """Подбирает свободное имя PDF. Базовый вид: report_ГГГГ-ММ-ДД_ЧЧ-ММ.pdf."""
    primary = directory / moment.strftime(FILENAME_MINUTE)
    if not primary.exists():
        return primary
    secondary = directory / moment.strftime(FILENAME_SECOND)
    if not secondary.exists():
        return secondary
    index = 2
    while index <= MAX_NAME_ATTEMPTS:
        candidate = directory / f"{secondary.stem}_{index}.pdf"
        if not candidate.exists():
            return candidate
        index += 1
    raise AppError(
        "PDF_ENGINE",
        "Не удалось подобрать свободное имя файла отчёта.",
        {"directory": str(directory)},
    )


def to_project_relative(path: Path) -> str:
    """Путь относительно корня проекта с прямыми слэшами: reports/report_….pdf."""
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _write_pdf_atomic(html: str, destination: Path) -> None:
    """Пишет PDF во временный файл и только потом подменяет итоговое имя.

    WeasyPrint импортируется здесь: на Windows импорт тянет GTK и не должен
    ломать запуск, пока пользователь ещё только проверяет аргументы CLI.
    """
    try:
        from weasyprint import HTML
    except ImportError as exc:
        raise AppError(
            "PDF_ENGINE",
            "Библиотека WeasyPrint не установлена. Выполните pip install -r requirements.txt.",
        ) from exc
    except OSError as exc:
        raise _pdf_engine_error(exc) from exc

    temporary = destination.with_suffix(".pdf.part")
    try:
        HTML(string=html, base_url=str(TEMPLATES_DIR)).write_pdf(str(temporary))
        temporary.replace(destination)
    except OSError as exc:
        _remove_partial(temporary)
        raise _pdf_engine_error(exc) from exc
    except Exception as exc:
        _remove_partial(temporary)
        logger.exception("Сбой записи PDF")
        raise AppError(
            "PDF_ENGINE",
            "Не удалось конвертировать HTML в PDF.",
            {"error_type": type(exc).__name__},
        ) from exc


def _pdf_engine_error(exc: OSError) -> AppError:
    """Подсказывает про GTK, если WeasyPrint не нашёл системные библиотеки."""
    return AppError(
        "PDF_ENGINE",
        "WeasyPrint не смог создать PDF. На Windows обычно нужно установить GTK 3.",
        {"reason": str(exc)},
    )


def _remove_partial(path: Path) -> None:
    """Удаляет незавершённый файл, если конвертация оборвалась."""
    if not path.exists():
        return
    try:
        path.unlink()
    except OSError:
        logger.warning(
            "Не удалось удалить временный PDF",
            extra={"context": {"path": path.name}},
        )
