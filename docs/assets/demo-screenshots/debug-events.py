# -*- coding: utf-8 -*-
# 抓大事件调用的原始输出
import importlib.util
from datetime import datetime as dt

spec = importlib.util.spec_from_file_location("w", r'tools/consumer_brief_writer.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
import json, urllib.request, sqlite3

now = dt.now(mod.BJ)
db = sqlite3.connect(mod.DB)
context, n = mod.build_context(db, now)
weekday_cn = "一二三四五六日"[now.weekday()]
prompt = (
    "你是服务公募基金经理的资深消费行业研究员。基于研究底座，选出今天最值得关注的内容并严格按分隔格式输出"
    f"（今天 {now.strftime('%Y-%m-%d')} 星期{weekday_cn}，交易日）。"
    "禁止编造；每条写足即停，写完立即结束，不要任何其他文字：\n"
    "【宏观政策1】一行标题（≤30字）\n100-160字：这条宏观/政策为何今天最值得关注\n"
    "【宏观政策2】一行标题\n100-160字正文\n"
    "【重点研报】一行标题（含机构名）\n100-160字：该研报核心观点与时效价值\n"
    "【行业事件1】一行标题\n100-160字：对消费行业的具体影响\n"
    "【行业事件2】一行标题\n100-160字正文\n"
    "格式补充：每条正文结束后另起一行，写“来源：素材中对应事件或文档的原标题”（必须逐字引用素材原标题；由数据直接得出而没有对应事件时写“来源：官方发布数据”）。"
)
messages = [{"role": "system", "content": prompt}, {"role": "user", "content": f"【研究底座】\n{context}"}]
body = json.dumps({"model": "kimi-k3", "messages": messages, "max_tokens": 7000, "stream": False}).encode("utf-8")
req = urllib.request.Request(mod.LLM_URL, data=body, headers={"Content-Type": "application/json", "Authorization": "Bearer PROXY_MANAGED"})
with urllib.request.urlopen(req, timeout=240) as resp:
    data = json.loads(resp.read().decode("utf-8"))
text = data["choices"][0]["message"]["content"]
print("finish_reason:", data["choices"][0].get("finish_reason"), "| 长度:", len(text))
print("===== 前 700 字 =====")
print(text[:700])
print("===== 解析 =====")
p = mod.parse_sections(text)
de = p["daily_events"]
print("宏观政策:", [x["title"][:24] for x in de["macro_policies"]])
print("行业事件:", [x["title"][:24] for x in de["industry_events"]])
print("达标:", mod.events_ok(de))
