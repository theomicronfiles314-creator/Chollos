from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from html import escape

from telegram import Bot
from telegram.constants import ParseMode

from .pricing import RARITY_EMOJI, RARITY_LABEL
from .trust import SellerTrustResult
from .wallapop_client import Listing

logger = logging.getLogger(__name__)


def _age_str(created_at: datetime | None) -> str:
    if created_at is None:
        return "fecha desconocida"
    delta = datetime.now(timezone.utc) - created_at
    minutes = delta.total_seconds() / 60
    if minutes < 60:
        return f"publicado hace {minutes:.0f} min"
    hours = minutes / 60
    if hours < 24:
        return f"publicado hace {hours:.0f} h"
    return f"publicado hace {hours / 24:.0f} d"


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        # No se guarda un Bot ya instanciado: su cliente HTTP interno queda
        # atado al primer event loop en el que se usa, y como el pipeline
        # es síncrono y llama a asyncio.run() una vez por cada alerta, la
        # segunda llamada revienta con "Event loop is closed". Se crea un
        # Bot nuevo (como context manager async) en cada envío.
        self._bot_token = bot_token
        self._chat_id = chat_id

    def send_chollo_alert(
        self,
        listing: Listing,
        discount_pct: float,
        reference_price: float,
        margin_eur: float,
        rarity: str,
        suspicious: bool,
        suspicious_reasons: tuple[str, ...],
        search_name: str,
        distance_km: float | None,
        seller: SellerTrustResult | None = None,
    ) -> None:
        emoji = RARITY_EMOJI.get(rarity, "⚪")
        label = RARITY_LABEL.get(rarity, rarity.upper())
        roi_pct = (margin_eur / listing.price * 100) if listing.price else 0

        if listing.is_shippable:
            logistics = "📦 Admite envío"
        elif distance_km is not None:
            logistics = f"🚗 Sin envío — a {distance_km:.0f} km de tu ubicación"
        else:
            logistics = "🚗 Sin envío (a recoger en mano)"

        seller_line = ""
        if seller is not None and seller.account_age_days is not None:
            seller_line = (
                f"\n👤 Vendedor: cuenta de {seller.account_age_days:.0f} días, "
                f"{seller.review_count} valoraciones"
                + (f", {seller.avg_rating:.1f}/5" if seller.avg_rating is not None else "")
            )

        car_line = ""
        if listing.car_year or listing.car_km is not None:
            parts = []
            if listing.car_brand or listing.car_model:
                parts.append(f"{listing.car_brand or ''} {listing.car_model or ''}".strip())
            if listing.car_year:
                parts.append(str(listing.car_year))
            if listing.car_km is not None:
                parts.append(f"{listing.car_km:,.0f} km".replace(",", "."))
            car_line = f"\n🚐 {escape(' · '.join(parts))}"

        warning = ""
        if suspicious:
            reasons_str = "; ".join(suspicious_reasons) if suspicious_reasons else "revisa antes de contactar"
            warning = (
                f"\n⚠️ <b>Sospechoso: {escape(reasons_str)}.</b> "
                f"Revisa bien el anuncio y el vendedor antes de contactar, "
                f"podría ser un timo o un error de precio.\n"
            )

        text = (
            f"{emoji} <b>CHOLLO {label}</b> ({escape(search_name)})\n\n"
            f"<b>{escape(listing.title)}</b>\n"
            f"Precio de compra: <b>{listing.price:.0f} {listing.currency}</b>\n"
            f"Precio sugerido de reventa (mediana del mercado): "
            f"<b>{reference_price:.0f} {listing.currency}</b>\n"
            f"Margen estimado: <b>+{margin_eur:.0f} {listing.currency}</b> "
            f"(descuento {discount_pct:.0f}% / ROI ~{roi_pct:.0f}%)"
            f"{car_line}\n"
            f"{logistics}\n"
            f"📍 {escape(listing.city or 'ubicación no indicada')} · {_age_str(listing.created_at)}"
            f"{seller_line}"
            f"{warning}\n"
            f"{listing.url}"
        )
        try:
            asyncio.run(self._send_alert(text, listing.image_url))
        except Exception:
            logger.exception("Fallo al enviar alerta de Telegram para %s", listing.id)
            raise

    async def _send_alert(self, text: str, image_url: str | None) -> None:
        async with Bot(token=self._bot_token) as bot:
            if image_url:
                try:
                    await bot.send_photo(
                        chat_id=self._chat_id,
                        photo=image_url,
                        caption=text,
                        parse_mode=ParseMode.HTML,
                    )
                    return
                except Exception:
                    logger.warning(
                        "No se pudo enviar la foto (%s), mando solo texto", image_url
                    )
            await bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )

    def send_cycle_summary(
        self,
        searches_total: int,
        searches_failed: int,
        new_listings: int,
        chollos_found: int,
        duration_seconds: float,
        aborted: bool,
        failures: list[tuple[str, str]] | None = None,
    ) -> None:
        """Mensaje de "sigo vivo" al terminar cada ciclo, haya chollos o no.

        Sin esto no hay forma de distinguir desde Telegram "no ha habido
        ningún chollo este ciclo" de "el bot lleva horas sin ejecutarse".

        Cuando hay fallos, se listan por nombre y motivo — en GitHub
        Actions los logs del job piden iniciar sesión incluso en repos
        públicos, así que este mensaje es la única forma práctica de ver
        qué ha fallado y por qué sin entrar a la cuenta de GitHub.
        """
        if aborted:
            status_emoji = "🛑"
            status_line = "Ciclo CANCELADO parcialmente (demasiados fallos seguidos)"
        elif searches_failed > 0:
            status_emoji = "⚠️"
            status_line = f"Ciclo completado con {searches_failed} búsqueda(s) fallida(s)"
        else:
            status_emoji = "✅"
            status_line = "Ciclo completado sin errores"

        failures_block = ""
        if failures:
            lines = "\n".join(f"  • <b>{escape(name)}</b>: {escape(reason)}" for name, reason in failures)
            failures_block = f"\n{lines}"

        text = (
            f"{status_emoji} <b>{escape(status_line)}</b>\n"
            f"Búsquedas: {searches_total - searches_failed}/{searches_total} ok\n"
            f"Anuncios nuevos: {new_listings}\n"
            f"Chollos detectados: {chollos_found}\n"
            f"Duración: {duration_seconds:.0f}s"
            f"{failures_block}"
        )
        try:
            asyncio.run(self._send(text))
        except Exception:
            logger.exception("Fallo al enviar el heartbeat de ciclo a Telegram")
            # No relanzamos: que falle el heartbeat no debe hacer fallar el ciclo.

    async def _send(self, text: str) -> None:
        async with Bot(token=self._bot_token) as bot:
            await bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
