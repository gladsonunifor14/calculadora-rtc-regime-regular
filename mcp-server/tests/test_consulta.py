"""Testes da base de fundamentação (consulta.py) e do agente rtc-consultor."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from rtc_mcp.agente_consultor import FERRAMENTAS, consultar, formato_resposta
from rtc_mcp.client import RtcClient
from rtc_mcp.consulta import Trecho, artigos_em, carregar_base, conferir_citacoes, formatar_base
from test_classificador import _calculadora_acessivel, _parametros_servidor

calculadora = pytest.mark.skipif(not _calculadora_acessivel(), reason="calculadora do regime regular não está acessível")

CESTA = Trecho(
    id="F-200003", cClassTrib="200003", descricao="Vendas de produtos destinados à alimentação humana (Anexo I)",
    cst="200", descricaoCst="Alíquota reduzida", artigo="Art. 125", referencia="CAPÍTULO II > DA CESTA BÁSICA",
    texto="Art. 125. Ficam reduzidas a zero as alíquotas do IBS e da CBS incidentes sobre as vendas de produtos "
          "destinados à alimentação humana relacionados no Anexo I desta Lei Complementar.",
)
EDUCACAO = Trecho(
    id="F-200028", cClassTrib="200028", descricao="Fornecimento dos serviços de educação (Anexo II)", cst="200",
    descricaoCst="Alíquota reduzida", artigo="Art. 129", referencia="Seção II > Dos Serviços de Educação",
    texto="Art. 129. Ficam reduzidas em 60% (sessenta por cento) as alíquotas do IBS e da CBS incidentes sobre "
          "os fornecimentos dos serviços de educação relacionados no Anexo II desta Lei Complementar.",
)
BASE = [CESTA, EDUCACAO]
CITACAO_CESTA = {"id": "F-200003", "trecho": "Ficam reduzidas a zero as alíquotas do IBS e da CBS"}


# ----------------------------------------------------------------------
# Conferência das citações (unitários)
# ----------------------------------------------------------------------


def test_citacao_literal_e_aceita():
    assert conferir_citacoes("Arroz da cesta básica tem alíquota zero (art. 125).", [CITACAO_CESTA], True, BASE) == []


def test_citacao_tolera_aspas_espacos_e_maiusculas():
    citacao = {"id": "F-200003", "trecho": '"FICAM  reduzidas a zero\nas alíquotas do IBS e da CBS…"'}
    assert conferir_citacoes("Alíquota zero.", [citacao], True, BASE) == []


def test_citacao_parafraseada_e_recusada():
    citacao = {"id": "F-200003", "trecho": "As alíquotas de IBS e CBS são zeradas para a cesta básica"}
    problemas = conferir_citacoes("Alíquota zero.", [citacao], True, BASE)
    assert any("palavra por palavra" in p for p in problemas)
    assert any("sem nenhuma citação válida" in p for p in problemas)


def test_citacao_de_trecho_inexistente_ou_curto():
    assert any("não existe" in p for p in conferir_citacoes("x", [{"id": "F-999999", "trecho": "qualquer coisa longa aqui"}], False, BASE))
    assert any("curto demais" in p for p in conferir_citacoes("x", [{"id": "F-200003", "trecho": "Art. 125"}], False, BASE))


def test_artigo_sem_trecho_de_apoio():
    problemas = conferir_citacoes("Pelo art. 125 e pelo art. 129, ...", [CITACAO_CESTA], True, BASE)
    assert problemas == ["Artigos mencionados sem trecho citado que os contenha: art. 129."]
    citacoes = [CITACAO_CESTA, {"id": "F-200028", "trecho": "Ficam reduzidas em 60% (sessenta por cento)"}]
    assert conferir_citacoes("Pelo art. 125 e pelo art. 129, ...", citacoes, True, BASE) == []


def test_cclasstrib_inexistente():
    problemas = conferir_citacoes("Use a cClassTrib 200003 ou a 299999.", [CITACAO_CESTA], True, BASE)
    assert problemas == ["cClassTrib mencionadas que não existem na base: 299999."]


def test_fora_da_base_sem_citacao_nao_e_problema():
    assert conferir_citacoes("A base não trata de split payment.", [], False, BASE) == []


def test_artigos_em():
    assert artigos_em("arts. 125, art. 9º, Artigo 138 e art.2") == {"125", "9", "138", "2"}


def test_formatar_base_e_esquema():
    texto = formatar_base(BASE)
    assert '<trecho id="F-200003">' in texto and "Dispositivo: Art. 125" in texto
    assert '"additionalProperties": false' in json.dumps(formato_resposta()["schema"])


# ----------------------------------------------------------------------
# Base real da calculadora
# ----------------------------------------------------------------------


@calculadora
async def test_base_real_da_calculadora():
    trechos, meta = await carregar_base(RtcClient(), "2027-01-01")
    assert len(trechos) > 100 and meta["versaoBaseDados"]
    cesta = next(t for t in trechos if t.id == "F-200003")
    assert cesta.texto.startswith("Art. 125.") and "\\n" not in cesta.texto
    # Uma citação literal da base real passa na conferência.
    assert conferir_citacoes("Alíquota zero (art. 125).", [{"id": "F-200003", "trecho": cesta.texto[9:90]}], True, trechos) == []


# ----------------------------------------------------------------------
# Agente: API simulada + rtc-mcp real + calculadora real
# ----------------------------------------------------------------------


class ApiConsultor(BaseHTTPRequestHandler):
    parecer: dict[str, Any] = {}
    requisicoes: list[dict[str, Any]] = []
    conversas: dict[str, list[dict[str, Any]]] = {}

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        ApiConsultor.requisicoes.append(corpo)
        thread = corpo.get("thread") or {}
        anteriores = ApiConsultor.conversas.get(thread.get("previous_message_id"), []) if thread.get("type") == "continue" else []
        historico = anteriores + corpo["messages"]
        if not any(m["role"] == "assistant" for m in historico):
            conteudo = [{"type": "tool_use", "id": "t1", "name": "consultar_classificacao_tributaria",
                         "input": {"c_class_trib": "200003", "data": "2027-01-01"}}]
            parada = "tool_use"
        else:
            conteudo = [{"type": "text", "text": json.dumps(ApiConsultor.parecer)}]
            parada = "end_turn"
        id_ = f"msg_{len(ApiConsultor.requisicoes)}"
        ApiConsultor.conversas[id_] = historico + [{"role": "assistant", "content": conteudo}]
        dados = json.dumps({"id": id_, "type": "message", "role": "assistant", "model": corpo["model"],
                            "content": conteudo, "stop_reason": parada, "stop_sequence": None,
                            "usage": {"input_tokens": 10, "output_tokens": 10}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)


def _parecer(**kw) -> dict[str, Any]:
    base = {"resposta": "O arroz da cesta básica tem alíquota zero de IBS e CBS (art. 125, cClassTrib 200003).",
            "citacoes": [CITACAO_CESTA], "baseSuficiente": True, "confianca": "alta", "limitacoes": [], "perguntas": []}
    return base | kw


@pytest.fixture
def api():
    ApiConsultor.requisicoes, ApiConsultor.conversas, ApiConsultor.parecer = [], {}, _parecer()
    srv = HTTPServer(("127.0.0.1", 0), ApiConsultor)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


async def _consultar(api_url: str):
    from anthropic import AsyncAnthropic

    return await consultar("O arroz paga IBS e CBS?", data="2027-01-01",
                           cliente=AsyncAnthropic(base_url=api_url, api_key="teste", max_retries=0),
                           parametros_servidor=_parametros_servidor())


@calculadora
async def test_agente_com_citacoes_validas(api):
    r = await _consultar(api)
    assert r.problemas == [] and not r.revisaoHumanaRecomendada
    assert r.base["versaoBaseDados"]
    primeira = ApiConsultor.requisicoes[0]
    assert [t["name"] for t in primeira["tools"]] == list(FERRAMENTAS)
    sistema = primeira["system"] if isinstance(primeira["system"], str) else " ".join(b["text"] for b in primeira["system"])
    assert '<trecho id="F-200003">' in sistema and "Toda afirmação jurídica precisa de citação" in sistema
    assert primeira["cache_control"] == {"type": "ephemeral"}


@calculadora
async def test_agente_que_inventa_artigo_vai_para_revisao(api):
    ApiConsultor.parecer = _parecer(resposta="Segundo o art. 999, o arroz tem alíquota zero.")
    r = await _consultar(api)
    assert r.revisaoHumanaRecomendada and any("art. 999" in p for p in r.problemas)


@calculadora
async def test_agente_com_citacao_parafraseada_vai_para_revisao(api):
    ApiConsultor.parecer = _parecer(citacoes=[{"id": "F-200003", "trecho": "o IBS e a CBS do arroz são zero por lei"}])
    r = await _consultar(api)
    assert r.revisaoHumanaRecomendada and any("palavra por palavra" in p for p in r.problemas)


@calculadora
async def test_pergunta_fora_da_base_vai_para_revisao_sem_problemas(api):
    ApiConsultor.parecer = _parecer(resposta="A base não trata do split payment.", citacoes=[], baseSuficiente=False,
                                    confianca="baixa", limitacoes=["Fora da base de fundamentação."])
    r = await _consultar(api)
    assert r.problemas == [] and r.revisaoHumanaRecomendada
