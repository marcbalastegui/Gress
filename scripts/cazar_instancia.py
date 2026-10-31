"""
Intenta crear una instancia de Oracle cada pocos minutos hasta conseguirlo.
Cuando lo consigue, avisa por Telegram con la IP pública.
La máquina que busca se configura en el .env (OCI_NAME, OCI_SHAPE, OCI_IMAGE...).
"""
import os
import random
import time
import logging

import oci
import requests
from dotenv import load_dotenv

load_dotenv()

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/cazador.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("cazador")

INTERVALO = 300  # segundos entre intentos (5 min)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
COMPARTMENT = os.getenv("OCI_COMPARTMENT")
AD = os.getenv("OCI_AD")
SUBNET = os.getenv("OCI_SUBNET")
IMAGE = os.getenv("OCI_IMAGE")
NOMBRE = os.getenv("OCI_NAME", "gress-01")
SHAPE = os.getenv("OCI_SHAPE", "VM.Standard.A1.Flex")
OCPUS = float(os.getenv("OCI_OCPUS", "2"))
MEMORY = float(os.getenv("OCI_MEMORY", "12"))
SSH_PUBKEY = os.getenv("SSH_PUBKEY")

config = oci.config.from_file()
compute = oci.core.ComputeClient(config)
red = oci.core.VirtualNetworkClient(config)


def avisar(texto):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": texto},
            timeout=10,
        )
    except Exception:
        log.exception("No se pudo enviar el aviso por Telegram")


def ya_existe():
    """Evita crear una segunda máquina si ya hay una con el mismo nombre."""
    instancias = compute.list_instances(COMPARTMENT, display_name=NOMBRE).data
    return [i for i in instancias if i.lifecycle_state not in ("TERMINATED", "TERMINATING")]


def detalles():
    with open(SSH_PUBKEY) as f:
        clave = f.read().strip()

    # Solo las shapes "Flex" permiten elegir CPU y RAM. La Micro tiene tamaño fijo.
    shape_config = None
    if SHAPE.endswith(".Flex"):
        shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=OCPUS, memory_in_gbs=MEMORY
        )

    return oci.core.models.LaunchInstanceDetails(
        compartment_id=COMPARTMENT,
        availability_domain=AD,
        display_name=NOMBRE,
        shape=SHAPE,
        shape_config=shape_config,
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=IMAGE, source_type="image"
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=SUBNET, assign_public_ip=True
        ),
        metadata={"ssh_authorized_keys": clave},
        is_pv_encryption_in_transit_enabled=True,
    )


def ip_publica(instancia_id):
    vnics = compute.list_vnic_attachments(COMPARTMENT, instance_id=instancia_id).data
    return red.get_vnic(vnics[0].vnic_id).data.public_ip


def main():
    if ya_existe():
        log.info("Ya existe una instancia llamada %s. No hago nada.", NOMBRE)
        return

    tamano = f"{OCPUS} OCPU / {MEMORY} GB" if SHAPE.endswith(".Flex") else "tamaño fijo"
    log.info("Buscando capacidad para %s (%s, %s)...", NOMBRE, SHAPE, tamano)
    intento = 0
    while True:
        intento += 1
        try:
            instancia = compute.launch_instance(detalles()).data
            log.info("Instancia creada (intento %d). Esperando a que arranque...", intento)

            oci.wait_until(
                compute, compute.get_instance(instancia.id),
                "lifecycle_state", "RUNNING", max_wait_seconds=900,
            )
            ip = ip_publica(instancia.id)
            log.info("Instancia en marcha. IP pública: %s", ip)
            avisar(
                f"Servidor {NOMBRE} conseguido tras {intento} intentos.\n"
                f"IP pública: {ip}\n"
                f"Conéctate con: ssh opc@{ip}"
            )
            return

        except oci.exceptions.ServiceError as e:
            mensaje = (e.message or "").lower()
            if "capacity" in mensaje:
                log.info("Intento %d: sin capacidad.", intento)
            elif e.status == 429:
                log.warning("Intento %d: demasiadas peticiones, espero el doble.", intento)
                time.sleep(INTERVALO)
            else:
                # Un error distinto no se arregla reintentando: avisa y para
                log.error("Error inesperado: %s %s", e.status, e.message)
                avisar(f"El cazador de instancias se ha parado por un error:\n{e.status} {e.message}")
                return

        # Un poco de aleatoriedad para no ir siempre al mismo segundo
        time.sleep(INTERVALO + random.randint(0, 60))


if __name__ == "__main__":
    main()