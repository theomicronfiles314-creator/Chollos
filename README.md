# wallapop-chollos-bot

Bot que vigila búsquedas en Wallapop y avisa por Telegram cuando aparece un
anuncio con precio anómalamente bajo respecto al histórico reciente de ese
mismo tipo de producto.

Usa el endpoint interno `https://api.wallapop.com/api/v3/search` (no es una
API pública ni documentada por Wallapop: se obtuvo inspeccionando el
tráfico real de es.wallapop.com). Puede cambiar o bloquearse sin aviso.

## Instalación

```bash
python -m venv .venv
.venv\Scripts\activate      # Windows
pip install -r requirements.txt
cp .env.example .env        # y rellena TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
```

Para obtener `TELEGRAM_BOT_TOKEN`: habla con [@BotFather](https://t.me/BotFather)
y crea un bot con `/newbot`. Para `TELEGRAM_CHAT_ID`: escribe algo a tu bot y
visita `https://api.telegram.org/bot<TOKEN>/getUpdates` para leer tu chat id.

## Configuración

Edita [`config/searches.yaml`](config/searches.yaml): ahí van tanto los
parámetros generales (`settings`) como la lista de búsquedas a vigilar
(`searches`). Cada búsqueda soporta `keywords`, `min_price`, `max_price`,
`category_id` y `order_by`.

## Uso

Ejecutar un único ciclo (útil para probar):

```bash
python -m src.main --once
```

Ejecutar en bucle según `interval_minutes` de la config:

```bash
python -m src.main
```

## Cómo decide qué es un "chollo"

1. Cada anuncio nuevo se agrupa por `product_key` = categoría + primeras
   palabras significativas del título ya limpiado de relleno/marketing
   (ver [`src/normalizer.py`](src/normalizer.py)). Es una heurística de
   texto, no un catálogo de productos: agrupa razonablemente bien modelos
   iguales, pero no es perfecta.
2. Se calcula la mediana (o el percentil configurado en
   `price_percentile`) de los precios históricos guardados en SQLite para
   esa `product_key`.
3. Si hay al menos `min_samples_for_reference` anuncios históricos y el
   precio nuevo está `discount_threshold_pct`% o más por debajo de esa
   referencia, se marca como chollo y se notifica por Telegram.

Con la base de datos vacía (primera vez), no habrá suficiente histórico
para ninguna `product_key` y no se detectará ningún chollo hasta que se
acumulen unos cuantos ciclos de datos.

## Estructura

```
config/searches.yaml   # búsquedas a vigilar + parámetros
src/wallapop_client.py # cliente HTTP con retries/backoff
src/normalizer.py      # título -> product_key
src/pricing.py         # mediana/percentil + umbral de chollo
src/db.py              # esquema y acceso a SQLite
src/notifier.py        # alertas por Telegram
src/pipeline.py        # un ciclo completo: buscar -> analizar -> notificar
src/main.py            # CLI + scheduler (APScheduler)
```

## Limitaciones conocidas

- El endpoint no es oficial: si Wallapop cambia su WAF o la estructura de
  respuesta, el bot dejará de funcionar hasta actualizar
  `src/wallapop_client.py`.
- La agrupación de "tipo de producto" es heurística por texto; productos
  con títulos muy distintos pero equivalentes no se agruparán bien, y
  ocasionalmente productos distintos con títulos parecidos sí.
- Respeta un delay aleatorio entre peticiones (`request_delay_range`) para
  no ser agresivo; no bajes esos valores a 0.
