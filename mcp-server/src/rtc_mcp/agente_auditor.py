"""Agente auditor de NF-e: confere o IBS/CBS de uma nota e explica as divergências.

Roda a auditoria determinística (auditar_nfe) sobre o XML, investiga as causas com as ferramentas de
consulta (classificação, fundamentação legal, base de cálculo) e escreve um relatório com o impacto de
cada divergência e a correção sugerida.

Os achados não vêm do modelo: depois do relatório, o código roda a auditoria de novo e anexa o
resultado. O relatório vai para revisão humana quando a situação declarada difere da auditoria, quando
um item com erro ficou sem explicação ou quando uma cClassTrib sugerida não passa na conferência da
calculadora.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .auditoria import ErroAuditoria, auditar
from .classificador import ErroClassificador as ErroAgente
from .classificador import Registro, _SessaoRegistrada, _texto_final
from .client import CalculadoraError, RtcClient

MODELO_PADRAO = "claude-opus-5-5"

FERRAMENTAS = (
    "auditar_nfe",
    "descrever_erro_regime_geral",
    "consultar_ncm",
    "listar_classificacoes_aplicaveis_ncm",
    "consultar_classificacao_tributaria",
    "listar_classificacoes_por_cst",
    "verificar_ncm_aplicavel",
    "consultar_fundamentacao_legal",
)

SISTEMA = """\
Você é um auditor fiscal especializado no IBS e na CBS da Reforma Tributária do Consumo (LC 214/2025).
Sua tarefa é auditar o grupo IBSCBS de uma NF-e ou NFC-e e explicar cada divergência.

Os achados vêm da ferramenta auditar_nfe, que confere a nota contra as calculadoras oficiais: XSD,
classificação (cClassTrib x CST x NCM), base de cálculo recalculada a partir dos componentes do item,
reduções, valores e totais. Não recalcule tributo por conta própria e não contradiga a ferramenta.

Método:
1. Rode auditar_nfe com o caminho ou o XML informado.
2. Para cada achado de gravidade "erro", investigue a causa:
   - classificação (REG-011 e semelhantes): use descrever_erro_regime_geral, consultar_ncm e
     listar_classificacoes_aplicaveis_ncm para propor a cClassTrib correta para o produto; confirme com
     verificar_ncm_aplicavel e listar_classificacoes_por_cst e cite consultar_fundamentacao_legal.
     Se a escolha depender de informação que não está na nota (quem é o adquirente, por exemplo),
     diga isso em vez de supor.
   - base de cálculo: explique quais componentes do item formam a base (vProd, frete, seguro, outras
     despesas, menos desconto e os tributos deduzidos no ano) e qual parece ter ficado de fora ou sobrando.
   - reduções e valores: diga se a redução da cClassTrib foi esquecida ou aplicada errada e o efeito no
     valor do item.
   - totais: aponte se a diferença vem dos itens ou só do grupo de totais.
3. Achados "alerta" e "info" entram no relatório só quando mudam a leitura do resultado.

Resposta final no esquema JSON pedido, em português:
- status: "conforme" quando a auditoria não tem erros; "com_divergencias" quando tem; "nao_auditavel"
  quando a ferramenta não conseguiu auditar a nota.
- divergencias: uma por problema, com o item (null para a nota toda), o que está errado, o impacto e a
  correção. Cite valores só como aparecem na ferramenta.
- classificacoesSugeridas: só quando você propõe trocar a cClassTrib de um item.
- ressalvas: inclua o escopo da auditoria (o que ela não confere).
"""


class Divergencia(BaseModel):
    item: int | None = Field(description="Número do item (nItem) ou null para a nota toda")
    tipo: Literal["xsd", "classificacao", "base", "reducao", "aliquota", "valor", "total", "outro"]
    descricao: str
    impacto: str = Field(description="Efeito no tributo devido, no crédito do cliente ou na validade da nota")
    correcao: str
    confianca: Literal["alta", "media", "baixa"]


class ClassificacaoSugerida(BaseModel):
    item: int
    cClassTribAtual: str | None
    cstSugerido: str
    cClassTribSugerida: str
    justificativa: str


class Relatorio(BaseModel):
    status: Literal["conforme", "com_divergencias", "nao_auditavel"]
    resumo: str
    divergencias: list[Divergencia]
    classificacoesSugeridas: list[ClassificacaoSugerida]
    perguntas: list[str] = Field(description="Informações que faltam para concluir alguma correção")
    ressalvas: list[str]


class ResultadoAuditoria(BaseModel):
    relatorio: Relatorio
    auditoria: dict[str, Any] | None = Field(description="Resultado da auditoria determinística")
    verificacoes: list[str]
    revisaoHumanaRecomendada: bool
    modelo: str


def formato_resposta() -> dict[str, Any]:
    from anthropic import transform_schema

    return {"type": "json_schema", "schema": transform_schema(Relatorio.model_json_schema())}


async def conferir_sugestao(cliente: RtcClient, sugestao: ClassificacaoSugerida, ncm: str | None, data: str) -> list[str]:
    """Confere na calculadora uma cClassTrib sugerida: existe, pertence ao CST e aceita a NCM."""
    problemas: list[str] = []
    base = "calculadora/dados-abertos/classificacoes-tributarias"
    cc, cst = sugestao.cClassTribSugerida, sugestao.cstSugerido
    try:
        await cliente.regime_geral("GET", f"{base}/cbs-ibs/class-trib/{cc}", params={"data": data})
    except CalculadoraError:
        return [f"Item {sugestao.item}: cClassTrib sugerida {cc} não existe em {data}."]
    try:
        lista = (await cliente.regime_geral("GET", f"{base}/cbs-ibs/{cst}", params={"data": data}))["dados"]
        if cc not in {x.get("codigo") for x in lista or []}:
            problemas.append(f"Item {sugestao.item}: cClassTrib sugerida {cc} não pertence ao CST {cst}.")
    except CalculadoraError:
        problemas.append(f"Item {sugestao.item}: CST sugerido {cst} não existe em {data}.")
    if ncm:
        try:
            r = (await cliente.regime_geral(
                "GET", f"{base}/ncm-aplicavel", params={"cClassTrib": cc, "ncm": ncm, "dataOcorrenciaFatoGerador": data}
            ))["dados"]
            if not (r or {}).get("valido"):
                problemas.append(f"Item {sugestao.item}: cClassTrib sugerida {cc} não aceita a NCM {ncm}.")
        except CalculadoraError as exc:
            problemas.append(f"Item {sugestao.item}: não foi possível conferir a NCM {ncm} com {cc}: {exc}")
    return problemas


async def verificar(cliente: RtcClient, relatorio: Relatorio, auditoria: dict[str, Any] | None) -> list[str]:
    """Confere o relatório contra a auditoria determinística. Devolve os problemas encontrados."""
    if auditoria is None:
        return [] if relatorio.status == "nao_auditavel" else ["A nota não pôde ser auditada, mas o relatório tem conclusão."]
    problemas: list[str] = []
    if relatorio.status != auditoria["situacao"]:
        problemas.append(f"Status do relatório '{relatorio.status}' difere da auditoria '{auditoria['situacao']}'.")
    itens_com_erro = {a["item"] for a in auditoria["achados"] if a["gravidade"] == "erro"}
    explicados = {d.item for d in relatorio.divergencias}
    sem_explicacao = sorted(itens_com_erro - explicados, key=lambda i: (i is None, i or 0))
    if sem_explicacao:
        rotulos = ", ".join("nota" if i is None else f"item {i}" for i in sem_explicacao)
        problemas.append(f"Erros da auditoria sem explicação no relatório: {rotulos}.")
    ncm_por_item = {i["numero"]: i["ncm"] for i in auditoria["itens"]}
    for sugestao in relatorio.classificacoesSugeridas:
        problemas += await conferir_sugestao(cliente, sugestao, ncm_por_item.get(sugestao.item), auditoria["nota"]["data"])
    return problemas


async def _executar(cliente, sessao, async_mcp_tool, pedido, modelo, esforco, max_tokens, max_iteracoes, progresso, registro):
    disponiveis = {t.name: t for t in (await sessao.list_tools()).tools}
    faltando = [n for n in FERRAMENTAS if n not in disponiveis]
    if faltando:
        raise ErroAgente(f"O servidor MCP não oferece as ferramentas: {', '.join(faltando)}")
    sessao_ferramentas = sessao
    if registro is not None:
        registro.transcricao += [{"role": "system", "content": SISTEMA}, {"role": "user", "content": pedido}]
        sessao_ferramentas = _SessaoRegistrada(sessao, registro)
    runner = cliente.beta.messages.tool_runner(
        model=modelo,
        max_tokens=max_tokens,
        max_iterations=max_iteracoes,
        system=SISTEMA,
        tools=[async_mcp_tool(disponiveis[n], sessao_ferramentas) for n in FERRAMENTAS],
        messages=[{"role": "user", "content": pedido}],
        thinking={"type": "adaptive"},
        output_config={"effort": esforco, "format": formato_resposta()},
        cache_control={"type": "ephemeral"},
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
                    progresso(f"→ {bloco.name}")
    if ultima is None:
        raise ErroAgente("O agente não produziu resposta.")
    if ultima.stop_reason == "tool_use":
        raise ErroAgente(f"O agente atingiu o limite de {max_iteracoes} iterações sem concluir.")
    try:
        return Relatorio.model_validate_json(_texto_final(ultima))
    except ValidationError as exc:
        raise ErroAgente(f"Resposta final fora do esquema esperado: {exc}") from exc


def montar_pedido(caminho: Path, contexto: str | None) -> str:
    linhas = [f"Audite a NF-e do arquivo {caminho} (use auditar_nfe com caminho_xml)."]
    if contexto:
        linhas.append(f"Contexto informado pelo usuário: {contexto}")
    return "\n".join(linhas)


async def auditar_nota(
    caminho_xml: str | Path,
    *,
    contexto: str | None = None,
    modelo: str = MODELO_PADRAO,
    esforco: str = "high",
    max_tokens: int = 16000,  # acima disso o SDK exige streaming
    max_iteracoes: int = 30,
    cliente: Any = None,
    parametros_servidor: Any = None,
    cliente_rtc: RtcClient | None = None,
    progresso: Any = None,
    registro: Registro | None = None,
) -> ResultadoAuditoria:
    """Audita uma nota e devolve o relatório do agente com a auditoria determinística anexada."""
    from anthropic import AsyncAnthropic
    from anthropic.lib.tools.mcp import async_mcp_tool
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    caminho = Path(caminho_xml).expanduser().resolve()
    try:
        xml = caminho.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ErroAgente(f"Não foi possível ler {caminho}: {exc}") from exc

    cliente = cliente or AsyncAnthropic()
    parametros_servidor = parametros_servidor or StdioServerParameters(
        command=sys.executable, args=["-m", "rtc_mcp.server"], env=dict(os.environ)
    )
    erro: Exception | None = None
    try:
        async with AsyncExitStack() as pilha:
            log = sys.stderr if os.environ.get("RTC_DEBUG") else pilha.enter_context(open(os.devnull, "w"))
            leitura, escrita = await pilha.enter_async_context(stdio_client(parametros_servidor, errlog=log))
            sessao = await pilha.enter_async_context(ClientSession(leitura, escrita))
            await sessao.initialize()
            try:
                relatorio = await _executar(
                    cliente, sessao, async_mcp_tool, montar_pedido(caminho, contexto), modelo, esforco,
                    max_tokens, max_iteracoes, progresso, registro,
                )
            except Exception as exc:  # noqa: BLE001 - relançado fora da sessão MCP
                erro = exc
    except Exception as falha:
        if erro is None:
            causa = getattr(falha, "exceptions", [falha])[0]
            raise ErroAgente(f"Falha no servidor MCP rtc-mcp: {causa!r}") from falha
    if erro is not None:
        raise erro

    # Achados autoritativos: o código roda a auditoria de novo, sem o modelo.
    rtc = cliente_rtc or RtcClient()
    verificacoes: list[str] = []
    try:
        try:
            resultado = await auditar(rtc, xml)
        except ErroAuditoria as exc:
            resultado = None
            verificacoes.append(f"Nota não auditável: {exc}")
        verificacoes += await verificar(rtc, relatorio, resultado)
    finally:
        if cliente_rtc is None:
            await rtc.aclose()
    problemas = [v for v in verificacoes if not v.startswith("Nota não auditável")]
    revisao = bool(problemas) or relatorio.status != "conforme" or bool(relatorio.perguntas)
    return ResultadoAuditoria(
        relatorio=relatorio,
        auditoria=resultado,
        verificacoes=verificacoes or ["Relatório conferido com a auditoria determinística."],
        revisaoHumanaRecomendada=revisao,
        modelo=modelo,
    )


ROTULOS = {"conforme": "CONFORME", "com_divergencias": "COM DIVERGÊNCIAS", "nao_auditavel": "NÃO AUDITÁVEL"}


def formatar(r: ResultadoAuditoria) -> str:
    rel, a = r.relatorio, r.auditoria
    linhas = []
    if a:
        n = a["nota"]
        linhas.append(f"NF-e {n['numero']} (modelo {n['modelo']}), {n['data']}, chave {n['chave']}")
        linhas.append(f"Itens: {a['resumo']['itens']}  |  erros: {a['resumo']['erros']}  |  alertas: {a['resumo']['alertas']}")
    linhas += [f"Situação: {ROTULOS[rel.status]}", "", rel.resumo, ""]
    if rel.divergencias:
        linhas.append("Divergências:")
        for d in rel.divergencias:
            onde = "nota" if d.item is None else f"item {d.item}"
            linhas += [f"  [{onde}] {d.tipo} (confiança {d.confianca}): {d.descricao}",
                       f"      impacto: {d.impacto}", f"      correção: {d.correcao}"]
    if rel.classificacoesSugeridas:
        linhas.append("Classificações sugeridas:")
        for s in rel.classificacoesSugeridas:
            linhas.append(f"  item {s.item}: {s.cClassTribAtual} -> {s.cClassTribSugerida} (CST {s.cstSugerido}): {s.justificativa}")
    if a and a["achados"]:
        linhas.append("Achados da auditoria (calculadoras oficiais):")
        for x in a["achados"]:
            onde = "nota" if x["item"] is None else f"item {x['item']}"
            linhas.append(f"  {x['gravidade'].upper():6} [{onde}] {x['mensagem']}")
    for titulo, itens in (("Perguntas", rel.perguntas),
                          ("Premissas", a["premissas"] if a else []),
                          ("Ressalvas", rel.ressalvas + ([a["escopo"]] if a else [])),
                          ("Conferência", r.verificacoes)):
        unicos = list(dict.fromkeys(itens))
        if unicos:
            linhas.append(f"{titulo}:")
            linhas.extend(f"  - {i}" for i in unicos)
    linhas.append("REVISÃO HUMANA RECOMENDADA" if r.revisaoHumanaRecomendada else "Nota conforme e relatório conferido.")
    return "\n".join(linhas)


def main() -> None:
    import anthropic

    parser = argparse.ArgumentParser(description="Audita o IBS/CBS de uma NF-e/NFC-e com o Claude e as calculadoras RTC")
    parser.add_argument("xml", help="Arquivo XML da NF-e ou NFC-e")
    parser.add_argument("--contexto", help="Informações que não estão na nota (adquirente, finalidade etc.)")
    parser.add_argument("--modelo", default=MODELO_PADRAO)
    parser.add_argument("--esforco", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--json", action="store_true", help="Imprime o resultado em JSON")
    parser.add_argument("--silencioso", action="store_true", help="Não mostra as ferramentas chamadas")
    args = parser.parse_args()
    try:
        resultado = asyncio.run(
            auditar_nota(args.xml, contexto=args.contexto, modelo=args.modelo, esforco=args.esforco,
                         progresso=None if args.silencioso else (lambda t: print(t, file=sys.stderr)))
        )
    except ErroAgente as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        sys.exit(1)
    except anthropic.AuthenticationError:
        print("Erro: credencial da API da Anthropic inválida (verifique ANTHROPIC_API_KEY).", file=sys.stderr)
        sys.exit(1)
    except anthropic.APIStatusError as exc:
        print(f"Erro da API da Anthropic (HTTP {exc.status_code}): {exc.message}", file=sys.stderr)
        sys.exit(1)
    except anthropic.APIConnectionError as exc:
        print(f"Erro: sem conexão com a API da Anthropic: {exc}", file=sys.stderr)
        sys.exit(1)
    except TypeError as exc:
        if "authentication" not in str(exc):
            raise
        print("Erro: nenhuma credencial da API da Anthropic encontrada. Defina ANTHROPIC_API_KEY "
              "ou faça login com `ant auth login`.", file=sys.stderr)
        sys.exit(1)
    print(resultado.model_dump_json(indent=2) if args.json else formatar(resultado))
    sys.exit(2 if resultado.revisaoHumanaRecomendada else 0)


if __name__ == "__main__":
    main()
