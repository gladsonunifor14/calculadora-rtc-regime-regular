"""Integração com o Mercado Pago: webhook de pagamento e resgate da chave.

Fluxo (Checkout Pro, pagamento único):

1. Antes do checkout, o sistema de vendas registra o pedido (POST /admin/pedidos) e recebe a
   ``external_reference`` para pôr na preferência e o código de resgate para mostrar ao comprador na página
   de retorno (back_url).
2. O Mercado Pago avisa o webhook. A assinatura (cabeçalho ``x-signature``) é conferida com o validador do
   SDK oficial. O corpo da notificação não é confiável: o pagamento é consultado na API do Mercado Pago
   (GET /v1/payments/{id}) e só conta se estiver ``approved``, em BRL, com o valor do pedido e a
   ``external_reference`` dele.
3. O comprador resgata a chave uma única vez com o código. A chave só é gerada nesse momento, então nunca
   fica guardada em lugar nenhum, nem cifrada.
4. Se uma notificação posterior do mesmo pagamento mostrar outro status ou valor reembolsado, o pedido é
   estornado e a chave, se já resgatada, é revogada.

Referências: documentação de webhooks do Mercado Pago (cabeçalhos x-signature/x-request-id, resposta 200/201
em até 22 s, reenvio a cada 15 min) e SDK oficial ``mercadopago`` 3.6 (``WebhookSignatureValidator``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import httpx

from .banco import BancoPedidos, Pedido

log = logging.getLogger("rtc_api.mercado_pago")

PREFIXO_REFERENCIA = "rtc-"

Desfecho = Literal[
    "ignorado", "pago", "ja_processado", "pagamento_duplicado", "divergente", "aguardando", "estornado"
]


class AssinaturaInvalida(Exception):
    """A notificação não tem assinatura válida do Mercado Pago."""


class MercadoPagoIndisponivel(Exception):
    """A API do Mercado Pago falhou; o webhook responde 5xx para o Mercado Pago reenviar."""


@dataclass(frozen=True)
class Resultado:
    desfecho: Desfecho
    pedido_id: str | None = None
    detalhe: str = ""
    chave_revogada: str | None = None


def validar_assinatura(segredo: str, x_signature: str | None, x_request_id: str | None, data_id: str | None) -> None:
    from mercadopago.webhook import InvalidWebhookSignatureError, WebhookSignatureValidator

    try:
        WebhookSignatureValidator.validate(x_signature, x_request_id, data_id, segredo)
    except InvalidWebhookSignatureError as exc:
        raise AssinaturaInvalida(exc.reason.value) from exc


def centavos(valor: Any) -> int | None:
    try:
        return int((Decimal(str(valor)) * 100).to_integral_value())
    except (InvalidOperation, ValueError, TypeError):
        return None


async def consultar_pagamento(http: httpx.AsyncClient, api_url: str, access_token: str, pagamento_id: str) -> dict[str, Any] | None:
    """GET /v1/payments/{id}. None se o pagamento não existir; MercadoPagoIndisponivel em falhas."""
    try:
        r = await http.get(f"{api_url}/v1/payments/{pagamento_id}", headers={"Authorization": f"Bearer {access_token}"})
    except httpx.HTTPError as exc:
        raise MercadoPagoIndisponivel(repr(exc)) from exc
    if r.status_code == 404:
        return None
    if r.is_error:
        raise MercadoPagoIndisponivel(f"HTTP {r.status_code}")
    return r.json()


def _data(texto: str | None) -> datetime:
    if texto:
        try:
            momento = datetime.fromisoformat(texto.replace("Z", "+00:00"))
            return momento if momento.tzinfo else momento.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


async def processar_pagamento(
    pagamento: dict[str, Any], pedidos: BancoPedidos, revogar: Any
) -> Resultado:
    """Aplica o estado de um pagamento (já consultado na API do Mercado Pago) ao pedido correspondente."""
    referencia = str(pagamento.get("external_reference") or "")
    pagamento_id = str(pagamento.get("id"))
    if not referencia.startswith(PREFIXO_REFERENCIA):
        return Resultado("ignorado", detalhe="pagamento sem external_reference desta API")
    pedido = pedidos.por_referencia(referencia)
    if pedido is None:
        return Resultado("ignorado", detalhe=f"external_reference {referencia} sem pedido")

    aprovado = pagamento.get("status") == "approved"
    reembolsado = (centavos(pagamento.get("transaction_amount_refunded")) or 0) > 0

    if aprovado and not reembolsado:
        if pedido.situacao in ("pago", "resgatado"):
            if pedido.pagamento_id == pagamento_id:
                return Resultado("ja_processado", pedido.id)
            log.warning("Pedido %s pago duas vezes (pagamentos %s e %s): reembolsar manualmente",
                        pedido.id, pedido.pagamento_id, pagamento_id)
            return Resultado("pagamento_duplicado", pedido.id, f"pedido já pago pelo pagamento {pedido.pagamento_id}")
        if pedido.situacao == "estornado":
            return Resultado("ignorado", pedido.id, "pedido estornado")
        valor_pago = centavos(pagamento.get("transaction_amount"))
        if pagamento.get("currency_id") != "BRL" or valor_pago is None or valor_pago < pedido.valor_centavos:
            log.warning("Pagamento %s divergente do pedido %s: %s %s (esperado BRL %s)", pagamento_id, pedido.id,
                        pagamento.get("currency_id"), pagamento.get("transaction_amount"), pedido.valor_centavos / 100)
            return Resultado("divergente", pedido.id, "moeda ou valor diferente do pedido")
        if pedidos.marcar_pago(pedido.id, pagamento_id, valor_pago, _data(pagamento.get("date_approved"))):
            log.info("Pedido %s pago (pagamento %s)", pedido.id, pagamento_id)
            return Resultado("pago", pedido.id)
        return Resultado("ja_processado", pedido.id)

    # Não aprovado (ou com reembolso): só importa se este pagamento já tinha pago o pedido.
    if pedido.situacao in ("pago", "resgatado") and pedido.pagamento_id == pagamento_id:
        motivo = f"pagamento {pagamento_id} no Mercado Pago: status {pagamento.get('status')}" + (
            ", com reembolso" if reembolsado else "")
        if pedidos.marcar_estornado(pedido.id, motivo):
            revogada = None
            if pedido.chave_id:
                revogar(pedido.chave_id, motivo)
                revogada = pedido.chave_id
            log.warning("Pedido %s estornado: %s", pedido.id, motivo)
            return Resultado("estornado", pedido.id, motivo, revogada)
        return Resultado("ja_processado", pedido.id)
    return Resultado("aguardando", pedido.id, f"status {pagamento.get('status')}")


def situacao_publica(pedido: Pedido) -> str:
    return {"aguardando": "aguardando_pagamento", "pago": "pago", "resgatado": "resgatado", "estornado": "estornado"}[pedido.situacao]
