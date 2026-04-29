(() => {
  const groupSel = document.getElementById('group-filter');
  if (!groupSel) return; // not on the index page
  const sourceSel = document.getElementById('source-filter');
  const searchInp = document.getElementById('search');
  const reloadBtn = document.getElementById('reload-btn');
  const castBtn = document.getElementById('cast-btn');           // may be null when TV_IP is unset
  const streamBtn = document.getElementById('stream-btn');
  const watchBtn = document.getElementById('watch-btn');
  const stopBtn = document.getElementById('stop-btn');
  const statusEl = document.getElementById('status');
  const selectedName = document.getElementById('selected-name');
  const nowPlaying = document.getElementById('now-playing');
  const nowPlayingName = document.getElementById('now-playing-name');
  const nowPlayingState = document.getElementById('now-playing-state');
  const nowPlayingLink = document.getElementById('now-playing-stream-link');
  const grid = document.getElementById('grid');

  const allTiles = Array.from(grid.querySelectorAll('.tile'));
  let selectedTile = null;

  const setStatus = (msg, isError = false) => {
    statusEl.textContent = msg || '';
    statusEl.style.color = isError ? '#f66' : '#aaa';
  };

  const updateActionButtons = () => {
    const hasSelection = !!selectedTile;
    if (castBtn) castBtn.disabled = !hasSelection;
    streamBtn.disabled = !hasSelection;
    watchBtn.disabled = !hasSelection;
    // Stop is enabled whenever something is playing
    stopBtn.disabled = !nowPlaying.dataset.currentId;
  };

  const setSelected = (tile) => {
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
      nowPlaying.dataset.streaming = s.streaming ? '1' : '';
      nowPlaying.dataset.streamUrl = s.stream_url || '';
      nowPlayingName.textContent = s.current.name;
      nowPlayingState.textContent = s.casting ? 'Casting:' : (s.streaming ? 'Streaming:' : '');
      if (s.stream_url) {
        nowPlayingLink.href = s.stream_url;
        nowPlayingLink.hidden = false;
      } else {
        nowPlayingLink.hidden = true;
      }
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
    const tile = e.target.closest('.tile');
    if (!tile) return;
    setSelected(tile);
  });
  grid.addEventListener('dblclick', (e) => {
    const tile = e.target.closest('.tile');
    if (!tile) return;
    setSelected(tile);
    castBtn.click();
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

  streamBtn.addEventListener('click', async () => {
    if (!selectedTile) return;
    setStatus('Starting stream…');
    streamBtn.disabled = true;
    try {
      const j = await postJson('/stream', { channel_id: selectedTile.dataset.id });
      renderStatus(j);
      setStatus('Streaming.');
    } catch (err) { setStatus(err.message, true); }
    finally { updateActionButtons(); }
  });

  const MP4_URL = document.querySelector('main').dataset.mp4Url || '';

  watchBtn.addEventListener('click', async () => {
    if (!selectedTile) return;
    if (!MP4_URL) { setStatus('mp4 url not configured', true); return; }
    setStatus('Starting stream…');
    watchBtn.disabled = true;
    // Open the tab synchronously so the popup blocker allows it; we'll
    // navigate to the MP4 URL once the relay is up.
    const win = window.open('about:blank', '_blank');
    try {
      const j = await postJson('/stream', { channel_id: selectedTile.dataset.id });
      renderStatus(j);
      const url = (j.mp4_url || MP4_URL) + '?t=' + Date.now();
      if (win) win.location.href = url;
      setStatus('Streaming.');
    } catch (err) {
      if (win) win.close();
      setStatus(err.message, true);
    } finally {
      updateActionButtons();
    }
  });

  stopBtn.addEventListener('click', async () => {
    setStatus('Stopping…');
    try {
      const j = await postJson('/stop');
      renderStatus(j);
      setStatus('Stopped.');
    } catch (err) { setStatus(err.message, true); }
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
