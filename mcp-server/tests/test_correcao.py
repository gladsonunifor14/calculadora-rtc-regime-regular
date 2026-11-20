"""Testes do diagnóstico/conferência de correções (correcao.py) e do agente rtc-corretor."""

from __future__ import annotations

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from rtc_mcp.agente_corretor import FERRAMENTAS, RespostaCorretor, avaliar_resposta, corrigir, formato_resposta
from rtc_mcp.client import RtcClient
from rtc_mcp.correcao import conferir, diagnosticar, diferencas
from test_classificador import _calculadora_acessivel, _parametros_servidor

calculadora = pytest.mark.skipif(not _calculadora_acessivel(), reason="calculadora do regime regular não está acessível")

CABECALHO = {"id": "op-1", "versao": "1.0.0", "dhFatoGerador": "2026-06-10T10:00:00-03:00", "uf": "RS", "municipio": 4314902}
# Carne bovina com a cClassTrib de higiene (REG-011) e arroz com NCM pontuada e CST incoerente (REG-002 e REG-006).
COM_ERROS = {**CABECALHO, "itens": [
    {"numero": 1, "ncm": "02013000", "cst": "200", "cClassTrib": "200035", "baseCalculo": 80, "quantidade": 1, "unidade": "KG"},
    {"numero": 2, "ncm": "1006.30.21", "cst": "000", "cClassTrib": "200003", "baseCalculo": 50, "quantidade": 5, "unidade": "KG"},
    {"numero": 3, "ncm": "48202000", "cst": "000", "cClassTrib": "000001", "baseCalculo": 20, "quantidade": 1, "unidade": "UN"},
]}


def corrigida_certa() -> dict[str, Any]:
    op = copy.deepcopy(COM_ERROS)
    op["itens"][0]["cClassTrib"] = "200003"
    op["itens"][1]["ncm"] = "10063021"
    op["itens"][1]["cst"] = "200"
    return op


CORRECOES_CERTAS = [
    {"ponteiro": "/itens/0/cClassTrib", "valorAnterior": "200035", "valorNovo": "200003", "codigoErro": "REG-011",
     "motivo": "carne no Anexo I", "confianca": "alta"},
    {"ponteiro": "/itens/1/ncm", "valorAnterior": "1006.30.21", "valorNovo": "10063021", "codigoErro": "REG-002",
     "motivo": "NCM sem pontos", "confianca": "alta"},
    {"ponteiro": "/itens/1/cst", "valorAnterior": "000", "valorNovo": "200", "codigoErro": "REG-006",
     "motivo": "CST da 200003", "confianca": "alta"},
]


def resposta(operacao: dict[str, Any], correcoes=None, **kw) -> RespostaCorretor:
    base = {"status": "corrigida", "resumo": "r", "operacaoCorrigidaJson": json.dumps(operacao),
            "correcoes": CORRECOES_CERTAS if correcoes is None else correcoes, "pendencias": [], "perguntas": []}
    return RespostaCorretor(**(base | kw))


# ----------------------------------------------------------------------
# Unitários
# ----------------------------------------------------------------------


def test_diferencas_com_ponteiros_json():
    antes = {"a": 1, "b": {"c": [1, 2]}, "x/y": 1}
    depois = {"a": 1, "b": {"c": [1, 3]}, "d": 4, "x/y": 2}
    assert diferencas(antes, depois) == [
        {"ponteiro": "/b/c/1", "antes": 2, "depois": 3, "tipo": "alterado"},
        {"ponteiro": "/d", "antes": None, "depois": 4, "tipo": "incluido"},
        {"ponteiro": "/x~1y", "antes": 1, "depois": 2, "tipo": "alterado"},
    ]


SEM_ERROS = {"situacao": "sem_erros", "erros": []}


def test_conferir_correcao_legitima():
    r = conferir(COM_ERROS, corrigida_certa(), [c["ponteiro"] for c in CORRECOES_CERTAS], SEM_ERROS, [])
    assert r["violacoes"] == [] and r["alteracoesProtegidas"] == [] and len(r["diferencas"]) == 3


def test_conferir_recusa_item_removido():
    op = corrigida_certa()
    del op["itens"][0]
    r = conferir(COM_ERROS, op, ["/itens"], SEM_ERROS, [])
    assert any("Itens removidos" in v for v in r["violacoes"])


def test_conferir_recusa_alteracao_nao_declarada_e_marca_protegidas():
    op = corrigida_certa()
    op["itens"][2]["baseCalculo"] = 0.01
    r = conferir(COM_ERROS, op, [c["ponteiro"] for c in CORRECOES_CERTAS], SEM_ERROS, [])
    assert any("/itens/2/baseCalculo" in v for v in r["violacoes"])
    assert [d["ponteiro"] for d in r["alteracoesProtegidas"]] == ["/itens/2/baseCalculo"]
    # Declarada, deixa de ser violação, mas continua protegida.
    r = conferir(COM_ERROS, op, [c["ponteiro"] for c in CORRECOES_CERTAS] + ["/itens/2/baseCalculo"], SEM_ERROS, [])
    assert r["violacoes"] == [] and r["alteracoesProtegidas"]


def test_conferir_exige_declarar_erros_restantes():
    final = {"situacao": "com_erros", "erros": [{"codigo": "REG-037", "detalhe": "alíquotas"}]}
    assert conferir(COM_ERROS, COM_ERROS, [], final, [])["violacoes"]
    assert conferir(COM_ERROS, COM_ERROS, [], final, ["REG-037"])["violacoes"] == []


def test_esquema_da_resposta():
    assert '"additionalProperties": false' in json.dumps(formato_resposta()["schema"])


# ----------------------------------------------------------------------
# Diagnóstico com a calculadora real
# ----------------------------------------------------------------------


@calculadora
async def test_diagnostico_de_erros_por_item():
    d = await diagnosticar(RtcClient(), COM_ERROS)
    assert d["situacao"] == "com_erros" and not d["rejeitadaInteira"] and d["itensCalculados"] == 1
    assert {(e["item"], e["codigo"]) for e in d["erros"]} == {(1, "REG-011"), (2, "REG-002")}
    assert all(e["descricao"] for e in d["erros"])


@calculadora
async def test_diagnostico_de_operacao_rejeitada_inteira():
    # Os erros vêm em camadas: a validação de campos obrigatórios esconde as regras de negócio.
    op = {k: v for k, v in COM_ERROS.items() if k != "versao"} | {"municipio": 3550308}
    d = await diagnosticar(RtcClient(), op)
    assert d["rejeitadaInteira"] is True
    assert [(e["codigo"], e["ponteiro"], e["escopo"]) for e in d["erros"]] == [("VAL-001", "#/versao", "operacao")]
    d = await diagnosticar(RtcClient(), op | {"versao": "1.0.0"})
    assert [(e["codigo"], e["escopo"]) for e in d["erros"]] == [("REG-018", "operacao")]


@calculadora
async def test_diagnostico_de_2027_sem_aliquotas():
    op = {**corrigida_certa(), "dhFatoGerador": "2027-06-10T10:00:00-03:00"}
    d = await diagnosticar(RtcClient(), op)
    assert {e["codigo"] for e in d["erros"]} == {"REG-037"}


@calculadora
async def test_ferramenta_mcp_diagnosticar_operacao():
    from rtc_mcp.server import criar_servidor
    from test_server import chamar

    erro, corpo = await chamar(criar_servidor(), "diagnosticar_operacao", {"operacao": COM_ERROS})
    assert not erro and corpo["dados"]["situacao"] == "com_erros"


# ----------------------------------------------------------------------
# Conferência das respostas do agente (calculadora real, sem modelo)
# ----------------------------------------------------------------------


async def _avaliar(resp: RespostaCorretor):
    cli = RtcClient()
    return await avaliar_resposta(cli, COM_ERROS, resp, await diagnosticar(cli, COM_ERROS))


@calculadora
async def test_correcao_certa_fica_pronta():
    r = await _avaliar(resposta(corrigida_certa()))
    assert r.pronta and not r.revisaoHumanaRecomendada and r.violacoes == []
    assert r.diagnosticoFinal["situacao"] == "sem_erros"


@calculadora
async def test_apagar_o_item_com_erro_nao_e_correcao():
    op = corrigida_certa()
    del op["itens"][0]
    r = await _avaliar(resposta(op, correcoes=CORRECOES_CERTAS[1:] + [
        {"ponteiro": "/itens", "valorAnterior": None, "valorNovo": None, "codigoErro": "REG-011", "motivo": "x", "confianca": "alta"}]))
    assert not r.pronta and any("Itens removidos" in v for v in r.violacoes)


@calculadora
async def test_mexer_na_base_exige_revisao():
    op = corrigida_certa()
    op["itens"][0]["baseCalculo"] = 1
    r = await _avaliar(resposta(op, correcoes=CORRECOES_CERTAS + [
        {"ponteiro": "/itens/0/baseCalculo", "valorAnterior": "80", "valorNovo": "1", "codigoErro": None, "motivo": "x", "confianca": "alta"}]))
    assert not r.pronta and r.revisaoHumanaRecomendada and r.alteracoesProtegidas


@calculadora
async def test_erro_restante_nao_declarado():
    op = corrigida_certa()
    op["itens"][1]["cst"] = "000"  # volta o REG-006
    r = await _avaliar(resposta(op, correcoes=CORRECOES_CERTAS[:2]))
    assert not r.pronta and any("REG-006" in v for v in r.violacoes)


@calculadora
async def test_valor_declarado_diferente_do_gravado():
    correcoes = copy.deepcopy(CORRECOES_CERTAS)
    correcoes[0]["valorNovo"] = "000001"
    r = await _avaliar(resposta(corrigida_certa(), correcoes=correcoes))
    assert any("declara o valor '000001'" in v for v in r.violacoes)


@calculadora
async def test_pendencia_declarada_deixa_parcial_com_revisao():
    original = {**COM_ERROS, "dhFatoGerador": "2027-06-10T10:00:00-03:00"}
    op = {**corrigida_certa(), "dhFatoGerador": "2027-06-10T10:00:00-03:00"}
    cli = RtcClient()
    resp = resposta(op, status="parcial", pendencias=[{"codigoErro": "REG-037", "item": None, "motivo": "alíquotas"}],
                    perguntas=["Quais alíquotas nominais usar?"])
    r = await avaliar_resposta(cli, original, resp, await diagnosticar(cli, original))
    assert r.violacoes == [] and r.revisaoHumanaRecomendada and not r.pronta


@calculadora
async def test_operacao_sem_erros_nao_chama_o_modelo():
    r = await corrigir(corrigida_certa(), cliente=object(), parametros_servidor=None)
    assert r.resposta.status == "sem_erros" and r.pronta and not r.revisaoHumanaRecomendada


# ----------------------------------------------------------------------
# Agente: API simulada + rtc-mcp real + calculadora real
# ----------------------------------------------------------------------


class ApiCorretor(BaseHTTPRequestHandler):
    resposta: dict[str, Any] = {}
    requisicoes: list[dict[str, Any]] = []
    conversas: dict[str, list[dict[str, Any]]] = {}

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        ApiCorretor.requisicoes.append(corpo)
        thread = corpo.get("thread") or {}
        anteriores = ApiCorretor.conversas.get(thread.get("previous_message_id"), []) if thread.get("type") == "continue" else []
        historico = anteriores + corpo["messages"]
        passos = sum(m["role"] == "assistant" for m in historico)
        if passos == 0:
            conteudo = [{"type": "tool_use", "id": "t1", "name": "diagnosticar_operacao", "input": {"operacao": COM_ERROS}}]
        elif passos == 1:
            conteudo = [{"type": "tool_use", "id": "t2", "name": "diagnosticar_operacao", "input": {"operacao": corrigida_certa()}}]
        else:
            conteudo = [{"type": "text", "text": json.dumps(ApiCorretor.resposta)}]
        id_ = f"msg_{len(ApiCorretor.requisicoes)}"
        ApiCorretor.conversas[id_] = historico + [{"role": "assistant", "content": conteudo}]
        dados = json.dumps({"id": id_, "type": "message", "role": "assistant", "model": corpo["model"],
                            "content": conteudo, "stop_reason": "tool_use" if passos < 2 else "end_turn",
                            "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 10}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)


@pytest.fixture
def api():
    ApiCorretor.requisicoes, ApiCorretor.conversas = [], {}
    ApiCorretor.resposta = resposta(corrigida_certa()).model_dump()
    srv = HTTPServer(("127.0.0.1", 0), ApiCorretor)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@calculadora
async def test_agente_corrige_e_o_codigo_confere(api):
    from anthropic import AsyncAnthropic

    from rtc_mcp.classificador import Registro

    registro = Registro()
    r = await corrigir(COM_ERROS, cliente=AsyncAnthropic(base_url=api, api_key="teste", max_retries=0),
                       parametros_servidor=_parametros_servidor(), registro=registro)
    assert r.pronta and not r.revisaoHumanaRecomendada
    assert r.operacaoCorrigida == corrigida_certa()
    assert len(r.diagnosticoOriginal["erros"]) == 2 and r.diagnosticoFinal["erros"] == []
    primeira = ApiCorretor.requisicoes[0]
    assert [t["name"] for t in primeira["tools"]] == list(FERRAMENTAS)
    assert "200035" in primeira["messages"][0]["content"]  # a operação vai no pedido
    # As duas rodadas de diagnóstico rodaram de verdade pelo MCP.
    resultados = [json.loads(t["content"])["dados"]["situacao"] for t in registro.transcricao if t["role"] == "tool_result"]
    assert resultados == ["com_erros", "sem_erros"]


@calculadora
async def test_agente_que_apaga_item_e_barrado(api):
    from anthropic import AsyncAnthropic

    op = corrigida_certa()
    del op["itens"][0]
    ApiCorretor.resposta = resposta(op, correcoes=CORRECOES_CERTAS[1:]).model_dump()
    r = await corrigir(COM_ERROS, cliente=AsyncAnthropic(base_url=api, api_key="teste", max_retries=0),
                       parametros_servidor=_parametros_servidor())
    assert not r.pronta and r.revisaoHumanaRecomendada and r.violacoes
