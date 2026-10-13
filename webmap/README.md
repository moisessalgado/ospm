# webmap

Site estático (sem build) com duas páginas:

- `index.html` — o mapa interativo, que apresenta os pontos de vagas de
  plantio gerados pelo módulo `modeling/` sobre um basemap do Google Maps.
- `metodologia.html` — apresentação em formato de scrollytelling da
  metodologia de alocação, fontes de dados e limites de precisão, com os
  números do resultado carregados ao vivo de `data/manifest.json`.

`css/base.css` reúne os tokens visuais (cores, tipografia, nav, botões,
utilitário `.reveal` para animações de entrada) compartilhados pelas duas
páginas; `css/style.css` e `css/metodologia.css` têm os estilos específicos
de cada uma.

## Mapa (`index.html`)

- Basemap: tiles do Google Maps via endpoint XYZ não-oficial (`mt0-3.google.com/vt/...`),
  sem necessidade de API key. Risco: não é um endpoint suportado oficialmente
  pelo Google, podendo mudar ou ser bloqueado sem aviso.
- OpenStreetMap: disponível como opção de mapa base (tiles raster padrão do OSM).
- Clustering: até o zoom 14 os pontos são agregados em bolhas com contagem
  (clique para aproximar); a partir do 15 aparecem os pontos individuais.
- Medição de distância: botão "Medir" — cada clique adiciona um vértice, a
  linha segue o cursor mostrando a distância ao vivo; duplo clique, Enter ou
  Esc finalizam a linha (Backspace desfaz o último vértice). Com "Snap" ligado,
  o cursor gruda em qualquer ponto de vaga sob ele (zoom 15+).
- Vagas de plantio: carregadas de `data/<municipio>/<variante>.json`, gerado
  por `modeling/scripts/04_export_webmap.py`. Não é GeoJSON — é um array plano
  `[lon, lat, porte, tipo_vaga, ...]` em que os atributos são índices da
  tabela `legend`, o que reduz bastante o tamanho do arquivo sem perder
  nenhuma vaga. `js/app.js` reconstrói o GeoJSON no navegador antes de passar
  para o MapLibre. `data/manifest.json` lista municípios e variantes de
  espaçamento disponíveis nos seletores.
- A posição lateral das vagas é indicativa (linha da face de quadra do IBGE);
  ver `modeling/README.md`.
- Em telas estreitas (≤560px) o painel de controles inicia recolhido; o botão
  "Controles" alterna a exibição (`js/app.js`).

## Metodologia (`metodologia.html`)

- As seções revelam ao rolar via `IntersectionObserver` (classe `.reveal`,
  alternada para `.in-view`); respeita `prefers-reduced-motion`.
- Os diagramas são SVG inline (sem imagens externas), com animações próprias
  (`.pop`, `.fade`, `.draw-path`) definidas em `css/metodologia.css`.
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
— lembre de incrementar esse `N` ao editar `css/` ou `js/`.
