"""Gera casos.jsonl e CASOS.md a partir de casos_rascunho.py, conferindo cada gabarito na calculadora.

Uso: python gerar_casos.py   (requer a calculadora do regime regular em RTC_REGIME_GERAL_URL)
"""
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from casos_rascunho import C  # noqa: E402

AQUI = Path(__file__).parent
# Registro de quem validou o gabarito; atualize ao mudar casos e obter nova revisão.
ORIGEM_GABARITO = (
    "derivado dos pares NCM/NBS -> cClassTrib dos testes oficiais; conferido na calculadora; "
    "revisado e aprovado pelo responsável em 2026-11-20"
)
B = os.environ.get("RTC_REGIME_GERAL_URL", "http://localhost:8080/api") + "/calculadora/dados-abertos"
cli = httpx.Client(timeout=60)


def get(caminho, **params):
    r = cli.get(f"{B}/{caminho}", params=params)
    r.raise_for_status()
    return r.json()


def main():
    data = C[0]["data"]
    versao = get("versao")
    catalogo = {c["codigo"]: c for c in get("classificacoes-tributarias/cbs-ibs", data=data)}
    cst_de = {}
    for s in get("situacoes-tributarias/cbs-ibs", data=data):
        for c in s["classificacoesTributarias"]:
            cst_de.setdefault(c["codigo"], []).append(s["codigo"])
    fund = {}
    for f in get("fundamentacoes-legais", data=data):
        fund.setdefault(f["codigoClassificacaoTributaria"], f)

    def aplicavel(cc, ncm=None, nbs=None):
        if ncm:
            return get("classificacoes-tributarias/ncm-aplicavel", cClassTrib=cc, ncm=ncm, dataOcorrenciaFatoGerador=data)["valido"]
        if nbs:
            return get("classificacoes-tributarias/nbs-aplicavel", cClassTrib=cc, nbs=nbs, dataOcorrenciaFatoGerador=data)["valido"]
        return None

    def do_anexo(ncm):
        out = []
        for cc, c in catalogo.items():
            if c.get("nomenclatura") == "NBS":
                continue
            if aplicavel(cc, ncm=ncm) and not aplicavel(cc, ncm="00000000"):
                out.append(cc)
        return out

    linhas, md = [], []
    for k in C:
        g = k["gabarito"]
        codigos = g.get("aceitas") or g.get("plausiveis")
        for cc in codigos:
            assert cc in cst_de, f"{k['id']}: {cc} inexistente"
            assert aplicavel(cc, g.get("ncm"), g.get("nbs")) is not False, f"{k['id']}: {cc} rejeita a nomenclatura"
        if k["tipo"] == "definido":
            assert g["cst"] in cst_de[codigos[0]], f"{k['id']}: CST {g['cst']}"
        k = dict(k)
        k["origem_gabarito"] = ORIGEM_GABARITO
        k["conferencia"] = {
            "outrasDoAnexo": [c for c in do_anexo(g["ncm"]) if c not in codigos] if g.get("ncm") else [],
            "versaoBaseDados": versao.get("versaoDb"),
        }
        linhas.append(k)

    (AQUI / "casos.jsonl").write_text("".join(json.dumps(k, ensure_ascii=False) + "\n" for k in linhas))

    def desc(cc):
        return f"`{cc}` (CST {'/'.join(cst_de[cc])}) {catalogo.get(cc, {}).get('descricao', '')}"

    def ref(cc):
        r = (fund.get(cc) or {}).get("referenciaNormativa") or ""
        return " ".join(r.split())[-160:]

    n_def = sum(k["tipo"] == "definido" for k in linhas)
    md.append("# Casos de avaliação do classificador fiscal\n")
    md.append(
        f"{len(linhas)} casos ({n_def} definidos, {len(linhas) - n_def} ambíguos), data do fato gerador {data}, "
        f"base de dados da calculadora {versao.get('versaoDb')}. Derivados dos pares NCM/NBS → cClassTrib dos testes "
        "oficiais (`src/test/resources/entradas`). Cada gabarito foi conferido na calculadora: a cClassTrib existe, "
        "pertence ao CST e aceita a NCM/NBS.\n"
    )
    md.append(f"**Gabarito:** {ORIGEM_GABARITO}.\n")
    md.append("**O que revisar:** se o gabarito está certo para a descrição e o contexto. Atenção especial às linhas "
              "com *outras classificações do anexo*: a calculadora aceita mais de uma, e a escolha depende de "
              "interpretação.\n")
    md.append("Gerado por `gerar_casos.py`; para mudar um caso, edite `casos_rascunho.py` e gere de novo.\n")
    md.append("| id | tipo | tema | código informado | gabarito |\n|---|---|---|---|---|")
    for k in linhas:
        g = k["gabarito"]
        inf = k["ncm_informada"] or k["nbs_informada"] or "-"
        gab = ", ".join(g.get("aceitas") or []) if k["tipo"] == "definido" else "revisão; plausíveis " + ", ".join(g["plausiveis"])
        md.append(f"| `{k['id']}` | {k['tipo']} | {k['tema']} | {inf} | {gab} |")
    for k in linhas:
        g = k["gabarito"]
        md.append(f"\n## `{k['id']}` ({k['tipo']}, {k['tema']})\n")
        md.append("````text")
        md.append(f"Item: {k['descricao']}")
        md.append(f"Contexto: {k['contexto']}")
        if k["ncm_informada"]:
            md.append(f"NCM informada: {k['ncm_informada']}")
        if k["nbs_informada"]:
            md.append(f"NBS informada: {k['nbs_informada']}")
        md.append("````")
        nom = f"NCM {g['ncm']}" if g.get("ncm") else (f"NBS {g['nbs']}" if g.get("nbs") else "sem código de nomenclatura no gabarito")
        if k["tipo"] == "definido":
            md.append(f"- **Gabarito:** {desc(g['aceitas'][0])}; {nom}")
            md.append(f"- **Fundamentação (calculadora):** …{ref(g['aceitas'][0])}")
        else:
            md.append(f"- **Esperado:** pedir informação / revisão. Falta saber {g['falta']}.")
            md.append(f"- **Plausíveis:** " + "; ".join(desc(c) for c in g["plausiveis"]) + f"; {nom}")
        outras = k["conferencia"]["outrasDoAnexo"]
        if outras:
            md.append(f"- **Outras classificações cujo anexo inclui a NCM:** " + "; ".join(desc(c) for c in outras))
    (AQUI / "CASOS.md").write_text("\n".join(md) + "\n")
    print(f"{len(linhas)} casos gravados ({n_def} definidos)")


if __name__ == "__main__":
    main()
