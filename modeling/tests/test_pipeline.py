import json

import geopandas as gpd
import numpy as np
import pytest
from scipy.spatial import cKDTree

from planting_slots.lote import COLUMNS, EMPTY, OK, REASON_IDS, process_municipality, read_status, run_batch, validate, write_metadata
from planting_slots.osm_pbf import extract_region
from helpers import BOUNDARY, CD_MUN, URBAN, make_inputs, write_canopy_tile


@pytest.fixture()
def extracted(tmp_path):
    layout = make_inputs(tmp_path)
    counts = extract_region(layout.pbf("sudeste"), layout.osm_roads("sudeste"), layout.osm_obstacles("sudeste"))
    return layout, counts


def test_extraction_keeps_roads_and_obstacles(extracted):
    layout, counts = extracted
    assert counts["vias"] == 5 and counts["obstaculos"] == 1  # footway is not a road; the bus stop is an obstacle
    roads = gpd.read_parquet(layout.osm_roads("sudeste"))
    assert set(roads["highway"]) == {"residential", "unclassified"}
    obstacles = gpd.read_parquet(layout.osm_obstacles("sudeste"))
    assert list(obstacles["obstaculo"]) == ["ponto_onibus"]


def test_municipality_output_schema_and_content(extracted):
    layout, _ = extracted
    status = process_municipality(layout, CD_MUN, ("glad_30m",))
    assert status["status"] == OK and status["pontos_viaveis"] > 40, status
    assert status["pontos"] == status["pontos_viaveis"] + status["pontos_inviaveis"]
    everything = gpd.read_parquet(layout.points(CD_MUN))
    assert list(everything.columns) == COLUMNS and everything.crs.to_epsg() == 4326
    points = everything[everything["viavel"]]
    assert (everything["cd_mun"] == CD_MUN).all() and everything["id_ponto"].is_unique
    # urban only: the rural road at lat -22.95 produces nothing, every point is inside the urban tract
    assert everything.geometry.within(URBAN).all() and everything.geometry.within(BOUNDARY).all()
    assert (everything["cd_setor"] == "355480505000001P").all() and (everything["nm_bairro"] == "Centro").all()
    assert (everything["cobertura_copa_pct"] == 40).all() and (everything["cobertura_copa_fonte"] == "glad_30m").all()
    assert set(points["porte"]) == {"medio"} and set(points["via_osm"]) == {"residential"}
    xy = np.c_[points.to_crs("EPSG:31983").geometry.x, points.to_crs("EPSG:31983").geometry.y]
    assert not cKDTree(xy).query_pairs(9.99)  # 10 m spacing holds across the two streets
    assert validate(layout, [CD_MUN]).loc[0, "problemas"] == ""


def test_unviable_points_carry_the_reason(extracted):
    layout, _ = extracted
    process_municipality(layout, CD_MUN, ("glad_30m",))
    everything = gpd.read_parquet(layout.points(CD_MUN))
    unviable = everything[~everything["viavel"]]
    assert everything.loc[everything["viavel"], "motivo"].isna().all() and unviable["motivo"].notna().all()
    # the corners of the two streets, the bus stop beside street A and the crossing of the streets all drop candidates
    assert {"corner", "bus-stop"} <= set(unviable["motivo"])
    assert set(unviable["motivo"]) <= set(REASON_IDS.values())
    assert "facade" not in set(unviable["motivo"]) and "sidewalk" not in set(unviable["motivo"])
    assert (unviable["porte"] == "medio").all() and unviable["cd_setor"].notna().all()


def test_points_are_off_the_centerline(extracted):
    layout, _ = extracted
    process_municipality(layout, CD_MUN, ("glad_30m",))
    points = gpd.read_parquet(layout.points(CD_MUN)).query("viavel").to_crs("EPSG:31983")
    roads = gpd.read_parquet(layout.osm_roads("sudeste")).to_crs("EPSG:31983")
    street_a = roads[roads["name"] == "Rua A"].geometry.union_all()
    on_a = points[points.geometry.distance(street_a) < 8]
    assert len(on_a) > 10 and (on_a.geometry.distance(street_a) > 4.5).all()  # half width 4.5 m + 0.5 m


def test_resume_skips_done_and_redoes_failures(extracted):
    layout, _ = extracted
    first = run_batch(layout, [CD_MUN], workers=1, sources=("glad_30m",))
    assert [r["status"] for r in first] == [OK]
    assert run_batch(layout, [CD_MUN], workers=1, sources=("glad_30m",)) == []  # nothing left to do
    assert len(run_batch(layout, [CD_MUN], workers=1, sources=("glad_30m",), redo=True)) == 1
    # a failing municipality is recorded and does not raise
    layout.canopy_tile("glad_30m", "20S_050W").unlink()
    status = process_municipality(layout, CD_MUN, ("glad_30m",))
    assert status["status"] == "erro" and "Tile de copa ausente" in status["erro"]
    assert read_status(layout, CD_MUN)["status"] == "erro"
    # ...and a rerun (without redo) picks the failed one up again
    write_canopy_tile(layout, "glad_30m", 40)
    assert [r["status"] for r in run_batch(layout, [CD_MUN], workers=1, sources=("glad_30m",))] == [OK]


def test_municipality_without_urban_tracts_is_empty_not_error(extracted):
    layout, _ = extracted
    layout.tracts(CD_MUN).unlink()
    status = process_municipality(layout, CD_MUN, ("glad_30m",))
    assert status["status"] == EMPTY and status["motivo"] == "sem_setores_urbanos"
    assert not layout.points(CD_MUN).exists()


def test_parallel_workers_and_metadata(extracted):
    layout, _ = extracted
    run_batch(layout, [CD_MUN], workers=2, sources=("glad_30m",))
    summary = validate(layout, [CD_MUN])
    write_metadata(layout, summary, ("glad_30m",))
    meta = json.loads(layout.metadata.read_text())
    assert meta["pontos"] == int(summary["pontos"].sum()) > 0 and meta["metodologia"] == "osm_calcada"
    assert meta["esquema"] == COLUMNS
