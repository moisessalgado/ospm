# modeling

Modelagem de vagas de plantio de árvores em vias públicas, por município, com
cinco métodos de alocação comparáveis entre si (ver `## Métodos` abaixo) —
o webmap deixa alternar entre eles para o mesmo município.

## Métodos

Explicação didática dos algoritmos, com as fórmulas e exemplos numéricos, em
[ALGORITMOS.md](ALGORITMOS.md).

Na ordem do seletor do webmap: `osm_eixo` (padrão do mapa),
`osm_calcada_lado`, `osm_calcada`, `face_ibge` e `osm_overture`. O
`face_ibge` é o mais completo em metadados; os outros existem para comparar
o que cada fonte de dados consegue garantir — ver `ROAD_METHODS` e
`generate_face_ibge` em `scripts/03_allocate_slots.py`.

### face_ibge — método de produção

Cada vaga é **vinculada a uma face de quadra** da Base de Faces de Logradouros
do IBGE (Censo 2022): setor, quadra, face, logradouro e posição ao longo da
face. A contagem e o espaçamento ao longo da face são exatos; a posição
lateral é indicativa.

Motivo: nenhuma fonte disponível em escala nacional localiza o meio-fio com
precisão melhor que ±2–3 m (comparação OSM × IBGE × edificações Overture),
o que é maior que a largura típica de uma calçada. Posicionar o ponto
"dentro da calçada" a partir do eixo da via não é confiável com esses dados.

A linha da face em si também não é confiável sozinha: em muitas quadras ela é
digitalizada rente ao eixo da via, em vez de na divisa do lote — o que
colocaria a vaga dentro do leito carroçável. Por isso a posição lateral de
cada vaga é ajustada, perpendicularmente à via do OSM já co-registrada às
edificações, para nunca ficar mais perto da via do que o leito carroçável
(largura por classe/tag + `CURB_SETBACK_M`) nem mais longe do que a fachada
mais próxima medida na Overture; uma face sem espaço seguro entre os dois
limites não gera vaga ali (`scripts/03_allocate_slots.py`,
`clamp_to_road`).

### osm_eixo, osm_calcada, osm_calcada_lado, osm_overture — comparação

Os métodos de comparação não usam a face do IBGE: caminham diretamente
sobre o eixo da via do OSM.

- `osm_eixo` não desloca o ponto — fica sobre o próprio eixo, de propósito,
  como referência de quanto os outros métodos conseguem melhorar.
- `osm_calcada` desloca o ponto, dos dois lados da via, até a largura de
  calçada estimada pela classe/tag da via (`carriageway_half_width` +
  `CURB_SETBACK_M`) — sem checar prédios.
- `osm_calcada_lado` é o `osm_calcada` em um lado só de cada via: o lado
  cujas faces de quadra do IBGE (paralelas, a até `FACE_STREET_MATCH_M`)
  somam menos endereços (`TOT_GERAL`); se só um lado tem face, esse lado;
  sem face IBGE nenhuma, o lado esquerdo do traçado OSM
  (`choose_side_by_addresses`).
- `osm_overture` faz o mesmo deslocamento, mas usa o OSM já co-registrado às
  edificações e nunca passa da fachada mais próxima (mesma lógica de
  `clamp_to_road`, sem depender da face do IBGE).

Em todos eles, a vaga recebe metadados do IBGE (setor/quadra/face, logradouro
oficial) por proximidade — a face mais perto dentro de `IBGE_MATCH_M`,
só para preencher atributos, nunca para mover o ponto (`enrich_with_ibge`).

### Regras comuns

Parâmetros das Coletâneas Brasileiras de Arborização Urbana, MMA 2026:

- apenas faces em **setores censitários urbanos** (IBGE, CD_SIT 1–3) voltadas
  para uma via do OSM de classe elegível (`residential` a `primary`);
- **6 m livres em cada esquina** (pontas da face; nos métodos que caminham
  sobre a via do OSM, só nas pontas que são cruzamento de fato — trechos OSM
  consecutivos da mesma rua são unidos antes, ver `build_road_chains`);
- trechos próximos a **obstáculos do OSM** projetados na face ficam sem vaga:
  travessias (3 m), acessos de garagem (2 m), pontos de ônibus (12 m),
  árvores já mapeadas (4 m);
- **canteiros centrais** de pistas duplas do OSM, co-registradas às
  edificações, com margem de pior caso igual à incerteza medida contra o IBGE;
- **espaçamento mínimo entre quaisquer duas vagas igual ao espaçamento da
  variante** (10 m fixo em `espacamento_10m`; 6/8/11 m pelo porte da maior
  das duas vagas em `espacamento_porte`), inclusive entre vagas de calçada e
  de canteiro central;
- duas variantes de espaçamento por método (critérios do MMA ainda
  pendentes): `10m` e `porte` (6/8/11 m pelo porte, que vem da classe da
  via) — dez arquivos de saída por município (5 métodos × 2 espaçamentos).

O espaçamento ao longo da via/face é medido em linha reta (corda), não ao
longo da curva, e com os pontos já deslocados para a calçada quando é o caso
(`advance` em `scripts/03_allocate_slots.py`): assim duas vagas consecutivas
nunca ficam abaixo do espaçamento mínimo e não são removidas depois. Uma
zona bloqueada (esquina, obstáculo) só custa as vagas que de fato cobre — a
caminhada retoma logo após o fim dela.

Todo candidato descartado é exportado também, em vez de virar uma lacuna
silenciosa no webmap: `vagas_plantio_<variante>_sem_alocacao.geojson` (um por
variante, quando há candidatos descartados) traz a posição aproximada e o
porte que cada um teria, com o motivo em `properties.motivo` — `esquina`
(afastamento de esquina), o nome do obstáculo do OSM (`travessia`,
`acesso_garagem`, `ponto_onibus`, `arvore_existente`), `sem_espaco_seguro`
(não coube entre o leito carroçável e a fachada mais próxima — ver
`clamp_to_road`/`allocate_from_roads`), `via_transversal` (limpou a própria
via mas caiu dentro do leito carroçável de uma via transversal perto de um
cruzamento — ver `drop_crossing_road_points`, aplicado a todos os métodos
exceto `osm_eixo`, que fica no eixo de propósito) ou `espacamento_minimo`
(a menos do espaçamento da variante de uma vaga de outra rua, quase sempre
perto de uma esquina). Ver `blocked_intervals`/
`allocate_face` para como os outros motivos são atribuídos.

## Pipeline

```bash
pip install -e ".[dev]"     # a partir de modeling/

python scripts/01_download_osm.py --municipio suzano        # limite, vias e obstáculos (OSM)
python scripts/02_download_buildings.py --municipio suzano  # edificações (Overture Maps)
python scripts/03_allocate_slots.py --municipio suzano      # alocação das vagas (5 métodos x 2 espaçamentos)
python scripts/04_export_webmap.py --municipio suzano       # dados do webmap
```

### Cobertura de copa (opcional)

```bash
export GFW_API_KEY=...                                        # chave dos links do shapefile Tropical_Tree_Cover
python scripts/02b_download_canopy.py --municipio suzano      # raster recortado (data/<municipio>/cobertura_copa_gfw_10m.tif)
python scripts/03b_add_canopy_cover.py --municipio suzano     # roda após o 03 e antes do 04

# Santa Catarina: o GFW não tem dado de copa; usar o GLAD (sem chave)
python scripts/02b_download_canopy.py --municipio florianopolis --fonte glad_30m
python scripts/03b_add_canopy_cover.py --municipio florianopolis --fonte glad_30m
```

Fontes (`--fonte`, padrão `gfw_10m`): **GFW Tropical Tree Cover** — 10 m, 2020,
classes de 10 % (0, 20, 30, …, 100); e **GLAD Global Tree Cover** — 30 m, 2010,
percentual contínuo (Landsat, máximo do pico da estação de crescimento). Os
valores **não são comparáveis entre as fontes** (resolução, ano e definição
diferentes), então a fonte fica registrada em `cobertura_copa_fonte` e aparece
no popup do webmap. Com pixel de 30 m e buffer de 3–5,5 m, vagas vizinhas
costumam herdar o mesmo valor.

Cada vaga ganha `cobertura_copa_pct` — o **máximo** da cobertura de copa entre
os pixels de 10 m que tocam um buffer de **metade do espaçamento** da vaga (5 m
no espaçamento de 10 m; 3, 4 e 5,5 m para pequeno/médio/grande no espaçamento
por porte) — e `raio_copa_m`, o raio usado. Nenhuma vaga é removida: é só um
atributo, que o webmap usa para filtrar. Pixel 255 = sem dado, que vira nulo. A etapa 03b reescreve os
`.gpkg`/`.geojson` das variantes; rodar o 03 de novo apaga as colunas, então
rode o 03b depois. Só municípios dentro de um único tile de 10° são suportados no download
(Suzano, Sorocaba, Tremembé e Florianópolis caem no 20S_050W).

Municípios processados: Sorocaba e Suzano (SP), Tremembé (SP), Florianópolis
(SC) — ver `planting_slots/municipalities.py`.

As faces de logradouro do IBGE ficam atrás de uma verificação anti-robô e
precisam ser baixadas manualmente: "Base de Faces de Logradouros por
UF/Municípios (shp)" na [página do IBGE](https://www.ibge.gov.br/geociencias/organizacao-do-territorio/malhas-territoriais/28971-base-de-faces-de-logradouros-do-brasil.html),
descompactada em `data/ibge/faces_logradouros_2022/<UF>/`. Os setores
censitários são baixados automaticamente.

## Novo município

Adicione uma entrada em `planting_slots/municipalities.py` com o código IBGE
de 7 dígitos e o EPSG SIRGAS 2000/UTM da longitude do município. Passo a
passo completo (faces do IBGE, publicação no webmap) no
[README da raiz](../README.md#adicionar-um-novo-município).

## Estrutura

```
planting_slots/          pacote Python (código em inglês)
  municipalities.py      registro de municípios, região e calçada mínima
  paths.py               nomes de todos os arquivos de dados e saídas
  osm.py  buildings.py  ibge.py  registration.py  allocation.py
scripts/                 etapas do pipeline, na ordem de execução
data/<municipio>/        insumos baixados (fora do git)
data/ibge/               setores censitários e faces de logradouro (fora do git)
output/<municipio>/      vagas_plantio_<metodo>_<espacamento>.gpkg/.geojson e relatorio_alocacao.json (fora do git)
```

Convenção: código (módulos, funções, variáveis) em inglês; arquivos de dados,
atributos das saídas, opções de linha de comando, logs e documentação em pt-BR.
