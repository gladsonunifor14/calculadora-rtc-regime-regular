"""Testes do comparador Simples x regime regular e do agente rtc-comparador."""

from __future__ import annotations

import json
import os
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx
import pytest
import respx

from rtc_mcp.agente_comparador import (
    FERRAMENTAS,
    Parecer,
    comparar_regimes,
    formato_resposta,
    mais_barato,
    verificar,
)
from rtc_mcp.client import Config, RtcClient
from rtc_mcp.comparador import Cenario, ErroComparacao, comparar, resolver_aliquotas
from test_classificador import _calculadora_acessivel, _parametros_servidor

REG = "http://regime.test/api"
SN = "http://simples.test/api"
URL_SIMPLES = os.environ.get("RTC_SIMPLES_NACIONAL_URL", "http://localhost:8082/api")

CENARIO = {
    "data": "2027-06-15",
    "uf": "RS",
    "municipio": 4314902,
    "pIBSSN": 1.25,
    "pCBSSN": 2.75,
    "itens": [
        {"descricao": "Sabonete 90 g", "ncm": "34011190", "cst": "200", "cClassTrib": "200035", "valor": 1000,
         "cAtivSN": "01"},
        {"descricao": "Caderno", "ncm": "48202000", "cst": "000", "cClassTrib": "000001", "valor": 2000,
         "cAtivSN": "01"},
    ],
}


def _cliente() -> RtcClient:
    return RtcClient(Config(regime_geral_url=REG, simples_nacional_url=SN, timeout=5))


def _regular(*itens: tuple[str, str, str | None], aviso: str | None = "6") -> httpx.Response:
    objetos = []
    for n, (vibs, vcbs, red) in enumerate(itens, start=1):
        gcbs: dict[str, Any] = {"pCBS": "8.80", "vCBS": vcbs}
        if red:
            gcbs["gRed"] = {"pRedAliq": red, "pAliqEfet": "3.52"}
        objetos.append({"nObj": n, "estadoItem": "CALCULO_SIMULADO",
                        "tribCalc": {"IBSCBS": {"gIBSCBS": {"vIBS": vibs, "gCBS": gcbs}}}})
    headers = {"x-warning-dados-simulados": aviso} if aviso else {}
    return httpx.Response(200, json={"objetos": objetos}, headers=headers)


def _simples(*itens: tuple[str, str]) -> httpx.Response:
    return httpx.Response(200, json={"objetos": [
        {"nObj": n, "estadoItem": "CALCULADO",
         "tribCalc": {"vIBSSN": vibs, "vCBSSN": vcbs, "pIBSSN": "1.25", "pCBSSN": "2.75"}}
        for n, (vibs, vcbs) in enumerate(itens, start=1)
    ]})


# ----------------------------------------------------------------------
# Premissas de alíquota
# ----------------------------------------------------------------------


def test_premissas_de_aliquota_por_ano():
    with pytest.raises(ErroComparacao, match="a partir de 2027"):
        resolver_aliquotas(Cenario(**{**CENARIO, "data": "2026-06-15"}))
    aliquotas, premissas = resolver_aliquotas(Cenario(**CENARIO))
    assert aliquotas == {"cbs": 8.8, "ibsEstadual": 0.05, "ibsMunicipal": 0.05}
    assert premissas[0].startswith("PREMISSA (editável)")
    with pytest.raises(ErroComparacao, match="Informe"):
        resolver_aliquotas(Cenario(**{**CENARIO, "data": "2029-01-10"}))
    proprias = {"cbs": 9.0, "ibsEstadual": 2.0, "ibsMunicipal": 1.0}
    aliquotas, premissas = resolver_aliquotas(Cenario(**{**CENARIO, "data": "2029-01-10", "aliquotasNominais": proprias}))
    assert aliquotas == proprias and "informadas pelo usuário" in premissas[0]


def test_item_sem_nomenclatura_e_recusado():
    with pytest.raises(ValueError, match="ncm"):
        Cenario(**{**CENARIO, "itens": [{**CENARIO["itens"][0], "ncm": None}]})


# ----------------------------------------------------------------------
# Comparação (APIs simuladas)
# ----------------------------------------------------------------------


@respx.mock
async def test_comparacao_soma_os_dois_regimes_e_os_creditos():
    rota_rg = respx.post(f"{REG}/calculadora/observabilidade/regime-geral").mock(
        return_value=_regular(("0.40", "35.20", "60.00"), ("2.00", "176.00", None))
    )
    rota_sn = respx.post(f"{SN}/calculadora/observabilidade/simples-nacional").mock(
        return_value=_simples(("12.50", "27.50"), ("25.00", "55.00"))
    )
    cenario = Cenario(**{**CENARIO, "creditosCompras": {"ibs": 0.5, "cbs": 30}})
    r = await comparar(_cliente(), cenario)

    t = r["totais"]
    assert t["valorVendas"] == "3000.00"
    assert t["simples"] == {"ibs": "37.50", "cbs": "82.50", "devido": "120.00", "creditoParaClienteContribuinte": "120.00"}
    assert t["regimeRegular"] == {"ibs": "2.40", "cbs": "211.20", "debito": "213.60", "creditosCompras": "30.50",
                                  "saldoAPagar": "183.10", "creditoParaClienteContribuinte": "213.60"}
    assert t["diferencas"] == {"tributoAPagarRegularMenosSimples": "63.10", "creditoClienteRegularMenosSimples": "93.60"}
    assert r["itens"][0]["regimeRegular"]["reducaoCBS"] == "60.00"
    assert "código 6" in r["avisos"][0]
    assert "só IBS e CBS" in r["escopo"]

    # As operações enviadas às calculadoras.
    rg = json.loads(rota_rg.calls.last.request.content)
    assert rg["itens"][0]["aliquotasNominais"] == {"cbs": 8.8, "ibsEstadual": 0.05, "ibsMunicipal": 0.05}
    assert rg["itens"][1]["ncm"] == "48202000" and rg["dhFatoGerador"].startswith("2027-06-15")
    sn = json.loads(rota_sn.calls.last.request.content)
    assert sn["CRT"] == "1" and sn["itens"][0]["baseCalculo"] == "1000.00" and sn["itens"][0]["pIBSSN"] == "1.25"


@respx.mock
async def test_sem_creditos_vira_premissa_e_saldo_credor_aparece_negativo():
    respx.post(f"{REG}/calculadora/observabilidade/regime-geral").mock(return_value=_regular(("0.40", "35.20", "60.00")))
    respx.post(f"{SN}/calculadora/observabilidade/simples-nacional").mock(return_value=_simples(("12.50", "27.50")))
    um_item = {**CENARIO, "itens": CENARIO["itens"][:1]}
    r = await comparar(_cliente(), Cenario(**um_item))
    assert any("Créditos sobre compras não informados" in p for p in r["premissas"])
    r = await comparar(_cliente(), Cenario(**{**um_item, "creditosCompras": {"ibs": 0, "cbs": 50}}))
    assert r["totais"]["regimeRegular"]["saldoAPagar"] == "-14.40"


@respx.mock
async def test_item_com_erro_fica_fora_dos_totais():
    erro = {"nObj": 2, "estadoItem": "INCONSISTENCIA_ENTRADA", "erros": [{"code": "REG-013", "detail": "IS"}]}
    respx.post(f"{REG}/calculadora/observabilidade/regime-geral").mock(
        return_value=httpx.Response(200, json={"objetos": [_regular(("0.40", "35.20", "60.00")).json()["objetos"][0], erro]})
    )
    respx.post(f"{SN}/calculadora/observabilidade/simples-nacional").mock(
        return_value=_simples(("12.50", "27.50"), ("25.00", "55.00"))
    )
    r = await comparar(_cliente(), Cenario(**CENARIO))
    assert r["totais"]["valorVendas"] == "1000.00"
    assert r["itens"][1]["comparado"] is False and "REG-013" in r["problemas"][0]


@respx.mock
async def test_nenhum_item_comparavel_e_erro():
    erro = {"nObj": 1, "estadoItem": "INCONSISTENCIA_ENTRADA", "erros": [{"code": "REG-011", "detail": "NCM"}]}
    respx.post(f"{REG}/calculadora/observabilidade/regime-geral").mock(return_value=httpx.Response(200, json={"objetos": [erro]}))
    respx.post(f"{SN}/calculadora/observabilidade/simples-nacional").mock(return_value=_simples(("1", "1")))
    with pytest.raises(ErroComparacao, match="Nenhum item"):
        await comparar(_cliente(), Cenario(**{**CENARIO, "itens": CENARIO["itens"][:1]}))


@respx.mock
async def test_calculadora_fora_do_ar_vira_erro_de_comparacao():
    respx.post(f"{REG}/calculadora/observabilidade/regime-geral").mock(side_effect=httpx.ConnectError("x"))
    with pytest.raises(ErroComparacao, match="Não foi possível conectar"):
        await comparar(_cliente(), Cenario(**CENARIO))


# ----------------------------------------------------------------------
# Conferência do parecer
# ----------------------------------------------------------------------


def _parecer(**kw) -> Parecer:
    base = {"status": "analisado", "cenario": CENARIO, "perfilClientes": "consumidores_finais",
            "recomendacao": "permanecer_no_simples", "analise": "a", "fatoresDecisivos": [], "premissas": [],
            "perguntas": [], "ressalvas": []}
    return Parecer(**(base | kw))


def _comparacao(diferenca: str, vendas: str = "3000.00", problemas=()) -> dict[str, Any]:
    return {"totais": {"valorVendas": vendas, "diferencas": {"tributoAPagarRegularMenosSimples": diferenca}},
            "problemas": list(problemas)}


def test_mais_barato():
    assert mais_barato(_comparacao("63.10")) == "permanecer_no_simples"
    assert mais_barato(_comparacao("-63.10")) == "apurar_pelo_regime_regular"
    assert mais_barato(_comparacao("20.00")) == "indiferente"  # abaixo de 1% das vendas


def test_verificar_recomendacao_contra_os_numeros():
    assert verificar(_parecer(), _comparacao("63.10")) == []
    problemas = verificar(_parecer(recomendacao="apurar_pelo_regime_regular"), _comparacao("63.10"))
    assert "contraria os números" in problemas[0]
    # Com clientes contribuintes o crédito entra na conta: não há regra automática.
    assert verificar(_parecer(perfilClientes="contribuintes", recomendacao="apurar_pelo_regime_regular"),
                     _comparacao("63.10")) == []
    assert "sem cenário" in verificar(_parecer(), None)[0]
    assert "fora da comparação" in verificar(_parecer(), _comparacao("63.10", problemas=["Item 2"]))[0]


def test_esquema_do_parecer_atende_structured_outputs():
    def objetos(no):
        if isinstance(no, dict):
            if no.get("type") == "object":
                yield no
            for v in no.values():
                yield from objetos(v)
        elif isinstance(no, list):
            for v in no:
                yield from objetos(v)

    def chaves(no):
        if isinstance(no, dict):
            yield from no.keys()
            for v in no.values():
                yield from chaves(v)
        elif isinstance(no, list):
            for v in no:
                yield from chaves(v)

    esquema = formato_resposta()["schema"]
    for objeto in objetos(esquema):
        assert objeto["additionalProperties"] is False
    # Restrições sem suporte em structured outputs saem do esquema (o SDK as move para a descrição).
    assert not {"pattern", "exclusiveMinimum", "minimum", "maxLength"} & set(chaves(esquema))


# ----------------------------------------------------------------------
# Integração com as duas calculadoras reais
# ----------------------------------------------------------------------


def _simples_acessivel() -> bool:
    try:
        return httpx.get(f"{URL_SIMPLES}/versao", timeout=2).is_success
    except httpx.HTTPError:
        return False


calculadoras = pytest.mark.skipif(
    not (_calculadora_acessivel() and _simples_acessivel()), reason="calculadoras do regime regular e do Simples"
)


@calculadoras
async def test_comparacao_com_as_calculadoras_reais():
    cenario = Cenario(**{**CENARIO, "itens": CENARIO["itens"] + [
        {"descricao": "Arroz 5 kg", "ncm": "10063021", "cst": "200", "cClassTrib": "200003", "valor": 500,
         "cAtivSN": "01"}]})
    r = await comparar(RtcClient(), cenario)
    t = r["totais"]
    assert r["problemas"] == [] and t["valorVendas"] == "3500.00"
    # Alíquota efetiva do Simples (1,25% + 2,75%) sobre todas as vendas.
    assert t["simples"]["devido"] == "140.00"
    # Regime regular: sabonete com redução de 60%, caderno integral, arroz da cesta básica zerado.
    assert t["regimeRegular"]["debito"] == "213.60"
    arroz = r["itens"][2]
    assert arroz["regimeRegular"]["total"] == "0.00" and arroz["simples"]["total"] == "20.00"
    assert r["metadados"]["regimeRegular"]["versaoBaseDados"]


# ----------------------------------------------------------------------
# Agente: API simulada + rtc-mcp real + calculadoras reais
# ----------------------------------------------------------------------


class ApiComparador(BaseHTTPRequestHandler):
    """Primeiro pede a comparação; depois responde com o parecer configurado."""

    parecer: dict[str, Any] = {}
    requisicoes: list[dict[str, Any]] = []
    conversas: dict[str, list[dict[str, Any]]] = {}

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        ApiComparador.requisicoes.append(corpo)
        thread = corpo.get("thread") or {}
        anteriores = ApiComparador.conversas.get(thread.get("previous_message_id"), []) if thread.get("type") == "continue" else []
        historico = anteriores + corpo["messages"]
        if not any(m["role"] == "assistant" for m in historico):
            conteudo = [{"type": "tool_use", "id": "t1", "name": "comparar_regimes_simples_regular",
                         "input": {"cenario": CENARIO}}]
            parada = "tool_use"
        else:
            conteudo = [{"type": "text", "text": json.dumps(ApiComparador.parecer)}]
            parada = "end_turn"
        id_ = f"msg_{len(ApiComparador.requisicoes)}"
        ApiComparador.conversas[id_] = historico + [{"role": "assistant", "content": conteudo}]
        dados = json.dumps({"id": id_, "type": "message", "role": "assistant", "model": corpo["model"],
                            "content": conteudo, "stop_reason": parada, "stop_sequence": None,
                            "usage": {"input_tokens": 10, "output_tokens": 10}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)


@pytest.fixture
def api():
    ApiComparador.requisicoes, ApiComparador.conversas = [], {}
    ApiComparador.parecer = _parecer().model_dump()
    srv = HTTPServer(("127.0.0.1", 0), ApiComparador)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@calculadoras
async def test_agente_anexa_numeros_recalculados(api):
    from anthropic import AsyncAnthropic

    from rtc_mcp.classificador import Registro

    registro = Registro()
    r = await comparar_regimes(
        "Vendo sabonete e caderno a consumidores finais...",
        cliente=AsyncAnthropic(base_url=api, api_key="teste", max_retries=0),
        parametros_servidor=_parametros_servidor(),
        registro=registro,
    )
    # Os números vêm das calculadoras, recalculados pelo código para o cenário final.
    assert r.comparacao["totais"]["simples"]["devido"] == "120.00"
    assert r.comparacao["totais"]["regimeRegular"]["debito"] == "213.60"
    assert r.verificacoes == ["Números do parecer recalculados nas calculadoras oficiais."]
    assert r.revisaoHumanaRecomendada is False

    primeira = ApiComparador.requisicoes[0]
    assert [t["name"] for t in primeira["tools"]] == list(FERRAMENTAS)
    assert primeira["output_config"]["format"]["type"] == "json_schema" and primeira["fallbacks"] == "default"
    # A ferramenta rodou de verdade pelo MCP durante o loop.
    resultado_ferramenta = next(t for t in registro.transcricao if t["role"] == "tool_result")["content"]
    assert json.loads(resultado_ferramenta)["dados"]["totais"]["valorVendas"] == "3000.00"


@calculadoras
async def test_agente_com_recomendacao_contra_os_numeros_vai_para_revisao(api):
    from anthropic import AsyncAnthropic

    ApiComparador.parecer = _parecer(recomendacao="apurar_pelo_regime_regular").model_dump()
    r = await comparar_regimes(
        "...", cliente=AsyncAnthropic(base_url=api, api_key="teste", max_retries=0),
        parametros_servidor=_parametros_servidor(),
    )
    assert r.revisaoHumanaRecomendada is True
    assert any("contraria os números" in v for v in r.verificacoes)


@calculadoras
async def test_agente_sem_aliquotas_do_simples_pede_informacao(api):
    from anthropic import AsyncAnthropic

    ApiComparador.parecer = _parecer(status="precisa_informacao", cenario=None, recomendacao="inconclusivo",
                                     perguntas=["Quais as alíquotas efetivas de IBS e CBS no PGDAS?"]).model_dump()
    r = await comparar_regimes(
        "...", cliente=AsyncAnthropic(base_url=api, api_key="teste", max_retries=0),
        parametros_servidor=_parametros_servidor(),
    )
    assert r.comparacao is None and r.revisaoHumanaRecomendada is True
    assert Decimal("0") == Decimal(len(r.verificacoes) - 1)  # só a mensagem padrão, nenhum problema
