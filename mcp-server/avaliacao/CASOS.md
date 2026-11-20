# Casos de avaliação do classificador fiscal

41 casos (31 definidos, 10 ambíguos), data do fato gerador 2027-03-10, base de dados da calculadora V0059. Derivados dos pares NCM/NBS → cClassTrib dos testes oficiais (`src/test/resources/entradas`). Cada gabarito foi conferido na calculadora: a cClassTrib existe, pertence ao CST e aceita a NCM/NBS.

**O que revisar:** se o gabarito está certo para a descrição e o contexto. Atenção especial às linhas com *outras classificações do anexo*: a calculadora aceita mais de uma, e a escolha depende de interpretação.

Gerado por `gerar_casos.py`; para mudar um caso, edite `casos_rascunho.py` e gere de novo.

| id | tipo | tema | código informado | gabarito |
|---|---|---|---|---|
| `arroz-varejo` | definido | alimentos | - | 200003 |
| `arroz-parboilizado-ncm` | definido | alimentos | 10063011 | 200003 |
| `tomate-ceasa` | definido | alimentos | - | 200014 |
| `mexilhao-peixaria` | definido | alimentos | - | 200034 |
| `sabonete-varejo` | definido | higiene | - | 200035 |
| `ecg-clinica` | definido | saude | - | 200004 |
| `ecg-emergencia` | definido | saude | - | 200006, 200004 |
| `bolsa-coletora-privado` | definido | saude | 39269030 | 200030 |
| `bolsa-coletora-publico` | definido | saude | 39269030 | 200005 |
| `vitamina-e-privado` | definido | saude | 29362812 | 200033 |
| `vitamina-e-publico` | definido | saude | 29362812 | 200011 |
| `internacao` | definido | saude | - | 200029 |
| `plano-saude` | definido | saude | - | 011002 |
| `barra-apoio` | definido | acessibilidade | 83024100 | 200007 |
| `kit-direcao-pf` | definido | acessibilidade | 87089910 | 200031 |
| `kit-direcao-prefeitura` | definido | acessibilidade | 87089910 | 200008 |
| `carro-pf` | definido | veiculos | - | 000001 |
| `carro-taxista` | definido | veiculos | - | 200015 |
| `gasolina-refinaria` | definido | combustiveis | - | 620001 |
| `energia-mercado-livre` | definido | energia | - | 510001 |
| `escola-fundamental` | definido | servicos | - | 200028 |
| `licenca-filme` | definido | servicos | - | 200039 |
| `pentest-banco` | definido | servicos | - | 200044 |
| `restaurante` | definido | servicos | - | 200047 |
| `hotel` | definido | servicos | - | 200048 |
| `onibus-urbano` | definido | servicos | - | 400001 |
| `exportacao-direta` | definido | operacoes | - | 410004 |
| `exportacao-trading` | definido | operacoes | - | 550001 |
| `transferencia-filial` | definido | operacoes | - | 410002 |
| `bonificacao` | definido | operacoes | - | 410001 |
| `livro-didatico` | definido | operacoes | - | 410008 |
| `adubo-sem-destino` | ambiguo | agro | - | revisão; plausíveis 200038, 515001, 200002 |
| `dipirona` | ambiguo | saude | - | revisão; plausíveis 200009, 200032 |
| `bolsa-coletora-sem-comprador` | ambiguo | saude | 39269030 | revisão; plausíveis 200030, 200005 |
| `kit-direcao-sem-comprador` | ambiguo | acessibilidade | 87089910 | revisão; plausíveis 200031, 200008 |
| `vitamina-e-sem-destino` | ambiguo | saude | 29362812 | revisão; plausíveis 200033, 200011 |
| `cafe-sem-destino` | ambiguo | operacoes | - | revisão; plausíveis 200003, 200036, 000001, 410004, 550001, 200054 |
| `aluguel-sala` | ambiguo | servicos | - | revisão; plausíveis 200027, 221001 |
| `pentest-sem-cliente` | ambiguo | servicos | - | revisão; plausíveis 200044, 200043, 000001 |
| `tomate-produtor` | ambiguo | alimentos | - | revisão; plausíveis 200014, 410014 |
| `carro-motorista-app` | ambiguo | veiculos | - | revisão; plausíveis 000001, 200015 |

## `arroz-varejo` (definido, alimentos)

````text
Item: Arroz branco tipo 1, polido, pacote de 5 kg
Contexto: Venda de supermercado a consumidor final
````
- **Gabarito:** `200003` (CST 200) Vendas de produtos destinados à alimentação humana (Anexo I); NCM 10063021
- **Fundamentação (calculadora):** … III \n\nDA DEVOLUÇÃO PERSONALIZADA DO IBS E DA CBS (CASHBACK) E DA CESTA BÁSICA NACIONAL DE ALIMENTOS \n\nCAPÍTULO II \n\nDA CESTA BÁSICA NACIONAL DE ALIMENTOS
- **Outras classificações cujo anexo inclui a NCM:** `200038` (CST 200) Fornecimento dos insumos agropecuários e aquícolas (Anexo IX); `515001` (CST 515) Operações, sujeitas a diferimento, com insumos agropecuários e aquícolas (Anexo IX)

## `arroz-parboilizado-ncm` (definido, alimentos)

````text
Item: Arroz parboilizado polido, fardo com 6 pacotes de 1 kg
Contexto: Venda de atacadista a supermercado
NCM informada: 10063011
````
- **Gabarito:** `200003` (CST 200) Vendas de produtos destinados à alimentação humana (Anexo I); NCM 10063011
- **Fundamentação (calculadora):** … III \n\nDA DEVOLUÇÃO PERSONALIZADA DO IBS E DA CBS (CASHBACK) E DA CESTA BÁSICA NACIONAL DE ALIMENTOS \n\nCAPÍTULO II \n\nDA CESTA BÁSICA NACIONAL DE ALIMENTOS
- **Outras classificações cujo anexo inclui a NCM:** `200038` (CST 200) Fornecimento dos insumos agropecuários e aquícolas (Anexo IX); `515001` (CST 515) Operações, sujeitas a diferimento, com insumos agropecuários e aquícolas (Anexo IX)

## `tomate-ceasa` (definido, alimentos)

````text
Item: Tomate italiano in natura, caixa de 20 kg
Contexto: Venda de distribuidor da Ceasa a restaurante
````
- **Gabarito:** `200014` (CST 200) Fornecimento dos produtos hortícolas, frutas e ovos (Anexo XV); NCM 07020000
- **Fundamentação (calculadora):** …OS REGIMES DIFERENCIADOS DO IBS E DA CBS\n\nCAPÍTULO IV\n\nDA REDUÇÃO A ZERO DAS ALÍQUOTAS DO IBS E DA CBS\n\nSeção VI\n\nDos Produtos Hortícolas, Frutas e Ovos
- **Outras classificações cujo anexo inclui a NCM:** `200038` (CST 200) Fornecimento dos insumos agropecuários e aquícolas (Anexo IX); `515001` (CST 515) Operações, sujeitas a diferimento, com insumos agropecuários e aquícolas (Anexo IX)

## `mexilhao-peixaria` (definido, alimentos)

````text
Item: Mexilhão fresco resfriado, bandeja de 1 kg
Contexto: Venda de peixaria a consumidor final
````
- **Gabarito:** `200034` (CST 200) Fornecimento dos alimentos destinados ao consumo humano (Anexo VII); NCM 03073100
- **Fundamentação (calculadora):** …DOS DO IBS E DA CBS\n\nCAPÍTULO III\n\nDA REDUÇÃO EM SESSENTA POR CENTO DAS ALÍQUOTAS DO IBS E DA CBS\n\nSeção VII\n\nDos Alimentos Destinados ao Consumo Humano

## `sabonete-varejo` (definido, higiene)

````text
Item: Sabonete em barra de 90 g
Contexto: Venda de supermercado a consumidor final
````
- **Gabarito:** `200035` (CST 200) Fornecimento dos produtos de higiene pessoal e limpeza (Anexo VIII); NCM 34011190
- **Fundamentação (calculadora):** …SENTA POR CENTO DAS ALÍQUOTAS DO IBS E DA CBS\n\nSeção VIII\n\nDos Produtos de Higiene Pessoal e Limpeza Majoritariamente Consumidos por Famílias de Baixa Renda

## `ecg-clinica` (definido, saude)

````text
Item: Eletrocardiógrafo portátil de 12 derivações
Contexto: Venda de distribuidora de equipamentos médicos a clínica cardiológica particular
````
- **Gabarito:** `200004` (CST 200) Fornecimento de dispositivos médicos (Anexo XII); NCM 90181100
- **Fundamentação (calculadora):** …TÍTULO IV\n\nDOS REGIMES DIFERENCIADOS DO IBS E DA CBS\n\nCAPÍTULO IV\n\nDA REDUÇÃO A ZERO DAS ALÍQUOTAS DO IBS E DA CBS\n\nSeção II\n\nDos Dispositivos Médicos

## `ecg-emergencia` (definido, saude)

````text
Item: Eletrocardiógrafo portátil de 12 derivações
Contexto: Venda ao Ministério da Saúde durante situação de emergência de saúde pública reconhecida pelo Poder Público, para uso no atendimento da emergência
````
- **Gabarito:** `200006` (CST 200) Situação de emergência de saúde pública reconhecida pelo Poder público; NCM 90181100
- **Fundamentação (calculadora):** …TÍTULO IV\n\nDOS REGIMES DIFERENCIADOS DO IBS E DA CBS\n\nCAPÍTULO IV\n\nDA REDUÇÃO A ZERO DAS ALÍQUOTAS DO IBS E DA CBS\n\nSeção II\n\nDos Dispositivos Médicos

## `bolsa-coletora-privado` (definido, saude)

````text
Item: Bolsa coletora de urina descartável, de plástico, uso hospitalar
Contexto: Venda de distribuidora a hospital particular com fins lucrativos
NCM informada: 39269030
````
- **Gabarito:** `200030` (CST 200) Venda dos dispositivos médicos (Anexo IV); NCM 39269030
- **Fundamentação (calculadora):** … REGIMES DIFERENCIADOS DO IBS E DA CBS\n\nCAPÍTULO III\n\nDA REDUÇÃO EM SESSENTA POR CENTO DAS ALÍQUOTAS DO IBS E DA CBS\n\nSeção IV\n\nDos Dispositivos Médicos
- **Outras classificações cujo anexo inclui a NCM:** `200005` (CST 200) Fornecimento de dispositivos médicos para órgãos da administração pública e entidades de saúde imunes (Anexo IV)

## `bolsa-coletora-publico` (definido, saude)

````text
Item: Bolsa coletora de urina descartável, de plástico, uso hospitalar
Contexto: Venda de distribuidora a hospital público estadual (órgão da administração pública direta)
NCM informada: 39269030
````
- **Gabarito:** `200005` (CST 200) Fornecimento de dispositivos médicos para órgãos da administração pública e entidades de saúde imunes (Anexo IV); NCM 39269030
- **Fundamentação (calculadora):** …TÍTULO IV\n\nDOS REGIMES DIFERENCIADOS DO IBS E DA CBS\n\nCAPÍTULO IV\n\nDA REDUÇÃO A ZERO DAS ALÍQUOTAS DO IBS E DA CBS\n\nSeção II\n\nDos Dispositivos Médicos
- **Outras classificações cujo anexo inclui a NCM:** `200030` (CST 200) Venda dos dispositivos médicos (Anexo IV)

## `vitamina-e-privado` (definido, saude)

````text
Item: Acetato de tocoferol (vitamina E) para composição de nutrição parenteral
Contexto: Venda de laboratório a hospital particular
NCM informada: 29362812
````
- **Gabarito:** `200033` (CST 200) Fornecimento das composições para nutrição enteral e parenteral (Anexo VI); NCM 29362812
- **Fundamentação (calculadora):** …V\n\nDOS REGIMES DIFERENCIADOS DO IBS E DA CBS\n\nCAPÍTULO III\n\nDA REDUÇÃO EM SESSENTA POR CENTO DAS ALÍQUOTAS DO IBS E DA CBS\n\nSeção VI\n\nDos Medicamentos
- **Outras classificações cujo anexo inclui a NCM:** `200011` (CST 200) Fornecimento das composições para nutrição enteral e parenteral quando adquiridas por órgãos da administração pública (Anexo VI)

## `vitamina-e-publico` (definido, saude)

````text
Item: Acetato de tocoferol (vitamina E) para composição de nutrição parenteral
Contexto: Venda de laboratório a hospital universitário federal (órgão da administração pública)
NCM informada: 29362812
````
- **Gabarito:** `200011` (CST 200) Fornecimento das composições para nutrição enteral e parenteral quando adquiridas por órgãos da administração pública (Anexo VI); NCM 29362812
- **Fundamentação (calculadora):** …BS) \n\nTÍTULO IV\n\nDOS REGIMES DIFERENCIADOS DO IBS E DA CBS\n\nCAPÍTULO IV\n\nDA REDUÇÃO A ZERO DAS ALÍQUOTAS DO IBS E DA CBS\n\nSeção IV\n\nDos Medicamentos
- **Outras classificações cujo anexo inclui a NCM:** `200033` (CST 200) Fornecimento das composições para nutrição enteral e parenteral (Anexo VI)

## `internacao` (definido, saude)

````text
Item: Diária de internação hospitalar em enfermaria
Contexto: Prestada por hospital privado a paciente particular
````
- **Gabarito:** `200029` (CST 200) Fornecimento dos serviços de saúde humana (Anexo III); NBS 123011100
- **Fundamentação (calculadora):** …OS REGIMES DIFERENCIADOS DO IBS E DA CBS\n\nCAPÍTULO III\n\nDA REDUÇÃO EM SESSENTA POR CENTO DAS ALÍQUOTAS DO IBS E DA CBS\n\nSeção III\n\nDos Serviços de Saúde

## `plano-saude` (definido, saude)

````text
Item: Mensalidade de plano de assistência à saúde individual
Contexto: Cobrada por operadora registrada na ANS a beneficiário pessoa física
````
- **Gabarito:** `011002` (CST 011) Planos de assistência à saúde; NBS 109101000
- **Fundamentação (calculadora):** …A CONTRIBUIÇÃO SOCIAL SOBRE BENS E SERVIÇOS (CBS) \n\nTÍTULO V \n\nDOS REGIMES ESPECÍFICOS DO IBS E DA CBS \n\nCAPÍTULO III\n\nDOS PLANOS DE ASSISTÊNCIA À SAÚDE

## `barra-apoio` (definido, acessibilidade)

````text
Item: Barra de apoio em aço inox para banheiro, própria para pessoas com deficiência
Contexto: Venda de loja de materiais de construção a consumidor final
NCM informada: 83024100
````
- **Gabarito:** `200007` (CST 200) Fornecimento dos dispositivos de acessibilidade próprios para pessoas com deficiência (Anexo XIII); NCM 83024100
- **Fundamentação (calculadora):** …A CBS\n\nCAPÍTULO IV\n\nDA REDUÇÃO A ZERO DAS ALÍQUOTAS DO IBS E DA CBS\n\nSeção III\n\nDos Dispositivos de Acessibilidade Próprios para Pessoas com Deficiência

## `kit-direcao-pf` (definido, acessibilidade)

````text
Item: Kit de adaptação veicular com comandos manuais de freio e acelerador, para condução por pessoa com deficiência
Contexto: Venda de oficina especializada a pessoa física com deficiência
NCM informada: 87089910
````
- **Gabarito:** `200031` (CST 200) Fornecimento dos dispositivos de acessibilidade próprios para pessoas com deficiência (Anexo V); NCM 87089910
- **Fundamentação (calculadora):** …ULO III\n\nDA REDUÇÃO EM SESSENTA POR CENTO DAS ALÍQUOTAS DO IBS E DA CBS\n\nSeção V\n\nDos Dispositivos de Acessibilidade Próprios para Pessoas com Deficiência
- **Outras classificações cujo anexo inclui a NCM:** `200008` (CST 200) Fornecimento dos dispositivos de acessibilidade próprios para pessoas com deficiência adquiridos por órgãos da administração pública (Anexo V)

## `kit-direcao-prefeitura` (definido, acessibilidade)

````text
Item: Kit de adaptação veicular com comandos manuais de freio e acelerador, para condução por pessoa com deficiência
Contexto: Venda à prefeitura municipal (órgão da administração pública) para veículos do transporte adaptado
NCM informada: 87089910
````
- **Gabarito:** `200008` (CST 200) Fornecimento dos dispositivos de acessibilidade próprios para pessoas com deficiência adquiridos por órgãos da administração pública (Anexo V); NCM 87089910
- **Fundamentação (calculadora):** … DA CBS\nCAPÍTULO IV\n\nDA REDUÇÃO A ZERO DAS ALÍQUOTAS DO IBS E DA CBS\n\nSeção III\n\nDos Dispositivos de Acessibilidade Próprios para Pessoas com Deficiência
- **Outras classificações cujo anexo inclui a NCM:** `200031` (CST 200) Fornecimento dos dispositivos de acessibilidade próprios para pessoas com deficiência (Anexo V)

## `carro-pf` (definido, veiculos)

````text
Item: Automóvel de passeio 1.0 flex, fabricação nacional, zero km
Contexto: Venda de concessionária a pessoa física sem nenhuma condição especial
````
- **Gabarito:** `000001` (CST 000) Situações tributadas integralmente pelo IBS e CBS.; NCM 87032100
- **Fundamentação (calculadora):** …RVIÇOS (IBS) E DA CONTRIBUIÇÃO SOCIAL SOBRE BENS E SERVIÇOS (CBS) \n\nTÍTULO I \n\nDAS NORMAS GERAIS DO IBS E DA CBS \n\nCAPÍTULO I \n\nDISPOSIÇÕES PRELIMINARES

## `carro-taxista` (definido, veiculos)

````text
Item: Automóvel de passeio 1.0 flex, fabricação nacional, zero km
Contexto: Venda de concessionária a motorista profissional (taxista) que comprovou os requisitos legais para a redução
````
- **Gabarito:** `200015` (CST 200) Venda de automóveis de passageiros de fabricação nacional adquiridos por motoristas profissionais ou pessoas com deficiência; NCM 87032100
- **Fundamentação (calculadora):** …essoas com Deficiência ou com Transtorno do Espectro Autista e por Motoristas Profissionais que Destinem o Automóvel à Utilização na Categoria de Aluguel (Táxi)

## `gasolina-refinaria` (definido, combustiveis)

````text
Item: Gasolina A (sem adição de etanol)
Contexto: Venda de refinaria a distribuidora de combustíveis
````
- **Gabarito:** `620001` (CST 620) Tributação monofásica sobre combustíveis; NCM 27101259
- **Fundamentação (calculadora):** …S ESPECÍFICOS DO IBS E DA CBS\n\nCAPÍTULO I\n\nDOS COMBUSTÍVEIS\n\nSeção I\n\nDisposições Gerais\n\nSeção V\n\nDas Operações com Etanol Anidro Combustível (EAC)

## `energia-mercado-livre` (definido, energia)

````text
Item: Energia elétrica
Contexto: Venda de comercializadora a distribuidora de energia elétrica no mercado livre
````
- **Gabarito:** `510001` (CST 510) Operações, sujeitas a diferimento, com energia elétrica, relativas à importação, geração, comercialização, distribuição e transmissão; NCM 27160000
- **Fundamentação (calculadora):** …n\nTÍTULO I\n\nDAS NORMAS GERAIS DO IBS E DA CBS\n\nCAPÍTULO II\n\nDO IBS E DA CBS SOBRE OPERAÇÕES com bens e serviços\n\nSeção I\n\nDas Hipóteses de Incidência

## `escola-fundamental` (definido, servicos)

````text
Item: Mensalidade escolar do 5º ano do ensino fundamental
Contexto: Escola particular cobrando dos pais do aluno
````
- **Gabarito:** `200028` (CST 200) Fornecimento dos serviços de educação (Anexo II); NBS 122012000
- **Fundamentação (calculadora):** … REGIMES DIFERENCIADOS DO IBS E DA CBS\n\nCAPÍTULO III\n\nDA REDUÇÃO EM SESSENTA POR CENTO DAS ALÍQUOTAS DO IBS E DA CBS\n\nSeção II\n\nDos Serviços de Educação

## `licenca-filme` (definido, servicos)

````text
Item: Licenciamento dos direitos de exibição de um longa-metragem brasileiro
Contexto: Produtora audiovisual nacional licencia o filme a uma emissora de TV por assinatura
````
- **Gabarito:** `200039` (CST 200) Fornecimento dos bens e serviços relacionados com produções nacionais artísticas, culturais, de eventos, jornalísticas e audiovisuais (Anexo X); NBS 111031000
- **Fundamentação (calculadora):** …DUÇÃO EM SESSENTA POR CENTO DAS ALÍQUOTAS DO IBS E DA CBS\n\nSeção XI\n\nDas Produções Nacionais Artísticas, Culturais, de Eventos, Jornalísticas e Audiovisuais

## `pentest-banco` (definido, servicos)

````text
Item: Teste de invasão (pentest) e consultoria em segurança cibernética
Contexto: Empresa de tecnologia com sócio brasileiro presta o serviço a um banco privado
````
- **Gabarito:** `200044` (CST 200) Operações e prestações de serviços de segurança da informação e segurança cibernética desenvolvidos por sociedade que tenha sócio brasileiro (Anexo XI); NBS 115012000
- **Fundamentação (calculadora):** … EM SESSENTA POR CENTO DAS ALÍQUOTAS DO IBS E DA CBS\n\nSeção XIV\n\nDa Soberania e da Segurança Nacional, da Segurança da Informação e da Segurança Cibernética

## `restaurante` (definido, servicos)

````text
Item: Prato executivo servido no salão de um restaurante
Contexto: Restaurante vendendo a consumidor final
````
- **Gabarito:** `200047` (CST 200) Bares e Restaurantes; NBS 103011000
- **Fundamentação (calculadora):** …ESTAURANTES, HOTELARIA, PARQUES DE DIVERSÃO E PARQUES TEMÁTICOS, TRANSPORTE COLETIVO DE PASSAGEIROS E AGÊNCIAS DE TURISMO\n\nSeção I\n\nDos Bares e Restaurantes

## `hotel` (definido, servicos)

````text
Item: Diária de quarto de hotel com café da manhã
Contexto: Hotel cobrando de hóspede pessoa física
````
- **Gabarito:** `200048` (CST 200) Hotelaria, Parques de Diversão e Parques Temáticos; NBS 103031100
- **Fundamentação (calculadora):** …S DE DIVERSÃO E PARQUES TEMÁTICOS, TRANSPORTE COLETIVO DE PASSAGEIROS E AGÊNCIAS DE TURISMO\n\nSeção II\n\nDa Hotelaria, Parques de Diversão e Parques Temáticos

## `onibus-urbano` (definido, servicos)

````text
Item: Passagem de ônibus urbano municipal
Contexto: Transporte público coletivo rodoviário de passageiros operado por concessionária municipal
````
- **Gabarito:** `400001` (CST 400) Fornecimento de serviços de transporte público coletivo de passageiros rodoviário e metroviário; NBS 104011119
- **Fundamentação (calculadora):** …(CBS) \n\nTÍTULO I \n\nDAS NORMAS GERAIS DO IBS E DA CBS \n\nCAPÍTULO II\n\nDO IBS E DA CBS SOBRE OPERAÇÕES COM BENS E SERVIÇOS\n\nSeção V\n\nDa Base de Cálculo

## `exportacao-direta` (definido, operacoes)

````text
Item: Café cru em grão, sacas de 60 kg
Contexto: Venda direta a importador nos Estados Unidos, com embarque para o exterior pelo próprio vendedor
````
- **Gabarito:** `410004` (CST 410) Exportações de bens e serviços; NCM 09011110
- **Fundamentação (calculadora):** …VIÇOS (CBS)\n\nTÍTULO I\n\nDAS NORMAS GERAIS DO IBS E DA CBS\n\nCAPÍTULO II\n\nDO IBS E DA CBS SOBRE OPERAÇÕES COM BENS E SERVIÇOS\n\nSeção II\n\nDas Imunidades
- **Outras classificações cujo anexo inclui a NCM:** `200003` (CST 200) Vendas de produtos destinados à alimentação humana (Anexo I)

## `exportacao-trading` (definido, operacoes)

````text
Item: Café cru em grão, sacas de 60 kg
Contexto: Venda a empresa comercial exportadora (trading) com fim específico de exportação; a mercadoria vai direto ao porto
````
- **Gabarito:** `550001` (CST 550) Exportações de bens materiais; NCM 09011110
- **Fundamentação (calculadora):** …ERENCIADOS DO IBS E DA CBS\n\nCAPÍTULO III\n\nDA REDUÇÃO EM SESSENTA POR CENTO DAS ALÍQUOTAS DO IBS E DA CBS\n\nSeção X\n\nDos Insumos Agropecuários e Aquícolas
- **Outras classificações cujo anexo inclui a NCM:** `200003` (CST 200) Vendas de produtos destinados à alimentação humana (Anexo I)

## `transferencia-filial` (definido, operacoes)

````text
Item: 200 caixas de biscoito recheado
Contexto: Transferência da matriz para filial da mesma empresa em outro estado, sem venda
````
- **Gabarito:** `410002` (CST 410) Transferências entre estabelecimentos pertencentes ao mesmo contribuinte; NCM 19053100
- **Fundamentação (calculadora):** …n\nTÍTULO I\n\nDAS NORMAS GERAIS DO IBS E DA CBS\n\nCAPÍTULO II\n\nDO IBS E DA CBS SOBRE OPERAÇÕES COM BENS E SERVIÇOS\n\nSeção I\n\nDas Hipóteses de Incidência

## `bonificacao` (definido, operacoes)

````text
Item: 5 fardos de refrigerante dados em bonificação
Contexto: Bonificação incondicional informada na própria nota fiscal de venda, sem depender de evento posterior
````
- **Gabarito:** `410001` (CST 410) Fornecimento de bonificações quando constem no documento fiscal e que não dependam de evento posterior; NCM 22021000
- **Fundamentação (calculadora):** …ERENCIADOS DO IBS E DA CBS\n\nCAPÍTULO V\n\nDO TRANSPORTE PÚBLICO COLETIVO DE PASSAGEIROS RODOVIÁRIO E METROVIÁRIO DE CARÁTER URBANO, SEMIURBANO E METROPOLITANO

## `livro-didatico` (definido, operacoes)

````text
Item: Livro didático impresso de matemática
Contexto: Venda de editora a livraria
````
- **Gabarito:** `410008` (CST 410) Fornecimentos de livros, jornais, periódicos e do papel destinado a sua impressão; NCM 49019900
- **Fundamentação (calculadora):** …VIÇOS (CBS)\n\nTÍTULO I\n\nDAS NORMAS GERAIS DO IBS E DA CBS\n\nCAPÍTULO II\n\nDO IBS E DA CBS SOBRE OPERAÇÕES COM BENS E SERVIÇOS\n\nSeção II\n\nDas Imunidades

## `adubo-sem-destino` (ambiguo, agro)

````text
Item: Adubo orgânico de origem animal, sacos de 50 kg
Contexto: Não informado
````
- **Esperado:** pedir informação / revisão. Falta saber quem é o adquirente (produtor rural contribuinte ou não) e se a operação está sujeita a diferimento.
- **Plausíveis:** `200038` (CST 200) Fornecimento dos insumos agropecuários e aquícolas (Anexo IX); `515001` (CST 515) Operações, sujeitas a diferimento, com insumos agropecuários e aquícolas (Anexo IX); `200002` (CST 200) Fornecimento ou importação para produtor rural não contribuinte ou TAC; NCM 31010000

## `dipirona` (ambiguo, saude)

````text
Item: Dipirona sódica 500 mg, caixa com 10 comprimidos, medicamento registrado na Anvisa
Contexto: Venda de farmácia a consumidor final
````
- **Esperado:** pedir informação / revisão. Falta saber se o medicamento consta da lista de medicamentos com alíquota zero ou da redução de 60%.
- **Plausíveis:** `200009` (CST 200) Fornecimento dos medicamentos registrados na Anvisa; `200032` (CST 200) Fornecimento dos medicamentos registrados na Anvisa ou produzidos por farmácias de manipulação, ressalvados os medicamentos sujeitos à alíquota zero; NCM 30049069
- **Outras classificações cujo anexo inclui a NCM:** `200038` (CST 200) Fornecimento dos insumos agropecuários e aquícolas (Anexo IX); `515001` (CST 515) Operações, sujeitas a diferimento, com insumos agropecuários e aquícolas (Anexo IX)

## `bolsa-coletora-sem-comprador` (ambiguo, saude)

````text
Item: Bolsa coletora de urina descartável, de plástico, uso hospitalar
Contexto: Venda de distribuidora; o comprador não foi informado
NCM informada: 39269030
````
- **Esperado:** pedir informação / revisão. Falta saber se o adquirente é órgão da administração pública ou entidade de saúde imune.
- **Plausíveis:** `200030` (CST 200) Venda dos dispositivos médicos (Anexo IV); `200005` (CST 200) Fornecimento de dispositivos médicos para órgãos da administração pública e entidades de saúde imunes (Anexo IV); NCM 39269030

## `kit-direcao-sem-comprador` (ambiguo, acessibilidade)

````text
Item: Kit de adaptação veicular com comandos manuais de freio e acelerador, para condução por pessoa com deficiência
Contexto: Não informado
NCM informada: 87089910
````
- **Esperado:** pedir informação / revisão. Falta saber se o adquirente é órgão da administração pública.
- **Plausíveis:** `200031` (CST 200) Fornecimento dos dispositivos de acessibilidade próprios para pessoas com deficiência (Anexo V); `200008` (CST 200) Fornecimento dos dispositivos de acessibilidade próprios para pessoas com deficiência adquiridos por órgãos da administração pública (Anexo V); NCM 87089910

## `vitamina-e-sem-destino` (ambiguo, saude)

````text
Item: Acetato de tocoferol (vitamina E) para composição de nutrição parenteral
Contexto: Não informado
NCM informada: 29362812
````
- **Esperado:** pedir informação / revisão. Falta saber se o adquirente é órgão da administração pública.
- **Plausíveis:** `200033` (CST 200) Fornecimento das composições para nutrição enteral e parenteral (Anexo VI); `200011` (CST 200) Fornecimento das composições para nutrição enteral e parenteral quando adquiridas por órgãos da administração pública (Anexo VI); NCM 29362812

## `cafe-sem-destino` (ambiguo, operacoes)

````text
Item: Café cru em grão, sacas de 60 kg
Contexto: Venda de cooperativa; destino e adquirente não informados
````
- **Esperado:** pedir informação / revisão. Falta saber se é venda no mercado interno, exportação direta ou venda a comercial exportadora, e quem é o adquirente.
- **Plausíveis:** `200003` (CST 200) Vendas de produtos destinados à alimentação humana (Anexo I); `200036` (CST 200) Fornecimento de produtos agropecuários, aquícolas, pesqueiros, florestais e extrativistas vegetais in natura; `000001` (CST 000) Situações tributadas integralmente pelo IBS e CBS.; `410004` (CST 410) Exportações de bens e serviços; `550001` (CST 550) Exportações de bens materiais; `200054` (CST 200) Fornecimento de bem material pela cooperativa de produção agropecuária a associado não sujeito ao regime regular do IBS e da CBS; NCM 09011110

## `aluguel-sala` (ambiguo, servicos)

````text
Item: Aluguel mensal de sala comercial
Contexto: Locador pessoa jurídica; regime de apuração do locador não informado
````
- **Esperado:** pedir informação / revisão. Falta saber se o locador optou pelo regime de alíquota fixa sobre a receita bruta.
- **Plausíveis:** `200027` (CST 200) Operações de locação, cessão onerosa e arrendamento de bens imóveis; `221001` (CST 221) Locação, cessão onerosa ou arrendamento de bem imóvel com alíquota sobre a receita bruta; sem código de nomenclatura no gabarito

## `pentest-sem-cliente` (ambiguo, servicos)

````text
Item: Teste de invasão (pentest) e consultoria em segurança cibernética
Contexto: Não informado
````
- **Esperado:** pedir informação / revisão. Falta saber se o prestador tem sócio brasileiro e se o tomador é a administração pública em atividade de soberania.
- **Plausíveis:** `200044` (CST 200) Operações e prestações de serviços de segurança da informação e segurança cibernética desenvolvidos por sociedade que tenha sócio brasileiro (Anexo XI); `200043` (CST 200) Fornecimento à administração pública dos serviços e dos bens relativos à soberania (Anexo XI); `000001` (CST 000) Situações tributadas integralmente pelo IBS e CBS.; NBS 115012000

## `tomate-produtor` (ambiguo, alimentos)

````text
Item: Tomate in natura, caixas de 20 kg
Contexto: Venda feita diretamente por produtor rural pessoa física; não se sabe se ele é contribuinte do IBS/CBS
````
- **Esperado:** pedir informação / revisão. Falta saber se o produtor rural é contribuinte ou não contribuinte do IBS e da CBS.
- **Plausíveis:** `200014` (CST 200) Fornecimento dos produtos hortícolas, frutas e ovos (Anexo XV); `410014` (CST 410) Fornecimento de produtor rural não contribuinte; NCM 07020000
- **Outras classificações cujo anexo inclui a NCM:** `200038` (CST 200) Fornecimento dos insumos agropecuários e aquícolas (Anexo IX); `515001` (CST 515) Operações, sujeitas a diferimento, com insumos agropecuários e aquícolas (Anexo IX)

## `carro-motorista-app` (ambiguo, veiculos)

````text
Item: Automóvel de passeio 1.0 flex, fabricação nacional, zero km
Contexto: Venda de concessionária; o comprador diz ser motorista de aplicativo, mas não apresentou comprovação dos requisitos legais
````
- **Esperado:** pedir informação / revisão. Falta saber se o comprador cumpre e comprova os requisitos legais da redução para motoristas profissionais.
- **Plausíveis:** `000001` (CST 000) Situações tributadas integralmente pelo IBS e CBS.; `200015` (CST 200) Venda de automóveis de passageiros de fabricação nacional adquiridos por motoristas profissionais ou pessoas com deficiência; NCM 87032100
