"use strict";

// Tiles do Google Maps via endpoint XYZ não-oficial (sem necessidade de API key).
// lyrs: m = ruas, s = satélite, y = híbrido (satélite + rótulos/vias).
function googleTileUrls(lyrs) {
  return [0, 1, 2, 3].map(
    (s) => `https://mt${s}.google.com/vt/lyrs=${lyrs}&x={x}&y={y}&z={z}`
  );
}

const BASEMAPS = {
  roadmap: googleTileUrls("m"),
  satellite: googleTileUrls("s"),
  hybrid: googleTileUrls("y"),
};

const OVERPASS_URL = "https://overpass-api.de/api/interpreter";
const OSM_MIN_ZOOM = 14; // evita baixar a malha viária de toda a cidade de uma vez

const style = {
  version: 8,
  sources: Object.fromEntries(
    Object.entries(BASEMAPS).map(([key, tiles]) => [
      `basemap-${key}`,
      { type: "raster", tiles, tileSize: 256, attribution: "© Google" },
    ])
  ),
  layers: Object.keys(BASEMAPS).map((key) => ({
    id: `basemap-${key}`,
    type: "raster",
    source: `basemap-${key}`,
    layout: { visibility: key === "satellite" ? "visible" : "none" },
  })),
};

const map = new maplibregl.Map({
  container: "map",
  style,
  center: [-47.46, -23.5],
  zoom: 12,
});

const statusEl = document.getElementById("status");
const municipioSelect = document.getElementById("municipio-select");
const basemapSelect = document.getElementById("basemap-select");
const osmToggle = document.getElementById("osm-toggle");

let manifest = { municipios: [] };
let osmFetchController = null;

function setStatus(msg) {
  statusEl.textContent = msg || "";
}

map.on("load", async () => {
  map.addSource("vagas", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({
    id: "vagas-points",
    type: "circle",
    source: "vagas",
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 10, 1.5, 16, 4, 19, 7],
      "circle-color": [
        "match",
        ["get", "porte"],
        "pequeno", "#66bb6a",
        "medio", "#1565c0",
        "grande", "#6a1b9a",
        "#999999",
      ],
      "circle-opacity": 0.85,
      "circle-stroke-width": 0.5,
      "circle-stroke-color": "#ffffff",
    },
  });

  map.addSource("osm-live", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({
    id: "osm-live-lines",
    type: "line",
    source: "osm-live",
    paint: { "line-color": "#ffeb3b", "line-width": 2, "line-opacity": 0.9 },
  });

  await loadManifest();
});

async function loadManifest() {
  const resp = await fetch("data/manifest.json");
  manifest = await resp.json();
  municipioSelect.innerHTML = manifest.municipios
    .map((m) => `<option value="${m.slug}">${m.nome} — ${m.uf}</option>`)
    .join("");
  if (manifest.municipios.length) {
    await loadMunicipio(manifest.municipios[0].slug);
  }
}

// Decodifica o formato compacto exportado por modeling/scripts/10_export_web.py
// — points é um array plano [lon, lat, porte_code, slot_code, ...] e legend
// mapeia cada código de volta ao texto usado na legenda/estilo dos pontos.
function decodeCompactPoints(payload) {
  const { legend, points } = payload;
  const features = [];
  for (let i = 0; i < points.length; i += 4) {
    const [lon, lat, porteCode, slotCode] = points.slice(i, i + 4);
    features.push({
      type: "Feature",
      geometry: { type: "Point", coordinates: [lon, lat] },
      properties: {
        porte: legend.porte[porteCode] ?? null,
        slot_type: legend.slot_type[slotCode] ?? null,
      },
    });
  }
  return { type: "FeatureCollection", features };
}

async function loadMunicipio(slug) {
  const entry = manifest.municipios.find((m) => m.slug === slug);
  if (!entry) return;
  setStatus(`Carregando ${entry.nome}…`);
  const resp = await fetch(`data/${slug}.json`);
  const payload = await resp.json();
  const geojson = decodeCompactPoints(payload);
  map.getSource("vagas").setData(geojson);
  map.flyTo({ center: entry.center, zoom: 13 });
  setStatus(`${entry.nome}: ${entry.count.toLocaleString("pt-BR")} vagas de plantio`);
}

municipioSelect.addEventListener("change", (e) => loadMunicipio(e.target.value));

basemapSelect.addEventListener("change", (e) => {
  for (const key of Object.keys(BASEMAPS)) {
    map.setLayoutProperty(`basemap-${key}`, "visibility", key === e.target.value ? "visible" : "none");
  }
});

osmToggle.addEventListener("change", () => {
  if (osmToggle.checked) {
    fetchOsmForView();
    map.on("moveend", fetchOsmForView);
  } else {
    map.off("moveend", fetchOsmForView);
    map.getSource("osm-live").setData({ type: "FeatureCollection", features: [] });
  }
});

async function fetchOsmForView() {
  if (!osmToggle.checked) return;
  if (map.getZoom() < OSM_MIN_ZOOM) {
    setStatus(`Aproxime o zoom (nível ${OSM_MIN_ZOOM}+) para ver as vias do OSM ao vivo`);
    return;
  }
  const b = map.getBounds();
  const bbox = `${b.getSouth()},${b.getWest()},${b.getNorth()},${b.getEast()}`;

  if (osmFetchController) osmFetchController.abort();
  osmFetchController = new AbortController();

  setStatus("Buscando vias do OpenStreetMap…");
  try {
    const query = `[out:json][timeout:25];way["highway"](${bbox});out geom;`;
    const resp = await fetch(OVERPASS_URL, {
      method: "POST",
      body: `data=${encodeURIComponent(query)}`,
      signal: osmFetchController.signal,
    });
    const data = await resp.json();
    const features = data.elements
      .filter((el) => el.type === "way" && el.geometry)
      .map((el) => ({
        type: "Feature",
        properties: { name: el.tags?.name || "" },
        geometry: { type: "LineString", coordinates: el.geometry.map((p) => [p.lon, p.lat]) },
      }));
    map.getSource("osm-live").setData({ type: "FeatureCollection", features });
    setStatus(`${features.length} vias OSM carregadas na área visível`);
  } catch (err) {
    if (err.name !== "AbortError") setStatus("Falha ao buscar vias do OSM. Tente novamente.");
  }
}
