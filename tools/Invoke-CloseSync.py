"""交易日收盘同步：更新全池行情快照与股票评级，不改写08:00晨报口径。

若收盘行情源尚未落库导致覆盖不足，本脚本会通过 systemd 自动安排下一次重试：
16:10 首次尝试；失败后补排 16:30、17:00，此后每半小时一次，直到成功。
"""
import os
import subprocess
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / "data" / "monitoring" / "module3-realtime-research"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "close-sync.log"
BJ = timezone(timedelta(hours=8))

sys.path.insert(0, str(PROJECT_ROOT / "tools"))
import consumer_stock_focus  # noqa: E402

RETRY_TIMER = "consumer-research-close-sync-retry.timer"
RETRY_SERVICE = "consumer-research-close-sync.service"


def log(message: str) -> None:
    line = f"[{datetime.now(BJ).isoformat(timespec='seconds')}] {message}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def next_retry_time(now: datetime) -> datetime:
    today_1630 = datetime.combine(now.date(), time(16, 30), BJ)
    today_1700 = datetime.combine(now.date(), time(17, 0), BJ)
    if now < today_1630:
        return today_1630
    if now < today_1700:
        return today_1700
    minute = 30 if now.minute < 30 else 0
    hour = now.hour if now.minute < 30 else now.hour + 1
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(minutes=30)
    return candidate


def schedule_retry(reason: str) -> None:
    if os.name == "nt":
        log(f"收盘同步重试未注册（非 systemd 环境）：{reason}")
        return
    retry_at = next_retry_time(datetime.now(BJ))
    command = [
        "systemd-run",
        "--unit", "consumer-research-close-sync-retry",
        "--on-calendar", retry_at.strftime("%Y-%m-%d %H:%M:%S"),
        "--property", "Description=Retry Consumer Research Close Market Sync",
        "/bin/systemctl", "start", RETRY_SERVICE,
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
        log(f"收盘行情源未完成落库，已自动安排下次重试：{retry_at.isoformat(timespec='minutes')}；原因：{reason}")
    except Exception as exc:
        log(f"收盘同步重试注册失败: {type(exc).__name__} {exc}")


def clear_retry_timer() -> None:
    if os.name == "nt":
        return
    subprocess.run(["systemctl", "reset-failed", RETRY_SERVICE], check=False, capture_output=True, timeout=20)
    subprocess.run(["systemctl", "reset-failed", "consumer-research-close-sync-retry.service"], check=False, capture_output=True, timeout=20)


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
        if "行情覆盖不足80%" in str(exc):
            schedule_retry(str(exc))
        return 1
    clear_retry_timer()
    log(f"收盘同步完成: 评级批次{result['date']}，行情实际交易日{result['market_date']}，{result['count']}只，分布 {result['tiers']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
