import asyncio
import datetime
import logging
from telegram.ext import ContextTypes

from gress.config import CHAT_ID
from gress.tiempo import tiempo_barcelona
from gress.calendario import eventos_de_hoy, eventos_entre, MADRID
from gress import memoria
from gress import videos

log = logging.getLogger("gress")

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def formatear_evento(e):
    if "dateTime" in e["start"]:
        inicio = datetime.datetime.fromisoformat(e["start"]["dateTime"]).astimezone(MADRID)
        hora = inicio.strftime("%H:%M")
    else:
        hora = "Todo el día"
    return f"• {hora}  {e.get('summary', '(sin título)')}"


def texto_resumen():
    hoy = datetime.datetime.now(MADRID)
    partes = [
        f"Buenos días, Marc. Hoy es {DIAS[hoy.weekday()]} {hoy.strftime('%d/%m')}.",
        "",
        tiempo_barcelona(),
        "",
    ]
    try:
        video = videos.texto_video_hoy()
    except Exception:
        log.exception("No se pudo leer el vídeo de hoy")
        video = None
    if video:
        partes += [video, ""]
    eventos = eventos_de_hoy()
    if eventos:
        partes.append("Hoy tienes:")
        partes += [formatear_evento(e) for e in eventos]
    else:
        partes.append("No tienes nada en el calendario hoy.")

    pendientes = memoria.recordatorios_pendientes()
    if pendientes:
        partes.append("")
        partes.append(f"Tienes {len(pendientes)} recordatorio(s) pendiente(s). Usa /recordatorios.")
    return "\n".join(partes)


async def resumen_manana(context: ContextTypes.DEFAULT_TYPE):
    try:
        texto = await asyncio.to_thread(texto_resumen)
    except Exception:
        log.exception("Error preparando el resumen de la mañana")
        texto = "Buenos días. No he podido preparar el resumen, revisa los logs."
    await context.bot.send_message(chat_id=CHAT_ID, text=texto)

    # Aprovecha para limpiar avisos antiguos una vez al día
    await asyncio.to_thread(memoria.limpiar_avisados)


async def comprobar_proximos(context: ContextTypes.DEFAULT_TYPE):
    ahora = datetime.datetime.now(MADRID)
    try:
        eventos = await asyncio.to_thread(
            eventos_entre, ahora, ahora + datetime.timedelta(minutes=60)
        )
    except Exception:
        log.exception("Error consultando el calendario")
        return

    for e in eventos:
        # Ignora eventos de todo el día y los ya avisados
        if "dateTime" not in e["start"] or memoria.ya_avisado(e["id"]):
            continue
        inicio = datetime.datetime.fromisoformat(e["start"]["dateTime"])
        if inicio < ahora:
            continue  # Ya ha empezado
        minutos = int((inicio - ahora).total_seconds() // 60)
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=f"En {minutos} min: {e.get('summary', '(sin título)')} "
                 f"({inicio.astimezone(MADRID).strftime('%H:%M')})",
        )
        memoria.marcar_avisado(e["id"])


async def enviar_recordatorios(context: ContextTypes.DEFAULT_TYPE):
    for rec in memoria.recordatorios_vencidos():
        await context.bot.send_message(chat_id=CHAT_ID, text=f"Recordatorio: {rec['texto']}")
        memoria.marcar_enviado(rec["id"])