"""Diagnóstico e conferência de correções de operações do regime regular.

Uma operação de cálculo (o JSON enviado a /regime-geral, montado por um ERP, por exemplo) pode voltar
com erros por item (REG-011, REG-006, REG-002, REG-037...) ou ser rejeitada inteira (HTTP 422, como
REG-018 e VAL-001). Este módulo:

- diagnostica a operação na calculadora e devolve os erros estruturados, com a descrição de cada código;
- compara a operação original com a corrigida campo a campo (ponteiros JSON, RFC 6901);
- confere se uma correção é legítima: nada de remover ou acrescentar itens, nenhuma mudança fora das
  correções declaradas e nenhum valor monetário, quantidade ou data alterado sem marcação explícita.
  "Corrigir" um erro apagando o item, ou mexendo no valor da venda, não é correção.
"""

from __future__ import annotations

import re
from typing import Any

from .client import CalculadoraError, RtcClient

# Campos que definem o fato econômico da operação: uma correção de classificação não deve mexer neles.
PROTEGIDOS = re.compile(
    r"^/(id|dhFatoGerador|dataHoraEmissao)$"
    r"|^/itens/\d+/(numero|baseCalculo|quantidade|valor\w*)$"
    r"|^/itens/\d+/impostoSeletivo/(baseCalculo|quantidade|impostoInformado)$"
)


class ErroCorrecao(Exception):
    """A operação não pôde ser diagnosticada; a mensagem explica o motivo."""


def _ponteiro_item(ponteiro: str | None) -> int | None:
    m = re.match(r"^#?/itens/(-?\d+)", ponteiro or "")
    if not m or int(m.group(1)) < 0:
        return None
    return int(m.group(1))


async def diagnosticar(cliente: RtcClient, operacao: dict[str, Any]) -> dict[str, Any]:
    """Roda a operação na calculadora (observabilidade) e devolve os erros estruturados."""
    try:
        r = await cliente.regime_geral(
            "POST", "calculadora/observabilidade/regime-geral", json_body=operacao, aceitar_422=True
        )
    except CalculadoraError as exc:
        raise ErroCorrecao(str(exc)) from exc

    dados = r["dados"] if isinstance(r["dados"], dict) else {}
    erros: list[dict[str, Any]] = []
    itens = operacao.get("itens") if isinstance(operacao.get("itens"), list) else []
    if r.get("status") in (400, 422):
        lista = dados.get("errors") or dados.get("objects") or []
        if not lista:
            lista = [{"code": None, "pointer": None, "detail": dados.get("detail") or dados.get("title")}]
        for e in lista:
            ponteiro = e.get("pointer") or (f"#/{e['name']}" if e.get("name") else None)
            indice = _ponteiro_item(ponteiro)
            erros.append({
                "escopo": "operacao" if indice is None else "item",
                "item": itens[indice].get("numero") if indice is not None and indice < len(itens) else None,
                "codigo": e.get("code"),
                "ponteiro": ponteiro,
                "detalhe": e.get("detail") or e.get("userMessage") or e.get("title"),
            })
        calculados = 0
    else:
        calculados = 0
        for objeto in dados.get("objetos") or []:
            if objeto.get("estadoItem") in ("CALCULADO", "CALCULO_SIMULADO"):
                calculados += 1
            for e in objeto.get("erros") or []:
                erros.append({
                    "escopo": "item",
                    "item": objeto.get("nObj"),
                    "codigo": e.get("code"),
                    "ponteiro": e.get("pointer"),
                    "detalhe": e.get("detail") or e.get("title"),
                })

    # Descrição de cada código distinto, para orientar a correção.
    descricoes: dict[str, str] = {}
    for codigo in sorted({e["codigo"] for e in erros if e["codigo"]}):
        try:
            d = (await cliente.regime_geral("GET", "calculadora/observabilidade/erros", params={"codigo": codigo}))["dados"]
            descricoes[codigo] = f"{d.get('titulo')}: {d.get('descricao')}" if isinstance(d, dict) else str(d)
        except CalculadoraError:
            pass
    for e in erros:
        e["descricao"] = descricoes.get(e["codigo"])

    return {
        "situacao": "sem_erros" if not erros else "com_erros",
        "rejeitadaInteira": r.get("status") in (400, 422),
        "itens": len(itens),
        "itensCalculados": calculados,
        "erros": erros,
        "metadados": r["metadados"],
    }


def _escapar(chave: str) -> str:
    return chave.replace("~", "~0").replace("/", "~1")


def diferencas(antes: Any, depois: Any, ponteiro: str = "") -> list[dict[str, Any]]:
    """Diferenças entre dois JSON, como ponteiros RFC 6901 com o valor anterior e o novo."""
    if isinstance(antes, dict) and isinstance(depois, dict):
        saida: list[dict[str, Any]] = []
        for chave in sorted(set(antes) | set(depois)):
            filho = f"{ponteiro}/{_escapar(str(chave))}"
            if chave not in depois:
                saida.append({"ponteiro": filho, "antes": antes[chave], "depois": None, "tipo": "removido"})
            elif chave not in antes:
                saida.append({"ponteiro": filho, "antes": None, "depois": depois[chave], "tipo": "incluido"})
            else:
                saida += diferencas(antes[chave], depois[chave], filho)
        return saida
    if isinstance(antes, list) and isinstance(depois, list) and len(antes) == len(depois):
        saida = []
        for i, (a, d) in enumerate(zip(antes, depois)):
            saida += diferencas(a, d, f"{ponteiro}/{i}")
        return saida
    if antes != depois:
        return [{"ponteiro": ponteiro or "/", "antes": antes, "depois": depois, "tipo": "alterado"}]
    return []


def _normalizar(ponteiro: str) -> str:
    return "/" + ponteiro.lstrip("#").lstrip("/")


def conferir(
    original: dict[str, Any],
    corrigida: dict[str, Any],
    ponteiros_declarados: list[str],
    diagnostico_final: dict[str, Any],
    pendencias_declaradas: list[str],
) -> dict[str, Any]:
    """Confere a correção. Devolve as violações, as alterações protegidas e o diff completo."""
    violacoes: list[str] = []
    itens_antes = original.get("itens") if isinstance(original.get("itens"), list) else []
    itens_depois = corrigida.get("itens") if isinstance(corrigida.get("itens"), list) else []
    numeros_antes = [i.get("numero") for i in itens_antes if isinstance(i, dict)]
    numeros_depois = [i.get("numero") for i in itens_depois if isinstance(i, dict)]
    if numeros_antes != numeros_depois:
        violacoes.append(
            f"Itens removidos, acrescentados ou reordenados: antes {numeros_antes}, depois {numeros_depois}. "
            "Corrigir não pode mudar a composição da operação."
        )

    diff = diferencas(original, corrigida)
    declarados = [_normalizar(p) for p in ponteiros_declarados]
    nao_declaradas = [
        d["ponteiro"] for d in diff
        if not any(d["ponteiro"] == p or d["ponteiro"].startswith(p + "/") for p in declarados)
    ]
    if nao_declaradas:
        violacoes.append("Alterações não declaradas nas correções: " + ", ".join(nao_declaradas))
    protegidas = [d for d in diff if PROTEGIDOS.match(d["ponteiro"])]

    pendentes = set(pendencias_declaradas)
    residuais = [e for e in diagnostico_final["erros"] if e["codigo"] not in pendentes]
    if residuais:
        violacoes.append(
            "A operação corrigida ainda tem erros não declarados como pendência: "
            + "; ".join(f"{e['codigo']} {e['detalhe']}" for e in residuais)
        )
    return {"violacoes": violacoes, "alteracoesProtegidas": protegidas, "diferencas": diff}
