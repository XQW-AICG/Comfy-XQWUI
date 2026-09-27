// api.js —— REST 请求封装（fetch + JSON + 统一错误）

import { toast } from './core.js';

async function req(method, url, body, raw = false) {
  const opt = { method, headers: {} };
  if (body instanceof FormData) {
    opt.body = body;
  } else if (body !== undefined && body !== null) {
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch(url, opt);
  } catch (e) {
    throw new Error('网络请求失败: ' + e.message);
  }
  let data = null;
  try { data = await r.json(); } catch { /* 非 JSON 响应 */ }
  if (!r.ok) {
    const msg = (data && (data.error?.message || data.error)) || r.statusText;
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return data;
}

export const API = {
  get: (url) => req('GET', url),
  post: (url, body) => req('POST', url, body),
  del: (url) => req('DELETE', url),

  /** multipart 文件上传，返回响应 JSON */
  async upload(url, file, field = 'file') {
    const fd = new FormData();
    fd.append(field, file, file.name);
    return req('POST', url, fd);
  },

  /** 静默请求：失败 toast 并返回 null（用于非关键路径） */
  async soft(promise, label) {
    try {
      return await promise;
    } catch (e) {
      toast((label ? label + '：' : '') + e.message, 'err');
      return null;
    }
  },
};
