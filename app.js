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

      renderQueue();
      recalc();
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
    { msg: 'Reading job description...',          delay: 0    },
    { msg: 'Scoring technical fit...',            delay: 4000 },
    { msg: 'Assessing culture signals...',        delay: 8000 },
    { msg: 'Calculating compensation alignment...', delay: 13000 },
    { msg: 'Finalising scores...',                delay: 18000 },
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
    // Pre-fill extracted company / role
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

    // Pre-fill knockout checkboxes
    const koChecks = document.querySelectorAll('.ko-check');
    result.knockouts.forEach((flagged, i) => {
      if (koChecks[i]) koChecks[i].classList.toggle('flagged', flagged);
    });
    const anyFlagged = result.knockouts.some(Boolean);
    document.getElementById('ko-warning').classList.toggle('visible', anyFlagged);

    // Pre-fill rating buttons and reasoning
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
          // Set rating button
          const btn = item.querySelectorAll('.rating button')[score];
          if (btn) rate(btn, score);

          // Set reasoning
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

    // Scroll to scoring area
    document.getElementById('knockout-mount').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  // Store last evaluation result for addToQueue
  let lastEvalResult = null;

  async function runEvaluation() {
    const jdText = document.getElementById('jd-text').value.trim();
    if (!jdText) {
      showToast('Paste a job description first');
      return;
    }

    const btn = document.getElementById('evaluate-btn');
    btn.disabled = true;
    showEvalOverlay();

    try {
      const res = await fetch('/api/evaluate', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
          jd_text: jdText,
          url:     document.getElementById('jd-url').value.trim(),
        }),
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
    resetInterest();
    recalc();
  }

  // ── QUEUE ─────────────────────────────────────────────────────────────────

  let queue = [];

  const INTEREST_LABELS = {
    'not-interested':      'Not Interested',
    'backburner':          'Backburner',
    'under-consideration': 'Under Consideration',
    'interested':          'Interested',
  };

  function interestBadge(level) {
    const label = INTEREST_LABELS[level] || level;
    return `<div class="interest-badge ${level}">${label}</div>`;
  }

  const INTEREST_LEVELS = ['not-interested', 'considering', 'pursuing', 'interested'];
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
      queue.push(saved);
      renderQueue();
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
      queue = queue.filter(e => e.id !== id);
      renderQueue();
    } catch (err) {
      console.error('removeFromQueue failed:', err);
      showToast('Failed to remove — please try again');
    }
  }

  function renderQueue() {
    const sort   = document.getElementById('queue-sort-select').value;
    const sorted = [...queue].sort((a, b) => {
      if (sort === 'score-desc') return b.total - a.total;
      if (sort === 'score-asc')  return a.total - b.total;
      if (sort === 'added-desc') return new Date(b.addedAt) - new Date(a.addedAt);
      if (sort === 'added-asc')  return new Date(a.addedAt) - new Date(b.addedAt);
      return 0;
    });

    document.getElementById('queue-count').textContent = `${queue.length} role${queue.length !== 1 ? 's' : ''}`;
    document.getElementById('queue-clear-btn').classList.toggle('visible', queue.length > 0);

    const container = document.getElementById('queue-container');
    if (sorted.length === 0) {
      container.innerHTML = `<div class="queue-empty">No roles queued yet.<br>Evaluate a role below and click <strong>Add to Pipeline</strong>.</div>`;
      return;
    }

    const dimLabels = Object.fromEntries(CONTENT.sections.map(s => [s.id, s.title.split(' ')[0]]));
    container.innerHTML = `<div class="queue-grid">${sorted.map(e => `
      <div class="queue-card">
        <div class="queue-card-top">
          <div>
            <div class="queue-card-company">${escHtml(e.company)}</div>
            <div class="queue-card-role">${escHtml(e.role)}</div>
            ${e.interest ? interestBadge(e.interest) : ''}
          </div>
          <div>
            <div class="queue-card-score ${getVerdictClass(e.total)}">${e.total}</div>
            <div class="queue-card-verdict">${getVerdictLabel(e.total)}</div>
          </div>
        </div>
        <div class="queue-card-breakdown">
          ${CONTENT.sections.map(s => `<span class="qc-dim">${dimLabels[s.id]} ${e.weighted[s.id]}/${s.weight}</span>`).join('')}
        </div>
        ${e.notes ? `<div class="queue-card-notes">${escHtml(e.notes)}</div>` : ''}
        <div class="queue-card-actions">
          ${e.url ? `<a href="${escHtml(e.url)}" target="_blank" rel="noopener" class="qc-remove" style="text-decoration:none">↗ posting</a>` : ''}
          <button class="qc-remove" onclick="removeFromQueue('${e.id}')">Remove</button>
        </div>
      </div>
    `).join('')}</div>`;
  }

  function escHtml(str) {
    return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  let toastTimer;
  function showToast(msg) {
    const toast = document.getElementById('queue-toast');
    toast.textContent = msg;
    toast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('show'), 2400);
  }

  function promptClearQueue() {
    document.getElementById('queue-confirm-bar').classList.add('visible');
    document.getElementById('queue-clear-btn').style.display = 'none';
  }

  function cancelClearQueue() {
    document.getElementById('queue-confirm-bar').classList.remove('visible');
    document.getElementById('queue-clear-btn').style.display = '';
  }

  async function confirmClearQueue() {
    try {
      const res = await fetch('/api/queue', { method: 'DELETE' });
      if (!res.ok) throw new Error(`DELETE /api/queue ${res.status}`);
      queue = [];
      document.getElementById('queue-confirm-bar').classList.remove('visible');
      renderQueue();
      showToast('Pipeline cleared');
    } catch (err) {
      console.error('confirmClearQueue failed:', err);
      showToast('Failed to clear — please try again');
    }
  }

  init();