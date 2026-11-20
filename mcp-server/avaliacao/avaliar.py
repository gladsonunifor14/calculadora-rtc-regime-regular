"""Avaliação do agente classificador fiscal (rtc-classificador).

Roda o classificador real sobre os casos de casos.jsonl, dá nota a cada saída de forma programática
e grava os resultados no formato do relatório de avaliação:

    <flow>/<variante>/results.jsonl        uma linha por (caso, repetição) com nota e métricas
    <flow>/<variante>/traces/<id>_rep<k>.json   transcrição completa
    <flow>/<variante>/errors.jsonl         tentativas que não produziram saída avaliável

Uso (calculadora do regime regular em execução e credencial da Anthropic configurada):

    python avaliacao/avaliar.py --aprovar-harness          # só na primeira vez ou após mudar o harness
    python avaliacao/avaliar.py --reps 2
    python avaliacao/avaliar.py --casos arroz-varejo,dipirona --reps 1   # piloto

A execução é retomável: (caso, repetição) já gravados em results.jsonl são pulados.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import sys
import time
from pathlib import Path
from typing import Any

AQUI = Path(__file__).resolve().parent
RAIZ_PACOTE = AQUI.parent  # mcp-server/
RAIZ_REPO = RAIZ_PACOTE.parent
FLOW_PADRAO = RAIZ_REPO / ".claude" / "hillclimb" / "classificador"

# Arquivos cujo conteúdo define o experimento. Mudou algum, a avaliação exige nova aprovação.
ARQUIVOS_HARNESS = [
    "avaliacao/avaliar.py",
    "avaliacao/casos.jsonl",
    "src/rtc_mcp/classificador.py",
    "src/rtc_mcp/server.py",
    "src/rtc_mcp/client.py",
]

METRICAS = [
    {"id": "acerto", "label": "acerto", "kind": "binary"},
    {"id": "acerto_definidos", "label": "acerto defin.", "kind": "binary"},
    {"id": "sinalizou_ambiguos", "label": "sinal. ambíg.", "kind": "binary"},
    {"id": "revisao_indevida", "label": "rev. indevida", "kind": "binary", "better": "lower"},
    {"id": "cst_ok", "label": "CST ok", "kind": "binary"},
    {"id": "nomenclatura_ok", "label": "NCM/NBS ok", "kind": "binary"},
    {"id": "conferencia_ok", "label": "conferência", "kind": "binary"},
    {"id": "recusa", "label": "recusa", "kind": "binary", "better": "lower"},
]
PERF_FIELDS = [
    {"id": "latency_s", "label": "latência", "unit": "s"},
    {"id": "tool_calls", "label": "ferramentas"},
    {"id": "in_tokens", "label": "tokens in"},
    {"id": "out_tokens", "label": "tokens out"},
    {"id": "cost_usd", "label": "custo", "unit": "$"},
]
# US$ por milhão de tokens (tabela de preços da Anthropic, API direta).
PRECOS = {
    "claude-opus-5-5": {"in": 4.0, "out": 20.0},
    "claude-sonnet-5-5": {"in": 2.0, "out": 10.0},
    "claude-haiku-5-5": {"in": 0.10, "out": 0.50},
}


# ----------------------------------------------------------------------
# Nota
# ----------------------------------------------------------------------


def _nomenclatura_ok(caso: dict[str, Any], c: dict[str, Any]) -> float | None:
    """NCM: mesma posição (4 dígitos), ou código idêntico quando o usuário o informou. NBS: mesmos 5 dígitos."""
    g = caso["gabarito"]
    if g.get("ncm"):
        obtido = (c.get("ncm") or "").replace(".", "")
        if caso.get("ncm_informada"):
            return float(obtido == caso["ncm_informada"])
        return float(obtido[:4] == g["ncm"][:4])
    if g.get("nbs"):
        obtido = (c.get("nbs") or "").replace(".", "")
        if caso.get("nbs_informada"):
            return float(obtido == caso["nbs_informada"])
        return float(obtido[:5] == g["nbs"][:5])
    return None


def dar_nota(caso: dict[str, Any], resultado: dict[str, Any]) -> tuple[dict[str, float | None], dict[str, str]]:
    """Nota de uma saída do classificador (ResultadoClassificacao em dict). Métricas que não se aplicam = None."""
    c = resultado["classificacao"]
    g = caso["gabarito"]
    revisao = bool(resultado["revisaoHumanaRecomendada"])
    conferencia_ok = resultado["verificacoes"] == ["CST, cClassTrib e NCM/NBS confirmados na calculadora."]
    explicacao: dict[str, str] = {}

    if caso["tipo"] == "definido":
        acerto = float(c.get("cClassTrib") in g["aceitas"])
        nota = {
            "acerto": acerto,
            "acerto_definidos": acerto,
            "sinalizou_ambiguos": None,
            "revisao_indevida": float(revisao and bool(acerto)),
            "cst_ok": float(c.get("cst") == g["cst"]),
        }
        explicacao["acerto"] = f"obtido {c.get('cClassTrib')}, aceitas {g['aceitas']}"
    else:
        proposta_plausivel = c.get("cClassTrib") is None or c.get("cClassTrib") in g["plausiveis"]
        acerto = float(revisao and proposta_plausivel)
        nota = {
            "acerto": acerto,
            "acerto_definidos": None,
            "sinalizou_ambiguos": float(revisao),
            "revisao_indevida": None,
            "cst_ok": None,
        }
        explicacao["acerto"] = (
            f"revisão={'sim' if revisao else 'não'}, proposta {c.get('cClassTrib')}, plausíveis {g['plausiveis']}"
        )
    nota["nomenclatura_ok"] = _nomenclatura_ok(caso, c)
    # Nos ambíguos a resposta certa pode deixar o CST em aberto; a conferência só diz algo nos definidos.
    nota["conferencia_ok"] = float(conferencia_ok) if caso["tipo"] == "definido" else None
    nota["recusa"] = 0.0
    return nota, explicacao


NOTA_RECUSA = {m["id"]: 0.0 for m in METRICAS} | {"recusa": 1.0}


# ----------------------------------------------------------------------
# Harness
# ----------------------------------------------------------------------


def sha_harness() -> str:
    h = hashlib.sha256()
    for rel in ARQUIVOS_HARNESS:
        h.update(rel.encode())
        h.update((RAIZ_PACOTE / rel).read_bytes())
    return h.hexdigest()


def carregar_casos(filtro: set[str] | None) -> list[dict[str, Any]]:
    casos = [json.loads(linha) for linha in (AQUI / "casos.jsonl").read_text().splitlines() if linha.strip()]
    if filtro:
        desconhecidos = filtro - {c["id"] for c in casos}
        if desconhecidos:
            raise SystemExit(f"casos desconhecidos: {sorted(desconhecidos)}")
        casos = [c for c in casos if c["id"] in filtro]
    return casos


def modelo_confere(pedido: str, servido: str | None) -> bool:
    # Aceita a resolução documentada de alias para versão datada (claude-x-y -> claude-x-y-AAAAMMDD).
    return servido is not None and (servido == pedido or servido.startswith(pedido + "-"))


def custo(modelo: str | None, uso: dict[str, int]) -> float | None:
    p = PRECOS.get(modelo or "")
    if not p:
        return None
    return (
        uso["input_tokens"] * p["in"]
        + uso["cache_creation_input_tokens"] * p["in"] * 1.25
        + uso["cache_read_input_tokens"] * p["in"] * 0.1
        + uso["output_tokens"] * p["out"]
    ) / 1e6


class Gravador:
    def __init__(self, pasta: Path):
        self.pasta = pasta
        (pasta / "traces").mkdir(parents=True, exist_ok=True)
        self._trava = asyncio.Lock()

    def feitos(self) -> set[tuple[str, int]]:
        arq = self.pasta / "results.jsonl"
        if not arq.exists():
            return set()
        return {(r["prompt_id"], r["rep"]) for r in map(json.loads, arq.read_text().splitlines()) if r}

    async def resultado(self, linha: dict[str, Any], transcricao: list[dict[str, Any]]) -> None:
        async with self._trava:
            nome = f"{linha['prompt_id']}_rep{linha['rep']}.json"
            (self.pasta / "traces" / nome).write_text(json.dumps(transcricao, ensure_ascii=False, indent=1))
            with (self.pasta / "results.jsonl").open("a") as f:
                f.write(json.dumps(linha, ensure_ascii=False) + "\n")

    async def erro(self, linha: dict[str, Any]) -> None:
        async with self._trava:
            with (self.pasta / "errors.jsonl").open("a") as f:
                f.write(json.dumps(linha, ensure_ascii=False) + "\n")


def _prompt(caso: dict[str, Any]) -> str:
    partes = [f"Item: {caso['descricao']}", f"Contexto: {caso['contexto']}"]
    if caso.get("ncm_informada"):
        partes.append(f"NCM informada: {caso['ncm_informada']}")
    if caso.get("nbs_informada"):
        partes.append(f"NBS informada: {caso['nbs_informada']}")
    return "\n".join(partes)


async def rodar_caso(
    caso: dict[str, Any],
    rep: int,
    args: argparse.Namespace,
    gravador: Gravador,
    limite: asyncio.Semaphore,
    cliente_fabrica: Any,
    parametros_servidor: Any,
) -> str:
    import anthropic

    from rtc_mcp.classificador import ErroClassificador, Registro, classificar

    base = {"prompt_id": caso["id"], "rep": rep, "model": args.modelo}
    tentativas = 0
    async with limite:
        while True:
            tentativas += 1
            registro = Registro()
            inicio = time.monotonic()
            try:
                resultado = await asyncio.wait_for(
                    classificar(
                        caso["descricao"],
                        data=caso["data"],
                        ncm=caso.get("ncm_informada"),
                        nbs=caso.get("nbs_informada"),
                        contexto=caso["contexto"],
                        modelo=args.modelo,
                        esforco=args.esforco,
                        cliente=cliente_fabrica(),
                        parametros_servidor=parametros_servidor,
                        registro=registro,
                    ),
                    timeout=args.timeout_s,
                )
                falha, mensagem = None, None
            except asyncio.TimeoutError:
                await gravador.erro(base | _uso(registro) | {"classe": "timeout", "tentativas": tentativas})
                return "erro"
            except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError) as exc:
                if tentativas < 4:
                    await asyncio.sleep(min(60, 2**tentativas) * (0.5 + random.random()))
                    continue
                await gravador.erro(
                    base | _uso(registro) | {"classe": "api", "tentativas": tentativas, "detalhe": repr(exc)[:300]}
                )
                return "erro"
            except ErroClassificador as exc:
                falha, mensagem, resultado = _classe_falha(str(exc)), str(exc), None
                if falha == "harness":
                    await gravador.erro(
                        base | _uso(registro) | {"classe": "harness", "tentativas": tentativas, "detalhe": mensagem}
                    )
                    return "erro"
            except Exception as exc:  # noqa: BLE001 - qualquer outra falha é do harness, não do modelo
                await gravador.erro(
                    base | _uso(registro) | {"classe": "harness", "tentativas": tentativas, "detalhe": repr(exc)[:300]}
                )
                return "erro"
            break

    latencia = round(time.monotonic() - inicio, 2)
    servidos = [m for m in registro.modelos if m]
    divergentes = sorted({m for m in servidos if not modelo_confere(args.modelo, m)})
    if divergentes:
        await gravador.erro(
            base | _uso(registro) | {"classe": "modelo_servido_diferente", "servidos": divergentes, "tentativas": tentativas}
        )
        return "erro"

    status, stop = "ok", (registro.stop_reasons[-1] if registro.stop_reasons else None)
    if falha == "truncado":
        status = "truncated"
        nota, explicacao = {m["id"]: None for m in METRICAS}, {"acerto": mensagem}
    elif falha == "recusa":
        nota, explicacao = dict(NOTA_RECUSA), {"acerto": mensagem}
    elif falha is not None:  # falha genuína do agente: limite de iterações, resposta fora do esquema
        nota, explicacao = {m["id"]: 0.0 for m in METRICAS}, {"acerto": mensagem}
        nao_aplicaveis = ("acerto_definidos", "revisao_indevida", "cst_ok", "conferencia_ok")
        for m in nao_aplicaveis if caso["tipo"] == "ambiguo" else ("sinalizou_ambiguos",):
            nota[m] = None
    else:
        nota, explicacao = dar_nota(caso, resultado.model_dump())

    uso = registro.uso
    linha = {
        "prompt_id": caso["id"],
        "prompt": _prompt(caso),
        "tags": [caso["tipo"], caso["tema"]],
        "rep": rep,
        "model": servidos[-1] if servidos else args.modelo,
        "status": status,
        "stop_reason": stop,
        "grade": nota,
        "explanation": explicacao,
        "latency_s": latencia,
        "tool_calls": registro.chamadas_ferramenta,
        "in_tokens": uso["input_tokens"] + uso["cache_read_input_tokens"] + uso["cache_creation_input_tokens"],
        "out_tokens": uso["output_tokens"],
        "usage": uso,
        "cost_usd": custo(args.modelo, uso),
        "meta": {
            "tentativas": tentativas,
            "falha": falha,
            "saida": resultado.model_dump() if resultado is not None else None,
            "gabarito": caso["gabarito"],
        },
    }
    if status == "ok" and (not uso["output_tokens"] or not servidos):
        # Sem uso ou sem modelo registrado numa execução bem-sucedida = defeito do harness, não nota.
        await gravador.erro(base | {"classe": "harness", "detalhe": "execução sem usage/model registrados"})
        return "erro"
    await gravador.resultado(linha, registro.transcricao)
    return status


def _classe_falha(mensagem: str) -> str:
    if "recusou" in mensagem:
        return "recusa"
    if "max_tokens" in mensagem:
        return "truncado"
    if "servidor MCP" in mensagem:
        return "harness"
    return "agente"


def _uso(registro: Any) -> dict[str, Any]:
    return {"usage": dict(registro.uso), "modelos_servidos": [m for m in registro.modelos if m]}


# ----------------------------------------------------------------------
# Resumo
# ----------------------------------------------------------------------


def resumo(pasta: Path) -> str:
    arq = pasta / "results.jsonl"
    linhas = [json.loads(x) for x in arq.read_text().splitlines()] if arq.exists() else []
    erros = (pasta / "errors.jsonl").read_text().splitlines() if (pasta / "errors.jsonl").exists() else []
    ok = [r for r in linhas if r["status"] == "ok"]
    saida = [f"{len({r['prompt_id'] for r in linhas})} casos, {len(linhas)} execuções avaliadas "
             f"({len(linhas) - len(ok)} truncadas), {len(erros)} tentativas com erro (fora das médias)"]
    for m in METRICAS:
        valores = [r["grade"][m["id"]] for r in ok if r["grade"].get(m["id"]) is not None]
        if not valores:
            continue
        media = sum(valores) / len(valores)
        meia = 1.96 * math.sqrt(max(media * (1 - media), 1e-9) / len(valores))
        seta = " (menor é melhor)" if m.get("better") == "lower" else ""
        saida.append(f"  {m['id']:<20} {media:6.1%} ± {meia:.1%}  (n={len(valores)}){seta}")
    custos = [r["cost_usd"] for r in ok if r.get("cost_usd") is not None]
    lat = sorted(r["latency_s"] for r in ok)
    if custos:
        saida.append(f"  custo total US$ {sum(custos):.2f}  (US$ {sum(custos) / len(custos):.3f}/execução)")
    if lat:
        saida.append(f"  latência mediana {lat[len(lat) // 2]:.1f} s, máx {lat[-1]:.1f} s")
    return "\n".join(saida)


def gravar_estado(flow: Path) -> None:
    estado = flow / "_state.json"
    if not estado.exists():
        flow.mkdir(parents=True, exist_ok=True)
        estado.write_text(
            json.dumps(
                {"metrics": METRICAS, "perf_fields": PERF_FIELDS, "harness_paths": ARQUIVOS_HARNESS},
                ensure_ascii=False,
                indent=2,
            )
        )


async def principal(args: argparse.Namespace, cliente_fabrica: Any = None, parametros_servidor: Any = None) -> int:
    flow = Path(args.flow)
    gravar_estado(flow)
    aprovado = flow / "harness.sha"
    atual = sha_harness()
    if args.aprovar_harness:
        aprovado.write_text(atual + "\n")
        print(f"Harness aprovado ({atual[:12]}).")
        return 0
    if not aprovado.exists() or aprovado.read_text().strip() != atual:
        print(
            "O harness mudou desde a última aprovação (ou nunca foi aprovado). Revise as mudanças em "
            f"{', '.join(ARQUIVOS_HARNESS)} e rode de novo com --aprovar-harness.",
            file=sys.stderr,
        )
        return 2

    if cliente_fabrica is None:
        from anthropic import AsyncAnthropic

        cliente_fabrica = AsyncAnthropic

    casos = carregar_casos(set(args.casos.split(",")) if args.casos else None)
    gravador = Gravador(flow / args.variante)
    feitos = gravador.feitos()
    pendentes = [(c, r) for c in casos for r in range(args.reps) if (c["id"], r) not in feitos]
    print(f"{len(pendentes)} execuções pendentes ({len(feitos)} já gravadas) em {flow / args.variante}")
    limite = asyncio.Semaphore(args.concorrencia)
    inicio = time.monotonic()
    situacoes = await asyncio.gather(
        *(rodar_caso(c, r, args, gravador, limite, cliente_fabrica, parametros_servidor) for c, r in pendentes)
    )
    print(f"Concluído em {time.monotonic() - inicio:.0f} s: {sum(s == 'ok' for s in situacoes)} ok, "
          f"{sum(s == 'erro' for s in situacoes)} erros, {sum(s == 'truncated' for s in situacoes)} truncadas")
    print(resumo(flow / args.variante))
    return 0


def argumentos(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Avaliação do agente classificador fiscal")
    p.add_argument("--variante", default="baseline", help="baseline, v1, v2, ...")
    p.add_argument("--modelo", default="claude-opus-5-5")
    p.add_argument("--esforco", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--reps", type=int, default=2)
    p.add_argument("--casos", help="Lista de ids separados por vírgula (piloto)")
    p.add_argument("--concorrencia", type=int, default=4)
    p.add_argument("--timeout-s", type=float, default=600, help="Teto de tempo por execução")
    p.add_argument("--flow", default=str(FLOW_PADRAO))
    p.add_argument("--aprovar-harness", action="store_true", help="Registra o estado atual do harness como aprovado")
    p.add_argument("--resumo", action="store_true", help="Só imprime o resumo da variante")
    a = p.parse_args(argv)
    if a.variante != "baseline" and not (a.variante.startswith("v") and a.variante[1:].isdigit()):
        p.error("--variante deve ser 'baseline' ou 'v<N>'")
    return a


def main() -> None:
    sys.path.insert(0, str(RAIZ_PACOTE / "src"))
    args = argumentos()
    if args.resumo:
        print(resumo(Path(args.flow) / args.variante))
        return
    sys.exit(asyncio.run(principal(args)))


if __name__ == "__main__":
    main()
