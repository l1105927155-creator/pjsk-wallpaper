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
  var lock = document.getElementById('lockImg');
  return JSON.stringify({
    src: img ? img.getAttribute('src') : null,
    badge: t('deskBadge'), last: t('deskLast'), next: t('deskNext'),
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

  console.log(`目标: ${APP}\n`);
  await send('Runtime.enable');
  await send('Page.enable');
  await send('Page.navigate', { url: APP });
  await sleep(3500);

  console.log('[1] 初始状态');
  const a = await probe();
  console.log('    ' + JSON.stringify(a));
  check('页面已连上后端', a.pill === '运行中', 'pill=' + a.pill);
  check('预览图加载成功', a.loaded === true);
  check('预览 URL 带变体参数（避免停留在旧位图）', /[?&]v=/.test(a.src || ''));

  console.log('\n[2] 点击「下一张」后界面应立即刷新（不依赖 3 秒轮询）');
  await evaluate("document.getElementById('deskNext').click(), 1");
  await sleep(900);
  const b = await probe();
  console.log('    ' + JSON.stringify(b));
  check('返回了新图片且徽标已更新', b.badge !== a.badge, `${a.badge} -> ${b.badge}`);
  check('上一次/下一次时间已更新', b.last !== a.last);
  check('新预览图已加载', b.loaded === true);

  console.log('\n[3] 外部改状态后页面应自行追上（轮询/看门狗）');
  const before = await probe();
  await evaluate(`(async function(){
    await fetch('/api/wallpaper/desktop/next', { method:'POST',
      headers:{'X-Pjsk-Token': window.PJSK_TOKEN, 'Content-Type':'application/json'},
      body:'{}' });
    return 1;
  })()`);
  await sleep(4500);
  const c = await probe();
  check('页面自动同步到最新状态', c.badge !== before.badge, `${before.badge} -> ${c.badge}`);

  console.log('\n[4] 页面冻结→恢复后应重新同步');
  await send('Page.setWebLifecycleState', { state: 'frozen' });
  await sleep(800);
  await send('Page.setWebLifecycleState', { state: 'active' });
  await evaluate("window.dispatchEvent(new Event('focus')), 1");
  await sleep(1200);
  const d = await probe();
  check('恢复后状态正常', d.pill === '运行中', 'pill=' + d.pill);

  console.log('\n[5] 页面 JS 异常');
  check('无未捕获异常', errors.length === 0, errors.join(' | '));

  ws.close();
  const failed = results.filter(r => !r.ok);
  console.log(`\n==== ${results.length - failed.length}/${results.length} 通过 ====`);
  process.exit(failed.length ? 1 : 0);
}

main().catch(e => { console.error('运行失败:', e.message); process.exit(2); });
