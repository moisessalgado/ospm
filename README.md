# OpenStreetPlantsMap (OSPM)

Modelagem de **vagas de plantio de árvores em vias públicas** a partir de dados
abertos — OpenStreetMap, IBGE (Censo 2022) e edificações da Overture Maps — e
um webmap para explorar o resultado por município.

- **Mapa:** <http://moisessalgado.com.br/ospm/>
- **Metodologia:** <http://moisessalgado.com.br/ospm/metodologia.html>

Municípios processados: Suzano, Sorocaba e Tremembé (SP) e Florianópolis (SC).

## Como funciona, em uma página

Para cada município, o pipeline:

1. baixa o limite municipal, as vias e os obstáculos do OSM (travessias,
   acessos de garagem, pontos de ônibus, árvores já mapeadas) e as edificações
   da Overture;
2. une os trechos OSM da mesma rua em vias contínuas, identifica as esquinas
   reais e estima a largura do leito carroçável pela classe/tags da via;
3. caminha ao longo de cada via (ou face de quadra do IBGE) colocando uma vaga
   a cada N metros, pulando esquinas (6 m) e obstáculos;
4. descarta candidatos que caem dentro de outra via ou ficam a menos do
   espaçamento mínimo de outra vaga — e exporta esses descartes com o motivo
   ("vagas inviáveis");
5. exporta tudo num formato compacto para o webmap.

São **5 métodos de alocação × 2 espaçamentos = 10 variantes** por município,
para comparar o que cada fonte de dados consegue garantir:

| # | id | Método | Usa |
|---|---|---|---|
| 1 | `osm_eixo` | Eixo da via (padrão do webmap) | OSM |
| 2 | `osm_calcada_lado` | Calçada em um dos lados da via (o lado com menos endereços no IBGE) | OSM + IBGE |
| 3 | `osm_calcada` | Calçada nos dois lados da via | OSM |
| 4 | `face_ibge` | Face de quadra do IBGE, ajustada à via e às fachadas | IBGE + OSM + Overture |
| 5 | `osm_overture` | Calçada ajustada com prédios | OSM + Overture |

Espaçamentos: `10m` (genérico) e `porte` (6/8/11 m pelo porte da árvore, que
vem da classe da via). Detalhes, parâmetros e limitações em
[modeling/README.md](modeling/README.md) e na página de metodologia; a
matemática de cada passo, com exemplos numéricos, em
[modeling/ALGORITMOS.md](modeling/ALGORITMOS.md).

## Estrutura do repositório

```
modeling/                 pipeline em Python (ver modeling/README.md)
  planting_slots/         pacote: registro de municípios, caminhos, OSM, IBGE,
                          edificações, co-registro e regras de alocação
  scripts/01..04_*.py     etapas do pipeline, na ordem de execução
  data/                   insumos baixados (fora do git)
  output/                 saídas completas .gpkg/.geojson (fora do git)
webmap/                   site estático, sem build (ver webmap/README.md)
  index.html, js/app.js   mapa (MapLibre GL)
  metodologia.html        página de metodologia
  data/                   saída do pipeline para o mapa (versionada)
.github/workflows/        deploy do webmap no GitHub Pages
```

## Requisitos

- Python 3.11+
- Acesso à internet para Nominatim, Overpass e Overture (S3 público)

```bash
cd modeling
python -m venv ../.venv && source ../.venv/bin/activate
pip install -e ".[dev]"
playwright install chromium   # opcional — só para testes visuais do webmap
```

## Processar um município já registrado

```bash
cd modeling
python scripts/01_download_osm.py       --municipio suzano   # limite, vias e obstáculos (OSM)
python scripts/02_download_buildings.py --municipio suzano   # edificações (Overture)
python scripts/03_allocate_slots.py     --municipio suzano   # 5 métodos x 2 espaçamentos
python scripts/04_export_webmap.py      --municipio suzano   # webmap/data/suzano/ + manifest.json
```

Os downloads ficam em cache em `modeling/data/<municipio>/`; apague um arquivo
para baixá-lo de novo. O passo 03 também precisa das faces de logradouro do
IBGE do estado (ver abaixo). Ele grava `modeling/output/<municipio>/`:

- `vagas_plantio_<metodo>_<espacamento>.gpkg` / `.geojson` — as vagas, com
  porte, tipo, logradouro, setor/quadra/face do IBGE e posição ao longo da via;
- `vagas_plantio_<variante>_sem_alocacao.geojson` — os candidatos descartados,
  com `motivo`;
- `relatorio_alocacao.json` — contagens por variante, porte e motivo.

Para ver o mapa localmente:

```bash
cd webmap && python3 -m http.server 8000   # http://localhost:8000
```

## Adicionar um novo município

1. **Registro.** Acrescente uma entrada em `MUNICIPALITIES`, em
   [modeling/planting_slots/municipalities.py](modeling/planting_slots/municipalities.py):
   ```python
   "campinas-SP": Municipality(slug="campinas-SP", name="Campinas", state="São Paulo",
                               ibge_code="3509502", epsg="EPSG:31983"),
   ```
   - `slug`: nome sem acento seguido da UF (`campinas-SP`), para não haver
     conflito entre municípios de mesmo nome em estados diferentes (há, por
     exemplo, um Bom Jesus no PI, no RN, no RS e em SC); é usado no
     `--municipio`, nas pastas de dados e no seletor do webmap;
   - `ibge_code`: código IBGE de 7 dígitos (os 2 primeiros definem a UF);
   - `epsg`: SIRGAS 2000 / UTM do fuso da longitude do município —
     `31978` a `31985` para os fusos 18S a 25S (ex.: `31983` = 23S, a
     maior parte de SP/MG/RJ; `31982` = 22S, SC/PR/RS e oeste de SP); confira em <https://epsg.io>;
   - `state` precisa ser o nome por extenso, como em `REGION_BY_STATE` — ele
     define a região e, por ela, a largura mínima de calçada.
2. **Faces de logradouro do IBGE.** Ficam atrás de uma verificação anti-robô e
   precisam ser baixadas à mão: "Base de Faces de Logradouros por
   UF/Municípios (shp)" na
   [página do IBGE](https://www.ibge.gov.br/geociencias/organizacao-do-territorio/malhas-territoriais/28971-base-de-faces-de-logradouros-do-brasil.html).
   Descompacte em `modeling/data/ibge/faces_logradouros_2022/<UF>/`, de modo
   que exista `<codigo_ibge>_faces_de_logradouros_2022.shp` (ver
   `ibge_street_faces` em
   [paths.py](modeling/planting_slots/paths.py)). Os setores censitários são
   baixados automaticamente.
3. **Rode as quatro etapas** como acima, com `--municipio <slug>`.
4. **Publique.** O passo 04 grava `webmap/data/<slug>/` e acrescenta o
   município ao `webmap/data/manifest.json` — o seletor do mapa se popula a
   partir dele, sem mexer em HTML/JS. Faça commit de `webmap/data/` e do
   registro; o push na `master` dispara o deploy.

O webmap abre em Suzano (`DEFAULT_MUNICIPALITY` em
[webmap/js/app.js](webmap/js/app.js)); troque ali se quiser outro município
inicial.

**Tamanho dos dados:** cada município soma de 20 a 160 MB em `webmap/data/`
(Sorocaba é o maior, com arquivos de até 25 MB). O GitHub aceita até 100 MB
por arquivo, mas o repositório cresce a cada reprocessamento — para muitos
municípios, vale considerar Git LFS ou hospedar `webmap/data/` fora do git.

## Estender

### Ajustar parâmetros

As regras ficam como constantes no topo de
[modeling/scripts/03_allocate_slots.py](modeling/scripts/03_allocate_slots.py):

| Constante | O que controla |
|---|---|
| `CORNER_SETBACK_M` | afastamento livre em cada esquina (6 m) |
| `OBSTACLES` | tags OSM de cada obstáculo e seu raio de exclusão |
| `GENERATION_CLASSES` | classes de via OSM que recebem vagas |
| `PORTE_BY_CLASS` | porte da árvore por classe de via |
| `SPACINGS` | variantes de espaçamento (função porte → metros) |
| `CURB_SETBACK_M` | recuo a partir do meio-fio estimado |
| `FACE_STREET_MATCH_M` / `FACE_STREET_MAX_ANGLE_DEG` | pareamento face IBGE ↔ via OSM |

A largura do leito carroçável por classe e o tratamento de pistas duplas ficam
em [planting_slots/osm.py](modeling/planting_slots/osm.py)
(`DEFAULT_WIDTH_BY_CLASS`, `carriageway_half_width`); a largura mínima de
calçada por região em
[municipalities.py](modeling/planting_slots/municipalities.py). Depois de
mudar uma regra, rode de novo os passos 03 e 04 de cada município.

### Novo tipo de obstáculo

1. Inclua a consulta Overpass em `OBSTACLE_QUERY`
   ([01_download_osm.py](modeling/scripts/01_download_osm.py)) e apague
   `modeling/data/<municipio>/obstaculos_osm.json` para rebaixar.
2. Acrescente o obstáculo a `OBSTACLES` em `03_allocate_slots.py` —
   `nome: (filtro de tags, raio em m)`. O `nome` vira o `motivo` das vagas
   inviáveis.
3. Acrescente o mesmo nome ao fim de `LEGEND["motivo"]` em
   [04_export_webmap.py](modeling/scripts/04_export_webmap.py) (a ordem é o
   índice no arquivo compacto — só acrescente no fim).
4. No webmap, dê uma cor ao motivo em `rejected-points`
   (`circle-stroke-color`) e um rótulo em `MOTIVO_LABEL`, em
   [webmap/js/app.js](webmap/js/app.js), e uma linha na legenda de vagas
   inviáveis em [webmap/index.html](webmap/index.html).

### Novo método de alocação

- **Método que caminha sobre a via do OSM:** acrescente uma entrada a
  `ROAD_METHODS` em `03_allocate_slots.py`. As chaves `offset` (desloca para a
  calçada estimada?), `clamp_building` (limita pela fachada e usa o OSM
  co-registrado às edificações?) e `one_side` (só um lado, por
  `choose_side_by_addresses`) já são tratadas por `allocate_from_roads`; um
  comportamento novo entra como uma nova chave ali.
- **Método com lógica própria:** escreva uma função `generate(spacing_fn) ->
  (records, rejected, stats)` como `generate_face_ibge` e registre-a no dict
  `methods` dentro de `main()`. As regras comuns (metadados do IBGE, vias
  transversais, espaçamento mínimo, exportação) são aplicadas depois, para
  todos os métodos.
- Em ambos os casos, acrescente o id e o rótulo a `METHODS` em
  `04_export_webmap.py` — a ordem do dict é a ordem do seletor no mapa — e
  descreva o método em [webmap/metodologia.html](webmap/metodologia.html)
  (seção `#metodos`).

### Webmap

Site estático em HTML/CSS/JS puro com MapLibre GL, sem etapa de build. Ao
editar `css/` ou `js/`, incremente o `?v=N` das tags `<link>`/`<script>` para
furar o cache do navegador. Formato dos dados, camadas e controles em
[webmap/README.md](webmap/README.md).

## Convenções

- Código (módulos, funções, variáveis, comentários) em inglês; nomes de
  arquivos de dados, atributos das saídas, opções de linha de comando, logs e
  documentação em português.
- Sem sufixos de versão em nomes de arquivo ou função (`_v1`, `_v2`): o
  histórico fica no git.
- Mensagens de commit e de pull request em português.

## Fontes de dados e licenças

- **OpenStreetMap** — © colaboradores do OpenStreetMap, licença
  [ODbL](https://www.openstreetmap.org/copyright).
- **Overture Maps** (edificações) — [licenças por tema](https://docs.overturemaps.org/attribution/)
  (ODbL / CDLA Permissive 2.0, conforme a fonte de cada edificação).
- **IBGE** — Censo 2022: setores censitários e Base de Faces de Logradouros.

As saídas derivadas do OSM herdam a ODbL. O código ainda não tem uma licença
definida.
