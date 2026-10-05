# utils

Вспомогательный пакет генератора отчётов. Запуск всегда идёт из корня проекта через `python main.py`.

## Модули

- `logging_config.py` — настройка журнала и маскировка ключей API.
- `ai_processor.py` — `process_dialog_with_ai(text)` отправляет транскрибацию в OpenAI или Claude и возвращает пять строковых полей.
- `pdf_generator.py` — `generate_pdf(report_data)` рендерит `templates/report_template.html` и пишет PDF в `reports/`.
- `errors.py` — `AppError` с единым видом `{ code, message, details }`.

Текст диалога и API-ключи в журнал не попадают. Повторы сетевых сбоев живут в `ai_processor.py`.
