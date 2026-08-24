# -*- coding: utf-8 -*-
"""每日晨报文案撰写器：数据同步完成后，由模型基于当日底座撰写核心观点/宏观解读/风险提示。

产物写入 daily_brief_sections（按 日期+栏目 幂等）。周末/节假日照常产出，
行情类内容自动标注"最近交易日"口径。
"""
import json
import argparse
import os
import re
import sqlite3
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB = PROJECT_ROOT / "data" / "curated" / "consumer-research.db"
MCP_URL = os.environ.get("GILDATA_MCP_URL") or (
    f"https://api.gildata.com/mcp-servers/aidata-assistant-srv-tool?token={os.environ.get('GILDATA_MCP_TOKEN', '')}"
)
LLM_URL = os.environ.get("LOCAL_LLM_URL", "http://127.0.0.1:15721/v1/chat/completions")
BJ = timezone(timedelta(hours=8))

DDL = """
CREATE TABLE IF NOT EXISTS daily_brief_sections (
  brief_date TEXT NOT NULL,
  section TEXT NOT NULL,
  content_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (brief_date, section)
);
"""


def log(msg: str) -> None:
    print(f"[{datetime.now(BJ).isoformat(timespec='seconds')}] {msg}", flush=True)


def build_context(db: sqlite3.Connection, now: datetime) -> tuple[str, int]:
    db.row_factory = sqlite3.Row
    since = (now - timedelta(days=2)).isoformat()
    until = now.isoformat()
    until_date = now.strftime("%Y-%m-%d")
    events = db.execute(
        """SELECT title, summary, event_type, available_at, raw_json FROM monitor_events
           WHERE status='accepted' AND available_at >= ? AND available_at <= ?
           ORDER BY available_at DESC LIMIT 12""",
        (since, until),
    ).fetchall()
    snapshot = db.execute(
        """SELECT title, summary FROM monitor_events WHERE status='accepted' AND event_type='market_move'
           AND available_at <= ? ORDER BY available_at DESC LIMIT 1""",
        (until,),
    ).fetchone()
    ratings = db.execute(
        """SELECT security_name, total_score, rationale, rating_date FROM daily_stock_ratings
           WHERE rating_date=(SELECT MAX(rating_date) FROM daily_stock_ratings WHERE rating_date <= ?)
           ORDER BY total_score DESC LIMIT 10""",
        (until_date,),
    ).fetchall()
    releases = db.execute(
        """SELECT d.title, (SELECT c.text_content FROM document_chunks c
             WHERE c.document_id=d.document_id ORDER BY c.sequence_no LIMIT 1) AS key_figure
           FROM documents d WHERE d.document_type='official_statistics_release' AND d.status='curated'
             AND d.published_at <= ?
           ORDER BY d.published_at DESC LIMIT 4""",
        (until,),
    ).fetchall()

    lines = []
    if snapshot:
        lines.append(f"【市场快照】{snapshot['title']}：{(snapshot['summary'] or '')[:220]}")
    lines.append("【近 48 小时事件】")
    for e in events:
        raw = json.loads(e["raw_json"] or "{}")
        layer = raw.get("research_layer", {})
        lines.append(f"- [{str(e['available_at'])[:16]}] {e['title']}（{layer.get('so_what') or (e['summary'] or '')[:70]}）")
    if ratings:
        rdate = ratings[0]["rating_date"]
        lines.append(f"【股票评级·{rdate} 评分前列】")
        for r in ratings:
            lines.append(f"- {r['security_name']} {r['total_score']}：{r['rationale']}")
    lines.append("【官方宏观发布】")
    for r in releases:
        lines.append(f"- {r['title']}：{(r['key_figure'] or '')[:80]}")
    return "\n".join(lines), len(events) + len(ratings)


def llm_write(context: str, now: datetime, is_trading_day: bool) -> dict:
    weekday_cn = "一二三四五六日"[now.weekday()]
    day_note = (
        f"今天是 {now.strftime('%Y-%m-%d')} 星期{weekday_cn}，为交易日，行情为当日或最近收盘。"
        if is_trading_day else
        f"今天是 {now.strftime('%Y-%m-%d')} 星期{weekday_cn}，股市休市，行情与评级为最近交易日口径，请在相关表述中注明。"
    )
    prompt = (
        "你是服务公募基金经理的资深消费行业研究员，正在撰写每日消费行研晨报的核心文案。"
        + day_note
        + "严格基于所给研究底座内容撰写，禁止编造数据；每条观点可直接溯源到所给事件或数据。\n\n"
        "请严格按以下分隔格式输出（不要输出任何其他标记或解释）：\n"
        "【大盘判断|neutral或bullish或bearish】150-200字\n"
        "【最重要边际|bullish或neutral】150-200字\n"
        "【交易提示|bullish或neutral】150-200字（开头注明研究观点非交易指令）\n"
        "【风险预警|risk】150-200字\n"
        "【宏观解读】100-150字\n"
        "【风格判断】80-100字\n"
        "【催化剂】60-80字（无则写：暂无重大催化剂）\n"
        "【风险1】不超过50字\n"
        "【风险2】不超过50字\n"
        "【风险3】不超过50字\n"
        "每条写足即停，不要超出字数，写完最后一条立即结束。"
    )
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"【研究底座】\n{context}"},
    ]
    body = json.dumps({"model": "kimi-k3", "messages": messages, "max_tokens": 7000, "stream": False}).encode("utf-8")
    req = urllib.request.Request(
        LLM_URL, data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer PROXY_MANAGED"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = data["choices"][0]["message"]["content"].strip()
    return parse_sections(text)


def parse_ok(content: dict) -> bool:
    """内容达标：至少 2 条真实核心观点且宏观解读非空。"""
    real = [t for t in content["takeaway"] if t["text"] and "未覆盖" not in t["text"] and len(t["text"]) >= 60]
    return len(real) >= 2 and bool(content.get("macro_read"))


def llm_write_with_retry(context: str, now: datetime, is_trading_day: bool) -> dict:
    for attempt in range(2):
        try:
            content = llm_write(context, now, is_trading_day)
        except Exception as exc:
            log(f"  第 {attempt + 1} 次撰写调用失败: {type(exc).__name__} {str(exc)[:60]}")
            continue
        if parse_ok(content):
            return content
        log(f"  第 {attempt + 1} 次撰写内容不达标，重试…")
    raise ValueError("连续两次撰写均未达标，保留既有晨报文案")


def llm_write_events(context: str, now: datetime, is_trading_day: bool) -> dict:
    """独立小调用：宏观政策/重点研报/行业大事件三段（输出短，成功率高）。"""
    weekday_cn = "一二三四五六日"[now.weekday()]
    prompt = (
        "你是服务公募基金经理的资深消费行业研究员。基于研究底座，选出今天最值得关注的内容并严格按分隔格式输出"
        f"（今天 {now.strftime('%Y-%m-%d')} 星期{weekday_cn}{'，交易日' if is_trading_day else '，股市休市'}）。"
        "禁止编造；每条写足即停，写完立即结束，不要任何其他文字：\n"
        "【宏观政策1】一行标题（≤30字）\n100-160字：这条宏观/政策为何今天最值得关注\n"
        "【宏观政策2】一行标题\n100-160字正文\n"
        "【重点研报】一行标题（含机构名）\n100-160字：该研报核心观点与时效价值\n"
        "【行业事件1】一行标题\n100-160字：对消费行业的具体影响\n"
        "【行业事件2】一行标题\n100-160字正文\n"
        "格式补充：每条正文结束后另起一行，写“来源：素材中对应事件或文档的原标题”（必须逐字引用素材原标题；由数据直接得出而没有对应事件时写“来源：官方发布数据”）。"
    )
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"【研究底座】\n{context}"},
    ]
    body = json.dumps({"model": "kimi-k3", "messages": messages, "max_tokens": 7000, "stream": False}).encode("utf-8")
    req = urllib.request.Request(
        LLM_URL, data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer PROXY_MANAGED"},
    )
    with urllib.request.urlopen(req, timeout=240) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = data["choices"][0]["message"]["content"].strip()
    parsed = parse_sections(text)
    return parsed["daily_events"]


def events_ok(daily_events: dict) -> bool:
    """大事件达标：宏观政策与行业事件至少各 1 条真实内容。"""
    real_p = [x for x in daily_events.get("macro_policies", []) if x.get("text")]
    real_i = [x for x in daily_events.get("industry_events", []) if x.get("text")]
    return len(real_p) >= 1 and len(real_i) >= 1


def llm_write_events_with_retry(context: str, now: datetime, is_trading_day: bool) -> dict | None:
    for attempt in range(2):
        try:
            daily_events = llm_write_events(context, now, is_trading_day)
        except Exception as exc:
            log(f"  大事件第 {attempt + 1} 次调用失败: {type(exc).__name__} {str(exc)[:60]}")
            continue
        if events_ok(daily_events):
            return daily_events
        log(f"  大事件第 {attempt + 1} 次不达标，重试…")
    log("  大事件两次未达标，本期留空（明日再试）")
    return None


TAG_RE = re.compile(r"【([^】]+)】")


def llm_write_library(context: str, now: datetime, is_trading_day: bool) -> list[dict]:
    """每日研报库：从素材中选出基金经理应关注的研报/新闻/政策，
    按重要程度分两类，分点摘要，标注来源。"""
    weekday_cn = "一二三四五六日"[now.weekday()]
    prompt = (
        "你是服务公募基金经理的资深消费行业研究员。基于研究底座素材，"
        f"选出基金经理今天（{now.strftime('%Y-%m-%d')} 星期{weekday_cn}{'，交易日' if is_trading_day else '，股市休市'}）最应该关注的研报、新闻、政策共 6-10 条，"
        "按重要程度分为「重要且紧急」（当日必须看：行情异动、当日政策发布、重大公告、评级调整）"
        "与「重要不紧急」（值得读但可安排：深度研报、趋势分析、一般行业动态）两类。"
        "严格按以下分隔格式输出，不要任何其他文字：\n"
        "【紧急1】类型（研报/新闻/政策/行业事件）| 一行标题（≤32字）\n"
        "板块：从【白酒/食品饮料/美容个护/宠物家庭新消费/家电/汽车出行/家居/纺织服饰/零售电商/餐饮本地生活/酒店旅游/文化娱乐】中选一个\n"
        "3-5 个分点摘要（每点一行，以「·」开头，写清事实与数据）\n"
        "来源：素材中对应事件或文档的原标题\n"
        "【紧急2】……\n"
        "【常规1】类型 | 标题\n板块：……\n3-5 个分点摘要\n来源：……\n"
        "【常规2】……\n"
        "禁止编造；每条写足即停，写完立即结束。"
    )
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"【研究底座】\n{context}"},
    ]
    body = json.dumps({"model": "kimi-k3", "messages": messages, "max_tokens": 12000, "stream": False}).encode("utf-8")
    req = urllib.request.Request(
        LLM_URL, data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer PROXY_MANAGED"},
    )
    with urllib.request.urlopen(req, timeout=480) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    msg = data["choices"][0]["message"]
    text = (msg.get("content") or "").strip()
    if not text:
        log(f"  研报库模型正文为空（推理消耗 {len(msg.get('reasoning_content') or '')} 字符），本次解析必为 0 条")
    return parse_library(text)


ITEM_RE = re.compile(r"【(紧急|常规)\d*】")


def parse_library(text: str) -> list[dict]:
    """解析研报库分隔格式为条目列表。"""
    text = text.replace("**", "").replace("｜", "|")
    parts = ITEM_RE.split(text)
    items = []
    for i in range(1, len(parts) - 1, 2):
        urgent = parts[i] == "紧急"
        block = parts[i + 1].strip()
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        head = lines[0]
        item_type, _, title = head.partition("|") if "|" in head else ("", "", head)
        sector = ""
        points = []
        source_hint = None
        for ln in lines[1:]:
            if ln.startswith(("板块：", "板块:")):
                sector = ln.split("：", 1)[-1].split(":", 1)[-1].strip()
            elif ln.startswith(("来源：", "来源:")):
                source_hint = ln.split("：", 1)[-1].split(":", 1)[-1].strip().strip("。")
            elif ln.startswith(("·", "-", "•")):
                points.append(ln.lstrip("·-•").strip())
            elif not points:
                points.append(ln)
        if title and points:
            items.append({
                "category": "重要且紧急" if urgent else "重要不紧急",
                "item_type": (item_type or "新闻").strip() or "新闻",
                "sector": sector,
                "title": title.strip(),
                "points": points[:6],
                "source_hint": source_hint,
            })
    return items


def library_ok(items: list[dict]) -> bool:
    return len(items) >= 4 and any(i["category"] == "重要且紧急" for i in items) and any(i["category"] == "重要不紧急" for i in items)


def fallback_library(db: sqlite3.Connection, now: datetime) -> list[dict]:
    """模型服务不可用时，从当天已入库的真实事件生成可追溯研报库。"""
    db.row_factory = sqlite3.Row
    since = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    until = now.isoformat()
    rows = db.execute(
        """SELECT title, summary, event_type, available_at
           FROM monitor_events
           WHERE status='accepted' AND date(available_at) >= ? AND available_at <= ?
             AND event_type <> 'market_move'
           ORDER BY available_at DESC LIMIT 8""",
        (since, until),
    ).fetchall()

    def sector_for(text: str) -> str:
        rules = (
            ("白酒", r"白酒|茅台|五粮液|酒价|飞天"),
            ("食品饮料", r"食品|饮料|乳品|啤酒|调味|零食|社零|CPI"),
            ("美容个护", r"美妆|护肤|个护|化妆品"),
            ("宠物家庭新消费", r"宠物|母婴|玩具|潮玩"),
            ("家电", r"家电|空调|冰箱|洗衣机|家居|家具"),
            ("汽车出行", r"汽车|新能源车|出行"),
            ("纺织服饰", r"服装|纺织|鞋|运动户外"),
            ("零售电商", r"零售|电商|商超|百货|县域消费|下沉市场"),
            ("餐饮本地生活", r"餐饮|茶饮|咖啡|本地生活"),
            ("酒店旅游", r"酒店|旅游|文旅|景区|体育"),
            ("文化娱乐", r"游戏|影视|教育|娱乐"),
        )
        for sector, pattern in rules:
            if re.search(pattern, text):
                return sector
        return "零售电商"

    def summary_points(summary: str, available_at: str) -> list[str]:
        clean = re.sub(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}[，,]?", "", summary or "").strip()
        parts = [p.strip() for p in re.split(r"[。；;]", clean) if len(p.strip()) >= 8]
        points = [p[:150] for p in parts[:3]]
        points.append(f"信息时间：{str(available_at)[:16].replace('T', ' ')}")
        return points

    items = []
    for index, row in enumerate(rows):
        text = f"{row['title']} {row['summary'] or ''}"
        is_policy = row["event_type"] == "policy_release" or bool(re.search(r"政策|意见|方案|规划|部门|发改委|商务部|市监", text))
        is_report = row["event_type"] == "research_report"
        is_enterprise_risk = row["event_type"] == "enterprise_risk"
        item_type = "企业风险" if is_enterprise_risk else "研报" if is_report else "政策" if is_policy else ("行业事件" if row["event_type"] == "industry_data_release" else "新闻")
        items.append({
            "category": "重要且紧急" if is_enterprise_risk else "重要不紧急" if is_report else "重要且紧急" if index < 3 or is_policy else "重要不紧急",
            "item_type": item_type,
            "sector": sector_for(text),
            "title": row["title"],
            "points": summary_points(row["summary"] or "", row["available_at"]),
            "source_hint": row["title"],
        })

    if len(items) >= 4 and not any(x["category"] == "重要不紧急" for x in items):
        items[-1]["category"] = "重要不紧急"
    log(f"  模型不可用，研报库使用真实底座兜底生成 {len(items)} 条")
    return items


def fallback_brief_content(db: sqlite3.Connection, now: datetime) -> dict:
    """用当日行情、评级和事件生成无编造的晨报核心栏目。"""
    db.row_factory = sqlite3.Row
    today = now.strftime("%Y-%m-%d")
    quote_date = db.execute("SELECT MAX(trade_date) FROM stock_daily_quotes WHERE trade_date <= ?", (today,)).fetchone()[0]
    sector_rows = []
    if quote_date:
        sector_rows = db.execute(
            """WITH member AS (
                   SELECT security_id, MIN(sector_code) AS sector_code
                   FROM research_universe_members GROUP BY security_id
               )
               SELECT COALESCE(p.sector_name,'未分类') AS sector_name,
                      COUNT(*) AS stock_count, AVG(q.change_pct) AS avg_change,
                      SUM(CASE WHEN q.change_pct>0 THEN 1 ELSE 0 END) AS up_count,
                      SUM(CASE WHEN q.change_pct<0 THEN 1 ELSE 0 END) AS down_count
               FROM stock_daily_quotes q
               LEFT JOIN member m ON m.security_id=q.security_id
               LEFT JOIN research_sector_packs p ON p.sector_code=m.sector_code
               WHERE q.trade_date=? GROUP BY COALESCE(p.sector_name,'未分类')
               ORDER BY avg_change DESC""",
            (quote_date,),
        ).fetchall()
    total = sum(int(r["stock_count"] or 0) for r in sector_rows)
    up = sum(int(r["up_count"] or 0) for r in sector_rows)
    down = sum(int(r["down_count"] or 0) for r in sector_rows)
    avg = (sum(float(r["avg_change"] or 0) * int(r["stock_count"] or 0) for r in sector_rows) / total) if total else 0.0
    strongest = sector_rows[:3]
    weakest = list(reversed(sector_rows[-3:])) if sector_rows else []

    rating_date = db.execute("SELECT MAX(rating_date) FROM daily_stock_ratings WHERE rating_date <= ?", (today,)).fetchone()[0]
    ratings = db.execute(
        """SELECT security_name,total_score,tier FROM daily_stock_ratings
           WHERE rating_date=? ORDER BY total_score DESC LIMIT 5""",
        (rating_date,),
    ).fetchall() if rating_date else []
    market_event = db.execute(
        """SELECT monitor_event_id,title FROM monitor_events
           WHERE status='accepted' AND event_type='market_move' AND available_at<=?
           ORDER BY available_at DESC LIMIT 1""",
        (now.isoformat(),),
    ).fetchone()
    library = fallback_library(db, now)
    policy_items = [x for x in library if x["item_type"] == "政策"][:2]
    research_items = [x for x in library if x["item_type"] == "研报"][:1]
    enterprise_risks = [x for x in library if x["item_type"] == "企业风险"][:3]
    industry_items = [x for x in library if x["item_type"] != "政策"][:2]

    def sector_text(rows: list[sqlite3.Row]) -> str:
        return "、".join(f"{r['sector_name']} {float(r['avg_change'] or 0):+.2f}%" for r in rows) or "暂无可用板块行情"

    market_ref = [market_event["monitor_event_id"]] if market_event else []
    rating_text = "、".join(f"{r['security_name']}（{r['total_score']:.1f}分）" for r in ratings) or "暂无当日评级"
    policy_text = "；".join(x["title"] for x in policy_items) or "当日未发现已入库的重要政策事件"
    takeaway = [
        {"label": "大盘判断", "tone": "neutral",
         "text": f"研究截止{now.strftime('%Y年%m月%d日')}08:00，采用最近可得交易日{quote_date or '暂无'}的全消费A股行情。覆盖{total}只股票，等权平均涨跌幅{avg:+.2f}%，上涨{up}只、下跌{down}只；相对较强板块为{sector_text(strongest)}，较弱板块为{sector_text(weakest)}。以上为客观行情汇总，不使用旧日期模板。",
         "refs": market_ref},
        {"label": "最重要边际", "tone": "neutral",
         "text": f"截至今日08:00，底座新增政策与行业信息中最值得跟踪的是：{policy_text}。板块强弱继续以{sector_text(strongest)}为主要边际，后续需结合成交与公司公告验证持续性。",
         "refs": market_ref},
        {"label": "交易提示", "tone": "neutral",
         "text": f"研究观点（非交易指令）：{rating_date or '最近批次'}综合评分前列为{rating_text}。建议仅将其作为今日研究候选，并继续核验估值、公告和基本面，不据此自动交易。",
         "refs": []},
        {"label": "风险预警", "tone": "risk",
         "text": (("今日企业风险新增：" + "；".join(x["title"] for x in enterprise_risks)) if enterprise_risks else
                  f"今日重点企业风险流未发现新增记录；当前较弱板块为{sector_text(weakest)}。无新增不等于风险消失。") +
                 " 行情口径为最近可得交易日，盘中变化需等待收盘补同步确认。",
         "refs": market_ref},
    ]
    daily_events = {
        "macro_policies": [{"title": x["title"], "text": "；".join(x["points"][:3]), "source_hint": x["source_hint"]} for x in policy_items],
        "research_pick": ({"title": research_items[0]["title"], "text": "；".join(research_items[0]["points"][:3]),
                           "source_hint": research_items[0]["source_hint"]} if research_items else None),
        "industry_events": [{"title": x["title"], "text": "；".join(x["points"][:3]), "source_hint": x["source_hint"]}
                            for x in industry_items if x["item_type"] != "研报"][:2],
    }
    return {
        "takeaway": takeaway,
        "macro_read": f"今日08:00前宏观与政策增量：{policy_text}。仅依据已入库来源陈述；无新增的官方月度数据沿用最近发布值并保留原数据期。",
        "sector_style": f"最近交易日板块表现：强势端为{sector_text(strongest)}；弱势端为{sector_text(weakest)}。",
        "catalysts": "；".join(x["title"] for x in (policy_items + industry_items)) or "暂无已入库的当日重大催化剂",
        "risks": [
            *( ["企业风险新增：" + "；".join(x["title"] for x in enterprise_risks)] if enterprise_risks else [] ),
            f"弱势板块：{sector_text(weakest)}。",
            "公告、企业风险和研报专流若无新增，页面必须保留数据日期，不以旧内容冒充当日更新。",
            "盘前评级使用最近收盘行情，盘中变化以收盘后补同步为准。",
        ],
        "daily_events": daily_events,
    }


def llm_write_library_with_retry(context: str, now: datetime, is_trading_day: bool) -> list[dict] | None:
    for attempt in range(3):
        try:
            items = llm_write_library(context, now, is_trading_day)
        except Exception as exc:
            log(f"  研报库第 {attempt + 1} 次调用失败: {type(exc).__name__} {str(exc)[:60]}")
            continue
        if library_ok(items):
            return items
        log(f"  研报库第 {attempt + 1} 次不达标（{len(items)} 条），重试…")
    log("  研报库两次未达标，本期留空（明日再试）")
    return None


def parse_sections(text: str) -> dict:
    """把分隔格式文案解析为结构化 dict；容错：缺项用占位说明。"""
    text = text.replace("**", "").replace("｜", "|")
    parts = TAG_RE.split(text)
    fields: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        tag = parts[i].strip()
        fields[tag] = parts[i + 1].strip()

    def get(label: str) -> tuple[str, str]:
        for key, body in fields.items():
            if key == label or key.startswith(label + "|"):
                tone = key.split("|", 1)[1].strip() if "|" in key else ""
                return body, tone
        return "", ""

    takeaway = []
    for label in ("大盘判断", "最重要边际", "交易提示", "风险预警"):
        body, tone = get(label)
        if tone not in ("neutral", "bullish", "bearish", "risk"):
            tone = "risk" if label == "风险预警" else "neutral"
        takeaway.append({"label": label, "tone": tone, "text": body or "（本次撰写未覆盖此项）"})

    risks = [fields[k] for k in ("风险1", "风险2", "风险3") if fields.get(k)]
    def titled(label: str) -> dict | None:
        body, _tone = get(label)
        if not body:
            return None
        first, _, rest = body.partition("\n")
        text_part = rest.strip() or first.strip()
        source_hint = None
        m = re.search(r"来源[：:](.+?)(?:\n|$)", text_part)
        if m:
            source_hint = m.group(1).strip().strip("。")
            text_part = text_part[:m.start()].strip()
        return {"title": first.strip(), "text": text_part, "source_hint": source_hint}

    daily_events = {
        "macro_policies": [x for x in (titled("宏观政策1"), titled("宏观政策2")) if x],
        "research_pick": titled("重点研报"),
        "industry_events": [x for x in (titled("行业事件1"), titled("行业事件2")) if x],
    }
    return {
        "takeaway": takeaway,
        "macro_read": fields.get("宏观解读", ""),
        "sector_style": fields.get("风格判断", ""),
        "catalysts": fields.get("催化剂", "暂无重大催化剂"),
        "risks": risks or ["（本次撰写未生成风险提示）"],
        "daily_events": daily_events,
    }


def run(target_date: str | None = None) -> dict:
    now = datetime.now(BJ)
    if target_date:
        now = datetime.fromisoformat(f"{target_date}T08:00:00+08:00")
    today = now.strftime("%Y-%m-%d")
    is_trading_day = now.weekday() < 5
    db = sqlite3.connect(DB)
    db.execute(DDL)
    context, n_items = build_context(db, now)
    log(f"上下文就绪（{n_items} 条素材，{'交易日' if is_trading_day else '非交易日'}），开始撰写 {today} 晨报文案…")
    try:
        content = llm_write_with_retry(context, now, is_trading_day)
    except ValueError as exc:
        log(f"核心文案暂不可用：{exc}；改用当日真实底座生成核心栏目")
        content = fallback_brief_content(db, now)
    daily_events = llm_write_events_with_retry(context, now, is_trading_day)
    if daily_events:
        content["daily_events"] = daily_events
    elif not content.get("daily_events"):
        content.pop("daily_events", None)
    library = llm_write_library_with_retry(context, now, is_trading_day)
    if not library:
        library = fallback_library(db, now)
    if library:
        content["research_library"] = library
    now_utc = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    for section, value in content.items():
        db.execute(
            "INSERT OR REPLACE INTO daily_brief_sections(brief_date, section, content_json, created_at) VALUES(?,?,?,?)",
            (today, section, json.dumps(value, ensure_ascii=False), now_utc),
        )
    db.commit()
    db.close()
    log(f"{today} 晨报文案已入库：{list(content.keys())}")
    return {"date": today, "sections": list(content.keys())}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="补跑指定日期 YYYY-MM-DD")
    args = ap.parse_args()
    run(args.date)
