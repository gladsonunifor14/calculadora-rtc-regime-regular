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
| `listar_classificacoes_aplicaveis_ncm` | regime regular | Separa as cClassTrib cujo anexo inclui a NCM das que não restringem NCM |
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

Fluxo esperado do agente: `consultar_ncm` → `listar_classificacoes_aplicaveis_ncm` → `verificar_ncm_aplicavel`
(para a cClassTrib escolhida) → `consultar_aliquotas_referencia` → `calcular_tributos_regime_geral` →
`consultar_fundamentacao_legal`. A resposta final cita os valores da calculadora, a cClassTrib, o dispositivo
legal e a versão da base.

### DANFSe

> "Gere o DANFSe da NFS-e em ~/notas/nfse-123.xml, que foi cancelada, sem canhoto."

O agente chama `gerar_danfse` com `caminho_xml`, `situacao="cancelada"` e `canhoto=false`. O PDF, com a marca
d'água de cancelada, fica em `~/rtc-danfse/danfse-<Id da NFS-e>.pdf`.

No transporte HTTP remoto, o arquivo é gravado na máquina do servidor MCP. Para que o cliente receba o PDF, use
`incluir_pdf_base64=true`.

## Agente classificador fiscal

`rtc-classificador` recebe a descrição de um item e o contexto da operação e propõe NCM/NBS, CST e cClassTrib
de CBS/IBS, com a fundamentação legal. Ele usa o Claude (Claude Opus 5.5, pela API da Anthropic) com as
ferramentas de consulta deste servidor MCP, iniciado automaticamente como subprocesso.

```bash
pip install -e ".[agente]"
export ANTHROPIC_API_KEY=...        # ou: ant auth login
rtc-classificador "arroz branco polido, pacote 5 kg" --data 2027-03-10 \
  --contexto "venda de supermercado a consumidor final"
```

Opções: `--ncm`/`--nbs` (quando o código já é conhecido), `--contexto` (adquirente, finalidade, regime,
destino), `--esforco` (`low` a `max`, padrão `high`), `--json` (saída estruturada) e `--silencioso` (oculta as
ferramentas chamadas). Também pode ser usado em código: `await classificar(descricao, data=..., contexto=...)`.

Como ele chega à classificação:

1. Confirma ou propõe a NCM/NBS e compara a descrição oficial com o item.
2. Levanta as candidatas. Para mercadorias, `listar_classificacoes_aplicaveis_ncm` separa as cClassTrib cujo
   anexo inclui a NCM (por exemplo, a cesta básica) das que dependem só do contexto da operação.
3. Escolhe considerando o contexto. Condições não informadas viram premissas ou perguntas, nunca suposições.
4. Busca o dispositivo legal e devolve a resposta num esquema JSON fixo (structured outputs).

**Conferência independente do modelo:** depois da resposta, o próprio código confere na calculadora se a
cClassTrib existe, se pertence ao CST e se aceita a NCM/NBS proposta. Uma falha nessa conferência, confiança
abaixo de "alta" ou falta de informação marcam o resultado com `revisaoHumanaRecomendada`, e a CLI sai com
código 2. Uma classificação proposta por IA é um apoio à decisão; a responsabilidade fiscal continua com o
contribuinte.

Exemplo de saída (ilustrativo, no formato da CLI):

```text
Item: Arroz branco polido, pacote de 5 kg
Status: classificado  |  Confiança: alta
NCM: 10063021  NBS: -
CST: 200  cClassTrib: 200003  Vendas de produtos destinados à alimentação humana (Anexo I)
Tratamento: Alíquota zero
Redução: CBS 100.0%  IBS UF 100.0%  IBS Mun 100.0%
Fundamentação: LC 214/2025, art. 125: "Ficam reduzidas a zero as alíquotas do IBS e da CBS ..."
...
Conferência na calculadora:
  - CST, cClassTrib e NCM/NBS confirmados na calculadora.
Classificação confirmada.
```

Detalhes técnicos: tool runner do SDK `anthropic` (que usa message threads), pensamento adaptativo, cache de
prompt e fallback automático do servidor em caso de recusa indevida (`fallbacks: "default"`). Para ver o log do
servidor MCP, defina `RTC_DEBUG=1`.

## Testes

```bash
pip install -e ".[dev]"
pytest
```

Os testes do servidor simulam as APIs com `respx` e não precisam das calculadoras em execução. Os testes de
integração do classificador usam um servidor local que imita a API da Anthropic (sem chave), o `rtc-mcp` real e
a calculadora do regime regular; são pulados quando ela não está acessível em `RTC_REGIME_GERAL_URL`.

## Avaliação do classificador

A pasta `avaliacao/` mede a qualidade do `rtc-classificador` em 41 casos (31 com resposta definida e 10 em que
falta informação e o certo é pedir revisão). Os casos derivam dos pares NCM/NBS → cClassTrib dos testes oficiais
e cada gabarito é conferido na calculadora; a lista para revisão está em [`avaliacao/CASOS.md`](avaliacao/CASOS.md).

```bash
pip install -e ".[agente]"
python avaliacao/avaliar.py --aprovar-harness            # registra o harness aprovado
python avaliacao/avaliar.py --casos arroz-varejo,dipirona,hotel --reps 1   # piloto
python avaliacao/avaliar.py --reps 2                     # rodada completa (retomável)
```

A nota é programática, sem juiz de IA. Métrica principal: `acerto` (cClassTrib dentro do gabarito nos casos
definidos; nos ambíguos, sinalizar revisão e propor uma classificação plausível). Também são medidos CST,
NCM/NBS, revisão indevida, conferência na calculadora, recusas, tokens, custo e latência. Os resultados vão para
`.claude/hillclimb/classificador/<variante>/` na raiz do repositório. Para mudar os casos, edite
`avaliacao/casos_rascunho.py` e rode `python avaliacao/gerar_casos.py` com a calculadora em execução.
