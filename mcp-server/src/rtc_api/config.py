"""Configuração da API, lida das variáveis de ambiente.

A API se recusa a iniciar sem os segredos obrigatórios, em vez de subir com um valor padrão inseguro.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ESCOPOS = ("calculo", "agentes")

# Planos padrão. Podem ser substituídos por um JSON em RTC_API_PLANOS (mesmo formato).
PLANOS_PADRAO: dict[str, dict[str, Any]] = {
    "calculo": {
        "descricao": "Operações determinísticas (comparação de regimes, auditoria de NF-e, diagnóstico)",
        "escopos": ["calculo"],
        "cotaCalculo": 5000,
        "cotaAgentes": 0,
        "limitePorMinuto": 60,
    },
    "completo": {
        "descricao": "Operações determinísticas e os cinco agentes de IA",
        "escopos": ["calculo", "agentes"],
        "cotaCalculo": 5000,
        "cotaAgentes": 300,
        "limitePorMinuto": 30,
    },
}


class ErroConfiguracao(Exception):
    """Configuração ausente ou inválida; a API não deve iniciar."""


@dataclass(frozen=True)
class Configuracao:
    token_admin: str
    pepper: str
    banco: Path
    planos: dict[str, dict[str, Any]] = field(default_factory=lambda: dict(PLANOS_PADRAO))
    agentes_simultaneos: int = 4
    tamanho_maximo_bytes: int = 5 * 1024 * 1024
    docs_admin: bool = False

    @classmethod
    def from_env(cls) -> "Configuracao":
        token = os.environ.get("RTC_API_ADMIN_TOKEN", "")
        pepper = os.environ.get("RTC_API_PEPPER", "")
        if len(token) < 32:
            raise ErroConfiguracao("Defina RTC_API_ADMIN_TOKEN com pelo menos 32 caracteres aleatórios.")
        if len(pepper) < 32:
            raise ErroConfiguracao("Defina RTC_API_PEPPER com pelo menos 32 caracteres aleatórios.")
        planos = dict(PLANOS_PADRAO)
        if caminho := os.environ.get("RTC_API_PLANOS"):
            try:
                planos = json.loads(Path(caminho).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ErroConfiguracao(f"Não foi possível ler os planos em {caminho}: {exc}") from exc
        validar_planos(planos)
        return cls(
            token_admin=token,
            pepper=pepper,
            banco=Path(os.environ.get("RTC_API_BANCO", "rtc-api.db")),
            planos=planos,
            agentes_simultaneos=int(os.environ.get("RTC_API_AGENTES_SIMULTANEOS", "4")),
            docs_admin=os.environ.get("RTC_API_DOCS_ADMIN", "").lower() in ("1", "true", "sim"),
        )


def validar_planos(planos: dict[str, dict[str, Any]]) -> None:
    if not planos:
        raise ErroConfiguracao("Nenhum plano configurado.")
    for nome, p in planos.items():
        faltando = {"escopos", "cotaCalculo", "cotaAgentes", "limitePorMinuto"} - set(p)
        if faltando:
            raise ErroConfiguracao(f"Plano {nome!r} sem os campos {sorted(faltando)}.")
        invalidos = set(p["escopos"]) - set(ESCOPOS)
        if invalidos:
            raise ErroConfiguracao(f"Plano {nome!r} com escopos desconhecidos: {sorted(invalidos)}.")
        for campo in ("cotaCalculo", "cotaAgentes", "limitePorMinuto"):
            if not isinstance(p[campo], int) or p[campo] < 0:
                raise ErroConfiguracao(f"Plano {nome!r}: {campo} deve ser um inteiro >= 0.")
