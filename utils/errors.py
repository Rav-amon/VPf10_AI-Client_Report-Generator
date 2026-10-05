"""Единый формат ошибок приложения: код, сообщение и детали."""

from __future__ import annotations

from typing import Mapping


class AppError(Exception):
    """Ошибка с машинным кодом для CLI и HTTP API.

    Поля совпадают с контрактом ответа: code, message, details.
    """

    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        # Копия, чтобы вызывающий код не менял детали ошибки снаружи.
        self.details: dict[str, object] = dict(details or {})

    def to_dict(self) -> dict[str, object]:
        """Возвращает ошибку в виде словаря для JSON-ответа."""
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }
