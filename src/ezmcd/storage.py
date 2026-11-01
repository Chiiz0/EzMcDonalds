"""Local credentials, atomic journal, identity-scoped locks and budgets."""
from contextlib import contextmanager
from datetime import date
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time

from .errors import EzMcdError
from .models import canonical, fingerprint, Policy, pickup_time


def secure_directory(path: Path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise EzMcdError("symlink_data_directory")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def read_private(path: Path):
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_mode & 0o077:
        raise EzMcdError("private_file_permissions_required")
    if path.stat().st_size > 4 * 1024 * 1024:
        raise EzMcdError("private_file_too_large")
    return json.loads(path.read_text())


def write_private(path: Path, value):
    secure_directory(path.parent)
    if path.is_symlink():
        raise EzMcdError("symlink_private_file")
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Store:
    def __init__(self, root: Path):
        requested = Path(root).expanduser().absolute()
        if requested.is_symlink():
            raise EzMcdError("symlink_data_directory")
        self.root = requested.resolve()
        secure_directory(self.root)
        secure_directory(self.root / "accounts")
        secure_directory(self.root / "locks")
        db = self.root / "journal.sqlite3"
        if db.is_symlink():
            raise EzMcdError("symlink_database")
        fd = os.open(db, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        with self.connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts(alias TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL);
            CREATE TABLE IF NOT EXISTS quotes(id TEXT PRIMARY KEY, identity TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, identity TEXT NOT NULL,
                request_key TEXT NOT NULL, intent TEXT NOT NULL, day TEXT NOT NULL, amount INTEGER NOT NULL,
                state TEXT NOT NULL, data TEXT NOT NULL,
                UNIQUE(identity, request_key), UNIQUE(identity, intent));
            CREATE TABLE IF NOT EXISTS writes(attempt TEXT NOT NULL, stage TEXT NOT NULL,
                claimed REAL NOT NULL, receipt TEXT, PRIMARY KEY(attempt, stage));
            CREATE TABLE IF NOT EXISTS request_keys(identity TEXT NOT NULL, request_key TEXT NOT NULL,
                intent TEXT NOT NULL, attempt TEXT NOT NULL, PRIMARY KEY(identity, request_key));
            INSERT OR IGNORE INTO request_keys SELECT identity, request_key, intent, id FROM attempts;
            """)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.root / "journal.sqlite3", timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=FULL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @contextmanager
    def lock(self, identity):
        if not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise EzMcdError("invalid_account_identity")
        fd = os.open(self.root / "locks" / identity, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise EzMcdError("account_busy_try_status_only") from None
            yield
        finally:
            os.close(fd)

    def account(self, alias):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", alias):
            raise EzMcdError("invalid_account_alias")
        with self.connect() as conn:
            row = conn.execute("SELECT identity FROM accounts WHERE alias=?", (alias,)).fetchone()
        if not row:
            raise EzMcdError("account_not_connected")
        return read_private(self.root / "accounts" / (row["identity"] + ".json"))

    def accounts(self):
        with self.connect() as conn:
            aliases = [x[0] for x in conn.execute("SELECT alias FROM accounts ORDER BY alias")]
        return [self.account(alias) for alias in aliases]

    def add_account(self, record):
        alias, identity = record["alias"], record["identity"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", alias):
            raise EzMcdError("invalid_account_alias")
        with self.lock(identity), self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM accounts WHERE alias=? OR identity=?", (alias, identity)).fetchone():
                raise EzMcdError("account_exists_update_session_without_resetting_ledger")
            write_private(self.root / "accounts" / (identity + ".json"), record)
            conn.execute("INSERT INTO accounts VALUES (?,?)", (alias, identity))

    def update_account(self, record):
        return self.modify_account(record["alias"], lambda old: record)

    def modify_account(self, alias, transform):
        identity = self.account(alias)["identity"]
        with self.lock(identity):
            old = self.account(alias)
            record = transform(old)
            if record["identity"] != identity or record["alias"] != alias:
                raise EzMcdError("account_identity_changed")
            write_private(self.root / "accounts" / (identity + ".json"), record)
        return record

    def save_quote(self, identity, data):
        with self.connect() as conn:
            conn.execute("INSERT INTO quotes VALUES (?,?,?)", (data["quote_id"], identity, canonical(data)))

    def quote(self, identity, quote_id):
        with self.connect() as conn:
            row = conn.execute("SELECT data FROM quotes WHERE id=? AND identity=?", (quote_id, identity)).fetchone()
        if not row:
            raise EzMcdError("quote_not_found_for_account")
        return json.loads(row[0])

    def existing(self, identity, request_key, intent):
        with self.connect() as conn:
            binding = conn.execute("SELECT intent FROM request_keys WHERE identity=? AND request_key=?",
                                   (identity, request_key)).fetchone()
            if binding and binding["intent"] != intent:
                raise EzMcdError("idempotency_key_reused_for_different_order")
            rows = conn.execute("SELECT * FROM attempts WHERE identity=? AND (request_key=? OR intent=?)",
                                (identity, request_key, intent)).fetchall()
            if not rows:
                return None
            if len(rows) != 1 or rows[0]["intent"] != intent:
                raise EzMcdError("idempotency_key_reused_for_different_order")
            # Bind alternate retry keys too; they cannot later mean a different purchase.
            conn.execute("INSERT OR IGNORE INTO request_keys VALUES (?,?,?,?)",
                         (identity, request_key, intent, rows[0]["id"]))
            return json.loads(rows[0]["data"])

    def reserve(self, identity, key, quote, policy, data):
        day, amount = quote["pickup_at"][:10], quote["amount_cents"]
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # A changed/reordered cart or new key must not sidestep an UNKNOWN
            # order. The server list can lag a successful write whose reply was lost.
            previous = conn.execute("SELECT data FROM attempts WHERE identity=? AND day=?", (identity, day)).fetchall()
            if any((a := json.loads(row[0]))["store_code"] == quote["store_code"]
                   and a["pickup_at"] == quote["pickup_at"] for row in previous):
                raise EzMcdError("existing_local_attempt_at_same_store_and_pickup_time")
            row = conn.execute("SELECT count(*), coalesce(sum(amount),0) FROM attempts WHERE identity=? AND day=?",
                               (identity, day)).fetchone()
            if row[0] >= policy.max_orders_per_day or row[1] + amount > policy.daily_budget_cents:
                raise EzMcdError("daily_order_or_budget_limit_reached")
            try:
                conn.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?)", (data["attempt_id"], identity,
                    key, quote["intent"], day, amount, data["state"], canonical(data)))
                conn.execute("INSERT INTO request_keys VALUES (?,?,?,?)", (identity, key, quote["intent"], data["attempt_id"]))
            except sqlite3.IntegrityError:
                raise EzMcdError("attempt_already_reserved_query_status") from None

    def attempt(self, identity, attempt_id):
        with self.connect() as conn:
            row = conn.execute("SELECT data FROM attempts WHERE id=? AND identity=?", (attempt_id, identity)).fetchone()
        if not row:
            raise EzMcdError("attempt_not_found_for_account")
        return json.loads(row[0])

    def list_attempts(self, identity, *, pickup_date=None, state=None, limit=20, offset=0):
        """Read a bounded identity-scoped page, including unknown/unfinished writes.

        A list is a journal snapshot, never an assertion about the live server.
        Keep unknown attempts visible even when no order response was received.
        """
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 10000:
            raise EzMcdError("invalid_order_history_pagination")
        if pickup_date is not None:
            try:
                if not isinstance(pickup_date, str) or date.fromisoformat(pickup_date).isoformat() != pickup_date:
                    raise ValueError
            except ValueError:
                raise EzMcdError("invalid_order_history_date") from None
        if state is not None and state not in ("RESERVED", "CREATED", "PAYMENT_ATTEMPTED", "UNKNOWN", "PAID", "CANCELLED"):
            raise EzMcdError("invalid_order_history_state")
        where, values = "identity=?", [identity]
        if pickup_date is not None:
            where += " AND day=?"
            values.append(pickup_date)
        if state is not None:
            where += " AND state=?"
            values.append(state)
        with self.connect() as conn:
            # Count and page share one read snapshot without taking a write lock.
            conn.execute("BEGIN")
            total = conn.execute("SELECT count(*) FROM attempts WHERE " + where, values).fetchone()[0]
            rows = conn.execute("SELECT data FROM attempts WHERE " + where +
                                " ORDER BY day DESC, rowid DESC LIMIT ? OFFSET ?", [*values, limit, offset]).fetchall()
        return {"attempts": [json.loads(row[0]) for row in rows], "total": total,
                "offset": offset, "limit": limit, "has_more": offset + len(rows) < total}

    def update_attempt(self, identity, data):
        with self.connect() as conn:
            result = conn.execute("UPDATE attempts SET state=?,data=? WHERE id=? AND identity=?",
                                 (data["state"], canonical(data), data["attempt_id"], identity))
            if result.rowcount != 1:
                raise EzMcdError("attempt_not_found_for_account")

    def claim(self, attempt_id, stage):
        with self.connect() as conn:
            try:
                conn.execute("INSERT INTO writes VALUES (?,?,?,NULL)", (attempt_id, stage, time.time()))
            except sqlite3.IntegrityError:
                raise EzMcdError("write_already_attempted_query_only") from None

    def receipt(self, attempt_id, stage, response):
        with self.connect() as conn:
            conn.execute("UPDATE writes SET receipt=? WHERE attempt=? AND stage=? AND receipt IS NULL",
                         (canonical(response), attempt_id, stage))
