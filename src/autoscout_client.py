"""Cliente para AutoScout24 España.

Sin API pública ni bloqueo anti-bot detectado: la página de resultados
(Next.js) incrusta los datos en `<script id="__NEXT_DATA__">`. A
diferencia de Wallapop/Milanuncios, el parámetro de texto libre ("q") NO
filtra por contenido del anuncio (comprobado: devuelve el catálogo
genérico sin importar el texto) — solo permite filtrar por atributos
estructurados. Por eso aquí se filtra por carrocería "furgoneta" (body=13,
confirmado inspeccionando la respuesta) y es el pipeline quien decide,
con trust.is_confirmed_adapted_vehicle, cuáles de esas furgonetas están
realmente adaptadas.

Limitación conocida: el listado no trae descripción completa del
anuncio (solo un subtítulo corto de equipamiento), así que muchas menos
furgonetas pasarán el filtro de "adaptada confirmada" aquí que en
Wallapop/Milanuncios, donde sí hay descripción completa.

Confirmado inspeccionando tráfico real de www.autoscout24.es (2026-08-15).
Puede cambiar sin aviso.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime

import httpx

from .wallapop_client import Listing, SearchPage, SellerInfo

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.autoscout24.es/lst"
VAN_BODY_TYPE = 13  # confirmado: listHeaderTitle dice "... coches de segunda mano furgoneta"
PAGE_SIZE = 20

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9",
}

RETRYABLE_STATUS_CODES = {403, 429, 500, 502, 503, 504}

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)


class AutoScoutRequestError(Exception):
    pass


def _extract_next_data(html: str) -> dict:
    match = _NEXT_DATA_RE.search(html)
    if not match:
        raise AutoScoutRequestError("No se encontró __NEXT_DATA__ en la página")
    return json.loads(match.group(1))


def _find_year(vehicle_details: list[dict]) -> int | None:
    for detail in vehicle_details or []:
        if detail.get("ariaLabel") == "Año":
            match = re.search(r"(19|20)\d{2}", detail.get("data", ""))
            if match:
                return int(match.group(0))
    return None


def _parse_km(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def _parse_listing(raw: dict) -> Listing | None:
    try:
        vehicle = raw.get("vehicle") or {}
        price = (raw.get("price") or {}).get("priceRaw")
        if price is None:
            return None

        location = raw.get("location") or {}
        images = raw.get("images") or []

        title = " ".join(
            filter(None, [vehicle.get("make"), vehicle.get("model"), vehicle.get("modelVersionInput")])
        )

        return Listing(
            id=f"autoscout24:{raw['id']}",
            user_id=None,
            title=title,
            description=vehicle.get("subtitle", "") or "",
            price=float(price),
            currency="EUR",
            category_id=100,  # marcador interno compartido con Wallapop para "vehículo"
            city=location.get("city"),
            region=None,
            postal_code=location.get("zip"),
            latitude=None,
            longitude=None,
            web_slug=raw.get("url", ""),
            created_at=None,  # no disponible en el listado resumido
            url=f"https://www.autoscout24.es{raw.get('url', '')}",
            image_url=images[0] if images else None,
            is_shippable=False,
            car_year=_find_year(raw.get("vehicleDetails")),
            car_km=_parse_km(vehicle.get("mileageInKm", "")),
            car_brand=vehicle.get("make"),
            car_model=vehicle.get("model"),
            source="autoscout24",
        )
    except (KeyError, TypeError) as exc:
        logger.warning("Anuncio de AutoScout24 con formato inesperado, se descarta: %s", exc)
        return None


class AutoScoutClient:
    def __init__(self, max_retries: int = 4, backoff_base_seconds: float = 2.0, request_timeout: float = 15.0):
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self._client = httpx.Client(timeout=request_timeout, headers=HEADERS)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AutoScoutClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def search(self, keywords: str = "", start_cursor: str | None = None, **_ignored) -> SearchPage:
        """`keywords` se ignora (ver docstring del módulo: AutoScout24 no
        filtra por texto libre) — la búsqueda real es por carrocería
        "furgoneta"; el filtrado por adaptación lo hace el pipeline sobre
        el resultado, igual que con las demás plataformas.
        `start_cursor` se reutiliza como número de página."""
        page = int(start_cursor) if start_cursor else 1
        params = {
            "atype": "C",
            "body": VAN_BODY_TYPE,
            "cy": "E",
            "size": PAGE_SIZE,
            "page": page,
        }

        html = self._get_with_retry(SEARCH_URL, params)
        data = _extract_next_data(html)
        page_props = data.get("props", {}).get("pageProps", {})
        raw_listings = page_props.get("listings") or []
        total_pages = page_props.get("numberOfPages") or 1

        listings = [l for raw in raw_listings if (l := _parse_listing(raw)) is not None]
        next_cursor = str(page + 1) if listings and page < total_pages else None

        return SearchPage(listings=listings, next_cursor=next_cursor)

    def get_seller_info(self, user_id: str) -> SellerInfo | None:
        # No implementado: no se investigó un endpoint de reputación de
        # vendedor para AutoScout24.
        return None

    def _get_with_retry(self, url: str, params: dict) -> str:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._client.get(url, params=params)
            except httpx.RequestError as exc:
                if attempt > self.max_retries:
                    raise AutoScoutRequestError(f"Fallo de red tras {attempt} intentos: {exc}") from exc
                self._sleep_backoff(attempt, str(exc))
                continue

            if response.status_code == 200:
                return response.text

            if response.status_code in RETRYABLE_STATUS_CODES and attempt <= self.max_retries:
                self._sleep_backoff(attempt, f"HTTP {response.status_code}")
                continue

            raise AutoScoutRequestError(
                f"HTTP {response.status_code} tras {attempt} intento(s)."
            )

    def _sleep_backoff(self, attempt: int, reason: str) -> None:
        delay = self.backoff_base_seconds * (2 ** (attempt - 1))
        logger.warning("Reintento %d/%d tras %.1fs (motivo: %s)", attempt, self.max_retries, delay, reason)
        time.sleep(delay)
