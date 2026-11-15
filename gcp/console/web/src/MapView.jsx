import { useEffect, useRef } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { SEVERITY } from "./api.js";

// Satelite de Esri (sin llave) + etiquetas con las fuentes de demotiles de MapLibre
const STYLE = {
  version: 8,
  glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
  sources: {
    sat: {
      type: "raster",
      tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"],
      tileSize: 256,
      maxzoom: 18,
      attribution: "Imágenes © Esri, Maxar, Earthstar Geographics",
    },
  },
  layers: [
    { id: "sat", type: "raster", source: "sat", paint: { "raster-saturation": -0.35, "raster-brightness-max": 0.75 } },
  ],
};

// Operacion Cerro Azul: 3 zonas a menos de 6 km
const CENTER = [-71.5525, -13.92];

const ZONES = [
  { id: "NORTE", name: "Norte · Tajo", lon: -71.56, lat: -13.9 },
  { id: "CENTRO", name: "Centro · Carga y chancado", lon: -71.545, lat: -13.92 },
  { id: "SUR", name: "Sur · Botadero y vías", lon: -71.555, lat: -13.94 },
];

const sevColor = ["match", ["get", "severity"],
  "immediate", SEVERITY.immediate.color, "schedule", SEVERITY.schedule.color,
  "observe", SEVERITY.observe.color, SEVERITY.ok.color];

function toGeoJSON(machines) {
  return {
    type: "FeatureCollection",
    features: machines.filter((m) => m.lat != null && m.lon != null).map((m) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [m.lon, m.lat] },
      properties: {
        id: m.machine_id,
        severity: m.severity || "ok",
        off: m.status === "off" ? 1 : 0,
        rank: { immediate: 3, schedule: 2, observe: 1 }[m.severity] || 0,
      },
    })),
  };
}

export default function MapView({ machines, selected, onSelect }) {
  const box = useRef(null);
  const map = useRef(null);
  const ready = useRef(false);
  const latest = useRef(machines);
  latest.current = machines;

  useEffect(() => {
    const m = new maplibregl.Map({ container: box.current, style: STYLE, center: CENTER, zoom: 12.85, attributionControl: { compact: true } });
    map.current = m;
    m.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
    // style.load (no load): no espera a que bajen todas las teselas satelitales para dibujar las maquinas
    const ro = new ResizeObserver(() => m.resize());
    ro.observe(box.current);
    m.once("style.load", () => {
      m.addSource("zones", {
        type: "geojson",
        data: { type: "FeatureCollection", features: ZONES.map((z) => ({ type: "Feature", geometry: { type: "Point", coordinates: [z.lon, z.lat] }, properties: { name: z.name } })) },
      });
      m.addLayer({ id: "zone-ring", type: "circle", source: "zones", paint: { "circle-radius": ["interpolate", ["exponential", 2], ["zoom"], 12, 40, 15, 320], "circle-color": "rgba(255,255,255,0.03)", "circle-stroke-color": "rgba(255,255,255,0.35)", "circle-stroke-width": 1 } });
      m.addLayer({ id: "zone-label", type: "symbol", source: "zones", layout: { "text-field": ["get", "name"], "text-font": ["Open Sans Semibold"], "text-size": 12, "text-offset": [0, -6], "text-letter-spacing": 0.08 }, paint: { "text-color": "#e5e7eb", "text-halo-color": "#0b0f14", "text-halo-width": 1.5 } });

      m.addSource("machines", { type: "geojson", data: toGeoJSON(latest.current) });
      m.addLayer({ id: "halo", type: "circle", source: "machines", filter: [">=", ["get", "rank"], 2], paint: { "circle-radius": ["match", ["get", "severity"], "immediate", 18, 13], "circle-color": sevColor, "circle-opacity": 0.28, "circle-blur": 0.6 } });
      m.addLayer({ id: "dots", type: "circle", source: "machines", layout: { "circle-sort-key": ["get", "rank"] }, paint: { "circle-radius": ["match", ["get", "severity"], "immediate", 8, "schedule", 7, "observe", 6, 5], "circle-color": sevColor, "circle-opacity": ["case", ["==", ["get", "off"], 1], 0.45, 1], "circle-stroke-color": "#0b0f14", "circle-stroke-width": 1.5 } });
      m.addLayer({ id: "sel", type: "circle", source: "machines", filter: ["==", ["get", "id"], ""], paint: { "circle-radius": 13, "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": "#ffffff", "circle-stroke-width": 2.5 } });
      m.addLayer({ id: "labels", type: "symbol", source: "machines", filter: [">=", ["get", "rank"], 1], layout: { "text-field": ["get", "id"], "text-font": ["Open Sans Semibold"], "text-size": 11, "text-offset": [0, 1.4], "text-anchor": "top" }, paint: { "text-color": "#ffffff", "text-halo-color": "#0b0f14", "text-halo-width": 1.6 } });

      m.on("click", "dots", (e) => onSelect(e.features[0].properties.id));
      m.on("mouseenter", "dots", () => (m.getCanvas().style.cursor = "pointer"));
      m.on("mouseleave", "dots", () => (m.getCanvas().style.cursor = ""));
      ready.current = true;
      m.getSource("machines").setData(toGeoJSON(latest.current));
    });
    return () => { ro.disconnect(); m.remove(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!ready.current) return;
    map.current.getSource("machines")?.setData(toGeoJSON(machines));
  }, [machines]);

  useEffect(() => {
    if (!ready.current) return;
    map.current.setFilter("sel", ["==", ["get", "id"], selected || ""]);
    const m = machines.find((x) => x.machine_id === selected);
    if (m) map.current.easeTo({ center: [m.lon, m.lat], zoom: Math.max(map.current.getZoom(), 14.2), duration: 600 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected]);

  return <div ref={box} className="map" />;
}
