/*
 * the-examiner — per-criterion feedback logic
 *
 * Loaded as a deferred script on every per-assessment page. The
 * HTML carries the criterion markup; this file wires up the
 * agree / disagree / "I read it as X" controls and the
 * "send all" / "clear all" rail actions.
 *
 * State model:
 *   - Per-criterion: localStorage[examiner:feedback:<bucket>:<criterion_id>]
 *     = { mode: "AWARD"|"DISAGREE"|"NOTE", note: "...", saved_at: ISO }
 *   - On load: restore any saved state into the UI.
 *   - Save button: writes to localStorage only. Not sent yet.
 *   - "Send all feedback" button: collects all saved items and
 *     PUTs them to https://kvdb.io/<bucket>/student-feedback
 *     as JSON. Anonymous, no auth. Failures show a browser alert.
 *
 * Privacy:
 *   - The script never reads the student's name or email. The
 *     bucket id is on <body data-kvdb-bucket="...">.
 *   - All criterion ids are stable per-paper so the saved state
 *     follows the user across page loads on the same device.
 */
(function () {
  'use strict';

  var BUCKET = document.body.getAttribute('data-kvdb-bucket') || '';
  var STORAGE_PREFIX = 'examiner:feedback:';
  var BATCH_KEY = STORAGE_PREFIX + (BUCKET || 'unknown');

  // ---- storage helpers ----
  function storageKey(criterionId) { return BATCH_KEY + ':' + criterionId; }
  function loadLocal(criterionId) {
    try {
      var raw = localStorage.getItem(storageKey(criterionId));
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }
  function saveLocal(criterionId, payload) {
    try { localStorage.setItem(storageKey(criterionId), JSON.stringify(payload)); }
    catch (e) {}
  }
  function clearLocal(criterionId) {
    try { localStorage.removeItem(storageKey(criterionId)); }
    catch (e) {}
  }

  // ---- rail counter ----
  function updateRailCount() {
    var responded = 0;
    var total = 0;
    document.querySelectorAll('.criterion').forEach(function (c) {
      total++;
      var id = c.getAttribute('data-criterion-id');
      if (id && loadLocal(id)) responded++;
    });
    var el = document.getElementById('rail-feedback-count');
    if (el) el.textContent = String(responded);
    var btn = document.getElementById('send-all-btn');
    if (btn) btn.disabled = (responded === 0);
  }

  // ---- per-criterion wiring ----
  document.querySelectorAll('.criterion').forEach(function (critEl) {
    var id = critEl.getAttribute('data-criterion-id');
    var fb = critEl.querySelector('.feedback');
    if (!id || !fb) return;
    var btns = fb.querySelectorAll('.fb-btn');
    var disagreeNote = fb.querySelector('.fb-note[data-mode="disagree"]');
    var noteNote = fb.querySelector('.fb-note[data-mode="note"]');
    var saveBtn = fb.querySelector('.fb-save');
    var status = fb.querySelector('.fb-status');

    function currentNoteFor(mode) {
      if (mode === 'DISAGREE') return disagreeNote.value;
      if (mode === 'NOTE')     return noteNote.value;
      return '';
    }

    function applyState(mode, note, statusKind) {
      fb.setAttribute('data-mode', mode || '');
      btns.forEach(function (b) {
        if (b.getAttribute('data-mode') === mode) b.classList.add('active');
        else b.classList.remove('active');
      });
      if (mode === 'DISAGREE') disagreeNote.value = note;
      if (mode === 'NOTE')     noteNote.value = note;
      if (status) {
        status.className = 'fb-status' + (statusKind ? ' ' + statusKind : '');
        if (statusKind === 'saved')     status.textContent = '✓ saved locally';
        else if (statusKind === 'sent') status.textContent = '✓ sent';
        else if (statusKind === 'error') status.textContent = '! save failed';
        else status.textContent = '';
      }
    }

    // Restore from localStorage
    var saved = loadLocal(id);
    if (saved) applyState(saved.mode, saved.note || '', 'saved');

    btns.forEach(function (btn) {
      btn.addEventListener('click', function () {
        var mode = btn.getAttribute('data-mode');
        applyState(mode, currentNoteFor(mode), '');
        saveBtn.disabled = false;
      });
    });

    [disagreeNote, noteNote].forEach(function (note) {
      if (!note) return;
      note.addEventListener('input', function () {
        var mode = fb.getAttribute('data-mode');
        if (mode) saveBtn.disabled = false;
      });
    });

    saveBtn.addEventListener('click', function () {
      var mode = fb.getAttribute('data-mode');
      if (!mode) { applyState('', '', ''); return; }
      var note = currentNoteFor(mode);
      var payload = { mode: mode, note: note, saved_at: new Date().toISOString() };
      saveLocal(id, payload);
      applyState(mode, note, 'saved');
      saveBtn.disabled = true;
      updateRailCount();
    });
  });

  // ---- rail: send all + clear all ----
  var sendAllBtn = document.getElementById('send-all-btn');
  if (sendAllBtn) {
    sendAllBtn.addEventListener('click', function () {
      var items = [];
      document.querySelectorAll('.criterion').forEach(function (c) {
        var id = c.getAttribute('data-criterion-id');
        var saved = id ? loadLocal(id) : null;
        if (saved) items.push({ criterion_id: id, mode: saved.mode, note: saved.note || '' });
      });
      if (!items.length) return;
      if (!BUCKET) {
        alert('No KVdb bucket configured for this paper.');
        return;
      }
      var url = 'https://kvdb.io/' + BUCKET + '/student-feedback';
      sendAllBtn.disabled = true;
      sendAllBtn.textContent = 'Sending…';
      // Snapshot the ids we are about to upload, BEFORE the fetch resolves,
      // so we can mark only those criteria as "sent" on success. Previously
      // this loop marked every criterion on the page as "sent" — which was
      // misleading for criteria that had no localStorage entry (and thus
      // were silently skipped from the payload).
      var uploadedIds = items.map(function (it) { return it.criterion_id; });
      fetch(url, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ batch: BATCH_KEY, items: items, sent_at: new Date().toISOString() })
      }).then(function (r) {
        if (r.ok) {
          uploadedIds.forEach(function (cid) {
            var c = document.querySelector('.criterion[data-criterion-id="' + cid + '"]');
            if (!c) return;
            var s = c.querySelector('.fb-status');
            if (s) { s.className = 'fb-status saved'; s.textContent = '\u2713 sent'; }
          });
          // Reset the button to a clean state so the next click is unambiguous.
          sendAllBtn.disabled = false;
          sendAllBtn.textContent = 'Send all feedback';
        } else {
          sendAllBtn.disabled = false;
          sendAllBtn.textContent = 'Send all feedback';
          alert('Send failed: ' + r.status + ' ' + r.statusText);
        }
      }).catch(function (e) {
        sendAllBtn.disabled = false;
        sendAllBtn.textContent = 'Send all feedback';
        alert('Send failed: ' + e.message);
      });
    });
  }

  var clearAllBtn = document.getElementById('clear-all-btn');
  if (clearAllBtn) {
    clearAllBtn.addEventListener('click', function () {
      if (!confirm('Clear all responses for this paper?')) return;
      document.querySelectorAll('.criterion').forEach(function (c) {
        var id = c.getAttribute('data-criterion-id');
        if (id) clearLocal(id);
        var fb = c.querySelector('.feedback');
        if (fb) {
          fb.setAttribute('data-mode', '');
          fb.querySelectorAll('.fb-btn').forEach(function (b) { b.classList.remove('active'); });
          fb.querySelectorAll('.fb-note').forEach(function (n) { n.value = ''; });
          var s = fb.querySelector('.fb-status');
          if (s) { s.className = 'fb-status'; s.textContent = ''; }
          var sv = fb.querySelector('.fb-save');
          if (sv) sv.disabled = true;
        }
      });
      updateRailCount();
    });
  }

  // ---- accordion: open on click or keyboard activation ----
  document.querySelectorAll('.qhead').forEach(function (head) {
    head.addEventListener('click', function () {
      var section = head.closest('.qsection');
      var open = section.getAttribute('data-open') === 'true';
      section.setAttribute('data-open', open ? 'false' : 'true');
      head.setAttribute('aria-expanded', open ? 'false' : 'true');
    });
  });

  // ---- open the first two questions on desktop on first load ----
  if (window.matchMedia('(min-width: 1024px)').matches) {
    document.querySelectorAll('.qsection').forEach(function (s, i) {
      if (i < 2) {
        s.setAttribute('data-open', 'true');
        var head = s.querySelector('.qhead');
        if (head) head.setAttribute('aria-expanded', 'true');
      }
    });
  }

  updateRailCount();
})();
