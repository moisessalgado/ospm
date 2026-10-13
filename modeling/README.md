# modeling

Modelagem de vagas de plantio de árvores em vias públicas, por município.

## Método

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

Regras de alocação (parâmetros das Coletâneas Brasileiras de Arborização
Urbana, MMA 2026):

- apenas faces em **setores censitários urbanos** (IBGE, CD_SIT 1–3) voltadas
  para uma via do OSM de classe elegível (`residential` a `primary`);
- **6 m livres em cada esquina** (pontas da face);
- trechos próximos a **obstáculos do OSM** projetados na face ficam sem vaga:
  travessias (3 m), acessos de garagem (2 m), pontos de ônibus (12 m),
  árvores já mapeadas (4 m);
- **canteiros centrais** de pistas duplas do OSM, co-registradas às
  edificações, com margem de pior caso igual à incerteza medida contra o IBGE;
- **espaçamento mínimo entre quaisquer duas vagas igual ao espaçamento da
  variante** (10 m fixo em `espacamento_10m`; 6/8/11 m pelo porte da maior
  das duas vagas em `espacamento_porte`), inclusive entre vagas de calçada e
  de canteiro central;
- duas variantes de espaçamento (critérios do MMA ainda pendentes):
  `espacamento_10m` e `espacamento_porte` (6/8/11 m pelo porte, que vem da
  classe da via).

## Pipeline

```bash
pip install -e ".[dev]"     # a partir de modeling/

python scripts/01_download_osm.py --municipio suzano        # limite, vias e obstáculos (OSM)
python scripts/02_download_buildings.py --municipio suzano  # edificações (Overture Maps)
python scripts/03_allocate_slots.py --municipio suzano      # alocação das vagas
python scripts/04_export_webmap.py --municipio suzano       # dados do webmap
```

As faces de logradouro do IBGE ficam atrás de uma verificação anti-robô e
precisam ser baixadas manualmente: "Base de Faces de Logradouros por
UF/Municípios (shp)" na [página do IBGE](https://www.ibge.gov.br/geociencias/organizacao-do-territorio/malhas-territoriais/28971-base-de-faces-de-logradouros-do-brasil.html),
descompactada em `data/ibge/faces_logradouros_2022/<UF>/`. Os setores
censitários são baixados automaticamente.

## Novo município

Adicione uma entrada em `planting_slots/municipalities.py` com o código IBGE
de 7 dígitos e o EPSG SIRGAS 2000/UTM da longitude do município.

## Estrutura

```
planting_slots/          pacote Python (código em inglês)
  municipalities.py      registro de municípios, região e calçada mínima
  paths.py               nomes de todos os arquivos de dados e saídas
  osm.py  buildings.py  ibge.py  registration.py  allocation.py
scripts/                 etapas do pipeline, na ordem de execução
data/<municipio>/        insumos baixados (fora do git)
data/ibge/               setores censitários e faces de logradouro (fora do git)
output/<municipio>/      vagas_plantio_<variante>.gpkg/.geojson e relatorio_alocacao.json (fora do git)
```

Convenção: código (módulos, funções, variáveis) em inglês; arquivos de dados,
atributos das saídas, opções de linha de comando, logs e documentação em pt-BR.
