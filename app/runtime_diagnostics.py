from __future__ import annotations

import asyncio
import faulthandler
import logging
import os
import signal
import sys
import threading
from types import TracebackType


_installed = False
_fault_log_file_handle = None


def configure_logging() -> None:
    """Configure application loggers so background worker INFO logs reach stderr."""
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(levelname)s:%(name)s:%(message)s",
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in root_logger.handlers:
        if handler.level == logging.NOTSET or handler.level > level:
            handler.setLevel(level)


def install_runtime_diagnostics() -> None:
    """Install process-wide crash diagnostics for uncaught and fatal errors."""
    global _installed, _fault_log_file_handle
    if _installed:
        return

    logger = logging.getLogger("lightning_rod.runtime")

    fault_log_path = os.getenv("LIGHTNING_FAULT_LOG")
    try:
        if fault_log_path:
            _fault_log_file_handle = open(fault_log_path, "a", buffering=1)
            faulthandler.enable(file=_fault_log_file_handle, all_threads=True)
            logger.info("Faulthandler enabled with output file: %s", fault_log_path)
        else:
            faulthandler.enable(all_threads=True)
            logger.info("Faulthandler enabled (stderr output).")
    except Exception:
        logger.exception("Failed to enable faulthandler.")

    if hasattr(signal, "SIGUSR1"):
        try:
            faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
            logger.info("Registered SIGUSR1 for manual traceback dump.")
        except Exception:
            logger.exception("Failed to register SIGUSR1 faulthandler hook.")

    original_excepthook = sys.excepthook

    def _uncaught_exception_hook(
        exc_type: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
    ) -> None:
        logger.critical("Uncaught top-level exception", exc_info=(exc_type, exc, tb))
        original_excepthook(exc_type, exc, tb)

    sys.excepthook = _uncaught_exception_hook

    original_threading_hook = threading.excepthook

    def _thread_exception_hook(args: threading.ExceptHookArgs) -> None:
        logger.critical(
            "Uncaught thread exception in %s",
            args.thread.name if args.thread else "unknown-thread",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        original_threading_hook(args)

    threading.excepthook = _thread_exception_hook
    _installed = True
    logger.info("Runtime crash diagnostics installed.")


def install_asyncio_exception_handler() -> None:
    """Install logging for otherwise-unhandled asyncio exceptions."""
    logger = logging.getLogger("lightning_rod.runtime")
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    previous_handler = loop.get_exception_handler()

    def _handler(loop: asyncio.AbstractEventLoop, context: dict[object, object]) -> None:
        message = context.get("message", "Unhandled asyncio exception")
        exc = context.get("exception")
        if isinstance(exc, BaseException):
            logger.critical("Asyncio loop exception: %s", message, exc_info=exc)
        else:
            logger.critical("Asyncio loop exception: %s | context=%r", message, context)

        if previous_handler is not None:
            previous_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)
    logger.info("Asyncio exception handler installed.")
