"""Testes do agente classificador fiscal.

Os testes unitários não precisam de rede. O teste de integração usa um servidor local que imita a
API da Anthropic com respostas roteirizadas, o rtc-mcp real (subprocesso) e a calculadora real; ele
só roda se a calculadora do regime regular estiver acessível.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from rtc_mcp.classificador import (
    FERRAMENTAS,
    Classificacao,
    ErroClassificador,
    _texto_final,
    classificar,
    conferir,
    formato_resposta,
    interpretar,
    montar_pedido,
)

ARROZ = {
    "status": "classificado",
    "item": "Arroz branco polido, pacote de 5 kg",
    "ncm": "10063021",
    "nbs": None,
    "cst": "200",
    "cClassTrib": "200003",
    "descricaoClassificacao": "Vendas de produtos destinados à alimentação humana (Anexo I)",
    "tratamentoTributario": "Alíquota zero",
    "reducao": {"cbs": 100.0, "ibsUf": 100.0, "ibsMun": 100.0},
    "fundamentacaoLegal": {"referencia": "LC 214/2025, art. 125", "trecho": "Ficam reduzidas a zero..."},
    "confianca": "alta",
    "justificativa": "A NCM consta do Anexo I (cesta básica).",
    "premissas": [],
    "alternativas": [],
    "perguntas": [],
}


# ----------------------------------------------------------------------
# Unitários
# ----------------------------------------------------------------------


def _objetos(no: Any):
    if isinstance(no, dict):
        if no.get("type") == "object":
            yield no
        for v in no.values():
            yield from _objetos(v)
    elif isinstance(no, list):
        for v in no:
            yield from _objetos(v)


def test_esquema_atende_structured_outputs():
    formato = formato_resposta()
    assert formato["type"] == "json_schema"
    objetos = list(_objetos(formato["schema"]))
    assert len(objetos) == 4  # Classificacao, Reducao, Fundamentacao, Alternativa
    for objeto in objetos:
        assert objeto.get("additionalProperties") is False
        assert set(objeto["required"]) == set(objeto["properties"])


def test_montar_pedido():
    texto = montar_pedido("arroz", "2027-03-10", "10063021", None, None)
    assert "Item: arroz" in texto and "2027-03-10" in texto and "10063021" in texto
    assert "Contexto da operação: não informado" in texto


def test_interpretar_valida_esquema():
    assert interpretar(json.dumps(ARROZ)).cClassTrib == "200003"
    with pytest.raises(ErroClassificador):
        interpretar(json.dumps({**ARROZ, "confianca": "total"}))


@pytest.mark.parametrize(
    ("stop_reason", "mensagem"),
    [("refusal", "recusou"), ("max_tokens", "max_tokens"), ("end_turn", "sem resposta final")],
)
def test_texto_final_erros(stop_reason, mensagem):
    msg = SimpleNamespace(stop_reason=stop_reason, content=[])
    with pytest.raises(ErroClassificador, match=mensagem):
        _texto_final(msg)


class SessaoFalsa:
    """Imita ClientSession.call_tool com respostas fixas por ferramenta."""

    def __init__(self, respostas: dict[str, tuple[bool, Any]]):
        self.respostas = respostas
        self.chamadas: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, nome: str, argumentos: dict[str, Any]):
        self.chamadas.append((nome, argumentos))
        ok, dados = self.respostas[nome]
        texto = json.dumps({"dados": dados, "metadados": {}}) if ok else str(dados)
        return SimpleNamespace(is_error=not ok, content=[SimpleNamespace(text=texto)])


def _sessao(**sobrescritas: tuple[bool, Any]) -> SessaoFalsa:
    respostas = {
        "consultar_classificacao_tributaria": (True, {"codigo": "200003"}),
        "listar_classificacoes_por_cst": (True, [{"codigo": "200001"}, {"codigo": "200003"}]),
        "verificar_ncm_aplicavel": (True, {"valido": True}),
        "verificar_nbs_aplicavel": (True, {"valido": True}),
    }
    respostas.update(sobrescritas)
    return SessaoFalsa(respostas)


async def test_conferencia_aprova_classificacao_correta():
    sessao = _sessao()
    assert await conferir(sessao, Classificacao(**ARROZ), "2027-03-10") == []
    assert ("verificar_ncm_aplicavel", {"c_class_trib": "200003", "ncm": "10063021", "data": "2027-03-10"}) in (
        sessao.chamadas
    )


async def test_conferencia_detecta_codigos_inconsistentes():
    sessao = _sessao(
        consultar_classificacao_tributaria=(False, "HTTP 404"),
        listar_classificacoes_por_cst=(True, [{"codigo": "200001"}]),
        verificar_ncm_aplicavel=(True, {"valido": False}),
    )
    falhas = await conferir(sessao, Classificacao(**ARROZ), "2027-03-10")
    assert len(falhas) == 3
    assert any("não encontrada" in f for f in falhas)
    assert any("não pertence ao CST 200" in f for f in falhas)
    assert any("NCM 10063021" in f for f in falhas)


async def test_conferencia_exige_codigos():
    sem_codigos = Classificacao(**{**ARROZ, "cst": None, "cClassTrib": None})
    assert await conferir(_sessao(), sem_codigos, "2027-03-10") == ["A resposta não traz CST e cClassTrib."]
    sem_nomenclatura = Classificacao(**{**ARROZ, "ncm": None})
    assert "A resposta não traz NCM nem NBS." in await conferir(_sessao(), sem_nomenclatura, "2027-03-10")


# ----------------------------------------------------------------------
# Integração: API simulada + rtc-mcp real + calculadora real
# ----------------------------------------------------------------------

URL_CALCULADORA = os.environ.get("RTC_REGIME_GERAL_URL", "http://localhost:8080/api")


def _calculadora_acessivel() -> bool:
    try:
        return httpx.get(f"{URL_CALCULADORA}/calculadora/dados-abertos/versao", timeout=2).is_success
    except httpx.HTTPError:
        return False


class ApiSimulada(BaseHTTPRequestHandler):
    """Responde a POST /v1/messages com um roteiro: duas chamadas de ferramenta e a resposta final.

    Emula message threads como a API: "create" guarda a conversa sob o id da resposta e
    "continue" envia só as mensagens novas a partir de previous_message_id.
    """

    requisicoes: list[dict[str, Any]] = []
    cabecalhos: list[dict[str, str]] = []
    resposta_final: dict[str, Any] = ARROZ
    parada_final = "end_turn"
    conversas: dict[str, list[dict[str, Any]]] = {}

    def log_message(self, *args: Any) -> None:  # silencia o log do http.server
        pass

    def do_POST(self) -> None:  # noqa: N802
        corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        ApiSimulada.requisicoes.append(corpo)
        ApiSimulada.cabecalhos.append(dict(self.headers))
        thread = corpo.get("thread") or {}
        anteriores = ApiSimulada.conversas[thread["previous_message_id"]] if thread.get("type") == "continue" else []
        historico = anteriores + corpo["messages"]
        passo = sum(1 for m in historico if m["role"] == "assistant")
        if passo == 0:
            conteudo = [_tool_use("t1", "listar_classificacoes_aplicaveis_ncm", {"ncm": "10063021", "data": "2027-03-10"})]
            parada = "tool_use"
        elif passo == 1:
            conteudo = [
                _tool_use(
                    "t2", "consultar_fundamentacao_legal", {"data": "2027-03-10", "c_class_trib": "200003"}
                )
            ]
            parada = "tool_use"
        else:
            conteudo = [{"type": "text", "text": json.dumps(ApiSimulada.resposta_final)}]
            parada = ApiSimulada.parada_final
        id_resposta = f"msg_{len(ApiSimulada.requisicoes)}"
        ApiSimulada.conversas[id_resposta] = historico + [{"role": "assistant", "content": conteudo}]
        resposta = json.dumps(
            {
                "id": id_resposta,
                "type": "message",
                "role": "assistant",
                "model": corpo["model"],
                "content": conteudo,
                "stop_reason": parada,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 10},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resposta)))
        self.end_headers()
        self.wfile.write(resposta)


def _tool_use(id_: str, nome: str, entrada: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": id_, "name": nome, "input": entrada}


@pytest.fixture
def api_simulada():
    ApiSimulada.requisicoes, ApiSimulada.cabecalhos, ApiSimulada.resposta_final = [], [], ARROZ
    ApiSimulada.conversas, ApiSimulada.parada_final = {}, "end_turn"
    servidor = HTTPServer(("127.0.0.1", 0), ApiSimulada)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{servidor.server_port}"
    servidor.shutdown()


def _texto_tool_result(tool_use_id: str) -> str:
    mensagens = [m for r in ApiSimulada.requisicoes for m in r["messages"]]
    for mensagem in mensagens:
        if mensagem["role"] != "user" or isinstance(mensagem["content"], str):
            continue
        for bloco in mensagem["content"]:
            if bloco.get("type") == "tool_result" and bloco["tool_use_id"] == tool_use_id:
                conteudo = bloco["content"]
                return conteudo if isinstance(conteudo, str) else "".join(c.get("text", "") for c in conteudo)
    raise AssertionError(f"tool_result {tool_use_id} não encontrado")


def _parametros_servidor():
    from mcp.client.stdio import StdioServerParameters

    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "rtc_mcp.server"],
        env={**os.environ, "RTC_REGIME_GERAL_URL": URL_CALCULADORA},
    )


@pytest.mark.skipif(not _calculadora_acessivel(), reason="calculadora do regime regular não está acessível")
async def test_loop_completo_com_calculadora_real(api_simulada):
    from anthropic import AsyncAnthropic

    cliente = AsyncAnthropic(base_url=api_simulada, api_key="teste", max_retries=0)
    chamadas: list[str] = []
    resultado = await classificar(
        "arroz branco polido, pacote 5 kg",
        data="2027-03-10",
        contexto="venda de supermercado a consumidor final",
        cliente=cliente,
        parametros_servidor=_parametros_servidor(),
        progresso=chamadas.append,
    )

    # A conferência determinística confirmou os códigos na calculadora real.
    assert resultado.classificacao.cClassTrib == "200003"
    assert resultado.verificacoes == ["CST, cClassTrib e NCM/NBS confirmados na calculadora."]
    assert resultado.revisaoHumanaRecomendada is False
    assert len(chamadas) == 2 and "listar_classificacoes_aplicaveis_ncm" in chamadas[0]

    primeira = ApiSimulada.requisicoes[0]
    assert len(ApiSimulada.requisicoes) == 3
    assert primeira["model"] == "claude-opus-5-5"
    assert [t["name"] for t in primeira["tools"]] == list(FERRAMENTAS)
    assert primeira["thinking"] == {"type": "adaptive"}
    assert primeira["output_config"]["effort"] == "high"
    assert primeira["output_config"]["format"]["type"] == "json_schema"
    assert primeira["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in ApiSimulada.cabecalhos[0].get("anthropic-beta", "")
    assert "Data do fato gerador: 2027-03-10" in primeira["messages"][0]["content"]

    # Os resultados das ferramentas vieram da calculadora real, pelo MCP.
    candidatas = json.loads(_texto_tool_result("t1"))["dados"]
    assert "200003" in [c["cClassTrib"] for c in candidatas["doAnexo"]]
    fundamentacao = _texto_tool_result("t2")
    assert "Art. 125" in fundamentacao


@pytest.mark.skipif(not _calculadora_acessivel(), reason="calculadora do regime regular não está acessível")
async def test_codigo_inventado_pelo_modelo_vai_para_revisao(api_simulada):
    from anthropic import AsyncAnthropic

    # O modelo "erra": usa a cClassTrib 200035, cujo anexo não inclui a NCM do arroz.
    ApiSimulada.resposta_final = {**ARROZ, "cClassTrib": "200035"}
    cliente = AsyncAnthropic(base_url=api_simulada, api_key="teste", max_retries=0)
    resultado = await classificar(
        "arroz", data="2027-03-10", cliente=cliente, parametros_servidor=_parametros_servidor()
    )
    assert resultado.revisaoHumanaRecomendada is True
    assert any("NCM 10063021 não é aceita" in v for v in resultado.verificacoes)


@pytest.mark.skipif(not _calculadora_acessivel(), reason="calculadora do regime regular não está acessível")
async def test_recusa_vira_erro_do_classificador(api_simulada):
    from anthropic import AsyncAnthropic

    ApiSimulada.parada_final = "refusal"
    cliente = AsyncAnthropic(base_url=api_simulada, api_key="teste", max_retries=0)
    with pytest.raises(ErroClassificador, match="recusou"):
        await classificar("arroz", data="2027-03-10", cliente=cliente, parametros_servidor=_parametros_servidor())


@pytest.mark.skipif(not _calculadora_acessivel(), reason="calculadora do regime regular não está acessível")
async def test_limite_de_iteracoes(api_simulada):
    from anthropic import AsyncAnthropic

    cliente = AsyncAnthropic(base_url=api_simulada, api_key="teste", max_retries=0)
    with pytest.raises(ErroClassificador, match="limite de 1 iterações"):
        await classificar(
            "arroz", data="2027-03-10", cliente=cliente, parametros_servidor=_parametros_servidor(), max_iteracoes=1
        )


async def test_servidor_mcp_que_nao_inicia():
    from mcp.client.stdio import StdioServerParameters

    with pytest.raises(ErroClassificador, match="servidor MCP"):
        await classificar(
            "arroz",
            data="2027-03-10",
            cliente=object(),
            parametros_servidor=StdioServerParameters(command=sys.executable, args=["-c", "raise SystemExit(3)"]),
        )
