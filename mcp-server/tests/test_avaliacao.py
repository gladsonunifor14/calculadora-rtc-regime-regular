"""Testes do harness de avaliação do classificador (avaliacao/avaliar.py).

Verificação exigida antes de qualquer rodada paga: oráculo (~100%), resposta constante (nota baixa),
erro de API fora das notas e modelo servido diferente detectado. Usa uma API da Anthropic simulada,
o rtc-mcp real e a calculadora real; os testes de integração são pulados sem a calculadora.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

AVALIACAO = Path(__file__).resolve().parent.parent / "avaliacao"
sys.path.insert(0, str(AVALIACAO))

import avaliar  # noqa: E402
from test_classificador import _calculadora_acessivel, _parametros_servidor  # noqa: E402

CASOS = [json.loads(x) for x in (AVALIACAO / "casos.jsonl").read_text().splitlines()]
POR_PEDIDO = {(c["descricao"], c["contexto"]): c for c in CASOS}

# ----------------------------------------------------------------------
# Nota (unitários)
# ----------------------------------------------------------------------


def _resultado(c_class_trib, cst="200", revisao=False, ncm=None, nbs=None, confirmado=True):
    return {
        "classificacao": {"cClassTrib": c_class_trib, "cst": cst, "ncm": ncm, "nbs": nbs},
        "revisaoHumanaRecomendada": revisao,
        "verificacoes": ["CST, cClassTrib e NCM/NBS confirmados na calculadora."] if confirmado else ["falhou"],
    }


def _caso(id_):
    return next(c for c in CASOS if c["id"] == id_)


def test_nota_definido_certo():
    nota, _ = avaliar.dar_nota(_caso("arroz-varejo"), _resultado("200003", ncm="10063021"))
    assert nota == {
        "acerto": 1.0, "acerto_definidos": 1.0, "sinalizou_ambiguos": None, "revisao_indevida": 0.0,
        "cst_ok": 1.0, "nomenclatura_ok": 1.0, "conferencia_ok": 1.0, "recusa": 0.0,
    }


def test_nota_definido_certo_mas_com_revisao_e_ncm_de_outra_posicao():
    nota, _ = avaliar.dar_nota(_caso("arroz-varejo"), _resultado("200003", revisao=True, ncm="10019900"))
    assert nota["acerto"] == 1.0 and nota["revisao_indevida"] == 1.0 and nota["nomenclatura_ok"] == 0.0


def test_nota_ncm_informada_exige_codigo_identico():
    caso = _caso("arroz-parboilizado-ncm")
    assert avaliar.dar_nota(caso, _resultado("200003", ncm="10063011"))[0]["nomenclatura_ok"] == 1.0
    assert avaliar.dar_nota(caso, _resultado("200003", ncm="10063021"))[0]["nomenclatura_ok"] == 0.0


def test_nota_definido_errado():
    nota, explicacao = avaliar.dar_nota(_caso("bolsa-coletora-publico"), _resultado("200030", ncm="39269030"))
    assert nota["acerto"] == 0.0 and nota["revisao_indevida"] == 0.0
    assert "200030" in explicacao["acerto"]


def test_nota_ambiguo():
    caso = _caso("dipirona")
    sinalizou = avaliar.dar_nota(caso, _resultado("200032", revisao=True, ncm="30049069"))[0]
    cravou = avaliar.dar_nota(caso, _resultado("200032", revisao=False, ncm="30049069"))[0]
    implausivel = avaliar.dar_nota(caso, _resultado("000001", cst="000", revisao=True, ncm="30049069"))[0]
    assert (sinalizou["acerto"], sinalizou["sinalizou_ambiguos"]) == (1.0, 1.0)
    assert (cravou["acerto"], cravou["sinalizou_ambiguos"]) == (0.0, 0.0)
    assert implausivel["acerto"] == 0.0
    assert sinalizou["cst_ok"] is None and sinalizou["revisao_indevida"] is None
    assert sinalizou["conferencia_ok"] is None


def test_casos_bem_formados():
    ids = [c["id"] for c in CASOS]
    assert len(ids) == len(set(ids)) == 41
    ambiguos = sum(c["tipo"] == "ambiguo" for c in CASOS)
    assert 0.2 <= ambiguos / len(CASOS) <= 0.3
    assert len(POR_PEDIDO) == len(CASOS), "dois casos com a mesma descrição e contexto"
    for c in CASOS:
        g = c["gabarito"]
        assert (g.get("aceitas") if c["tipo"] == "definido" else g.get("plausiveis")), c["id"]


def test_modelo_confere():
    assert avaliar.modelo_confere("claude-opus-5-5", "claude-opus-5-5")
    assert avaliar.modelo_confere("claude-opus-5-5", "claude-opus-5-5-20260401")
    assert not avaliar.modelo_confere("claude-opus-5-5", "claude-opus-5")
    assert not avaliar.modelo_confere("claude-opus-5-5", None)


def test_custo():
    uso = {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_input_tokens": 0,
           "cache_creation_input_tokens": 0}
    assert avaliar.custo("claude-opus-5-5", uso) == pytest.approx(6.0)
    assert avaliar.custo("desconhecido", uso) is None


# ----------------------------------------------------------------------
# Integração: oráculo, nulo, erros
# ----------------------------------------------------------------------


def _resposta_oraculo(caso: dict[str, Any]) -> dict[str, Any]:
    g = caso["gabarito"]
    definido = caso["tipo"] == "definido"
    return {
        "status": "classificado" if definido else "precisa_informacao",
        "item": caso["descricao"],
        "ncm": caso.get("ncm_informada") or g.get("ncm"),
        "nbs": caso.get("nbs_informada") or g.get("nbs"),
        "cst": g["cst"] if definido else None,
        "cClassTrib": g["aceitas"][0] if definido else g["plausiveis"][0],
        "descricaoClassificacao": None,
        "tratamentoTributario": None,
        "reducao": None,
        "fundamentacaoLegal": None,
        "confianca": "alta" if definido else "baixa",
        "justificativa": "oráculo",
        "premissas": [],
        "alternativas": [],
        "perguntas": [] if definido else ["?"],
    }


def _resposta_constante(caso: dict[str, Any]) -> dict[str, Any]:
    r = _resposta_oraculo(caso)
    return r | {"status": "classificado", "cst": "000", "cClassTrib": "000001", "confianca": "alta",
                "ncm": r["ncm"] or "87032100", "perguntas": []}


class ApiRoteiro(BaseHTTPRequestHandler):
    modo = "oraculo"
    modelo_servido: str | None = None

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if ApiRoteiro.modo == "erro":
            self._enviar(529, {"type": "error", "error": {"type": "overloaded_error", "message": "sobrecarga"}})
            return
        texto = corpo["messages"][0]["content"]
        campos = dict(linha.split(": ", 1) for linha in texto.splitlines() if ": " in linha)
        caso = POR_PEDIDO[(campos["Item"], campos["Contexto da operação"])]
        resposta = _resposta_oraculo(caso) if ApiRoteiro.modo == "oraculo" else _resposta_constante(caso)
        self._enviar(200, {
            "id": "msg_1", "type": "message", "role": "assistant",
            "model": ApiRoteiro.modelo_servido or corpo["model"],
            "content": [{"type": "text", "text": json.dumps(resposta)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 500,
                      "cache_creation_input_tokens": 0},
        })

    def _enviar(self, codigo, corpo):
        dados = json.dumps(corpo).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)


@pytest.fixture
def api():
    ApiRoteiro.modo, ApiRoteiro.modelo_servido = "oraculo", None
    srv = HTTPServer(("127.0.0.1", 0), ApiRoteiro)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


async def _rodar(api_url: str, flow: Path, *extra: str) -> list[dict[str, Any]]:
    from anthropic import AsyncAnthropic

    base = ["--flow", str(flow), "--reps", "1", "--concorrencia", "8", *extra]
    assert await avaliar.principal(avaliar.argumentos([*base, "--aprovar-harness"])) == 0
    codigo = await avaliar.principal(
        avaliar.argumentos(base),
        cliente_fabrica=lambda: AsyncAnthropic(base_url=api_url, api_key="teste", max_retries=0),
        parametros_servidor=_parametros_servidor(),
    )
    assert codigo == 0
    arq = flow / "baseline" / "results.jsonl"
    return [json.loads(x) for x in arq.read_text().splitlines()] if arq.exists() else []


def _media(linhas, metrica):
    vals = [r["grade"][metrica] for r in linhas if r["grade"][metrica] is not None]
    return sum(vals) / len(vals)


calculadora = pytest.mark.skipif(not _calculadora_acessivel(), reason="calculadora do regime regular não está acessível")


@calculadora
async def test_oraculo_tira_nota_maxima(api, tmp_path):
    linhas = await _rodar(api, tmp_path)
    assert len(linhas) == 41
    assert _media(linhas, "acerto") == 1.0
    assert _media(linhas, "revisao_indevida") == 0.0
    assert _media(linhas, "sinalizou_ambiguos") == 1.0
    # A conferência na calculadora aprova todos os gabaritos definidos.
    assert _media(linhas, "conferencia_ok") == 1.0
    linha = linhas[0]
    assert linha["model"] == "claude-opus-5-5" and linha["usage"]["output_tokens"] == 200
    assert linha["cost_usd"] == pytest.approx((1000 * 4 + 500 * 0.4 + 200 * 20) / 1e6)
    trace = json.loads((tmp_path / "baseline" / "traces" / f"{linha['prompt_id']}_rep0.json").read_text())
    assert [t["role"] for t in trace][:2] == ["system", "user"] and trace[-1]["role"] == "assistant"


@calculadora
async def test_resposta_constante_tira_nota_baixa(api, tmp_path):
    ApiRoteiro.modo = "constante"
    linhas = await _rodar(api, tmp_path)
    assert _media(linhas, "acerto") < 0.1  # só o caso cujo gabarito é 000001 acerta
    assert _media(linhas, "sinalizou_ambiguos") == 0.0


@calculadora
async def test_erro_de_api_vai_para_errors_e_retomada_completa(api, tmp_path):
    ApiRoteiro.modo = "erro"
    linhas = await _rodar(api, tmp_path, "--casos", "arroz-varejo")
    assert linhas == []
    erros = [json.loads(x) for x in (tmp_path / "baseline" / "errors.jsonl").read_text().splitlines()]
    assert erros and erros[0]["classe"] == "api" and erros[0]["tentativas"] == 4
    # Retomada: o erro não ocupa o lugar do caso; depois que a API volta, ele roda.
    ApiRoteiro.modo = "oraculo"
    linhas = await _rodar(api, tmp_path, "--casos", "arroz-varejo")
    assert [r["prompt_id"] for r in linhas] == ["arroz-varejo"]
    # E não roda de novo o que já foi gravado.
    assert len(await _rodar(api, tmp_path, "--casos", "arroz-varejo")) == 1


@calculadora
async def test_modelo_servido_diferente_e_detectado(api, tmp_path):
    ApiRoteiro.modelo_servido = "claude-opus-5"
    linhas = await _rodar(api, tmp_path, "--casos", "arroz-varejo")
    assert linhas == []
    erro = json.loads((tmp_path / "baseline" / "errors.jsonl").read_text().splitlines()[0])
    assert erro["classe"] == "modelo_servido_diferente" and erro["servidos"] == ["claude-opus-5"]


async def test_harness_alterado_exige_nova_aprovacao(tmp_path, monkeypatch):
    args = avaliar.argumentos(["--flow", str(tmp_path)])
    assert await avaliar.principal(args) == 2  # nunca aprovado
    assert await avaliar.principal(avaliar.argumentos(["--flow", str(tmp_path), "--aprovar-harness"])) == 0
    monkeypatch.setattr(avaliar, "sha_harness", lambda: "outro")
    assert await avaliar.principal(args) == 2
