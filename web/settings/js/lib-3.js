  function refreshModeBadges(){ syncModeUI(); }

  // 首屏直出（v3.08）：用上次缓存的 schema+配置先画一遍表单，接口回来再覆盖。
  // 单群模式不缓存——每个群的值都不一样，直出反而会显示别的群的数。
  function collectSettings(){
    var out = {};
    Object.keys(schemaCache).forEach(function(k){
      var f = schemaCache[k] || {};
      var t = f.type || "string";
      var node = document.querySelector("#settings [data-k='" + k.replace(/'/g, "\\'") + "']");
      if(!node) return;
      // 单群模式下输入框空着 = 跟随全局。这里必须显式提交 null：直接 return 的话
      // 后端 merge=1 会把旧覆盖并回来，清空某项永远清不掉，界面还提示「已保存」
      // （审查 bug）。null 在后端表示「把这个覆盖项删掉」。
      // v3.70（用户 m21533 第 2 条）：私聊特例项（触发概率 100%）只在界面上显示默认值，
      // 没手动改过就不提交——不往 overrides 里写，会话卡才还是「全局配置」。
      if(scopeSid && DM_SPECIAL[k] && dmDefaultsCache[k] != null &&
         node.getAttribute("data-dmdef") === "1" &&
         !Object.prototype.hasOwnProperty.call(overrideCache, k)){
        var dnv = String(node.value == null ? "" : node.value).trim();
        if(dnv === String(dmDefaultsCache[k])) return;
      }
      if(scopeSid && t !== "switch" && t !== "bool" && t !== "boolean" &&
         (node.value == null || String(node.value).trim() === "")){ out[k] = null; return; }
      if(t === "switch" || t === "boolean" || t === "bool") out[k] = !!node.checked;
      else if(t === "integer"){
        var iv = parseInt(node.value, 10) || 0;
        // 用户 m10202：界面上按分钟填的键，存盘换回秒。
        var mul = parseInt(node.getAttribute("data-mul"), 10) || 0;
        out[k] = mul ? iv * mul : iv;
      }
      else if(t === "float" || t === "number") out[k] = parseFloat(node.value) || 0;
      else if(t === "multi_select" || t === "list" || t === "array"){
        var arr = [];
        node.querySelectorAll(".chip").forEach(function(c){
          arr.push(c.textContent.replace(/×$/, "").trim());
        });
        out[k] = arr;
      } else out[k] = node.value;
    });
    // v3.09：表单里已经不再显示 enabled 与 takeover_default_chat，但配置里它们还在。
    // 提交时不能少这两项——PUT /config 收到一份缺键的对象等于把它们清掉，
    // enabled 会回落到默认 true，用户刚在首页关掉的插件又被打开。
    // 所以没渲染出来的键一律原样带上上次读到的值。
    Object.keys(schemaCache).forEach(function(k){
      if(Object.prototype.hasOwnProperty.call(out, k)) return;
      if(!configCache || !Object.prototype.hasOwnProperty.call(configCache, k)) return;
      out[k] = configCache[k];
    });
    // 合并后的语义：停不停内置 default-chat 跟着「接管群聊触发判定」走。
    // 单独留一个开关只会产生「自己决策但不禁用 default-chat」的双回复坏状态。
    if(Object.prototype.hasOwnProperty.call(out, "decision_enabled")){
      out.takeover_default_chat = !!out.decision_enabled;
    }
    // v3.13（用户 m39983）：window_enabled 恒为开。后端 _LOCKED_ON 已经忽略任何关闭意图，
    // 这里再补一次，免得旧配置里残留的 false 被原样提交上去、日志里看着像关掉了。
    if(Object.prototype.hasOwnProperty.call(out, "window_enabled")) out.window_enabled = true;
    // v3.67（用户 m20117 第 2 条）：消息日志 / 会话统计的保留天数有硬下限 56 天（自适应阈值要长期历史）。
    // 2026-11-18（用户 m13966）：两者合成一行，只剩 decisions_keep_days 一个键（stats_keep_days 后端当别名读）。
    // 填 0 / 30 一律按 56 存，输入框也顺手改成 56——别让页面留着一个后端不接受的数。
    ["decisions_keep_days"].forEach(function(k){
      if(!Object.prototype.hasOwnProperty.call(out, k)) return;
      var n = parseInt(out[k], 10);
      if(!isFinite(n) || n < 56){
        out[k] = 56;
        var el = document.querySelector("#settings [data-k='" + k + "']");
        if(el) el.value = "56";
      }
    });
    return out;
  }

  // 配置体检：把互相冲突的组合直接摆在表单上面，只提示不改值
  function checkConfig(){
    var url = API + "/config-check" + (scopeSid ? "?sid=" + encodeURIComponent(scopeSid) : "");
    get(url).then(function(d){
      var box = document.getElementById("check-bar");
      var items = (d && d.items) || [];
      if(!items.length){ box.innerHTML = ""; return; }
      box.innerHTML = items.map(function(x){
        var lvl = x.level === "error" ? "error" : "warn";
        var label = lvl === "error" ? "不通" : "注意";
        return "<div class='chk " + lvl + "' data-k='" + esc(x.key) + "'>" +
               "<span class='lvl'><i>" + label + "</i></span>" +
               "<div class='txt'>" + esc(x.text) + "</div></div>";
      }).join("");
    }).catch(function(){
      var box = document.getElementById("check-bar");
      if(box) box.innerHTML = "";
    });
  }

  // v3.30（用户 m47796 第 3 条）：节奏页那张「近 7 天消息分布」柱状图删掉了——
  // 它和时段曲线的底图是同一份 48 格数据，重复占版面。这里只负责把分布取回来喂曲线。
  function loadHours(){
    var url = API + "/hour-histogram?days=7" + (scopeSid ? "&sid=" + encodeURIComponent(scopeSid) : "");
    get(url).then(function(d){
      // 带 sid 就是本群自己的分布，不带就是全部会话的（v3.30 第 5 条：
      // 单群默认走本群统计，切到全局作用域时看全体）
      bandSlots = (d && d.slots && d.slots.length === BAND_SLOTS) ? d.slots : null;
      // v3.75（用户本轮第 2 条）：bot 回复的分布跟着一起回来，曲线页叠第二条线用
      bandBotSlots = (d && d.bot_slots && d.bot_slots.length === BAND_SLOTS) ? d.bot_slots : null;
      refreshBandBox();
    }).catch(function(){
      bandSlots = null;
      bandBotSlots = null;
    });
  }

  // 用户 m08902 第 1 条：「休息时段·必回」默认是开的，线上却是关的。
  // 根因是 /config 只下发「存过」的键：新加的开关配置里还没有，页面上就渲染成没勾；
  // 此后随便改任何一项，collectSettings 都会把页面上所有开关的勾选状态整份提交，
  // 于是它被写成 false。这里先按 schema 补上默认值，让渲染和提交看到同一个值。
  function fillSchemaDefaults(cfg){
    var out = {};
    Object.keys(cfg || {}).forEach(function(k){ out[k] = cfg[k]; });
    Object.keys(schemaCache || {}).forEach(function(k){
      if(Object.prototype.hasOwnProperty.call(out, k)) return;
      var f = schemaCache[k] || {};
      if(!Object.prototype.hasOwnProperty.call(f, "default")) return;
      var dv = f["default"];
      try { dv = JSON.parse(JSON.stringify(dv)); } catch(e){}
      out[k] = dv;
    });
    return out;
  }

  var overviewInFlight = null;
  var overviewCache = null;
  var overviewAt = 0;
  function getOverview(force){
    if(!force && overviewCache && Date.now() - overviewAt < 5000) return Promise.resolve(overviewCache);
    if(overviewInFlight) return overviewInFlight;
    overviewInFlight = get(API + "/overview?light=1").then(function(d){
      overviewCache = d || {};
      overviewAt = Date.now();
      return overviewCache;
    }).catch(function(){ return {}; }).then(function(d){
      overviewInFlight = null;
      return d;
    });
    return overviewInFlight;
  }
  function loadScopes(){
    var p1 = get(API + "/context-sessions").catch(function(){ return {}; });
    var p2 = getOverview(true);
    Promise.all([p1, p2]).then(function(res){
      var d = res[0] || {}, ov = res[1] || {};
      aliasCache = d.aliases || {};
      var list = [];
      (d.items || []).forEach(function(x){ if(x && x.sid) list.push(x.sid); });
      (ov.sessions || []).forEach(function(s){ if(s && s.sid && list.indexOf(s.sid) < 0) list.push(s.sid); });
      sessCache = list;
      cachePut("scopes", {list: list, aliases: aliasCache});
      renderScopeOptions(list);
    });
  }

  // 卡片上的名字：有别名就只写别名（群里都这么叫），没有就写「群 1145141974 / 私聊 123」。
  function scopeCardName(sid){
    if(aliasCache[sid]) return aliasCache[sid];
    return sidLabel(sid);
  }
  // v3.68（用户 m20632 第 1 条）：名片那块删掉时把取色函数一起带走了，作用范围卡片还在用它——补回来。
  var CACHE_AV_COLORS = ["#8b5cf6", "#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#06b6d4"];
  function cacheAvatarColor(sid){
    var s = String(sid || ""), h = 0;
    for(var i = 0; i < s.length; i++){ h = (h * 31 + s.charCodeAt(i)) % 9973; }
    return CACHE_AV_COLORS[h % CACHE_AV_COLORS.length];
  }
  // 卡片头像：跟缓存名片同一套取号规则（gm 走群头像、dm 走 QQ 头像），取不到号就只显示首字。
  function scopeAvatarHtml(sid){
    var ch = (scopeCardName(sid) || "?").slice(0, 1) || "?";
    var m = /(?:^|[_:])(gm|dm)[:_]?(\d+)$/.exec(String(sid || ""));
    var img = "";
    if(m){
      var url = (m[1] === "gm")
        ? "https://p.qlogo.cn/gh/" + m[2] + "/" + m[2] + "/100"
        : "https://q1.qlogo.cn/g?b=qq&nk=" + m[2] + "&s=100";
      img = "<img src='" + url + "' referrerpolicy='no-referrer' loading='lazy' alt=''" +
            " onerror='this.remove()'>";
    }
    return "<span class='sc-av' style='background:" + cacheAvatarColor(sid) + "'>" + esc(ch) + img + "</span>";
  }
  // 作用范围卡片条（v3.67 用户 m20117 第 6 条，替掉原来的下拉框）：
  // 最左一张「全局」（默认选中），后面每张一个会话；选中谁就编辑谁的设置。
  function renderScopeOptions(list){
    var box = document.getElementById("set-cards");
    if(Array.isArray(list)) sessCache = list.slice();
    var all = Array.isArray(sessCache) ? sessCache.slice() : [];
    // 当前正在改的会话即使不在列表里（刚建、或者列表还没拉回来），也得有一张卡，不然高亮会跳回全局
    if(scopeSid && all.indexOf(scopeSid) < 0) all.push(scopeSid);
    if(!box) return;
    var html = "<button type='button' class='scard all" + (scopeSid ? "" : " on") +
      "' data-sid='' title='" + esc("全局设置：所有会话的默认值") + "'>" +
      "<span class='sc-globe' aria-hidden='true'>◎</span><span class='sc-nm'>全局</span></button>";
    html += all.map(function(sid){
      return "<button type='button' class='scard" + (sid === scopeSid ? " on" : "") +
        "' data-sid='" + esc(sid) + "' title='" + esc(scopeCardName(sid) + "（" + sid + "）") + "'>" +
        scopeAvatarHtml(sid) + "<span class='sc-nm'>" + esc(scopeCardName(sid)) + "</span></button>";
    }).join("");
    box.innerHTML = html;
  }

  // v3.11（用户 m39492）：改完就存，不再手动点「保存并生效」。
  // 防抖 700ms 把连续调整（连续加外号、拖动数字）合并成一次提交——
  // 每提交一次都会热重载插件，逐项提交会把插件反复重启。
  var saveTimer = null, saving = false, saveAgain = false;
  var saveEditVersion = 0, saveScopeVersion = 0, saveAckVersion = -1;
  // 2026-11-12（用户 m09483）：恢复默认的摘要要等保存回包之后再写，
  // 否则会被「已自动保存 N 项覆盖」顶掉，用户只看到覆盖数、以为恢复默认没生效。
  var resetNote = "";
  function clearResetNoteSoon(){
    var rn = resetNote;
    setTimeout(function(){ if(resetNote === rn) resetNote = ""; }, 5000);
  }
  function settingsScopeChanged(){
    saveScopeVersion += 1;
    saveAgain = false;
    if(saveTimer){ clearTimeout(saveTimer); saveTimer = null; }
  }
  function flushPendingSave(){
    if(saveTimer){ clearTimeout(saveTimer); saveTimer = null; }
    if(!dirty) return true;
    // 补发是 keepalive 的异步请求，下面拿不到结果；失败时把 dirty 恢复回来，
    // 页面若还在（bfcache 回退 / 跳转被拦）还能再存一次，别显示成「已保存」。
    var flushSid = String(scopeSid || ""), flushScopeVersion = saveScopeVersion, flushEditVersion = saveEditVersion;
    var onFail = function(msg){
      // 旧作用域/草稿或已确认版本的失败，不能倒退当前页面的保存状态。
      if(flushScopeVersion !== saveScopeVersion || flushSid !== String(scopeSid || "") ||
         flushEditVersion !== saveEditVersion || flushEditVersion <= saveAckVersion) return;
      dirty = true;
      try{ var m = document.getElementById("set-msg"); if(m) m.textContent = msg; }catch(x){}
    };
    var onResp = function(r){
      if(r && !r.ok) onFail("补发保存失败 HTTP " + r.status);
    };
    try{
      if(scopeSid){
        var all = collectSettings(), vals = {};
        scopeKeys.forEach(function(k){ if(Object.prototype.hasOwnProperty.call(all, k)) vals[k] = all[k]; });
        fetch(API + "/overrides?sid=" + encodeURIComponent(scopeSid) + "&merge=1&values=" +
              encodeURIComponent(JSON.stringify(vals)),
              {method: "POST", credentials: "same-origin", keepalive: true})
          .then(onResp).catch(function(){ onFail("补发保存失败，网络断了"); });
      } else {
        fetch(CORE + "/config", {
          method: "PUT", credentials: "same-origin", keepalive: true,
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ config: collectSettings() })
        }).then(onResp).catch(function(){ onFail("补发保存失败，网络断了"); });
        // 配置落盘了但插件还没热重载：留个标记，首页打开时补一次 reload。
        try{ localStorage.setItem("mdp_need_reload", "1"); }catch(x){}
      }
      dirty = false;
      return true;
    }catch(e){
      return false;
    }
  }

  // v3.93（用户 m07570 图8）：本页恢复默认。
  // 键的范围 = 这一页 SET_GROUPS 里声明的 keys + 页面上真实渲染出来的 data-k，
  // 两边并集才不会漏掉 custom 块里的（曲线 time_bands、配额、提示词页那一堆）。
  function pageKeyList(){
    var keys = {}, pageId = curPage;
    Array.prototype.forEach.call(
      document.querySelectorAll("#settings [data-page='" + pageId + "'] [data-k]"),
      function(el){
        var k = el.getAttribute("data-k");
        if(k) keys[k] = 1;
      });
    SET_GROUPS.forEach(function(g){
      if((g.p || "basic") !== pageId) return;
      (g.keys || []).forEach(function(k){ keys[k] = 1; });
    });
    return Object.keys(keys);
  }
  function pageTitle(pageId){
    var t = pageId;
    PAGES.forEach(function(p){ if(p.id === pageId) t = p.t; });
    return t;
  }
  // 2026-11-12（用户 m09483）：恢复默认要按「用户眼前的值」判断有没有变，
  // 也要把默认值写进会话覆盖与缓存表的取值来源，否则重画后还是旧值。
  function pageValOf(k){
    var node = document.querySelector("#settings [data-k='" + String(k).replace(/'/g, "\\'") + "']");
    if(node){
      var t = (schemaCache[k] || {}).type || "string";
      if(t === "switch" || t === "boolean" || t === "bool") return !!node.checked;
      var v = String(node.value == null ? "" : node.value).trim();
      return v === "" ? null : v;
    }
    if(configCache && Object.prototype.hasOwnProperty.call(configCache, k)) return configCache[k];
    return null;
  }
  function pageValSame(a, b){
    var an = (a === null || a === undefined || a === ""), bn = (b === null || b === undefined || b === "");
    if(an || bn) return an && bn;
    if(typeof a === "boolean" || typeof b === "boolean") return !!a === !!b;
    var na = Number(a), nb = Number(b);
    if(isFinite(na) && isFinite(nb)) return na === nb;
    return String(a) === String(b);
  }
  var lastEditAt = 0;
  function waitQuiet(ms){
    return new Promise(function(res){
      (function tick(){
        var gap = Date.now() - lastEditAt;
        if(gap >= ms) return res();
        setTimeout(tick, Math.min(200, ms - gap));
      })();
    });
  }

  function saveOverrides(sid, all){
    var vals = {};
    scopeKeys.forEach(function(k){ if(Object.prototype.hasOwnProperty.call(all, k)) vals[k] = all[k]; });
    // merge=1 保留 session_enabled 等表单外覆盖；null 仍表示恢复跟随全局。
    var url = API + "/overrides?sid=" + encodeURIComponent(sid) + "&merge=1" +
              "&values=" + encodeURIComponent(JSON.stringify(vals));
    return fetch(url, {method: "POST", credentials: "same-origin"}).then(function(r){
      if(!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function(d){
      if(!d.ok) throw new Error(d.detail || "保存失败");
      return d;
    });
  }

  function blkFormOf(node){
    return (node && node.closest) ? node.closest(".blk-form") : null;
  }
  function closeBlkForm(form){
    if(!form) return;
    form.hidden = true;
    var n0 = form.querySelector(".ub-name"), q0 = form.querySelector(".ub-qq");
    if(n0) n0.value = "";
    if(q0) q0.value = "";
  }
  function closeBlkInput(inp){
    if(!inp) return;
    inp.value = "";
    inp.hidden = true;
    var add = inp.parentNode && inp.parentNode.querySelector(".chip-add");
    if(add) add.hidden = false;
  }
  function ublkPost(qs){
    // qs 可能以 ? 开头（老调用点），统一改成 & 拼在 sid 后面，别拼出第二个问号
    var tail = String(qs || "").replace(/^\?/, "");
    var url = API + "/user-blacklist?sid=" + encodeURIComponent(scopeSid || "") + (tail ? "&" + tail : "");
    return fetch(url, { method: "POST", credentials: "same-origin" })
      .then(function(r){ return r.json(); })
      .then(function(d){
        if(d && d.ok){
          ublkCache = { items: Array.isArray(d.items) ? d.items : [],
                        unblock_items: Array.isArray(d.unblock_items) ? d.unblock_items : [] };
          ublkPaint();
        }
        return d;
      });
  }
  function ublkCommit(){
    var frm = document.getElementById("ublk-form");
    if(!frm) return;
    var qq = ((frm.querySelector(".ub-qq") || {}).value || "").trim();
    var nm = ((frm.querySelector(".ub-name") || {}).value || "").trim();
    if(!/^\d{4,12}$/.test(qq)){ mdAlert("QQ 号要填数字（4–12 位）。", {title:"填错了"}); return; }
    ublkPost("?uid=" + encodeURIComponent(qq) + "&name=" + encodeURIComponent(nm) + "&action=add")
      .then(function(d){
        if(d && d.ok){ closeBlkForm(frm); }
        else { mdAlert("没加上：" + ((d && d.detail) || "接口没返回 ok")); }
      })
      .catch(function(err){ mdAlert("没加上：" + (err && err.message ? err.message : err)); });
  }
  function jevSliderPaint(box, lo, hi){
    box.setAttribute("data-lo", String(lo));
    box.setAttribute("data-hi", String(hi));
    var fill = box.querySelector(".jev-slider-fill");
    if(fill){ fill.style.left = lo + "%"; fill.style.right = (100 - hi) + "%"; }
    var kl = box.querySelector(".jev-slider-knob[data-h='lo']");
    var kh = box.querySelector(".jev-slider-knob[data-h='hi']");
    // 用户 m16691：两个把手改成半圆，贴着高亮段的两头往外鼓；到 0% / 100% 时用 clamp 卡住，别戳出轨道。
    if(kl) kl.style.left = "clamp(var(--kw)," + lo + "%,100%)";
    if(kh) kh.style.left = "clamp(0px," + hi + "%,calc(100% - var(--kw)))";
    var tag = box.querySelector(".jev-slider-tag");
    if(tag){
      tag.textContent = lo + "%~" + hi + "%";
      tag.style.left = Math.min(92, Math.max(8, (lo + hi) / 2)) + "%";
    }
  }
  var BAND_SLOTS = 48;
  var bandSlots = null;   // 近 7 天 30 分钟分布，迷你图与编辑器都拿它当底图

  function slotTime(i){
    var h = Math.floor(i / 2), m = (i % 2) ? 30 : 0;
    return (h < 10 ? "0" : "") + h + ":" + (m ? "30" : "00");
  }
  function bandProb(){
    var p = Number(configCache && configCache.trigger_probability);
    if(!isFinite(p) || p <= 0) p = 10;
    return Math.max(1, Math.min(100, Math.round(p)));
  }
  // v3.81（用户 m02645 图3）：段上的「按星期分」目标（周一..周日，0 = 这天没单独设）。
  // 坏值、空数组一律当「没有这一份」，前端不猜、后端那边也有同样一份解析（_band_day_target）。
  var WEEK_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
  function parseWeekList(raw, high){
    if(!Array.isArray(raw) || raw.length !== 7) return null;
    var out = [], any = false;
    for(var i = 0; i < 7; i++){
      var n = Math.round(Number(raw[i]));
      if(!isFinite(n) || n <= 0){ out.push(0); continue; }
      out.push(Math.min(high, n));
      any = true;
    }
    return any ? out : null;
  }
  function parseBands(txt){
    var arr = [];
    try{ arr = JSON.parse(txt || "[]"); }catch(e){ arr = []; }
    if(!Array.isArray(arr) || !arr.length) return [{ s: 0, e: BAND_SLOTS, v: bandProb() }];
    var out = [];
    arr.forEach(function(it){
      if(!it) return;
      var s = Math.max(0, Math.min(BAND_SLOTS - 1, Math.round(Number(it.s) || 0)));
      var e = Math.max(s + 1, Math.min(BAND_SLOTS, Math.round(Number(it.e) || (s + 1))));
      var v = Math.max(0, Math.min(100, Math.round(Number(it.v) || 0)));
      // v3.71（用户第 7 条）：动态模式的目标值存在段上（tr = 每天目标回复次数，
      // tp = 每条消息的目标概率），解析时原样带上，否则切一次作用范围就把目标抹掉。
      // v3.73（用户 m24640 第 5 条）：休息时段是段上的月亮标记（rest: true），
      // 解析时必须原样带回来——漏掉它，重载一次设置页月亮就全灭了，下次保存还会把配置洗掉。
      var seg = { s: s, e: e, v: v };
      var tr = Math.round(Number(it.tr) || 0), tp = Math.round(Number(it.tp) || 0);
      if(it.rest) seg.rest = true;
      if(tr > 0) seg.tr = Math.min(9999, tr);
      if(tp > 0) seg.tp = Math.max(1, Math.min(100, tp));
      // v3.81（用户 m02645 图3）：开了「使用星期数据」后目标按星期分 7 份（trw / tpw），
      // 跟 tr / tp 一样原样带回来——漏掉它，切一刀作用范围就只剩统一值了。
      var trw = parseWeekList(it.trw, 9999), tpw = parseWeekList(it.tpw, 100);
      if(trw) seg.trw = trw;
      if(tpw) seg.tpw = tpw;
      out.push(seg);
    });
    out.sort(function(a, b){ return a.s - b.s; });
    var fixed = [];
    out.forEach(function(b){
      var start = fixed.length ? fixed[fixed.length - 1].e : 0;
      if(b.e <= start) return;
      var seg2 = { s: start, e: Math.min(BAND_SLOTS, b.e), v: b.v };
      if(b.rest) seg2.rest = true;
      if(b.tr) seg2.tr = b.tr;
      if(b.tp) seg2.tp = b.tp;
      if(b.trw) seg2.trw = b.trw.slice();
      if(b.tpw) seg2.tpw = b.tpw.slice();
      fixed.push(seg2);
    });
    if(!fixed.length) return [{ s: 0, e: BAND_SLOTS, v: bandProb() }];
    if(fixed[fixed.length - 1].e < BAND_SLOTS) fixed[fixed.length - 1].e = BAND_SLOTS;
    return fixed;
  }
  function bandsRaw(){
    var el = document.getElementById("bands-val");
    if(el) return String(el.value || "");
    return String((configCache && configCache.time_bands) || "");
  }
  function bandsSummary(bands){
    if(bands.length <= 1) return "整段 00:00–24:00 · " + bands[0].v + "%";
    var lo = 100, hi = 0;
    bands.forEach(function(b){ lo = Math.min(lo, b.v); hi = Math.max(hi, b.v); });
    return bands.length + " 段 · " + (lo === hi ? lo + "%" : lo + "–" + hi + "%");
  }
  function bandsYMax(bands){
    var m = 0;
    bands.forEach(function(b){ if(b.v > m) m = b.v; });
    m = Math.max(20, Math.ceil(m * 1.45));
    return Math.min(100, Math.round(m / 5) * 5);
  }
  // v3.69（用户 m21003 第 6 条）：每段一个稳定的淡色——用户要的「随机淡一点的颜色」，
  // 但重画不能跳色，所以按段序取调色板，不真随机。
  var BAND_TINTS = ["#8b5cf6", "#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#06b6d4", "#a855f7", "#14b8a6"];
  function bandTint(idx){
    return BAND_TINTS[Math.abs(Number(idx) || 0) % BAND_TINTS.length];
  }
  // 休息时段汇总文案：首段从 00:00 起、末段到 24:00 都标了休息就当跨零点那一段，
  // 读作「23:00–09:00」，别拆成两截。
  function bandRestLabel(bands){
    var rs = [];
    (bands || []).forEach(function(b){ if(b.rest) rs.push({ s: b.s, e: b.e }); });
    if(!rs.length) return "";
    var wrap = rs.length > 1 && rs[0].s === 0 && rs[rs.length - 1].e === BAND_SLOTS;
    var out = [];
    if(wrap) out.push(slotTime(rs[rs.length - 1].s) + "–" + slotTime(rs[0].e));
    rs.forEach(function(r, i){
      if(wrap && (i === 0 || i === rs.length - 1)) return;
      out.push(slotTime(r.s) + "–" + slotTime(r.e));
    });
    return out.join("、");
  }
  // v3.30（用户 m47796 第 1 条）：底图从柱状改折线——48 根柱子看着像锯齿，
  // 折线 + 淡色面积更像「一天里的活跃度」。坐标用 0-100 的 viewBox 拉伸铺满，
  // 线宽靠 vector-effect 固定，否则横向拉伸会把线拉成细丝。
  // v3.75（用户本轮第 2 条）：maxRef 允许外部传一把共用尺子——用户消息与 bot 回复两条线
  // 必须按同一个峰值归一化，否则各画各的，看着一边高一边矮、读数也跟左边 Y 轴对不上。
  function bandsLineSvg(slots, cls, maxRef){
    var max = (maxRef && maxRef > 0) ? maxRef : 1;
    if(!(maxRef && maxRef > 0)){
      (slots || []).forEach(function(n){ if(n > max) max = n; });
    }
    var pts = [];
    for(var i = 0; i < BAND_SLOTS; i++){
      var n = (slots && slots[i]) || 0;
      pts.push((i / BAND_SLOTS * 100).toFixed(2) + "," +
               (100 - (n ? Math.max(2, n * 94 / max) : 1)).toFixed(2));
    }
    // v3.74（用户 m00024 第 4-f 条）：右端 24:00 与左端 00:00 是同一个时刻，原来最后一个点停在
    // 23:30（97.92%），曲线尾巴直接砸到底、跟左边接不上。末尾补一个 x=100 的点、高度取 00:00 那个，
    // 首尾就对齐了（面积路径也跟着收口）。
    var firstY = 100 - ((slots && slots[0]) ? Math.max(2, slots[0] * 94 / max) : 1);
    pts.push("100.00," + firstY.toFixed(2));
    var area = "M0,100 L" + pts.join(" L") + " L100,100 Z";
    return "<svg class='" + (cls || "bp-line") + "' viewBox='0 0 100 100' preserveAspectRatio='none' aria-hidden='true'>" +
           "<path class='larea' d='" + area + "'/>" +
           "<polyline class='lline' points='" + pts.join(" ") + "' vector-effect='non-scaling-stroke' fill='none'/>" +
           "</svg>";
  }
  // 概率线：每段一条水平线，段与段之间竖直相连（阶梯形），比一堆短柱子清楚。
  function bandsStepSvg(bands, yMax, cls){
    var d = "";
    bands.forEach(function(b){
      var x0 = (b.s / BAND_SLOTS * 100).toFixed(2);
      var x1 = (b.e / BAND_SLOTS * 100).toFixed(2);
      var y = (100 - b.v / yMax * 100).toFixed(2);
      d += (d ? " L" : "M") + x0 + "," + y + " L" + x1 + "," + y;
    });
    return "<svg class='" + (cls || "bp-prob") + "' viewBox='0 0 100 100' preserveAspectRatio='none' aria-hidden='true'>" +
           "<path d='" + d + "' vector-effect='non-scaling-stroke'/></svg>";
  }
  // 底图口径：单群作用域统计这个群自己的消息，全局作用域统计全部会话（v3.30 第 5 条）
  function bandSrcNote(){
    return (scopeSid ? "本群" : "全部会话") + "近 7 天消息量";
  }
  // v3.31（用户 m48323 第 1 条）：曲线不再弹窗，直接内联编辑——拖分界、点数字改概率，
  // 每次改动立刻写回隐藏 input 走自动保存（debounce 0.8s）。
  var bandBands = null;   // 正在编辑的段落数组（renderSettings 重建时从 bandsRaw 重新解析）
  var bandSel = 0;        // 当前选中的段
  var bandDrag = null;    // 拖动中的分界索引
  // v3.74（用户 m00024 第 4-e 条）：「隐藏时间段划分」只是这张图的显示开关，不进插件配置，
  // 记在浏览器本地，下次打开设置页还是上次的选择。
  var bandHideSeg = (function(){
    try{ return localStorage.getItem("band_hide_seg") === "1"; }catch(e){ return false; }
  })();
  function setBandHideSeg(v){
    bandHideSeg = !!v;
    try{ localStorage.setItem("band_hide_seg", bandHideSeg ? "1" : "0"); }catch(e){}
  }
  // v3.75（用户本轮第 2 条）：这张图上现在有两条曲线（用户消息 / bot 回复），
  // 右上角下拉里勾哪条画哪条，默认全勾；也是本地显示偏好，不进插件配置。
  var bandBotSlots = null;      // bot 回复的 48 格分布（GET /hour-histogram 的 bot_slots）
  var bandShow = (function(){
    try{
      var s = localStorage.getItem("band_show_lines");
      if(s === null || s === undefined) return { u: true, b: true };
      return { u: s.indexOf("u") >= 0, b: s.indexOf("b") >= 0 };
    }catch(e){ return { u: true, b: true }; }
  })();
  function setBandShow(k, v){
    bandShow[k] = !!v;
    // 两条都不勾就看不出任何东西了，至少留另一条
    if(!bandShow.u && !bandShow.b) bandShow[k === "u" ? "b" : "u"] = true;
    try{
      localStorage.setItem("band_show_lines",
        (bandShow.u ? "u" : "") + (bandShow.b ? "b" : ""));
    }catch(e){}
    syncCurMenu();
  }
  // 下拉里的勾要跟着 bandShow 走（「两条都不勾」被回退成一条时，框也得跟着回去）
  function syncCurMenu(){
    var m = document.getElementById("bd-cur-menu");
    if(!m) return;
    Array.prototype.forEach.call(m.querySelectorAll("input[data-cur]"), function(inp){
      inp.checked = !!bandShow[inp.getAttribute("data-cur")];
    });
  }

  // 底图口径（v3.31 第 9 条）：只统计群聊，私聊消息不算在内
  function bandSrcNote(){
    return (scopeSid ? "本群群聊" : "全部群聊") + "近 7 天消息量";
  }
  // v3.37（用户 m02080 第 4 条）：API 价目表。模型名由后端并集给出（价目表 ∪ 最近 24 小时用到的），
  // 每行两个单价输入框；保存走独立接口，不参与 collectSettings 那套 schema 表单。
  function pricingBlockHtml(){
    return "<div class='pricing' id='pricing-box'><div class='phint'>正在读价目…</div></div>";
  }

  // v3.56（用户本轮第 4 条）：这一页改名「缓存记录」——各类缓存一张账本表：
  // 类型（+ 一句说明） / 数量 / 占用 / 群 / 私聊上限 / 保留天数。
  // 上限与保留天数直接内联成数字框，走的就是 data-k 那条自动保存通道（和别处设置行同一条路），
  // 不再在表格下面另起一排 rowHtml。
  // v3.67（用户 m20117 第 4 条）：清空按钮整列撤掉，改成超上限自动删（后端带 5 分钟缓冲）。
  // v4.00（用户 m10782）：「请求快照」拆成「回复快照」和「决策快照」两类，单位都是份。
  var CACHE_UNIT = { media: "张", requests: "份", decision_snapshots: "份",
                     decisions: "条", windows: "条", summaries: "份", stats: "条",
                     logs: "条", ledger: "条",
                     forward_cache: "条", retry_history: "条",
                     window_dm: "个" };
  // 这张表是设置页自己的兜底副本（后端不一定逐键给 bounds），必须与
  // main.py 的 _BOUNDS 一致：下限、上限都按后端抄，不在这边放宽。
  // 后端下限是 1 的条数类键，这里也写 1 —— 0 只在后端下限为 0 的键上表示
  // 「不限」，下限 1 的键填 0 会被后端夹回 1，界面不该先放行。
  // v3.63（用户 m19536 第 1/5 条）：上限分成「群聊默认 / 私聊默认」两套键（*_dm 是私聊那套）；
  // 消息日志与会话统计不设上限、只按天数滚动，而且天数下限锁 56 天（自适应要用这段数据），
  // 所以这两个键的下限跟后端 bounds 一样写 56。
  var CACHE_NUM_BOUNDS = {
    media_cache_limit: [1, 100000], media_cache_limit_dm: [0, 100000],
    media_cache_keep_days: [0, 3650], media_other_keep_days: [0, 3650],
    requests_keep_days: [0, 3650], decisions_keep_days: [56, 3650],
    windows_keep: [0, 1000], windows_keep_dm: [0, 1000],
    windows_keep_days: [1, 3650], summaries_keep: [0, 1000], summaries_keep_dm: [0, 1000],
    summaries_keep_days: [0, 3650],
    trace_keep: [1, 1000], trace_keep_dm: [1, 1000],
    decision_keep: [1, 1000], decision_keep_dm: [1, 1000],
    forward_media_keep: [0, 10000], forward_media_keep_dm: [0, 10000],
    media_other_limit: [0, 10000], media_other_limit_dm: [0, 10000],
    retry_keep: [0, 5000], retry_keep_dm: [0, 5000],
    retry_keep: [0, 5000], retry_keep_dm: [0, 5000],
    forward_media_keep_days: [0, 3650],
    retry_keep_days: [0, 3650]
  };
  // v3.62（用户 m18059）：这些缓存设置现在既能写全局默认值，也能只写给某个群/私聊。
  // 前端自己留一份清单——后端 /overrides 的 keys 里还没收这几个键时，表格照样画得出来。
  var CACHE_OV_KEYS = ["trace_keep", "trace_keep_dm", "decision_keep", "decision_keep_dm",
                       "windows_keep", "windows_keep_dm",
                       "requests_keep_days", "windows_keep_days",
                       "media_cache_limit", "media_cache_limit_dm", "media_cache_keep_days",
                       "media_other_limit", "media_other_limit_dm", "media_other_keep_days",
                       "summaries_keep", "summaries_keep_dm", "summaries_keep_days",
                       "forward_media_keep", "forward_media_keep_dm",
                       "retry_keep", "retry_keep_dm",
                       "retry_keep", "retry_keep_dm",
                       "forward_media_keep_days",
                       "retry_keep_days"];
  var CACHE_MSG_SCOPE_GLOBAL = "改的是所有会话的默认值";
  var CACHE_MSG_SCOPE_SESS = "";
  var cacheScopeSid = "";       // "" = 全局默认值；否则是正在编辑的会话
  var cacheScopeOv = null;      // 这个会话的 {keys, global, override, effective, bounds}
  var cacheScopeErr = "";       // 读不到就把话说清楚，别拿全局值冒充这个会话的值
  var cacheScopeLoading = false;
  // 全局默认值：作用域模式下 configCache 装的是这个会话的有效值，后端一时没回缓存键时
  // 还得靠这份全局配置把框填上，不然一格全是空白，看着像「这个群什么都没有」。
  var cacheDefCfg = {};
  var cacheStatsData = null;    // 最近一次 cache-stats 回包（切范围时要按它重画表格）
  var cacheSessList = [];       // 会话选择器的选项（cache-stats 的 sessions）

  function cacheHas(map, k){
    return !!map && Object.prototype.hasOwnProperty.call(map, k);
  }
  // 这个会话的覆盖拿到了没有——没有就只能按全局值画
  function cacheScopeLive(){ return !!cacheScopeSid && !!cacheScopeOv; }
  // 数字格该显示什么：会话模式下先看这个会话写过的覆盖，没有就跟随全局；
  // 全局模式就是原来那套（配置里的值）。
  function cacheCurVal(k){
    if(!k) return "";
    if(cacheScopeLive()){
      if(cacheHas(cacheScopeOv.override, k)) return cacheScopeOv.override[k];
      if(cacheHas(cacheScopeOv.global, k)) return cacheScopeOv.global[k];
      if(cacheHas(cacheScopeOv.effective, k)) return cacheScopeOv.effective[k];
    }
    var v = (cacheHas(configCache, k) ? configCache[k] : cacheDefCfg[k]);
    return (v == null) ? "" : v;
  }
  // 上下限：后端 /overrides 给了就用它的，没给（或还没加这几个键）用上面写死的那份。
  function cacheBounds(k){
    if(cacheScopeLive() && cacheScopeOv.bounds){
      var b = cacheScopeOv.bounds[k];
      if(b && b.length === 2 && isFinite(b[0]) && isFinite(b[1])) return [Number(b[0]), Number(b[1])];
    }
    return CACHE_NUM_BOUNDS[k] || null;
  }
  function cacheNumAttrs(k){
    var b = cacheBounds(k);
    return b ? " min='" + b[0] + "' max='" + b[1] + "'" : "";
  }
  // kinds 的 label，用来把 limit_follow（key）翻成中文名，例如「跟随快照」跟的是哪一类。
  // 先写一份默认名，接口只回一部分类型时也能翻译；paintCacheStats 里再用后端给的 label 覆盖。
  // v4.00（用户 m10782）：请求快照改名回复快照，另加一类决策快照。
  var CACHE_KIND_LABELS = { media: "图片缓存", media_unknown: "未归属图片",
                            media_other: "其他图片缓存", requests: "窗口快照",
                            decision_snapshots: "决策快照",
                            decisions: "消息日志", windows: "窗口快照",
                            summaries: "中期摘要", stats: "会话统计",
                            forward_cache: "转发缓存",
                            retry_history: "自动重试记录", window_dm: "私聊上下文存档" };
  var cacheKindLabels = {};
  // 占用大小：不到 1 GB 写 MB，过了 1 GB 换成 GB，都留一位小数。
  function cacheSize(mb){
    var v = Number(mb || 0);
    if(v >= 1024) return (Math.round(v / 1024 * 10) / 10).toFixed(1) + " GB";
    // v3.72（用户 m24260 第 11 条）：不到 0.1 MB 的写 KB——窗口快照只有几十 KB，
    // 写成 0.0 MB 会让人以为这一类根本没占空间。
    if(v > 0 && v < 0.1) return Math.max(1, Math.round(v * 1024)) + " KB";
    return (Math.round(v * 10) / 10).toFixed(1) + " MB";
  }
  // 数字框的初值：配置里有就填配置，没有就留空——别硬编 0，一保存就会把 0 写进后端。
  function cacheVal(key){
    if(!key) return "";
    var v = configCache ? configCache[key] : "";
    return (v == null) ? "" : v;
  }
  function cacheRowTitle(k, it){
    var f = (schemaCache && schemaCache[k]) || {};
    var nm = (it && it.label) || nameOf(k, f) || k;
    return f.hint || nm;
  }
  // 这个格是不是这个会话单独设过的：是就挂一个「单独」胶囊 + ×（点 × 删掉这条覆盖，恢复跟随全局）。
  function cacheOwnPill(k){
    if(!k || !cacheScopeLive() || !cacheHas(cacheScopeOv.override, k)) return "";
    return "<span class='cov'><i>单独</i><button type='button' class='covx' data-covclear='" + esc(k) +
      "' title='" + esc("删掉这个会话对「" + k + "」的单独设置，恢复跟随全局默认值") + "'>×</button></span>";
  }
  // 数字框：data-k 是配置键（决定存哪儿），data-ck='1' 说明这是缓存设置——
  // 选中某个会话时，这个框改的是那个会话的覆盖，不是全局默认值。
  // v3.68（用户 m20632 第 4 条）：单位不再挂在框外面，塞进框里（右对齐那一列才不会越排越宽）
  function cacheNumInput(cls, k, it, v, tip, unit){
    var zero = (String(v) !== "" && Number(v) === 0);
    // 填 0 时那句 title 换成「0 = 不限」，别让人以为填错了
    var title = (zero && !tip) ? "0 = 不限" : (tip || cacheRowTitle(k, it));
    // 会话模式下这个键没被单独设过、全局又暂时没有值（后端还没加这个键）——留个灰字提示，
    // 别让人对着一个空框猜「是不是坏了」。
    var ph = (cacheScopeLive() && !cacheHas(cacheScopeOv.override, k) && (v === "" || v == null))
      ? " placeholder='跟随全局'" : "";
    var inp = "<input type='number' class='" + cls + "' data-k='" + esc(k) + "' data-ck='1'" +
              cacheNumAttrs(k) + " value='" + esc(v) + "'" + ph + " title='" + esc(title) + "'>";
    if(!unit) return inp;
    return "<span class='iuw'>" + inp + "<i class='iu'>" + esc(unit) + "</i></span>";
  }
  // 这个会话是私聊吗（sid 写法有 gm_1 / gm:1 / bot_gm_1 / dm_admin 几种，只认最后那一段）。
  function cacheSidIsDM(sid){
    return /(?:^|[_:])dm[_:]/i.test(String(sid || ""));
  }
  // 上限：能改的给数字框（data-k 直接挂到那个配置键上）；跟着别的设置走的只写一句，不给框。
  // v3.63（用户 m19536 第 1 条）：上限不再是「一个总上限」——
  //   · 选中「全局配置」：并排两个框，左边群聊默认（limit_key）、右边私聊默认（limit_key_dm）；
  //   · 选中某个会话：只画这一个会话该用的那个框——私聊会话用 *_dm 那套，群聊会话用群聊默认那套。
  // 消息日志 / 会话统计这类没有 limit_key 的，写「—」并说明只按保留天数滚动。
  // 用户 m09847 第 2 条：群聊上限 / 私聊上限拆成两列，不再挤在一个格子里写「群 [框] 私 [框]」。
  // 两列共用 cacheLimitOne()：isDm 决定这一列该用 limit_key 还是 limit_key_dm。
  function cacheLimitUnit(it){
    // v3.68（用户 m20632 第 4 条）：上限单位塞进框里（「份/会话」这种长单位只取斜杠前那两个字）。
    var lu = (it && it.limit_unit) || "";
    return lu ? String(lu).split("/")[0] : "";
  }
  function cacheLimitOne(it, k, isDm){
    var unit = cacheLimitUnit(it);
    var noLimit = "<span class='cdim' title='" +
      esc("这一类不设上限，只按保留天数滚动") + "'>—</span>";
    if(it && it.limit_fixed){
      var fu = (it && it.limit_unit) || "张";
      return "<span class='cfixed' title='" +
        esc("固定上限：按 100 次回复快照约 4.3 张 ×4 取整") + "'>" +
        fmtNum(it.limit) + " " + esc(fu) + "</span>";
    }
    if(!k){
      // 用户 m18819：私聊窗口存档不设个数上限，有多少私聊就留多少。
      // 用户 m14386：这一格不再写「不限」，跟别的列一样写「—」，说明留在 title 里。
      if(it && it.key === "window_dm"){
        return isDm
          ? "<span class='cdim' title='" +
            esc("不再限制私聊个数，有多少私聊就留多少个窗口") + "'>—</span>"
          : noLimit;
      }
      // 窗口快照这类跟着别的设置走的，仍然只写一句「跟随快照」
      if(it && it.limit_follow){
        var fu2 = it.limit_follow;
        // 大多数跟的是别的缓存类型（labels 里有），也可能跟某个配置键（如 trace_keep），
        // 那就翻 schema 里的中文名，翻不到才原样写 key。
        var fn2 = cacheKindLabels[fu2] ||
                  ((schemaCache && schemaCache[fu2]) ? nameOf(fu2, schemaCache[fu2]) : fu2);
        var per = it.per_session ? "（每会话上限）" : "";
        return "<span class='cfollow' title='" +
          esc("上限跟随「" + fn2 + "」" + per + "，改那一项时一起变") + "'>跟随快照</span>";
      }
      return noLimit;
    }
    if(cacheScopeSid){
      // 会话模式：只画这个会话该用的那一列，另一列写「—」并说明为什么不适用。
      var sidDm = cacheSidIsDM(cacheScopeSid);
      if(sidDm !== !!isDm){
        return "<span class='cdim' title='" +
          esc("当前选中的是" + (sidDm ? "私聊" : "群聊") + "，这一列不适用") + "'>—</span>";
      }
      // v4.84（用户 m09738）：中期摘要这类只按全局算的项，后端只吃全局值，
      // 会话里改了也存不进去——画成只读，别让人以为改了会生效。
      if(it && it.global_only){
        return "<span class='cfixed' title='" +
          esc("这一项只按全局设置算，不能单独给某个会话设") + "'>" +
          fmtNum(cacheCurVal(k)) + " " + esc(unit) + "</span>";
      }
      return cacheNumInput("ipt cmax", k, it, cacheCurVal(k), "", unit) + cacheOwnPill(k);
    }
    return cacheNumInput("ipt cmax", k, it, cacheCurVal(k), "", unit) + cacheOwnPill(k);
  }
  // 群聊上限那一列：limit_key（老数据里 windows 这类没给 limit_key 的，退回 windows_keep）。
  function cacheLimitCellGroup(it){
    var k = (it && it.limit_key) || "";
    if(!k && it && it.key === "windows" && !it.limit_follow) k = "windows_keep";
    return cacheLimitOne(it, k, false);
  }
  // 私聊上限那一列：limit_key_dm；只有一套键的项退回 limit_key，别写「—」让人以为私聊没上限。
  function cacheLimitCellDm(it){
    var kdm = (it && it.limit_key_dm) || (it && it.limit_key) || "";
    if(!kdm && it && it.key === "windows" && !it.limit_follow) kdm = "windows_keep";
    return cacheLimitOne(it, kdm, true);
  }
  // 保留天数：有 days_key 的给数字框（0 = 不限，下限 56 的那两个例外），没有的写「—」。
  function cacheDaysCell(it){
    var k = (it && it.days_key) || "";
    if(!k) return "<span class='cdim'>—</span>";
    var v = cacheCurVal(k);
    // v3.63（用户 m19536 第 5 条）：消息日志 / 会话统计的天数下限锁 56 天，title 里说清楚。
    var b = cacheBounds(k);
    var tip = "";
    if(b && b[0] > 1){
      tip = (it && it.label ? it.label : k) + "：最少 " + b[0] + " 天，只能调高";
    }
    // v4.84（用户 m09738）：只按全局算的项（中期摘要、会话统计），会话里天数也改不了。
    if(cacheScopeSid && it && it.global_only){
      return "<span class='cfixed' title='" +
        esc("这一项只按全局设置算，不能单独给某个会话设") + "'>" +
        fmtNum(v) + " 天</span>";
    }
    // v3.68：保留天数的「天」也塞进框里
    return cacheNumInput("ipt cdays", k, it, v, tip, "天") + cacheOwnPill(k);
  }
  // 用户 m21482：每类缓存最右边两个清理按钮——只清 N 天前 / 清全部。
  // 用户 m14386：N 由后端每行给（clear_days，默认 7；用量账本 30），不再写死 7。
  // 清理范围是「所有会话」，不是当前作用域；按钮 title 里写明，避免误以为只清这个群。
  function cacheClearCell(it){
    if(!it || it.clearable === false){
      return "<span class='cdim' title='" + esc("这一类不提供手动清理") + "'>—</span>";
    }
    var key = esc(it.key || "");
    var label = esc(it.label || it.key || "");
    var cd = Number((it && it.clear_days) || 7) || 7;
    var tip = "清理所有会话的「" + label + "」，不是只清当前选中的会话";
    return "<button class='cclr' type='button' data-ckclear='" + key +
      "' data-ckdays='" + cd + "' data-label='清理 " + cd + " 天前' title='" +
      esc(tip + "；只删 " + cd + " 天前的记录") + "'>清理 " + cd + " 天前</button>" +
      "<button class='cclr' type='button' data-ckclear='" + key +
      "' data-ckdays='0' data-label='清理全部' title='" +
      esc(tip + "；这一类全部删掉") + "'>清理全部</button>";
  }
  function cacheRowHtml(it){
    var key = (it && it.key) || "";
    var unit = (it && it.unit) || CACHE_UNIT[key] || "项";
    var cnt = Number((it && it.count) || 0);
    var mb = (it && it.mb != null) ? it.mb : (Number((it && it.bytes) || 0) / 1048576);
    var note = (it && it.note) || "";
    return "<tr data-ckind='" + esc(key) + "'>" +
      "<td class='cname'><b>" + esc((it && it.label) || key) + "</b>" +
        (note ? "<div class='cnote'>" + esc(note) + "</div>" : "") + "</td>" +
      // 用户 m14353：数量那一格可以带一小段说明（转发图片用它报「N 张图」）。
      // 用户 m14620：说明跟数量锁在同一行，别换行。
      "<td class='num'>" + esc(fmtNum(cnt) + " " + unit) +
        ((it && it.sub) ? "<span class='csub'> · " + esc(it.sub) + "</span>" : "") + "</td>" +
      "<td class='num'>" + esc(cacheSize(mb)) + "</td>" +
      "<td class='cset'>" + cacheLimitCellGroup(it) + "</td>" +
      "<td class='cset'>" + cacheLimitCellDm(it) + "</td>" +
      "<td class='cset'>" + cacheDaysCell(it) + "</td>" +
      "<td class='cact'>" + cacheClearCell(it) + "</td>" +
      "</tr>";
  }
  function setCacheTotal(t){
    var nEl = document.getElementById("cache-total-n");
    var mEl = document.getElementById("cache-total-mb");
    if(nEl) nEl.textContent = t ? fmtNum(Number(t.count || 0)) : "—";
    if(mEl) mEl.textContent = t ? cacheSize(t.mb) : "—";
  }
  function cacheBlockHtml(){
    // 用户 m17752：右边那张「页面显示条数」卡片搬去基础设置了，这里只剩表格一列。
    return "<div class='cachebox' id='cache-box'>" +
      "<div class='cmain'>" +
      "<div class='cmain-l'>" +
      "<table class='ctable'><thead><tr>" +
        "<th>缓存类型</th><th class='num'>数量</th><th class='num'>占用</th>" +
        "<th class='num'>群聊上限</th><th class='num'>私聊上限</th>" +
        "<th class='num'>保留天数</th><th class='num'>操作</th>" +
      "</tr></thead>" +
      "<tbody id='cache-rows'><tr><td colspan='7' class='cnote'>读取中…</td></tr></tbody>" +
      "</table>" +
      "<div class='cbulk'>" +
        "<button class='cclr' type='button' data-ckclear='__all__' data-ckdays='7'" +
          " data-label='清理 7 天前的全部缓存' title='" +
          esc("清理所有会话里 7 天前的全部缓存（消息日志与统计、用量账本除外）") + "'>清理 7 天前的全部缓存</button>" +
        "<button class='cclr' type='button' data-ckclear='__all__' data-ckdays='0'" +
          " data-label='清理全部缓存' title='" +
          esc("清理所有会话的全部缓存（消息日志与统计、用量账本除外）") + "'>清理全部缓存</button>" +
        "<div class='csum'>" +
          "<span>共 <b id='cache-total-n'>—</b> 项 · 占用 <b id='cache-total-mb'>—</b></span>" +
        "</div>" +
      "</div>" +
      "<div class='chint' id='cache-msg'></div>" +
      "<div class='cextra' id='cache-extra'></div>" +
      "</div>" +
      "</div>" +
      "</div>";
  }

  // 表格的「上限 / 保留天数」两列是后端按 kinds 的 limit_key / days_key 长出来的：后端少给一类，
  // 那一项设置就会从页面上消失。这里兜一层——没被任何一类用到的缓存设置，补成普通行放表格下面；
  // 接口整个失败时四行全都补上，至少还能改，不至于「服务挂了连设置都看不见」。
  // v4.84：中期摘要的三项（私聊上限 / 群聊上限 / 保留天数）从「摘要」组挪到缓存记录页，
  // 并且直接长在表格里「中期摘要」那一行上（后端 _cache_kinds 给了 limit_key / days_key），
  // 所以这里不再列它们——列了反而会在表格下面多出三行重复的（用户 m09738）。
  // v4.90（用户 m01133 第 2 条）：三种快照合成一个「窗口快照」，表格里只剩一行；
  // 回复 / 决策记录页各自列多少条不再管磁盘留存，降级成显示条数。
  // 用户 m17752：这些显示条数后来搬去了设置页的独立一组（见 showRowsHtml），缓存页只剩表格。
  // 用户 m05550：那一组又从基础设置挪到了「窗口设置」页，跟窗口相关的项摆在一起。
  var CACHE_SET_KEYS = ["media_cache_limit", "media_cache_keep_days",
    "media_other_limit", "media_other_limit_dm", "media_other_keep_days",
    "forward_media_keep", "retry_keep",
    "forward_media_keep_days", "retry_keep_days"];
  // 「窗口设置」页那一组：回复记录 / 决策记录 / 窗口快照，每行「群聊 + 私聊」两个数字框。
  var SHOW_ROWS = [
    {t: "回复记录", k: "trace_keep", kd: "trace_keep_dm", d: "回复记录页最多列多少条"},
    {t: "决策记录", k: "decision_keep", kd: "decision_keep_dm", d: "决策记录页最多列多少条"},
    {t: "窗口快照", k: "window_show", kd: "window_show_dm", d: "走势图和快照列表最多画多少条"}
  ];
  // 用户 m17752：原来缓存记录页右边那张卡片，现在是一组普通行——三行，
  // 名称 + 说明 + 右侧「群聊 / 私聊」两个数字框，跟「配额」那两行同一套写法。
  function showRowsHtml(){
    return SHOW_ROWS.map(function(r){
      var f = schemaCache[r.k] || {}, fd = schemaCache[r.kd] || {};
      return "<div class='setrow inline showrow' data-row='" + esc(r.k) + "'>" +
        "<div class='nm'>" + esc(r.t) + defaultBadge(f, r.k) + "</div>" +
        "<div class='ds'>" + esc(r.d) + "</div>" +
        "<div class='ctl pair'>" +
          "<span class='nspan'><b>群聊</b>" + fieldControl(r.k, f, configCache[r.k], "条") + "</span>" +
          "<span class='nspan'><b>私聊</b>" + fieldControl(r.kd, fd, configCache[r.kd], "条") + "</span>" +
        "</div></div>";
    }).join("");
  }
  // 用户 m09851：窗口页「图片」组。前两行是插件自己的两个看图开关（从提示词页的模型卡搬来），
  // 后四行接管 KiraAI 基础设置里的「图片压缩」——那四项存在框架的 system_config.json
  // （bot_config.image_compression），不在插件配置里，所以不带 data-k，走 data-core 单独保存。
  var CORE_IMG_FIELDS = [
    { k: "core_img_enabled", f: "enabled", t: "图片压缩", type: "switch",
      d: "在 VLM 识图和原生多模态请求前压缩大图或高分辨率图片。" },
    { k: "core_img_max", f: "max_size", t: "图片最长边", type: "integer", unit: "像素",
      d: "压缩后的最长边，单位为像素。", min: 1, max: 8192 },
    { k: "core_img_quality", f: "quality", t: "JPEG 质量", type: "integer", unit: "",
      d: "JPEG 输出质量，范围为 1 到 100。", min: 1, max: 100 },
    { k: "core_img_minmb", f: "min_file_size_mb", t: "压缩阈值", type: "integer", unit: "MB",
      d: "仅在图片超过该 MB 大小或最长边超限时压缩。", min: 0 }
  ];
  var coreImgCache = null;   // 框架 bot_config.image_compression 的当前值，loadSettings 里拉
  function coreImgVal(field, dft){
    var ic = coreImgCache || {};
    var v = ic[field];
    return (v === undefined || v === null) ? dft : v;
  }
  function coreImgRow(it){
    var ctl;
    if(it.type === "switch"){
      ctl = "<label class='sw2'><input type='checkbox' data-core='" + esc(it.f) + "'" +
        (coreImgVal(it.f, false) ? " checked" : "") +
        "><span class='tr' data-on='开启' data-off='关闭'></span></label>";
    } else {
      var u = it.unit || "";
      ctl = "<span class='ipt" + (u ? (" has-unit" + (u.length > 1 ? " uw2" : "") + (u.length >= 4 ? " uw3" : "")) : "") +
        "'><input class='dipt num' type='number' data-core='" + esc(it.f) + "' value='" + esc(coreImgVal(it.f, "")) + "'" +
        (it.min === undefined ? "" : " min='" + it.min + "'") +
        (it.max === undefined ? "" : " max='" + it.max + "'") + ">" +
        (u ? "<b class='unit'>" + esc(u) + "</b>" : "") + "</span>";
    }
    return "<div class='setrow inline' data-row='" + esc(it.k) + "'>" +
      "<div class='nm'>" + esc(it.t) + "</div>" +
      "<div class='ds'>" + esc(it.d) + "</div>" +
      "<div class='ctl'>" + ctl + "</div></div>";
  }
  function imgBlockHtml(){
    var h = "";
    ["decision_see_images", "reply_see_images"].forEach(function(k){
      var f = schemaCache[k];
      if(f) h += rowHtml(k, f);
    });
    if(!coreImgCache){
      // 框架配置没读回来（接口挂了 / 没登录）时别画一排空框，给一句能看懂的提示。
      h += "<div class='setrow inline' data-row='core_img_note'>" +
        "<div class='nm'>图片压缩</div>" +
        "<div class='ds'>读取框架配置失败，刷新页面再试。</div>" +
        "<div class='ctl'></div></div>";
      return h;
    }
    // 用户 m13535：像素数超标的图（手机长截图）会先缩小再发，不然上游按像素数拒收。
    // 这一项固定开启，开关只用来告诉用户有这个功能，所以不带 data-k / data-core，点不动。
    h += "<div class='setrow inline' data-row='image_pixel_guard'>" +
      "<div class='nm'>长图自动缩小</div>" +
      "<div class='ds'>超过 600 万像素先缩小再发，固定开启。</div>" +
      "<div class='ctl'><label class='sw2 lock'><input type='checkbox' checked disabled>" +
      "<span class='tr' data-on='开启' data-off='关闭'></span></label></div></div>";
    h += CORE_IMG_FIELDS.map(coreImgRow).join("");
    return h;
  }
  // 框架的图片压缩只能走框架自己的接口：整份读回来 → 改 bot_config.image_compression → 整份写回去。
  // 插件配置接口（PUT /config）改不到 system_config.json，混进去只会被后端丢掉。
  function cacheExtraHtml(kinds){
    var box = document.getElementById("cache-extra");
    var covered = {};
    (kinds || []).forEach(function(it){
      if(!it) return;
      // v3.63：上限现在有群聊 / 私聊两套键，两套都算「已经进表了」
      if(it.limit_key) covered[it.limit_key] = 1;
      if(it.limit_key_dm) covered[it.limit_key_dm] = 1;
      if(it.days_key) covered[it.days_key] = 1;
    });
    var out = "";
    CACHE_SET_KEYS.forEach(function(k){
      var f = schemaCache[k];
      if(!f || covered[k]) return;
      out += rowHtml(k, f);
    });
    if(box) box.innerHTML = out;
  }

  function setCacheMsg(text, cls){
    var el = document.getElementById("cache-msg");
    if(!el) return;
    el.textContent = text || "";
    el.className = "chint" + (cls ? " " + cls : "");
  }

  function cacheSessName(sid){
    if(!sid) return "未知会话";
    if(aliasCache[sid]) return aliasCache[sid] + "（" + sid + "）";
    // 会话 id 有两种写法：设置页顶部是「gm:1145141974」，缓存记录的回包是「gm_1145141974」
    // （线上就是这样）。两种都认得出「群 N / 私聊 N」，别把 sid 原样怼给用户；
    // 「bot_gm_1」这种老写法不认识号，原样显示。
    var s = String(sid);
    return /^(gm|dm)[_:]/.test(s) ? sidLabel(s.replace(/^(gm|dm)[_:]/, "$1:")) : s;
  }
  // 作用域统一用设置页顶部那套冒号写法（gm:1145141974）：缓存回包是下划线写法，
  // 两边不归一的话，缓存记录里选中的会话在顶部那个下拉里匹配不上，会把选择器打成空白。
  function cacheNormSid(sid){
    return String(sid || "").replace(/^(gm|dm)[_:]/, "$1:");
  }

  // v3.54（用户本轮第 4 条）：按新契约渲染——kinds 每类一行，total 写进合计行。
  // 拿不到数据 / 接口失败一律写「读取失败」，不留白。
  // v3.62（用户 m18059）：「缓存设置能独立到每个群和私聊」——不再整块禁用，
  // 改成顶部选作用范围：选「全局配置」就是改所有会话的默认值；
  // 选中某个群 / 私聊，每行显示它自己的有效值，改动写成这个会话的覆盖（没设过的跟随全局）。
  // 用户本轮：「全局设置的是每个群组的上限 不是总的上限，总的上限=全局设置*群组数量」。
  // 用户 m21482：底部不再写整段说明，只留合计；这里只放操作结果，默认空着。
  var CACHE_MSG_DEFAULT = "";
  // 作用范围选择器：全局配置 + cache-stats 里的各会话（缓存里一个都没有的会话也要能选到——
  // 选它是为了给它单独设上限，不是因为它已经有记录）。
  // v3.67（用户 m20117 第 6 条）：缓存页自己那个「作用范围」下拉框撤掉——
  // 页面顶部已经有常驻卡片条，两个选择器各说各话反而容易看错。这里只留一行说明。
  // 2026-11-08：这句说明从表格上面挪到「缓存记录」标题右边，标题由 renderSettings 画，
  // 文字由这里给——两处共用同一句，别各写一份。
  function cacheScopeHintText(){
    return cacheScopeSid ? ("正在改「" + cacheSessName(cacheScopeSid) + "」自己的缓存设置；" +
      "灰色胶囊「单独」= 这项这个会话单独设过，点 × 恢复跟随全局")
      : ("现在" + CACHE_MSG_SCOPE_GLOBAL + "。点页面顶部某个群 / 私聊的卡片，可以只改它自己的。");
  }
  function renderCacheScopeOptions(){
    var hint = document.getElementById("cache-scope-hint");
    if(!hint) return;
    hint.textContent = cacheScopeHintText();
  }
  // 这一行既写作用范围说明，也写「已清掉 8 张」这类结果——所以只在当前显示的就是
  // 上一次由这里写进去的那句（或空着）时才改写，别把刚清完的结果冲掉。
  var cacheScopeMsgText = "";
  function paintCacheScopeMsg(){
    var msg = document.getElementById("cache-msg");
    if(!msg) return;
    var cur = String(msg.textContent || "");
    // 读不到这个会话的覆盖属于「现在显示的东西不可信」，优先说出来，别被别的提示盖住
    if(cacheScopeSid && cacheScopeErr){
      if(cur !== cacheScopeErr){ msg.textContent = cacheScopeErr; cacheScopeMsgText = cacheScopeErr; }
      msg.className = "chint warn";
      return;
    }
    var want = cacheScopeSid ? CACHE_MSG_SCOPE_SESS : CACHE_MSG_DEFAULT;
    if(cur === want){ cacheScopeMsgText = want; msg.className = "chint"; return; }
    if(cur !== "" && cur !== cacheScopeMsgText) return;   // 正在显示清空/保存结果，留着
    msg.textContent = want;
    msg.className = "chint";
    cacheScopeMsgText = want;
  }
  // 表格按「最近一次 cache-stats 回包 + 当前作用范围」重画：会话模式取值走 cacheCurVal，
  // 窗口快照那类后端暂时没给 limit_key 的，按约定兜成 windows_keep。
  function renderCacheRows(){
    var body = document.getElementById("cache-rows");
    if(!body) return;
    var d = cacheStatsData;
    if(!d || !d.ok || !(d.kinds && d.kinds.length)) return;  // 占位 / 失败文案已经由 loadCacheStats 写过
    body.innerHTML = d.kinds.map(cacheRowHtml).join("");
    setCacheTotal(d.total || null);
    if(cacheScopeSid){
      // 会话模式下不显示全局补充行：这几个键能不能按会话单独设，后端说了算，别在这乱写
      var ex = document.getElementById("cache-extra");
      if(ex) ex.innerHTML = "";
    }else{
      cacheExtraHtml(d.kinds);
    }
  }
  // 换作用范围：跟页面顶部那个「全局设置 / 单群」选择器是同一件事，所以直接改 scopeSid 再整页重载，
  // 免得两个下拉各说各话（改这边、那边还写着全局）。
  function loadCacheScope(){
    var sid = cacheScopeSid;
    if(!sid) return Promise.resolve();
    cacheScopeLoading = true;
    return get(API + "/overrides?sid=" + encodeURIComponent(sid)).then(function(d){
      if(sid !== cacheScopeSid) return;   // 期间又切走了，别把旧结果画上去
      cacheScopeLoading = false;
      if(d && d.ok !== false){ cacheScopeOv = d; cacheScopeErr = ""; }
      else { cacheScopeOv = null; cacheScopeErr =
        "读这个会话的单独设置失败了：先按全局值显示，改了会再试着保存一次。"; }
      renderCacheRows();
      paintCacheScopeMsg();
    }).catch(function(err){
      if(sid !== cacheScopeSid) return;
      cacheScopeLoading = false;
      cacheScopeOv = null;
      cacheScopeErr = "读这个会话的单独设置失败了（" + String((err && err.message) || "网络不通") +
        "）：先按全局值显示，改了会再试着保存一次。";
      renderCacheRows();
      paintCacheScopeMsg();
    });
  }
  function postJson(url, body){
    return fetch(url, {method:"POST", credentials:"same-origin",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify(body || {})}
    ).then(function(r){ if(!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
  }
  // 写覆盖：先按老写法（values 塞在查询串里）POST；后端换成 JSON 体了就再试一次，两条路都留着。
  function cachePostOverrides(sid, values, merge){
    var qs = API + "/overrides?sid=" + encodeURIComponent(sid) + (merge ? "&merge=1" : "") +
      "&values=" + encodeURIComponent(JSON.stringify(values || {}));
    return post(qs).then(function(d){
      if(d && d.ok !== false) return d;
      // 原来这里还有一条 postJson(CORE + "/overrides") 的兜底，但后端只在
      // /api/plugin/<id>/overrides 注册了这个接口，那条路必然 404，只会把真实错误
      // 盖成「兜底也失败」（审查报告）。直接把原始错误抛出去。
      throw new Error((d && (d.detail || d.error)) || "保存失败");
    });
  }
  // 改一个键：merge 只提交这一个键，别把别的覆盖冲掉
  function cacheWriteKey(k, raw){
    var sid = cacheScopeSid;
    if(!sid || !k) return;
    var v = String(raw == null ? "" : raw).trim();
    if(v === "" || !isFinite(Number(v))){
      setCacheMsg("「" + k + "」要填数字（0 = 不限）", "warn");
      renderCacheRows();
      return;
    }
    // v3.67（用户 m20117 第 2 条）：这两个键的天数下限是 56，单独给某个会话设也一样，填小了一律按 56 存。
    var num = Number(v);
    if(k === "decisions_keep_days" && num < 56){ num = 56; v = "56"; }
    var kv = {}; kv[k] = num;
    cachePostOverrides(sid, kv, true).then(function(d){
      if(d && d.ok === false){
        setCacheMsg("保存失败：" + String((d && d.detail) || "后端没接受这次修改"), "bad");
        renderCacheRows();
        return;
      }
      setCacheMsg("已保存「" + cacheSessName(sid) + "」的 " + k + " = " + v + "（只对这个会话生效）", "ok");
      loadCacheScope();
    }).catch(function(err){
      setCacheMsg("保存失败：" + String((err && err.message) || "网络不通"), "bad");
      renderCacheRows();
    });
  }
  // 点 ×：删掉这个键的覆盖——提交这个会话剩下的覆盖集合（整体替换），恢复跟随全局
  function cacheClearKey(k){
    var sid = cacheScopeSid;
    if(!sid || !k) return;
    var rest = {};
    var ov = (cacheScopeOv && cacheScopeOv.override) || {};
    Object.keys(ov).forEach(function(x){ if(x !== k) rest[x] = ov[x]; });
    cachePostOverrides(sid, rest, false).then(function(d){
      if(d && d.ok === false){
        setCacheMsg("改回跟随全局失败：" + String((d && d.detail) || "后端没接受这次修改"), "bad");
        return;
      }
      setCacheMsg("「" + k + "」已改回跟随全局默认值", "ok");
      loadCacheScope();
    }).catch(function(err){
      setCacheMsg("改回跟随全局失败：" + String((err && err.message) || "网络不通"), "bad");
    });
  }
  // 首帧 / 重画后：装选择器、画表格、必要时把这个会话的覆盖拉一次
  function mountCacheBlock(){
    renderCacheScopeOptions();
    renderCacheRows();
    paintCacheScopeMsg();
    if(cacheScopeSid && !cacheScopeOv && !cacheScopeLoading) loadCacheScope();
  }
  function paintCacheStats(d){
    paintCacheStatsInner(d);
    mountCacheBlock();
  }
  function paintCacheStatsInner(d){
    var body = document.getElementById("cache-rows");
    var kinds = (d && d.kinds) || [];
    if(!body) return;
    // 回包存一份：切作用范围（全局 <-> 某个会话）时不用再请求一次就能重画表格
    cacheStatsData = (d && d.ok && kinds.length) ? d : null;
    cacheSessList = (d && Array.isArray(d.sessions)) ? d.sessions : [];
    if(!cacheStatsData){
      if(body) body.innerHTML = "<tr><td colspan='7' class='cnote bad'>读取失败</td></tr>";
      setCacheTotal(null);
      // 接口整个失败时也得让人改：全局模式下把四行设置补在表格下面（会话模式下这些键归会话管，不补）
      var ex0 = document.getElementById("cache-extra");
      if(cacheScopeSid){
        if(ex0) ex0.innerHTML = "";
      }else{
        cacheExtraHtml(null);
      }
      return;
    }
    cacheKindLabels = {};
    Object.keys(CACHE_KIND_LABELS).forEach(function(k){ cacheKindLabels[k] = CACHE_KIND_LABELS[k]; });
    kinds.forEach(function(it){ if(it && it.key) cacheKindLabels[it.key] = (it.label || it.key); });
  }

  function loadCacheStats(){
    var body = document.getElementById("cache-rows");
    if(!body) return;
    // v3.68（用户 m20632 第 4 条后半）：这里不再先清成「读取中…」——旧数据留在原位，
    // 新数据回来直接替换，切作用范围时不会闪一下（后端 /cache-stats 本身有 10 秒缓存）。
    if(!cacheStatsData) body.innerHTML = "<tr><td colspan='7' class='cnote'>读取中…</td></tr>";
    // 用户 m22090：选了单个群 / 私聊时，数量 / 占用 / 合计都要按这个会话取，
    // 不能还拿全局总量。后端按 sid 逐类过滤，10 秒缓存也按 sid 分开。
    var wantSid = cacheScopeSid || "";
    var qs = wantSid ? ("?sid=" + encodeURIComponent(wantSid)) : "";
    get(API + "/cache-stats" + qs).then(function(d){
      // 用户 m22090：切作用范围时可能同时有全局和单会话两个请求在飞，
      // 旧的那个晚回来会把新表格盖回全局总量。回包只认当前作用范围。
      if(wantSid !== (cacheScopeSid || "")) return;
      paintCacheStats(d);
    }).catch(function(){
      if(wantSid !== (cacheScopeSid || "")) return;
      paintCacheStats(null);
    });
  }

  // 用户 m21482：每类一行两个清理按钮 + 底部两个总按钮。第一次点变「确认清理？」，
  // 3 秒内再点才真发请求；点别的按钮 / 超时都会复位。清理范围是所有会话，不是当前作用域。
  var cacheClearArm = null;
  function cacheClearReset(btn){
    if(!btn) return;
    btn.removeAttribute("data-armed");
    btn.textContent = btn.getAttribute("data-label") || "清理";
  }
  function cacheClearKind(kind, days, btn){
    var sig = String(kind) + "|" + String(days);
    if(cacheClearArm && cacheClearArm.sig === sig && cacheClearArm.btn === btn){
      clearTimeout(cacheClearArm.timer);
      cacheClearArm = null;
      cacheClearReset(btn);
      doCacheClear(kind, days);
      return;
    }
    if(cacheClearArm){
      clearTimeout(cacheClearArm.timer);
      cacheClearReset(cacheClearArm.btn);
    }
    cacheClearArm = {
      sig: sig, btn: btn,
      timer: setTimeout(function(){
        cacheClearReset(btn);
        if(cacheClearArm && cacheClearArm.btn === btn) cacheClearArm = null;
      }, 3000)
    };
    btn.setAttribute("data-armed", "1");
    btn.textContent = "确认清理？";
  }
  function doCacheClear(kind, days){
    var apiKind = (kind === "__all__") ? "all" : kind;
    var label = (kind === "__all__") ? "全部缓存" : (cacheKindLabels[kind] || kind);
    setCacheMsg("正在清理…", "");
    post(API + "/cache-clear?kind=" + encodeURIComponent(apiKind) +
         "&days=" + encodeURIComponent(days)).then(function(d){
      if(!d || !d.ok){
        setCacheMsg("清理失败：" + String((d && d.detail) || "后端没接受这次清理"), "bad");
        return;
      }
      var when = Number(days) > 0 ? ("（只清 " + Number(days) + " 天前的）") : "";
      setCacheMsg("已清理「" + label + "」" + when + "：" +
        fmtNum(d.removed || 0) + " 项，约 " + cacheSize(d.mb || 0), "ok");
      loadCacheStats();
    }).catch(function(err){
      setCacheMsg("清理失败：" + String((err && err.message) || "网络不通"), "bad");
    });
  }
  function bindCacheClear(){
    var box = document.getElementById("cache-box");
    if(!box) return;
    box.onclick = function(ev){
      var t = ev && ev.target;
      var xn = t;
      while(xn && xn !== box){
        if(xn.getAttribute && xn.getAttribute("data-ckclear")){
          cacheClearKind(xn.getAttribute("data-ckclear"),
                         xn.getAttribute("data-ckdays") || "0", xn);
          return;
        }
        if(xn.getAttribute && xn.getAttribute("data-covclear")){
          cacheClearKey(xn.getAttribute("data-covclear"));
          return;
        }
        xn = xn.parentNode;
      }
    };
  }

  // v3.43（用户本轮第 4 条）：价目改成实时保存——没有「保存价目」按钮了。
  // 停手 600ms 自动 POST 一次；连续敲键只发最后一个请求。结果写行内小字（#p-stat），不弹窗不刷新。
  var PRICING_DELAY = 600;
  // 用户 m00153：高峰时段固定两行，最多两段；没填起止的那行等于没有。
  var PEAK_ROWS = 2;
  var pricingTimer = 0;
  var pricingSaving = false, pricingSaveQueued = false;
  var pricingPartialSaved = false;
  var pricingLoadSeq = 0, pricingWriteVersion = 0;
  var pricingInFlight = null, pricingInFlightHours = "";
  var pricingCacheAt = 0, pricingCacheHours = "", pricingCacheWriteVersion = -1;
  var pricingTimezone = "";
  // 用户 m23331：表格底部那两行小字不要了；「另有 N 个模型没有调用」挪到顶部总花费右边。
  var pricingIdleCount = 0;
  var pricingRemoved = Object.create(null);
  // 用户 m19975：图表按日历切——七天 / 本月 / 上月，默认本月；跟上面表格的
  // 「最近 24 小时 / 7 天 / 30 天」不是一回事，本月只画 1 号到今天（未来的日子不算）。
  // 用户 m20332：刷新后固定回到「本月」，不再记住上次选的区间。
  // 用户 m23442：柱图改成 14 天窗口、滚轮按天翻（一个月 30 根柱子挤不下柱底那两行字）；
  // 下面三张饼各自选区间，默认本月。
  // 用户 m29560 第 8 条：只剩 默认 / 本周 / 上周 / 本月 / 上月 五档，外加月历自选。
  var pricingTrendMode = "default";
  // 用户 m29560 第 7 条：右上角月历（点开才展开，展开就是真正的月历）。
  var pricingCal = { open: false, ym: "", start: "", end: "", target: "" };
  // 用户 m29947：柱图绘图区在屏幕上的位置，滚轮只在这块里翻图。
  var pricingGeom = null;
  // 用户 m29560 第 8 条：月历选出来的自定义区间。
  var pricingCustom = { start: "", end: "" };
  // 用户 m29478：滚轮翻窗口时给个方向，画完就清掉（0 = 不播动画）。
  var pricingAnimDir = 0;
  // 用户 m30335：这一下滚了几格（正 = 往后翻）。平移距离按格数算，滚快了图跟着多滑一段。
  var pricingPanDays = 0;
  // 用户 m30335：上一帧此刻滑到哪儿。新的一帧要从这个位置接着走，
  // 不然连着滚的时候每一格都从原位重新起步，看着就是一跳一跳的。
  var pricingPanFrom = 0;
  var pricingWinEnd = "";
  var pricingWinTimer = 0;
  // 用户 m09125：滚轮翻窗口改成排队——滚几格排几格，每格走完自己的动画时间再走下一格。
  // pricingPanQueue 是还没走的格数（正 = 往后翻），pricingPanBusy 表示当前这一格还在走。
  var pricingPanQueue = 0, pricingPanBusy = false, pricingWheelAcc = 0;
  var pricingTrend = null, pricingTrendSeq = 0, pricingTrendAt = 0, pricingTrendRangeLoaded = "";
  // 用户 m06116：三张饼，一张饼一个指标。
  // 用户 m06437：每张饼里改成左右两个饼——左边按功能、右边按模型，sub 只在数据没到时露一下。
  var pricingPies = [
    { id: "cost", title: "花费占比", sub: "按功能 / 按模型", start: "", end: "", data: null, tag: "", at: 0, seq: 0, open: false },
    { id: "tokens", title: "Token 占比", sub: "按功能 / 按模型", start: "", end: "", data: null, tag: "", at: 0, seq: 0, open: false },
    { id: "calls", title: "调用分布", sub: "按功能 / 按模型", start: "", end: "", data: null, tag: "", at: 0, seq: 0, open: false }
  ];

  function setPMsg(text, cls){
    var el = document.getElementById("p-stat");
    if(!el) return;
    el.textContent = text || "";
    el.className = "pstat" + (cls ? " " + cls : "");
  }

  function fmtNum(n){
    var v = Number(n || 0);
    return v.toLocaleString("zh-CN");
  }

  // 用户 m10749 第 3 条：区间三档——最近 24 小时 / 7 天 / 30 天，对应后端 hours=24/168/720。
  var PRICE_RANGES = [{ h: 24, t: "最近 24 小时" }, { h: 168, t: "最近 7 天" }, { h: 720, t: "最近 30 天" }];

  function priceRangeLabel(h){
    for(var i = 0; i < PRICE_RANGES.length; i++){
      if(PRICE_RANGES[i].h === Number(h)) return PRICE_RANGES[i].t;
    }
    return PRICE_RANGES[0].t;
  }

  // 用户 m10749 第 3 条：token 数字自动换算到 K / M / B，表里只留一位小数；
  // 完整数字走 title（见 paintPricing），要看精确值悬停即可。
  function fmtBig(n){
    var v = Number(n || 0);
    if(!isFinite(v)) return "0";
    var abs = Math.abs(v);
    if(abs >= 1e9) return (v / 1e9).toFixed(1) + "B";
    if(abs >= 1e6) return (v / 1e6).toFixed(1) + "M";
    // 用户 m23535：10 万以上直接换成 M，字更短（100K 写成 0.1M，650.2K 写成 0.65M）。
    if(abs >= 1e5) return (v / 1e6).toFixed(2).replace(/\.?0+$/, "") + "M";
    if(abs >= 1e3) return (v / 1e3).toFixed(1) + "K";
    return fmtNum(v);
  }

  function priceRangeHtml(){
    return PRICE_RANGES.map(function(r){
      return "<button type='button' class='prb" + (Number(pricingHours) === r.h ? " on" : "") +
             "' data-phours='" + r.h + "'>" + esc(r.t) + "</button>";
    }).join("");
  }

  // 用户本轮：价目表「费用」列与顶部总计共用这一套钱数写法（两位小数、千分位），不另造一份。
  function priceMoneyText(v){
    var n = Number(v || 0);
    if(!isFinite(n)) n = 0;
    return n.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  // 柱子顶上那点钱常常只有几厘（回复一天也就 0.004 元），两位小数会全变成 0.00，
  // 写了等于没写；不到一分时多给一位，才看得出是多少（用户 m21592）。
  function barCostText(v){
    var n = Number(v || 0);
    // 用户 m22683：单价记成 0 的模型（比如免费渠道）照实写 0.00，别再留一个看不懂的「—」。
    if(!isFinite(n) || n <= 0) return priceMoneyText(0);
    if(n < 0.01) return n.toFixed(3);
    return priceMoneyText(n);
  }

  // 用户 m22269：有 token 却没价（模型身份没记下来、或那行被价目表删了）不是「没有数据」，
  // 柱子上写「未定价」，别跟真没花钱混成同一个「—」。
  function barCostLabel(kb){
    var kcost = Number((kb || {}).cost) || 0;
    if(kcost <= 0 && (kb || {}).priced === false) return "未定价";
    // 用户 m05232：这一格是按「当时的回复模型」估出来的，前面加个 ≈ 跟实算的区分开。
    if((kb || {}).estimated) return "≈" + barCostText(kcost);
    return barCostText(kcost);
  }
  // 标签宽度估算：「未定价」是中文，按 5.9px 一个字算会窄一半，撞位判定就漏了。
  function labTextWidth(txt){
    var t = String(txt), w = 0;
    for(var i = 0; i < t.length; i++){ w += t.charCodeAt(i) > 255 ? 10.4 : 5.9; }
    return w + 3;
  }

  // 用户 m10749 第 3 条：区间总花费。cost 为 null（后端统计失败）时显示「—」；
  // 有模型没在价目表里时补一行小字，说明这些按 0 元算。
  // 用户本轮第 6 条：数字在前、单位在后（原来单位粘在数字前面，读起来像代号）——
  // 单位单独一个 .cunit，字号小一号、垂直居中；后端没给币种就干脆不输出单位。
  function priceCostHtml(){
    var label = priceRangeLabel(pricingHours);
    var c = pricingCost;
    if(!c){
      return "<span class='clab'>" + esc(label) + "共</span><span class='cnum'>—</span>";
    }
    var total = Number(c.total || 0);
    var cur = String(c.currency == null ? "" : c.currency);
    var num = priceMoneyText(total);
    var html = "<span class='clab'>" + esc(label) + "共</span>" +
               "<span class='cnum'>" + esc(num) + "</span>" +
               (cur ? "<span class='cunit'>" + esc(cur) + "</span>" : "");
    var unset = (c.unset || []).length, short = [], tips = [];
    if(unset){ short.push(fmtNum(unset) + " 模型未设价"); tips.push(fmtNum(unset) + " 个模型没在价目表里，按 0 元算"); }
    if(c.approximate){ short.push("近似估算"); tips.push("部分记录只有整轮或观测时间，按近似时间选价"); }
    if(c.unknown_time){ short.push("缺时间按普通价"); tips.push("有记录缺有效时间，按普通价算"); }
    if(short.length) html += "<span class='cwarn'" + (tips.length ? " title='" + esc(tips.join("；")) + "'" : "") +
      ">" + esc(short.join(" · ")) + "</span>";
    // 用户 m23331：这句放在「近似估算」右边。
    if(pricingIdleCount) html += "<span class='cmore'>另有 " + esc(fmtNum(pricingIdleCount)) +
      " 个模型这段时间没有调用，已隐藏。</span>";
    return html;
  }

  function pricingPeak(value){
    // 用户 m23442：周末默认按高峰算，不再默认当谷价（勾选框默认不勾）。
  var p = Object.assign({enabled:false,in:null,out:null,cache:null,start:"",end:"",weekends:true}, value || {});
    // An explicit list (including []) is authoritative; legacy fields mirror only its first interval.
    p.periods = Array.isArray(p.periods) ? p.periods.map(function(t){ return {start:String(t.start || ""),end:String(t.end || "")}; }) :
      (p.start || p.end ? [{start:p.start,end:p.end}] : []);
    p.start = p.periods.length ? p.periods[0].start : "";
    p.end = p.periods.length ? p.periods[0].end : "";
    return p;
  }

  function pricingPeakIncomplete(p){
    var clock=/^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/;
    return (p.enabled && !p.periods.length) || p.periods.some(function(t){
      return !clock.test(t.start) || !clock.test(t.end) || t.start === t.end;
    });
  }

  // 用户 m00153：分钟只有 00 / 30 两档，别的分钟就近夹到这两档（45 分进到下一小时）。
  function snapClock(value){
    var text=String(value || "").trim();
    if(!text) return "";
    var hit=text.match(/^(\d{1,2}):(\d{2})/);
    if(!hit) return "";
    var hour=Math.min(23, Math.max(0, Number(hit[1]) || 0));
    var minute=Number(hit[2]) || 0;
    if(minute >= 45){ hour=(hour + 1) % 24; minute=0; }
    else if(minute >= 15) minute=30;
    else minute=0;
    return (hour < 10 ? "0" : "") + hour + ":" + (minute ? "30" : "00");
  }

  function pricingPeriodsFromRow(tr){
    var ends=tr.querySelectorAll("[data-pfield='peak.end']"), periods=[];
    Array.prototype.forEach.call(tr.querySelectorAll("[data-pfield='peak.start']"), function(el,i){
      var t={start:snapClock(el.value),end:snapClock(ends[i] && ends[i].value)};
      // 用户 m00153：起止都没填的那一行等于没有，直接不收集。
      if(t.start || t.end) periods.push(t);
    });
    return periods.slice(0, PEAK_ROWS);
  }

  function pricingPendingMessage(saved){
    var models=collectPricing(), incomplete=Object.keys(models).some(function(m){ return pricingPeakIncomplete(models[m].peak); });
    if(incomplete) return saved ? "普通价格已保存，高峰时段待补全" : "高峰时段待补全（普通价格仍会保存）";
    return "待保存…";
  }

  // 用户 m00153：固定画两行、最多两段；不填起止的那行等于没有，所以不再有「添加时段 / 删段」。
  function pricingPeriodsHtml(p){
    var periods=(p.periods || []).slice(0, PEAK_ROWS);
    while(periods.length < PEAK_ROWS) periods.push({start:"",end:""});
    return "<div class='pperiods'>" + periods.map(function(t,i){
      var attrs=" data-pperiod='" + i + "'";
      return "<div class='ptimes'>" +
        "<input type='time' step='1800' data-pfield='peak.start'" + attrs + " aria-label='第 " + (i+1) + " 段高峰开始时间' value='" + esc(t.start) + "'>" +
        "<span class='psep'>–</span>" +
        "<input type='time' step='1800' data-pfield='peak.end'" + attrs + " aria-label='第 " + (i+1) + " 段高峰结束时间' value='" + esc(t.end) + "'>" +
        "</div>";
    }).join("") + "</div>";
  }

  function pricingFocus(box){
    var el = document.activeElement;
    var tr = el && el.closest ? el.closest("tr[data-pmodel]") : null;
    if(!tr || tr.closest("#pricing-box") !== box) return null;
    return {model:tr.getAttribute("data-pmodel"), field:el.getAttribute("data-pfield"), period:el.getAttribute("data-pperiod"), start:el.selectionStart, end:el.selectionEnd};
  }

  function restorePricingFocus(box, focus){
    if(!focus || !focus.field) return;
    Array.prototype.some.call(box.querySelectorAll("tr[data-pmodel]"), function(tr){
      if(tr.getAttribute("data-pmodel") !== focus.model) return false;
      var el = tr.querySelector("[data-pfield='" + focus.field + "']" + (focus.period !== null ? "[data-pperiod='" + focus.period + "']" : ""));
      if(el){
        el.focus();
        if(typeof focus.start === "number" && el.setSelectionRange){
          try{ el.setSelectionRange(focus.start, focus.end); }catch(e){}
        }
      }
      return true;
    });
  }

  // 用户 m00153：silent=true 时只把花费与统计换掉，不重画整张表（保存后用）。
  function pricePairHtml(field, value, peak){
    return "<td class='pprice'><div class='ppair'>" +
      "<label><span>普通</span><input class='p" + field + "' data-pfield='" + field + "' type='number' min='0' step='0.001' placeholder='未填' value='" + esc(value) + "'></label>" +
      "<label class='ppeak'><span>高峰</span><input data-pfield='peak." + field + "' type='number' min='0' step='0.001' placeholder='继承' value='" + esc(peak == null ? "" : peak) + "'></label>" +
      "</div></td>";
  }

  // 用户 m00443：缓存命中率 = 命中 token / 输入 token，没输入过就留一个破折号。
  function hitRateHtml(r){
    var inp = Number(r.input) || 0, hit = Number(r.cached) || 0;
    if(!(inp > 0)) return "<span class='pdim'>—</span>";
    var pct = Math.max(0, Math.min(100, hit / inp * 100));
    // 完整数字留在 title：悬停能看出命中多少、整轮 prompt 多大、其中多少没命中。
    return "<span title='缓存 " + esc(fmtNum(hit)) + " / 完整 prompt " + esc(fmtNum(inp)) +
           "（未命中 " + esc(fmtNum(inp - hit)) + " + 缓存 " + esc(fmtNum(hit)) + "）'>" +
           pct.toFixed(1) + "%</span>";
  }


  function partCostHtml(part){
    var c = pricingCost;
    if(!c || !part) return "<span class='pdim'>—</span>";
    if(part.unset) return "<span class='pdim'>未定价</span>";
    var cur = String(c.currency == null ? "" : c.currency);
    // 用户 m17492 图4：≈ 是「这个数不精确」的前缀，写在数字前面（≈0.08元），
    // 写在后面像单位一样跟着，读起来是「0.08 元约」。
    return (part.approximate ? "<span class='pcf-e'>≈</span>" : "") +
           "<span class='pcf-n'>" + esc(priceMoneyText(part.cost)) + "</span>" +
           (cur ? "<span class='pcf-u'>" + esc(cur) + "</span>" : "");
  }

  // 静默刷新（保存价目后只换花费、不重画整张表）时同步这一列，别让顶部总计已经变了、行里还是旧数。
  function paintCostCells(box){
    if(!box) return;
    var parts = Object.create(null);
    if(pricingCost && pricingCost.parts) pricingCost.parts.forEach(function(p){
      if(p && p.model != null) parts[p.model] = p;
    });
    Array.prototype.forEach.call(box.querySelectorAll("tr[data-pmodel]"), function(tr){
      var cell = tr.querySelector("td.pcostc");
      if(!cell) return;
      var part = parts[tr.getAttribute("data-pmodel")] || null;
      cell.innerHTML = partCostHtml(part);
    });
  }

  // ------------------------------------------------------------ 价目页图表
  // 用户 m19357：价目表下面一行两张图——左边「柱 + 折线」（柱 = token 总量，折线 = 花费），
  // 右边饼图（各模型花费占比）。用户 m19436：按日历切日 / 周 / 月，不是「最近 N 天」。
  var PIE_COLORS = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#06b6d4",
                    "#ec4899", "#84cc16", "#f97316", "#14b8a6", "#6366f1", "#eab308"];

  // ------------------------------------------------------------ 价目页：日期小工具
  function pdToday(){ var d = new Date(); return new Date(d.getFullYear(), d.getMonth(), d.getDate()); }
  function pdFmt(d){
    return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2);
  }
  function pdParse(txt){
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(txt || ""));
    if(!m) return null;
    return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  }
  function pdAdd(d, n){ return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n); }
  function pdDays(a, b){ return Math.round((a.getTime() - b.getTime()) / 86400000); }
  function pdShort(txt){
    var d = pdParse(txt);
    return d ? ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2) : "";
  }
  function pdSpanText(start, end){
    var a = pdShort(start), b = pdShort(end);
    if(!a || !b) return "";
    return a === b ? a : (a + " ~ " + b);
  }
  // 用户 m29560 第 8 条：「默认」是最近 N 天的窗口，N 按卡片宽度自适应，最多 10 天，且不跨月份。
  // 一天最多三根柱子、每根至少 14px、天与天之间留 6% 的缝，反推一天至少要 46px。
  function pricingWinDays(){
    if(pricingTrendMode === "custom"){
      var ca = pdParse(pricingCustom.start), cb = pdParse(pricingCustom.end);
      if(ca && cb) return Math.max(1, pdDays(cb, ca) + 1);
    }
    var w = pricingChartW || 900;
    var plotW = Math.max(300, w - 128);
    var days = Math.floor(plotW / 46);
    if(days < 3) days = 3;
    if(days > 10) days = 10;
    return days;
  }
  function pricingWinScrollable(){ return pricingTrendMode === "default"; }
  function pricingWinRange(){
    var today = pdToday();
    if(pricingTrendMode === "custom" && pricingCustom.start && pricingCustom.end){
      return { start: pricingCustom.start, end: pricingCustom.end };
    }
    if(pricingTrendMode === "thisweek" || pricingTrendMode === "lastweek"){
      return pricingQuickRange(pricingTrendMode);
    }
    var days = pricingWinDays();
    var end = pdParse(pricingWinEnd) || today;
    if(end > today) end = today;
    var first = new Date(today.getFullYear(), today.getMonth(), 1);
    var minEnd = pdAdd(first, days - 1);
    if(minEnd > today) minEnd = today;
    if(end < minEnd) end = minEnd;
    var start = pdAdd(end, -(days - 1));
    if(start < first) start = first;
    return { start: pdFmt(start), end: pdFmt(end) };
  }
  // 用户 m29947：区间面板里直接摆左右两个日历（左边上个月、右边这个月），
  // 点一下选起、再点一下选止，然后「应用」。
  function pricingCalMonthHtml(y, mo, s, e, todayTxt, navL, navR){
    var first = new Date(y, mo, 1);
    var lead = (first.getDay() + 6) % 7;
    var dim = new Date(y, mo + 1, 0).getDate();
    var cells = [];
    for(var i = 0; i < lead; i++) cells.push(0);
    for(var dd = 1; dd <= dim; dd++) cells.push(dd);
    while(cells.length % 7) cells.push(0);
    var body = cells.map(function(d){
      if(!d) return "<span class='pcal-d mute'></span>";
      var txt = y + "-" + ("0" + (mo + 1)).slice(-2) + "-" + ("0" + d).slice(-2);
      var cls = "pcal-d";
      if(s && e && txt >= s && txt <= e) cls += " in";
      if(txt === s) cls += " s";
      if(txt === e) cls += " e";
      if(txt === todayTxt) cls += " today";
      return "<button type='button' class='" + cls + "' data-cal-d='" + txt + "'>" + d + "</button>";
    }).join("");
    return "<div class='pcal'>" +
      "<div class='pcal-h'>" + (navL || "<span></span>") +
      "<b>" + y + " 年 " + (mo + 1) + " 月</b>" + (navR || "<span></span>") + "</div>" +
      "<div class='pcal-w'><span>一</span><span>二</span><span>三</span><span>四</span>" +
      "<span>五</span><span>六</span><span>日</span></div>" +
      "<div class='pcal-g'>" + body + "</div>" +
      "</div>";
  }
  function pricingCalPairHtml(){
    var today = pdToday();
    var ym = pricingCal.ym || (today.getFullYear() + "-" + ("0" + (today.getMonth() + 1)).slice(-2));
    var y = Number(ym.slice(0, 4)), mo = Number(ym.slice(5, 7)) - 1;
    var y2 = mo + 1 > 11 ? y + 1 : y, mo2 = (mo + 1) % 12;
    var s = pricingCal.start, e = pricingCal.end, todayTxt = pdFmt(today);
    return "<div class='pcal-pair'>" +
      pricingCalMonthHtml(y, mo, s, e, todayTxt,
        "<button type='button' class='pcal-nav' data-cal-nav='-1'>‹</button>", "<span></span>") +
      pricingCalMonthHtml(y2, mo2, s, e, todayTxt,
        "<span></span>", "<button type='button' class='pcal-nav' data-cal-nav='1'>›</button>") +
      "</div>";
  }
