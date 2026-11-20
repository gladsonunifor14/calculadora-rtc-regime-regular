"""Cliente HTTP das duas calculadoras (regime regular e Simples Nacional).

Toda resposta bem-sucedida é devolvida como ``{"dados": ..., "metadados": {...}}``.
Os metadados carregam a versão da aplicação e da base de dados (cabeçalhos
``X-CALC-APP-VERSION`` e ``X-CALC-DB-VERSION``) e o aviso de dados simulados
(``x-warning-dados-simulados``), para que o agente possa citar de onde veio
cada número.

Falhas viram ``CalculadoraError`` com uma mensagem em português pensada para
ser lida pelo modelo: o corpo de erro da API (problem+json) é repassado, pois
ele aponta o campo inválido e permite que o agente corrija a entrada.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx

REGIME_GERAL_URL_PADRAO = "http://localhost:8080/api"
SIMPLES_NACIONAL_URL_PADRAO = "http://localhost:8082/api"
TIMEOUT_PADRAO = 30.0
LIMITE_CORPO_ERRO = 4000


class CalculadoraError(Exception):
    """Falha prevista ao consultar uma calculadora; a mensagem vai para o modelo."""


@dataclass(frozen=True)
class Config:
    regime_geral_url: str
    simples_nacional_url: str
    timeout: float

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            regime_geral_url=os.environ.get("RTC_REGIME_GERAL_URL", REGIME_GERAL_URL_PADRAO).rstrip("/"),
            simples_nacional_url=os.environ.get("RTC_SIMPLES_NACIONAL_URL", SIMPLES_NACIONAL_URL_PADRAO).rstrip("/"),
            timeout=float(os.environ.get("RTC_TIMEOUT", TIMEOUT_PADRAO)),
        )


def _sem_nulos(params: dict[str, Any] | None) -> dict[str, Any] | None:
    if params is None:
        return None
    return {k: v for k, v in params.items() if v is not None}


# Valores do cabeçalho x-warning-dados-simulados no cálculo (enum TipoWarningDadosSimulados).
AVISOS_CALCULO = {
    "1": "cálculo com alíquotas simuladas",
    "2": "cálculo simulado: regras de compras governamentais ainda não definidas",
    "3": "cálculo simulado: alíquotas do Imposto Seletivo ainda não definidas em lei",
    "4": "cálculo simulado: compras governamentais e Imposto Seletivo ainda não definidos",
    "5": "cálculo com alíquotas fictícias (CST 010, 220 ou 221)",
    "6": "fato gerador após 2026: cálculo simulado com alíquotas ainda não definitivas",
}
# Nos dados abertos o cabeçalho tem outro significado.
AVISOS_DADOS_ABERTOS = {"1": "alíquotas do Imposto Seletivo ainda não definidas em lei"}


def _metadados(resposta: httpx.Response) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    if v := resposta.headers.get("X-CALC-APP-VERSION"):
        meta["versaoAplicacao"] = v
    if v := resposta.headers.get("X-CALC-DB-VERSION"):
        meta["versaoBaseDados"] = v
    if v := resposta.headers.get("x-warning-dados-simulados"):
        avisos = AVISOS_DADOS_ABERTOS if "/dados-abertos/" in resposta.request.url.path else AVISOS_CALCULO
        descricao = avisos.get(v.strip(), "dados simulados")
        meta["avisoDadosSimulados"] = (
            f"{descricao} (código {v}). Não apresente estes valores como definitivos."
        )
    return meta


def _corpo(resposta: httpx.Response) -> Any:
    if not resposta.content:
        return None
    tipo = resposta.headers.get("content-type", "")
    if "json" in tipo:
        return resposta.json()
    texto = resposta.text
    try:
        return json.loads(texto)
    except ValueError:
        return texto


class RtcClient:
    """Acesso às APIs REST das calculadoras."""

    def __init__(self, config: Config | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config or Config.from_env()
        self._http = httpx.AsyncClient(timeout=self.config.timeout, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def regime_geral(self, metodo: str, caminho: str, **kwargs: Any) -> dict[str, Any]:
        return await self._requisitar(self.config.regime_geral_url, "regime regular", metodo, caminho, **kwargs)

    async def simples_nacional(self, metodo: str, caminho: str, **kwargs: Any) -> dict[str, Any]:
        return await self._requisitar(
            self.config.simples_nacional_url, "Simples Nacional", metodo, caminho, **kwargs
        )

    async def _requisitar(
        self,
        base: str,
        nome: str,
        metodo: str,
        caminho: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        conteudo: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{base}/{caminho.lstrip('/')}"
        try:
            resposta = await self._http.request(
                metodo,
                url,
                params=_sem_nulos(params),
                json=json_body,
                content=conteudo,
                headers=headers,
            )
        except httpx.TimeoutException as exc:
            raise CalculadoraError(
                f"A calculadora do {nome} não respondeu em {self.config.timeout:.0f}s ({metodo} {url})."
            ) from exc
        except httpx.TransportError as exc:
            raise CalculadoraError(
                f"Não foi possível conectar à calculadora do {nome} em {base}. "
                "Verifique se a aplicação está em execução (perfil offline) e se a URL configurada "
                "está correta."
            ) from exc

        if resposta.status_code == 404:
            raise CalculadoraError(f"Não encontrado (HTTP 404): {metodo} {caminho}. {_resumo_erro(resposta)}")
        if resposta.is_error:
            raise CalculadoraError(
                f"A calculadora do {nome} recusou a requisição (HTTP {resposta.status_code}) "
                f"em {metodo} {caminho}: {_resumo_erro(resposta)}"
            )
        return {"dados": _corpo(resposta), "metadados": _metadados(resposta)}


def _resumo_erro(resposta: httpx.Response) -> str:
    corpo = _corpo(resposta)
    if corpo is None:
        return "sem corpo de resposta"
    texto = corpo if isinstance(corpo, str) else json.dumps(corpo, ensure_ascii=False)
    if len(texto) > LIMITE_CORPO_ERRO:
        texto = texto[:LIMITE_CORPO_ERRO] + "... (truncado)"
    return texto
