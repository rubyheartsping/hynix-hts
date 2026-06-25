"""
Hynix HTS - 실시간 시세 폴링 수집기 (PoC)

SK하이닉스(000660.KS), 마이크론(MU), USD/KRW 환율(KRW=X)을
N초마다 폴링해 SQLite DB에 적재한다.

주의(중요):
  - 여기서 찍히는 timestamp는 "수집(폴링) 시각"이지 거래소 체결 시각이 아니다.
    무료 소스(yfinance)는 갱신 주기가 약 1초 이상이라 초이하(ms/us) 정밀도는 의미 없음.
    진짜 틱 타임스탬프(ms/us)는 브로커 웹소켓(KIS/키움) 붙일 때 가능.
  - 시간대: 하이닉스는 한국 주간장, 마이크론은 미국 주간장(한국 야간)이라
    동시간대 데이터가 항상 존재하진 않음. 장 마감 시엔 직전 종가가 그대로 반복 적재됨.

로컬 프로토타입용. 브로커 API 연동은 차후 단계.

사용법:
    pip install -r requirements.txt
    python collector.py                      # 기본 2초 간격, 무한
    python collector.py --interval 1         # 1초 간격
    python collector.py --duration 1800      # 30분만 수집 후 종료
    python collector.py --db ticks.db        # DB 경로 지정
"""

from __future__ import annotations

import argparse
import logging
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    import yfinance as yf
except ImportError:
    sys.exit("yfinance 미설치. 실행: pip install -r requirements.txt")

KST = timezone(timedelta(hours=9), name="KST")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("collector")


# --------------------------------------------------------------------------- #
# 수집 대상 정의
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Asset:
    symbol: str          # yfinance 티커
    name: str            # 사람이 읽을 이름
    kind: str            # "stock" | "fx"


ASSETS: list[Asset] = [
    Asset("000660.KS", "SK Hynix", "stock"),
    Asset("MU", "Micron", "stock"),
    Asset("KRW=X", "USD/KRW", "fx"),
]


@dataclass
class Quote:
    symbol: str
    name: str
    price: Optional[float]
    prev_close: Optional[float]
    volume: Optional[float]   # 당일 누적 거래량(주식). 환율은 보통 None.
    currency: Optional[str]
    source: str
    ts_poll_utc: str     # 수집 시각 (ISO-8601, UTC)
    ts_poll_kst: str     # 수집 시각 (ISO-8601, KST)
    ok: bool
    error: Optional[str] = None

    @property
    def change_pct(self) -> Optional[float]:
        if self.price and self.prev_close:
            return (self.price - self.prev_close) / self.prev_close * 100.0
        return None


# --------------------------------------------------------------------------- #
# Fetcher: 현재는 yfinance 백엔드만. 나중에 네이버/브로커 백엔드 추가 가능.
# --------------------------------------------------------------------------- #
def fetch_yfinance(asset: Asset, poll_utc: datetime) -> Quote:
    base = dict(
        symbol=asset.symbol,
        name=asset.name,
        ts_poll_utc=poll_utc.isoformat(),
        ts_poll_kst=poll_utc.astimezone(KST).isoformat(),
        source="yfinance",
    )
    try:
        fi = yf.Ticker(asset.symbol).fast_info
        price = _safe_get(fi, "last_price", "lastPrice")
        prev = _safe_get(fi, "previous_close", "previousClose")
        vol = _safe_get(fi, "last_volume", "lastVolume",
                        "regular_market_volume", "regularMarketVolume")
        ccy = _safe_get(fi, "currency")
        if price is None:
            return Quote(**base, price=None, prev_close=_as_float(prev),
                         volume=_as_float(vol), currency=ccy,
                         ok=False, error="last_price 없음")
        return Quote(**base, price=float(price), prev_close=_as_float(prev),
                     volume=_as_float(vol), currency=ccy, ok=True)
    except Exception as e:  # 한 종목 실패가 루프 죽이지 않게
        return Quote(**base, price=None, prev_close=None, volume=None,
                     currency=None, ok=False, error=f"{type(e).__name__}: {e}")


def _safe_get(fast_info, *keys):
    """yfinance fast_info는 버전별로 키 이름이 다름(snake/camel). 둘 다 시도."""
    for k in keys:
        try:
            v = fast_info[k]
        except (KeyError, TypeError):
            v = getattr(fast_info, k, None)
        if v is not None:
            return v
    return None


def _as_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 저장소
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_poll_utc TEXT    NOT NULL,
    ts_poll_kst TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    price       REAL,
    prev_close  REAL,
    change_pct  REAL,
    volume      REAL,
    currency    TEXT,
    source      TEXT    NOT NULL,
    ok          INTEGER NOT NULL,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_ticks_symbol_ts ON ticks(symbol, ts_poll_utc);
"""


class Store:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """기존(volume 없는) DB도 깨지지 않게 컬럼을 사후 추가한다."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(ticks)")}
        if "volume" not in cols:
            self.conn.execute("ALTER TABLE ticks ADD COLUMN volume REAL")
            log.info("기존 DB 마이그레이션: ticks.volume 컬럼 추가")

    def insert(self, q: Quote) -> None:
        self.conn.execute(
            """INSERT INTO ticks
               (ts_poll_utc, ts_poll_kst, symbol, name, price, prev_close,
                change_pct, volume, currency, source, ok, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (q.ts_poll_utc, q.ts_poll_kst, q.symbol, q.name, q.price,
             q.prev_close, q.change_pct, q.volume, q.currency, q.source,
             int(q.ok), q.error),
        )

    def commit(self) -> None:
        self.conn.commit()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()


# --------------------------------------------------------------------------- #
# 메인 루프
# --------------------------------------------------------------------------- #
_stop = False


def _handle_sigint(signum, frame):
    global _stop
    _stop = True
    log.info("종료 신호 수신. 마지막 커밋 후 정지...")


def run(interval: float, duration: Optional[float], db_path: str) -> None:
    store = Store(db_path)
    signal.signal(signal.SIGINT, _handle_sigint)

    log.info("수집 시작 | DB=%s | 간격=%ss | 대상=%s",
             db_path, interval, ", ".join(a.symbol for a in ASSETS))
    start = time.monotonic()
    rounds = 0

    while not _stop:
        poll_utc = datetime.now(timezone.utc)
        line = []
        for asset in ASSETS:
            q = fetch_yfinance(asset, poll_utc)
            store.insert(q)
            if q.ok:
                chg = f"{q.change_pct:+.2f}%" if q.change_pct is not None else "  -  "
                vol = f" vol={q.volume:,.0f}" if q.volume is not None else ""
                line.append(f"{q.name}={q.price:,.2f}({chg}){vol}")
            else:
                line.append(f"{q.name}=ERR")
        store.commit()
        rounds += 1
        log.info("[%d] %s", rounds, " | ".join(line))

        if duration is not None and (time.monotonic() - start) >= duration:
            log.info("지정 시간 도달. 종료.")
            break

        # 종료 신호에 빠르게 반응하도록 잘게 나눠 대기
        slept = 0.0
        while slept < interval and not _stop:
            time.sleep(min(0.2, interval - slept))
            slept += 0.2

    total = store.count()
    store.close()
    log.info("정지. 총 적재 행수=%d", total)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="하이닉스/마이크론/환율 실시간 폴링 수집기")
    p.add_argument("--interval", type=float, default=2.0, help="폴링 간격(초), 기본 2")
    p.add_argument("--duration", type=float, default=None, help="총 수집 시간(초), 미지정시 무한")
    p.add_argument("--db", default="ticks.db", help="SQLite 경로, 기본 ticks.db")
    return p.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    run(interval=args.interval, duration=args.duration, db_path=args.db)
