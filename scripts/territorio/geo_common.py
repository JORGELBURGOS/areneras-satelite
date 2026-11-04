"""Utilidades del agente territorial: esquema de SITIOS, geometría y archivos.

Concepto clave: un SITIO es el lugar físico de donde sale la arena (cantera, banco, frente de
extracción, playa de acopio arenera, tramo de dragado). Se guarda con su geometría real.
La EMPRESA (titular/operador) y su domicilio son otra cosa y viven en la base nacional;
el vínculo sitio→operador se registra aparte, con método y confianza.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from pyproj import Geod, Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform as shp_transform

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import DATA, ROOT, get_logger, log_error, load_config  # noqa: E402,F401

TERR = DATA / "territorio"
T_RAW, T_SITES, T_THUMBS = TERR / "raw", TERR / "sitios", TERR / "miniaturas"
for d in (T_RAW, T_SITES, T_THUMBS, T_RAW / "edictos"):
    d.mkdir(parents=True, exist_ok=True)

SITE_COLS = [
    "id_sitio", "nombre", "origen", "origen_ref", "tipo_sitio", "material", "provincia", "departamento",
    "localidad_cercana", "lat", "lon", "area_ha", "estado_actividad", "fecha_evidencia", "titular", "cuit_titular",
    "expediente", "id_registro_operador", "operador", "vinculo_metodo", "vinculo_confianza", "verificado",
    "verificado_por", "miniatura", "url_fuente", "fecha_consulta", "notas",
]
TIPOS_SITIO = {"cantera", "banco", "acopio_puerto", "tramo_dragado", "planta", "desconocido"}
ESTADOS_ACT = {"activo", "creciendo", "nuevo", "sin_cambios", "inactivo", "desconocido"}

# Provincias: código ISO 3166-2 (para consultas Overpass por área) y faja Gauss-Krüger habitual
PROV_ISO = {
    "Buenos Aires": "AR-B", "Ciudad Autónoma de Buenos Aires": "AR-C", "Catamarca": "AR-K", "Chaco": "AR-H",
    "Chubut": "AR-U", "Córdoba": "AR-X", "Corrientes": "AR-W", "Entre Ríos": "AR-E", "Formosa": "AR-P",
    "Jujuy": "AR-Y", "La Pampa": "AR-L", "La Rioja": "AR-F", "Mendoza": "AR-M", "Misiones": "AR-N",
    "Neuquén": "AR-Q", "Río Negro": "AR-R", "Salta": "AR-A", "San Juan": "AR-J", "San Luis": "AR-D",
    "Santa Cruz": "AR-Z", "Santa Fe": "AR-S", "Santiago del Estero": "AR-G", "Tierra del Fuego": "AR-V", "Tucumán": "AR-T",
}
GEOD = Geod(ellps="WGS84")


def area_ha(geom) -> float:
    """Superficie geodésica en hectáreas de un polígono WGS84 (0 para puntos/líneas)."""
    if geom.geom_type not in ("Polygon", "MultiPolygon"):
        return 0.0
    return abs(GEOD.geometry_area_perimeter(geom)[0]) / 10_000


def site_id(origen: str, ref: str) -> str:
    return "S-" + hashlib.md5(f"{origen}|{ref}".encode()).hexdigest()[:8].upper()


# Gauss-Krüger Argentina: POSGAR 2007 fajas 1-7 = EPSG:5343..5349; POSGAR 94 = EPSG:22181..22187;
# Campo Inchauspe (datum antiguo) = EPSG:22191..22197. Los edictos suelen indicar faja y a veces el marco.
GK_EPSG = {"posgar2007": 5342, "posgar94": 22180, "inchauspe": 22190}


def gk_to_wgs84(x: float, y: float, faja: int, marco: str = "posgar2007") -> tuple[float, float]:
    """Convierte coordenadas Gauss-Krüger (X=norte, Y=este con prefijo de faja) a (lat, lon)."""
    epsg = GK_EPSG[marco] + int(faja)
    t = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    # En Argentina se publica X = Norte (~6.000.000) e Y = Este (~faja*1.000.000 + 500.000)
    lon, lat = t.transform(y, x)
    return lat, lon


def write_geojson(features: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False), encoding="utf-8")


def read_geojson(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("features", [])


def arreglar_texto(v):
    """Repara texto UTF-8 que se leyó como Latin-1 (ej. 'trÃ¡mite' -> 'trámite')."""
    if isinstance(v, str) and ("Ã" in v or "Â" in v):
        try:
            return v.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return v
    return v


def feature(geom, props: dict) -> dict:
    props = {k: arreglar_texto(v) for k, v in props.items()}
    return {"type": "Feature", "geometry": mapping(geom), "properties": props}


def to_shape(f: dict):
    return shape(f["geometry"])


def round_geom(geom, nd: int = 6):
    return shp_transform(lambda x, y, z=None: (round(x, nd), round(y, nd)), geom)
