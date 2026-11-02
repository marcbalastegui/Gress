"""
Acceso a YouTube (canal Medular2) con permisos de solo lectura.

Usa su propia autorización (token_youtube.json), separada de la del calendario
(token.json), porque el canal está en otra cuenta de Google.

- YouTube Data API v3: lista de vídeos y estadísticas públicas.
- YouTube Analytics API: retención (porcentaje medio visto y duración media vista).
"""
import os
import re

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
TOKEN = "token_youtube.json"

_lista_subidas = None  # Se guarda tras la primera consulta para no pedirla cada vez


def credenciales():
    creds = None
    if os.path.exists(TOKEN):
        creds = Credentials.from_authorized_user_file(TOKEN, SCOPES)
    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        # Solo debería pasar en el PC: abre el navegador para autorizar.
        flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
        creds = flow.run_local_server(port=0)

    with open(TOKEN, "w") as f:
        f.write(creds.to_json())
    return creds


def _data():
    return build("youtube", "v3", credentials=credenciales(), cache_discovery=False)


def _analytics():
    return build("youtubeAnalytics", "v2", credentials=credenciales(), cache_discovery=False)


def canal():
    """Devuelve (id, nombre, id de la lista de subidas) del canal autorizado."""
    r = _data().channels().list(part="snippet,contentDetails", mine=True).execute()
    c = r["items"][0]
    return c["id"], c["snippet"]["title"], c["contentDetails"]["relatedPlaylists"]["uploads"]


def _duracion_segundos(iso):
    """Convierte la duración de YouTube (formato ISO 8601, p. ej. 'PT1M5S') a segundos."""
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso)
    if not m:
        return None
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def ultimos_videos(n=50):
    """Últimos n vídeos subidos, con sus estadísticas actuales, del más nuevo al más viejo."""
    global _lista_subidas
    yt = _data()
    if _lista_subidas is None:
        _, _, _lista_subidas = canal()

    items = yt.playlistItems().list(
        part="contentDetails", playlistId=_lista_subidas, maxResults=n
    ).execute().get("items", [])
    ids = [i["contentDetails"]["videoId"] for i in items]
    if not ids:
        return []

    vids = yt.videos().list(
        part="snippet,statistics,contentDetails", id=",".join(ids)
    ).execute().get("items", [])

    resultado = []
    for v in vids:
        s = v.get("statistics", {})
        resultado.append({
            "id": v["id"],
            "titulo": v["snippet"]["title"],
            "publicado": v["snippet"]["publishedAt"],  # UTC, formato '2026-10-31T17:00:00Z'
            "duracion": _duracion_segundos(v["contentDetails"]["duration"]),
            "views": int(s.get("viewCount", 0)),
            "likes": int(s.get("likeCount", 0)),
            "comments": int(s.get("commentCount", 0)),
        })
    resultado.sort(key=lambda x: x["publicado"], reverse=True)
    return resultado


def retencion(video_id, desde, hasta):
    """
    Porcentaje medio visto y duración media vista (segundos) entre dos fechas (AAAA-MM-DD).
    Ojo: YouTube Analytics tarda uno o dos días en tener los datos.
    Devuelve (None, None) si todavía no hay datos.
    """
    r = _analytics().reports().query(
        ids="channel==MINE",
        startDate=desde,
        endDate=hasta,
        metrics="views,averageViewDuration,averageViewPercentage",
        filters=f"video=={video_id}",
    ).execute()
    filas = r.get("rows")
    if not filas:
        return None, None
    _, duracion_media, porcentaje = filas[0]
    return porcentaje, duracion_media


if __name__ == "__main__":
    # Prueba / autorización inicial: ejecutar en el PC con  python -m gress.youtube
    cid, nombre, _ = canal()
    print(f"Canal autorizado: {nombre} ({cid})")
    for v in ultimos_videos(5):
        print(f"{v['publicado'][:10]}  {v['views']:>7} vistas  {v['titulo']}")
