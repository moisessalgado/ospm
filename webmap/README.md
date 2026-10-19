# webmap

Site estático (sem build) com duas páginas:

- `index.html` — o mapa interativo, que apresenta os pontos de vagas de
  plantio gerados pelo módulo `modeling/` sobre um basemap do OpenStreetMap.
- `metodologia.html` — apresentação em formato de scrollytelling da
  metodologia de alocação, fontes de dados e limites de precisão, com os
  números do resultado carregados ao vivo de `data/manifest.json`.

`css/base.css` reúne os tokens visuais (cores, tipografia, nav, botões,
utilitário `.reveal` para animações de entrada) compartilhados pelas duas
páginas; `css/style.css` e `css/metodologia.css` têm os estilos específicos
de cada uma.

## Mapa (`index.html`)

- Basemap: abre no OpenStreetMap (tiles raster padrão) por padrão; também
  disponíveis tiles do Google Maps via endpoint XYZ não-oficial
  (`mt0-3.google.com/vt/...`), sem necessidade de API key — risco: não é um
  endpoint suportado oficialmente pelo Google, podendo mudar ou ser bloqueado
  sem aviso.
- Clustering: até o zoom 14 os pontos são agregados em bolhas com contagem
  (clique para aproximar); a partir do 15 aparecem os pontos individuais.
- Clique num ponto individual para ver seus detalhes (id, logradouro, porte,
  tipo, setor/quadra/face do IBGE e posição ao longo da via/face), num popup
  do MapLibre (`showSlotPopup` em `js/app.js`).
- Dois seletores — **Método de alocação** e **Espaçamento** — escolhem a
  variante: 5 métodos x 2 espaçamentos, na ordem do seletor — `osm_eixo`
  (padrão), `osm_calcada_lado` (um lado da via), `osm_calcada` (dois lados),
  `face_ibge` e `osm_overture`; ver `modeling/README.md`. A ordem e os rótulos
  vêm de `METHODS` em `modeling/scripts/04_export_webmap.py` (via manifesto).
  O webmap abre em Suzano/`osm_eixo` (`DEFAULT_MUNICIPALITY`/`DEFAULT_METHOD`
  em `js/app.js`, sem animação de voo — o mapa já nasce centrado ali).
- Dois conjuntos de pontos, cada um com um checkbox próprio na legenda para
  ligar/desligar a camada (`layer-slots-toggle`/`layer-rejected-toggle` em
  `js/app.js`). As vagas inviáveis abrem desligadas e com a legenda recolhida;
  ao ligar o checkbox a camada e a legenda aparecem. A contagem "N vagas de
  plantio · M inviáveis" fica no topo do painel (`#status`):
  - **Vagas de plantio** (cor por porte — pequeno/médio/grande).
  - **Vagas inviáveis**: candidatos que o algoritmo descartou, na mesma cor
    de porte (é o porte que a vaga teria), mas com um contorno colorido
    indicando o motivo — afastamento de esquina, dentro de uma via
    transversal (vaga empurrada para dentro de outra rua perto de um
    cruzamento — ver `drop_crossing_road_points` em
    `modeling/scripts/03_allocate_slots.py`), um obstáculo do OSM
    (travessia, acesso de garagem, ponto de ônibus, árvore existente),
    falta de espaço seguro até a fachada mais próxima ou espaçamento mínimo
    em relação a uma vaga de outra rua (perto de esquinas); clique para o motivo
    por extenso. Só aparecem a partir do zoom 15 (ver `rejected-points` em
    `js/app.js`) — em zoom de cidade seriam um borrão cobrindo o mapa.
  Ambos vêm de `data/<municipio>/<variante>.json` (`points`/`rejeitados`),
  gerado por `modeling/scripts/04_export_webmap.py`. Textos explicativos da
  legenda (o que é porte, o que são vagas inviáveis) ficam em tooltips
  (`title`/`ⓘ`) para manter o painel enxuto, não em parágrafos fixos.
- Medição de distância: botão "Medir" — cada clique adiciona um vértice, a
  linha segue o cursor mostrando a distância ao vivo; duplo clique, Enter ou
  Esc finalizam a linha (Backspace desfaz o último vértice). Com "Snap" ligado,
  o cursor gruda em qualquer ponto de vaga sob ele (zoom 15+).
- Vagas de plantio: carregadas de `data/<municipio>/<variante>.json`, gerado
  por `modeling/scripts/04_export_webmap.py`, onde `<variante>` é
  `<metodo>_<espacamento>` (ex.: `face_ibge_10m`). Não é GeoJSON — é um array
  plano `[lon, lat, porte, tipo_vaga, logradouro, face, posicao_face_m, ...]`
  (7 números por vaga, ou 8 quando o município tem cobertura de copa: o 8º é
  `cobertura_copa_pct`, -1 = sem dado; o JSON traz `stride` com o valor) em que
  os atributos categóricos são índices da tabela
  `legend` (`face` indexa `legend.face`, uma tripla `[cd_setor, cd_quadra,
  cd_face]`), o que reduz bastante o tamanho do arquivo sem perder nenhuma
  vaga nem atributo. O mesmo arquivo traz `rejeitados`: `[lon, lat, porte,
  motivo, ...]` (4 números por ponto) para as vagas inviáveis, `motivo`
  indexando `legend.motivo`. `js/app.js` reconstrói o GeoJSON no navegador
  antes de passar para o MapLibre. `data/manifest.json` lista, por município,
  cada variante com seu `method`/`method_label` e `spacing`/`spacing_label`,
  que os dois seletores usam para se popular.
- Cobertura de copa: nos municípios com o atributo (todos os quatro; Florianópolis com o GLAD de 30 m), o popup da
  vaga mostra `cobertura_copa_pct` e o painel ganha o filtro "Cobertura de copa
  máxima", que oculta as vagas acima do valor (vagas sem dado ficam). É só
  filtro de exibição — o conjunto de vagas não muda.
- Raster de cobertura de copa: a camada "Cobertura de copa (raster)" desenha
  `data/<municipio>/copa.png` (PNG com paleta, em Web Mercator, ~0,2–1,1 MB por
  município) sob as vagas, com rampa verde e legenda. O PNG só é baixado ao
  ligar a camada (desligada por padrão); 0 % e sem dado ficam transparentes. Os
  cantos, a fonte, o crédito e a rampa vêm de `canopy` no `manifest.json`. O
  raster não é lido direto dos provedores (GLAD e GFW não liberam CORS, e o GFW
  exige chave e usa URL assinada que expira): o recorte baixado pelo pipeline é
  que vira imagem estática.
- A posição lateral das vagas é indicativa (linha da face de quadra do IBGE);
  ver `modeling/README.md`.
- Em telas estreitas (≤560px) o painel de controles inicia recolhido; o botão
  "Controles" alterna a exibição (`js/app.js`).

## Metodologia (`metodologia.html`)

- As seções revelam ao rolar via `IntersectionObserver` (classe `.reveal`,
  alternada para `.in-view`); respeita `prefers-reduced-motion`.
- Os diagramas são SVG inline (sem imagens externas), com animações próprias
  (`.pop`, `.fade`, `.draw-path`) definidas em `css/metodologia.css`.
- As imagens de exemplo ficam em `img/` (JPEG 800 px): capturas do próprio
  webmap no mesmo trecho de Suzano, uma por método, e recortes das análises de
  QA sobre satélite. Os diagramas de "Regras comuns" e "Métodos em detalhe"
  têm coordenadas calculadas (curva, caminhada) a partir dos mesmos números de
  `modeling/ALGORITMOS.md`. Ao mudar o algoritmo, refaça as capturas.
- O bloco "Resultado" não tem números fixos no HTML: `js/metodologia.js` busca
  `data/manifest.json` em tempo de execução e monta os cartões e o gráfico de
  barras a partir dele, então atualiza sozinho a cada novo município exportado.

## Testar localmente

```bash
cd webmap
python3 -m http.server 8000
# abrir http://localhost:8000
```

## Publicar no GitHub Pages

O workflow `.github/workflows/deploy-webmap.yml` publica esta pasta a cada
push na branch `master` que toque `webmap/**`. Para ativar (uma vez, nas
configurações do repositório no GitHub): **Settings → Pages → Source →
GitHub Actions**.

O CSS/JS é servido com `cache-control: max-age=14400` (4h); os `<link>` e
`<script>` locais em `index.html`/`metodologia.html` têm um `?v=N` no final
da URL para forçar o navegador a buscar a versão nova assim que o deploy sai
— lembre de incrementar esse `N` ao editar `css/` ou `js/`. Os `data/*.json`
(manifesto e vagas) usam um cache-buster por timestamp (`?t=...` em
`js/app.js`/`js/metodologia.js`), porque uma contagem ou posição de vaga
desatualizada é pior que buscar o arquivo de novo a cada carregamento.
