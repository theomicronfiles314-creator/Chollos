from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.wallapop.com/api/v3/search"
USER_URL = "https://api.wallapop.com/api/v3/users/{user_id}"
USER_REVIEWS_URL = "https://api.wallapop.com/api/v3/users/{user_id}/reviews"

# Confirmed by inspecting real traffic from es.wallapop.com (2026-07-26).
# Without X-DeviceOS + a browser-like User-Agent, api.wallapop.com's
# CloudFront/WAF layer returns 403 even for requests that carry valid
# session cookies.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
]

BASE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "X-DeviceOS": "0",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

RETRYABLE_STATUS_CODES = {403, 429, 500, 502, 503, 504}


class WallapopRequestError(Exception):
    pass


@dataclass
class Listing:
    id: str
    user_id: str | None
    title: str
    description: str
    price: float
    currency: str
    category_id: int | None
    city: str | None
    region: str | None
    postal_code: str | None
    latitude: float | None
    longitude: float | None
    web_slug: str
    created_at: datetime | None
    url: str
    image_url: str | None
    is_shippable: bool
    car_year: int | None = None
    car_km: int | None = None
    car_brand: str | None = None
    car_model: str | None = None
    source: str = "wallapop"

    @property
    def item_url(self) -> str:
        return self.url


@dataclass
class SearchPage:
    listings: list[Listing]
    next_cursor: str | None


@dataclass
class SellerInfo:
    account_age_days: float | None
    review_count: int
    avg_rating_over_five: float | None
    verified: bool


def _random_headers() -> dict[str, str]:
    headers = dict(BASE_HEADERS)
    headers["User-Agent"] = random.choice(USER_AGENTS)
    return headers


def _parse_item(raw: dict) -> Listing | None:
    try:
        price = raw["price"]["amount"]
        currency = raw["price"]["currency"]
        location = raw.get("location") or {}
        web_slug = raw["web_slug"]
        created_at_ms = raw.get("created_at")
        created_at = (
            datetime.fromtimestamp(created_at_ms / 1000, tz=timezone.utc)
            if created_at_ms
            else None
        )
        images = raw.get("images") or []
        image_url = None
        if images:
            urls = images[0].get("urls") or {}
            image_url = urls.get("big") or urls.get("medium") or urls.get("small")
        shipping = raw.get("shipping") or {}
        type_attributes = raw.get("type_attributes") or {}

        return Listing(
            id=raw["id"],
            user_id=raw.get("user_id"),
            title=raw.get("title", ""),
            description=raw.get("description", ""),
            price=float(price),
            currency=currency,
            category_id=raw.get("category_id"),
            city=location.get("city"),
            region=location.get("region"),
            postal_code=location.get("postal_code"),
            latitude=location.get("latitude"),
            longitude=location.get("longitude"),
            web_slug=web_slug,
            created_at=created_at,
            url=f"https://es.wallapop.com/item/{web_slug}",
            image_url=image_url,
            is_shippable=bool(shipping.get("item_is_shippable")),
            car_year=type_attributes.get("year"),
            car_km=type_attributes.get("km"),
            car_brand=type_attributes.get("brand"),
            car_model=type_attributes.get("model"),
        )
    except (KeyError, TypeError) as exc:
        logger.warning("Item con formato inesperado, se descarta: %s", exc)
        return None


class WallapopClient:
    """Cliente para el endpoint de búsqueda interno de Wallapop.

    No es una API pública/oficial: la estructura de la respuesta y las
    cabeceras requeridas se obtuvieron inspeccionando el tráfico real de
    es.wallapop.com y pueden cambiar sin aviso.
    """

    def __init__(
        self,
        max_retries: int = 4,
        backoff_base_seconds: float = 2.0,
        request_timeout: float = 15.0,
    ):
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self._client = httpx.Client(timeout=request_timeout, http2=True)
        # Un único User-Agent por instancia/sesión: el cursor de paginación
        # (start_cursor) que devuelve Wallapop deja de ser válido si las
        # peticiones siguientes llegan con una huella de cliente distinta
        # (comprobado empíricamente: rotar el UA entre páginas hace que el
        # servidor ignore el cursor y devuelva la página 1 de nuevo).
        self._session_headers = _random_headers()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "WallapopClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def search(
        self,
        keywords: str,
        latitude: float,
        longitude: float,
        order_by: str = "newest",
        min_price: float | None = None,
        max_price: float | None = None,
        category_id: int | None = None,
        min_year: int | None = None,
        max_km: int | None = None,
        start_cursor: str | None = None,
    ) -> SearchPage:
        params: dict[str, str | float] = {
            "keywords": keywords,
            "latitude": latitude,
            "longitude": longitude,
            "source": "search_box",
            "order_by": order_by,
        }
        if min_price is not None:
            params["min_sale_price"] = int(min_price)
        if max_price is not None:
            params["max_sale_price"] = int(max_price)
        if category_id is not None:
            params["category_id"] = category_id
        if min_year is not None:
            params["min_year"] = int(min_year)
        if max_km is not None:
            params["max_km"] = int(max_km)
        if start_cursor:
            params["start_cursor"] = start_cursor

        data = self._get_with_retry(SEARCH_URL, params)

        payload = data.get("data", {}).get("section", {}).get("payload", {})
        raw_items = payload.get("items", []) or []
        listings = [l for raw in raw_items if (l := _parse_item(raw)) is not None]
        next_cursor = data.get("meta", {}).get("next_page")

        return SearchPage(listings=listings, next_cursor=next_cursor)

    def get_seller_info(self, user_id: str) -> SellerInfo | None:
        """Antigüedad de cuenta y valoraciones del vendedor.

        Pensado para llamarse solo para anuncios que YA han pasado el
        filtro de precio (unos pocos por ciclo), no para cada anuncio
        escaneado — si no, multiplicaría por varias veces el número de
        peticiones por ciclo.
        """
        try:
            user_data = self._get_with_retry(
                USER_URL.format(user_id=user_id), params={}
            )
            reviews_data = self._get_with_retry(
                USER_REVIEWS_URL.format(user_id=user_id), params={"init": 0}
            )
        except WallapopRequestError:
            logger.warning("No se pudo obtener info del vendedor %s", user_id)
            return None

        register_date_ms = user_data.get("register_date")
        account_age_days = None
        if register_date_ms:
            registered_at = datetime.fromtimestamp(register_date_ms / 1000, tz=timezone.utc)
            account_age_days = (datetime.now(timezone.utc) - registered_at).total_seconds() / 86400

        reviews = reviews_data if isinstance(reviews_data, list) else []
        ratings = [
            r["review"]["rating_over_five"]
            for r in reviews
            if isinstance(r, dict) and r.get("review", {}).get("rating_over_five") is not None
        ]
        avg_rating = sum(ratings) / len(ratings) if ratings else None

        seller_type = user_data.get("seller_type") or {}

        return SellerInfo(
            account_age_days=account_age_days,
            review_count=len(reviews),
            avg_rating_over_five=avg_rating,
            verified=bool(seller_type.get("verified")),
        )

    def _get_with_retry(self, url: str, params: dict) -> dict:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._client.get(url, params=params, headers=self._session_headers)
            except httpx.RequestError as exc:
                if attempt > self.max_retries:
                    raise WallapopRequestError(
                        f"Fallo de red tras {attempt} intentos: {exc}"
                    ) from exc
                self._sleep_backoff(attempt, reason=str(exc))
                continue

            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise WallapopRequestError(
                        f"Respuesta 200 pero JSON inválido: {exc}"
                    ) from exc

            if response.status_code in RETRYABLE_STATUS_CODES and attempt <= self.max_retries:
                retry_after = response.headers.get("Retry-After")
                self._sleep_backoff(attempt, reason=f"HTTP {response.status_code}", retry_after=retry_after)
                continue

            raise WallapopRequestError(
                f"HTTP {response.status_code} tras {attempt} intento(s). "
                f"Body: {response.text[:300]!r}"
            )

    def _sleep_backoff(self, attempt: int, reason: str, retry_after: str | None = None) -> None:
        if retry_after is not None:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = self.backoff_base_seconds * (2 ** (attempt - 1))
        else:
            delay = self.backoff_base_seconds * (2 ** (attempt - 1))
        delay += random.uniform(0, 1)
        logger.warning(
            "Reintento %d/%d tras %.1fs (motivo: %s)",
            attempt,
            self.max_retries,
            delay,
            reason,
        )
        time.sleep(delay)
