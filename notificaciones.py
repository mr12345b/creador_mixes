"""Avisos por Telegram (opcional). Define TELEGRAM_TOKEN y TELEGRAM_CHAT_ID en Render para activarlos."""
import logging
import os

import requests

logger = logging.getLogger(__name__)


def notificar(mensaje: str) -> None:
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": mensaje, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        logger.error("Error enviando Telegram: %s", e)
