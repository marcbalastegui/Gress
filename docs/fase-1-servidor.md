# Fase 1: Servidor de Gress

En esta fase documento el proceso entero de la preparación y protección del servidor Linux donde reside Gress.
**Objetivo:** Tener una máquina las 24 horas encendida a la que solo pueda entrar yo, con un firewall cerrado y protecciñon contra ataques automáticos.

## 1. El plan inicial
El plan inicial era usar Oracle Cloud Always Free, un plan que da Oracle, máquinas ARM de 4 núcleos con 24 GB de RAM, una capa muy generosa comparada con la competencia por lo que la demanda es alta. 

Monté una clave SSH en mi PC

La primera vez que intenté crear la instancia, no pude, por el error `Out of Host capacity`. Lo que básicamente significa que en los centros de datos de Madrid (La región que escogí) no quedaba hueco para una máquina más.



Con un script de python que servia para "cazar" la instancia, que lanzaba la petición cada 5 minutos, intenté durante unas 2 semanas conseguirla, pero siempre salía el mismo problema "Out of host capacity". Incluso lo intenté con la máquina más pequeña.

1. Comprobaba si la máquina existía, para evitar duplicados
2. Pedía la máquina a la API de Oracle
3. Si la respuesta era `Out of Host capacity`, volvía a lanzar la petición.
4. Si daba otro error, se paraba y avisaba
5. Si funcionaba esperaba a que arrancara, sacaba la IP y nos avisaba a través del bot de Telegram

Esto se debía a varias causas:
- Los que pagan, tienen prioridad. Solo una parte de los servidores físicos se destina a los del plan gratuito
- Lo mismo que hacía yo, lo hacían otras muchas personas. Es decir, una máquina de 24 GB, 4 núcleos y 200 GB disco gratis, cubre las necesidades de cualquier usuario, así que era tarea complicada que alguien borrara su máquina
- Los huecos solo aparecen si otra persona borra una máquina, y con lo difícil que es conseguir una, nadie la quiere.

Así que tocaba buscar otras opciones

### Alternativas

- Google Cloud e2-micro: la IP pública se cobraba aparte y el disco por defecto no era gratuito. Además solo nos daba 1 GB de RAM
- IONOS VPS XS: era otra opción barata, pero mismo problema de RAM, 1 GB
- Hetzner: más caro, subió de precio (Algunos medios lo atribuyen a la demanda del hardware de la IA)

La decisión final fue OVH VPS-1 (2 vCPU, 4 GB, 40 GB NVMe) por 55 € al año. La consideré la mejor opción relación recursos y precio.

### Decisión final
