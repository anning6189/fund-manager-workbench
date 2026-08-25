# -*- coding: utf-8 -*-
# 抓核心撰写的原始输出，定位连续不达标原因
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(r"C:\Users\chi\Documents\ChatGPT\New project\tools")))
from consumer_brief_writer import build_context, LLM_URL, parse_sections, parse_ok

BJ = timezone(timedelta(hours=8))
now = datetime.now(BJ)
db = __import__("sqlite3").connect(r"C:\Users\chi\Documents\ChatGPT\New project\data\curated\consumer-research.db")
context, n = build_context(db, now)
weekday_cn = "一二三四五六日"[now.weekday()]
prompt = (
    "你是服务公募基金经理的资深消费行业研究员，正在撰写每日消费行研晨报的核心文案。"
    f"今天是 {now.strftime('%Y-%m-%d')} 星期{weekday_cn}，为交易日，行情为当日或最近收盘。"
    "严格基于所给研究底座内容撰写，禁止编造数据；每条观点可直接溯源到所给事件或数据。\n\n"
    "请严格按以下分隔格式输出（不要输出任何其他标记或解释）：\n"
    "【大盘判断|neutral或bullish或bearish】150-200字\n"
    "【最重要边际|bullish或neutral】150-200字\n"
    "【交易提示|bullish或neutral】150-200字（开头注明研究观点非交易指令）\n"
    "【风险预警|risk】150-200字\n"
    "【宏观解读】100-150字\n"
    "【风格判断】80-100字\n"
    "【催化剂】60-80字（无则写：暂无重大催化剂）\n"
    "【风险1】不超过50字\n【风险2】不超过50字\n【风险3】不超过50字\n"
    "每条写足即停，不要超出字数，写完最后一条立即结束。"
)
messages = [{"role": "system", "content": prompt}, {"role": "user", "content": f"【研究底座】\n{context}"}]
body = json.dumps({"model": "kimi-k3", "messages": messages, "max_tokens": 3200, "stream": False}).encode("utf-8")
req = urllib.request.Request(LLM_URL, data=body, headers={"Content-Type": "application/json", "Authorization": "Bearer PROXY_MANAGED"})
with urllib.request.urlopen(req, timeout=120) as resp:
    data = json.loads(resp.read().decode("utf-8"))
text = data["choices"][0]["message"]["content"]
print("finish_reason:", data["choices"][0].get("finish_reason"))
print("输出长度:", len(text))
print("===== 前 600 字 =====")
print(text[:600])
print("===== 解析 =====")
p = parse_sections(text)
print("达标:", parse_ok(p))
for t in p["takeaway"]:
    print(f"  [{t['label']}] {len(t['text'])}字")
print("  宏观解读:", len(p["macro_read"]), "字")
