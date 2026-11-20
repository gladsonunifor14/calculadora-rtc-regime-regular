"""Base de fundamentação legal e conferência de citações do consultor jurídico.

A base vem da calculadora oficial (dados abertos, fundamentações legais): para cada classificação
tributária (cClassTrib), o trecho da LC 214/2025 que a fundamenta, a referência normativa (livro, título,
capítulo, seção) e o artigo. Cada trecho recebe um identificador estável ("F-<cClassTrib>") que o agente
usa para citar.

A conferência é determinística: cada citação precisa existir na base e o texto citado precisa estar,
palavra por palavra, no trecho; todo artigo mencionado na resposta precisa aparecer num trecho citado;
toda cClassTrib mencionada precisa existir.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from .client import CalculadoraError, RtcClient


class ErroConsulta(Exception):
    """A base de fundamentação não pôde ser carregada."""


@dataclass(frozen=True)
class Trecho:
    id: str
    cClassTrib: str
    descricao: str
    cst: str
    descricaoCst: str
    artigo: str
    referencia: str
    texto: str


def _limpar(texto: str | None) -> str:
    # A base traz quebras de linha escapadas ("\\n") e espaços repetidos.
    t = (texto or "").replace("\\n", "\n")
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", t)).strip()


async def carregar_base(cliente: RtcClient, data: str) -> tuple[list[Trecho], dict[str, Any]]:
    """Carrega as fundamentações legais vigentes na data."""
    try:
        r = await cliente.regime_geral("GET", "calculadora/dados-abertos/fundamentacoes-legais", params={"data": data})
    except CalculadoraError as exc:
        raise ErroConsulta(f"Não foi possível carregar a fundamentação legal: {exc}") from exc
    trechos = []
    for f in r["dados"] or []:
        cc = f.get("codigoClassificacaoTributaria")
        if not cc or not f.get("texto"):
            continue
        trechos.append(
            Trecho(
                id=f"F-{cc}",
                cClassTrib=cc,
                descricao=_limpar(f.get("descricaoClassificacaoTributaria")),
                cst=f.get("codigoSituacaoTributaria") or "",
                descricaoCst=_limpar(f.get("descricaoSituacaoTributaria")),
                artigo=_limpar(f.get("textoCurto")),
                # Os níveis finais (capítulo, seção) bastam para localizar o dispositivo e poupam tokens.
                referencia=" > ".join([p.strip() for p in _limpar(f.get("referenciaNormativa")).split("\n") if p.strip()][-4:]),
                texto=_limpar(f.get("texto")),
            )
        )
    if not trechos:
        raise ErroConsulta(f"A calculadora não devolveu fundamentação legal para {data}.")
    trechos.sort(key=lambda t: t.cClassTrib)
    try:
        versao = (await cliente.regime_geral("GET", "calculadora/dados-abertos/versao"))["dados"] or {}
    except CalculadoraError:
        versao = {}
    return trechos, {"versaoBaseDados": versao.get("versaoDb"), "dataVersaoBase": versao.get("dataVersaoDb"), "data": data}


def formatar_base(trechos: list[Trecho]) -> str:
    """A base em texto, para o prompt do agente."""
    blocos = []
    for t in trechos:
        blocos.append(
            f'<trecho id="{t.id}">\n'
            f"cClassTrib {t.cClassTrib} (CST {t.cst} - {t.descricaoCst}): {t.descricao}\n"
            f"Dispositivo: {t.artigo} | {t.referencia}\n"
            f"{t.texto}\n</trecho>"
        )
    return "\n".join(blocos)


def _normalizar(texto: str) -> str:
    t = unicodedata.normalize("NFKC", texto)
    t = t.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", t).strip().lower()


ARTIGO = re.compile(r"\bart(?:igo)?s?\.?\s*(\d{1,3})", re.IGNORECASE)
CCLASSTRIB = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def artigos_em(texto: str) -> set[str]:
    return {m.group(1) for m in ARTIGO.finditer(texto)}


def conferir_citacoes(
    resposta: str, citacoes: list[dict[str, str]], base_suficiente: bool, trechos: list[Trecho]
) -> list[str]:
    """Confere a resposta e as citações contra a base. Devolve os problemas encontrados."""
    por_id = {t.id: t for t in trechos}
    problemas: list[str] = []
    citados: list[Trecho] = []
    for c in citacoes:
        t = por_id.get(c["id"])
        if t is None:
            problemas.append(f"Citação {c['id']!r} não existe na base.")
            continue
        trecho = c["trecho"].strip().strip('"').strip("“”").rstrip(".…").strip()
        if len(trecho) < 15:
            problemas.append(f"Citação {c['id']}: trecho curto demais para ser conferido ({trecho!r}).")
            continue
        if _normalizar(trecho) not in _normalizar(t.texto):
            problemas.append(f"Citação {c['id']}: o texto citado não está, palavra por palavra, no trecho da base.")
            continue
        citados.append(t)

    if base_suficiente and not citados:
        problemas.append("Resposta marcada como fundamentada, mas sem nenhuma citação válida.")

    # Todo artigo mencionado na resposta deve estar num trecho citado (texto ou dispositivo).
    apoio = set()
    for t in citados:
        apoio |= artigos_em(t.texto) | artigos_em(t.artigo)
    sem_apoio = sorted(artigos_em(resposta) - apoio, key=int)
    if sem_apoio:
        problemas.append("Artigos mencionados sem trecho citado que os contenha: " + ", ".join(f"art. {a}" for a in sem_apoio) + ".")

    codigos = {t.cClassTrib for t in trechos}
    inexistentes = sorted(set(CCLASSTRIB.findall(resposta)) - codigos)
    if inexistentes:
        problemas.append("cClassTrib mencionadas que não existem na base: " + ", ".join(inexistentes) + ".")
    return problemas
