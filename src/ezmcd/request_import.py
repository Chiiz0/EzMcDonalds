"""Parse a copied request as data. Never execute curl, shell code or its body."""
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
from urllib.parse import urlsplit

from .capture import BALANCE, ORDER
from .errors import EzMcdError

LIMIT = 128 * 1024
HEADERS = frozenset(("ct", "language", "p", "sid", "token", "v", "sv", "x-mcd-gw-v",
    "authorization", "x-hmac-digest", "meddyid", "routingid", "tid", "d", "x-mcd-sign"))


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > LIMIT or "\x00" in value:
        raise EzMcdError("copied_request_empty_or_too_large")
    return value.strip()


def _header(headers, value):
    name, separator, content = value.partition(":")
    name = name.lower()
    if (not separator or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", name)
            or name in headers or len(headers) >= 100 or len(content) > 8192
            or any(ord(c) < 32 or ord(c) == 127 for c in content)):
        raise EzMcdError("copied_request_headers_invalid_or_repeated")
    headers[name] = content.strip()


def _curl(text):
    # Exporters use POSIX quoting and sometimes backslash-newline continuation.
    # shlex only tokenizes; no subprocess receives any of these tokens.
    try:
        words = shlex.split(text.replace("\\\r\n", "").replace("\\\n", ""), posix=True)
    except ValueError:
        raise EzMcdError("copied_request_format_not_supported") from None
    if not words or words[0] not in ("curl", "/usr/bin/curl") or len(words) > 400:
        raise EzMcdError("copied_request_format_not_supported")
    headers, urls, methods, bodies = {}, [], [], []
    i = 1
    # These export flags are discarded, not applied to our verified transport.
    ignored = {"--compressed", "--insecure", "-k", "--http1.1", "--http2", "--globoff", "-g", "--path-as-is"}
    values = {"-H": "header", "--header": "header", "-X": "method", "--request": "method",
              "--url": "url", "-d": "body", "--data": "body", "--data-raw": "body", "--data-binary": "body"}
    while i < len(words):
        word = words[i]; i += 1
        if word in ignored:
            continue
        option, equal, inline = word.partition("=")
        if option in values and (not equal or option.startswith("--")):
            if equal:
                value = inline
            else:
                if i == len(words):
                    raise EzMcdError("copied_request_format_not_supported")
                value = words[i]; i += 1
            kind = values[option]
            if kind == "header":
                _header(headers, value)
            elif kind == "url":
                urls.append(value)
            elif kind == "method":
                methods.append(value)
            else:
                # File references are not read. Refuse rather than invent data.
                if value.startswith("@"):
                    raise EzMcdError("copied_request_file_reference_not_allowed")
                bodies.append(value)
        elif word.startswith("https://"):
            urls.append(word)
        else:
            raise EzMcdError("copied_request_option_not_supported")
    if len(urls) != 1 or len(methods) > 1 or len(bodies) > 1:
        raise EzMcdError("copied_request_requires_one_request")
    method = methods[0] if methods else "POST" if bodies else "GET"
    return method, urls[0], headers, bool(bodies)


def _raw(text):
    lines = text.replace("\r\n", "\n").split("\n")
    match = re.fullmatch(r"(GET|POST) (\S+) HTTP/(?:1\.0|1\.1|2|2\.0)", lines[0])
    if not match:
        raise EzMcdError("copied_request_format_not_supported")
    headers, body = {}, False
    for i, line in enumerate(lines[1:], 1):
        if not line:
            body = bool("\n".join(lines[i+1:]).strip())
            break
        _header(headers, line)
    if "host" not in headers:
        raise EzMcdError("copied_request_official_origin_required")
    target = match[2]
    url = "https://" + headers["host"] + target if target.startswith("/") else target
    return match[1], url, headers, body


def parse_request(value, *, purpose):
    """Return only necessary headers for one exact endpoint; discard all bodies."""
    if purpose not in ("balance", "runtime"):
        raise EzMcdError("invalid_request_import_purpose")
    text = _text(value)
    method, target, headers, body_present = _curl(text) if text.startswith(("curl ", "curl\n", "/usr/bin/curl ")) else _raw(text)
    url = urlsplit(target)
    host = headers.get("host")
    if (url.scheme != "https" or url.netloc not in ("api.mcd.cn", "api.mcd.cn:443")
            or url.query or url.fragment or host is not None and host not in ("api.mcd.cn", "api.mcd.cn:443")):
        raise EzMcdError("copied_request_official_origin_required")
    expected = ("GET", BALANCE) if purpose == "balance" else ("POST", ORDER)
    if (method, url.path) != expected or purpose == "balance" and body_present:
        raise EzMcdError("copied_request_wrong_endpoint_or_method")
    if "transfer-encoding" in headers or purpose == "balance" and headers.get("content-length", "0") != "0":
        raise EzMcdError("copied_request_unexpected_body_encoding")
    return {"method": method, "host": "api.mcd.cn", "path": url.path,
            "headers": {k: v for k, v in headers.items() if k in HEADERS}, "body": ""}


def read_request_file(path):
    """Read bounded private text, refusing symlinks and permissive modes."""
    path = Path(path).expanduser()
    if path.is_symlink():
        raise EzMcdError("private_file_permissions_required")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise EzMcdError("private_file_permissions_required")
        if info.st_size > LIMIT:
            raise EzMcdError("copied_request_empty_or_too_large")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            value = stream.read(LIMIT + 1)
        if len(value) > LIMIT:
            raise EzMcdError("copied_request_empty_or_too_large")
        return _text(value.decode("utf-8"))
    finally:
        os.close(fd)


def read_request_clipboard():
    """Explicit local CLI opt-in only. Never log, execute or change clipboard text."""
    if sys.platform != "darwin":
        raise EzMcdError("clipboard_import_requires_macos_use_request_file")
    if not sys.stdin.isatty():
        raise EzMcdError("clipboard_import_requires_private_terminal")
    try:
        result = subprocess.run(["/usr/bin/pbpaste", "-Prefer", "txt"], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
            env=os.environ | {"LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"})
        return _text(result.stdout.decode("utf-8"))
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise EzMcdError("clipboard_request_unreadable") from None
