// Disbursement Desk, slice 1 (#32): switch the read-only queue between Exceptions and All.
// The server already rendered every current-queue row; this only shows or hides them.
// #f=all in the URL opens on All (for reproducible screenshots). Reset demo is wired later.
(function () {
  const chips = document.querySelectorAll('.chip[data-f]');
  const rows = document.querySelectorAll('#list li[data-ex]');
  const empty = document.querySelector('#list li[data-empty]');
  function show(filter) {
    let visible = 0;
    chips.forEach(c => c.setAttribute('aria-pressed', String(c.dataset.f === filter)));
    rows.forEach(li => {
      li.hidden = filter !== 'all' && li.dataset.ex !== '1';
      if (!li.hidden) visible += 1;
    });
    if (empty) empty.hidden = visible > 0;
  }
  chips.forEach(c => c.addEventListener('click', () => show(c.dataset.f)));
  if (new URLSearchParams(location.hash.slice(1)).get('f') === 'all') show('all');
})();
