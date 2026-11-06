"""Reading the two secrets: the Resend key and the token Alertmanager sends with each webhook.

They are files outside the repository, mounted into the container. A file that only root can read fails for a non-root notifier, and Docker Compose ignores `uid`, `gid` and `mode` for file secrets,
so the permissions are the host file's own. The notifier reads both at start, as the user it runs as, and fails closed with a message that names the file and never repeats a value. A secret that is
empty, longer than any real one, has control characters inside it, or is readable by everyone is refused: a world-readable secret is already leaked.
"""
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from cricket_notifier.errors import ConfigError

MAX_SECRET_BYTES = 512
MIN_SECRET_CHARS = 16


@dataclass(frozen=True)
class Secrets:
    resend_api_key: str = field(repr=False)
    webhook_token: str = field(repr=False)


def read_secret(path: str, what: str, check_mode: bool = True) -> str:
    """The text in a secret file, without its trailing newline. Raises ConfigError with a message that never holds the value."""
    file = Path(path)
    try:
        info = file.stat()
    except OSError:
        raise ConfigError(f"the {what} file does not exist or cannot be reached: {str(path)[:120]}") from None
    if not stat.S_ISREG(info.st_mode):
        raise ConfigError(f"the {what} path is not a regular file: {str(path)[:120]}")
    if check_mode and info.st_mode & stat.S_IROTH:
        raise ConfigError(f"the {what} file is readable by every user: {str(path)[:120]}. Make it readable by the notifier's user only (mode 0400 or 0440)")
    if info.st_size > MAX_SECRET_BYTES:
        raise ConfigError(f"the {what} file is too large to be a secret")
    try:
        raw = file.read_bytes()
    except OSError:
        raise ConfigError(f"the {what} file cannot be read by this user: {str(path)[:120]}. Check its owner and mode on the host") from None
    try:
        text = raw.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError:
        raise ConfigError(f"the {what} file is not text") from None
    if len(text) < MIN_SECRET_CHARS:
        raise ConfigError(f"the {what} file is empty or too short to be a secret")
    if any(ord(c) < 33 or ord(c) == 127 for c in text):
        raise ConfigError(f"the {what} file holds spaces or control characters inside the secret")
    return text


def load_secrets(resend_api_key_file: str, webhook_token_file: str, check_mode: bool = True) -> Secrets:
    key = read_secret(resend_api_key_file, "Resend API key", check_mode)
    token = read_secret(webhook_token_file, "webhook token", check_mode)
    if key == token:
        raise ConfigError("the Resend key and the webhook token must not be the same value")
    return Secrets(key, token)
