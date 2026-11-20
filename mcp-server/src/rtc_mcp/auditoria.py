"""Auditoria determinística do IBS/CBS de uma NF-e ou NFC-e.

Lê o XML da nota, extrai de cada item o grupo IBSCBS declarado pelo emitente e confere com as
calculadoras oficiais:

1. estrutura: o XML contra o XSD oficial (validação da calculadora);
2. classificação: a cClassTrib existe, pertence ao CST e aceita a NCM (erros REG-xxx do cálculo);
3. base de cálculo: o vBC declarado contra a base recalculada a partir dos componentes do item
   (vProd, vFrete, vSeg, vOutro, vDesc, vICMS etc.);
4. valores: reduções, alíquotas efetivas e valores de IBS UF, IBS municipal e CBS recalculados sobre
   o vBC declarado;
5. totais: a soma dos itens contra o grupo IBSCBSTot.

Nada é calculado aqui: o módulo só extrai campos, chama as calculadoras e compara os resultados.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel

from .client import CalculadoraError, RtcClient

TOLERANCIA = Decimal("0.01")
NS = re.compile(r"^\{[^}]+\}")

Gravidade = Literal["erro", "alerta", "info"]


class Achado(BaseModel):
    gravidade: Gravidade
    tipo: str
    item: int | None
    campo: str | None = None
    declarado: str | None = None
    calculado: str | None = None
    mensagem: str


class ErroAuditoria(Exception):
    """A nota não pôde ser auditada; a mensagem explica o motivo."""


# ----------------------------------------------------------------------
# Leitura do XML
# ----------------------------------------------------------------------


def _sem_ns(elemento: ET.Element) -> ET.Element:
    for e in elemento.iter():
        if isinstance(e.tag, str):
            e.tag = NS.sub("", e.tag)
    return elemento


def ler_xml(xml: str) -> ET.Element:
    """Faz o parse do XML recusando DOCTYPE e entidades (proteção contra XXE e expansão de entidades)."""
    if re.search(r"<!DOCTYPE|<!ENTITY", xml, re.IGNORECASE):
        raise ErroAuditoria("XML com DOCTYPE ou ENTITY não é aceito.")
    try:
        raiz = _sem_ns(ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml))
    except ET.ParseError as exc:
        raise ErroAuditoria(f"XML malformado: {exc}") from exc
    inf = raiz if raiz.tag == "infNFe" else raiz.find(".//infNFe")
    if inf is None:
        raise ErroAuditoria("O XML não contém uma NF-e/NFC-e (elemento infNFe não encontrado).")
    return inf


def _txt(no: ET.Element | None, caminho: str) -> str | None:
    if no is None:
        return None
    e = no.find(caminho)
    return e.text.strip() if e is not None and e.text else None


def _dec(valor: str | None) -> Decimal | None:
    if valor is None:
        return None
    try:
        return Decimal(valor)
    except InvalidOperation:
        return None


def _primeiro_valor(no: ET.Element | None, campo: str) -> str | None:
    """Primeiro elemento com o nome dado em qualquer subgrupo (ex.: vICMS dentro de ICMS00, ICMS20...)."""
    if no is None:
        return None
    e = no.find(f".//{campo}")
    return e.text.strip() if e is not None and e.text else None


def extrair(inf: ET.Element) -> dict[str, Any]:
    ide, emit = inf.find("ide"), inf.find("emit")
    dh = _txt(ide, "dhEmi") or ""
    nota = {
        "chave": (inf.get("Id") or "").removeprefix("NFe") or None,
        "modelo": _txt(ide, "mod"),
        "numero": _txt(ide, "nNF"),
        "dhEmi": dh,
        "data": dh[:10] or None,
        "municipio": _txt(ide, "cMunFG") or _txt(emit, "enderEmit/cMun"),
        "uf": _txt(emit, "enderEmit/UF"),
        "crt": _txt(emit, "CRT"),
        "itens": [],
        "totalIBSCBS": None,
    }
    for det in inf.findall("det"):
        prod, imposto = det.find("prod"), det.find("imposto")
        ibscbs = imposto.find("IBSCBS") if imposto is not None else None
        g = ibscbs.find("gIBSCBS") if ibscbs is not None else None
        componentes = {
            "vProd": _txt(prod, "vProd"),
            "vFrete": _txt(prod, "vFrete"),
            "vSeg": _txt(prod, "vSeg"),
            "vOutro": _txt(prod, "vOutro"),
            "vDesc": _txt(prod, "vDesc"),
            "vII": _txt(imposto, "II/vII"),
            "vIS": _primeiro_valor(imposto.find("IS") if imposto is not None else None, "vIS"),
            "vPIS": _primeiro_valor(imposto.find("PIS") if imposto is not None else None, "vPIS"),
            "vCOFINS": _primeiro_valor(imposto.find("COFINS") if imposto is not None else None, "vCOFINS"),
            "vICMS": _primeiro_valor(imposto.find("ICMS") if imposto is not None else None, "vICMS"),
            "vFCP": _primeiro_valor(imposto.find("ICMS") if imposto is not None else None, "vFCP"),
            "vICMSMono": _primeiro_valor(imposto.find("ICMS") if imposto is not None else None, "vICMSMono"),
            "vICMSUFDest": _txt(imposto, "ICMSUFDest/vICMSUFDest"),
            "vFCPUFDest": _txt(imposto, "ICMSUFDest/vFCPUFDest"),
            "vISSQN": _txt(imposto, "ISSQN/vISSQN"),
        }
        nota["itens"].append(
            {
                "numero": int(det.get("nItem") or len(nota["itens"]) + 1),
                "descricao": _txt(prod, "xProd"),
                "ncm": _txt(prod, "NCM"),
                "cfop": _txt(prod, "CFOP"),
                "quantidade": _txt(prod, "qCom"),
                "unidade": _txt(prod, "uCom"),
                "servico": imposto is not None and imposto.find("ISSQN") is not None,
                "temIS": imposto is not None and imposto.find("IS") is not None,
                "componentes": {k: v for k, v in componentes.items() if v is not None},
                "ibscbs": None
                if ibscbs is None
                else {
                    "CST": _txt(ibscbs, "CST"),
                    "cClassTrib": _txt(ibscbs, "cClassTrib"),
                    "vBC": _txt(g, "vBC"),
                    "pIBSUF": _txt(g, "gIBSUF/pIBSUF"),
                    "pRedAliqIBSUF": _txt(g, "gIBSUF/gRed/pRedAliq"),
                    "pAliqEfetIBSUF": _txt(g, "gIBSUF/gRed/pAliqEfet"),
                    "vIBSUF": _txt(g, "gIBSUF/vIBSUF"),
                    "pIBSMun": _txt(g, "gIBSMun/pIBSMun"),
                    "pRedAliqIBSMun": _txt(g, "gIBSMun/gRed/pRedAliq"),
                    "pAliqEfetIBSMun": _txt(g, "gIBSMun/gRed/pAliqEfet"),
                    "vIBSMun": _txt(g, "gIBSMun/vIBSMun"),
                    "vIBS": _txt(g, "vIBS"),
                    "pCBS": _txt(g, "gCBS/pCBS"),
                    "pRedAliqCBS": _txt(g, "gCBS/gRed/pRedAliq"),
                    "pAliqEfetCBS": _txt(g, "gCBS/gRed/pAliqEfet"),
                    "vCBS": _txt(g, "gCBS/vCBS"),
                    "temGrupoValores": g is not None,
                },
            }
        )
    tot = inf.find("total/IBSCBSTot")
    if tot is not None:
        nota["totalIBSCBS"] = {
            "vBCIBSCBS": _txt(tot, "vBCIBSCBS"),
            "vIBSUF": _txt(tot, "gIBS/gIBSUF/vIBSUF"),
            "vIBSMun": _txt(tot, "gIBS/gIBSMun/vIBSMun"),
            "vIBS": _txt(tot, "gIBS/vIBS"),
            "vCBS": _txt(tot, "gCBS/vCBS"),
        }
    return nota


def _campos_calculados(objeto: dict[str, Any]) -> dict[str, str | None]:
    """Os mesmos campos de `extrair`, lidos do resultado da calculadora."""
    t = (objeto.get("tribCalc") or {}).get("IBSCBS") or {}
    g = t.get("gIBSCBS") or {}
    uf, mun, cbs = g.get("gIBSUF") or {}, g.get("gIBSMun") or {}, g.get("gCBS") or {}
    return {
        "vBC": g.get("vBC"),
        "pIBSUF": uf.get("pIBSUF"),
        "pRedAliqIBSUF": (uf.get("gRed") or {}).get("pRedAliq"),
        "pAliqEfetIBSUF": (uf.get("gRed") or {}).get("pAliqEfet"),
        "vIBSUF": uf.get("vIBSUF"),
        "pIBSMun": mun.get("pIBSMun"),
        "pRedAliqIBSMun": (mun.get("gRed") or {}).get("pRedAliq"),
        "pAliqEfetIBSMun": (mun.get("gRed") or {}).get("pAliqEfet"),
        "vIBSMun": mun.get("vIBSMun"),
        "vIBS": g.get("vIBS"),
        "pCBS": cbs.get("pCBS"),
        "pRedAliqCBS": (cbs.get("gRed") or {}).get("pRedAliq"),
        "pAliqEfetCBS": (cbs.get("gRed") or {}).get("pAliqEfet"),
        "vCBS": cbs.get("vCBS"),
    }


VALORES = ("vIBSUF", "vIBSMun", "vIBS", "vCBS")
REDUCOES = ("pRedAliqIBSUF", "pAliqEfetIBSUF", "pRedAliqIBSMun", "pAliqEfetIBSMun", "pRedAliqCBS", "pAliqEfetCBS")
ALIQUOTAS = ("pIBSUF", "pIBSMun", "pCBS")


def _diferente(declarado: str | None, calculado: str | None) -> bool:
    d = _dec(declarado) if declarado is not None else Decimal(0)
    c = _dec(calculado) if calculado is not None else Decimal(0)
    if d is None or c is None:
        return declarado != calculado
    return abs(d - c) > TOLERANCIA


# ----------------------------------------------------------------------
# Auditoria
# ----------------------------------------------------------------------


async def auditar(cliente: RtcClient, xml: str) -> dict[str, Any]:
    nota = extrair(ler_xml(xml))
    achados: list[Achado] = []
    premissas: list[str] = []

    if nota["modelo"] not in ("55", "65"):
        raise ErroAuditoria(f"Modelo {nota['modelo']} não suportado: só NF-e (55) e NFC-e (65).")
    if not nota["data"] or not nota["municipio"] or not nota["uf"]:
        raise ErroAuditoria("A nota não informa data de emissão, município do fato gerador ou UF do emitente.")
    ano = int(nota["data"][:4])
    if ano < 2026:
        raise ErroAuditoria(
            f"Nota emitida em {nota['data']}: o IBS e a CBS só existem a partir de 2026, não há o que auditar."
        )
    tipo = "nfe" if nota["modelo"] == "55" else "nfce"

    # 1. Estrutura (XSD)
    try:
        valido = (
            await cliente.regime_geral(
                "POST",
                "calculadora/xml/validate",
                params={"tipo": tipo, "subtipo": "nota"},
                conteudo=xml,
                headers={"Content-Type": "application/xml"},
            )
        )["dados"]
        if valido is not True:
            achados.append(Achado(gravidade="erro", tipo="xsd", item=None, mensagem="O XML não é válido no XSD oficial da nota."))
    except CalculadoraError as exc:
        achados.append(Achado(gravidade="erro", tipo="xsd", item=None, mensagem=f"O XML não passou na validação do XSD: {exc}"))

    simples = nota["crt"] in ("1", "2", "4")
    if simples:
        achados.append(
            Achado(
                gravidade="info",
                tipo="escopo",
                item=None,
                mensagem=f"Emitente com CRT {nota['crt']} (Simples Nacional/MEI): os valores de IBS/CBS do regime "
                "regular não foram recalculados; só a estrutura e a classificação foram conferidas.",
            )
        )

    itens_auditaveis = [i for i in nota["itens"] if i["ibscbs"] and i["ibscbs"]["temGrupoValores"]]
    for item in nota["itens"]:
        if not item["ibscbs"]:
            achados.append(
                Achado(gravidade="erro" if ano >= 2026 else "info", tipo="grupo_ausente", item=item["numero"],
                       mensagem="Item sem o grupo IBSCBS.")
            )
        elif not item["ibscbs"]["temGrupoValores"]:
            achados.append(
                Achado(gravidade="info", tipo="sem_valores", item=item["numero"],
                       mensagem=f"Item com CST {item['ibscbs']['CST']} sem grupo gIBSCBS; valores não recalculados.")
            )

    # 2 e 4. Classificação e valores, recalculados sobre o vBC declarado
    calculados: dict[int, dict[str, Any]] = {}
    if itens_auditaveis:
        operacao = {
            "id": nota["chave"] or "auditoria",
            "versao": "1.0.0",
            "dhFatoGerador": nota["dhEmi"],
            "municipio": int(nota["municipio"]),
            "uf": nota["uf"],
            "tpDoc": int(nota["modelo"]),
            "itens": [],
        }
        for item in itens_auditaveis:
            d = item["ibscbs"]
            entrada: dict[str, Any] = {
                "numero": item["numero"],
                "cst": d["CST"],
                "cClassTrib": d["cClassTrib"],
                "baseCalculo": float(_dec(d["vBC"]) or 0),
                "quantidade": float(_dec(item["quantidade"]) or 1),
                "unidade": item["unidade"] or "UN",
            }
            if item["ncm"]:
                entrada["ncm"] = item["ncm"]
            if ano >= 2027:
                entrada["aliquotasNominais"] = {
                    "cbs": float(_dec(d["pCBS"]) or 0),
                    "ibsEstadual": float(_dec(d["pIBSUF"]) or 0),
                    "ibsMunicipal": float(_dec(d["pIBSMun"]) or 0),
                }
            operacao["itens"].append(entrada)
        if ano >= 2027:
            premissas.append(
                "A partir de 2027 as alíquotas nominais ainda não estão definidas em lei: o recálculo usou as "
                "alíquotas nominais declaradas na nota (pIBSUF, pIBSMun, pCBS). Elas mesmas não foram conferidas."
            )
        try:
            resposta = await cliente.regime_geral("POST", "calculadora/observabilidade/regime-geral", json_body=operacao)
        except CalculadoraError as exc:
            raise ErroAuditoria(f"Não foi possível recalcular os itens: {exc}") from exc
        metadados_calculo = resposta["metadados"]
        for objeto in resposta["dados"].get("objetos") or []:
            calculados[objeto["nObj"]] = objeto
    else:
        metadados_calculo = {}

    comparacoes: list[dict[str, Any]] = []
    for item in itens_auditaveis:
        n, d = item["numero"], item["ibscbs"]
        objeto = calculados.get(n, {})
        if objeto.get("estadoItem") not in ("CALCULADO", "CALCULO_SIMULADO"):
            for e in objeto.get("erros") or [{"code": None, "detail": f"estado {objeto.get('estadoItem')}"}]:
                achados.append(
                    Achado(gravidade="erro", tipo="classificacao" if e.get("code") in ("REG-011", "REG-012") else "calculo",
                           item=n, campo=e.get("pointer"), mensagem=f"{e.get('code') or ''} {e.get('detail') or e.get('title')}".strip())
                )
            continue
        calc = _campos_calculados(objeto)
        comparacoes.append({"item": n, "declarado": {k: d.get(k) for k in calc}, "calculado": calc})
        if simples:
            continue
        campos = REDUCOES + VALORES + (ALIQUOTAS if ano < 2027 else ())
        for campo in campos:
            if _diferente(d.get(campo), calc.get(campo)):
                achados.append(
                    Achado(gravidade="erro", tipo="aliquota" if campo in ALIQUOTAS else ("reducao" if campo in REDUCOES else "valor"),
                           item=n, campo=campo, declarado=d.get(campo), calculado=calc.get(campo),
                           mensagem=f"{campo} declarado {d.get(campo)} difere do calculado {calc.get(campo)}.")
                )

    # 3. Base de cálculo
    for item in itens_auditaveis:
        if simples:
            break
        d = item["ibscbs"]
        entrada_base: dict[str, Any] = {
            "anoFatoGerador": ano,
            "tipoDocumento": int(nota["modelo"]),
            "natureza": "servico" if item["servico"] else "bem",
            **{k: float(_dec(v) or 0) for k, v in item["componentes"].items()},
        }
        try:
            base = (await cliente.regime_geral(
                "POST", "calculadora/base-calculo/base-calculo-unificada", json_body=entrada_base
            ))["dados"]
        except CalculadoraError as exc:
            achados.append(Achado(gravidade="alerta", tipo="base", item=item["numero"],
                                  mensagem=f"Base de cálculo não recalculada: {exc}"))
            continue
        esperado = _dec(str(base.get("baseRG")))
        declarado = _dec(d["vBC"])
        if esperado is not None and declarado is not None and abs(esperado - declarado) > TOLERANCIA:
            achados.append(
                Achado(gravidade="erro", tipo="base", item=item["numero"], campo="vBC", declarado=d["vBC"],
                       calculado=str(esperado.quantize(TOLERANCIA)),
                       mensagem=f"vBC declarado {d['vBC']} difere da base recalculada a partir dos componentes do item "
                       f"({', '.join(f'{k}={v}' for k, v in item['componentes'].items())}).")
            )
        for aviso in base.get("avisos") or []:
            achados.append(Achado(gravidade="info", tipo="base", item=item["numero"], mensagem=aviso))

    # 5. Totais
    tot = nota["totalIBSCBS"]
    if tot and itens_auditaveis:
        somas = {
            "vBCIBSCBS": sum((_dec(i["ibscbs"]["vBC"]) or Decimal(0)) for i in itens_auditaveis),
            **{c: sum((_dec(i["ibscbs"][c]) or Decimal(0)) for i in itens_auditaveis) for c in VALORES},
        }
        for campo, soma in somas.items():
            if _diferente(tot.get(campo), str(soma)):
                achados.append(
                    Achado(gravidade="erro", tipo="total", item=None, campo=f"IBSCBSTot/{campo}", declarado=tot.get(campo),
                           calculado=str(soma), mensagem=f"Total {campo} declarado {tot.get(campo)} difere da soma dos itens {soma}.")
                )
    elif itens_auditaveis and not simples:
        achados.append(Achado(gravidade="erro", tipo="total", item=None, mensagem="Nota sem o grupo IBSCBSTot."))

    erros = sum(a.gravidade == "erro" for a in achados)
    return {
        "nota": {k: nota[k] for k in ("chave", "modelo", "numero", "data", "municipio", "uf", "crt")},
        "situacao": "conforme" if erros == 0 else "com_divergencias",
        "resumo": {
            "itens": len(nota["itens"]),
            "itensRecalculados": len(comparacoes),
            "erros": erros,
            "alertas": sum(a.gravidade == "alerta" for a in achados),
        },
        "achados": [a.model_dump() for a in achados],
        "comparacoes": comparacoes,
        "itens": [
            {"numero": i["numero"], "descricao": i["descricao"], "ncm": i["ncm"], "cfop": i["cfop"],
             "CST": (i["ibscbs"] or {}).get("CST"), "cClassTrib": (i["ibscbs"] or {}).get("cClassTrib")}
            for i in nota["itens"]
        ],
        "premissas": premissas,
        "escopo": "Confere o grupo IBSCBS (classificação, base, reduções, valores e totais) e o XSD. Não confere "
        "ICMS, IPI, PIS/COFINS, diferimento (gDif), devolução de tributos (gDevTrib), tributação regular "
        "(gTribRegular), crédito presumido nem Imposto Seletivo.",
        "metadados": metadados_calculo,
    }
