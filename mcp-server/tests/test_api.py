"""Testes da API HTTP (rtc_api): chaves, cotas, rotas e documentação."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rtc_api.app import criar_app
from rtc_api.banco import Banco, agora
from rtc_api.config import Configuracao, ErroConfiguracao, PLANOS_PADRAO, validar_planos
from rtc_api.seguranca import gerar_chave, hash_chave, prefixo_de
from test_classificador import _calculadora_acessivel

ADMIN = "t" * 40
PEPPER = "p" * 40
calculadora = pytest.mark.skipif(not _calculadora_acessivel(), reason="calculadora do regime regular não está acessível")


class Relogio:
    def __init__(self):
        self.momento = datetime(2027, 3, 10, 12, 0, 30, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.momento


class Resultado:
    def __init__(self, dados: dict[str, Any]):
        self.dados = dados

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        return self.dados


@pytest.fixture
def ambiente(tmp_path):
    relogio = Relogio()
    chamadas: list[tuple[str, tuple, dict]] = []

    def fake(nome: str, falha: Exception | None = None):
        async def executar(*args, **kwargs):
            chamadas.append((nome, args, kwargs))
            if ambiente_estado.get("falha"):
                raise ambiente_estado["falha"]
            if nome == "auditor":
                return Resultado({"agente": nome, "xml": Path(args[0]).read_text()})
            return Resultado({"agente": nome})
        return executar

    ambiente_estado: dict[str, Any] = {}
    executores = {n: fake(n) for n in ("classificador", "comparador", "auditor", "corretor", "consultor")}
    config = Configuracao(token_admin=ADMIN, pepper=PEPPER, banco=tmp_path / "api.db", docs_admin=False)
    app = criar_app(config, executores=executores, relogio=relogio)
    with TestClient(app) as cliente:
        yield SimpleNamespace(cliente=cliente, relogio=relogio, chamadas=chamadas, estado=ambiente_estado,
                              banco=config.banco, config=config)


def emitir(amb, plano="completo", referencia="compra-1", **extra) -> dict[str, Any]:
    corpo = {"cliente": "12345678000199", "referenciaCompra": referencia, "plano": plano, "validadeDias": 30} | extra
    r = amb.cliente.post("/admin/chaves", json=corpo, headers={"X-Admin-Token": ADMIN})
    assert r.status_code == 201, r.text
    return r.json()


def h(chave: str) -> dict[str, str]:
    return {"X-API-Key": chave}


PEDIDO_CONSULTA = {"pergunta": "Educação tem redução de IBS?"}


# ----------------------------------------------------------------------
# Configuração e chaves
# ----------------------------------------------------------------------


def test_configuracao_exige_segredos(monkeypatch):
    monkeypatch.setenv("RTC_API_ADMIN_TOKEN", "curto")
    monkeypatch.setenv("RTC_API_PEPPER", "p" * 40)
    with pytest.raises(ErroConfiguracao, match="RTC_API_ADMIN_TOKEN"):
        Configuracao.from_env()
    monkeypatch.setenv("RTC_API_ADMIN_TOKEN", "t" * 40)
    monkeypatch.delenv("RTC_API_PEPPER")
    with pytest.raises(ErroConfiguracao, match="RTC_API_PEPPER"):
        Configuracao.from_env()


def test_planos_invalidos():
    validar_planos(PLANOS_PADRAO)
    with pytest.raises(ErroConfiguracao, match="escopos desconhecidos"):
        validar_planos({"x": {"escopos": ["tudo"], "cotaCalculo": 1, "cotaAgentes": 1, "limitePorMinuto": 1}})
    with pytest.raises(ErroConfiguracao, match="inteiro"):
        validar_planos({"x": {"escopos": [], "cotaCalculo": -1, "cotaAgentes": 1, "limitePorMinuto": 1}})


def test_formato_e_hash_da_chave():
    chave, prefixo = gerar_chave()
    assert prefixo_de(chave) == prefixo and len(chave) == 4 + 12 + 1 + 43
    assert hash_chave(chave, PEPPER) != hash_chave(chave, "outro" * 10)
    assert prefixo_de("rtc_123_abc") is None and prefixo_de("") is None


def test_banco_guarda_so_o_hash(ambiente):
    emitida = emitir(ambiente)
    conteudo = Path(ambiente.banco).read_bytes()
    assert emitida["chave"].encode() not in conteudo
    assert emitida["chave"].split("_")[2].encode() not in conteudo


# ----------------------------------------------------------------------
# Administração
# ----------------------------------------------------------------------


def test_admin_exige_token(ambiente):
    corpo = {"cliente": "cliente-x", "referenciaCompra": "r", "plano": "completo", "validadeDias": 30}
    assert ambiente.cliente.post("/admin/chaves", json=corpo).status_code == 401
    assert ambiente.cliente.post("/admin/chaves", json=corpo, headers={"X-Admin-Token": "x" * 40}).status_code == 401
    # Uma chave de cliente não abre a administração.
    chave = emitir(ambiente)["chave"]
    assert ambiente.cliente.get("/admin/chaves", headers={"X-Admin-Token": chave}).status_code == 401


def test_emissao_e_idempotente_por_compra(ambiente):
    primeira = emitir(ambiente)
    assert primeira["situacao"] == "ativa" and primeira["limites"]["cotaAgentes"] == 300
    r = ambiente.cliente.post("/admin/chaves", headers={"X-Admin-Token": ADMIN},
                              json={"cliente": "cliente-x", "referenciaCompra": "compra-1", "plano": "completo", "validadeDias": 30})
    assert r.status_code == 409 and primeira["id"] in r.json()["detail"]
    assert "chave" not in r.json()  # a chave nunca é mostrada de novo


def test_emissao_validacoes(ambiente):
    def post(**corpo):
        base = {"cliente": "cliente-x", "referenciaCompra": f"r-{len(corpo)}", "plano": "completo"}
        return ambiente.cliente.post("/admin/chaves", json=base | corpo, headers={"X-Admin-Token": ADMIN})

    assert post(plano="ouro", validadeDias=1).status_code == 422
    assert post().status_code == 422  # sem validade
    assert post(validadeDias=1, expiraEm="2030-01-01T00:00:00Z").status_code == 422  # as duas
    assert post(expiraEm="2020-01-01T00:00:00Z").status_code == 422  # no passado


def test_limites_proprios_por_chave(ambiente):
    emitida = emitir(ambiente, limites={"cotaCalculo": 1, "cotaAgentes": 2, "limitePorMinuto": 3})
    assert emitida["limites"] == {"cotaCalculo": 1, "cotaAgentes": 2, "limitePorMinuto": 3}


def test_listar_detalhar_renovar_revogar(ambiente):
    emitida = emitir(ambiente)
    adm = {"X-Admin-Token": ADMIN}
    assert [c["id"] for c in ambiente.cliente.get("/admin/chaves", headers=adm).json()] == [emitida["id"]]
    ambiente.cliente.post("/v1/agentes/consultor", json=PEDIDO_CONSULTA, headers=h(emitida["chave"]))
    detalhe = ambiente.cliente.get(f"/admin/chaves/{emitida['id']}", headers=adm).json()
    assert detalhe["usoNoPeriodo"] == {"agentes": 1}
    assert detalhe["ultimasRequisicoes"][0]["rota"] == "/v1/agentes/consultor"
    assert "chave" not in detalhe

    renovada = ambiente.cliente.post(f"/admin/chaves/{emitida['id']}/renovar", json={"dias": 10}, headers=adm).json()
    assert datetime.fromisoformat(renovada["expiraEm"]) - datetime.fromisoformat(emitida["expiraEm"]) == timedelta(days=10)

    revogada = ambiente.cliente.post(f"/admin/chaves/{emitida['id']}/revogar", json={"motivo": "reembolso"}, headers=adm).json()
    assert revogada["situacao"] == "revogada"
    assert ambiente.cliente.post(f"/admin/chaves/{emitida['id']}/renovar", json={"dias": 10}, headers=adm).status_code == 409
    assert ambiente.cliente.get("/admin/chaves/nao-existe", headers=adm).status_code == 404


def test_renovar_chave_expirada_conta_a_partir_de_agora(ambiente):
    emitida = emitir(ambiente)
    ambiente.relogio.momento += timedelta(days=100)
    renovada = ambiente.cliente.post(f"/admin/chaves/{emitida['id']}/renovar", json={"dias": 30},
                                     headers={"X-Admin-Token": ADMIN}).json()
    assert datetime.fromisoformat(renovada["expiraEm"]) == ambiente.relogio.momento + timedelta(days=30)
    assert renovada["situacao"] == "ativa"


# ----------------------------------------------------------------------
# Autenticação do cliente
# ----------------------------------------------------------------------


def test_sem_chave_ou_chave_invalida(ambiente):
    emitida = emitir(ambiente)
    falsa = emitida["chave"][:-1] + ("A" if emitida["chave"][-1] != "A" else "B")
    for cabecalho in ({}, h("qualquer"), h("rtc_000000000000_" + "a" * 43), h(falsa)):
        r = ambiente.cliente.get("/v1/conta", headers=cabecalho)
        assert r.status_code == 401 and r.json()["type"].endswith("chave-invalida")
        assert r.headers["content-type"].startswith("application/problem+json")


def test_chave_expirada(ambiente):
    emitida = emitir(ambiente)
    assert ambiente.cliente.get("/v1/conta", headers=h(emitida["chave"])).status_code == 200
    ambiente.relogio.momento += timedelta(days=30)
    r = ambiente.cliente.get("/v1/conta", headers=h(emitida["chave"]))
    assert r.status_code == 401 and r.json()["type"].endswith("chave-expirada")


def test_chave_revogada(ambiente):
    emitida = emitir(ambiente)
    ambiente.cliente.post(f"/admin/chaves/{emitida['id']}/revogar", json={"motivo": "vazou"}, headers={"X-Admin-Token": ADMIN})
    r = ambiente.cliente.post("/v1/agentes/consultor", json=PEDIDO_CONSULTA, headers=h(emitida["chave"]))
    assert r.status_code == 401 and r.json()["type"].endswith("chave-revogada")
    assert ambiente.chamadas == []


def test_conta(ambiente):
    emitida = emitir(ambiente)
    conta = ambiente.cliente.get("/v1/conta", headers=h(emitida["chave"])).json()
    assert conta["plano"] == "completo" and conta["diasRestantes"] == 30 and conta["periodo"] == "2027-03"
    assert conta["situacao"] == "ativa" and conta["usoNoPeriodo"] == {}
    ambiente.relogio.momento += timedelta(days=29, hours=1)  # faltam 23 horas
    assert ambiente.cliente.get("/v1/conta", headers=h(emitida["chave"])).json()["diasRestantes"] == 1


# ----------------------------------------------------------------------
# Escopo, cota e limite por minuto
# ----------------------------------------------------------------------


def test_escopo_do_plano(ambiente):
    chave = emitir(ambiente, plano="calculo")["chave"]
    r = ambiente.cliente.post("/v1/agentes/consultor", json=PEDIDO_CONSULTA, headers=h(chave))
    assert r.status_code == 403 and ambiente.chamadas == []


def test_limite_por_minuto(ambiente):
    chave = emitir(ambiente, limites={"cotaCalculo": 100, "cotaAgentes": 100, "limitePorMinuto": 2})["chave"]
    codigos = [ambiente.cliente.post("/v1/agentes/consultor", json=PEDIDO_CONSULTA, headers=h(chave)).status_code for _ in range(3)]
    assert codigos == [200, 200, 429]
    r = ambiente.cliente.post("/v1/agentes/consultor", json=PEDIDO_CONSULTA, headers=h(chave))
    assert r.json()["type"].endswith("limite-por-minuto") and r.headers["Retry-After"] == "30"
    ambiente.relogio.momento += timedelta(minutes=1)
    assert ambiente.cliente.post("/v1/agentes/consultor", json=PEDIDO_CONSULTA, headers=h(chave)).status_code == 200


def test_cota_mensal_e_virada_do_mes(ambiente):
    chave = emitir(ambiente, limites={"cotaCalculo": 0, "cotaAgentes": 2, "limitePorMinuto": 100}, validadeDias=90)["chave"]
    codigos = [ambiente.cliente.post("/v1/agentes/consultor", json=PEDIDO_CONSULTA, headers=h(chave)).status_code for _ in range(3)]
    assert codigos == [200, 200, 429]
    ambiente.relogio.momento = datetime(2027, 4, 1, 0, 0, 5, tzinfo=timezone.utc)
    assert ambiente.cliente.post("/v1/agentes/consultor", json=PEDIDO_CONSULTA, headers=h(chave)).status_code == 200


def test_falha_estorna_a_cota(ambiente):
    from rtc_mcp.classificador import ErroClassificador

    chave = emitir(ambiente, limites={"cotaCalculo": 0, "cotaAgentes": 1, "limitePorMinuto": 100})["chave"]
    ambiente.estado["falha"] = ErroClassificador("O agente atingiu o limite de iterações.")
    r = ambiente.cliente.post("/v1/agentes/consultor", json=PEDIDO_CONSULTA, headers=h(chave))
    assert r.status_code == 502 and "limite de iterações" in r.json()["detail"]
    assert ambiente.cliente.get("/v1/conta", headers=h(chave)).json()["usoNoPeriodo"] == {"agentes": 0}
    ambiente.estado["falha"] = None
    assert ambiente.cliente.post("/v1/agentes/consultor", json=PEDIDO_CONSULTA, headers=h(chave)).status_code == 200


def test_erro_inesperado_nao_vaza_detalhes(ambiente):
    chave = emitir(ambiente)["chave"]
    ambiente.estado["falha"] = RuntimeError("segredo interno /etc/x")
    r = ambiente.cliente.post("/v1/agentes/consultor", json=PEDIDO_CONSULTA, headers=h(chave))
    assert r.status_code == 500 and "segredo" not in r.text


def test_concorrencia_nao_ultrapassa_a_cota(tmp_path):
    """Muitas requisições simultâneas disputando as últimas unidades: nunca passam mais que a cota."""
    banco = Banco(tmp_path / "c.db")
    valor, prefixo = gerar_chave()
    chave = banco.emitir(prefixo=prefixo, hash_=hash_chave(valor, PEPPER), cliente="c", referencia_compra="r",
                         plano="completo", escopos=["agentes"], cota_calculo=0, cota_agentes=10,
                         limite_por_minuto=1000, expira_em=agora() + timedelta(days=1))
    momento = agora()
    with ThreadPoolExecutor(max_workers=16) as pool:
        resultados = list(pool.map(lambda _: banco.consumir(chave, "agentes", momento), range(60)))
    assert resultados.count("ok") == 10 and resultados.count("cota_esgotada") == 50
    assert banco.uso(chave.id, momento.strftime("%Y-%m")) == {"agentes": 10}


# ----------------------------------------------------------------------
# Rotas dos agentes (executores simulados)
# ----------------------------------------------------------------------


def test_rotas_dos_agentes_repassam_os_parametros(ambiente):
    chave = h(emitir(ambiente)["chave"])
    c = ambiente.cliente
    assert c.post("/v1/agentes/classificador", json={"descricao": "Arroz 5 kg", "data": "2027-03-10", "contexto": "varejo"}, headers=chave).status_code == 200
    assert c.post("/v1/agentes/comparador", json={"pedido": "Mercadinho no Simples ..."}, headers=chave).status_code == 200
    r = c.post("/v1/agentes/auditor", json={"xml": "<NFe>conteudo da nota</NFe>"}, headers=chave)
    assert r.json()["xml"] == "<NFe>conteudo da nota</NFe>"
    assert c.post("/v1/agentes/corretor", json={"operacao": {"itens": []}}, headers=chave).status_code == 200
    assert c.post("/v1/agentes/consultor", json={"pergunta": "Educação tem redução?", "data": "2027-01-01"}, headers=chave).status_code == 200
    nomes = [n for n, _, _ in ambiente.chamadas]
    assert nomes == ["classificador", "comparador", "auditor", "corretor", "consultor"]
    assert ambiente.chamadas[0][2] == {"data": "2027-03-10", "ncm": None, "nbs": None, "contexto": "varejo"}
    # O arquivo temporário do auditor é apagado depois.
    assert not Path(ambiente.chamadas[2][1][0]).exists()


def test_validacao_da_entrada(ambiente):
    chave = h(emitir(ambiente, limites={"cotaCalculo": 0, "cotaAgentes": 1, "limitePorMinuto": 100})["chave"])
    r = ambiente.cliente.post("/v1/agentes/classificador", json={"descricao": "Arroz", "data": "10/03/2027"}, headers=chave)
    assert r.status_code == 422 and ambiente.chamadas == []
    # Validação falha antes de consumir a cota.
    assert ambiente.cliente.post("/v1/agentes/consultor", json=PEDIDO_CONSULTA, headers=chave).status_code == 200


def test_corpo_grande_demais(ambiente):
    chave = h(emitir(ambiente)["chave"])
    grande = "x" * (ambiente.config.tamanho_maximo_bytes + 1)
    r = ambiente.cliente.post("/v1/agentes/auditor", json={"xml": grande}, headers=chave)
    assert r.status_code == 413


# ----------------------------------------------------------------------
# Rotas de cálculo (calculadora real)
# ----------------------------------------------------------------------


@calculadora
def test_rotas_de_calculo_com_a_calculadora(ambiente):
    chave = h(emitir(ambiente, plano="calculo")["chave"])
    c = ambiente.cliente
    nfe = (Path(__file__).parent / "fixtures" / "nfe_2026_conforme.xml").read_text()
    r = c.post("/v1/calculo/auditoria-nfe", content=nfe, headers=chave | {"Content-Type": "application/xml"})
    assert r.status_code == 200 and r.json()["situacao"] == "conforme"

    operacao = {"id": "x", "versao": "1.0.0", "dhFatoGerador": "2026-06-10T10:00:00-03:00", "uf": "RS", "municipio": 4314902,
                "itens": [{"numero": 1, "ncm": "02013000", "cst": "200", "cClassTrib": "200035", "baseCalculo": 80,
                           "quantidade": 1, "unidade": "KG"}]}
    r = c.post("/v1/calculo/diagnostico-operacao", json=operacao, headers=chave)
    assert r.status_code == 200 and r.json()["erros"][0]["codigo"] == "REG-011"

    # Entrada inválida para a operação (2026, antes do Simples): 422 e a cota é estornada.
    cenario = {"data": "2026-06-15", "uf": "RS", "municipio": 4314902, "pIBSSN": 1.25, "pCBSSN": 2.75,
               "itens": [{"descricao": "x", "ncm": "48202000", "cst": "000", "cClassTrib": "000001", "valor": 10, "cAtivSN": "01"}]}
    r = c.post("/v1/calculo/comparacao-regimes", json=cenario, headers=chave)
    assert r.status_code == 422 and "a partir de 2027" in r.json()["detail"]
    assert c.get("/v1/conta", headers=chave).json()["usoNoPeriodo"] == {"calculo": 2}


def test_calculadora_fora_do_ar_e_503(tmp_path):
    from rtc_mcp.client import Config, RtcClient

    config = Configuracao(token_admin=ADMIN, pepper=PEPPER, banco=tmp_path / "a.db")
    rtc = RtcClient(Config(regime_geral_url="http://127.0.0.1:9/api", simples_nacional_url="http://127.0.0.1:9/api", timeout=2))
    with TestClient(criar_app(config, cliente_rtc=rtc)) as c:
        r = c.post("/admin/chaves", headers={"X-Admin-Token": ADMIN},
                   json={"cliente": "cliente-x", "referenciaCompra": "r", "plano": "calculo", "validadeDias": 1})
        chave = h(r.json()["chave"])
        r = c.post("/v1/calculo/diagnostico-operacao", json={"itens": []}, headers=chave)
        assert r.status_code == 503 and r.json()["type"].endswith("calculadora-indisponivel")
        assert c.get("/v1/conta", headers=chave).json()["usoNoPeriodo"] == {"calculo": 0}
        assert c.get("/saude").json()["calculadoras"] == {"regimeGeral": "indisponivel", "simplesNacional": "indisponivel"}


# ----------------------------------------------------------------------
# Documentação
# ----------------------------------------------------------------------


def test_swagger_e_openapi(ambiente):
    assert ambiente.cliente.get("/docs").status_code == 200
    o = ambiente.cliente.get("/openapi.json").json()
    assert o["components"]["securitySchemes"]["ChaveDeAcesso"] == {
        "type": "apiKey", "in": "header", "name": "X-API-Key",
        "description": "Chave de acesso liberada após a compra (formato `rtc_<prefixo>_<segredo>`)."}
    assert not any(p.startswith("/admin") for p in o["paths"])  # administração fora da documentação pública
    assert o["paths"]["/v1/calculo/auditoria-nfe"]["post"]["requestBody"]["content"].keys() == {"application/xml"}
    seguras = [p for p, ops in o["paths"].items() for op in ops.values() if op.get("security")]
    assert set(seguras) == {p for p in o["paths"] if p.startswith("/v1")}


def test_documentacao_administrativa_opcional(tmp_path):
    config = Configuracao(token_admin=ADMIN, pepper=PEPPER, banco=tmp_path / "d.db", docs_admin=True)
    with TestClient(criar_app(config)) as c:
        assert "/admin/chaves" in c.get("/openapi.json").json()["paths"]
