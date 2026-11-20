"""Agente comparador de regimes: Simples Nacional x apuração de IBS/CBS pelo regime regular.

Recebe a situação da empresa em linguagem natural, monta o cenário (itens classificados, atividade do
Simples, alíquotas efetivas, perfil de clientes e créditos das compras), roda a comparação nas
calculadoras oficiais e escreve um parecer com recomendação, premissas e ressalvas.

Os números do parecer não vêm do modelo: depois da resposta, o código roda a comparação de novo com o
cenário final e anexa o resultado das calculadoras. Uma recomendação que contraria esses números, num
caso em que eles bastam para decidir, vai para revisão humana.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .classificador import ErroClassificador as ErroAgente
from .classificador import Registro, _SessaoRegistrada, _texto_final
from .client import RtcClient
from .comparador import Cenario, ErroComparacao, comparar

MODELO_PADRAO = "claude-opus-5-5"

FERRAMENTAS = (
    "consultar_ncm",
    "consultar_nbs",
    "listar_classificacoes_aplicaveis_ncm",
    "listar_classificacoes_por_nbs",
    "consultar_classificacao_tributaria",
    "listar_atividades_simples_nacional",
    "consultar_atividade_simples_nacional",
    "listar_municipios",
    "comparar_regimes_simples_regular",
)

SISTEMA = """\
Você é um consultor tributário especializado na Reforma Tributária do Consumo (LC 214/2025). Ajuda
empresas do Simples Nacional a decidir, a partir de 2027, entre recolher o IBS e a CBS dentro do DAS
(alíquotas efetivas do Simples) ou apurá-los pelo regime regular, "por fora" do DAS, com débitos e
créditos.

Os números vêm só da ferramenta comparar_regimes_simples_regular, que roda as calculadoras oficiais.
Nunca calcule tributo por conta própria, nem estime alíquotas do Simples.

Método:
1. Monte o cenário: as vendas de um período típico (um mês, por exemplo), item a item, com valor,
   NCM ou NBS, CST e cClassTrib; a atividade do Simples (cAtivSN) de cada item; UF e município; a data
   do fato gerador (2027 em diante).
   - Classifique os itens com consultar_ncm/consultar_nbs e listar_classificacoes_aplicaveis_ncm ou
     listar_classificacoes_por_nbs. Na dúvida sobre um tratamento específico, use a tributação integral
     (CST 000, cClassTrib 000001) e registre isso em premissas.
   - cAtivSN: listar_atividades_simples_nacional na data. Códigos de município: listar_municipios.
2. Dados que só o usuário tem. Não invente nenhum deles:
   - pIBSSN e pCBSSN: as alíquotas efetivas de IBS e CBS no Simples, do PGDAS ou do contador. Sem elas
     não há comparação: responda com status "precisa_informacao" e peça-as.
   - Perfil dos clientes (contribuintes do regime regular, consumidores finais ou misto): decide se o
     crédito do cliente importa. Se não for informado, registre como "nao_informado" e pergunte.
   - IBS/CBS pagos nas compras do período (créditos no regime regular): se faltar, a ferramenta
     considera zero; registre a premissa e diga que o resultado do regime regular tende a melhorar.
   - Alíquotas nominais: em 2027 e 2028 a ferramenta usa uma premissa padrão; a partir de 2029, peça-as.
3. Rode comparar_regimes_simples_regular. Se a ferramenta apontar itens fora da comparação ou erro,
   corrija o cenário (por exemplo, a classificação) e rode de novo.
4. Analise:
   - Clientes consumidores finais: o crédito não lhes serve; compare o tributo a pagar (devido no
     Simples x saldo a pagar no regular).
   - Clientes contribuintes: o crédito que eles tomam reduz o custo líquido da compra; um crédito maior
     no regime regular pode compensar pagar mais e tornar a empresa mais competitiva. Explique esse
     trade-off com os valores da ferramenta.
   - Itens com redução de alíquota ou alíquota zero (cesta básica, saúde etc.) costumam favorecer o
     regime regular, porque a alíquota efetiva do Simples não reflete a redução; mostre isso por item.
5. Ressalvas obrigatórias: a comparação cobre só IBS/CBS (IRPJ, CSLL, ICMS, ISS e os demais tributos do
   DAS ficam fora); valores de 2027 em diante são simulações; a opção pelo regime regular tem regras e
   prazos próprios que o contador deve confirmar.

Resposta final no esquema JSON pedido, em português. Em "cenario", repita exatamente o último cenário
enviado à ferramenta (ou null se faltou dado essencial). Cite valores só como aparecem na ferramenta.
"""


class Parecer(BaseModel):
    status: Literal["analisado", "precisa_informacao"]
    cenario: Cenario | None = Field(description="O último cenário enviado à ferramenta de comparação")
    perfilClientes: Literal["contribuintes", "consumidores_finais", "misto", "nao_informado"]
    recomendacao: Literal["permanecer_no_simples", "apurar_pelo_regime_regular", "indiferente", "inconclusivo"]
    analise: str = Field(description="Explicação da comparação, citando os valores da ferramenta")
    fatoresDecisivos: list[str]
    premissas: list[str]
    perguntas: list[str] = Field(description="Dados que faltam e podem mudar a recomendação")
    ressalvas: list[str]


class ResultadoComparacao(BaseModel):
    parecer: Parecer
    comparacao: dict[str, Any] | None = Field(description="Resultado das calculadoras para o cenário final")
    verificacoes: list[str]
    revisaoHumanaRecomendada: bool
    modelo: str


def formato_resposta() -> dict[str, Any]:
    from anthropic import transform_schema

    return {"type": "json_schema", "schema": transform_schema(Parecer.model_json_schema())}


def mais_barato(comparacao: dict[str, Any], tolerancia: Decimal = Decimal("0.01")) -> str:
    """Opção com menor IBS/CBS a pagar; 'indiferente' se a diferença ficar abaixo de 1% das vendas."""
    t = comparacao["totais"]
    diferenca = Decimal(t["diferencas"]["tributoAPagarRegularMenosSimples"])
    if abs(diferenca) <= Decimal(t["valorVendas"]) * tolerancia:
        return "indiferente"
    return "apurar_pelo_regime_regular" if diferenca < 0 else "permanecer_no_simples"


def verificar(parecer: Parecer, comparacao: dict[str, Any] | None) -> list[str]:
    """Confere o parecer contra os números das calculadoras. Devolve os problemas encontrados."""
    problemas: list[str] = []
    if parecer.status == "analisado" and comparacao is None:
        problemas.append("Parecer 'analisado' sem cenário calculável.")
    if comparacao and comparacao["problemas"]:
        problemas.append("Há itens fora da comparação: " + " | ".join(comparacao["problemas"]))
    if comparacao and parecer.perfilClientes == "consumidores_finais":
        # Sem crédito em jogo, o tributo a pagar decide; a recomendação deve acompanhar os números.
        esperado = mais_barato(comparacao)
        if parecer.recomendacao not in (esperado, "inconclusivo"):
            problemas.append(
                f"Recomendação '{parecer.recomendacao}' contraria os números para clientes consumidores "
                f"finais (menor tributo a pagar: '{esperado}')."
            )
    return problemas


async def _executar(
    cliente: Any,
    sessao: Any,
    async_mcp_tool: Any,
    pedido: str,
    modelo: str,
    esforco: str,
    max_tokens: int,
    max_iteracoes: int,
    progresso: Any,
    registro: Registro | None,
) -> Parecer:
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
        return Parecer.model_validate_json(_texto_final(ultima))
    except ValidationError as exc:
        raise ErroAgente(f"Resposta final fora do esquema esperado: {exc}") from exc


async def comparar_regimes(
    pedido: str,
    *,
    modelo: str = MODELO_PADRAO,
    esforco: str = "high",
    max_tokens: int = 16000,  # acima disso o SDK exige streaming
    max_iteracoes: int = 30,
    cliente: Any = None,
    parametros_servidor: Any = None,
    cliente_rtc: RtcClient | None = None,
    progresso: Any = None,
    registro: Registro | None = None,
) -> ResultadoComparacao:
    """Responde a um pedido de comparação. Os parâmetros opcionais permitem injeção nos testes."""
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
            log = sys.stderr if os.environ.get("RTC_DEBUG") else pilha.enter_context(open(os.devnull, "w"))
            leitura, escrita = await pilha.enter_async_context(stdio_client(parametros_servidor, errlog=log))
            sessao = await pilha.enter_async_context(ClientSession(leitura, escrita))
            await sessao.initialize()
            try:
                parecer = await _executar(
                    cliente, sessao, async_mcp_tool, pedido, modelo, esforco, max_tokens, max_iteracoes,
                    progresso, registro,
                )
            except Exception as exc:  # noqa: BLE001 - relançado fora da sessão MCP
                erro = exc
    except Exception as falha:  # servidor MCP não iniciou ou caiu
        if erro is None:
            causa = getattr(falha, "exceptions", [falha])[0]
            raise ErroAgente(f"Falha no servidor MCP rtc-mcp: {causa!r}") from falha
    if erro is not None:
        raise erro

    # Números autoritativos: o código roda a comparação de novo com o cenário final.
    comparacao, verificacoes = None, []
    if parecer.cenario is not None:
        rtc = cliente_rtc or RtcClient()
        try:
            comparacao = await comparar(rtc, parecer.cenario)
        except ErroComparacao as exc:
            verificacoes.append(f"O cenário final não pôde ser calculado: {exc}")
        finally:
            if cliente_rtc is None:
                await rtc.aclose()
    verificacoes += verificar(parecer, comparacao)
    revisao = (
        bool(verificacoes)
        or parecer.status != "analisado"
        or parecer.recomendacao == "inconclusivo"
        or parecer.perfilClientes == "nao_informado"
    )
    return ResultadoComparacao(
        parecer=parecer,
        comparacao=comparacao,
        verificacoes=verificacoes or ["Números do parecer recalculados nas calculadoras oficiais."],
        revisaoHumanaRecomendada=revisao,
        modelo=modelo,
    )


ROTULOS = {
    "permanecer_no_simples": "Permanecer com IBS/CBS no Simples (DAS)",
    "apurar_pelo_regime_regular": "Apurar IBS/CBS pelo regime regular",
    "indiferente": "Indiferente para IBS/CBS",
    "inconclusivo": "Inconclusivo",
}


def formatar(r: ResultadoComparacao) -> str:
    p = r.parecer
    linhas = [f"Recomendação: {ROTULOS[p.recomendacao]}  |  Clientes: {p.perfilClientes}  |  {p.status}", ""]
    if r.comparacao:
        t = r.comparacao["totais"]
        sn, rg, dif = t["simples"], t["regimeRegular"], t["diferencas"]
        linhas += [
            f"Vendas comparadas: R$ {t['valorVendas']}",
            f"{'':32}{'Simples':>12}{'Regular':>12}",
            f"{'IBS/CBS sobre as vendas':32}{sn['devido']:>12}{rg['debito']:>12}",
            f"{'(-) créditos das compras':32}{'-':>12}{rg['creditosCompras']:>12}",
            f"{'= a pagar':32}{sn['devido']:>12}{rg['saldoAPagar']:>12}",
            f"{'Crédito do cliente contribuinte':32}{sn['creditoParaClienteContribuinte']:>12}"
            f"{rg['creditoParaClienteContribuinte']:>12}",
            f"Diferença a pagar (regular - Simples): R$ {dif['tributoAPagarRegularMenosSimples']}",
            "",
        ]
    linhas += ["Análise:", p.analise, ""]
    for titulo, itens in (
        ("Fatores decisivos", p.fatoresDecisivos),
        ("Premissas", p.premissas + (r.comparacao["premissas"] if r.comparacao else [])),
        ("Perguntas", p.perguntas),
        ("Ressalvas", p.ressalvas + ([r.comparacao["escopo"]] if r.comparacao else [])),
        ("Conferência", r.verificacoes),
    ):
        unicos = list(dict.fromkeys(itens))
        if unicos:
            linhas.append(f"{titulo}:")
            linhas.extend(f"  - {i}" for i in unicos)
    linhas.append("REVISÃO HUMANA RECOMENDADA" if r.revisaoHumanaRecomendada else "Parecer conferido.")
    return "\n".join(linhas)


def main() -> None:
    import anthropic

    parser = argparse.ArgumentParser(
        description="Compara IBS/CBS no Simples Nacional e no regime regular com o Claude e as calculadoras RTC"
    )
    parser.add_argument("pedido", help="Situação da empresa: o que vende, valores, clientes, alíquotas do PGDAS...")
    parser.add_argument("--modelo", default=MODELO_PADRAO)
    parser.add_argument("--esforco", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--json", action="store_true", help="Imprime o resultado em JSON")
    parser.add_argument("--silencioso", action="store_true", help="Não mostra as ferramentas chamadas")
    args = parser.parse_args()
    try:
        resultado = asyncio.run(
            comparar_regimes(
                args.pedido,
                modelo=args.modelo,
                esforco=args.esforco,
                progresso=None if args.silencioso else (lambda t: print(t, file=sys.stderr)),
            )
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
        print(
            "Erro: nenhuma credencial da API da Anthropic encontrada. Defina ANTHROPIC_API_KEY "
            "ou faça login com `ant auth login`.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(resultado.model_dump_json(indent=2) if args.json else formatar(resultado))
    sys.exit(2 if resultado.revisaoHumanaRecomendada else 0)


if __name__ == "__main__":
    main()
