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
const measureToggleBtn = document.getElementById("measure-toggle");
const measureClearBtn = document.getElementById("measure-clear");
const measureStatusEl = document.getElementById("measure-status");
const mapContainer = document.getElementById("map");

let manifest = { municipios: [] };
let osmFetchController = null;

function setStatus(msg) {
  statusEl.textContent = msg || "";
}

// Clustering nativo do MapLibre: evita plotar centenas de milhares de
// círculos de uma vez quando o zoom está baixo (cidade toda em tela).
// A partir de CLUSTER_MAX_ZOOM os pontos individuais passam a ser
// renderizados normalmente, coloridos por porte.
const CLUSTER_MAX_ZOOM = 16;
const CLUSTER_RADIUS = 60;

map.on("load", async () => {
  map.addSource("vagas", {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
    cluster: true,
    clusterMaxZoom: CLUSTER_MAX_ZOOM,
    clusterRadius: CLUSTER_RADIUS,
  });

  map.addLayer({
    id: "vagas-clusters",
    type: "circle",
    source: "vagas",
    filter: ["has", "point_count"],
    paint: {
      "circle-radius": ["step", ["get", "point_count"], 14, 100, 18, 1000, 24, 10000, 32],
      "circle-color": ["step", ["get", "point_count"], "#90caf9", 100, "#42a5f5", 1000, "#1565c0", 10000, "#0d47a1"],
      "circle-opacity": 0.85,
      "circle-stroke-width": 1.5,
      "circle-stroke-color": "#ffffff",
    },
  });
  map.addLayer({
    id: "vagas-cluster-count",
    type: "symbol",
    source: "vagas",
    filter: ["has", "point_count"],
    layout: {
      "text-field": ["get", "point_count_abbreviated"],
      "text-size": 12,
    },
    paint: { "text-color": "#ffffff" },
  });

  map.addLayer({
    id: "vagas-points",
    type: "circle",
    source: "vagas",
    filter: ["!", ["has", "point_count"]],
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

  map.on("click", "vagas-clusters", (e) => {
    if (measureActive) return; // não conflitar com a ferramenta de medição
    const [feature] = map.queryRenderedFeatures(e.point, { layers: ["vagas-clusters"] });
    const clusterId = feature.properties.cluster_id;
    map.getSource("vagas").getClusterExpansionZoom(clusterId, (err, zoom) => {
      if (err) return;
      map.easeTo({ center: feature.geometry.coordinates, zoom });
    });
  });
  map.on("mouseenter", "vagas-clusters", () => (map.getCanvas().style.cursor = "pointer"));
  map.on("mouseleave", "vagas-clusters", () => (map.getCanvas().style.cursor = ""));

  map.addSource("osm-live", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({
    id: "osm-live-lines",
    type: "line",
    source: "osm-live",
    paint: { "line-color": "#ffeb3b", "line-width": 2, "line-opacity": 0.9 },
  });

  setupMeasureTool();
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

// --- Ferramenta de medição de distância ---------------------------------
// Modo "sempre partindo do último ponto": cada clique adiciona um vértice e
// liga ao anterior, formando uma linha encadeada (sem precisar redesenhar
// um segmento por vez). "Limpar" reinicia a medição; alternar o botão
// "Medir" desliga a captura de cliques sem perder a linha já desenhada.

let measureActive = false;
let measurePoints = []; // [[lon, lat], ...]

function haversineMeters(a, b) {
  const R = 6371000;
  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(b[1] - a[1]);
  const dLon = toRad(b[0] - a[0]);
  const lat1 = toRad(a[1]);
  const lat2 = toRad(b[1]);
  const sinDLat = Math.sin(dLat / 2);
  const sinDLon = Math.sin(dLon / 2);
  const h = sinDLat * sinDLat + Math.cos(lat1) * Math.cos(lat2) * sinDLon * sinDLon;
  return 2 * R * Math.asin(Math.sqrt(h));
}

function formatMeters(m) {
  return m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${m.toFixed(1)} m`;
}

function setupMeasureTool() {
  map.addSource("measure-line", { type: "geojson", data: emptyFC() });
  map.addLayer({
    id: "measure-line",
    type: "line",
    source: "measure-line",
    paint: { "line-color": "#ff5722", "line-width": 2.5, "line-dasharray": [2, 1] },
  });

  map.addSource("measure-points", { type: "geojson", data: emptyFC() });
  map.addLayer({
    id: "measure-points",
    type: "circle",
    source: "measure-points",
    paint: {
      "circle-radius": 5,
      "circle-color": "#ff5722",
      "circle-stroke-width": 1.5,
      "circle-stroke-color": "#ffffff",
    },
  });

  map.addSource("measure-labels", { type: "geojson", data: emptyFC() });
  map.addLayer({
    id: "measure-labels",
    type: "symbol",
    source: "measure-labels",
    layout: {
      "text-field": ["get", "label"],
      "text-size": 12,
      "text-offset": [0, -1],
      "text-anchor": "bottom",
    },
    paint: {
      "text-color": "#ff5722",
      "text-halo-color": "#ffffff",
      "text-halo-width": 1.5,
    },
  });

  map.on("click", onMeasureClick);
}

function emptyFC() {
  return { type: "FeatureCollection", features: [] };
}

function onMeasureClick(e) {
  if (!measureActive) return;
  measurePoints.push([e.lngLat.lng, e.lngLat.lat]);
  renderMeasure();
}

function renderMeasure() {
  map.getSource("measure-points").setData({
    type: "FeatureCollection",
    features: measurePoints.map((c) => ({ type: "Feature", geometry: { type: "Point", coordinates: c }, properties: {} })),
  });

  map.getSource("measure-line").setData(
    measurePoints.length >= 2
      ? { type: "FeatureCollection", features: [{ type: "Feature", geometry: { type: "LineString", coordinates: measurePoints }, properties: {} }] }
      : emptyFC()
  );

  const labelFeatures = [];
  let total = 0;
  for (let i = 1; i < measurePoints.length; i++) {
    const a = measurePoints[i - 1];
    const b = measurePoints[i];
    const segDist = haversineMeters(a, b);
    total += segDist;
    labelFeatures.push({
      type: "Feature",
      geometry: { type: "Point", coordinates: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2] },
      properties: { label: formatMeters(segDist) },
    });
  }
  map.getSource("measure-labels").setData({ type: "FeatureCollection", features: labelFeatures });

  if (measurePoints.length === 0) {
    measureStatusEl.textContent = measureActive ? "Clique no mapa para começar a medir." : "";
  } else if (measurePoints.length === 1) {
    measureStatusEl.textContent = "Clique no próximo ponto para medir a distância.";
  } else {
    measureStatusEl.textContent = `Distância total: ${formatMeters(total)} (${measurePoints.length - 1} segmento${measurePoints.length > 2 ? "s" : ""})`;
  }
}

measureToggleBtn.addEventListener("click", () => {
  measureActive = !measureActive;
  measureToggleBtn.classList.toggle("active", measureActive);
  measureToggleBtn.textContent = measureActive ? "Medindo…" : "Medir";
  mapContainer.classList.toggle("measuring", measureActive);
  renderMeasure();
});

measureClearBtn.addEventListener("click", () => {
  measurePoints = [];
  renderMeasure();
});
