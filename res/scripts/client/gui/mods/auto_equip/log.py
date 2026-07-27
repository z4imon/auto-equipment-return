import logging

_LOGGER_NAME = 'AutoEquipmentReturn'
_PREFIX = '[AutoEquip] '


class _ModLogger(object):
    """Thin wrapper around a stdlib logger with a couple of convenience helpers."""

    def __init__(self):
        self._logger = logging.getLogger(_LOGGER_NAME)
        self._logger.setLevel(logging.DEBUG)

    # -- standard levels -------------------------------------------------
    def debug(self, msg, *args):
        self._logger.debug(_PREFIX + str(msg), *args)

    def info(self, msg, *args):
        self._logger.info(_PREFIX + str(msg), *args)

    def warning(self, msg, *args):
        self._logger.warning(_PREFIX + str(msg), *args)

    def error(self, msg, *args):
        self._logger.error(_PREFIX + str(msg), *args)

    # -- helpers ---------------------------------------------------------
    def step(self, msg, *args):
        """A loud milestone marker, easy to scan for in the log."""
        self._logger.info(_PREFIX + '======== ' + str(msg) + ' ========', *args)

    def exc(self, msg, *args):
        """Log a message plus the current exception traceback."""
        self._logger.error(_PREFIX + 'EXCEPTION: ' + str(msg), *args, exc_info=True)


LOG = _ModLogger()
