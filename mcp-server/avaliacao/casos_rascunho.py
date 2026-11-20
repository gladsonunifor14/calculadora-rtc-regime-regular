# Rascunho dos casos: (id, tipo, descricao, contexto, ncm_informada, nbs_informada, gabarito)
# gabarito definido: aceitas=[...], cst, ncm/nbs esperada; ambíguo: plausiveis=[...], falta=...
D = "2027-03-10"
C = []
def d(i, desc, ctx, aceitas, cst, ncm=None, nbs=None, ncm_inf=None, nbs_inf=None, tema=""):
    C.append(dict(id=i, tipo="definido", tema=tema, descricao=desc, contexto=ctx, data=D,
                  ncm_informada=ncm_inf, nbs_informada=nbs_inf,
                  gabarito=dict(aceitas=aceitas, cst=cst, ncm=ncm, nbs=nbs)))
def a(i, desc, ctx, plausiveis, falta, ncm=None, nbs=None, ncm_inf=None, nbs_inf=None, tema=""):
    C.append(dict(id=i, tipo="ambiguo", tema=tema, descricao=desc, contexto=ctx, data=D,
                  ncm_informada=ncm_inf, nbs_informada=nbs_inf,
                  gabarito=dict(plausiveis=plausiveis, falta=falta, ncm=ncm, nbs=nbs)))

# --- alimentos ---
d("arroz-varejo", "Arroz branco tipo 1, polido, pacote de 5 kg", "Venda de supermercado a consumidor final", ["200003"], "200", ncm="10063021", tema="alimentos")
d("arroz-parboilizado-ncm", "Arroz parboilizado polido, fardo com 6 pacotes de 1 kg", "Venda de atacadista a supermercado", ["200003"], "200", ncm="10063011", ncm_inf="10063011", tema="alimentos")
d("tomate-ceasa", "Tomate italiano in natura, caixa de 20 kg", "Venda de distribuidor da Ceasa a restaurante", ["200014"], "200", ncm="07020000", tema="alimentos")
d("mexilhao-peixaria", "Mexilhão fresco resfriado, bandeja de 1 kg", "Venda de peixaria a consumidor final", ["200034"], "200", ncm="03073100", tema="alimentos")
# --- higiene ---
d("sabonete-varejo", "Sabonete em barra de 90 g", "Venda de supermercado a consumidor final", ["200035"], "200", ncm="34011190", tema="higiene")
# --- saúde: dispositivos médicos ---
d("ecg-clinica", "Eletrocardiógrafo portátil de 12 derivações", "Venda de distribuidora de equipamentos médicos a clínica cardiológica particular", ["200004"], "200", ncm="90181100", tema="saude")
d("ecg-emergencia", "Eletrocardiógrafo portátil de 12 derivações", "Venda ao Ministério da Saúde durante situação de emergência de saúde pública reconhecida pelo Poder Público, para uso no atendimento da emergência", ["200006", "200004"], "200", ncm="90181100", tema="saude")
d("bolsa-coletora-privado", "Bolsa coletora de urina descartável, de plástico, uso hospitalar", "Venda de distribuidora a hospital particular com fins lucrativos", ["200030"], "200", ncm="39269030", ncm_inf="39269030", tema="saude")
d("bolsa-coletora-publico", "Bolsa coletora de urina descartável, de plástico, uso hospitalar", "Venda de distribuidora a hospital público estadual (órgão da administração pública direta)", ["200005"], "200", ncm="39269030", ncm_inf="39269030", tema="saude")
d("vitamina-e-privado", "Acetato de tocoferol (vitamina E) para composição de nutrição parenteral", "Venda de laboratório a hospital particular", ["200033"], "200", ncm="29362812", ncm_inf="29362812", tema="saude")
d("vitamina-e-publico", "Acetato de tocoferol (vitamina E) para composição de nutrição parenteral", "Venda de laboratório a hospital universitário federal (órgão da administração pública)", ["200011"], "200", ncm="29362812", ncm_inf="29362812", tema="saude")
d("internacao", "Diária de internação hospitalar em enfermaria", "Prestada por hospital privado a paciente particular", ["200029"], "200", nbs="123011100", tema="saude")
d("plano-saude", "Mensalidade de plano de assistência à saúde individual", "Cobrada por operadora registrada na ANS a beneficiário pessoa física", ["011002"], "011", nbs="109101000", tema="saude")
# --- acessibilidade ---
d("barra-apoio", "Barra de apoio em aço inox para banheiro, própria para pessoas com deficiência", "Venda de loja de materiais de construção a consumidor final", ["200007"], "200", ncm="83024100", ncm_inf="83024100", tema="acessibilidade")
d("kit-direcao-pf", "Kit de adaptação veicular com comandos manuais de freio e acelerador, para condução por pessoa com deficiência", "Venda de oficina especializada a pessoa física com deficiência", ["200031"], "200", ncm="87089910", ncm_inf="87089910", tema="acessibilidade")
d("kit-direcao-prefeitura", "Kit de adaptação veicular com comandos manuais de freio e acelerador, para condução por pessoa com deficiência", "Venda à prefeitura municipal (órgão da administração pública) para veículos do transporte adaptado", ["200008"], "200", ncm="87089910", ncm_inf="87089910", tema="acessibilidade")
# --- veículos e combustíveis ---
d("carro-pf", "Automóvel de passeio 1.0 flex, fabricação nacional, zero km", "Venda de concessionária a pessoa física sem nenhuma condição especial", ["000001"], "000", ncm="87032100", tema="veiculos")
d("carro-taxista", "Automóvel de passeio 1.0 flex, fabricação nacional, zero km", "Venda de concessionária a motorista profissional (taxista) que comprovou os requisitos legais para a redução", ["200015"], "200", ncm="87032100", tema="veiculos")
d("gasolina-refinaria", "Gasolina A (sem adição de etanol)", "Venda de refinaria a distribuidora de combustíveis", ["620001"], "620", ncm="27101259", tema="combustiveis")
d("energia-mercado-livre", "Energia elétrica", "Venda de comercializadora a distribuidora de energia elétrica no mercado livre", ["510001"], "510", ncm="27160000", tema="energia")
# --- serviços ---
d("escola-fundamental", "Mensalidade escolar do 5º ano do ensino fundamental", "Escola particular cobrando dos pais do aluno", ["200028"], "200", nbs="122012000", tema="servicos")
d("licenca-filme", "Licenciamento dos direitos de exibição de um longa-metragem brasileiro", "Produtora audiovisual nacional licencia o filme a uma emissora de TV por assinatura", ["200039"], "200", nbs="111031000", tema="servicos")
d("pentest-banco", "Teste de invasão (pentest) e consultoria em segurança cibernética", "Empresa de tecnologia com sócio brasileiro presta o serviço a um banco privado", ["200044"], "200", nbs="115012000", tema="servicos")
d("restaurante", "Prato executivo servido no salão de um restaurante", "Restaurante vendendo a consumidor final", ["200047"], "200", nbs="103011000", tema="servicos")
d("hotel", "Diária de quarto de hotel com café da manhã", "Hotel cobrando de hóspede pessoa física", ["200048"], "200", nbs="103031100", tema="servicos")
d("onibus-urbano", "Passagem de ônibus urbano municipal", "Transporte público coletivo rodoviário de passageiros operado por concessionária municipal", ["400001"], "400", nbs="104011119", tema="servicos")
# --- operações sem incidência / suspensão ---
d("exportacao-direta", "Café cru em grão, sacas de 60 kg", "Venda direta a importador nos Estados Unidos, com embarque para o exterior pelo próprio vendedor", ["410004"], "410", ncm="09011110", tema="operacoes")
d("exportacao-trading", "Café cru em grão, sacas de 60 kg", "Venda a empresa comercial exportadora (trading) com fim específico de exportação; a mercadoria vai direto ao porto", ["550001"], "550", ncm="09011110", tema="operacoes")
d("transferencia-filial", "200 caixas de biscoito recheado", "Transferência da matriz para filial da mesma empresa em outro estado, sem venda", ["410002"], "410", ncm="19053100", tema="operacoes")
d("bonificacao", "5 fardos de refrigerante dados em bonificação", "Bonificação incondicional informada na própria nota fiscal de venda, sem depender de evento posterior", ["410001"], "410", ncm="22021000", tema="operacoes")
d("livro-didatico", "Livro didático impresso de matemática", "Venda de editora a livraria", ["410008"], "410", ncm="49019900", tema="operacoes")

# --- ambíguos: falta um dado que muda a classificação ---
a("adubo-sem-destino", "Adubo orgânico de origem animal, sacos de 50 kg", "Não informado", ["200038", "515001", "200002"], "quem é o adquirente (produtor rural contribuinte ou não) e se a operação está sujeita a diferimento", ncm="31010000", tema="agro")
a("dipirona", "Dipirona sódica 500 mg, caixa com 10 comprimidos, medicamento registrado na Anvisa", "Venda de farmácia a consumidor final", ["200009", "200032"], "se o medicamento consta da lista de medicamentos com alíquota zero ou da redução de 60%", ncm="30049069", tema="saude")
a("bolsa-coletora-sem-comprador", "Bolsa coletora de urina descartável, de plástico, uso hospitalar", "Venda de distribuidora; o comprador não foi informado", ["200030", "200005"], "se o adquirente é órgão da administração pública ou entidade de saúde imune", ncm="39269030", ncm_inf="39269030", tema="saude")
a("kit-direcao-sem-comprador", "Kit de adaptação veicular com comandos manuais de freio e acelerador, para condução por pessoa com deficiência", "Não informado", ["200031", "200008"], "se o adquirente é órgão da administração pública", ncm="87089910", ncm_inf="87089910", tema="acessibilidade")
a("vitamina-e-sem-destino", "Acetato de tocoferol (vitamina E) para composição de nutrição parenteral", "Não informado", ["200033", "200011"], "se o adquirente é órgão da administração pública", ncm="29362812", ncm_inf="29362812", tema="saude")
a("cafe-sem-destino", "Café cru em grão, sacas de 60 kg", "Venda de cooperativa; destino e adquirente não informados", ["200003", "200036", "000001", "410004", "550001", "200054"], "se é venda no mercado interno, exportação direta ou venda a comercial exportadora, e quem é o adquirente", ncm="09011110", tema="operacoes")
a("aluguel-sala", "Aluguel mensal de sala comercial", "Locador pessoa jurídica; regime de apuração do locador não informado", ["200027", "221001"], "se o locador optou pelo regime de alíquota fixa sobre a receita bruta", tema="servicos")
a("pentest-sem-cliente", "Teste de invasão (pentest) e consultoria em segurança cibernética", "Não informado", ["200044", "200043", "000001"], "se o prestador tem sócio brasileiro e se o tomador é a administração pública em atividade de soberania", nbs="115012000", tema="servicos")
a("tomate-produtor", "Tomate in natura, caixas de 20 kg", "Venda feita diretamente por produtor rural pessoa física; não se sabe se ele é contribuinte do IBS/CBS", ["200014", "410014"], "se o produtor rural é contribuinte ou não contribuinte do IBS e da CBS", ncm="07020000", tema="alimentos")
a("carro-motorista-app", "Automóvel de passeio 1.0 flex, fabricação nacional, zero km", "Venda de concessionária; o comprador diz ser motorista de aplicativo, mas não apresentou comprovação dos requisitos legais", ["000001", "200015"], "se o comprador cumpre e comprova os requisitos legais da redução para motoristas profissionais", ncm="87032100", tema="veiculos")
