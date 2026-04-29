(() => {
  const groupSel = document.getElementById('group-filter');
  const searchInp = document.getElementById('search');
  const reloadBtn = document.getElementById('reload-btn');
  const stopBtn = document.getElementById('stop-btn');
  const statusEl = document.getElementById('status');
  const nowPlaying = document.getElementById('now-playing');
  const nowPlayingName = document.getElementById('now-playing-name');
  const grid = document.getElementById('grid');

  const setStatus = (msg, isError = false) => {
    statusEl.textContent = msg || '';
    statusEl.style.color = isError ? '#f66' : '#aaa';
  };

  const setCurrent = (channel) => {
    document.querySelectorAll('.tile.current').forEach(t => t.classList.remove('current'));
    if (channel) {
      const tile = grid.querySelector(`.tile[data-id="${CSS.escape(channel.id)}"]`);
      if (tile) tile.classList.add('current');
      nowPlayingName.textContent = channel.name;
      nowPlaying.hidden = false;
    } else {
      nowPlaying.hidden = true;
    }
  };

  const applyFilter = () => {
    const g = groupSel.value;
    const q = searchInp.value.trim().toLowerCase();
    grid.querySelectorAll('.tile').forEach(tile => {
      const matchG = !g || tile.dataset.group === g;
      const matchQ = !q || tile.dataset.name.includes(q);
      tile.classList.toggle('hidden', !(matchG && matchQ));
    });
  };

  groupSel.addEventListener('change', applyFilter);
  searchInp.addEventListener('input', applyFilter);

  grid.addEventListener('click', async (e) => {
    const btn = e.target.closest('.cast-btn');
    if (!btn) return;
    const id = btn.dataset.id;
    setStatus('Casting…');
    btn.disabled = true;
    try {
      const r = await fetch('/cast', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel_id: id }),
      });
      const j = await r.json();
      if (!j.ok) throw new Error(j.error || 'cast failed');
      setCurrent(j.current);
      setStatus('Playing.');
    } catch (err) {
      setStatus(err.message, true);
    } finally {
      btn.disabled = false;
    }
  });

  stopBtn.addEventListener('click', async () => {
    setStatus('Stopping…');
    try {
      const r = await fetch('/stop', { method: 'POST' });
      const j = await r.json();
      if (!j.ok) throw new Error(j.error || 'stop failed');
      setCurrent(null);
      setStatus('Stopped.');
    } catch (err) {
      setStatus(err.message, true);
    }
  });

  reloadBtn.addEventListener('click', async () => {
    setStatus('Reloading M3U…');
    try {
      const r = await fetch('/reload', { method: 'POST' });
      const j = await r.json();
      if (!j.ok) throw new Error(j.error || 'reload failed');
      setStatus(`Reloaded (${j.count} channels). Refreshing…`);
      location.reload();
    } catch (err) {
      setStatus(err.message, true);
    }
  });
})();
