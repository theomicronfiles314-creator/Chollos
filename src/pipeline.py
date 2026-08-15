from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import db, trust
from .autoscout_client import AutoScoutClient, AutoScoutRequestError
from .config import AppConfig, SearchConfig, Settings
from .geo import haversine_km
from .milanuncios_client import MilanunciosClient, MilanunciosRequestError
from .normalizer import CARS_CATEGORY_ID, is_accessory_or_broken, is_buyback_listing, is_rental_listing
from .normalizer import product_key as make_product_key
from .notifier import TelegramNotifier
from .pricing import evaluate_price
from .wallapop_client import WallapopClient, WallapopRequestError

logger = logging.getLogger(__name__)

REQUEST_ERRORS = (WallapopRequestError, MilanunciosRequestError, AutoScoutRequestError)

# Los anuncios de coches de concesionarios profesionales a veces exponen en
# el campo de precio la cuota de financiación mensual en vez del precio de
# venta (visto en la práctica: un anuncio con "Precio al contado: 21490€"
# en la descripción pero price.amount=199). Por debajo de este umbral, un
# vehículo no es un chollo, es un dato mal formado — se descarta de la
# comparación de precios, aunque se sigue guardando el anuncio.
MIN_PLAUSIBLE_CAR_PRICE = 1000


def _sleep_politely(settings: Settings) -> None:
    low, high = settings.request_delay_range
    time.sleep(random.uniform(low, high))


def _run_search(
    search: SearchConfig,
    settings: Settings,
    client: WallapopClient | MilanunciosClient | AutoScoutClient,
    conn,
    notifier: TelegramNotifier | None,
) -> tuple[int, int]:
    """Ejecuta una búsqueda paginando hasta max_pages_per_search.

    Devuelve (anuncios_nuevos, chollos_detectados).
    """
    new_count = 0
    chollo_count = 0
    cursor: str | None = None

    for page_num in range(settings.max_pages_per_search):
        page = client.search(
            keywords=search.keywords,
            latitude=settings.latitude,
            longitude=settings.longitude,
            order_by=search.order_by,
            min_price=search.min_price,
            max_price=search.max_price,
            category_id=search.category_id,
            min_year=search.min_year,
            max_km=search.max_km,
            start_cursor=cursor,
        )

        new_on_this_page = 0
        for listing in page.listings:
            if db.listing_exists(conn, listing.id):
                continue

            new_count += 1
            new_on_this_page += 1
            seller_summary = None

            is_car = listing.category_id == CARS_CATEGORY_ID
            car_confirmed_adapted = (
                trust.is_confirmed_adapted_vehicle(listing.title, listing.description)
                if is_car
                else True
            )
            p_key = make_product_key(
                listing.category_id,
                listing.title,
                car_year=listing.car_year,
                car_is_adapted=car_confirmed_adapted,
            )
            historical_prices = db.get_reference_prices(
                conn, p_key, window_days=settings.reference_window_days
            )

            implausible_car_price = is_car and listing.price < MIN_PLAUSIBLE_CAR_PRICE
            unconfirmed_adapted_car = is_car and not car_confirmed_adapted
            is_rental = is_rental_listing(listing.title, listing.description)
            is_buyback = is_buyback_listing(listing.title, listing.description)

            if (
                is_accessory_or_broken(listing.title)
                or implausible_car_price
                or unconfirmed_adapted_car
                or is_rental
                or is_buyback
            ):
                # Un estuche suelto, un cable, "para piezas"... nunca debe
                # compararse contra el precio del producto completo en buen
                # estado. Tampoco un precio de coche a todas luces mal
                # formado (cuota de financiación en vez de precio de venta),
                # ni una furgoneta que coló por relevancia de texto pero no
                # menciona adaptación en ningún sitio, ni un anuncio de
                # ALQUILER (su "precio" es una tarifa diaria, no de compra),
                # ni uno de alguien que se ofrece a COMPRAR furgonetas
                # adaptadas (no está vendiendo nada) — los tres últimos
                # vistos en producción con datos reales de Milanuncios. Se
                # guarda igualmente en la BD, pero jamás puede ser "chollo".
                evaluation = evaluate_price(
                    price=listing.price,
                    historical_prices=[],
                    discount_threshold_pct=settings.discount_threshold_pct,
                    price_percentile=settings.price_percentile,
                    min_samples=settings.min_samples_for_reference,
                )
            else:
                condition_extra_pct = trust.condition_discount_modifier(
                    listing.title, listing.description
                )
                evaluation = evaluate_price(
                    price=listing.price,
                    historical_prices=historical_prices,
                    discount_threshold_pct=settings.discount_threshold_pct,
                    price_percentile=settings.price_percentile,
                    min_samples=settings.min_samples_for_reference,
                    rarity_raro_pct=settings.rarity_raro_pct,
                    rarity_epico_pct=settings.rarity_epico_pct,
                    rarity_legendario_pct=settings.rarity_legendario_pct,
                    min_margin_eur=settings.min_margin_eur,
                    scam_discount_threshold_pct=settings.scam_discount_threshold_pct,
                    condition_extra_pct=condition_extra_pct,
                    max_reference_cv=settings.max_reference_cv,
                )

            notified = False
            if evaluation.is_chollo:
                # Capas de criterio adicionales, solo para los pocos
                # anuncios que ya pasaron el filtro de precio (no tiene
                # sentido gastarlas en los cientos descartados por precio).
                reasons = list(evaluation.suspicious_reasons)
                if trust.has_scam_language(listing.title, listing.description):
                    reasons.append("lenguaje típico de estafa en el anuncio")

                seller_summary = None
                if settings.seller_trust_check_enabled and listing.user_id:
                    seller_info = client.get_seller_info(listing.user_id)
                    seller_result = trust.evaluate_seller(
                        seller_info,
                        min_account_age_days=settings.seller_min_account_age_days,
                        min_reviews=settings.seller_min_reviews,
                        min_avg_rating=settings.seller_min_avg_rating,
                    )
                    if seller_result.is_risky:
                        reasons.append(f"vendedor poco fiable ({seller_result.reason})")
                    if seller_info is not None:
                        seller_summary = seller_result

                evaluation.suspicious = bool(reasons)
                evaluation.suspicious_reasons = tuple(reasons)

            if evaluation.is_chollo and notifier is not None:
                distance_km = None
                if listing.latitude is not None and listing.longitude is not None:
                    distance_km = haversine_km(
                        settings.latitude, settings.longitude,
                        listing.latitude, listing.longitude,
                    )
                try:
                    notifier.send_chollo_alert(
                        listing=listing,
                        discount_pct=evaluation.discount_pct,
                        reference_price=evaluation.reference_price,
                        margin_eur=evaluation.margin_eur,
                        rarity=evaluation.rarity,
                        suspicious=evaluation.suspicious,
                        suspicious_reasons=evaluation.suspicious_reasons,
                        search_name=search.name,
                        distance_km=distance_km,
                        seller=seller_summary,
                    )
                    notified = True
                    chollo_count += 1
                except Exception:
                    logger.exception(
                        "No se pudo notificar el chollo %s (%s)", listing.id, listing.title
                    )
            elif evaluation.is_chollo:
                chollo_count += 1

            db.insert_listing(
                conn,
                listing=listing,
                search_name=search.name,
                product_key=p_key,
                is_chollo=evaluation.is_chollo,
                discount_pct=evaluation.discount_pct,
                reference_price=evaluation.reference_price,
                margin_eur=evaluation.margin_eur,
                rarity=evaluation.rarity,
                notified=notified,
            )

        cursor = page.next_cursor
        if not cursor or not page.listings:
            break

        if new_on_this_page == 0:
            # O hemos llegado al final del histórico ya visto, o Wallapop ha
            # repetido la misma página (se ha observado con cursores de
            # start_cursor en búsquedas con pocos resultados). En ambos
            # casos no tiene sentido seguir paginando.
            break

        _sleep_politely(settings)

    return new_count, chollo_count


def _search_worker(
    search: SearchConfig,
    config: AppConfig,
    clients: dict[str, WallapopClient | MilanunciosClient | AutoScoutClient],
    notifier: TelegramNotifier | None,
    abort_event: threading.Event,
) -> tuple[bool, int, int]:
    """Ejecuta una búsqueda en su propio hilo, con su propia conexión SQLite
    (sqlite3 no permite compartir una conexión entre hilos). Usa el
    cliente de la plataforma indicada en `search.source`.

    Devuelve (éxito, anuncios_nuevos, chollos_detectados).
    """
    if abort_event.is_set():
        return True, 0, 0  # no se ha ni intentado: no cuenta como fallo

    client = clients.get(search.source)
    if client is None:
        logger.error("[%s] Plataforma desconocida: %s", search.name, search.source)
        return False, 0, 0

    try:
        with db.connect(config.db_path) as conn:
            new_count, chollo_count = _run_search(
                search, config.settings, client, conn, notifier
            )
        logger.info(
            "[%s] %d anuncios nuevos, %d chollos detectados",
            search.name,
            new_count,
            chollo_count,
        )
        return True, new_count, chollo_count
    except REQUEST_ERRORS:
        logger.exception("[%s] Error consultando %s", search.name, search.source)
        return False, 0, 0
    except Exception:
        logger.exception("[%s] Error inesperado en el ciclo", search.name)
        return False, 0, 0
    finally:
        _sleep_politely(config.settings)


def run_cycle(config: AppConfig) -> None:
    notifier = None
    if config.telegram_bot_token and config.telegram_chat_id:
        notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)
    else:
        logger.warning(
            "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID no configurados: "
            "los chollos se guardarán en BD pero no se notificarán."
        )

    workers = config.settings.concurrent_searches
    logger.info(
        "Iniciando ciclo (%d búsquedas, %d en paralelo)", len(config.searches), workers
    )
    started_at = time.monotonic()

    abort_event = threading.Event()
    state_lock = threading.Lock()
    state = {
        "consecutive_failures": 0,
        "searches_failed": 0,
        "new_listings": 0,
        "chollos_found": 0,
    }

    def register_result(success: bool, new_count: int, chollo_count: int) -> None:
        with state_lock:
            state["new_listings"] += new_count
            state["chollos_found"] += chollo_count
            if success:
                state["consecutive_failures"] = 0
                return
            state["searches_failed"] += 1
            state["consecutive_failures"] += 1
            if (
                state["consecutive_failures"] >= config.settings.circuit_breaker_failures
                and not abort_event.is_set()
            ):
                logger.error(
                    "%d fallos en poco tiempo: cancelando el resto del ciclo "
                    "(posible bloqueo de IP o caída de alguna de las plataformas)",
                    state["consecutive_failures"],
                )
                abort_event.set()

    with WallapopClient() as wallapop_client, MilanunciosClient() as milanuncios_client, AutoScoutClient() as autoscout_client:
        clients = {
            "wallapop": wallapop_client,
            "milanuncios": milanuncios_client,
            "autoscout24": autoscout_client,
        }
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(_search_worker, search, config, clients, notifier, abort_event)
                for search in config.searches
            ]
            for future in as_completed(futures):
                register_result(*future.result())

    if abort_event.is_set():
        logger.warning("Ciclo terminado con cancelación parcial por fallos en cascada")
    logger.info("Ciclo completado")

    if notifier is not None and config.settings.heartbeat_enabled:
        notifier.send_cycle_summary(
            searches_total=len(config.searches),
            searches_failed=state["searches_failed"],
            new_listings=state["new_listings"],
            chollos_found=state["chollos_found"],
            duration_seconds=time.monotonic() - started_at,
            aborted=abort_event.is_set(),
        )
