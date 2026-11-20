"""Agente consultor jurídico da Reforma Tributária do Consumo.

Responde perguntas sobre o IBS e a CBS com base na fundamentação legal oficial da calculadora (trechos da
LC 214/2025 associados a cada classificação tributária) e nas tabelas de consulta. A base inteira vai no
prompt, em cache; o agente cita os trechos pelo identificador e copia o texto citado.

As citações não ficam por conta do modelo: depois da resposta, o código confere que cada citação existe e
que o texto citado está palavra por palavra no trecho, que todo artigo mencionado na resposta está num
trecho citado e que toda cClassTrib mencionada existe. Uma falha, uma pergunta fora da base ou confiança
abaixo de "alta" mandam a resposta para revisão humana.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from contextlib import AsyncExitStack
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .classificador import ErroClassificador as ErroAgente
from .classificador import Registro, _SessaoRegistrada, _texto_final
from .client import RtcClient
from .consulta import ErroConsulta, carregar_base, conferir_citacoes, formatar_base

MODELO_PADRAO = "claude-opus-5-5"

FERRAMENTAS = (
    "consultar_classificacao_tributaria",
    "listar_situacoes_tributarias",
    "consultar_ncm",
    "consultar_nbs",
    "listar_classificacoes_aplicaveis_ncm",
    "listar_classificacoes_por_nbs",
    "consultar_aliquotas_referencia",
)

INSTRUCOES = """\
Você é um consultor jurídico especializado na Reforma Tributária do Consumo (EC 132/2023 e LC 214/2025),
focado no IBS e na CBS. Responde perguntas de contadores, advogados e empresas.

Sua fonte é a base de fundamentação legal abaixo: trechos oficiais da LC 214/2025, cada um ligado a uma
classificação tributária (cClassTrib) e identificado por um id ("F-200003", por exemplo). As ferramentas
completam com dados das tabelas oficiais (percentuais de redução, CST, NCM/NBS, alíquotas de referência).

Regras:
- Toda afirmação jurídica precisa de citação de um trecho da base: informe o id e copie, palavra por
  palavra, a parte do texto que a sustenta (uma frase ou um inciso, não o trecho inteiro).
- Só mencione artigos que aparecem nos trechos que você cita, e só cClassTrib que existem na base.
- A base cobre os dispositivos ligados às classificações tributárias, não a lei inteira. Se a pergunta
  depende de algo que não está nela (por exemplo, regras de cashback, split payment, prazos de
  apuração), diga isso claramente, marque baseSuficiente = false e não responda de memória como se fosse
  fundamentado. Pode indicar onde o usuário deve procurar.
- Dados numéricos (percentuais de redução, alíquotas) vêm das ferramentas, não da sua memória.
- Seja objetivo: responda à pergunta primeiro, depois explique e cite.
- Não é aconselhamento jurídico definitivo: casos concretos devem ser confirmados por profissional.

Resposta final no esquema JSON pedido, em português.
"""


class Citacao(BaseModel):
    id: str = Field(description='Id do trecho da base, ex.: "F-200003"')
    trecho: str = Field(description="Parte do texto do trecho, copiada palavra por palavra")


class Parecer(BaseModel):
    resposta: str
    citacoes: list[Citacao]
    baseSuficiente: bool = Field(description="A base de fundamentação sustenta a resposta")
    confianca: Literal["alta", "media", "baixa"]
    limitacoes: list[str]
    perguntas: list[str] = Field(description="Informações que mudariam a resposta")


class ResultadoConsulta(BaseModel):
    parecer: Parecer
    problemas: list[str]
    revisaoHumanaRecomendada: bool
    base: dict[str, Any]
    modelo: str


def formato_resposta() -> dict[str, Any]:
    from anthropic import transform_schema

    return {"type": "json_schema", "schema": transform_schema(Parecer.model_json_schema())}


def montar_sistema(base_formatada: str, metadados: dict[str, Any]) -> str:
    return (
        f"{INSTRUCOES}\n<base_fundamentacao versao=\"{metadados.get('versaoBaseDados')}\" "
        f"vigencia=\"{metadados.get('data')}\">\n{base_formatada}\n</base_fundamentacao>"
    )


async def _executar(cliente, sessao, async_mcp_tool, sistema, pedido, modelo, esforco, max_tokens, max_iteracoes, progresso, registro):
    disponiveis = {t.name: t for t in (await sessao.list_tools()).tools}
    faltando = [n for n in FERRAMENTAS if n not in disponiveis]
    if faltando:
        raise ErroAgente(f"O servidor MCP não oferece as ferramentas: {', '.join(faltando)}")
    sessao_ferramentas = sessao
    if registro is not None:
        registro.transcricao += [{"role": "system", "content": sistema}, {"role": "user", "content": pedido}]
        sessao_ferramentas = _SessaoRegistrada(sessao, registro)
    runner = cliente.beta.messages.tool_runner(
        model=modelo,
        max_tokens=max_tokens,
        max_iterations=max_iteracoes,
        system=sistema,
        tools=[async_mcp_tool(disponiveis[n], sessao_ferramentas) for n in FERRAMENTAS],
        messages=[{"role": "user", "content": pedido}],
        thinking={"type": "adaptive"},
        output_config={"effort": esforco, "format": formato_resposta()},
        cache_control={"type": "ephemeral"},  # a base de fundamentação é o prefixo estável
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
        return Parecer.model_validate_json(_texto_final(ultima))
    except ValidationError as exc:
        raise ErroAgente(f"Resposta final fora do esquema esperado: {exc}") from exc


async def consultar(
    pergunta: str,
    *,
    data: str | None = None,
    modelo: str = MODELO_PADRAO,
    esforco: str = "high",
    max_tokens: int = 16000,  # acima disso o SDK exige streaming
    max_iteracoes: int = 20,
    cliente: Any = None,
    parametros_servidor: Any = None,
    cliente_rtc: RtcClient | None = None,
    progresso: Any = None,
    registro: Registro | None = None,
) -> ResultadoConsulta:
    """Responde a uma pergunta e confere as citações contra a base oficial."""
    from anthropic import AsyncAnthropic
    from anthropic.lib.tools.mcp import async_mcp_tool
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    data = data or date.today().isoformat()
    rtc = cliente_rtc or RtcClient()
    try:
        trechos, metadados = await carregar_base(rtc, data)
    except ErroConsulta as exc:
        raise ErroAgente(str(exc)) from exc
    finally:
        if cliente_rtc is None:
            await rtc.aclose()
    sistema = montar_sistema(formatar_base(trechos), metadados)

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
                parecer = await _executar(
                    cliente, sessao, async_mcp_tool, sistema, f"Data de referência: {data}\nPergunta: {pergunta}",
                    modelo, esforco, max_tokens, max_iteracoes, progresso, registro,
                )
            except Exception as exc:  # noqa: BLE001 - relançado fora da sessão MCP
                erro = exc
    except Exception as falha:
        if erro is None:
            causa = getattr(falha, "exceptions", [falha])[0]
            raise ErroAgente(f"Falha no servidor MCP rtc-mcp: {causa!r}") from falha
    if erro is not None:
        raise erro

    problemas = conferir_citacoes(
        parecer.resposta, [c.model_dump() for c in parecer.citacoes], parecer.baseSuficiente, trechos
    )
    revisao = bool(problemas) or not parecer.baseSuficiente or parecer.confianca != "alta"
    return ResultadoConsulta(
        parecer=parecer, problemas=problemas, revisaoHumanaRecomendada=revisao, base=metadados, modelo=modelo
    )


def formatar(r: ResultadoConsulta, trechos_por_id: dict[str, Any] | None = None) -> str:
    p = r.parecer
    linhas = [p.resposta, ""]
    if p.citacoes:
        linhas.append("Fundamentação:")
        for c in p.citacoes:
            linhas.append(f'  [{c.id}] "{c.trecho}"')
    linhas.append(
        f"Base: fundamentação legal da calculadora, versão {r.base.get('versaoBaseDados')} "
        f"({r.base.get('dataVersaoBase')}), vigente em {r.base.get('data')}"
    )
    linhas.append(f"Confiança: {p.confianca}  |  base suficiente: {'sim' if p.baseSuficiente else 'não'}")
    for titulo, itens in (("Limitações", p.limitacoes), ("Perguntas", p.perguntas), ("Problemas nas citações", r.problemas)):
        if itens:
            linhas.append(f"{titulo}:")
            linhas.extend(f"  - {i}" for i in itens)
    linhas.append("Não substitui parecer profissional para o caso concreto.")
    linhas.append("REVISÃO HUMANA RECOMENDADA" if r.revisaoHumanaRecomendada else "Citações conferidas na base oficial.")
    return "\n".join(linhas)


def main() -> None:
    import anthropic

    parser = argparse.ArgumentParser(description="Responde perguntas sobre IBS/CBS citando a fundamentação legal oficial")
    parser.add_argument("pergunta")
    parser.add_argument("--data", help="Data de referência da legislação (AAAA-MM-DD); padrão: hoje")
    parser.add_argument("--modelo", default=MODELO_PADRAO)
    parser.add_argument("--esforco", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--json", action="store_true", help="Imprime o resultado em JSON")
    parser.add_argument("--silencioso", action="store_true", help="Não mostra as ferramentas chamadas")
    args = parser.parse_args()
    try:
        resultado = asyncio.run(
            consultar(args.pergunta, data=args.data, modelo=args.modelo, esforco=args.esforco,
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
