"""Точка входа: транскрибация -> модель -> PDF-отчёт."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from utils.ai_processor import MAX_DIALOG_CHARS, ReportData, process_dialog_with_ai
from utils.errors import AppError
from utils.logging_config import configure_logging, new_correlation_id
from utils.pdf_generator import generate_pdf, to_project_relative

# Корень проекта совпадает с папкой, где лежит этот файл.
PROJECT_ROOT = Path(__file__).resolve().parent

ALLOWED_DIALOG_SUFFIXES = frozenset({".txt", ".md", ".text"})
MAX_FILE_SIZE_BYTES = 1_048_576
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 5000

# Коды ошибок -> HTTP-статусы. Незнакомый код остаётся 400.
HTTP_STATUS_BY_CODE = {
    "EMPTY_DIALOG": 400,
    "AMBIGUOUS_INPUT": 400,
    "FILE_NOT_FOUND": 404,
    "UNSUPPORTED_FILE_TYPE": 415,
    "FILE_TOO_LARGE": 413,
    "DIALOG_TOO_LONG": 413,
    "AI_AUTH": 401,
    "AI_RATE_LIMIT": 429,
    "AI_UPSTREAM": 502,
    "AI_INVALID_RESPONSE": 502,
    "UNSUPPORTED_PROVIDER": 500,
    "PDF_ENGINE": 500,
    "PDF_TEMPLATE": 500,
    "DEPENDENCY_MISSING": 500,
    "NOT_FOUND": 404,
    "METHOD_NOT_ALLOWED": 405,
    "INTERNAL": 500,
}

HTTP_ERROR_MESSAGES = {
    404: "Маршрут не найден.",
    405: "Метод не поддерживается.",
    413: "Тело запроса превышает допустимый размер.",
}

logger = logging.getLogger("report_generator")


def configure_stdio() -> None:
    """Переключает консоль на UTF-8, чтобы кириллица в Windows не ломалась."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            # Этот поток не поддерживает смену кодировки — пробуем следующий.
            continue


def build_parser() -> argparse.ArgumentParser:
    """Описывает аргументы CLI и примеры запуска."""
    parser = argparse.ArgumentParser(
        description="Формирует PDF-отчёт по диалогу с клиентом.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  python main.py --file examples/sample_dialog.txt\n"
            "  python main.py --text \"Клиент просит перенести встречу\"\n"
            "  python main.py --api\n"
        ),
    )
    parser.add_argument(
        "-f",
        "--file",
        type=Path,
        help="Путь к текстовому файлу с транскрибацией (.txt, .md).",
    )
    parser.add_argument(
        "-t",
        "--text",
        help="Транскрибация одной строкой.",
    )
    parser.add_argument(
        "--api",
        action="store_true",
        help="Запустить необязательный HTTP API на Flask.",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_API_HOST,
        help=f"Адрес API, по умолчанию {DEFAULT_API_HOST}.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_API_PORT,
        help=f"Порт API, по умолчанию {DEFAULT_API_PORT}.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Писать в журнал сообщения уровня DEBUG.",
    )
    return parser


def read_dialog(file_path: Path | None, inline_text: str | None) -> str:
    """Берёт транскрибацию из файла, аргумента, потока или ручного ввода."""
    if file_path is not None and inline_text is not None:
        raise AppError(
            "AMBIGUOUS_INPUT",
            "Укажите либо файл, либо текст, но не оба источника сразу.",
        )
    if inline_text is not None:
        return inline_text
    if file_path is not None:
        return read_dialog_file(file_path)
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return read_dialog_interactive()


def read_dialog_file(path: Path) -> str:
    """Читает текстовый файл транскрибации с проверкой типа и размера."""
    if not path.is_file():
        raise AppError(
            "FILE_NOT_FOUND",
            "Файл транскрибации не найден.",
            {"path": str(path)},
        )
    suffix = path.suffix.lower()
    if suffix not in ALLOWED_DIALOG_SUFFIXES:
        raise AppError(
            "UNSUPPORTED_FILE_TYPE",
            "Нужен текстовый файл с расширением .txt, .md или .text.",
            {"suffix": suffix},
        )
    size = path.stat().st_size
    if size > MAX_FILE_SIZE_BYTES:
        raise AppError(
            "FILE_TOO_LARGE",
            "Файл больше 1 МБ.",
            {"max_bytes": MAX_FILE_SIZE_BYTES, "actual_bytes": size},
        )
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AppError(
            "UNSUPPORTED_FILE_TYPE",
            "Файл должен быть в кодировке UTF-8.",
        ) from exc


def read_dialog_interactive() -> str:
    """Просит ввести диалог вручную до символа конца ввода."""
    if sys.platform.startswith("win"):
        finish_hint = "Ctrl+Z, затем Enter"
    else:
        finish_hint = "Ctrl+D"
    print(
        "Введите транскрибацию диалога.\n"
        f"Чтобы закончить ввод, нажмите {finish_hint}.\n"
    )
    return sys.stdin.read()


def ensure_dialog_length(text: str) -> str:
    """Отсекает пустой ввод до обращения к модели. Длину повторно проверит процессор."""
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


def create_report_file(text: str) -> tuple[Path, ReportData]:
    """Прогоняет диалог через модель и сохраняет PDF."""
    dialog = ensure_dialog_length(text)
    report = process_dialog_with_ai(dialog)
    return generate_pdf(report), report


def run_cli(file_path: Path | None, inline_text: str | None) -> int:
    """CLI-сценарий из задания: прочитать, спросить модель, записать PDF, сообщить путь."""
    new_correlation_id()
    source = describe_dialog_source(file_path, inline_text)
    try:
        dialog = read_dialog(file_path, inline_text)
        logger.info(
            "Транскрибация прочитана",
            extra={
                "context": {
                    "source": source,
                    "dialog_chars": len(dialog.strip()),
                    "file_name": file_path.name if file_path is not None else "",
                }
            },
        )
        pdf_path, _report = create_report_file(dialog)
    except AppError as exc:
        logger.error(
            "Отчёт не создан",
            extra={"context": {"code": exc.code, "source": source}},
        )
        print(f"Ошибка [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1
    except Exception:
        logger.exception("Необработанная ошибка CLI")
        print("Не удалось создать отчёт.", file=sys.stderr)
        return 1
    public_path = to_project_relative(pdf_path)
    logger.info("Отчёт создан", extra={"context": {"pdf_path": public_path}})
    print(f"Отчёт успешно создан: {public_path}")
    return 0


def describe_dialog_source(file_path: Path | None, inline_text: str | None) -> str:
    """Называет источник текста, не открывая сам файл и не копируя содержимое."""
    if file_path is not None and inline_text is not None:
        return "ambiguous"
    if inline_text is not None:
        return "text"
    if file_path is not None:
        return "file"
    if not sys.stdin.isatty():
        return "stdin"
    return "interactive"


def create_app() -> object:
    """Собирает необязательный Flask API. Импорт Flask только в этом режиме."""
    try:
        from flask import Flask, Response, g, jsonify, request, send_file
        from werkzeug.datastructures import FileStorage
        from werkzeug.exceptions import HTTPException
    except ImportError as exc:
        raise AppError(
            "DEPENDENCY_MISSING",
            "Библиотека Flask не установлена. Выполните pip install -r requirements.txt.",
        ) from exc

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE_BYTES

    @app.before_request
    def assign_request_id() -> None:
        """Ставит идентификатор запроса, чтобы связать журнал и ответ."""
        g.request_id = new_correlation_id()
        g.started_at = time.perf_counter()
        logger.info(
            "HTTP-запрос",
            extra={"context": {"method": request.method, "path": request.path}},
        )

    @app.after_request
    def attach_request_id(response: Response) -> Response:
        request_id = getattr(g, "request_id", "")
        if isinstance(request_id, str) and request_id:
            response.headers["X-Request-Id"] = request_id
        started_at = getattr(g, "started_at", None)
        elapsed_ms = 0
        if isinstance(started_at, float):
            elapsed_ms = round((time.perf_counter() - started_at) * 1000)
        logger.info(
            "HTTP-ответ",
            extra={
                "context": {
                    "method": request.method,
                    "path": request.path,
                    "status": response.status_code,
                    "elapsed_ms": elapsed_ms,
                }
            },
        )
        return response

    def fail(exc: AppError):
        status = HTTP_STATUS_BY_CODE.get(exc.code, 400)
        body = {"data": None, "error": exc.to_dict(), "message": exc.message}
        return jsonify(body), status

    def ok(data: dict[str, object], message: str, status: int):
        return jsonify({"data": data, "error": None, "message": message}), status

    @app.get("/api/v1/health")
    def health():
        return ok({"status": "ok"}, "Сервис доступен.", 200)

    @app.post("/api/v1/reports")
    def create_report():
        try:
            dialog = dialog_from_request()
            pdf_path, report = create_report_file(dialog)
            public_path = to_project_relative(pdf_path)
            logger.info(
                "Отчёт создан через API",
                extra={
                    "context": {
                        "pdf_path": public_path,
                        "request_id": getattr(g, "request_id", ""),
                    }
                },
            )
            if request.args.get("download") == "1":
                return send_file(
                    pdf_path,
                    mimetype="application/pdf",
                    as_attachment=True,
                    download_name=pdf_path.name,
                )
            return ok(
                {"pdf_path": public_path, "report": dict(report)},
                "Отчёт успешно создан",
                201,
            )
        except AppError as exc:
            status = HTTP_STATUS_BY_CODE.get(exc.code, 400)
            log = logger.error if status >= 500 else logger.warning
            log(
                "Запрос отчёта отклонён",
                extra={"context": {"code": exc.code, "status": status}},
            )
            return fail(exc)
        except Exception:
            logger.exception("Необработанная ошибка API")
            return fail(AppError("INTERNAL", "Внутренняя ошибка сервера."))

    def dialog_from_request() -> str:
        """Достаёт текст из JSON, формы, text/plain или загруженного файла."""
        uploaded = request.files.get("file")
        if uploaded is not None and uploaded.filename:
            return read_uploaded_file(uploaded)
        payload = request.get_json(silent=True)
        if isinstance(payload, dict) and isinstance(payload.get("text"), str):
            return payload["text"]
        form_text = request.form.get("text")
        if isinstance(form_text, str) and form_text.strip():
            return form_text
        content_type = request.content_type or ""
        if content_type.startswith("text/plain"):
            raw = request.get_data(as_text=True)
            if raw.strip():
                return raw
        raise AppError(
            "EMPTY_DIALOG",
            "Передайте транскрибацию в поле text или файлом file.",
        )

    def read_uploaded_file(uploaded: FileStorage) -> str:
        """Проверяет расширение и размер загруженной транскрибации."""
        filename = uploaded.filename or ""
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_DIALOG_SUFFIXES:
            raise AppError(
                "UNSUPPORTED_FILE_TYPE",
                "Нужен текстовый файл с расширением .txt, .md или .text.",
                {"suffix": suffix},
            )
        raw_bytes = uploaded.read(MAX_FILE_SIZE_BYTES + 1)
        if len(raw_bytes) > MAX_FILE_SIZE_BYTES:
            raise AppError(
                "FILE_TOO_LARGE",
                "Файл больше 1 МБ.",
                {"max_bytes": MAX_FILE_SIZE_BYTES},
            )
        try:
            return bytes(raw_bytes).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise AppError(
                "UNSUPPORTED_FILE_TYPE",
                "Файл должен быть в кодировке UTF-8.",
            ) from exc

    @app.errorhandler(HTTPException)
    def handle_http(exc: HTTPException):
        status = exc.code or 500
        message = HTTP_ERROR_MESSAGES.get(status, "Ошибка запроса.")
        error = AppError(
            "HTTP_ERROR",
            message,
            {"status_code": status},
        )
        return jsonify(
            {"data": None, "error": error.to_dict(), "message": message}
        ), status

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):
        if isinstance(exc, HTTPException):
            return handle_http(exc)
        logger.exception("Необработанная ошибка API")
        error = AppError("INTERNAL", "Внутренняя ошибка сервера.")
        return jsonify(
            {"data": None, "error": error.to_dict(), "message": error.message}
        ), 500

    return app


def run_api(host: str, port: int) -> int:
    """Запускает локальный API. Отладочный режим Flask выключен."""
    new_correlation_id()
    if port < 1 or port > 65535:
        logger.error(
            "Некорректный порт",
            extra={"context": {"port": port}},
        )
        print("Ошибка [INVALID_PORT]: Порт должен быть от 1 до 65535.", file=sys.stderr)
        return 1
    try:
        app = create_app()
    except AppError as exc:
        print(f"Ошибка [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1
    print(f"API запущен: http://{host}:{port}/api/v1/health")
    logger.info(
        "API запущен",
        extra={"context": {"host": host, "path": "/api/v1/health", "port": port}},
    )
    # create_app прячет класс Flask внутри себя, поэтому сервер запускаем через атрибут.
    run_server = getattr(app, "run")
    run_server(host=host, port=port, debug=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Разбирает аргументы и запускает CLI или API."""
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    if args.api:
        return run_api(args.host, args.port)
    return run_cli(args.file, args.text)


if __name__ == "__main__":
    sys.exit(main())
