# modeling

Pipeline de modelagem das vagas de plantio de árvores a partir de dados do
OpenStreetMap, parametrizado por município.

## Uso

```bash
pip install -r requirements.txt

# roda cada etapa para um município (slug deve existir em common/municipios.py)
python scripts/01_download_osm.py --municipio sorocaba
python scripts/02_explore_data.py --municipio sorocaba
python scripts/05_fetch_auxiliary_osm.py --municipio sorocaba
python scripts/06_fetch_buildings.py --municipio sorocaba
python scripts/07_build_model_v1.py --municipio sorocaba
python scripts/08_generate_points_v1.py --municipio sorocaba

# gera o GeoJSON leve consumido pelo webmap/
python scripts/10_export_web.py --municipio sorocaba
```

Os scripts `03`/`04` são o modelo v0 (pilot, superado pelo v1) e `09` é um
plot de conferência visual do v1 — não fazem parte do caminho até o webmap.

## Adicionar um novo município

Edite `common/municipios.py` e adicione uma entrada em `MUNICIPIOS` com o
slug, nome, UF e o EPSG SIRGAS 2000/UTM correto para a longitude do
município (consulte epsg.io). Depois rode o pipeline acima passando
`--municipio <slug>`.

## Dados

`data/<slug>/` e `output/<slug>/` são gerados localmente e ficam fora do
git (ver `.gitignore` — alguns arquivos passam de 100MB e o GitHub rejeita).
O único artefato que sai deste módulo para o repositório é o GeoJSON leve
escrito em `../webmap/data/<slug>.geojson` pelo `10_export_web.py`.
