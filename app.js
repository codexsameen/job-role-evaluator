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
  let _currentProfile = null;

  // ── INIT ──────────────────────────────────────────────────────────────────

  async function init() {
    applyTheme(localStorage.getItem('theme') || 'dark');
    try {
      const [profile, queueData] = await Promise.all([
        fetch('/api/profile').then(r => r.ok ? r.json() : null).catch(() => null),
        fetch('/api/queue').then(r => r.ok ? r.json() : []).catch(() => []),
      ]);

      _currentProfile = profile;

      if (profile?.displayName) {
        document.querySelector('.eyebrow').textContent = profile.displayName;
      }

      CONTENT = profile?.rubric || null;
      document.getElementById('view-rubric-btn').hidden = !CONTENT;

      queue = queueData || [];
      renderSummary();
      renderTable();
      setupUrlField();

      if (profile?.rubricStatus === 'generating') {
        // Page reloaded mid-generation — resume polling without showing the modal
        setEvaluateBtnState(true, 'Generating rubric…');
        startRubricProgress();
        _pollForRubric();
      } else if (!profile?.rubricGeneratedAt) {
        showProfileModal(profile);
      }
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

  // ── PROFILE MODAL ─────────────────────────────────────────────────────────

  function showProfileModal(profile) {
    const overlay = document.getElementById('profile-overlay');
    if (!overlay) return;

    // Pre-fill fields if a partial profile exists
    if (profile) {
      const set = (id, val) => { if (val && document.getElementById(id)) document.getElementById(id).value = val; };
      set('pf-role-title', profile.roleTitle);
      set('pf-location',   profile.location);
      set('pf-currency',   profile.currencySymbol);
      set('pf-comp-min',   profile.compMin);
      set('pf-comp-max',   profile.compMax);
      set('pf-skills',     profile.skills);
      set('pf-background', profile.backgroundSummary);
      set('pf-goals',      profile.careerGoals);
      set('pf-company-size', profile.companySizePreference);
      set('pf-arrangement',  profile.workArrangement);
    }

    // Hide cancel button on first-time setup (no rubric yet)
    const hasRubric = !!(profile?.rubricGeneratedAt);
    const cancelBtn = document.getElementById('profile-cancel-btn');
    if (cancelBtn) cancelBtn.style.display = hasRubric ? '' : 'none';

    overlay.classList.add('visible');
  }

  function closeProfileModal() {
    document.getElementById('profile-overlay')?.classList.remove('visible');
    setProfileStatus('');
  }

  function setProfileStatus(msg, isError) {
    const el = document.getElementById('profile-status');
    if (!el) return;
    el.textContent = msg;
    el.style.display = msg ? '' : 'none';
    el.style.color = isError ? 'var(--danger)' : 'var(--muted)';
  }

  async function saveProfile() {
    const btn     = document.getElementById('generate-rubric-btn');
    const spinner = document.getElementById('profile-spinner');

    const profileData = {
      roleTitle:             document.getElementById('pf-role-title')?.value.trim(),
      location:              document.getElementById('pf-location')?.value.trim(),
      currencySymbol:        document.getElementById('pf-currency')?.value,
      compMin:               document.getElementById('pf-comp-min')?.value.trim(),
      compMax:               document.getElementById('pf-comp-max')?.value.trim(),
      skills:                document.getElementById('pf-skills')?.value.trim(),
      backgroundSummary:     document.getElementById('pf-background')?.value.trim(),
      careerGoals:           document.getElementById('pf-goals')?.value.trim(),
      companySizePreference: document.getElementById('pf-company-size')?.value,
      workArrangement:       document.getElementById('pf-arrangement')?.value,
    };

    if (!profileData.roleTitle || !profileData.location) {
      setProfileStatus('Role title and location are required.', true);
      return;
    }

    btn.disabled = true;
    btn.textContent = 'Saving…';
    spinner?.classList.remove('hidden');
    setProfileStatus('Saving profile…');

    try {
      // Save profile fields
      const saveRes = await fetch('/api/profile', {
        method:  'PUT',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(profileData),
      });
      if (!saveRes.ok) throw new Error(`PUT /api/profile ${saveRes.status}`);
      const savedProfile = await saveRes.json();

      // Update eyebrow and cached profile
      _currentProfile = savedProfile;
      if (savedProfile.displayName) {
        document.querySelector('.eyebrow').textContent = savedProfile.displayName;
      }

      setProfileStatus('Generating your personalised rubric…');
      btn.textContent = 'Generating…';

      // Kick off rubric generation — returns 202 immediately, runs in background
      const rubrRes = await fetch('/api/profile/generate-rubric', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      });
      if (rubrRes.status !== 202) {
        const msg = await rubrRes.text();
        throw new Error(msg || 'Rubric generation failed');
      }

      // Close modal immediately and disable Evaluate while rubric generates
      closeProfileModal();
      showToast('Profile saved — generating your rubric…');
      setEvaluateBtnState(true, 'Generating rubric…');
      startRubricProgress();
      _pollForRubric();  // fire-and-forget

    } catch (err) {
      console.error('saveProfile failed:', err);
      setProfileStatus(`Error: ${err.message}`, true);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Generate My Rubric';
      spinner?.classList.add('hidden');
    }
  }

  function setEvaluateBtnState(disabled, label) {
    const btn = document.getElementById('evaluate-btn');
    if (!btn) return;
    btn.disabled = disabled;
    btn.textContent = label;
  }

  async function _pollForRubric() {
    try {
      let rubric = null;
      for (let i = 0; i < 40; i++) {
        await new Promise(r => setTimeout(r, 3000));
        const pollRes = await fetch('/api/profile');
        if (!pollRes.ok) continue;
        const polled = await pollRes.json();
        if (polled.rubricStatus === 'error') {
          resetRubricProgress();
          showToast('Rubric generation failed — open profile to retry');
          setEvaluateBtnState(false, 'Evaluate Role');
          return;
        }
        if (polled.rubricStatus === 'done' && polled.rubric) {
          rubric = polled.rubric;
          break;
        }
      }
      if (!rubric) {
        resetRubricProgress();
        showToast('Rubric generation timed out — open profile to retry');
        setEvaluateBtnState(false, 'Evaluate Role');
        return;
      }
      CONTENT = rubric;
      document.getElementById('view-rubric-btn').hidden = false;
      finishRubricProgress();
      showToast('Rubric ready — start evaluating!');
      setEvaluateBtnState(false, 'Evaluate Role');
    } catch (err) {
      console.error('_pollForRubric failed:', err);
      resetRubricProgress();
      showToast('Rubric generation failed — open profile to retry');
      setEvaluateBtnState(false, 'Evaluate Role');
    }
  }

  // ── EVALUATION ────────────────────────────────────────────────────────────

  function evalStatusBadge(status) {
    if (status === 'pending') return '<span class="eval-badge eval-pending">Awaiting</span>';
    if (status === 'error')   return '<span class="eval-badge eval-error">Error</span>';
    return '<span class="eval-badge eval-done">Evaluated</span>';
  }

  async function runEvaluation() {
    const jdText = document.getElementById('jd-text').value.trim();
    const url    = document.getElementById('jd-url').value.trim();

    if (!jdText) {
      showToast('Paste a job description or fetch one from a URL first');
      return;
    }

    // POST pending entry with defaults
    const pendingPayload = {
      company:    '—',
      role:       'Evaluating…',
      url:        url || '',
      total:      0,
      weighted:   Object.fromEntries(CONTENT.sections.map(s => [s.id, 0])),
      scores:     {},
      reasoning:  {},
      knockouts:  [],
      interest:   'under-consideration',
      status:     'bookmarked',
      notes:      '',
      addedAt:    new Date().toISOString(),
      evalStatus: 'pending',
    };

    let pendingEntry;
    try {
      const res = await fetch('/api/queue', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(pendingPayload),
      });
      if (!res.ok) throw new Error(`POST /api/queue ${res.status}`);
      pendingEntry = await res.json();
    } catch (err) {
      showToast('Failed to queue role — please try again');
      return;
    }

    pipelineState.queue.push(pendingEntry);
    renderSummary();
    renderTable();
    showToast('Role queued for evaluation');

    const evalBar = _createProgressBar('Evaluating role\u2026', 10);
    try {
      const res = await fetch('/api/evaluate', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ jd_text: jdText, url }),
      });
      if (!res.ok) throw new Error(`POST /api/evaluate ${res.status}`);
      const result = await res.json();

      const patch = {
        company:    result.company,
        role:       result.role,
        url:        result.url || url || '',
        total:      result.total,
        weighted:   result.weighted,
        scores:     result.scores,
        reasoning:  result.reasoning,
        knockouts:  result.knockouts,
        evalStatus: 'evaluated',
      };
      await patchEntry(pendingEntry.id, patch);
      const entry = pipelineState.queue.find(e => e.id === pendingEntry.id);
      if (entry) Object.assign(entry, patch);
      evalBar.finish();
      renderSummary();
      renderTable();

      // Clear inputs for next evaluation
      document.getElementById('jd-text').value = '';
      document.getElementById('jd-url').value  = '';
      setFetchBtnState('idle');

    } catch (err) {
      console.error('runEvaluation failed:', err);
      evalBar.reset();
      const patch = { evalStatus: 'error' };
      await patchEntry(pendingEntry.id, patch);
      const entry = pipelineState.queue.find(e => e.id === pendingEntry.id);
      if (entry) Object.assign(entry, patch);
      renderSummary();
      renderTable();
      showToast('Evaluation failed — please try again');
    }
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
        ? `No roles in the pipeline yet.<br>Evaluate a role below to get started.`
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
              <td class="col-score ${e.evalStatus === 'pending' ? '' : getVerdictClass(e.total)}">${e.evalStatus === 'pending' ? '—' : e.total}</td>
              <td class="col-verdict">${e.evalStatus === 'pending' ? '' : getVerdictLabel(e.total)}</td>
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

  function barColor(pct) {
    // 0% → red (hsl 0), 100% → teal (hsl 150), muted saturation to match dark theme
    return `hsl(${Math.round(pct * 1.45)}, 52%, 46%)`;
  }

  function openDrawer(id) {
    const entry = pipelineState.queue.find(e => e.id === id);
    if (!entry) return;
    pipelineState.activeId = id;

    const dimLabels = CONTENT.sections.reduce((acc, s) => ({ ...acc, [s.id]: s.title }), {});

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
        <div class="drawer-section-label">Evaluation</div>
        ${evalStatusBadge(entry.evalStatus)}
      </div>

      <div class="drawer-section">
        <div class="drawer-section-label">Score</div>
        <div class="drawer-score-row">
          <span class="drawer-total ${entry.evalStatus === 'pending' ? '' : getVerdictClass(entry.total)}">${entry.evalStatus === 'pending' ? '—' : entry.total}</span>
          <span class="drawer-verdict">${entry.evalStatus === 'pending' ? '' : getVerdictLabel(entry.total)}</span>
        </div>
        ${entry.evalStatus !== 'pending' ? `
        <div class="drawer-total-track">
          <div class="drawer-total-fill" style="width: ${entry.total}%; background: ${barColor(entry.total)}"></div>
        </div>
        <div class="drawer-dims">
          ${CONTENT.sections.map(s => `
            <div class="drawer-dim">
              <span class="drawer-dim-label">${dimLabels[s.id]}</span>
              <span class="drawer-dim-score">${entry.weighted[s.id]}<span class="drawer-dim-max"> / ${s.weight}</span></span>
            </div>`).join('')}
        </div>` : ''}
      </div>

      ${entry.evalStatus === 'evaluated' && entry.knockouts?.length ? `
        <div class="drawer-section">
          <div class="drawer-section-label">Knockout Flags</div>
          <div class="drawer-ko-list">
            ${CONTENT.knockouts.map((ko, i) => `
              <div class="drawer-ko-item">
                <div class="drawer-ko-check${entry.knockouts[i] ? ' flagged' : ''}"></div>
                <div class="drawer-ko-label">${escHtml(ko.label)}<small>${escHtml(ko.detail)}</small></div>
              </div>
            `).join('')}
          </div>
          ${entry.knockouts.some(Boolean) ? `<div class="drawer-ko-warning">One or more knockout flags raised.</div>` : ''}
        </div>
      ` : ''}

      ${entry.evalStatus === 'evaluated' ? `
        <div class="drawer-section">
          <div class="drawer-section-label">Evaluation Detail</div>
          ${CONTENT.sections.map(s => {
            const sid       = String(s.id);
            const secScores = (entry.scores?.[sid])    || [];
            const secReason = (entry.reasoning?.[sid]) || [];
            return `
              <div class="drawer-eval-section">
                <div class="drawer-eval-section-title">${escHtml(s.title)}</div>
                ${s.items.map((item, i) => {
                  const score     = secScores[i] ?? 0;
                  const label     = item.labels?.[score] ?? String(score);
                  const reason    = secReason[i] || '';
                  const pct  = item.max > 0 ? Math.round((score / item.max) * 100) : 0;
                  return `
                    <div class="drawer-eval-item">
                      <div class="drawer-eval-question">${escHtml(item.question)}</div>
                      ${reason ? `<div class="drawer-eval-reasoning">${escHtml(reason)}</div>` : ''}
                      <div class="drawer-eval-bar-wrap">
                        <div class="drawer-eval-bar-track">
                          <div class="drawer-eval-bar-fill" style="width: ${pct}%; background: ${barColor(pct)}"></div>
                        </div>
                        <span class="drawer-eval-score-num">${score}/${item.max}</span>
                        <span class="drawer-eval-label">${escHtml(label)}</span>
                      </div>
                    </div>`;
                }).join('')}
              </div>`;
          }).join('')}
        </div>
      ` : ''}

      <div class="drawer-section">
        <div class="drawer-section-label">Notes</div>
        <textarea class="drawer-notes-edit"
          onblur="updateNotes('${entry.id}', this.value)"
          placeholder="Gut feel, red flags, things to research..."
        >${escHtml(entry.notes || '')}</textarea>
      </div>

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

  async function updateNotes(id, value) {
    const entry = pipelineState.queue.find(e => e.id === id);
    if (!entry || value.trim() === (entry.notes || '').trim()) return;
    entry.notes = value.trim();
    await patchEntry(id, { notes: entry.notes });
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

  // ── PROGRESS BARS ─────────────────────────────────────────────────────────

  function _createProgressBar(label, estimatedSeconds) {
    const item = document.createElement('div');
    item.className = 'progress-item';
    item.innerHTML = `<div class="progress-item-label">${label}</div><div class="progress-item-fill"></div>`;
    document.getElementById('progress-stack').appendChild(item);

    const fill = item.querySelector('.progress-item-fill');
    fill.style.transition = 'none';
    fill.style.width = '0%';
    fill.getBoundingClientRect(); // force reflow
    fill.style.transition = `width ${estimatedSeconds}s linear`;
    fill.style.width = '90%';

    return {
      finish() {
        fill.style.transition = 'width 0.3s ease';
        fill.style.width = '100%';
        setTimeout(() => item.remove(), 600);
      },
      reset() { item.remove(); },
    };
  }

  let _rubricBar = null;

  function startRubricProgress() { _rubricBar = _createProgressBar('Generating rubric\u2026', 55); }
  function finishRubricProgress() { _rubricBar?.finish(); _rubricBar = null; }
  function resetRubricProgress()  { _rubricBar?.reset();  _rubricBar = null; }

  // ── RUBRIC EDITOR ─────────────────────────────────────────────────────────

  function showRubricModal() {
    document.getElementById('rubric-editor').value = JSON.stringify(CONTENT, null, 2);
    document.getElementById('rubric-error').style.display = 'none';
    document.getElementById('rubric-overlay').classList.add('visible');
  }

  function closeRubricModal() {
    document.getElementById('rubric-overlay').classList.remove('visible');
  }

  async function saveRubric() {
    const errorEl = document.getElementById('rubric-error');
    const saveBtn = document.getElementById('rubric-save-btn');
    errorEl.style.display = 'none';

    let rubric;
    try {
      rubric = JSON.parse(document.getElementById('rubric-editor').value);
    } catch (e) {
      errorEl.textContent = `Invalid JSON: ${e.message}`;
      errorEl.style.display = '';
      return;
    }

    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';
    try {
      const res = await fetch('/api/profile/rubric', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(rubric),
      });
      if (!res.ok) {
        const msg = await res.text();
        errorEl.textContent = msg || 'Save failed';
        errorEl.style.display = '';
        return;
      }
      CONTENT = rubric;
      closeRubricModal();
      showToast('Rubric saved');
    } catch (err) {
      errorEl.textContent = 'Network error — please try again';
      errorEl.style.display = '';
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save Rubric';
    }
  }

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && document.getElementById('rubric-overlay').classList.contains('visible')) {
      closeRubricModal();
    }
  });

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