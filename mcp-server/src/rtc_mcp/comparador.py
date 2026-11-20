"""Comparação de IBS/CBS entre o Simples Nacional e a apuração pelo regime regular.

Desde 2027, a empresa do Simples Nacional pode recolher o IBS e a CBS dentro do DAS, com as alíquotas
efetivas do Simples, ou apurá-los pelo regime regular, "por fora" do DAS, com débitos e créditos. Este
módulo calcula os dois cenários com as calculadoras oficiais e compara:

- o IBS/CBS devido nas vendas em cada cenário;
- o crédito que um cliente contribuinte pode tomar em cada cenário;
- o saldo a pagar pelo regime regular depois dos créditos sobre as compras.

Escopo: só IBS e CBS. IRPJ, CSLL, ICMS, ISS e os demais tributos do DAS ficam fora, e o resultado diz
isso. Nenhum valor de tributo é calculado aqui: as contas são somas e diferenças sobre o que as
calculadoras devolvem.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .client import CalculadoraError, RtcClient

# Premissa padrão para 2027 e 2028 (o usuário pode trocar). A CBS de 8,8% é a estimativa usada nos
# exemplos oficiais da calculadora; a alíquota real será fixada em resolução do Senado. O IBS de 0,05%
# estadual e 0,05% municipal é o percentual de teste previsto para 2027 e 2028.
ALIQUOTAS_PADRAO_2027_2028 = {"cbs": 8.8, "ibsEstadual": 0.05, "ibsMunicipal": 0.05}

ESCOPO = (
    "A comparação cobre só IBS e CBS. IRPJ, CSLL, ICMS, ISS, contribuição previdenciária e os demais "
    "tributos do DAS não entram, e a decisão de regime deve considerá-los."
)

CENTAVO = Decimal("0.01")


class AliquotasNominais(BaseModel):
    cbs: float = Field(description="Alíquota nominal da CBS, em %")
    ibsEstadual: float = Field(description="Alíquota nominal do IBS estadual, em %")
    ibsMunicipal: float = Field(description="Alíquota nominal do IBS municipal, em %")


class CreditosCompras(BaseModel):
    ibs: float = Field(0, description="IBS cobrado nas compras do período que daria direito a crédito (R$)")
    cbs: float = Field(0, description="CBS cobrada nas compras do período que daria direito a crédito (R$)")


class ItemCenario(BaseModel):
    descricao: str
    ncm: str | None = Field(None, pattern=r"^\d{8}$")
    nbs: str | None = Field(None, pattern=r"^\d{9}$")
    cst: str = Field(pattern=r"^\d{3}$")
    cClassTrib: str = Field(pattern=r"^\d{6}$")
    valor: float = Field(gt=0, description="Valor da venda do item, sem IBS/CBS e sem Imposto Seletivo (R$)")
    cAtivSN: str = Field(pattern=r"^\d{2}$", description="Atividade do Simples Nacional (2 dígitos)")
    pIBSSN: float | None = Field(None, description="Alíquota efetiva de IBS no Simples, se diferente da geral")
    pCBSSN: float | None = Field(None, description="Alíquota efetiva de CBS no Simples, se diferente da geral")

    @model_validator(mode="after")
    def _nomenclatura(self) -> "ItemCenario":
        if not self.ncm and not self.nbs:
            raise ValueError("informe ncm (mercadoria) ou nbs (serviço)")
        return self


class Cenario(BaseModel):
    """Vendas de um período típico da empresa e os parâmetros dos dois regimes."""

    data: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="Data do fato gerador (2027 em diante)")
    uf: str = Field(pattern=r"^[A-Z]{2}$")
    municipio: int = Field(description="Código IBGE do município do estabelecimento")
    tpDoc: Literal["55", "65"] = Field("55", description="55 NF-e ou 65 NFC-e")
    itens: list[ItemCenario] = Field(min_length=1)
    pIBSSN: float = Field(ge=0, description="Alíquota efetiva de IBS no Simples, do PGDAS (%)")
    pCBSSN: float = Field(ge=0, description="Alíquota efetiva de CBS no Simples, do PGDAS (%)")
    aliquotasNominais: AliquotasNominais | None = Field(
        None, description="Alíquotas nominais do regime regular; vazio usa a premissa padrão de 2027-2028"
    )
    creditosCompras: CreditosCompras | None = Field(
        None, description="IBS/CBS das compras do mesmo período, creditáveis no regime regular"
    )


class ErroComparacao(Exception):
    """O cenário não pode ser comparado; a mensagem explica o motivo."""


def _dec(valor: Any) -> Decimal:
    return Decimal(str(valor or "0"))


def _txt(valor: Decimal) -> str:
    return str(valor.quantize(CENTAVO))


def _ano(data: str) -> int:
    return int(data[:4])


def resolver_aliquotas(cenario: Cenario) -> tuple[dict[str, float], list[str]]:
    """Alíquotas nominais do regime regular e as premissas assumidas."""
    ano = _ano(cenario.data)
    if ano < 2027:
        raise ErroComparacao(
            "A comparação vale a partir de 2027: antes disso o Simples Nacional não recolhe IBS/CBS "
            "(a calculadora do Simples rejeita datas de 2026)."
        )
    if cenario.aliquotasNominais is not None:
        return cenario.aliquotasNominais.model_dump(), [
            "Alíquotas nominais do regime regular informadas pelo usuário: "
            f"CBS {cenario.aliquotasNominais.cbs}%, IBS estadual {cenario.aliquotasNominais.ibsEstadual}%, "
            f"IBS municipal {cenario.aliquotasNominais.ibsMunicipal}%."
        ]
    if ano <= 2028:
        return dict(ALIQUOTAS_PADRAO_2027_2028), [
            "PREMISSA (editável): alíquotas nominais do regime regular de CBS 8,8%, IBS estadual 0,05% e IBS "
            "municipal 0,05%. A CBS de 8,8% é a estimativa dos exemplos oficiais da calculadora; a alíquota "
            "real ainda será fixada em resolução do Senado. Os valores do regime regular são simulações."
        ]
    raise ErroComparacao(
        "A partir de 2029 o IBS sobe gradualmente até a alíquota cheia, que ainda não foi definida. Informe "
        "as alíquotas nominais (CBS, IBS estadual e IBS municipal) que devem ser usadas como premissa."
    )


def _operacao_regular(cenario: Cenario, aliquotas: dict[str, float]) -> dict[str, Any]:
    itens = []
    for n, item in enumerate(cenario.itens, start=1):
        entrada: dict[str, Any] = {
            "numero": n,
            "cst": item.cst,
            "cClassTrib": item.cClassTrib,
            "baseCalculo": item.valor,
            "quantidade": 1,
            "unidade": "UN",
            "aliquotasNominais": aliquotas,
        }
        entrada["ncm" if item.ncm else "nbs"] = item.ncm or item.nbs
        itens.append(entrada)
    return {
        "id": "comparacao-regimes",
        "versao": "1.0.0",
        "dhFatoGerador": f"{cenario.data}T12:00:00-03:00",
        "municipio": cenario.municipio,
        "uf": cenario.uf,
        "tpDoc": int(cenario.tpDoc),
        "itens": itens,
    }


def _operacao_simples(cenario: Cenario) -> dict[str, Any]:
    return {
        "id": "comparacao-regimes",
        "versao": "2.0.0",
        "data": cenario.data,
        "tpDoc": cenario.tpDoc,
        "CRT": "1",
        "itens": [
            {
                "numero": n,
                "cAtivSN": item.cAtivSN,
                "cClassTrib": item.cClassTrib,
                "baseCalculo": f"{item.valor:.2f}",
                "valorImpostoSeletivo": "0.00",
                "pIBSSN": str(item.pIBSSN if item.pIBSSN is not None else cenario.pIBSSN),
                "pCBSSN": str(item.pCBSSN if item.pCBSSN is not None else cenario.pCBSSN),
            }
            for n, item in enumerate(cenario.itens, start=1)
        ],
    }


def _por_numero(objetos: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {o["nObj"]: o for o in objetos or []}


async def comparar(cliente: RtcClient, cenario: Cenario) -> dict[str, Any]:
    """Calcula os dois cenários nas calculadoras oficiais e devolve a comparação."""
    aliquotas, premissas = resolver_aliquotas(cenario)
    try:
        regular = await cliente.regime_geral(
            "POST", "calculadora/observabilidade/regime-geral", json_body=_operacao_regular(cenario, aliquotas)
        )
        simples = await cliente.simples_nacional(
            "POST", "calculadora/observabilidade/simples-nacional", json_body=_operacao_simples(cenario)
        )
    except CalculadoraError as exc:
        raise ErroComparacao(str(exc)) from exc

    objetos_rg = _por_numero(regular["dados"].get("objetos"))
    objetos_sn = _por_numero(simples["dados"].get("objetos"))

    itens: list[dict[str, Any]] = []
    problemas: list[str] = []
    soma = {k: Decimal(0) for k in ("valor", "rg_ibs", "rg_cbs", "sn_ibs", "sn_cbs")}
    for n, item in enumerate(cenario.itens, start=1):
        o_rg, o_sn = objetos_rg.get(n, {}), objetos_sn.get(n, {})
        erros = [
            f"{nome}: {e.get('code')} {e.get('detail') or e.get('title')}"
            for nome, o in (("regime regular", o_rg), ("Simples", o_sn))
            for e in o.get("erros") or []
        ]
        calc_rg = o_rg.get("estadoItem") in ("CALCULADO", "CALCULO_SIMULADO")
        calc_sn = o_sn.get("estadoItem") == "CALCULADO"
        if not (calc_rg and calc_sn):
            estados = f"regime regular={o_rg.get('estadoItem')}, Simples={o_sn.get('estadoItem')}"
            problemas.append(f"Item {n} ({item.descricao}) fora da comparação: {estados}. " + "; ".join(erros))
            itens.append({"numero": n, "descricao": item.descricao, "comparado": False, "erros": erros})
            continue

        g = o_rg["tribCalc"]["IBSCBS"]["gIBSCBS"]
        rg_ibs, rg_cbs = _dec(g.get("vIBS")), _dec((g.get("gCBS") or {}).get("vCBS"))
        t = o_sn["tribCalc"]
        sn_ibs, sn_cbs = _dec(t.get("vIBSSN")), _dec(t.get("vCBSSN"))
        valor = Decimal(str(item.valor))
        for chave, v in (("valor", valor), ("rg_ibs", rg_ibs), ("rg_cbs", rg_cbs), ("sn_ibs", sn_ibs), ("sn_cbs", sn_cbs)):
            soma[chave] += v
        itens.append(
            {
                "numero": n,
                "descricao": item.descricao,
                "comparado": True,
                "valor": _txt(valor),
                "cClassTrib": item.cClassTrib,
                "regimeRegular": {
                    "ibs": _txt(rg_ibs),
                    "cbs": _txt(rg_cbs),
                    "total": _txt(rg_ibs + rg_cbs),
                    "aliquotaEfetivaCBS": (g.get("gCBS") or {}).get("gRed", {}).get("pAliqEfet")
                    or (g.get("gCBS") or {}).get("pCBS"),
                    "reducaoCBS": (g.get("gCBS") or {}).get("gRed", {}).get("pRedAliq"),
                },
                "simples": {
                    "ibs": _txt(sn_ibs),
                    "cbs": _txt(sn_cbs),
                    "total": _txt(sn_ibs + sn_cbs),
                    "pIBSSN": t.get("pIBSSN"),
                    "pCBSSN": t.get("pCBSSN"),
                },
            }
        )

    if soma["valor"] == 0:
        raise ErroComparacao("Nenhum item pôde ser calculado nos dois regimes: " + " | ".join(problemas))

    creditos = cenario.creditosCompras or CreditosCompras()
    if cenario.creditosCompras is None:
        premissas.append(
            "Créditos sobre compras não informados: considerados zero. No regime regular a empresa abate o "
            "IBS/CBS pago nas compras, então informar esse valor costuma reduzir o saldo a pagar."
        )
    cred_ibs, cred_cbs = Decimal(str(creditos.ibs)), Decimal(str(creditos.cbs))
    debito_rg = soma["rg_ibs"] + soma["rg_cbs"]
    devido_sn = soma["sn_ibs"] + soma["sn_cbs"]
    saldo_rg = debito_rg - cred_ibs - cred_cbs

    avisos = [v for v in (regular["metadados"].get("avisoDadosSimulados"),) if v]
    if problemas:
        avisos.append("Itens fora da comparação; os totais consideram só os itens calculados nos dois regimes.")

    return {
        "escopo": ESCOPO,
        "premissas": premissas,
        "avisos": avisos,
        "problemas": problemas,
        "itens": itens,
        "totais": {
            "valorVendas": _txt(soma["valor"]),
            "simples": {
                "ibs": _txt(soma["sn_ibs"]),
                "cbs": _txt(soma["sn_cbs"]),
                "devido": _txt(devido_sn),
                "creditoParaClienteContribuinte": _txt(devido_sn),
            },
            "regimeRegular": {
                "ibs": _txt(soma["rg_ibs"]),
                "cbs": _txt(soma["rg_cbs"]),
                "debito": _txt(debito_rg),
                "creditosCompras": _txt(cred_ibs + cred_cbs),
                "saldoAPagar": _txt(saldo_rg),
                "creditoParaClienteContribuinte": _txt(debito_rg),
            },
            "diferencas": {
                "tributoAPagarRegularMenosSimples": _txt(saldo_rg - devido_sn),
                "creditoClienteRegularMenosSimples": _txt(debito_rg - devido_sn),
            },
        },
        "leitura": [
            "simples.devido: IBS/CBS recolhido no DAS sobre essas vendas.",
            "regimeRegular.saldoAPagar: débito nas vendas menos créditos das compras; negativo = saldo credor.",
            "creditoParaClienteContribuinte: crédito que um cliente do regime regular pode tomar na compra; "
            "consumidor final e cliente não contribuinte não aproveitam crédito.",
        ],
        "metadados": {
            "regimeRegular": regular["metadados"],
            "simples": simples["metadados"],
            "aliquotasNominaisUsadas": aliquotas,
        },
    }
