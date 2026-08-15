from __future__ import annotations

import re
import unicodedata

# Palabras de relleno / marketing habituales en títulos de Wallapop que no
# aportan a identificar el tipo de producto (y por tanto no deben influir
# en el agrupamiento para comparar precios).
STOPWORDS = {
    "nuevo", "nueva", "nuevos", "nuevas", "seminuevo", "seminueva",
    "como", "perfecto", "perfecta", "estado", "impecable", "impoluto",
    "urge", "urgente", "oferta", "ofertas", "envio", "envios", "incluido",
    "incluida", "incluye", "con", "sin", "caja", "original", "originales",
    "libre", "liberado", "liberada", "garantia", "factura", "precio",
    "negociable", "vendo", "vende", "regalo", "gratis", "promo",
    "promocion", "oportunidad", "unico", "unica", "ideal", "barato",
    "barata", "rebajado", "rebajada", "super", "top", "full", "real",
    "de", "el", "la", "los", "las", "un", "una", "para", "por", "en",
    "y", "o", "a", "muy",
}

# Se conserva el orden de aparición y se limita a los primeros N tokens
# significativos: en la mayoría de títulos ("iPhone 13 128GB Verde,
# impecable, con caja...") la marca/modelo/capacidad van al principio y
# el resto es relleno.
MAX_KEY_TOKENS = 6

# Palabras que indican que el anuncio NO es el producto completo, sino solo
# un accesorio suelto, una pieza, o algo roto/incompleto — no debe poder
# marcarse nunca como "chollo" comparándolo contra el precio del producto
# entero (p.ej. "Estuche AirPods Pro" a 25€ no es un 65% de descuento sobre
# unos AirPods Pro completos, es el precio normal de un estuche suelto).
# Deliberadamente NO están en STOPWORDS: tienen que seguir formando parte
# de la product_key para que, aun sin este filtro, no se agrupen con el
# producto completo.
ACCESSORY_OR_BROKEN_MARKERS = {
    "estuche", "funda", "carcasa", "cargador", "cable", "correa",
    "protector", "repuesto", "recambio", "despiece",
    "roto", "rota", "rotos", "rotas", "averiado", "averiada",
    "piezas", "resto",
}


def is_accessory_or_broken(title: str) -> bool:
    """True si el título sugiere que es solo un accesorio suelto, una
    pieza, o el artículo está roto/incompleto — nunca debería compararse
    su precio contra el del producto completo y en buen estado."""
    tokens = set(clean_title(title).split())
    return bool(tokens & ACCESSORY_OR_BROKEN_MARKERS)


# "Se alquila furgoneta adaptada... 90€" — un precio de ALQUILER (diario o
# mensual) mezclado con precios de COMPRA destrozaría la mediana (90€ no es
# un 99% de descuento, es una tarifa de un día). Detectado en producción
# con datos reales de Milanuncios.
RENTAL_MARKERS = {"alquiler", "alquila", "alquilo", "renting", "arrendamiento"}

# "VOLKSWAGEN COMPRAMOS VEHÍCULOS ADAPTADOS" — un anuncio de un comprador
# ofreciéndose a COMPRAR furgonetas, no una furgoneta en venta. Pasa el
# filtro de "menciona adaptación/PMR" porque legítimamente habla de eso,
# pero su "precio" no es el precio de compra de nada real. Detectado en
# producción con datos reales de Milanuncios.
BUYBACK_PHRASES = [
    "compramos vehiculos", "compramos furgonetas", "compramos coches",
    "compra de vehiculos", "compra tu vehiculo", "se compra furgoneta",
    "se compra vehiculo", "tasamos tu vehiculo", "compro furgoneta",
    "compro vehiculo adaptado",
]


def is_buyback_listing(title: str, description: str = "") -> bool:
    """True si el anuncio es de alguien OFRECIÉNDOSE a comprar, no vendiendo."""
    text = _strip_accents(f"{title} {description or ''}".lower())
    return any(p in text for p in BUYBACK_PHRASES)


def is_rental_listing(title: str, description: str = "") -> bool:
    """True si el anuncio es de alquiler, no de venta."""
    tokens = set(clean_title(f"{title} {description or ''}").split())
    return bool(tokens & RENTAL_MARKERS)


def _strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(c for c in normalized if not unicodedata.combining(c))


def clean_title(title: str) -> str:
    text = _strip_accents(title.lower())
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    tokens = [t for t in text.split() if t and t not in STOPWORDS]
    return " ".join(tokens)


CARS_CATEGORY_ID = 100

# Franjas de años para coches/furgonetas: agrupar por texto de título no
# tiene sentido aquí (un Renault Master 2011 y uno 2024 no son comparables
# aunque el título sea casi idéntico), pero agrupar por año EXACTO deja
# cada grupo con 0-1 anuncios para siempre en una categoría de tan poco
# volumen como "furgoneta adaptada". Las franjas son la solución intermedia.
CAR_YEAR_BUCKETS = [
    (2022, "2022+"),
    (2018, "2018-2021"),
    (2014, "2014-2017"),
    (0, "pre-2014"),
]


def car_year_bucket(year: int) -> str:
    for threshold, label in CAR_YEAR_BUCKETS:
        if year >= threshold:
            return label
    return "pre-2014"


def product_key(
    category_id: int | None,
    title: str,
    car_year: int | None = None,
    car_is_adapted: bool = True,
) -> str:
    """Clave heurística para agrupar anuncios equivalentes.

    Para coches/furgonetas (category_id=100) se agrupa por franja de año en
    vez de por texto del título: el precio de un vehículo depende sobre
    todo de su antigüedad, así que comparar contra la mediana de su misma
    franja de años es lo que hace la comparación justa. Se ignora
    deliberadamente la marca/modelo para tener suficiente volumen de
    muestra en una búsqueda de nicho como "furgoneta adaptada" — es una
    media del segmento, no de un modelo concreto.

    `car_is_adapted` separa las furgonetas confirmadas como adaptadas
    (mencionan PMR/discapacidad/rampa/etc.) de las que solo colaron por
    relevancia de texto de Wallapop pero son furgonetas de carga
    normales — si se mezclaran en la misma clave, las furgonetas sin
    adaptar (más baratas) contaminarían la mediana de las sí adaptadas
    para siempre, haciendo parecer "chollo" lo que solo es un vehículo
    distinto al que se busca.

    Para el resto de categorías: aproximación por texto — misma categoría +
    primeras palabras significativas del título ya limpiado, ORDENADAS
    alfabéticamente.

    Se ordenan a propósito: "mando ps5 dualsense" y "mando dualsense ps5
    sony" deberían ser el mismo producto, y con el orden de aparición tal
    cual salían en claves distintas (mucha fragmentación observada en la
    práctica: con 769 anuncios guardados salieron 699 claves distintas).
    Ordenar alfabéticamente los primeros N tokens hace que el orden de las
    palabras dentro del título deje de importar para el agrupamiento.
    """
    if category_id == CARS_CATEGORY_ID and car_year:
        segment = "furgoneta_adaptada" if car_is_adapted else "furgoneta_sin_confirmar"
        return f"{category_id}:{segment}:{car_year_bucket(car_year)}"

    cleaned = clean_title(title)
    key_tokens = sorted(cleaned.split()[:MAX_KEY_TOKENS])
    category_part = str(category_id) if category_id is not None else "unknown"
    return f"{category_part}:{' '.join(key_tokens)}"
