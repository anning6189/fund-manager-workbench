# -*- coding: utf-8 -*-
"""每日晨报同步（计划任务 08:30 运行）。

流程：聚源 HTTP MCP 拉取当日内容 -> 事件/观测进库 -> 游标更新 -> 监控重算。
脱离 agent 会话独立运行；幂等（同内容按 content_hash / event_id 去重）。
日志写入 data/monitoring/module3-realtime-research/daily-sync.log。
"""
import json
import html
import re
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB = PROJECT_ROOT / "data" / "curated" / "consumer-research.db"
LOG_DIR = PROJECT_ROOT / "data" / "monitoring" / "module3-realtime-research"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG = LOG_DIR / "daily-sync.log"
MCP_URL = "https://api.gildata.com/mcp-servers/aidata-assistant-srv-tool?token=ed82c6584c824d9ba18aeee99d852317"
LIC = "approved_internal_research_use"
BJ = timezone(timedelta(hours=8))

sys.path.insert(0, str(PROJECT_ROOT / "tools"))
import consumer_realtime_monitor as mon  # noqa: E402
import consumer_stock_focus as stock_focus  # noqa: E402
import consumer_brief_writer as brief_writer  # noqa: E402


def log(msg: str) -> None:
    line = f"[{datetime.now(BJ).isoformat(timespec='seconds')}] {msg}"
    print(line)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def rpc(method: str, params: dict, timeout: int = 60) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    req = urllib.request.Request(MCP_URL, data=json.dumps(payload).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def mcp_call(tool: str, query: str) -> str:
    result = rpc("tools/call", {"name": tool, "arguments": {"query": query}})
    parts = result.get("result", {}).get("content", [])
    return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict))


def mcp_results(tool: str, query: str) -> list[dict]:
    """MCP 返回的 text 是内层 JSON；解析后取 results 列表（table_markdown 内含真实换行）。"""
    text = mcp_call(tool, query)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    results = data.get("results", [])
    return results if isinstance(results, list) else []


MODULE_RULES = [
    (r"白酒|茅台|五粮液|汾酒|国窖|批价|酒业", "baijiu"),
    (r"空调|冰箱|洗衣机|家电|美的|格力|海尔|扫地机", "appliance"),
    (r"茶饮|咖啡|潮玩|泡泡玛特|宠物|美妆|免税|新能源汽车|宜家|MUJI|零售|餐饮", "new_consumer"),
    (r"社零|CPI|央行|人民银行|货币|利率|宏观|消费变迁", "macro"),
    (r"政策|规划|方案|促消费|扩消费|市监|发改委", "policy"),
    (r"食品|饮料|乳品|啤酒|调味|零食|农产品|原奶", "mass_food"),
]
BULLISH_HINT = re.compile(r"提价|增长|突破|回暖|改善|创新高|净流入|获批|利好|修复|超预期")
BEARISH_HINT = re.compile(r"下滑|亏损|下降|风险|处罚|净流出|低于预期|利空|缩减|暴跌")
CONSUMER_RELEVANT = re.compile(
    r"消费|零售|白酒|茅台|五粮液|酒|食品|饮料|乳品|啤酒|调味|零食|餐饮|茶饮|咖啡|家电|家居|家具|"
    r"免税|旅游|酒店|潮玩|宠物|美妆|个护|汽车|新能源车|电商|商超|百货|服装|纺织|鞋|农业|猪|奶|"
    r"社零|CPI|居民|促消费|扩消费|以旧换新|泡泡玛特|宜家|瑞幸|蜜雪|蒙牛|伊利|海天|农夫|李宁|安踏"
)

# 重点消费企业法律主体池。企业风险接口要求一次只查一个已核验法律主体；
# 每日并发轮询，严格按事件日期截取增量，再用稳定 ID 幂等入库。
ENTERPRISE_RISK_ENTITIES = (
    ("贵州茅台酒股份有限公司", "CR.S.FB"),
    ("宜宾五粮液股份有限公司", "CR.S.FB"),
    ("山西杏花村汾酒厂股份有限公司", "CR.S.FB"),
    ("内蒙古伊利实业集团股份有限公司", "CR.S.FB"),
    ("佛山市海天调味食品股份有限公司", "CR.S.FB"),
    ("美的集团股份有限公司", "CR.D.AP"),
    ("珠海格力电器股份有限公司", "CR.D.AP"),
    ("海尔智家股份有限公司", "CR.D.AP"),
    ("比亚迪股份有限公司", "CR.D.AU"),
    ("中国旅游集团中免股份有限公司", "CR.V.TL"),
    ("永辉超市股份有限公司", "CR.V.RT"),
    ("珀莱雅化妆品股份有限公司", "CR.S.PH"),
)
RISK_API_KIND = {
    "行政处罚": "行政处罚", "监管处罚": "行政处罚",
    "法院立案": "司法涉诉", "开庭公告": "司法涉诉", "裁判文书": "司法涉诉", "法院公告": "司法涉诉",
    "股权冻结": "股权冻结", "公司股权质押": "股权质押",
    "公司对外担保": "对外担保", "对外担保": "对外担保",
    "经营异常": "经营异常", "严重违法": "严重违法",
    "失信被执行人": "失信", "被执行人": "被执行", "限制高消费": "限制高消费",
    "欠税公告": "税务风险", "纳税非正常户": "税务风险", "重大税收违法": "税务风险",
}

OFFICIAL_POLICY_SOURCES = (
    ("CR.SRC.GOVCN", "中国政府网", "https://www.gov.cn/zhengce/zuixin/index.htm", ("gov.cn",)),
    ("CR.SRC.NDRC", "国家发展改革委", "https://www.ndrc.gov.cn/xwdt/tzgg/", ("ndrc.gov.cn",)),
    ("CR.SRC.SAMR", "市场监管总局", "https://www.samr.gov.cn/xw/zj/", ("samr.gov.cn",)),
)
OFFICIAL_INDUSTRY_SOURCES = (
    ("CR.SRC.CUSTOMS", "海关总署", "http://english.customs.gov.cn/statics/report/monthly.html", ("customs.gov.cn",)),
    ("CR.SRC.MOFCOM", "商务部", "https://www.mofcom.gov.cn/syxwfb/", ("mofcom.gov.cn",)),
    ("CR.SRC.MIIT", "工业和信息化部", "https://www.miit.gov.cn/gxsj/tjfx/xfpgy/index.html", ("miit.gov.cn",)),
    ("CR.SRC.MCT", "文化和旅游部", "https://zwgk.mct.gov.cn/zfxxgkml/tjxx/", ("mct.gov.cn",)),
)
POLICY_RELEVANT = re.compile(
    r"消费|内需|零售|居民价格|消费价格|食品|餐饮|酒|乳品|家电|汽车|以旧换新|旅游|酒店|电商|平台经济|市场监管|"
    r"产品质量|国家标准|广告|反垄断|消费者权益|召回|化妆品|服装|纺织|宠物|服务业"
)
INDUSTRY_RELEVANT = re.compile(
    r"进出口|进口|出口|Imports?|Exports?|Commodit|消费品|零售|消费市场|国内贸易|服务消费|食品|饮料|酒|乳品|家电|轻工|纺织|"
    r"服装|家具|化妆品|汽车|旅游|出游|旅行社|酒店|住宿|文化产业|演出|票房|文旅|统计数据|运行情况"
)


def auto_tag(title: str, summary: str, event_type: str, sector_code: str | None) -> dict:
    text = f"{title} {summary}"
    module = None
    for pattern, mod in MODULE_RULES:
        if re.search(pattern, text):
            module = mod
            break
    if module is None and event_type == "market_move":
        module = "market"
    tone = "neutral"
    if BULLISH_HINT.search(text) and not BEARISH_HINT.search(text):
        tone = "bullish"
    elif BEARISH_HINT.search(text) and not BULLISH_HINT.search(text):
        tone = "bearish"
    return {"module": module, "tone": tone, "so_what": None}


def parse_news_items(raw: str) -> list[dict]:
    """从资讯舆情库 results 列表中拆出单条新闻（报告标题/撰写时间/来源）。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw).get("results", [])
        except json.JSONDecodeError:
            raw = []
    items = []
    for r in raw:
        md = r.get("table_markdown", "") if isinstance(r, dict) else ""
        m = re.search(
            r"报告标题：(?P<title>[^；\n]{6,120})；\s*撰写时间：(?P<time>20\d{2}-\d{2}-\d{2}(?: \d{2}:\d{2}:?\d{0,2})?)；\s*新闻舆情来源：(?P<src>[^；\n]{2,30})",
            md,
        )
        if m:
            body = ""
            bm = re.search(r"原文：(?P<body>.{40,260})", md, re.S)
            if bm:
                body = re.sub(r"\s+", "", bm.group("body"))
            items.append({"title": m.group("title").strip(), "time": m.group("time").strip(), "src": m.group("src").strip(), "body": body})
    return items


def parse_research_reports(results: list[dict]) -> list[dict]:
    """解析聚源研报库返回的键值文本，保留元数据、摘要、风险和库内定位。"""
    reports = []
    for result in results:
        if not isinstance(result, dict):
            continue
        text = str(result.get("table_markdown") or "")

        def field(label: str) -> str | None:
            match = re.search(rf"(?:^|[\r\n]){re.escape(label)}[：:]\s*([^；;\r\n]+)", text)
            return match.group(1).strip() if match else None

        title = field("报告标题") or str(result.get("title") or "").strip()
        published_at = field("发布时间") or field("撰写时间")
        institution = field("撰写机构")
        industry = field("行业")
        authors = field("作者")
        rating = field("投资评级")
        if not title or not published_at:
            continue
        body_match = re.search(r"(?:^|[\r\n])原文[：:]\s*(.+)", text, re.S)
        body = re.sub(r"\s+", " ", body_match.group(1)).strip() if body_match else ""
        risk_match = re.search(r"风险提示[：:]\s*(.+?)(?:\r?\n|$)", text)
        original_url_match = re.search(r"https?://[^\s；;]+", text)
        reports.append({
            "title": title[:180], "published_at": published_at[:10],
            "institution": institution, "industry": industry, "authors": authors,
            "rating": rating, "excerpt": body[:4000],
            "summary": body[:900] or title,
            "risk": risk_match.group(1).strip()[:800] if risk_match else None,
            "original_url": original_url_match.group(0) if original_url_match else None,
            "score": float(result.get("score") or 0),
        })
    return reports


def report_sector_code(text: str) -> str | None:
    rules = (
        (r"食品|饮料|白酒|乳品|啤酒|调味|零食|酵母", "CR.S.FB"),
        (r"美妆|护肤|化妆品|个护", "CR.S.PH"),
        (r"宠物|母婴|潮玩|玩具", "CR.S.PT"),
        (r"汽车|新能源车|出行", "CR.D.AU"),
        (r"服装|纺织|鞋|运动户外", "CR.D.AF"),
        (r"家电|空调|冰箱|洗衣机|消费电子", "CR.D.AP"),
        (r"家居|家具|建材", "CR.D.HL"),
        (r"游戏|影视|教育|文化娱乐", "CR.V.CE"),
        (r"零售|电商|商超|百货|珠宝", "CR.V.RT"),
        (r"餐饮|茶饮|咖啡|本地生活", "CR.V.FS"),
        (r"酒店|旅游|景区|社服", "CR.V.TL"),
    )
    for pattern, code in rules:
        if re.search(pattern, text):
            return code
    return None


def parse_enterprise_risks(results: list[dict], company: str, sector_code: str,
                           start_date: str, end_date: str) -> list[dict]:
    """将企业风险表格转为统一事件；旧记录即使被接口返回也不会混入当日增量。"""
    events = []
    for result in results:
        if not isinstance(result, dict):
            continue
        api_name = str(result.get("api_name") or result.get("title") or "其他企业风险").strip()
        kind = RISK_API_KIND.get(api_name, "其他企业风险")
        lines = [line.strip() for line in str(result.get("table_markdown") or "").splitlines() if line.strip().startswith("|")]
        if len(lines) < 3:
            continue
        headers = [cell.strip() for cell in lines[0].strip("|").split("|")]
        for line in lines[2:]:
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if not cells or any("暂无数据" in cell for cell in cells):
                continue
            row = dict(zip(headers, cells))
            joined = "；".join(f"{key}：{value}" for key, value in row.items() if value and value != "--")
            dates = re.findall(r"20\d{2}-\d{2}-\d{2}", joined)
            event_date = next((d for d in dates if start_date <= d <= end_date), None)
            if not event_date:
                continue
            role = "subject"
            if re.search(r"原告|申请人|权利人", joined) and not re.search(r"被告|被执行人|被处罚|失信|限制高消费", joined):
                role = "claimant"
            severity = 0.72 if re.search(r"失信|冻结|处罚|严重违法|限制高消费|被执行人", kind + joined) else 0.58
            if role == "claimant":
                severity = min(severity, 0.48)
            fingerprint = re.search(r"(?:案号|文号|裁定书文号)[：:]?([^；|]+)", joined)
            identity = fingerprint.group(1).strip() if fingerprint else joined[:240]
            event_id = mon.stable_id("me", "CR.SRC.GILDATA.ENTERPRISE", company, kind, event_date, identity)
            summary = joined[:900]
            events.append({
                "monitor_event_id": event_id,
                "source_id": "CR.SRC.GILDATA.ENTERPRISE", "event_type": "enterprise_risk",
                "event_time": f"{event_date}T08:00:00+08:00", "available_at": datetime.now(BJ).isoformat(),
                "sector_code": sector_code, "title": f"{company}：{kind}新增记录",
                "summary": summary, "materiality_score": severity,
                "locator": f"聚源工商与企业风险库 · {company} · {api_name} · {event_date}",
                "license_status": LIC, "no_fund_holdings_or_positions": True,
                "enterprise_risk": {"legal_name": company, "risk_type": kind, "api_name": api_name,
                                    "event_role": role, "event_date": event_date, "fields": row},
                "research_layer": {"module": report_sector_code(company), "tone": "bearish" if role == "subject" else "neutral",
                                   "so_what": summary[:180], "abstract": summary[:700]},
            })
    return events


def fetch_enterprise_risks(company: str, sector_code: str, start_date: str, end_date: str) -> list[dict]:
    query = (
        f"查询{company}在{start_date}至{end_date}新增的行政处罚、监管处罚、法院立案、开庭公告、裁判文书、"
        "法院公告、股权质押、股权冻结、经营异常、严重违法、失信被执行人、被执行人、限制高消费、"
        "欠税及其他企业风险；返回事件日期、主体身份、案号或文号、摘要与来源。"
    )
    return parse_enterprise_risks(mcp_results("IcEnterpriseDataQuery", query), company, sector_code, start_date, end_date)


def fetch_html(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        "Referer": f"{urlparse(url).scheme}://{urlparse(url).netloc}/",
    })
    try:
        response = urllib.request.urlopen(req, timeout=timeout)
    except (urllib.error.URLError, ssl.SSLError):
        # 海关公开站点在部分 Windows Python TLS 栈上返回不完整证书链/EOF。
        # 仅对白名单 customs.gov.cn 做无凭据只读兼容重试，后续仍执行严格域名白名单校验。
        host = (urlparse(url).hostname or "").lower()
        if not (host == "customs.gov.cn" or host.endswith(".customs.gov.cn")):
            raise
        response = urllib.request.urlopen(req, timeout=timeout, context=ssl._create_unverified_context())
    with response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read(3_000_000).decode(charset, errors="replace")


def parse_official_policy_list(source_id: str, source_name: str, list_url: str,
                               allowed_hosts: tuple[str, ...], start_date: str, end_date: str,
                               relevant: re.Pattern = POLICY_RELEVANT,
                               event_type: str = "policy_release") -> list[dict]:
    """从官方列表页提取政策链接；仅接受官方域名、明确发布日期和消费相关标题。"""
    page = fetch_html(list_url)
    candidates = []
    anchor_re = re.compile(r'<a\b[^>]*href=["\'](?P<href>[^"\']+)["\'][^>]*>(?P<title>.*?)</a>(?P<tail>.{0,240})', re.I | re.S)
    for match in anchor_re.finditer(page):
        title = html.unescape(re.sub(r"<[^>]+>", "", match.group("title")))
        title = re.sub(r"\s+", " ", title).strip()
        if len(title) < 6 or not relevant.search(title):
            continue
        href = urljoin(list_url, html.unescape(match.group("href")))
        host = (urlparse(href).hostname or "").lower()
        if not any(host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts):
            continue
        nearby = page[max(0, match.start() - 500):min(len(page), match.end() + 700)]
        date_match = re.search(r"(20\d{2})[年/.-](\d{1,2})[月/.-](\d{1,2})", re.sub(r"<[^>]+>", " ", nearby))
        if not date_match:
            # 部分官网把完整发布日期编码在链接路径中（如 t20260817_...）。
            compact_date = re.search(r"(?<!\d)(20\d{6})(?!\d)", href)
            if not compact_date:
                continue
            raw_date = compact_date.group(1)
            published = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        else:
            try:
                published = f"{int(date_match.group(1)):04d}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"
            except (TypeError, ValueError):
                continue
        if not (start_date <= published <= end_date):
            continue
        event_id = mon.stable_id("me", source_id, href, published)
        candidates.append({
            "monitor_event_id": event_id, "source_id": source_id,
            "event_type": event_type, "event_time": f"{published}T08:00:00+08:00",
            "available_at": datetime.now(BJ).isoformat(), "title": title[:180],
            "summary": f"{source_name}官方发布：{title}。请点击官方原文核验文件正文、发文机关、文号和生效口径。",
            "materiality_score": 0.72, "source_url": href,
            "locator": f"{source_name}官方文件 · {published}",
            "license_status": "public_government_information", "no_fund_holdings_or_positions": True,
            "official_release": {"publisher": source_name, "published_at": published,
                                 "official_url": href, "list_url": list_url, "verified_host": host,
                                 "release_class": "policy" if event_type == "policy_release" else "industry_data"},
            "research_layer": {"module": "policy" if event_type == "policy_release" else auto_tag(title, "", event_type, None).get("module"),
                               "tone": "neutral", "so_what": title, "abstract": title},
        })
    unique = {event["monitor_event_id"]: event for event in candidates}
    return list(unique.values())


def parse_time_fuzzy(value: str) -> str:
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=BJ).isoformat()
        except ValueError:
            continue
    return datetime.now(BJ).isoformat()


def ingest_events(events: list[dict], cutoff: str) -> int:
    count = 0
    tmp_dir = Path(tempfile.mkdtemp(prefix="daily-sync-"))
    for i, ev in enumerate(events):
        p = tmp_dir / f"ev-{i}.json"
        p.write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            result = mon.ingest_event(DB, p, cutoff)
            if result["status"] == "accepted":
                count += 1
        except mon.MonitorValidationError as exc:
            log(f"  事件校验未过: {ev['title'][:30]} -> {exc}")
    return count


def update_cursor(db: sqlite3.Connection, source: str, stream: str, cursor: str, watermark: str, now_utc: str) -> None:
    db.execute(
        """UPDATE source_cursors SET cursor_value=?, watermark_available_at=?, last_success_at=?, status='success'
           WHERE source_id=? AND stream_name=?""",
        (cursor, watermark, now_utc, source, stream),
    )


def refresh_retail_sales(db: sqlite3.Connection, now_utc: str) -> None:
    """宏观时新性：核验社零当月值是否有比库内更新的月度，若有则补入观测值。"""
    latest = db.execute(
        "SELECT MAX(period_end) FROM observations WHERE metric_id='CR.MAC.RETAIL_SALES'"
    ).fetchone()[0]
    results = mcp_results("MacroIndustryData", "中国社会消费品零售总额当月值最新月度数据")
    rows = []
    for r in results:
        for line in str(r.get("table_markdown", "")).splitlines():
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 6 and cells[0] == "110003911":
                try:
                    rows.append((cells[5], float(cells[4])))
                except ValueError:
                    continue
    newer = [(d, v) for d, v in rows if d > (latest or "")]
    if not newer:
        log(f"  社零观测已是最新（库内至 {latest}）")
        return
    ev_id = "ev:gildata:retail-sales:2026-08-14"
    for period_end, value_yi in sorted(newer):
        y, m = int(period_end[:4]), int(period_end[5:7])
        db.execute(
            """INSERT OR IGNORE INTO observations(observation_id,metric_id,entity_id,security_id,value_numeric,
               value_text,unit,period_start,period_end,as_of_date,observed_at,published_at,available_at,ingested_at,
               source_id,evidence_id,value_status,statement_scope,consolidation_scope,accounting_standard,currency,
               scale,restatement_status,fiscal_period_type,version_no,is_current,supersedes_observation_id,
               quality_status,attributes_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"obs:cn:retail-sales:{y}-{m:02d}", "CR.MAC.RETAIL_SALES", "cr:geography:cn", None, value_yi * 1e8,
             None, "CNY", f"{y}-{m:02d}-01", period_end, period_end, None, now_utc, now_utc, now_utc,
             "CR.SRC.GILDATA.MACRO_INDUSTRY", ev_id, "reported", None, None, None, "CNY", 1.0, "original",
             "monthly", 1, 1, None, "curated",
             json.dumps({"as_published_unit": "亿元", "as_published_value": value_yi,
                         "official_origin": "国家统计局", "gildata_code": "110003911",
                         "conservative_availability": "以聚源拉取时间作为可得时间"}, ensure_ascii=False)),
        )
        log(f"  社零新月度入库: {period_end} = {value_yi:,.1f} 亿元")


def main() -> int:
    now_bj = datetime.now(BJ)
    now_utc = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    today = now_bj.strftime("%Y-%m-%d")
    yesterday = (now_bj - timedelta(days=1)).strftime("%Y-%m-%d")
    cutoff = now_bj.isoformat()
    log(f"===== 每日晨报同步开始（{today}）=====")
    events: list[dict] = []
    research_fetch_ok = False
    research_latest_date = None
    enterprise_fetch_ok = False
    official_policy_success: dict[str, int] = {}
    official_industry_success: dict[str, int] = {}

    try:
        raw = mcp_results("NewsDataQuery", f"{today}消费行业重要新闻、政策与市场动态")
        items = [it for it in parse_news_items(raw) if CONSUMER_RELEVANT.search(it["title"] + it.get("body", ""))][:4]
        for it in items:
            ts = parse_time_fuzzy(it["time"])
            tag = auto_tag(it["title"], "", "news_lead", None)
            abstract = f"{it['time']}，{it['src']}报道：{it['body'] or it['title']}。"[:240]
            events.append({
                "source_id": "CR.SRC.GILDATA.NEWS", "event_type": "news_lead",
                "event_time": ts, "available_at": ts,
                "title": it["title"][:110],
                "summary": abstract,
                "materiality_score": 0.5,
                "locator": f"聚源资讯舆情库 {today} · {it['src']}",
                "license_status": LIC, "no_fund_holdings_or_positions": True,
                "research_layer": {**tag, "abstract": abstract},
            })
        log(f"1) 消费新闻: 解析 {len(items)} 条")
    except Exception as e:
        log(f"1) 消费新闻失败: {type(e).__name__} {e}")

    try:
        research_results = mcp_results(
            "FinancialResearchReport",
            f"查询{yesterday}至{today}发布的消费行业最新券商研报，覆盖食品饮料、白酒、家电、汽车、零售电商、纺织服饰、酒店旅游、文化娱乐、美容个护、宠物和餐饮；返回标题、机构、发布日期、行业、评级、核心观点与风险提示，最多20篇。",
        )
        reports = parse_research_reports(research_results)
        reports = [r for r in reports if CONSUMER_RELEVANT.search(
            f"{r['title']} {r.get('industry') or ''} {r.get('summary') or ''}"
        )][:4]
        for report in reports:
            published = report["published_at"]
            available_at = now_bj.isoformat()
            event_id = mon.stable_id("me", "CR.SRC.GILDATA.RESEARCH", report["title"], published, report.get("institution") or "")
            sector_code = report_sector_code(f"{report['title']} {report.get('industry') or ''}")
            abstract = report["summary"][:700]
            events.append({
                "monitor_event_id": event_id,
                "source_id": "CR.SRC.GILDATA.RESEARCH", "event_type": "research_report",
                "event_time": f"{published}T08:00:00+08:00", "available_at": available_at,
                "sector_code": sector_code, "title": report["title"], "summary": abstract,
                "materiality_score": max(0.55, min(0.85, report["score"])),
                "source_url": f"/api/research-reports/{event_id}/content",
                "locator": f"聚源研报库 · {report.get('institution') or '机构未标注'} · {published}",
                "license_status": LIC, "no_fund_holdings_or_positions": True,
                "research_report": report,
                "research_layer": {
                    "module": auto_tag(report["title"], abstract, "research_report", sector_code).get("module"),
                    "tone": "neutral", "so_what": abstract[:180], "abstract": abstract,
                },
            })
        research_fetch_ok = True
        research_latest_date = max((r["published_at"] for r in reports), default=today)
        log(f"1b) 消费研报: 聚源返回 {len(research_results)} 组，筛选入库候选 {len(reports)} 篇")
    except Exception as e:
        log(f"1b) 消费研报失败: {type(e).__name__} {e}")

    try:
        enterprise_events = []
        # 企业接口为网络慢请求；限制 3 并发，单主体失败不影响其余主体和晨报主链路。
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(fetch_enterprise_risks, company, sector, yesterday, today)
                       for company, sector in ENTERPRISE_RISK_ENTITIES]
            for (company, _), future in zip(ENTERPRISE_RISK_ENTITIES, futures):
                try:
                    enterprise_events.extend(future.result(timeout=120))
                except Exception as exc:
                    log(f"  企业风险主体失败: {company} -> {type(exc).__name__} {exc}")
        events.extend(enterprise_events)
        enterprise_fetch_ok = True
        log(f"1c) 企业风险: 核验 {len(ENTERPRISE_RISK_ENTITIES)} 个法律主体，新增候选 {len(enterprise_events)} 条")
    except Exception as e:
        log(f"1c) 企业风险失败: {type(e).__name__} {e}")

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [(source_id, source_name, pool.submit(
                parse_official_policy_list, source_id, source_name, list_url, allowed_hosts, yesterday, today
            )) for source_id, source_name, list_url, allowed_hosts in OFFICIAL_POLICY_SOURCES]
            for source_id, source_name, future in futures:
                try:
                    found = future.result(timeout=45)
                    events.extend(found)
                    official_policy_success[source_id] = len(found)
                    log(f"  官方政策源成功: {source_name}，消费相关新增 {len(found)} 条")
                except Exception as exc:
                    log(f"  官方政策源失败: {source_name} -> {type(exc).__name__} {exc}")
        log(f"1d) 官方政策: {len(official_policy_success)}/{len(OFFICIAL_POLICY_SOURCES)} 个来源直连成功")
    except Exception as e:
        log(f"1d) 官方政策失败: {type(e).__name__} {e}")

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [(source_id, source_name, pool.submit(
                parse_official_policy_list, source_id, source_name, list_url, allowed_hosts,
                yesterday, today, INDUSTRY_RELEVANT, "industry_data_release"
            )) for source_id, source_name, list_url, allowed_hosts in OFFICIAL_INDUSTRY_SOURCES]
            for source_id, source_name, future in futures:
                try:
                    found = future.result(timeout=45)
                    events.extend(found)
                    official_industry_success[source_id] = len(found)
                    log(f"  官方行业源成功: {source_name}，消费相关新增 {len(found)} 条")
                except Exception as exc:
                    log(f"  官方行业源失败: {source_name} -> {type(exc).__name__} {exc}")
        log(f"1e) 官方行业数据: {len(official_industry_success)}/{len(OFFICIAL_INDUSTRY_SOURCES)} 个来源直连成功")
    except Exception as e:
        log(f"1e) 官方行业数据失败: {type(e).__name__} {e}")

    try:
        candidates = []
        for attempt_query in (
            f"{now_bj.year}年{now_bj.month}月飞天茅台批价最新行情、五粮液批价、今日酒价",
            f"今日酒价 {today} 各大名酒批发参考价",
        ):
            results = mcp_results("NewsDataQuery", attempt_query)
            candidates = [r for r in results if "今日酒价" in r.get("table_markdown", "")]
            if candidates:
                break
        todays = [r for r in candidates if today in r.get("table_markdown", "")]
        chosen = todays[0] if todays else (candidates[0] if candidates else {})
        raw = chosen.get("table_markdown", "")
        m = re.search(r"26年飞天\(原\)\s+\S+/500ml\s+(\d{3,4})\s+(\d{3,4})", raw)
        m2 = re.search(r"26年飞天\(散\)\s+\S+/500ml\s+(\d{3,4})\s+(\d{3,4})", raw)
        w = re.search(r"普五\(八代\)\s+\S+/500ml\s+(\d{3,4})\s+(\d{3,4})", raw)
        if m and m2:
            summary = (
                f"今日酒价 {today}：26年飞天(原) {m.group(2)}元（昨日{m.group(1)}）、"
                f"26年飞天(散) {m2.group(2)}元（昨日{m2.group(1)}）"
                + (f"；普五(八代) {w.group(2)}元（昨日{w.group(1)}）" if w else "")
            )
            events.append({
                "source_id": "CR.SRC.GILDATA.NEWS", "event_type": "industry_data_release", "sector_code": "CR.S.FB",
                "event_time": now_bj.replace(hour=9, minute=45, second=0, microsecond=0).isoformat(),
                "available_at": now_bj.isoformat(),
                "title": f"名酒批价日报（今日酒价 {today}）：飞天散瓶{m2.group(2)}元",
                "summary": summary, "materiality_score": 0.62,
                "locator": f"今日酒价 {today} 日报（聚源资讯舆情库转发）",
                "license_status": LIC, "no_fund_holdings_or_positions": True,
                "research_layer": {"module": "baijiu", "tone": "neutral", "so_what": None},
            })
            log(f"2) 批价: {summary[:60]}")
        else:
            log("2) 批价: 未解析到飞天价格，跳过")
    except Exception as e:
        log(f"2) 批价失败: {type(e).__name__} {e}")

    try:
        raw = mcp_call(
            "FinQuery",
            f"查询上证指数、深证成指、创业板指、沪深300、800消费指数、申万食品饮料指数、申万家用电器指数{yesterday}的收盘点位与涨跌幅，以及{yesterday}沪深两市总成交额",
        )
        events.append({
            "source_id": "CR.SRC.GILDATA.FINQUERY", "event_type": "market_move",
            "event_time": f"{yesterday}T15:00:00+08:00", "available_at": now_bj.isoformat(),
            "title": f"消费板块收盘快照（{yesterday}）",
            "summary": f"聚源指数日行情 {yesterday}：{raw[:600]}",
            "materiality_score": 0.5,
            "locator": f"聚源指数日行情 {yesterday} 收盘",
            "license_status": LIC, "no_fund_holdings_or_positions": True,
            "research_layer": {"module": "market", "tone": "neutral", "so_what": None},
        })
        log("3) 行情快照已生成")
    except Exception as e:
        log(f"3) 行情失败: {type(e).__name__} {e}")

    # 网络采集可能跨越数分钟；以采集完成时作为本批校验截止，避免合法返回被误判为未来数据。
    cutoff = datetime.now(BJ).isoformat()
    n = ingest_events(events, cutoff)
    log(f"4) 事件进库: {n}/{len(events)}")

    watermark = (now_bj - timedelta(days=1)).strftime("%Y-%m-%dT15:59:59.000Z")
    db = sqlite3.connect(DB)
    for src, stream, cur in [
        ("CR.SRC.GILDATA.NEWS", "news_leads", f"gildata:news:{today}"),
        ("CR.SRC.GILDATA.FINQUERY", "market_daily", f"gildata:market:{yesterday}-close"),
        ("CR.SRC.GILDATA.MACRO_INDUSTRY", "macro", f"gildata:macro:{today}-verified"),
    ]:
        update_cursor(db, src, stream, cur, watermark, now_utc)
    if research_fetch_ok:
        update_cursor(
            db, "CR.SRC.GILDATA.RESEARCH", "research_metadata",
            f"gildata:research:{research_latest_date}", now_utc, now_utc,
        )
    if enterprise_fetch_ok:
        update_cursor(
            db, "CR.SRC.GILDATA.ENTERPRISE", "enterprise_risk",
            f"gildata:enterprise-risk:{today}", now_utc, now_utc,
        )
    for source_id, count in official_policy_success.items():
        update_cursor(
            db, source_id, "official_policy_documents",
            f"official-policy:{today}:items={count}", now_utc, now_utc,
        )
    for source_id, count in official_industry_success.items():
        update_cursor(
            db, source_id, "official_industry_releases",
            f"official-industry:{today}:items={count}", now_utc, now_utc,
        )
    try:
        refresh_retail_sales(db, now_utc)
    except Exception as e:
        log(f"  社零核验失败: {type(e).__name__} {e}")
    db.commit()
    db.close()
    log("5) 游标已更新")

    try:
        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "tools" / "consumer_realtime_monitor.py"),
             "--db", str(DB), "run", "--cutoff", cutoff, "--mode", "scheduled"],
            check=True, capture_output=True, timeout=300,
        )
        log("6) 监控重算完成")
    except Exception as e:
        log(f"6) 监控重算失败: {type(e).__name__} {e}")

    # 股票评级是慢网络任务，与晨报撰写并行；评级限流不再阻塞晨报先落库。
    executor = ThreadPoolExecutor(max_workers=1) if now_bj.weekday() < 5 else None
    rating_future = executor.submit(stock_focus.run) if executor else None
    if rating_future:
        log("7) 股票评级已在后台并行启动（支持断点续跑）")
    else:
        log("7) 非交易日，跳过股票评级（沿用最近交易日评级）")
    try:
        result = brief_writer.run()
        log(f"8) 晨报文案撰写完成（不等待评级）: {result['date']} {result['sections']}")
    except Exception as e:
        log(f"8) 晨报文案撰写失败: {type(e).__name__} {e}")
    if rating_future:
        try:
            result = rating_future.result()
            log(f"8b) 股票评级完成: 批次 {result['date']}，行情截至 {result['market_date']}，{result['count']} 只，分布 {result['tiers']}")
        except Exception as e:
            log(f"8b) 股票评级失败（断点已保留）: {type(e).__name__} {e}")
        finally:
            executor.shutdown(wait=True)

    # 9) 轻量化：30 天滚动清理（晨报各模块档案含研报库，超出窗口的每日删除）
    try:
        horizon = (now_bj.date() - timedelta(days=30)).isoformat()
        with sqlite3.connect(DB) as conn:
            purged = conn.execute(
                "DELETE FROM daily_brief_sections WHERE brief_date < ?", (horizon,)
            ).rowcount
            conn.commit()
        log(f"9) 30 天滚动清理完成：删除 {horizon} 之前晨报档案 {purged} 行")
    except Exception as e:
        log(f"9) 30 天滚动清理失败: {type(e).__name__} {e}")

    log("===== 每日晨报同步结束 =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
