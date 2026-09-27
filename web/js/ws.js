// ws.js —— WebSocket 客户端：自动重连 + 事件分发

const handlers = {};          // type → [fn...]
let sock = null;
let backoff = 1500;
let closedByUser = false;

function dispatch(type, data) {
  (handlers[type] || []).forEach(fn => {
    try { fn(data); } catch (e) { console.error('[ws handler]', type, e); }
  });
}

export const WS = {
  /** 订阅事件；返回解绑函数 */
  on(type, fn) {
    (handlers[type] = handlers[type] || []).push(fn);
    return () => {
      const arr = handlers[type] || [];
      const i = arr.indexOf(fn);
      if (i >= 0) arr.splice(i, 1);
    };
  },

  get connected() { return sock && sock.readyState === WebSocket.OPEN; },

  connect() {
    closedByUser = false;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    try {
      sock = new WebSocket(`${proto}//${location.host}/ws`);
    } catch (e) {
      setTimeout(() => WS.connect(), backoff);
      return;
    }
    sock.onopen = () => { backoff = 1500; };
    sock.onmessage = (ev) => {
      let m = null;
      try { m = JSON.parse(ev.data); } catch { return; }
      if (m && m.type) dispatch(m.type, m.data);
    };
    sock.onclose = () => {
      sock = null;
      if (!closedByUser) {
        setTimeout(() => WS.connect(), backoff);
        backoff = Math.min(backoff * 1.6, 15000);
      }
    };
    sock.onerror = () => { try { sock && sock.close(); } catch { /* ignore */ } };
  },

  close() {
    closedByUser = true;
    try { sock && sock.close(); } catch { /* ignore */ }
  },

  /** 保活（应用后端 30s 无消息即断；收到 hello/status 即无需 ping） */
  ping() {
    if (WS.connected) {
      try { sock.send(JSON.stringify({ type: 'ping' })); } catch { /* ignore */ }
    }
  },
};

setInterval(() => WS.ping(), 20000);
