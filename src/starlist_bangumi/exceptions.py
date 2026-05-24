class AppError(Exception):
    """Base application exception with user-facing context."""

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        self.details = details or {}
        super().__init__(message)


class ConfigurationError(AppError):
    """Raised when config is missing or invalid."""


class ExternalServiceError(AppError):
    """Raised when OpenList, TMDB, or LLM services fail."""


class OperationError(AppError):
    """Raised when an organize operation cannot be completed safely."""
