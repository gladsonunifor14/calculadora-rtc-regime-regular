"""Agente corretor de erros: corrige operações que a calculadora rejeitou.

Recebe uma operação do regime regular (o JSON de entrada da calculadora, montado por um ERP, por
exemplo), diagnostica os erros, corrige o que dá para corrigir com as tabelas oficiais (classificação,
nomenclatura, campos técnicos) e diagnostica de novo até zerar os erros. O que depende de informação que
não está na operação, como as alíquotas nominais de 2027 em diante ou os dados do Imposto Seletivo, vira
pendência com pergunta ao usuário.

Os limites não ficam por conta do modelo: depois da resposta, o código diagnostica a operação corrigida
e compara campo a campo com a original. Remover ou acrescentar itens, mudar um campo sem declarar a
correção ou deixar erros não declarados invalida o resultado; mudar valor, quantidade ou data exige
revisão humana.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .classificador import ErroClassificador as ErroAgente
from .classificador import Registro, _SessaoRegistrada, _texto_final
from .client import RtcClient
from .correcao import ErroCorrecao, conferir, diagnosticar

MODELO_PADRAO = "claude-opus-5-5"

FERRAMENTAS = (
    "diagnosticar_operacao",
    "descrever_erro_regime_geral",
    "consultar_ncm",
    "consultar_nbs",
    "listar_classificacoes_aplicaveis_ncm",
    "listar_classificacoes_por_nbs",
    "verificar_ncm_aplicavel",
    "verificar_nbs_aplicavel",
    "consultar_classificacao_tributaria",
    "listar_classificacoes_por_cst",
    "listar_municipios",
)

SISTEMA = """\
Você corrige operações de cálculo de IBS/CBS (regime regular, LC 214/2025) que a calculadora oficial
rejeitou. A operação é o JSON de entrada da calculadora.

Regras invioláveis:
- Corrija só o que os erros apontam. Não "melhore" campos sem erro.
- Nunca remova, acrescente ou reordene itens.
- Nunca altere baseCalculo, quantidade, valores, dhFatoGerador ou id para fazer um erro sumir. Se o erro
  estiver mesmo num desses campos, não corrija: registre uma pendência e pergunte.
- Nunca invente dados que só o usuário tem: alíquotas nominais (REG-037, obrigatórias de 2027 em diante),
  dados do Imposto Seletivo (REG-013), município quando ele e a UF não combinam e não há como saber qual
  dos dois está certo. Esses viram pendência com pergunta.

Método:
1. Rode diagnosticar_operacao com a operação recebida. Use descrever_erro_regime_geral quando o código
   não for claro.
2. Corrija os erros com as ferramentas de consulta:
   - NCM/NBS não encontrada (REG-002): formato (8 dígitos para NCM, 9 para NBS, sem pontos) ou código
     vigente para a descrição do produto (consultar_ncm).
   - NCM não vinculada à cClassTrib (REG-011): proponha a cClassTrib aplicável com
     listar_classificacoes_aplicaveis_ncm e confirme com verificar_ncm_aplicavel; se a escolha depender do
     contexto da operação e ele não estiver claro, use a tributação integral (CST 000, cClassTrib 000001)
     só se for de fato o caso geral, ou registre pendência.
   - cClassTrib fora do CST (REG-006): ajuste o CST ao da cClassTrib (listar_classificacoes_por_cst) ou a
     cClassTrib ao CST, o que for coerente com o item.
   - Campos técnicos ausentes (VAL-001), como versao: preencha com o valor padrão evidente ("1.0.0").
3. Diagnostique de novo depois de cada rodada: a calculadora mostra um erro por vez em cada item, e a
   validação de campos obrigatórios (VAL-xxx) esconde os erros de regra de negócio até ser resolvida.
4. Pare quando não houver erros ou quando só restarem pendências.

Resposta final no esquema JSON pedido, em português:
- operacaoCorrigidaJson: a operação completa corrigida, como texto JSON.
- correcoes: uma por campo alterado, com o ponteiro JSON (ex.: /itens/0/cClassTrib), o valor anterior,
  o novo, o código do erro e o motivo.
- pendencias: um registro por erro que ficou sem correção, com o código.
- status: "sem_erros" se a operação original já não tinha erros; "corrigida" se ficou sem erros;
  "parcial" se restaram pendências; "nao_corrigivel" se nada pôde ser corrigido.
"""


class Correcao(BaseModel):
    ponteiro: str = Field(description="Ponteiro JSON do campo alterado, ex.: /itens/0/cClassTrib")
    valorAnterior: str | None
    valorNovo: str | None
    codigoErro: str | None
    motivo: str
    confianca: Literal["alta", "media", "baixa"]


class Pendencia(BaseModel):
    codigoErro: str
    item: int | None
    motivo: str


class RespostaCorretor(BaseModel):
    status: Literal["sem_erros", "corrigida", "parcial", "nao_corrigivel"]
    resumo: str
    operacaoCorrigidaJson: str = Field(description="A operação completa corrigida, como texto JSON")
    correcoes: list[Correcao]
    pendencias: list[Pendencia]
    perguntas: list[str]


class ResultadoCorrecao(BaseModel):
    resposta: RespostaCorretor
    operacaoCorrigida: dict[str, Any] | None
    diagnosticoOriginal: dict[str, Any]
    diagnosticoFinal: dict[str, Any] | None
    violacoes: list[str]
    alteracoesProtegidas: list[dict[str, Any]]
    diferencas: list[dict[str, Any]]
    pronta: bool = Field(description="A operação corrigida pode ser usada sem revisão")
    revisaoHumanaRecomendada: bool
    modelo: str


def formato_resposta() -> dict[str, Any]:
    from anthropic import transform_schema

    return {"type": "json_schema", "schema": transform_schema(RespostaCorretor.model_json_schema())}


def _como_texto(valor: Any) -> str | None:
    if valor is None:
        return None
    return valor if isinstance(valor, str) else json.dumps(valor, ensure_ascii=False)


async def avaliar_resposta(
    cliente: RtcClient, original: dict[str, Any], resposta: RespostaCorretor, diagnostico_original: dict[str, Any]
) -> ResultadoCorrecao:
    """Confere a resposta do agente contra a calculadora e contra a operação original."""
    violacoes: list[str] = []
    try:
        corrigida = json.loads(resposta.operacaoCorrigidaJson)
        if not isinstance(corrigida, dict):
            raise ValueError("não é um objeto JSON")
    except ValueError as exc:
        corrigida = None
        violacoes.append(f"operacaoCorrigidaJson não é um JSON de operação válido: {exc}")

    diagnostico_final, protegidas, diff = None, [], []
    if corrigida is not None:
        try:
            diagnostico_final = await diagnosticar(cliente, corrigida)
        except ErroCorrecao as exc:
            violacoes.append(f"A operação corrigida não pôde ser diagnosticada: {exc}")
        if diagnostico_final is not None:
            r = conferir(
                original, corrigida, [c.ponteiro for c in resposta.correcoes], diagnostico_final,
                [p.codigoErro for p in resposta.pendencias],
            )
            violacoes += r["violacoes"]
            protegidas, diff = r["alteracoesProtegidas"], r["diferencas"]
            # O valor novo declarado deve ser o que de fato está na operação corrigida.
            por_ponteiro = {d["ponteiro"]: d for d in diff}
            for c in resposta.correcoes:
                d = por_ponteiro.get("/" + c.ponteiro.lstrip("#").lstrip("/"))
                if d and _como_texto(d["depois"]) != c.valorNovo:
                    violacoes.append(
                        f"Correção em {c.ponteiro} declara o valor {c.valorNovo!r}, mas a operação tem {_como_texto(d['depois'])!r}."
                    )

    sem_erros = diagnostico_final is not None and diagnostico_final["situacao"] == "sem_erros"
    pronta = not violacoes and not protegidas and sem_erros
    revisao = (
        not pronta
        or bool(resposta.pendencias)
        or bool(resposta.perguntas)
        or any(c.confianca != "alta" for c in resposta.correcoes)
    )
    return ResultadoCorrecao(
        resposta=resposta,
        operacaoCorrigida=corrigida,
        diagnosticoOriginal=diagnostico_original,
        diagnosticoFinal=diagnostico_final,
        violacoes=violacoes,
        alteracoesProtegidas=protegidas,
        diferencas=diff,
        pronta=pronta,
        revisaoHumanaRecomendada=revisao,
        modelo="",
    )


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
        return RespostaCorretor.model_validate_json(_texto_final(ultima))
    except ValidationError as exc:
        raise ErroAgente(f"Resposta final fora do esquema esperado: {exc}") from exc


def montar_pedido(operacao: dict[str, Any], contexto: str | None) -> str:
    linhas = ["Corrija esta operação de cálculo:", "```json", json.dumps(operacao, ensure_ascii=False, indent=1), "```"]
    if contexto:
        linhas.append(f"Contexto informado pelo usuário: {contexto}")
    return "\n".join(linhas)


async def corrigir(
    operacao: dict[str, Any],
    *,
    contexto: str | None = None,
    modelo: str = MODELO_PADRAO,
    esforco: str = "high",
    max_tokens: int = 16000,  # acima disso o SDK exige streaming
    max_iteracoes: int = 40,
    cliente: Any = None,
    parametros_servidor: Any = None,
    cliente_rtc: RtcClient | None = None,
    progresso: Any = None,
    registro: Registro | None = None,
) -> ResultadoCorrecao:
    """Corrige uma operação e devolve a resposta do agente conferida pelo código."""
    from anthropic import AsyncAnthropic
    from anthropic.lib.tools.mcp import async_mcp_tool
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    rtc = cliente_rtc or RtcClient()
    try:
        try:
            diagnostico_original = await diagnosticar(rtc, operacao)
        except ErroCorrecao as exc:
            raise ErroAgente(f"Não foi possível diagnosticar a operação: {exc}") from exc

        if diagnostico_original["situacao"] == "sem_erros":
            # Nada a corrigir: não há por que chamar o modelo.
            resposta = RespostaCorretor(
                status="sem_erros", resumo="A operação não tem erros na calculadora.",
                operacaoCorrigidaJson=json.dumps(operacao, ensure_ascii=False), correcoes=[], pendencias=[], perguntas=[],
            )
        else:
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
                        resposta = await _executar(
                            cliente, sessao, async_mcp_tool, montar_pedido(operacao, contexto), modelo, esforco,
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

        resultado = await avaliar_resposta(rtc, operacao, resposta, diagnostico_original)
    finally:
        if cliente_rtc is None:
            await rtc.aclose()
    resultado.modelo = modelo
    return resultado


def formatar(r: ResultadoCorrecao) -> str:
    resp = r.resposta
    linhas = [f"Situação: {resp.status}  |  erros na original: {len(r.diagnosticoOriginal['erros'])}"
              f"  |  erros na corrigida: {len(r.diagnosticoFinal['erros']) if r.diagnosticoFinal else '-'}", "", resp.resumo, ""]
    if resp.correcoes:
        linhas.append("Correções:")
        for c in resp.correcoes:
            linhas.append(f"  {c.ponteiro}: {c.valorAnterior} -> {c.valorNovo} [{c.codigoErro or '-'}, confiança {c.confianca}] {c.motivo}")
    if resp.pendencias:
        linhas.append("Pendências:")
        linhas.extend(f"  {p.codigoErro} (item {p.item if p.item is not None else '-'}): {p.motivo}" for p in resp.pendencias)
    if resp.perguntas:
        linhas.append("Perguntas:")
        linhas.extend(f"  - {p}" for p in resp.perguntas)
    if r.alteracoesProtegidas:
        linhas.append("ATENÇÃO, campos de valor, quantidade ou data alterados:")
        linhas.extend(f"  {d['ponteiro']}: {d['antes']} -> {d['depois']}" for d in r.alteracoesProtegidas)
    if r.violacoes:
        linhas.append("Correção recusada pela conferência:")
        linhas.extend(f"  - {v}" for v in r.violacoes)
    if r.diagnosticoFinal and r.diagnosticoFinal["erros"]:
        linhas.append("Erros que restam na operação corrigida:")
        linhas.extend(f"  {e['codigo']} (item {e['item'] if e['item'] is not None else '-'}): {e['detalhe']}" for e in r.diagnosticoFinal["erros"])
    linhas.append("Operação pronta para uso." if r.pronta and not r.revisaoHumanaRecomendada else "REVISÃO HUMANA RECOMENDADA")
    return "\n".join(linhas)


def _gravar_sem_sobrescrever(origem: Path, conteudo: str) -> Path:
    for n in range(1000):
        destino = origem.with_name(f"{origem.stem}.corrigida{'' if n == 0 else f'-{n}'}.json")
        try:
            with destino.open("x", encoding="utf-8") as f:
                f.write(conteudo)
            return destino
        except FileExistsError:
            continue
    raise OSError(f"muitos arquivos corrigidos para {origem}")


def main() -> None:
    import anthropic

    parser = argparse.ArgumentParser(description="Corrige uma operação rejeitada pela calculadora RTC (regime regular)")
    parser.add_argument("operacao", help="Arquivo JSON com a operação (formato de entrada da calculadora)")
    parser.add_argument("--contexto", help="Informações que não estão na operação")
    parser.add_argument("--modelo", default=MODELO_PADRAO)
    parser.add_argument("--esforco", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--json", action="store_true", help="Imprime o resultado em JSON")
    parser.add_argument("--silencioso", action="store_true", help="Não mostra as ferramentas chamadas")
    args = parser.parse_args()
    origem = Path(args.operacao)
    try:
        operacao = json.loads(origem.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"Erro: não foi possível ler a operação em {origem}: {exc}", file=sys.stderr)
        sys.exit(1)
    try:
        resultado = asyncio.run(corrigir(operacao, contexto=args.contexto, modelo=args.modelo, esforco=args.esforco,
                                         progresso=None if args.silencioso else (lambda t: print(t, file=sys.stderr))))
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
    if resultado.operacaoCorrigida is not None and not resultado.violacoes and resultado.resposta.correcoes:
        destino = _gravar_sem_sobrescrever(origem, json.dumps(resultado.operacaoCorrigida, ensure_ascii=False, indent=2))
        print(f"Operação corrigida gravada em {destino} (o arquivo original não foi alterado).", file=sys.stderr)
    sys.exit(2 if resultado.revisaoHumanaRecomendada else 0)


if __name__ == "__main__":
    main()
