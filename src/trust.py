"""Heurísticas de "criterio" más allá del precio puro: estado del producto,
lenguaje típico de estafa, y fiabilidad del vendedor.

Son heurísticas de texto simples (no NLU real), con limitaciones conocidas
documentadas en cada función — mejor que nada, no infalibles.
"""

from __future__ import annotations

import statistics
import unicodedata
from dataclasses import dataclass

from .wallapop_client import SellerInfo


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower()


# Señales de que el artículo está en mejor estado de lo normal: si aparecen,
# no se penaliza aunque también hubiera alguna palabra de "usado".
PREMIUM_PHRASES = [
    "precintado", "precintada", "sellado", "sellada", "a estrenar",
    "sin estrenar", "nunca usado", "nunca usada", "con etiqueta",
    "con etiquetas", "nuevo a estrenar",
]

# Señales de peor estado del habitual: si el título/descripción las
# contiene, un precio bajo no es un chollo, es el precio normal para ese
# estado. Limitación conocida: no detecta negaciones ("sin arañazos" cuenta
# igual que "con arañazos") por ser un match de texto simple.
WORN_PHRASES = [
    "usado", "usada", "señales de uso", "marcas de uso", "arañazos",
    "aranazos", "rayado", "rayada", "rayones", "desgaste", "desgastado",
    "desgastada", "golpe", "golpes", "abolladura", "amarillento",
    "amarillenta", "sin caja", "sin cargador", "no incluye", "batería degradada",
    "bateria degradada", "funciona pero", "algun fallo", "algún fallo",
]

CONDITION_WORN_MODIFIER_PCT = 15.0


def condition_discount_modifier(title: str, description: str) -> float:
    """Puntos porcentuales EXTRA de descuento que hay que exigir para
    considerar un anuncio chollo, según señales de estado en el texto.

    Un "usado, con arañazos" barato no es un chollo: es el precio de
    mercado normal para ese estado. Se exige más margen de descuento antes
    de confiar en que de verdad es una ganga y no solo un artículo tocado.
    """
    text = _normalize(f"{title} {description or ''}")
    if any(p in text for p in PREMIUM_PHRASES):
        return 0.0
    if any(p in text for p in WORN_PHRASES):
        return CONDITION_WORN_MODIFIER_PCT
    return 0.0


# Frases típicas de estafas o transacciones fuera de la plataforma (donde
# Wallapop ya no puede mediar si algo sale mal). Ver un precio muy bajo
# JUNTO a alguna de estas frases es una señal de alarma mucho más fuerte
# que el precio por sí solo.
SCAM_PHRASES = [
    "whatsapp", "fuera de la app", "fuera de wallapop", "contactar por telegram",
    "transferencia antes", "bizum antes", "pago por adelantado",
    "envio ya pagado", "envío ya pagado", "gestoria de envio", "gestoria de envío",
    "empresa de transporte externa", "motivo de viaje urgente",
    "no puedo enseñarlo en persona", "no puedo enseñarlo", "vendo por urgencia medica",
    "necesito el dinero hoy",
]


def has_scam_language(title: str, description: str) -> bool:
    """Heurística simple de texto — falsos negativos esperables (un
    estafador puede simplemente no usar estas frases), pero es una capa
    extra barata sobre el aviso ya existente por descuento demasiado alto."""
    text = _normalize(f"{title} {description or ''}")
    return any(p in text for p in SCAM_PHRASES)


# Wallapop hace matching por relevancia, no por frase exacta: una búsqueda
# de "furgoneta adaptada" también devuelve furgonetas de carga normales de
# concesionario que ni mencionan discapacidad (comprobado en la práctica:
# de 8 "chollos" en la primera pasada, ninguno mencionaba adaptación en el
# título, y la descripción del más barato confirmó que era una furgoneta de
# carga estándar sin adaptar). Comparar su precio contra la mediana de
# furgonetas SÍ adaptadas (más caras por la conversión) las hace parecer
# gangas enormes cuando en realidad no son ni el vehículo que se busca.
#
# IMPORTANTE: "adaptada"/"adaptado" y "homologada"/"homologado" a secas NO
# sirven como marcador — se comprobó en producción que el boilerplate de
# financiación de los concesionarios dice literalmente "financiación 100%
# personalizada y ADAPTADA a sus necesidades", lo que colaba CUALQUIER
# furgoneta de concesionario como "adaptada". Hacen falta frases compuestas
# específicas de movilidad/discapacidad, no la palabra suelta.
ADAPTED_VEHICLE_MARKERS = [
    "pmr", "discapacidad", "discapacitados", "discapacitado",
    "silla de ruedas", "movilidad reducida", "minusvalidos", "minusvalido",
    "adaptada pmr", "adaptado pmr", "adaptada a pmr", "adaptado a pmr",
    "pmr adaptada", "pmr adaptado",
    "adaptada para discapacitados", "adaptado para discapacitados",
    "adaptada discapacidad", "adaptado discapacidad",
    "adaptada minusvalidos", "adaptado minusvalidos",
    "rampa de acceso", "rampa movilidad", "rampa para silla",
    "elevador para silla", "elevador de silla", "grua embarcada",
    "grua para silla", "homologada pmr", "homologado pmr",
]


def is_confirmed_adapted_vehicle(title: str, description: str) -> bool:
    """True solo si el anuncio menciona explícitamente adaptación para
    movilidad reducida. Si no, aunque el precio parezca un chollo, no se
    puede confiar en que sea el tipo de vehículo que se busca."""
    text = _normalize(f"{title} {description or ''}")
    return any(m in text for m in ADAPTED_VEHICLE_MARKERS)


def price_dispersion_cv(historical_prices: list[float]) -> float:
    """Coeficiente de variación (desviación típica / media) del histórico
    de precios de un product_key. Alto = los precios están muy dispersos,
    señal de que el agrupamiento ha mezclado cosas distintas (o hay mucha
    variedad de estado/modelo) y la mediana no es una referencia fiable."""
    if len(historical_prices) < 2:
        return 0.0
    mean = statistics.mean(historical_prices)
    if mean <= 0:
        return 0.0
    return statistics.stdev(historical_prices) / mean


@dataclass
class SellerTrustResult:
    is_risky: bool
    reason: str | None
    account_age_days: float | None
    review_count: int
    avg_rating: float | None


def evaluate_seller(
    seller: SellerInfo | None,
    min_account_age_days: float,
    min_reviews: float,
    min_avg_rating: float,
) -> SellerTrustResult:
    """Cuenta nueva + sin valoraciones vendiendo algo muy barato es la
    combinación clásica de scam en marketplaces de segunda mano. No es
    prueba de nada por sí sola (todo vendedor fue nuevo alguna vez), pero
    combinada con un descuento ya sospechoso, refuerza la alerta."""
    if seller is None:
        return SellerTrustResult(False, None, None, 0, None)

    reasons = []
    if seller.account_age_days is not None and seller.account_age_days < min_account_age_days:
        reasons.append(f"cuenta creada hace {seller.account_age_days:.0f} días")
    if seller.review_count < min_reviews:
        reasons.append(f"solo {seller.review_count} valoraciones")
    if seller.avg_rating_over_five is not None and seller.avg_rating_over_five < min_avg_rating:
        reasons.append(f"valoración media {seller.avg_rating_over_five:.1f}/5")

    is_risky = len(reasons) >= 2  # una sola señal débil no basta, dos ya preocupa
    return SellerTrustResult(
        is_risky=is_risky,
        reason=", ".join(reasons) if is_risky else None,
        account_age_days=seller.account_age_days,
        review_count=seller.review_count,
        avg_rating=seller.avg_rating_over_five,
    )
