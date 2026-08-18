[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Write-JsonFile {
    param([string]$Path, [object]$Value)
    $json = $Value | ConvertTo-Json -Depth 30
    [System.IO.File]::WriteAllText($Path, $json + [Environment]::NewLine, $utf8NoBom)
}

function New-Subcategory {
    param([string]$Code, [string]$Name)
    [ordered]@{
        code = $Code
        name = $Name
        level = 3
        status = 'active'
        valid_from = '2026-08-12'
        valid_to = $null
    }
}

$subcategoryCodes = @{
    'CR.S.FB' = @(
        @('BJ','白酒'), @('BE','啤酒及其他酒'), @('BV','软饮与包装水'), @('DA','乳品'),
        @('SN','休闲食品'), @('CO','调味品'), @('PF','速冻预制与餐饮供应链'), @('BA','烘焙'),
        @('ME','肉制品'), @('NU','保健营养食品')
    )
    'CR.S.PH' = @(
        @('SK','护肤'), @('MU','彩妆'), @('FR','香水'), @('HC','洗护'), @('OC','口腔护理'),
        @('PC','个护用品'), @('HH','家清'), @('TI','生活用纸')
    )
    'CR.S.PT' = @(
        @('PF','宠物食品'), @('PS','宠物用品'), @('PV','宠物医疗服务'), @('MB','母婴用品'),
        @('TY','玩具潮玩'), @('OT','其他家庭新消费')
    )
    'CR.D.AP' = @(
        @('WH','白色家电'), @('KA','厨房电器'), @('AV','黑色家电'), @('SA','小家电'),
        @('VC','清洁电器'), @('SH','智能家居'), @('CE','消费电子终端')
    )
    'CR.D.AU' = @(
        @('PV','乘用车'), @('MC','摩托车'), @('AD','汽车流通'), @('AM','汽车售后'), @('MS','出行消费服务')
    )
    'CR.D.HL' = @(
        @('FF','成品家具'), @('CU','定制家居'), @('UP','软体家居'), @('HR','家装服务'),
        @('BM','家居建材'), @('HD','家居流通')
    )
    'CR.D.AF' = @(
        @('CL','服装'), @('FW','鞋履'), @('SO','运动户外'), @('HT','家纺'), @('JE','珠宝首饰'), @('WE','可穿戴消费品')
    )
    'CR.V.RT' = @(
        @('SM','商超'), @('DS','百货'), @('SC','专业连锁'), @('DF','免税'), @('EC','电商平台'),
        @('IR','即时零售'), @('RS','零售服务')
    )
    'CR.V.FS' = @(
        @('FR','正餐'), @('QS','快餐'), @('BC','茶饮咖啡'), @('BS','烘焙门店'),
        @('FS','餐饮供应链'), @('LP','本地生活平台')
    )
    'CR.V.TL' = @(
        @('HO','酒店'), @('AT','景区'), @('TA','旅行社与OTA'), @('TP','主题公园及博彩'),
        @('SF','体育健身'), @('LS','休闲服务')
    )
    'CR.V.CE' = @(
        @('GM','游戏'), @('FC','影视院线'), @('MR','音乐阅读'), @('OE','线上娱乐'),
        @('NE','非学历教育'), @('OC','其他文化消费')
    )
}

$domainPath = Join-Path $projectRoot 'specs\consumer-domain-model.v1.json'
$domain = Get-Content -Raw -Encoding UTF8 $domainPath | ConvertFrom-Json
foreach ($group in $domain.taxonomy) {
    foreach ($sector in $group.children) {
        $definitions = $subcategoryCodes[$sector.code]
        if ($null -eq $definitions) { throw "No subcategory code map for $($sector.code)" }
        $sector.children = @($definitions | ForEach-Object { New-Subcategory -Code "$($sector.code).$($_[0])" -Name $_[1] })
    }
}
$domain.spec_version = '1.1.0'
$domain.status = 'production_spec_complete_data_population_incremental'
$domain.id_policy.taxonomy_code_pattern = 'CR.<group>.<sector>[.<subcategory>]'
$domain | Add-Member -Force NoteProperty taxonomy_version_policy ([ordered]@{
    current_version = '2026.08'
    stable_code_rule = '代码一经发布不可复用；更名不改代码；拆分与合并使用SUCCESSOR_OF或MERGED_INTO关系'
    required_node_fields = @('code','name','level','status','valid_from','valid_to')
    release_gate = '全树代码唯一、父子前缀一致、有效期不重叠、停用节点保留历史映射'
})
$domain | Add-Member -Force NoteProperty classification_assignment_schema ([ordered]@{
    required_fields = @('assignment_id','entity_id','classification_code','assignment_type','valid_from','valid_to','as_of_date','source_id','confidence','review_status')
    assignment_types = @('primary','secondary','business_exposure')
    confidence_range = '0_to_1'
    production_rule = 'primary每个时点最多一个；secondary与business_exposure可多选；无证据不得自动赋类'
})
Write-JsonFile -Path $domainPath -Value $domain

$metricPath = Join-Path $projectRoot 'specs\consumer-metric-dictionary.v1.json'
$metricSpec = Get-Content -Raw -Encoding UTF8 $metricPath | ConvertFrom-Json

$baseFinancialMetrics = @(
    [ordered]@{
        metric_id='CR.CO.OPERATING_COST'; name='营业成本'; definition='适用会计准则下与营业收入配比确认的营业成本';
        metric_type='observed'; formula='财务报表值'; unit='currency'; frequency='quarterly|annual';
        grain='legal_entity|issuer|segment|product|geography'; time_semantics='period'; preferred_source_tier='A';
        comparability='成本分类、并表范围、会计准则、币种、单位和重述版本一致'; missing_policy='null';
        validation='与营业收入、毛利及报表附注勾稽'
    },
    [ordered]@{
        metric_id='CR.CO.SELLING_EXPENSE'; name='销售费用'; definition='报告期间为销售商品或提供服务发生并计入损益的销售相关费用';
        metric_type='observed'; formula='财务报表值'; unit='currency'; frequency='quarterly|annual';
        grain='legal_entity|issuer'; time_semantics='period'; preferred_source_tier='A';
        comparability='平台佣金、广告、运输履约等费用分类及并表范围一致'; missing_policy='null';
        validation='与销售费用率复算并检查附注明细和重分类'
    },
    [ordered]@{
        metric_id='CR.CO.CFO_NET'; name='经营活动现金流量净额'; definition='报告期间经营活动现金流入减经营活动现金流出的净额';
        metric_type='observed'; formula='现金流量表值'; unit='currency'; frequency='quarterly|annual|ttm';
        grain='legal_entity|issuer'; time_semantics='period'; preferred_source_tier='A';
        comparability='并表范围、期间、币种、重述版本及现金流分类政策一致'; missing_policy='null';
        validation='与现金流量表经营活动分项及期初期末现金变动勾稽'
    },
    [ordered]@{
        metric_id='CR.CO.PARENT_NET_PROFIT'; name='归属于母公司股东的净利润'; definition='合并利润表中归属于母公司所有者的净利润';
        metric_type='observed'; formula='财务报表值'; unit='currency'; frequency='quarterly|annual|ttm';
        grain='issuer'; time_semantics='period'; preferred_source_tier='A';
        comparability='合并范围、持续经营、会计准则、币种、重述版本和一次性项目披露一致'; missing_policy='null';
        validation='与合并净利润及少数股东损益勾稽'
    }
)
$existingCommonMetricIds = @($metricSpec.common_metrics.metric_id)
foreach ($baseMetric in $baseFinancialMetrics) {
    if ($existingCommonMetricIds -notcontains $baseMetric.metric_id) {
        $metricSpec.common_metrics += [pscustomobject]$baseMetric
    }
}

$formulaMap = @{
    'CR.FB.VOLUME'='SUM(standardized_quantity) BY sell_stage'; 'CR.FB.UNIT_PRICE'='comparable_net_revenue / sales_volume';
    'CR.FB.WHOLESALE_PRICE'='MEDIAN(valid_channel_transaction_price)'; 'CR.FB.CHANNEL_INVENTORY'='sellable_channel_inventory / average_monthly_sell_through_volume_N';
    'CR.FB.CONTRACT_LIABILITY_RATIO'='ending_contract_liabilities / trailing_twelve_month_revenue'; 'CR.FB.RAW_MATERIAL_COST_INDEX'='SUM(material_weight_i * material_price_index_i)';
    'CR.PH.GMV'='SUM(paid_order_amount - cancellations - refunds) under declared subsidy_and_tax_policy'; 'CR.PH.TRAFFIC'='COUNT(DISTINCT valid_visitor_id)';
    'CR.PH.CONVERSION'='valid_purchasers / valid_traffic'; 'CR.PH.AOV'='net_transaction_value / valid_orders';
    'CR.PH.REPURCHASE'='repeat_buyers_within_window / eligible_buyers'; 'CR.PH.MARKETING_ROI'='attributable_incremental_gross_profit / attributable_marketing_spend';
    'CR.PH.HERO_SKU_SHARE'='hero_sku_net_sales / brand_net_sales'; 'CR.PT.HOUSEHOLD_PENETRATION'='purchasing_or_owning_households / target_households';
    'CR.PT.REPURCHASE'='repeat_buyers_within_window / eligible_buyers'; 'CR.PT.SPEND_PER_USER'='annual_net_sales / annual_active_buyers';
    'CR.PT.SKU_PRODUCTIVITY'='net_sales_or_gross_profit / active_sku_count'; 'CR.PT.CAC'='attributable_acquisition_spend / new_valid_customers';
    'CR.AP.SHIPMENT'='SUM(manufacturer_to_channel_standardized_units)'; 'CR.AP.RETAIL_VOLUME'='SUM(end_customer_sell_through_standardized_units)';
    'CR.AP.RETAIL_ASP'='end_customer_net_sales / retail_volume'; 'CR.AP.REPLACEMENT_RATE'='replacement_purchase_units / total_purchase_units';
    'CR.AP.CHANNEL_INVENTORY'='sellable_channel_inventory / average_sell_through_volume_N'; 'CR.AP.EXPORT_VOLUME'='SUM(customs_or_company_export_standardized_units)';
    'CR.AU.RETAIL_SALES'='SUM(registered_or_declared_retail_units)'; 'CR.AU.ORDER_BACKLOG'='COUNT(valid_undelivered_nonduplicate_orders at period_end)';
    'CR.AU.DISCOUNT_RATE'='1 - actual_transaction_price / valid_list_price'; 'CR.AU.DEALER_INVENTORY_COEF'='ending_dealer_inventory / current_month_retail_sales';
    'CR.AU.AFTERSALES_REVENUE'='repair_revenue + parts_revenue + service_revenue - refunds'; 'CR.HL.ORDER_INTAKE'='SUM(new_valid_order_amount - cancellations)';
    'CR.HL.ORDER_TO_REVENUE'='recognized_revenue_from_convertible_orders / convertible_orders'; 'CR.HL.BACKLOG'='SUM(valid_unrecognized_order_amount at period_end)';
    'CR.HL.STORE_COUNT'='COUNT(valid_operating_stores at period_end)'; 'CR.HL.SSS'='comparable_store_sales_current / comparable_store_sales_prior - 1';
    'CR.HL.DELIVERY_CYCLE'='MEDIAN(acceptance_timestamp - order_timestamp)'; 'CR.AF.SELL_THROUGH'='retail_units_sold / available_units_under_declared_replenishment_policy';
    'CR.AF.FULL_PRICE_SHARE'='full_price_net_sales / total_net_sales'; 'CR.AF.DISCOUNT_RATE'='actual_transaction_price / ticket_price';
    'CR.AF.INVENTORY_SALES_RATIO'='ending_sellable_inventory / average_retail_sales_N'; 'CR.AF.DTC_SHARE'='direct_to_consumer_net_revenue / total_net_revenue';
    'CR.RT.GMV'='SUM(paid_order_amount - cancellations - refunds) under declared tax_and_subsidy_policy'; 'CR.RT.TAKE_RATE'='platform_net_revenue / comparable_GMV';
    'CR.RT.TRAFFIC'='COUNT(DISTINCT valid_visit_or_store_entry)'; 'CR.RT.CONVERSION'='valid_transacting_customers / valid_traffic';
    'CR.RT.MEMBER_SHARE'='member_net_sales / total_net_sales'; 'CR.RT.SALES_PER_SQM'='comparable_store_net_sales / effective_operating_area';
    'CR.FS.SSS'='comparable_store_sales_current / comparable_store_sales_prior - 1'; 'CR.FS.TABLE_TURNOVER'='completed_table_services / available_tables / operating_days';
    'CR.FS.TRANSACTIONS'='COUNT(completed_nonrefunded_orders)'; 'CR.FS.AVERAGE_TICKET'='net_sales / valid_transactions_or_diners';
    'CR.FS.STORE_PAYBACK'='MIN(months where cumulative_store_cash_contribution >= initial_investment)'; 'CR.FS.CLOSURE_RATE'='closed_stores_excluding_relocation_and_renovation / beginning_stores';
    'CR.TL.TRIPS'='source_reported_weighted_trip_count'; 'CR.TL.SPEND_PER_TRIP'='total_travel_spend / trips';
    'CR.TL.OCCUPANCY'='sold_room_nights / available_room_nights'; 'CR.TL.ADR'='net_room_revenue / sold_room_nights';
    'CR.TL.REVPAR'='net_room_revenue / available_room_nights'; 'CR.TL.VISITOR_SPEND'='comparable_attraction_revenue / valid_visitors';
    'CR.CE.MAU'='COUNT(DISTINCT users_meeting_valid_activity_rule_in_calendar_month)'; 'CR.CE.PAYING_USERS'='COUNT(DISTINCT users_with_net_payment_in_period)';
    'CR.CE.PAY_RATE'='paying_users / MAU'; 'CR.CE.ARPPU'='related_net_revenue / average_paying_users';
    'CR.CE.ATTENDANCE'='COUNT(valid_admission_or_redeemed_ticket)'; 'CR.CE.CONTENT_PIPELINE'='COUNT(content_items) BY approval_and_release_status'
}

$unitMap = @{
    'VOLUME'='standardized_units'; 'UNIT_PRICE'='currency/unit'; 'WHOLESALE_PRICE'='currency/unit'; 'CHANNEL_INVENTORY'='months_or_weeks';
    'CONTRACT_LIABILITY_RATIO'='ratio'; 'RAW_MATERIAL_COST_INDEX'='index'; 'GMV'='currency'; 'TRAFFIC'='visits_or_users'; 'CONVERSION'='ratio';
    'AOV'='currency/order'; 'REPURCHASE'='ratio'; 'MARKETING_ROI'='ratio'; 'HERO_SKU_SHARE'='ratio'; 'HOUSEHOLD_PENETRATION'='ratio';
    'SPEND_PER_USER'='currency/user/year'; 'SKU_PRODUCTIVITY'='currency/SKU/period'; 'CAC'='currency/customer'; 'SHIPMENT'='standardized_units';
    'RETAIL_VOLUME'='standardized_units'; 'RETAIL_ASP'='currency/unit'; 'REPLACEMENT_RATE'='ratio'; 'EXPORT_VOLUME'='standardized_units';
    'RETAIL_SALES'='vehicles'; 'ORDER_BACKLOG'='orders_or_currency'; 'DISCOUNT_RATE'='ratio'; 'DEALER_INVENTORY_COEF'='months';
    'AFTERSALES_REVENUE'='currency'; 'ORDER_INTAKE'='currency'; 'ORDER_TO_REVENUE'='ratio'; 'BACKLOG'='currency'; 'STORE_COUNT'='stores';
    'SSS'='ratio'; 'DELIVERY_CYCLE'='days'; 'SELL_THROUGH'='ratio'; 'FULL_PRICE_SHARE'='ratio'; 'INVENTORY_SALES_RATIO'='months';
    'DTC_SHARE'='ratio'; 'TAKE_RATE'='ratio'; 'MEMBER_SHARE'='ratio'; 'SALES_PER_SQM'='currency/square_meter/period';
    'TABLE_TURNOVER'='turns/table/day'; 'TRANSACTIONS'='transactions'; 'AVERAGE_TICKET'='currency/transaction'; 'STORE_PAYBACK'='months';
    'CLOSURE_RATE'='ratio'; 'TRIPS'='person_trips'; 'SPEND_PER_TRIP'='currency/person_trip'; 'OCCUPANCY'='ratio'; 'ADR'='currency/room_night';
    'REVPAR'='currency/available_room_night'; 'VISITOR_SPEND'='currency/visitor'; 'MAU'='users'; 'PAYING_USERS'='users'; 'PAY_RATE'='ratio';
    'ARPPU'='currency/paying_user/period'; 'ATTENDANCE'='admissions'; 'CONTENT_PIPELINE'='content_items'
}

$sectorDefaults = @{
    'FB'=@('monthly|quarterly','company|brand|category|sku|channel|geography','A|B');
    'PH'=@('daily|weekly|monthly','company|brand|category|sku|channel|geography','B');
    'PT'=@('monthly|quarterly|annual','company|brand|category|channel|customer_segment|geography','B');
    'AP'=@('monthly|quarterly','company|brand|category|model|channel|geography','A|B');
    'AU'=@('weekly|monthly|quarterly','company|brand|model|channel|geography','A|B');
    'HL'=@('monthly|quarterly','company|brand|category|store|geography','A|B');
    'AF'=@('weekly|monthly|quarterly','company|brand|category|sku|channel|geography','A|B');
    'RT'=@('daily|weekly|monthly','company|platform|format|category|channel|geography','A|B');
    'FS'=@('daily|weekly|monthly','company|brand|store|format|geography','A|B');
    'TL'=@('monthly|quarterly','company|brand|property|attraction|geography','A|B');
    'CE'=@('monthly|quarterly','company|product|content|platform|geography','A|B')
}

$pointInTimeIds = @(
    'CR.FB.WHOLESALE_PRICE','CR.FB.CHANNEL_INVENTORY','CR.PT.HOUSEHOLD_PENETRATION','CR.AP.CHANNEL_INVENTORY',
    'CR.AU.ORDER_BACKLOG','CR.AU.DEALER_INVENTORY_COEF','CR.HL.BACKLOG','CR.HL.STORE_COUNT','CR.AF.INVENTORY_SALES_RATIO',
    'CR.CE.MAU','CR.CE.CONTENT_PIPELINE'
)

function Get-MetricSuffix([string]$MetricId) { ($MetricId -split '\.')[-1] }
function Get-MetricDomain([string]$MetricId) { ($MetricId -split '\.')[1] }

foreach ($pack in $metricSpec.sector_metric_packs) {
    $enriched = @()
    foreach ($metric in $pack.metrics) {
        if ($metric.PSObject.Properties.Name -contains 'metric_id') {
            $metricId = [string]$metric.metric_id
            $name = [string]$metric.name
            $definition = [string]$metric.definition
        } else {
            $metricId = [string]$metric[0]
            $name = [string]$metric[1]
            $definition = [string]$metric[2]
        }
        $suffix = Get-MetricSuffix $metricId
        $domainCode = Get-MetricDomain $metricId
        $defaults = $sectorDefaults[$domainCode]
        if (-not $formulaMap.ContainsKey($metricId)) { throw "Missing formula for $metricId" }
        if (-not $unitMap.ContainsKey($suffix)) { throw "Missing unit for $metricId" }
        $metricType = if ($formulaMap[$metricId] -match '^COUNT|^SUM|^MEDIAN|source_reported') { 'observed|derived' } else { 'derived' }
        $validation = if ($unitMap[$suffix] -eq 'ratio') {
            '检查分子分母同范围同期间；通常应在0至1，允许超界的业务情形必须解释并人工复核'
        } elseif ($unitMap[$suffix] -match 'currency') {
            '币种、含税口径和单位一致；与收入、销量或订单桥接；异常跳变触发复核'
        } else {
            '非负值、单位标准化、样本覆盖和跨期跳变检查；与相关收入或数量指标交叉验证'
        }
        $enriched += [ordered]@{
            metric_id = $metricId
            name = $name
            definition = $definition
            metric_type = $metricType
            formula = $formulaMap[$metricId]
            unit = $unitMap[$suffix]
            frequency = $defaults[0]
            grain = $defaults[1]
            time_semantics = $(if ($pointInTimeIds -contains $metricId) { 'point_in_time' } else { 'period' })
            preferred_source_tier = $defaults[2]
            comparability = "$definition；比较时必须保持实体层级、地域、渠道、观察窗口、样本和原始口径一致"
            missing_policy = '未知为null；仅在披露方法、输入证据、区间和estimated标记后允许估算'
            validation = $validation
        }
    }
    $pack.metrics = $enriched
}

$metricSpec.spec_version = '1.1.0'
$metricSpec.status = 'production_spec_complete_source_binding_incremental'
$metricSpec.global_rules.point_in_time_policy = '历史研究使用带时区cutoff_timestamp；published_at与available_at均不得晚于截止时点；未知时间按当日23:59:59保守归一化；截止日当天能否使用由availability_mode决定'
$metricSpec.global_rules | Add-Member -Force NoteProperty financial_statement_policy ([ordered]@{
    required_for_metric_prefix = 'CR.CO.'
    required_fields = @('statement_scope','consolidation_scope','accounting_standard','currency','scale','restatement_status','fiscal_period_type')
    statement_scope_values = @('income_statement','balance_sheet','cash_flow_statement','notes','derived_cross_statement')
    consolidation_scope_values = @('consolidated','parent_company','segment')
    restatement_status_values = @('original','restated','not_applicable')
    comparison_gate = '参与比较的原值必须使用兼容的并表范围、会计准则、币种/单位和重述版本；否则降级或阻断'
})
$metricSpec | Add-Member -Force NoteProperty observation_schema ([ordered]@{
    required_fields = @('observation_id','metric_id','entity_id','value','unit','period_start','period_end','as_of_date','published_at','available_at','source_id','evidence_id','value_status')
    conditional_requirements = @(
        [ordered]@{ when='metric_id starts_with CR.CO.'; require=@('statement_scope','consolidation_scope','accounting_standard','currency','scale','restatement_status','fiscal_period_type') },
        [ordered]@{ when='unit contains currency'; require=@('currency') },
        [ordered]@{ when='metric.time_semantics equals point_in_time'; require=@('observed_at') },
        [ordered]@{ when='value_status equals estimated'; require=@('estimation_method','estimation_interval','input_evidence_ids') }
    )
    value_status_values = @('reported','vendor_derived','agent_calculated','estimated','restated')
    uniqueness_key = @('metric_id','entity_id','period_start','period_end','as_of_date','source_id','restatement_status')
})
Write-JsonFile -Path $metricPath -Value $metricSpec

$evidencePath = Join-Path $projectRoot 'specs\research-evidence-standard.v1.json'
$evidence = Get-Content -Raw -Encoding UTF8 $evidencePath | ConvertFrom-Json
$evidence.spec_version = '1.1.0'
$evidence.status = 'production_spec_complete_connector_enforcement_required'
$evidence.evidence_required_fields = @(
    'evidence_id','source_id','source_type','title','publisher','published_at','available_at','as_of_date','retrieved_at',
    'locator','content_hash','license_tag','access_class','evidence_tier','support_type','document_version'
)
$evidence | Add-Member -Force NoteProperty point_in_time_control ([ordered]@{
    cutoff_field = 'cutoff_timestamp'
    cutoff_format = 'ISO-8601 timestamp with explicit UTC offset'
    default_timezone = 'Asia/Shanghai'
    availability_modes = [ordered]@{
        exact_timestamp = '只使用在cutoff_timestamp之前已经公开可得的信息'
        end_of_day = '日期型问题归一化为当地次日00:00:00的排他上界；当日未知具体时刻的材料按23:59:59保守处理'
        market_open = '仅使用当日开盘前已公开可得的信息；盘中及盘后材料排除'
        market_close = '仅使用当日收盘前已公开可得的信息；盘后材料排除'
    }
    eligibility_rule = 'published_at != null AND available_at != null AND published_at < cutoff_exclusive AND available_at < cutoff_exclusive'
    unknown_time_rule = '只有日期时按来源所在地当日23:59:59归一化并标记time_precision=date；未知日期直接阻断'
    later_retrieval_rule = 'retrieved_at可晚于截止时点，但必须证明document_version在截止时点已存在；可修订来源还需archived_snapshot_at或版本哈希'
    hard_block = 'future_information_leakage'
})
$evidence | Add-Member -Force NoteProperty connector_result_contract ([ordered]@{
    query_required_fields = @('cutoff_timestamp','availability_mode','allowed_entity_ids','allowed_security_ids','allowed_metric_ids','allowed_periods','allowed_statement_scopes')
    response_required_fields = @('connector_name','query_id','retrieved_at','raw_record_count','truncated')
    deterministic_post_filters = @('entity_or_security_whitelist','metric_whitelist','period_whitelist','statement_scope_whitelist','point_in_time_filter','evidence_completeness_filter')
    out_of_scope_action = 'discard_and_log'
    future_record_action = 'hard_block_entire_answer'
    truncation_action = 'mark_incomplete_and_retry_with_smaller_query_partition'
    empty_after_filter_action = 'return_no_eligible_data_not_unfiltered_fallback'
})
$evidence | Add-Member -Force NoteProperty comparative_conclusion_policy ([ordered]@{
    required_sections = @('dimension_results','supporting_evidence','counter_evidence','comparability_limits','cannot_conclude','pm_judgment_required')
    single_winner_allowed_when = @('explicit_decision_rule','explicit_weights','defined_time_horizon','all_material_dimensions_comparable')
    default_without_rule = '按维度给出有限结论，不合成为单一公司排名'
    prohibited_inference = @('future_stock_return','buy_or_sell_recommendation','portfolio_action','overall_superiority_from_single_period_subset')
})

$templateDefinitions = @(
    [ordered]@{
        template_id='P0_01_DAILY_SCAN'; name='每日信息扫描与晨报'; purpose='在明确时间窗内筛选消费行业新增信息并解释研究影响';
        required_input_fields=@('cutoff_timestamp','availability_mode','window_start','sector_codes','source_scope');
        required_output_fields=@('coverage_statement','material_events','data_changes','research_implications','counter_signals','source_gaps','evidence_index');
        allowed_content_labels=@('FACT_PRIMARY','FACT_SECONDARY','MANAGEMENT_STATEMENT','THIRD_PARTY_VIEW','AGENT_CALCULATION','AGENT_INFERENCE','UNVERIFIED_SIGNAL','PM_JUDGMENT_REQUIRED');
        evidence_requirements=@('每个事件逐项claim引用','关键事件至少一个A/B级直接证据','截止时点后信息硬阻断');
        validation_rules=@('覆盖区间完整','去重同一事件','无证据不进入material_events','不得出现基金持仓或交易建议')
    },
    [ordered]@{
        template_id='P0_02_POLICY_EVENT'; name='政策、监管与重大事件解读'; purpose='把政策或事件转化为产业链、需求、供给和公司影响路径';
        required_input_fields=@('cutoff_timestamp','event_id','affected_geographies','affected_sector_codes');
        required_output_fields=@('event_facts','effective_timeline','transmission_chain','affected_entities','base_case','alternative_explanations','monitoring_indicators','evidence_index');
        allowed_content_labels=@('FACT_PRIMARY','FACT_SECONDARY','AGENT_INFERENCE','SCENARIO_ASSUMPTION','PM_JUDGMENT_REQUIRED');
        evidence_requirements=@('政策原文或法定公告为首要证据','生效日和发布日期分离','推断不得伪装成事实');
        validation_rules=@('事件实体已消歧','适用范围明确','传导链每一跳可解释','包含反方情景和失效条件')
    },
    [ordered]@{
        template_id='P0_03_DATA_QUERY'; name='行业及公司数据资料查询'; purpose='返回口径明确、可复算、可追溯的数据答案';
        required_input_fields=@('cutoff_timestamp','entity_or_sector_scope','metric_ids','periods','statement_scope');
        required_output_fields=@('query_scope','metric_definitions','observations','calculations','comparability_notes','missing_data','evidence_index');
        allowed_content_labels=@('FACT_PRIMARY','FACT_SECONDARY','AGENT_CALCULATION','UNVERIFIED_SIGNAL');
        evidence_requirements=@('每个原值绑定evidence_id','计算保存公式和输入observation_id','历史查询通过时间准入门');
        validation_rules=@('公司与证券分离','指标和期间白名单','单位币种统一','报表范围明确','结果可复算')
    },
    [ordered]@{
        template_id='P0_04_COMPANY_COMPARISON'; name='消费公司横向比较'; purpose='按统一维度比较公司并给出有限、可反驳的结论';
        required_input_fields=@('cutoff_timestamp','company_ids','metric_ids','periods','decision_rule_or_null');
        required_output_fields=@('entity_mapping','dimension_results','metric_bridge','supporting_evidence','counter_evidence','comparability_limits','cannot_conclude','pm_judgment_required');
        allowed_content_labels=@('FACT_PRIMARY','FACT_SECONDARY','AGENT_CALCULATION','AGENT_INFERENCE','PM_JUDGMENT_REQUIRED');
        evidence_requirements=@('关键财务事实优先A类年报或公告','所有派生指标可复算','来源冲突显式呈现');
        validation_rules=@('同口径同期间','无决策规则不得输出单一赢家','至少一个反方解释','不得转化为买卖建议')
    },
    [ordered]@{
        template_id='P0_05_EARNINGS_EVENT_REVIEW'; name='财报、公告与重大公司事件点评'; purpose='区分披露事实、预期差、驱动因素和后续验证信号';
        required_input_fields=@('cutoff_timestamp','issuer_id','event_document_ids','comparison_periods');
        required_output_fields=@('reported_facts','changes_vs_prior','driver_bridge','cash_flow_quality','management_statements','surprises_vs_available_expectations','risks','next_validation_points','evidence_index');
        allowed_content_labels=@('FACT_PRIMARY','MANAGEMENT_STATEMENT','AGENT_CALCULATION','AGENT_INFERENCE','SCENARIO_ASSUMPTION','PM_JUDGMENT_REQUIRED');
        evidence_requirements=@('财报与法定公告为首要证据','管理层表述单独标记','预期数据必须为截止时点可得版本');
        validation_rules=@('并表范围和重述版本明确','一次性项目单列','利润与现金流勾稽','禁止用后见信息解释历史预期差')
    }
)
$evidence.p0_templates = $templateDefinitions
Write-JsonFile -Path $evidencePath -Value $evidence

Write-Output 'Stage 3/4 specifications upgraded to v1.1.0.'
