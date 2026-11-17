# START HERE — Builds the Disbursement Desk page (HTML): the payout queue, the payout
# detail, the Approve / Hold / Return confirm dialog and the note badges (issue #32
# slices 1 to 5). This file never changes the database: desk.js sends a confirmed
# decision to the #35 decision API (POST /payouts/<id>/<action>) and reloads. Slice 5
# reads each payout's stored #37 note (note_text, note_source, note_fallback) and
# shows it on screen. It does not call a model or invent note sentences.
"""Disbursement Desk presentation for the approved #40 design. Never writes to the database."""
from desk_common import money, text
import payout_notes
import review_reasons

# FEATURE 01 — Turn status codes into the approved plain words
# The API keeps its own codes; viewers only ever see these four words (#40). PENDING
# and APPROVED are not in the seed; if one appears it is shown as the nearest word,
# never as the raw code.
STATUS_WORDS = {
    'FLAGGED': ('Needs review', 's-review'),
    'PENDING': ('Needs review', 's-review'),
    'HELD': ('Held', 's-held'),
    'RETURNED': ('Returned', 's-returned'),
    'PAID': ('Sent', 's-sent'),
    'APPROVED': ('Sent', 's-sent'),
}
EXCEPTION_STATUSES = frozenset({'FLAGGED', 'PENDING', 'HELD'})
LOAD_ERROR = 'The payout list couldn’t be loaded. Refresh the page to try again.'
EMPTY_EXCEPTIONS = 'No exceptions left. Nice.'
FINAL_STATUSES = frozenset({'PAID', 'RETURNED'})
FINAL_MESSAGE = 'This payout is final. No actions available.'
ACTION_FAILED = 'That didn’t save. Nothing has changed. Try again.'
RESET_FAILED = 'That didn’t reset. Nothing has changed. Try again.'
CREATE_FAILED = 'That didn’t create. Nothing has changed. Try again.'
CREATE_HEADING = 'Create a demo payout'
CREATE_HINT = 'Synthetic payee only. Uses the same Idempotency-Key rules as the API.'
# Viewer words for payout_events.action (who/what/when on the detail panel).
ACTION_WORDS = {
    'approve': 'Approved',
    'hold': 'Held',
    'return': 'Returned',
    'clear': 'Cleared',
    'block': 'Blocked',
    'screen': 'Held for sanctions screen',
    'create': 'Created',
    'route': 'Routed',
}
AUDIT_EMPTY = 'No activity yet.'
AUDIT_HEADING = 'Activity'

# FEATURE 01b — The three confirm dialogs, word for word from #40
# action: (button, heading, body, confirm-button classes). Return's confirm is red.
ACTIONS = {
    'approve': ('Approve', 'Approve and send?', 'The payout is sent to the payee.', 'btn primary'),
    'hold': ('Hold', 'Hold this payout?',
             'It stays in Exceptions and will not send until someone approves it.', 'btn primary'),
    'return': ('Return', 'Return this payout?',
               'It is cancelled and no money moves. This cannot be undone.', 'btn danger fill'),
}
PANEL_BUTTON_CLASS = {'approve': 'btn primary', 'hold': 'btn', 'return': 'btn danger'}
# Clear / Block replace Approve / Hold / Return when the payout is a possible
# OFAC match (#111). Clear sends it; Block cancels it, the same as Return.
SANCTIONS_ACTIONS = {
    'clear': ('Clear', 'Clear this possible sanctions match?',
              'The reviewer treats this as not a match. The payout is sent to the payee.',
              'btn primary'),
    'block': ('Block', 'Block this payout?',
              'It is cancelled and no money moves. This cannot be undone.', 'btn danger fill'),
}
SANCTIONS_BUTTON_CLASS = {'clear': 'btn primary', 'block': 'btn danger'}
SANCTIONS_FLAG = 'POSSIBLE_SANCTIONS_MATCH'
OPERATOR_FLAGS = {'HELD': 'Held by operator', 'RETURNED': 'Returned by operator'}


# FEATURE 01c — Which flag sentence to show
# A payout an operator has decided (its updated_at moved past created_at; the seed keeps
# them equal) shows Held by operator / Returned by operator, and an approved (Sent) one
# shows no flag. Undecided payouts keep their stored rule sentence. Read-only.
def shown_flag(payout):
    decided = payout.get('updated_at') and payout['updated_at'] != payout.get('created_at')
    if not decided:
        return payout['flag_text']
    if payout['status'] in ('PAID', 'APPROVED'):
        return None
    return OPERATOR_FLAGS.get(payout['status'], payout['flag_text'])


# FEATURE 02c — Reasons for the review panel
# The Desk reads review_reasons.panel_items() so stored flags stay in stored
# order, sanctions facts come from the same OFAC match as for_payout, and a
# bank-details reason includes the required phone-check line. Sent and
# Returned payouts keep these original reasons, under a past-tense heading,
# instead of replacing them with "Returned by operator".
def review_items(payout):
    """One stored rule reason per item: sentence, code, optional facts and phone line."""
    return review_reasons.panel_items(payout)


def _review_panel(payout, items):
    count = len(items)
    noun = 'reason' if count == 1 else 'reasons'
    heading = ('Why this was in review' if payout['status'] in FINAL_STATUSES
               else 'Why this is in review')
    rows = []
    for item in items:
        facts = ''
        if item['facts']:
            cells = ''.join(f'<dt>{text(label)}</dt><dd>{text(value)}</dd>'
                            for label, value in item['facts'])
            facts = f'<dl class="review-facts">{cells}</dl>'
        phone = ''
        if item.get('phone'):
            phone = f'<p class="review-phone">{text(item["phone"])}</p>'
        rows.append(f'<li><p class="review-reason">{text(item["sentence"])}</p>{phone}{facts}'
                    f'<code class="review-code">{text(item["code"])}</code></li>')
    return (f'<div class="review"><h3>{text(heading)} '
            f'<span class="review-count">{count} {noun}</span></h3>'
            f'<ul class="review-list">{"".join(rows)}</ul>'
            f'<p class="review-foot">From fixed rules. Review before sending.</p></div>')


# FEATURE 01d — Choose the note badge and helper line (slice 5, Q3 option A)
# Stored #37 notes come from a template, so they are shown only as RULES NOTE
# plus "From fixed rules". There is no AI model, so the AI NOTE spot uses the
# approved #40 fallback wording: RULES NOTE plus "AI unavailable, rules fallback".
# The body is the payout's stored note_text. "Written by AI, may be wrong" and
# "AI-drafted, review required" are never shown on template text. This slice does
# not call a model, add an env var or key, or write note columns. The AI never
# decides the flag.
RULES_NOTE_BADGE = 'RULES NOTE'
RULES_NOTE_HELPER = 'From fixed rules'
FALLBACK_HELPER = 'AI unavailable, rules fallback'
AI_TIMEOUT_SECONDS = 3


def ai_unavailable(ai_off=False, error=False, elapsed_seconds=None):
    if ai_off or error:
        return True
    return elapsed_seconds is not None and elapsed_seconds > AI_TIMEOUT_SECONDS


def shown_note(payout, ai_off=False, error=False, elapsed_seconds=None):
    """Badge, helper line, body, kind and whether this is the fallback."""
    body = payout.get('note_text') or ''
    # No model: never label stored text as Written by AI, even if note_source is ai.
    if (payout.get('note_fallback') or payout.get('note_source') == 'ai'
            or ai_unavailable(ai_off, error, elapsed_seconds)):
        return {
            'badge': RULES_NOTE_BADGE,
            'helper': FALLBACK_HELPER,
            'body': body,
            'kind': 'rules',
            'fallback': True,
        }
    return {
        'badge': RULES_NOTE_BADGE,
        'helper': RULES_NOTE_HELPER,
        'body': body,
        'kind': 'rules',
        'fallback': False,
    }


def _note_block(note, hidden=False):
    css = 'b-ai' if note['kind'] == 'ai' else 'b-rules'
    if note['fallback']:
        which = 'fallback'
    elif note['kind'] == 'ai':
        which = 'ai'
    else:
        which = 'rules'
    hide = ' hidden' if hidden else ''
    return (f'<div class="note" data-note="{which}"{hide}><div class="note-h">'
            f'<span class="badge {css}">{note["badge"]}</span>{text(note["helper"])}</div>'
            f'<p>{text(note["body"])}</p></div>')


def _notes(payout):
    # The stored rules note is visible on first paint, so the panel never sits
    # empty or keeps spinning. The approved AI-unavailable fallback (same stored
    # body, fallback helper) is also in the page, hidden, and desk.js shows it
    # when #ai=off. No model is called.
    default = shown_note(payout)
    blocks = [_note_block(default)]
    if not default['fallback']:
        blocks.append(_note_block(shown_note(payout, ai_off=True), hidden=True))
    return ''.join(blocks)


# FEATURE 02 — Build one queue row
# Payee, amount like $4,850.00, the payout id plus why it was flagged (or the rail when
# nothing fired) and the status pill. Each row is a button that opens its detail (slice 2).
# Rows that are not exceptions start hidden because the page opens on Exceptions.
def _row(payout):
    word, css = STATUS_WORDS[payout['status']]
    exception = payout['status'] in EXCEPTION_STATUSES
    why = shown_flag(payout) or payout['rail']
    return (f'<li data-ex="{int(exception)}"{"" if exception else " hidden"}>'
            f'<button type="button" class="row" data-id="{text(payout["payout_id"])}" aria-current="false">'
            f'<span class="payee">{text(payout["payee_name"])}</span>'
            f'<span class="amt">{money(payout["amount"])}</span>'
            f'<span class="sub">{text(payout["payout_id"])} · {text(why)}</span>'
            f'<span class="pill {css}">{word}</span></button></li>')


def status_word(code):
    """Plain Desk word for a stored status code (fallback: the code itself)."""
    if code in STATUS_WORDS:
        return STATUS_WORDS[code][0]
    return code or '—'


def action_word(code):
    """Plain Desk word for a payout_events.action code."""
    return ACTION_WORDS.get(code, code or 'Updated')


def audit_when(at):
    """Show a compact UTC timestamp; blank when missing."""
    if not isinstance(at, str) or not at.strip():
        return ''
    raw = at.strip()
    if 'T' in raw:
        date, _, rest = raw.partition('T')
        clock = rest[:8] if len(rest) >= 8 else rest.rstrip('Z')
        return f'{date} {clock} UTC'.strip()
    return raw


def audit_line(event):
    """One Activity row: when · who · what (from → to), plus reason when present."""
    when = audit_when(event.get('at'))
    actor = text(event.get('actor') or 'Unknown')
    what = text(action_word(event.get('action')))
    frm = text(status_word(event.get('from_status')))
    to = text(status_word(event.get('to_status')))
    move = f'{frm} → {to}' if frm != to else frm
    head = f'<span class="audit-when">{text(when)}</span>' if when else ''
    line = (f'<li><p class="audit-main">{head}<span class="audit-actor">{actor}</span>'
            f'<span class="audit-what">{what} · {move}</span></p>')
    reason = event.get('reason')
    if isinstance(reason, str) and reason.strip():
        line += f'<p class="audit-reason">{text(reason.strip())}</p>'
    return line + '</li>'


def _audit_panel(events):
    """Activity list for one payout (oldest first). Empty when no events yet."""
    parts = [f'<section class="audit" aria-label="{AUDIT_HEADING}">',
             f'<h3>{AUDIT_HEADING}</h3>']
    if not events:
        parts.append(f'<p class="audit-empty">{text(AUDIT_EMPTY)}</p>')
    else:
        parts.append('<ol class="audit-list">' + ''.join(audit_line(e) for e in events) + '</ol>')
    parts.append('</section>')
    return ''.join(parts)


# FEATURE 02b — Build one payout's detail, its review reasons and its action buttons
# Payee, amount, payout id, account, rail, status, then one "Why this is in review"
# panel that lists every stored rule reason. The yellow "Why flagged" box and the
# RULES NOTE card are not shown. Sent and Returned payouts keep the original
# reasons under "Why this was in review" and say they are final. Needs review
# and Held payouts get Approve / Hold / Return (slice 3); Hold is disabled when
# the payout is already Held. A possible sanctions match gets Clear / Block.
# Activity (who/what/when from payout_events) sits under the review block.
# The panel carries the payee and amount so the dialog can repeat them.
def _detail(payout, events=()):
    word, css = STATUS_WORDS[payout['status']]
    pid = text(payout['payout_id'])
    parts = [f'<div class="panel" data-id="{pid}" data-payee="{text(payout["payee_name"])}" '
             f'data-amount="{money(payout["amount"])}" hidden>',
             '<div class="dtop"><button type="button" class="back" aria-label="Back to list">← Back</button>'
             f'<span class="pill {css}">{word}</span></div>',
             f'<h2 tabindex="-1">{text(payout["payee_name"])}</h2>',
             f'<p class="big">{money(payout["amount"])}</p>',
             f'<dl><dt>Payout</dt><dd>{pid}</dd><dt>To account</dt><dd>{text(payout["payee_account"])}</dd>'
             f'<dt>Rail</dt><dd>{text(payout["rail"])} · {text(payout["currency"])}</dd></dl>']
    items = review_items(payout)
    if items:
        parts.append(_review_panel(payout, items))
    else:
        parts.append(f'<p class="review-clean">{text(payout_notes.CLEAN_NOTE)}</p>')
    parts.append(_audit_panel(events))
    if payout['status'] in FINAL_STATUSES:
        parts.append(f'<p class="done">{FINAL_MESSAGE}</p>')
    else:
        codes = (payout.get('flag_code') or '').split(',')
        if SANCTIONS_FLAG in codes:
            actions, classes = SANCTIONS_ACTIONS, SANCTIONS_BUTTON_CLASS
        else:
            actions, classes = ACTIONS, PANEL_BUTTON_CLASS
        buttons = []
        for action, (label, heading, body, ok_class) in actions.items():
            disabled = ' disabled' if action == 'hold' and payout['status'] == 'HELD' else ''
            buttons.append(f'<button type="button" class="{classes[action]}" data-act="{action}" '
                           f'data-heading="{text(heading)}" data-body="{text(body)}" data-ok="{label}" '
                           f'data-ok-class="{ok_class}"{disabled}>{label}</button>')
        parts.append('<div class="actions">' + ''.join(buttons) + '</div>')
    parts.append('</div>')
    return ''.join(parts)


# FEATURE 03 — Build the whole Desk page
# queue is the current-queue payouts (payouts.current_queue), or None when it could not be
# read, which shows the approved load-error sentence instead of the list (Q2 option A).
# The list is part of the first paint, so there is no loading message.
def render_desk(queue, demo_id='local-test', operator=None, events_by_id=None):
    events_by_id = events_by_id or {}
    if queue is None:
        exceptions = total = details = ''
        items = f'<li class="err" role="alert">{LOAD_ERROR}</li>'
    else:
        ex_count = sum(p['status'] in EXCEPTION_STATUSES for p in queue)
        exceptions, total = f' <b>{ex_count}</b>', f' <b>{len(queue)}</b>'
        items = ''.join(_row(p) for p in queue)
        items += f'<li class="empty" data-empty{"" if ex_count == 0 else " hidden"}>{EMPTY_EXCEPTIONS}</li>'
        details = ''.join(_detail(p, events_by_id.get(p['payout_id'], ())) for p in queue)
    operator_name = text(getattr(operator, 'display_name', None) or 'Desk operator')
    operator_role = text(getattr(operator, 'role', None) or 'admin')
    operator_id = text(getattr(operator, 'operator_id', None) or 'local-test')
    return '''<!doctype html>
<html lang="en"><head><meta name="demo-id" content="''' + text(demo_id) + '''"><meta name="operator-id" content="''' + operator_id + '''"><meta name="operator-role" content="''' + operator_role + '''"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Disbursement Desk · Example data</title>
<style>
:root{color-scheme:light;--ink:#173b35;--muted:#4f6159;--line:#d5ddd5;--paper:#f5f6f0;--green:#176348;--amber-bg:#fff2d6;--amber:#6b4d0f;--red-bg:#f9ebe5;--red:#8a3523;--ok-bg:#e8f2eb;--ok:#1f5a3f;--blue-bg:#e7eefb;--blue:#234a8a}
*{box-sizing:border-box}html,body{margin:0;overflow-x:hidden}
body{background:var(--paper);color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
button{font:inherit;cursor:pointer}
:focus-visible{outline:3px solid #b38528;outline-offset:2px}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:14px 24px;background:#fff;border-bottom:1px solid var(--line)}
.top .right{display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:flex-end}
.brand{font-weight:750;font-size:19px;display:flex;gap:10px;align-items:center}
.mark{background:var(--ink);color:#fff;border-radius:8px;width:32px;height:32px;display:grid;place-items:center;flex-shrink:0}
.tag{font-size:13px;font-weight:700;background:var(--amber-bg);color:var(--amber);padding:4px 10px;border-radius:6px;white-space:nowrap}
.layout{display:grid;grid-template-columns:minmax(0,1fr) 440px;gap:24px;max-width:1240px;margin:auto;padding:24px}
.layout:has(.detail[hidden]){grid-template-columns:minmax(0,1fr)}
h2{font-size:22px;margin:12px 0 0}h2:focus{outline:none}
h1{font-size:26px;margin:0;letter-spacing:-.5px}
.qhead{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.qhead .filters{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.field{display:grid;gap:4px;margin:12px 0 0}
.field label{font-size:14px;font-weight:600;color:var(--muted)}
.field input,.field select{min-height:44px;border:1px solid var(--line);border-radius:8px;padding:0 12px;font:inherit;color:var(--ink);background:#fff;width:100%}
.field .hint,dialog .hint{font-size:13px;color:var(--muted);font-weight:500}
.filters{display:flex;gap:8px}
.chip{min-height:44px;min-width:44px;padding:0 16px;border-radius:22px;border:1px solid var(--line);background:#fff;color:var(--ink);font-weight:600}
.chip[aria-pressed=true]{background:var(--ink);color:#fff;border-color:var(--ink)}
.chip b{margin-left:4px}
.list{list-style:none;margin:0;padding:0;background:#fff;border:1px solid var(--line);border-radius:12px;overflow:hidden}
.list li[hidden]{display:none}
.row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:4px 16px;width:100%;min-height:64px;padding:14px 18px;border:0;background:#fff;color:var(--ink);text-align:left;box-shadow:0 -1px 0 var(--line)}
.row[aria-current=true]{background:#eef3ea;box-shadow:0 -1px 0 var(--line),inset 4px 0 0 var(--green)}
/* Draw the keyboard focus ring inside the row so .list overflow:hidden does not clip it (#102). */
.row:focus-visible{outline-offset:-3px}
/* The line above each row; .list clips it above the first visible row. */
.payee{font-weight:650;overflow-wrap:anywhere}.amt{font-weight:700;text-align:right;font-variant-numeric:tabular-nums}
.sub{font-size:14px;color:var(--muted)}
.pill{display:inline-block;font-size:13px;font-weight:700;padding:2px 9px;border-radius:5px;justify-self:end;align-self:start}
.s-review{background:var(--amber-bg);color:var(--amber)}.s-held{background:var(--blue-bg);color:var(--blue)}
.s-returned{background:var(--red-bg);color:var(--red)}.s-sent{background:var(--ok-bg);color:var(--ok)}
.empty{padding:18px;color:var(--muted);box-shadow:0 -1px 0 var(--line)}
.err{background:var(--red-bg);color:var(--red);border-radius:8px;padding:14px 16px;margin:8px;font-size:15px}
.foot{font-size:14px;color:var(--muted);margin-top:12px}
.link{background:none;border:0;color:var(--green);text-decoration:underline;font-weight:600;min-height:44px;padding:0 8px}
.detail{background:#fff;border:1px solid var(--line);border-radius:12px;padding:20px;align-self:start;position:sticky;top:16px}
.panel[hidden]{display:none}
.dtop{display:flex;justify-content:space-between;align-items:center;gap:8px}
.back{min-height:44px;min-width:44px;border:1px solid var(--line);background:#fff;color:var(--ink);border-radius:8px;padding:0 12px;font-weight:600}
.big{font-size:36px;font-weight:700;letter-spacing:-1px;margin:10px 0 0;font-variant-numeric:tabular-nums}
dl{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;margin:14px 0;font-size:15px}dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere}
.flag{background:var(--amber-bg);color:var(--amber);border-radius:8px;padding:10px 12px;font-size:15px;margin:12px 0}
.review{background:var(--amber-bg);color:var(--amber);border-radius:8px;padding:12px 14px;margin:12px 0}
.review h3{font-size:15px;font-weight:700;line-height:1.35;margin:0;display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.review-count{font-size:13px;font-weight:600;color:var(--muted)}
.review-list{list-style:none;margin:8px 0 0;padding:0}
.review-list li + li{border-top:1px solid #ead7a8;margin-top:8px;padding-top:8px}
.review-reason{margin:0;font-size:15px;overflow-wrap:anywhere}
.review-facts{display:grid;grid-template-columns:auto minmax(0,1fr);gap:4px 14px;margin:8px 0 2px;font-size:15px}
.review-facts dt{color:var(--muted)}
.review-facts dd{margin:0;overflow-wrap:anywhere;color:var(--ink)}
.review-code{display:block;margin-top:4px;font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:var(--muted);overflow-wrap:anywhere}
.review-phone{margin:4px 0 0;font-size:15px;overflow-wrap:anywhere}
.review-foot{margin:10px 0 0;padding-top:8px;border-top:1px solid #ead7a8;font-size:13px;color:var(--muted)}
.review-clean{font-size:15px;color:var(--muted);margin:12px 0}
.audit{margin:14px 0 0;padding-top:12px;border-top:1px solid var(--line)}
.audit h3{font-size:15px;font-weight:700;margin:0 0 8px}
.audit-empty{font-size:14px;color:var(--muted);margin:0}
.audit-list{list-style:none;margin:0;padding:0}
.audit-list li+li{margin-top:10px;padding-top:10px;border-top:1px solid var(--line)}
.audit-main{margin:0;font-size:14px;display:flex;flex-direction:column;gap:2px}
.audit-when{color:var(--muted);font-size:13px}
.audit-actor{font-weight:650}
.audit-what{color:var(--ink)}
.audit-reason{margin:4px 0 0;font-size:13px;color:var(--muted);overflow-wrap:anywhere}
.note{border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:12px 0}
.note[hidden]{display:none}
.note-h{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-weight:700;font-size:14px;margin-bottom:4px}
.badge{font-size:12px;font-weight:800;padding:2px 8px;border-radius:5px;letter-spacing:.04em}
.b-ai{background:#ece8fb;color:#4a2f99}.b-rules{background:#e9ece9;color:#2f3d37}
.note p{margin:4px 0;font-size:15px}
.done{font-size:15px;color:var(--muted);margin-top:14px}
.actions{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:16px}
.btn{min-height:48px;min-width:44px;border-radius:9px;font-weight:700;border:2px solid var(--ink);background:#fff;color:var(--ink);padding:0 12px}
.btn.primary{background:var(--green);border-color:var(--green);color:#fff}
.btn.danger{border-color:var(--red);color:var(--red)}
.btn.danger.fill{background:var(--red);color:#fff}
.btn:disabled{opacity:.45;cursor:not-allowed}
dialog{border:0;border-radius:14px;padding:22px;max-width:420px;width:calc(100% - 32px);color:var(--ink)}
dialog::backdrop{background:rgba(23,59,53,.45)}
dialog p{margin:8px 0}dialog h2{margin:0}
dialog .row2{display:flex;gap:10px;justify-content:flex-end;margin-top:18px}
dialog .btn{min-width:110px}
.err-inline{background:var(--red-bg);color:var(--red);border-radius:8px;padding:10px 12px;margin:12px 0 0;font-size:15px}
.err-inline[hidden]{display:none}
.foot .err-inline{display:inline;margin:0 0 0 .4em;padding:4px 8px;font-size:14px}
.foot .err-inline[hidden]{display:none}
@media(max-width:760px){
 .top{padding:12px 16px}.brand{font-size:17px}.tag{font-size:12px}
 .layout{grid-template-columns:minmax(0,1fr);padding:16px}
 .detail{position:static;padding:16px}
 .actions{grid-template-columns:1fr}
 .layout:has(.detail:not([hidden])) .queue{display:none}
}
</style></head>
<body>
<header class="top">
  <div class="brand"><span class="mark" aria-hidden="true">D</span>Disbursement Desk</div>
  <div class="right"><span class="tag">Example data · fake money</span><span class="tag op" title="Signed-in operator">''' + operator_name + ''' · ''' + operator_role + '''</span></div>
</header>
<main class="layout">
  <section class="queue" aria-labelledby="qh">
    <div class="qhead">
      <h1 id="qh">Outgoing payouts</h1>
      <div class="filters" role="group" aria-label="Filter">
        <button type="button" class="chip" data-f="exceptions" aria-pressed="true">Exceptions''' + exceptions + '''</button>
        <button type="button" class="chip" data-f="all" aria-pressed="false">All''' + total + '''</button>
        <button type="button" class="btn" id="create-open">Create payout</button>
      </div>
    </div>
    <ul id="list" class="list">''' + items + '''</ul>
    <p class="foot">Your changes stay in this browser only. <button type="button" id="reset" class="link">Reset demo</button><span id="r-error" class="err-inline" role="status" hidden>''' + RESET_FAILED + '''</span></p>
  </section>
  <aside id="detail" class="detail" aria-label="Payout detail" hidden>''' + details + '''</aside>
</main>
<dialog id="confirm" aria-labelledby="ch">
  <h2 id="ch"></h2>
  <p><strong id="c-payee"></strong> · <span id="c-amount"></span> · <span id="c-id"></span></p>
  <p id="c-body"></p>
  <p id="c-error" class="err-inline" role="alert" hidden>''' + ACTION_FAILED + '''</p>
  <div class="row2"><button type="button" class="btn" id="cancel">Cancel</button><button type="button" id="ok"></button></div>
</dialog>
<dialog id="create" aria-labelledby="cr-h">
  <h2 id="cr-h">''' + CREATE_HEADING + '''</h2>
  <p class="hint">''' + CREATE_HINT + '''</p>
  <form id="create-form">
    <div class="field"><label for="cr-payee">Payee name</label>
      <input id="cr-payee" name="payee_name" autocomplete="off" required maxlength="120"></div>
    <div class="field"><label for="cr-account">Payee account</label>
      <input id="cr-account" name="payee_account" autocomplete="off" required maxlength="64"
             pattern="DEMO-[A-Za-z0-9-]+" placeholder="DEMO-ACME-001"
             title="Must start with DEMO-"></div>
    <div class="field"><label for="cr-amount">Amount (USD)</label>
      <input id="cr-amount" name="amount" inputmode="decimal" autocomplete="off" required
             placeholder="1000.00"></div>
    <div class="field"><label for="cr-rail">Rail</label>
      <select id="cr-rail" name="rail" required>
        <option value="ACH" selected>ACH</option>
        <option value="SAME_DAY_ACH">Same-day ACH</option>
        <option value="RTP">RTP</option>
        <option value="FEDNOW">FedNow</option>
        <option value="WIRE">Wire</option>
      </select></div>
    <div class="field"><label for="cr-id">Payout id <span class="hint">(optional)</span></label>
      <input id="cr-id" name="payout_id" autocomplete="off" maxlength="32"
             placeholder="Leave blank to mint the next PO-…"></div>
    <p id="cr-error" class="err-inline" role="alert" hidden>''' + CREATE_FAILED + '''</p>
    <div class="row2">
      <button type="button" class="btn" id="cr-cancel">Cancel</button>
      <button type="submit" class="btn primary" id="cr-ok">Create</button>
    </div>
  </form>
</dialog>
<script src="/desk.js"></script>
</body></html>'''
