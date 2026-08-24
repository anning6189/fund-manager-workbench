-- CR.V.FS 餐饮与本地生活 板块映射修复 SQL
-- 步骤:
--   1. 把行业分类为餐饮/熟食的公司映射到 CR.V.FS
--   2. 把名称含"酒家"的也映射到 CR.V.FS
--   3. 从 CR.S.FB 移除广州酒家

-- 先把社会服务/酒店餐饮/餐饮 的公司标记为 CR.V.FS
UPDATE research_universe_members
SET sector_code = 'CR.V.FS',
    mapping_status = 'mapped',
    mapping_confidence = 0.99
WHERE vendor_industry_l1 = '社会服务'
  AND vendor_industry_l2 = '酒店餐饮'
  AND vendor_industry_l3 = '餐饮';

-- 把 食品饮料/休闲食品/熟食 的公司标记为 CR.V.FS
UPDATE research_universe_members
SET sector_code = 'CR.V.FS',
    mapping_status = 'mapped',
    mapping_confidence = 0.99
WHERE vendor_industry_l1 = '食品饮料'
  AND vendor_industry_l2 = '休闲食品'
  AND vendor_industry_l3 = '熟食';

-- 名称含"酒家"/"餐饮"/"咖啡"/"茶饮"/"快餐"/"卤味" 的强制覆盖
UPDATE research_universe_members
SET sector_code = 'CR.V.FS',
    mapping_status = 'mapped',
    mapping_confidence = 0.95
WHERE (security_name LIKE '%酒家%'
    OR security_name LIKE '%餐饮%'
    OR security_name LIKE '%咖啡%'
    OR security_name LIKE '%茶饮%'
    OR security_name LIKE '%快餐%'
    OR security_name LIKE '%卤味%')
  AND sector_code NOT IN ('CR.V.TL');

-- 验证结果
SELECT 'CR.V.FS 成员:' AS info;
SELECT security_name,
       vendor_industry_l1 || '/' || vendor_industry_l2 || '/' || vendor_industry_l3 AS 行业,
       mapping_confidence
FROM research_universe_members
WHERE sector_code = 'CR.V.FS'
ORDER BY security_name;

SELECT 'CR.V.FS 总数: ' || COUNT(*) AS result FROM research_universe_members WHERE sector_code = 'CR.V.FS';