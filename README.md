# AI-Client Report Generator

Программа читает транскрибацию диалога с клиентом, просит языковую модель выделить структуру и сохраняет PDF-отчёт.

Цепочка такая:

1. Текст диалога приходит из файла, аргумента, консоли или HTTP-запроса.
2. `process_dialog_with_ai` возвращает JSON с полями клиента, темы, запроса, настроения и следующих шагов.
3. `generate_pdf` подставляет поля в HTML-шаблон Jinja2 и собирает PDF через WeasyPrint.
4. Файл ложится в `reports/` с именем вида `report_2025-11-13_15-30.pdf`.

## Структура

```
├── main.py                      # CLI и необязательный Flask API
├── templates/
│   └── report_template.html     # HTML-шаблон отчёта
├── reports/                     # готовые PDF
├── logs/                        # журнал report_generator.log
├── utils/
│   ├── ai_processor.py          # OpenAI или Claude
│   ├── pdf_generator.py         # HTML -> PDF
│   ├── logging_config.py        # JSON-журнал в консоль и файл
│   └── errors.py                # формат ошибок { code, message, details }
├── examples/
│   └── sample_dialog.txt
├── .env                         # локальные ключи, не коммитить
├── .env.example
└── requirements.txt
```

## Требования

- Python 3.10 или новее
- Ключ OpenAI или Anthropic

На этой машине WeasyPrint 70 собрал PDF без отдельной установки GTK. Если при запуске появится ошибка про отсутствующий GTK, поставьте [GTK for Windows Runtime](https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer/releases) и откройте новый терминал.

## Установка

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Откройте `.env` и вставьте ключ.

OpenAI:

```
AI_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
```

Claude:

```
AI_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-5
```

Уже заданные переменные окружения важнее значений из `.env`.

## Запуск

Файл:

```powershell
python main.py --file examples/sample_dialog.txt
```

Строка:

```powershell
python main.py --text "Клиент Анна просит перенести доставку на пятницу."
```

Ручной ввод. В конце нажмите `Ctrl+Z` и `Enter`:

```powershell
python main.py
```

Успех выглядит так:

```
Отчёт успешно создан: reports/report_2026-10-05_07-32.pdf
```

Время в имени файла — локальное. Если отчёт за ту же минуту уже есть, к имени добавляются секунды.

## Журнал

Каждый запуск пишет JSON-строки в консоль (stderr) и в `logs/report_generator.log`. Файл поворачивается после 1 МБ, хранятся три предыдущие копии.

В записи есть `level`, `message` и `context`. В `context` лежит `correlation_id`: один на запуск CLI и отдельный на каждый HTTP-запрос. Тот же идентификатор API отдаёт в заголовке `X-Request-Id`.

В журнал не попадают текст диалога, имя клиента и API-ключи. Пишутся источник, число символов, провайдер, модель, длительность и путь к PDF. Флаг `--verbose` добавляет уровень DEBUG.

## HTTP API

Флаг `--api` поднимает Flask. Это необязательный режим.

```powershell
python main.py --api
```

Проверка:

```powershell
Invoke-RestMethod http://127.0.0.1:5000/api/v1/health
```

Создать отчёт:

```powershell
$body = @{ text = "Клиент Игорь просит счёт на телефонию." } | ConvertTo-Json
$bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5000/api/v1/reports `
  -ContentType "application/json; charset=utf-8" -Body $bytes
```

Скачать PDF сразу: добавьте `?download=1`.

Файл можно отправить полем `file` (расширения `.txt`, `.md`, `.text`, до 1 МБ, UTF-8).

Ответ об успехе:

```json
{
  "data": {
    "pdf_path": "reports/report_2026-10-05_07-32.pdf",
    "report": {
      "client_name": "Игорь Соколов",
      "topic": "Корпоративная телефония",
      "main_request": "...",
      "mood": "...",
      "desired_timeline": "Запуск через три недели",
      "desired_cost": "До 180 тысяч рублей",
      "product_requirements": "Очередь звонков\nЗапись разговоров на 90 дней",
      "next_steps": "..."
    }
  },
  "error": null,
  "message": "Отчёт успешно создан"
}
```

Ответ об ошибке:

```json
{
  "data": null,
  "error": { "code": "AI_AUTH", "message": "...", "details": {} },
  "message": "..."
}
```

В заголовке `X-Request-Id` есть идентификатор запроса.

## Что возвращает модель

```json
{
  "client_name": "...",
  "topic": "...",
  "main_request": "...",
  "mood": "...",
  "desired_timeline": "...",
  "desired_cost": "...",
  "product_requirements": "...",
  "next_steps": "..."
}
```

Пустое поле заменяется на «Не указано», чтобы отчёт всё равно собрался. Временные сбои сети и ответ 429 повторяются до трёх раз с паузой 1 с, 2 с и 4 с.

## Модули

- `utils/logging_config.py` — JSON-журнал в stderr и в `logs/report_generator.log`.
- `utils/ai_processor.py` — выбор провайдера, запрос к модели, разбор JSON.
- `utils/pdf_generator.py` — контекст шаблона, рендер Jinja2, запись PDF.
- `utils/errors.py` — исключение `AppError` с полями `code`, `message`, `details`.
- `templates/report_template.html` — печатная страница A4. Текст экранируется автоматически.
