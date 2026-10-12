"use strict";

// Tiles do Google Maps via endpoint XYZ não-oficial (sem necessidade de API key).
// lyrs: m = ruas, s = satélite, y = híbrido (satélite + rótulos/vias).
function googleTileUrls(lyrs) {
  return [0, 1, 2, 3].map(
    (s) => `https://mt${s}.google.com/vt/lyrs=${lyrs}&x={x}&y={y}&z={z}`
  );
}

const BASEMAPS = {
  roadmap: { tiles: googleTileUrls("m"), attribution: "© Google", maxzoom: 22 },
  satellite: { tiles: googleTileUrls("s"), attribution: "© Google", maxzoom: 22 },
  hybrid: { tiles: googleTileUrls("y"), attribution: "© Google", maxzoom: 22 },
  osm: {
    tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    maxzoom: 19,
  },
};

// Clustering: abaixo deste zoom os pontos são agregados em bolhas com contagem.
// Acima dele os pontos individuais aparecem (e o snap da medição funciona).
const CLUSTER_MAX_ZOOM = 14;
const CLUSTER_RADIUS = 50;

const SNAP_RADIUS_PX = 14;
const FONT = ["Open Sans Semibold"];

const style = {
  version: 8,
  // Sem "glyphs" nenhuma camada de texto (contagem de cluster, rótulos de
  // distância) é criada pelo MapLibre.
  glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
  sources: Object.fromEntries(
    Object.entries(BASEMAPS).map(([key, b]) => [
      `basemap-${key}`,
      { type: "raster", tiles: b.tiles, tileSize: 256, attribution: b.attribution, maxzoom: b.maxzoom },
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
map.addControl(new maplibregl.NavigationControl(), "top-right");
map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");

const statusEl = document.getElementById("status");
const municipioSelect = document.getElementById("municipio-select");
const basemapSelect = document.getElementById("basemap-select");
const measureToggleBtn = document.getElementById("measure-toggle");
const measureClearBtn = document.getElementById("measure-clear");
const snapToggle = document.getElementById("snap-toggle");
const measureStatusEl = document.getElementById("measure-status");
const cursorLabel = document.getElementById("cursor-label");

let manifest = { municipios: [] };

const emptyFC = () => ({ type: "FeatureCollection", features: [] });

function setStatus(msg) {
  statusEl.textContent = msg || "";
}

map.on("load", async () => {
  addVagasLayers();
  addMeasureLayers();
  await loadManifest();
});

// --- Camada de vagas (com clustering) ---------------------------------------

function addVagasLayers() {
  map.addSource("vagas", {
    type: "geojson",
    data: emptyFC(),
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
      "circle-color": ["step", ["get", "point_count"], "#81c784", 100, "#43a047", 1000, "#2e7d32", 10000, "#1b5e20"],
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
      "text-font": FONT,
      "text-size": 12,
      "text-allow-overlap": true,
    },
    paint: { "text-color": "#ffffff" },
  });

  map.addLayer({
    id: "vagas-points",
    type: "circle",
    source: "vagas",
    filter: ["!", ["has", "point_count"]],
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 15, 3, 18, 6, 21, 10],
      "circle-color": [
        "match",
        ["get", "porte"],
        "pequeno", "#66bb6a",
        "medio", "#1565c0",
        "grande", "#6a1b9a",
        "#999999",
      ],
      "circle-opacity": 0.9,
      "circle-stroke-width": 1,
      "circle-stroke-color": "#ffffff",
    },
  });

  map.on("click", "vagas-clusters", async (e) => {
    if (measure.active) return;
    const feature = e.features[0];
    const zoom = await map.getSource("vagas").getClusterExpansionZoom(feature.properties.cluster_id);
    map.easeTo({ center: feature.geometry.coordinates, zoom });
  });
  map.on("mouseenter", "vagas-clusters", () => {
    if (!measure.active) map.getCanvas().style.cursor = "pointer";
  });
  map.on("mouseleave", "vagas-clusters", () => {
    if (!measure.active) map.getCanvas().style.cursor = "";
  });
}

// Formato compacto exportado por modeling/scripts/10_export_web.py:
// points = [lon, lat, porte_code, slot_code, ...]; legend decodifica os códigos.
function decodeCompactPoints(payload) {
  const { legend, points } = payload;
  const features = new Array(points.length / 4);
  for (let i = 0, j = 0; i < points.length; i += 4, j++) {
    features[j] = {
      type: "Feature",
      geometry: { type: "Point", coordinates: [points[i], points[i + 1]] },
      properties: {
        porte: legend.porte[points[i + 2]] ?? null,
        slot_type: legend.slot_type[points[i + 3]] ?? null,
      },
    };
  }
  return { type: "FeatureCollection", features };
}

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

async function loadMunicipio(slug) {
  const entry = manifest.municipios.find((m) => m.slug === slug);
  if (!entry) return;
  setStatus(`Carregando ${entry.nome}…`);
  const resp = await fetch(`data/${slug}.json`);
  const geojson = decodeCompactPoints(await resp.json());
  map.getSource("vagas").setData(geojson);
  map.flyTo({ center: entry.center, zoom: 12 });
  setStatus(`${entry.nome}: ${entry.count.toLocaleString("pt-BR")} vagas de plantio`);
}

municipioSelect.addEventListener("change", (e) => loadMunicipio(e.target.value));

basemapSelect.addEventListener("change", (e) => {
  for (const key of Object.keys(BASEMAPS)) {
    map.setLayoutProperty(`basemap-${key}`, "visibility", key === e.target.value ? "visible" : "none");
  }
});

// --- Ferramenta de medição ---------------------------------------------------
// Clique adiciona um vértice; a linha "elástica" segue o cursor mostrando a
// distância ao vivo. Duplo clique, Enter ou Esc finalizam a linha atual (ela
// fica no mapa) e o próximo clique começa uma nova. Com snap ligado, o cursor
// gruda no ponto de vaga mais próximo sob ele (qualquer ponto visível, não
// só vizinhos), para medir com precisão a distância entre árvores.

const measure = {
  active: false,
  lines: [], // linhas finalizadas: [[lon, lat], ...]
  current: [], // linha em desenho
  cursor: null, // posição atual do cursor (já com snap aplicado)
  snapped: false,
};

function haversineMeters(a, b) {
  const R = 6371008.8;
  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(b[1] - a[1]);
  const dLon = toRad(b[0] - a[0]);
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(a[1])) * Math.cos(toRad(b[1])) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

function formatMeters(m) {
  return m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${m.toFixed(2)} m`;
}

function lineLength(coords) {
  let total = 0;
  for (let i = 1; i < coords.length; i++) total += haversineMeters(coords[i - 1], coords[i]);
  return total;
}

function addMeasureLayers() {
  map.addSource("measure", { type: "geojson", data: emptyFC() });
  map.addLayer({
    id: "measure-lines",
    type: "line",
    source: "measure",
    filter: ["==", ["get", "kind"], "line"],
    layout: { "line-cap": "round" },
    paint: { "line-color": "#ff5722", "line-width": 2.5 },
  });
  map.addLayer({
    id: "measure-rubber",
    type: "line",
    source: "measure",
    filter: ["==", ["get", "kind"], "rubber"],
    paint: { "line-color": "#ff5722", "line-width": 2, "line-dasharray": [2, 1.5] },
  });
  map.addLayer({
    id: "measure-vertices",
    type: "circle",
    source: "measure",
    filter: ["==", ["get", "kind"], "vertex"],
    paint: {
      "circle-radius": 4,
      "circle-color": "#ffffff",
      "circle-stroke-width": 2,
      "circle-stroke-color": "#ff5722",
    },
  });
  map.addLayer({
    id: "measure-snap",
    type: "circle",
    source: "measure",
    filter: ["==", ["get", "kind"], "snap"],
    paint: {
      "circle-radius": 9,
      "circle-color": "rgba(0,0,0,0)",
      "circle-stroke-width": 2.5,
      "circle-stroke-color": "#ffeb3b",
    },
  });
  map.addLayer({
    id: "measure-labels",
    type: "symbol",
    source: "measure",
    filter: ["==", ["get", "kind"], "label"],
    layout: {
      "text-field": ["get", "label"],
      "text-font": FONT,
      "text-size": 13,
      "text-offset": [0, -0.9],
      "text-allow-overlap": true,
      "text-ignore-placement": true,
    },
    paint: { "text-color": "#bf360c", "text-halo-color": "#ffffff", "text-halo-width": 2 },
  });

  map.on("click", onMeasureClick);
  map.on("dblclick", onMeasureDblClick);
  map.on("mousemove", onMeasureMove);
  map.getCanvasContainer().addEventListener("mouseleave", () => {
    measure.cursor = null;
    cursorLabel.hidden = true;
    renderMeasure();
  });
}

function snapTarget(point) {
  if (!snapToggle.checked) return null;
  const box = [
    [point.x - SNAP_RADIUS_PX, point.y - SNAP_RADIUS_PX],
    [point.x + SNAP_RADIUS_PX, point.y + SNAP_RADIUS_PX],
  ];
  const candidates = map.queryRenderedFeatures(box, { layers: ["vagas-points"] });
  let best = null;
  let bestDist = Infinity;
  for (const f of candidates) {
    const px = map.project(f.geometry.coordinates);
    const d = Math.hypot(px.x - point.x, px.y - point.y);
    if (d < bestDist && d <= SNAP_RADIUS_PX) {
      bestDist = d;
      best = f.geometry.coordinates;
    }
  }
  return best;
}

function resolvePosition(e) {
  const snap = snapTarget(e.point);
  measure.snapped = Boolean(snap);
  return snap ? [snap[0], snap[1]] : [e.lngLat.lng, e.lngLat.lat];
}

function onMeasureClick(e) {
  if (!measure.active) return;
  // O segundo click de um duplo clique chega aqui também; detail===2 é tratado no dblclick.
  if (e.originalEvent.detail > 1) return;
  measure.current.push(resolvePosition(e));
  renderMeasure();
}

function onMeasureDblClick(e) {
  if (!measure.active) return;
  e.preventDefault();
  finishCurrentLine();
}

function onMeasureMove(e) {
  if (!measure.active) return;
  measure.cursor = resolvePosition(e);
  map.getCanvas().style.cursor = measure.snapped ? "pointer" : "crosshair";

  if (measure.current.length) {
    const last = measure.current[measure.current.length - 1];
    const seg = haversineMeters(last, measure.cursor);
    const total = lineLength(measure.current) + seg;
    cursorLabel.textContent =
      measure.current.length > 1 ? `${formatMeters(seg)}  (total ${formatMeters(total)})` : formatMeters(seg);
    cursorLabel.style.left = `${e.point.x + 14}px`;
    cursorLabel.style.top = `${e.point.y + 14}px`;
    cursorLabel.hidden = false;
  } else {
    cursorLabel.hidden = true;
  }
  renderMeasure();
}

function finishCurrentLine() {
  if (measure.current.length >= 2) measure.lines.push(measure.current);
  measure.current = [];
  cursorLabel.hidden = true;
  renderMeasure();
}

function segmentFeatures(coords) {
  const out = [];
  for (let i = 1; i < coords.length; i++) {
    const a = coords[i - 1];
    const b = coords[i];
    out.push({
      type: "Feature",
      properties: { kind: "label", label: formatMeters(haversineMeters(a, b)) },
      geometry: { type: "Point", coordinates: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2] },
    });
  }
  return out;
}

function renderMeasure() {
  const features = [];
  for (const coords of [...measure.lines, measure.current]) {
    if (coords.length >= 2) {
      features.push({ type: "Feature", properties: { kind: "line" }, geometry: { type: "LineString", coordinates: coords } });
      features.push(...segmentFeatures(coords));
    }
    for (const c of coords) {
      features.push({ type: "Feature", properties: { kind: "vertex" }, geometry: { type: "Point", coordinates: c } });
    }
  }
  if (measure.active && measure.current.length && measure.cursor) {
    const last = measure.current[measure.current.length - 1];
    features.push({ type: "Feature", properties: { kind: "rubber" }, geometry: { type: "LineString", coordinates: [last, measure.cursor] } });
  }
  if (measure.active && measure.snapped && measure.cursor) {
    features.push({ type: "Feature", properties: { kind: "snap" }, geometry: { type: "Point", coordinates: measure.cursor } });
  }
  map.getSource("measure").setData({ type: "FeatureCollection", features });

  if (!measure.active) {
    measureStatusEl.textContent = measure.lines.length ? `${measure.lines.length} medição(ões) no mapa` : "";
  } else if (!measure.current.length) {
    measureStatusEl.textContent = "Clique para iniciar uma linha.";
  } else {
    measureStatusEl.textContent = `Linha atual: ${formatMeters(lineLength(measure.current))} — duplo clique, Enter ou Esc para finalizar.`;
  }
}

function setMeasureActive(active) {
  measure.active = active;
  if (!active) finishCurrentLine();
  measureToggleBtn.classList.toggle("active", active);
  measureToggleBtn.textContent = active ? "Parar" : "Medir";
  // Duplo clique finaliza a linha em vez de dar zoom enquanto mede.
  if (active) map.doubleClickZoom.disable();
  else map.doubleClickZoom.enable();
  map.getCanvas().style.cursor = active ? "crosshair" : "";
  measure.cursor = null;
  cursorLabel.hidden = true;
  renderMeasure();
}

measureToggleBtn.addEventListener("click", () => setMeasureActive(!measure.active));

measureClearBtn.addEventListener("click", () => {
  measure.lines = [];
  measure.current = [];
  renderMeasure();
});

snapToggle.addEventListener("change", () => {
  if (snapToggle.checked && map.getZoom() <= CLUSTER_MAX_ZOOM) {
    measureStatusEl.textContent = `Snap funciona com os pontos individuais — aproxime o zoom (nível ${CLUSTER_MAX_ZOOM + 1}+).`;
  }
});

document.addEventListener("keydown", (e) => {
  if (!measure.active) return;
  if (e.key === "Escape" || e.key === "Enter") finishCurrentLine();
  if (e.key === "Backspace" && measure.current.length) {
    measure.current.pop();
    renderMeasure();
  }
});
