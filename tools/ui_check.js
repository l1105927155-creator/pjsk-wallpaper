#!/usr/bin/env node
/**
 * tools/ui_check.js — Web UI 实机自检（CDP 驱动真实 Chrome）
 *
 * 用途：验证「点下一张后界面是否正确刷新」以及「页面能否自行追上外部状态变化」，
 *       并抓取页面 JS 异常。排查过「接口成功但界面不更新」一类问题，
 *       比只看后端接口更有说服力。
 *
 * 前置：
 *   1) 程序在运行（默认 http://127.0.0.1:8788/）
 *   2) 已在后台启动一个可远程调试的 Chrome：
 *        chrome --headless=new --remote-debugging-port=9222 \
 *               --user-data-dir=%TEMP%\pjsk-cdp about:blank
 *
 * 用法：
 *   node tools/ui_check.js [http://127.0.0.1:8788/] [--debug-port=9222]
 *
 * 退出码：0 = 全部通过；1 = 存在失败项。
 */
'use strict';

const args = process.argv.slice(2);
const APP = args.find(a => a.startsWith('http')) || 'http://127.0.0.1:8788/';
const dbgArg = args.find(a => a.startsWith('--debug-port='));
const CDP_HTTP = 'http://127.0.0.1:' + (dbgArg ? dbgArg.split('=')[1] : '9222');

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

const PROBE = `(function(){
  function t(id){ var e = document.getElementById(id); return e ? e.textContent : null; }
  var img = document.getElementById('deskImg');
  var buf = document.getElementById('deskImgBuf');
  var lock = document.getElementById('lockImg');
  function vis(e){
    if (!e) return null;
    var cs = getComputedStyle(e);
    return e.classList.contains('show') && cs.display !== 'none';
  }
  return JSON.stringify({
    src: img ? img.getAttribute('src') : null,
    cls: img ? img.className : null,
    opacity: img ? Number(getComputedStyle(img).opacity) : null,
    bufVisible: vis(buf),
    bufHasSrc: !!(buf && buf.getAttribute('src')),
    // 可见图层数：过渡结束后应当只剩 1 个可见层，不能留下重影
    visibleLayers: (buf && vis(buf) ? 1 : 0) + (img && !img.classList.contains('hidden') ? 1 : 0),
    badge: t('deskBadge'), last: t('deskLast'), next: t('deskNextTime'),
    lockBadge: t('lockBadge'),
    pill: t('statusPill'),
    loaded: img ? (img.complete && img.naturalWidth > 0) : null,
    lockLoaded: lock ? (lock.complete && lock.naturalWidth > 0) : null
  });
})()`;

const results = [];
function check(name, ok, extra) {
  results.push({ name, ok: !!ok, extra: extra || '' });
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${name}${extra ? '  (' + extra + ')' : ''}`);
}

async function main() {
  let page = null;
  for (let i = 0; i < 40 && !page; i++) {
    try {
      const list = await (await fetch(CDP_HTTP + '/json/list')).json();
      page = list.find(x => x.type === 'page');
    } catch (e) { /* Chrome 还没起来 */ }
    if (!page) await sleep(300);
  }
  if (!page) throw new Error(`无法连接 Chrome 调试端口 ${CDP_HTTP}，请先按文件头说明启动 Chrome`);

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const errors = [];
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const { resolve, reject } = pending.get(m.id);
      pending.delete(m.id);
      m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
    } else if (m.method === 'Runtime.exceptionThrown') {
      const d = m.params.exceptionDetails;
      errors.push((d.exception && d.exception.description) || d.text);
    }
  };
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const mid = ++id;
    pending.set(mid, { resolve, reject });
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
  const evaluate = async (expr) => {
    const r = await send('Runtime.evaluate',
      { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) {
      throw new Error(r.exceptionDetails.text + ' ' +
        ((r.exceptionDetails.exception && r.exceptionDetails.exception.description) || ''));
    }
    return r.result.value;
  };
  const probe = async () => JSON.parse(await evaluate(PROBE));
  const waitFor = async (fn, timeoutMs) => {
    const t0 = Date.now();
    while (Date.now() - t0 < timeoutMs) {
      if (await fn()) return true;
      await sleep(250);
    }
    return false;
  };

  console.log(`目标: ${APP}\n`);
  await send('Runtime.enable');
  await send('Page.enable');
  // 固定视口：默认的无头窗口只有 800×600，「下一张」按钮会掉到折线以下，
  // elementFromPoint 返回 null，真实鼠标点击就永远打不到按钮上。
  await send('Emulation.setDeviceMetricsOverride',
    { width: 1280, height: 1000, deviceScaleFactor: 1, mobile: false });
  await send('Page.navigate', { url: APP });
  await sleep(3500);

  console.log('[1] 基础检查');
  const basic = await evaluate(`(function(){
    var seen = {}, dups = [];
    Array.prototype.forEach.call(document.querySelectorAll('[id]'), function(el){
      if (seen[el.id]) { dups.push(el.id + '(<' + seen[el.id] + '>+<' + el.tagName + '>)'); }
      else { seen[el.id] = el.tagName; }
    });
    return JSON.stringify({ dupIds: dups.slice(0, 10), dupCount: dups.length });
  })()`);
  const bi = JSON.parse(basic);
  check('页面无重复 id（重复会让 getElementById 拿到错误元素）', bi.dupCount === 0,
        bi.dupIds.join(', '));

  const a = await probe();
  console.log('    ' + JSON.stringify(a));
  check('页面已连上后端', a.pill === '运行中', 'pill=' + a.pill);
  check('预览图加载成功', a.loaded === true);

  console.log('\n[2] 用「真实鼠标点击」按下「下一张」');
  // 关键：必须用真实鼠标事件，不能用 JS 的 element.click()——
  // 后者绕过命中测试，会把「事件绑到了错误元素/被遮挡」这类问题掩盖掉。
  const box = await evaluate(`(function(){
    var all = document.querySelectorAll('[id="deskNext"]');
    var b = document.querySelector('button#deskNext');
    if (!b) return JSON.stringify({ missing: true, count: all.length });
    var r = b.getBoundingClientRect();
    var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    var top = document.elementFromPoint(cx, cy);
    return JSON.stringify({
      dupCount: all.length,
      x: cx, y: cy, w: Math.round(r.width), h: Math.round(r.height),
      disabled: !!b.disabled,
      topTag: top ? top.tagName : null,
      topId: top ? top.id : null,
      hitsButton: top === b
    });
  })()`);
  const bx = JSON.parse(box);
  console.log('    ' + JSON.stringify(bx));
  if (bx.missing) {
    check('存在 <button id="deskNext">', false);
    ws.close(); process.exit(1);
  }
  check('id="deskNext" 在页面中唯一', bx.dupCount === 1, `${bx.dupCount} 个`);
  check('按钮位置命中的就是按钮本身', bx.hitsButton && bx.topTag === 'BUTTON',
        `命中 <${bx.topTag}${bx.topId ? '#' + bx.topId : ''}>`);
  check('按钮未被禁用', !bx.disabled);

  const mouse = (type, extra) => send('Input.dispatchMouseEvent', Object.assign({
    type: type, x: bx.x, y: bx.y, button: 'left', clickCount: 1
  }, extra || {}));
  await mouse('mouseMoved');
  await mouse('mousePressed', { buttons: 1 });
  await mouse('mouseReleased', { buttons: 0 });
  const tClick = Date.now();

  // 密集采样上层透明度：硬切会一直是 1（或直接跳到新图），
  // 只有真的走了过渡才会采到 0 与 1 之间的中间态。
  // 同时记时间戳，用来核对「起步时刻」和「过渡时长」。
  const samples = [], times = [];
  for (let i = 0; i < 45; i++) {
    samples.push(await evaluate(`(function(){
      var c = document.getElementById('deskImg');
      var b = document.getElementById('deskImgBuf');
      return getComputedStyle(c).opacity + '|' +
             (c.classList.contains('fading') ? 'fading' : '-') + '|' +
             (b.classList.contains('show') ? 'buf' : '-');
    })()`));
    times.push(Date.now());
    await sleep(50);
  }
  const mid = samples.map(s => Number(s.split('|')[0])).filter(o => o > 0.02 && o < 0.98);
  const sawFading = samples.some(s => s.indexOf('|fading|') >= 0);
  // 过渡区间：'fading' 只在这段内为真，其跨度就是过渡时长
  const fi = samples.map((s, i) => (s.indexOf('|fading|') >= 0 ? i : -1)).filter(i => i >= 0);
  const fadeSpan = fi.length > 1 ? times[fi[fi.length - 1]] - times[fi[0]] : 0;

  await waitFor(async () => {
    const p = await probe();
    return p.badge !== a.badge && p.loaded === true;
  }, 10000);
  // 过渡结束后下层必须被撤掉，不能留下两份可见图层
  await waitFor(async () => (await probe()).visibleLayers === 1, 4000);
  const b = await probe();
  console.log('    ' + JSON.stringify(b));
  check('真实点击后返回了新图片且界面已刷新', b.badge !== a.badge,
        `${a.badge} -> ${b.badge}`);
  check('预览切换有过渡动画（采到淡出的中间透明度）',
        mid.length > 0 && sawFading,
        `采样 ${samples.length} 次，中间态 ${mid.length} 次，fading ${sawFading}`);
  // 900ms 是实测的 Windows 换壁纸淡入时长；若这里退回 600ms 说明常量又被改回去了
  check('过渡时长与系统淡入一致（约 900ms）',
        fadeSpan >= 700 && fadeSpan <= 1200, `实测 ${fadeSpan}ms`);
  check('过渡结束后不留重影（可见图层只剩 1 个）', b.visibleLayers === 1,
        'visibleLayers=' + b.visibleLayers + ' bufVisible=' + b.bufVisible);
  check('上一次/下一次时间已更新', b.last !== a.last);
  check('「下一次切换」显示的是时间而不是按钮文字', /^\d\d-\d\d \d\d:\d\d:\d\d$|^—|^即将执行/.test(b.next || ''),
        'next=' + b.next);
  check('新预览图已加载', b.loaded === true);

  console.log('\n[3] 预览等够提前量才起步，且最终状态推送不会让过渡重来');
  const before = await probe();
  const seq = await evaluate(`(async function(){
    var img = document.getElementById('deskImg');
    var badge = document.getElementById('deskBadge');
    var tNotify = null, tSrc = null, starts = 0, prev = img.classList.contains('fading');
    // 状态推送到达前端、render 把新名字写进角标的时刻
    var mBadge = new MutationObserver(function(){
      if (tNotify === null) { tNotify = performance.now(); }
    });
    mBadge.observe(badge, { childList: true, characterData: true, subtree: true });
    var mImg = new MutationObserver(function(){
      var f = img.classList.contains('fading');
      if (f && !prev) { starts++; if (tSrc === null) { tSrc = performance.now(); } }
      prev = f;
    });
    mImg.observe(img, { attributes: true, attributeFilter: ['src', 'class'] });
    var t0 = performance.now();
    await fetch('/api/wallpaper/desktop/next', { method:'POST',
      headers:{'X-Pjsk-Token': window.PJSK_TOKEN, 'Content-Type':'application/json'},
      body:'{}' });
    var tDone = performance.now();
    // 等过渡走完，也等切换完成后的那次状态推送到达
    await new Promise(function(r){ setTimeout(r, 2200); });
    mBadge.disconnect();
    mImg.disconnect();
    return JSON.stringify({ t0: t0, tNotify: tNotify, tSrc: tSrc, tDone: tDone, starts: starts });
  })()`);
  const sq = JSON.parse(seq);
  // 只量前端负责的那一段：从「收到通知」到「开始过渡」。
  // （点击到通知之间还有后端预热缩略图的耗时，那部分会把系统与预览一起推迟，不算错位。）
  const delayMs = (sq.tSrc === null || sq.tNotify === null) ? -1 : sq.tSrc - sq.tNotify;
  // 系统在收到下发后还要约 0.5s 才真正开始换桌面，预览必须等它，
  // 否则就是「预览先切换，随后壁纸切换」。
  check('预览等够提前量才起步（不是一收到通知就换）',
        delayMs >= 350 && delayMs <= 900,
        `起步于收到通知后 ${delayMs >= 0 ? delayMs.toFixed(0) : '—'}ms（提前量 500ms）`);
  check('预览在「下一张」请求返回之前就已开始换图（同步推进，非事后补）',
        sq.tSrc !== null && sq.tSrc < sq.tDone,
        `预览 ${sq.tSrc === null ? '—' : sq.tSrc.toFixed(0)}ms < 响应 ${sq.tDone.toFixed(0)}ms`);
  // 切换完成时后端会再推一次状态（current 已是新图、pending 为空）；
  // 排定的目标若没提前登记，那次推送会把过渡提前重来一遍。
  check('最终状态推送没有把过渡重来一遍', sq.starts === 1, `过渡开始 ${sq.starts} 次`);
  const c = await probe();
  check('外部触发切换后页面自行同步到最新状态', c.badge !== before.badge,
        `${before.badge} -> ${c.badge}`);

  console.log('\n[4] 页面 JS 异常');
  check('无未捕获异常', errors.length === 0, errors.join(' | '));

  ws.close();
  const failed = results.filter(r => !r.ok);
  console.log(`\n==== ${results.length - failed.length}/${results.length} 通过 ====`);
  process.exit(failed.length ? 1 : 0);
}

main().catch(e => { console.error('运行失败:', e.message); process.exit(2); });
