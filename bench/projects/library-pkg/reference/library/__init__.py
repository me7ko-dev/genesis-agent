"""Еталонно решение — доказва, че скритите тестове се минават. Genesis не го вижда."""
from .errors import LimitReached, NotAvailable, NotFound
from .service import Library

__all__ = ["Library", "LimitReached", "NotAvailable", "NotFound"]
