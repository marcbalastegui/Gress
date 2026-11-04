"""
Seguimiento de vídeos de AnimaMed en varias plataformas (YouTube e Instagram).

Tablas (en gress.db):
- videos:        cada vídeo como idea (título, tema, categoría).
- publicaciones: ese vídeo en cada plataforma. Un mismo vídeo puede tener
                 una publicación en YouTube y otra en Instagram.
- mediciones:    las estadísticas de cada publicación a una edad fija (1 h, 7 h, 24 h, 7 d, 28 d).

Medir a edad fija permite comparar vídeos en igualdad de condiciones:
no es justo comparar uno publicado hace 3 días con otro de hace 3 semanas.

Enlace automático: si en una plataforma aparece una publicación el mismo día
(hora de Madrid) que otra de otra plataforma, se consideran el mismo vídeo.
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
from gress import youtube, instagram
from gress.interprete import DISPARADORES

log = logging.getLogger("gress")
MADRID = ZoneInfo("Europe/Madrid")

CATEGORIAS = ["Curiosidad", "Psicología", "Filosofía", "Historia"]
CORTE_FORMATO_S = 45  # hasta 45 s = formato corto (40 s); más = formato largo (~55 s)
NOMBRES = {"youtube": "YouTube", "instagram": "Instagram"}

# Hitos de vistas: aviso cuando una publicación cruza cada umbral
UMBRALES_VISTAS = [1_000, 5_000, 10_000, 50_000, 100_000, 1_000_000]

# Hitos de suscriptores del canal de YouTube
UMBRALES_SUSCRIPTORES = [100, 250, 500, 1_000, 2_500, 5_000, 10_000, 25_000, 50_000, 100_000]

# Requisitos del Programa de Partners de YouTube (comprobados en noviembre de 2026).
# Desde el 1 de febrero de 2027, el nivel completo por Shorts pasa a 20 millones de vistas.
YPP_BASICO = {"suscriptores": 500, "shorts_90d": 3_000_000, "subidas_90d": 3}
YPP_COMPLETO = {"suscriptores": 1_000, "shorts_90d": 10_000_000}
YPP_COMPLETO_2027 = {"suscriptores": 1_000, "shorts_90d": 20_000_000}
CAMBIO_2027 = datetime.date(2027, 2, 1)
UMBRALES_SHORTS_90D = [1_000_000, 3_000_000, 5_000_000, 10_000_000, 20_000_000]

# Despegue: un vídeo de más de 48 h que gana en una hora muchas más vistas de lo normal
DESPEGUE_EDAD_MIN_H = 48
DESPEGUE_MIN_VISTAS_HORA = 300
DESPEGUE_FACTOR = 5

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


def _fuentes():
    """Plataformas activas y la función que da sus últimas publicaciones."""
    fuentes = {"youtube": lambda: youtube.ultimos_videos(50)}
    if instagram.configurado():
        fuentes["instagram"] = lambda: instagram.ultimos_reels(35)
    return fuentes


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
        con.execute("""
            CREATE TABLE IF NOT EXISTS hitos_vistas (
                publicacion_id INTEGER NOT NULL REFERENCES publicaciones(id),
                umbral INTEGER NOT NULL,
                alcanzado_en TEXT NOT NULL,
                PRIMARY KEY (publicacion_id, umbral)
            )
        """)
        con.executescript("""
            CREATE TABLE IF NOT EXISTS hitos_canal (
                plataforma TEXT NOT NULL,
                metrica TEXT NOT NULL,
                umbral INTEGER NOT NULL,
                alcanzado_en TEXT NOT NULL,
                PRIMARY KEY (plataforma, metrica, umbral)
            );
            CREATE TABLE IF NOT EXISTS lecturas (
                publicacion_id INTEGER NOT NULL REFERENCES publicaciones(id),
                leido_en TEXT NOT NULL,
                views INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_lecturas ON lecturas (publicacion_id, leido_en);
            CREATE TABLE IF NOT EXISTS despegues (
                publicacion_id INTEGER NOT NULL REFERENCES publicaciones(id),
                avisado_en TEXT NOT NULL
            );
        """)
        # Columnas añadidas con Instagram. ALTER TABLE solo si no existen,
        # para que funcione también con la base de datos que ya está en el servidor.
        existentes = {r["name"] for r in con.execute("PRAGMA table_info(mediciones)")}
        for col, tipo in (("saved", "INTEGER"), ("reach", "INTEGER"), ("skip_rate", "REAL")):
            if col not in existentes:
                con.execute(f"ALTER TABLE mediciones ADD COLUMN {col} {tipo}")


def _ahora_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def _fecha(iso):
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _dia_madrid(iso):
    return _fecha(iso).astimezone(MADRID).date()


def _miles(n):
    return "?" if n is None else f"{n:,}".replace(",", ".")


# ---------------------------------------------------------------------------
# Registro y enlace entre plataformas
# ---------------------------------------------------------------------------

def _buscar_pareja(con, plataforma, v):
    """
    Busca un vídeo publicado el mismo día en otra plataforma que todavía no tenga
    publicación en esta. Devuelve su id solo si hay exactamente uno (si hay dudas, no enlaza).
    """
    dia = _dia_madrid(v["publicado"])
    candidatos = set()
    for r in con.execute(
        """SELECT p.video_id, p.publicado_en FROM publicaciones p
           WHERE p.plataforma <> ?
             AND NOT EXISTS (SELECT 1 FROM publicaciones q
                             WHERE q.video_id = p.video_id AND q.plataforma = ?)""",
        (plataforma, plataforma),
    ):
        if _dia_madrid(r["publicado_en"]) == dia:
            candidatos.add(r["video_id"])
    return candidatos.pop() if len(candidatos) == 1 else None


def _registrar(con, plataforma, v):
    """
    Registra la publicación si no existía.
    Devuelve (id publicación, id vídeo, publicación_nueva, vídeo_nuevo).
    """
    fila = con.execute(
        "SELECT id, video_id FROM publicaciones WHERE plataforma = ? AND id_externo = ?",
        (plataforma, v["id"]),
    ).fetchone()
    if fila:
        return fila["id"], fila["video_id"], False, False

    video_id = _buscar_pareja(con, plataforma, v)
    video_nuevo = video_id is None
    if video_nuevo:
        video_id = con.execute(
            "INSERT INTO videos (titulo, creado) VALUES (?, ?)",
            (v["titulo"], _ahora_utc().isoformat()),
        ).lastrowid

    pub_id = con.execute(
        """INSERT INTO publicaciones (video_id, plataforma, id_externo, titulo, publicado_en, duracion_s)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (video_id, plataforma, v["id"], v["titulo"], v["publicado"], v["duracion"]),
    ).lastrowid
    if not video_nuevo:
        log.info("%s '%s' enlazado con el vídeo %s", NOMBRES[plataforma], v["titulo"], video_id)
    return pub_id, video_id, True, video_nuevo


def _duracion_video(con, video_id):
    """Duración del vídeo según cualquier plataforma que la conozca (en la práctica, YouTube)."""
    fila = con.execute(
        "SELECT MAX(duracion_s) AS d FROM publicaciones WHERE video_id = ?", (video_id,)
    ).fetchone()
    return fila["d"] if fila else None


# ---------------------------------------------------------------------------
# Ciclo de medición (cada hora)
# ---------------------------------------------------------------------------

def _mediana_anteriores(con, plataforma, hito, excluir_pub_id):
    return [
        r["views"] for r in con.execute(
            """SELECT m.views FROM mediciones m
               JOIN publicaciones p ON p.id = m.publicacion_id
               WHERE m.hito = ? AND p.plataforma = ? AND m.publicacion_id <> ?
                 AND m.views IS NOT NULL""",
            (hito, plataforma, excluir_pub_id),
        )
    ]


def _comparacion(views, anteriores, texto_mediana):
    """Texto ' Un X % por encima/debajo de ...' o '' si no hay base para comparar."""
    if views is None or len(anteriores) < 3:
        return ""
    med = median(anteriores)  # Mediana y no media: un vídeo viral no deforma la comparación
    if not med:
        return ""
    dif = (views - med) / med * 100
    sentido = "por encima" if dif >= 0 else "por debajo"
    return f" Un {abs(dif):.0f} % {sentido} de {texto_mediana} ({_miles(round(med))})."


def _aviso_24h(con, plataforma, pub_id, titulo, views):
    base = f"{titulo}: {_miles(views)} vistas en 24 h en {NOMBRES[plataforma]}."
    anteriores = _mediana_anteriores(con, plataforma, "24h", pub_id)
    if len(anteriores) < 3:
        return base + " (Aún no hay vídeos suficientes para comparar.)"
    return base + _comparacion(views, anteriores, "tu mediana")


def _medir(con, plataforma, pub_id, video_id, v, hito, horas, ahora):
    retencion_pct = duracion_media = None

    if plataforma == "youtube" and horas >= 168:
        # YouTube Analytics tarda uno o dos días en tener datos: solo a partir de 7 días
        publicado = _fecha(v["publicado"])
        try:
            retencion_pct, duracion_media = youtube.retencion(
                v["id"], publicado.date().isoformat(), ahora.date().isoformat()
            )
        except Exception:
            log.exception("No se pudo obtener la retención de %s", v["id"])

    if plataforma == "instagram" and v.get("media_vista_s") is not None:
        duracion_media = v["media_vista_s"]
        duracion = _duracion_video(con, video_id)
        if duracion:
            retencion_pct = duracion_media / duracion * 100

    con.execute(
        """INSERT INTO mediciones
           (publicacion_id, hito, views, likes, comments, shares, saved, reach, skip_rate,
            retencion_pct, duracion_media_s, medido_en)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (pub_id, hito, v.get("views"), v.get("likes"), v.get("comments"), v.get("shares"),
         v.get("saved"), v.get("reach"), v.get("skip_rate"),
         retencion_pct, duracion_media, ahora.isoformat()),
    )
    log.info("Medición %s en %s guardada para '%s'", hito, NOMBRES[plataforma], v["titulo"])


def _texto_umbral(n):
    if n >= 1_000_000:
        return f"{n // 1_000_000} millón" if n < 2_000_000 else f"{n // 1_000_000} millones"
    return f"{n // 1_000}k"


def _comprobar_umbrales(con, plataforma, pub_id, titulo, views, silencioso, ahora):
    """
    Registra los umbrales de vistas que ha cruzado la publicación.
    Devuelve el texto del aviso (solo del umbral más alto nuevo), o None.
    En modo silencioso registra sin avisar: sirve para no avisar de golpe
    de todos los vídeos antiguos la primera vez.
    """
    if views is None:
        return None
    ya = {r["umbral"] for r in con.execute(
        "SELECT umbral FROM hitos_vistas WHERE publicacion_id = ?", (pub_id,)
    )}
    nuevos = [u for u in UMBRALES_VISTAS if u <= views and u not in ya]
    for u in nuevos:
        con.execute(
            "INSERT INTO hitos_vistas (publicacion_id, umbral, alcanzado_en) VALUES (?, ?, ?)",
            (pub_id, u, ahora.isoformat()),
        )
    if not nuevos or silencioso:
        return None
    return f"«{titulo}» ha llegado a {_texto_umbral(max(nuevos))} vistas en {NOMBRES[plataforma]}."


def _comprobar_despegue(con, plataforma, pub_id, titulo, views, edad_h, ahora):
    """
    Guarda la lectura de esta hora y avisa si el vídeo está despegando:
    gana en la última hora al menos DESPEGUE_MIN_VISTAS_HORA vistas y DESPEGUE_FACTOR veces
    su ritmo medio de las 24 h anteriores. Como mucho un aviso al día por vídeo.
    """
    if views is None:
        return None
    anterior = con.execute(
        "SELECT views, leido_en FROM lecturas WHERE publicacion_id = ? ORDER BY leido_en DESC LIMIT 1",
        (pub_id,),
    ).fetchone()
    hace_24h = con.execute(
        """SELECT views, leido_en FROM lecturas
           WHERE publicacion_id = ? AND leido_en <= ? ORDER BY leido_en DESC LIMIT 1""",
        (pub_id, (ahora - datetime.timedelta(hours=24)).isoformat()),
    ).fetchone()
    con.execute(
        "INSERT INTO lecturas (publicacion_id, leido_en, views) VALUES (?, ?, ?)",
        (pub_id, ahora.isoformat(), views),
    )
    if edad_h < DESPEGUE_EDAD_MIN_H or not anterior or not hace_24h:
        return None

    horas_ult = (ahora - _fecha(anterior["leido_en"])).total_seconds() / 3600
    horas_24 = (_fecha(anterior["leido_en"]) - _fecha(hace_24h["leido_en"])).total_seconds() / 3600
    if horas_ult <= 0 or horas_24 <= 0:
        return None
    ritmo_ahora = (views - anterior["views"]) / horas_ult
    ritmo_normal = (anterior["views"] - hace_24h["views"]) / horas_24

    if ritmo_ahora < DESPEGUE_MIN_VISTAS_HORA or ritmo_ahora < DESPEGUE_FACTOR * max(ritmo_normal, 1):
        return None
    reciente = con.execute(
        "SELECT 1 FROM despegues WHERE publicacion_id = ? AND avisado_en > ?",
        (pub_id, (ahora - datetime.timedelta(hours=24)).isoformat()),
    ).fetchone()
    if reciente:
        return None
    con.execute(
        "INSERT INTO despegues (publicacion_id, avisado_en) VALUES (?, ?)", (pub_id, ahora.isoformat())
    )
    return (
        f"«{titulo}» está despegando en {NOMBRES[plataforma]}: "
        f"+{_miles(round(ritmo_ahora))} vistas en la última hora "
        f"(lo normal era ~{_miles(round(ritmo_normal))}/h). Buen momento para responder comentarios."
    )


def _limpiar_lecturas(con, ahora):
    con.execute(
        "DELETE FROM lecturas WHERE leido_en < ?", ((ahora - datetime.timedelta(days=3)).isoformat(),)
    )


def _hitos_canal(con, plataforma, metrica, valor, umbrales, ahora):
    """
    Registra los umbrales cruzados por una métrica del canal (suscriptores, vistas de Shorts...).
    Devuelve el umbral más alto nuevo, o None. La primera vez que se mide una métrica
    registra en silencio los umbrales ya superados, para no avisar de golpe de todos.
    """
    ya = {r["umbral"] for r in con.execute(
        "SELECT umbral FROM hitos_canal WHERE plataforma = ? AND metrica = ?", (plataforma, metrica)
    )}
    primera_vez = not ya
    nuevos = [u for u in umbrales if u <= valor and u not in ya]
    for u in nuevos:
        con.execute(
            "INSERT INTO hitos_canal (plataforma, metrica, umbral, alcanzado_en) VALUES (?, ?, ?, ?)",
            (plataforma, metrica, u, ahora.isoformat()),
        )
    if primera_vez:
        # Marca la métrica como iniciada aunque no haya superado ningún umbral todavía
        con.execute(
            """INSERT OR IGNORE INTO hitos_canal (plataforma, metrica, umbral, alcanzado_en)
               VALUES (?, ?, 0, ?)""",
            (plataforma, metrica, ahora.isoformat()),
        )
        return None
    return max(nuevos) if nuevos else None


def _ciclo():
    """
    Detecta publicaciones nuevas en cada plataforma y guarda las mediciones que tocan.
    Devuelve una lista de (texto, video_id): si video_id no es None, el mensaje lleva
    los botones para elegir la categoría de ese vídeo.
    """
    mensajes = []
    ahora = _ahora_utc()

    for plataforma, obtener in _fuentes().items():
        try:
            publicaciones = obtener()
        except Exception:
            # Si una plataforma falla, las demás siguen funcionando
            log.exception("No se pudieron leer las publicaciones de %s", NOMBRES[plataforma])
            continue

        with closing(conectar()) as con, con:
            # Si nunca se han registrado umbrales de esta plataforma (primer arranque),
            # se registran los ya superados sin avisar.
            arranque = con.execute(
                """SELECT COUNT(*) FROM hitos_vistas h
                   JOIN publicaciones p ON p.id = h.publicacion_id
                   WHERE p.plataforma = ?""",
                (plataforma,),
            ).fetchone()[0] == 0

            for v in publicaciones:
                pub_id, video_id, pub_nueva, video_nuevo = _registrar(con, plataforma, v)

                aviso = _comprobar_umbrales(
                    con, plataforma, pub_id, v["titulo"], v.get("views"),
                    silencioso=arranque or pub_nueva, ahora=ahora,
                )
                if aviso:
                    mensajes.append((aviso, None))
                edad_h = (ahora - _fecha(v["publicado"])).total_seconds() / 3600

                despegue = _comprobar_despegue(
                    con, plataforma, pub_id, v["titulo"], v.get("views"), edad_h, ahora
                )
                if despegue:
                    mensajes.append((despegue, None))

                # Solo pregunta la categoría de vídeos nuevos y recientes
                # (la primera vez se importan todos los antiguos sin avisar)
                if video_nuevo and edad_h < 48:
                    mensajes.append(
                        (f"Nuevo vídeo en {NOMBRES[plataforma]}: {v['titulo']}\n¿Categoría?", video_id)
                    )

                hechos = {
                    r["hito"] for r in con.execute(
                        "SELECT hito FROM mediciones WHERE publicacion_id = ?", (pub_id,)
                    )
                }
                for hito, (horas, margen) in HITOS.items():
                    if hito in hechos or not (horas <= edad_h < horas + margen):
                        continue
                    _medir(con, plataforma, pub_id, video_id, v, hito, horas, ahora)
                    if hito == "24h":
                        mensajes.append(
                            (_aviso_24h(con, plataforma, pub_id, v["titulo"], v.get("views")), None)
                        )
            _limpiar_lecturas(con, ahora)

    # Suscriptores del canal de YouTube
    try:
        canal = youtube.estadisticas_canal()
        with closing(conectar()) as con, con:
            umbral = _hitos_canal(
                con, "youtube", "suscriptores", canal["suscriptores"], UMBRALES_SUSCRIPTORES, ahora
            )
        if umbral:
            mensajes.append((f"{canal['nombre']} ha llegado a {_miles(umbral)} suscriptores en YouTube.", None))
    except Exception:
        log.exception("No se pudieron leer los suscriptores de YouTube")
    return mensajes


async def comprobar_videos(context: ContextTypes.DEFAULT_TYPE):
    try:
        mensajes = await asyncio.to_thread(_ciclo)
    except Exception:
        log.exception("Error en el seguimiento de vídeos")
        return
    for texto, video_id in mensajes:
        teclado = _teclado_categorias(video_id) if video_id else None
        await context.bot.send_message(chat_id=CHAT_ID, text=texto, reply_markup=teclado)


# ---------------------------------------------------------------------------
# /videos
# ---------------------------------------------------------------------------

def _linea_publicacion(v):
    fecha = _dia_madrid(v["publicado"]).strftime("%d/%m")
    extra = []
    if v.get("shares") is not None:
        extra.append(f"{_miles(v['shares'])} compartidos")
    if v.get("media_vista_s") is not None:
        extra.append(f"media vista {v['media_vista_s']:.1f} s")
    if v.get("skip_rate") is not None:
        extra.append(f"saltado {v['skip_rate']:.0f} %")
    detalle = f" · {' · '.join(extra)}" if extra else ""
    return (
        f"• {fecha}  {v['titulo']}\n"
        f"   {_miles(v.get('views'))} vistas · {_miles(v.get('likes'))} likes · "
        f"{_miles(v.get('comments'))} coment.{detalle}"
    )


def texto_videos(n=6):
    """Últimas publicaciones de cada plataforma con sus estadísticas actuales (para /videos)."""
    bloques = []
    for plataforma, obtener in _fuentes().items():
        try:
            pubs = obtener()[:n]
        except Exception:
            log.exception("No se pudo leer %s para /videos", NOMBRES[plataforma])
            bloques.append(f"{NOMBRES[plataforma]}: no he podido leer los datos.")
            continue
        if not pubs:
            continue
        bloques.append(f"{NOMBRES[plataforma]}:\n" + "\n".join(_linea_publicacion(v) for v in pubs))
    return "\n\n".join(bloques) or "No hay publicaciones."


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
               GROUP BY v.id
               ORDER BY MAX(p.publicado_en) DESC LIMIT 1"""
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
    """Cifras de lo publicado hoy en cada plataforma, comparadas a la misma edad. None si no hay."""
    hoy = datetime.datetime.now(MADRID).date()
    ahora = _ahora_utc()
    lineas = []

    with closing(conectar()) as con:
        for plataforma, obtener in _fuentes().items():
            try:
                de_hoy = [v for v in obtener()[:5] if _dia_madrid(v["publicado"]) == hoy]
            except Exception:
                log.exception("No se pudo leer %s para el resumen", NOMBRES[plataforma])
                continue

            for v in de_hoy:
                horas = (ahora - _fecha(v["publicado"])).total_seconds() / 3600
                linea = (
                    f"{NOMBRES[plataforma]}, «{v['titulo']}»: {_miles(v.get('views'))} vistas, "
                    f"{_miles(v.get('likes'))} likes y {_miles(v.get('comments'))} comentarios "
                    f"en {horas:.0f} h."
                )
                fila = con.execute(
                    "SELECT id FROM publicaciones WHERE plataforma = ? AND id_externo = ?",
                    (plataforma, v["id"]),
                ).fetchone()
                anteriores = _mediana_anteriores(con, plataforma, "7h", fila["id"] if fila else -1)
                linea += _comparacion(v.get("views"), anteriores, "tu mediana a esta hora")
                lineas.append(linea)

    if not lineas:
        return None
    return "Tu vídeo de hoy:\n" + "\n".join(lineas)


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
    plataformas = list(_fuentes())

    with closing(conectar()) as con:
        semana = con.execute(
            """SELECT v.id, v.titulo, p.plataforma, MIN(p.publicado_en) AS publicado,
                      (SELECT m.views FROM mediciones m
                       WHERE m.publicacion_id = p.id AND m.hito = '24h') AS v24
               FROM publicaciones p JOIN videos v ON v.id = p.video_id
               WHERE p.publicado_en >= ?
               GROUP BY p.id
               ORDER BY publicado""",
            (desde,),
        ).fetchall()

        historico = con.execute(
            """SELECT v.categoria, p.plataforma, m.hito, m.views, m.retencion_pct, m.skip_rate,
                      (SELECT MAX(q.duracion_s) FROM publicaciones q WHERE q.video_id = v.id) AS duracion
               FROM mediciones m
               JOIN publicaciones p ON p.id = m.publicacion_id
               JOIN videos v ON v.id = p.video_id
               WHERE m.hito IN ('24h', '7d')"""
        ).fetchall()

    partes = ["Resumen semanal de AnimaMed", ""]

    # La semana, agrupada por vídeo
    por_video = {}
    for r in semana:
        d = por_video.setdefault(r["id"], {"titulo": r["titulo"], "v24": {}})
        d["v24"][r["plataforma"]] = r["v24"]

    if por_video:
        partes.append(f"Esta semana: {len(por_video)} vídeo(s). Vistas a las 24 h:")
        totales = []
        for d in por_video.values():
            cifras = [
                f"{NOMBRES[p]} {_miles(d['v24'][p]) if d['v24'].get(p) is not None else 'pendiente'}"
                for p in plataformas if p in d["v24"]
            ]
            partes.append(f"• {d['titulo']}: {' · '.join(cifras)}")
            medidos = [x for x in d["v24"].values() if x is not None]
            if medidos:
                totales.append((sum(medidos), d["titulo"]))
        if len(totales) >= 2:
            partes += ["", f"Mejor: {max(totales)[1]}", f"Peor: {min(totales)[1]}"]
    else:
        partes.append("Esta semana no has publicado.")

    # Histórico por categoría y por formato, separado por plataforma
    for plataforma in plataformas:
        filas = [r for r in historico if r["plataforma"] == plataforma]
        if not filas:
            continue

        def grupo(clave):
            datos = {}
            for r in filas:
                d = datos.setdefault(clave(r), {"v24": [], "ret": [], "salto": []})
                if r["hito"] == "24h":
                    d["v24"].append(r["views"])
                    d["salto"].append(r["skip_rate"])
                elif r["hito"] == "7d":
                    d["ret"].append(r["retencion_pct"])
            return datos

        por_categoria = grupo(lambda r: r["categoria"] or "Sin categoría")
        por_formato = grupo(
            lambda r: "Corto (≤45 s)" if (r["duracion"] or 0) <= CORTE_FORMATO_S else "Largo (>45 s)"
        )

        partes += ["", f"== {NOMBRES[plataforma]} =="]
        cabecera = "vistas a 24 h · retención a 7 d"
        if plataforma == "instagram":
            cabecera += " · saltado"
        for titulo, datos in (("Por categoría", por_categoria), ("Por formato", por_formato)):
            partes.append(f"{titulo} (mediana de {cabecera}):")
            for k, d in sorted(datos.items()):
                linea = f"• {k}: {_mediana_txt(d['v24'])} · {_mediana_txt(d['ret'], ' %')}"
                if plataforma == "instagram":
                    linea += f" · {_mediana_txt(d['salto'], ' %')}"
                partes.append(linea)

    return "\n".join(partes)


async def resumen_semanal(context: ContextTypes.DEFAULT_TYPE):
    try:
        texto = await asyncio.to_thread(texto_semanal)
    except Exception:
        log.exception("Error preparando el resumen semanal de vídeos")
        return
    await context.bot.send_message(chat_id=CHAT_ID, text=texto)


# ---------------------------------------------------------------------------
# Monetización: progreso hacia el Programa de Partners de YouTube
# ---------------------------------------------------------------------------

def _barra(actual, objetivo, ancho=10):
    llenos = min(ancho, int(actual / objetivo * ancho)) if objetivo else 0
    return "▓" * llenos + "░" * (ancho - llenos) + f" {min(actual / objetivo * 100, 100):.0f} %"


def _datos_monetizacion():
    hoy = datetime.datetime.now(MADRID).date()
    desde = (hoy - datetime.timedelta(days=90)).isoformat()
    canal = youtube.estadisticas_canal()
    shorts = youtube.vistas_shorts(desde, hoy.isoformat())
    with closing(conectar()) as con:
        subidas = con.execute(
            """SELECT COUNT(*) FROM publicaciones
               WHERE plataforma = 'youtube' AND publicado_en >= ?""",
            (desde,),
        ).fetchone()[0]
    return hoy, canal["nombre"], canal["suscriptores"], shorts, subidas


def texto_monetizacion():
    hoy, nombre, subs, shorts, subidas = _datos_monetizacion()
    completo = YPP_COMPLETO_2027 if hoy >= CAMBIO_2027 else YPP_COMPLETO

    partes = [
        f"Progreso hacia la monetización de YouTube ({nombre})",
        "",
        "Nivel básico (financiación de fans, compras):",
        f"• Suscriptores: {_miles(subs)} / {_miles(YPP_BASICO['suscriptores'])}  "
        f"{_barra(subs, YPP_BASICO['suscriptores'])}",
        f"• Vistas de Shorts (90 días): {_miles(shorts)} / {_miles(YPP_BASICO['shorts_90d'])}  "
        f"{_barra(shorts, YPP_BASICO['shorts_90d'])}",
        f"• Subidas (90 días): {subidas} / {YPP_BASICO['subidas_90d']}",
        "",
        "Nivel completo (ingresos por anuncios):",
        f"• Suscriptores: {_miles(subs)} / {_miles(completo['suscriptores'])}  "
        f"{_barra(subs, completo['suscriptores'])}",
        f"• Vistas de Shorts (90 días): {_miles(shorts)} / {_miles(completo['shorts_90d'])}  "
        f"{_barra(shorts, completo['shorts_90d'])}",
    ]
    if hoy < CAMBIO_2027:
        partes.append(
            f"  Ojo: desde el 1/2/2027 el requisito de Shorts para entrar sube a "
            f"{_miles(YPP_COMPLETO_2027['shorts_90d'])}."
        )
    partes += [
        "",
        "Las vistas son las de YouTube Analytics; para monetizar solo cuentan las «válidas», "
        "así que la cifra real puede ser algo menor. YouTube Studio tiene la cifra oficial.",
    ]
    return "\n".join(partes)


def _ciclo_monetizacion():
    """Comprobación diaria: avisa al cruzar umbrales de vistas de Shorts en 90 días."""
    ahora = _ahora_utc()
    _, nombre, _, shorts, _ = _datos_monetizacion()
    with closing(conectar()) as con, con:
        umbral = _hitos_canal(con, "youtube", "shorts_90d", shorts, UMBRALES_SHORTS_90D, ahora)
    if not umbral:
        return None
    return (
        f"{nombre} ha superado {_miles(umbral)} vistas de Shorts en los últimos 90 días. "
        "Usa /monetizacion para ver cuánto te falta."
    )


async def monetizacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        texto = await asyncio.to_thread(texto_monetizacion)
    except Exception:
        log.exception("Error calculando el progreso de monetización")
        texto = "No he podido leer los datos de YouTube. Mira los logs."
    await update.message.reply_text(texto)


async def comprobar_monetizacion(context: ContextTypes.DEFAULT_TYPE):
    try:
        texto = await asyncio.to_thread(_ciclo_monetizacion)
    except Exception:
        log.exception("Error en la comprobación diaria de monetización")
        return
    if texto:
        await context.bot.send_message(chat_id=CHAT_ID, text=texto)
