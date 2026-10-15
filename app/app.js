/* pjsk 壁纸 · 控制台前端
   原生 JS，无框架、无外部依赖。只通过同源 HTTP 调用本地后端；
   所有状态以后端返回为准，倒计时基于后端绝对时间并定期校准。 */

(function () {
  "use strict";

  var TOKEN = window.PJSK_TOKEN || "";
  var PAGE_SIZE = 24;

  // ---------------------------------------------------------------- 工具

  var $ = function (id) { return document.getElementById(id); };

  function api(path, opts) {
    opts = opts || {};
    var init = { method: opts.method || "GET", headers: {} };
    init.headers["X-Pjsk-Token"] = TOKEN;
    if (opts.form) {
      init.body = opts.form;                       // FormData：交给浏览器设 Content-Type
    } else if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    return fetch(path, init).then(function (r) {
      return r.text().then(function (t) {
        var body;
        try { body = JSON.parse(t); } catch (e) { body = t; }
        return { status: r.status, ok: r.ok, body: body };
      });
    });
  }

  function imgUrl(id, width) {
    var u = "/api/images/" + id + "?t=" + encodeURIComponent(TOKEN);
    if (width) { u += "&w=" + width; }
    return u;
  }

  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function fmtTime(ts) {
    if (!ts) { return "—"; }
    var d = new Date(ts * 1000);
    return pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " +
           pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  function fmtLeft(sec) {
    if (sec === null || sec === undefined) { return "—"; }
    if (sec < 0) { return "即将执行"; }
    var s = Math.round(sec), h = Math.floor(s / 3600),
        m = Math.floor((s % 3600) / 60);
    s = s % 60;
    if (h > 0) { return h + " 小时 " + m + " 分"; }
    if (m > 0) { return m + " 分 " + pad(s) + " 秒"; }
    return s + " 秒";
  }

  function toast(msg, kind) {
    var box = $("toasts");
    var el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(function () { el.remove(); }, 4200);
  }

  function setBanner(el, kind, text) {
    if (!text) { el.classList.add("hidden"); el.textContent = ""; return; }
    el.className = "banner " + (kind || "");
    el.textContent = text;
    el.classList.remove("hidden");
  }

  // 项目地址：内嵌窗口里非输入元素默认不可选中，所以必须点一下就能复制。
  // 优先用剪贴板 API；不可用时退回「临时文本框 + execCommand」。
  var REPO_URL = "https://github.com/l1105927155-creator/pjsk-wallpaper";

  function copyText(text, okMsg) {
    function fallback() {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.top = "-1000px";
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      document.body.removeChild(ta);
      toast(ok ? okMsg : "复制失败，请手动输入地址", ok ? "ok" : "err");
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () { toast(okMsg, "ok"); }, fallback);
    } else {
      fallback();
    }
  }

  function setInput(el, value) {
    if (!el || document.activeElement === el) { return; }
    if (String(el.value) !== String(value)) { el.value = value; }
  }

  function check(el, value) {
    if (!el || document.activeElement === el) { return; }
    el.checked = !!value;
  }

  function busy(btn, on) {
    if (!btn) { return; }
    btn.disabled = !!on;
    btn.classList.toggle("busy", !!on);
  }

  // 「立即更新」按钮的忙碌状态只有一个来源：后端 update.running。
  // 每次按状态设置都 +1，供点击后的兜底判断使用（见 doUpdate 的看门狗）：
  // 计数没动 = 这次点击之后一次状态都没成功应用到按钮上。
  var updateBusySeq = 0;

  function applyUpdateBusy(running) {
    updateBusySeq++;
    busy($("btnUpdate"), running);
    busy($("btnUpdate2"), running);
  }

  // ---------------------------------------------------------------- 状态

  var S = null;            // 最近一次 /api/state 的返回
  var offset = 0;          // server_time - 本地时间（秒），用于倒计时校准
  var connected = false;
  var pollTimer = null;    // 长轮询重排计时器
  var pollSeq = 0;         // 请求序号：只认最后一次发出的请求的响应
  var POLL_WAIT = 25;      // 长轮询挂起秒数（与后端 STATE_WAIT_MAX 对应）

  function setPill(kind, text) {
    var el = $("statusPill");
    el.className = "pill" + (kind ? " " + kind : "");
    el.textContent = text;
  }

  function schedulePoll(delay) {
    if (pollTimer) { clearTimeout(pollTimer); }
    pollTimer = setTimeout(loadState, delay || 0);
  }

  /* 取状态：首次与动作后立即返回；之后带上已知修订号长轮询，
     后端在状态变化的同一时刻唤醒它。
     这样「切换壁纸」时前端不必等下一次固定周期轮询就能知道新图——
     那正是预览比桌面晚一步的原因。
     前端不做任何延迟：收到通知即开始过渡，与后端发起切换是同一时刻。 */
  function loadState() {
    var mine = ++pollSeq;
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    var q = (connected && S && typeof S.rev === "number")
      ? "?since=" + S.rev + "&wait=" + POLL_WAIT : "";
    return api("/api/state" + q).then(function (r) {
      if (mine !== pollSeq) { return; }
      if (r.status !== 200) {
        connected = false;
        setPill("err", r.status === 403 ? "令牌无效，请从托盘重新打开" : "服务不可用");
        schedulePoll(2000);
        return;
      }
      connected = true;
      S = r.body;
      offset = (S.server_time || Date.now() / 1000) - Date.now() / 1000;
      render(S);
      // 宿主在「用户点了关闭且未保存过选择」时经状态推送请求弹确认框。
      // 该标志是粘性待办（状态读取者不止本页一个，读到不清零）：弹框后带
      // 当次请求身份号确认，只有匹配的确认才清除，迟到的旧确认不会误清新请求。
      if (S.close_prompt) {
        promptClose();
        api("/api/window/close/ack",
            { method: "POST", body: { prompt: S.close_prompt } })
          .catch(function () { /* 确认失败也没关系，下次状态仍会重提 */ });
      }
      schedulePoll();
    }).catch(function () {
      if (mine !== pollSeq) { return; }
      connected = false;
      setPill("err", "连接中断");
      schedulePoll(2000);
    });
  }

  var TAB_TITLES = { overview: "概览", gallery: "图库", settings: "设置" };

  // 更新进度后缀「已下载/总计」：下载还没开始时后端没有总数，只显示「更新中…」
  function updateProgressSuffix(u) {
    if (!u || !u.progress || !u.progress.total) { return ""; }
    return " " + u.progress.done + "/" + u.progress.total;
  }

  function render(s) {
    // 顶部状态
    if (s.update.running) {
      setPill("busy", "图库更新中…" + updateProgressSuffix(s.update));
    }
    else if (s.status && s.status.indexOf("失败") >= 0) { setPill("err", s.status); }
    else { setPill("ok", "运行中"); }

    // 概览：桌面 / 锁屏
    renderWall("desk", s.desktop);
    renderWall("lock", s.lock);

    // 图库统计
    $("cntPjsk").textContent = s.gallery.pjsk;
    $("cntCustom").textContent = s.gallery.custom;
    $("cntTotal").textContent = s.gallery.total;
    if (!s.config.use_pjsk && !s.config.use_custom) {
      setBanner($("sourceBanner"), "warn",
        "pjsk 与自定义图库均已关闭：壁纸切换将因图库为空而无动作。");
    } else if (s.gallery.total === 0) {
      setBanner($("sourceBanner"), "warn",
        "当前图库为空：请添加图片或执行图库更新。");
    } else {
      setBanner($("sourceBanner"), "", "");
    }

    // 更新状态
    var u = s.update;
    // 更新图源地址由后端给出（地区到地址的映射以后端为准，前端不重复维护）
    $("updateSourceHint").textContent = u.source
      ? ("更新将从 " + u.source + " 拉取卡面，需保持网络通畅")
      : "更新将从卡面图源拉取，需保持网络通畅";
    $("upState").textContent = u.running
      ? ("更新中…" + updateProgressSuffix(u)) : (u.message || "空闲");
    $("upLast").textContent = fmtTime(u.last_run);
    $("upNext").textContent = u.interval_enabled ? fmtTime(u.next) : "—（未启用间隔更新）";
    if (u.running) {
      setBanner($("updateBanner"), "warn",
        "图库更新进行中" + updateProgressSuffix(u) + "，完成后会自动刷新计数。");
    } else if (u.ok === false) {
      setBanner($("updateBanner"), "err", u.message || "上次更新失败");
    } else if (u.ok === true) {
      setBanner($("updateBanner"), "ok", u.message || "上次更新完成");
    } else {
      setBanner($("updateBanner"), "", "");
    }
    applyUpdateBusy(u.running);

    // 设置区（同步后端最新值）
    syncSettings(s);
    $("dzScreen").textContent = (s.system.screen || []).join("×") || "屏幕";

    tickCountdown();
  }

  // ------------------------------------------------- 壁纸预览（与切换同步）

  // 与 styles.css 的 --wall-fade 必须一致。
  // 900ms 是实测的 Windows 换壁纸淡入时长（本机 8 次落在 851~934ms），
  // 取它就是让预览的过渡与桌面背景的过渡观感相同。
  var WALL_FADE_MS = 900;
  var wallSeq = { desk: 0, lock: 0 };

  /* 预览换图，并做与桌面背景相同的交叉淡入。
     两层设计：上层是当前图，下层是即将生效的新图。
     新图先整幅就位，再让上层淡出把它「揭」出来 —— 这就是一次交叉过渡，
     且角色不互换，<prefix>Img 始终代表当前壁纸。
     不做任何人为延迟：后端在写系统壁纸之前就广播，前端收到即开始，
     程序层面与切换同一时刻。 */
  function setWall(prefix, url) {
    var cur = $(prefix + "Img"), buf = $(prefix + "ImgBuf");
    if (!url) {                                   // 无图：两层都清空
      wallSeq[prefix]++;
      cur.classList.add("hidden");
      cur.classList.remove("fading");
      cur.dataset.url = "";
      cur.removeAttribute("src");
      buf.classList.remove("show");
      buf.removeAttribute("src");
      return;
    }
    if (url === cur.dataset.url) { return; }       // 已经（或已排定为）当前图，不动

    if (!cur.dataset.url) {                        // 首图：没有旧图可淡出
      cur.dataset.url = url;
      cur.setAttribute("src", url);
      cur.classList.remove("hidden", "fading");
      return;
    }

    var my = ++wallSeq[prefix];                    // 连点「下一张」时只认最后一次
    var prevUrl = cur.dataset.url;
    // 立刻登记目标：切换完成时后端会再推一次状态（那次 current 已是新图、
    // pending 为空），没有这个标记就会把同一次过渡再触发一遍。
    cur.dataset.url = url;

    var settle = function () {
      // 过渡结束：把新图搬到上层并立即恢复不透明。
      // 此刻下层是同图，即使上层还没绘制完也看不出跳变。
      cur.classList.remove("fading");
      var done = function () {
        cur.onload = cur.onerror = null;
        if (wallSeq[prefix] !== my) { return; }
        buf.classList.remove("show");              // 上层已就位，撤掉下层，不留重影
        buf.removeAttribute("src");
      };
      cur.onload = cur.onerror = done;
      cur.setAttribute("src", url);
      if (cur.complete && cur.naturalWidth) { done(); }
    };
    var ready = function (ok) {
      buf.onload = buf.onerror = null;
      if (wallSeq[prefix] !== my) { return; }
      if (!ok) {                                   // 新图取不到：保留当前图，不做过渡
        buf.classList.remove("show");
        buf.removeAttribute("src");
        cur.dataset.url = prevUrl;                 // 撤销登记，别把当前图卡住
        return;
      }
      buf.classList.add("show");                   // 新图就位（下层、无过渡，被上层盖住）
      cur.classList.add("fading");                 // 旧图淡出，揭示新图
      setTimeout(function () {
        if (wallSeq[prefix] !== my) { return; }
        settle();
      }, WALL_FADE_MS + 50);
    };
    buf.onload = function () { ready(true); };
    buf.onerror = function () { ready(false); };
    buf.setAttribute("src", url);
  }

  function renderWall(prefix, w) {
    var ph = $(prefix + "Placeholder"), badge = $(prefix + "Badge");
    // 应用中的目标优先：后端在写系统壁纸之前就广播了它，
    // 前端收到即开始过渡，与后端发起切换同一时刻。
    var show = w.pending || w.current;
    if (show && show.id) {
      setWall(prefix, imgUrl(show.id, 1024));
      ph.classList.add("hidden");
      badge.textContent = show.name;
      badge.classList.remove("hidden");
    } else {
      setWall(prefix, "");
      ph.classList.remove("hidden");
      badge.classList.add("hidden");
    }
    $(prefix + "Last").textContent = fmtTime(w.last);
    // 注意：显示位 id 是 <prefix>NextTime，不能与「下一张」按钮 <prefix>Next 重名，
    // 否则 getElementById 会先命中显示位、把事件绑到错误元素上。
    $(prefix + "NextTime").textContent = w.enabled
      ? (w.next ? fmtTime(w.next) : "即将执行") : "—（未启用）";
    $(prefix + "IntervalTxt").textContent = w.interval + " 分钟";
    setInput($(prefix + "Interval"), w.interval);
    check($(prefix + "Enabled"), w.enabled);
  }

  function syncSettings(s) {
    var c = s.config;
    setInput($("setDeskInterval"), c.desktop.interval);
    setInput($("setLockInterval"), c.lock.interval);
    check($("setDeskEnabled"), c.desktop.enabled);
    check($("setLockEnabled"), c.lock.enabled);
    check($("setUsePjsk"), c.use_pjsk);
    check($("setUseCustom"), c.use_custom);
    setInput($("setCap"), c.cap_pjsk);
    check($("setUpIntervalOn"), c.update.interval_enabled);
    setInput($("setUpInterval"), c.update.interval);
    setInput($("setUpCount"), c.update.count);
    setInput($("setUpRarity"), c.update.min_rarity);
    setInput($("setUpChara"), c.update.chara);
    setInput($("setUpUnit"), c.update.unit);
    check($("setAutostart"), s.system.autostart);

    var kv = $("sysInfo");
    kv.innerHTML = "";
    [["程序目录", s.system.app_dir], ["pjsk 图库", s.system.pjsk_dir],
     ["自定义图库", s.system.custom_dir],
     ["处理分辨率", (s.system.screen || []).join(" × ")],
     ["Python", s.system.python]].forEach(function (row) {
      var dt = document.createElement("dt"); dt.textContent = row[0];
      var dd = document.createElement("dd"); dd.textContent = row[1];
      kv.appendChild(dt); kv.appendChild(dd);
    });
  }

  // ------------------------------------------------------- 倒计时（每秒）

  var last = { desk: null, lock: null, up: null };

  function tickCountdown() {
    if (!S) { return; }
    var now = Date.now() / 1000 + offset;
    [["desk", S.desktop, "deskCountdown"], ["lock", S.lock, "lockCountdown"]]
      .forEach(function (t) {
        var w = t[1], el = $(t[2]);
        if (!w.enabled) { el.textContent = "—"; return; }
        if (!w.next) { el.textContent = "即将执行"; return; }
        el.textContent = fmtLeft(w.next - now);
      });
  }
  setInterval(tickCountdown, 1000);

  // ---------------------------------------------------------------- 操作

  function patch(body, statusEl, okMsg) {
    return api("/api/config", { method: "PATCH", body: body }).then(function (r) {
      if (r.ok) {
        if (statusEl) { setBanner($(statusEl), "ok", okMsg || "已保存"); }
        if (r.body && r.body.warnings && r.body.warnings.length) {
          toast(r.body.warnings.join("；"), "err");
        }
        if (r.body && r.body.state) {
          S = r.body.state;
          offset = (S.server_time || Date.now() / 1000) - Date.now() / 1000;
          render(S);
        }
        if (okMsg) { toast(okMsg, "ok"); }
      } else {
        var msg = (r.body && r.body.errors && r.body.errors.join("；")) || "保存失败";
        if (statusEl) { setBanner($(statusEl), "err", msg); }
        toast(msg, "err");
        loadState();   // 回退到后端真实值
      }
      return r;
    }).catch(function () {
      if (statusEl) { setBanner($(statusEl), "err", "保存失败：连接中断"); }
      toast("保存失败：连接中断", "err");
    });
  }

  function nextWall(target, btn) {
    var path = target === "desktop"
      ? "/api/wallpaper/desktop/next" : "/api/wallpaper/lock/next";
    busy(btn, true);
    api(path, { method: "POST", body: {} }).then(function (r) {
      var b = r.body || {};
      if (b.ok) { toast((target === "desktop" ? "桌面" : "锁屏") + " → " + b.name, "ok"); }
      else if (b.busy) { toast("切换进行中，请稍候", "err"); }
      else if (b.empty) { toast("图库为空：请先添加图片或更新图库", "err"); }
      else { toast(b.error || "切换失败", "err"); }
      return loadState();
    }).catch(function () {
      toast("切换失败：连接中断", "err");
    }).then(function () { busy(btn, false); });
  }

  // 点击「立即更新」后，等不到任何一次状态更新的兜底时限。
  // 判据是「有没有拿到过状态」，不是「更新跑了多久」——图库更新可能要几分钟，
  // 按时长解除会把正在更新时的 loading 弄丢。
  var UPDATE_STATE_TIMEOUT_MS = 15000;

  function doUpdate(btn) {
    busy(btn, true);           // 点击即反馈，不等状态刷新
    var seen = updateBusySeq;
    var watchdog = setTimeout(function () {
      // 状态一直没刷新成功（服务无响应、连接异常、页面渲染出错）时，不能把按钮
      // 永久卡在 loading：解除并说明，让用户还能重试或刷新页面。
      if (updateBusySeq === seen) {
        busy(btn, false);
        toast("未能确认更新状态，请刷新页面查看", "err");
      }
    }, UPDATE_STATE_TIMEOUT_MS);
    api("/api/gallery/update", { method: "POST", body: {} }).then(function (r) {
      var b = r.body || {};
      if (b.ok) { toast("已开始图库更新", "ok"); }
      else if (b.busy) { toast("更新已在进行中", "err"); }
      else { toast(b.message || "无法开始更新", "err"); }
      // 刷新后由 render() 按后端 update.running 决定按钮是否禁用：
      // 这里不能再无条件解除禁用，否则会把正在更新时的 loading 状态覆盖掉。
      return loadState();
    }).catch(function () {
      // 连接中断时不会有状态刷新，必须自己解除禁用，否则按钮会一直卡住
      clearTimeout(watchdog);
      toast("更新失败：连接中断", "err");
      busy(btn, false);
    });
  }

  // ---------------------------------------------------------------- 图库

  var gallery = { source: "all", keyword: "", offset: 0, total: 0 };

  function loadGallery() {
    var q = "/api/gallery?offset=" + gallery.offset + "&limit=" + PAGE_SIZE +
            "&source=" + encodeURIComponent(gallery.source) +
            "&keyword=" + encodeURIComponent(gallery.keyword);
    return api(q).then(function (r) {
      if (r.status !== 200) { return; }
      renderGallery(r.body);
    });
  }

  function renderGallery(data) {
    gallery.total = data.total;
    var grid = $("galleryGrid");
    grid.innerHTML = "";
    data.items.forEach(function (it) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "thumb";
      b.title = it.name;
      var img = document.createElement("img");
      img.loading = "lazy";
      img.decoding = "async";
      img.alt = it.name;
      img.src = imgUrl(it.id, 360);
      var tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = it.source === "pjsk" ? "pjsk" : "自定义";
      var nm = document.createElement("span");
      nm.className = "nm";
      nm.textContent = it.name;
      b.appendChild(img); b.appendChild(tag); b.appendChild(nm);
      b.addEventListener("click", function () { openLightbox(it); });
      grid.appendChild(b);
    });

    var isEmpty = data.total === 0;
    setBanner($("galleryEmpty"), isEmpty ? "warn" : "",
      isEmpty ? (gallery.keyword ? "没有匹配的图片。"
                                 : "图库为空：可拖动图片到上方区域添加，或执行图库更新。") : "");

    var from = data.total ? gallery.offset + 1 : 0;
    var to = Math.min(gallery.offset + data.limit, data.total);
    $("gallerySummary").textContent =
      "共 " + data.total + " 张（pjsk " + data.counts.pjsk +
      " / 自定义 " + data.counts.custom + "），显示 " + from + "–" + to;
    $("pageInfo").textContent = "第 " + (Math.floor(gallery.offset / PAGE_SIZE) + 1) +
      " / " + Math.max(1, Math.ceil(data.total / PAGE_SIZE)) + " 页";
    $("pagePrev").disabled = gallery.offset <= 0;
    $("pageNext").disabled = gallery.offset + PAGE_SIZE >= data.total;
  }

  function openLightbox(item) {
    $("lightboxTitle").textContent = item.name + "（" + item.source + "）";
    $("lightboxImg").src = imgUrl(item.id, 1024);
    $("lightbox").classList.remove("hidden");
  }

  function uploadFiles(files) {
    if (!files || !files.length) { return; }
    var fd = new FormData();
    for (var i = 0; i < files.length; i++) { fd.append("file", files[i], files[i].name); }
    toast("正在上传 " + files.length + " 个文件…");
    api("/api/gallery/custom", { method: "POST", form: fd }).then(function (r) {
      var b = r.body || {};
      if (b.imported && b.imported.length) {
        toast("已导入 " + b.imported.length + " 张", "ok");
      }
      if (b.failed && b.failed.length) {
        toast("失败 " + b.failed.length + " 个：" +
              b.failed.map(function (f) { return f.name + "(" + f.error + ")"; }).join("，"),
              "err");
      }
      if (r.status !== 200 && !(b.imported && b.imported.length)) {
        toast(typeof b.error === "string" ? b.error : "上传失败", "err");
      }
      return loadGallery().then(loadState);
    }).catch(function () { toast("上传失败：连接中断", "err"); });
  }

  // ---------------------------------------------------------------- 绑定

  function bindTabs() {
    var buttons = document.querySelectorAll(".tabs button");
    function activate(view) {
      if (!TAB_TITLES[view]) { view = "overview"; }
      Array.prototype.forEach.call(buttons, function (b) {
        b.classList.toggle("active", b.dataset.view === view);
      });
      Array.prototype.forEach.call(document.querySelectorAll(".view"), function (v) {
        v.classList.toggle("active", v.id === "view-" + view);
      });
      if (view === "gallery") { loadGallery(); }
      if (location.hash !== "#" + view) {
        history.replaceState(null, "", "#" + view);
      }
    }
    Array.prototype.forEach.call(buttons, function (b) {
      b.addEventListener("click", function () { activate(b.dataset.view); });
    });
    var idx = buttons.length;
    for (var i = 0; i < idx; i++) {
      (function (i) {
        buttons[i].addEventListener("keydown", function (e) {
          var step = e.key === "ArrowRight" ? 1 : (e.key === "ArrowLeft" ? -1 : 0);
          if (!step) { return; }
          e.preventDefault();
          var n = (i + step + idx) % idx;
          buttons[n].focus();
          activate(buttons[n].dataset.view);
        });
      })(i);
    }
    activate((location.hash || "").replace("#", "") || "overview");
  }

  function bindAll() {
    // 概览：启停 / 间隔 / 下一张
    $("deskEnabled").addEventListener("change", function (e) {
      patch({ desktop: { enabled: e.target.checked } }, null, "桌面轮换已" +
        (e.target.checked ? "启用" : "停用"));
    });
    $("lockEnabled").addEventListener("change", function (e) {
      patch({ lock: { enabled: e.target.checked } }, null, "锁屏轮换已" +
        (e.target.checked ? "启用" : "停用"));
    });
    $("deskInterval").addEventListener("change", function (e) {
      patch({ desktop: { interval: parseInt(e.target.value, 10) } }, null, "桌面间隔已保存");
    });
    $("lockInterval").addEventListener("change", function (e) {
      patch({ lock: { interval: parseInt(e.target.value, 10) } }, null, "锁屏间隔已保存");
    });
    $("deskNext").addEventListener("click", function () { nextWall("desktop", this); });
    $("lockNext").addEventListener("click", function () { nextWall("lock", this); });

    // 概览：目录 / 更新 / 日志
    $("openPjsk").addEventListener("click", function () { openFolder("pjsk", this); });
    $("openCustom").addEventListener("click", function () { openFolder("custom", this); });
    $("btnUpdate").addEventListener("click", function () { doUpdate(this); });
    $("btnUpdate2").addEventListener("click", function () { doUpdate(this); });
    $("btnLog").addEventListener("click", loadLog);
    $("btnCopyRepo").addEventListener("click", function () {
      copyText(REPO_URL, "已复制项目地址");
    });
    $("logClose").addEventListener("click", function () {
      $("logModal").classList.add("hidden");
    });

    // 关闭确认弹窗（宿主在用户点窗口关闭按钮时唤起）
    $("closeCancel").addEventListener("click", hideCloseDialog);
    $("closeConfirm").addEventListener("click", confirmClose);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !$("closeModal").classList.contains("hidden")) {
        e.preventDefault();
        hideCloseDialog();
      }
    });

    // 图库
    $("srcSeg").addEventListener("click", function (e) {
      var b = e.target.closest("button[data-src]");
      if (!b) { return; }
      Array.prototype.forEach.call(this.querySelectorAll("button"), function (x) {
        x.classList.toggle("active", x === b);
      });
      gallery.source = b.dataset.src;
      gallery.offset = 0;
      loadGallery();
    });
    var searchTimer = null;
    $("gallerySearch").addEventListener("input", function (e) {
      clearTimeout(searchTimer);
      var v = e.target.value;
      searchTimer = setTimeout(function () {
        gallery.keyword = v; gallery.offset = 0; loadGallery();
      }, 250);
    });
    $("pagePrev").addEventListener("click", function () {
      gallery.offset = Math.max(0, gallery.offset - PAGE_SIZE); loadGallery();
    });
    $("pageNext").addEventListener("click", function () {
      gallery.offset += PAGE_SIZE; loadGallery();
    });
    $("btnAddImages").addEventListener("click", function () { $("fileInput").click(); });
    $("fileInput").addEventListener("change", function (e) {
      uploadFiles(e.target.files);
      e.target.value = "";
    });

    var dz = $("dropzone");
    ["dragenter", "dragover"].forEach(function (ev) {
      dz.addEventListener(ev, function (e) {
        e.preventDefault(); dz.classList.add("over");
      });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      dz.addEventListener(ev, function (e) {
        e.preventDefault(); dz.classList.remove("over");
      });
    });
    dz.addEventListener("drop", function (e) {
      if (e.dataTransfer && e.dataTransfer.files) { uploadFiles(e.dataTransfer.files); }
    });
    dz.addEventListener("click", function () { $("fileInput").click(); });

    // 设置：轮换
    $("setDeskInterval").addEventListener("change", function (e) {
      patch({ desktop: { interval: parseInt(e.target.value, 10) } },
            "rotateStatus", "桌面间隔已保存");
    });
    $("setLockInterval").addEventListener("change", function (e) {
      patch({ lock: { interval: parseInt(e.target.value, 10) } },
            "rotateStatus", "锁屏间隔已保存");
    });
    $("setDeskEnabled").addEventListener("change", function (e) {
      patch({ desktop: { enabled: e.target.checked } }, "rotateStatus", "已保存");
    });
    $("setLockEnabled").addEventListener("change", function (e) {
      patch({ lock: { enabled: e.target.checked } }, "rotateStatus", "已保存");
    });

    // 设置：图库来源 / 上限
    $("setUsePjsk").addEventListener("change", function (e) {
      patch({ use_pjsk: e.target.checked }, "sourceStatus", "已保存");
    });
    $("setUseCustom").addEventListener("change", function (e) {
      patch({ use_custom: e.target.checked }, "sourceStatus", "已保存");
    });
    $("setCap").addEventListener("change", function (e) {
      patch({ cap_pjsk: parseInt(e.target.value, 10) }, "sourceStatus",
            "上限已保存（下次更新生效）");
    });

    // 设置：更新
    $("setUpIntervalOn").addEventListener("change", function (e) {
      patch({ update: { interval_enabled: e.target.checked } },
            "updateStatus", "已保存");
    });
    [["setUpInterval", "interval"], ["setUpCount", "count"],
     ["setUpRarity", "min_rarity"]].forEach(function (p) {
      $(p[0]).addEventListener("change", function (e) {
        var body = { update: {} };
        body.update[p[1]] = parseInt(e.target.value, 10);
        patch(body, "updateStatus", "已保存");
      });
    });
    $("setUpChara").addEventListener("change", function (e) {
      patch({ update: { chara: e.target.value } }, "updateStatus", "已保存");
    });
    $("setUpUnit").addEventListener("change", function (e) {
      patch({ update: { unit: e.target.value } }, "updateStatus", "已保存");
    });

    // 设置：系统
    $("setAutostart").addEventListener("change", function (e) {
      var want = e.target.checked;
      api("/api/system/autostart", { method: "POST", body: { on: want } })
        .then(function (r) {
          var b = r.body || {};
          if (b.ok) { setBanner($("sysStatus"), "ok", "开机自启已" +
                      (b.autostart ? "开启" : "关闭")); toast("已保存", "ok"); }
          else { setBanner($("sysStatus"), "err", b.error || "设置失败"); loadState(); }
        }).catch(function () { setBanner($("sysStatus"), "err", "连接中断"); });
    });
    $("btnShortcuts").addEventListener("click", function () {
      var btn = this; busy(btn, true);
      api("/api/system/create-shortcuts", { method: "POST", body: {} })
        .then(function (r) {
          var b = r.body || {};
          if (b.ok) { setBanner($("sysStatus"), "ok", "快捷方式已创建/修复"); toast("完成", "ok"); }
          else { setBanner($("sysStatus"), "err", "创建失败：" +
                  ((b.failed || []).join("，") || b.error || "未知错误")); }
        }).catch(function () { setBanner($("sysStatus"), "err", "连接中断"); })
        .then(function () { busy(btn, false); });
    });

    // 设置：配置文件导入 / 导出
    bindConfigTransfer();
    bindGlobalDrop();

    // 刷新 / 弹层
    $("btnRefresh").addEventListener("click", function () {
      loadState(); loadGallery(); toast("已刷新");
    });
    $("lightboxClose").addEventListener("click", function () {
      $("lightbox").classList.add("hidden");
    });
    $("lightbox").addEventListener("click", function (e) {
      if (e.target === this) { this.classList.add("hidden"); }
    });
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") { return; }
      $("lightbox").classList.add("hidden");
      $("logModal").classList.add("hidden");
    });
  }

  function openFolder(which, btn) {
    busy(btn, true);
    api("/api/system/open-folder", { method: "POST", body: { which: which } })
      .then(function (r) {
        if (!r.ok) { toast((r.body && r.body.error) || "打开失败", "err"); }
      }).catch(function () { toast("连接中断", "err"); })
      .then(function () { busy(btn, false); });
  }

  // ------------------------------------------------------- 设置导入 / 导出

  var FIELD_LABELS = {
    desktop: "桌面轮换", lock: "锁屏轮换", use_pjsk: "pjsk 图库开关",
    use_custom: "自定义图库开关", cap_pjsk: "pjsk 容量上限", update: "图库更新设置"
  };
  var MAX_CONFIG_BYTES = 256 * 1024;

  function exportConfig(btn) {
    busy(btn, true);
    api("/api/config/export").then(function (r) {
      if (r.status !== 200 || !r.body || !r.body.config) {
        toast("导出失败", "err");
        return;
      }
      var text = JSON.stringify(r.body, null, 2);
      var blob = new Blob([text], { type: "application/json" });
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "pjsk-wallpaper-settings.json";
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
      toast("设置已导出", "ok");
    }).catch(function () {
      toast("导出失败：连接中断", "err");
    }).then(function () { busy(btn, false); });
  }

  function importConfigFile(file) {
    var banner = $("configImportStatus");
    if (!file) { return; }
    if (file.size > MAX_CONFIG_BYTES) {
      setBanner(banner, "err", "文件过大（应小于 256KB）：" + file.name);
      return;
    }
    setBanner(banner, "", "正在导入 " + file.name + " …");
    var reader = new FileReader();
    reader.onload = function () {
      var payload;
      try {
        payload = JSON.parse(String(reader.result));
      } catch (err) {
        setBanner(banner, "err", "不是合法的 JSON 文件：" + err.message);
        toast("导入失败：JSON 格式错误", "err");
        return;
      }
      api("/api/config/import", { method: "POST", body: payload })
        .then(function (r) {
          var b = r.body || {};
          if (r.ok && b.ok) {
            var names = (b.applied || []).map(function (k) {
              return FIELD_LABELS[k] || k;
            });
            var msg = "已导入 " + names.length + " 项：" +
                      (names.join("、") || "（无可应用项）");
            if (b.warnings && b.warnings.length) {
              msg += "；" + b.warnings.join("；");
            }
            setBanner(banner, "ok", msg);
            toast("设置已导入", "ok");
            if (b.state) {
              S = b.state;
              offset = (S.server_time || Date.now() / 1000) - Date.now() / 1000;
              render(S);
            } else { loadState(); }
          } else {
            var errs = (b.errors || []).join("；") || "导入失败";
            var warn = (b.warnings && b.warnings.length)
              ? "（" + b.warnings.join("；") + "）" : "";
            setBanner(banner, "err", "导入失败：" + errs + warn);
            toast("导入失败", "err");
            loadState();          // 回退显示后端真实值
          }
        }).catch(function () {
          setBanner(banner, "err", "导入失败：连接中断");
        });
    };
    reader.onerror = function () {
      setBanner(banner, "err", "读取文件失败");
    };
    reader.readAsText(file, "utf-8");
  }

  function bindConfigTransfer() {
    $("btnExportConfig").addEventListener("click", function () { exportConfig(this); });
    $("btnImportConfig").addEventListener("click", function () {
      $("configFileInput").click();
    });
    $("configFileInput").addEventListener("change", function (e) {
      importConfigFile(e.target.files && e.target.files[0]);
      e.target.value = "";
    });
    var dz = $("configDropzone");
    ["dragenter", "dragover"].forEach(function (ev) {
      dz.addEventListener(ev, function (e) {
        e.preventDefault(); dz.classList.add("over");
      });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      dz.addEventListener(ev, function (e) {
        e.preventDefault(); dz.classList.remove("over");
      });
    });
    dz.addEventListener("drop", function (e) {
      var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      importConfigFile(f);
    });
    dz.addEventListener("click", function () { $("configFileInput").click(); });
    dz.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault(); $("configFileInput").click();
      }
    });
  }

  function bindGlobalDrop() {
    // 应用窗口里拖放文件时，避免浏览器直接打开/导航；json 视为设置文件
    function inZone(node, selector) {
      return !!(node && node.closest && node.closest(selector));
    }
    document.addEventListener("dragover", function (e) { e.preventDefault(); });
    document.addEventListener("drop", function (e) {
      if (inZone(e.target, "#dropzone") || inZone(e.target, "#configDropzone")) {
        return;                       // 交给各自的拖放区域处理
      }
      e.preventDefault();
      var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (!f) { return; }
      if (/\.json$/i.test(f.name)) {
        var tab = document.querySelector('.tabs button[data-view="settings"]');
        if (tab) { tab.click(); }
        importConfigFile(f);
      } else {
        toast("图片请拖到「图库」页的上传区域", "err");
      }
    });
  }

  function loadLog() {
    $("logModal").classList.remove("hidden");
    $("logBody").textContent = "加载中…";
    api("/api/gallery/update/log?lines=300").then(function (r) {
      var lines = (r.body && r.body.lines) || [];
      $("logBody").textContent = lines.length ? lines.join("\n") : "暂无更新日志。";
      $("logBody").scrollTop = $("logBody").scrollHeight;
    }).catch(function () { $("logBody").textContent = "读取失败：连接中断"; });
  }

  // ---------------------------------------------------------------- 关闭确认

  // 用户点窗口关闭按钮、且没有保存过选择时，宿主（launcher.pyw 的内嵌 WebView）
  // 会经状态推送置一个 close_prompt 标志，本页在收到状态时弹出确认框；
  // 用户的选择再经本机 HTTP 接口回报给持有窗口的进程执行。
  // 用浏览器打开本页时没有可控制的窗口，接口会如实返回不支持。

  function closeDialog() { return $("closeModal"); }

  function hideCloseDialog() {
    closeDialog().classList.add("hidden");
    setBanner($("closeStatus"), null, "");
    $("closeConfirm").disabled = false;
  }

  function confirmClose() {
    var picked = document.querySelector('input[name="closeAction"]:checked');
    if (!picked) { return; }
    var btn = $("closeConfirm");
    btn.disabled = true;
    setBanner($("closeStatus"), null, "");
    api("/api/window/close", {
      method: "POST",
      body: { action: picked.value, remember: $("closeRemember").checked }
    }).then(function (r) {
      btn.disabled = false;
      var b = r.body || {};
      if (r.status === 200 && b.ok) { hideCloseDialog(); return; }
      setBanner($("closeStatus"), "err",
                typeof b.error === "string" ? b.error : "操作失败，请重试。");
    }).catch(function () {
      btn.disabled = false;
      setBanner($("closeStatus"), "err", "操作失败：连接中断");
    });
  }

  function promptClose() {
    var dlg = closeDialog();
    if (!dlg || !dlg.classList.contains("hidden")) { return true; }
    $("closeRemember").checked = false;
    var dflt = document.querySelector('input[name="closeAction"][value="tray"]');
    if (dflt) { dflt.checked = true; }
    setBanner($("closeStatus"), null, "");
    $("closeConfirm").disabled = false;
    dlg.classList.remove("hidden");
    $("closeConfirm").focus();
    return true;
  }

  // ---------------------------------------------------------------- 启动

  function boot() {
    bindTabs();
    bindAll();
    // loadState 自身会用长轮询接力，无需再挂定时器
    loadState().then(loadGallery);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
