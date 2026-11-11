// Disbursement Desk, slices 1 to 5 (#32): switch the queue between Exceptions and All,
// open one payout's detail, Approve / Hold / Return it after a confirm dialog,
// Reset demo, and show the stored rules note or the AI-unavailable fallback. The
// server already rendered every row, panel, note and the dialog words; this shows
// or hides them. A confirmed
// action is sent to the #35 decision API (POST /payouts/<id>/<action>) and the page
// reloads, so the list and detail show the saved state. If it does not save, the
// dialog stays open and says so; nothing on screen changes. Cancel changes nothing.
// Reset demo posts to /payouts/reset, which restores this visitor's synthetic seed
// only, then reloads on Exceptions with no payout open. If reset does not save,
// nothing on screen changes, and one approved line appears next to Reset demo.
// There is no confirm step for Reset (the approved mock and slice 4 spec have none).
// The API body is never shown.
// URL hash for reproducible screenshots: #f=all opens on All, #id=PO-1042 opens a
// payout, #id=PO-1043&confirm=hold also opens that dialog, #id=PO-1042&ai=off shows
// the approved AI-unavailable fallback on the stored note.
(function () {
  const chips = document.querySelectorAll('.chip[data-f]');
  const rows = document.querySelectorAll('#list li[data-ex]');
  const empty = document.querySelector('#list li[data-empty]');
  const detail = document.getElementById('detail');
  const panels = detail ? detail.querySelectorAll('.panel[data-id]') : [];
  const dialog = document.getElementById('confirm');
  let filter = 'exceptions';
  let selected = null;
  let pending = null; // {id, action, key} while the dialog is open
  let aiOff = false;

  function writeHash() {
    const q = new URLSearchParams();
    if (filter === 'all') q.set('f', 'all');
    if (selected) q.set('id', selected);
    if (aiOff) q.set('ai', 'off');
    const hash = q.toString();
    history.replaceState(null, '', hash ? '#' + hash : location.pathname + location.search);
  }

  // #ai=off shows the already-rendered fallback note and hides the stored rules
  // note. No model is called; the fallback is already on the page, and its body
  // is the same stored #37 text.
  function showFallback(off) {
    document.querySelectorAll('.note[data-note="ai"]').forEach(n => { n.hidden = off; });
    document.querySelectorAll('.note[data-note="rules"]').forEach(n => { n.hidden = off; });
    document.querySelectorAll('.note[data-note="fallback"]').forEach(n => { n.hidden = !off; });
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

  function newKey() {
    if (window.crypto && crypto.randomUUID) return 'desk-' + crypto.randomUUID();
    return 'desk-' + Date.now() + '-' + Math.random().toString(16).slice(2);
  }

  // One key per opened dialog: pressing the confirm button again after a failure retries
  // the same decision, so it can never be saved twice.
  function ask(action) {
    const panel = [...panels].find(p => p.dataset.id === selected);
    const button = panel && panel.querySelector('[data-act="' + action + '"]');
    if (!button || button.disabled) return;
    pending = {id: selected, action, key: newKey()};
    document.getElementById('ch').textContent = button.dataset.heading;
    document.getElementById('c-payee').textContent = panel.dataset.payee;
    document.getElementById('c-amount').textContent = panel.dataset.amount;
    document.getElementById('c-id').textContent = selected;
    document.getElementById('c-body').textContent = button.dataset.body;
    const ok = document.getElementById('ok');
    ok.textContent = button.dataset.ok;
    ok.className = button.dataset.okClass;
    setBusy(false);
    document.getElementById('c-error').hidden = true;
    dialog.showModal();
  }

  function setBusy(busy) {
    document.getElementById('ok').disabled = busy;
    document.getElementById('cancel').disabled = busy;
  }

  async function confirmAction() {
    if (!pending) return;
    setBusy(true);
    const demo = document.querySelector('meta[name="demo-id"]');
    try {
      const reply = await fetch('/payouts/' + encodeURIComponent(pending.id) + '/' + pending.action, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'Idempotency-Key': pending.key,
                  'X-Demo-ID': demo ? demo.content : ''},
        body: JSON.stringify({reviewer: 'Desk operator'}),
      });
      if (!reply.ok) throw new Error('not saved');
    } catch (error) {
      setBusy(false);
      document.getElementById('c-error').hidden = false;
      return;
    }
    writeHash(); // keeps the filter and payout, drops any #confirm=
    location.reload();
  }

  if (dialog) dialog.addEventListener('close', () => { pending = null; });

  // Reset is immediate: the approved mock and slice 4 spec have no confirm dialog.
  // A failed reset leaves the page as it is and shows the approved line next to
  // Reset demo (option A, c6774718893). The line clears on the next successful
  // reset or reload. The API body is never shown.
  let resetting = false;
  const resetError = document.getElementById('r-error');
  async function resetDemo() {
    if (resetting) return;
    resetting = true;
    if (resetError) resetError.hidden = true;
    const demo = document.querySelector('meta[name="demo-id"]');
    try {
      const reply = await fetch('/payouts/reset', {
        method: 'POST',
        headers: {'X-Demo-ID': demo ? demo.content : ''},
      });
      if (!reply.ok) throw new Error('not reset');
    } catch (error) {
      resetting = false;
      if (resetError) resetError.hidden = false;
      return;
    }
    location.replace(location.pathname + location.search);
  }

  document.addEventListener('click', e => {
    if (e.target.closest('#reset')) { resetDemo(); return; }
    if (e.target.closest('#cancel')) { dialog.close(); return; }
    if (e.target.closest('#ok')) { confirmAction(); return; }
    const act = e.target.closest('.panel [data-act]');
    if (act) { ask(act.dataset.act); return; }
    const chip = e.target.closest('.chip[data-f]');
    const row = e.target.closest('.row[data-id]');
    if (chip) show(chip.dataset.f);
    else if (row) open(row.dataset.id, true);
    else if (e.target.closest('.back')) close(true);
    else return;
    writeHash();
  });

  // Escape closes the payout detail the same way Back does: hide the panel,
  // clear the hash, and return focus to the row that opened it. Leave Escape
  // to the confirm dialog while that dialog is open.
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if (dialog && dialog.open) return;
    if (!selected) return;
    e.preventDefault();
    close(true);
    writeHash();
  });

  const q = new URLSearchParams(location.hash.slice(1));
  aiOff = q.get('ai') === 'off';
  showFallback(aiOff);
  if (q.get('f') === 'all') show('all');
  if (q.get('id')) open(q.get('id'), false);
  if (q.get('id') && q.get('confirm')) ask(q.get('confirm'));
})();
