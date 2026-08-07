// Loads the real tool-approval recovery/render logic out of static/js/chat.js and
// runs it under Node against a minimal DOM. chat.js is a ~5.8k-line browser module
// with ~20 sibling imports, so importing it whole under Node is neither possible
// nor useful here. Instead this harness slices the exact approval source region
// out of the real file and evaluates it with explicit stubs, mirroring the
// source-surgery loader in tests/streaming/markdownHarness.mjs. The slice is taken
// by marker text, so the test breaks loudly if the region is renamed or moved
// rather than silently drifting away from the shipped code.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const CHAT_JS = path.join(REPO, 'static/js/chat.js');

const START_MARKER = 'function _approvalErrorText(payload, fallback) {';
const END_MARKER = 'export async function handleChatSubmit(e) {';

export function readApprovalSource() {
  const src = fs.readFileSync(CHAT_JS, 'utf8');
  const start = src.indexOf(START_MARKER);
  const end = src.indexOf(END_MARKER);
  if (start < 0) throw new Error(`approval region start marker not found in ${CHAT_JS}`);
  if (end < 0) throw new Error(`approval region end marker not found in ${CHAT_JS}`);
  if (end <= start) throw new Error('approval region markers are out of order');
  return src.slice(start, end).replace(/\bexport\s+async\s+function\b/g, 'async function');
}

// ── Minimal DOM ────────────────────────────────────────────────────────────
// Only the surface the approval code actually touches: element creation,
// append/replaceChildren, dataset, textContent, click listeners, and the two
// attribute/class selectors used by _findToolApprovalCard and startResume.

class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.childNodes = [];
    this.parentNode = null;
    this.dataset = {};
    this.style = { cssText: '' };
    this.className = '';
    this.textContent = '';
    this.type = '';
    this.value = '';
    this.disabled = false;
    this._listeners = new Map();
  }

  get classNames() {
    return String(this.className).split(/\s+/).filter(Boolean);
  }

  append(...nodes) {
    for (const node of nodes) {
      node.parentNode = this;
      this.childNodes.push(node);
    }
  }

  appendChild(node) {
    this.append(node);
    return node;
  }

  replaceChildren(...nodes) {
    this.childNodes = [];
    this.append(...nodes);
  }

  addEventListener(type, handler) {
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    this._listeners.get(type).push(handler);
  }

  async click() {
    for (const handler of this._listeners.get('click') || []) {
      await handler({ type: 'click' });
    }
  }

  descendants() {
    const out = [];
    for (const child of this.childNodes) {
      out.push(child, ...child.descendants());
    }
    return out;
  }

  // Recursively collect textContent for assertions.
  get text() {
    if (this.childNodes.length === 0) return this.textContent;
    return [this.textContent, ...this.childNodes.map(c => c.text)].filter(Boolean).join(' ');
  }
}

function matches(element, selector) {
  const attrMatch = selector.match(/\[([a-zA-Z-]+)\]$/);
  const base = attrMatch ? selector.slice(0, attrMatch.index) : selector;

  if (attrMatch) {
    const prop = attrMatch[1]
      .replace(/^data-/, '')
      .replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    if (element.dataset[prop] === undefined) return false;
  }

  for (const cls of base.split('.').filter(Boolean)) {
    if (!element.classNames.includes(cls)) return false;
  }
  return true;
}

export function createDom() {
  const root = new El('body');
  const chatBox = new El('div');
  const messageInput = new El('textarea');
  const sendButton = new El('button');
  sendButton.className = 'send-btn';

  const byId = { 'chat-history': chatBox, message: messageInput };

  const document = {
    getElementById: id => byId[id] || null,
    createElement: tag => new El(tag),
    querySelectorAll(selector) {
      if (selector === '.send-btn') return [sendButton];
      return root.descendants().filter(el => matches(el, selector));
    },
    querySelector(selector) {
      return document.querySelectorAll(selector)[0] || null;
    },
  };

  root.append(chatBox);

  return { document, root, chatBox, messageInput, sendButton, El };
}

// ── Module loader ──────────────────────────────────────────────────────────

/**
 * Evaluate the approval region with injected dependencies.
 *
 * @param {object} options
 * @param {string} options.currentSessionId  value returned by sessionModule.getCurrentSessionId()
 * @param {Function} options.fetch           fetch stub
 * @param {string} [options.apiBase]
 */
export async function loadApprovalModule(options = {}) {
  const dom = createDom();
  const calls = { fetch: [], scrolls: 0, warnings: [], errors: [] };

  let currentSessionId = options.currentSessionId || '';

  const sessionModule = {
    getCurrentSessionId: () => currentSessionId,
  };

  const uiModule = {
    el: id => dom.document.getElementById(id),
    scrollHistory: () => {
      calls.scrolls += 1;
    },
  };

  const fetchStub = async (...args) => {
    calls.fetch.push(args);
    return options.fetch(...args);
  };

  const source = readApprovalSource();
  const wrapped = `
    export default function boot(deps) {
      const { document, sessionModule, uiModule, fetch, URL, console } = deps;
      let API_BASE = deps.API_BASE;
      let _pendingApprovalResume = null;
      let _hideUserBubble = false;

      ${source}

      return {
        recoverToolApprovals,
        renderToolApproval: _renderToolApproval,
        findToolApprovalCard: _findToolApprovalCard,
        approvalErrorText: _approvalErrorText,
        state: () => ({
          pendingApprovalResume: _pendingApprovalResume,
          hideUserBubble: _hideUserBubble,
        }),
      };
    }
  `;

  const url = 'data:text/javascript;base64,' + Buffer.from(wrapped).toString('base64');
  const mod = await import(url);

  const api = mod.default({
    document: dom.document,
    sessionModule,
    uiModule,
    fetch: fetchStub,
    URL,
    API_BASE: options.apiBase ?? '',
    console: {
      warn: (...a) => calls.warnings.push(a),
      error: (...a) => calls.errors.push(a),
      log: () => {},
    },
  });

  return {
    ...api,
    ...dom,
    calls,
    setCurrentSessionId(value) {
      currentSessionId = value;
    },
    cards: () => dom.document.querySelectorAll('.approval-request[data-approval-id]'),
  };
}

/** Build a fetch stub that answers with `body` and `status`. */
export function jsonResponse(body, status = 200) {
  return async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  });
}
