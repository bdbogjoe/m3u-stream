(() => {
  const groupSel = document.getElementById('group-filter');
  if (!groupSel) return; // not on the index page
  const sourceSel = document.getElementById('source-filter');
  const searchInp = document.getElementById('search');
  const reloadBtn = document.getElementById('reload-btn');
  const toggleOfflineBtn = document.getElementById('toggle-offline');
  const castBtn = document.getElementById('cast-btn');           // null when TV_IP unset
  const stopBtn = document.getElementById('stop-btn');           // null when TV_IP unset
  const statusEl = document.getElementById('status');
  const selectedName = document.getElementById('selected-name');
  const nowPlaying = document.getElementById('now-playing');
  const nowPlayingName = document.getElementById('now-playing-name');
  const grid = document.getElementById('grid');

  const allTiles = Array.from(grid.querySelectorAll('.tile'));
  let selectedTile = null;

  const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
                (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

  const setStatus = (msg, isError = false) => {
    statusEl.textContent = msg || '';
    statusEl.style.color = isError ? '#f66' : '#aaa';
  };

  const updateActionButtons = () => {
    if (castBtn) castBtn.disabled = !selectedTile;
    if (stopBtn) stopBtn.disabled = !nowPlaying.dataset.currentId;
  };

  const setSelected = (tile) => {
    if (!selectedName) return; // cast disabled — selection is meaningless
    if (selectedTile) selectedTile.classList.remove('selected');
    selectedTile = tile;
    if (tile) {
      tile.classList.add('selected');
      selectedName.textContent = tile.querySelector('.name').textContent;
    } else {
      selectedName.textContent = '— none —';
    }
    updateActionButtons();
  };

  const renderStatus = (s) => {
    document.querySelectorAll('.tile.current').forEach(t => t.classList.remove('current'));
    if (s.current) {
      const tile = grid.querySelector(`.tile[data-id="${CSS.escape(s.current.id)}"]`);
      if (tile) tile.classList.add('current');
      nowPlaying.dataset.currentId = s.current.id;
      nowPlaying.dataset.casting = s.casting ? '1' : '';
      nowPlayingName.textContent = s.current.name;
      nowPlaying.hidden = false;
    } else {
      nowPlaying.dataset.currentId = '';
      nowPlaying.hidden = true;
    }
    updateActionButtons();
  };

  const rebuildGroupOptions = () => {
    const s = sourceSel ? sourceSel.value : '';
    const previous = groupSel.value;
    const groups = new Set();
    allTiles.forEach(tile => {
      if (s && tile.dataset.source !== s) return;
      if (tile.dataset.group) groups.add(tile.dataset.group);
    });
    const sorted = Array.from(groups).sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
    groupSel.innerHTML = '<option value="">All</option>'
      + sorted.map(g => `<option value="${g.replace(/"/g, '&quot;')}">${g}</option>`).join('');
    if (sorted.includes(previous)) groupSel.value = previous;
  };

  const applyFilter = () => {
    const g = groupSel.value;
    const s = sourceSel ? sourceSel.value : '';
    const q = searchInp.value.trim().toLowerCase();
    allTiles.forEach(tile => {
      const matchG = !g || tile.dataset.group === g;
      const matchS = !s || tile.dataset.source === s;
      const matchQ = !q || tile.dataset.name.includes(q);
      tile.classList.toggle('hidden', !(matchG && matchS && matchQ));
    });
  };

  groupSel.addEventListener('change', applyFilter);
  if (sourceSel) sourceSel.addEventListener('change', () => {
    setSelected(null);
    rebuildGroupOptions();
    applyFilter();
  });
  searchInp.addEventListener('input', applyFilter);

  grid.addEventListener('click', (e) => {
    const watch = e.target.closest('.watch-btn');
    if (watch) {
      const tile = watch.closest('.tile');
      if (!tile) return;
      window.open(tile.dataset.watchUrl, '_blank', 'noopener');
      e.stopPropagation();
      return;
    }
    const tile = e.target.closest('.tile');
    if (!tile) return;
    setSelected(tile);
  });

  const postJson = async (path, body) => {
    const r = await fetch(path, {
      method: 'POST',
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : null,
    });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || `${path} failed`);
    return j;
  };

  if (castBtn) castBtn.addEventListener('click', async () => {
    if (!selectedTile) return;
    setStatus('Casting…');
    castBtn.disabled = true;
    try {
      const j = await postJson('/cast', { channel_id: selectedTile.dataset.id });
      renderStatus(j);
      setStatus('Casting.');
    } catch (err) { setStatus(err.message, true); }
    finally { updateActionButtons(); }
  });

  if (stopBtn) stopBtn.addEventListener('click', async () => {
    setStatus('Stopping…');
    try {
      const j = await postJson('/stop');
      renderStatus(j);
      setStatus('Stopped.');
    } catch (err) { setStatus(err.message, true); }
  });

  const offlineCount = allTiles.filter(t => t.classList.contains('offline')).length;
  const applyOfflineToggle = () => {
    const on = localStorage.getItem('show-offline') === '1';
    document.body.classList.toggle('show-offline', on);
    if (toggleOfflineBtn) {
      toggleOfflineBtn.textContent = on
        ? `Hide offline (${offlineCount})`
        : `Show offline (${offlineCount})`;
      toggleOfflineBtn.disabled = offlineCount === 0;
    }
  };
  applyOfflineToggle();
  if (toggleOfflineBtn) toggleOfflineBtn.addEventListener('click', () => {
    const on = localStorage.getItem('show-offline') === '1';
    localStorage.setItem('show-offline', on ? '0' : '1');
    applyOfflineToggle();
  });

  reloadBtn.addEventListener('click', async () => {
    setStatus('Reloading M3U…');
    try {
      const j = await postJson('/reload');
      setStatus(`Reloaded (${j.count} channels). Refreshing…`);
      location.reload();
    } catch (err) { setStatus(err.message, true); }
  });

  updateActionButtons();
})();
