"""
Acceso a Instagram (cuenta profesional de AnimaMed) con la API de Instagram
con inicio de sesión de Instagram. Solo lectura: reels y sus estadísticas.

El token se guarda en token_instagram.json (fuera de Git por el patrón token*.json).
Caduca a los 60 días, así que Gress lo renueva solo cada semana.
"""
import datetime
import json
import os

import requests

API = "https://graph.instagram.com"
TOKEN = "token_instagram.json"
RENOVAR_CADA = datetime.timedelta(days=7)

# Métricas de reels. Si Instagram rechaza alguna, se reintenta solo con las básicas.
METRICAS = "views,reach,shares,saved,ig_reels_avg_watch_time,reels_skip_rate"
METRICAS_BASICAS = "views,reach,shares,saved"


class ErrorInstagram(Exception):
    pass


def _ahora():
    return datetime.datetime.now(datetime.timezone.utc)


def configurado():
    return os.path.exists(TOKEN)


def _leer_token():
    with open(TOKEN) as f:
        return json.load(f)


def _guardar_token(token):
    datos = {"access_token": token, "renovado": _ahora().isoformat()}
    with open(TOKEN, "w") as f:
        json.dump(datos, f)
    try:
        os.chmod(TOKEN, 0o600)  # Solo el propietario puede leerlo (en Linux)
    except OSError:
        pass


def _get(ruta, token, **params):
    """
    Petición GET a la API. Si falla, lanza un error SIN la URL:
    la URL lleva el token y acabaría en los logs.
    """
    params["access_token"] = token
    r = requests.get(f"{API}/{ruta}", params=params, timeout=20)
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", "sin detalle")
        except ValueError:
            msg = "respuesta no válida"
        raise ErrorInstagram(f"Instagram {r.status_code}: {msg}")
    return r.json()


def token_valido():
    """Devuelve el token, renovándolo si toca."""
    datos = _leer_token()
    token = datos["access_token"]
    renovado = datetime.datetime.fromisoformat(datos["renovado"])
    if _ahora() - renovado >= RENOVAR_CADA:
        nuevo = _get("refresh_access_token", token, grant_type="ig_refresh_token")
        token = nuevo["access_token"]
        _guardar_token(token)
    return token


def cuenta():
    datos = _get("me", token_valido(), fields="user_id,username")
    return datos.get("username"), datos.get("user_id")


def _valor(metrica):
    if "values" in metrica and metrica["values"]:
        return metrica["values"][0].get("value")
    if "total_value" in metrica:
        return metrica["total_value"].get("value")
    return None


def _insights(media_id, token):
    try:
        datos = _get(f"{media_id}/insights", token, metric=METRICAS)
    except ErrorInstagram:
        datos = _get(f"{media_id}/insights", token, metric=METRICAS_BASICAS)
    return {m["name"]: _valor(m) for m in datos.get("data", [])}


def _iso_z(ts):
    """'2026-11-03T23:00:05+0000' -> '2026-11-03T23:00:05Z' (mismo formato que YouTube)."""
    return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").strftime("%Y-%m-%dT%H:%M:%SZ")


def _titulo(caption):
    if not caption:
        return "(reel sin texto)"
    primera = caption.strip().split("\n")[0]
    return primera if len(primera) <= 80 else primera[:77] + "..."


def ultimos_reels(n=35, dias_con_estadisticas=31):
    """
    Últimos reels con sus estadísticas, del más nuevo al más viejo.
    Solo pide estadísticas de los de menos de `dias_con_estadisticas` días,
    que son los únicos que todavía pueden necesitar una medición.
    """
    token = token_valido()
    datos = _get(
        "me/media", token,
        fields="id,caption,media_product_type,timestamp,like_count,comments_count",
        limit=n,
    )
    ahora = _ahora()
    resultado = []
    for m in datos.get("data", []):
        if m.get("media_product_type") != "REELS":
            continue
        publicado = _iso_z(m["timestamp"])
        edad = ahora - datetime.datetime.fromisoformat(publicado.replace("Z", "+00:00"))

        stats = {}
        if edad.days < dias_con_estadisticas:
            stats = _insights(m["id"], token)

        espera_ms = stats.get("ig_reels_avg_watch_time")
        resultado.append({
            "id": m["id"],
            "titulo": _titulo(m.get("caption")),
            "publicado": publicado,
            "duracion": None,  # Instagram no da la duración; se toma la de YouTube si está enlazado
            "views": stats.get("views"),
            "likes": m.get("like_count"),
            "comments": m.get("comments_count"),
            "shares": stats.get("shares"),
            "saved": stats.get("saved"),
            "reach": stats.get("reach"),
            "media_vista_s": espera_ms / 1000 if espera_ms is not None else None,
            "skip_rate": stats.get("reels_skip_rate"),
        })
    resultado.sort(key=lambda x: x["publicado"], reverse=True)
    return resultado


if __name__ == "__main__":
    import sys
    from getpass import getpass

    if len(sys.argv) > 1 and sys.argv[1] == "guardar":
        # Uso: python -m gress.instagram guardar
        # Pide el token sin mostrarlo en pantalla ni dejarlo en el historial de la terminal.
        _guardar_token(getpass("Pega el token de Instagram (no se verá): ").strip())
        print("Token guardado en", TOKEN)

    usuario, uid = cuenta()
    print(f"Cuenta autorizada: @{usuario} ({uid})")
    for r in ultimos_reels(5):
        print(f"{r['publicado'][:10]}  {r['views']} vistas  {r['likes']} likes  "
              f"media vista {r['media_vista_s']} s  salto {r['skip_rate']}  {r['titulo']}")
