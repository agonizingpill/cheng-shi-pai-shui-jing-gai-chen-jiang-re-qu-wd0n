import {esc} from './components/common.js';
import {correctReading, savePointLimits, saveBatchLimits, recomputeSettlement, issueWarning, issueCorrection, acknowledgeCorrection} from './api/client.js';

const CATEGORY_META = {
  certain: {title: '必超限成片', hint: '算上测量误差仍超限，可签发道路预警', tone: 'danger'},
  possible: {title: '可能超限成片', hint: '读数刚过线，扣误差后回到线内，待复测', tone: 'warn'},
};

const CATEGORY_LABEL = {
  certain: '必超限',
  possible: '可能超限，待复测',
  within: '限内',
  excluded: '不出片：基准不一致',
};

function metric(label, value, unit = '') {
  return `<div><span>${esc(label)}</span><b>${value}${unit ? ` <small>${esc(unit)}</small>` : ''}</b></div>`;
}

function plateCard(category, plate) {
  const meta = CATEGORY_META[category];
  if (!plate) return `<article class="plate-card ${meta.tone}"><header><b>${meta.title}</b></header><div class="plate-empty">尚未重算，暂无成片</div></article>`;
  return `<article class="plate-card ${meta.tone}">
    <header>
      <div><span class="section-kicker">${esc(plate.plate_code)} · R${plate.revision}</span><b>${meta.title}</b><small>${meta.hint}</small></div>
      <div class="plate-tally"><strong>${plate.point_count}</strong><span>测点</span></div>
    </header>
    <div class="plate-scroll"><div class="plate-image">${plate.image_svg || '<div class="plate-empty">本类别暂无测点</div>'}</div></div>
    <footer>
      <span>道路 <b>${esc((plate.roads || []).join('、') || '—')}</b></span>
      <span>原尺度 <b>${plate.scale_px_per_m} px/m</b></span>
      <span>峰值 <b>${plate.max_settlement_mm ?? '—'} mm</b></span>
      <span>点位 <b>${esc((plate.point_codes || []).join(' ') || '—')}</b></span>
    </footer>
  </article>`;
}

function assessmentRow(row, editable) {
  const category = row.category;
  const tone = category === 'certain' ? 'danger' : category === 'possible' ? 'warn' : category === 'excluded' ? 'muted' : 'good';
  const interval = `${row.confidence_low_mm} – ${row.confidence_high_mm} mm`;
  const lower = row.limit_lower_mm ?? '—';
  const limitSource = row.limit_source === 'point' ? '测点' : '批次';
  const editor = editable && row.category !== 'excluded'
    ? `<form class="row-form" data-reading="${row.point_id}"><input name="settlement_mm" type="number" step="0.1" min="0" max="200" value="${row.settlement_mm}" aria-label="${esc(row.point_code)} 订正读数"><button class="mini">订正读数</button></form>
       <form class="row-form limits" data-limits="${row.point_id}"><input name="limit_lower_mm" type="number" step="0.1" min="0" max="200" placeholder="下限" value="${row.limit_lower_mm ?? ''}" aria-label="下限"><input name="limit_upper_mm" type="number" step="0.1" min="0" max="200" placeholder="上限" value="${row.limit_upper_mm ?? ''}" aria-label="上限"><input name="tolerance_mm" type="number" step="0.1" min="0" max="20" placeholder="误差" value="${row.tolerance_mm ?? ''}" aria-label="误差"><button class="mini">存上下限</button></form>`
    : '';
  return `<tr class="row ${tone}">
    <td class="code">${esc(row.point_code)}</td>
    <td>${esc(row.asset_code)}</td>
    <td>${esc(row.street_name || '—')}</td>
    <td class="num">${row.settlement_mm}</td>
    <td class="num">${lower}</td>
    <td class="num">${row.limit_upper_mm}<small> ${esc(limitSource)}</small></td>
    <td class="num">±${row.tolerance_mm}</td>
    <td class="num">${interval}</td>
    <td><span class="tag ${tone}">${esc(CATEGORY_LABEL[category] || category)}</span>${row.excluded_reason ? `<small class="reason">${esc(row.excluded_reason)}</small>` : ''}</td>
    <td class="row-actions">${editor}</td>
  </tr>`;
}

function warningBlock(overview, canSignRole) {
  const warning = overview.warning;
  const corrections = overview.corrections || [];
  const certain = overview.plates?.certain;
  const canSign = canSignRole && !warning && certain && certain.point_count > 0 && !overview.settlement_dirty;
  const signAction = warning
    ? `<div class="warning-signed"><span class="tag good">已签发</span><b>${esc(warning.warning_code)}</b><small>道路：${esc(warning.road_name)} · 签发版本 R${warning.revision} · 生效版本 R${warning.effective_revision ?? warning.revision} · 生效范围 ${warning.effective_scope?.point_count ?? warning.scope?.point_count ?? 0} 点</small></div>`
    : canSign
      ? `<form id="warning-sign-form" class="sign-form"><input name="note" placeholder="签发说明（可选）" maxlength="120"><button class="mini danger">签发道路预警</button></form>`
      : `<p class="warning-hint">${overview.settlement_dirty ? '读数或上下限已订正，请先重算沉降范围再签发。' : '没有必超限成片，不能签发道路预警；可能超限成片请转复测。'}</p>`;

  const correctionList = corrections.length
    ? `<div class="correction-list"><div class="section-kicker">更正单 / 范围变化依据</div>${corrections.map(c => `
        <div class="correction-item ${c.change_type}">
          <div class="correction-head"><b>${esc(c.correction_code)}</b><span class="tag ${c.change_type === 'cleared' ? 'danger' : 'warn'}">${esc(changeLabel(c.change_type))}</span><span class="tag ${c.status === 'acknowledged' ? 'good' : 'muted'}">${esc(c.status === 'acknowledged' ? '已接收' : '待接收')}</span></div>
          <small>基准 R${c.base_revision} → 重算 R${c.new_revision} · ${esc(c.reason || '')}</small>
          <small>新增：${esc((c.added_point_codes || []).join('、') || '无')} · 移除：${esc((c.removed_point_codes || []).join('、') || '无')}</small>
          <small>依据：${esc(c.basis?.method || '')}</small>
          ${c.status === 'issued' ? `<button class="mini" data-action="ack-correction" data-id="${c.id}">接收更正单</button>` : ''}
        </div>`).join('')}</div>`
    : `<p class="warning-hint">尚无更正单。签发后若订正读数或上下限并重算导致范围变化，会自动生成更正依据。</p>`;

  return `<aside class="panel warning-panel">
    <div class="section-kicker">ROAD WARNING / 道路预警</div>
    <h2>签发与更正</h2>
    ${signAction}
    ${warning ? `<button class="mini" data-action="issue-correction" data-id="${warning.id}" ${canSignRole ? '' : 'disabled'}>按当前重算补发更正单（重试不重复）</button>` : ''}
    ${correctionList}
  </aside>`;
}

function changeLabel(type) {
  return {expanded: '范围扩大', reduced: '范围缩小', replaced: '点位替换', cleared: '范围清空'}[type] || type;
}

export function settlementSection(d) {
  const overview = d.settlement;
  const batch = d.batches?.[0];
  if (!overview || !batch) {
    return `<section class="settlement-board"><div class="empty"><strong>暂无沉降判定</strong><p>请先在测绘图板运行聚类，或手动重算沉降范围。</p></div></section>`;
  }
  const run = overview.latest_run;
  const editable = ['operator', 'supervisor'].includes(d.user?.role);
  const canSignRole = d.user?.role === 'supervisor';
  const summary = run?.summary || {};
  const stateTag = overview.settlement_dirty
    ? `<span class="tag warn">待重算</span>`
    : `<span class="tag good">已重算 R${overview.settlement_revision}</span>`;
  const warningTag = overview.warning ? `<span class="tag danger">${esc(overview.warning.warning_code)}</span>` : `<span class="tag muted">未签发预警</span>`;

  const rows = run?.assessments || [];
  const tableRows = rows.length
    ? rows.map(row => assessmentRow(row, editable)).join('')
    : `<tr><td colspan="10" class="plate-empty">尚无判定结果，请点击右侧重算。</td></tr>`;

  return `<section class="settlement-board">
    <div class="settlement-head">
      <div><div class="eyebrow">SETTLEMENT LIMITS / 沉降超限</div><h2>超限成片与道路预警</h2><p>按测点录入沉降上下限与测量误差：算上误差仍超限的井盖进必超限成片，只有它才能签发道路预警；刚过线的进可能超限成片，留待复测。</p></div>
      <div class="settlement-state">${stateTag}${warningTag}</div>
    </div>
    <div class="settlement-metrics">
      ${metric('必超限', summary.certain ?? 0, '点')}
      ${metric('可能超限', summary.possible ?? 0, '点')}
      ${metric('限内', summary.within ?? 0, '点')}
      ${metric('不出片', summary.excluded ?? 0, '点')}
      ${metric('判定版本', `R${overview.settlement_revision}`)}
    </div>
    <div class="settlement-controls">
      <form id="batch-limits-form" class="inline-form">
        <label>批次警戒线 mm<input name="warning_limit_mm" type="number" step="0.1" min="0" max="200" value="${batch.warning_limit_mm}"></label>
        <label>默认误差 mm<input name="default_tolerance_mm" type="number" step="0.1" min="0" max="20" value="${batch.default_tolerance_mm}"></label>
        <button class="mini">保存批次阈值</button>
      </form>
      <button class="secondary" data-action="recompute" data-id="${batch.id}" ${editable ? '' : 'disabled'}>${overview.settlement_dirty ? '重算沉降范围（有待订正）' : '重算沉降范围'}</button>
    </div>
    <div class="plate-grid">
      ${plateCard('certain', overview.plates?.certain)}
      ${plateCard('possible', overview.plates?.possible)}
    </div>
    <div class="settlement-lower">
      <div class="panel assessment-panel">
        <div class="panel-head"><div><span class="section-kicker">POINT ASSESSMENT / 测点判定</span><h2>逐点读数与上下限</h2></div><span class="micro">${rows.length} 个测点 · 订正后请重算</span></div>
        <div class="assessment-table"><table>
          <thead><tr><th>点位</th><th>井盖</th><th>道路</th><th>读数 mm</th><th>下限</th><th>上限</th><th>误差</th><th>置信区间</th><th>判定</th><th>订正 / 上下限</th></tr></thead>
          <tbody>${tableRows}</tbody>
        </table></div>
      </div>
      ${warningBlock(overview, d.user?.role === 'supervisor')}
    </div>
  </section>`;
}

function numberOrNull(value) {
  if (value === null || value === undefined || value === '') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function bindSettlement(d, refresh, setMessage) {
  const overview = d.settlement;
  if (!overview) return;

  const batchForm = document.querySelector('#batch-limits-form');
  if (batchForm) batchForm.onsubmit = async event => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    try {
      setMessage();
      await saveBatchLimits(d.batches[0].id, {
        warning_limit_mm: numberOrNull(data.get('warning_limit_mm')),
        default_tolerance_mm: numberOrNull(data.get('default_tolerance_mm')),
      });
      setMessage('批次警戒线与默认误差已保存，请重算沉降范围。');
      await refresh();
    } catch (error) { setMessage('', error.message); }
  };

  document.querySelectorAll('form[data-reading]').forEach(form => form.onsubmit = async event => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    try {
      setMessage();
      await correctReading(form.dataset.reading, {settlement_mm: Number(data.get('settlement_mm')), note: '热区页面订正读数'});
      setMessage('沉降读数已订正，重算后更新成片。');
      await refresh();
    } catch (error) { setMessage('', error.message); }
  });

  document.querySelectorAll('form[data-limits]').forEach(form => form.onsubmit = async event => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    try {
      setMessage();
      await savePointLimits(form.dataset.limits, {
        limit_lower_mm: numberOrNull(data.get('limit_lower_mm')),
        limit_upper_mm: numberOrNull(data.get('limit_upper_mm')),
        tolerance_mm: numberOrNull(data.get('tolerance_mm')),
      });
      setMessage('测点沉降上下限已保存，请重算。');
      await refresh();
    } catch (error) { setMessage('', error.message); }
  });

  const signForm = document.querySelector('#warning-sign-form');
  if (signForm) signForm.onsubmit = async event => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    try {
      setMessage();
      const result = await issueWarning(d.batches[0].id, {note: data.get('note') || ''});
      setMessage(`道路预警 ${result.warning.warning_code} 已签发，范围 ${result.warning.scope.point_count} 点。`);
      await refresh();
    } catch (error) { setMessage('', error.body?.code === 'SETTLEMENT_PENDING_RECOMPUTE' ? '读数或上下限已订正，请先重算再签发。' : error.message); }
  };

  document.querySelectorAll('[data-action="ack-correction"]').forEach(button => button.onclick = async () => {
    try {
      setMessage();
      await acknowledgeCorrection(button.dataset.id);
      setMessage('更正单已接收。');
      await refresh();
    } catch (error) { setMessage('', error.message); }
  });

  document.querySelectorAll('[data-action="issue-correction"]').forEach(button => button.onclick = async () => {
    try {
      setMessage();
      const result = await issueCorrection(button.dataset.id, {reason: '按当前重算版本补发更正单'});
      setMessage(result.created ? `已补发更正单 ${result.correction.correction_code}。` : `重算版本 R${result.correction.new_revision} 已有更正单，未重复签发。`);
      await refresh();
    } catch (error) { setMessage('', error.message); }
  });
}
