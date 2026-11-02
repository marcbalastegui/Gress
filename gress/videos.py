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

from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from gress.config import CHAT_ID
from gress.memoria import conectar
from gress import youtube
from gress.interprete import DISPARADORES

log = logging.getLogger("gress")
MADRID = ZoneInfo("Europe/Madrid")

CATEGORIAS = ["Curiosidad", "Psicología", "Filosofía", "Historia"]
CORTE_FORMATO_S = 45  # hasta 45 s = formato corto (40 s); más = formato largo (~55 s)

# hito: (horas desde la publicación, margen en horas)
# El margen permite medir un poco tarde (por ejemplo, si el servidor estaba reiniciándose),
# pero evita que un vídeo antiguo descubierto hoy se mida como si tuviera 1 hora.
HITOS = {
    "1h": (1, 3),
    "7h": (7, 3),   # para comparar el vídeo del día en el resumen de las 7:30
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
    """Registra la publicación si no existía. Devuelve (id publicación, id vídeo, es_nueva)."""
    fila = con.execute(
        "SELECT id, video_id FROM publicaciones WHERE plataforma = ? AND id_externo = ?",
        (plataforma, v["id"]),
    ).fetchone()
    if fila:
        return fila["id"], fila["video_id"], False

    ahora = _ahora_utc().isoformat()
    video_id = con.execute(
        "INSERT INTO videos (titulo, creado) VALUES (?, ?)", (v["titulo"], ahora)
    ).lastrowid
    pub_id = con.execute(
        """INSERT INTO publicaciones (video_id, plataforma, id_externo, titulo, publicado_en, duracion_s)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (video_id, plataforma, v["id"], v["titulo"], v["publicado"], v["duracion"]),
    ).lastrowid
    return pub_id, video_id, True


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
    """
    Detecta vídeos nuevos y guarda las mediciones que tocan.
    Devuelve una lista de (texto, video_id): si video_id no es None, el mensaje lleva
    los botones para elegir la categoría de ese vídeo.
    """
    mensajes = []
    ahora = _ahora_utc()
    vids = youtube.ultimos_videos(50)

    with closing(conectar()) as con, con:
        for v in vids:
            pub_id, video_id, nueva = _registrar(con, "youtube", v)
            publicado = _fecha(v["publicado"])
            edad_h = (ahora - publicado).total_seconds() / 3600

            # Solo avisa de vídeos recientes (la primera vez se importan todos los antiguos)
            if nueva and edad_h < 48:
                mensajes.append((f"Nuevo vídeo en YouTube: {v['titulo']}\n¿Categoría?", video_id))

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
                    mensajes.append((_aviso_24h(con, pub_id, v["titulo"], v["views"]), None))
    return mensajes


async def comprobar_videos(context: ContextTypes.DEFAULT_TYPE):
    try:
        mensajes = await asyncio.to_thread(_ciclo_youtube)
    except Exception:
        log.exception("Error en el seguimiento de vídeos")
        return
    for texto, video_id in mensajes:
        teclado = _teclado_categorias(video_id) if video_id else None
        await context.bot.send_message(chat_id=CHAT_ID, text=texto, reply_markup=teclado)


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


# ---------------------------------------------------------------------------
# Etiquetas: categoría (botones) y tema (texto)
# ---------------------------------------------------------------------------

def _teclado_categorias(video_id):
    botones = [
        InlineKeyboardButton(c, callback_data=f"cat:{video_id}:{i}")
        for i, c in enumerate(CATEGORIAS)
    ]
    return InlineKeyboardMarkup([botones[:2], botones[2:]])


def _guardar(video_id, campo, valor):
    if campo not in ("categoria", "tema"):
        raise ValueError(f"Campo no permitido: {campo}")
    with closing(conectar()) as con, con:
        con.execute(f"UPDATE videos SET {campo} = ? WHERE id = ?", (valor, video_id))


def _siguiente_sin_etiquetar():
    with closing(conectar()) as con:
        return con.execute(
            """SELECT v.id, v.titulo FROM videos v
               JOIN publicaciones p ON p.video_id = v.id
               WHERE v.categoria IS NULL
               ORDER BY p.publicado_en DESC LIMIT 1"""
        ).fetchone()


async def _ofrecer_siguiente(context: ContextTypes.DEFAULT_TYPE):
    fila = _siguiente_sin_etiquetar()
    if not fila:
        context.bot_data["modo_etiquetar"] = False
        await context.bot.send_message(chat_id=CHAT_ID, text="No quedan vídeos sin etiquetar.")
        return
    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=f"{fila['titulo']}\n¿Categoría?",
        reply_markup=_teclado_categorias(fila["id"]),
    )


async def etiquetar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/etiquetar: va ofreciendo, uno a uno, los vídeos que aún no tienen categoría."""
    context.bot_data["modo_etiquetar"] = True
    await _ofrecer_siguiente(context)


async def elegir_categoria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Se ejecuta al pulsar un botón de categoría."""
    q = update.callback_query
    if update.effective_chat is None or update.effective_chat.id != CHAT_ID:
        await q.answer()
        return

    _, video_id, indice = q.data.split(":")
    video_id = int(video_id)
    categoria = CATEGORIAS[int(indice)]
    _guardar(video_id, "categoria", categoria)

    await q.answer(f"Categoría: {categoria}")
    titulo = q.message.text.split("\n")[0]
    await q.edit_message_text(f"{titulo}\nCategoría: {categoria}")

    context.bot_data["tema_pendiente"] = video_id
    await context.bot.send_message(
        chat_id=CHAT_ID,
        text="¿Cuál es el tema? Escríbelo en pocas palabras, o '-' para dejarlo vacío.",
    )


async def recibir_tema(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Si Gress está esperando el tema de un vídeo, lo guarda y devuelve True.
    Devuelve False si el mensaje no era un tema (para que el bot lo trate como siempre).
    """
    video_id = context.bot_data.get("tema_pendiente")
    texto = update.message.text.strip()
    if video_id is None or DISPARADORES.match(texto):
        return False

    del context.bot_data["tema_pendiente"]
    if texto != "-":
        _guardar(video_id, "tema", texto)
        await update.message.reply_text(f"Tema guardado: {texto}")
    else:
        await update.message.reply_text("Sin tema.")

    if context.bot_data.get("modo_etiquetar"):
        await _ofrecer_siguiente(context)
    return True


# ---------------------------------------------------------------------------
# Vídeo del día (para el resumen de las 7:30)
# ---------------------------------------------------------------------------

def texto_video_hoy():
    """Cifras del vídeo publicado hoy, comparadas con tus vídeos a la misma edad. None si no hay."""
    hoy = datetime.datetime.now(MADRID).date()
    ahora = _ahora_utc()
    de_hoy = [
        v for v in youtube.ultimos_videos(5)
        if _fecha(v["publicado"]).astimezone(MADRID).date() == hoy
    ]
    if not de_hoy:
        return None

    lineas = []
    with closing(conectar()) as con:
        for v in de_hoy:
            horas = (ahora - _fecha(v["publicado"])).total_seconds() / 3600
            linea = (
                f"Tu vídeo de hoy, «{v['titulo']}»: {_miles(v['views'])} vistas, "
                f"{_miles(v['likes'])} likes y {_miles(v['comments'])} comentarios en {horas:.0f} h."
            )
            anteriores = [
                r["views"] for r in con.execute(
                    """SELECT m.views FROM mediciones m
                       JOIN publicaciones p ON p.id = m.publicacion_id
                       WHERE m.hito = '7h' AND p.plataforma = 'youtube' AND p.id_externo <> ?""",
                    (v["id"],),
                )
            ]
            if len(anteriores) >= 3:
                med = median(anteriores)
                if med:
                    dif = (v["views"] - med) / med * 100
                    sentido = "por encima" if dif >= 0 else "por debajo"
                    linea += f" Un {abs(dif):.0f} % {sentido} de tu mediana a esta hora ({_miles(round(med))})."
            lineas.append(linea)
    return "\n".join(lineas)


# ---------------------------------------------------------------------------
# Resumen semanal (domingo a las 23:00)
# ---------------------------------------------------------------------------

def _mediana_txt(valores, sufijo=""):
    valores = [x for x in valores if x is not None]
    if not valores:
        return "sin datos"
    m = median(valores)
    return (f"{m:.0f}{sufijo}" if sufijo else _miles(round(m))) + f" (n={len(valores)})"


def texto_semanal():
    ahora = _ahora_utc()
    desde = (ahora - datetime.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    with closing(conectar()) as con:
        semana = con.execute(
            """SELECT v.titulo,
                      (SELECT m.views FROM mediciones m
                       WHERE m.publicacion_id = p.id AND m.hito = '24h') AS v24
               FROM publicaciones p JOIN videos v ON v.id = p.video_id
               WHERE p.plataforma = 'youtube' AND p.publicado_en >= ?
               ORDER BY p.publicado_en""",
            (desde,),
        ).fetchall()

        historico = con.execute(
            """SELECT v.categoria, p.duracion_s, m.hito, m.views, m.retencion_pct
               FROM mediciones m
               JOIN publicaciones p ON p.id = m.publicacion_id
               JOIN videos v ON v.id = p.video_id
               WHERE p.plataforma = 'youtube' AND m.hito IN ('24h', '7d')"""
        ).fetchall()

    partes = ["Resumen semanal de AnimaMed (YouTube)", ""]

    # La semana
    if semana:
        partes.append(f"Esta semana: {len(semana)} vídeo(s).")
        con_24h = [r for r in semana if r["v24"] is not None]
        for r in semana:
            cifra = f"{_miles(r['v24'])} vistas a las 24 h" if r["v24"] is not None else "aún sin 24 h"
            partes.append(f"• {r['titulo']}: {cifra}")
        if len(con_24h) >= 2:
            mejor = max(con_24h, key=lambda r: r["v24"])
            peor = min(con_24h, key=lambda r: r["v24"])
            partes += ["", f"Mejor: {mejor['titulo']}", f"Peor: {peor['titulo']}"]
    else:
        partes.append("Esta semana no has publicado.")

    # Histórico por categoría y por formato
    def grupo(clave):
        datos = {}
        for r in historico:
            k = clave(r)
            d = datos.setdefault(k, {"v24": [], "ret": []})
            if r["hito"] == "24h":
                d["v24"].append(r["views"])
            elif r["hito"] == "7d":
                d["ret"].append(r["retencion_pct"])
        return datos

    por_categoria = grupo(lambda r: r["categoria"] or "Sin categoría")
    por_formato = grupo(
        lambda r: "Corto (≤45 s)" if (r["duracion_s"] or 0) <= CORTE_FORMATO_S else "Largo (>45 s)"
    )

    for titulo, datos in (("Por categoría", por_categoria), ("Por formato", por_formato)):
        if not datos:
            continue
        partes += ["", f"{titulo} (mediana de vistas a 24 h · retención a 7 días):"]
        for k, d in sorted(datos.items()):
            partes.append(f"• {k}: {_mediana_txt(d['v24'])} · {_mediana_txt(d['ret'], ' %')}")

    return "\n".join(partes)


async def resumen_semanal(context: ContextTypes.DEFAULT_TYPE):
    try:
        texto = await asyncio.to_thread(texto_semanal)
    except Exception:
        log.exception("Error preparando el resumen semanal de vídeos")
        return
    await context.bot.send_message(chat_id=CHAT_ID, text=texto)
