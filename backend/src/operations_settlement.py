"""沉降超限、道路预警与更正单的业务核对视图。"""

SETTLEMENT_VIEWS = {
    'settlement-limit-warning': (
        '沉降超限成片核对',
        '按测点上下限与测量误差复核必超限、可能超限分片，标明未成片点位的原因。',
        ['测量批', '点位', '井盖', '道路', '读数 mm', '下限 mm', '上限 mm', '误差 mm', '置信区间 mm', '判定', '成片'],
        """
SELECT b.batch_code,p.point_code,a.asset_code,a.street_name,p.settlement_mm,
       p.limit_lower_mm,
       COALESCE(p.limit_upper_mm,b.warning_limit_mm),
       COALESCE(p.tolerance_mm,b.default_tolerance_mm),
       ROUND((p.settlement_mm-COALESCE(p.tolerance_mm,b.default_tolerance_mm))::numeric,2)||' ~ '
         ||ROUND((p.settlement_mm+COALESCE(p.tolerance_mm,b.default_tolerance_mm))::numeric,2),
       CASE WHEN p.coordinate_reference<>b.coordinate_reference THEN '不出片：基准不一致'
            WHEN p.settlement_mm>COALESCE(p.limit_upper_mm,b.warning_limit_mm)
                 AND p.settlement_mm-COALESCE(p.tolerance_mm,b.default_tolerance_mm)>COALESCE(p.limit_upper_mm,b.warning_limit_mm)
                 THEN '必超限'
            WHEN p.settlement_mm>COALESCE(p.limit_upper_mm,b.warning_limit_mm) THEN '可能超限，待复测'
            WHEN p.limit_lower_mm IS NOT NULL AND p.settlement_mm<p.limit_lower_mm
                 AND p.settlement_mm+COALESCE(p.tolerance_mm,b.default_tolerance_mm)<p.limit_lower_mm
                 THEN '必超限（低于下限）'
            WHEN p.limit_lower_mm IS NOT NULL AND p.settlement_mm<p.limit_lower_mm THEN '可能超限（低于下限），待复测'
            ELSE '限内' END,
       CASE WHEN p.coordinate_reference<>b.coordinate_reference THEN '—'
            WHEN p.settlement_mm>COALESCE(p.limit_upper_mm,b.warning_limit_mm)
                 AND p.settlement_mm-COALESCE(p.tolerance_mm,b.default_tolerance_mm)>COALESCE(p.limit_upper_mm,b.warning_limit_mm)
                 THEN '必超限片'
            WHEN p.settlement_mm>COALESCE(p.limit_upper_mm,b.warning_limit_mm) THEN '可能超限片'
            WHEN p.limit_lower_mm IS NOT NULL AND p.settlement_mm<p.limit_lower_mm
                 AND p.settlement_mm+COALESCE(p.tolerance_mm,b.default_tolerance_mm)<p.limit_lower_mm
                 THEN '必超限片'
            WHEN p.limit_lower_mm IS NOT NULL AND p.settlement_mm<p.limit_lower_mm THEN '可能超限片'
            ELSE '—' END
FROM survey_points p JOIN survey_batches b ON b.id=p.batch_id JOIN manhole_assets a ON a.id=p.manhole_id
ORDER BY b.measured_at DESC,p.point_code
""",
    ),
    'road-warning-corrections': (
        '道路预警与更正依据',
        '核对已签发道路预警的生效范围、每次重算触发的更正单及范围变化，说明先订正还是先签发。',
        ['预警', '签发版本', '预警状态', '生效版本', '更正单', '变化类型', '原范围点数', '新范围点数', '新增点', '移除点', '更正状态'],
        """
SELECT w.warning_code,w.revision,w.status,
       COALESCE((SELECT MAX(c.new_revision) FROM warning_corrections c WHERE c.warning_id=w.id),w.revision),
       COALESCE(c.correction_code,'—'),COALESCE(c.change_type,'—'),
       COALESCE(jsonb_array_length(c.scope_before->'point_codes'),jsonb_array_length(w.scope->'point_codes')),
       COALESCE(jsonb_array_length(c.scope_after->'point_codes'),jsonb_array_length(w.scope->'point_codes')),
       COALESCE(NULLIF(array_to_string(ARRAY(SELECT jsonb_array_elements_text(c.added_point_codes)),'、'),''),'—'),
       COALESCE(NULLIF(array_to_string(ARRAY(SELECT jsonb_array_elements_text(c.removed_point_codes)),'、'),''),'—'),
       COALESCE(c.status,'原单有效')
FROM road_warnings w
LEFT JOIN LATERAL (SELECT * FROM warning_corrections x WHERE x.warning_id=w.id
                   ORDER BY x.new_revision DESC,x.id DESC LIMIT 1) c ON TRUE
ORDER BY w.id DESC
""",
    ),
}
