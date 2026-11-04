"""Utilidades compartidas del pipeline de relevamiento de areneras.

Define rutas, logging, el esquema de la base nacional, los valores permitidos,
lectura de configuración y un cliente HTTP "educado" (robots.txt, rate limit,
reintentos con backoff). Todos los scripts importan desde aquí.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import time
import unicodedata
import urllib.robotparser
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import yaml

ROOT = Path(os.environ.get("ARENAS_ROOT", Path(__file__).resolve().parents[1]))
DATA = ROOT / "data"
RAW = DATA / "raw"
PROC = DATA / "processed"
OUT = DATA / "output"
CACHE = DATA / "cache"
LOGS = ROOT / "logs"
TEMPLATES = ROOT / "templates"
CONFIG_FILE = Path(os.environ.get("ARENAS_CONFIG", ROOT / "config" / "config.yaml"))

for _d in (RAW, PROC, OUT, CACHE, LOGS, RAW / "manual"):
    _d.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------------ esquema
COLUMNS = [
    "id_registro", "empresa", "razon_social", "cuit", "establecimiento", "tipo_entidad",
    "provincia", "departamento", "localidad", "latitud", "longitud", "precision_geo",
    "metodo_extraccion", "tipo_material", "uso_previsto", "capacidad_instalada",
    "unidad_capacidad", "produccion_efectiva", "unidad_produccion",
    "instalaciones_procesamiento", "logistica", "clientes_identificables",
    "mercados_atendidos", "antecedentes_petroleo_gas", "sitio_web", "contacto_comercial",
    "fuente_principal", "url_fuente", "url_fuente_2", "fecha_consulta", "estado_dato",
    "nivel_confianza", "duplicado_de", "prioridad_preliminar", "notas",
]
# columnas internas de trabajo (no se exportan a la base para el cliente)
INTERNAL = ["empresa_norm", "origen", "lote"]

ESTADOS = {"confirmado_documental", "declarado_empresa", "estimado", "pendiente_verificacion"}
CONFIANZA = {"alta", "media", "baja"}
PRECISION = {"alta", "media", "baja", ""}
TIPOS_ENTIDAD = {"empresa", "establecimiento", "activo"}
METODOS = {"cantera", "banco", "dragado", "rio", "otro", ""}

PROVINCIAS = [
    "Buenos Aires", "Ciudad Autónoma de Buenos Aires", "Catamarca", "Chaco", "Chubut",
    "Córdoba", "Corrientes", "Entre Ríos", "Formosa", "Jujuy", "La Pampa", "La Rioja",
    "Mendoza", "Misiones", "Neuquén", "Río Negro", "Salta", "San Juan", "San Luis",
    "Santa Cruz", "Santa Fe", "Santiago del Estero", "Tierra del Fuego", "Tucumán",
]
BBOX_AR = {"lat_min": -55.5, "lat_max": -21.0, "lon_min": -73.6, "lon_max": -53.0}
# Añelo (centro operativo de Vaca Muerta) para calcular distancias logísticas
VACA_MUERTA = (-38.354, -68.788)


# ------------------------------------------------------------------ logging
def get_logger(name: str) -> logging.Logger:
    """Logger con salida a consola y a logs/pipeline.log (una línea por evento)."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    fh = logging.FileHandler(LOGS / "pipeline.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.addHandler(fh)
    return logger


def log_error(step: str, detail: str, ref: str = "") -> None:
    """Agrega un error a logs/errores.csv (lo lee el informe de calidad y n8n)."""
    f = LOGS / "errores.csv"
    new = not f.exists()
    with f.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["timestamp", "paso", "detalle", "referencia"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), step, detail, ref])


# ------------------------------------------------------------------ config
def load_env() -> None:
    """Carga .env si existe (sin dependencias externas)."""
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def load_config() -> dict:
    load_env()
    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    cfg["fecha_corte"] = os.environ.get("FECHA_CORTE") or cfg.get("fecha_corte") or date.today().isoformat()
    if os.environ.get("PROVINCIAS"):
        cfg["provincias_prioritarias"] = [p.strip() for p in os.environ["PROVINCIAS"].split(",")]
    return cfg


# ------------------------------------------------------------------ texto
LEGAL_SUFFIX = re.compile(r"\b(s\.?a\.?u?|s\.?r\.?l|s\.?a\.?s|s\.?c\.?a|s\.?h|sociedad anonima|ltda|inc|y cia|& cia)\b\.?", re.I)


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def norm_name(s: str) -> str:
    """Nombre normalizado para comparar: minúsculas, sin acentos ni sufijos societarios."""
    s = strip_accents(str(s or "")).lower()
    s = LEGAL_SUFFIX.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def cuit_valido(cuit: str) -> bool:
    """Verifica formato y dígito verificador de un CUIT argentino."""
    d = re.sub(r"\D", "", str(cuit or ""))
    if len(d) != 11:
        return False
    w = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    s = 11 - sum(int(a) * b for a, b in zip(d[:10], w)) % 11
    dv = 0 if s == 11 else 9 if s == 10 else s
    return dv == int(d[10])


def fmt_cuit(cuit: str) -> str:
    d = re.sub(r"\D", "", str(cuit or ""))
    return f"{d[:2]}-{d[2:10]}-{d[10]}" if len(d) == 11 else ""


# ------------------------------------------------------------------ tablas
def read_table(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = ""
    return df


def write_table(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8")


def append_note(df: pd.DataFrame, idx, note: str) -> None:
    cur = df.at[idx, "notas"]
    if note not in cur:
        df.at[idx, "notas"] = (cur + " | " if cur else "") + note


# ------------------------------------------------------------------ HTTP
class PoliteClient:
    """Cliente HTTP que respeta robots.txt, un rate limit por dominio y reintenta con backoff.

    No evade captchas ni bloqueos: ante 401/403/429 repetidos abandona y lo registra.
    """

    def __init__(self, user_agent: str | None = None, min_interval: float = 2.0, max_retries: int = 3):
        import requests

        self.s = requests.Session()
        self.ua = user_agent or os.environ.get("HTTP_USER_AGENT", "TTConsulting-RelevamientoAreneras/1.0")
        self.s.headers["User-Agent"] = self.ua
        self.min_interval = float(os.environ.get("HTTP_MIN_INTERVAL", min_interval))
        self.max_retries = int(os.environ.get("HTTP_MAX_RETRIES", max_retries))
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.log = get_logger("http")

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.s.get(base + "/robots.txt", timeout=10)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
                self._robots[base] = rp
            except Exception:
                self._robots[base] = None  # sin robots accesible: se permite con prudencia
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.ua, url)

    def get(self, url: str, check_robots: bool = True, **kw):
        if check_robots and not self.allowed(url):
            self.log.warning("robots.txt no permite %s", url)
            log_error("http", "bloqueado por robots.txt", url)
            return None
        host = urlparse(url).netloc
        for attempt in range(1, self.max_retries + 1):
            wait = self.min_interval - (time.time() - self._last.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.time()
            try:
                r = self.s.get(url, timeout=kw.pop("timeout", 25), **kw)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"HTTP {r.status_code}")
                if r.status_code in (401, 403):
                    log_error("http", f"acceso denegado {r.status_code} (no se evade)", url)
                    return None
                return r
            except Exception as e:  # noqa: BLE001
                self.log.warning("intento %s/%s falló para %s: %s", attempt, self.max_retries, url, e)
                time.sleep(2 ** attempt)
        log_error("http", "reintentos agotados", url)
        return None


def save_json(obj, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
