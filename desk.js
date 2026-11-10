// Disbursement Desk, slices 1 and 2 (#32): switch the read-only queue between Exceptions
// and All, and open one payout's read-only detail. The server already rendered every
// current-queue row and every detail panel; this only shows or hides them.
// URL hash for reproducible screenshots: #f=all opens on All, #id=PO-1042 opens a payout.
// Nothing here saves anything. Reset demo is wired in slice 4.
(function () {
  const chips = document.querySelectorAll('.chip[data-f]');
  const rows = document.querySelectorAll('#list li[data-ex]');
  const empty = document.querySelector('#list li[data-empty]');
  const detail = document.getElementById('detail');
  const panels = detail ? detail.querySelectorAll('.panel[data-id]') : [];
  let filter = 'exceptions';
  let selected = null;

  function writeHash() {
    const q = new URLSearchParams();
    if (filter === 'all') q.set('f', 'all');
    if (selected) q.set('id', selected);
    const hash = q.toString();
    history.replaceState(null, '', hash ? '#' + hash : location.pathname + location.search);
  }

  function show(next) {
    filter = next;
    let visible = 0;
    chips.forEach(c => c.setAttribute('aria-pressed', String(c.dataset.f === filter)));
    rows.forEach(li => {
      li.hidden = filter !== 'all' && li.dataset.ex !== '1';
      if (!li.hidden) visible += 1;
    });
    if (empty) empty.hidden = visible > 0;
  }

  function open(id, focus) {
    const panel = [...panels].find(p => p.dataset.id === id);
    if (!panel) return close(false);
    selected = id;
    panels.forEach(p => { p.hidden = p !== panel; });
    detail.hidden = false;
    document.querySelectorAll('.row[data-id]').forEach(r => r.setAttribute('aria-current', String(r.dataset.id === id)));
    if (focus) panel.querySelector('h2').focus();
  }

  function close(focus) {
    const was = selected;
    selected = null;
    if (detail) detail.hidden = true;
    panels.forEach(p => { p.hidden = true; });
    document.querySelectorAll('.row[data-id]').forEach(r => r.setAttribute('aria-current', 'false'));
    if (focus && was) {
      const row = document.querySelector('.row[data-id="' + CSS.escape(was) + '"]');
      if (row && row.offsetParent) row.focus();
    }
  }

  document.addEventListener('click', e => {
    const chip = e.target.closest('.chip[data-f]');
    const row = e.target.closest('.row[data-id]');
    if (chip) show(chip.dataset.f);
    else if (row) open(row.dataset.id, true);
    else if (e.target.closest('.back')) close(true);
    else return;
    writeHash();
  });

  const q = new URLSearchParams(location.hash.slice(1));
  if (q.get('f') === 'all') show('all');
  if (q.get('id')) open(q.get('id'), false);
})();
