import sys
import types

# messaging imports aiogram exception classes; provide tiny stubs if aiogram is not installed in audit environment.
try:
    import aiogram  # noqa
except ImportError:
    aiogram = types.ModuleType("aiogram")
    ex = types.ModuleType("aiogram.exceptions")

    class E(Exception):
        pass

    ex.TelegramForbiddenError = E
    ex.TelegramBadRequest = E

    class Retry(E):
        retry_after = 1

    ex.TelegramRetryAfter = Retry
    aiogram.Bot = object
    sys.modules["aiogram"] = aiogram
    sys.modules["aiogram.exceptions"] = ex
from app.messaging import split_text


def test_split_text_under_limit():
    parts = split_text("a\n" * 5000, 3900)
    assert all(len(x) <= 3900 for x in parts)
    assert "".join(parts) == "a\n" * 5000
