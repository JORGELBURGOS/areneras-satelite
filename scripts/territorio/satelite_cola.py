"""satelite_cola.py — Barrido satelital progresivo: toma cuadrantes de la cola, los analiza con Sentinel-2
y guarda lo que encuentra. Pensado para correr varias veces por día en GitHub Actions sin pisarse.

Cómo evita problemas:
  - Toma los cuadrantes de a uno (los de más prioridad primero) con bloqueo en la base: dos corridas
    simultáneas nunca agarran el mismo.
  - Guarda cada cuadrante apenas termina: si algo se corta, se pierde solo ese cuadrante.
  - Tiene tope de tiempo por cuadrante y por corrida; lo que no llega queda para la próxima.
  - Un cuadrante que falla 3 veces queda marcado con error y no traba la cola.

Necesita SATELITE_DATABASE_URL (rol satelite_publico: solo ve la cola de cuadrantes y las detecciones).
Uso:
  python scripts/territorio/satelite_cola.py --minutos 320
  python scripts/territorio/satelite_cola.py --minutos 20 --max 1        (prueba con un cuadrante)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

from geo_common import SITE_COLS, area_ha, get_logger, load_config, round_geom, site_id
from sites_satellite import DEFAULTS, run_tile, thumbnail

log = get_logger("satelite_cola")


class Tope(Exception):
    pass


def _alarma(signum, frame):
    raise Tope("el cuadrante superó el tiempo máximo")


def analizar(t: dict, anio: int, p: dict, tmp: Path) -> list[tuple]:
    bbox = (float(t["lon_min"]), float(t["lat_min"]), float(t["lon_max"]), float(t["lat_max"]))
    res, rgbs = run_tile(bbox, anio, p)
    filas = []
    for g, estado, delta, sl in res:
        ha = area_ha(g)
        if ha < p["area_min_ha"]:
            continue
        c = g.representative_point()
        sid = site_id("satelite", f"{round(c.x, 4)},{round(c.y, 4)}")
        png = None
        try:
            pad = 15
            big = (slice(max(sl[0].start - pad, 0), sl[0].stop + pad), slice(max(sl[1].start - pad, 0), sl[1].stop + pad))
            f = tmp / f"{sid}.png"
            thumbnail(rgbs[0], rgbs[1], big, f)
            png = base64.b64encode(f.read_bytes()).decode()
        except Exception:  # noqa: BLE001
            pass
        props = {k: "" for k in SITE_COLS}
        props.update({
            "id_sitio": sid, "origen": "satelite", "origen_ref": f"Sentinel-2 {anio}, cuadrante {t['id']}", "tipo_sitio": "desconocido",
            "material": "desconocido", "provincia": t.get("provincia") or "", "lat": round(c.y, 6), "lon": round(c.x, 6),
            "area_ha": round(ha, 2), "estado_actividad": estado, "fecha_evidencia": str(anio), "verificado": "pendiente",
            "url_fuente": "Copernicus Sentinel-2 L2A (Microsoft Planetary Computer)", "fecha_consulta": date.today().isoformat(),
            "notas": " ".join(x for x in ["Suelo desnudo claro durante al menos el 80 % de los meses del año.",
                                          f"Creció un {delta:.0%} respecto de {anio - 2}." if estado == "creciendo" else "",
                                          "Apareció después de " + str(anio - 2) + "." if estado == "nuevo" else "",
                                          "Superficie grande: puede ser médano, salina o cauce." if ha > p["area_max_ha"] else ""] if x),
        })
        filas.append((sid, t["id"], json.dumps(props, ensure_ascii=False), json.dumps(round_geom(g, 6).__geo_interface__), png))
    return filas


def limpiar_url(v: str) -> str:
    """Tolera cómo se haya pegado el secreto: espacios, comillas, saltos de línea, sin el prefijo postgresql://."""
    import re
    v = re.sub(r"\s+", "", v or "").strip("`'\"")  # una clave pegada en dos renglones queda en uno
    m = re.search(r"postgres(?:ql)?://\S+", v)
    if m:
        return m.group(0).rstrip("`'\"")
    m = re.search(r"[\w.-]+:[^@\s]+@[\w.-]+(?::\d+)?/\S*", v)
    return ("postgresql://" + m.group(0)) if m else v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutos", type=float, default=float(os.environ.get("MINUTOS", 320)), help="tiempo total de la corrida")
    ap.add_argument("--max", type=int, default=0, help="máximo de cuadrantes (0 = sin límite)")
    ap.add_argument("--anio", type=int, default=int(os.environ.get("ANIO") or date.today().year - 1))
    ap.add_argument("--tope-cuadrante", type=float, default=float(os.environ.get("TOPE_CUADRANTE_MIN", 25)))
    a = ap.parse_args()
    url = limpiar_url(os.environ.get("SATELITE_DATABASE_URL", ""))
    if not url:
        print("::error::Falta el secreto SATELITE_DATABASE_URL (Settings > Secrets and variables > Actions).")
        return 1
    import psycopg
    p = {**DEFAULTS, **load_config().get("territorio", {}).get("satelite", {})}
    fin = time.time() + a.minutos * 60
    prom = 6.0  # minutos estimados por cuadrante hasta medir
    hechos = errores = total_det = 0
    signal.signal(signal.SIGALRM, _alarma)
    try:
        cx = psycopg.connect(url, autocommit=True, connect_timeout=20)
    except psycopg.OperationalError:
        # el pooler de Supabase en São Paulo tiene dos direcciones; si una no responde se prueba la otra
        alt = url.replace("aws-1-", "aws-0-") if "aws-1-" in url else url.replace("aws-0-", "aws-1-")
        cx = psycopg.connect(alt, autocommit=True, connect_timeout=20)
    with cx, cx.cursor() as cur, tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        while True:
            if a.max and hechos + errores >= a.max:
                break
            if time.time() + prom * 60 * 1.3 > fin:
                log.info("no alcanza el tiempo para otro cuadrante (promedio %.1f min): fin de la corrida", prom)
                break
            cur.execute("select id, lon_min, lat_min, lon_max, lat_max, provincia, prioridad from tomar_teselas(1)")
            r = cur.fetchone()
            if not r:
                log.info("no quedan cuadrantes pendientes")
                break
            t = dict(zip(["id", "lon_min", "lat_min", "lon_max", "lat_max", "provincia", "prioridad"], r))
            t0 = time.time()
            signal.alarm(int(a.tope_cuadrante * 60))
            try:
                filas = analizar(t, a.anio, p, tmp)
                signal.alarm(0)
                cur.execute("delete from satelite_detecciones where id_tesela = %s", (t["id"],))
                for sid, tid, props, gj, png in filas:
                    cur.execute("insert into satelite_detecciones (id_sitio, id_tesela, props, geom, png_base64) values "
                                "(%s,%s,%s::jsonb, extensions.st_setsrid(extensions.st_geomfromgeojson(%s),4326), %s) "
                                "on conflict (id_sitio) do update set id_tesela=excluded.id_tesela, props=excluded.props, geom=excluded.geom, "
                                "png_base64=coalesce(excluded.png_base64, satelite_detecciones.png_base64), detectado_en=now()",
                                (sid, tid, props, gj, png))
                mins = (time.time() - t0) / 60
                cur.execute("update teselas set estado='hecha', procesada_en=now(), anio=%s, detecciones=%s, minutos=%s, error=null, "
                            "proximo_en = now() + case when prioridad >= 70 then interval '180 days' else interval '365 days' end where id=%s",
                            (a.anio, len(filas), round(mins, 1), t["id"]))
                hechos += 1
                total_det += len(filas)
                prom = mins if hechos == 1 else prom * 0.7 + mins * 0.3
                log.info("cuadrante %s (%s, prioridad %s): %s candidatos en %.1f min", t["id"], t["provincia"], t["prioridad"], len(filas), mins)
            except Exception as e:  # noqa: BLE001
                signal.alarm(0)
                errores += 1
                msg = f"{type(e).__name__}: {str(e)[:300]}"
                cur.execute("update teselas set estado='error', error=%s, minutos=%s where id=%s", (msg, round((time.time() - t0) / 60, 1), t["id"]))
                log.warning("cuadrante %s con error: %s", t["id"], msg)
    log.info("corrida terminada: %s cuadrantes analizados, %s con error, %s candidatos", hechos, errores, total_det)
    resumen = Path(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"))
    try:
        with resumen.open("a", encoding="utf-8") as f:
            f.write(f"### Barrido satelital\n\n- Cuadrantes analizados: **{hechos}**\n- Con error: {errores}\n- Candidatos encontrados: {total_det}\n- Promedio: {prom:.1f} min por cuadrante\n")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
