from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler

from apscheduler.schedulers.blocking import BlockingScheduler

from .config import ROOT_DIR, load_config
from .pipeline import run_cycle


def setup_logging() -> None:
    log_dir = ROOT_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # RotatingFileHandler en vez de dejar que el .bat programado haga ">>
    # bot.log" para siempre: sin esto, el log crecería sin límite mientras
    # el bot esté corriendo cada 15 min indefinidamente. Se queda con
    # bot.log + 5 copias rotadas de hasta 5MB cada una (~30MB máximo).
    file_handler = RotatingFileHandler(
        log_dir / "bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # httpx es bastante verboso en INFO por cada request; lo bajamos a WARNING.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot de detección de chollos en Wallapop")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Ejecuta un único ciclo y termina, en vez de programar ejecuciones periódicas.",
    )
    args = parser.parse_args()

    setup_logging()
    config = load_config()
    logger = logging.getLogger(__name__)

    if args.once:
        run_cycle(config)
        return

    scheduler = BlockingScheduler()
    scheduler.add_job(
        run_cycle,
        args=[config],
        trigger="interval",
        minutes=config.settings.interval_minutes,
    )
    logger.info(
        "Scheduler iniciado: primer ciclo inmediato, luego cada %d minutos. Ctrl+C para parar.",
        config.settings.interval_minutes,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Parando scheduler...")


if __name__ == "__main__":
    main()
