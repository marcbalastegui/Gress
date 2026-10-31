import asyncio
import datetime
import logging

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters,
)

from gress.config import TELEGRAM_TOKEN, CHAT_ID
from gress.tiempo import tiempo_barcelona
from gress.interprete import interpretar, interpretar_relativo
from gress.calendario import crear_evento, proximos_eventos, MADRID
from gress.avisos import (
    resumen_manana, comprobar_proximos, enviar_recordatorios, texto_resumen,
)
from gress import memoria

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
# httpx registra las URLs completas, que incluyen el token. Lo silenciamos.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("gress")
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

# Solo acepta mensajes de tu chat
SOLO_MARC = filters.Chat(chat_id=CHAT_ID)

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def hora_local(iso_utc):
    return datetime.datetime.fromisoformat(iso_utc).astimezone(MADRID)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Gress en línea.\n"
        "/ping - comprobar que estoy vivo\n"
        "/tiempo - previsión de hoy\n"
        "/agenda - próximos eventos\n"
        "/resumen - resumen del día\n"
        "/recordatorios - recordatorios pendientes\n"
        "/borrar_rec N - cancelar el recordatorio N\n\n"
        "Para el calendario: 'apunta que mañana...' o 'recuérdame el viernes a las 17:00...'\n"
        "Para un aviso rápido: 'recuérdame en 2 horas...' o 'recuérdame dentro de 30 minutos...'"
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")


async def tiempo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = await asyncio.to_thread(tiempo_barcelona)
    await update.message.reply_text(texto)


async def agenda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        eventos = await asyncio.to_thread(proximos_eventos, 5)
    except Exception:
        log.exception("Error leyendo la agenda")
        await update.message.reply_text("No he podido leer el calendario. Mira los logs.")
        return

    if not eventos:
        await update.message.reply_text("No tienes eventos próximos.")
        return

    lineas = []
    for e in eventos:
        inicio = e["start"].get("dateTime", e["start"].get("date"))
        lineas.append(f"• {inicio[:16].replace('T', ' ')}  {e.get('summary', '(sin título)')}")
    await update.message.reply_text("Próximos eventos:\n" + "\n".join(lineas))


async def resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        texto = await asyncio.to_thread(texto_resumen)
    except Exception:
        log.exception("Error preparando el resumen")
        texto = "No he podido preparar el resumen. Mira los logs."
    await update.message.reply_text(texto)


async def recordatorios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pendientes = memoria.recordatorios_pendientes()
    if not pendientes:
        await update.message.reply_text("No tienes recordatorios pendientes.")
        return
    lineas = [
        f"{r['id']}. {hora_local(r['cuando']).strftime('%d/%m %H:%M')}  {r['texto']}"
        for r in pendientes
    ]
    await update.message.reply_text("Recordatorios pendientes:\n" + "\n".join(lineas))


async def borrar_rec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Uso: /borrar_rec N (mira el número en /recordatorios)")
        return
    if memoria.borrar_recordatorio(int(context.args[0])):
        await update.message.reply_text("Recordatorio cancelado.")
    else:
        await update.message.reply_text("No existe ese recordatorio o ya se envió.")


async def texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = update.message.text

    # 1. ¿Es un recordatorio relativo? ("recuérdame en 2 horas...")
    rel = interpretar_relativo(mensaje)
    if rel:
        if not rel["texto"]:
            await update.message.reply_text("¿Qué quieres que te recuerde?")
            return
        rec_id = memoria.crear_recordatorio(rel["texto"], rel["cuando"])
        hora = rel["cuando"].astimezone(MADRID).strftime("%H:%M")
        await update.message.reply_text(
            f"Te lo recuerdo a las {hora}: {rel['texto']} (n.º {rec_id})"
        )
        return

    # 2. ¿Es algo para el calendario? ("apunta que mañana...")
    r = interpretar(mensaje)
    if r is None:
        await update.message.reply_text(
            "No te he entendido. Empieza con 'apunta que...' o 'recuérdame...'."
        )
        return
    if not r["titulo"]:
        await update.message.reply_text("¿Qué quieres que apunte?")
        return
    if r["fecha"] is None:
        await update.message.reply_text(
            f"Vale, '{r['titulo']}', pero ¿para cuándo? "
            "Dímelo en la frase, por ejemplo 'apunta que el lunes...' o 'recuérdame en 2 horas...'."
        )
        return

    try:
        # La API de Google es bloqueante; la ejecutamos aparte para no congelar el bot
        await asyncio.to_thread(crear_evento, r["titulo"], r["fecha"], r["con_hora"])
    except Exception:
        log.exception("Error creando evento")
        await update.message.reply_text("No he podido crear el evento. Mira los logs.")
        return

    f = r["fecha"]
    cuando = f"{DIAS[f.weekday()]} {f.strftime('%d/%m/%Y')}"
    if r["con_hora"]:
        cuando += f" a las {f.strftime('%H:%M')}"
    await update.message.reply_text(f"Apuntado: {r['titulo']}, {cuando}.")


async def intruso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = update.effective_user
    log.warning("Mensaje ignorado de %s (id %s)", usuario.username, usuario.id)


def main():
    memoria.iniciar()  # Crea las tablas si no existen

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(20)
        .read_timeout(20)
        .get_updates_connect_timeout(20)
        .get_updates_read_timeout(30)
        .build()
    )

    # Comandos
    app.add_handler(CommandHandler("start", start, filters=SOLO_MARC))
    app.add_handler(CommandHandler("ping", ping, filters=SOLO_MARC))
    app.add_handler(CommandHandler("tiempo", tiempo, filters=SOLO_MARC))
    app.add_handler(CommandHandler("agenda", agenda, filters=SOLO_MARC))
    app.add_handler(CommandHandler("resumen", resumen, filters=SOLO_MARC))
    app.add_handler(CommandHandler("recordatorios", recordatorios, filters=SOLO_MARC))
    app.add_handler(CommandHandler("borrar_rec", borrar_rec, filters=SOLO_MARC))

    # Mensajes de texto
    app.add_handler(MessageHandler(SOLO_MARC & filters.TEXT & ~filters.COMMAND, texto))
    app.add_handler(MessageHandler(~SOLO_MARC, intruso))

    # Tareas programadas
    jq = app.job_queue
    jq.run_daily(resumen_manana, time=datetime.time(7, 30, tzinfo=MADRID), name="resumen_manana")
    jq.run_repeating(comprobar_proximos, interval=300, first=10, name="avisos_eventos")
    jq.run_repeating(enviar_recordatorios, interval=30, first=5, name="recordatorios")

    log.info("Gress arrancado, esperando mensajes...")
    app.run_polling()


if __name__ == "__main__":
    main()