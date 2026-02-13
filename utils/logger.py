import logging
import os
import sys
from logging.handlers import RotatingFileHandler


def setup_logger(
    name: str = "deal-finder",
    log_file: str = "deal_finder.log",
    level: str = "INFO",
) -> logging.Logger:
    """Configura e restituisce un logger con output su file e console.

    Args:
        name: Nome del logger.
        log_file: Percorso del file di log.
        level: Livello di logging (DEBUG, INFO, WARNING, ERROR).

    Returns:
        Logger configurato.
    """
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    log_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(log_level)

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler con rotazione (5MB max, 3 backup)
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def get_logger(module_name: str) -> logging.Logger:
    """Restituisce un child logger per un modulo specifico.

    Args:
        module_name: Nome del modulo (es. "subito", "llm_parser").

    Returns:
        Child logger.
    """
    return logging.getLogger(f"deal-finder.{module_name}")
