"""Пакет вспомогательных модулей генератора клиентских отчётов."""

# Импортируйте функции из подмодулей напрямую:
# process_dialog_with_ai — из utils.ai_processor
# generate_pdf — из utils.pdf_generator
# Так CLI не загружает WeasyPrint, пока отчёт ещё не собран.
