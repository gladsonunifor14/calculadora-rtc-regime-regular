"""Testes das ferramentas MCP com as APIs simuladas (respx)."""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import httpx
import pytest
import respx

from rtc_mcp.client import Config, RtcClient
from rtc_mcp.server import ToolError, criar_servidor

REG = "http://regime.test/api"
SN = "http://simples.test/api"
DANFSE = "http://danfse.test"
PDF = b"%PDF-1.7 conteudo de teste"
NFSE = '<NFSe versao="1.00"><infNFSe Id="NFS3550308123" dhProc="2026-07-14T10:00:00-03:00"></infNFSe></NFSe>'
CABECALHOS = {"X-CALC-APP-VERSION": "1.6.1", "X-CALC-DB-VERSION": "2026.11.01"}


@pytest.fixture
def pasta_pdf(tmp_path):
    return tmp_path / "pdfs"


@pytest.fixture
def servidor(pasta_pdf):
    cliente = RtcClient(
        Config(
            regime_geral_url=REG,
            simples_nacional_url=SN,
            timeout=5,
            danfse_url=DANFSE,
            danfse_dir=str(pasta_pdf),
        )
    )
    return criar_servidor(cliente)


async def chamar(servidor, nome: str, argumentos: dict[str, Any]) -> tuple[bool, Any]:
    """Chama a ferramenta e devolve (erro?, conteúdo decodificado).

    Chamada direta, o servidor levanta ToolError; pelo protocolo, o mesmo erro chega
    ao cliente como resultado com is_error=True e a mensagem como texto.
    """
    try:
        resultado = await servidor.call_tool(nome, argumentos)
    except ToolError as exc:
        return True, str(exc)
    # SDK 2.x devolve CallToolResult; 1.x devolve (conteúdo, estruturado) ou lista de conteúdos.
    if hasattr(resultado, "content"):
        erro = bool(getattr(resultado, "is_error", getattr(resultado, "isError", False)))
        conteudo = resultado.content
    else:
        erro = False
        conteudo = resultado[0] if isinstance(resultado, tuple) else resultado
    texto = conteudo[0].text
    try:
        return erro, json.loads(texto)
    except ValueError:
        return erro, texto


async def test_lista_todas_as_ferramentas(servidor):
    nomes = {t.name for t in await servidor.list_tools()}
    assert {
        "calcular_tributos_regime_geral",
        "calcular_tributos_simples_nacional",
        "verificar_ncm_aplicavel",
        "consultar_fundamentacao_legal",
        "gerar_xml_documento",
        "validar_xml_documento",
        "listar_atividades_simples_nacional",
        "consultar_versoes",
        "gerar_danfse",
    } <= nomes


@respx.mock
async def test_calculo_regime_geral_usa_observabilidade_e_devolve_metadados(servidor):
    rota = respx.post(f"{REG}/calculadora/observabilidade/regime-geral").mock(
        return_value=httpx.Response(
            200,
            json={"objetos": [{"nObj": 1, "estadoItem": "CALCULO_SIMULADO"}]},
            headers={**CABECALHOS, "x-warning-dados-simulados": "1"},
        )
    )
    operacao = {"id": "x", "itens": [{"numero": 1}]}
    erro, corpo = await chamar(servidor, "calcular_tributos_regime_geral", {"operacao": operacao})

    assert not erro
    assert json.loads(rota.calls.last.request.content) == operacao
    assert corpo["dados"]["objetos"][0]["estadoItem"] == "CALCULO_SIMULADO"
    assert corpo["metadados"]["versaoAplicacao"] == "1.6.1"
    assert corpo["metadados"]["versaoBaseDados"] == "2026.11.01"
    assert corpo["metadados"]["avisoDadosSimulados"].startswith("cálculo com alíquotas simuladas (código 1)")


@respx.mock
async def test_calculo_regime_geral_modo_estrito(servidor):
    rota = respx.post(f"{REG}/calculadora/regime-geral").mock(return_value=httpx.Response(200, json={"ok": 1}))
    erro, _ = await chamar(servidor, "calcular_tributos_regime_geral", {"operacao": {}, "modo": "estrito"})
    assert not erro and rota.called


@respx.mock
async def test_erro_de_validacao_da_api_volta_para_o_modelo(servidor):
    problema = {"status": 400, "title": "Dados inválidos", "objects": [{"name": "uf", "userMessage": "obrigatório"}]}
    respx.post(f"{REG}/calculadora/regime-geral").mock(return_value=httpx.Response(400, json=problema))
    erro, texto = await chamar(servidor, "calcular_tributos_regime_geral", {"operacao": {}, "modo": "estrito"})
    assert erro
    assert "HTTP 400" in texto and "obrigatório" in texto


@respx.mock
async def test_calculadora_fora_do_ar_gera_mensagem_clara(servidor):
    respx.get(f"{REG}/calculadora/dados-abertos/ncm").mock(side_effect=httpx.ConnectError("recusada"))
    erro, texto = await chamar(servidor, "consultar_ncm", {"ncm": "10063021", "data": "2027-01-01"})
    assert erro
    assert "Não foi possível conectar" in texto and REG in texto


async def test_parametros_invalidos_sao_recusados_antes_da_chamada(servidor):
    with respx.mock(assert_all_called=False) as mock:
        erro, _ = await chamar(servidor, "consultar_ncm", {"ncm": "1006.30", "data": "01/01/2027"})
        assert erro
        assert not mock.calls


@respx.mock
async def test_verificar_ncm_aplicavel_mapeia_parametros(servidor):
    rota = respx.get(f"{REG}/calculadora/dados-abertos/classificacoes-tributarias/ncm-aplicavel").mock(
        return_value=httpx.Response(200, json={"aplicavel": True})
    )
    erro, corpo = await chamar(
        servidor,
        "verificar_ncm_aplicavel",
        {"c_class_trib": "200003", "ncm": "10063021", "data": "2027-03-10"},
    )
    assert not erro and corpo["dados"] == {"aplicavel": True}
    assert dict(rota.calls.last.request.url.params) == {
        "cClassTrib": "200003",
        "ncm": "10063021",
        "dataOcorrenciaFatoGerador": "2027-03-10",
    }


@respx.mock
async def test_fundamentacao_legal_filtra_por_cclasstrib(servidor):
    respx.get(f"{REG}/calculadora/dados-abertos/fundamentacoes-legais").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"codigoClassificacaoTributaria": "200003", "codigoSituacaoTributaria": "200", "texto": "a"},
                {"codigoClassificacaoTributaria": "000001", "codigoSituacaoTributaria": "000", "texto": "b"},
            ],
        )
    )
    erro, corpo = await chamar(
        servidor, "consultar_fundamentacao_legal", {"data": "2027-01-01", "c_class_trib": "200003"}
    )
    assert not erro
    assert [f["texto"] for f in corpo["dados"]] == ["a"]


@respx.mock
async def test_aliquotas_referencia_combina_uniao_uf_e_municipio(servidor):
    base = f"{REG}/calculadora/dados-abertos"
    respx.get(f"{base}/aliquota-uniao").mock(return_value=httpx.Response(200, json={"aliquota": 8.8}))
    rota_uf = respx.get(f"{base}/aliquota-uf").mock(return_value=httpx.Response(200, json={"aliquota": 0.1}))
    respx.get(f"{base}/aliquota-municipio").mock(return_value=httpx.Response(200, json={"aliquota": 0.05}))
    erro, corpo = await chamar(
        servidor,
        "consultar_aliquotas_referencia",
        {"data": "2027-01-01", "codigo_uf": 43, "codigo_municipio": 4314902},
    )
    assert not erro
    assert corpo["dados"] == {"uniao": {"aliquota": 8.8}, "uf": {"aliquota": 0.1}, "municipio": {"aliquota": 0.05}}
    assert rota_uf.calls.last.request.url.params["codigoUf"] == "43"


@respx.mock
async def test_aliquotas_referencia_tolera_esfera_sem_aliquota(servidor):
    base = f"{REG}/calculadora/dados-abertos"
    nao_encontrada = httpx.Response(404, json={"title": "Alíquota não encontrada"})
    respx.get(f"{base}/aliquota-uniao").mock(return_value=nao_encontrada)
    respx.get(f"{base}/aliquota-uf").mock(return_value=httpx.Response(200, json={"aliquotaReferencia": 0.1}))
    erro, corpo = await chamar(servidor, "consultar_aliquotas_referencia", {"data": "2027-01-01", "codigo_uf": 43})
    assert not erro
    assert corpo["dados"]["uf"] == {"aliquotaReferencia": 0.1}
    assert "Alíquota não encontrada" in corpo["dados"]["uniao"]["indisponivel"]

    erro, texto = await chamar(servidor, "consultar_aliquotas_referencia", {"data": "2027-01-01"})
    assert erro and "Nenhuma alíquota" in texto


@respx.mock
async def test_aviso_dos_dados_abertos_tem_significado_proprio(servidor):
    respx.get(f"{REG}/calculadora/dados-abertos/ncm").mock(
        return_value=httpx.Response(200, json={}, headers={"x-warning-dados-simulados": "1"})
    )
    _, corpo = await chamar(servidor, "consultar_ncm", {"ncm": "22030000", "data": "2027-01-01"})
    assert "Imposto Seletivo" in corpo["metadados"]["avisoDadosSimulados"]


@respx.mock
async def test_validar_xml_envia_corpo_xml(servidor):
    rota = respx.post(f"{REG}/calculadora/xml/validate").mock(return_value=httpx.Response(200, json=True))
    erro, corpo = await chamar(servidor, "validar_xml_documento", {"xml": "<IBSCBS/>", "tipo": "nfe"})
    assert not erro and corpo["dados"] is True
    requisicao = rota.calls.last.request
    assert requisicao.content == b"<IBSCBS/>"
    assert requisicao.headers["content-type"] == "application/xml"
    assert dict(requisicao.url.params) == {"tipo": "nfe", "subtipo": "grupo"}


@respx.mock
async def test_calculo_simples_nacional(servidor):
    rota = respx.post(f"{SN}/calculadora/observabilidade/simples-nacional").mock(
        return_value=httpx.Response(200, json={"objetos": []}, headers=CABECALHOS)
    )
    erro, corpo = await chamar(servidor, "calcular_tributos_simples_nacional", {"operacao": {"CRT": "1"}})
    assert not erro and rota.called
    assert corpo["metadados"]["versaoAplicacao"] == "1.6.1"


@respx.mock
async def test_atividades_simples_omitem_filtros_vazios_e_juntam_anexos(servidor):
    rota = respx.get(f"{SN}/calculadora/simples-nacional/atividades").mock(return_value=httpx.Response(200, json=[]))
    erro, _ = await chamar(
        servidor, "listar_atividades_simples_nacional", {"data": "2027-01-01", "anexos": ["I", "III"]}
    )
    assert not erro
    assert dict(rota.calls.last.request.url.params) == {"data": "2027-01-01", "anexos": "I,III"}


@respx.mock
async def test_atividade_inexistente(servidor):
    respx.get(f"{SN}/calculadora/simples-nacional/atividades/99").mock(return_value=httpx.Response(404))
    erro, texto = await chamar(servidor, "consultar_atividade_simples_nacional", {"codigo": "99", "data": "2027-01-01"})
    assert erro and "404" in texto


@respx.mock
async def test_versoes_toleram_calculadora_indisponivel(servidor):
    respx.get(f"{REG}/calculadora/dados-abertos/versao").mock(
        return_value=httpx.Response(200, json={"versaoApp": "1.6.1"})
    )
    respx.get(f"{SN}/versao").mock(side_effect=httpx.ConnectError("recusada"))
    respx.get(f"{DANFSE}/health/ready").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    erro, corpo = await chamar(servidor, "consultar_versoes", {})
    assert not erro
    assert corpo["dados"]["regimeGeral"] == {"versaoApp": "1.6.1"}
    assert "indisponivel" in corpo["dados"]["simplesNacional"]
    assert corpo["dados"]["geradorDanfse"] == {"status": "ok"}


# ----------------------------------------------------------------------
# gerar_danfse
# ----------------------------------------------------------------------


def _mock_danfse(resposta: httpx.Response | None = None):
    return respx.post(f"{DANFSE}/api/danfse").mock(
        return_value=resposta or httpx.Response(200, content=PDF, headers={"Content-Type": "application/pdf"})
    )


@respx.mock
async def test_gerar_danfse_grava_pdf_com_nome_do_id(servidor, pasta_pdf):
    rota = _mock_danfse()
    erro, corpo = await chamar(servidor, "gerar_danfse", {"xml": NFSE})

    assert not erro
    destino = pasta_pdf / "danfse-NFS3550308123.pdf"
    assert corpo["dados"] == {
        "arquivo": str(destino),
        "tamanhoBytes": len(PDF),
        "situacao": "valida",
        "canhoto": True,
    }
    assert destino.read_bytes() == PDF
    requisicao = rota.calls.last.request
    assert requisicao.content == NFSE.encode()
    assert requisicao.headers["content-type"] == "application/xml"
    assert dict(requisicao.url.params) == {"stc": "0", "canhoto": "true"}


@respx.mock
async def test_gerar_danfse_situacao_canhoto_e_base64(servidor):
    rota = _mock_danfse()
    erro, corpo = await chamar(
        servidor,
        "gerar_danfse",
        {"xml": NFSE, "situacao": "cancelada", "canhoto": False, "incluir_pdf_base64": True},
    )
    assert not erro
    assert dict(rota.calls.last.request.url.params) == {"stc": "2", "canhoto": "false"}
    assert base64.b64decode(corpo["dados"]["pdfBase64"]) == PDF


@respx.mock
async def test_gerar_danfse_nunca_sobrescreve(servidor, pasta_pdf):
    _mock_danfse()
    pasta_pdf.mkdir(parents=True)
    (pasta_pdf / "nota.pdf").write_bytes(b"original")

    _, corpo = await chamar(servidor, "gerar_danfse", {"xml": NFSE, "nome_arquivo": "nota"})

    assert corpo["dados"]["arquivo"] == str(pasta_pdf / "nota-1.pdf")
    assert (pasta_pdf / "nota.pdf").read_bytes() == b"original"


@respx.mock
async def test_gerar_danfse_le_xml_de_arquivo(servidor, tmp_path):
    rota = _mock_danfse()
    origem = tmp_path / "nota.xml"
    origem.write_text(NFSE, encoding="utf-8")
    erro, _ = await chamar(servidor, "gerar_danfse", {"caminho_xml": str(origem)})
    assert not erro
    assert rota.calls.last.request.content == NFSE.encode()


async def test_gerar_danfse_exige_exatamente_uma_origem(servidor, tmp_path):
    with respx.mock(assert_all_called=False) as mock:
        erro, texto = await chamar(servidor, "gerar_danfse", {})
        assert erro and "exatamente um" in texto
        erro, texto = await chamar(servidor, "gerar_danfse", {"xml": NFSE, "caminho_xml": "a.xml"})
        assert erro and "exatamente um" in texto
        erro, texto = await chamar(servidor, "gerar_danfse", {"caminho_xml": str(tmp_path / "x.txt")})
        assert erro and ".xml" in texto
        erro, _ = await chamar(servidor, "gerar_danfse", {"xml": NFSE, "nome_arquivo": "../fora.pdf"})
        assert erro
        assert not mock.calls


@respx.mock
async def test_gerar_danfse_repassa_erro_do_gerador(servidor, pasta_pdf):
    _mock_danfse(httpx.Response(400, json={"sucesso": False, "mensagem": "infNFSe Id is empty"}))
    erro, texto = await chamar(servidor, "gerar_danfse", {"xml": "<NFSe/>"})
    assert erro
    assert "HTTP 400" in texto and "infNFSe Id is empty" in texto
    assert not pasta_pdf.exists()


@respx.mock
async def test_gerar_danfse_recusa_resposta_que_nao_e_pdf(servidor):
    _mock_danfse(httpx.Response(200, content=b"<html>proxy</html>"))
    erro, texto = await chamar(servidor, "gerar_danfse", {"xml": NFSE})
    assert erro and "não é um PDF" in texto


def test_nome_padrao_sem_id_usa_data_e_hora():
    from rtc_mcp.server import _nome_padrao_danfse

    assert _nome_padrao_danfse('<ns:infNFSe Id="A/B 1">') == "danfse-A_B_1.pdf"
    assert re.fullmatch(r"danfse-\d{8}-\d{6}\.pdf", _nome_padrao_danfse("<NFSe/>"))
