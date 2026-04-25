"""encode_payload / decode_payload — order_id для платёжных провайдеров.

Heleket валидирует order_id по правилу alpha_dash ([A-Za-z0-9_-]).
Двоеточие запрещено — поэтому разделитель сменён с `:` на `_`.
"""
from __future__ import annotations

from app.payments.cryptopay import decode_payload, encode_payload


def test_encode_payload_uses_underscore_separator():
    assert encode_payload(123, "tariff_3d") == "123_tariff_3d"


def test_encode_payload_real_world_example():
    """Точная репродукция кейса из прод-бага: tg=869598289, plan=tariff_3d."""
    assert encode_payload(869598289, "tariff_3d") == "869598289_tariff_3d"


def test_encode_payload_passes_heleket_alpha_dash():
    """alpha_dash = [A-Za-z0-9_-]. Проверяем символьный состав."""
    out = encode_payload(869598289, "tariff_30d")
    import re
    assert re.fullmatch(r"[A-Za-z0-9_-]+", out), f"alpha_dash violated: {out!r}"


def test_decode_payload_with_new_underscore_format():
    assert decode_payload("123_tariff_3d") == (123, "tariff_3d")


def test_decode_payload_with_legacy_colon_format():
    """Backward compat: записи в БД могли быть созданы до фикса."""
    assert decode_payload("123:tariff_3d") == (123, "tariff_3d")


def test_decode_payload_handles_all_three_plans():
    for plan in ("tariff_3d", "tariff_7d", "tariff_30d"):
        s = encode_payload(42, plan)
        assert decode_payload(s) == (42, plan)


def test_encode_decode_roundtrip():
    tg, plan = 869598289, "tariff_3d"
    assert decode_payload(encode_payload(tg, plan)) == (tg, plan)
