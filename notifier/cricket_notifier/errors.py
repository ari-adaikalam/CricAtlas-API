"""The exceptions the notifier raises on purpose."""


class ConfigError(RuntimeError):
    """The configuration or a secret cannot be used. The notifier refuses to start, and the message never holds a secret."""


class IllegalTransition(RuntimeError):
    """A message was asked to move between states in a way the state table does not allow."""


class StorageUnavailable(RuntimeError):
    """The ledger cannot record what is about to happen, so nothing may be sent."""


class PayloadTooLarge(RuntimeError):
    """A rendered message is over its size limit."""
