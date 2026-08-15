from __future__ import annotations

from dataclasses import dataclass

from .trust import price_dispersion_cv


def percentile(values: list[float], pct: float) -> float:
    """Percentil con interpolación lineal (equivalente a numpy default)."""
    if not values:
        raise ValueError("No se puede calcular un percentil de una lista vacía")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100) * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


# Rareza del chollo según lo grande que sea el descuento frente a la
# mediana del mercado. Puramente cosmético/informativo (no cambia si se
# notifica o no, eso lo decide discount_threshold_pct); solo clasifica lo
# buena que es la ganga para que sea más fácil distinguir un vistazo de una
# oportunidad excepcional. Umbrales configurables en searches.yaml.
RARITY_ORDER = ("legendario", "epico", "raro", "comun")
RARITY_EMOJI = {
    "legendario": "🟡",
    "epico": "🟣",
    "raro": "🔵",
    "comun": "⚪",
}
RARITY_LABEL = {
    "legendario": "LEGENDARIO",
    "epico": "ÉPICO",
    "raro": "RARO",
    "comun": "COMÚN",
}


def classify_rarity(
    discount_pct: float,
    raro_pct: float,
    epico_pct: float,
    legendario_pct: float,
) -> str:
    """Devuelve una de "comun", "raro", "epico", "legendario" según el %
    de descuento. Se asume que discount_pct ya superó discount_threshold_pct
    (si no, no se llega a llamar a esto)."""
    if discount_pct >= legendario_pct:
        return "legendario"
    if discount_pct >= epico_pct:
        return "epico"
    if discount_pct >= raro_pct:
        return "raro"
    return "comun"


@dataclass
class CholloEvaluation:
    is_chollo: bool
    reference_price: float | None
    discount_pct: float | None
    margin_eur: float | None = None
    rarity: str | None = None
    suspicious: bool = False
    suspicious_reasons: tuple[str, ...] = ()


def evaluate_price(
    price: float,
    historical_prices: list[float],
    discount_threshold_pct: float,
    price_percentile: int,
    min_samples: int,
    rarity_raro_pct: float = 45,
    rarity_epico_pct: float = 60,
    rarity_legendario_pct: float = 75,
    min_margin_eur: float = 0,
    scam_discount_threshold_pct: float = 85,
    condition_extra_pct: float = 0,
    max_reference_cv: float = 0.6,
) -> CholloEvaluation:
    """Compara `price` contra el histórico de precios del mismo tipo de
    producto y decide si es un chollo.

    Si no hay suficiente histórico (`min_samples`), no se puede calcular
    un precio de referencia fiable y se devuelve is_chollo=False.

    Filtros pensados para "flipping" real, no solo % bonito:
    - `min_margin_eur`: un 60% de descuento en un artículo de 5€ da 3€ de
      margen, no vale la pena la gestión. Exigir un mínimo en euros, no
      solo en porcentaje.
    - `condition_extra_pct`: si el título/descripción sugiere peor estado
      de lo normal (ver trust.py), hace falta más descuento del habitual
      para que cuente como chollo real y no como "precio normal para su
      estado".
    - `max_reference_cv`: si el histórico de precios de ese product_key
      está muy disperso (coeficiente de variación alto), probablemente el
      agrupamiento ha mezclado productos distintos y la mediana no es de
      fiar — no se marca como chollo aunque el % de descuento diera para
      ello, mejor no decir nada que dar un aviso engañoso.
    - `scam_discount_threshold_pct`: un descuento absurdamente alto (>85%
      por defecto) es estadísticamente mucho más probable que sea un
      anuncio con foto/precio erróneo, un timo o un artículo robado que un
      chollo real. Se sigue marcando como chollo (por si acaso es real),
      pero con `suspicious=True` para que la alerta avise en vez de
      generar hype ciego.
    """
    if len(historical_prices) < min_samples:
        return CholloEvaluation(is_chollo=False, reference_price=None, discount_pct=None)

    if price_dispersion_cv(historical_prices) > max_reference_cv:
        return CholloEvaluation(is_chollo=False, reference_price=None, discount_pct=None)

    reference_price = percentile(historical_prices, price_percentile)
    if reference_price <= 0:
        return CholloEvaluation(is_chollo=False, reference_price=reference_price, discount_pct=None)

    discount_pct = (1 - price / reference_price) * 100
    margin_eur = reference_price - price
    effective_threshold = discount_threshold_pct + condition_extra_pct
    is_chollo = discount_pct >= effective_threshold and margin_eur >= min_margin_eur
    rarity = (
        classify_rarity(discount_pct, rarity_raro_pct, rarity_epico_pct, rarity_legendario_pct)
        if is_chollo
        else None
    )

    suspicious_reasons = []
    if is_chollo and discount_pct >= scam_discount_threshold_pct:
        suspicious_reasons.append("descuento inusualmente alto")

    return CholloEvaluation(
        is_chollo=is_chollo,
        reference_price=reference_price,
        discount_pct=discount_pct,
        margin_eur=margin_eur,
        rarity=rarity,
        suspicious=bool(suspicious_reasons),
        suspicious_reasons=tuple(suspicious_reasons),
    )
