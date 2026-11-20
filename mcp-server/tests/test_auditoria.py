"""Testes da auditoria de NF-e (auditoria.py, ferramenta auditar_nfe) e do agente rtc-auditor.

As notas de teste (tests/fixtures/nfe_*_conforme.xml) foram geradas com as calculadoras oficiais e
passam no XSD. Os testes de mutação estragam um campo por vez e conferem que a auditoria acusa
exatamente esse erro.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from rtc_mcp.agente_auditor import (
    FERRAMENTAS,
    ClassificacaoSugerida,
    Relatorio,
    auditar_nota,
    formato_resposta,
    verificar,
)
from rtc_mcp.auditoria import ErroAuditoria, auditar, extrair, ler_xml
from rtc_mcp.client import RtcClient
from test_classificador import _calculadora_acessivel, _parametros_servidor

FIXTURES = Path(__file__).parent / "fixtures"
NFE_2026 = (FIXTURES / "nfe_2026_conforme.xml").read_text()
NFE_2027 = (FIXTURES / "nfe_2027_conforme.xml").read_text()

calculadora = pytest.mark.skipif(not _calculadora_acessivel(), reason="calculadora do regime regular não está acessível")


def mudar_item(xml: str, n: int, antigo: str, novo: str) -> str:
    """Troca `antigo` por `novo` só dentro do <det nItem="n">."""
    padrao = re.compile(rf'(<det nItem="{n}">.*?</det>)', re.S)
    bloco = padrao.search(xml).group(1)
    assert antigo in bloco, f"{antigo!r} não está no item {n}"
    return xml.replace(bloco, bloco.replace(antigo, novo, 1))


def _tipos(r: dict[str, Any], gravidade: str = "erro") -> set[tuple[Any, str]]:
    return {(a["item"], a["tipo"]) for a in r["achados"] if a["gravidade"] == gravidade}


# ----------------------------------------------------------------------
# Leitura do XML (sem calculadora)
# ----------------------------------------------------------------------


def test_extrair_nota_e_itens():
    nota = extrair(ler_xml(NFE_2026))
    assert (nota["modelo"], nota["data"], nota["municipio"], nota["uf"], nota["crt"]) == ("55", "2026-06-10", "2111300", "MA", "3")
    assert [i["ncm"] for i in nota["itens"]] == ["10063021", "34011190", "48202000"]
    sabonete = nota["itens"][1]
    assert sabonete["ibscbs"]["cClassTrib"] == "200035" and sabonete["ibscbs"]["pRedAliqCBS"] == "60.00"
    assert sabonete["componentes"]["vProd"] == "1000.00" and sabonete["componentes"]["vICMS"] == "180.00"
    assert nota["itens"][2]["componentes"]["vFrete"] == "100.00" and nota["itens"][2]["componentes"]["vDesc"] == "50.00"
    assert nota["totalIBSCBS"]["vBCIBSCBS"]


def test_xml_perigoso_ou_invalido_e_recusado():
    with pytest.raises(ErroAuditoria, match="DOCTYPE"):
        ler_xml('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>')
    with pytest.raises(ErroAuditoria, match="malformado"):
        ler_xml("<NFe><infNFe>")
    with pytest.raises(ErroAuditoria, match="infNFe"):
        ler_xml("<outra/>")


async def test_nota_anterior_a_2026_nao_e_auditada():
    with pytest.raises(ErroAuditoria, match="só existem a partir de 2026"):
        await auditar(RtcClient(), NFE_2026.replace("2026-06-10T", "2025-06-10T"))


def test_esquema_do_relatorio():
    esquema = json.dumps(formato_resposta()["schema"])
    assert '"additionalProperties": false' in esquema


# ----------------------------------------------------------------------
# Auditoria com a calculadora real
# ----------------------------------------------------------------------


@calculadora
@pytest.mark.parametrize("xml", [NFE_2026, NFE_2027], ids=["2026", "2027"])
async def test_nota_conforme(xml):
    r = await auditar(RtcClient(), xml)
    assert r["situacao"] == "conforme", r["achados"]
    assert r["resumo"] == {"itens": 3, "itensRecalculados": 3, "erros": 0, "alertas": 0}
    assert len(r["comparacoes"]) == 3


@calculadora
async def test_nota_de_2027_usa_aliquotas_declaradas_como_premissa():
    r = await auditar(RtcClient(), NFE_2027)
    assert "alíquotas nominais declaradas" in r["premissas"][0]


@calculadora
async def test_cclasstrib_que_nao_aceita_a_ncm():
    xml = mudar_item(NFE_2026, 1, "<cClassTrib>200003</cClassTrib>", "<cClassTrib>200035</cClassTrib>")
    r = await auditar(RtcClient(), xml)
    assert (1, "classificacao") in _tipos(r)
    assert any("REG-011" in a["mensagem"] for a in r["achados"])


@calculadora
async def test_base_de_calculo_inflada():
    nota = extrair(ler_xml(NFE_2026))
    vbc = nota["itens"][2]["ibscbs"]["vBC"]
    xml = mudar_item(NFE_2026, 3, f"<vBC>{vbc}</vBC>", "<vBC>2050.00</vBC>")
    r = await auditar(RtcClient(), xml)
    base = next(a for a in r["achados"] if a["tipo"] == "base" and a["gravidade"] == "erro")
    assert (base["item"], base["declarado"], base["calculado"]) == (3, "2050.00", vbc)
    assert (None, "total") in _tipos(r)  # o total não acompanha o item alterado


@calculadora
async def test_valor_de_cbs_errado():
    nota = extrair(ler_xml(NFE_2026))
    vcbs = nota["itens"][1]["ibscbs"]["vCBS"]
    xml = mudar_item(NFE_2026, 2, f"<vCBS>{vcbs}</vCBS>", "<vCBS>9.00</vCBS>")
    r = await auditar(RtcClient(), xml)
    valor = next(a for a in r["achados"] if a["tipo"] == "valor")
    assert (valor["item"], valor["campo"], valor["declarado"], valor["calculado"]) == (2, "vCBS", "9.00", vcbs)


@calculadora
async def test_reducao_esquecida():
    xml = re.sub(r'(<det nItem="2">.*?<gCBS><pCBS>[^<]*</pCBS>)<gRed>.*?</gRed>', r"\1", NFE_2026, count=1, flags=re.S)
    r = await auditar(RtcClient(), xml)
    assert (2, "reducao") in _tipos(r)


@calculadora
async def test_aliquota_nominal_errada_em_2026():
    xml = mudar_item(NFE_2026, 3, "<pCBS>0.90</pCBS>", "<pCBS>1.00</pCBS>")
    r = await auditar(RtcClient(), xml)
    assert any(a["campo"] == "pCBS" and a["item"] == 3 for a in r["achados"])


@calculadora
async def test_total_divergente():
    xml = re.sub(r"(<IBSCBSTot>.*?<gCBS>.*?<vCBS>)[^<]*(</vCBS>)", r"\g<1>999.99\2", NFE_2026, count=1, flags=re.S)
    r = await auditar(RtcClient(), xml)
    assert _tipos(r) == {(None, "total")}


@calculadora
async def test_xml_invalido_no_xsd():
    xml = mudar_item(NFE_2026, 1, "<CFOP>5102</CFOP>", "")
    r = await auditar(RtcClient(), xml)
    assert (None, "xsd") in _tipos(r)


@calculadora
async def test_emitente_do_simples_so_tem_estrutura_e_classificacao_conferidas():
    nota = extrair(ler_xml(NFE_2026))
    vcbs = nota["itens"][1]["ibscbs"]["vCBS"]
    xml = mudar_item(NFE_2026.replace("<CRT>3</CRT>", "<CRT>1</CRT>"), 2, f"<vCBS>{vcbs}</vCBS>", "<vCBS>9.00</vCBS>")
    r = await auditar(RtcClient(), xml)
    assert (None, "escopo") in _tipos(r, "info")
    assert not any(a["tipo"] == "valor" for a in r["achados"])


@calculadora
async def test_ferramenta_mcp_auditar_nfe(tmp_path):
    from rtc_mcp.server import criar_servidor

    arquivo = tmp_path / "nota.xml"
    arquivo.write_text(NFE_2026)
    from test_server import chamar

    erro, corpo = await chamar(criar_servidor(), "auditar_nfe", {"caminho_xml": str(arquivo)})
    assert not erro and corpo["dados"]["situacao"] == "conforme"
    erro, texto = await chamar(criar_servidor(), "auditar_nfe", {"xml": NFE_2026.replace("2026-06-10T", "2025-06-10T")})
    assert erro and "a partir de 2026" in texto


# ----------------------------------------------------------------------
# Agente: API simulada + rtc-mcp real + calculadora real
# ----------------------------------------------------------------------


class ApiAuditor(BaseHTTPRequestHandler):
    relatorio: dict[str, Any] = {}
    requisicoes: list[dict[str, Any]] = []
    conversas: dict[str, list[dict[str, Any]]] = {}

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        ApiAuditor.requisicoes.append(corpo)
        thread = corpo.get("thread") or {}
        anteriores = ApiAuditor.conversas.get(thread.get("previous_message_id"), []) if thread.get("type") == "continue" else []
        historico = anteriores + corpo["messages"]
        if not any(m["role"] == "assistant" for m in historico):
            caminho = re.search(r"arquivo (\S+) ", historico[0]["content"]).group(1)
            conteudo = [{"type": "tool_use", "id": "t1", "name": "auditar_nfe", "input": {"caminho_xml": caminho}}]
            parada = "tool_use"
        else:
            conteudo = [{"type": "text", "text": json.dumps(ApiAuditor.relatorio)}]
            parada = "end_turn"
        id_ = f"msg_{len(ApiAuditor.requisicoes)}"
        ApiAuditor.conversas[id_] = historico + [{"role": "assistant", "content": conteudo}]
        dados = json.dumps({"id": id_, "type": "message", "role": "assistant", "model": corpo["model"],
                            "content": conteudo, "stop_reason": parada, "stop_sequence": None,
                            "usage": {"input_tokens": 10, "output_tokens": 10}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)


def _relatorio(**kw) -> dict[str, Any]:
    base = {"status": "conforme", "resumo": "ok", "divergencias": [], "classificacoesSugeridas": [],
            "perguntas": [], "ressalvas": []}
    return base | kw


def _divergencia(item, tipo="classificacao") -> dict[str, Any]:
    return {"item": item, "tipo": tipo, "descricao": "d", "impacto": "i", "correcao": "c", "confianca": "alta"}


@pytest.fixture
def api():
    ApiAuditor.requisicoes, ApiAuditor.conversas, ApiAuditor.relatorio = [], {}, _relatorio()
    srv = HTTPServer(("127.0.0.1", 0), ApiAuditor)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


async def _rodar(api_url: str, xml: str, tmp_path: Path):
    from anthropic import AsyncAnthropic

    arquivo = tmp_path / "nota.xml"
    arquivo.write_text(xml)
    return await auditar_nota(
        arquivo, cliente=AsyncAnthropic(base_url=api_url, api_key="teste", max_retries=0),
        parametros_servidor=_parametros_servidor(),
    )


@calculadora
async def test_agente_nota_conforme(api, tmp_path):
    r = await _rodar(api, NFE_2026, tmp_path)
    assert r.auditoria["situacao"] == "conforme"
    assert r.verificacoes == ["Relatório conferido com a auditoria determinística."]
    assert r.revisaoHumanaRecomendada is False
    primeira = ApiAuditor.requisicoes[0]
    assert [t["name"] for t in primeira["tools"]] == list(FERRAMENTAS)
    assert primeira["fallbacks"] == "default" and primeira["output_config"]["format"]["type"] == "json_schema"


@calculadora
async def test_agente_que_ignora_divergencias_vai_para_revisao(api, tmp_path):
    xml = mudar_item(NFE_2026, 1, "<cClassTrib>200003</cClassTrib>", "<cClassTrib>200035</cClassTrib>")
    r = await _rodar(api, xml, tmp_path)  # o relatório simulado diz "conforme"
    assert r.revisaoHumanaRecomendada is True
    assert any("difere da auditoria" in v for v in r.verificacoes)
    assert any("sem explicação" in v and "item 1" in v for v in r.verificacoes)


@calculadora
async def test_agente_com_sugestao_correta(api, tmp_path):
    xml = mudar_item(NFE_2026, 1, "<cClassTrib>200003</cClassTrib>", "<cClassTrib>200035</cClassTrib>")
    ApiAuditor.relatorio = _relatorio(
        status="com_divergencias", divergencias=[_divergencia(1)],
        classificacoesSugeridas=[{"item": 1, "cClassTribAtual": "200035", "cstSugerido": "200",
                                  "cClassTribSugerida": "200003", "justificativa": "arroz no Anexo I"}],
    )
    r = await _rodar(api, xml, tmp_path)
    assert r.verificacoes == ["Relatório conferido com a auditoria determinística."]
    assert r.revisaoHumanaRecomendada is True  # há divergências a corrigir


@calculadora
async def test_sugestao_de_classificacao_invalida_e_detectada():
    auditoria = await auditar(RtcClient(), mudar_item(NFE_2026, 1, "<cClassTrib>200003</cClassTrib>", "<cClassTrib>200035</cClassTrib>"))
    relatorio = Relatorio(**_relatorio(
        status="com_divergencias", divergencias=[_divergencia(1)],
        classificacoesSugeridas=[ClassificacaoSugerida(item=1, cClassTribAtual="200035", cstSugerido="000",
                                                       cClassTribSugerida="200034", justificativa="x").model_dump()],
    ))
    problemas = await verificar(RtcClient(), relatorio, auditoria)
    assert any("não pertence ao CST 000" in p for p in problemas)
    assert any("não aceita a NCM 10063021" in p for p in problemas)
