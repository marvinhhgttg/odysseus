// Frontend counterpart to tests/test_tool_approval_routes.py::reload coverage.
//
// The backend now persists approvals across a restart and serves them from
// GET /api/tool-approvals?sessionId=... . This suite pins the browser half of
// that contract: what static/js/chat.js actually renders when sessions.js calls
// chatModule.recoverToolApprovals(id) after a page reload or session switch.
//
// These are the invariants that keep a persisted approval from turning into a
// silently stuck run (nothing rendered) or a double-executed tool (two cards,
// two resumes).
import test from 'node:test';
import assert from 'node:assert/strict';

import { loadApprovalModule, jsonResponse, readApprovalSource } from './approvalHarness.mjs';

const SESSION = 'session-a';
const OTHER_SESSION = 'session-b';

function approval(overrides = {}) {
  return {
    approvalId: 'apr-1',
    sessionId: SESSION,
    runId: 'run-1',
    tool: 'shell',
    risk: 'high',
    status: 'pending',
    expiresAt: '2030-01-01T00:00:00+00:00',
    ...overrides,
  };
}

function cardOf(mod, approvalId = 'apr-1') {
  return mod.cards().find(el => el.dataset.approvalId === approvalId) || null;
}

function buttonsOf(card) {
  return card.descendants().filter(el => el.tagName === 'BUTTON');
}

// ── Source-region guard ────────────────────────────────────────────────────

test('approval region is still sliceable out of chat.js', () => {
  const source = readApprovalSource();
  for (const name of [
    '_findToolApprovalCard',
    '_renderToolApproval',
    'recoverToolApprovals',
    '/api/tool-approvals',
  ]) {
    assert.ok(source.includes(name), `approval region no longer contains ${name}`);
  }
  assert.ok(!/\bexport\s+async\s+function\b/.test(source), 'export keyword must be stripped');
});

// ── Recovery rendering ─────────────────────────────────────────────────────

test('a persisted pending approval is rendered after session load', async () => {
  const mod = await loadApprovalModule({
    currentSessionId: SESSION,
    fetch: jsonResponse({ approvals: [approval()] }),
  });

  await mod.recoverToolApprovals(SESSION);

  const cards = mod.cards();
  assert.equal(cards.length, 1);

  const card = cards[0];
  assert.equal(card.dataset.approvalStatus, 'pending');
  assert.match(card.text, /Allow shell to run\?/);
  assert.match(card.text, /Risk: high/);
  // Recovered cards must be labelled as such, not as a live policy prompt.
  assert.match(card.text, /Source: recovered/);
  // The 60s auto-approve policy is surfaced as a live countdown.
  assert.match(card.text, /Auto-approves in \d+s…/);

  const labels = buttonsOf(card).map(b => b.textContent);
  assert.deepEqual(labels, ['Approve', 'Reject']);
});

test('recovery queries the owner-scoped session endpoint', async () => {
  const mod = await loadApprovalModule({
    currentSessionId: SESSION,
    apiBase: 'http://localhost:9001',
    fetch: jsonResponse({ approvals: [] }),
  });

  await mod.recoverToolApprovals(SESSION);

  assert.equal(mod.calls.fetch.length, 1);
  const [url, init] = mod.calls.fetch[0];
  assert.equal(url, `http://localhost:9001/api/tool-approvals?sessionId=${SESSION}`);
  assert.equal(init.credentials, 'same-origin');
});

test('a pending card arms the auto-approve countdown and stops it on decision', async () => {
  const mod = await loadApprovalModule({
    currentSessionId: SESSION,
    fetch: jsonResponse({ approvals: [approval()] }),
  });

  await mod.recoverToolApprovals(SESSION);

  // The 60s policy renders as a repeating half-second countdown ticker.
  assert.equal(mod.calls.intervals.length, 1);
  assert.equal(mod.calls.intervals[0].ms, 500);

  await buttonsOf(cardOf(mod)).find(b => b.textContent === 'Reject').click();

  // Deciding the card arms nothing more; the interval is cleared.
  assert.equal(mod.calls.clearedIntervals.length, 1);
});

test('recovery works before init() sets an absolute API base', async () => {
  // Regression pin: API_BASE is '' until chatModule.init() runs. Building the
  // recovery URL with new URL('/api/tool-approvals') throws TypeError in that
  // window, and this function's catch would swallow it — no cards, no error.
  const mod = await loadApprovalModule({
    currentSessionId: SESSION,
    apiBase: '',
    fetch: jsonResponse({ approvals: [approval()] }),
  });

  await mod.recoverToolApprovals(SESSION);

  assert.equal(mod.calls.warnings.length, 0, 'recovery must not fail on a relative API base');
  assert.equal(mod.calls.fetch[0][0], `/api/tool-approvals?sessionId=${SESSION}`);
  assert.equal(mod.cards().length, 1);
});

test('the session id is encoded into the recovery query', async () => {
  const weird = 'session a/b?c';
  const mod = await loadApprovalModule({
    currentSessionId: weird,
    apiBase: 'http://localhost:9001',
    fetch: jsonResponse({ approvals: [] }),
  });

  await mod.recoverToolApprovals(weird);

  assert.equal(
    mod.calls.fetch[0][0],
    'http://localhost:9001/api/tool-approvals?sessionId=session%20a%2Fb%3Fc',
  );
});

test('an already-approved approval recovers as a resumable card', async () => {
  const mod = await loadApprovalModule({
    currentSessionId: SESSION,
    fetch: jsonResponse({ approvals: [approval({ status: 'approved' })] }),
  });

  await mod.recoverToolApprovals(SESSION);

  const card = cardOf(mod);
  assert.ok(card);
  assert.equal(card.dataset.approvalStatus, 'approved');
  // Approving twice would re-decide a decided approval; the only affordance
  // left must be the local resume.
  const labels = buttonsOf(card).map(b => b.textContent);
  assert.deepEqual(labels, ['Resume']);
  assert.match(card.text, /Approved\. Ready to resume\./);
});

// ── Filtering invariants ───────────────────────────────────────────────────

test('approvals belonging to another session are not rendered', async () => {
  const mod = await loadApprovalModule({
    currentSessionId: SESSION,
    fetch: jsonResponse({
      approvals: [approval({ approvalId: 'apr-other', sessionId: OTHER_SESSION })],
    }),
  });

  await mod.recoverToolApprovals(SESSION);

  assert.equal(mod.cards().length, 0);
});

test('an approval payload without sessionId is not rendered', async () => {
  // Regression guard for the route contract: _approval_recovery_response must
  // keep emitting sessionId. Drop it and every recovered approval is filtered
  // out here, leaving the user with an invisible, unresumable run.
  const { sessionId, ...withoutSession } = approval();
  const mod = await loadApprovalModule({
    currentSessionId: SESSION,
    fetch: jsonResponse({ approvals: [withoutSession] }),
  });

  await mod.recoverToolApprovals(SESSION);

  assert.equal(mod.cards().length, 0);
});

test('decided or expired approvals are not resurrected', async () => {
  const mod = await loadApprovalModule({
    currentSessionId: SESSION,
    fetch: jsonResponse({
      approvals: [
        approval({ approvalId: 'apr-rejected', status: 'rejected' }),
        approval({ approvalId: 'apr-expired', status: 'expired' }),
        approval({ approvalId: 'apr-consumed', status: 'consumed' }),
        approval({ approvalId: 'apr-live', status: 'pending' }),
      ],
    }),
  });

  await mod.recoverToolApprovals(SESSION);

  assert.deepEqual(
    mod.cards().map(c => c.dataset.approvalId),
    ['apr-live'],
  );
});

test('recovery is idempotent across repeated session loads', async () => {
  // sessions.js fires recovery on every session load; switching away and back
  // must not stack duplicate cards for one approval.
  const mod = await loadApprovalModule({
    currentSessionId: SESSION,
    fetch: jsonResponse({ approvals: [approval()] }),
  });

  await mod.recoverToolApprovals(SESSION);
  await mod.recoverToolApprovals(SESSION);
  await mod.recoverToolApprovals(SESSION);

  assert.equal(mod.cards().length, 1);
});

test('a session switch during the request discards the response', async () => {
  const mod = await loadApprovalModule({
    currentSessionId: SESSION,
    fetch: async () => {
      mod.setCurrentSessionId(OTHER_SESSION);
      return {
        ok: true,
        status: 200,
        json: async () => ({ approvals: [approval()] }),
      };
    },
  });

  await mod.recoverToolApprovals(SESSION);

  assert.equal(mod.cards().length, 0);
});

test('an empty session id performs no request', async () => {
  const mod = await loadApprovalModule({
    currentSessionId: SESSION,
    fetch: jsonResponse({ approvals: [approval()] }),
  });

  await mod.recoverToolApprovals('');
  await mod.recoverToolApprovals('   ');
  await mod.recoverToolApprovals(null);
  await mod.recoverToolApprovals(undefined);

  assert.equal(mod.calls.fetch.length, 0);
  assert.equal(mod.cards().length, 0);
});

// ── Failure handling ───────────────────────────────────────────────────────

test('a rejected recovery request degrades quietly', async () => {
  const mod = await loadApprovalModule({
    currentSessionId: SESSION,
    fetch: jsonResponse({ detail: 'approval unavailable' }, 409),
  });

  await mod.recoverToolApprovals(SESSION);

  assert.equal(mod.cards().length, 0);
  assert.equal(mod.calls.warnings.length, 1);
  assert.match(String(mod.calls.warnings[0][1]), /approval unavailable/);
});

test('a network failure never breaks session loading', async () => {
  const mod = await loadApprovalModule({
    currentSessionId: SESSION,
    fetch: async () => {
      throw new Error('offline');
    },
  });

  await assert.doesNotReject(() => mod.recoverToolApprovals(SESSION));
  assert.equal(mod.cards().length, 0);
  assert.equal(mod.calls.warnings.length, 1);
});

test('a malformed payload yields no cards', async () => {
  for (const body of [null, {}, { approvals: null }, { approvals: 'nope' }, { approvals: [null] }]) {
    const mod = await loadApprovalModule({
      currentSessionId: SESSION,
      fetch: jsonResponse(body),
    });
    await mod.recoverToolApprovals(SESSION);
    assert.equal(mod.cards().length, 0, `unexpected card for payload ${JSON.stringify(body)}`);
  }
});

test('an approval missing runId is refused', async () => {
  // runId is what the chat stream needs to resume; a card without it would
  // render an Approve button that can never continue the run.
  const { runId, ...withoutRun } = approval();
  const mod = await loadApprovalModule({
    currentSessionId: SESSION,
    fetch: jsonResponse({ approvals: [withoutRun] }),
  });

  await mod.recoverToolApprovals(SESSION);

  assert.equal(mod.cards().length, 0);
  assert.equal(mod.calls.errors.length, 1);
});

// ── Decisions on a recovered card ──────────────────────────────────────────

test('approving a recovered card posts the decision and arms the resume', async () => {
  const responses = [
    { approvals: [approval({ approvalId: 'apr with/slash' })] },
    { approvalId: 'apr with/slash', sessionId: SESSION, runId: 'run-1', status: 'approved' },
  ];
  let call = 0;
  const mod = await loadApprovalModule({
    currentSessionId: SESSION,
    fetch: async () => ({
      ok: true,
      status: 200,
      json: async () => responses[call++],
    }),
  });

  await mod.recoverToolApprovals(SESSION);
  const card = cardOf(mod, 'apr with/slash');
  assert.ok(card);

  let submitted = 0;
  mod.sendButton.addEventListener('click', () => {
    submitted += 1;
  });

  await buttonsOf(card).find(b => b.textContent === 'Approve').click();

  const [decisionUrl, init] = mod.calls.fetch[1];
  assert.equal(decisionUrl, '/api/tool-approvals/apr%20with%2Fslash/approve');
  assert.equal(init.method, 'POST');
  assert.equal(init.credentials, 'same-origin');

  assert.equal(card.dataset.approvalStatus, 'approved');
  assert.equal(submitted, 1, 'resume must be submitted exactly once');
  assert.equal(mod.messageInput.value, 'Continue the approved tool call.');

  const state = mod.state();
  assert.deepEqual(state.pendingApprovalResume, {
    approvalId: 'apr with/slash',
    runId: 'run-1',
    sessionId: SESSION,
  });
  assert.equal(state.hideUserBubble, true);
});

test('rejecting a recovered card clears its actions', async () => {
  const responses = [
    { approvals: [approval()] },
    { approvalId: 'apr-1', sessionId: SESSION, runId: 'run-1', status: 'rejected' },
  ];
  let call = 0;
  const mod = await loadApprovalModule({
    currentSessionId: SESSION,
    fetch: async () => ({ ok: true, status: 200, json: async () => responses[call++] }),
  });

  await mod.recoverToolApprovals(SESSION);
  const card = cardOf(mod);
  await buttonsOf(card).find(b => b.textContent === 'Reject').click();

  assert.equal(mod.calls.fetch[1][0], '/api/tool-approvals/apr-1/reject');
  assert.equal(card.dataset.approvalStatus, 'rejected');
  assert.equal(buttonsOf(card).length, 0);
  assert.match(card.text, /Rejected\./);
  assert.equal(mod.state().pendingApprovalResume, null);
});

test('a decision refused by the server leaves the card actionable', async () => {
  const responses = [
    { ok: true, status: 200, json: async () => ({ approvals: [approval()] }) },
    { ok: false, status: 409, json: async () => ({ detail: 'approval expired' }) },
  ];
  let call = 0;
  const mod = await loadApprovalModule({
    currentSessionId: SESSION,
    fetch: async () => responses[call++],
  });

  await mod.recoverToolApprovals(SESSION);
  const card = cardOf(mod);
  await buttonsOf(card).find(b => b.textContent === 'Approve').click();

  // Still pending, buttons re-enabled, and the server reason surfaced.
  assert.equal(card.dataset.approvalStatus, 'pending');
  assert.deepEqual(buttonsOf(card).map(b => b.disabled), [false, false]);
  assert.match(card.text, /approval expired/);
  assert.equal(mod.state().pendingApprovalResume, null);
  assert.equal(mod.state().hideUserBubble, false);
});

test('resuming an approval from another session is refused locally', async () => {
  const mod = await loadApprovalModule({
    currentSessionId: SESSION,
    fetch: jsonResponse({ approvals: [approval({ status: 'approved' })] }),
  });

  await mod.recoverToolApprovals(SESSION);
  const card = cardOf(mod);

  // User switches chats, then clicks Resume on the stale card.
  mod.setCurrentSessionId(OTHER_SESSION);

  let submitted = 0;
  mod.sendButton.addEventListener('click', () => {
    submitted += 1;
  });

  await buttonsOf(card).find(b => b.textContent === 'Resume').click();

  assert.equal(submitted, 0, 'must not resume into the wrong chat');
  assert.equal(mod.state().pendingApprovalResume, null);
  assert.match(card.text, /This approval belongs to a different chat\./);
});
