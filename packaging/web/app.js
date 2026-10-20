/* ---------------------------------------------------------------------------
   WBBridge 本地控制台
   · 全部数据来自同源 HTTP 端点（/status /ble /conversations /messages /voice
     /logs /config ...），1.5 秒轮询；页面只做渲染，不持业务状态。
   · 交互（打开日志/停止服务）优先走 WKWebView 的原生桥，浏览器里打开时
     退化成提示。
   --------------------------------------------------------------------------- */

const $  = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ESC[c]);

const state = {
  page: 'dashboard',
  online: false,
  status: null,
  ble: null,
  health: null,
  convs: [],
  convSig: '',
  selConv: null,
  messages: [],
  msgSig: '',
  msgsLoading: false,
  voice: [],
  voiceDir: '',
  voiceSig: '',
  selVoice: null,
  transcripts: {},
  playing: null,
  logLines: [],
  logCount: 0,
  config: null,
  tick: 0,
  targetConv: '',
};

/* ------------------------------ 工具 ------------------------------ */

async function jget(path, timeoutMs = 6000) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(path, { signal: ctl.signal, cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return await r.json();
  } finally {
    clearTimeout(timer);
  }
}

function toast(msg, kind = 'ok') {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast ' + (kind === 'err' ? 'err' : 'ok');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add('hidden'), 3200);
}

function busy(text) {
  const el = $('#busy');
  if (!text) { el.classList.add('hidden'); return; }
  $('#busyTxt').textContent = text;
  el.classList.remove('hidden');
}

function native(cmd, payload = {}) {
  const h = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.wb;
  if (h) { h.postMessage(Object.assign({ cmd }, payload)); return true; }
  return false;
}

function fmtRel(ms) {
  if (!ms) return '—';
  const d = new Date(ms);
  const now = Date.now();
  const diff = Math.floor((now - ms) / 1000);
  if (d.toDateString() === new Date().toDateString()) {
    if (diff < 60) return '刚刚';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    return d.toTimeString().slice(0, 5);
  }
  const y = new Date(now - 86400000);
  if (d.toDateString() === y.toDateString()) return '昨天 ' + d.toTimeString().slice(0, 5);
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${d.toTimeString().slice(0, 5)}`;
}

function fmtBytes(n) {
  n = Number(n || 0);
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1048576).toFixed(1) + ' MB';
}

function shortPeer(p) {
  if (!p) return '—';
  return p.length > 17 ? p.slice(0, 17) : p;
}

function statusColor(s) {
  return s === 'active' ? 'ok' : s === 'working' ? 'warn' : '';
}

/* ------------------------------ 轮询 ------------------------------ */

let polling = false;

async function tick() {
  if (polling) return;
  polling = true;
  state.tick++;
  try {
    const [status, ble] = await Promise.all([
      jget('/status', 2500),
      jget('/ble', 2500),
    ]);
    state.status = status;
    state.ble = ble;
    state.online = true;

    const conv = await jget('/conversations?limit=40', 4000).catch(() => null);
    if (conv && Array.isArray(conv.items)) {
      state.convs = conv.items;
      if (!state.selConv && conv.items.length) {
        const cur = status.conv_id && conv.items.find((c) => c.id === status.conv_id);
        state.selConv = (cur || conv.items[0]).id;
      }
      renderConvList();
      renderTargetSelect();
    }

    if (state.tick % 4 === 0) {
      const h = await jget('/health', 4000).catch(() => null);
      if (h) state.health = h;
    }

    if (state.tick % 3 === 0 || state.page === 'voice') {
      const v = await jget('/voice', 6000).catch(() => null);
      if (v) {
        state.voice = v.items || [];
        state.voiceDir = v.dir || '';
        renderVoiceList();
        renderDashVoice();
      }
    }

    if (state.page === 'conversations' && state.selConv && state.tick % 2 === 0) {
      loadMessages();
    }
    if (state.page === 'logs') loadLogs();

    renderGlobal();
    renderDash();
  } catch (e) {
    state.online = false;
    renderGlobal();
    renderDash();
    setServiceDown();
  } finally {
    polling = false;
  }
}

/* ------------------------------ 全局状态 ------------------------------ */

function renderGlobal() {
  const b = state.ble || {};
  const connected = !!b.connected;
  const dot = $('#railDot');
  dot.className = 'dot ' + (state.online ? (connected ? 'ok' : 'warn') : 'bad');
  $('#railSvc').textContent = state.online ? '服务运行中' : '服务未连接';
  const st = state.status || {};
  $('#railPid').textContent = state.online && st.pid ? 'pid ' + st.pid : '';
  $('#railBle').textContent = connected
    ? `工牌 ${shortPeer(b.peer)} 已连接 · MTU ${b.mtu || 0}`
    : (state.online ? '工牌未连接' : '—');

  const pill = $('#pillSvc');
  pill.textContent = state.online ? 'ONLINE' : 'OFFLINE';
  pill.className = 'pill ' + (state.online ? 'ok' : 'bad');

  $('#navConvCount').textContent = state.convs.length || '';
  $('#navVoiceCount').textContent = state.voice.length || '';

  const s = state.status || {};
  $('#pillModel').textContent = s.model || (state.health && state.health.whisper ? state.health.whisper.model : '—');
}

function setServiceDown() {
  $('#svcBig').textContent = '未连接';
  $('#svcBig').classList.add('dim');
  $('#svcDot').className = 'dot bad';
  $('#bleBig').textContent = '—';
  $('#bleDot').className = 'dot bad';
}

/* ------------------------------ 总览 ------------------------------ */

function renderDash() {
  const s = state.status || {};
  const b = state.ble || {};
  const h = state.health || {};

  // 服务
  $('#svcBig').textContent = state.online ? '运行中' : '未运行';
  $('#svcBig').classList.toggle('dim', !state.online);
  $('#svcDot').className = 'dot ' + (state.online ? 'ok' : 'bad');
  $('#svcPid').textContent = state.online && s.pid ? String(s.pid) : '—';
  $('#svcPort').textContent = '127.0.0.1:' + (s.port || location.port || 8765);
  $('#svcAsr').textContent = h.whisper
    ? `${h.whisper.model || '?'}${h.whisper.cached ? '' : ' (未缓存)'}`
    : '—';

  // 工牌
  const connected = !!b.connected;
  $('#bleBig').textContent = connected ? '已连接' : (state.online ? '未连接' : '—');
  $('#bleBig').classList.toggle('dim', !connected);
  $('#bleDot').className = 'dot ' + (connected ? 'ok' : (state.online ? 'warn' : 'bad'));
  $('#blePeer').textContent = shortPeer(b.peer);
  $('#bleMtu').textContent = b.mtu || '—';
  $('#bleFrames').textContent = b.pushed != null
    ? `${b.pushed} 帧 / 上行 ${fmtBytes(b.received)}` : '—';
  $('#bleErr').textContent = (!connected && b.error) ? b.error : '';

  // WorkBuddy
  const isBusy = (s.state || 0) === 1;
  const wbPill = $('#wbPill');
  wbPill.textContent = s.state_name || (state.online ? (isBusy ? 'BUSY' : 'IDLE') : '—');
  wbPill.className = 'pill ' + (isBusy ? '' : 'ok');
  $('#wbBig').textContent = isBusy ? '正在跑任务' : (state.online ? '空闲' : '—');
  $('#wbTask').textContent = s.task || '—';
  $('#wbTask').title = s.task || '';
  $('#wbModel').textContent = s.model || '—';
  $('#wbUptime').textContent = s.uptime_s > 0
    ? (s.uptime_s < 60 ? s.uptime_s + ' 秒' : s.uptime_s < 3600 ? Math.floor(s.uptime_s / 60) + ' 分钟' : Math.floor(s.uptime_s / 3600) + ' 小时')
    : '—';
  $('#wbPercent').textContent = s.percent != null ? Number(s.percent).toFixed(0) + '%' : '—';

  renderDashConvs();
  renderDashVoice();
}

function renderDashConvs() {
  const box = $('#dashConvs');
  if (!state.convs.length) {
    box.innerHTML = '<div class="hint" style="padding:6px 8px">没有读到会话（WorkBuddy 至少打开过一个对话）</div>';
    return;
  }
  box.innerHTML = state.convs.slice(0, 5).map((c) => `
    <div class="row" data-conv="${esc(c.id)}">
      <span class="dot ${statusColor(c.status)}"></span>
      <span class="title">${esc(c.title)}</span>
      <span class="meta">${esc(fmtRel(c.updated_at))}</span>
    </div>`).join('');
}

function renderDashVoice() {
  const box = $('#dashVoice');
  const items = state.voice.slice(-5).reverse();
  if (!items.length) {
    box.innerHTML = '<div class="hint" style="padding:6px 8px">还没有录音 —— 按工牌 OK 说一句话就会出现在这里</div>';
    return;
  }
  box.innerHTML = items.map((v) => `
    <div class="row" data-voice="${esc(v.name)}">
      <button class="play" data-play="${esc(v.name)}">${state.playing === v.name ? '❙❙' : '▶'}</button>
      <span class="title">${esc(v.name.replace(/\.wav$/, ''))}</span>
      <span class="meta">${v.duration.toFixed(1)}s · ${esc(fmtBytes(v.size))}</span>
      ${state.transcripts[v.name] ? '<span class="dot ok"></span>' : ''}
    </div>`).join('');
}

/* ------------------------------ 对话 ------------------------------ */

function renderConvList() {
  const q = ($('#convSearch').value || '').trim().toLowerCase();
  const items = q ? state.convs.filter((c) => (c.title || '').toLowerCase().includes(q)) : state.convs;
  const sig = items.map((c) => c.id + c.status + c.title).join('|') + '#' + state.selConv
    + '#' + state.messages.length;
  const box = $('#convList');
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;

  box.innerHTML = items.map((c) => `
    <div class="row ${c.id === state.selConv ? 'sel' : ''}" data-conv="${esc(c.id)}">
      <span class="dot ${statusColor(c.status)}"></span>
      <div style="flex:1;min-width:0">
        <div class="title ${c.id === state.selConv ? '' : 'dim'}">${esc(c.title)}</div>
        <div class="sub">${esc(fmtRel(c.updated_at))}${c.model ? ' · ' + esc(c.model) : ''}</div>
      </div>
    </div>`).join('') || '<div class="hint" style="padding:10px">没有匹配的对话</div>';

  const cur = state.convs.find((c) => c.id === state.selConv);
  $('#convTitle').textContent = cur ? cur.title : '选择一个对话';
  $('#convMeta').textContent = cur
    ? `${cur.status} · ${state.messages.length} 条消息${cur.model ? ' · ' + cur.model : ''}`
    : '';
}

async function loadMessages() {
  if (!state.selConv || state.msgsLoading) return;
  state.msgsLoading = true;
  try {
    const m = await jget(`/messages?id=${encodeURIComponent(state.selConv)}&size=80&len=6000`, 8000);
    const msgs = (m && m.messages) || [];
    const sig = msgs.length + '#' + (msgs.length ? msgs[msgs.length - 1].text.slice(0, 24) : '');
    if (sig !== state.msgSig) {
      state.msgSig = sig;
      state.messages = msgs;
      renderMessages();
      renderConvList();
    }
  } catch (e) { /* 忽略单次失败 */ }
  finally { state.msgsLoading = false; }
}

function renderMessages() {
  const box = $('#msgList');
  if (!state.selConv) {
    box.innerHTML = '<div class="hint" style="margin:auto">左边选一个对话</div>';
    return;
  }
  if (!state.messages.length) {
    box.innerHTML = '<div class="hint" style="margin:auto">这个对话还没有消息记录</div>';
    return;
  }
  box.innerHTML = state.messages.map((m) => `
    <div class="msg ${m.role === 'user' ? 'user' : 'bot'}">
      <div class="who">${m.role === 'user' ? '你（工牌 / 输入）' : 'WorkBuddy'}</div>
      <div class="body">${esc(m.text)}</div>
    </div>`).join('');
  box.scrollTop = box.scrollHeight;
}

function renderTargetSelect() {
  const sel = $('#targetConv');
  const sig = state.convs.map((c) => c.id).join('|');
  if (sel.dataset.sig === sig) return;
  sel.dataset.sig = sig;
  const keep = state.targetConv || '';
  sel.innerHTML = '<option value="">跟随工牌当前选择</option>' +
    state.convs.map((c) => `<option value="${esc(c.id)}">${esc(c.title)}</option>`).join('');
  sel.value = keep;
}

/* ------------------------------ 语音 ------------------------------ */

function renderVoiceList() {
  const box = $('#voiceList');
  const items = state.voice.slice(-80).reverse();
  const sig = items.map((v) => v.name).join('|') + '#' + state.selVoice + '#' + state.playing;
  $('#voiceCountLabel').textContent = '录音 ' + state.voice.length;
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;

  if (!items.length) {
    box.innerHTML = '<div class="hint" style="padding:12px">还没有录音</div>';
    return;
  }
  box.innerHTML = items.map((v) => `
    <div class="row ${v.name === state.selVoice ? 'sel' : ''}" data-voice="${esc(v.name)}">
      <button class="play" data-play="${esc(v.name)}">${state.playing === v.name ? '❙❙' : '▶'}</button>
      <div style="flex:1;min-width:0">
        <div class="sub" style="color:${v.name === state.selVoice ? 'var(--text)' : 'var(--dim)'}">${esc(v.name.replace(/\.wav$/, ''))}</div>
        <div class="sub">${v.duration.toFixed(1)}s · ${esc(fmtBytes(v.size))}</div>
      </div>
      ${state.transcripts[v.name] ? '<span class="dot ok"></span>' : ''}
    </div>`).join('');
  if (!state.selVoice && items.length) selectVoice(items[0].name);
}

function selectVoice(name) {
  state.selVoice = name;
  const v = state.voice.find((x) => x.name === name);
  $('#voiceName').textContent = name;
  $('#voiceSub').textContent = v ? `${v.duration.toFixed(2)} 秒 · ${fmtBytes(v.size)} · ${fmtRel(v.ts)}` : '';
  $('#transcript').value = state.transcripts[name] || '';
  $('#btnPlay').textContent = state.playing === name ? '❙❙' : '▶';
  renderVoiceList();
}

function togglePlay(name) {
  const audio = $('#player');
  if (state.playing === name) {
    audio.pause();
    state.playing = null;
  } else {
    audio.src = '/voice/wav?file=' + encodeURIComponent(name);
    audio.currentTime = 0;
    audio.play().catch(() => toast('无法播放（文件可能被删了）', 'err'));
    state.playing = name;
  }
  $('#btnPlay').textContent = state.playing === name ? '❙❙' : '▶';
  renderVoiceList();
  renderDashVoice();
}

/* ------------------------------ 日志 ------------------------------ */

async function loadLogs() {
  const r = await jget('/logs?tail=400', 6000).catch(() => null);
  if (!r) return;
  if (r.lines.length === state.logCount && state.logSig === r.lines.length) return;
  state.logCount = r.lines.length;
  const filter = ($('#logFilter').value || '').trim().toLowerCase();
  const quiet = $('#logQuiet') && $('#logQuiet').checked;
  let lines = r.lines;
  if (quiet) {
    // 工牌每到一帧就写一行 [BLE] ... pushed=N recv=0，全是心跳噪声；
    // 出问题时真正有用的是连接变化、[SEND]、[ASR]、Traceback。
    lines = lines.filter((l) => !/\[BLE\] .*pushed=\d+ recv=\d+$/.test(l) ||
                              /mtu=\d+ pushed=1\b/.test(l));
  }
  if (filter) lines = lines.filter((l) => l.toLowerCase().includes(filter));
  const cls = (l) => {
    if (/Traceback|错误|失败|ERROR/.test(l)) return 'l-err';
    if (l.includes('[SEND]')) return 'l-send';
    if (l.includes('[BLE]')) return 'l-ble';
    if (l.includes('[ASR]')) return 'l-asr';
    if (l.includes('[GW]')) return 'l-gw';
    if (l.startsWith('===')) return 'l-sep';
    if (l.includes('[app]') || l.includes('[menubar]')) return 'l-app';
    return '';
  };
  const view = $('#logView');
  view.innerHTML = lines
    .map((l) => `<span class="${cls(l)}">${esc(l)}</span>`)
    .join('\n');
  if ($('#logAuto').checked) view.scrollTop = view.scrollHeight;
}

/* ------------------------------ 设置 ------------------------------ */

async function loadConfig(force = false) {
  if (state.config && !force) return;
  const c = await jget('/config', 6000).catch(() => null);
  if (!c) return;
  state.config = c;
  const sel = $('#cfgModel');
  sel.innerHTML = (c.models || ['small']).map((m) => `<option value="${m}">${m}</option>`).join('');
  sel.value = c.model || 'small';
  $('#cfgLang').value = c.lang || 'zh';
  const w = c.whisper || {};
  $('#cfgInfo').innerHTML = [
    ['端口', '127.0.0.1:' + (c.port || 8765)],
    ['模型', `${c.model}（${w.cached ? '已缓存' : '未缓存'}，${w.loaded ? '已加载' : '未加载'}）`],
    ['语音目录', c.voice_dir || '—'],
    ['服务代码', c.bridge_dir || '—'],
    ['前端目录', c.web_dir || '—'],
    ['日志', c.log || '—'],
  ].map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
}

/* ------------------------------ 事件绑定 ------------------------------ */

function go(page) {
  if (!page || !document.getElementById('page-' + page)) page = 'dashboard';
  state.page = page;
  if (location.hash !== '#' + page) {
    history.replaceState(null, '', '#' + page);   // 深链接：/ui#voice 可直接打开
  }
  $$('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.page === page));
  $$('.page').forEach((p) => p.classList.toggle('hidden', p.id !== 'page-' + page));
  const titles = {
    dashboard: ['总览', '工牌语音 → 本地转写 → 投进 WorkBuddy 对话框'],
    conversations: ['对话', '点左边任意对话查看消息；底部可直发一句测试投递'],
    voice: ['语音', '试听、重新转写、把录音重新投进任意对话'],
    logs: ['日志', '~/.workbuddy/wb_bridge.log 最近 400 行'],
    settings: ['设置', '识别模型与运行环境'],
  };
  $('#pageTitle').textContent = titles[page][0];
  $('#pageSub').textContent = titles[page][1];

  if (page === 'conversations') { loadMessages(); renderConvList(); }
  if (page === 'voice') { renderVoiceList(); }
  if (page === 'logs') loadLogs();
  if (page === 'settings') loadConfig();
}

function bind() {
  $$('.nav-item').forEach((b) => b.addEventListener('click', () => go(b.dataset.page)));
  $$('[data-goto]').forEach((b) => b.addEventListener('click', () => go(b.dataset.goto)));

  // 对话
  $('#convList').addEventListener('click', (e) => {
    const row = e.target.closest('[data-conv]');
    if (!row) return;
    state.selConv = row.dataset.conv;
    state.msgSig = '';
    state.messages = [];
    renderConvList();
    renderMessages();
    loadMessages();
  });
  $('#dashConvs').addEventListener('click', (e) => {
    const row = e.target.closest('[data-conv]');
    if (!row) return;
    state.selConv = row.dataset.conv;
    state.msgSig = '';
    go('conversations');
  });
  $('#convSearch').addEventListener('input', renderConvList);
  $('#btnReloadMsgs').addEventListener('click', () => { state.msgSig = ''; loadMessages(); });
  $('#btnSend').addEventListener('click', sendDraft);
  $('#draft').addEventListener('keydown', (e) => { if (e.key === 'Enter') sendDraft(); });

  // 语音
  document.addEventListener('click', (e) => {
    const play = e.target.closest('[data-play]');
    if (play) { e.stopPropagation(); togglePlay(play.dataset.play); return; }
    const row = e.target.closest('[data-voice]');
    if (row && state.page === 'voice') { selectVoice(row.dataset.voice); return; }
    if (row && state.page === 'dashboard') { go('voice'); selectVoice(row.dataset.voice); }
  });
  $('#btnPlay').addEventListener('click', () => { if (state.selVoice) togglePlay(state.selVoice); });
  $('#player').addEventListener('ended', () => {
    state.playing = null;
    $('#btnPlay').textContent = '▶';
    renderVoiceList(); renderDashVoice();
  });
  $('#btnAsr').addEventListener('click', async () => {
    if (!state.selVoice) return;
    busy('转写中：' + state.selVoice);
    try {
      const r = await jget('/asr?file=' + encodeURIComponent(state.selVoice), 300000);
      busy(null);
      if (r.ok) {
        state.transcripts[state.selVoice] = r.text;
        $('#transcript').value = r.text;
        toast(`转写完成（${(r.ms / 1000).toFixed(1)}s）：${r.text.slice(0, 26)}`);
      } else {
        toast('转写失败：' + (r.error || '未知'), 'err');
      }
    } catch (err) { busy(null); toast('转写请求失败', 'err'); }
  });
  $('#btnDeliver').addEventListener('click', async () => {
    if (!state.selVoice) return;
    const text = $('#transcript').value.trim();
    state.transcripts[state.selVoice] = text;
    const target = $('#targetConv').value;
    const q = new URLSearchParams({ file: state.selVoice, wait: '15' });
    if (text) q.set('text', text);
    if (target) q.set('conv', target);
    busy('投递中（等目标会话空闲，最多 15 秒）…');
    try {
      const r = await jget('/redeliver?' + q.toString(), 120000);
      busy(null);
      if (r.ok) {
        toast('已投递：' + (r.message || 'Sent'));
        state.msgSig = '';
        loadMessages();
      } else {
        toast('投递失败：' + (r.message || r.error || '未知'), 'err');
      }
    } catch (err) { busy(null); toast('投递请求失败', 'err'); }
  });
  $('#btnCopy').addEventListener('click', async () => {
    const t = $('#transcript').value;
    try { await navigator.clipboard.writeText(t); toast('已复制'); }
    catch { toast('复制失败（需要剪贴板权限）', 'err'); }
  });
  $('#btnVoiceDir2').addEventListener('click', () => openDir());

  // 日志
  $('#logFilter').addEventListener('input', () => { state.logCount = -1; loadLogs(); });
  $('#logQuiet').addEventListener('change', () => { state.logCount = -1; loadLogs(); });
  $('#btnOpenLog').addEventListener('click', () => openLog());

  // 设置
  $('#cfgSave').addEventListener('click', async () => {
    const body = { model: $('#cfgModel').value, lang: $('#cfgLang').value.trim() || 'zh' };
    try {
      const r = await fetch('/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then((x) => x.json());
      if (r.ok) {
        toast(r.applied ? '已应用，下次转写用新模型' : '没有变化');
        loadConfig(true);
      } else {
        toast('保存失败：' + (r.error || '未知'), 'err');
      }
    } catch (e) { toast('保存请求失败', 'err'); }
  });

  // 总览快捷
  $('#actPush').addEventListener('click', async () => {
    const r = await jget('/push', 8000).catch(() => null);
    toast(r && r.ok ? '已向工牌推送一次' : '推送失败（工牌可能没连）', r && r.ok ? 'ok' : 'err');
  });
  $('#actLog').addEventListener('click', () => openLog());
  $('#actVoiceDir').addEventListener('click', () => openDir());
  $('#actDiag').addEventListener('click', () => openDiag());
  $('#actRefresh').addEventListener('click', () => { tick(); toast('已刷新'); });
  $('#btnOpenLog2').addEventListener('click', () => openLog());
  $('#btnVoiceDir3').addEventListener('click', () => openDir());
  $('#btnDiag2').addEventListener('click', () => openDiag());

  // 服务控制（原生）
  $('#btnStop').addEventListener('click', () => {
    if (!native('stop')) toast('浏览器里无法控制服务，请用菜单栏图标', 'err');
  });
  $('#btnRestart').addEventListener('click', () => {
    if (!native('restart')) toast('浏览器里无法控制服务，请用菜单栏图标', 'err');
  });
  $('#dashRestart').addEventListener('click', () => {
    if (!native('restart')) toast('浏览器里无法控制服务，请用菜单栏图标', 'err');
  });
}

function openLog() {
  if (!native('openLog')) toast('请在菜单栏图标里选「打开日志」');
}
function openDir() {
  if (!native('openVoice')) toast('语音目录：' + (state.voiceDir || '~/.workbuddy/voice_out'));
}
function openDiag() {
  const url = 'http://127.0.0.1:' + (location.port || 8765) + '/status';
  if (!native('openDiag', { url })) window.open(url, '_blank');
}

async function sendDraft() {
  const text = $('#draft').value.trim();
  if (!text) return;
  const q = new URLSearchParams({ text, wait: '8' });
  if (state.selConv) q.set('conv', state.selConv);
  $('#btnSend').disabled = true;
  busy('发送中…');
  try {
    const r = await jget('/send?' + q.toString(), 90000);
    busy(null);
    if (r.ok) {
      toast('已投递（' + (r.message || 'Sent') + '）');
      $('#draft').value = '';
      state.msgSig = '';
      loadMessages();
    } else {
      toast('投递失败：' + (r.message || '未知'), 'err');
    }
  } catch (e) {
    busy(null);
    toast('投递请求失败', 'err');
  } finally {
    $('#btnSend').disabled = false;
  }
}

/* ------------------------------ 启动 ------------------------------ */

if (new URLSearchParams(location.search).get('app') === '1') {
  document.body.classList.add('app-embedded');
}

bind();
go(location.hash.slice(1) || 'dashboard');
window.addEventListener('hashchange', () => go(location.hash.slice(1)));
tick();
setInterval(tick, 1500);
setInterval(() => { if (!state.online) tick(); }, 3000);
