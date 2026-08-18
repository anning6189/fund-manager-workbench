"""交易日收盘同步：更新全池行情快照与股票评级，不改写08:00晨报口径。"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / "data" / "monitoring" / "module3-realtime-research"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "close-sync.log"
BJ = timezone(timedelta(hours=8))

sys.path.insert(0, str(PROJECT_ROOT / "tools"))
import consumer_stock_focus  # noqa: E402


def log(message: str) -> None:
    line = f"[{datetime.now(BJ).isoformat(timespec='seconds')}] {message}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def main() -> int:
    now = datetime.now(BJ)
    if now.weekday() >= 5:
        log("非交易日，跳过收盘同步")
        return 0
    log("===== 收盘行情与评级同步开始 =====")
    try:
        result = consumer_stock_focus.run()
    except Exception as exc:
        log(f"收盘同步失败: {type(exc).__name__} {exc}")
        return 1
    log(f"收盘同步完成: 评级批次{result['date']}，行情实际交易日{result['market_date']}，{result['count']}只，分布 {result['tiers']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
