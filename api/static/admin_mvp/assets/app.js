const state = {
  defaults: null,
  dashboard: null,
  kbs: [],
  docs: [],
  sessions: [],
  evalFiles: [],
  latestEval: null,
  currentPage: 'dashboard',
  paging: {
    kbs: { page: 1, pageSize: 8, total: 0 },
    docs: { page: 1, pageSize: 8, total: 0 },
    sessions: { page: 1, pageSize: 10, total: 0 },
  },
};

const qs = (s) => document.querySelector(s);
const qsa = (s) => document.querySelectorAll(s);

function esc(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function showToast(message, type = 'info') {
  const wrap = qs('#toast-wrap');
  const node = document.createElement('div');
  node.className = `toast ${type}`;
  node.textContent = message;
  wrap.appendChild(node);
  setTimeout(() => node.classList.add('show'), 5);
  setTimeout(() => {
    node.classList.remove('show');
    setTimeout(() => node.remove(), 180);
  }, 2200);
}

function setGlobalError(message) {
  const box = qs('#global-error');
  if (!message) {
    box.classList.add('hidden');
    box.textContent = '';
    return;
  }
  box.classList.remove('hidden');
  box.textContent = message;
}

async function api(path, options = {}) {
  try {
    const res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    const raw = await res.text();
    let data = null;
    try {
      data = JSON.parse(raw);
    } catch (e) {
      throw new Error(`接口返回非 JSON: ${res.status}`);
    }
    if (!res.ok || data.code !== 0) {
      throw new Error(data.message || `请求失败(${res.status})`);
    }
    return data.data;
  } catch (e) {
    setGlobalError(e.message || '请求失败');
    throw e;
  }
}

async function apiForm(path, formData, options = {}) {
  try {
    const res = await fetch(path, {
      method: 'POST',
      body: formData,
      ...options,
    });
    const raw = await res.text();
    let data = null;
    try {
      data = JSON.parse(raw);
    } catch (e) {
      throw new Error(`接口返回非 JSON: ${res.status}`);
    }
    if (!res.ok || data.code !== 0) {
      throw new Error(data.message || `请求失败(${res.status})`);
    }
    return data.data;
  } catch (e) {
    setGlobalError(e.message || '请求失败');
    throw e;
  }
}

function openModal({ title, contentHtml, onConfirm, confirmText = '保存', width = '560px' }) {
  const host = qs('#modal-host');
  host.innerHTML = `
    <div class="overlay show" id="overlay-modal">
      <div class="modal" style="max-width:${width}">
        <div class="overlay-head">
          <h3>${esc(title)}</h3>
          <button class="ghost" id="modal-close">关闭</button>
        </div>
        <div class="overlay-body">${contentHtml}</div>
        <div class="overlay-foot">
          <button id="modal-cancel">取消</button>
          <button class="primary" id="modal-ok">${esc(confirmText)}</button>
        </div>
      </div>
    </div>`;

  const close = () => {
    host.innerHTML = '';
  };

  qs('#modal-close').onclick = close;
  qs('#modal-cancel').onclick = close;
  qs('#modal-ok').onclick = async () => {
    const okBtn = qs('#modal-ok');
    const prev = okBtn.textContent;
    okBtn.disabled = true;
    okBtn.textContent = '处理中...';
    try {
      await onConfirm(close);
    } catch (e) {
      showToast(e.message || '操作失败', 'error');
    } finally {
      okBtn.disabled = false;
      okBtn.textContent = prev;
    }
  };
}

function openConfirm({ title, text, onConfirm, confirmText = '确认' }) {
  openModal({
    title,
    contentHtml: `<p class="confirm-text">${esc(text)}</p>`,
    onConfirm,
    confirmText,
    width: '460px',
  });
}

function openDrawer({ title, contentHtml }) {
  const host = qs('#drawer-host');
  host.innerHTML = `
    <div class="overlay show" id="overlay-drawer">
      <aside class="drawer">
        <div class="overlay-head">
          <h3>${esc(title)}</h3>
          <button class="ghost" id="drawer-close">关闭</button>
        </div>
        <div class="overlay-body">${contentHtml}</div>
      </aside>
    </div>`;
  qs('#drawer-close').onclick = () => {
    host.innerHTML = '';
  };
}

function tag(text, style = 'muted') {
  return `<span class="tag ${style}">${esc(text)}</span>`;
}

function boolTag(v) {
  return v ? tag('正常', 'ok') : tag('异常', 'err');
}

function renderSources(sources) {
  const rows = Array.isArray(sources) ? sources : [];
  if (!rows.length) return emptyBlock('no citation sources');
  return `<table class="table dense">
    <thead><tr><th>source_label</th><th>doc_name</th><th>snippet</th><th>score</th><th>kb_id</th><th>chunk_id</th></tr></thead>
    <tbody>
      ${rows
        .map(
          (s) => `<tr>
            <td>${esc(s.source_label || '-')}</td>
            <td>${esc(s.doc_name || '-')}</td>
            <td>${esc(s.snippet || '-')}</td>
            <td>${esc(s.score ?? '-')}</td>
            <td>${esc(s.kb_id || '-')}</td>
            <td>${esc(s.chunk_id || '-')}</td>
          </tr>`,
        )
        .join('')}
    </tbody>
  </table>`;
}

function statusTag(status) {
  const s = String(status || '').toLowerCase();
  if (s === 'normal' || s === 'done') return tag(s === 'done' ? '完成' : '正常', 'ok');
  if (s === 'empty') return tag('空库', 'warn');
  if (s === 'running') return tag('解析中', 'warn');
  if (s === 'low_chunk') return tag('低 chunk', 'warn');
  if (s === 'failed' || s === 'cancelled') return tag('异常', 'err');
  if (s === 'conversation') return tag('线上会话', 'muted');
  if (s === 'eval') return tag('离线评测', 'warn');
  return tag(status || '-', 'muted');
}

function emptyBlock(text) {
  return `<div class="empty">${esc(text)}</div>`;
}

function loadingBlock(text = '加载中...') {
  return `<div class="loading"><span class="spinner"></span>${esc(text)}</div>`;
}

function truncate(text, len = 120) {
  const t = String(text || '');
  if (t.length <= len) return t;
  return t.slice(0, len) + '...';
}

function pageSwitch(page) {
  state.currentPage = page;
  qsa('.menu-item').forEach((b) => b.classList.toggle('active', b.dataset.page === page));
  qsa('.page').forEach((p) => p.classList.add('hidden'));
  qs(`#page-${page}`).classList.remove('hidden');
}

function renderPagination({ key, page, pageSize, total }) {
  const pages = Math.max(1, Math.ceil((Number(total) || 0) / (Number(pageSize) || 1)));
  const p = Math.min(Math.max(1, Number(page) || 1), pages);
  state.paging[key].page = p;
  state.paging[key].total = Number(total) || 0;
  state.paging[key].pageSize = Number(pageSize) || state.paging[key].pageSize;

  return `
    <div class="pager" data-pager="${key}">
      <span>共 ${total} 条</span>
      <button data-act="prev" ${p <= 1 ? 'disabled' : ''}>上一页</button>
      <span>第 ${p} / ${pages} 页</span>
      <button data-act="next" ${p >= pages ? 'disabled' : ''}>下一页</button>
      <select data-act="size">
        ${[8, 10, 20, 50]
          .map((x) => `<option value="${x}" ${x === Number(pageSize) ? 'selected' : ''}>${x}/页</option>`)
          .join('')}
      </select>
    </div>`;
}

function bindPager(key, refresh) {
  const root = qs(`[data-pager="${key}"]`);
  if (!root) return;
  root.querySelectorAll('button').forEach((btn) => {
    btn.onclick = async () => {
      const act = btn.dataset.act;
      if (act === 'prev') state.paging[key].page = Math.max(1, state.paging[key].page - 1);
      if (act === 'next') state.paging[key].page = state.paging[key].page + 1;
      await refresh();
    };
  });
  const sel = root.querySelector('select[data-act="size"]');
  if (sel) {
    sel.onchange = async () => {
      state.paging[key].pageSize = Number(sel.value || 10);
      state.paging[key].page = 1;
      await refresh();
    };
  }
}

function setupMenu() {
  qsa('.menu-item').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const page = btn.dataset.page;
      pageSwitch(page);
      if (page === 'dashboard') await renderDashboard();
      if (page === 'kbs') await renderKbPage();
      if (page === 'docs') await renderDocPage();
      if (page === 'acl') await renderAclPage();
      if (page === 'sessions') await renderSessionPage();
      if (page === 'evals') await renderEvalPage();
    });
  });
}

async function loadDefaults() {
  state.defaults = await api('/api/admin/defaults');
}

async function renderDashboard() {
  const box = qs('#page-dashboard');
  box.innerHTML = loadingBlock('Dashboard 数据加载中...');

  try {
    const d = await api('/api/admin/dashboard');
    state.dashboard = d;

    const sys = d.system_status || {};
    const perf = d.performance || {};
    const kbHealth = d.kb_health || {};
    const qa = Array.isArray(d.recent_qa) ? d.recent_qa : [];

    const healthRows = [
      { title: '空知识库', rows: kbHealth.empty_kbs || [] },
      { title: '低 chunk 知识库', rows: kbHealth.low_chunk_kbs || [] },
      { title: '解析失败文档', rows: kbHealth.failed_docs || [] },
    ];

    box.innerHTML = `
      <section class="page-head">
        <h2>Dashboard</h2>
        <p>核心指标、系统状态、知识库健康度与最近问答概览</p>
      </section>

      <section class="metric-grid">
        <article class="metric-card"><span>今日提问数</span><strong>${esc(d.cards.today_questions)}</strong></article>
        <article class="metric-card"><span>成功回答数</span><strong>${esc(d.cards.success_answers)}</strong></article>
        <article class="metric-card"><span>命中率</span><strong>${esc(d.cards.hit_rate)}</strong></article>
        <article class="metric-card"><span>平均响应(s)</span><strong>${esc(d.cards.avg_response_seconds)}</strong></article>
        <article class="metric-card"><span>知识库数量</span><strong>${esc(d.cards.kb_count)}</strong></article>
        <article class="metric-card"><span>文档数量</span><strong>${esc(d.cards.document_count)}</strong></article>
      </section>

      <section class="grid-2">
        <article class="panel">
          <div class="panel-head"><h3>系统状态</h3></div>
          <div class="status-list">
            <div><label>WS 连接</label>${boolTag(Boolean(sys.ws_connected))}</div>
            <div><label>Worker 状态</label>${boolTag(Boolean(sys.worker_running))}</div>
            <div><label>队列积压</label>${tag(sys.queue_size, Number(sys.queue_size || 0) > 0 ? 'warn' : 'ok')}</div>
            <div><label>最后处理时间</label><span>${esc(sys.last_processed_at || '-')}</span></div>
            <div><label>最后错误</label>${sys.last_error ? tag('有错误', 'err') : tag('无', 'ok')}</div>
          </div>
        </article>
        <article class="panel">
          <div class="panel-head"><h3>最近表现</h3></div>
          <table class="table dense">
            <tbody>
              <tr><th>answer_keyword_match_rate</th><td>${esc(perf.answer_keyword_match_rate)}</td></tr>
              <tr><th>source_keyword_match_rate</th><td>${esc(perf.source_keyword_match_rate)}</td></tr>
              <tr><th>Query Rewrite</th><td>${boolTag(Boolean(perf.query_rewrite_enabled))}</td></tr>
              <tr><th>Eval 文件</th><td class="small">${esc(perf.eval_file || '-')}</td></tr>
            </tbody>
          </table>
        </article>
      </section>

      <section class="panel">
        <div class="panel-head"><h3>知识库健康度</h3></div>
        <div class="grid-3">
          ${healthRows
            .map((g) => `
              <div class="sub-panel">
                <h4>${esc(g.title)} (${g.rows.length})</h4>
                ${
                  g.rows.length
                    ? `<ul class="item-list">${g.rows
                        .slice(0, 6)
                        .map((r) => `<li><span>${esc(r.name || r.kb_name || r.id || '-')}</span><small>${esc(r.kb_id || r.id || '')}</small></li>`)
                        .join('')}</ul>`
                    : emptyBlock('暂无数据')
                }
              </div>`)
            .join('')}
        </div>
      </section>

      <section class="panel">
        <div class="panel-head"><h3>最近问答</h3></div>
        ${
          qa.length
            ? `<table class="table">
                <thead><tr><th>时间</th><th>用户</th><th>问题</th><th>命中 KB</th><th>结果</th><th>耗时(s)</th></tr></thead>
                <tbody>
                  ${qa
                    .map(
                      (x) => `<tr>
                        <td>${esc(x.time || '-')}</td>
                        <td>${esc(x.user || '-')}</td>
                        <td><div class="truncate" title="${esc(x.question || '')}">${esc(x.question || '-')}</div></td>
                        <td>${esc((x.hit_kbs || []).join(', ') || '-')}</td>
                        <td>${x.success ? tag('成功', 'ok') : tag('失败', 'err')}</td>
                        <td>${esc(x.latency_seconds || 0)}</td>
                      </tr>`,
                    )
                    .join('')}
                </tbody>
              </table>`
            : emptyBlock('暂无最近问答记录')
        }
      </section>`;
  } catch (e) {
    box.innerHTML = `<div class="panel">${emptyBlock(`Dashboard 加载失败: ${e.message}`)}</div>`;
  }
}

function kbFormHtml(title, init = {}) {
  const allowText = Array.isArray(init.allowed_user_ids) ? init.allowed_user_ids.join(',') : '';
  const denyText = Array.isArray(init.denied_user_ids) ? init.denied_user_ids.join(',') : '';
  return `
    <form id="kb-form" class="form-grid">
      <div class="field"><label>名称</label><input name="name" value="${esc(init.name || '')}" placeholder="知识库名称" /></div>
      <div class="field"><label>scope</label>
        <select name="scope" id="kb-form-scope">
          <option value="public" ${init.scope === 'public' ? 'selected' : ''}>public</option>
          <option value="department" ${init.scope === 'department' ? 'selected' : ''}>department</option>
          <option value="personal" ${init.scope === 'personal' ? 'selected' : ''}>personal</option>
        </select>
      </div>
      <div class="field"><label>owner_user_id</label><input name="owner_user_id" value="${esc(init.owner_user_id || '')}" placeholder="personal 时必填" /></div>
      <div class="field"><label>department_id</label><input name="department_id" value="${esc(init.department_id || '')}" placeholder="department 时必填" /></div>
      <div class="field col-2"><label>allowed_user_ids</label><input name="allowed_user_ids" value="${esc(allowText)}" placeholder="逗号分隔，如 u_demo,u_alice" /></div>
      <div class="field col-2"><label>denied_user_ids</label><input name="denied_user_ids" value="${esc(denyText)}" placeholder="逗号分隔，如 u_bob,u_test" /></div>
      <div class="field col-2"><label>描述</label><textarea name="description" rows="3" placeholder="可选">${esc(init.description || '')}</textarea></div>
      <p class="small">${esc(title)}</p>
    </form>`;
}

function parseCsvUsers(text) {
  const raw = String(text || '').trim();
  if (!raw) return [];
  const seen = new Set();
  return raw
    .split(',')
    .map((x) => x.trim())
    .filter((x) => {
      if (!x || seen.has(x)) return false;
      seen.add(x);
      return true;
    });
}

function parseKbFormPayload() {
  const fd = new FormData(qs('#kb-form'));
  const payload = {
    name: String(fd.get('name') || '').trim(),
    description: String(fd.get('description') || '').trim(),
    scope: String(fd.get('scope') || 'public').trim(),
    owner_user_id: String(fd.get('owner_user_id') || '').trim(),
    department_id: String(fd.get('department_id') || '').trim(),
    allowed_user_ids: parseCsvUsers(fd.get('allowed_user_ids')),
    denied_user_ids: parseCsvUsers(fd.get('denied_user_ids')),
  };

  if (!payload.name) {
    throw new Error('名称不能为空');
  }

  if (payload.scope === 'public') {
    payload.owner_user_id = '';
    payload.department_id = '';
  }
  if (payload.scope === 'department') {
    payload.owner_user_id = '';
    if (!payload.department_id) throw new Error('department scope 必须填写 department_id');
  }
  if (payload.scope === 'personal') {
    payload.department_id = '';
    if (!payload.owner_user_id) throw new Error('personal scope 必须填写 owner_user_id');
  }

  return payload;
}

async function showKbDetail(kbId) {
  const d = await api('/api/admin/kbs/' + kbId);
  const docs = Array.isArray(d.documents) ? d.documents : [];
  openDrawer({
    title: `知识库详情 · ${d.name || kbId}`,
    contentHtml: `
      <div class="kpi-strip">
        <div><span>文档数</span><strong>${esc(d.doc_count)}</strong></div>
        <div><span>chunk 总数</span><strong>${esc(d.chunk_count)}</strong></div>
      </div>
      <div class="kv-grid">
        <div><label>KB ID</label><span class="mono">${esc(d.kb_id)}</span></div>
        <div><label>Tenant</label><span class="mono">${esc(d.tenant_id)}</span></div>
        <div><label>scope</label><span>${statusTag(d.scope)}</span></div>
        <div><label>ACL 摘要</label><span>${esc(`${d.acl_preview?.scope || d.scope || 'public'} / owner:${d.acl_preview?.owner_user_id || '-'} / dept:${d.acl_preview?.department_id || '-'} / allow:${(d.acl_preview?.allowed_user_ids || []).length} / deny:${(d.acl_preview?.denied_user_ids || []).length}`)}</span></div>
        <div><label>owner</label><span>${esc(d.owner_user_id || '-')}</span></div>
        <div><label>department</label><span>${esc(d.department_id || '-')}</span></div>
        <div><label>allowed_user_ids</label><span>${esc((d.allowed_user_ids || []).join(', ') || '-')}</span></div>
        <div><label>denied_user_ids</label><span>${esc((d.denied_user_ids || []).join(', ') || '-')}</span></div>
        <div><label>更新时间</label><span>${esc(d.update_date || '-')}</span></div>
      </div>
      <div class="sub-title">关联文档</div>
      ${
        docs.length
          ? `<table class="table dense"><thead><tr><th>ID</th><th>名称</th><th>状态</th><th>chunk</th><th>更新时间</th></tr></thead><tbody>
              ${docs
                .map(
                  (x) => `<tr><td><code>${esc(x.id)}</code></td><td>${esc(x.name || '-')}</td><td>${statusTag(x.status)}</td><td>${esc(x.chunk_num)}</td><td>${esc(x.update_date || '-')}</td></tr>`,
                )
                .join('')}
            </tbody></table>`
          : emptyBlock('该知识库暂无关联文档')
      }`,
  });
}

async function renderKbPage() {
  const box = qs('#page-kbs');
  box.innerHTML = `
    <section class="page-head">
      <h2>知识库管理</h2>
      <p>筛选、分页、查看详情与编辑 ACL 策略</p>
    </section>
    <section class="panel">
      <div class="filter-grid kb-filter">
        <div class="field"><label>关键词</label><input id="kb-q" placeholder="名称 / kb_id" /></div>
        <div class="field"><label>scope</label>
          <select id="kb-scope"><option value="">全部</option><option value="public">public</option><option value="department">department</option><option value="personal">personal</option></select>
        </div>
        <div class="field"><label>department</label><input id="kb-department" placeholder="department_id" /></div>
        <div class="field field-check"><label>筛选</label><label><input type="checkbox" id="kb-empty-only" /> 仅空库</label></div>
        <div class="field actions-row">
          <button class="primary" id="kb-refresh">查询</button>
          <button id="kb-create">新建知识库</button>
        </div>
      </div>
      <div id="kb-table-wrap">${loadingBlock('加载知识库中...')}</div>
    </section>`;

  async function refresh() {
    const params = new URLSearchParams({
      q: qs('#kb-q').value.trim(),
      scope: qs('#kb-scope').value,
      department: qs('#kb-department').value.trim(),
      empty_only: qs('#kb-empty-only').checked ? '1' : '',
      page: String(state.paging.kbs.page),
      page_size: String(state.paging.kbs.pageSize),
    });

    const data = await api('/api/admin/kbs?' + params.toString());
    const rows = data.items || [];

    qs('#kb-table-wrap').innerHTML = rows.length
      ? `${renderPagination({ key: 'kbs', page: data.page || 1, pageSize: data.page_size || state.paging.kbs.pageSize, total: data.total || 0 })}
         <table class="table">
          <thead><tr><th>名称</th><th>KB ID</th><th>scope</th><th>owner</th><th>department</th><th>ACL 摘要</th><th>文档数</th><th>chunk</th><th>状态</th><th>更新时间</th><th>操作</th></tr></thead>
          <tbody>
            ${rows
              .map(
                (k) => `<tr>
                <td>${esc(k.name)}</td>
                <td><code>${esc(k.kb_id)}</code></td>
                <td>${tag(k.scope || 'public', 'muted')}</td>
                <td>${esc(k.owner_user_id || '-')}</td>
                <td>${esc(k.department_id || '-')}</td>
                <td>${esc(`allow:${k.allowed_user_count || 0} / deny:${k.denied_user_count || 0}`)}</td>
                <td>${esc(k.doc_count)}</td>
                <td>${esc(k.chunk_count)}</td>
                <td>${statusTag(k.status)}</td>
                <td>${esc(k.update_date || '-')}</td>
                <td class="actions">
                  <button data-act="detail" data-id="${esc(k.kb_id)}">详情</button>
                  <button data-act="edit" data-id="${esc(k.kb_id)}">编辑</button>
                  <button class="danger" data-act="del" data-id="${esc(k.kb_id)}">删除</button>
                </td>
              </tr>`,
              )
              .join('')}
          </tbody>
        </table>`
      : emptyBlock('未找到符合条件的知识库');

    bindPager('kbs', refresh);

    qsa('#kb-table-wrap button').forEach((btn) => {
      btn.onclick = async () => {
        const id = btn.dataset.id;
        const act = btn.dataset.act;

        if (act === 'detail') {
          try {
            await showKbDetail(id);
          } catch (e) {
            showToast('加载详情失败: ' + e.message, 'error');
          }
          return;
        }

        if (act === 'edit') {
          const current = rows.find((x) => x.kb_id === id) || {};
          openModal({
            title: `编辑知识库 · ${current.name || id}`,
            contentHtml: kbFormHtml('保存后会更新名称/描述与 ACL 策略', current),
            onConfirm: async (close) => {
              const payload = parseKbFormPayload();
              await api('/api/admin/kbs/' + id, { method: 'PUT', body: JSON.stringify(payload) });
              close();
              showToast('知识库已更新', 'success');
              await refresh();
            },
          });
          return;
        }

        if (act === 'del') {
          openConfirm({
            title: '删除知识库',
            text: `确认软删除知识库 ${id} ？`,
            confirmText: '确认删除',
            onConfirm: async (close) => {
              await api('/api/admin/kbs/' + id, { method: 'DELETE' });
              close();
              showToast('知识库已删除', 'success');
              await refresh();
            },
          });
        }
      };
    });
  }

  qs('#kb-refresh').onclick = async () => {
    state.paging.kbs.page = 1;
    await refresh();
  };

  qs('#kb-create').onclick = () => {
    openModal({
      title: '新建知识库',
      contentHtml: kbFormHtml('最小字段创建，tenant 将使用后端默认推断'),
      onConfirm: async (close) => {
        const payload = parseKbFormPayload();
        await api('/api/admin/kbs', { method: 'POST', body: JSON.stringify(payload) });
        close();
        showToast('知识库已创建', 'success');
        await refresh();
      },
    });
  };

  try {
    await refresh();
  } catch (e) {
    qs('#kb-table-wrap').innerHTML = emptyBlock('知识库加载失败: ' + e.message);
  }
}

async function showDocDetail(docId) {
  const d = await api('/api/admin/documents/' + docId);
  const chunks = Array.isArray(d.chunk_preview) ? d.chunk_preview : [];

  openDrawer({
    title: `文档详情 · ${d.name || docId}`,
    contentHtml: `
      <div class="kv-grid">
        <div><label>文档ID</label><span class="mono">${esc(d.id)}</span></div>
        <div><label>KB ID</label><span class="mono">${esc(d.kb_id)}</span></div>
        <div><label>状态</label><span>${statusTag(d.status)}</span></div>
        <div><label>进度</label><span>${esc(d.progress)} (${esc(d.progress_msg || '-')})</span></div>
        <div><label>chunk 数</label><span>${esc(d.chunk_num)}</span></div>
        <div><label>文件类型</label><span>${esc(d.type || d.suffix || '-')}</span></div>
        <div><label>创建时间</label><span>${esc(d.create_date || '-')}</span></div>
        <div><label>更新时间</label><span>${esc(d.update_date || '-')}</span></div>
        <div><label>最近错误</label><span>${esc(d.last_error || '-')}</span></div>
      </div>
      <div class="sub-title">Chunk 预览（前 5 条）</div>
      ${
        chunks.length
          ? `<div class="chunk-list">${chunks
              .map(
                (c, i) => `<article><h4>#${i + 1} ${esc(c.chunk_id || '-')} · ${esc(c.doc_name || c.doc_id || '-')}</h4><p>${esc(c.snippet || c.content || '-')}</p></article>`,
              )
              .join('')}</div>`
          : emptyBlock('暂无 chunk 预览')
      }`,
  });
}

async function openUploadModal(refresh) {
  let kbOpts = '<option value="">请选择知识库</option>';
  try {
    const kbs = await api('/api/admin/kbs?page=1&page_size=200');
    const items = Array.isArray(kbs.items) ? kbs.items : [];
    kbOpts += items.map((k) => `<option value="${esc(k.kb_id)}">${esc(k.name)} (${esc(k.kb_id)})</option>`).join('');
  } catch (e) {
    kbOpts += '';
  }

  openModal({
    title: '上传文档',
    confirmText: '上传并入库',
    contentHtml: `
      <form id="doc-upload-form" class="form-grid">
        <div class="field col-2"><label>目标知识库</label><select name="kb_id">${kbOpts}</select></div>
        <div class="field col-2"><label>文件</label><input name="file" type="file" /></div>
        <p class="small">上传后将触发解析任务并自动刷新列表</p>
      </form>`,
    onConfirm: async (close) => {
      const form = qs('#doc-upload-form');
      const kbId = String(new FormData(form).get('kb_id') || '').trim();
      const fileInput = form.querySelector('input[name="file"]');
      const files = fileInput.files;

      if (!kbId) throw new Error('请选择知识库');
      if (!files || files.length === 0) throw new Error('请选择文件');

      const fd = new FormData();
      fd.append('kb_id', kbId);
      fd.append('file', files[0]);

      const data = await apiForm('/api/admin/documents/upload', fd);
      close();
      showToast(data.message || `上传完成，已排队 ${data.queued_count || 0} 个文档`, 'success');
      if (Array.isArray(data.errors) && data.errors.length) {
        showToast(data.errors.join(' | '), 'error');
      }
      await refresh();
    },
    width: '700px',
  });
}

async function renderDocPage() {
  const box = qs('#page-docs');
  box.innerHTML = `
    <section class="page-head">
      <h2>文档管理</h2>
      <p>按状态筛选、分页查看、上传、详情、重解析与删除</p>
    </section>
    <section class="panel">
      <div class="filter-grid doc-filter">
        <div class="field"><label>文档名</label><input id="doc-q" placeholder="输入关键词" /></div>
        <div class="field"><label>所属 KB</label><input id="doc-kb" placeholder="kb_id 或 kb_name" /></div>
        <div class="field"><label>状态</label>
          <select id="doc-status">
            <option value="">全部状态</option>
            <option value="running">running</option>
            <option value="done">done</option>
            <option value="failed">failed</option>
            <option value="cancelled">cancelled</option>
          </select>
        </div>
        <div class="field actions-row">
          <button class="primary" id="doc-refresh">查询</button>
          <button id="doc-upload">上传文档</button>
        </div>
      </div>
      <div id="doc-table-wrap">${loadingBlock('加载文档中...')}</div>
    </section>`;

  async function refresh() {
    const kbRaw = qs('#doc-kb').value.trim();
    const params = new URLSearchParams({
      q: qs('#doc-q').value.trim(),
      kb_id: /^[a-z0-9]{32}$/i.test(kbRaw) ? kbRaw : '',
      kb_name: /^[a-z0-9]{32}$/i.test(kbRaw) ? '' : kbRaw,
      status: qs('#doc-status').value,
      page: String(state.paging.docs.page),
      page_size: String(state.paging.docs.pageSize),
    });
    const data = await api('/api/admin/documents?' + params.toString());
    const rows = data.items || [];
    state.docs = rows;

    qs('#doc-table-wrap').innerHTML = rows.length
      ? `${renderPagination({ key: 'docs', page: data.page || 1, pageSize: data.page_size || state.paging.docs.pageSize, total: data.total || 0 })}
         <table class="table">
          <thead><tr><th>文档名</th><th>所属 KB</th><th>类型</th><th>解析状态</th><th>chunk</th><th>上传时间</th><th>更新时间</th><th>操作</th></tr></thead>
          <tbody>
            ${rows
              .map(
                (d) => `<tr>
                <td>${esc(d.name || '-')}</td>
                <td><div>${esc(d.kb_name || '-')}</div><code>${esc(d.kb_id || '-')}</code></td>
                <td>${esc(d.type || d.suffix || d.file_type || '-')}</td>
                <td>${statusTag(d.status)}<div class="small">run=${esc(d.run)}</div><div class="small">progress=${esc(d.progress)}</div></td>
                <td>${esc(d.chunk_num)}</td>
                <td>${esc(d.create_time || d.create_date || '-')}</td>
                <td>${esc(d.update_time || d.update_date || '-')}</td>
                <td class="actions">
                  <button data-act="detail" data-id="${esc(d.id)}">详情</button>
                  <button data-act="reparse" data-id="${esc(d.id)}">重解析</button>
                  <button class="danger" data-act="del" data-id="${esc(d.id)}">删除</button>
                </td>
              </tr>`,
              )
              .join('')}
          </tbody>
        </table>`
      : emptyBlock('当前筛选条件下暂无文档');

    bindPager('docs', refresh);

    qsa('#doc-table-wrap button').forEach((btn) => {
      btn.onclick = async () => {
        const id = btn.dataset.id;
        const act = btn.dataset.act;

        if (act === 'detail') {
          try {
            await showDocDetail(id);
          } catch (e) {
            showToast('加载详情失败: ' + e.message, 'error');
          }
          return;
        }

        if (act === 'reparse') {
          openConfirm({
            title: '确认重解析',
            text: `确认重解析文档 ${id} ？`,
            confirmText: '确认重解析',
            onConfirm: async (close) => {
              await api('/api/admin/documents/' + id + '/reparse', { method: 'POST' });
              close();
              showToast('已触发重解析', 'success');
              await refresh();
            },
          });
          return;
        }

        if (act === 'del') {
          openConfirm({
            title: '确认删除文档',
            text: `删除后不可恢复，确认删除 ${id} ？`,
            confirmText: '确认删除',
            onConfirm: async (close) => {
              await api('/api/admin/documents/' + id, { method: 'DELETE' });
              close();
              showToast('文档已删除', 'success');
              await refresh();
            },
          });
        }
      };
    });
  }

  qs('#doc-refresh').onclick = async () => {
    state.paging.docs.page = 1;
    await refresh();
  };

  qs('#doc-upload').onclick = async () => {
    await openUploadModal(refresh);
  };

  try {
    await refresh();
  } catch (e) {
    qs('#doc-table-wrap').innerHTML = emptyBlock('文档加载失败: ' + e.message);
  }
}

async function renderAclPage() {
  const box = qs('#page-acl');
  const defaults = state.defaults || { default_dialog_id: '' };
  box.innerHTML = `
    <section class="page-head">
      <h2>ACL 调试</h2>
      <p>按 dialog + 用户身份查看 KB 过滤决策</p>
    </section>
    <section class="panel">
      <div class="filter-grid acl-filter">
        <div class="field"><label>dialog_id</label><input id="acl-dialog" value="${esc(defaults.default_dialog_id || '')}" /></div>
        <div class="field"><label>open_id</label><input id="acl-open" value="ou_test_citation" /></div>
        <div class="field"><label>user_id(可选)</label><input id="acl-user" /></div>
        <div class="field actions-row"><button class="primary" id="acl-run">查询 ACL</button></div>
      </div>
      <div id="acl-result">${emptyBlock('请输入参数后点击查询')}</div>
    </section>`;

  qs('#acl-run').onclick = async () => {
    const dialog_id = qs('#acl-dialog').value.trim();
    const open_id = qs('#acl-open').value.trim();
    const user_id = qs('#acl-user').value.trim();

    if (!dialog_id || (!open_id && !user_id)) {
      showToast('dialog_id 与 open_id/user_id 至少提供其一', 'error');
      return;
    }

    qs('#acl-result').innerHTML = loadingBlock('ACL 调试中...');
    const params = new URLSearchParams({ dialog_id, open_id, user_id });

    try {
      const data = await api('/api/admin/acl-debug?' + params.toString());
      const original = Array.isArray(data.original_kb_ids) ? data.original_kb_ids : [];
      const filtered = Array.isArray(data.filtered_kb_ids) ? data.filtered_kb_ids : [];
      const decisions = Array.isArray(data.per_kb_decisions) ? data.per_kb_decisions : [];

      qs('#acl-result').innerHTML = `
        <div class="grid-2">
          <div class="sub-panel">
            <h4>原始 KB 列表 (${original.length})</h4>
            ${original.length ? `<ul class="id-list">${original.map((x) => `<li><code>${esc(x)}</code></li>`).join('')}</ul>` : emptyBlock('无')}
          </div>
          <div class="sub-panel">
            <h4>过滤后 KB 列表 (${filtered.length})</h4>
            ${filtered.length ? `<ul class="id-list">${filtered.map((x) => `<li><code>${esc(x)}</code></li>`).join('')}</ul>` : emptyBlock('无')}
          </div>
        </div>

        <div class="sub-panel">
          <h4>用户上下文</h4>
          <div class="kv-grid compact">
            <div><label>external_user</label><span>${esc(data.external_user || '-')}</span></div>
            <div><label>internal_user</label><span>${esc(data.internal_user || '-')}</span></div>
            <div><label>department_id</label><span>${esc(data.department_id || '-')}</span></div>
            <div><label>dialog_id</label><span>${esc(data.dialog_id || '-')}</span></div>
          </div>
        </div>

        <div class="sub-panel">
          <h4>per_kb_decisions</h4>
          ${
            decisions.length
              ? `<table class="table dense">
                  <thead><tr><th>kb_id</th><th>scope</th><th>结果</th><th>reason</th><th>matched_rule</th><th>owner_user_id</th><th>department_id</th><th>allowed_user_ids</th><th>denied_user_ids</th></tr></thead>
                  <tbody>
                    ${decisions
                      .map(
                        (x) => `<tr>
                          <td><code>${esc(x.kb_id || '-')}</code></td>
                          <td>${tag(x.scope || '-', 'muted')}</td>
                          <td>${x.allow ? tag('ALLOW', 'ok') : tag('DENY', 'err')}</td>
                          <td>${esc(x.reason || '-')}</td>
                            <td>${esc(x.matched_rule || '-')}</td>
                            <td>${esc(x.owner_user_id || '-')}</td>
                            <td>${esc(x.department_id || '-')}</td>
                            <td>${esc((x.allowed_user_ids || []).join(', ') || '-')}</td>
                            <td>${esc((x.denied_user_ids || []).join(', ') || '-')}</td>
                        </tr>`,
                      )
                      .join('')}
                  </tbody>
                </table>`
              : emptyBlock('无决策数据')
          }
        </div>`;
    } catch (e) {
      qs('#acl-result').innerHTML = emptyBlock('ACL 查询失败: ' + e.message);
    }
  };
}

async function renderSessionPage() {
  const box = qs('#page-sessions');
  box.innerHTML = `
    <section class="page-head">
      <h2>问答记录</h2>
      <p>查看原始问题、检索问题、命中 KB、耗时与结果摘要</p>
    </section>
    <section class="panel">
      <div class="filter-grid session-filter">
        <div class="field"><label>关键词</label><input id="sess-q" placeholder="问题 / 回答 / 用户" /></div>
        <div class="field"><label>来源</label><select id="sess-source"><option value="">全部</option><option value="conversation">conversation</option><option value="eval">eval</option></select></div>
        <div class="field"><label>结果</label><select id="sess-success"><option value="">全部</option><option value="1">成功</option><option value="0">失败</option></select></div>
        <div class="field actions-row"><button class="primary" id="sess-refresh">查询</button></div>
      </div>
      <div id="sess-table-wrap">${loadingBlock('加载问答记录中...')}</div>
    </section>`;

  async function refresh() {
    const params = new URLSearchParams({
      q: qs('#sess-q').value.trim(),
      source: qs('#sess-source').value,
      success: qs('#sess-success').value,
      page: String(state.paging.sessions.page),
      page_size: String(state.paging.sessions.pageSize),
    });
    const data = await api('/api/admin/sessions?' + params.toString());
    const rows = data.items || [];
    state.sessions = rows;

    qs('#sess-table-wrap').innerHTML = rows.length
      ? `${renderPagination({ key: 'sessions', page: data.page || 1, pageSize: data.page_size || state.paging.sessions.pageSize, total: data.total || 0 })}
        <table class="table">
          <thead><tr><th>时间</th><th>来源</th><th>用户</th><th>原始问题</th><th>检索问题</th><th>命中KB</th><th>pipeline</th><th>结果</th><th>失败原因</th><th>耗时(s)</th><th>回答摘要</th><th>操作</th></tr></thead>
          <tbody>
            ${rows
              .map(
                (r) => `<tr>
                  <td>${esc(r.time || '-')}</td>
                  <td>${statusTag(r.source)}</td>
                  <td><div>${esc(r.external_user || '-')}</div><small>${esc(r.internal_user || '')}</small></td>
                  <td><div class="truncate" title="${esc(r.used_original_query || '')}">${esc(truncate(r.used_original_query || '-', 38))}</div></td>
                  <td><div class="truncate" title="${esc(r.used_retrieval_query || '')}">${esc(truncate(r.used_retrieval_query || '-', 38))}</div></td>
                  <td>${esc((r.hit_kbs || []).join(', ') || '-')}</td>
                  <td>${esc(r.pipeline_name || '-')}</td>
                  <td>${r.success ? tag('成功', 'ok') : tag('失败', 'err')}</td>
                  <td>${esc(r.failure_reason || '-')}</td>
                  <td>${esc(r.latency_seconds || 0)}</td>
                  <td>${esc(r.answer_summary || '-')}</td>
                  <td><button data-act="detail" data-id="${esc(r.id)}">详情</button></td>
                </tr>`,
              )
              .join('')}
          </tbody>
        </table>`
      : emptyBlock('暂无问答记录，请检查数据源或筛选条件');

    bindPager('sessions', refresh);

    qsa('#sess-table-wrap button[data-act="detail"]').forEach((btn) => {
      btn.onclick = async () => {
        const id = btn.dataset.id;
        try {
          const d = await api('/api/admin/sessions/' + encodeURIComponent(id));
          openDrawer({
            title: '问答记录详情',
            contentHtml: `
              <div class="kv-grid">
                <div><label>ID</label><span class="mono">${esc(d.id || '-')}</span></div>
                <div><label>来源</label><span>${statusTag(d.source)}</span></div>
                <div><label>时间</label><span>${esc(d.time || '-')}</span></div>
                <div><label>pipeline</label><span>${esc(d.pipeline_name || '-')}</span></div>
                <div><label>rewrite 生效</label><span>${boolTag(Boolean(d.rewrite_applied))}</span></div>
                <div><label>耗时(s)</label><span>${esc(d.latency_seconds || 0)}</span></div>
                <div><label>citation_count</label><span>${esc(d.citation_count ?? 0)}</span></div>
              </div>
              <div class="sub-title">完整 answer</div>
              <pre>${esc(d.answer || '-')}</pre>
              <div class="sub-title">context_seed_question</div>
              <div>${esc(d.context_seed_question || '-')}</div>
              <div class="sub-title">retrieved_kb_ids</div>
              <div>${esc((d.retrieved_kb_ids || []).join(', ') || '-')}</div>
              <div class="sub-title">source/citation 摘要</div>
              <div>${esc((d.source_summary || []).join(', ') || '-')}</div>
              <div class="sub-title">normalized_sources</div>
              ${renderSources(d.normalized_sources)}
              <div class="sub-title">formatted_answer_preview</div>
              <pre>${esc(d.formatted_answer_preview || '-')}</pre>
            `,
          });
        } catch (e) {
          showToast('加载详情失败: ' + e.message, 'error');
        }
      };
    });
  }

  qs('#sess-refresh').onclick = async () => {
    state.paging.sessions.page = 1;
    await refresh();
  };

  await refresh();
}

async function renderEvalPage() {
  const box = qs('#page-evals');
  box.innerHTML = loadingBlock('加载评测结果中...');

  async function refresh() {
    const [listData, latestData] = await Promise.all([
      api('/api/admin/evals'),
      api('/api/admin/evals/latest'),
    ]);
    state.evalFiles = listData.items || [];
    state.latestEval = latestData.latest || null;

    const latest = state.latestEval;
    const summary = latest?.summary || {};
    const rows = latest?.per_case_results || [];

    const options = (state.evalFiles || []).map((x) => `<option value="${esc(x.name)}">${esc(x.name)}</option>`).join('');
    const defaultAfter = state.evalFiles?.[0]?.name || '';
    const defaultBefore = state.evalFiles?.[1]?.name || defaultAfter;

    box.innerHTML = `
      <section class="page-head">
        <h2>评测结果</h2>
        <p>展示最近评测指标、样例结果与 before/after 对比</p>
      </section>

      <section class="metric-grid eval-kpi">
        <article class="metric-card"><span>total_cases</span><strong>${esc(summary.total_cases ?? '-')}</strong></article>
        <article class="metric-card"><span>hit_rate</span><strong>${esc(summary.hit_rate ?? '-')}</strong></article>
        <article class="metric-card"><span>answer_keyword_match_rate</span><strong>${esc(summary.answer_keyword_match_rate ?? '-')}</strong></article>
        <article class="metric-card"><span>source_keyword_match_rate</span><strong>${esc(summary.source_keyword_match_rate ?? '-')}</strong></article>
        <article class="metric-card"><span>avg_latency_seconds</span><strong>${esc(summary.avg_latency_seconds ?? '-')}</strong></article>
        <article class="metric-card"><span>avg_citation_count</span><strong>${esc(summary.avg_citation_count ?? '-')}</strong></article>
        <article class="metric-card"><span>citation_coverage_rate</span><strong>${esc(summary.citation_coverage_rate ?? '-')}</strong></article>
      </section>

      <section class="panel">
        <div class="panel-head"><h3>评测文件对比</h3><button id="eval-refresh">刷新评测结果</button></div>
        <div class="compare-grid">
          <div class="field"><label>before</label><select id="eval-before">${options}</select></div>
          <div class="field"><label>after</label><select id="eval-after">${options}</select></div>
          <div class="field actions-row"><button class="primary" id="eval-compare">对比</button></div>
        </div>
        <div id="eval-compare-out">${emptyBlock('请选择 before / after 后点击对比')}</div>
      </section>

      <section class="panel">
        <div class="panel-head"><h3>最近评测 Case 结果</h3></div>
        ${
          rows.length
            ? `<table class="table">
                <thead><tr><th>case_id</th><th>question</th><th>success</th><th>failure_reason</th><th>answer 摘要</th><th>used_original_query</th><th>used_retrieval_query</th></tr></thead>
                <tbody>
                  ${rows
                    .map(
                      (r) => `<tr>
                        <td>${esc(r.case_id || '-')}</td>
                        <td><div class="truncate" title="${esc(r.question || '')}">${esc(truncate(r.question || '-', 60))}</div></td>
                        <td>${r.success ? tag('成功', 'ok') : tag('失败', 'err')}</td>
                        <td>${esc(r.failure_reason || '-')}</td>
                        <td><div>citation_count: ${esc(r.citation_count ?? 0)}</div><div>${esc(truncate(r.answer || '-', 80))}</div><small>${esc(truncate(r.formatted_answer_preview || '-', 80))}</small></td>
                        <td>${esc(truncate(r.used_original_query || '-', 60))}</td>
                        <td>${esc(truncate(r.used_retrieval_query || '-', 60))}<div><button data-act="eval-detail" data-case-id="${esc(r.case_id || '')}">detail</button></div></td>
                      </tr>`,
                    )
                    .join('')}
                </tbody>
              </table>`
            : emptyBlock('暂无评测结果，请先生成 eval_*.json')
        }
      </section>`;

    qsa('button[data-act="eval-detail"]').forEach((btn) => {
      btn.onclick = () => {
        const caseId = btn.dataset.caseId || '';
        const row = rows.find((x) => String(x.case_id || '') === caseId) || {};
        openDrawer({
          title: `eval case detail · ${caseId || '-'}`,
          contentHtml: `
            <div class="kv-grid">
              <div><label>case_id</label><span>${esc(row.case_id || '-')}</span></div>
              <div><label>citation_count</label><span>${esc(row.citation_count ?? 0)}</span></div>
              <div><label>success</label><span>${row.success ? tag('success', 'ok') : tag('failed', 'err')}</span></div>
              <div><label>failure_reason</label><span>${esc(row.failure_reason || '-')}</span></div>
            </div>
            <div class="sub-title">formatted_answer_preview</div>
            <pre>${esc(row.formatted_answer_preview || '-')}</pre>
            <div class="sub-title">normalized_sources</div>
            ${renderSources(row.normalized_sources)}
            <div class="sub-title">answer</div>
            <pre>${esc(row.answer || '-')}</pre>
          `,
        });
      };
    });

    if (defaultBefore) qs('#eval-before').value = defaultBefore;
    if (defaultAfter) qs('#eval-after').value = defaultAfter;

    qs('#eval-refresh').onclick = refresh;
    qs('#eval-compare').onclick = async () => {
      const before = qs('#eval-before').value;
      const after = qs('#eval-after').value;
      if (!before || !after) {
        showToast('请选择 before/after 文件', 'error');
        return;
      }
      const out = qs('#eval-compare-out');
      out.innerHTML = loadingBlock('对比中...');
      try {
        const d = await api(`/api/admin/evals/compare?before=${encodeURIComponent(before)}&after=${encodeURIComponent(after)}`);
        const b = d.before?.summary || {};
        const a = d.after?.summary || {};
        const diff = d.diff || {};

        out.innerHTML = `
          <div class="grid-2">
            <div class="sub-panel">
              <h4>Before: ${esc(d.before?.meta?.name || '-')}</h4>
              <pre>${esc(JSON.stringify(b, null, 2))}</pre>
            </div>
            <div class="sub-panel">
              <h4>After: ${esc(d.after?.meta?.name || '-')}</h4>
              <pre>${esc(JSON.stringify(a, null, 2))}</pre>
            </div>
          </div>
          <div class="sub-panel">
            <h4>核心指标差值 (after - before)</h4>
            <pre>${esc(JSON.stringify(diff, null, 2))}</pre>
          </div>
          <div class="grid-2">
            <div class="sub-panel"><h4>failure_reason_counts (before)</h4><pre>${esc(JSON.stringify(d.failure_reason_counts?.before || {}, null, 2))}</pre></div>
            <div class="sub-panel"><h4>failure_reason_counts (after)</h4><pre>${esc(JSON.stringify(d.failure_reason_counts?.after || {}, null, 2))}</pre></div>
          </div>`;
      } catch (e) {
        out.innerHTML = emptyBlock('对比失败: ' + e.message);
      }
    };
  }

  try {
    await refresh();
  } catch (e) {
    box.innerHTML = emptyBlock('评测结果加载失败: ' + e.message);
  }
}

async function boot() {
  setupMenu();
  try {
    await loadDefaults();
  } catch (e) {
    showToast('默认参数加载失败: ' + e.message, 'error');
  }
  pageSwitch('dashboard');
  await renderDashboard();
}

boot();
