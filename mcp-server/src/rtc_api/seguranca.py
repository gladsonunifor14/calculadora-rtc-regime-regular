"""Geração e verificação das chaves de acesso.

Formato da chave: ``rtc_<prefixo>_<segredo>``.

- prefixo: 12 caracteres hexadecimais, público; identifica a chave no banco e aparece nos registros.
- segredo: 32 bytes aleatórios em base64url (256 bits).

O banco guarda só o HMAC-SHA256 da chave inteira com um segredo do servidor (pepper), nunca a chave. Com
256 bits de entropia, um hash lento não é necessário; o pepper impede que um vazamento do banco permita
testar chaves sem o segredo do servidor. A chave completa só é mostrada uma vez, na emissão.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

FORMATO = re.compile(r"^rtc_([0-9a-f]{12})_([A-Za-z0-9_-]{43})$")


def gerar_chave() -> tuple[str, str]:
    """Devolve (chave completa, prefixo)."""
    prefixo = secrets.token_hex(6)
    return f"rtc_{prefixo}_{secrets.token_urlsafe(32)}", prefixo


def hash_chave(chave: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), chave.encode(), hashlib.sha256).hexdigest()


def prefixo_de(chave: str) -> str | None:
    m = FORMATO.match(chave or "")
    return m.group(1) if m else None


def confere(chave: str, hash_guardado: str, pepper: str) -> bool:
    return hmac.compare_digest(hash_chave(chave, pepper), hash_guardado)


def confere_token(recebido: str | None, esperado: str) -> bool:
    return recebido is not None and hmac.compare_digest(recebido.encode(), esperado.encode())
