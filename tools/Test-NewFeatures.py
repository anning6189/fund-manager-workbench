# -*- coding: utf-8 -*-
"""新功能验收测试：股票关注/晨报日更/AI研究员/Token用量/数据来源。
运行前确保本机服务在 127.0.0.1:8765 运行。"""
import json
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "http://127.0.0.1:8765"
DB = Path(r"C:\Users\chi\Documents\ChatGPT\New project\data\curated\consumer-research.db")

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


brief = get("/api/morning-brief")
check("晨报-核心观点4条", len(brief["takeaway"]) == 4, f"{len(brief['takeaway'])}条")
check("晨报-观点字数≥100", all(len(t["text"]) >= 100 for t in brief["takeaway"]),
      "/".join(str(len(t["text"])) for t in brief["takeaway"]))
check("晨报-宏观解读非空", bool(brief["macro_policy"]["read"]))
check("晨报-风险提示≥1", len(brief["risks"]) >= 1, f"{len(brief['risks'])}条")
check("晨报-文案日期存在", bool(brief.get("brief_source_date")), str(brief.get("brief_source_date")))

focus = get("/api/stock-focus")
counts = focus.get("counts", {})
check("评级-四档齐全", all(t in counts for t in ["重点关注", "增持观察", "中性", "回避"]), str(counts))
check("评级-重点关注=20", counts.get("重点关注") == 20)
total = sum(counts.values())
check("评级-总展示≥120", total >= 120, f"{total}只")
first = focus["tiers"]["重点关注"][0]
check("评级-字段完整", all(k in first for k in ("security_name", "close_price", "change_pct", "pe_ttm", "total_score", "rationale")))
check("评级-含板块字段", "sector_name" in first)

usage = get("/api/token-usage")
check("用量-可用", usage.get("available") is True)
check("用量-本月>0", usage["month"]["tokens"] > 0, f"{usage['month']['tokens']:,}")
check("用量-最近调用非空", len(usage["recent"]) > 0)

sources = get("/api/data-sources")
groups = {g["key"]: g["items"] for g in sources["groups"]}
check("来源-聚源≥7", len(groups.get("gildata", [])) >= 7, f"{len(groups.get('gildata', []))}")
check("来源-官方≥10", len(groups.get("official", [])) >= 10, f"{len(groups.get('official', []))}")

db = sqlite3.connect(DB)
db.row_factory = sqlite3.Row
sections = {r["section"] for r in db.execute(
    "SELECT DISTINCT section FROM daily_brief_sections WHERE brief_date=(SELECT MAX(brief_date) FROM daily_brief_sections)").fetchall()}
check("文案-栏目齐全", {"takeaway", "macro_read", "sector_style", "risks"} <= sections, str(sections))
rated = db.execute("SELECT COUNT(*) FROM daily_stock_ratings WHERE rating_date=(SELECT MAX(rating_date) FROM daily_stock_ratings)").fetchone()[0]
check("评级表-最新批次≥120", rated >= 120, f"{rated}行")
lo, hi = db.execute("SELECT MIN(total_score), MAX(total_score) FROM daily_stock_ratings").fetchone()
check("评级表-分数区间合理", 0 <= lo <= hi <= 100, f"{lo}~{hi}")
qdates = db.execute("SELECT COUNT(DISTINCT trade_date) FROM stock_daily_quotes").fetchone()[0]
check("行情快照≥20个交易日", qdates >= 20, f"{qdates}个交易日")
risk_count = db.execute(
    "SELECT COUNT(*) FROM monitor_events WHERE event_type='enterprise_risk' AND date(event_time)>=date('now','+8 hours','-1 day')"
).fetchone()[0]
check("企业风险-近两日已同步", risk_count >= 0, f"{risk_count}条新增（0条也代表已核验）")
risk_cursor = db.execute(
    "SELECT cursor_value,status FROM source_cursors WHERE source_id='CR.SRC.GILDATA.ENTERPRISE' AND stream_name='enterprise_risk'"
).fetchone()
today_bj = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
check("企业风险-每日游标成功", bool(risk_cursor and risk_cursor["status"] == "success" and today_bj in risk_cursor["cursor_value"]),
      str(dict(risk_cursor)) if risk_cursor else "游标缺失")
policy_cursors = db.execute(
    "SELECT source_id,cursor_value,status FROM source_cursors WHERE source_id IN ('CR.SRC.GOVCN','CR.SRC.NDRC','CR.SRC.SAMR') AND stream_name='official_policy_documents'"
).fetchall()
policy_ok = {row["source_id"] for row in policy_cursors if row["status"] == "success" and today_bj in row["cursor_value"]}
check("政策直连-三家官网每日游标成功",
      policy_ok == {"CR.SRC.GOVCN", "CR.SRC.NDRC", "CR.SRC.SAMR"}, str(sorted(policy_ok)))
industry_cursors = db.execute(
    "SELECT source_id,cursor_value,status FROM source_cursors WHERE source_id IN ('CR.SRC.CUSTOMS','CR.SRC.MOFCOM','CR.SRC.MIIT','CR.SRC.MCT') AND stream_name='official_industry_releases'"
).fetchall()
industry_ok = {row["source_id"] for row in industry_cursors if row["status"] == "success" and today_bj in row["cursor_value"]}
check("行业直连-四家官网每日游标成功",
      industry_ok == {"CR.SRC.CUSTOMS", "CR.SRC.MOFCOM", "CR.SRC.MIIT", "CR.SRC.MCT"}, str(sorted(industry_ok)))
member_stats = db.execute(
    "SELECT COUNT(*) rows_n,COUNT(DISTINCT security_id) ids,SUM(CASE WHEN sector_code IS NULL THEN 1 ELSE 0 END) unmapped FROM research_universe_members"
).fetchone()
check("研究池-一证券一主板块", member_stats["rows_n"] == member_stats["ids"],
      f"{member_stats['rows_n']}行/{member_stats['ids']}只")
check("研究池-未分类清零", member_stats["unmapped"] == 0, f"{member_stats['unmapped']}只")
hotel_sector = db.execute("SELECT sector_code FROM research_universe_members WHERE security_id='000428.SZ'").fetchone()
check("研究池-华天酒店归属旅游酒店", bool(hotel_sector and hotel_sector["sector_code"] == "CR.V.TL"),
      str(hotel_sector["sector_code"] if hotel_sector else None))
db.close()

heat_day = get("/api/sector-heatmap?period=day")
heat_month = get("/api/sector-heatmap?period=month")
check("热力图-日板块≥10", len(heat_day["sectors"]) >= 10, f"{len(heat_day['sectors'])}个")
check("热力图-月有锚定日", bool(heat_month["anchor_date"]), f"锚定{heat_month['anchor_date']}")
check("热力图-涨跌家数守恒", all(s["up_count"] + s["down_count"] <= s["stock_count"] for s in heat_day["sectors"]))

passed = sum(1 for _, ok, _ in results if ok)
print(f"\n===== {passed}/{len(results)} 通过 =====")
raise SystemExit(0 if passed == len(results) else 1)
