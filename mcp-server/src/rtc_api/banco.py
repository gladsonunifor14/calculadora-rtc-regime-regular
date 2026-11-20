"""Persistência em SQLite: chaves, consumo de cota, janela por minuto e registro de requisições.

O consumo de cota e de limite por minuto é feito numa transação ``BEGIN IMMEDIATE``: a verificação e o
incremento acontecem juntos, então duas requisições simultâneas não passam ambas pela última unidade
disponível, nem entre processos diferentes no mesmo servidor.

O registro de requisições guarda rota, status e latência, nunca o conteúdo (que pode ter dados fiscais).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

ESQUEMA = """
CREATE TABLE IF NOT EXISTS chaves (
    id                 TEXT PRIMARY KEY,
    prefixo            TEXT NOT NULL UNIQUE,
    hash               TEXT NOT NULL,
    cliente            TEXT NOT NULL,
    referencia_compra  TEXT NOT NULL UNIQUE,
    plano              TEXT NOT NULL,
    escopos            TEXT NOT NULL,
    cota_calculo       INTEGER NOT NULL CHECK (cota_calculo >= 0),
    cota_agentes       INTEGER NOT NULL CHECK (cota_agentes >= 0),
    limite_por_minuto  INTEGER NOT NULL CHECK (limite_por_minuto >= 0),
    criada_em          TEXT NOT NULL,
    expira_em          TEXT NOT NULL,
    revogada_em        TEXT,
    motivo_revogacao   TEXT
);
CREATE TABLE IF NOT EXISTS uso (
    chave_id  TEXT NOT NULL REFERENCES chaves(id),
    periodo   TEXT NOT NULL,
    escopo    TEXT NOT NULL,
    contagem  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chave_id, periodo, escopo)
);
CREATE TABLE IF NOT EXISTS janela (
    chave_id  TEXT NOT NULL REFERENCES chaves(id),
    minuto    TEXT NOT NULL,
    contagem  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chave_id, minuto)
);
CREATE TABLE IF NOT EXISTS requisicoes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chave_id     TEXT NOT NULL REFERENCES chaves(id),
    rota         TEXT NOT NULL,
    status       INTEGER NOT NULL,
    latencia_ms  INTEGER NOT NULL,
    criada_em    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_requisicoes_chave ON requisicoes (chave_id, criada_em);
CREATE TABLE IF NOT EXISTS pedidos (
    id                   TEXT PRIMARY KEY,
    external_reference   TEXT NOT NULL UNIQUE,
    codigo_hash          TEXT NOT NULL UNIQUE,
    cliente              TEXT NOT NULL,
    plano                TEXT NOT NULL,
    validade_dias        INTEGER NOT NULL CHECK (validade_dias > 0),
    valor_centavos       INTEGER NOT NULL CHECK (valor_centavos > 0),
    situacao             TEXT NOT NULL CHECK (situacao IN ('aguardando', 'pago', 'resgatado', 'estornado')),
    criado_em            TEXT NOT NULL,
    pagamento_id         TEXT UNIQUE,
    valor_pago_centavos  INTEGER,
    pago_em              TEXT,
    chave_id             TEXT REFERENCES chaves(id),
    resgatado_em         TEXT,
    estornado_em         TEXT,
    motivo_estorno       TEXT
);
"""


def agora() -> datetime:
    return datetime.now(timezone.utc)


def iso(momento: datetime) -> str:
    return momento.astimezone(timezone.utc).isoformat(timespec="seconds")


def periodo_de(momento: datetime) -> str:
    return momento.astimezone(timezone.utc).strftime("%Y-%m")


def minuto_de(momento: datetime) -> str:
    return momento.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")


@dataclass(frozen=True)
class Chave:
    id: str
    prefixo: str
    hash: str
    cliente: str
    referencia_compra: str
    plano: str
    escopos: list[str]
    cota_calculo: int
    cota_agentes: int
    limite_por_minuto: int
    criada_em: datetime
    expira_em: datetime
    revogada_em: datetime | None
    motivo_revogacao: str | None

    def situacao(self, momento: datetime) -> Literal["ativa", "expirada", "revogada"]:
        if self.revogada_em is not None:
            return "revogada"
        if momento >= self.expira_em:
            return "expirada"
        return "ativa"

    def cota(self, escopo: str) -> int:
        return self.cota_calculo if escopo == "calculo" else self.cota_agentes


class ReferenciaDuplicada(Exception):
    """Já existe chave para esta referência de compra (a emissão é idempotente por compra)."""

    def __init__(self, chave: Chave):
        super().__init__(chave.id)
        self.chave = chave


def _chave(linha: sqlite3.Row) -> Chave:
    return Chave(
        id=linha["id"],
        prefixo=linha["prefixo"],
        hash=linha["hash"],
        cliente=linha["cliente"],
        referencia_compra=linha["referencia_compra"],
        plano=linha["plano"],
        escopos=json.loads(linha["escopos"]),
        cota_calculo=linha["cota_calculo"],
        cota_agentes=linha["cota_agentes"],
        limite_por_minuto=linha["limite_por_minuto"],
        criada_em=datetime.fromisoformat(linha["criada_em"]),
        expira_em=datetime.fromisoformat(linha["expira_em"]),
        revogada_em=datetime.fromisoformat(linha["revogada_em"]) if linha["revogada_em"] else None,
        motivo_revogacao=linha["motivo_revogacao"],
    )


class Banco:
    def __init__(self, caminho: Path):
        self.caminho = caminho
        with self._conexao() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(ESQUEMA)

    @contextmanager
    def _conexao(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.caminho, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
        finally:
            c.close()

    @contextmanager
    def _transacao(self) -> Iterator[sqlite3.Connection]:
        with self._conexao() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            c.execute("COMMIT")

    # ------------------------------------------------------------------ chaves

    def emitir(
        self, *, prefixo: str, hash_: str, cliente: str, referencia_compra: str, plano: str,
        escopos: list[str], cota_calculo: int, cota_agentes: int, limite_por_minuto: int, expira_em: datetime,
    ) -> Chave:
        with self._transacao() as c:
            existente = c.execute("SELECT * FROM chaves WHERE referencia_compra = ?", (referencia_compra,)).fetchone()
            if existente:
                raise ReferenciaDuplicada(_chave(existente))
            id_ = str(uuid.uuid4())
            c.execute(
                "INSERT INTO chaves (id, prefixo, hash, cliente, referencia_compra, plano, escopos, cota_calculo,"
                " cota_agentes, limite_por_minuto, criada_em, expira_em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (id_, prefixo, hash_, cliente, referencia_compra, plano, json.dumps(escopos), cota_calculo,
                 cota_agentes, limite_por_minuto, iso(agora()), iso(expira_em)),
            )
            return _chave(c.execute("SELECT * FROM chaves WHERE id = ?", (id_,)).fetchone())

    def por_prefixo(self, prefixo: str) -> Chave | None:
        with self._conexao() as c:
            linha = c.execute("SELECT * FROM chaves WHERE prefixo = ?", (prefixo,)).fetchone()
        return _chave(linha) if linha else None

    def obter(self, id_: str) -> Chave | None:
        with self._conexao() as c:
            linha = c.execute("SELECT * FROM chaves WHERE id = ?", (id_,)).fetchone()
        return _chave(linha) if linha else None

    def listar(self, *, cliente: str | None, limite: int, deslocamento: int) -> list[Chave]:
        sql, params = "SELECT * FROM chaves", []
        if cliente:
            sql, params = sql + " WHERE cliente = ?", [cliente]
        sql += " ORDER BY criada_em DESC, id LIMIT ? OFFSET ?"
        with self._conexao() as c:
            return [_chave(linha) for linha in c.execute(sql, [*params, limite, deslocamento])]

    def renovar(self, id_: str, expira_em: datetime) -> Chave | None:
        with self._transacao() as c:
            c.execute("UPDATE chaves SET expira_em = ? WHERE id = ? AND revogada_em IS NULL", (iso(expira_em), id_))
            linha = c.execute("SELECT * FROM chaves WHERE id = ?", (id_,)).fetchone()
        return _chave(linha) if linha else None

    def revogar(self, id_: str, motivo: str) -> Chave | None:
        with self._transacao() as c:
            c.execute(
                "UPDATE chaves SET revogada_em = ?, motivo_revogacao = ? WHERE id = ? AND revogada_em IS NULL",
                (iso(agora()), motivo, id_),
            )
            linha = c.execute("SELECT * FROM chaves WHERE id = ?", (id_,)).fetchone()
        return _chave(linha) if linha else None

    # ------------------------------------------------------------------ consumo

    def consumir(self, chave: Chave, escopo: str, momento: datetime) -> Literal["ok", "limite_minuto", "cota_esgotada"]:
        """Verifica e consome, atomicamente, uma unidade do limite por minuto e da cota do período."""
        periodo, minuto = periodo_de(momento), minuto_de(momento)
        with self._transacao() as c:
            usados_minuto = c.execute(
                "SELECT COALESCE(SUM(contagem), 0) FROM janela WHERE chave_id = ? AND minuto = ?", (chave.id, minuto)
            ).fetchone()[0]
            if usados_minuto >= chave.limite_por_minuto:
                return "limite_minuto"
            usados = c.execute(
                "SELECT COALESCE(SUM(contagem), 0) FROM uso WHERE chave_id = ? AND periodo = ? AND escopo = ?",
                (chave.id, periodo, escopo),
            ).fetchone()[0]
            if usados >= chave.cota(escopo):
                return "cota_esgotada"
            c.execute(
                "INSERT INTO janela (chave_id, minuto, contagem) VALUES (?, ?, 1) "
                "ON CONFLICT (chave_id, minuto) DO UPDATE SET contagem = contagem + 1",
                (chave.id, minuto),
            )
            c.execute(
                "INSERT INTO uso (chave_id, periodo, escopo, contagem) VALUES (?, ?, ?, 1) "
                "ON CONFLICT (chave_id, periodo, escopo) DO UPDATE SET contagem = contagem + 1",
                (chave.id, periodo, escopo),
            )
            # Janelas de minutos passados não servem mais.
            c.execute("DELETE FROM janela WHERE chave_id = ? AND minuto < ?", (chave.id, minuto))
        return "ok"

    def estornar(self, chave: Chave, escopo: str, momento: datetime) -> None:
        """Devolve a unidade de cota de uma requisição que não teve sucesso (o limite por minuto não volta)."""
        with self._transacao() as c:
            c.execute(
                "UPDATE uso SET contagem = contagem - 1 WHERE chave_id = ? AND periodo = ? AND escopo = ? AND contagem > 0",
                (chave.id, periodo_de(momento), escopo),
            )

    def uso(self, chave_id: str, periodo: str) -> dict[str, int]:
        with self._conexao() as c:
            linhas = c.execute("SELECT escopo, contagem FROM uso WHERE chave_id = ? AND periodo = ?", (chave_id, periodo))
            return {linha["escopo"]: linha["contagem"] for linha in linhas}

    def registrar(self, chave_id: str, rota: str, status: int, latencia_ms: int) -> None:
        with self._transacao() as c:
            c.execute(
                "INSERT INTO requisicoes (chave_id, rota, status, latencia_ms, criada_em) VALUES (?,?,?,?,?)",
                (chave_id, rota, status, latencia_ms, iso(agora())),
            )

    def requisicoes(self, chave_id: str, limite: int) -> list[dict[str, Any]]:
        with self._conexao() as c:
            linhas = c.execute(
                "SELECT rota, status, latencia_ms, criada_em FROM requisicoes WHERE chave_id = ? ORDER BY id DESC LIMIT ?",
                (chave_id, limite),
            )
            return [dict(linha) for linha in linhas]


# ---------------------------------------------------------------------- pedidos (Mercado Pago)


@dataclass(frozen=True)
class Pedido:
    id: str
    external_reference: str
    cliente: str
    plano: str
    validade_dias: int
    valor_centavos: int
    situacao: Literal["aguardando", "pago", "resgatado", "estornado"]
    criado_em: datetime
    pagamento_id: str | None
    valor_pago_centavos: int | None
    pago_em: datetime | None
    chave_id: str | None
    resgatado_em: datetime | None
    estornado_em: datetime | None
    motivo_estorno: str | None


def _pedido(linha: sqlite3.Row) -> Pedido:
    def data(campo: str) -> datetime | None:
        return datetime.fromisoformat(linha[campo]) if linha[campo] else None

    return Pedido(
        id=linha["id"], external_reference=linha["external_reference"], cliente=linha["cliente"],
        plano=linha["plano"], validade_dias=linha["validade_dias"], valor_centavos=linha["valor_centavos"],
        situacao=linha["situacao"], criado_em=data("criado_em"), pagamento_id=linha["pagamento_id"],
        valor_pago_centavos=linha["valor_pago_centavos"], pago_em=data("pago_em"), chave_id=linha["chave_id"],
        resgatado_em=data("resgatado_em"), estornado_em=data("estornado_em"), motivo_estorno=linha["motivo_estorno"],
    )


class BancoPedidos:
    """Pedidos pagos pelo Mercado Pago. Mesmo arquivo SQLite das chaves."""

    def __init__(self, banco: Banco):
        self._banco = banco

    def criar(self, *, codigo_hash: str, cliente: str, plano: str, validade_dias: int, valor_centavos: int) -> Pedido:
        id_ = str(uuid.uuid4())
        with self._banco._transacao() as c:
            c.execute(
                "INSERT INTO pedidos (id, external_reference, codigo_hash, cliente, plano, validade_dias, valor_centavos,"
                " situacao, criado_em) VALUES (?,?,?,?,?,?,?,'aguardando',?)",
                (id_, f"rtc-{id_}", codigo_hash, cliente, plano, validade_dias, valor_centavos, iso(agora())),
            )
            return _pedido(c.execute("SELECT * FROM pedidos WHERE id = ?", (id_,)).fetchone())

    def obter(self, id_: str) -> Pedido | None:
        with self._banco._conexao() as c:
            linha = c.execute("SELECT * FROM pedidos WHERE id = ?", (id_,)).fetchone()
        return _pedido(linha) if linha else None

    def por_referencia(self, external_reference: str) -> Pedido | None:
        with self._banco._conexao() as c:
            linha = c.execute("SELECT * FROM pedidos WHERE external_reference = ?", (external_reference,)).fetchone()
        return _pedido(linha) if linha else None

    def por_codigo(self, codigo_hash: str) -> Pedido | None:
        with self._banco._conexao() as c:
            linha = c.execute("SELECT * FROM pedidos WHERE codigo_hash = ?", (codigo_hash,)).fetchone()
        return _pedido(linha) if linha else None

    def marcar_pago(self, id_: str, pagamento_id: str, valor_pago_centavos: int, pago_em: datetime) -> bool:
        """Aguardando -> pago. Devolve False se o pedido já não estava aguardando (idempotente)."""
        with self._banco._transacao() as c:
            r = c.execute(
                "UPDATE pedidos SET situacao = 'pago', pagamento_id = ?, valor_pago_centavos = ?, pago_em = ?"
                " WHERE id = ? AND situacao = 'aguardando'",
                (pagamento_id, valor_pago_centavos, iso(pago_em), id_),
            )
            return r.rowcount == 1

    def marcar_resgatado(self, id_: str, chave_id: str) -> bool:
        """Pago -> resgatado, com a chave emitida. Só um resgate passa, mesmo com requisições simultâneas."""
        with self._banco._transacao() as c:
            r = c.execute(
                "UPDATE pedidos SET situacao = 'resgatado', chave_id = ?, resgatado_em = ? WHERE id = ? AND situacao = 'pago'",
                (chave_id, iso(agora()), id_),
            )
            return r.rowcount == 1

    def marcar_estornado(self, id_: str, motivo: str) -> bool:
        with self._banco._transacao() as c:
            r = c.execute(
                "UPDATE pedidos SET situacao = 'estornado', estornado_em = ?, motivo_estorno = ?"
                " WHERE id = ? AND situacao IN ('pago', 'resgatado')",
                (iso(agora()), motivo, id_),
            )
            return r.rowcount == 1
