// ── THEME ─────────────────────────────────────────────────────────────────

  function applyTheme(theme) {
    document.body.classList.toggle('light', theme === 'light');
    const btn = document.getElementById('theme-toggle');
    if (btn) btn.textContent = theme === 'light' ? '◑' : '◐';
  }

  function toggleTheme() {
    const next = document.body.classList.contains('light') ? 'dark' : 'light';
    localStorage.setItem('theme', next);
    applyTheme(next);
  }

  // ── CONTENT ───────────────────────────────────────────────────────────────
  let CONTENT = null;

  // ── RENDERER ─────────────────────────────────────────────────────────────

  function renderEvaluator(content = null) {
    const cfg = content || CONTENT;
    if (!cfg) return;
    document.querySelector('.eyebrow').textContent  = cfg.meta.eyebrow;
    document.querySelector('h1').innerHTML          = cfg.meta.title;
    document.querySelector('.subtitle').textContent = cfg.meta.subtitle;

    // Knockouts
    document.getElementById('knockout-mount').innerHTML = `
      <div class="knockout-section">
        <div class="knockout-title">⚠ Knockout Flags — check any that apply</div>
        ${cfg.knockouts.map(ko => `
          <div class="knockout-item">
            <div class="ko-check" onclick="toggleKO(this)"></div>
            <div class="ko-label">${ko.label}<small>${ko.detail}</small></div>
          </div>
        `).join('')}
        <div id="ko-warning" class="knockout-warning">
          One or more knockout flags raised. Consider carefully before investing time in this application.
        </div>
      </div>`;

    // Dashboard
    const dashLabels = { 1: 'Technical Fit', 2: 'Environment & Culture', 3: 'Comp & FIRE', 4: 'Learning Trajectory' };
    document.getElementById('dashboard-mount').innerHTML = `
      <div class="dashboard">
        ${cfg.sections.map(s => `
          <div class="score-card">
            <div class="score-label">${dashLabels[s.id]}</div>
            <div><span class="score-value" id="s${s.id}-val">0</span><span class="score-max"> / ${s.weight}</span></div>
          </div>
        `).join('')}
      </div>
      <div class="total-bar">
        <div>
          <div class="total-label">Total Score</div>
          <div class="total-score" id="total-val">0</div>
        </div>
        <div class="progress-track">
          <div class="progress-fill" id="progress-fill"></div>
        </div>
        <div class="verdict">
          <strong id="verdict-label">—</strong>
          <span id="verdict-sub">Paste a job description above and click Evaluate.</span>
        </div>
      </div>`;

    // Sections
    document.getElementById('sections-mount').innerHTML = cfg.sections.map(s => `
      <div class="section s${s.id}">
        <div class="section-header">
          <div class="section-title">${s.title}</div>
          <div class="section-weight">Weight: ${s.weight}pts</div>
          <div class="section-score-inline" id="s${s.id}-inline">0/${s.weight}</div>
        </div>
        ${s.items.map((item, idx) => {
          const btns = Array.from({ length: item.max + 1 }, (_, i) => {
            const label = item.labels ? item.labels[i] : String(i);
            return `<button onclick="rate(this,${i})" title="${label}">${i}</button>`;
          }).join('');
          return `
            <div class="item" data-section="${s.id}" data-max="${item.max}">
              <div class="item-left">
                <div class="item-question">${item.question}</div>
                <div class="item-hint">Signals: <span class="signal">${item.signals}</span></div>
                <div class="item-reasoning">
                  <button class="reasoning-toggle" onclick="toggleReasoning(this)" style="display:none">▸ reasoning</button>
                  <div class="reasoning-text"></div>
                </div>
              </div>
              <div class="rating">${btns}</div>
            </div>`;
        }).join('')}
      </div>`).join('');
  }

  // ── INIT ──────────────────────────────────────────────────────────────────

  async function init() {
    applyTheme(localStorage.getItem('theme') || 'dark');
    try {
      const [content, queueData] = await Promise.all([
        fetch('./content.json').then(r => r.json()),
        fetch('/api/queue').then(r => r.ok ? r.json() : []).catch(() => []),
      ]);

      CONTENT    = content;
      queue      = queueData || [];
      maxScores  = Object.fromEntries(CONTENT.sections.map(s => [s.id, s.weight]));
      scores     = Object.fromEntries(CONTENT.sections.map(s => [s.id, 0]));

      renderEvaluator(content);

      items      = document.querySelectorAll('.item[data-section]');
      itemScores = new Array(items.length).fill(0);

      renderSummary();
      renderTable();
      recalc();
      setupUrlField();
    } catch (err) {
      console.error('Failed to initialize:', err);
    }
  }

  // ── USER IDENTITY ─────────────────────────────────────────────────────────

  fetch('/.auth/me')
    .then(r => r.json())
    .then(data => {
      const user = data.clientPrincipal;
      if (!user) return;
      document.getElementById('user-name').innerHTML = `logged in as <span>${user.userDetails}</span>`;
      const avatar = document.getElementById('user-avatar');
      avatar.src   = `https://github.com/${user.userDetails}.png?size=44`;
      avatar.onload = () => avatar.classList.add('loaded');
    })
    .catch(() => {});

  // ── URL FIELD SETUP ───────────────────────────────────────────────────────

  function setupUrlField() {
    // Reset fetch button to idle whenever the URL field is edited
    document.getElementById('jd-url').addEventListener('input', () => {
      const empty = document.getElementById('jd-url').value.trim() === '';
      if (empty) setFetchBtnState('idle');
    });
  }

  // ── FETCH JD BUTTON ───────────────────────────────────────────────────────

  function setFetchBtnState(state) {
    const btn = document.getElementById('fetch-btn');
    btn.classList.remove('loading', 'success', 'error');
    btn.disabled = false;

    switch (state) {
      case 'idle':    btn.textContent = '↓ Fetch JD'; break;
      case 'loading': btn.textContent = '…'; btn.classList.add('loading'); btn.disabled = true; break;
      case 'success': btn.textContent = '✓'; btn.classList.add('success'); btn.disabled = true; break;
      case 'error':   btn.textContent = '✕'; btn.classList.add('error');   btn.disabled = true; break;
    }
  }

  async function fetchJdFromUrl(url) {
    if (!url || !url.startsWith('http')) {
      showToast('Enter a valid URL first');
      return;
    }

    const jdArea = document.getElementById('jd-text');
    jdArea.value       = '';
    jdArea.placeholder = 'Fetching job description...';
    setFetchBtnState('loading');

    try {
      const res = await fetch('/api/fetch-jd', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ url }),
      });

      if (!res.ok) {
        const msg = await res.text();
        setFetchBtnState('error');
        showErrorDialog(msg || 'Could not extract a job description from that URL.');
        return;
      }

      const { text } = await res.json();
      jdArea.value = text;
      setFetchBtnState('success');
    } catch (err) {
      console.error('fetchJdFromUrl failed:', err);
      setFetchBtnState('error');
      showErrorDialog('Failed to fetch the job posting. Check your connection and try again.');
    } finally {
      jdArea.placeholder = 'Paste the full job description here...';
    }
  }

  // ── ERROR DIALOG ──────────────────────────────────────────────────────────

  function showErrorDialog(msg) {
    document.getElementById('eval-spinner').classList.add('hidden');
    document.getElementById('eval-overlay-title').textContent = 'Could Not Fetch Posting';
    document.getElementById('eval-status').textContent        = msg;
    document.getElementById('eval-status').style.color       = 'var(--danger)';
    document.getElementById('eval-dismiss-btn').style.display = '';
    document.getElementById('eval-overlay').classList.add('visible');
  }

  function closeErrorDialog() {
    const overlay = document.getElementById('eval-overlay');
    overlay.classList.remove('visible');
    // Restore spinner state for next evaluation
    document.getElementById('eval-spinner').classList.remove('hidden');
    document.getElementById('eval-overlay-title').textContent  = 'Evaluating Role';
    document.getElementById('eval-status').textContent         = 'Reading job description...';
    document.getElementById('eval-status').style.color         = '';
    document.getElementById('eval-dismiss-btn').style.display  = 'none';
  }

  // ── SCORING ───────────────────────────────────────────────────────────────

  let maxScores, scores, items, itemScores;

  function rate(btn, val) {
    const item    = btn.closest('.item');
    const allBtns = item.querySelectorAll('.rating button');
    const idx     = Array.from(items).indexOf(item);
    const max     = parseInt(item.dataset.max);

    allBtns.forEach((b, i) => {
      b.classList.remove('active', 'low', 'mid', 'high');
      if (i === val) {
        b.classList.add('active');
        if (val === 0)       b.classList.add('low');
        else if (val < max)  b.classList.add('mid');
        else                 b.classList.add('high');
      }
    });

    itemScores[idx] = val;
    recalc();
  }

  function recalc() {
    Object.keys(scores).forEach(k => scores[k] = 0);
    items.forEach((item, i) => {
      scores[parseInt(item.dataset.section)] += itemScores[i];
    });

    const actualMax = Object.fromEntries(CONTENT.sections.map(s => [s.id, 0]));
    items.forEach(item => {
      actualMax[parseInt(item.dataset.section)] += parseInt(item.dataset.max);
    });

    const weighted = {};
    CONTENT.sections.forEach(s => {
      weighted[s.id] = actualMax[s.id] > 0
        ? Math.round((scores[s.id] / actualMax[s.id]) * maxScores[s.id])
        : 0;
    });

    const total = Object.values(weighted).reduce((a, b) => a + b, 0);

    CONTENT.sections.forEach(s => {
      document.getElementById(`s${s.id}-val`).textContent    = weighted[s.id];
      document.getElementById(`s${s.id}-inline`).textContent = `${weighted[s.id]}/${s.weight}`;
    });
    document.getElementById('total-val').textContent        = total;
    document.getElementById('progress-fill').style.width    = total + '%';

    let label, sub;
    if      (total >= 80) { label = 'Strong Apply';     sub = 'High alignment across all dimensions. Prioritise this application.'; }
    else if (total >= 65) { label = 'Apply';             sub = 'Good fit with some gaps. Worth the investment.'; }
    else if (total >= 50) { label = 'Apply Selectively'; sub = 'Reasonable fit but notable gaps. Investigate before applying.'; }
    else if (total >= 35) { label = 'Weak Fit';          sub = 'Significant misalignment. Apply only if options are limited.'; }
    else                  { label = 'Pass';              sub = 'Poor fit across multiple dimensions.'; }

    document.getElementById('verdict-label').textContent = label;
    document.getElementById('verdict-sub').textContent   = sub;
  }

  function toggleKO(el) {
    el.classList.toggle('flagged');
    const anyFlagged = document.querySelectorAll('.ko-check.flagged').length > 0;
    document.getElementById('ko-warning').classList.toggle('visible', anyFlagged);
  }

  function toggleReasoning(btn) {
    const text = btn.nextElementSibling;
    const open = text.classList.toggle('visible');
    btn.textContent = open ? '▾ reasoning' : '▸ reasoning';
  }

  // ── EVALUATION ────────────────────────────────────────────────────────────

  const EVAL_STEPS = [
    { msg: 'Reading job description...',            delay: 0    },
    { msg: 'Scoring technical fit...',              delay: 4000 },
    { msg: 'Assessing culture signals...',          delay: 8000 },
    { msg: 'Calculating compensation alignment...', delay: 13000 },
    { msg: 'Finalising scores...',                  delay: 18000 },
  ];

  let evalStepTimers = [];

  function showEvalOverlay() {
    document.getElementById('eval-overlay').classList.add('visible');
    const statusEl = document.getElementById('eval-status');
    statusEl.textContent = EVAL_STEPS[0].msg;

    evalStepTimers = EVAL_STEPS.slice(1).map(step =>
      setTimeout(() => {
        statusEl.classList.add('fade');
        setTimeout(() => {
          statusEl.textContent = step.msg;
          statusEl.classList.remove('fade');
        }, 300);
      }, step.delay)
    );
  }

  function hideEvalOverlay() {
    evalStepTimers.forEach(clearTimeout);
    evalStepTimers = [];
    document.getElementById('eval-overlay').classList.remove('visible');
  }

  function applyEvaluation(result) {
    document.getElementById('extracted-company').textContent = result.company;
    document.getElementById('extracted-role').textContent    = result.role;
    const urlEl = document.getElementById('extracted-url');
    if (result.url) {
      urlEl.href        = result.url;
      urlEl.textContent = '↗ view posting';
    } else {
      urlEl.textContent = '';
    }
    document.getElementById('extracted-meta').classList.add('visible');

    const koChecks = document.querySelectorAll('.ko-check');
    result.knockouts.forEach((flagged, i) => {
      if (koChecks[i]) koChecks[i].classList.toggle('flagged', flagged);
    });
    const anyFlagged = result.knockouts.some(Boolean);
    document.getElementById('ko-warning').classList.toggle('visible', anyFlagged);

    let itemIdx = 0;
    CONTENT.sections.forEach(s => {
      const sid        = String(s.id);
      const rawScores  = result.scores[sid]    || [];
      const rawReason  = result.reasoning[sid] || [];

      s.items.forEach((_, i) => {
        const score      = rawScores[i] !== undefined ? rawScores[i] : 0;
        const reasonText = rawReason[i] || '';
        const item       = items[itemIdx];

        if (item) {
          const btn = item.querySelectorAll('.rating button')[score];
          if (btn) rate(btn, score);

          if (reasonText) {
            const toggle = item.querySelector('.reasoning-toggle');
            const text   = item.querySelector('.reasoning-text');
            toggle.style.display  = '';
            text.textContent      = reasonText;
          }
        }
        itemIdx++;
      });
    });

    document.getElementById('knockout-mount').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  let lastEvalResult = null;

  async function runEvaluation() {
    const jdText = document.getElementById('jd-text').value.trim();
    const url    = document.getElementById('jd-url').value.trim();

    if (!jdText) {
      showToast('Paste a job description or fetch one from a URL first');
      return;
    }

    const btn = document.getElementById('evaluate-btn');
    btn.disabled = true;
    showEvalOverlay();

    try {
      const res = await fetch('/api/evaluate', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ jd_text: jdText, url }),
      });

      if (!res.ok) throw new Error(`POST /api/evaluate ${res.status}`);
      const result = await res.json();

      hideEvalOverlay();
      lastEvalResult = result;
      applyEvaluation(result);
    } catch (err) {
      console.error('runEvaluation failed:', err);
      hideEvalOverlay();
      showToast('Evaluation failed — please try again');
    } finally {
      btn.disabled = false;
    }
  }

  // ── RESET ─────────────────────────────────────────────────────────────────

  function resetAll() {
    items.forEach((item, i) => {
      itemScores[i] = 0;
      item.querySelectorAll('.rating button').forEach(b => b.classList.remove('active','low','mid','high'));
      const toggle = item.querySelector('.reasoning-toggle');
      const text   = item.querySelector('.reasoning-text');
      toggle.style.display = 'none';
      text.textContent     = '';
      text.classList.remove('visible');
    });
    document.querySelectorAll('.ko-check').forEach(el => el.classList.remove('flagged'));
    document.getElementById('ko-warning').classList.remove('visible');
    document.getElementById('notes').value    = '';
    document.getElementById('jd-url').value   = '';
    document.getElementById('jd-text').value  = '';
    document.getElementById('extracted-meta').classList.remove('visible');
    lastEvalResult = null;
    setFetchBtnState('idle');
    resetInterest();
    recalc();
  }

  // ── PIPELINE STATE ────────────────────────────────────────────────────────

  const STATUS_LABELS = {
    'bookmarked':   'Bookmarked',
    'applied':      'Applied',
    'phone-screen': 'Phone Screen',
    'interview':    'Interview',
    'offer':        'Offer',
    'rejected':     'Rejected',
  };

  const INTEREST_LABELS = {
    'not-interested':      'Not Interested',
    'backburner':          'Backburner',
    'under-consideration': 'Under Consideration',
    'interested':          'Interested',
  };

  const STATUS_CLASSES = {
    'bookmarked':   'status-bookmarked',
    'applied':      'status-applied',
    'phone-screen': 'status-phone',
    'interview':    'status-interview',
    'offer':        'status-offer',
    'rejected':     'status-rejected',
  };

  function escHtml(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  let pipelineState = {
    queue:     [],
    sortKey:   'added-desc',
    activeId:  null,
    filters: {
      interest: new Set(),
      verdict:  new Set(),
      status:   new Set(),
    },
  };

  Object.defineProperty(window, 'queue', {
    get: () => pipelineState.queue,
    set: (v) => { pipelineState.queue = v; },
  });

  function getVerdictKey(total) {
    if (total >= 80) return 'strong-apply';
    if (total >= 65) return 'apply';
    if (total >= 50) return 'apply-selectively';
    if (total >= 35) return 'weak-fit';
    return 'skip';
  }

  function getFiltered() {
    const { queue, sortKey, filters } = pipelineState;
    const { interest, verdict, status } = filters;

    const filtered = queue.filter(e => {
      if (interest.size && !interest.has(e.interest || 'not-interested')) return false;
      if (verdict.size  && !verdict.has(getVerdictKey(e.total)))          return false;
      if (status.size   && !status.has(e.status   || 'bookmarked'))       return false;
      return true;
    });

    return filtered.sort((a, b) => {
      if (sortKey === 'score-desc') return b.total - a.total;
      if (sortKey === 'score-asc')  return a.total - b.total;
      if (sortKey === 'added-desc') return new Date(b.addedAt) - new Date(a.addedAt);
      if (sortKey === 'added-asc')  return new Date(a.addedAt) - new Date(b.addedAt);
      return 0;
    });
  }

  function toggleFilter(group, value) {
    const set = pipelineState.filters[group];
    set.has(value) ? set.delete(value) : set.add(value);
    renderSummary();
    renderTable();
  }

  function clearFilters() {
    pipelineState.filters.interest.clear();
    pipelineState.filters.verdict.clear();
    pipelineState.filters.status.clear();
    renderSummary();
    renderTable();
  }

  function filterByStatus(status) {
    const set = pipelineState.filters.status;
    if (set.size === 1 && set.has(status)) {
      set.clear();
    } else {
      set.clear();
      set.add(status);
    }
    renderSummary();
    renderTable();
  }

  function getVerdictClass(total) {
    if (total >= 80) return 'score-strong';
    if (total >= 65) return 'score-apply';
    if (total >= 35) return 'score-weak';
    return 'score-pass';
  }

  function getVerdictLabel(total) {
    if (total >= 80) return 'Strong Apply';
    if (total >= 65) return 'Apply';
    if (total >= 50) return 'Apply Selectively';
    if (total >= 35) return 'Weak Fit';
    return 'Skip';
  }

  function interestBadge(level) {
    const label = INTEREST_LABELS[level] || level;
    return `<span class="interest-badge ${level}">${label}</span>`;
  }

  function statusBadge(status) {
    if (!status) return '<span class="status-badge status-bookmarked">Bookmarked</span>';
    const label = STATUS_LABELS[status] || status;
    const cls   = STATUS_CLASSES[status] || '';
    return `<span class="status-badge ${cls}">${label}</span>`;
  }

  // ── TABLE RENDER ──────────────────────────────────────────────────────────

  function renderSummary() {
    const { queue, filters } = pipelineState;
    const container = document.getElementById('summary-bar');
    if (!container) return;

    if (queue.length === 0) { container.innerHTML = ''; return; }

    const avgScore = queue.length
      ? Math.round(queue.reduce((s, e) => s + e.total, 0) / queue.length)
      : 0;

    const statusCounts = {};
    queue.forEach(e => {
      const s = e.status || 'bookmarked';
      statusCounts[s] = (statusCounts[s] || 0) + 1;
    });

    const activeStatus = filters.status;
    const hasFilters = filters.interest.size || filters.verdict.size || filters.status.size;

    container.innerHTML = `
      <div class="summary-stats">
        <span class="summary-stat"><span class="summary-stat-val">${queue.length}</span> role${queue.length !== 1 ? 's' : ''}</span>
        <span class="summary-divider">·</span>
        <span class="summary-stat">avg <span class="summary-stat-val">${avgScore}</span></span>
      </div>
      <div class="summary-status-chips">
        ${Object.entries(STATUS_LABELS)
          .filter(([val]) => statusCounts[val])
          .map(([val, lbl]) => `
            <button class="summary-chip ${STATUS_CLASSES[val]}${activeStatus.has(val) ? ' active' : ''}"
                    onclick="filterByStatus('${val}')">${lbl} <span class="summary-chip-count">${statusCounts[val]}</span></button>
          `).join('')}
      </div>
      ${hasFilters ? `<button class="filter-clear-btn" onclick="clearFilters()">✕ Clear filters</button>` : ''}
    `;
  }

  function renderTable() {
    pipelineState.sortKey = document.getElementById('queue-sort-select').value;
    const filtered = getFiltered();
    const { queue, activeId, filters } = pipelineState;

    document.getElementById('queue-count').textContent = `${queue.length} role${queue.length !== 1 ? 's' : ''}`;
    document.getElementById('queue-clear-btn').classList.toggle('visible', queue.length > 0);

    const hasFilters = filters.interest.size || filters.verdict.size || filters.status.size;
    document.getElementById('filter-bar').innerHTML = `
      <div class="filter-group">
        <span class="filter-group-label">Interest</span>
        ${Object.entries(INTEREST_LABELS).map(([val, lbl]) => `
          <button class="filter-pill interest-pill ${val}${filters.interest.has(val) ? ' active' : ''}"
                  onclick="toggleFilter('interest', '${val}')">${lbl}</button>
        `).join('')}
      </div>
      <div class="filter-group">
        <span class="filter-group-label">Verdict</span>
        ${[
          ['strong-apply', 'Strong Apply'],
          ['apply', 'Apply'],
          ['apply-selectively', 'Apply Selectively'],
          ['weak-fit', 'Weak Fit'],
          ['skip', 'Skip'],
        ].map(([val, lbl]) => `
          <button class="filter-pill verdict-pill ${val}${filters.verdict.has(val) ? ' active' : ''}"
                  onclick="toggleFilter('verdict', '${val}')">${lbl}</button>
        `).join('')}
      </div>
      <div class="filter-group">
        <span class="filter-group-label">Status</span>
        ${Object.entries(STATUS_LABELS).map(([val, lbl]) => `
          <button class="filter-pill status-pill ${val}${filters.status.has(val) ? ' active' : ''}"
                  onclick="toggleFilter('status', '${val}')">${lbl}</button>
        `).join('')}
      </div>
    `;

    const container = document.getElementById('queue-container');

    if (filtered.length === 0) {
      const msg = queue.length === 0
        ? `No roles in the pipeline yet.<br>Evaluate a role below and click <strong>Add to Pipeline</strong>.`
        : `No roles match the current filters.`;
      container.innerHTML = `<div class="queue-empty">${msg}</div>`;
      return;
    }

    container.innerHTML = `
      <table class="pipeline-table">
        <thead>
          <tr>
            <th>Company</th>
            <th>Role</th>
            <th>Score</th>
            <th>Verdict</th>
            <th>Interest</th>
            <th>Status</th>
            <th>Added</th>
          </tr>
        </thead>
        <tbody>
          ${filtered.map(e => `
            <tr class="pipeline-row${activeId === e.id ? ' active' : ''}" onclick="openDrawer('${e.id}')">
              <td class="col-company">${escHtml(e.company)}</td>
              <td class="col-role">${escHtml(e.role)}</td>
              <td class="col-score ${getVerdictClass(e.total)}">${e.total}</td>
              <td class="col-verdict">${getVerdictLabel(e.total)}</td>
              <td class="col-interest">${e.interest ? interestBadge(e.interest) : '—'}</td>
              <td class="col-status">${statusBadge(e.status)}</td>
              <td class="col-added">${formatDate(e.addedAt)}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>`;
  }

  function formatDate(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
  }

  // ── DRAWER ────────────────────────────────────────────────────────────────

  function openDrawer(id) {
    const entry = pipelineState.queue.find(e => e.id === id);
    if (!entry) return;
    pipelineState.activeId = id;

    const dimLabels = { 1: 'Technical Fit', 2: 'Environment & Culture', 3: 'Comp & FIRE', 4: 'Learning Trajectory' };

    document.getElementById('drawer-inner').innerHTML = `
      <div class="drawer-header">
        <div class="drawer-header-meta">
          <div class="drawer-company">${escHtml(entry.company)}</div>
          <div class="drawer-role">${escHtml(entry.role)}</div>
        </div>
        <button class="drawer-close" onclick="closeDrawer()">✕</button>
      </div>

      ${entry.url ? `<a class="drawer-url" href="${escHtml(entry.url)}" target="_blank" rel="noopener">↗ View posting</a>` : ''}

      <div class="drawer-section">
        <div class="drawer-section-label">Interest</div>
        <div class="drawer-interest-options">
          ${Object.entries(INTEREST_LABELS).map(([val, lbl]) => `
            <button class="interest-btn${entry.interest === val ? ' active' : ''}"
                    data-level="${val}"
                    onclick="updateInterest('${id}', '${val}', this)">${lbl}</button>
          `).join('')}
        </div>
      </div>

      <div class="drawer-section">
        <div class="drawer-section-label">Status</div>
        <div class="drawer-status-options">
          ${Object.entries(STATUS_LABELS).map(([val, lbl]) => `
            <button class="status-option-btn${(entry.status || 'bookmarked') === val ? ' active' : ''}"
                    data-status="${val}"
                    onclick="updateStatus('${id}', '${val}', this)">${lbl}</button>
          `).join('')}
        </div>
      </div>

      <div class="drawer-section">
        <div class="drawer-section-label">Score</div>
        <div class="drawer-score-row">
          <span class="drawer-total ${getVerdictClass(entry.total)}">${entry.total}</span>
          <span class="drawer-verdict">${getVerdictLabel(entry.total)}</span>
        </div>
        <div class="drawer-dims">
          ${CONTENT.sections.map(s => `
            <div class="drawer-dim">
              <span class="drawer-dim-label">${dimLabels[s.id]}</span>
              <span class="drawer-dim-score">${entry.weighted[s.id]} <span class="drawer-dim-max">/ ${s.weight}</span></span>
            </div>
          `).join('')}
        </div>
      </div>

      ${entry.reasoning && Object.keys(entry.reasoning).length ? `
        <div class="drawer-section">
          <div class="drawer-section-label">AI Reasoning</div>
          ${CONTENT.sections.map(s => entry.reasoning[s.id] ? `
            <div class="drawer-reasoning-block">
              <div class="drawer-reasoning-dim">${dimLabels[s.id]}</div>
              <div class="drawer-reasoning-text">${escHtml(entry.reasoning[s.id])}</div>
            </div>
          ` : '').join('')}
        </div>
      ` : ''}

      ${entry.notes ? `
        <div class="drawer-section">
          <div class="drawer-section-label">Notes</div>
          <div class="drawer-notes">${escHtml(entry.notes)}</div>
        </div>
      ` : ''}

      <div class="drawer-section drawer-actions">
        <button class="qcb-confirm" onclick="removeFromQueue('${entry.id}')">Remove from pipeline</button>
      </div>
    `;

    document.getElementById('drawer').classList.add('open');
    document.getElementById('drawer-backdrop').classList.add('open');

    renderSummary();
    renderTable();
  }

  function closeDrawer() {
    pipelineState.activeId = null;
    document.getElementById('drawer').classList.remove('open');
    document.getElementById('drawer-backdrop').classList.remove('open');
    renderSummary();
    renderTable();
  }

  async function updateInterest(id, level, btn) {
    const entry = pipelineState.queue.find(e => e.id === id);
    if (!entry) return;
    entry.interest = level;
    btn.closest('.drawer-interest-options').querySelectorAll('.interest-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    renderSummary();
    renderTable();
    await patchEntry(id, { interest: level });
  }

  async function updateStatus(id, status, btn) {
    const entry = pipelineState.queue.find(e => e.id === id);
    if (!entry) return;
    entry.status = status;
    btn.closest('.drawer-status-options').querySelectorAll('.status-option-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    renderSummary();
    renderTable();
    await patchEntry(id, { status });
  }

  async function patchEntry(id, patch) {
    try {
      await fetch(`/api/queue/${id}`, {
        method:  'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(patch),
      });
    } catch (err) {
      console.error('patchEntry failed:', err);
      showToast('Failed to save change');
    }
  }

  // ── QUEUE MUTATIONS ───────────────────────────────────────────────────────

  const INTEREST_LEVELS = ['not-interested', 'backburner', 'under-consideration', 'interested'];
  let currentInterest = 'interested';

  function setInterest(btn, level) {
    currentInterest = level;
    document.querySelectorAll('.interest-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
  }

  function resetInterest() {
    currentInterest = 'interested';
    document.querySelectorAll('.interest-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.level === 'interested');
    });
  }

  async function addToQueue() {
    if (!lastEvalResult) {
      showToast('Evaluate a role first');
      return;
    }

    const actualMax  = Object.fromEntries(CONTENT.sections.map(s => [s.id, 0]));
    items.forEach(item => {
      actualMax[parseInt(item.dataset.section)] += parseInt(item.dataset.max);
    });
    const sectScores = Object.fromEntries(CONTENT.sections.map(s => [s.id, 0]));
    items.forEach((item, i) => {
      sectScores[parseInt(item.dataset.section)] += itemScores[i];
    });
    const weighted = {};
    CONTENT.sections.forEach(s => {
      weighted[s.id] = actualMax[s.id] > 0
        ? Math.round((sectScores[s.id] / actualMax[s.id]) * maxScores[s.id])
        : 0;
    });
    const total = Object.values(weighted).reduce((a, b) => a + b, 0);

    const payload = {
      company:   lastEvalResult.company,
      role:      lastEvalResult.role,
      url:       lastEvalResult.url || '',
      total,
      weighted,
      reasoning: lastEvalResult.reasoning,
      interest:  currentInterest,
      status:    'bookmarked',
      notes:     document.getElementById('notes').value.trim(),
      addedAt:   new Date().toISOString(),
    };

    try {
      const res = await fetch('/api/queue', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(payload),
      });
      if (!res.ok) throw new Error(`POST /api/queue ${res.status}`);
      const saved = await res.json();
      pipelineState.queue.push(saved);
      renderSummary();
      renderTable();
      showToast(`${saved.company} · ${saved.role} added to pipeline`);
      resetInterest();
      resetAll();
    } catch (err) {
      console.error('addToQueue failed:', err);
      showToast('Failed to save — please try again');
    }
  }

  async function removeFromQueue(id) {
    try {
      const res = await fetch(`/api/queue/${id}`, { method: 'DELETE' });
      if (!res.ok) throw new Error(`DELETE /api/queue/${id} ${res.status}`);
      pipelineState.queue = pipelineState.queue.filter(e => e.id !== id);
      if (pipelineState.activeId === id) closeDrawer();
      renderSummary();
      renderTable();
    } catch (err) {
      console.error('removeFromQueue failed:', err);
      showToast('Failed to remove — please try again');
    }
  }

  // ── TOAST ─────────────────────────────────────────────────────────────────

  let toastTimer;
  function showToast(msg) {
    const el = document.getElementById('queue-toast');
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), 3000);
  }

  // ── CLEAR PIPELINE ────────────────────────────────────────────────────────

  function promptClearQueue() {
    document.getElementById('queue-confirm-bar').classList.add('visible');
  }

  function cancelClearQueue() {
    document.getElementById('queue-confirm-bar').classList.remove('visible');
  }

  async function confirmClearQueue() {
    try {
      const res = await fetch('/api/queue', { method: 'DELETE' });
      if (!res.ok) throw new Error(`DELETE /api/queue ${res.status}`);
      pipelineState.queue = [];
      cancelClearQueue();
      renderSummary();
      renderTable();
    } catch (err) {
      console.error('confirmClearQueue failed:', err);
      showToast('Failed to clear pipeline');
    }
  }

  init();