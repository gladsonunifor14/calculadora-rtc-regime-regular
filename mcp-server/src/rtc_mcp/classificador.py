"""Agente classificador fiscal da Reforma Tributária do Consumo.

Recebe a descrição de um item (mercadoria ou serviço) e o contexto da operação e
propõe NCM/NBS, CST e cClassTrib de CBS/IBS, com a fundamentação legal.

O agente usa o Claude (Messages API, tool runner) com as ferramentas do servidor
MCP rtc-mcp, que consultam as tabelas oficiais da calculadora. Ele não inventa
códigos: depois da resposta do modelo, o código confere na calculadora se a
cClassTrib existe, se pertence ao CST e se aceita a NCM/NBS proposta. Qualquer
falha nessa conferência, ou confiança abaixo de "alta", marca o resultado para
revisão humana.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

MODELO_PADRAO = "claude-opus-5-5"

# Ferramentas do rtc-mcp que o classificador pode usar. As de cálculo, XML e DANFSe ficam de
# fora: um conjunto pequeno e focado ajuda o modelo a escolher.
FERRAMENTAS = (
    "consultar_ncm",
    "consultar_nbs",
    "listar_classificacoes_aplicaveis_ncm",
    "listar_classificacoes_por_nbs",
    "verificar_ncm_aplicavel",
    "verificar_nbs_aplicavel",
    "consultar_classificacao_tributaria",
    "listar_classificacoes_por_cst",
    "listar_situacoes_tributarias",
    "consultar_fundamentacao_legal",
)

SISTEMA = """\
Você é um especialista em classificação fiscal da Reforma Tributária do Consumo brasileira
(EC 132/2023 e LC 214/2025). Sua tarefa é classificar um item de uma operação para fins de CBS e
IBS: identificar a NCM (mercadoria) ou a NBS (serviço), o CST e a cClassTrib, e justificar com a
fundamentação legal.

As ferramentas consultam as tabelas oficiais da calculadora da Receita Federal. Elas são a única
fonte aceitável de códigos. Nunca proponha um código que você não tenha visto numa resposta de
ferramenta.

Método:
1. Entenda o item e a operação. Se o usuário informou a NCM ou a NBS, confirme-a com consultar_ncm
   ou consultar_nbs e verifique se a descrição oficial corresponde ao item. Se não informou, proponha
   o código mais provável a partir do seu conhecimento da nomenclatura e confirme-o na ferramenta;
   se a descrição oficial não bater, tente outro código.
2. Levante as classificações candidatas:
   - mercadoria: listar_classificacoes_aplicaveis_ncm. O grupo "doAnexo" traz os tratamentos
     específicos cujo anexo inclui a NCM e é o primeiro lugar a olhar. O grupo "semAnexo" traz
     tratamentos que dependem só do contexto da operação.
   - serviço: listar_classificacoes_por_nbs e consultar_classificacao_tributaria para as candidatas.
3. Escolha a classificação considerando o contexto: quem é o adquirente, a finalidade, o regime do
   fornecedor, o destino e o tipo de documento. Muitos tratamentos com anexo exigem também uma
   condição da operação (por exemplo, insumos agropecuários vendidos a produtor rural). Se a
   condição não estiver informada, não a presuma: registre-a em "premissas" ou pergunte em
   "perguntas". Sem tratamento específico aplicável, a classificação é a tributação integral
   (CST 000, cClassTrib 000001).
4. Confirme a escolha: o CST com listar_classificacoes_por_cst (a cClassTrib deve estar na lista) e
   a NCM/NBS com verificar_ncm_aplicavel ou verificar_nbs_aplicavel.
5. Busque o dispositivo legal com consultar_fundamentacao_legal filtrando pela cClassTrib escolhida
   e cite a referência normativa e um trecho curto do texto.

Use sempre a data do fato gerador informada em todas as consultas.

Resposta final: preencha o esquema JSON pedido, em português.
- status "classificado" quando houver uma classificação defensável; "precisa_informacao" quando
  faltar um dado que muda o resultado. Nesse caso, ainda proponha a classificação mais provável,
  liste as perguntas e use confiança "baixa".
- confiança "alta" só quando o item, o código da nomenclatura e as condições do tratamento estiverem
  confirmados pelas ferramentas e pelo contexto informado.
- alternativas: outras classificações plausíveis e em que situação se aplicariam.
- reducao: os percentuais devolvidos pela ferramenta, sem cálculos próprios.
"""


class Reducao(BaseModel):
    cbs: float | None = Field(description="Percentual de redução da CBS")
    ibsUf: float | None = Field(description="Percentual de redução do IBS estadual")
    ibsMun: float | None = Field(description="Percentual de redução do IBS municipal")


class Fundamentacao(BaseModel):
    referencia: str = Field(description="Referência normativa, por exemplo 'LC 214/2025, art. 125'")
    trecho: str = Field(description="Trecho curto do texto legal devolvido pela ferramenta")


class Alternativa(BaseModel):
    cClassTrib: str
    cst: str
    quandoSeAplica: str


class Classificacao(BaseModel):
    """Resultado da classificação de um item."""

    status: Literal["classificado", "precisa_informacao"]
    item: str = Field(description="Resumo do item como você o entendeu")
    ncm: str | None = Field(description="NCM com 8 dígitos, para mercadorias")
    nbs: str | None = Field(description="NBS com 9 dígitos, para serviços")
    cst: str | None
    cClassTrib: str | None
    descricaoClassificacao: str | None
    tratamentoTributario: str | None
    reducao: Reducao | None
    fundamentacaoLegal: Fundamentacao | None
    confianca: Literal["alta", "media", "baixa"]
    justificativa: str
    premissas: list[str] = Field(description="Condições assumidas que não foram informadas")
    alternativas: list[Alternativa]
    perguntas: list[str] = Field(description="Perguntas ao usuário que podem mudar a classificação")


class ResultadoClassificacao(BaseModel):
    classificacao: Classificacao
    verificacoes: list[str] = Field(description="Conferências feitas pelo código na calculadora")
    revisaoHumanaRecomendada: bool
    data: str
    modelo: str


@dataclass
class Registro:
    """Registro detalhado de uma execução, preenchido à medida que ela avança (também em caso de erro).

    Usado pela avaliação para guardar a transcrição, o uso de tokens e o modelo que respondeu.
    """

    transcricao: list[dict[str, Any]] = field(default_factory=list)
    uso: dict[str, int] = field(
        default_factory=lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
    )
    modelos: list[str] = field(default_factory=list)
    stop_reasons: list[str | None] = field(default_factory=list)
    chamadas_ferramenta: int = 0

    def registrar_mensagem(self, mensagem: Any) -> None:
        self.modelos.append(getattr(mensagem, "model", None))
        self.stop_reasons.append(getattr(mensagem, "stop_reason", None))
        uso = getattr(mensagem, "usage", None)
        for chave in self.uso:
            self.uso[chave] += int(getattr(uso, chave, 0) or 0)
        pensamento = ""
        for bloco in mensagem.content:
            tipo = getattr(bloco, "type", None)
            if tipo == "thinking":
                pensamento += getattr(bloco, "thinking", "") or ""
            elif tipo == "text":
                self._turno({"role": "assistant", "content": bloco.text}, pensamento)
                pensamento = ""
            elif tipo == "tool_use":
                self.chamadas_ferramenta += 1
                self._turno(
                    {
                        "role": "tool_call",
                        "name": bloco.name,
                        "content": json.dumps(bloco.input, ensure_ascii=False, indent=2),
                    },
                    pensamento,
                )
                pensamento = ""

    def _turno(self, turno: dict[str, Any], pensamento: str) -> None:
        if pensamento:
            turno["thinking"] = pensamento
        self.transcricao.append(turno)


class _SessaoRegistrada:
    """Repassa as chamadas de ferramenta à sessão MCP e registra os resultados."""

    def __init__(self, sessao: Any, registro: Registro):
        self._sessao = sessao
        self._registro = registro

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        resultado = await self._sessao.call_tool(name, arguments, **kwargs)
        texto = "".join(getattr(c, "text", "") for c in (resultado.content or []))
        erro = bool(getattr(resultado, "is_error", None) or getattr(resultado, "isError", False))
        self._registro.transcricao.append({"role": "tool_result", "content": ("ERRO: " if erro else "") + texto})
        return resultado


class ErroClassificador(Exception):
    """Falha que impede a classificação."""


def formato_resposta() -> dict[str, Any]:
    """Esquema JSON da resposta final, adaptado às regras de structured outputs pelo SDK."""
    from anthropic import transform_schema

    return {"type": "json_schema", "schema": transform_schema(Classificacao.model_json_schema())}


def montar_pedido(descricao: str, data: str, ncm: str | None, nbs: str | None, contexto: str | None) -> str:
    linhas = [f"Item: {descricao}", f"Data do fato gerador: {data}"]
    if ncm:
        linhas.append(f"NCM informada pelo usuário: {ncm}")
    if nbs:
        linhas.append(f"NBS informada pelo usuário: {nbs}")
    linhas.append(f"Contexto da operação: {contexto or 'não informado'}")
    return "\n".join(linhas)


def _texto_final(mensagem: Any) -> str:
    if mensagem.stop_reason == "refusal":
        raise ErroClassificador("O modelo recusou o pedido (stop_reason=refusal).")
    if mensagem.stop_reason == "max_tokens":
        raise ErroClassificador("A resposta foi cortada por max_tokens; aumente --max-tokens.")
    textos = [b.text for b in mensagem.content if getattr(b, "type", None) == "text"]
    if not textos:
        raise ErroClassificador(f"O agente terminou sem resposta final (stop_reason={mensagem.stop_reason}).")
    return textos[-1]


def interpretar(texto: str) -> Classificacao:
    try:
        return Classificacao.model_validate_json(texto)
    except ValidationError as exc:
        raise ErroClassificador(f"Resposta final fora do esquema esperado: {exc}") from exc


async def _chamar(sessao: Any, nome: str, argumentos: dict[str, Any]) -> tuple[bool, Any]:
    """Chama uma ferramenta MCP e devolve (sucesso, dados)."""
    resultado = await sessao.call_tool(nome, argumentos)
    erro = getattr(resultado, "is_error", None)
    if erro is None:
        erro = getattr(resultado, "isError", False)
    texto = resultado.content[0].text if resultado.content else ""
    if erro:
        return False, texto
    try:
        return True, json.loads(texto).get("dados")
    except (ValueError, AttributeError):
        return True, texto


async def conferir(sessao: Any, c: Classificacao, data: str) -> list[str]:
    """Confere na calculadora os códigos propostos. Devolve as falhas (vazio = tudo confirmado)."""
    falhas: list[str] = []
    if c.cClassTrib is None or c.cst is None:
        return ["A resposta não traz CST e cClassTrib."]

    ok, dados = await _chamar(
        sessao, "consultar_classificacao_tributaria", {"c_class_trib": c.cClassTrib, "data": data}
    )
    if not ok:
        falhas.append(f"cClassTrib {c.cClassTrib} não encontrada na calculadora em {data}: {dados}")

    ok, dados = await _chamar(sessao, "listar_classificacoes_por_cst", {"cst": c.cst, "data": data})
    codigos = {x.get("codigo") for x in dados} if ok and isinstance(dados, list) else set()
    if c.cClassTrib not in codigos:
        falhas.append(f"cClassTrib {c.cClassTrib} não pertence ao CST {c.cst} em {data}.")

    if c.ncm:
        ok, dados = await _chamar(
            sessao, "verificar_ncm_aplicavel", {"c_class_trib": c.cClassTrib, "ncm": c.ncm, "data": data}
        )
        if not (ok and isinstance(dados, dict) and dados.get("valido")):
            falhas.append(f"NCM {c.ncm} não é aceita pela cClassTrib {c.cClassTrib}: {dados}")
    if c.nbs:
        ok, dados = await _chamar(
            sessao, "verificar_nbs_aplicavel", {"c_class_trib": c.cClassTrib, "nbs": c.nbs, "data": data}
        )
        if not (ok and isinstance(dados, dict) and dados.get("valido")):
            falhas.append(f"NBS {c.nbs} não é aceita pela cClassTrib {c.cClassTrib}: {dados}")
    if not c.ncm and not c.nbs:
        falhas.append("A resposta não traz NCM nem NBS.")
    return falhas


async def _executar(
    cliente: Any,
    sessao: Any,
    async_mcp_tool: Any,
    descricao: str,
    data: str,
    ncm: str | None,
    nbs: str | None,
    contexto: str | None,
    modelo: str,
    esforco: str,
    max_tokens: int,
    max_iteracoes: int,
    progresso: Any,
    registro: Registro | None = None,
) -> tuple[Classificacao, list[str]]:
    disponiveis = {t.name: t for t in (await sessao.list_tools()).tools}
    faltando = [n for n in FERRAMENTAS if n not in disponiveis]
    if faltando:
        raise ErroClassificador(f"O servidor MCP não oferece as ferramentas: {', '.join(faltando)}")
    pedido = montar_pedido(descricao, data, ncm, nbs, contexto)
    sessao_ferramentas = sessao
    if registro is not None:
        registro.transcricao += [{"role": "system", "content": SISTEMA}, {"role": "user", "content": pedido}]
        sessao_ferramentas = _SessaoRegistrada(sessao, registro)
    ferramentas = [async_mcp_tool(disponiveis[n], sessao_ferramentas) for n in FERRAMENTAS]

    runner = cliente.beta.messages.tool_runner(
        model=modelo,
        max_tokens=max_tokens,
        max_iterations=max_iteracoes,
        system=SISTEMA,
        tools=ferramentas,
        messages=[{"role": "user", "content": pedido}],
        thinking={"type": "adaptive"},
        output_config={"effort": esforco, "format": formato_resposta()},
        cache_control={"type": "ephemeral"},
        # Se o modelo recusar por engano, o servidor tenta outro modelo automaticamente.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    ultima = None
    async for mensagem in runner:
        ultima = mensagem
        if registro is not None:
            registro.registrar_mensagem(mensagem)
        if progresso:
            for bloco in mensagem.content:
                if getattr(bloco, "type", None) == "tool_use":
                    progresso(f"→ {bloco.name}({json.dumps(bloco.input, ensure_ascii=False)})")
    if ultima is None:
        raise ErroClassificador("O agente não produziu resposta.")
    if ultima.stop_reason == "tool_use":
        raise ErroClassificador(f"O agente atingiu o limite de {max_iteracoes} iterações sem concluir.")

    classificacao = interpretar(_texto_final(ultima))
    return classificacao, await conferir(sessao, classificacao, data)


async def classificar(
    descricao: str,
    *,
    data: str,
    ncm: str | None = None,
    nbs: str | None = None,
    contexto: str | None = None,
    modelo: str = MODELO_PADRAO,
    esforco: str = "high",
    max_tokens: int = 16000,
    max_iteracoes: int = 25,
    cliente: Any = None,
    parametros_servidor: Any = None,
    progresso: Any = None,
    registro: Registro | None = None,
) -> ResultadoClassificacao:
    """Classifica um item. `cliente` e `parametros_servidor` permitem injeção nos testes."""
    from anthropic import AsyncAnthropic
    from anthropic.lib.tools.mcp import async_mcp_tool
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    cliente = cliente or AsyncAnthropic()
    parametros_servidor = parametros_servidor or StdioServerParameters(
        command=sys.executable, args=["-m", "rtc_mcp.server"], env=dict(os.environ)
    )

    erro: Exception | None = None
    try:
        async with AsyncExitStack() as pilha:
            # O log do servidor MCP só interessa para depuração (RTC_DEBUG=1).
            log_servidor = sys.stderr if os.environ.get("RTC_DEBUG") else pilha.enter_context(open(os.devnull, "w"))
            leitura, escrita = await pilha.enter_async_context(
                stdio_client(parametros_servidor, errlog=log_servidor)
            )
            sessao = await pilha.enter_async_context(ClientSession(leitura, escrita))
            await sessao.initialize()
            # Erros dentro da sessão MCP sairiam embrulhados num ExceptionGroup do anyio;
            # guardamos o erro e o levantamos depois de fechar a sessão.
            try:
                classificacao, falhas = await _executar(
                    cliente, sessao, async_mcp_tool, descricao, data, ncm, nbs, contexto,
                    modelo, esforco, max_tokens, max_iteracoes, progresso, registro,
                )
            except Exception as exc:  # noqa: BLE001 - relançado abaixo, fora da sessão
                erro = exc
    except Exception as falha:  # o servidor MCP não iniciou ou caiu (pode vir num ExceptionGroup)
        if erro is None:
            causa = getattr(falha, "exceptions", [falha])[0]
            raise ErroClassificador(f"Falha no servidor MCP rtc-mcp: {causa!r}") from falha
    if erro is not None:
        raise erro

    verificacoes = falhas or ["CST, cClassTrib e NCM/NBS confirmados na calculadora."]
    revisao = bool(falhas) or classificacao.status != "classificado" or classificacao.confianca != "alta"
    return ResultadoClassificacao(
        classificacao=classificacao,
        verificacoes=verificacoes,
        revisaoHumanaRecomendada=revisao,
        data=data,
        modelo=modelo,
    )


def formatar(r: ResultadoClassificacao) -> str:
    c = r.classificacao
    linhas = [
        f"Item: {c.item}",
        f"Status: {c.status}  |  Confiança: {c.confianca}",
        f"NCM: {c.ncm or '-'}  NBS: {c.nbs or '-'}",
        f"CST: {c.cst or '-'}  cClassTrib: {c.cClassTrib or '-'}  {c.descricaoClassificacao or ''}",
    ]
    if c.tratamentoTributario:
        linhas.append(f"Tratamento: {c.tratamentoTributario}")
    if c.reducao:
        linhas.append(f"Redução: CBS {c.reducao.cbs}%  IBS UF {c.reducao.ibsUf}%  IBS Mun {c.reducao.ibsMun}%")
    if c.fundamentacaoLegal:
        linhas.append(f"Fundamentação: {c.fundamentacaoLegal.referencia}: \"{c.fundamentacaoLegal.trecho}\"")
    linhas.append(f"Justificativa: {c.justificativa}")
    for titulo, itens in (("Premissas", c.premissas), ("Perguntas", c.perguntas)):
        if itens:
            linhas.append(f"{titulo}:")
            linhas.extend(f"  - {i}" for i in itens)
    if c.alternativas:
        linhas.append("Alternativas:")
        linhas.extend(f"  - {a.cClassTrib} (CST {a.cst}): {a.quandoSeAplica}" for a in c.alternativas)
    linhas.append("Conferência na calculadora:")
    linhas.extend(f"  - {v}" for v in r.verificacoes)
    linhas.append("REVISÃO HUMANA RECOMENDADA" if r.revisaoHumanaRecomendada else "Classificação confirmada.")
    return "\n".join(linhas)


def main() -> None:
    import anthropic

    parser = argparse.ArgumentParser(
        description="Classifica um item (NCM/NBS, CST e cClassTrib de CBS/IBS) com o Claude e a calculadora RTC"
    )
    parser.add_argument("descricao", help="Descrição do item, por exemplo 'arroz branco polido, pacote 5 kg'")
    parser.add_argument("--data", default=date.today().isoformat(), help="Data do fato gerador (AAAA-MM-DD)")
    parser.add_argument("--ncm", help="NCM, se já conhecida")
    parser.add_argument("--nbs", help="NBS, se já conhecida")
    parser.add_argument("--contexto", help="Contexto da operação: adquirente, finalidade, regime, destino etc.")
    parser.add_argument("--modelo", default=MODELO_PADRAO)
    parser.add_argument("--esforco", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--max-tokens", type=int, default=16000)
    parser.add_argument("--json", action="store_true", help="Imprime o resultado em JSON")
    parser.add_argument("--silencioso", action="store_true", help="Não mostra as ferramentas chamadas")
    args = parser.parse_args()

    def progresso(texto: str) -> None:
        print(texto, file=sys.stderr)

    try:
        resultado = asyncio.run(
            classificar(
                args.descricao,
                data=args.data,
                ncm=args.ncm,
                nbs=args.nbs,
                contexto=args.contexto,
                modelo=args.modelo,
                esforco=args.esforco,
                max_tokens=args.max_tokens,
                progresso=None if args.silencioso else progresso,
            )
        )
    except ErroClassificador as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        sys.exit(1)
    except anthropic.AuthenticationError:
        print("Erro: credencial da API da Anthropic inválida (verifique ANTHROPIC_API_KEY).", file=sys.stderr)
        sys.exit(1)
    except TypeError as exc:
        # O SDK sinaliza a falta de credencial com TypeError ao enviar a primeira requisição.
        if "authentication" not in str(exc):
            raise
        print(
            "Erro: nenhuma credencial da API da Anthropic encontrada. Defina ANTHROPIC_API_KEY "
            "ou faça login com `ant auth login`.",
            file=sys.stderr,
        )
        sys.exit(1)
    except anthropic.RateLimitError:
        print("Erro: limite de uso da API da Anthropic atingido; tente novamente em instantes.", file=sys.stderr)
        sys.exit(1)
    except anthropic.APIConnectionError as exc:
        print(f"Erro: sem conexão com a API da Anthropic: {exc}", file=sys.stderr)
        sys.exit(1)
    except anthropic.APIStatusError as exc:
        print(f"Erro da API da Anthropic (HTTP {exc.status_code}): {exc.message}", file=sys.stderr)
        sys.exit(1)
    if args.json:
        print(resultado.model_dump_json(indent=2))
    else:
        print(formatar(resultado))
    sys.exit(2 if resultado.revisaoHumanaRecomendada else 0)


if __name__ == "__main__":
    main()
