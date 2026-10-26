/* Daily QC Operation Report - shared front-end helpers */
const QC = (() => {
  const css = getComputedStyle(document.documentElement);
  const C = {
    ink: '#14213D', mute: '#5B6677', rule: '#E6EAF0', blue: '#1F4FD8',
    ok: '#18794E', ng: '#C4281C', nc: '#E0A100', prod: '#8A97AD', check: '#3D5A99',
  };
  const fmt = n => (n == null ? '–' : Number(n).toLocaleString('en-US'));
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const data = id => { const el = document.getElementById(id); return el ? JSON.parse(el.textContent) : null; };
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  if (window.Chart) {
    Chart.defaults.font.family = css.getPropertyValue('--text') || 'Tahoma, sans-serif';
    Chart.defaults.font.size = 12.5;
    Chart.defaults.color = C.mute;
    Chart.defaults.maintainAspectRatio = false;
    Chart.defaults.animation = false;
    Chart.defaults.plugins.legend.position = 'bottom';
    Chart.defaults.plugins.legend.labels.boxWidth = 12;
    Chart.defaults.plugins.legend.labels.boxHeight = 12;
    Chart.defaults.plugins.tooltip.backgroundColor = C.ink;
    Chart.defaults.scale.grid.color = C.rule;
  }
  const mk = (id, cfg) => { const el = document.getElementById(id); return el && window.Chart ? new Chart(el, cfg) : null; };
  const pctAxis = { position: 'right', beginAtZero: true, grid: { drawOnChartArea: false }, ticks: { callback: v => v + '%' } };

  /* Writes each value on the chart itself, so nobody has to hover to read it.
     Bars: the number above the bar (or at the end of a horizontal bar). Doughnut: the number inside each slice. */
  const opts_total_label = document.documentElement.lang === 'en' ? 'cases' : 'เคส';
  const valueLabels = {
    id: 'valueLabels',
    afterDatasetsDraw(chart, _args, o) {
      const { ctx } = chart;
      const horizontal = chart.options.indexAxis === 'y';
      ctx.save();
      ctx.font = `600 ${o.size || 12}px ${Chart.defaults.font.family}`;
      chart.data.datasets.forEach((ds, i) => {
        const meta = chart.getDatasetMeta(i);
        if (meta.hidden || (o.types && !o.types.includes(meta.type))) return;
        meta.data.forEach((el, j) => {
          const v = ds.data[j];
          if (v == null || v === 0) return;
          const txt = String(v);
          if (meta.type === 'doughnut' || meta.type === 'pie') {
            const p = el.tooltipPosition();
            ctx.fillStyle = '#fff'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
            ctx.font = `700 ${o.size || 15}px ${Chart.defaults.font.family}`;
            ctx.fillText(txt, p.x, p.y);
          } else if (horizontal) {
            ctx.fillStyle = C.ink; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
            ctx.fillText(txt, el.x + 4, el.y);
          } else {
            ctx.fillStyle = C.ink; ctx.textAlign = 'center'; ctx.textBaseline = 'bottom';
            ctx.fillText(txt, el.x, el.y - 3);
          }
        });
      });
      ctx.restore();
    },
  };

  const charts = {
    /* OK / NG / NC stacked by period, with % NG and % NC lines */
    /* opts.rates = false leaves out the % NG / % NC lines */
    trend(id, t, opts = {}) {
      const rates = opts.rates !== false;
      const sets = [
        { type: 'bar', label: 'OK', data: t.ok, backgroundColor: C.ok, stack: 's' },
        { type: 'bar', label: 'NG', data: t.ng, backgroundColor: C.ng, stack: 's' },
        { type: 'bar', label: 'NC', data: t.nc, backgroundColor: C.nc, stack: 's' },
      ];
      if (rates) sets.push(
        { type: 'line', label: '% NG', data: t.ng_pct, yAxisID: 'p', borderColor: C.ng, backgroundColor: C.ng, borderWidth: 2, pointRadius: 2.5, spanGaps: true },
        { type: 'line', label: '% NC', data: t.nc_pct, yAxisID: 'p', borderColor: '#8F6200', backgroundColor: '#8F6200', borderWidth: 2, pointRadius: 2.5, spanGaps: true });
      const scales = { x: { stacked: true, grid: { display: false } }, y: { stacked: true, beginAtZero: true, title: { display: true, text: 'pcs' } } };
      if (rates) scales.p = pctAxis;
      return mk(id, { data: { labels: t.labels, datasets: sets }, options: { interaction: { mode: 'index', intersect: false }, scales } });
    },
    prodCheck(id, t) {
      return mk(id, {
        type: 'bar',
        data: { labels: t.labels, datasets: [
          { label: 'Production', data: t.production, backgroundColor: C.prod },
          { label: 'QC Check', data: t.qc_check, backgroundColor: C.check },
        ] },
        options: { interaction: { mode: 'index', intersect: false }, scales: { x: { grid: { display: false } }, y: { beginAtZero: true } } },
      });
    },
    /* Pareto: items = [{name, ng, nc, cum_pct}] or [{name, count, cum_pct}] */
    /* opts: max (how many items, default 12), single + label (one bar series), labelLen, values (numbers on bars) */
    pareto(id, items, opts = {}) {
      const top = items.slice(0, opts.max || 12), cut = opts.labelLen || 22;
      const sets = opts.single
        ? [{ type: 'bar', label: opts.label || 'Cases', data: top.map(d => d.count), backgroundColor: C.ng }]
        : [{ type: 'bar', label: 'NG', data: top.map(d => d.ng), backgroundColor: C.ng, stack: 's' },
           { type: 'bar', label: 'NC', data: top.map(d => d.nc), backgroundColor: C.nc, stack: 's' }];
      sets.push({ type: 'line', label: 'สะสม %', data: top.map(d => d.cum_pct), yAxisID: 'p', borderColor: C.ink, backgroundColor: C.ink, borderWidth: 2, pointRadius: 3 });
      return mk(id, { data: { labels: top.map(d => d.name), datasets: sets }, plugins: opts.values ? [valueLabels] : [],
        options: { layout: { padding: { top: opts.values ? 18 : 0 } },
          plugins: { valueLabels: { types: ['bar'] }, tooltip: { callbacks: { title: it => it[0]?.label } } },
          scales: { x: { stacked: true, grid: { display: false }, ticks: { maxRotation: 60, minRotation: 0, autoSkip: false, callback(v) { const s = this.getLabelForValue(v); return s.length > cut ? s.slice(0, cut - 1) + '…' : s; } } },
          y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } }, p: { ...pctAxis, max: 100 } } } });
    },
    /* horizontal stacked OK/NG/NC per category (customer, line, part) */
    hstack(id, rows, labelKey = 'code') {
      return mk(id, { type: 'bar',
        data: { labels: rows.map(r => r[labelKey]), datasets: [
          { label: 'OK', data: rows.map(r => r.ok), backgroundColor: C.ok }, { label: 'NG', data: rows.map(r => r.ng), backgroundColor: C.ng },
          { label: 'NC', data: rows.map(r => r.nc), backgroundColor: C.nc }] },
        options: { indexAxis: 'y', scales: { x: { stacked: true, beginAtZero: true }, y: { stacked: true, grid: { display: false }, ticks: { autoSkip: false } } } } });
    },
    /* NG + NC only, horizontal - for "worst parts" */
    hbad(id, rows, labelKey) {
      return mk(id, { type: 'bar',
        data: { labels: rows.map(r => r[labelKey]), datasets: [
          { label: 'NG', data: rows.map(r => r.ng), backgroundColor: C.ng }, { label: 'NC', data: rows.map(r => r.nc), backgroundColor: C.nc }] },
        options: { indexAxis: 'y', scales: { x: { stacked: true, beginAtZero: true, ticks: { precision: 0 } }, y: { stacked: true, grid: { display: false }, ticks: { autoSkip: false } } } } });
    },
    rateBars(id, rows, labelKey = 'code') {
      return mk(id, { type: 'bar',
        data: { labels: rows.map(r => r[labelKey]), datasets: [
          { label: '% NG', data: rows.map(r => r.ng_pct), backgroundColor: C.ng }, { label: '% NC', data: rows.map(r => r.nc_pct), backgroundColor: C.nc }] },
        options: { scales: { x: { grid: { display: false } }, y: { beginAtZero: true, ticks: { callback: v => v + '%' } } } } });
    },
    lines(id, labels, series, yPct) {
      return mk(id, { type: 'line',
        data: { labels, datasets: series.map(s => ({ label: s.label, data: s.data, borderColor: s.color, backgroundColor: s.color, borderWidth: 2, pointRadius: 2.5, spanGaps: true, tension: .15 })) },
        options: { interaction: { mode: 'index', intersect: false }, scales: { x: { grid: { display: false } }, y: { beginAtZero: !yPct, ticks: yPct ? { callback: v => v + '%' } : { precision: 0 } } } } });
    },
    grouped(id, labels, series, stacked) {
      return mk(id, { type: 'bar',
        data: { labels, datasets: series.map(s => ({ label: s.label, data: s.data, backgroundColor: s.color })) },
        options: { scales: { x: { stacked: !!stacked, grid: { display: false } }, y: { stacked: !!stacked, beginAtZero: true, ticks: { precision: 0 } } } } });
    },
    /* the count is printed inside every slice and next to its name in the legend */
    donut(id, labels, values, colors) {
      const total = values.reduce((a, b) => a + (+b || 0), 0);
      return mk(id, { type: 'doughnut', plugins: [valueLabels, {
          id: 'centerTotal',
          afterDraw(chart) {
            const m = chart.getDatasetMeta(0).data[0]; if (!m) return;
            const { ctx } = chart; ctx.save(); ctx.textAlign = 'center'; ctx.fillStyle = C.ink;
            ctx.font = `700 22px ${Chart.defaults.font.family}`; ctx.textBaseline = 'bottom'; ctx.fillText(total, m.x, m.y + 4);
            ctx.font = `12px ${Chart.defaults.font.family}`; ctx.fillStyle = C.mute; ctx.textBaseline = 'top'; ctx.fillText(opts_total_label, m.x, m.y + 6);
            ctx.restore();
          } }],
        data: { labels: labels.map((l, i) => `${l}  ${values[i] || 0}`), datasets: [{ data: values, backgroundColor: colors, borderColor: '#fff', borderWidth: 2 }] },
        options: { cutout: '52%', plugins: { legend: { position: 'right', labels: { font: { size: 13 } } } } } });
    },
  };

  /* ---- shell behaviour */
  document.addEventListener('DOMContentLoaded', () => {
    // ---- side menu: a drawer on phones and tablets, collapsible on wide screens
    const root = document.documentElement, rail = $('#rail');
    const wide = () => window.matchMedia('(min-width:1200px)').matches;
    const closeRail = () => root.classList.remove('rail-open');
    $('#menuBtn')?.addEventListener('click', e => {
      e.stopPropagation();
      if (wide()) {
        const closed = root.classList.toggle('rail-closed');
        try { localStorage.setItem('qcRail', closed ? 'closed' : 'open'); } catch (_) { /* private mode */ }
        window.dispatchEvent(new Event('resize'));          // let charts take the new width
      } else root.classList.toggle('rail-open');
    });
    $('#railClose')?.addEventListener('click', closeRail);
    $('#scrim')?.addEventListener('click', closeRail);
    rail?.addEventListener('click', e => { if (e.target.closest('nav a')) closeRail(); });
    window.addEventListener('resize', () => { if (wide()) closeRail(); });

    // account menu closes when you click elsewhere
    const um = $('#userMenu');
    document.addEventListener('click', e => { if (um?.open && !um.contains(e.target)) um.open = false; });

    // confirmations
    document.addEventListener('submit', e => {
      const m = e.target.dataset.confirm || e.submitter?.dataset.confirm;
      if (m && !confirm(m)) e.preventDefault();
    });

    // date filter: editing a date switches to a custom range
    $$('.filter form').forEach(f => {
      $$('input[type=date]', f).forEach(i => i.addEventListener('change', () => { const p = f.querySelector('[name=p]'); if (p) p.value = 'custom'; }));
      $$('select[data-auto]', f).forEach(s => s.addEventListener('change', () => f.submit()));
    });

    // clock
    const clk = $('#clock');
    if (clk) {
      const tick = () => { const d = new Date(); clk.textContent = d.toLocaleTimeString('th-TH', { hour: '2-digit', minute: '2-digit' }); };
      tick(); setInterval(tick, 20000);
    }

    // lightbox
    const lb = $('#lightbox');
    document.addEventListener('click', e => {
      const im = e.target.closest('img[data-full]');
      if (im && lb) { lb.querySelector('img').src = im.dataset.full; lb.classList.add('open'); }
      else if (lb && e.target.closest('#lightbox')) lb.classList.remove('open');
    });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') { lb?.classList.remove('open'); $('#drawer')?.classList.remove('open'); closeRail(); if (um) um.open = false; } });

    // notices
    const bell = $('#bell'), drawer = $('#drawer');
    const badge = n => { const b = $('#bellBadge'); if (b) { b.textContent = n > 99 ? '99+' : n; b.hidden = !n; } };
    async function loadNotices(markRead) {
      try {
        const r = await fetch('/api/notices'); if (!r.ok) return;
        const j = await r.json(); badge(j.unread);
        if (!markRead) return;
        $('#noticeList').innerHTML = j.items.length ? j.items.map(n =>
          `<a class="notice ${esc(n.level)} ${n.new ? 'new' : ''}" href="${esc(n.link || '#')}"><time>${esc(n.at)}</time><b>${esc(n.title)}</b>${n.body ? `<pre>${esc(n.body)}</pre>` : ''}</a>`).join('')
          : '<div class="empty"><b>ยังไม่มีการแจ้งเตือน</b>เมื่อมีเคสใหม่หรือ NG เกินเกณฑ์ จะแสดงที่นี่</div>';
        if (j.unread) { await fetch('/api/notices/read', { method: 'POST' }); badge(0); }
      } catch (_) { /* offline: ignore */ }
    }
    bell?.addEventListener('click', e => { e.stopPropagation(); drawer.classList.toggle('open'); if (drawer.classList.contains('open')) loadNotices(true); });
    $('#drawerClose')?.addEventListener('click', () => drawer.classList.remove('open'));
    document.addEventListener('click', e => { if (drawer?.classList.contains('open') && !drawer.contains(e.target)) drawer.classList.remove('open'); });
    // every 90 s ask the server whether anything is new; the full list is only fetched when it is
    if (bell) setInterval(async () => {
      if (document.hidden) return;
      try { const r = await fetch('/api/ping'); if (r.ok && (await r.json()).new) loadNotices(false); } catch (_) { /* offline */ }
    }, 90000);
  });

  return { C, fmt, $, $$, data, esc, charts, mk, valueLabels };
})();
