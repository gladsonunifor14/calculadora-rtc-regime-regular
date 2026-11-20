# rtc-mcp: servidor MCP das calculadoras da Reforma Tributária

Servidor [MCP (Model Context Protocol)](https://modelcontextprotocol.io) que expõe as calculadoras oficiais da
Reforma Tributária do Consumo (CBS, IBS e Imposto Seletivo, LC 214/2025) como ferramentas para agentes de IA:

- **Regime regular**: esta aplicação (`calculadora-rtc-regime-regular`, porta 8080);
- **Simples Nacional**: `calculadora-rtc-simples-nacional` (porta 8082);
- **DANFSe**: `calculadora-rtc-gerador-danfse`, que gera o PDF da NFS-e (porta 8083).

O servidor **não calcula tributo**. Ele só repassa as chamadas às APIs. O agente monta a entrada, interpreta a
saída e explica a fundamentação, e os valores vêm sempre da calculadora oficial.

## Ferramentas

| Ferramenta | API | O que faz |
|---|---|---|
| `calcular_tributos_regime_geral` | regime regular | Cálculo de CBS/IBS/IS. Modo `observabilidade` (erros por item) ou `estrito` (devolve o ROC) |
| `descrever_erro_regime_geral` | regime regular | Explica um código `REG-xxx` |
| `calcular_base_calculo_unificada` | regime regular | Base de cálculo unificada da NF-e/NFC-e ou da NFS-e |
| `gerar_xml_documento` | regime regular | Gera o grupo XML de tributação a partir do ROC |
| `validar_xml_documento` | regime regular | Valida grupo ou nota contra o XSD oficial |
| `consultar_ncm` / `consultar_nbs` | regime regular | Descrição da mercadoria ou do serviço |
| `listar_situacoes_tributarias` | regime regular | CSTs com as cClassTrib aninhadas (CBS/IBS ou IS) |
| `consultar_classificacao_tributaria` | regime regular | Detalhe de uma cClassTrib |
| `listar_classificacoes_por_cst` / `listar_classificacoes_por_nbs` | regime regular | cClassTrib candidatas |
| `verificar_ncm_aplicavel` / `verificar_nbs_aplicavel` | regime regular | Confere a combinação NCM/NBS × cClassTrib |
| `consultar_fundamentacao_legal` | regime regular | Texto e referência normativa, com filtro por cClassTrib/CST |
| `consultar_aliquotas_referencia` | regime regular | Alíquotas da União, da UF e do município |
| `listar_municipios` | regime regular | Códigos IBGE de uma UF |
| `calcular_tributos_simples_nacional` | Simples Nacional | Cálculo de CBS/IBS no Simples (NT 2026.005) |
| `descrever_erro_simples_nacional` | Simples Nacional | Explica um código `VSN-xxx` |
| `listar_atividades_simples_nacional` / `consultar_atividade_simples_nacional` | Simples Nacional | Atividades (cAtivSN) vigentes |
| `gerar_danfse` | gerador de DANFSe | Gera o DANFSe (PDF da NFS-e) a partir do XML, com situação e canhoto, e grava o arquivo |
| `consultar_versoes` | todas | Versão das calculadoras e disponibilidade dos três serviços |

Toda resposta tem o formato `{"dados": ..., "metadados": {...}}`. Os `metadados` trazem `versaoAplicacao`,
`versaoBaseDados` e, quando a API sinaliza, `avisoDadosSimulados`, o que permite que o agente cite a origem e a
versão de cada número. Os erros da API (problem+json) voltam ao modelo como erro da ferramenta, com o campo
inválido, para que ele corrija a entrada.

As ferramentas são marcadas como somente leitura: os cálculos são simulações sobre tabelas que não são alteradas.
A exceção é `gerar_danfse`, que grava um arquivo novo no disco e nunca sobrescreve um existente.

## Instalação

Requer Python 3.10+ e os serviços em execução: as calculadoras no perfil `offline` (veja o README de cada
projeto) e, para o DANFSe, o gerador em Go na porta 8083. A porta padrão dele, 8080, conflita com a calculadora
do regime regular:

```bash
cd calculadora-rtc-gerador-danfse
SERVER_PORT=8083 go run ./cmd/danfse
```

```bash
cd mcp-server
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Configuração

| Variável | Padrão | Descrição |
|---|---|---|
| `RTC_REGIME_GERAL_URL` | `http://localhost:8080/api` | URL base da calculadora do regime regular |
| `RTC_SIMPLES_NACIONAL_URL` | `http://localhost:8082/api` | URL base da calculadora do Simples Nacional |
| `RTC_DANFSE_URL` | `http://localhost:8083` | URL base do gerador de DANFSe (sem `/api`) |
| `RTC_DANFSE_DIR` | `~/rtc-danfse` | Diretório onde `gerar_danfse` grava os PDFs |
| `RTC_TIMEOUT` | `30` | Tempo limite das requisições, em segundos |

Os serviços são independentes: se um não estiver em execução, só as ferramentas dele devolvem um erro explicativo.

## Uso

### Claude Code

```bash
claude mcp add rtc -- /caminho/para/mcp-server/.venv/bin/rtc-mcp
```

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "rtc": {
      "command": "/caminho/para/mcp-server/.venv/bin/rtc-mcp",
      "env": {
        "RTC_REGIME_GERAL_URL": "http://localhost:8080/api",
        "RTC_SIMPLES_NACIONAL_URL": "http://localhost:8082/api",
        "RTC_DANFSE_URL": "http://localhost:8083"
      }
    }
  }
}
```

### Transporte HTTP (agentes remotos)

```bash
rtc-mcp --transport streamable-http --host 127.0.0.1 --port 8765
# endpoint MCP: http://127.0.0.1:8765/mcp
```

O transporte HTTP não tem autenticação. Para expor fora da máquina local, coloque-o atrás de um proxy com
autenticação.

### Inspecionar as ferramentas

```bash
npx @modelcontextprotocol/inspector .venv/bin/rtc-mcp
```

## Exemplo de pedido a um agente

> "Vendo 20 kg de arroz (NCM 1006.30.21) em Porto Alegre em março de 2027 por R$ 100. Quanto de CBS e IBS
> incide e por quê?"

Fluxo esperado do agente: `consultar_ncm` → `listar_situacoes_tributarias` → `verificar_ncm_aplicavel` (para a
cClassTrib escolhida) → `consultar_aliquotas_referencia` → `calcular_tributos_regime_geral` →
`consultar_fundamentacao_legal`. A resposta final cita os valores da calculadora, a cClassTrib, o dispositivo
legal e a versão da base.

### DANFSe

> "Gere o DANFSe da NFS-e em ~/notas/nfse-123.xml, que foi cancelada, sem canhoto."

O agente chama `gerar_danfse` com `caminho_xml`, `situacao="cancelada"` e `canhoto=false`. O PDF, com a marca
d'água de cancelada, fica em `~/rtc-danfse/danfse-<Id da NFS-e>.pdf`.

No transporte HTTP remoto, o arquivo é gravado na máquina do servidor MCP. Para que o cliente receba o PDF, use
`incluir_pdf_base64=true`.

## Testes

```bash
pip install -e ".[dev]"
pytest
```

Os testes simulam as APIs com `respx` e não precisam das calculadoras em execução.
