"""Gera as NF-e de teste do auditor (nfe_2026_conforme.xml e nfe_2027_conforme.xml).

Parte da NF-e de exemplo válida no XSD (src/test/resources/xml/nfe/valido/nota/nfe_valido.xml), troca a
data, os itens e os totais, e preenche o grupo IBSCBS com os valores das calculadoras oficiais. Rodar só
quando for preciso regerar os arquivos (requer a calculadora do regime regular em execução):

    python tests/fixtures/gerar_nfe.py
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import httpx

AQUI = Path(__file__).resolve().parent
EXEMPLO = AQUI.parents[2] / "src/test/resources/xml/nfe/valido/nota/nfe_valido.xml"
API = "http://localhost:8080/api/calculadora"

# (descrição, NCM, CST, cClassTrib, vProd, vFrete, vDesc, alíquota de ICMS %)
ITENS = [
    ("ARROZ BRANCO TIPO 1 5KG", "10063021", "200", "200003", "500.00", "0.00", "0.00", "0"),
    ("SABONETE EM BARRA 90G", "34011190", "200", "200035", "1000.00", "0.00", "0.00", "18"),
    ("CADERNO UNIVERSITARIO 200 FOLHAS", "48202000", "000", "000001", "2000.00", "100.00", "50.00", "18"),
]
ALIQUOTAS_2027 = {"cbs": 8.8, "ibsEstadual": 0.05, "ibsMunicipal": 0.05}

D = Decimal
C = D("0.01")


def q(v) -> str:
    return str(D(str(v)).quantize(C))


def grupo_red(red: str | None, efet: str | None) -> str:
    if red is None:
        return ""
    return f"<gRed><pRedAliq>{red}</pRedAliq><pAliqEfet>{efet}</pAliqEfet></gRed>"


def gerar(ano: int) -> str:
    cli = httpx.Client(timeout=30)
    data = f"{ano}-06-10"
    linhas = []
    for n, (desc, ncm, cst, cct, vprod, vfrete, vdesc, picms) in enumerate(ITENS, start=1):
        bc_icms = D(vprod) + D(vfrete) - D(vdesc)
        vicms = (bc_icms * D(picms) / 100).quantize(C)
        vpis = (bc_icms * D("1.65") / 100).quantize(C) if ano == 2026 else D(0)
        vcofins = (bc_icms * D("7.60") / 100).quantize(C) if ano == 2026 else D(0)
        componentes = {"vProd": float(vprod), "vFrete": float(vfrete), "vDesc": float(vdesc), "vICMS": float(vicms)}
        if ano == 2026:
            componentes |= {"vPIS": float(vpis), "vCOFINS": float(vcofins)}
        base = cli.post(f"{API}/base-calculo/base-calculo-unificada",
                        json={"anoFatoGerador": ano, "tipoDocumento": 55, "natureza": "bem", **componentes}).json()
        vbc = q(base["baseRG"])
        item = {"numero": 1, "ncm": ncm, "cst": cst, "cClassTrib": cct, "baseCalculo": float(vbc),
                "quantidade": 1, "unidade": "UN"}
        if ano >= 2027:
            item["aliquotasNominais"] = ALIQUOTAS_2027
        r = cli.post(f"{API}/observabilidade/regime-geral", json={
            "id": "fixture", "versao": "1.0.0", "dhFatoGerador": f"{data}T10:00:00-03:00",
            "municipio": 2111300, "uf": "MA", "tpDoc": 55, "itens": [item]}).json()
        g = r["objetos"][0]["tribCalc"]["IBSCBS"]["gIBSCBS"]
        uf, mun, cbs = g["gIBSUF"], g["gIBSMun"], g["gCBS"]
        r_uf, r_mun, r_cbs = uf.get("gRed") or {}, mun.get("gRed") or {}, cbs.get("gRed") or {}
        pis = (f"<PISAliq><CST>01</CST><vBC>{q(bc_icms)}</vBC><pPIS>1.6500</pPIS><vPIS>{vpis}</vPIS></PISAliq>"
               if ano == 2026 else "<PISNT><CST>08</CST></PISNT>")
        cofins = (f"<COFINSAliq><CST>01</CST><vBC>{q(bc_icms)}</vBC><pCOFINS>7.6000</pCOFINS><vCOFINS>{vcofins}</vCOFINS></COFINSAliq>"
                  if ano == 2026 else "<COFINSNT><CST>08</CST></COFINSNT>")
        frete = f"<vFrete>{vfrete}</vFrete>" if D(vfrete) else ""
        desconto = f"<vDesc>{vdesc}</vDesc>" if D(vdesc) else ""
        linhas.append({
            "vprod": D(vprod), "vfrete": D(vfrete), "vdesc": D(vdesc), "vicms": vicms, "vpis": vpis, "vcofins": vcofins,
            "bc_icms": bc_icms if D(picms) else D(0), "vbc": D(vbc), "vibsuf": D(uf["vIBSUF"]), "vibsmun": D(mun["vIBSMun"]),
            "vibs": D(g["vIBS"]), "vcbs": D(cbs["vCBS"]),
            "xml": f"""      <det nItem="{n}">
        <prod>
          <cProd>{n:08d}</cProd><cEAN>SEM GTIN</cEAN><xProd>{desc}</xProd><NCM>{ncm}</NCM><CFOP>5102</CFOP>
          <uCom>UN</uCom><qCom>1.0000</qCom><vUnCom>{vprod}</vUnCom><vProd>{vprod}</vProd>
          <cEANTrib>SEM GTIN</cEANTrib><uTrib>UN</uTrib><qTrib>1.0000</qTrib><vUnTrib>{vprod}</vUnTrib>{frete}{desconto}<indTot>1</indTot>
        </prod>
        <imposto>
          <ICMS><ICMS00><orig>0</orig><CST>00</CST><modBC>3</modBC><vBC>{q(bc_icms) if D(picms) else "0.00"}</vBC><pICMS>{D(picms):.2f}</pICMS><vICMS>{vicms}</vICMS></ICMS00></ICMS>
          <PIS>{pis}</PIS>
          <COFINS>{cofins}</COFINS>
          <IBSCBS>
            <CST>{cst}</CST><cClassTrib>{cct}</cClassTrib>
            <gIBSCBS>
              <vBC>{vbc}</vBC>
              <gIBSUF><pIBSUF>{uf["pIBSUF"]}</pIBSUF>{grupo_red(r_uf.get("pRedAliq"), r_uf.get("pAliqEfet"))}<vIBSUF>{uf["vIBSUF"]}</vIBSUF></gIBSUF>
              <gIBSMun><pIBSMun>{mun["pIBSMun"]}</pIBSMun>{grupo_red(r_mun.get("pRedAliq"), r_mun.get("pAliqEfet"))}<vIBSMun>{mun["vIBSMun"]}</vIBSMun></gIBSMun>
              <vIBS>{g["vIBS"]}</vIBS>
              <gCBS><pCBS>{cbs["pCBS"]}</pCBS>{grupo_red(r_cbs.get("pRedAliq"), r_cbs.get("pAliqEfet"))}<vCBS>{cbs["vCBS"]}</vCBS></gCBS>
            </gIBSCBS>
          </IBSCBS>
        </imposto>
      </det>""",
        })

    s = {k: sum((linha[k] for linha in linhas), D(0)) for k in linhas[0] if k != "xml"}
    vnf = s["vprod"] + s["vfrete"] - s["vdesc"]
    total = f"""      <total>
        <ICMSTot>
          <vBC>{q(s["bc_icms"])}</vBC><vICMS>{q(s["vicms"])}</vICMS><vICMSDeson>0.00</vICMSDeson><vFCP>0.00</vFCP>
          <vBCST>0.00</vBCST><vST>0.00</vST><vFCPST>0.00</vFCPST><vFCPSTRet>0.00</vFCPSTRet>
          <vProd>{q(s["vprod"])}</vProd><vFrete>{q(s["vfrete"])}</vFrete><vSeg>0.00</vSeg><vDesc>{q(s["vdesc"])}</vDesc>
          <vII>0.00</vII><vIPI>0.00</vIPI><vIPIDevol>0.00</vIPIDevol><vPIS>{q(s["vpis"])}</vPIS><vCOFINS>{q(s["vcofins"])}</vCOFINS>
          <vOutro>0.00</vOutro><vNF>{q(vnf)}</vNF>
        </ICMSTot>
        <IBSCBSTot>
          <vBCIBSCBS>{q(s["vbc"])}</vBCIBSCBS>
          <gIBS>
            <gIBSUF><vDif>0.00</vDif><vDevTrib>0.00</vDevTrib><vIBSUF>{q(s["vibsuf"])}</vIBSUF></gIBSUF>
            <gIBSMun><vDif>0.00</vDif><vDevTrib>0.00</vDevTrib><vIBSMun>{q(s["vibsmun"])}</vIBSMun></gIBSMun>
            <vIBS>{q(s["vibs"])}</vIBS><vCredPres>0.00</vCredPres><vCredPresCondSus>0.00</vCredPresCondSus>
          </gIBS>
          <gCBS><vDif>0.00</vDif><vDevTrib>0.00</vDevTrib><vCBS>{q(s["vcbs"])}</vCBS><vCredPres>0.00</vCredPres><vCredPresCondSus>0.00</vCredPresCondSus></gCBS>
        </IBSCBSTot>
      </total>"""

    xml = EXEMPLO.read_text()
    xml = re.sub(r"\s*<det nItem.*</det>", "\n" + "\n".join(linha["xml"] for linha in linhas), xml, count=1, flags=re.S)
    xml = re.sub(r"\s*<total>.*</total>", "\n" + total, xml, count=1, flags=re.S)
    xml = xml.replace("2025-09-01T00:00:00-03:00", f"{data}T10:00:00-03:00")
    xml = re.sub(r"<vPag>[^<]*</vPag>", f"<vPag>{q(vnf)}</vPag>", xml)
    xml = xml.replace("<CRT>3</CRT>", "<CRT>3</CRT>")  # emitente do regime normal
    xml = xml.replace("<natOp>Compra P/Ind. sob o Regime de &amp;quot;Drawback&amp;quot;</natOp>", "<natOp>Venda de mercadoria</natOp>")
    xml = xml.replace("<idDest>2</idDest>", "<idDest>1</idDest>")
    return xml


if __name__ == "__main__":
    for ano in (2026, 2027):
        destino = AQUI / f"nfe_{ano}_conforme.xml"
        destino.write_text(gerar(ano))
        print("gravado", destino)
