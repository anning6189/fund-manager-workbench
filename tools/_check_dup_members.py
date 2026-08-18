import sqlite3

db = sqlite3.connect("data/curated/consumer-research.db")
db.row_factory = sqlite3.Row

print("== 同名股票出现在多个板块 ==")
rows = db.execute(
    """SELECT security_name, COUNT(DISTINCT sector_code) AS n, COUNT(*) AS rows_n,
              GROUP_CONCAT(security_id) AS ids, GROUP_CONCAT(sector_code) AS sectors
       FROM research_universe_members
       GROUP BY security_name
       HAVING COUNT(DISTINCT sector_code) > 1
       ORDER BY n DESC"""
).fetchall()
for r in rows:
    print(f"  {r['security_name']}: {r['n']}个板块 {r['rows_n']}行 | ids={r['ids']} | sectors={r['sectors']}")
print(f"共 {len(rows)} 个同名跨板块")

print("\n== 同一 security_id 重复行 ==")
dups = db.execute(
    """SELECT security_id, security_name, COUNT(*) AS n, GROUP_CONCAT(sector_code) AS sectors
       FROM research_universe_members GROUP BY security_id HAVING COUNT(*) > 1"""
).fetchall()
for r in dups:
    print(f"  {r['security_id']} {r['security_name']}: {r['n']}行 sectors={r['sectors']}")
print(f"共 {len(dups)} 个重复 security_id")

print("\n== 华天酒店明细 ==")
for r in db.execute(
    "SELECT security_id, security_code, security_name, sector_code, trading_status FROM research_universe_members WHERE security_name LIKE '%华天%'"
).fetchall():
    print(" ", dict(r))

print("\n== sector_code 分布 ==")
for r in db.execute(
    "SELECT sector_code, COUNT(*) AS n FROM research_universe_members GROUP BY sector_code ORDER BY n DESC"
).fetchall():
    print(f"  {r['sector_code']}: {r['n']}")
