# Itinerario

Itinerario convierte un pedido en Telegram en una respuesta conversacional o en un informe con evidencia, cartografía y próximos pasos. Sirve para explorar oportunidades, comparar opciones, preparar visitas o investigar un tema. La [muestra de Palermo](output/pdf/recorrido-comercial-palermo.pdf) incluye cartografía, un recorrido y tres sectores.

## Capacidades

- Consultas y repreguntas en el chat. `/historial` lista los informes y `/ver ID` vuelve a enviar un PDF y lo selecciona como contexto.
- Cuatro niveles. `/rapido`, `/detallado`, `/profundo` y `/extenso` funcionan en la misma línea o por encima del pedido en un mismo mensaje. Escribí en Telegram como en una charla o abrí `/ejemplos` para arrancar.
- `/modos` muestra el modelo, el alcance de cada perfil y los cupos. Todos usan el modelo de `OPENAI_MODEL`. El presupuesto nominal de búsqueda es de hasta 2, 5, 11 y 20 acciones respectivamente. Profundo agrega un pase de hilos públicos, contraste independiente y una segunda búsqueda social por nombre cuando el primer pase queda vacío. Extenso suma una búsqueda local o sectorial y espacio para desarrollar el PDF. Se activa con el comando explícito y usa dos puntos del cupo. Los demás perfiles usan uno.
- Podés pedir una respuesta en el chat o un PDF de extensión aproximada y definir cuántos sectores o paradas querés. La cantidad de páginas depende de las fuentes, las comparaciones y los mapas.
- Búsqueda web y rastreo dirigido a publicaciones y conversaciones públicas, con enlaces, fecha de consulta y distinción entre evidencia, inferencias y pendientes. Los iconos del PDF identifican la procedencia de las fuentes citadas.
- `/invitar` genera enlaces de un solo uso que vencen a los tres días. Las personas invitadas conservan su propio historial y comparten el cupo global.
- Mapas con cartografía de fondo, un color por sector y paradas numeradas. La leyenda del mapa general lleva a cada sector del PDF. Los enlaces de Google Maps sirven para abrir la navegación.
- Rutas para una persona o vehículo, con inicio fijo, llegada flexible o fija y regreso opcional. El orden se calcula sobre una matriz de costos viales. La distancia y duración se calculan con el motor de rutas.
- PDF adjunto con texto seleccionable, hasta 8 MB. Se extraen hasta 10.000 caracteres de las primeras 12 páginas para consultas en el chat.

## Desarrollo local

Usá Python 3.12 y [uv](https://docs.astral.sh/uv/). Creá un bot con [@BotFather](https://t.me/BotFather), un proyecto en [Geoapify](https://myprojects.geoapify.com/) y una clave dedicada en el proyecto de [OpenAI Platform](https://platform.openai.com/). Los informes usan `gpt-6.2-sol` por defecto.

```powershell
uv sync --locked
Copy-Item .env.example .env.local
```

Cargá las tres claves en `.env.local`. Está excluido de Git y de la imagen Docker. Para obtener tu ID desde el bot, ejecutá el siguiente comando y mandale `/miid` desde tu chat privado durante la ventana de lectura.

```powershell
uv run --locked itinerario --mi-id
```

Agregá tu ID a `TELEGRAM_ALLOWED_USERS`. Para varios usuarios separá sus identificadores numéricos por coma. Iniciá una sola instancia por token.

```powershell
uv run --locked itinerario --check
uv run --locked itinerario
```

`--check` verifica la configuración local y la autenticación con Telegram. Probá los tres servicios mandando un pedido como el siguiente y revisá el PDF en el chat.

> /detallado Buscá hasta ocho espacios culturales y clubes de CABA vinculados a música electrónica. Indicá evidencia de su programación, canales públicos de contacto y próximos pasos. Salgo desde el Obelisco a pie, en sectores de cuatro puntos.

## Diseño y operación

Telegram se consulta mediante long polling. SQLite confirma el ingreso de cada actualización antes de avanzar el cursor. Un proceso prepara los informes mientras otro flujo asíncrono sigue recibiendo mensajes. El resultado y su entrega se guardan en una transacción. Reiniciar después de generarlo permite reintentar la entrega sin volver a llamar al modelo. Si se corta durante la generación, el bot lo informa y el usuario decide si reenviar el pedido. Un corte después de que Telegram acepta un envío y antes de registrar ese estado puede duplicar la entrega.

La búsqueda y la composición son llamadas distintas. La búsqueda recibe el objetivo reformulado, con instrucciones para omitir domicilios particulares y datos privados. El historial y el texto de los PDF adjuntos se usan en la conversación. El informe usa enlaces observados en las anotaciones de la búsqueda. El sistema filtra referencias inexistentes y deja fuera de la ruta los domicilios sin evidencia o con una geocodificación ambigua. Cada cita permite inspeccionar qué respalda la fuente.

Las cuotas diarias, el archivo de variables y los parámetros del modelo están en `.env.example`. El presupuesto en dólares se controla en OpenAI Platform, además de las cuotas por usuario en el bot. La aplicación guarda los informes, texto reciente y PDF en el volumen de datos. `/privacidad` explica el uso de proveedores y `/borrar confirmar` elimina el historial del bot. La retención por defecto es de 30 días.

### Despliegue

En **Render**, creá un Web Service Docker, elegí **Free**, configurá `python -m itinerario --webhook` como comando y `/healthz` como comprobación. Render Free suspende el servicio inactivo y lo vuelve a arrancar con un pedido. Guardá el historial y los trabajos en una base PostgreSQL accesible con `DATABASE_URL` por una conexión de sesión. La base gratuita de Render vence a los 30 días y requiere migración si se usa para un piloto. [Render Free](https://render.com/docs/free) · [Web Services](https://render.com/docs/web-services)

Cargá `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`, `GEOAPIFY_API_KEY`, `TELEGRAM_ALLOWED_USERS`, `DATABASE_URL` y un `TELEGRAM_WEBHOOK_SECRET` aleatorio como secretos de ejecución. Configurá `SOURCE_CODE_URL` con el enlace a la versión desplegada. Una vez que `/healthz` responda, detené la instancia local y registrá por Bot API `setWebhook` con la URL HTTPS `/telegram`, el mismo `secret_token`, `allowed_updates` con `message` y `drop_pending_updates` en `false`. [Telegram Bot API](https://core.telegram.org/bots/api#setwebhook)

Para mantener el costo en cero, verificá el plan del servicio, usá los créditos disponibles de OpenAI y mantené deshabilitada la recarga automática en esa organización.

Probá desde Telegram un pedido corto, un informe con mapa por sectores y una repregunta. Reiniciá el worker de prueba y comprobá con `/historial` que el informe siga disponible. Los logs permiten cotejar estos pasos con la entrega del PDF y detectar fallos de Telegram, OpenAI o Geoapify.

Guardá copias mediante la API de backup de SQLite o con el servicio detenido. Una copia del archivo principal mientras el WAL está abierto puede quedar incompleta. Supervisá los rechazos `429`, el espacio de disco y el tiempo de generación. Probá la restauración de informes antes de usar el bot para trabajo importante.

Los logs distinguen `telegram_acuse_ms`, `informe_listo_ms` y `telegram_pdf_ms`. Los tres empiezan cuando el worker recibe el pedido. El primero termina cuando Telegram acepta el acuse, el segundo cuando el PDF se guarda y el último cuando Telegram acepta el documento. La percepción en el teléfono incluye también la red del usuario y la entrega del cliente de Telegram.

### Alcance de la cobertura

La búsqueda social prioriza publicaciones públicas en Instagram, Facebook, Threads, X, TikTok, Reddit, LinkedIn, YouTube, Bluesky y canales públicos indexados de Telegram. El informe señala si la consulta social falló o quedó sin resultados citables. Las fuentes privadas, comentarios no indexados y APIs con acceso restringido requieren integraciones autorizadas por separado.

Las rutas de esta versión optimizan tiempo o distancia vial. Las ventanas de atención, restricciones de carga, paradas y cierres en tiempo real requieren confirmación o planificación específica. El orden propuesto se compara con el orden de descubrimiento cuando ambos se pudieron calcular. Si falla la imagen cartográfica, el PDF marca explícitamente el trazado esquemático.

## Pruebas

```powershell
uv run --locked python -m unittest discover -s tests -v
```

Las pruebas usan respuestas controladas de Telegram, OpenAI y Geoapify. Comprueban contratos, aislamiento de historiales, recuperación, cálculo vial y PDF enlazado. La validación en vivo se realiza aparte con un bot privado.

## Licencia

[GNU AGPL v3](LICENSE). La licencia permite copiar, modificar y alojar el código. Si se ofrece por red una versión modificada, se debe ofrecer su código fuente correspondiente a quienes interactúen con ella. El código, las claves y los datos de cada instalación se gestionan por separado. Las bases cartográficas y fuentes citadas mantienen sus propias licencias.
