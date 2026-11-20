"""Testes da compra pelo Mercado Pago: pedido, webhook assinado, consulta do pagamento, resgate e estorno.

A API do Mercado Pago é simulada com respx. As assinaturas são geradas como o Mercado Pago gera
(HMAC-SHA256 do manifest ``id:<data.id>;request-id:<x-request-id>;ts:<ts>;`` com a assinatura secreta) e
conferidas pelo validador do SDK oficial.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from rtc_api.app import criar_app
from rtc_api.config import Configuracao

ADMIN = "t" * 40
PEPPER = "p" * 40
SEGREDO = "segredo-do-webhook-mp"
TOKEN_MP = "APP_USR-teste"
MP = "https://api.mercadopago.test"


class Relogio:
    def __init__(self):
        self.momento = datetime(2027, 3, 10, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.momento


def assinar(data_id: str | None, request_id: str | None, ts: str = "1704908010", segredo: str = SEGREDO) -> str:
    partes = ([f"id:{data_id}"] if data_id else []) + ([f"request-id:{request_id}"] if request_id else []) + [f"ts:{ts}"]
    manifest = ";".join(partes) + ";"
    return f"ts={ts},v1={hmac.new(segredo.encode(), manifest.encode(), hashlib.sha256).hexdigest()}"


@pytest.fixture
def amb(tmp_path):
    relogio = Relogio()
    config = Configuracao(token_admin=ADMIN, pepper=PEPPER, banco=tmp_path / "api.db",
                          mp_webhook_secret=SEGREDO, mp_access_token=TOKEN_MP, mp_api_url=MP)
    with respx.mock(assert_all_called=False) as mock:
        app = criar_app(config, executores={"consultor": _consultor}, relogio=relogio)
        with TestClient(app) as cliente:
            yield SimpleNamespace(c=cliente, mock=mock, relogio=relogio)


async def _consultor(*a, **k):
    class R:
        def model_dump(self, mode="python"):
            return {"ok": True}
    return R()


def criar_pedido(amb, valor="199.90", plano="completo", dias=365) -> dict[str, Any]:
    r = amb.c.post("/admin/pedidos", headers={"X-Admin-Token": ADMIN},
                   json={"cliente": "12345678000199", "plano": plano, "validadeDias": dias, "valor": valor})
    assert r.status_code == 201, r.text
    return r.json()


def pagamento(pedido: dict[str, Any], id_="1234567890", status="approved", valor="199.90", moeda="BRL", **extra) -> dict[str, Any]:
    return {"id": int(id_), "status": status, "status_detail": "accredited", "currency_id": moeda,
            "transaction_amount": valor, "transaction_amount_refunded": 0, "external_reference": pedido["externalReference"],
            "date_approved": "2027-03-10T11:59:00.000-03:00", "payer": {"email": "comprador@example.com"}, **extra}


def notificar(amb, pag: dict[str, Any] | None, data_id="1234567890", assinatura: str | None = "valida", tipo="payment",
              request_id="req-abc", corpo_id: str | None = None):
    if pag is not None:
        amb.mock.get(f"{MP}/v1/payments/{data_id}").mock(return_value=httpx.Response(200, json=pag))
    headers = {"x-request-id": request_id}
    if assinatura == "valida":
        headers["x-signature"] = assinar(data_id, request_id)
    elif assinatura is not None:
        headers["x-signature"] = assinatura
    corpo = {"id": 1, "live_mode": True, "type": tipo, "action": "payment.updated", "data": {"id": corpo_id or data_id}}
    return amb.c.post(f"/webhooks/mercadopago?data.id={data_id}&type={tipo}", json=corpo, headers=headers)


def resgatar(amb, codigo: str):
    return amb.c.post("/v1/resgate", json={"codigoResgate": codigo})


# ----------------------------------------------------------------------
# Fluxo feliz
# ----------------------------------------------------------------------


def test_compra_completa(amb):
    pedido = criar_pedido(amb)
    assert pedido["situacao"] == "aguardando_pagamento" and pedido["externalReference"].startswith("rtc-")
    assert pedido["valor"] == "199.90" and len(pedido["codigoResgate"]) >= 40

    # Antes do pagamento, o resgate informa que está pendente.
    r = resgatar(amb, pedido["codigoResgate"])
    assert r.status_code == 409 and r.json()["type"].endswith("pagamento-pendente")

    r = notificar(amb, pagamento(pedido))
    assert r.status_code == 200 and r.json() == {"desfecho": "pago"}
    consulta = amb.mock.calls.last.request
    assert consulta.url == f"{MP}/v1/payments/1234567890" and consulta.headers["Authorization"] == f"Bearer {TOKEN_MP}"

    r = resgatar(amb, pedido["codigoResgate"])
    assert r.status_code == 200
    chave = r.json()
    assert chave["plano"] == "completo" and chave["escopos"] == ["calculo", "agentes"]
    # Validade contada da aprovação do pagamento (11:59 -03:00 = 14:59 UTC).
    assert datetime.fromisoformat(chave["expiraEm"]) == datetime(2027, 3, 10, 14, 59, tzinfo=timezone.utc) + timedelta(days=365)

    # A chave funciona na API.
    conta = amb.c.get("/v1/conta", headers={"X-API-Key": chave["chave"]}).json()
    assert conta["cliente"] == "12345678000199" and conta["situacao"] == "ativa"
    assert amb.c.post("/v1/agentes/consultor", json={"pergunta": "Educação tem redução?"},
                      headers={"X-API-Key": chave["chave"]}).status_code == 200

    # Resgate só uma vez.
    r = resgatar(amb, pedido["codigoResgate"])
    assert r.status_code == 409 and r.json()["type"].endswith("ja-resgatado")

    situacao = amb.c.get(f"/admin/pedidos/{pedido['id']}", headers={"X-Admin-Token": ADMIN}).json()
    assert situacao["situacao"] == "resgatado" and situacao["pagamentoId"] == "1234567890" and situacao["chaveId"]
    assert "codigoResgate" not in situacao


def test_notificacao_repetida_e_idempotente(amb):
    pedido = criar_pedido(amb)
    assert notificar(amb, pagamento(pedido)).json() == {"desfecho": "pago"}
    assert notificar(amb, pagamento(pedido)).json() == {"desfecho": "ja_processado"}
    assert resgatar(amb, pedido["codigoResgate"]).status_code == 200
    assert notificar(amb, pagamento(pedido)).json() == {"desfecho": "ja_processado"}


def test_resgates_simultaneos_emitem_uma_chave(amb):
    pedido = criar_pedido(amb)
    notificar(amb, pagamento(pedido))
    with ThreadPoolExecutor(max_workers=8) as pool:
        codigos = [r.status_code for r in pool.map(lambda _: resgatar(amb, pedido["codigoResgate"]), range(8))]
    assert codigos.count(200) == 1 and codigos.count(409) == 7
    chaves = amb.c.get("/admin/chaves", headers={"X-Admin-Token": ADMIN}).json()
    assert [c["situacao"] for c in chaves] == ["ativa"]


# ----------------------------------------------------------------------
# Assinatura e corpo da notificação
# ----------------------------------------------------------------------


@pytest.mark.parametrize("assinatura", [None, "ts=1704908010,v1=" + "0" * 64, "lixo", "v1=abc"])
def test_assinatura_ausente_ou_invalida(amb, assinatura):
    pedido = criar_pedido(amb)
    r = notificar(amb, pagamento(pedido), assinatura=assinatura)
    assert r.status_code == 401
    assert not amb.mock.calls  # nem consulta o Mercado Pago


def test_assinatura_com_outro_segredo_ou_outro_id(amb):
    pedido = criar_pedido(amb)
    assert notificar(amb, pagamento(pedido), assinatura=assinar("1234567890", "req-abc", segredo="outro")).status_code == 401
    # Assinatura válida para outro pagamento não serve para este.
    assert notificar(amb, pagamento(pedido), assinatura=assinar("999", "req-abc")).status_code == 401


def test_id_do_corpo_diferente_do_assinado_e_ignorado(amb):
    pedido = criar_pedido(amb)
    r = notificar(amb, pagamento(pedido), corpo_id="555")
    assert r.json() == {"desfecho": "ignorado"} and not amb.mock.calls


def test_outros_topicos_sao_ignorados(amb):
    assert notificar(amb, None, tipo="merchant_order").json() == {"desfecho": "ignorado"}


def test_webhook_sem_configuracao_responde_503(tmp_path):
    config = Configuracao(token_admin=ADMIN, pepper=PEPPER, banco=tmp_path / "x.db")
    with TestClient(criar_app(config)) as c:
        r = c.post("/webhooks/mercadopago?data.id=1&type=payment", json={})
        assert r.status_code == 503


# ----------------------------------------------------------------------
# O pagamento consultado decide (o corpo não é confiável)
# ----------------------------------------------------------------------


def test_valor_menor_ou_outra_moeda_nao_paga(amb):
    pedido = criar_pedido(amb)
    assert notificar(amb, pagamento(pedido, valor="19.90")).json() == {"desfecho": "divergente"}
    assert notificar(amb, pagamento(pedido, moeda="USD")).json() == {"desfecho": "divergente"}
    assert resgatar(amb, pedido["codigoResgate"]).status_code == 409


def test_pagamento_pendente_ou_recusado(amb):
    pedido = criar_pedido(amb)
    assert notificar(amb, pagamento(pedido, status="pending")).json() == {"desfecho": "aguardando"}
    assert notificar(amb, pagamento(pedido, status="rejected")).json() == {"desfecho": "aguardando"}


def test_pagamento_de_outro_sistema_e_ignorado(amb):
    pedido = criar_pedido(amb)
    assert notificar(amb, pagamento(pedido, external_reference="PEDIDO-LOJA-1")).json() == {"desfecho": "ignorado"}
    assert notificar(amb, pagamento(pedido, external_reference="rtc-nao-existe")).json() == {"desfecho": "ignorado"}


def test_segundo_pagamento_do_mesmo_pedido(amb):
    pedido = criar_pedido(amb)
    notificar(amb, pagamento(pedido))
    assert notificar(amb, pagamento(pedido, id_="222"), data_id="222").json() == {"desfecho": "pagamento_duplicado"}


def test_mercado_pago_fora_do_ar_responde_503_para_reenvio(amb):
    pedido = criar_pedido(amb)
    amb.mock.get(f"{MP}/v1/payments/1234567890").mock(return_value=httpx.Response(500))
    r = notificar(amb, None)
    assert r.status_code == 503
    amb.mock.get(f"{MP}/v1/payments/1234567890").mock(side_effect=httpx.ConnectError("x"))
    assert notificar(amb, None).status_code == 503
    # No reenvio, com a API de volta, o pedido é pago.
    assert notificar(amb, pagamento(pedido)).json() == {"desfecho": "pago"}


# ----------------------------------------------------------------------
# Estorno
# ----------------------------------------------------------------------


def test_estorno_depois_do_resgate_revoga_a_chave(amb):
    pedido = criar_pedido(amb)
    notificar(amb, pagamento(pedido))
    chave = resgatar(amb, pedido["codigoResgate"]).json()["chave"]
    assert notificar(amb, pagamento(pedido, status="refunded", transaction_amount_refunded="199.90")).json() == {"desfecho": "estornado"}
    r = amb.c.get("/v1/conta", headers={"X-API-Key": chave})
    assert r.status_code == 401 and r.json()["type"].endswith("chave-revogada")
    assert amb.c.get(f"/admin/pedidos/{pedido['id']}", headers={"X-Admin-Token": ADMIN}).json()["situacao"] == "estornado"


def test_estorno_antes_do_resgate_bloqueia_o_resgate(amb):
    pedido = criar_pedido(amb)
    notificar(amb, pagamento(pedido))
    assert notificar(amb, pagamento(pedido, status="charged_back")).json() == {"desfecho": "estornado"}
    r = resgatar(amb, pedido["codigoResgate"])
    assert r.status_code == 409 and r.json()["type"].endswith("pedido-estornado")


def test_reembolso_parcial_com_status_aprovado_tambem_estorna(amb):
    pedido = criar_pedido(amb)
    notificar(amb, pagamento(pedido))
    assert notificar(amb, pagamento(pedido, transaction_amount_refunded="50.00")).json() == {"desfecho": "estornado"}


# ----------------------------------------------------------------------
# Resgate e pedidos
# ----------------------------------------------------------------------


def test_codigo_invalido(amb):
    criar_pedido(amb)
    r = resgatar(amb, "x" * 43)
    assert r.status_code == 404


def test_validade_encerrada_antes_do_resgate(amb):
    pedido = criar_pedido(amb, dias=1)
    notificar(amb, pagamento(pedido))
    amb.relogio.momento += timedelta(days=2)
    r = resgatar(amb, pedido["codigoResgate"])
    assert r.status_code == 409 and r.json()["type"].endswith("validade-encerrada")


def test_pedido_exige_admin_e_dados_validos(amb):
    corpo = {"cliente": "cliente-x", "plano": "completo", "validadeDias": 30, "valor": "10.00"}
    assert amb.c.post("/admin/pedidos", json=corpo).status_code == 401
    adm = {"X-Admin-Token": ADMIN}
    assert amb.c.post("/admin/pedidos", json=corpo | {"plano": "ouro"}, headers=adm).status_code == 422
    assert amb.c.post("/admin/pedidos", json=corpo | {"valor": "10,00"}, headers=adm).status_code == 422
    assert amb.c.post("/admin/pedidos", json=corpo | {"valor": "0"}, headers=adm).status_code == 422
    assert amb.c.post("/admin/pedidos", json=corpo | {"valor": "0.00"}, headers=adm).status_code == 422


def test_banco_nao_guarda_o_codigo_de_resgate(amb, tmp_path):
    pedido = criar_pedido(amb)
    assert pedido["codigoResgate"].encode() not in (tmp_path / "api.db").read_bytes()


def test_validador_e_o_do_sdk_oficial():
    """O formato de assinatura usado nos testes é aceito pelo validador do SDK oficial."""
    from mercadopago.webhook import WebhookSignatureValidator

    WebhookSignatureValidator.validate(assinar("123", "req"), "req", "123", SEGREDO)
    WebhookSignatureValidator.validate(assinar(None, "req"), "req", None, SEGREDO)
