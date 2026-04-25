"""Shared pytest fixtures for telegram-sub-bot tests.

Note on imports: most tests touch `app.config.settings` transitively. The
project loads .env at import time (Settings()), so .env must be present and
parseable for tests to import. CI should ship a test .env or set env vars.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def bot_mock():
    """AsyncMock standing in for aiogram.Bot. Has .id like a real bot."""
    bot = AsyncMock()
    bot.id = 12345
    return bot
