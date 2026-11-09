"""
Importación manual de estadísticas desde CSV (Instagram y, más adelante, TikTok).

Flujo: exportas el CSV de la plataforma y se lo mandas a Gress por Telegram como archivo.
Gress detecta de qué plataforma es por sus columnas, guarda la última foto de cada
publicación, la enlaza con su vídeo (por la fecha), guarda las mediciones que toquen
y te responde con un resumen.

Como solo hay una lectura al día, las mediciones a 1 h y 7 h no existen para estas
plataformas; las de 24 h, 7 días y 28 días se toman con márgenes más amplios.
"""
import csv
import datetime
import io
import json
import logging
import re
from contextlib import closing
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes

from gress.memoria import conectar
from gress import videos

log = logging.getLogger("gress")

# Meta exporta las horas de publicación en hora del Pacífico (sede de Meta).
# Lo hemos comprobado: tus vídeos de las 00:00 en Madrid salen a las 15:00 del día anterior.
HORA_META = ZoneInfo("America/Los_Angeles")

# hito: (horas desde la publicación, margen). Márgenes amplios por ser una lectura al día.
HITOS_IMPORTADOS = {
    "24h": (24, 24),
    "7d": (168, 48),
    "28d": (672, 72),
}

MAX_BYTES = 5 * 1024 * 1024  # Un CSV de estadísticas no debería pasar de unos pocos KB


def iniciar():
    with closing(conectar()) as con, con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS importados (
                plataforma TEXT NOT NULL,
                id_externo TEXT NOT NULL,
                datos TEXT NOT NULL,
                importado_en TEXT NOT NULL,
                PRIMARY KEY (plataforma, id_externo)
            )
        """)


def plataformas_importadas():
    try:
        with closing(conectar()) as con:
            return [r["plataforma"] for r in con.execute("SELECT DISTINCT plataforma FROM importados")]
    except Exception:
        return []  # La tabla aún no existe


def ultimos(plataforma, n=50):
    """Últimos datos importados de una plataforma, del más nuevo al más viejo (mismo formato que la API)."""
    with closing(conectar()) as con:
        filas = con.execute(
            "SELECT datos, importado_en FROM importados WHERE plataforma = ?", (plataforma,)
        ).fetchall()
    pubs = []
    for f in filas:
        d = json.loads(f["datos"])
        d["importado_en"] = f["importado_en"]
        pubs.append(d)
    pubs.sort(key=lambda x: x["publicado"], reverse=True)
    return pubs[:n]


# ---------------------------------------------------------------------------
# Lectores de cada formato
# ---------------------------------------------------------------------------

def _entero(valor):
    try:
        return int(str(valor).replace(",", "").replace(".", "").strip())
    except (TypeError, ValueError):
        return None


def _titulo(descripcion):
    if not descripcion:
        return "(sin texto)"
    primera = descripcion.strip().split("\n")[0].strip()
    return primera if len(primera) <= 80 else primera[:77] + "..."


def _es_instagram(columnas):
    return {"Post ID", "Publish time", "Views", "Post type"} <= set(columnas)


def _leer_instagram(filas):
    pubs = []
    for f in filas:
        if "reel" not in (f.get("Post type") or "").lower():
            continue
        local = datetime.datetime.strptime(f["Publish time"], "%m/%d/%Y %H:%M").replace(tzinfo=HORA_META)
        publicado = local.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        pubs.append({
            "id": f["Post ID"].strip(),
            "titulo": _titulo(f.get("Description")),
            "publicado": publicado,
            "duracion": _entero(f.get("Duration (sec)")),
            "views": _entero(f.get("Views")),
            "reach": _entero(f.get("Reach")),
            "likes": _entero(f.get("Likes")),
            "comments": _entero(f.get("Comments")),
            "shares": _entero(f.get("Shares")),
            "saved": _entero(f.get("Saves")),
            "follows": _entero(f.get("Follows")),
            "media_vista_s": None,
            "skip_rate": None,
        })
    return pubs


def _fin_del_periodo(nombre_archivo, ahora):
    """
    Meta pone el periodo en el nombre del archivo: 'Oct-12-2026_Nov-08-2026_....csv'.
    Los datos llegan hasta el final de ese día (hora del Pacífico). Si no se puede leer, 'ahora'.
    """
    fechas = re.findall(r"([A-Z][a-z]{2}-\d{2}-\d{4})", nombre_archivo or "")
    if len(fechas) >= 2:
        try:
            fin = datetime.datetime.strptime(fechas[1], "%b-%d-%Y").replace(
                hour=23, minute=59, tzinfo=HORA_META
            )
            return min(fin.astimezone(datetime.timezone.utc), ahora)
        except ValueError:
            pass
    return ahora


# ---------------------------------------------------------------------------
# Importar
# ---------------------------------------------------------------------------

def importar(texto, nombre_archivo=""):
    """Importa el contenido de un CSV. Devuelve el texto de respuesta para Telegram."""
    lector = csv.DictReader(io.StringIO(texto))
    columnas = lector.fieldnames or []
    filas = list(lector)

    if _es_instagram(columnas):
        plataforma = "instagram"
        pubs = _leer_instagram(filas)
    elif {"Date", "Video Views"} <= set(columnas) and "Post ID" not in columnas:
        return ("Este CSV es el resumen diario de la cuenta (Overview), no el de cada vídeo. "
                "En TikTok Studio exporta la sección de Contenido.")
    else:
        return "No reconozco este CSV. Columnas: " + ", ".join(columnas[:12])

    if not pubs:
        return "El archivo no tiene reels."

    ahora = videos._ahora_utc()
    medido_en = _fin_del_periodo(nombre_archivo, ahora)
    nombre = videos.NOMBRES[plataforma]
    nuevos, cambios, avisos = 0, [], []

    with closing(conectar()) as con, con:
        arranque = con.execute(
            "SELECT COUNT(*) FROM importados WHERE plataforma = ?", (plataforma,)
        ).fetchone()[0] == 0

        for v in pubs:
            anterior = con.execute(
                "SELECT datos FROM importados WHERE plataforma = ? AND id_externo = ?",
                (plataforma, v["id"]),
            ).fetchone()
            con.execute(
                """INSERT INTO importados (plataforma, id_externo, datos, importado_en) VALUES (?, ?, ?, ?)
                   ON CONFLICT (plataforma, id_externo) DO UPDATE
                   SET datos = excluded.datos, importado_en = excluded.importado_en""",
                (plataforma, v["id"], json.dumps(v), medido_en.isoformat()),
            )

            pub_id, video_id, pub_nueva, _ = videos._registrar(con, plataforma, v)
            if pub_nueva:
                nuevos += 1
            if anterior and v["views"] is not None:
                antes = json.loads(anterior["datos"]).get("views") or 0
                if v["views"] != antes:
                    cambios.append((v["views"] - antes, v["titulo"], v["views"]))

            aviso = videos._comprobar_umbrales(
                con, plataforma, pub_id, v["titulo"], v["views"],
                silencioso=arranque or pub_nueva, ahora=medido_en,
            )
            if aviso:
                avisos.append(aviso)

            # Mediciones a edad fija que toquen con esta lectura
            edad_h = (medido_en - videos._fecha(v["publicado"])).total_seconds() / 3600
            hechos = {r["hito"] for r in con.execute(
                "SELECT hito FROM mediciones WHERE publicacion_id = ?", (pub_id,)
            )}
            for hito, (horas, margen) in HITOS_IMPORTADOS.items():
                if hito not in hechos and horas <= edad_h < horas + margen:
                    videos._medir(con, plataforma, pub_id, video_id, v, hito, horas, medido_en)

    partes = [f"{nombre}: {len(pubs)} reels importados"
              + (f" ({nuevos} nuevos)." if nuevos else ".")]
    if arranque:
        partes.append("Primera importación: a partir de la próxima te diré cuánto ha crecido cada uno.")
    elif cambios:
        cambios.sort(reverse=True)
        partes.append("Lo que más ha crecido desde el último CSV:")
        for dif, titulo, total in cambios[:5]:
            partes.append(f"• {titulo}: +{videos._miles(dif)} (total {videos._miles(total)})")
    else:
        partes.append("Sin cambios desde el último CSV.")
    if avisos:
        partes += [""] + avisos
    return "\n".join(partes)


async def recibir_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if doc.file_size and doc.file_size > MAX_BYTES:
        await update.message.reply_text("El archivo es demasiado grande para ser un CSV de estadísticas.")
        return
    archivo = await doc.get_file()
    contenido = bytes(await archivo.download_as_bytearray())
    try:
        texto = contenido.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = contenido.decode("latin-1")
    try:
        respuesta = importar(texto, doc.file_name or "")
    except Exception:
        log.exception("Error importando %s", doc.file_name)
        respuesta = "No he podido importar el archivo. Mira los logs."
    await update.message.reply_text(respuesta)
