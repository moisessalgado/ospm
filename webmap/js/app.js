"use strict";

// Google Maps tiles through the unofficial XYZ endpoint (no API key).
// lyrs: m = roadmap, s = satellite, y = hybrid (satellite + labels/roads).
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

// Up to this zoom slots are aggregated into counted clusters; above it individual
// slots are drawn (and the measuring tool can snap to them).
const CLUSTER_MAX_ZOOM = 14;
const CLUSTER_RADIUS = 50;

const SNAP_RADIUS_PX = 14;
const FONT = ["Open Sans Semibold"];

const style = {
  version: 8,
  // Without "glyphs" MapLibre silently skips every text layer (cluster counts,
  // distance labels).
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
    layout: { visibility: key === "osm" ? "visible" : "none" },
  })),
};

// Suzano's center (see data/manifest.json) — the default municipality, set
// directly so the map doesn't paint over some other city before the first
// flyTo kicks in (see loadManifest/selectMunicipality).
const map = new maplibregl.Map({
  container: "map",
  style,
  center: [-46.3192, -23.6155],
  zoom: 12,
});
map.addControl(new maplibregl.NavigationControl(), "top-right");
map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");

const statusEl = document.getElementById("status");
const municipalitySelect = document.getElementById("municipality-select");
const methodSelect = document.getElementById("method-select");
const spacingSelect = document.getElementById("spacing-select");
const basemapSelect = document.getElementById("basemap-select");
const slotsToggle = document.getElementById("layer-slots-toggle");
const rejectedToggle = document.getElementById("layer-rejected-toggle");
const canopyLayerControl = document.getElementById("canopy-layer-control");
const canopyToggle = document.getElementById("layer-canopy-toggle");
const canopyLegend = document.getElementById("canopy-legend");
const canopyFilterEl = document.getElementById("canopy-filter");
const canopyRange = document.getElementById("canopy-range");
const canopyValueEl = document.getElementById("canopy-value");
const measureToggleBtn = document.getElementById("measure-toggle");
const measureClearBtn = document.getElementById("measure-clear");
const snapToggle = document.getElementById("snap-toggle");
const measureStatusEl = document.getElementById("measure-status");
const cursorLabel = document.getElementById("cursor-label");

let manifest = { municipalities: [] };

const emptyFC = () => ({ type: "FeatureCollection", features: [] });

function setStatus(msg) {
  statusEl.textContent = msg || "";
}

map.on("load", async () => {
  addSlotLayers();
  addMeasureLayers();
  await loadManifest();
});

// --- Planting slots layer (clustered) ---------------------------------------

function addSlotLayers() {
  map.addSource("slots", {
    type: "geojson",
    data: emptyFC(),
    cluster: true,
    clusterMaxZoom: CLUSTER_MAX_ZOOM,
    clusterRadius: CLUSTER_RADIUS,
  });

  map.addLayer({
    id: "slot-clusters",
    type: "circle",
    source: "slots",
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
    id: "slot-cluster-count",
    type: "symbol",
    source: "slots",
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
    id: "slot-points",
    type: "circle",
    source: "slots",
    filter: ["!", ["has", "point_count"]],
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 15, 3, 18, 6, 21, 10],
      "circle-color": PORTE_COLOR_EXPRESSION,
      "circle-opacity": 0.9,
      "circle-stroke-width": 1,
      "circle-stroke-color": "#ffffff",
    },
  });

  // Candidates the allocator dropped — same porte color as a real slot (it's
  // the size the slot would have had), but with a colored outline for why:
  // corner setback, an obstacle, or no safe room before the nearest facade
  // (see MOTIVO_COLOR). Only above the cluster threshold — at city scale
  // this would be a wash covering the whole map.
  map.addSource("rejected", { type: "geojson", data: emptyFC() });
  map.addLayer({
    id: "rejected-points",
    type: "circle",
    source: "rejected",
    minzoom: CLUSTER_MAX_ZOOM,
    layout: { visibility: "none" }, // off by default; the legend checkbox turns it on
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 15, 3, 18, 6, 21, 10],
      "circle-color": PORTE_COLOR_EXPRESSION,
      "circle-opacity": 0.9,
      "circle-stroke-width": 2.5,
      "circle-stroke-color": [
        "match", ["get", "motivo"],
        "sem_espaco_seguro", "#d64545",
        "esquina", "#fdd835",
        "via_transversal", "#e91e63",
        "espacamento_minimo", "#757575",
        "travessia", "#fb8c00",
        "acesso_garagem", "#8d6e63",
        "ponto_onibus", "#29b6f6",
        "arvore_existente", "#2e7d32",
        "#d64545",
      ],
    },
  });

  map.on("click", "rejected-points", (e) => {
    if (measure.active) return;
    const { porte, motivo } = e.features[0].properties;
    new maplibregl.Popup({ closeButton: true, maxWidth: "260px" })
      .setLngLat(e.features[0].geometry.coordinates)
      .setHTML(
        `<div class="slot-popup"><div class="slot-popup-id">Vaga inviável</div>` +
          `<div><strong>Porte:</strong> ${PORTE_LABEL[porte] ?? "—"}</div>` +
          `<div><strong>Motivo:</strong> ${MOTIVO_LABEL[motivo] ?? motivo}</div></div>`
      )
      .addTo(map);
  });
  map.on("mouseenter", "rejected-points", () => {
    if (!measure.active) map.getCanvas().style.cursor = "pointer";
  });
  map.on("mouseleave", "rejected-points", () => {
    if (!measure.active) map.getCanvas().style.cursor = "";
  });

  map.on("click", "slot-clusters", async (e) => {
    if (measure.active) return;
    const feature = e.features[0];
    const zoom = await map.getSource("slots").getClusterExpansionZoom(feature.properties.cluster_id);
    map.easeTo({ center: feature.geometry.coordinates, zoom });
  });
  map.on("mouseenter", "slot-clusters", () => {
    if (!measure.active) map.getCanvas().style.cursor = "pointer";
  });
  map.on("mouseleave", "slot-clusters", () => {
    if (!measure.active) map.getCanvas().style.cursor = "";
  });

  map.on("click", "slot-points", (e) => {
    if (measure.active) return;
    showSlotPopup(e.features[0]);
  });
  map.on("mouseenter", "slot-points", () => {
    if (!measure.active) map.getCanvas().style.cursor = "pointer";
  });
  map.on("mouseleave", "slot-points", () => {
    if (!measure.active) map.getCanvas().style.cursor = "";
  });
}

const PORTE_LABEL = { pequeno: "Pequeno", medio: "Médio", grande: "Grande" };
const TIPO_VAGA_LABEL = { calcada: "Calçada", canteiro_central: "Canteiro central", eixo_via: "Eixo da via (comparação)" };
const MOTIVO_LABEL = {
  sem_espaco_seguro: "Sem espaço seguro até a fachada",
  esquina: "Afastamento de esquina",
  via_transversal: "Dentro de uma via transversal",
  espacamento_minimo: "A menos do espaçamento mínimo de uma vaga de outra rua",
  travessia: "Travessia de pedestres",
  acesso_garagem: "Acesso de garagem",
  ponto_onibus: "Ponto de ônibus",
  arvore_existente: "Árvore existente",
};

// Shared by slot-points (real slots) and rejected-points (inviable
// candidates): the fill is always by porte, so a gray/red/whatever outline
// is what tells the two apart (see rejected-points' circle-stroke-color).
const PORTE_COLOR_EXPRESSION = [
  "match",
  ["get", "porte"],
  "pequeno", "#66bb6a",
  "medio", "#1565c0",
  "grande", "#6a1b9a",
  "#999999",
];

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function showSlotPopup(feature) {
  const p = feature.properties;
  const rows = [
    p.logradouro ? `<div class="slot-popup-street">${escapeHtml(p.logradouro)}</div>` : "",
    `<div><strong>Porte:</strong> ${PORTE_LABEL[p.porte] ?? "—"}</div>`,
    `<div><strong>Tipo:</strong> ${TIPO_VAGA_LABEL[p.tipo_vaga] ?? "—"}</div>`,
    p.cd_setor
      ? `<div><strong>Setor/quadra/face:</strong> ${escapeHtml(p.cd_setor)} / ${escapeHtml(p.cd_quadra)} / ${escapeHtml(p.cd_face)}</div>`
      : "",
    p.posicao_face_m != null ? `<div><strong>Posição ao longo da via/face:</strong> ${p.posicao_face_m} m</div>` : "",
    p.cobertura_copa_pct != null
      ? `<div><strong>Cobertura de copa:</strong> ${p.cobertura_copa_pct}% <span class="popup-note">(máx. a ${p.raio_copa_m} m${currentCanopySource ? ` · ${escapeHtml(currentCanopySource)}` : ""})</span></div>`
      : "",
  ].join("");
  new maplibregl.Popup({ closeButton: true, maxWidth: "260px" })
    .setLngLat(feature.geometry.coordinates)
    .setHTML(`<div class="slot-popup"><div class="slot-popup-id">Vaga #${p.id}</div>${rows}</div>`)
    .addTo(map);
}

// Compact format written by modeling/scripts/04_export_webmap.py:
// points = [lon, lat, porte, tipo_vaga, logradouro, face, posicao_face_m, (cobertura_copa_pct), ...],
// payload.stride numbers per slot (7, or 8 with the canopy-cover % — -1 where
// there is no data), where porte/tipo_vaga/logradouro/face are indices into
// legend ("face" indexes legend.face, a [cd_setor, cd_quadra, cd_face] triple;
// -1 for median slots, which have none). Rebuilt here as GeoJSON for MapLibre.
function decodeCompactPoints(payload) {
  const { legend, points } = payload;
  const stride = payload.stride ?? 7;
  const hasCanopy = stride > 7;
  const features = new Array(points.length / stride);
  for (let i = 0, j = 0; i < points.length; i += stride, j++) {
    const face = points[i + 5] >= 0 ? legend.face[points[i + 5]] : null;
    const pos = points[i + 6];
    features[j] = {
      type: "Feature",
      geometry: { type: "Point", coordinates: [points[i], points[i + 1]] },
      properties: {
        id: j + 1,
        porte: legend.porte[points[i + 2]] ?? null,
        tipo_vaga: legend.tipo_vaga[points[i + 3]] ?? null,
        logradouro: legend.logradouro[points[i + 4]] || null,
        cd_setor: face ? face[0] : null,
        cd_quadra: face ? face[1] : null,
        cd_face: face ? face[2] : null,
        posicao_face_m: pos >= 0 ? pos : null,
        cobertura_copa_pct: hasCanopy && points[i + 7] >= 0 ? points[i + 7] : null,
        // the canopy buffer is half the slot's spacing, which depends on the variant (see 03b_add_canopy_cover.py)
        raio_copa_m: hasCanopy ? canopyRadiusM(payload.spacing, legend.porte[points[i + 2]]) : null,
      },
    };
  }
  return { type: "FeatureCollection", features };
}

const SPACING_BY_PORTE_M = { pequeno: 6, medio: 8, grande: 11 };

function canopyRadiusM(spacingId, porte) {
  return (spacingId === "10m" ? 10 : SPACING_BY_PORTE_M[porte]) / 2;
}

// "rejeitados": [lon, lat, porte, motivo, ...] (4 numbers per point — see
// modeling/scripts/04_export_webmap.py): the porte and position the slot
// would have had, and why it was dropped (corner setback, an obstacle, or
// no safe room before the nearest facade).
const REJECTED_STRIDE = 4;

function decodeRejectedPoints(flat, legend) {
  const features = new Array(flat.length / REJECTED_STRIDE);
  for (let i = 0, j = 0; i < flat.length; i += REJECTED_STRIDE, j++) {
    features[j] = {
      type: "Feature",
      geometry: { type: "Point", coordinates: [flat[i], flat[i + 1]] },
      properties: {
        porte: legend.porte[flat[i + 2]] ?? null,
        motivo: legend.motivo[flat[i + 3]] ?? null,
      },
    };
  }
  return { type: "FeatureCollection", features };
}

// Bypasses the browser cache: the data changes whenever the pipeline is
// rerun, and a stale manifest/variant here means wrong slot counts and
// positions silently shown to the user (not just a stale style).
const CACHE_BUST = Date.now();
const DEFAULT_MUNICIPALITY = "suzano";
const DEFAULT_METHOD = "osm_eixo"; // first in the selector; see METHODS in 04_export_webmap.py

// A municipality's variants each carry a method and a spacing (see
// modeling/scripts/04_export_webmap.py); the two selects below combine to
// pick one. uniqueBy keeps the manifest's own order (method/spacing loop
// order at export time), so the selects list options consistently.
function uniqueBy(items, keyFn) {
  const seen = new Set();
  return items.filter((item) => {
    const key = keyFn(item);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

async function loadManifest() {
  const resp = await fetch(`data/manifest.json?t=${CACHE_BUST}`);
  manifest = await resp.json();
  municipalitySelect.innerHTML = manifest.municipalities
    .map((m) => `<option value="${m.slug}">${m.name} — ${m.state}</option>`)
    .join("");
  if (manifest.municipalities.length) {
    const initial = manifest.municipalities.find((m) => m.slug === DEFAULT_MUNICIPALITY) ?? manifest.municipalities[0];
    municipalitySelect.value = initial.slug;
    await selectMunicipality(initial.slug, false);
  }
}

// animate=false skips the flyTo — used on first load, since the map already
// opens centered on the default municipality (see the map constructor), so
// animating there too would fly away from and back to the same spot.
async function selectMunicipality(slug, animate = true) {
  const municipality = manifest.municipalities.find((m) => m.slug === slug);
  if (!municipality) return;
  const methods = uniqueBy(municipality.variants, (v) => v.method);
  methodSelect.innerHTML = methods.map((v) => `<option value="${v.method}">${v.method_label}</option>`).join("");
  const initialMethod = methods.find((v) => v.method === DEFAULT_METHOD) ?? methods[0];
  methodSelect.value = initialMethod.method;
  await selectMethod(municipality, initialMethod.method);
  syncCanopyLayer();
  if (animate) map.flyTo({ center: municipality.center, zoom: 12 });
  else map.jumpTo({ center: municipality.center, zoom: 12 });
}

async function selectMethod(municipality, method) {
  const spacings = municipality.variants.filter((v) => v.method === method);
  spacingSelect.innerHTML = spacings.map((v) => `<option value="${v.spacing}">${v.spacing_label}</option>`).join("");
  await loadVariant(municipality, spacings[0]);
}

async function loadVariant(municipality, variant) {
  setStatus(`Carregando ${municipality.name}…`);
  const resp = await fetch(`data/${municipality.slug}/${variant.id}.json?t=${CACHE_BUST}`);
  const payload = await resp.json();
  payload.spacing = variant.spacing;
  currentCanopySource = payload.copa_fonte ?? null;
  currentSlots = decodeCompactPoints(payload);
  const hasCanopy = (payload.stride ?? 7) > 7;
  canopyFilterEl.hidden = !hasCanopy;
  canopyRange.value = 100;
  currentCanopyMax = 100;
  canopyValueEl.textContent = "100";
  currentVariant = variant;
  applySlots();
  map.getSource("rejected").setData(decodeRejectedPoints(payload.rejeitados ?? [], payload.legend));
}

// Slots with no canopy data always stay; canopy filter keeps the ones whose
// cover is at most the slider's value (no filtering at 100).
let currentSlots = null;
let currentVariant = null;
let currentCanopyMax = 100;
let currentCanopySource = null; // raster the canopy % came from (differs by municipality)

function applySlots() {
  const features = currentCanopyMax >= 100
    ? currentSlots.features
    : currentSlots.features.filter((f) => f.properties.cobertura_copa_pct == null || f.properties.cobertura_copa_pct <= currentCanopyMax);
  map.getSource("slots").setData({ type: "FeatureCollection", features });
  const rejectedNote = currentVariant.rejected_count ? ` · ${currentVariant.rejected_count.toLocaleString("pt-BR")} inviáveis` : "";
  const filterNote = features.length < currentSlots.features.length
    ? ` (de ${currentSlots.features.length.toLocaleString("pt-BR")}, copa ≤ ${currentCanopyMax}%)` : "";
  setStatus(`${features.length.toLocaleString("pt-BR")} vagas de plantio${filterNote}${rejectedNote}`);
}

canopyRange.addEventListener("input", (e) => {
  currentCanopyMax = Number(e.target.value);
  canopyValueEl.textContent = currentCanopyMax;
  applySlots();
});

municipalitySelect.addEventListener("change", (e) => selectMunicipality(e.target.value));
methodSelect.addEventListener("change", (e) => {
  const municipality = manifest.municipalities.find((m) => m.slug === municipalitySelect.value);
  selectMethod(municipality, e.target.value);
});
spacingSelect.addEventListener("change", (e) => {
  const municipality = manifest.municipalities.find((m) => m.slug === municipalitySelect.value);
  const variant = municipality.variants.find((v) => v.method === methodSelect.value && v.spacing === e.target.value);
  loadVariant(municipality, variant);
});

basemapSelect.addEventListener("change", (e) => {
  for (const key of Object.keys(BASEMAPS)) {
    map.setLayoutProperty(`basemap-${key}`, "visibility", key === e.target.value ? "visible" : "none");
  }
});

slotsToggle.addEventListener("change", (e) => {
  const visibility = e.target.checked ? "visible" : "none";
  for (const id of ["slot-clusters", "slot-cluster-count", "slot-points"]) map.setLayoutProperty(id, "visibility", visibility);
});

// Canopy-cover raster (webmap/data/<municipality>/copa.png, drawn under the
// slots). The image is only requested once the checkbox is on, and re-added
// whenever the municipality changes — each has its own image and corners.
function syncCanopyLayer() {
  const municipality = manifest.municipalities.find((m) => m.slug === municipalitySelect.value);
  const canopy = municipality?.canopy;
  canopyLayerControl.hidden = !canopy;
  if (map.getLayer("canopy-raster")) map.removeLayer("canopy-raster");
  if (map.getSource("canopy")) map.removeSource("canopy");
  canopyLegend.hidden = !canopy || !canopyToggle.checked;
  if (!canopy) return;
  document.getElementById("canopy-ramp").style.background = `linear-gradient(to right, ${canopy.ramp.join(", ")})`;
  document.getElementById("canopy-credit").textContent = `Fonte: ${canopy.credit} — ${canopy.source}`;
  if (!canopyToggle.checked) return;
  map.addSource("canopy", { type: "image", url: `data/${canopy.image}?t=${CACHE_BUST}`, coordinates: canopy.coordinates });
  map.addLayer(
    { id: "canopy-raster", type: "raster", source: "canopy", paint: { "raster-opacity": 0.75, "raster-resampling": "nearest" } },
    "slot-clusters"
  );
}

canopyToggle.addEventListener("change", syncCanopyLayer);

rejectedToggle.addEventListener("change", (e) => {
  map.setLayoutProperty("rejected-points", "visibility", e.target.checked ? "visible" : "none");
  document.getElementById("rejected-legend").hidden = !e.target.checked;
});

// --- Measuring tool ---------------------------------------------------------
// Each click adds a vertex; a rubber-band segment follows the cursor showing the
// live distance. Double-click, Enter or Esc finish the current line (it stays on
// the map) and the next click starts a new one. With snap on, the cursor locks
// onto the nearest visible slot under it, to measure tree-to-tree distances.

const measure = {
  active: false,
  lines: [], // finished lines: [[lon, lat], ...]
  current: [], // line being drawn
  cursor: null, // current cursor position (snap already applied)
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
  const candidates = map.queryRenderedFeatures(box, { layers: ["slot-points"] });
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
  // The second click of a double-click also lands here; it is handled by dblclick.
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
  // While measuring, double-click finishes the line instead of zooming.
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

document.addEventListener("keydown", (e) => {
  if (!measure.active) return;
  if (e.key === "Escape" || e.key === "Enter") finishCurrentLine();
  if (e.key === "Backspace" && measure.current.length) {
    measure.current.pop();
    renderMeasure();
  }
});

// --- Mobile panel toggle -----------------------------------------------

const panelToggle = document.getElementById("panel-toggle");

function setPanelCollapsed(collapsed) {
  document.body.classList.toggle("panel-collapsed", collapsed);
  panelToggle.setAttribute("aria-expanded", String(!collapsed));
  panelToggle.textContent = collapsed ? "Controles" : "Fechar";
}

panelToggle.addEventListener("click", () => setPanelCollapsed(!document.body.classList.contains("panel-collapsed")));

// On first load, keep the map front and center on narrow screens; the panel
// opens on demand via the toggle button.
if (window.matchMedia("(max-width: 560px)").matches) setPanelCollapsed(true);
