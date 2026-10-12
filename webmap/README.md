# webmap

Site estático (sem build) que apresenta os pontos de vagas de plantio
gerados pelo módulo `modeling/`, sobre um basemap do Google Maps.

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
- Pontos de vagas de plantio: carregados de `data/<slug>.json`, gerado pelo
  `modeling/scripts/10_export_web.py`. Não é GeoJSON — é um array plano
  `[lon, lat, porte_code, slot_code, ...]` mais uma tabela `legend` para
  decodificar os códigos, o que reduz bastante o tamanho do arquivo
  (sem perder nenhum ponto). `js/app.js` reconstrói o GeoJSON no navegador
  antes de passar para o MapLibre. `data/manifest.json` lista os municípios
  disponíveis no seletor.

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
