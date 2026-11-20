"""Servidor MCP que expõe as calculadoras da Reforma Tributária do Consumo.

As ferramentas apenas repassam as chamadas às APIs oficiais: nenhum valor de
tributo é calculado aqui. O papel do agente é montar a entrada, interpretar a
saída e explicar a fundamentação; a conta é sempre da calculadora.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from .client import CalculadoraError, Config, RtcClient

try:  # SDK MCP 2.x
    from mcp.server.mcpserver import MCPServer as _Servidor
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # SDK MCP 1.x
    from mcp.server.fastmcp import FastMCP as _Servidor
    from mcp.server.fastmcp.exceptions import ToolError

from mcp.types import ToolAnnotations

INSTRUCOES = """\
Ferramentas das calculadoras oficiais da Reforma Tributária do Consumo (EC 132/2023 e LC 214/2025):
CBS, IBS (estadual e municipal) e Imposto Seletivo, no regime regular e no Simples Nacional,
e geração do DANFSe (PDF da NFS-e).

Regras de uso:
- Nunca calcule tributo por conta própria. Use calcular_tributos_regime_geral ou
  calcular_tributos_simples_nacional e reporte os valores devolvidos.
- Para classificar um item, consulte NCM/NBS e confirme a combinação com verificar_ncm_aplicavel ou
  verificar_nbs_aplicavel antes de calcular.
- Toda data é a data do fato gerador (AAAA-MM-DD); as tabelas mudam ao longo da transição (2026-2033).
- Cite cClassTrib, CST, fundamentação legal e as versões em "metadados" ao apresentar um resultado.
- Se "metadados.avisoDadosSimulados" estiver presente, avise que os valores são simulados.
- Erros por item (códigos REG-xxx e VSN-xxx) podem ser detalhados com as ferramentas descrever_erro_*.
- gerar_danfse produz o PDF da NFS-e a partir do XML autorizado e devolve o caminho do arquivo gravado.
"""

Data = Annotated[
    str,
    Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="Data do fato gerador no formato AAAA-MM-DD"),
]
Ncm = Annotated[str, Field(pattern=r"^\d{8}$", description="Código NCM com 8 dígitos, sem pontos")]
Nbs = Annotated[str, Field(pattern=r"^\d{9}$", description="Código NBS com 9 dígitos, sem pontos")]
CClassTrib = Annotated[str, Field(pattern=r"^\d{6}$", description="Código de Classificação Tributária (6 dígitos)")]
Cst = Annotated[str, Field(pattern=r"^\d{3}$", description="Código de Situação Tributária (3 dígitos)")]
Tributo = Literal["cbs-ibs", "is"]
TipoDocumento = Literal["nfe", "nfce", "nfse", "cte", "cte-simplificado", "bpe", "bpe-tm", "nf3e"]

LEITURA = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
# Os cálculos não alteram estado nenhum: são simulações sobre tabelas somente leitura.
CALCULO = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)

# Grava um arquivo novo no disco local, sem apagar nem sobrescrever nada.
GERACAO_ARQUIVO = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)

Resultado = dict[str, Any]

SITUACOES_DANFSE = {"valida": 0, "substituida": 1, "cancelada": 2}


def _nome_padrao_danfse(xml: str) -> str:
    """danfse-<Id da infNFSe>.pdf, ou danfse-<data e hora>.pdf se o Id não for encontrado."""
    achado = re.search(r"<(?:\w+:)?infNFSe\b[^>]*\bId\s*=\s*[\"']([^\"']+)[\"']", xml)
    identificador = re.sub(r"[^\w.-]", "_", achado.group(1))[:80] if achado else ""
    if not identificador:
        identificador = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"danfse-{identificador}.pdf"


def _gravar_sem_sobrescrever(diretorio: Path, nome: str, conteudo: bytes) -> Path:
    if not nome.lower().endswith(".pdf"):
        nome += ".pdf"
    base, extensao = nome[:-4], nome[-4:]
    for n in range(1000):
        destino = diretorio / (nome if n == 0 else f"{base}-{n}{extensao}")
        try:
            with destino.open("xb") as arquivo:  # "x" falha se o arquivo já existe
                arquivo.write(conteudo)
            return destino
        except FileExistsError:
            continue
    raise OSError(f"muitos arquivos com o nome {nome}")


def criar_servidor(cliente: RtcClient | None = None) -> Any:
    """Cria o servidor MCP. Recebe o cliente para permitir injeção nos testes."""

    rtc = cliente or RtcClient()
    mcp = _Servidor("rtc-calculadoras", instructions=INSTRUCOES)

    async def regime(metodo: str, caminho: str, **kwargs: Any) -> Resultado:
        try:
            return await rtc.regime_geral(metodo, caminho, **kwargs)
        except CalculadoraError as exc:
            raise ToolError(str(exc)) from exc

    async def simples(metodo: str, caminho: str, **kwargs: Any) -> Resultado:
        try:
            return await rtc.simples_nacional(metodo, caminho, **kwargs)
        except CalculadoraError as exc:
            raise ToolError(str(exc)) from exc

    # ------------------------------------------------------------------
    # Regime regular: cálculo
    # ------------------------------------------------------------------

    @mcp.tool(annotations=CALCULO)
    async def calcular_tributos_regime_geral(
        operacao: Annotated[dict[str, Any], Field(description="Operação no formato OperacaoInput da calculadora")],
        modo: Annotated[
            Literal["observabilidade", "estrito"],
            Field(
                description=(
                    "'observabilidade' (padrão) devolve erros por item sem interromper os demais; "
                    "'estrito' falha no primeiro erro e devolve o ROC usado por gerar_xml_documento"
                )
            ),
        ] = "observabilidade",
    ) -> Resultado:
        """Calcula CBS, IBS (UF e município) e Imposto Seletivo de uma operação do regime regular.

        Estrutura da operação:
        {
          "id": "identificador", "versao": "1.0.0",
          "dhFatoGerador": "2027-03-10T10:00:00-03:00",
          "municipio": 4314902,            # código IBGE
          "uf": "RS",
          "tpDoc": 55,                     # opcional (55 NF-e, 65 NFC-e...)
          "itens": [{
            "numero": 1,
            "ncm": "10063021"  ou  "nbs": "122011200",
            "cst": "200", "cClassTrib": "200003",
            "baseCalculo": 100.00, "quantidade": 20, "unidade": "KG",
            "aliquotasNominais": {"cbs": 8.8, "ibsEstadual": 0.05, "ibsMunicipal": 0.05},
            "impostoSeletivo": {...}       # opcional, só para itens sujeitos ao IS
          }]
        }

        A resposta traz, por item (nObj), o estadoItem, o grupo tribCalc (IBSCBS e/ou IS)
        com alíquotas, reduções e valores, a memoriaCalculo em texto e os erros (code,
        pointer, detail).
        """
        caminho = "calculadora/observabilidade/regime-geral" if modo == "observabilidade" else "calculadora/regime-geral"
        return await regime("POST", caminho, json_body=operacao)

    @mcp.tool(annotations=LEITURA)
    async def descrever_erro_regime_geral(
        codigo: Annotated[str, Field(description="Código do erro da observabilidade, por exemplo REG-011")],
    ) -> Resultado:
        """Explica um código de erro da observabilidade do regime regular e como corrigi-lo."""
        return await regime("GET", "calculadora/observabilidade/erros", params={"codigo": codigo})

    @mcp.tool(annotations=CALCULO)
    async def calcular_base_calculo_unificada(
        entrada: Annotated[dict[str, Any], Field(description="Componentes da base de cálculo do documento")],
        documento: Annotated[
            Literal["nfe", "nfse"],
            Field(description="'nfe' para NF-e/NFC-e ou 'nfse' para NFS-e"),
        ] = "nfe",
    ) -> Resultado:
        """Calcula a base de cálculo unificada da CBS/IBS a partir dos componentes do documento fiscal.

        Use antes de calcular_tributos_regime_geral quando o usuário informar valores de produto,
        frete, seguro, descontos etc. em vez da base pronta. O esquema da entrada está no Swagger
        da calculadora (/api/swagger-ui.html), seção Base de Cálculo.
        """
        caminho = (
            "calculadora/base-calculo/base-calculo-unificada"
            if documento == "nfe"
            else "calculadora/base-calculo/base-calculo-unificada-nfse"
        )
        return await regime("POST", caminho, json_body=entrada)

    # ------------------------------------------------------------------
    # Regime regular: XML dos documentos fiscais
    # ------------------------------------------------------------------

    @mcp.tool(annotations=CALCULO)
    async def gerar_xml_documento(
        roc: Annotated[
            dict[str, Any],
            Field(description="Campo 'dados' devolvido por calcular_tributos_regime_geral com modo='estrito'"),
        ],
        tipo: Annotated[TipoDocumento, Field(description="Documento de destino (NFS-e não suporta geração)")],
    ) -> Resultado:
        """Gera o grupo XML de tributação (IBSCBS/IS) do documento fiscal a partir do resultado do cálculo."""
        return await regime("POST", "calculadora/xml/generate", params={"tipo": tipo}, json_body=roc)

    @mcp.tool(annotations=LEITURA)
    async def validar_xml_documento(
        xml: Annotated[str, Field(description="Conteúdo XML a validar")],
        tipo: TipoDocumento,
        subtipo: Annotated[
            Literal["grupo", "nota"],
            Field(description="'grupo' para só o grupo de tributação ou 'nota' para o documento completo"),
        ] = "grupo",
    ) -> Resultado:
        """Valida um XML (grupo de tributação ou nota inteira) contra o XSD oficial. dados=true se válido."""
        return await regime(
            "POST",
            "calculadora/xml/validate",
            params={"tipo": tipo, "subtipo": subtipo},
            conteudo=xml,
            headers={"Content-Type": "application/xml"},
        )

    # ------------------------------------------------------------------
    # Regime regular: dados abertos (tabelas oficiais)
    # ------------------------------------------------------------------

    @mcp.tool(annotations=LEITURA)
    async def consultar_ncm(ncm: Ncm, data: Data) -> Resultado:
        """Consulta a descrição de uma NCM (mercadoria) vigente na data, incluindo se está sujeita ao IS."""
        return await regime("GET", "calculadora/dados-abertos/ncm", params={"ncm": ncm, "data": data})

    @mcp.tool(annotations=LEITURA)
    async def consultar_nbs(nbs: Nbs, data: Data) -> Resultado:
        """Consulta a descrição de uma NBS (serviço) vigente na data."""
        return await regime("GET", "calculadora/dados-abertos/nbs", params={"nbs": nbs, "data": data})

    @mcp.tool(annotations=LEITURA)
    async def listar_situacoes_tributarias(
        data: Data,
        tributo: Annotated[Tributo, Field(description="'cbs-ibs' ou 'is' (Imposto Seletivo)")] = "cbs-ibs",
        sigla_dfe: Annotated[
            str | None, Field(description="Filtra pelo documento fiscal (por exemplo NFe, NFCe, NFSe)")
        ] = None,
    ) -> Resultado:
        """Lista os CSTs vigentes, cada um com suas classificações tributárias (cClassTrib) aninhadas.

        É o catálogo completo: use para descobrir candidatos a cClassTrib antes de calcular.
        """
        return await regime(
            "GET",
            f"calculadora/dados-abertos/situacoes-tributarias/{tributo}",
            params={"data": data, "siglaDfe": sigla_dfe},
        )

    @mcp.tool(annotations=LEITURA)
    async def consultar_classificacao_tributaria(
        c_class_trib: CClassTrib,
        data: Data,
        tributo: Tributo = "cbs-ibs",
    ) -> Resultado:
        """Detalha uma cClassTrib: descrição, CST, percentuais de redução, indicadores e vigência."""
        segmento = "cbs-ibs" if tributo == "cbs-ibs" else "is"
        return await regime(
            "GET",
            f"calculadora/dados-abertos/classificacoes-tributarias/{segmento}/class-trib/{c_class_trib}",
            params={"data": data},
        )

    @mcp.tool(annotations=LEITURA)
    async def listar_classificacoes_por_cst(cst: Cst, data: Data, tributo: Tributo = "cbs-ibs") -> Resultado:
        """Lista as classificações tributárias (cClassTrib) de um CST vigentes na data."""
        segmento = "cbs-ibs" if tributo == "cbs-ibs" else "imposto-seletivo"
        return await regime(
            "GET", f"calculadora/dados-abertos/classificacoes-tributarias/{segmento}/{cst}", params={"data": data}
        )

    @mcp.tool(annotations=LEITURA)
    async def listar_classificacoes_por_nbs(nbs: Nbs, data: Data) -> Resultado:
        """Lista as cClassTrib que podem ser usadas com uma NBS (serviço) na data."""
        return await regime(
            "GET",
            "calculadora/dados-abertos/classificacoes-tributarias/nbs",
            params={"nbs": nbs, "data": data},
        )

    @mcp.tool(annotations=LEITURA)
    async def verificar_ncm_aplicavel(c_class_trib: CClassTrib, ncm: Ncm, data: Data) -> Resultado:
        """Verifica se uma NCM pode ser usada com a cClassTrib na data. Use antes de calcular."""
        return await regime(
            "GET",
            "calculadora/dados-abertos/classificacoes-tributarias/ncm-aplicavel",
            params={"cClassTrib": c_class_trib, "ncm": ncm, "dataOcorrenciaFatoGerador": data},
        )

    @mcp.tool(annotations=LEITURA)
    async def verificar_nbs_aplicavel(c_class_trib: CClassTrib, nbs: Nbs, data: Data) -> Resultado:
        """Verifica se uma NBS pode ser usada com a cClassTrib na data. Use antes de calcular."""
        return await regime(
            "GET",
            "calculadora/dados-abertos/classificacoes-tributarias/nbs-aplicavel",
            params={"cClassTrib": c_class_trib, "nbs": nbs, "dataOcorrenciaFatoGerador": data},
        )

    @mcp.tool(annotations=LEITURA)
    async def consultar_fundamentacao_legal(
        data: Data,
        c_class_trib: Annotated[
            str | None,
            Field(pattern=r"^\d{6}$", description="Filtra por cClassTrib; recomendado, a lista completa é longa"),
        ] = None,
        cst: Annotated[str | None, Field(pattern=r"^\d{3}$", description="Filtra por CST")] = None,
    ) -> Resultado:
        """Devolve a fundamentação legal (texto e referência normativa da LC 214/2025) das classificações.

        Use para justificar por que um item tem redução, isenção, diferimento etc.
        """
        resultado = await regime("GET", "calculadora/dados-abertos/fundamentacoes-legais", params={"data": data})
        itens = resultado.get("dados")
        if isinstance(itens, list) and (c_class_trib or cst):
            resultado["dados"] = [
                f
                for f in itens
                if isinstance(f, dict)
                and (not c_class_trib or f.get("codigoClassificacaoTributaria") == c_class_trib)
                and (not cst or f.get("codigoSituacaoTributaria") == cst)
            ]
        return resultado

    @mcp.tool(annotations=LEITURA)
    async def consultar_aliquotas_referencia(
        data: Data,
        codigo_uf: Annotated[int | None, Field(description="Código IBGE da UF (por exemplo 43 para RS)")] = None,
        codigo_municipio: Annotated[int | None, Field(description="Código IBGE do município (7 dígitos)")] = None,
    ) -> Resultado:
        """Consulta as alíquotas de referência da CBS (União) e, se informados, do IBS da UF e do município.

        Use os valores para preencher aliquotasNominais na operação de cálculo. Uma esfera sem
        alíquota cadastrada para a data aparece com "indisponivel" e o motivo; nesse caso, peça a
        alíquota ao usuário em vez de supor um valor.
        """
        dados: dict[str, Any] = {}
        metadados: dict[str, Any] = {}
        consultas: list[tuple[str, str, dict[str, Any]]] = [("uniao", "aliquota-uniao", {})]
        if codigo_uf is not None:
            consultas.append(("uf", "aliquota-uf", {"codigoUf": codigo_uf}))
        if codigo_municipio is not None:
            consultas.append(("municipio", "aliquota-municipio", {"codigoMunicipio": codigo_municipio}))
        falhas = 0
        for chave, recurso, params in consultas:
            try:
                r = await rtc.regime_geral(
                    "GET", f"calculadora/dados-abertos/{recurso}", params={**params, "data": data}
                )
            except CalculadoraError as exc:
                dados[chave] = {"indisponivel": str(exc)}
                falhas += 1
                continue
            dados[chave] = r["dados"]
            metadados.update(r["metadados"])
        if falhas == len(consultas):
            raise ToolError("Nenhuma alíquota de referência encontrada: " + json.dumps(dados, ensure_ascii=False))
        return {"dados": dados, "metadados": metadados}

    @mcp.tool(annotations=LEITURA)
    async def listar_municipios(
        sigla_uf: Annotated[str, Field(pattern=r"^[A-Za-z]{2}$", description="Sigla da UF, por exemplo SP")],
    ) -> Resultado:
        """Lista os municípios de uma UF com seus códigos IBGE (necessários no campo 'municipio')."""
        return await regime("GET", "calculadora/dados-abertos/ufs/municipios", params={"siglaUf": sigla_uf.upper()})

    # ------------------------------------------------------------------
    # Simples Nacional
    # ------------------------------------------------------------------

    @mcp.tool(annotations=CALCULO)
    async def calcular_tributos_simples_nacional(
        operacao: Annotated[dict[str, Any], Field(description="Operação no formato OperacaoSimplesInput")],
    ) -> Resultado:
        """Calcula CBS e IBS de uma operação de empresa do Simples Nacional (NT 2026.005).

        Estrutura da operação (valores decimais como string):
        {
          "id": "identificador", "versao": "2.0.0",
          "data": "2027-03-15",              # data do fato gerador
          "tpDoc": "55",                     # 55 NF-e ou 65 NFC-e
          "CRT": "1",                        # 1 SN, 2 SN com excesso de sublimite, 3 regime normal
          "tpRegimeIBSCBS": null,            # "2" (apuração por fora) só com CRT=3
          "tpEnteGov": null,                 # compra governamental: 1 União, 2 Estado, 3 DF, 4 Município
          "itens": [{
            "numero": 1,
            "cAtivSN": "01",                 # atividade do Simples (listar_atividades_simples_nacional)
            "cClassTrib": "000001",
            "baseCalculo": "950.00",         # sem o valor do IS
            "valorImpostoSeletivo": "0.00",
            "pIBSSN": "1.25", "pCBSSN": "2.75"   # alíquotas efetivas do SN, em pontos percentuais
          }]
        }

        Os erros vêm por item (códigos VSN-xxx) sem interromper os demais.
        """
        return await simples("POST", "calculadora/observabilidade/simples-nacional", json_body=operacao)

    @mcp.tool(annotations=LEITURA)
    async def descrever_erro_simples_nacional(
        codigo: Annotated[str, Field(description="Código do erro, por exemplo VSN-001")],
    ) -> Resultado:
        """Explica um código de erro VSN-xxx do cálculo do Simples Nacional."""
        return await simples("GET", "calculadora/observabilidade/simples-nacional/erros", params={"codigo": codigo})

    @mcp.tool(annotations=LEITURA)
    async def listar_atividades_simples_nacional(
        data: Data,
        anexos: Annotated[
            list[Literal["I", "II", "III", "IV", "V"]] | None,
            Field(description="Filtra pelos anexos da LC 123/2006"),
        ] = None,
        tp_doc: Annotated[str | None, Field(description="Tipo de documento, por exemplo 55 (NF-e)")] = None,
        c_class_trib: Annotated[str | None, Field(pattern=r"^\d{6}$")] = None,
    ) -> Resultado:
        """Lista as atividades do Simples Nacional (cAtivSN) vigentes na data, com filtros opcionais."""
        params: dict[str, Any] = {"data": data, "tpDoc": tp_doc, "cClassTrib": c_class_trib}
        if anexos is not None:
            params["anexos"] = ",".join(anexos)
        return await simples("GET", "calculadora/simples-nacional/atividades", params=params)

    @mcp.tool(annotations=LEITURA)
    async def consultar_atividade_simples_nacional(
        codigo: Annotated[str, Field(pattern=r"^\d{2}$", description="Código cAtivSN, com zero à esquerda")],
        data: Data,
        tp_doc: str | None = None,
        c_class_trib: Annotated[str | None, Field(pattern=r"^\d{6}$")] = None,
    ) -> Resultado:
        """Consulta uma atividade do Simples Nacional pelo código, vigente na data."""
        return await simples(
            "GET",
            f"calculadora/simples-nacional/atividades/{codigo}",
            params={"data": data, "tpDoc": tp_doc, "cClassTrib": c_class_trib},
        )

    # ------------------------------------------------------------------
    # DANFSe (documento auxiliar da NFS-e em PDF)
    # ------------------------------------------------------------------

    @mcp.tool(annotations=GERACAO_ARQUIVO)
    async def gerar_danfse(
        xml: Annotated[
            str | None,
            Field(description="XML completo da NFS-e autorizada (elemento NFSe com infNFSe). Use este ou caminho_xml"),
        ] = None,
        caminho_xml: Annotated[
            str | None,
            Field(description="Caminho de um arquivo .xml com a NFS-e, na máquina do servidor MCP. Use este ou xml"),
        ] = None,
        situacao: Annotated[
            Literal["valida", "substituida", "cancelada"],
            Field(description="Situação da nota; substituída e cancelada recebem marca d'água no PDF"),
        ] = "valida",
        canhoto: Annotated[bool, Field(description="Inclui o canhoto de recebimento")] = True,
        nome_arquivo: Annotated[
            str | None,
            Field(
                pattern=r"^[\w.-]{1,100}$",
                description="Nome do PDF a gravar (sem diretório). Padrão: danfse-<Id da NFS-e>.pdf",
            ),
        ] = None,
        incluir_pdf_base64: Annotated[
            bool,
            Field(
                description=(
                    "Devolve também o PDF em base64. Só use quando o cliente não tem acesso ao disco do "
                    "servidor MCP (transporte HTTP remoto): o conteúdo é grande"
                )
            ),
        ] = False,
    ) -> Resultado:
        """Gera o DANFSe (Documento Auxiliar da NFS-e) em PDF a partir do XML de uma NFS-e.

        O PDF é gravado no diretório configurado em RTC_DANFSE_DIR (padrão ~/rtc-danfse) e a
        ferramenta devolve o caminho do arquivo. Nunca sobrescreve um arquivo existente.
        Para conferir a estrutura do XML antes, use validar_xml_documento com tipo='nfse' e
        subtipo='nota'. Erros de conteúdo (por exemplo, Id da infNFSe vazio) voltam com a
        mensagem do gerador para correção.
        """
        if (xml is None) == (caminho_xml is None):
            raise ToolError("Informe exatamente um entre 'xml' e 'caminho_xml'.")
        if caminho_xml is not None:
            origem = Path(caminho_xml).expanduser()
            if origem.suffix.lower() != ".xml":
                raise ToolError("caminho_xml deve apontar para um arquivo .xml.")
            try:
                xml = origem.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise ToolError(f"Não foi possível ler {origem}: {exc}") from exc

        try:
            r = await rtc.danfse(
                "POST",
                "api/danfse",
                params={"stc": SITUACOES_DANFSE[situacao], "canhoto": "true" if canhoto else "false"},
                conteudo=xml,
                headers={"Content-Type": "application/xml"},
                binario=True,
            )
        except CalculadoraError as exc:
            raise ToolError(str(exc)) from exc

        pdf: bytes = r["dados"]
        if not pdf.startswith(b"%PDF"):
            raise ToolError("O gerador de DANFSe respondeu com sucesso, mas o conteúdo não é um PDF.")

        diretorio = Path(rtc.config.danfse_dir).expanduser()
        try:
            diretorio.mkdir(parents=True, exist_ok=True)
            destino = _gravar_sem_sobrescrever(diretorio, nome_arquivo or _nome_padrao_danfse(xml), pdf)
        except OSError as exc:
            raise ToolError(f"Não foi possível gravar o PDF em {diretorio}: {exc}") from exc

        dados: dict[str, Any] = {
            "arquivo": str(destino),
            "tamanhoBytes": len(pdf),
            "situacao": situacao,
            "canhoto": canhoto,
        }
        if incluir_pdf_base64:
            dados["pdfBase64"] = base64.b64encode(pdf).decode("ascii")
        return {"dados": dados, "metadados": r["metadados"]}

    # ------------------------------------------------------------------
    # Versões
    # ------------------------------------------------------------------

    @mcp.tool(annotations=LEITURA)
    async def consultar_versoes() -> Resultado:
        """Informa a versão das duas calculadoras e se elas e o gerador de DANFSe estão acessíveis.

        Registre essas versões junto de qualquer resultado: as tabelas mudam ao longo da transição.
        """
        versoes: dict[str, Any] = {}
        for nome, chamada, caminho in (
            ("regimeGeral", rtc.regime_geral, "calculadora/dados-abertos/versao"),
            ("simplesNacional", rtc.simples_nacional, "versao"),
        ):
            try:
                versoes[nome] = (await chamada("GET", caminho))["dados"]
            except CalculadoraError as exc:
                versoes[nome] = {"indisponivel": str(exc)}
        try:
            versoes["geradorDanfse"] = (await rtc.danfse("GET", "health/ready"))["dados"]
        except CalculadoraError as exc:
            versoes["geradorDanfse"] = {"indisponivel": str(exc)}
        return {"dados": versoes, "metadados": {}}

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Servidor MCP das calculadoras da Reforma Tributária do Consumo")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="stdio (padrão) para clientes locais como Claude Code/Desktop; streamable-http para acesso remoto",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Endereço do transporte HTTP")
    parser.add_argument("--port", type=int, default=8765, help="Porta do transporte HTTP")
    args = parser.parse_args()

    # O httpx registra cada requisição em INFO; no stdio isso só polui o log do cliente.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    servidor = criar_servidor(RtcClient(Config.from_env()))
    if args.transport == "stdio":
        servidor.run("stdio")
    else:
        _rodar_http(servidor, args.host, args.port)


def _rodar_http(servidor: Any, host: str, port: int) -> None:
    try:
        servidor.run("streamable-http", host=host, port=port)
    except TypeError:  # SDK 1.x: host e porta ficam em settings
        servidor.settings.host = host
        servidor.settings.port = port
        servidor.run("streamable-http")


if __name__ == "__main__":
    main()
