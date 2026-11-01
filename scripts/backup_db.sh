#!/usr/bin/env bash
# Copia de seguridad diaria de la BD
set -euo pipefail

ORIGEN="/opt/gress/app/gress.db"
DESTINO="/opt/gress/backups"
DIAS=14
FECHA="$(date +%Y-%m-%d_%H%M)"
ARCHIVO="$DESTINO/gress-$FECHA.db"

mkdir -p "$DESTINO"

sqlite3 "$ORIGEN" ".backup '$ARCHIVO'"
gzip "$ARCHIVO"

# Comprueba que la copia se puede leer
gzip -t "$ARCHIVO.gz"

# Borra las copias de más de $DIAS días
find "$DESTINO" -name "gress-*.db.gz" -mtime +"$DIAS" -delete

echo "Copia creada: $ARCHIVO.gz ($(du -h "$ARCHIVO.gz" | cut -f1))"