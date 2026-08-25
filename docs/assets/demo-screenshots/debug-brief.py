# -*- coding: utf-8 -*-
# 直接调 morning_brief 复现 500
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(r"C:\Users\chi\Documents\ChatGPT\New project\apps\fund-manager-workbench")))
import server as srv

service = srv.WorkbenchService(
    db_path=Path(r"C:\Users\chi\Documents\ChatGPT\New project\data\curated\consumer-research.db"),
    data_root=Path(r"C:\Users\chi\Documents\ChatGPT\New project\data\workbench\module5-fund-manager"),
    identity=srv.Identity(name="debug", role="public_fund_manager"),
    static_root=Path(r"C:\Users\chi\Documents\ChatGPT\New project\apps\fund-manager-workbench\public"),
)
try:
    d = service.morning_brief()
    print("OK, daily_picks keys:", list((d["macro_policy"].get("daily_picks") or {}).keys()))
except Exception:
    traceback.print_exc()
