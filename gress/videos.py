"""
Seguimiento de vídeos de AnimaMed.

Tablas (en gress.db):
- videos:        cada vídeo como idea (título, tema, categoría, estructura).
- publicaciones: ese vídeo en cada plataforma (YouTube, y más adelante Instagram y TikTok).
- mediciones:    las estadísticas de cada publicación a una edad fija (1 h, 24 h, 7 d, 28 d).

Medir a edad fija permite comparar vídeos en igualdad de condiciones:
no es justo comparar uno publicado hace 3 días con otro de hace 3 semanas.
"""
import asyncio
import datetime
import logging
from contextlib import closing
from statistics import median

from telegram.ext import ContextTypes

from gress.config import CHAT_ID
from gress.memoria import conectar
from gress import youtube

log = logging.getLogger("gress")

# hito: (horas desde la publicación, margen en horas)
# El margen permite medir un poco tarde (por ejemplo, si el servidor estaba reiniciándose),
# pero evita que un vídeo antiguo descubierto hoy se mida como si tuviera 1 hora.
HITOS = {
    "1h": (1, 3),
    "24h": (24, 6),
    "7d": (168, 24),
    "28d": (672, 48),
}


def iniciar():
    with closing(conectar()) as con, con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                titulo TEXT NOT NULL,
                tema TEXT,
                categoria TEXT,
                estructura TEXT,
                creado TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS publicaciones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id INTEGER NOT NULL REFERENCES videos(id),
                plataforma TEXT NOT NULL,
                id_externo TEXT NOT NULL,
                titulo TEXT,
                publicado_en TEXT NOT NULL,
                duracion_s INTEGER,
                UNIQUE (plataforma, id_externo)
            );

            CREATE TABLE IF NOT EXISTS mediciones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                publicacion_id INTEGER NOT NULL REFERENCES publicaciones(id),
                hito TEXT NOT NULL,
                views INTEGER,
                likes INTEGER,
                comments INTEGER,
                shares INTEGER,
                retencion_pct REAL,
                duracion_media_s REAL,
                medido_en TEXT NOT NULL,
                UNIQUE (publicacion_id, hito)
            );
        """)


def _ahora_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def _fecha(iso):
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _miles(n):
    return f"{n:,}".replace(",", ".")


def _registrar(con, plataforma, v):
    """Registra la publicación si no existía. Devuelve (id de la publicación, es_nueva)."""
    fila = con.execute(
        "SELECT id FROM publicaciones WHERE plataforma = ? AND id_externo = ?",
        (plataforma, v["id"]),
    ).fetchone()
    if fila:
        return fila["id"], False

    ahora = _ahora_utc().isoformat()
    video_id = con.execute(
        "INSERT INTO videos (titulo, creado) VALUES (?, ?)", (v["titulo"], ahora)
    ).lastrowid
    pub_id = con.execute(
        """INSERT INTO publicaciones (video_id, plataforma, id_externo, titulo, publicado_en, duracion_s)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (video_id, plataforma, v["id"], v["titulo"], v["publicado"], v["duracion"]),
    ).lastrowid
    return pub_id, True


def _aviso_24h(con, pub_id, titulo, views):
    """Compara las vistas a 24 h con la mediana de los vídeos anteriores."""
    anteriores = [
        r["views"] for r in con.execute(
            """SELECT m.views FROM mediciones m
               JOIN publicaciones p ON p.id = m.publicacion_id
               WHERE m.hito = '24h' AND p.plataforma = 'youtube' AND m.publicacion_id <> ?""",
            (pub_id,),
        )
    ]
    base = f"{titulo}: {_miles(views)} vistas en 24 h en YouTube."
    if len(anteriores) < 3:
        return base + " (Aún no hay vídeos suficientes para comparar.)"

    # Mediana y no media: un vídeo viral no deforma la comparación.
    med = median(anteriores)
    if med == 0:
        return base
    dif = (views - med) / med * 100
    sentido = "por encima" if dif >= 0 else "por debajo"
    return f"{base} Un {abs(dif):.0f} % {sentido} de tu mediana ({_miles(round(med))})."


def _ciclo_youtube():
    """Detecta vídeos nuevos y guarda las mediciones que tocan. Devuelve mensajes para Telegram."""
    mensajes = []
    ahora = _ahora_utc()
    vids = youtube.ultimos_videos(50)

    with closing(conectar()) as con, con:
        for v in vids:
            pub_id, nueva = _registrar(con, "youtube", v)
            publicado = _fecha(v["publicado"])
            edad_h = (ahora - publicado).total_seconds() / 3600

            # Solo avisa de vídeos recientes (la primera vez se importan todos los antiguos)
            if nueva and edad_h < 48:
                mensajes.append(f"Nuevo vídeo en YouTube: {v['titulo']}")

            hechos = {
                r["hito"] for r in con.execute(
                    "SELECT hito FROM mediciones WHERE publicacion_id = ?", (pub_id,)
                )
            }
            for hito, (horas, margen) in HITOS.items():
                if hito in hechos or not (horas <= edad_h < horas + margen):
                    continue

                retencion_pct = duracion_media = None
                if horas >= 168:  # Analytics tarda en tener datos; solo a partir de 7 días
                    try:
                        retencion_pct, duracion_media = youtube.retencion(
                            v["id"], publicado.date().isoformat(), ahora.date().isoformat()
                        )
                    except Exception:
                        log.exception("No se pudo obtener la retención de %s", v["id"])

                con.execute(
                    """INSERT INTO mediciones
                       (publicacion_id, hito, views, likes, comments, retencion_pct, duracion_media_s, medido_en)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (pub_id, hito, v["views"], v["likes"], v["comments"],
                     retencion_pct, duracion_media, ahora.isoformat()),
                )
                log.info("Medición %s guardada para '%s'", hito, v["titulo"])

                if hito == "24h":
                    mensajes.append(_aviso_24h(con, pub_id, v["titulo"], v["views"]))
    return mensajes


async def comprobar_videos(context: ContextTypes.DEFAULT_TYPE):
    try:
        mensajes = await asyncio.to_thread(_ciclo_youtube)
    except Exception:
        log.exception("Error en el seguimiento de vídeos")
        return
    for m in mensajes:
        await context.bot.send_message(chat_id=CHAT_ID, text=m)


def texto_videos(n=8):
    """Resumen de los últimos vídeos con sus estadísticas actuales (para /videos)."""
    vids = youtube.ultimos_videos(n)
    if not vids:
        return "No hay vídeos en el canal."
    lineas = ["Últimos vídeos en YouTube:"]
    for v in vids:
        fecha = _fecha(v["publicado"]).strftime("%d/%m")
        lineas.append(
            f"• {fecha}  {v['titulo']}\n"
            f"   {_miles(v['views'])} vistas · {_miles(v['likes'])} likes · {_miles(v['comments'])} coment."
        )
    return "\n".join(lineas)
