"""Cliente para Milanuncios.

No hay API pública: la página de resultados de búsqueda (SSR) incrusta un
bloque `window.__INITIAL_PROPS__ = JSON.parse("...")` con los anuncios ya
en JSON estructurado — no hace falta ninguna llamada AJAX aparte. Se
comprobó que funciona con una petición GET normal, sin cabeceras
especiales ni bloqueo anti-bot (a diferencia de Wallapop).

Estructura y URL confirmadas inspeccionando tráfico real de
www.milanuncios.com (2026-08-15). Puede cambiar sin aviso.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone

import httpx

from .wallapop_client import Listing, SearchPage, SellerInfo

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.milanuncios.com/anuncios/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9",
}

RETRYABLE_STATUS_CODES = {403, 429, 500, 502, 503, 504}

_INITIAL_PROPS_MARKER = "window.__INITIAL_PROPS__ = JSON.parse("


class MilanunciosRequestError(Exception):
    pass


def _extract_initial_props(html: str) -> dict:
    start = html.find(_INITIAL_PROPS_MARKER)
    if start == -1:
        raise MilanunciosRequestError("No se encontró __INITIAL_PROPS__ en la página")
    start += len(_INITIAL_PROPS_MARKER)
    end = html.find(");", start)
    js_string_literal = html[start:end]
    # Es un literal de cadena JS (con comillas y escapes) que contiene JSON
    # escapado dentro; lo más robusto es decodificarlo como JSON de un
    # literal de cadena Python-compatible tras normalizar comillas.
    json_str = json.loads(js_string_literal)
    return json.loads(json_str)


def _parse_km(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def _parse_year(text: str) -> int | None:
    match = re.search(r"(19|20)\d{2}", text or "")
    return int(match.group(0)) if match else None


def _parse_ad(ad: dict) -> Listing | None:
    try:
        price = (ad.get("price") or {}).get("cashPrice", {}).get("value")
        if price is None:
            return None

        tags = {t.get("type"): t.get("text") for t in (ad.get("tags") or [])}
        car_year = _parse_year(tags.get("año", ""))
        car_km = _parse_km(tags.get("kilómetros", ""))

        images = ad.get("images") or []
        # El CDN de imágenes exige un ?rule=<preset> válido o devuelve 404
        # ("Rule parameter not Found") — comprobado en producción, las fotos
        # no llegaban a Telegram sin esto. "hw396_70" es el preset que usa
        # la propia web para las miniaturas del listado.
        image_url = f"https://{images[0]}?rule=hw396_70" if images else None

        publish_date = ad.get("publishDate")
        created_at = None
        if publish_date:
            try:
                created_at = datetime.fromisoformat(publish_date.replace("Z", "+00:00"))
            except ValueError:
                created_at = None

        location = ad.get("location") or {}
        city = (location.get("city") or {}).get("name") or (ad.get("city") or {}).get("name")
        province = (location.get("province") or {}).get("name") or (ad.get("province") or {}).get("name")

        return Listing(
            id=f"milanuncios:{ad['id']}",
            user_id=str(ad["userId"]) if ad.get("userId") else None,
            title=ad.get("title", ""),
            description=ad.get("description", ""),
            price=float(price),
            currency="EUR",
            category_id=100,  # marcador interno compartido con Wallapop para "vehículo" (ver normalizer.CARS_CATEGORY_ID)
            city=city,
            region=province,
            postal_code=None,
            latitude=None,
            longitude=None,
            web_slug=ad.get("url", ""),
            created_at=created_at,
            url=f"https://www.milanuncios.com{ad.get('url', '')}",
            image_url=image_url,
            is_shippable=False,
            car_year=car_year,
            car_km=car_km,
            car_brand=None,
            car_model=None,
            source="milanuncios",
        )
    except (KeyError, TypeError) as exc:
        logger.warning("Anuncio de Milanuncios con formato inesperado, se descarta: %s", exc)
        return None


class MilanunciosClient:
    def __init__(self, max_retries: int = 4, backoff_base_seconds: float = 2.0, request_timeout: float = 15.0):
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self._client = httpx.Client(timeout=request_timeout, headers=HEADERS)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "MilanunciosClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def search(self, keywords: str, start_cursor: str | None = None, **_ignored) -> SearchPage:
        """`start_cursor` se reutiliza como número de página (Milanuncios
        no tiene cursor real, solo `?pagina=N`) para que el pipeline pueda
        paginar con la misma lógica que usa para Wallapop.

        `**_ignored` absorbe kwargs de otras plataformas (min_price,
        category_id, latitude, etc.) para que el pipeline pueda llamar a
        todos los clientes con la misma firma sin ramificar por plataforma.
        """
        page = int(start_cursor) if start_cursor else 1
        params = {"s": keywords}
        if page > 1:
            params["pagina"] = page

        # El parseo del JSON incrustado se reintenta igual que un fallo de
        # red: un 200 sin __INITIAL_PROPS__ (variante de página, hueco
        # momentáneo del servidor) se vio en producción y antes tumbaba la
        # búsqueda sin reintentar, ya que _get_with_retry solo reintenta
        # por código HTTP, no por contenido inesperado.
        attempt = 0
        while True:
            attempt += 1
            html = self._get_with_retry(SEARCH_URL, params)
            try:
                data = _extract_initial_props(html)
                break
            except MilanunciosRequestError:
                if attempt > self.max_retries:
                    raise
                self._sleep_backoff(attempt, "sin __INITIAL_PROPS__ en la respuesta")

        ads = ((data.get("adListPagination") or {}).get("adList") or {}).get("ads") or []
        listings = [l for raw in ads if (l := _parse_ad(raw)) is not None]

        return SearchPage(listings=listings, next_cursor=str(page + 1) if listings else None)

    def get_seller_info(self, user_id: str) -> SellerInfo | None:
        # No implementado para Milanuncios: no se investigó un endpoint de
        # reputación de vendedor equivalente. La comprobación de confianza
        # del vendedor simplemente no aporta info extra para estos anuncios.
        return None

    def _get_with_retry(self, url: str, params: dict) -> str:
        attempt = 0
        while True:
            attempt += 1
            # httpx.Client mantiene cookies entre peticiones por defecto.
            # Comprobado en producción: reutilizar la misma cookie de
            # sesión en varias búsquedas seguidas hace que Milanuncios
            # empiece a devolver páginas sin __INITIAL_PROPS__ (probable
            # limitación por sesión), mientras que peticiones sueltas sin
            # cookie (como curl sin -c/-b) siguen funcionando sin problema.
            # Se limpian antes de cada intento para que cada petición
            # parezca una visita nueva.
            self._client.cookies.clear()
            try:
                response = self._client.get(url, params=params)
            except httpx.RequestError as exc:
                if attempt > self.max_retries:
                    raise MilanunciosRequestError(f"Fallo de red tras {attempt} intentos: {exc}") from exc
                self._sleep_backoff(attempt, str(exc))
                continue

            if response.status_code == 200:
                return response.text

            if response.status_code in RETRYABLE_STATUS_CODES and attempt <= self.max_retries:
                self._sleep_backoff(attempt, f"HTTP {response.status_code}")
                continue

            raise MilanunciosRequestError(
                f"HTTP {response.status_code} tras {attempt} intento(s)."
            )

    def _sleep_backoff(self, attempt: int, reason: str) -> None:
        delay = self.backoff_base_seconds * (2 ** (attempt - 1))
        logger.warning("Reintento %d/%d tras %.1fs (motivo: %s)", attempt, self.max_retries, delay, reason)
        time.sleep(delay)
