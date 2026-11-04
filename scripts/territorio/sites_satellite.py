"""sites_satellite.py — Capa 3: detección de canteras y frentes de arena con Sentinel-2.

Idea: una cantera o un acopio de arena es SUELO DESNUDO, CLARO y PERSISTENTE. Un lote agrícola
también puede estar desnudo, pero solo algunos meses. Por eso se mira un año entero mes a mes:
  1. Para cada mes: compuesto mediano Sentinel-2 L2A sin nubes (máscara SCL), a 20 m.
  2. Píxel "desnudo" si: NDVI < 0.15, MNDWI < 0 (no es agua), BSI > 0.02 y brillo visible > 0.12.
  3. Píxel candidato si estuvo desnudo en >= 80 % de los meses con dato (persistencia).
  4. Limpieza morfológica, polígonos >= area_min_ha; los > area_max_ha se marcan
     "posible médano/salina/cauce" (no se descartan: se revisan).
  5. Cambio: se repite el cálculo dos años antes. Polígono nuevo (casi sin desnudo antes) = "nuevo";
     creció > 15 % = "creciendo"; si no, "sin_cambios".
  6. Miniatura PNG antes/después para la verificación humana.

Datos: Copernicus Sentinel-2 vía Microsoft Planetary Computer (STAC abierto, sin clave).
Cobertura: zonas piloto de config.yaml -> territorio.zonas_piloto (bbox), procesadas en teselas.

Salida: data/territorio/sitios/satelite_<zona>.geojson, data/territorio/miniaturas/<id>.png

Uso:
  python scripts/territorio/sites_satellite.py --zona ibicuy_gualeguaychu
  python scripts/territorio/sites_satellite.py --zona alto_valle_anelo --anio 2026
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

import numpy as np
from scipy import ndimage

from geo_common import (SITE_COLS, T_SITES, T_THUMBS, area_ha, feature, get_logger, load_config, log_error, round_geom,
                        site_id, write_geojson)

log = get_logger("sites_satellite")
BANDS = ["B02", "B03", "B04", "B08", "B11", "SCL"]
VALID_SCL = {2, 4, 5, 6, 7, 11}  # sombra oscura, vegetación, suelo desnudo, agua, sin clasificar, nieve (resto: nubes/sombras)

DEFAULTS = {"ndvi_max": 0.15, "bsi_min": 0.02, "brillo_min": 0.12, "persistencia": 0.8, "area_min_ha": 0.5,
            "area_max_ha": 500, "resolucion_m": 20, "tesela_grados": 0.25, "nubes_max": 30, "crecimiento": 0.15}


# ------------------------------------------------------------------ núcleo (sin red: testeable)
def bare_mask(b: dict, p: dict) -> np.ndarray:
    """Máscara de suelo desnudo claro para un compuesto (reflectancias 0-1)."""
    eps = 1e-6
    ndvi = (b["B08"] - b["B04"]) / (b["B08"] + b["B04"] + eps)
    mndwi = (b["B03"] - b["B11"]) / (b["B03"] + b["B11"] + eps)
    bsi = ((b["B11"] + b["B04"]) - (b["B08"] + b["B02"])) / ((b["B11"] + b["B04"]) + (b["B08"] + b["B02"]) + eps)
    bright = (b["B02"] + b["B03"] + b["B04"]) / 3
    m = (ndvi < p["ndvi_max"]) & (mndwi < 0) & (bsi > p["bsi_min"]) & (bright > p["brillo_min"])
    return m & np.isfinite(ndvi)


def persistent(masks: list[np.ndarray], valids: list[np.ndarray], frac: float) -> np.ndarray:
    cnt = np.sum([m & v for m, v in zip(masks, valids)], axis=0)
    obs = np.sum(valids, axis=0)
    out = np.zeros_like(cnt, dtype=bool)
    ok = obs >= 3  # al menos 3 meses con dato
    out[ok] = (cnt[ok] / obs[ok]) >= frac
    return out


def clean(mask: np.ndarray, min_px: int) -> np.ndarray:
    m = ndimage.binary_opening(mask, iterations=1)
    m = ndimage.binary_closing(m, iterations=1)
    lab, n = ndimage.label(m)
    if n == 0:
        return m
    sizes = ndimage.sum(m, lab, range(1, n + 1))
    keep = np.isin(lab, np.where(sizes >= min_px)[0] + 1)
    return keep


def polygons(mask: np.ndarray, transform, crs) -> list:
    """Vectoriza la máscara y devuelve polígonos en WGS84."""
    from pyproj import Transformer
    from rasterio import features
    from shapely.geometry import shape
    from shapely.ops import transform as shp_tr
    tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform
    out = []
    for geom, val in features.shapes(mask.astype("uint8"), mask=mask, transform=transform):
        if val == 1:
            g = shape(geom).simplify(transform.a * 0.5)
            out.append(shp_tr(tr, g))
    return out


def change_state(poly_mask: np.ndarray, before: np.ndarray, growth: float) -> tuple[str, float]:
    now = poly_mask.sum()
    prev = (poly_mask & before).sum()
    if now == 0:
        return "desconocido", 0.0
    frac_prev = prev / now
    if frac_prev < 0.2:
        return "nuevo", 1 - frac_prev
    if (now - prev) / max(prev, 1) > growth:
        return "creciendo", (now - prev) / max(prev, 1)
    return "sin_cambios", 0.0


def thumbnail(rgb_now: np.ndarray, rgb_before: np.ndarray | None, sl, path) -> None:
    from PIL import Image, ImageDraw
    def to_img(rgb):
        a = rgb[sl]
        a = np.clip(a / 0.3, 0, 1) ** 0.8 * 255
        return Image.fromarray(np.nan_to_num(a).astype("uint8")).resize((256, 256), Image.NEAREST)
    imgs = [to_img(rgb_now)] + ([to_img(rgb_before)] if rgb_before is not None else [])
    canvas = Image.new("RGB", (256 * len(imgs) + 8 * (len(imgs) - 1), 272), "white")
    d = ImageDraw.Draw(canvas)
    for i, im in enumerate(imgs):
        canvas.paste(im, (i * 264, 16))
        d.text((i * 264 + 4, 2), "actual" if i == 0 else "2 años antes", fill=(0, 49, 99))
    canvas.save(path)


# ------------------------------------------------------------------ datos (requiere red)
def monthly_composites(bbox, year: int, p: dict):
    """Genera (mes, bandas, válidos, transform, crs) con compuestos medianos mensuales."""
    import planetary_computer as pc
    import pystac_client
    from odc.stac import load
    cat = pystac_client.Client.open("https://planetarycomputer.microsoft.com/api/stac/v1", modifier=pc.sign_inplace)
    lon_c = (bbox[0] + bbox[2]) / 2
    crs = f"EPSG:{32700 + int((lon_c + 180) // 6) + 1}"
    for month in range(1, 13):
        start = date(year, month, 1)
        end = date(year + (month == 12), (month % 12) + 1, 1)
        items = list(cat.search(collections=["sentinel-2-l2a"], bbox=bbox, datetime=f"{start}/{end}",
                                query={"eo:cloud_cover": {"lt": p["nubes_max"]}}).items())
        if not items:
            continue
        ds = load(items, bands=BANDS, bbox=bbox, crs=crs, resolution=p["resolucion_m"], groupby="solar_day")
        scl = ds["SCL"].values
        valid = np.isin(scl, list(VALID_SCL))
        b = {}
        for k in BANDS[:-1]:
            v = ds[k].values.astype("float32")
            v = np.where(valid & (v > 0), (v - 1000) / 10000 if _offset(items) else v / 10000, np.nan)  # baseline 04.00: offset 1000
            b[k] = np.nanmedian(v, axis=0)
        vmask = np.isfinite(b["B04"])
        yield month, b, vmask, ds.odc.geobox.affine, crs


def _offset(items) -> bool:
    try:
        return float(items[0].properties.get("s2:processing_baseline", "0")) >= 4.0
    except ValueError:
        return False


def run_tile(bbox, year: int, p: dict):
    def year_stack(y):
        masks, valids, rgb, tf, crs = [], [], None, None, None
        for month, b, v, tf, crs in monthly_composites(bbox, y, p):
            masks.append(bare_mask(b, p) & v)
            valids.append(v)
            rgb = np.dstack([b["B04"], b["B03"], b["B02"]])
        return masks, valids, rgb, tf, crs
    m_now, v_now, rgb_now, tf, crs = year_stack(year)
    if len(m_now) < 3:
        return [], None
    m_bef, v_bef, rgb_bef, _, _ = year_stack(year - 2)
    px_ha = (p["resolucion_m"] ** 2) / 10_000
    now = clean(persistent(m_now, v_now, p["persistencia"]), max(1, int(p["area_min_ha"] / px_ha)))
    before = persistent(m_bef, v_bef, p["persistencia"]) if len(m_bef) >= 3 and m_bef[0].shape == now.shape else np.zeros_like(now)
    lab, n = ndimage.label(now)
    results = []
    for i, sl in enumerate(ndimage.find_objects(lab), start=1):
        pm = lab == i
        polys = polygons(pm[sl], tf * tf.translation(sl[1].start, sl[0].start), crs)
        if not polys:
            continue
        g = max(polys, key=lambda x: x.area)
        estado, delta = change_state(pm, before, p["crecimiento"])
        results.append((g, estado, delta, sl))
    return results, (rgb_now, rgb_bef if rgb_bef is not None and rgb_bef.shape == rgb_now.shape else None)


def tiles(bbox, step):
    x0, y0, x1, y1 = bbox
    x = x0
    while x < x1:
        y = y0
        while y < y1:
            yield (round(x, 4), round(y, 4), round(min(x + step, x1), 4), round(min(y + step, y1), 4))
            y += step
        x += step


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zona", required=True)
    ap.add_argument("--anio", type=int, default=date.today().year - 1, help="año a analizar (completo)")
    a = ap.parse_args()
    cfg = load_config().get("territorio", {})
    zona = cfg["zonas_piloto"][a.zona]
    p = {**DEFAULTS, **cfg.get("satelite", {})}
    feats = []
    for tb in tiles(zona["bbox"], p["tesela_grados"]):
        try:
            res, rgbs = run_tile(tb, a.anio, p)
        except Exception as e:  # noqa: BLE001
            log_error("sites_satellite", f"tesela {tb}: {e}")
            continue
        for g, estado, delta, sl in res:
            ha = area_ha(g)
            if ha < p["area_min_ha"]:
                continue
            c = g.representative_point()
            sid = site_id("satelite", f"{round(c.x, 4)},{round(c.y, 4)}")
            thumb = T_THUMBS / f"{sid}.png"
            try:
                pad = 15
                big = (slice(max(sl[0].start - pad, 0), sl[0].stop + pad), slice(max(sl[1].start - pad, 0), sl[1].stop + pad))
                thumbnail(rgbs[0], rgbs[1], big, thumb)
            except Exception as e:  # noqa: BLE001
                log_error("sites_satellite", f"miniatura: {e}", sid)
            props = {k: "" for k in SITE_COLS}
            props.update({
                "id_sitio": sid, "origen": "satelite", "origen_ref": f"S2 {a.anio} zona {a.zona}", "tipo_sitio": "desconocido",
                "material": "desconocido", "provincia": zona.get("provincia", ""), "lat": round(c.y, 6), "lon": round(c.x, 6),
                "area_ha": round(ha, 2), "estado_actividad": estado, "fecha_evidencia": f"{a.anio}", "verificado": "pendiente",
                "miniatura": f"miniaturas/{thumb.name}", "url_fuente": "Copernicus Sentinel-2 L2A (Planetary Computer)",
                "fecha_consulta": date.today().isoformat(),
                "notas": "; ".join(x for x in ["suelo desnudo claro persistente (≥80 % de los meses)",
                                                f"crecimiento {delta:.0%} vs {a.anio - 2}" if estado == "creciendo" else "",
                                                "superficie grande: posible médano, salina o cauce" if ha > p["area_max_ha"] else ""] if x),
            })
            feats.append(feature(round_geom(g, 6), props))
        log.info("tesela %s: %s candidatos", tb, len(res))
    write_geojson(feats, T_SITES / f"satelite_{a.zona}.geojson")
    log.info("zona %s: %s candidatos (%s nuevos, %s creciendo)", a.zona, len(feats),
             sum(f["properties"]["estado_actividad"] == "nuevo" for f in feats), sum(f["properties"]["estado_actividad"] == "creciendo" for f in feats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
