"""SQLite-backed order store: drafts, orders and idempotency records.

The key lesson of this module is the idempotency guarantee:

    The same (account, Idempotency-Key) always yields the same order, even when
    10 identical requests race each other.

How it is guaranteed:
  1. Every submission runs inside `BEGIN IMMEDIATE`, which takes SQLite's write
     lock up front. Concurrent submitters therefore run one after another, not
     in an interleaved read-check-write race.
  2. The idempotency table has a composite PRIMARY KEY (account_id, idem_key),
     so even a logic bug could not insert a second record for the same key.
  3. The draft row is flipped OPEN -> SUBMITTED in the same transaction, so a
     draft can become at most one order, even with *different* keys.

Why this lives in the backend and not in the MCP server: MCP servers are
stateless and horizontally scaled. Two replicas cannot share an in-memory
dict, so deduplication must happen where the state is, in the system of
record. In production that is your Order Submission API's database, with a
unique constraint doing the same job as the PRIMARY KEY here.
"""

import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from telco_mcp_lab.mock_apis import data

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    draft_id        TEXT PRIMARY KEY,
    account_id      TEXT NOT NULL,
    subscription_id TEXT NOT NULL,
    action          TEXT NOT NULL,
    target_code     TEXT NOT NULL,
    monthly_before  TEXT NOT NULL,
    monthly_after   TEXT NOT NULL,
    one_off_fee     TEXT NOT NULL,
    currency        TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('OPEN', 'SUBMITTED')),
    order_id        TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        TEXT NOT NULL UNIQUE,
    account_id      TEXT NOT NULL,
    subscription_id TEXT NOT NULL,
    action          TEXT NOT NULL,
    target_code     TEXT NOT NULL,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    draft_id        TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS orders_by_account ON orders(account_id, created_at);
CREATE TABLE IF NOT EXISTS idempotency (
    account_id  TEXT NOT NULL,
    idem_key    TEXT NOT NULL,
    draft_id    TEXT NOT NULL,
    response    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (account_id, idem_key)
);
"""


class StoreError(Exception):
    """Business-rule failure; mapped to an HTTP problem by the router."""

    def __init__(self, status: int, code: str, detail: str, **extra) -> None:
        super().__init__(detail)
        self.status, self.code, self.detail, self.extra = status, code, detail, extra


@dataclass(frozen=True)
class SubmissionResult:
    body: dict
    replayed: bool


class OrderStore:
    def __init__(self, db_path: Path, draft_ttl_seconds: int, clock: Clock = utc_now) -> None:
        self._db_path = db_path
        self._ttl = timedelta(seconds=draft_ttl_seconds)
        self._clock = clock
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(_SCHEMA)
            self._seed(c)

    # A fresh connection per operation keeps us thread-safe: FastAPI runs our
    # sync endpoints in a thread pool, so requests really do run in parallel.
    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write_tx(self) -> Iterator[sqlite3.Connection]:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            c.execute("COMMIT")

    @staticmethod
    def _seed(c: sqlite3.Connection) -> None:
        for o in data.SEED_ORDERS:
            c.execute(
                "INSERT OR IGNORE INTO orders "
                "(order_id, account_id, subscription_id, action, target_code, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    o["order_id"],
                    o["account_id"],
                    o["subscription_id"],
                    o["action"],
                    o["target_code"],
                    o["status"],
                    o["created_at"],
                ),
            )

    # ------------------------------------------------------------------ orders
    def list_orders(self, account_id: str, offset: int, limit: int) -> tuple[list[dict], bool]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT order_id, account_id, subscription_id, action, target_code, status,"
                " created_at, draft_id FROM orders WHERE account_id = ?"
                " ORDER BY created_at DESC, seq DESC LIMIT ? OFFSET ?",
                (account_id, limit + 1, offset),
            ).fetchall()
        items = [dict(r) for r in rows]
        return items[:limit], len(items) > limit

    def get_order(self, order_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT order_id, account_id, subscription_id, action, target_code, status,"
                " created_at, draft_id FROM orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ drafts
    def create_draft(self, sub: dict, action: str, target_code: str) -> dict:
        before, after, fee = _price(sub, action, target_code)
        now = self._clock()
        draft = {
            # Opaque, high-entropy handle, as the MCP spec recommends for
            # server-minted handles that outlive a single call.
            "draft_id": f"DRF-{uuid.uuid4().hex}",
            "account_id": sub["account_id"],
            "subscription_id": sub["subscription_id"],
            "action": action,
            "target_code": target_code,
            "monthly_before": str(before),
            "monthly_after": str(after),
            "one_off_fee": str(fee),
            "currency": data.CURRENCY,
            "created_at": iso(now),
            "expires_at": iso(now + self._ttl),
            "status": "OPEN",
            "order_id": None,
        }
        with self._write_tx() as c:
            c.execute(
                f"INSERT INTO drafts ({', '.join(draft)}) VALUES ({', '.join('?' * len(draft))})",  # noqa: S608 - column names are our own constants
                tuple(draft.values()),
            )
        return _draft_view(draft)

    def get_draft(self, draft_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM drafts WHERE draft_id = ?", (draft_id,)).fetchone()
        return _draft_view(dict(row)) if row else None

    # -------------------------------------------------------------- submission
    def submit(self, draft_id: str, idem_key: str) -> SubmissionResult:
        with self._write_tx() as c:
            draft = c.execute("SELECT * FROM drafts WHERE draft_id = ?", (draft_id,)).fetchone()
            if draft is None:
                raise StoreError(404, "DRAFT_NOT_FOUND", f"No draft with id {draft_id}.")
            account_id = draft["account_id"]

            # 1. Replay check first: a retry of a successful submission must
            #    return the original result, even if the draft has since expired.
            prior = c.execute(
                "SELECT draft_id, response FROM idempotency WHERE account_id = ? AND idem_key = ?",
                (account_id, idem_key),
            ).fetchone()
            if prior is not None:
                if prior["draft_id"] != draft_id:
                    raise StoreError(
                        422,
                        "IDEMPOTENCY_KEY_REUSED",
                        "This Idempotency-Key was already used for a different draft. "
                        "Generate a new key for a new submission.",
                    )
                return SubmissionResult(json.loads(prior["response"]), replayed=True)

            # 2. The draft can only become one order, whatever key is used.
            if draft["status"] == "SUBMITTED":
                raise StoreError(
                    409,
                    "DRAFT_ALREADY_SUBMITTED",
                    "This draft was already submitted with a different Idempotency-Key.",
                    order_id=draft["order_id"],
                )
            now = self._clock()
            if now >= datetime.fromisoformat(draft["expires_at"]):
                raise StoreError(
                    410,
                    "DRAFT_EXPIRED",
                    "The draft has expired. Create a new draft and submit that instead.",
                    expired_at=draft["expires_at"],
                )

            cur = c.execute(
                "INSERT INTO orders (order_id, account_id, subscription_id, action, target_code,"
                " status, created_at, draft_id) VALUES ('pending', ?, ?, ?, ?, 'SUBMITTED', ?, ?)",
                (
                    account_id,
                    draft["subscription_id"],
                    draft["action"],
                    draft["target_code"],
                    iso(now),
                    draft_id,
                ),
            )
            order_id = f"ORD-{cur.lastrowid + 100000:06d}"
            c.execute("UPDATE orders SET order_id = ? WHERE seq = ?", (order_id, cur.lastrowid))
            c.execute(
                "UPDATE drafts SET status = 'SUBMITTED', order_id = ? WHERE draft_id = ?",
                (order_id, draft_id),
            )
            body = {
                "order_id": order_id,
                "draft_id": draft_id,
                "account_id": account_id,
                "subscription_id": draft["subscription_id"],
                "status": "SUBMITTED",
                "submitted_at": iso(now),
            }
            c.execute(
                "INSERT INTO idempotency (account_id, idem_key, draft_id, response, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (account_id, idem_key, draft_id, json.dumps(body), iso(now)),
            )
            return SubmissionResult(body, replayed=False)

    def count_orders_for_draft(self, draft_id: str) -> int:
        """Test helper: how many orders exist for a draft (must never exceed 1)."""
        with self._conn() as c:
            return c.execute(
                "SELECT COUNT(*) FROM orders WHERE draft_id = ?", (draft_id,)
            ).fetchone()[0]


def _price(sub: dict, action: str, target_code: str) -> tuple[Decimal, Decimal, Decimal]:
    current_plan = data.PLANS[sub["plan_code"]]["monthly_price"]
    current_addons = sum(
        (data.ADDONS[a]["monthly_price"] for a in data.SERVICES[sub["service_id"]]["addons"]),
        Decimal("0"),
    )
    before = current_plan + current_addons
    if action == "CHANGE_PLAN":
        after = data.PLANS[target_code]["monthly_price"] + current_addons
        return before, after, data.PLAN_CHANGE_FEE
    if action == "ADD_ADDON":
        return before, before + data.ADDONS[target_code]["monthly_price"], data.ADDON_ACTIVATION_FEE
    if action == "REMOVE_ADDON":
        return before, before - data.ADDONS[target_code]["monthly_price"], Decimal("0.00")
    raise ValueError(f"unknown action {action}")


def _draft_view(d: dict) -> dict:
    before, after = Decimal(d["monthly_before"]), Decimal(d["monthly_after"])
    return {
        "draft_id": d["draft_id"],
        "account_id": d["account_id"],
        "subscription_id": d["subscription_id"],
        "action": d["action"],
        "target_code": d["target_code"],
        "status": d["status"],
        "order_id": d["order_id"],
        "price_summary": {
            "currency": d["currency"],
            "monthly_before": str(before),
            "monthly_after": str(after),
            "monthly_delta": str(after - before),
            "one_off_fee": d["one_off_fee"],
        },
        "created_at": d["created_at"],
        "expires_at": d["expires_at"],
    }
