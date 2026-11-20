"""Aplicação FastAPI: autenticação por chave expirável, cotas, rotas e documentação (Swagger em /docs)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

import httpx
from fastapi import Body, Depends, FastAPI, Header, Path as PathParam, Query, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, model_validator

from rtc_mcp.client import RtcClient
from rtc_mcp.comparador import Cenario

from . import __version__
from .banco import Banco, Chave, ReferenciaDuplicada, agora, iso, periodo_de
from .config import Configuracao
from .seguranca import confere, confere_token, gerar_chave, hash_chave, prefixo_de

log = logging.getLogger("rtc_api")

DESCRICAO = """
API dos agentes e cálculos da **Reforma Tributária do Consumo** (IBS, CBS e Imposto Seletivo,
LC 214/2025), sobre as calculadoras oficiais.

## Acesso

Todas as rotas `/v1` exigem uma **chave de acesso** no cabeçalho `X-API-Key`. A chave é liberada depois
da compra, tem **data de expiração** e um **plano** que define:

- os **escopos** liberados: `calculo` (operações determinísticas, sem IA) e/ou `agentes` (agentes de IA);
- a **cota mensal** de requisições de cada escopo (mês UTC);
- o **limite por minuto**.

Use o botão **Authorize** para informar a chave e testar as rotas aqui mesmo. `GET /v1/conta` mostra o
plano, a validade e o consumo da chave.

## Cobrança de cota

Só requisições com sucesso (HTTP 2xx) consomem cota. Requisições recusadas por validação, erro da
calculadora ou falha do agente têm a cota estornada.

## Erros

Os erros seguem o formato *problem details* (RFC 9457): `type`, `title`, `status` e `detail`.

| Status | Quando |
|---|---|
| 401 | chave ausente, inválida, expirada ou revogada |
| 403 | o plano não inclui o escopo da rota |
| 413 | corpo da requisição acima do limite |
| 422 | entrada inválida |
| 429 | limite por minuto atingido (`Retry-After`) ou cota mensal esgotada |
| 502 | o agente de IA não concluiu a tarefa |
| 503 | calculadora oficial ou serviço de IA indisponível |

## Responsabilidade

Os resultados dos agentes são apoio à decisão e trazem o campo `revisaoHumanaRecomendada`. A
responsabilidade fiscal continua com o contribuinte.
"""

TAGS = [
    {"name": "Conta", "description": "Situação da chave de acesso e consumo do período."},
    {"name": "Cálculo", "description": "Operações determinísticas sobre as calculadoras oficiais, sem IA (escopo `calculo`)."},
    {"name": "Agentes", "description": "Agentes de IA que conferem o resultado nas calculadoras oficiais (escopo `agentes`). Podem levar alguns minutos."},
    {"name": "Administração", "description": "Emissão, renovação e revogação de chaves. Exige `X-Admin-Token`."},
    {"name": "Saúde", "description": "Disponibilidade da API."},
]


# ----------------------------------------------------------------------
# Erros
# ----------------------------------------------------------------------


class ErroApi(Exception):
    def __init__(self, status: int, tipo: str, titulo: str, detalhe: str, headers: dict[str, str] | None = None):
        super().__init__(detalhe)
        self.status, self.tipo, self.titulo, self.detalhe, self.headers = status, tipo, titulo, detalhe, headers or {}


def _problema(e: ErroApi) -> JSONResponse:
    return JSONResponse(
        {"type": f"https://rtc-api/erros/{e.tipo}", "title": e.titulo, "status": e.status, "detail": e.detalhe},
        status_code=e.status, headers=e.headers, media_type="application/problem+json",
    )


class Problema(BaseModel):
    type: str
    title: str
    status: int
    detail: str


RESPOSTAS_ERRO = {
    s: {"model": Problema, "description": d}
    for s, d in {401: "Chave ausente, inválida, expirada ou revogada", 403: "Escopo não incluído no plano",
                 429: "Limite por minuto ou cota mensal", 503: "Calculadora ou serviço de IA indisponível"}.items()
}


# ----------------------------------------------------------------------
# Modelos de entrada e saída
# ----------------------------------------------------------------------


class Limites(BaseModel):
    cotaCalculo: int
    cotaAgentes: int
    limitePorMinuto: int


class Conta(BaseModel):
    cliente: str
    plano: str
    escopos: list[str]
    situacao: Literal["ativa", "expirada", "revogada"]
    expiraEm: datetime
    diasRestantes: int
    limites: Limites
    periodo: str = Field(description="Mês UTC da cota (AAAA-MM)")
    usoNoPeriodo: dict[str, int]


class PedidoClassificacao(BaseModel):
    descricao: str = Field(min_length=3, max_length=2000, examples=["Arroz branco polido, pacote de 5 kg"])
    data: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$", examples=["2027-03-10"], description="Data do fato gerador")
    ncm: str | None = Field(None, pattern=r"^\d{8}$")
    nbs: str | None = Field(None, pattern=r"^\d{9}$")
    contexto: str | None = Field(None, max_length=2000, examples=["Venda de supermercado a consumidor final"])


class PedidoComparador(BaseModel):
    pedido: str = Field(min_length=10, max_length=5000, examples=[
        "Mercadinho em Porto Alegre, Simples anexo I. Vendo R$ 30 mil/mês em arroz e feijão e R$ 20 mil em "
        "limpeza, a consumidor final. PGDAS: 1,25% de IBS e 2,75% de CBS."])


class PedidoAuditor(BaseModel):
    xml: str = Field(min_length=20, description="XML da NF-e ou NFC-e")
    contexto: str | None = Field(None, max_length=2000)


class PedidoCorretor(BaseModel):
    operacao: dict[str, Any] = Field(description="Operação no formato de entrada da calculadora (regime regular)")
    contexto: str | None = Field(None, max_length=2000)


class PedidoConsultor(BaseModel):
    pergunta: str = Field(min_length=5, max_length=2000, examples=["Serviços de educação têm redução de IBS e CBS?"])
    data: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="Data de referência; padrão: hoje")


class PedidoEmissao(BaseModel):
    cliente: str = Field(min_length=2, max_length=200, description="Identificação do comprador (ex.: CNPJ ou id no seu sistema)")
    referenciaCompra: str = Field(min_length=1, max_length=200, description="Id da compra; a emissão é idempotente por referência")
    plano: str = Field(examples=["completo"])
    validadeDias: int | None = Field(None, ge=1, le=1095)
    expiraEm: datetime | None = None
    limites: Limites | None = Field(None, description="Sobrescreve os limites do plano só para esta chave")

    @model_validator(mode="after")
    def _validade(self) -> "PedidoEmissao":
        if (self.validadeDias is None) == (self.expiraEm is None):
            raise ValueError("informe exatamente um entre validadeDias e expiraEm")
        return self


class ChaveAdmin(BaseModel):
    id: str
    prefixo: str
    cliente: str
    referenciaCompra: str
    plano: str
    escopos: list[str]
    limites: Limites
    criadaEm: datetime
    expiraEm: datetime
    situacao: Literal["ativa", "expirada", "revogada"]
    revogadaEm: datetime | None
    motivoRevogacao: str | None


class ChaveEmitida(ChaveAdmin):
    chave: str = Field(description="A chave completa. É mostrada só agora: guarde e entregue ao cliente.")


class DetalheChave(ChaveAdmin):
    usoNoPeriodo: dict[str, int]
    ultimasRequisicoes: list[dict[str, Any]]


class PedidoRenovacao(BaseModel):
    dias: int = Field(ge=1, le=1095, description="Dias somados à expiração atual (ou a partir de agora, se já expirou)")


class PedidoRevogacao(BaseModel):
    motivo: str = Field(min_length=3, max_length=500)


def _admin(chave: Chave, momento: datetime) -> dict[str, Any]:
    return {
        "id": chave.id, "prefixo": chave.prefixo, "cliente": chave.cliente, "referenciaCompra": chave.referencia_compra,
        "plano": chave.plano, "escopos": chave.escopos,
        "limites": {"cotaCalculo": chave.cota_calculo, "cotaAgentes": chave.cota_agentes, "limitePorMinuto": chave.limite_por_minuto},
        "criadaEm": chave.criada_em, "expiraEm": chave.expira_em, "situacao": chave.situacao(momento),
        "revogadaEm": chave.revogada_em, "motivoRevogacao": chave.motivo_revogacao,
    }


# ----------------------------------------------------------------------
# Execução das operações (substituível nos testes)
# ----------------------------------------------------------------------


def executores_padrao() -> dict[str, Callable[..., Awaitable[Any]]]:
    from rtc_mcp.agente_auditor import auditar_nota
    from rtc_mcp.agente_comparador import comparar_regimes
    from rtc_mcp.agente_consultor import consultar
    from rtc_mcp.agente_corretor import corrigir
    from rtc_mcp.classificador import classificar

    return {"classificador": classificar, "comparador": comparar_regimes, "auditor": auditar_nota,
            "corretor": corrigir, "consultor": consultar}


def _causa_transporte(exc: BaseException) -> bool:
    atual: BaseException | None = exc
    while atual is not None:
        if isinstance(atual, httpx.TransportError):
            return True
        atual = atual.__cause__ or atual.__context__
    return False


def _traduzir(exc: Exception) -> ErroApi:
    """Converte exceções das operações em respostas HTTP, sem expor detalhes internos em erros 5xx."""
    import anthropic

    from rtc_mcp.agente_comparador import ErroAgente
    from rtc_mcp.auditoria import ErroAuditoria
    from rtc_mcp.comparador import ErroComparacao
    from rtc_mcp.correcao import ErroCorrecao

    if _causa_transporte(exc):
        return ErroApi(503, "calculadora-indisponivel", "Calculadora indisponível",
                       "A calculadora oficial não respondeu. Tente novamente em instantes.")
    if isinstance(exc, (ErroComparacao, ErroAuditoria, ErroCorrecao)):
        return ErroApi(422, "entrada-invalida", "Entrada inválida", str(exc))
    if isinstance(exc, anthropic.RateLimitError):
        return ErroApi(503, "ia-sobrecarregada", "Serviço de IA sobrecarregado", "Tente novamente em instantes.",
                       {"Retry-After": "30"})
    if isinstance(exc, (anthropic.APIStatusError, anthropic.APIConnectionError)):
        log.error("Falha na API da Anthropic: %r", exc)
        return ErroApi(503, "ia-indisponivel", "Serviço de IA indisponível", "Tente novamente em instantes.")
    if isinstance(exc, ErroAgente):
        return ErroApi(502, "agente-nao-concluiu", "O agente não concluiu", str(exc))
    log.exception("Erro inesperado")
    return ErroApi(500, "erro-interno", "Erro interno", "Erro inesperado no servidor.")


# ----------------------------------------------------------------------
# Limite de tamanho do corpo (ASGI)
# ----------------------------------------------------------------------


class LimiteCorpo:
    """Recusa com 413 corpos acima do limite, com ou sem Content-Length."""

    def __init__(self, app: Any, maximo: int):
        self.app, self.maximo = app, maximo

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        tamanho = dict(scope.get("headers") or []).get(b"content-length")
        if tamanho is not None and tamanho.isdigit() and int(tamanho) > self.maximo:
            return await _problema(ErroApi(413, "corpo-grande", "Corpo grande demais",
                                           f"O limite é {self.maximo} bytes."))(scope, receive, send)
        recebidos = 0

        async def receber():
            nonlocal recebidos
            mensagem = await receive()
            if mensagem["type"] == "http.request":
                recebidos += len(mensagem.get("body", b""))
                if recebidos > self.maximo:
                    raise ErroApi(413, "corpo-grande", "Corpo grande demais", f"O limite é {self.maximo} bytes.")
            return mensagem

        await self.app(scope, receber, send)


# ----------------------------------------------------------------------
# Aplicação
# ----------------------------------------------------------------------


def criar_app(
    config: Configuracao | None = None,
    *,
    executores: dict[str, Callable[..., Awaitable[Any]]] | None = None,
    cliente_rtc: RtcClient | None = None,
    relogio: Callable[[], datetime] = agora,
) -> FastAPI:
    config = config or Configuracao.from_env()
    banco = Banco(config.banco)
    semaforo_agentes = asyncio.Semaphore(config.agentes_simultaneos)
    executores = executores or {}

    @asynccontextmanager
    async def ciclo(app: FastAPI):
        app.state.rtc = cliente_rtc or RtcClient()
        yield
        if cliente_rtc is None:
            await app.state.rtc.aclose()

    app = FastAPI(
        title="API RTC: agentes e cálculos da Reforma Tributária",
        version=__version__,
        description=DESCRICAO,
        openapi_tags=TAGS,
        lifespan=ciclo,
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.add_middleware(LimiteCorpo, maximo=config.tamanho_maximo_bytes)

    @app.exception_handler(ErroApi)
    async def _tratar(_: Request, exc: ErroApi):
        return _problema(exc)

    def executor(nome: str) -> Callable[..., Awaitable[Any]]:
        if nome not in executores:
            executores.update({k: v for k, v in executores_padrao().items() if k not in executores})
        return executores[nome]

    # ------------------------------------------------------------ autenticação

    esquema_chave = APIKeyHeader(name="X-API-Key", auto_error=False, scheme_name="ChaveDeAcesso",
                                 description="Chave de acesso liberada após a compra (formato `rtc_<prefixo>_<segredo>`).")
    esquema_admin = APIKeyHeader(name="X-Admin-Token", auto_error=False, scheme_name="TokenAdministrativo",
                                 description="Token do administrador (variável RTC_API_ADMIN_TOKEN).")

    def autenticar(valor: str | None = Security(esquema_chave)) -> Chave:
        nao_autorizado = ErroApi(401, "chave-invalida", "Chave de acesso inválida",
                                 "Informe uma chave de acesso válida no cabeçalho X-API-Key.")
        prefixo = prefixo_de(valor or "")
        if prefixo is None:
            raise nao_autorizado
        chave = banco.por_prefixo(prefixo)
        if chave is None or not confere(valor, chave.hash, config.pepper):
            raise nao_autorizado
        situacao = chave.situacao(relogio())
        if situacao == "revogada":
            raise ErroApi(401, "chave-revogada", "Chave de acesso revogada", "Esta chave foi revogada.")
        if situacao == "expirada":
            raise ErroApi(401, "chave-expirada", "Chave de acesso expirada",
                          f"A chave expirou em {iso(chave.expira_em)}. Renove a assinatura para continuar.")
        return chave

    def administrador(valor: str | None = Security(esquema_admin)) -> None:
        if not confere_token(valor, config.token_admin):
            raise ErroApi(401, "admin-invalido", "Token administrativo inválido", "Informe o X-Admin-Token.")

    async def executar(request: Request, chave: Chave, escopo: str, operacao: Callable[[], Awaitable[Any]]) -> Any:
        if escopo not in chave.escopos:
            raise ErroApi(403, "escopo-nao-incluido", "Escopo não incluído no plano",
                          f"O plano {chave.plano!r} não inclui o escopo {escopo!r}.")
        momento = relogio()
        situacao = banco.consumir(chave, escopo, momento)
        if situacao == "limite_minuto":
            espera = 60 - momento.second
            raise ErroApi(429, "limite-por-minuto", "Limite por minuto atingido",
                          f"O plano permite {chave.limite_por_minuto} requisições por minuto.", {"Retry-After": str(espera)})
        if situacao == "cota_esgotada":
            raise ErroApi(429, "cota-esgotada", "Cota mensal esgotada",
                          f"A cota de {chave.cota(escopo)} requisições de {escopo} em {periodo_de(momento)} foi usada.")
        inicio, status = time.monotonic(), 500
        try:
            resultado = await operacao()
            status = 200
            return resultado
        except ErroApi as exc:
            status = exc.status
            raise
        except Exception as exc:  # noqa: BLE001 - traduzido para HTTP
            erro = _traduzir(exc)
            status = erro.status
            raise erro from exc
        finally:
            if status >= 300:
                banco.estornar(chave, escopo, momento)
            banco.registrar(chave.id, request.url.path, status, int((time.monotonic() - inicio) * 1000))

    async def agente(request: Request, chave: Chave, operacao: Callable[[], Awaitable[Any]]) -> Any:
        async def limitado():
            async with semaforo_agentes:
                return await operacao()

        return await executar(request, chave, "agentes", limitado)

    # ------------------------------------------------------------ conta

    @app.get("/v1/conta", tags=["Conta"], response_model=Conta, responses={401: RESPOSTAS_ERRO[401]},
             summary="Situação e consumo da chave")
    def conta(chave: Chave = Depends(autenticar)):
        momento = relogio()
        periodo = periodo_de(momento)
        return Conta(
            cliente=chave.cliente, plano=chave.plano, escopos=chave.escopos, situacao=chave.situacao(momento),
            expiraEm=chave.expira_em, diasRestantes=max(0, math.ceil((chave.expira_em - momento).total_seconds() / 86400)),
            limites=Limites(cotaCalculo=chave.cota_calculo, cotaAgentes=chave.cota_agentes,
                            limitePorMinuto=chave.limite_por_minuto),
            periodo=periodo, usoNoPeriodo=banco.uso(chave.id, periodo),
        )

    # ------------------------------------------------------------ cálculo

    @app.post("/v1/calculo/comparacao-regimes", tags=["Cálculo"], responses=RESPOSTAS_ERRO,
              summary="Compara IBS/CBS no Simples Nacional e no regime regular")
    async def comparacao_regimes(request: Request, cenario: Cenario, chave: Chave = Depends(autenticar)) -> dict[str, Any]:
        """Roda as duas calculadoras oficiais sobre o cenário e compara o IBS/CBS devido, os créditos das compras
        e o crédito do cliente contribuinte. Só IBS/CBS; vale a partir de 2027."""
        from rtc_mcp.comparador import comparar

        return await executar(request, chave, "calculo", lambda: comparar(request.app.state.rtc, cenario))

    @app.post("/v1/calculo/auditoria-nfe", tags=["Cálculo"], responses=RESPOSTAS_ERRO,
              summary="Audita o IBS/CBS de uma NF-e ou NFC-e",
              openapi_extra={"requestBody": {"required": True, "content": {"application/xml": {"schema": {"type": "string"}}}}})
    async def auditoria_nfe(request: Request, chave: Chave = Depends(autenticar)) -> dict[str, Any]:
        """Envie o XML da nota no corpo (Content-Type: application/xml). Confere XSD, classificação, base de cálculo,
        reduções, valores e totais do grupo IBSCBS contra as calculadoras oficiais."""
        from rtc_mcp.auditoria import auditar

        corpo = (await request.body()).decode("utf-8", errors="replace")
        return await executar(request, chave, "calculo", lambda: auditar(request.app.state.rtc, corpo))

    @app.post("/v1/calculo/diagnostico-operacao", tags=["Cálculo"], responses=RESPOSTAS_ERRO,
              summary="Diagnostica uma operação do regime regular")
    async def diagnostico_operacao(
        request: Request,
        operacao: dict[str, Any] = Body(description="Operação no formato de entrada da calculadora (regime regular)"),
        chave: Chave = Depends(autenticar),
    ) -> dict[str, Any]:
        """Devolve os erros estruturados da operação (por item ou da operação inteira), com a descrição de cada código."""
        from rtc_mcp.correcao import diagnosticar

        return await executar(request, chave, "calculo", lambda: diagnosticar(request.app.state.rtc, operacao))

    # ------------------------------------------------------------ agentes

    @app.post("/v1/agentes/classificador", tags=["Agentes"], responses=RESPOSTAS_ERRO, summary="Classifica um item")
    async def agente_classificador(request: Request, pedido: PedidoClassificacao, chave: Chave = Depends(autenticar)) -> dict[str, Any]:
        """Propõe NCM/NBS, CST e cClassTrib de CBS/IBS com a fundamentação legal e confere na calculadora."""
        r = await agente(request, chave, lambda: executor("classificador")(
            pedido.descricao, data=pedido.data, ncm=pedido.ncm, nbs=pedido.nbs, contexto=pedido.contexto))
        return r.model_dump(mode="json")

    @app.post("/v1/agentes/comparador", tags=["Agentes"], responses=RESPOSTAS_ERRO, summary="Compara regimes")
    async def agente_comparador(request: Request, pedido: PedidoComparador, chave: Chave = Depends(autenticar)) -> dict[str, Any]:
        """A partir da descrição da empresa, compara o IBS/CBS no Simples e no regime regular e escreve um parecer."""
        r = await agente(request, chave, lambda: executor("comparador")(pedido.pedido))
        return r.model_dump(mode="json")

    @app.post("/v1/agentes/auditor", tags=["Agentes"], responses=RESPOSTAS_ERRO, summary="Audita uma NF-e")
    async def agente_auditor(request: Request, pedido: PedidoAuditor, chave: Chave = Depends(autenticar)) -> dict[str, Any]:
        """Audita o grupo IBSCBS da nota e explica cada divergência, com impacto e correção."""

        async def rodar():
            with tempfile.TemporaryDirectory(prefix="rtc-api-") as pasta:
                arquivo = Path(pasta) / "nota.xml"
                arquivo.write_text(pedido.xml, encoding="utf-8")
                return await executor("auditor")(arquivo, contexto=pedido.contexto)

        r = await agente(request, chave, rodar)
        return r.model_dump(mode="json")

    @app.post("/v1/agentes/corretor", tags=["Agentes"], responses=RESPOSTAS_ERRO, summary="Corrige uma operação")
    async def agente_corretor(request: Request, pedido: PedidoCorretor, chave: Chave = Depends(autenticar)) -> dict[str, Any]:
        """Corrige uma operação rejeitada pela calculadora; a correção é conferida campo a campo."""
        r = await agente(request, chave, lambda: executor("corretor")(pedido.operacao, contexto=pedido.contexto))
        return r.model_dump(mode="json")

    @app.post("/v1/agentes/consultor", tags=["Agentes"], responses=RESPOSTAS_ERRO, summary="Consulta jurídica")
    async def agente_consultor(request: Request, pedido: PedidoConsultor, chave: Chave = Depends(autenticar)) -> dict[str, Any]:
        """Responde sobre IBS/CBS citando a fundamentação legal oficial; as citações são conferidas."""
        r = await agente(request, chave, lambda: executor("consultor")(pedido.pergunta, data=pedido.data))
        return r.model_dump(mode="json")

    # ------------------------------------------------------------ administração

    docs = config.docs_admin

    @app.get("/admin/planos", tags=["Administração"], include_in_schema=docs, dependencies=[Depends(administrador)])
    def planos() -> dict[str, Any]:
        return config.planos

    @app.post("/admin/chaves", tags=["Administração"], include_in_schema=docs, status_code=201,
              response_model=ChaveEmitida, dependencies=[Depends(administrador)],
              responses={409: {"model": Problema, "description": "Já existe chave para a referência de compra"}})
    def emitir(pedido: PedidoEmissao):
        """Emite uma chave depois da compra confirmada. A chave completa só aparece nesta resposta."""
        if pedido.plano not in config.planos:
            raise ErroApi(422, "plano-desconhecido", "Plano desconhecido", f"Planos: {', '.join(config.planos)}.")
        momento = relogio()
        expira = pedido.expiraEm or (momento + timedelta(days=pedido.validadeDias))
        if expira.tzinfo is None:
            expira = expira.replace(tzinfo=timezone.utc)
        if expira <= momento:
            raise ErroApi(422, "validade-invalida", "Validade inválida", "A expiração precisa estar no futuro.")
        plano = config.planos[pedido.plano]
        limites = pedido.limites or Limites(cotaCalculo=plano["cotaCalculo"], cotaAgentes=plano["cotaAgentes"],
                                            limitePorMinuto=plano["limitePorMinuto"])
        valor, prefixo = gerar_chave()
        try:
            chave = banco.emitir(
                prefixo=prefixo, hash_=hash_chave(valor, config.pepper), cliente=pedido.cliente,
                referencia_compra=pedido.referenciaCompra, plano=pedido.plano, escopos=list(plano["escopos"]),
                cota_calculo=limites.cotaCalculo, cota_agentes=limites.cotaAgentes,
                limite_por_minuto=limites.limitePorMinuto, expira_em=expira,
            )
        except ReferenciaDuplicada as dup:
            raise ErroApi(409, "referencia-duplicada", "Compra já tem chave",
                          f"A referência {pedido.referenciaCompra!r} já gerou a chave {dup.chave.id} "
                          f"(prefixo {dup.chave.prefixo}). Use renovar em vez de emitir outra.") from None
        log.info("Chave emitida: id=%s prefixo=%s plano=%s", chave.id, chave.prefixo, chave.plano)
        return ChaveEmitida(chave=valor, **_admin(chave, momento))

    @app.get("/admin/chaves", tags=["Administração"], include_in_schema=docs, response_model=list[ChaveAdmin],
             dependencies=[Depends(administrador)])
    def listar(cliente: str | None = None, limite: int = Query(50, ge=1, le=500), deslocamento: int = Query(0, ge=0)):
        momento = relogio()
        return [_admin(c, momento) for c in banco.listar(cliente=cliente, limite=limite, deslocamento=deslocamento)]

    def _obter(id_: str) -> Chave:
        chave = banco.obter(id_)
        if chave is None:
            raise ErroApi(404, "chave-nao-encontrada", "Chave não encontrada", f"Não existe chave com id {id_}.")
        return chave

    @app.get("/admin/chaves/{id}", tags=["Administração"], include_in_schema=docs, response_model=DetalheChave,
             dependencies=[Depends(administrador)])
    def detalhar(id: str = PathParam()):
        chave, momento = _obter(id), relogio()
        return {**_admin(chave, momento), "usoNoPeriodo": banco.uso(chave.id, periodo_de(momento)),
                "ultimasRequisicoes": banco.requisicoes(chave.id, 50)}

    @app.post("/admin/chaves/{id}/renovar", tags=["Administração"], include_in_schema=docs, response_model=ChaveAdmin,
              dependencies=[Depends(administrador)])
    def renovar(pedido: PedidoRenovacao, id: str = PathParam()):
        """Soma dias à expiração (a partir de agora, se a chave já expirou). Chaves revogadas não são renovadas."""
        chave, momento = _obter(id), relogio()
        if chave.revogada_em is not None:
            raise ErroApi(409, "chave-revogada", "Chave revogada", "Chaves revogadas não podem ser renovadas.")
        nova = max(chave.expira_em, momento) + timedelta(days=pedido.dias)
        return _admin(banco.renovar(id, nova), momento)

    @app.post("/admin/chaves/{id}/revogar", tags=["Administração"], include_in_schema=docs, response_model=ChaveAdmin,
              dependencies=[Depends(administrador)])
    def revogar(pedido: PedidoRevogacao, id: str = PathParam()):
        """Revoga a chave imediatamente (reembolso, fraude, vazamento). Não pode ser desfeito."""
        _obter(id)
        chave = banco.revogar(id, pedido.motivo)
        log.info("Chave revogada: id=%s", id)
        return _admin(chave, relogio())

    # ------------------------------------------------------------ saúde

    @app.get("/saude", tags=["Saúde"], summary="Disponibilidade")
    async def saude(request: Request) -> dict[str, Any]:
        calculadoras = {}
        for nome, chamada, caminho in (("regimeGeral", request.app.state.rtc.regime_geral, "calculadora/dados-abertos/versao"),
                                       ("simplesNacional", request.app.state.rtc.simples_nacional, "versao")):
            try:
                await chamada("GET", caminho)
                calculadoras[nome] = "ok"
            except Exception:  # noqa: BLE001
                calculadoras[nome] = "indisponivel"
        return {"status": "ok", "versao": __version__, "calculadoras": calculadoras}

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Servidor HTTP da API RTC (documentação em /docs)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(criar_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
