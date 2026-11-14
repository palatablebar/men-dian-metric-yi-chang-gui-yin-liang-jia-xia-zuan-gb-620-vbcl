import { api } from '../api.js';
import { ENUMS, can, fmtMoney } from '../config.js';
import { drawer, errorBox, errorMessage, esc, field, filterBar, fmtTime, kv, table, toast } from '../ui.js';

const state = { status: '', data: [] };

const CHANNEL_OPTIONS = Object.entries(ENUMS.channel).map(([value, label]) => ({ value, label }));

function fmtDelta(value, fmt) {
  const number = Number(value || 0);
  const tone = number < 0 ? 'neg' : number > 0 ? 'pos' : 'muted';
  return `<span class="${tone}">${number > 0 ? '+' : ''}${fmt(number)}</span>`;
}

function beforeAfter(before, after, fmt) {
  if (before === null || before === undefined || before === '') {
    return `<span class="muted">新增</span> <b>${fmt(after)}</b>`;
  }
  return `<span class="muted">${fmt(before)}</span> → <b>${fmt(after)}</b>`;
}

function reconcileBadge(ok) {
  if (ok === null || ok === undefined) return '<span class="badge info">—</span>';
  return ok ? '<span class="badge ok">已对平</span>' : '<span class="badge danger">未对平</span>';
}

function checkCell(check) {
  if (!check) return '<span class="muted">—</span>';
  const diff = Math.abs(check.diff || 0) + Math.abs(check.current_diff || 0);
  return check.ok
    ? `<span class="pos">✓</span> <span class="mono muted">${diff.toFixed(2)}</span>`
    : `<span class="neg">✗</span> <span class="mono neg">${diff.toFixed(2)}</span>`;
}

function openDetail(view, ctx, detail) {
  const reconciliation = detail.reconciliation || {};
  const perAnomaly = reconciliation.per_anomaly || [];
  const items = detail.items || [];
  const changes = detail.anomaly_changes || [];
  const reports = detail.affected_reports || [];
  const { root, close } = drawer({
    title: `${detail.revision_no} · ${esc(detail.source_label)}`,
    subtitle: `数据截止 ${esc(detail.data_cutoff)} · ${esc(detail.status_label)}`,
    body: `${kv([
      ['数据截止时间', `<span class="mono">${esc(detail.data_cutoff)}</span>`],
      ['来源', esc(detail.source_label)],
      ['状态', `<span class="badge ${detail.status === 'confirmed' ? 'ok' : 'pending'}">${esc(detail.status_label)}</span>`],
      ['对平结果', `${reconcileBadge(detail.reconciled)} <span class="mono muted">最大差异 ${Number(detail.max_abs_diff ?? 0).toFixed(2)} 元（容差 ${Number(reconciliation.tolerance ?? 1).toFixed(2)}）</span>`],
      ['变更指标点', detail.changed_points],
      ['重算异常', `${detail.affected_anomalies} 起 · 复核回退 ${detail.reviews_reset} 起`],
      ['创建人', esc(detail.created_by || '—')],
      ['确认人', detail.confirmed_by ? `${esc(detail.confirmed_by)} · ${fmtTime(detail.confirmed_at)}` : '—'],
      ['备注', esc(detail.note || '—')],
    ])}
    <h3 class="sub-head">对平检查（量价三项 / 渠道贡献 / 同行对比 vs 同期明细）</h3>
    ${table({
      rows: perAnomaly.map((entry) => ({ id: entry.anomaly_id, ...entry })),
      columns: [
        { key: 'anomaly_id', label: '异常', className: 'mono', render: (row) => `#${row.anomaly_id}` },
        { key: 'volume_price', label: '量价三项', render: (row) => checkCell((row.checks || {}).volume_price) },
        { key: 'channel', label: '渠道贡献', render: (row) => checkCell((row.checks || {}).channel) },
        { key: 'peer', label: '同行对比', render: (row) => checkCell((row.checks || {}).peer) },
        { key: 'ok', label: '结果', render: (row) => reconcileBadge(row.ok) },
      ],
      empty: { title: '没有对平记录', hint: '本次修订没有重算异常。' },
    })}
    <h3 class="sub-head">水位变化（修订前 → 修订后）</h3>
    ${table({
      rows: items.map((item, index) => ({ id: index, ...item })),
      columns: [
        { key: 'store_name', label: '门店 / 日期', render: (row) => `<b>${esc(row.store_name)}</b><div class="mono muted">${esc(row.stat_date)} · ${esc(row.store_code)}</div>` },
        { key: 'channel', label: '渠道', render: (row) => esc(ENUMS.channel[row.channel] || row.channel) },
        { key: 'revenue', label: '营收', className: 'num', render: (row) => `${beforeAfter(row.before?.revenue, row.after.revenue, fmtMoney)} ${fmtDelta(row.delta.revenue, fmtMoney)}` },
        { key: 'adr', label: 'ADR', className: 'num', render: (row) => beforeAfter(row.before?.adr, row.after.adr, (v) => `${Number(v).toFixed(0)} 元`) },
        { key: 'rooms_sold', label: '间夜', className: 'num', render: (row) => `${beforeAfter(row.before?.rooms_sold, row.after.rooms_sold, (v) => Number(v).toFixed(0))} ${fmtDelta(row.delta.rooms_sold, (v) => Number(v).toFixed(0))}` },
      ],
      empty: { title: '没有水位变化', hint: '本次修订没有指标点变更。' },
    })}
    <h3 class="sub-head">受影响异常（方向 / 影响档位变化时保留原复核并回到待复核）</h3>
    ${table({
      rows: changes.map((change) => ({ id: change.anomaly_id, ...change })),
      columns: [
        { key: 'store_name', label: '门店 / 日期', render: (row) => `<b>${esc(row.store_name)}</b><div class="mono muted">${esc(row.metric_date)} · ${esc(row.metric_label)}${row.is_new ? ' · 新增' : ''}</div>` },
        { key: 'direction', label: '方向', render: (row) => beforeAfter(row.direction.before ? ENUMS.direction[row.direction.before] : null, ENUMS.direction[row.direction.after] || row.direction.after, (v) => esc(v)) },
        { key: 'severity', label: '影响档位', render: (row) => `${row.severity.before ? `<span class="badge ${row.severity.before}">${esc(row.severity.before_label)}</span>` : '<span class="muted">—</span>'} → <span class="badge ${row.severity.after}">${esc(row.severity.after_label)}</span>` },
        { key: 'impact', label: '影响金额', className: 'num', render: (row) => beforeAfter(row.impact_amount.before, row.impact_amount.after, fmtMoney) },
        { key: 'review', label: '复核', render: (row) => row.review_reset
          ? `<span class="badge warn">已回退待复核</span><div class="muted small">原结论已保留：${esc(row.archived_review?.note || '—')}</div>`
          : '<span class="badge ok">结论保留</span>' },
      ],
      empty: { title: '没有受影响异常', hint: '补报数据未改变任何异常结论。' },
    })}
    <h3 class="sub-head">受影响报告（区间覆盖变更日期、仍引用旧版数据）</h3>
    ${table({
      rows: reports.map((report) => ({ id: report.id, ...report })),
      columns: [
        { key: 'report_no', label: '报告', className: 'mono', render: (row) => `<b>${esc(row.report_no)}</b><div class="mono muted">${esc(row.period_start)} → ${esc(row.period_end)}</div>` },
        { key: 'status', label: '状态', render: (row) => `<span class="badge ${row.status === 'published' ? 'ok' : 'pending'}">${esc(ENUMS.reportStatus[row.status] || row.status)}</span>` },
        { key: 'revision_no', label: '当前引用', render: (row) => (row.revision_no ? `<span class="mono">${esc(row.revision_no)}</span>` : '<span class="muted">未绑定（旧版数据）</span>') },
      ],
      empty: { title: '没有受影响报告', hint: '没有区间覆盖变更日期、且未引用本次修订的报告。' },
    })}`,
    footer: can(ctx.user.role, 'revision:confirm') && detail.status !== 'confirmed'
      ? `<div class="footer-actions"><button class="btn accent" data-confirm>确认修订（报告此后可引用）</button></div>`
      : `<span class="mono muted">${detail.status === 'confirmed' ? '已确认，报告可引用本次修订' : '待确认：确认后报告才能引用'}</span>`,
  });
  root.querySelector('[data-confirm]')?.addEventListener('click', async () => {
    try {
      await api.revisions.confirm(detail.id);
      toast(`修订 ${detail.revision_no} 已确认`);
      close();
      await revisionsPage.render(view, ctx);
    } catch (err) {
      toast(errorMessage(err), 'err');
    }
  });
}

async function openRevision(view, ctx, id) {
  try {
    const detail = await api.revisions.get(id);
    openDetail(view, ctx, detail);
  } catch (err) {
    toast(errorMessage(err), 'err');
  }
}

function pointRow(index) {
  return `<div class="filter-bar point-row" data-row="${index}">
    <div class="field"><label>门店编码</label><input name="store_code" type="text" placeholder="LB-HD-001" /></div>
    <div class="field"><label>日期</label><input name="stat_date" type="text" placeholder="2026-11-03" /></div>
    <div class="field"><label>渠道</label><select name="channel">${CHANNEL_OPTIONS.map((opt) => `<option value="${esc(opt.value)}">${esc(opt.label)}</option>`).join('')}</select></div>
    <div class="field"><label>已售间夜</label><input name="rooms_sold" type="number" placeholder="68" /></div>
    <div class="field"><label>ADR（元）</label><input name="adr" type="number" placeholder="1138.59" /></div>
    <button class="btn sm ghost" type="button" data-remove="${index}">删除</button>
  </div>`;
}

function openIngest(view, ctx) {
  let rowCount = 2;
  const { root, close } = drawer({
    title: '迟到补报 · 形成数据修订',
    subtitle: '按数据截止时间幂等：同一截止时间重试不新增修订',
    body: `<form id="revision-form" class="stack">
      ${field({ label: '数据截止时间', name: 'data_cutoff', placeholder: '2026-11-14T07:00（或 2026-11-14）' })}
      ${field({ label: '变化来源', name: 'source', value: 'mixed', options: Object.entries(ENUMS.revisionSource).map(([value, label]) => ({ value, label })) })}
      ${field({ label: '备注', name: 'note', placeholder: '例如：OTA 漏传订单回补 + PMS 协议间夜补录' })}
      <h3 class="sub-head">补报指标点（营收 = ADR × 已售间夜）</h3>
      <div id="point-rows" class="stack">${pointRow(0)}${pointRow(1)}</div>
      <button class="btn sm ghost" type="button" id="add-row">＋ 添加一行</button>
      <div class="form-error" id="revision-error"></div>
      <button class="btn accent" type="submit">落库并形成修订</button>
    </form>`,
  });
  root.querySelector('#add-row').addEventListener('click', () => {
    const container = root.querySelector('#point-rows');
    container.insertAdjacentHTML('beforeend', pointRow(rowCount));
    rowCount += 1;
  });
  root.querySelector('#point-rows').addEventListener('click', (event) => {
    const button = event.target.closest('[data-remove]');
    if (!button) return;
    const rows = root.querySelectorAll('.point-row');
    if (rows.length <= 1) {
      toast('至少保留一行补报点', 'err');
      return;
    }
    button.closest('.point-row').remove();
  });
  root.querySelector('#revision-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.target;
    const errorNode = root.querySelector('#revision-error');
    const points = [];
    for (const row of root.querySelectorAll('.point-row')) {
      const storeCode = row.querySelector('[name=store_code]').value.trim();
      const statDate = row.querySelector('[name=stat_date]').value.trim();
      const channel = row.querySelector('[name=channel]').value;
      const roomsSold = row.querySelector('[name=rooms_sold]').value;
      const adr = row.querySelector('[name=adr]').value;
      if (!storeCode && !statDate && !roomsSold && !adr) continue;
      points.push({ store_code: storeCode, stat_date: statDate, channel, rooms_sold: Number(roomsSold), adr: Number(adr) });
    }
    if (!points.length) {
      errorNode.textContent = '至少填写一行完整的补报指标点';
      return;
    }
    const button = form.querySelector('button[type=submit]');
    button.disabled = true;
    button.textContent = '落库中…';
    try {
      const detail = await api.revisions.create({
        data_cutoff: form.data_cutoff.value.trim(),
        source: form.source.value,
        note: form.note.value.trim() || null,
        points,
      });
      toast(
        detail.created === false
          ? `同一截止时间的修订 ${detail.revision_no} 已存在，幂等返回`
          : `修订 ${detail.revision_no} 已形成：重算 ${detail.affected_anomalies} 起异常，回退 ${detail.reviews_reset} 起复核`,
      );
      close();
      await revisionsPage.render(view, ctx);
    } catch (err) {
      errorNode.textContent = errorMessage(err);
      button.disabled = false;
      button.textContent = '落库并形成修订';
    }
  });
}

export const revisionsPage = {
  async render(view, ctx) {
    let data;
    try {
      data = await api.revisions.list({ status: state.status, limit: 50 });
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => revisionsPage.render(view, ctx));
      return;
    }
    state.data = data.items;
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>数据修订（PMS / OTA 迟到补报）</h2>
          <div class="head-actions">
            <span class="meta">${data.total} 次</span>
            ${can(ctx.user.role, 'revision:create') ? '<button class="btn sm accent" id="new-revision">补报数据 · 形成修订</button>' : ''}
          </div>
        </div>
        <div class="panel-body">
          ${filterBar({
            id: 'revision-filters',
            fields: [
              { label: '状态', name: 'status', value: state.status, options: [{ value: '', label: '全部' }, ...Object.entries(ENUMS.revisionStatus).map(([value, label]) => ({ value, label }))] },
            ],
          })}
          <p class="muted small">按数据截止时间形成修订：量价三项、渠道贡献、同行对比与同期明细对平；方向或影响档位变化时保留原复核并回到待复核；报告只引用已确认修订，同一截止时间重试不新增修订或报告。</p>
        </div>
        ${table({
          rows: data.items,
          columns: [
            { key: 'revision_no', label: '修订号', render: (row) => `<b class="mono">${esc(row.revision_no)}</b><div class="mono muted">${esc(row.data_cutoff)}</div>` },
            { key: 'source_label', label: '来源' },
            { key: 'status', label: '状态', render: (row) => `<span class="badge ${row.status === 'confirmed' ? 'ok' : 'pending'}">${esc(row.status_label)}</span>` },
            { key: 'changed_points', label: '变更点', className: 'num' },
            { key: 'affected_anomalies', label: '重算异常', className: 'num' },
            { key: 'reviews_reset', label: '复核回退', className: 'num' },
            { key: 'reconciled', label: '对平', render: (row) => reconcileBadge(row.reconciled) },
            { key: 'created_by', label: '创建人', className: 'mono' },
          ],
          empty: { title: '还没有数据修订', hint: 'PMS / OTA 迟到补报落库后，会按数据截止时间形成第一次修订。' },
        })}
      </section>`;
    view.querySelector('#revision-filters')?.addEventListener('change', (event) => {
      state.status = event.target.value;
      revisionsPage.render(view, ctx);
    });
    view.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', () => openRevision(view, ctx, row.dataset.id));
    });
    view.querySelector('#new-revision')?.addEventListener('click', () => openIngest(view, ctx));
  },
};

export default revisionsPage;
