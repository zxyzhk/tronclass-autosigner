// ==UserScript==
// @name         厦门大学畅课(XMU TronClass) 自动签到
// @namespace    local.changke.autosign
// @version      1.0.0
// @description  【声明】XMU 畅课专用 · 仅用于快速签到等测试用途 · 严禁用于旷课。监听教师端发起的签到（数字 / GPS / 二维码），按门控策略自动提交。接口口径基于 2026-10 实测。
// @match        *://*/*
// @run-at       document-start
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @grant        GM_addStyle
// @grant        GM_notification
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// @noframes
// ==/UserScript==

/*
 * 【声明】本项目针对 **厦门大学（XMU）畅课（TronClass, https://lnt.xmu.edu.cn）** 编写与验证。
 *   仅用于快速签到等测试 / 研究 / 个人效率用途；**严禁用于旷课**
 *   （不得未到场代签、不得伪造二维码或数字码、不得替他人签到、不得规避考勤记录）。
 *   完整条款见仓库根目录 DISCLAIMER.md。
 *
 * 逆向依据（详见同目录《畅课签到逆向笔记.md》）：
 *   本人未签状态  : GET  /api/course/{cid}/student/{sid}/rollcalls  -> status = "absent"
 *   点名级状态    : GET  /api/rollcall/{rid}/student_rollcalls      -> status = in_progress / finished
 *   已签人数      : 同上，名单内 rollcall_status == "on_call_fine"
 *   数字码        : 优先详情 number_code；2026-10-10 起该字段恒为 null，
 *                   改走 GET /api/timetable_rollcalls?course_ids=&rollcall_date=  (UTC 日期)
 *   数字签到提交  : PUT  /api/rollcall/{rid}/answer_number_rollcall  {deviceId, numberCode}
 *   GPS 签到提交  : PUT  /api/rollcall/{rid}/answer                   {latitude, longitude, ...}
 *   二维码签到    : PUT  /api/rollcall/{rid}/answer_qr_rollcall       {data, deviceId}
 *                   —— 无法自行获取二维码内容，只能粘贴/识别到码后提交
 *
 * 说明：不保存任何账号密码；完全复用浏览器里已登录的同源会话（cookie + x-session-id）。
 */

(() => {
  'use strict';

  /* ------------------------------------------------------------------ 配置 */

  const DEFAULTS = {
    host: '',              // 绑定的畅课租户域名；空 = 未绑定（首次自动探测）
    enabled: true,
    intervalMs: 10000,     // 轮询间隔
    gate: true,            // 签到门控：等班里先签的人够多再签
    gateMinSigned: 4,      // 主条件：已签人数 >= 该值即自动签到（默认 4 人）
    gateRatio: 0,          // 附加条件：已签 > 全班 × 比例（0 = 关闭）
    gateMaxWaitMs: 120000, // 保底：等待超过此毫秒数直接签（漏签比早签糟）
    gateMinRemainMs: 30000,// 保底：距结束不足此毫秒数立即签
    coords: '',            // GPS 签到坐标 "纬度,经度"，留空则不做 GPS 签到
    tryPlain: false,       // 试验：对未知类型尝试 PUT /answer?api_version=1.76（客户端接口表实测存在，默认关）
    qrAutoFetch: true,     // 二维码签到：主动调服务端取码接口拿 token（免拍照的关键路径）
    qrPollMs: 700,         // 取码轮询间隔；token 只数秒有效，故需亚秒级刷新
    notifyOnDetect: true,  // 检出签到时提醒
    notifyOnSuccess: true, // 签到成功时提醒
    notifyOnFail: true,    // 失败/需人工时提醒
    notifyOnQr: true,      // 二维码（无法自动处理）时提醒
    relayEnabled: true,    // 把事件推送给本地 clawbot，再由它转发到微信
    relayUrl: 'http://127.0.0.1:8787/notify',
    scheduleEnabled: true, // 只在课表窗口内轮询（上课/课间，前后放宽 10 分钟）
    pollInWindowMs: 20000, // 窗口内的轮询间隔：20 秒
    windowCheckMs: 5000,   // 调度器检查窗口的频率
    clockResyncMs: 1800000,// 与服务端校时的间隔（30 分钟）
  };

  const hasGM = typeof GM_getValue === 'function';
  const store = {
    get(key, fallback) {
      try {
        const raw = hasGM ? GM_getValue(key) : localStorage.getItem('ck_' + key);
        if (raw === undefined || raw === null) return fallback;
        return typeof raw === 'string' ? JSON.parse(raw) : raw;
      } catch (e) { return fallback; }
    },
    set(key, value) {
      try {
        const raw = JSON.stringify(value);
        if (hasGM) GM_setValue(key, raw); else localStorage.setItem('ck_' + key, raw);
      } catch (e) {}
    },
  };

  function loadCfg() {
    const saved = store.get('cfg', {});
    return Object.assign({}, DEFAULTS, saved && typeof saved === 'object' ? saved : {});
  }
  function saveCfg() { store.set('cfg', S.cfg); }

  /* ===== 课表（示例数据，务必替换成你自己的）=====
     用 tools/gen_schedule.py 从你的课程表 JSON 生成这一段；
     或手写：day=1~7(周一到周日, 0=周日)、s/e = 当日分钟数、w=[起周,止周]。 */
  const SCHEDULE = {
    tzOffsetMin: 480,   // 学校所在地时区 UTC+8
    padMin: 10,         // 上课/课间窗口前后各放宽 10 分钟
    mergeGapMin: 25,    // 同一天内间隔 <= 25 分钟的课视为同一段
    periods: {"1":[480,525],"2":[535,580],"3":[610,655],"4":[665,710]},
    courses: [
      {day:1,s: 480,e: 580,w:[1,16],n:"示例课程 A（周一 08:00~09:40）"},
      {day:3,s:1000,e:1100,w:[1,16],n:"示例课程 B（周三 16:40~18:20）"},
    ],
  };

  /* ------------------------------------------------------------------ 状态 */

  const SIGNED = 'on_call_fine';
  const TERMINAL = new Set(['finished', 'expired', 'closed', 'on_call_fine', 'on_personal_leave']);
  const ACTIVE = new Set(['active', 'on_going', 'on_coming', 'started', 'in_progress',
                          'waiting', 'pending', 'not_started', 'absent']);
  const MAX_ATTEMPTS = 30;
  const LOG_MAX = 60;

  const S = {
    cfg: loadCfg(),
    sid: null,
    courses: [],
    coursesAt: 0,
    seen: new Set(),      // 已成功 / 已终止的点名 id
    attempts: new Map(),  // rid -> 失败次数
    gateSince: new Map(), // rid -> 进入门控的时间戳
    log: [],
    xSessionId: null,
    timer: null,
    qrTimer: null,        // 二维码取码的快速轮询定时器
    qrTargets: new Map(), // rid -> {cid, name}：进行中且未签的二维码点名
    pendingQr: new Map(), // rid -> {name, at}：需要人工处理（二维码/取不到码）
    qrGate: new Map(),    // rid -> {ok, at}：二维码路径的门控结果缓存（5 秒）
    notified: new Set(),  // 提醒去重键，避免同一事件反复弹
    relayWarned: false,   // 中继不可达只提示一次
    inWindowFlag: false,  // 是否处于课表窗口（日志与面板显示用）
    busy: false,
    lastTick: 0,
    booted: false,
  };

  const log = (msg, bad) => {
    let t;
    try {
      t = new Date(now()).toLocaleTimeString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' });
    } catch (e) {
      t = new Date(now()).toLocaleTimeString('zh-CN', { hour12: false });
    }
    S.log.push({ t, msg, bad: !!bad });
    if (S.log.length > LOG_MAX) S.log = S.log.slice(-LOG_MAX);
    render();
  };

  /* ------------------------------------------------------------ 提醒通知 */
  /* 三级降级：GM_notification（系统通知，标签页在后台也能弹）
     → Notification API（需授权）→ 页面内浮层。同键事件只提醒一次。 */

  const NOTIFY_ICON = 'data:image/svg+xml;charset=utf-8,'
    + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">'
      + '<circle cx="32" cy="32" r="30" fill="#ff9442"/>'
      + '<text x="32" y="44" font-size="34" text-anchor="middle" fill="#fff">签</text></svg>');

  function toast(title, body) {
    try {
      let box = document.getElementById('ck-toasts');
      if (!box) {
        box = document.createElement('div');
        box.id = 'ck-toasts';
        document.body.appendChild(box);
      }
      const el = document.createElement('div');
      el.className = 'ck-toast';
      el.innerHTML = `<strong>${escapeHtml(title)}</strong><span>${escapeHtml(body || '')}</span>`;
      box.appendChild(el);
      setTimeout(() => el.remove(), 12000);
    } catch (e) {}
  }

  /* 转发到本地 clawbot（再由它推到你微信）。
     走 GM_xmlhttpRequest：不受页面 CORS 限制，也不受 https 页面访问 http://127.0.0.1
     的混合内容限制；clawbot 不可达时只在日志里提示一次，不打扰正常签到流程。 */
  function relayNotify(title, body) {
    if (!S.cfg.relayEnabled || !S.cfg.relayUrl) return;
    const payload = JSON.stringify({
      title, body: body || '', event: 'changke',
      source: 'changke-autosign', at: new Date(now()).toISOString(),
    });
    const onFail = reason => {
      if (S.relayWarned) return;
      S.relayWarned = true;
      log(`[微信] 推送中继不可达（${reason}）——请确认 clawbot 正在运行`, true);
    };
    try {
      if (typeof GM_xmlhttpRequest === 'function') {
        GM_xmlhttpRequest({
          method: 'POST',
          url: S.cfg.relayUrl,
          headers: { 'Content-Type': 'application/json' },
          data: payload,
          onload: res => {
            if (res && res.status >= 200 && res.status < 300) { S.relayWarned = false; return; }
            onFail(`HTTP ${res && res.status}`);
          },
          onerror: () => onFail('连接失败'),
          ontimeout: () => onFail('超时'),
        });
        return;
      }
      fetch(S.cfg.relayUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: payload,
      }).then(r => { if (r.ok) S.relayWarned = false; else onFail(`HTTP ${r.status}`); })
        .catch(() => onFail('连接失败'));
    } catch (e) {
      onFail(e && e.message ? e.message : '异常');
    }
  }

  function notify(title, body, enabled, dedupeKey) {
    if (enabled === false) return;
    const key = dedupeKey || `${title}|${body || ''}`;
    if (S.notified.has(key)) return;
    S.notified.add(key);

    toast(title, body);
    relayNotify(title, body);

    try {
      if (typeof GM_notification === 'function') {
        GM_notification({ title, text: body || '', image: NOTIFY_ICON, timeout: 12000 });
        return;
      }
    } catch (e) {}

    try {
      if (typeof Notification === 'function') {
        if (Notification.permission === 'granted') {
          new Notification(title, { body: body || '', icon: NOTIFY_ICON });
        } else if (Notification.permission !== 'denied') {
          Notification.requestPermission().then(p => {
            if (p === 'granted') new Notification(title, { body: body || '', icon: NOTIFY_ICON });
          });
        }
      }
    } catch (e) {}
  }

  /* -------------------------------------------------- 抓取站点自身的会话头 */

  function installSniffer() {
    try {
      const origSet = XMLHttpRequest.prototype.setRequestHeader;
      XMLHttpRequest.prototype.setRequestHeader = function (name, value) {
        try {
          if (String(name).toLowerCase() === 'x-session-id' && value) S.xSessionId = String(value);
        } catch (e) {}
        return origSet.apply(this, arguments);
      };
    } catch (e) {}

    try {
      const origFetch = window.fetch;
      if (typeof origFetch === 'function') {
        window.fetch = function (input, init) {
          try {
            const h = (init && init.headers) || (input && input.headers);
            if (h) {
              const v = new Headers(h).get('x-session-id');
              if (v) S.xSessionId = v;
            }
          } catch (e) {}
          return origFetch.apply(this, arguments);
        };
      }
    } catch (e) {}

    // 部分版本把会话头放在 storage 里
    try {
      for (const box of [localStorage, sessionStorage]) {
        for (let i = 0; i < box.length; i++) {
          const k = box.key(i);
          if (k && /session[_-]?id/i.test(k)) {
            const v = box.getItem(k);
            if (v && v.length > 8 && v.length < 200) { S.xSessionId = v; break; }
          }
        }
      }
    } catch (e) {}
  }

  /* ------------------------------------------ 时间基准：以服务端时间为准 */
  /* 本机时钟可能不准、时区也可能不对，但排课时间是「学校当地时间」。
     这里绝不修改系统时间：只从畅课响应头的 Date 读真实时间，算出与本机的偏差，
     之后所有时间判断（课表窗口 / 门控 / token 时效）一律走 now()。 */

  const clock = { offsetMs: 0, syncedAt: 0, rttMs: null, source: '' };

  /** 真实时间（本机 Date.now() + 与服务端的偏差） */
  const now = () => Date.now() + clock.offsetMs;

  async function syncClock(force) {
    if (!force && clock.syncedAt && Date.now() - clock.syncedAt < Number(S.cfg.clockResyncMs)) {
      return true;
    }
    const t0 = Date.now();
    let res = null;
    try {
      res = await fetch('/api/profile', { method: 'GET', credentials: 'same-origin' });
    } catch (e) { res = null; }
    let header = res && res.headers ? res.headers.get('date') : null;
    if (!header) {
      try {
        const r2 = await fetch('/', { method: 'HEAD', credentials: 'same-origin' });
        header = r2.headers.get('date');
      } catch (e) {}
    }
    const t1 = Date.now();
    if (!header) return false;
    const serverMs = Date.parse(header);
    if (!isFinite(serverMs)) return false;

    const rtt = t1 - t0;
    const correction = Math.min(rtt / 2, 500);   // 链路慢时限制补偿，避免假偏差
    clock.offsetMs = serverMs + correction - t1;
    clock.syncedAt = Date.now();
    clock.rttMs = rtt;
    clock.source = 'Date 响应头';
    return true;
  }

  /* ---------------------------------------- 课表窗口（与机器时区/时钟无关） */

  /** 学校当地时间：把时间戳平移到 UTC+8 再用 UTC 取值读，于是与机器时区无关 */
  const schoolNow = ts => new Date((ts === undefined ? now() : ts) + SCHEDULE.tzOffsetMin * 60000);
  const schoolDay = ts => schoolNow(ts).getUTCDay();
  const schoolMinutes = ts => {
    const d = schoolNow(ts);
    return d.getUTCHours() * 60 + d.getUTCMinutes();
  };
  const hhmm = m => String(Math.floor(m / 60)).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0');
  const DOW_NAME = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

  /* 不判断学期周次。所有课程一律参与窗口计算
     （代价：已结课的课也会在其时段空查几次；好处是不可能因周次算错而漏签）。 */

  /** 课表编译成 {星期: [[起,止]]}：同一天相邻课段按 mergeGapMin 合并，两端各放宽 padMin */
  function buildWindowsFor() {
    const byDay = {};
    let used = 0;
    for (const c of SCHEDULE.courses) {
      (byDay[c.day] = byDay[c.day] || []).push([c.s, c.e]);
      used++;
    }
    const out = {};
    for (const day of Object.keys(byDay)) {
      const list = byDay[day].slice().sort((a, b) => a[0] - b[0]);
      const merged = [];
      for (const [s, e] of list) {
        const last = merged[merged.length - 1];
        if (last && s - last[1] <= SCHEDULE.mergeGapMin) last[1] = Math.max(last[1], e);
        else merged.push([s, e]);
      }
      out[day] = merged.map(([s, e]) => [s - SCHEDULE.padMin, e + SCHEDULE.padMin]);
    }
    out.__used = used;
    return out;
  }

  let windowCache = { week: undefined, table: null };
  function windowsFor() {
    if (!windowCache.table) windowCache = { week: null, table: buildWindowsFor() };
    return windowCache.table;
  }

  /** 当前是否处于课表窗口；不在窗口时给出最近的下一个窗口 */
  function scheduleState(ts) {
    const t = ts === undefined ? now() : ts;
    const dow = schoolDay(t);
    const mins = schoolMinutes(t);
    const table = windowsFor();
    const today = table[dow] || [];

    for (const [s, e] of today) {
      if (mins >= s && mins <= e) return { in: true, win: [s, e], dow, mins };
    }
    let next = null;
    for (let off = 0; off <= 7 && !next; off++) {
      const day = (dow + off) % 7;
      for (const [s, e] of (table[day] || [])) {
        if (off === 0 && s <= mins) continue;
        next = { delta: off * 1440 + s - mins, at: s, end: e, day };
        break;
      }
    }
    return { in: false, next, dow, mins };
  }

  /* ------------------------------------------------------------ HTTP 封装 */

  async function api(path, opts = {}) {
    const { method = 'GET', body, headers = {} } = opts;
    const h = Object.assign(
      { Accept: 'application/json, text/plain, */*', 'X-Requested-With': 'XMLHttpRequest' },
      headers,
    );
    if (S.xSessionId) h['x-session-id'] = S.xSessionId;
    if (body !== undefined) h['Content-Type'] = 'application/json;charset=utf-8';

    const res = await fetch(path, {
      method,
      headers: h,
      credentials: 'same-origin',
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
    return { ok: res.ok, status: res.status, data };
  }

  const asArray = (v, keys) => {
    if (Array.isArray(v)) return v;
    if (v && typeof v === 'object') {
      for (const k of keys) if (Array.isArray(v[k])) return v[k];
    }
    return [];
  };

  const uuid = () => (crypto && crypto.randomUUID
    ? crypto.randomUUID()
    : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
        const r = (Math.random() * 16) | 0;
        return (c === 'x' ? r : ((r & 0x3) | 0x8)).toString(16);
      }));

  /* 真机语义：客户端送的是「设备级稳定 UUID」（deviceInfo.uuid / 各平台设备插件），
     不是每次请求新生成。服务端有 RADAR_DEVICE_ID_INVALID 校验，故这里持久化复用。 */
  function deviceId() {
    let v = store.get('deviceId', '');
    if (!v || typeof v !== 'string') { v = uuid(); store.set('deviceId', v); }
    return v;
  }

  /* ------------------------------------------------------------ 时间判断 */

  const parseTime = v => {
    if (!v) return null;
    const d = new Date(String(v).replace(' ', 'T'));
    return isNaN(d.getTime()) ? null : d;
  };
  const remainingMs = endTime => {
    const d = parseTime(endTime);
    return d ? d.getTime() - now() : null;
  };
  const endPassed = endTime => {
    const r = remainingMs(endTime);
    return r !== null && r <= 0;
  };
  const isTerminal = (status, endTime) => {
    if (TERMINAL.has(status)) return true;
    if (ACTIVE.has(status)) return endPassed(endTime);
    if (endTime && endPassed(endTime)) return true;
    return false;
  };

  /* ------------------------------------------------------------ 接口调用 */

  async function ensureIdentity() {
    if (S.sid) return true;
    const r = await api('/api/profile');
    if (r.ok && r.data && typeof r.data === 'object') {
      const id = r.data.id || r.data.user_id || (r.data.user && r.data.user.id);
      if (id) {
        S.sid = String(id);
        if (!S.cfg.host) { S.cfg.host = location.host; saveCfg(); }
        log(`已识别身份 student_id=${S.sid}（绑定 ${S.cfg.host}）`);
        return true;
      }
    }
    return false;
  }

  async function loadCourses(force) {
    if (!force && S.courses.length && now() - S.coursesAt < 10 * 60 * 1000) return S.courses;

    let sem = null;
    try {
      const sr = await api('/api/current-semester-info');
      if (sr.ok && sr.data && sr.data.semester) {
        sem = {
          s: String(sr.data.semester.id),
          y: sr.data.academic_year ? String(sr.data.academic_year.id) : null,
        };
      }
    } catch (e) {}

    const conditions = { keyword: '', classify_type: 'recently_started', display_studio_list: false };
    if (sem && sem.s) conditions.semester_id = [sem.s];
    if (sem && sem.y) conditions.academic_year_id = [sem.y];

    const payload = {
      conditions,
      fields: 'id,name,display_name',
      page: 1,
      page_size: 30,
      showScorePassedStatus: false,
    };

    let r = await api('/api/my-courses', { method: 'POST', body: payload });
    let arr = asArray(r.data, ['courses', 'data']);
    if (!arr.length) {
      r = await api('/api/my-courses', { method: 'POST', body: { fields: 'id,name,display_name', page: 1, page_size: 30 } });
      arr = asArray(r.data, ['courses', 'data']);
    }

    const seen = new Set();
    S.courses = arr.filter(c => c && c.id && !seen.has(String(c.id)) && seen.add(String(c.id)));
    S.coursesAt = now();
    if (S.courses.length) log(`课程列表已刷新：${S.courses.length} 门`);
    return S.courses;
  }

  async function fetchRollcalls(cid) {
    const r = await api(`/api/course/${cid}/student/${S.sid}/rollcalls?page=1&page_size=20`);
    if (!r.ok) return [];
    return asArray(r.data, ['rollcalls', 'data']);
  }

  const fetchDetail = async rid => {
    const r = await api(`/api/rollcall/${rid}/student_rollcalls`);
    return r.ok ? r.data : null;
  };

  async function numberCodeFromTimetable(rid, cid, rollcallTime) {
    let date = new Date().toISOString().slice(0, 10);
    const d = parseTime(rollcallTime);
    if (d) date = d.toISOString().slice(0, 10); // 服务端按 UTC 日期查
    const r = await api(`/api/timetable_rollcalls?course_ids=${encodeURIComponent(cid)}&rollcall_date=${date}`);
    if (!r.ok) return null;
    const items = asArray(r.data && r.data.rollcalls, []);
    const hit = items.find(it => it && String(it.id) === String(rid));
    return hit && hit.number_code ? String(hit.number_code) : null;
  }

  /** 一次性取码诊断：数字码在哪条通道可取（每点名只跑一次，用于判定租户差异） */
  async function diagNumberChannels(rid, cid, rc) {
    try {
      const detail = await fetchDetail(rid);
      const fromDetail = detail && detail.number_code ? String(detail.number_code) : null;
      const fromTimetable = await numberCodeFromTimetable(rid, cid, rc.rollcall_time);
      log(`[取码诊断] rid=${rid}｜详情.number_code=${fromDetail || 'null（2026-10 后预期如此）'}`
        + `｜timetable_rollcalls=${fromTimetable || '未命中'}`
        + (fromDetail || fromTimetable
          ? '　→ 本租户可取码，数字签到可全自动'
          : '　→ 两通道均无码，等下一轮'));
    } catch (e) {
      log(`[取码诊断] rid=${rid} 失败：${e && e.message ? e.message : e}`, true);
    }
  }

  function signedStatsFrom(detail) {
    const list = detail && (detail.student_rollcalls || detail.students);
    if (!Array.isArray(list) || !list.length) return null;
    const me = list.find(s => s && [
      s.student_id, s.user_id, s.id,
      s.user && s.user.id, s.student && s.student.id,
    ].some(v => v !== undefined && v !== null && String(v) === String(S.sid)));
    // 判据与接口口径不符时（把自己也算成已签）宁可立即签，不卡住
    if (me && me.rollcall_status === SIGNED) return null;
    const signed = list.filter(s => s && s.rollcall_status === SIGNED).length;
    return { signed, total: list.length, endTime: detail.end_time };
  }

  async function gateCheck(rid) {
    if (!S.cfg.gate) return { go: true, msg: '' };

    const detail = await fetchDetail(rid);
    const stats = signedStatsFrom(detail);
    if (!stats) return { go: true, msg: '查不到签到人数，直接签' };

    const since = S.gateSince.get(rid) || now();
    if (!S.gateSince.has(rid)) S.gateSince.set(rid, since);
    const waited = now() - since;

    const needSigned = Math.max(1, Number(S.cfg.gateMinSigned) || 4);
    const msg = `已签 ${stats.signed}/${stats.total}`;
    const ratioOk = Number(S.cfg.gateRatio) > 0
      && stats.signed / stats.total > Number(S.cfg.gateRatio);
    if (stats.signed >= needSigned || ratioOk)
      return { go: true, msg: `${msg}，已达 ${needSigned} 人，开签` };

    const remain = remainingMs(stats.endTime);
    if (waited >= S.cfg.gateMaxWaitMs)
      return { go: true, msg: `${msg}，等待超 ${Math.round(S.cfg.gateMaxWaitMs / 1000)}s，直接签` };
    if (remain !== null && remain <= S.cfg.gateMinRemainMs)
      return { go: true, msg: `${msg}，剩余 ${Math.round(remain / 1000)}s，直接签` };

    return { go: false, msg: `${msg}，未达 ${needSigned} 人，继续等` };
  }

  /* ------------------------------------------------------------ 签到提交 */

  const answerNumber = async (rid, code) => (await api(`/api/rollcall/${rid}/answer_number_rollcall`, {
    method: 'PUT',
    body: { deviceId: deviceId(), numberCode: String(code) },
  })).ok;

  const answerQr = async (rid, data) => (await api(`/api/rollcall/${rid}/answer_qr_rollcall`, {
    method: 'PUT',
    body: { data, deviceId: deviceId() },
  })).ok;

  // 实测（app.js 接口表）：不存在 answer_rollcall；普通/其他类型走 /answer（与 GPS 同端点）
  const answerPlain = async rid => (await api(`/api/rollcall/${rid}/answer?api_version=1.76`, {
    method: 'PUT',
    body: { deviceId: deviceId() },
  })).ok;

  async function answerRadar(rid, lat, lon) {
    return (await api(`/api/rollcall/${rid}/answer`, {
      method: 'PUT',
      body: {
        accuracy: 35, altitude: 0, altitudeAccuracy: null, deviceId: deviceId(),
        heading: null, latitude: String(lat), longitude: String(lon), speed: null,
      },
    })).ok;
  }

  /* ------------------------------------- 二维码取码：免拍照路径（机制逆向） */
  /* token 由服务端下发：GET /api/course/{cid}/rollcall/{rid}/qr_code -> {"data": ...}
     结构 = 10 位 Unix 秒 + 32 位 hex，随点名滚动重新生成，仅数秒有效。
     所以只能在点名进行中以亚秒级频率取码、并立即提交。 */

  async function fetchQrToken(cid, rid) {
    const r = await api(`/api/course/${cid}/rollcall/${rid}/qr_code`);
    if (!r.ok) return { status: r.status, data: null };
    const body = r.data && typeof r.data === 'object' ? r.data : {};
    const raw = body.data || body.qr_code || body.token || '';
    const data = typeof raw === 'string' ? raw.trim() : '';
    return { status: r.status, data: data || null };
  }

  const TOKEN_SHAPE = /^[0-9]{10}[0-9a-f]{32}$/; // 服务端二维码 token 的真实形状（实测）

  const tokenAgeMs = data => (/^[0-9]{10}/.test(String(data || ''))
    ? now() - Number(String(data).slice(0, 10)) * 1000
    : null);

  async function qrFastTick() {
    if (!S.cfg.enabled || !S.cfg.qrAutoFetch || !S.qrTargets.size) return;

    for (const [rid, target] of Array.from(S.qrTargets)) {
      try {
        // 二维码路径同样遵守门控：满 N 人再签。
        // 名单查询缓存 5 秒，避免在 0.7 秒的快循环里反复拉名单。
        if (S.cfg.gate) {
          const cached = S.qrGate.get(rid);
          if (!cached || now() - cached.at > 5000) {
            const gate = await gateCheck(rid);
            S.qrGate.set(rid, { ok: gate.go, at: now() });
            if (!gate.go) {
              if (!S.notified.has('qrwait:' + rid)) {
                S.notified.add('qrwait:' + rid);
                log(`[等待] ${target.name} 二维码：${gate.msg}`);
              }
              continue;
            }
            log(`[开签] ${target.name} 二维码：${gate.msg}`);
          } else if (!cached.ok) {
            continue;
          }
        }

        const { status, data } = await fetchQrToken(target.cid, rid);
        if (!data) {
          if (status === 403 || status === 404) {
            S.qrTargets.delete(rid);
            S.seen.add(String(rid));
            log(`[权限] ${target.name} 取码接口 HTTP ${status}：学生会话读不到 token，`
              + '二维码仍需拿到码内容后粘贴提交', true);
            notify(`📷 二维码需要你出手 · ${target.name}`,
              `取码接口 HTTP ${status}，请把二维码内容粘到面板提交`,
              S.cfg.notifyOnQr, `qrperm:${rid}`);
          }
          continue; // 码未就绪 / 点名刚结束，下一轮再看
        }
        const age = tokenAgeMs(data);
        if (age !== null && age > 3000) continue; // 已过期，重新取
        if (await answerQr(rid, data)) {
          S.qrTargets.delete(rid);
          S.pendingQr.delete(rid);
          S.seen.add(String(rid));
          log(`[成功] 二维码签到 · ${target.name} · 自取码提交（未拍照）`);
          notify(`✅ 二维码签到成功 · ${target.name}`, '已自动取码并提交（未拍照）',
            S.cfg.notifyOnSuccess, `ok:${rid}`);
        }
      } catch (e) {
        log(`[二维码] 取码失败：${e && e.message ? e.message : e}`, true);
      }
    }
  }

  function startQrTimer() {
    if (S.qrTimer) { clearInterval(S.qrTimer); S.qrTimer = null; }
    if (!S.cfg.qrAutoFetch) return;
    S.qrTimer = setInterval(qrFastTick, Math.max(400, S.cfg.qrPollMs));
  }

  /* 服务端校验探测：
     服务端到底有没有真的校验 data？发一条"形状合法但 hash 伪造"的 data ——
       · 200 → 服务端不校验 ⇒ 免扫码提交可行（守护进程会据此全自动签到）
       · no_request_data / invalid_create_time_hash / invalid_qr_create_time /
         qr_code_expired → 确实校验 ⇒ 不存在算法捷径，只能"取得"真 token */
  const QR_ERROR_HINT = {
    no_request_data: '服务端要求 data 字段（说明它在读 data）',
    invalid_create_time_hash: '服务端校验 create_time 的 hash —— 伪造无效',
    invalid_qr_create_time: 'data 里的 create_time 不合法',
    qr_code_expired: 'data 已过期（二维码窗口仅数秒）',
    not_found_qr_rollcall: '该点名不是二维码类型或不存在',
    rollcall_already_closed: '点名已结束',
  };

  async function probeQrValidation() {
    const rid = Array.from(S.qrTargets.keys())[0]
      || Array.from(S.pendingQr.keys())[0];
    if (!rid) { log('[校验探测] 需要先有一个进行中的二维码点名', true); return; }

    const fake = `${Math.floor(now() / 1000)}${'f'.repeat(32)}`;
    log(`[校验探测] rid=${rid} 提交伪造 data（形状合法、hash 伪造）`);
    const r = await api(`/api/rollcall/${rid}/answer_qr_rollcall`, {
      method: 'PUT',
      body: { data: fake, deviceId: deviceId() },
    });

    if (r.ok) {
      log('[校验探测] 服务端返回 200 —— 它没有校验 data！免扫码提交可行');
      notify('🔓 服务端不校验二维码 data',
        '伪造 data 也被接受 ⇒ 二维码签到可全自动', true, `qrvalid:ok:${rid}`);
      S.seen.add(String(rid));
      return;
    }
    const body = r.data && typeof r.data === 'object' ? r.data : {};
    let code = '';
    for (const key of ['error', 'code', 'message', 'error_code']) {
      if (body[key]) { code = String(body[key]); break; }
    }
    log(`[校验探测] HTTP ${r.status}｜错误码 ${code || '(未返回)'} —— `
      + (QR_ERROR_HINT[code] || '未知错误'), true);
    log('[校验探测] 结论：服务端确实校验 data，无法自行构造 ⇒ 只能"取得"真实 token', true);
    notify('🔒 服务端确实校验二维码 data',
      `错误码 ${code || '(未返回)'}：${QR_ERROR_HINT[code] || '未知'}。二维码需人工提供码内容`,
      true, `qrvalid:no:${rid}`);
  }

  /** 一次性探测：学生对进行中的二维码点名能否直接取到 token */
  async function probeQrPermission() {
    if (!(await ensureIdentity())) { log('[探测] 未识别身份，请确认已登录畅课', true); return; }
    const courses = await loadCourses(false);
    let found = 0;

    for (const course of courses) {
      const cid = course.id;
      const name = course.display_name || course.name || String(cid);
      let rollcalls = [];
      try { rollcalls = await fetchRollcalls(cid); } catch (e) { continue; }

      for (const rc of rollcalls) {
        const rid = rc && (rc.id || rc.rollcall_id);
        if (!rid) continue;
        const isQr = /qr/i.test(String(rc.type || '')) || rc.is_qr === true;
        if (!isQr) continue;

        found++;
        // 候选取码来源：第 1 条采集项目实测有效（但对学生会话是否放行未知）；
        // 其余来自客户端接口表中的可疑端点。每条只发一次 GET，有界。
        const candidates = [
          `/api/course/${cid}/rollcall/${rid}/qr_code`,
          `/api/rollcall/${rid}/lite`,
          `/api/rollcall/${rid}/answers`,
          `/api/courses/rollcall_status/${rid}/result`,
        ];

        for (const path of candidates) {
          const r = await api(path);
          const body = r.data && typeof r.data === 'object' ? r.data : {};
          const hit = Object.entries(body).find(
            ([, v]) => typeof v === 'string' && TOKEN_SHAPE.test(v));
          if (hit) {
            log(`[探测] ${name} rid=${rid} → ${path} HTTP ${r.status}，`
              + `字段 ${hit[0]} 命中 token 形状：可免拍照自动签到`);
          } else {
            log(`[探测] ${name} rid=${rid} → ${path} HTTP ${r.status}，无 token`,
              r.status !== 200);
          }
        }
      }
    }

    if (!found) log('[探测] 未发现进行中的二维码点名（等老师发起后再点一次）');
  }

  /* ------------------------------------------------ 二维码内容解析（逆向） */
  /* 畅课二维码用一套自定义压缩编码：'!' 分段、'~' 分键值，
     key 为字段序号的 base36；值前缀 chr(26)=布尔/枚举、chr(16)=base36 数值。 */

  const CTRL = {
    ta: String.fromCharCode(30), ea: String.fromCharCode(31),
    na: String.fromCharCode(26), ra: String.fromCharCode(16),
  };
  const FIELD_ORDER = ['courseId', 'activityId', 'activityType', 'data', 'rollcallId',
    'groupSetId', 'accessCode', 'action', 'enableGroupRollcall', 'createUser', 'joinCourse'];
  const SENTINEL = ['classroom-exam', 'feedback', 'vote'];

  const KEYMAP = {};
  FIELD_ORDER.forEach((k, i) => { KEYMAP[(i).toString(36)] = k; });
  const VALMAP = {};
  SENTINEL.forEach((k, i) => { VALMAP[CTRL.na + (i + 2).toString(36)] = k; });

  function parseSignQr(text) {
    const out = {};
    if (!text || typeof text !== 'string') return out;
    for (const part of text.split('!').filter(Boolean)) {
      const idx = part.indexOf('~');
      if (idx < 0) continue;
      const rawKey = part.slice(0, idx);
      const rawVal = part.slice(idx + 1);
      const key = KEYMAP[rawKey] || rawKey;

      let value;
      if (rawVal.startsWith(CTRL.na)) {
        if (rawVal === CTRL.na + '1') value = true;
        else if (rawVal === CTRL.na + '0') value = false;
        else value = VALMAP[rawVal] !== undefined ? VALMAP[rawVal] : rawVal;
      } else if (rawVal.startsWith(CTRL.ra)) {
        const nums = rawVal.slice(1).split('.').map(p => parseInt(p, 36)).filter(n => !isNaN(n));
        value = nums.length > 1 ? parseFloat(`${nums[0]}.${nums[1]}`) : (nums.length ? nums[0] : rawVal);
      } else {
        value = rawVal.split(CTRL.ea).join('~').split(CTRL.ta).join('!');
      }
      out[key] = value;
    }
    return out;
  }

  /* 实测：客户端 scanner-jumper 的 te() 直接 JSON.parse(p)，
     二维码内容可能是「JSON 串」装在 p / _wg_p 参数里，也可能是紧凑编码。 */

  const tryJsonPayload = s => {
    if (typeof s !== 'string') return null;
    const t = s.trim();
    if (!t.startsWith('{')) return null;
    try {
      const obj = JSON.parse(t);
      return obj && obj.rollcallId !== undefined ? obj : null;
    } catch (e) { return null; }
  };

  /** 把扫到/粘贴到的二维码内容解析成 {rollcallId, data}，失败返回 null */
  function resolveQrPayload(content) {
    let text = String(content || '').trim();
    if (!text) return null;

    const direct = tryJsonPayload(text);          // 纯 JSON 形态
    if (direct) return direct;

    if ((text.includes('/j?p=') || text.includes('_wg_qr')) && !/^https?:/i.test(text)) {
      text = location.origin + text;
    }

    if (/^https?:/i.test(text)) {
      let url;
      try { url = new URL(text); } catch (e) { return null; }
      if (!['/j', '/scanner-jumper', '/_wg_qr'].includes(url.pathname)) return null;

      const params = url.searchParams;
      const pParam = params.get('_p') || params.get('_wg_p');
      if (pParam) {
        const obj = tryJsonPayload(pParam);
        if (obj) return obj;
      }
      const p = params.get('p') || params.get('_wg_p');
      if (!p) return null;
      const obj2 = tryJsonPayload(p);
      if (obj2) return obj2;
      text = p;
    }

    const parsed = parseSignQr(text);
    return parsed && parsed.rollcallId !== undefined ? parsed : null;
  }

  /* -------------------------------------------------------------- 主循环 */

  async function tick() {
    if (!S.cfg.enabled || S.busy) return;
    if (S.cfg.host && location.host !== S.cfg.host) return;

    S.busy = true;
    try {
      if (!(await ensureIdentity())) return;
      const courses = await loadCourses(false);
      if (!courses.length) return;

      for (const course of courses) {
        const cid = course.id;
        const name = course.display_name || course.name || String(cid);

        let rollcalls;
        try { rollcalls = await fetchRollcalls(cid); }
        catch (e) { continue; }

        for (const rc of rollcalls) {
          if (!rc) continue;
          const rid = rc.id || rc.rollcall_id;
          if (!rid || S.seen.has(String(rid))) continue;

          const isNumber = !!rc.is_number && !rc.is_radar;
          const isRadar = !!rc.is_radar;

          let endTime = null;
          let detailCode = null;
          if (isNumber || isRadar) {
            const detail = await fetchDetail(rid);
            if (detail) {
              detailCode = detail.number_code ? String(detail.number_code) : null;
              endTime = detail.end_time || null;
            }
          }

          const status = (isNumber || isRadar) ? rc.status : (rc.status || null);

          if (isTerminal(status, endTime)) {
            S.seen.add(String(rid));
            S.qrTargets.delete(String(rid));
            S.pendingQr.delete(String(rid));
            continue;
          }

          if (isNumber || isRadar) {
            notify(`🎯 检测到签到 · ${name}`,
              `${isNumber ? '数字签到' : 'GPS/雷达签到'}已发起，`
              + `等满 ${Math.max(1, Number(S.cfg.gateMinSigned) || 4)} 人后自动签`,
              S.cfg.notifyOnDetect, `detect:${rid}`);

            const gate = await gateCheck(rid);
            if (!gate.go) {
              const attempts = S.attempts.get(String(rid)) || 0;
              if (attempts === 0) log(`[等待] ${name}：${gate.msg}`);
              continue;
            }
          }

          const attempts = (S.attempts.get(String(rid)) || 0) + 1;
          S.attempts.set(String(rid), attempts);

          if (isNumber) {
            let code = detailCode;
            if (!code) code = await numberCodeFromTimetable(rid, cid, rc.rollcall_time);
            if (!code) {
              if (attempts === 1) {
                log(`[取码] ${name} 数字码尚未下发，将重试`, true);
                diagNumberChannels(rid, cid, rc); // 仅诊断用，不阻塞主循环
              }
              if (attempts >= MAX_ATTEMPTS) {
                S.seen.add(String(rid));
                log(`[放弃] ${name} 取码失败 ${attempts} 次`, true);
                notify(`❌ 数字签到我处理不了 · ${name}`, '取不到数字码，请手动签到',
                  S.cfg.notifyOnFail, `failcode:${rid}`);
              }
              continue;
            }
            if (await answerNumber(rid, code)) {
              S.seen.add(String(rid)); S.attempts.delete(String(rid)); S.gateSince.delete(String(rid));
              log(`[成功] 数字签到 · ${name} · 码=${code}`);
              notify(`✅ 数字签到成功 · ${name}`, `已自动提交，码=${code}`,
                S.cfg.notifyOnSuccess, `ok:${rid}`);
            } else if (attempts >= MAX_ATTEMPTS) {
              S.seen.add(String(rid));
              log(`[放弃] ${name} 数字签到提交失败 ${attempts} 次`, true);
              notify(`❌ 数字签到提交失败 · ${name}`, `已重试 ${attempts} 次，请手动签到`,
                S.cfg.notifyOnFail, `failsubmit:${rid}`);
            }
            continue;
          }

          if (isRadar) {
            const m = String(S.cfg.coords || '').match(/(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)/);
            if (!m) {
              if (attempts === 1) log(`[需坐标] ${name} 为 GPS 签到，请在面板填写坐标`, true);
              notify(`📍 需要坐标 · ${name}`, 'GPS/雷达签到，请在面板填写教室坐标',
                S.cfg.notifyOnFail, `needcoords:${rid}`);
              continue;
            }
            if (await answerRadar(rid, m[1], m[2])) {
              S.seen.add(String(rid)); S.attempts.delete(String(rid)); S.gateSince.delete(String(rid));
              log(`[成功] GPS 签到 · ${name} · (${m[1]}, ${m[2]})`);
            } else if (attempts >= MAX_ATTEMPTS) {
              S.seen.add(String(rid)); log(`[放弃] ${name} GPS 签到提交失败 ${attempts} 次`, true);
            }
            continue;
          }

          // 二维码签到：登记为取码目标，交给亚秒级快循环处理（免拍照路径）
          if (/qr/i.test(String(rc.type || '')) || rc.is_qr === true) {
            S.pendingQr.set(String(rid), { name, at: now() });
            if (!S.cfg.qrAutoFetch) {
              notify(`📷 二维码签到 · ${name}`, '自动取码已关闭，请粘贴二维码内容提交',
                S.cfg.notifyOnQr, `qroff:${rid}`);
              continue;
            }
            if (!S.qrTargets.has(String(rid))) {
              S.qrTargets.set(String(rid), { cid, name });
              log(`[二维码] ${name} 发现进行中的二维码签到，开始自动取码`);
              notify(`📷 二维码签到 · ${name}`,
                '已尝试自动取码；若取不到，请把二维码内容粘到面板提交',
                S.cfg.notifyOnQr, `qrdetect:${rid}`);
            }
            continue;
          }

          // 其他未知类型：普通签到接口为推断，默认关
          if (S.cfg.tryPlain) {
            if (await answerPlain(rid)) {
              S.seen.add(String(rid)); log(`[成功] ${name} · 已按普通签到提交`);
            } else if (attempts >= MAX_ATTEMPTS) {
              S.seen.add(String(rid)); log(`[放弃] ${name} 提交失败 ${attempts} 次`, true);
            }
          } else if (attempts === 1) {
            log(`[跳过] ${name}：类型不支持自动处理（可用二维码粘贴框提交）`);
            notify(`⚠️ 需要你处理 · ${name}`, '该签到类型无法自动处理，请手动签到',
              S.cfg.notifyOnFail, `unsupported:${rid}`);
          }
        }
      }
      S.lastTick = now();
    } catch (e) {
      log(`[错误] ${e && e.message ? e.message : e}`, true);
    } finally {
      S.busy = false;
      render();
    }
  }

  /* -------------------------------------------------------------- 面板 UI */

  const CSS = `
  #ck-panel{position:fixed;right:14px;bottom:14px;width:330px;z-index:2147483646;
    font:12px/1.55 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#e8eaf0;
    background:#1d2130;border:1px solid #333b52;border-radius:10px;box-shadow:0 8px 28px rgba(0,0,0,.42);
    overflow:hidden;user-select:none}
  #ck-panel.min .ck-body{display:none}
  #ck-head{display:flex;align-items:center;gap:8px;padding:8px 10px;background:#252b3d;cursor:move}
  #ck-title{font-weight:600;flex:1}
  #ck-dot{width:8px;height:8px;border-radius:50%;background:#43d17c}
  #ck-dot.off{background:#8b93a7}
  .ck-btn{border:0;border-radius:6px;background:#333c56;color:#dfe4f0;padding:3px 8px;cursor:pointer;font-size:11px}
  .ck-btn:hover{background:#3f4a68}
  .ck-body{padding:9px 10px}
  .ck-row{display:flex;align-items:center;gap:6px;margin-bottom:6px}
  .ck-row label{color:#9aa4bd;min-width:64px}
  .ck-row input[type=text],.ck-row input[type=number]{flex:1;min-width:0;background:#161a26;border:1px solid #333b52;
    border-radius:6px;color:#e8eaf0;padding:3px 6px;font-size:11px}
  .ck-row input[type=checkbox]{accent-color:#5b8cff}
  #ck-log{max-height:150px;overflow:auto;margin-top:7px;border-top:1px solid #2c3446;padding-top:6px;
    font-size:11px;color:#a9b2c6}
  #ck-log div{padding:1px 0;white-space:pre-wrap;word-break:break-all}
  #ck-log .bad{color:#ff9f9f}
  #ck-log .ok{color:#7ee0a1}
  .ck-hint{color:#7d879e;font-size:10.5px;margin-top:6px}
  #ck-toasts{position:fixed;right:14px;top:14px;z-index:2147483647;display:grid;gap:8px;max-width:330px}
  .ck-toast{background:#232a3d;border:1px solid #3c4761;border-left:3px solid #ff9442;border-radius:8px;
    padding:9px 11px;color:#eef1f7;font:12px/1.5 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
    box-shadow:0 10px 26px rgba(0,0,0,.42);display:grid;gap:3px}
  .ck-toast strong{font-size:12.5px}
  .ck-toast span{color:#aab3c6;font-size:11px}
  #ck-pending{margin-top:6px;border-top:1px solid #2c3446;padding-top:6px;font-size:11px;color:#ffce9a}
  #ck-pending div{padding:1px 0;word-break:break-all}
  `;

  function render() {
    const wrap = document.getElementById('ck-panel');
    if (!wrap) return;

    const dot = document.getElementById('ck-dot');
    if (dot) dot.className = S.cfg.enabled ? '' : 'off';
    const title = document.getElementById('ck-title');
    if (title) title.textContent = `畅课自动签到 · ${S.cfg.host || location.host}`;

    const en = document.getElementById('ck-enabled');
    if (en && en.checked !== S.cfg.enabled) en.checked = S.cfg.enabled;
    const gate = document.getElementById('ck-gate');
    if (gate && gate.checked !== S.cfg.gate) gate.checked = S.cfg.gate;
    const plain = document.getElementById('ck-plain');
    if (plain && plain.checked !== S.cfg.tryPlain) plain.checked = S.cfg.tryPlain;
    const qrauto = document.getElementById('ck-qrauto');
    if (qrauto && qrauto.checked !== S.cfg.qrAutoFetch) qrauto.checked = S.cfg.qrAutoFetch;

    const itv = document.getElementById('ck-interval');
    if (itv && document.activeElement !== itv) itv.value = String(Math.round(S.cfg.intervalMs / 1000));
    const need = document.getElementById('ck-need');
    if (need && document.activeElement !== need) need.value = String(S.cfg.gateMinSigned);
    const nAny = document.getElementById('ck-notify');
    if (nAny) {
      nAny.checked = !!(S.cfg.notifyOnDetect || S.cfg.notifyOnSuccess
        || S.cfg.notifyOnFail || S.cfg.notifyOnQr);
    }
    const co = document.getElementById('ck-coords');
    if (co && document.activeElement !== co) co.value = S.cfg.coords;
    const relay = document.getElementById('ck-relay');
    if (relay) relay.checked = !!S.cfg.relayEnabled;
    const relayUrl = document.getElementById('ck-relay-url');
    if (relayUrl && document.activeElement !== relayUrl) relayUrl.value = S.cfg.relayUrl || '';
    const sched = document.getElementById('ck-sched');
    if (sched) sched.checked = !!S.cfg.scheduleEnabled;

    const stat = document.getElementById('ck-status');
    if (stat) {
      stat.textContent = S.sid
        ? `student_id=${S.sid}｜课程 ${S.courses.length} 门｜已处理 ${S.seen.size} 条`
          + (S.lastTick ? `｜上次 ${new Date(S.lastTick).toLocaleTimeString('zh-CN', { hour12: false })}` : '')
        : '尚未识别身份（请确认已登录畅课）';
    }

    const ckClock = document.getElementById('ck-clock');
    if (ckClock) {
      let realTime;
      try {
        realTime = new Date(now()).toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' });
      } catch (e) {
        realTime = new Date(now()).toLocaleString('zh-CN', { hour12: false });
      }
      const drift = clock.syncedAt ? (clock.offsetMs / 1000).toFixed(1) + 's' : '未校时';
      let line = '';
      if (S.cfg.scheduleEnabled) {
        const st = scheduleState();
        line = st.in
          ? `窗口内 ${hhmm(st.win[0])}~${hhmm(st.win[1])}｜${DOW_NAME[st.dow]}｜20s 轮询中`
          : `窗口外｜下一个 ${st.next ? DOW_NAME[st.next.day] + ' ' + hhmm(st.next.at) + '~' + hhmm(st.next.end) : '近 7 天无课'}`;
      } else {
        line = '课表窗口已关闭：固定间隔轮询';
      }
      const html = escapeHtml(line) + '<br>学校时间 ' + escapeHtml(realTime) + '（本机偏差 ' + drift + '）';
      if (ckClock.dataset.html !== html) { ckClock.dataset.html = html; ckClock.innerHTML = html; }
    }

    const pend = document.getElementById('ck-pending');
    if (pend) {
      const rows = Array.from(S.pendingQr.entries())
        .map(([rid, v]) => `<div>· ${escapeHtml(v.name)}（rid=${rid}）</div>`).join('');
      const html = rows ? `<div>需人工处理：</div>${rows}` : '';
      if (pend.dataset.html !== html) { pend.dataset.html = html; pend.innerHTML = html; }
    }

    const box = document.getElementById('ck-log');
    if (box) {
      const html = S.log.slice(-40).map(l =>
        `<div class="${l.bad ? 'bad' : (/成功/.test(l.msg) ? 'ok' : '')}">${l.t} ${escapeHtml(l.msg)}</div>`
      ).join('');
      if (box.dataset.html !== html) { box.dataset.html = html; box.innerHTML = html; box.scrollTop = box.scrollHeight; }
    }
  }

  const escapeHtml = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  function buildPanel() {
    if (document.getElementById('ck-panel')) return;

    if (typeof GM_addStyle === 'function') GM_addStyle(CSS);
    else {
      const st = document.createElement('style');
      st.textContent = CSS;
      document.head.appendChild(st);
    }

    const wrap = document.createElement('div');
    wrap.id = 'ck-panel';
    wrap.innerHTML = `
      <div id="ck-head">
        <span id="ck-dot"></span>
        <span id="ck-title">畅课自动签到</span>
        <button class="ck-btn" id="ck-toggle">暂停</button>
        <button class="ck-btn" id="ck-min">—</button>
      </div>
      <div class="ck-body">
        <div class="ck-row"><label>启用</label><input type="checkbox" id="ck-enabled">
          <label style="min-width:auto">门控</label><input type="checkbox" id="ck-gate"></div>
        <div class="ck-row"><label style="min-width:auto">自动取码(二维码)</label>
          <input type="checkbox" id="ck-qrauto">
          <label style="min-width:auto">试未知类型</label><input type="checkbox" id="ck-plain"></div>
        <div class="ck-row"><label>间隔(秒)</label><input type="number" id="ck-interval" min="5" step="1">
          <label style="min-width:auto">满几人签</label><input type="number" id="ck-need" min="1" step="1" style="max-width:64px">
          <label style="min-width:auto">提醒</label><input type="checkbox" id="ck-notify"></div>
        <div class="ck-row"><label>GPS 坐标</label><input type="text" id="ck-coords" placeholder="如 24.4331, 118.0957"></div>
        <div class="ck-row"><label>微信推送</label><input type="checkbox" id="ck-relay">
          <input type="text" id="ck-relay-url" placeholder="http://127.0.0.1:8787/notify"></div>
        <div class="ck-row"><label>课表轮询</label><input type="checkbox" id="ck-sched">
          <span style="color:#7d879e;font-size:10.5px">仅上课/课间前后10分钟，窗口内 20s</span></div>
        <div class="ck-row"><label>二维码内容</label><input type="text" id="ck-qr" placeholder="粘贴扫到的码或 /j?p= 链接"></div>
        <div class="ck-row"><button class="ck-btn" id="ck-qr-submit">提交二维码签到</button>
          <button class="ck-btn" id="ck-now">立即检测</button>
          <button class="ck-btn" id="ck-probe">探测取码权限</button>
          <button class="ck-btn" id="ck-probe-valid">探测服务端校验</button>
          <button class="ck-btn" id="ck-test-notify">测试提醒</button></div>
        <div id="ck-pending"></div>
        <div id="ck-status" class="ck-hint"></div>
        <div id="ck-clock" class="ck-hint"></div>
        <div id="ck-log"></div>
        <div class="ck-hint">数字签到全自动；二维码先试自动取码，取不到再粘贴码；GPS 需填坐标。</div>
      </div>`;

    document.body.appendChild(wrap);

    const head = wrap.querySelector('#ck-head');
    let drag = null;
    head.addEventListener('mousedown', e => { drag = { x: e.clientX, y: e.clientY, r: wrap.getBoundingClientRect() }; });
    document.addEventListener('mousemove', e => {
      if (!drag) return;
      wrap.style.left = `${drag.r.left + e.clientX - drag.x}px`;
      wrap.style.top = `${drag.r.top + e.clientY - drag.y}px`;
      wrap.style.right = 'auto';
      wrap.style.bottom = 'auto';
    });
    document.addEventListener('mouseup', () => { drag = null; });

    wrap.querySelector('#ck-min').addEventListener('click', () => wrap.classList.toggle('min'));
    wrap.querySelector('#ck-toggle').addEventListener('click', () => {
      S.cfg.enabled = !S.cfg.enabled; saveCfg();
      wrap.querySelector('#ck-toggle').textContent = S.cfg.enabled ? '暂停' : '继续';
      log(S.cfg.enabled ? '已启用监控' : '已暂停监控');
    });
    wrap.querySelector('#ck-enabled').addEventListener('change', e => {
      S.cfg.enabled = e.target.checked; saveCfg();
      wrap.querySelector('#ck-toggle').textContent = S.cfg.enabled ? '暂停' : '继续';
    });
    wrap.querySelector('#ck-gate').addEventListener('change', e => { S.cfg.gate = e.target.checked; saveCfg(); });
    wrap.querySelector('#ck-plain').addEventListener('change', e => { S.cfg.tryPlain = e.target.checked; saveCfg(); });
    wrap.querySelector('#ck-qrauto').addEventListener('change', e => {
      S.cfg.qrAutoFetch = e.target.checked; saveCfg(); startQrTimer();
      if (S.cfg.qrAutoFetch) tick();
    });
    wrap.querySelector('#ck-probe').addEventListener('click', () => {
      log('开始探测二维码取码权限');
      probeQrPermission();
    });
    wrap.querySelector('#ck-probe-valid').addEventListener('click', () => {
      probeQrValidation();     // 决定性实验：服务端到底校验不校验 data
    });
    wrap.querySelector('#ck-interval').addEventListener('change', e => {
      const v = Math.max(5, Number(e.target.value) || 10);
      S.cfg.intervalMs = v * 1000; saveCfg(); restartTimer();
    });
    wrap.querySelector('#ck-need').addEventListener('change', e => {
      S.cfg.gateMinSigned = Math.max(1, Number(e.target.value) || 4);
      saveCfg();
      log(`门控阈值已设为 ${S.cfg.gateMinSigned} 人`);
    });
    wrap.querySelector('#ck-notify').addEventListener('change', e => {
      S.cfg.notifyOnDetect = S.cfg.notifyOnSuccess = S.cfg.notifyOnFail = S.cfg.notifyOnQr = !!e.target.checked;
      saveCfg();
      log(e.target.checked ? '提醒已开启' : '提醒已关闭');
    });
    wrap.querySelector('#ck-test-notify').addEventListener('click', () => {
      S.notified.delete('测试提醒|通知链路正常');
      notify('测试提醒', '通知链路正常', true);
      log('已发送测试提醒');
    });
    wrap.querySelector('#ck-relay').addEventListener('change', e => {
      S.cfg.relayEnabled = e.target.checked; saveCfg();
      log(e.target.checked ? '微信推送已开启' : '微信推送已关闭');
      if (e.target.checked) {
        S.relayWarned = false;
        relayNotify('clawbot 连通测试', '微信推送已开启');
      }
    });
    wrap.querySelector('#ck-relay-url').addEventListener('change', e => {
      S.cfg.relayUrl = e.target.value.trim(); saveCfg();
      S.relayWarned = false;
      log(`推送中继地址：${S.cfg.relayUrl || '(空)'}`);
    });
    wrap.querySelector('#ck-sched').addEventListener('change', e => {
      S.cfg.scheduleEnabled = e.target.checked; saveCfg();
      log(e.target.checked ? '已启用课表窗口轮询（窗口内 20s）' : '已关闭课表窗口，退回固定间隔轮询');
      schedulerTick();
    });
    wrap.querySelector('#ck-coords').addEventListener('change', e => { S.cfg.coords = e.target.value.trim(); saveCfg(); });
    wrap.querySelector('#ck-now').addEventListener('click', () => { log('手动触发检测'); tick(); });

    wrap.querySelector('#ck-qr-submit').addEventListener('click', async () => {
      const raw = wrap.querySelector('#ck-qr').value.trim();
      if (!raw) { log('请先粘贴二维码内容', true); return; }

      // 兼容三种输入：完整 QR 链接/压缩串、裸 token、面板里待处理点名的码
      const parsed = resolveQrPayload(raw);
      const pending = Array.from(S.pendingQr.keys());
      let rid = parsed && parsed.rollcallId !== undefined ? String(parsed.rollcallId) : '';
      let data = parsed && parsed.data ? String(parsed.data) : '';

      if (!data && TOKEN_SHAPE.test(raw)) data = raw; // 直接粘贴 token
      if (!rid && pending.length) rid = pending[0];   // 用待处理点名
      if (!rid) { log('无法确定 rollcallId：请粘贴完整二维码内容', true); return; }
      if (!data) { log('内容里没有 data 字段，也不像裸 token', true); return; }

      const age = tokenAgeMs(data);
      log(`提交二维码 · rid=${rid}（token 年龄 ${age === null ? '未知' : Math.round(age / 1000) + 's'}）`);

      if (await answerQr(rid, data)) {
        S.seen.add(rid);
        S.pendingQr.delete(rid);
        log(`[成功] 二维码签到 · rollcallId=${rid}`);
        notify(`✅ 二维码签到成功 · ${rid}`, '已按粘贴的码提交',
          S.cfg.notifyOnSuccess, `okpasted:${rid}`);
      } else {
        log(`失败：码可能已过期（仅数秒有效），请重新取码`, true);
        notify('❌ 二维码提交失败', '码可能已过期（仅数秒有效），请重新取码再试',
          S.cfg.notifyOnFail, `failpasted:${rid}-${Date.now()}`);
      }
    });

    render();
  }

  /** 调度器：窗口内按 20 秒轮询；窗口外不查签到，只做低频校时 */
  async function schedulerTick() {
    if (!S.cfg.enabled) return;

    if (!S.cfg.scheduleEnabled) {
      if (now() - S.lastTick >= Math.max(5000, Number(S.cfg.intervalMs) || 10000)) {
        S.lastTick = now();
        tick();
      }
      return;
    }

    const st = scheduleState();
    if (!st.in) {
      if (S.inWindowFlag) {
        S.inWindowFlag = false;
        const n = st.next;
        log('[课表] 离开窗口，暂停轮询' + (n ? `（下一个：${DOW_NAME[n.day]} ${hhmm(n.at)}~${hhmm(n.end)}）` : '（近 7 天无课）'));
      }
      // 窗口外只保持校时，不查签到
      syncClock(false);
      render();
      return;
    }

    if (!S.inWindowFlag) {
      S.inWindowFlag = true;
      log(`[课表] 进入窗口 ${hhmm(st.win[0])}~${hhmm(st.win[1])}（${DOW_NAME[st.dow]}）——开启 20s 轮询`);
      const ok = await syncClock(true);
      log(ok
        ? `[校时] 已对齐服务端时间（本机偏差 ${(clock.offsetMs / 1000).toFixed(1)}s，RTT ${clock.rttMs}ms）`
        : '[校时] 失败：暂用本机时间判断，窗口可能不准', !ok);
    }

    if (now() - S.lastTick >= (Number(S.cfg.pollInWindowMs) || 20000)) {
      S.lastTick = now();
      tick();
    }
    render();
  }

  function restartTimer() {
    if (S.timer) clearInterval(S.timer);
    S.timer = setInterval(schedulerTick, Math.max(1000, Number(S.cfg.windowCheckMs) || 5000));
  }

  /* --------------------------------------------------------------- 启动 */

  installSniffer();

  async function boot() {
    if (S.booted) return;
    S.booted = true;

    if (S.cfg.host && location.host !== S.cfg.host) return; // 不是已绑定的畅课站点，静默退出

    // 未绑定时先探测一次：能读到 /api/profile 才认为这是畅课
    if (!S.cfg.host) {
      try {
        const r = await api('/api/profile');
        if (!(r.ok && r.data && (r.data.id || r.data.user_id))) return;
      } catch (e) { return; }
    }

    if (!document.body) { document.addEventListener('DOMContentLoaded', boot, { once: true }); return; }

    buildPanel();
    log(`脚本已加载 @ ${location.href}`);
    restartTimer();
    startQrTimer();
    syncClock(true).then(ok => {
      log(ok
        ? `[校时] 本机时钟偏差 ${(clock.offsetMs / 1000).toFixed(1)}s（已按服务端时间校正，未改动系统时间）`
        : '[校时] 失败：将用本机时间，课表窗口可能不准', !ok);
      schedulerTick();
    });

    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) { log('标签页回到前台，立即检测'); tick(); }
    });

    if (typeof GM_registerMenuCommand === 'function') {
      GM_registerMenuCommand('绑定当前站点为畅课', () => {
        S.cfg.host = location.host; saveCfg(); log(`已绑定 ${S.cfg.host}`);
        tick();
      });
      GM_registerMenuCommand('解除绑定', () => { S.cfg.host = ''; saveCfg(); log('已解除绑定'); });
      GM_registerMenuCommand('立即检测', () => tick());
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, { once: true });
  else boot();

  // SPA 路由变化不重建面板，仅提前触发一次检测
  window.addEventListener('hashchange', () => { if (S.booted) tick(); });
})();
