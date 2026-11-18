  var foldedSids = (function(){
    var a = loadJSON(FOLD_KEY, []), m = {};
    (Array.isArray(a) ? a : []).forEach(function(x){ m[String(x)] = true; });
    return m;
  })();
  function saveFolded(){
    var arr = [];
    Object.keys(foldedSids).forEach(function(k){ if(foldedSids[k]) arr.push(k); });
    saveJSON(FOLD_KEY, arr);
  }
  // 群组折叠（用户 m02038 第 9 条）：分组标题最右侧一个按钮，收起后整组卡片换成
  // 一排固定大小的圆头像，头像外圈用竖条那四档颜色提示运行状态。
  var foldedGroups = (function(){
    var a = loadJSON(GRPFOLD_KEY, []), m = {};
    (Array.isArray(a) ? a : []).forEach(function(x){ m[String(x)] = true; });
    return m;
  })();
  function saveFoldedGroups(){
    var arr = [];
    Object.keys(foldedGroups).forEach(function(k){ if(foldedGroups[k]) arr.push(k); });
    saveJSON(GRPFOLD_KEY, arr);
  }

  // 触发口径的中文名（用户 m15045：概率模式触发曾被写成「攒够条数」，还挂上 n/阈值）。
  // count 之外的三档是概率掷骰/时段曲线/自适应阈值，它们没有「攒够几条」这回事。
  try{ TREND_FOLD = localStorage.getItem("mdp_trend_fold") === "1"; }catch(e){}
  // v4.83（用户 m08855）：刷新时走势图会分三次长出来（缓存里的请求 40 根 → 决策并入 150 根 →
  // 请求并入 227 根），标题「最近 N 次快照」跟着跳三次。现在趋势数据也进缓存（trendreq/trenddec），
  // 首屏直接用上次的完整趋势；缓存没有时先画骨架，等两路都回来再一次性画。
  function resumeAllMuted(){
    var list = (ctxHealth && ctxHealth.muted) || [];
    if(!list.length) return;
    var left = list.length;
    list.forEach(function(sid){
      get(API + "/overrides?sid=" + encodeURIComponent(sid)).then(function(d){
        var ov = (d && d.override) || {};
        delete ov.session_enabled;
        // 不能带 merge=1：merge 会把旧覆盖并回来，刚删掉的 session_enabled 又被加回去
        return post(API + "/overrides?sid=" + encodeURIComponent(sid) +
                    "&values=" + encodeURIComponent(JSON.stringify(ov)));
      }).catch(function(){}).then(function(){
        left -= 1;
        if(left <= 0) load();
      });
    });
  }

  function renderCtxList(){
    var box = document.getElementById("ctx-list");
    if(!ctxSessions.length){ box.innerHTML = '<div class="empty">还没有上下文记录</div>'; lastListSig = ""; return; }
    var groups = { gm: [], dm: [] };
    ctxSessions.forEach(function(s){
      if(s.eff) sEff[s.sid] = s.eff;   // 上下文页自带每个会话的有效配置，直接复用
      if(s.silent_eta) sEta[s.sid] = s.silent_eta;   // 静默/休兜倒计时（用户 m08226 第 2 条）
      sFired[s.sid] = { silent: s.silent_fired_ts || 0, rest: s.rest_fired_ts || 0, hold: s.silent_hold_ts || 0 };
      sStat[s.sid] = {                  // 胶囊要的实时数字：24h 统计 + 计数器（v3.23）
        messages: s.r24_messages || 0,
        replied: s.r24_replied || 0,
        counter: s.counter || 0
      };
      (isDM(s.sid) ? groups.dm : groups.gm).push(s);
    });
    // 每 5 秒轮询一次，数据没变就别重画：一重画，用户刚滚到的位置、
    // 按下的焦点全回原点，看着就像「自己弹回去」（用户 m29158）
    // 签名里必须带上「当前选中会话」和「别名表」：切会话、改名的时候
    // ctxSessions 一个字都没变，只比数据会直接早退，active 高亮和改后的名字
    // 永远落不到 DOM（审查 bug）
    var listSig = JSON.stringify([ctxSid, aliases, ctxSessions, Object.keys(foldedSids).sort(), Object.keys(foldedGroups).sort()]);
    if(listSig === lastListSig) return;
    lastListSig = listSig;
    var listTop = box.scrollTop;
    var html = "";
    [["gm", "群聊"], ["dm", "私聊"]].forEach(function(g){
      var arr = groups[g[0]];
      if(!arr.length) return;
      var gfold = !!foldedGroups[g[0]];
      html += "<div class='grp'><span class='gt'>" + g[1] + "（" + arr.length + "）</span>" +
              "<button class='grpb" + (gfold ? " on" : "") + "' data-grpfold='" + g[0] + "' title='" +
              (gfold ? "展开这一组，看完整卡片" : "收起这一组，只留头像") + "' aria-label='" +
              (gfold ? "展开这一组" : "收起这一组") + "'>" + ICON_CHEV + "</button></div>";
      // 收起后只留头像：固定 38px 圆，排不下自动换行；外圈颜色跟左边竖条同一套判定
      if(gfold){
        html += "<div class='avgrid'>" + arr.map(function(s){
          var lv = stateLvl(s);
          return "<span class='avcell " + lv + "' data-ctx='" + esc(s.sid) + "' title='" +
                 esc(displayName(s.sid) + " · " + stateStripeTitle(s, lv)) + "'>" +
                 avatarHtml(s.sid) + "</span>";
        }).join("") + "</div>";
        return;
      }
      arr.forEach(function(s){
        var on = !(s.eff && s.eff.session_enabled === false);
        // 每个胶囊自己带 data-cfg：原来整块 .cfg 都是可点区域，鼠标划过下面那一片
        // 也会跳去设置页（用户 m31156）。现在只有点中胶囊才跳。
        // cfgChips 每项带 sec（top 首行 / st 状态行）与 on（开关态），这里拆成两行摊平：
        // 首行只放模式、百分比/计数、窗口，最右边是「独立配置/全局配置」；五个两字
        // 状态标签单独一行（用户 m07864 第 3 条）。
        var cfgAll = cfgChips(s.sid);
        var chips = cfgAll.filter(function(x){
          return !(x && typeof x === "object" && x.sec === "st");
        }).map(function(x){ return ctxChipHtml(x, s.sid); }).join("");
        // v3.94（用户 m08398 图9）：带 right 的那颗（决策模式）要贴在最右边、
        // 正好落在首行那颗「全局配置」下面，所以从状态行里单拿出来放到末尾
        // （.chip.rt 的 margin-left:auto 只有在它排最后时才推得动）。
        var stAll = cfgAll.filter(function(x){
          return !!(x && typeof x === "object" && x.sec === "st");
        });
        var chipsSt = stAll.filter(function(x){ return !x.right; })
                           .map(function(x){ return ctxChipHtml(x, s.sid); }).join("");
        var chipsStR = stAll.filter(function(x){ return !!x.right; })
                            .map(function(x){ return ctxChipHtml(x, s.sid); }).join("");
        var own = !!(s.eff && s.eff.overridden);
        chips += "<i class='chip scope" + (own ? "" : " global") + "' data-cfg='" + esc(s.sid) + "' title='" +
                 (own ? "这个会话有自己的配置，点一下去改" : "跟随全局配置，点一下给这个会话单独设") + "'>" +
                 (own ? "独立配置" : "全局配置") + "</i>";
        var fold = !!foldedSids[s.sid];
        html += "<div class='it" + (s.sid === ctxSid ? " active" : "") + (on ? "" : " off") +
                (fold ? " fold" : "") + "' data-ctx='" + esc(s.sid) + "'>";
        // 头像和名字放同一排（用户 m31259）；头像不再单独占一列，下面两行从卡片左边开始
        html += stateStripe(s) + "<div class='nm-row'>" + avatarHtml(s.sid) + "<span class='nm' title='窗口 " + (s.window || 0) + " 条 · 请求 " +
                (s.rounds || 0) + " 轮'>" + esc(displayName(s.sid)) + "</span>" +
                "<button class='icon' data-rename='" + esc(s.sid) + "' title='改个名字' aria-label='改个名字'>" + ICON_PENCIL + "</button>" +
                "<button class='icon danger' data-hide='" + esc(s.sid) + "' title='隐藏或删除这个会话的记录' aria-label='删除会话'>" + ICON_TRASH + "</button>" +
                "<span class='swm" + (on ? " on" : "") + "' data-swm='" + esc(s.sid) +
                "' title='" + (on ? "这个会话的决策已开启，点一下关闭" : "这个会话的决策未开启，点一下开启") +
                "' aria-label='这个会话的决策开关'></span>" +
                "<button class='icon foldb' data-foldsid='" + esc(s.sid) + "' title='" +
                (fold ? "展开这条卡片的运行状态和配置" : "收起运行状态和配置胶囊，按钮位置不动") +
                "' aria-label='" + (fold ? "展开会话卡片" : "收起会话卡片") + "'>" + ICON_CHEV + "</button></div>";
        // 第二行只放运行状态（用户 m30428：窗口条数这类不常驻，状态要单独一行）
        html += "<div class='row2'><span class='st' data-st='" + esc(s.sid) + "'>" + stateTag(s) + "</span></div>";
        html += "<div class='cfg'>" + chips + "</div>";
        if(chipsSt || chipsStR) html += "<div class='cfg st'>" + chipsSt + chipsStR + "</div>";
        html += "</div>";
      });
    });
    box.innerHTML = html;
    if(listTop) box.scrollTop = listTop;
  }

  // 折叠只改显示，不动任何记录；重画靠 lastListSig 清空强制走一遍
  function toggleFoldSid(sid){
    sid = String(sid || "");
    if(!sid) return;
    if(foldedSids[sid]) delete foldedSids[sid]; else foldedSids[sid] = true;
    saveFolded();
    lastListSig = "";
    renderCtxList();
  }
  // 群组折叠：整组收起/展开，同样只改显示
  function toggleFoldGroup(g){
    g = String(g || "");
    if(!g) return;
    if(foldedGroups[g]) delete foldedGroups[g]; else foldedGroups[g] = true;
    saveFoldedGroups();
    lastListSig = "";
    renderCtxList();
  }

  // 每个会话一个独立开关：关掉 = 这个群/私聊插件完全不管（不计数、不进窗口、不触发）。
  // 存的是 overrides 里的 session_enabled；开回来就把这个键删掉（跟随全局默认 true）。
  function setSessionEnabled(sid, on){
    var url = API + "/overrides?sid=" + encodeURIComponent(sid);
    get(url).then(function(d){
      var ov = (d && d.override) || {};
      if(on){ delete ov.session_enabled; } else { ov.session_enabled = false; }
      return post(url + "&values=" + encodeURIComponent(JSON.stringify(ov)));
    }).then(function(){
      ctxSessions.forEach(function(s){
        if(s.sid !== sid) return;
        s.eff = s.eff || {};
        s.eff.session_enabled = !!on;
      });
      if(sEff[sid]) sEff[sid].session_enabled = !!on;
      lastHealthSig = "";
      renderCtxList();
      load();
      if(ctxWin && ctxWin.sid === sid) renderCtxWindow(ctxWin, true);
    }).catch(function(e){ mdAlert("切换失败：" + e.message); });
  }

  // 每轮结果只由该轮持久化字段判断，不从当前会话的 busy/error 推断。
  (function(){
    document.addEventListener("wheel", function(e){
      var el = e.target;
      while(el && el.nodeType === 1 && !(el.classList && el.classList.contains("trend"))) el = el.parentNode;
      if(!el || el.nodeType !== 1 || !el.classList) return;
      // v3.63（用户 m18492 图5）：鼠标在走势图上就不许滚到整页去——滚到头也只停住，不再「滚完柱子接着滚网页」。
      // 真正横向滚的是 .trend-in（.trend 只负责定位裁切线）。
      var inner = el.querySelector(".trend-in");
      if(!inner) return;
      var d = Math.abs(e.deltaY) > Math.abs(e.deltaX) ? e.deltaY : e.deltaX;
      if(d) inner.scrollLeft = inner.scrollLeft + (d > 0 ? 60 : -60);
      e.preventDefault();
    }, { passive: false });
  })();
  function markCards(sid){
    var box = document.getElementById("ctx-window");
    if(!box) return;
    Array.prototype.forEach.call(box.querySelectorAll(".mc"), function(c, ci){
      c.setAttribute("data-ci", ci);
      markBlockBtn(c);
      Array.prototype.forEach.call(c.querySelectorAll(".more"), function(mo, mi){
        mo.setAttribute("data-mi", mi);
        if(!foldOpen[sid + ":" + ci + ":" + mi]) return;
        var holder = mo.parentNode;
        var body = holder ? holder.querySelector(".mono") : null;
        if(body) body.classList.remove("clip");
        mo.textContent = "收起";
      });
    });
    paintBlockBtns();
    fitForwardCards();
  }

  // 转发卡折叠：默认只露前 3 条，第 3 条只留半截，剩下的用底部渐变盖住
  //（用户 m01011 第 2 条）。高度按真实行高量出来，换主题、换字号也不会切歪；
  // 展开后不再限高，重画一次又回到折叠态。
  // v4.02（用户 m11629 图2）：快照详情里的转发卡也要折叠。以前这里只认窗口面板，
  // 详情（#ctx-rounds 里那一行）没人量高度，卡就整段铺开——一张 18 条的转发吃掉半页。
  // v4.42（用户 m24522 图5/图6）：以前只在这里量一次高度。记录详情是「先画进隐藏的 <tr>、
  // 展开后才看得见」的，量的时候 offsetTop 全是 0 → h=0 → 根本没设 maxHeight，一展开就是
  // 10 条全铺开；而 #ctx-rounds 又没接 [data-fwd] 的点击，于是「不折叠 + 点不开」一起出现。
  // 现在：量不出来就先记一笔，等它真的进视口（IntersectionObserver）再量，量准了才取消观察。
  var FWD_IO = (typeof IntersectionObserver === "function") ? new IntersectionObserver(function(es){
    es.forEach(function(en){ if(en.isIntersecting) fitOneForwardCard(en.target); });
  }, {rootMargin:"120px"}) : null;
  function fitOneForwardCard(card){
    if(!card || !card.querySelector) return;
    var body = card.querySelector(".fwd-b");
    if(!body) return;
    var its = body.querySelectorAll(".fwd-it");
      // 用户 m03014 第 1 条：转发一律可点开（图片只有展开后才显示），不再走以前
      // 「3 条以内就撤掉折叠标记」那条路。3 条以内文字本来就全露，只挂 short
      // 让底部渐变别盖住最后一行，上限放开。
      if(its.length <= FWD_HEAD_ROWS){
        card.classList.add("short");
        body.style.maxHeight = "none";
        return;
      }
      card.classList.remove("short");
      var third = its[FWD_HEAD_ROWS - 1];
      var h = (third.offsetTop - its[0].offsetTop) + Math.round(third.offsetHeight / 2);
      if(h > 0){
        body.style.maxHeight = h + "px";
        if(FWD_IO) FWD_IO.unobserve(card);
        return;
      }
      // 还在隐藏容器里：等它进视口再量（这里千万别写 maxHeight=0，否则会一直铺开）
      if(FWD_IO) FWD_IO.observe(card);
  }
  function fitForwardCards(root){
    var box = root || document.getElementById("ctx-window");
    if(!box) return;
    Array.prototype.forEach.call(box.querySelectorAll(".fwd[data-fwd]"), fitOneForwardCard);
  }

  // 转发卡的「点击查看内容」＝弹出大框（用户 m03273）。卡里只有前 3 条预览，
  // 完整条目与图片放在 <template class='fwd-full'> 里，点开时整段搬进弹层——
  // 这样图文顺序跟转发原样一致，也不会像以前那样在原地铺开、把图堆到最后。
  function ensureImgPop(){
    var box = document.getElementById("img-pop");
    if(box) return box;
    box = document.createElement("div");
    box.id = "img-pop";
    box.className = "img-pop";
    box.hidden = true;
    box.innerHTML = "<div class='ip-mask' data-imgpop='close'></div>" +
      "<div class='ip-box'><img class='ip-img' alt='图片'>" +
      "<div class='ip-note'>图片加载失败</div>" +
      "<button class='ip-x' type='button' data-imgpop='close' title='关闭' aria-label='关闭'>" +
        "<svg viewBox='0 0 24 24' width='13' height='13' aria-hidden='true'><path d='M6 6l12 12M18 6L6 18' " +
        "stroke='currentColor' stroke-width='2.2' stroke-linecap='round'/></svg></button></div>" +
      "<div class='ip-bar' hidden>" +
        "<button class='ip-btn' type='button' data-ipzoom='out' title='缩小（滚轮向下 / 减号）' aria-label='缩小'>−</button>" +
        "<span class='ip-zoom' title='当前显示比例'>自适应</span>" +
        "<button class='ip-btn' type='button' data-ipzoom='in' title='放大（滚轮向上 / 加号）' aria-label='放大'>+</button>" +
        "<span class='ip-sep'></span>" +
        "<button class='ip-btn ip-btn-t' type='button' data-ipzoom='one' title='按原图像素 1:1 显示'>1:1</button>" +
        "<button class='ip-btn ip-btn-t' type='button' data-ipzoom='fit' title='缩回自适应屏幕'>自适应</button>" +
      "</div>";
    document.body.appendChild(box);
    var im = box.querySelector(".ip-img");
    var bar = box.querySelector(".ip-bar");
    im.addEventListener("error", function(){
      im.style.display = "none";
      box.querySelector(".ip-note").style.display = "block";
      bar.hidden = true;
    });
    im.addEventListener("load", function(){
      if(!IP.pending) return;
      IP.pending = false;
      bar.hidden = false;
      ipMeasure();
      IP.zoom = 1; IP.tx = 0; IP.ty = 0;
      ipApply();
    });
    // 点大图本身也能关；放大之后不关，免得想拖动时误关
    im.addEventListener("click", function(){
      if(IP.moved){ IP.moved = false; return; }
      if(Math.abs(IP.zoom - 1) > 0.001) return;
      closeImgPop();
    });
    // 双击：放大状态回到自适应
    im.addEventListener("dblclick", function(ev){
      if(Math.abs(IP.zoom - 1) < 0.001) return;
      ev.preventDefault();
      ipReset();
    });
    // 滚轮缩放：以鼠标位置为锚点
    box.addEventListener("wheel", function(ev){
      if(box.hidden) return;
      ev.preventDefault();
      var d = ev.deltaY || 0;
      var step = Math.abs(d) < 40 ? (d > 0 ? 1 / 1.06 : 1.06) : (d > 0 ? 1 / 1.18 : 1.18);
      ipZoomAt(IP.zoom * step, ev.clientX, ev.clientY);
    }, { passive: false });
    // 放大后按住拖动平移
    box.addEventListener("mousedown", function(ev){
      if(box.hidden || ev.button !== 0) return;
      var t = ev.target;
      if(t && t.closest && (t.closest("[data-imgpop]") || t.closest(".ip-bar"))) return;
      if(Math.abs(IP.zoom - 1) < 0.001) return;
      IP.drag = { x: ev.clientX, y: ev.clientY, tx: IP.tx, ty: IP.ty };
      IP.moved = false;
      box.classList.add("dragging");
      ev.preventDefault();
    });
    box.addEventListener("click", function(ev){
      var t = ev.target;
      var zb = t && t.closest ? t.closest("[data-ipzoom]") : null;
      if(zb){
        ev.preventDefault();
        ev.stopPropagation();
        var act = zb.getAttribute("data-ipzoom");
        var cx = window.innerWidth / 2, cy = window.innerHeight / 2;
        if(act === "in") ipZoomAt(IP.zoom * 1.25, cx, cy);
        else if(act === "out") ipZoomAt(IP.zoom / 1.25, cx, cy);
        else if(act === "one") ipZoomAt(IP.one, cx, cy);
        else ipReset();
        return;
      }
      if(t && t.closest && t.closest("[data-imgpop]")) closeImgPop();
      else if(t && t.closest && (t.closest(".ip-box") || t.closest(".ip-bar"))) return;
      else closeImgPop();
    });
    return box;
  }
  document.addEventListener("mousemove", function(ev){
    if(!IP.drag) return;
    var dx = ev.clientX - IP.drag.x, dy = ev.clientY - IP.drag.y;
    if(Math.abs(dx) > 3 || Math.abs(dy) > 3) IP.moved = true;
    IP.tx = IP.drag.tx + dx;
    IP.ty = IP.drag.ty + dy;
    ipApply();
  });
  document.addEventListener("mouseup", function(){
    if(!IP.drag) return;
    IP.drag = null;
    var box = ipBox();
    if(box) box.classList.remove("dragging");
  });
  // 大图打开时的缩放快捷键：+/- 缩放、0 自适应、1 按原像素
  document.addEventListener("keydown", function(e){
    var box = ipBox();
    if(!box || box.hidden) return;
    var k = e.key;
    var cx = window.innerWidth / 2, cy = window.innerHeight / 2;
    if(k === "+" || k === "="){ e.preventDefault(); ipZoomAt(IP.zoom * 1.25, cx, cy); }
    else if(k === "-" || k === "_"){ e.preventDefault(); ipZoomAt(IP.zoom / 1.25, cx, cy); }
    else if(k === "0"){ e.preventDefault(); ipReset(); }
    else if(k === "1"){ e.preventDefault(); ipZoomAt(IP.one, cx, cy); }
  }, true);
  function openImgPop(src, title){
    if(!src) return;
    var box = ensureImgPop();
    var im = box.querySelector(".ip-img");
    var note = box.querySelector(".ip-note");
    var bar = box.querySelector(".ip-bar");
    im.style.display = "";
    note.style.display = "none";
    im.style.transform = "";
    IP.zoom = 1; IP.tx = 0; IP.ty = 0; IP.drag = null; IP.moved = false;
    IP.pending = true;
    box.classList.remove("zoomed", "dragging");
    bar.hidden = true;
    im.src = src;
    im.alt = title || "图片";
    im.title = title || "";
    box.hidden = false;
    document.body.classList.add("img-pop-open");
    // 图已经在缓存里时 load 不会再触发，这里补一次
    if(im.complete && im.naturalWidth){
      IP.pending = false;
      bar.hidden = false;
      ipMeasure();
      ipApply();
    }
  }
  function closeImgPop(){
    var box = document.getElementById("img-pop");
    if(!box || box.hidden) return;
    box.hidden = true;
    box.classList.remove("zoomed", "dragging");
    IP.drag = null; IP.moved = false; IP.pending = false;
    IP.zoom = 1; IP.tx = 0; IP.ty = 0;
    var im = box.querySelector(".ip-img");
    if(im){ im.style.transform = ""; im.removeAttribute("src"); }   // 关掉就把图卸掉，不留在页面上
    var bar = box.querySelector(".ip-bar");
    if(bar) bar.hidden = true;
    document.body.classList.remove("img-pop-open");
  }
  // 捕获阶段拦缩略图的点击：先于卡片自己的点击逻辑（转发弹层、拉黑等）生效
  document.addEventListener("click", function(ev){
    var t = ev.target;
    var im = t && t.closest ? t.closest("[data-img]") : null;
    if(!im) return;
    var src = im.getAttribute("data-img") || "";
    if(!src) return;
    ev.preventDefault();
    ev.stopPropagation();
    openImgPop(src, im.getAttribute("data-imgt") || "");
  }, true);
  // 键盘可达：缩略图是 role=button + tabindex=0，回车/空格等于点它
  document.addEventListener("keydown", function(ev){
    if(ev.key !== "Enter" && ev.key !== " " && ev.key !== "Spacebar") return;
    var t = ev.target;
    var im = t && t.closest ? t.closest("[data-img]") : null;
    if(!im) return;
    ev.preventDefault();
    openImgPop(im.getAttribute("data-img") || "", im.getAttribute("data-imgt") || "");
  }, true);
  // Esc：先关大图，再关转发弹层（弹层打开时不编辑输入，直接收掉，不抢别处的键盘）
  document.addEventListener("keydown", function(e){
    if(e.key !== "Escape") return;
    var ib = document.getElementById("img-pop");
    if(ib && !ib.hidden){ e.preventDefault(); closeImgPop(); return; }
    var box = document.getElementById("fwd-pop");
    if(box && !box.hidden){ e.preventDefault(); closeFwdPop(); }
  }, true);

  // ------------------------------------------------------------ 窗口拉黑短语（v3.36）
  // 用户第 13 条：鼠标移到一条消息上才出现「拉黑」，点了把整条消息塞进这个群的
  // 「不触发短语」清单——和设置页里那张表是同一份，走单群覆盖，不影响别的群。
  // 后端比对口径是「整条消息归一化后完全等于清单里的词」（decision/engine.py:1960
  // _phrase_hit + :39 _norm_phrase），所以这里的 blkNorm 必须跟 :36 的 _PHRASE_STRIP
  // 用同一套字符，不然「已拉黑」会显示不出来。
  function setView(v){
    VIEW = v;
    try{ localStorage.setItem("mdp_view", v); }catch(e){}
    paintViewSw();
    // 先把「哪些行是展开的」记下来：重画以后 DOM 全换新的
    var opened = [];
    var foldKeep = {};
    Array.prototype.forEach.call(document.querySelectorAll("#ctx-rounds tr.round-detail"), function(tr){
      if(tr.style.display !== "none") opened.push(tr.getAttribute("data-detail"));
      // 行内 ①②③④ 哪几块是摊开的，也一起记下来。记的是「第几块」而不是块名：
      // 美化档叫 sys/hist/final/trace，原文档叫 raw1~raw4，按名字跨档对不上
      var nm = tr.getAttribute("data-detail");
      var idxs = [];
      Array.prototype.forEach.call(tr.querySelectorAll("[data-foldbody]"), function(el, i){
        if(el.style.display !== "none") idxs.push(i);
      });
      if(nm && idxs.length) foldKeep[nm] = idxs;
    });
    restoreFolds = foldKeep;
    // 窗口块必须强制重画：renderCtxWindow 有数据指纹，数据没变就直接 return，
    // 切档位时它会认为「什么都没变」而不动，页面就留着一半旧档的卡片（用户 m33271 图3）
    // 停在窗口页才需要重画窗口；停在请求页时窗口面板本来就只有标题行（atRounds 分支输出空），
    // 重画它等于白跑一遍布局。切回窗口时 force=true 会自己重画。
    if(ctxPane === "window") grabWinAnchor();
    if(ctxWin && ctxPane === "window") renderCtxWindow(ctxWin, true);
    // v3.94（用户 m08398 图7）：决策记录和回复记录共用 #ctx-rounds 这一张表，
    // 但上面那个 renderCtxRounds 在决策页会直接 return（各画各的），于是下面那句
    // toggleRequestRow 反倒把本来就摊开的行又收了回去——切个档位，展开的详情就没了。
    // 决策页单独走一遍：整表重建（内容按新档位重画）再把它展开回去。
    if(ctxPane === "decisions"){
      if(ctxRounds && ctxRounds.length) renderCtxDecisions(ctxDecisions, true);
      // preserveDetails 会把老的那一行原样搬回来（还摊开着、还带着 data-loaded），
      // 所以得先统一收起来再清空——否则下面那遍 toggle 会把「已经打开的行」又关掉。
      Array.prototype.forEach.call(document.querySelectorAll("#ctx-rounds tr.round-detail"), function(tr){
        tr.style.display = "none";
        tr.removeAttribute("data-loaded");
        // v3.97（用户 m09615 第 7 条）：这里不再先写一句「加载中…」，展开时直接用
        // 内存里的记录同步重画，切档就没有那一下闪。
        tr.innerHTML = "";
      });
      Array.prototype.forEach.call(document.querySelectorAll("#ctx-rounds tr.round-row"), function(tr){
        tr.classList.remove("open");
      });
      opened.forEach(function(nm){ toggleRequestRow(nm); });
      return;
    }
    // 请求表整张重建，再按名字把刚才展开的行重新展开——比逐行 removeAttribute
    // 稳，逐行改的时候异步回调回来的顺序会把档位又搅混
    if(ctxRounds && ctxRounds.length && (ctxPane !== "rounds" || ctxRoundsFull)) renderCtxRounds(ctxRounds);
    opened.forEach(function(nm){ toggleRequestRow(nm); });
    if(ctxPane === "window") fadeWinIn();
  }

  // 切档后给新铺进去的内容一层极短淡入，标题行不动（它是静态骨架）
  function fillQuoteMiss(){
    var box = document.body;
    if(!box || !QUOTE_SID || quoteFarBusy) return;
    var nodes = [].slice.call(box.querySelectorAll(".quote[data-qmiss], .quote[data-qpic]"));
    var ids = [], seen = Object.create(null), sid = QUOTE_SID;
    function keyOf(n){ return String(n.getAttribute("data-qmiss") || n.getAttribute("data-qpic") || ""); }
    function replaceNode(n, k, hit){
      if(!document.contains(n) || !n.parentNode) return false;
      // 用户 m21787 图4：后端只按 message_id 认回了图（source=media_index），没有正文/昵称。
      // 这种时候别整块替换——原节点已经按引用里那行原文画好了名字和原话，只把图补进去。
      if(!hit.body){
        var _th = thumbsHtml(hit.media || []), _who = n.querySelector(".qwho");
        if(_th && _who){
          var _ph = n.querySelector(".qpic-ph");
          if(_ph) _ph.parentNode.removeChild(_ph);
          _who.insertAdjacentHTML("afterend", _th);
          n.removeAttribute("data-qpic"); n.removeAttribute("data-qmiss");
          fitForwardCards(n);
          return true;
        }
      }
      var q = { id: k, text: hit.body, who: hit.name || hit.who, ts: hit.ts, display: hit.display };
      var html = quoteHtml(q, []);
      if(!html) return false;
      n.insertAdjacentHTML("beforebegin", html);
      var added = n.previousElementSibling;
      n.parentNode.removeChild(n);
      // 异步补引用替换进来的转发卡也要重新量高度，否则会整段铺开。
      if(added) fitForwardCards(added);
      return true;
    }
    nodes.forEach(function(n){
      var k = keyOf(n), cached = QUOTE_IDX[k];
      if(!displayOf(cached).known) cached = QUOTE_FAR[k];
      if(displayOf(cached).known){ replaceNode(n, k, cached); return; }
      if(k && !seen[k] && (QUOTE_PIC_TRIED[k] || 0) < 3){ seen[k] = 1; ids.push(k); }
    });
    if(!ids.length) return;
    ids = ids.slice(0, 12);
    ids.forEach(function(k){ QUOTE_PIC_TRIED[k] = (QUOTE_PIC_TRIED[k] || 0) + 1; });
    quoteFarBusy = true;
    function retry(){
      quoteFarBusy = false;
      if(QUOTE_SID === sid) setTimeout(fillQuoteMiss, 150);
    }
    fetch(API + "/quote-lookup?sid=" + encodeURIComponent(sid) +
          "&ids=" + encodeURIComponent(ids.join(",")), { cache: "no-store" })
      .then(function(r){ return r.ok ? r.json() : null; })
      .then(function(j){
        quoteFarBusy = false;
        if(QUOTE_SID !== sid) return;
        var items = (j && j.items) || {}, entries = Object.create(null);
        ids.forEach(function(k){
          if(!items[k]) return;
          var hit = quoteEntryOf(items[k], items[k].body);
          entries[k] = hit;
          // unknown 不固化成确定无图，下次窗口 GET 仍可重试。
          if(displayOf(hit).known) QUOTE_FAR[k] = hit;
        });
        // 查询期间 DOM 可能被轮询替换，重新找当前节点；空回查保留标记。
        [].slice.call(box.querySelectorAll(".quote[data-qmiss], .quote[data-qpic]")).forEach(function(n){
          var k = keyOf(n);
          if(entries[k]) replaceNode(n, k, entries[k]);
        });
        retry();
      }).catch(retry);
  }
  // 排查入口：控制台 / 自动化里手动补一次引用块（正常流程由 swapWinBody 调用）
  window.__mdpQuoteRepair = fillQuoteMiss;
  // 用户 m12814：点引用块跳到被引用的那条消息。
  // 当前视图里画着就滚过去闪一下；没画着就问后端它在哪一轮快照里，打开那一轮再滚过去。
  function midNodeOf(mid){
    var box = document.getElementById("ctx-window");
    if(!box || !mid) return null;
    var nodes = box.querySelectorAll(".mc[data-mid]");
    for(var i = 0; i < nodes.length; i++){
      var ids = String(nodes[i].getAttribute("data-mid") || "").split(/\s+/);
      if(ids.indexOf(mid) >= 0) return nodes[i];
    }
    return null;
  }
  function flashMid(node){
    if(!node) return;
    try{ node.scrollIntoView({ block: "center", behavior: "smooth" }); }
    catch(e){ node.scrollIntoView(); }
    node.classList.remove("mid-flash");
    void node.offsetWidth;   // 重排一次，动画才能重放
    node.classList.add("mid-flash");
    setTimeout(function(){ node.classList.remove("mid-flash"); }, 1700);
  }
  function waitMid(mid, tries, done){
    var node = midNodeOf(mid);
    if(node || tries <= 0){ done(node); return; }
    setTimeout(function(){ waitMid(mid, tries - 1, done); }, 80);
  }
  function jumpToQuote(mid, el){
    mid = String(mid || "");
    if(!mid) return;
    var here = midNodeOf(mid);
    if(here){ flashMid(here); return; }
    if(!ctxSid) return;
    // 回查要扫几百份快照，先把引用块压暗一点，别让人以为点了没反应。
    function busy(on){ if(el && el.classList) el.classList[on ? "add" : "remove"]("qjump-busy"); }
    busy(true);
    get(API + "/quote-where?sid=" + encodeURIComponent(ctxSid) + "&mid=" + encodeURIComponent(mid))
      .then(function(d){
        if(!d || !d.ok){ busy(false); mdAlert("找不到这条引用在哪一轮快照里"); return; }
        if(!d.found){ busy(false); mdAlert("没找到这条引用在哪一轮快照里（可能已经被清理）"); return; }
        if(ctxSnap && ctxSnap.name === d.name){
          busy(false);
          mdAlert("这一轮快照里没有画出这条消息");
          return;
        }
        openRoundByName(d.name, null);
        waitMid(mid, 40, function(node){
          busy(false);
          if(node) flashMid(node);
          else mdAlert("打开了那一轮快照，但没找到这条消息");
        });
      })
      .catch(function(){ busy(false); mdAlert("查询引用位置失败，看后台日志"); });
  }
  // 一条 user 里可能拼了好几条群消息（parts 多段），图属于「那一条」自己的正文占位。
  // 被引用时要画的是「被引用那条」的图（用户 m12256），所以这里按占位顺序把媒体切回各段。
  function ublkLoad(sid){
    var now = Date.now();
    if(sid === ublkSid && (now - ublkAt) < 30000) return;
    ublkSid = sid; ublkAt = now;
    // 用户 m07715：带上 sid，后端会把「本会话已解除」的人从 items 里剔掉，窗口页跟着不折叠
    get(API + "/user-blacklist?sid=" + encodeURIComponent(sid || "")).then(function(d){
      if(!d || !d.ok) return;
      if(ublkFill(d) && ctxWin && ctxPane === "window") renderCtxWindow(ctxWin, true);
    }).catch(function(){});
  }
  function ublkSet(uid, name, action){
    return post(API + "/user-blacklist?sid=" + encodeURIComponent(ublkSid || "") +
                "&uid=" + encodeURIComponent(uid) +
                "&name=" + encodeURIComponent(name || "") +
                "&action=" + encodeURIComponent(action)).then(function(d){
      if(!d || !d.ok) return d;
      if(ublkFill(d) && ctxWin && ctxPane === "window") renderCtxWindow(ctxWin, true);
      return d;
    });
  }
  // tools：这一段用了几次工具，紧跟在名字后面（用户 m17212 图1）。组头在整组还没读完时
  // 就得先画出来，所以调用方传 "\u0001" 占位，flush 时再换成真实次数（0 次就整块删掉）。
  function exitSnap(){
    var ry = ctxSnapReturnY;
    ctxSnapReturnY = null;
    winAnchor = null;
    ctxSnap = null;
    if(ctxWin) renderCtxWindow(ctxWin, true);
    if(ry != null) restoreWinScroll(ry);
  }

  // 右栏 #ctx-window 的标题行（sec-hd）现在是 HTML 静态骨架，重画时只换它之后的内容。
  // 别再整块 innerHTML：标题行会消失一帧，下面的内容跟着上下跳（用户 m39759 图1）。
  // 骨架万一不存在（老缓存），退回整块重建，行为跟以前一样。
  function swapWinBody(rest){
    var box = document.getElementById("ctx-window");
    if(!box) return null;
    var hd = box.querySelector(".sec-hd");
    var keepX = trendScrollX(box);
    if(!hd){
      box.innerHTML = SECHD_HTML + (rest || "");
      restoreTrendScroll(box, keepX);
      revealTrendSel(box.querySelector(".trend"));
      fillQuoteMiss();
      fillTweets();
      return box;
    }
    var node = hd.nextSibling;
    while(node){ var nx = node.nextSibling; box.removeChild(node); node = nx; }
    if(rest) box.insertAdjacentHTML("beforeend", rest);
    restoreTrendScroll(box, keepX);
    revealTrendSel(box.querySelector(".trend"));
    fillQuoteMiss();
    fillTweets();
    return box;
  }

  // 用户 m11091 第 8 条：滚动期间的「先别动 DOM」开关。
  // 被动监听 + 220ms 尾巴：滚轮惯性、拖动滚动条都算在内。
  function drainWinDefer(){
    deferTimer = 0;
    if(isScrolling()){ deferTimer = setTimeout(drainWinDefer, 240); return; }
    var q = deferWin;
    deferWin = null;
    if(q){ try{ renderCtxWindow(q.d, q.force); }catch(e){} }
  }
  window.addEventListener("scroll", function(){
    scrollingUntil = Date.now() + 220;
    if(!deferTimer) deferTimer = setTimeout(drainWinDefer, 240);
  }, { passive: true });

  // v4.01（用户 m11166 图1）：快照态里的决策注记是普通 assistant 消息——后端存的是
  // name=对当前聊天消息的决策建议、正文带「[决策记录] HH:MM」前缀，没有 decnote 标记。
  // 不认出来就会画成 bot 自己的话（名字是 bot 名、正文留着前缀、也没有注记那条样式）。
  function renderCtxWindow(d, force){
    var box = document.getElementById("ctx-window");
    var roundsBox = document.getElementById("ctx-rounds");
    if(roundsBox) roundsBox.style.display = (ctxPane === "rounds" || ctxPane === "decisions") ? "" : "none";
    // 请求页把上面那张卡和下面的表格贴成一块：两块各自圆角会留一道缝，
    // 看起来像两个不相干的盒子（用户 m30252 图1）
    // v3.94（用户 m08398 图10）：决策记录页也是「标题行 + 表格」一整块，跟 setCtxPane 一样
    // 用 ctxPane !== "window" 判断；以前只认 rounds，切到决策页时表格自己一个圆角，
    // 看着像两块分离的面板。
    if(box) box.classList.toggle("linked", ctxPane !== "window");
    // 用户 m14026 第 4 条：窗口主体的显隐跟着页签走，放在入口处——下面几条早退（没数据 /
    // 数据没变）都不会再把它漏掉，记录页也不会被一条「选一个会话」的占位顶下去。
    if(box) box.classList.toggle("win-off", ctxPane !== "window");
    if(roundsBox) roundsBox.classList.toggle("linked", ctxPane !== "window");
    if(!d || !d.ok){ swapWinBody("<div class='empty'>选一个会话</div>"); lastWinSig = ""; return; }
    // bot 自己的头像要用设置里填的 QQ 号（框架不提供），请求页/窗口页共用这一个全局值
    window.__BOT_QQ = String(d.bot_qq || "");
    var msgs = (d.messages || []).map(normDecNote);
    var pend = d.pending || [];
    var sm = d.summary || {};
    // 快照模式：用那一轮请求里存下的 messages 顶替实时窗口
    if(ctxSnap){
      msgs = ctxSnap.messages || [];
      pend = [];
      sm = {};
    }
    var smTip = "";
    if(sm.enabled === false && sm.chars){
      smTip = " · 摘要 " + sm.chars + " 字（开关已关，不会发出去）";
    } else if(sm.chars){
      smTip = " · 摘要 " + sm.chars + " 字";
    }
    var fromLast = (d.source === "last_request");
    // v3.15（用户 m40335 / m40353）：热重载或重启后内存里的窗口是空的，后端会拿最近一轮
    // 请求的历史顶上（source=last_request）。那份历史只是存档——铺在面板里会跟新进来的
    // 待发消息接成一串、还画不出绿柱。所以回显态只显示新进入的消息，历史不再展开。
    var echo = fromLast && !ctxSnap;
    var live = !fromLast;   // 快照时实时窗口仍然存在，绿柱要留着
    if(echo) msgs = [];
    // 主数字用「真实消息条数」：一条 user 里可能装着十几条群消息，
    // 只报数组长度会让 192 条显示成 20 条，用户根本看不出窗口已经爆了。
    var realCount = (typeof d.counted === "number" && d.counted > 0) ? d.counted : (d.size || 0);
    if(echo) realCount = 0;
    // v3.16（用户 m40583）：「当前 N 条」只算用户侧 + 待发，bot 自己的回复不占位数。
    // 以前用 counted（含 assistant 回复），既多算了 bot 的、又漏了待发，
    // 用户对着窗口数出来的数对不上。
    var userCount = (typeof d.user_counted === "number") ? d.user_counted : realCount;
    if(echo) userCount = 0;
    // 待发也算进窗口：它们下一轮就会真的发出去，绿柱子不该因为是 0 条历史就消失
    var pendCount = (typeof d.pending_counted === "number") ? d.pending_counted : pend.length;
    // 绿柱代表「此刻实时窗口」，看快照时它也在（只是不高亮），
    // 不然点一下柱子绿柱就没了，像窗口被清空了（用户 m29575）
    var trendCount = userCount + pendCount;
    // 「段」= messages 数组里有几项（框架把一批群消息拼成一条 user）
    var segCount = live ? (d.size || 0) : msgs.length;
    var histCount = ctxSnap ? (typeof ctxSnap.counted === "number" ? ctxSnap.counted : msgs.length) : (realCount || segCount);
    // 快照里的 bot 条数：新记录带 user_counted，旧记录退化成数 assistant 条数
    var snapBot = ctxSnap
      ? (typeof ctxSnap.user_counted === "number"
          ? Math.max(0, histCount - ctxSnap.user_counted)
          : msgs.filter(function(m){ return m.role === "assistant"; }).length)
      : 0;
    // 快照标题同样内联拆分（用户 m45753），旧记录没有 user_counted 时按 assistant 条数倒推用户数
    var snapUser = Math.max(0, histCount - snapBot);
    // v3.89（用户 m05321 第 1 条）：裁切快照里含待发，标题上直接写出来
    var snapPend = (ctxSnap && ctxSnap.pending_n) ? (Number(ctxSnap.pending_n) || 0) : 0;
    var snapTag = "[用户 " + snapUser + " 条 · bot " + snapBot + " 条" +
                  (snapPend ? " · 其中待发 " + snapPend + " 条" : "") + "]";
    var srcTip = ctxSnap ? " · " + fmtTime(ctxSnap.ts) + " 本轮输入及实际回复（非实时）"
               : (echo ? "" : (fromLast ? " · 取自最近一轮请求" : ""));
    var cntTip = (!ctxSnap && segCount && realCount && realCount !== segCount)
      ? "（" + segCount + " 段）" : "";
    // v3.93（用户 m07570 图6）：非窗口页（回复记录 / 决策记录）一律把窗口主体清掉，
    // 只留上面那行标题（页签 + 三档还要在这儿点）。以前只判 rounds，切到决策记录
    // 页时整块走势图 + 判定横幅 + 消息列表还铺在上面，把表格挤到了最底下。
    var atRounds = (ctxPane !== "window");
    // 用户 m14026 第 4 条：切到记录页时不再清空窗口主体，只加一个 class 藏起来；
    // 切回来时数据没变就直接显示，省掉整块重建（300+ 根柱子 + 几十条消息）。
    // （入口处已经按页签切过 win-off，这里不再重复。）
    // 数据没变就别重画——每 5 秒自动刷新一次，一重画滚动位置和「展开全文」全回原点
    // 用户 m14026 第 4 条：指纹里不再放 ctxPane——窗口主体跟当前页签无关（页签只决定显示/隐藏），
    // 放进指纹会让每次切页都判成「数据变了」，切回窗口就得整块重画。
    var sig = JSON.stringify([VIEW, d.sid, d.size, d.keep, d.threshold, d.enabled,
                              (ctxDecisions[0] || {}).name || "",
                              (d.last_decision || {}).ts || 0,
                              d.counted, d.user_counted, d.pending_counted, d.source, d.last_ts, d.bot_qq,
                              msgs, pend, sm, ctxSnap]);
    // v3.99（用户 m10577）：指纹要带上决策轮。窗口消息没变时这个函数会早返回，
    // 而决策记录是另一次请求（比窗口晚一点点到），不写进指纹就永远是「还没到」的那一版。
    var trendSig = JSON.stringify([ctxTrendReq, ctxTrendDec, ctxTrendRounds, ctxTrims,
                                   ctxDecTrend.map(function(r){ return r.name; })]);
    // 每次窗口 GET 都重新给 unknown 有限回查机会，指纹不变也不能永久丢图。
    QUOTE_PIC_TRIED = Object.create(null);
    if(!force && sig === lastWinSig && trendSig === lastTrendSig){ fillQuoteMiss(); return; }
    // 用户 m11091 第 8 条：正在滚动就先别重画。5 秒轮询一落地就整块 innerHTML
    // 重建（70 条消息几百个节点），恰好压在滚轮/拖动滚动条那一帧上就会卡一下；
    // 数据不丢，等滚动停下再画。
    if(!force && isScrolling()){ deferWin = { d: d, force: false }; return; }
    var bodyUnchanged = !force && sig === lastWinSig;
    // 用户 m02993 图10：引用只有 id 时要回查窗口，索引跟着这批消息一起换。
    if(!bodyUnchanged) QUOTE_IDX = buildQuoteIdx(msgs, pend);
    var _newSid = String(d.sid || "");
    if(_newSid !== QUOTE_SID){
      // 换了会话：上一会话回查到的原话、以及「问过但没图」的记录都不能再用
      QUOTE_FAR = {};
      QUOTE_PIC_TRIED = Object.create(null);
    }
    QUOTE_SID = _newSid;
    // 用户 m14026 第 4 条：记下这一版之前画进 DOM 的是哪一版，记录页那边据此判断要不要作废。
    var prevWinSig = lastWinSig, prevTrendSig = lastTrendSig;
    lastWinSig = sig;
    lastTrendSig = trendSig;
    var oldScroll = box.querySelector(".scroll");
    var keepTop = oldScroll ? oldScroll.scrollTop : 0;
    // 窗口页是整页滚（.scroll 不限高）。重画时 box.innerHTML 一换，
    // 文档高度瞬间掉到 0，浏览器把 scrollY 夹成 0 —— 表现出来就是
    // 「看着看着自己弹回顶部」（用户 m29158）。重画完放回去。
    var keepWinY = window.scrollY || window.pageYOffset || 0;
    // 快照态不再用 min-height 顶高度（用户 m42220：底部会留一大片空白）。
    // 滚动位置改由 winAnchor 换算，内容短也不会被夹回顶部。
    if(box.style.minHeight) box.style.minHeight = "";
    // 标题行是静态骨架，这里只把两页切换与三档显示的选中态刷对（用户 m28965 把显示开关放在这里）
    var hd = box.querySelector(".sec-hd");
    if(hd){
      Array.prototype.forEach.call(hd.querySelectorAll("[data-pane]"), function(b){
        b.classList.toggle("on", b.getAttribute("data-pane") === ctxPane);
      });
      Array.prototype.forEach.call(hd.querySelectorAll("[data-view]"), function(b){
        b.classList.toggle("on", b.getAttribute("data-view") === VIEW);
      });
    }
    // 标题行只留「两页切换 + 三档显示」；保留条数这类信息挪到走势图标题右边（用户 m30009 图3）
    // 「当前 N 条」的口径（用户 m40583）：窗口里的用户消息 + 待发，不含 bot 自己的回复。
    // 明细放 title，鼠标悬停能对上账。
    // v3.22（用户 m42531）：窗口标题报的是「窗口里实际有多少条」——含 bot 的回复，
    // 后面标一句 [包含bot消息]；走势柱那边只数用户消息，两边口径各写各的。
    var winTotal = realCount + pendCount;
    var botCount = Math.max(0, realCount - userCount);
    // v3.23（用户 m45720 / m45753）：把口径直接摊在标题里，不靠悬停——
    // 「当前 75 条 [用户 55 条 · bot 11 条 · 待发 9 条]」。bot 条数 = 总条数 - 用户条数，
    // 与走势柱口径一致；待发为 0 时整段省掉，别写「待发 0 条」。
    var cntTag = "[用户 " + userCount + " 条 · bot " + botCount + " 条" +
                 (pendCount ? " · 待发 " + pendCount + " 条" : "") + "]";
    // 用户 m04471 第 3 条：出错那一轮的快照标题也要写清楚「重试几次、成没成」。
    var sri = snapRetryInfo(ctxSnap);
    var snapRetry = sri.retry;
    var snapSent = sri.sent;
    // 用户 m00942 第 4 条：这排文字只留「本轮快照 N 条 [用户…· bot…]」和编号，
    // 后面那串「· 历史 68 · 本轮输入 3 · 实际回复 0 · 空回复」全删——数字对不上账时
    // 反而更糊涂。编号点一下复制，方便贴到别处对账。
    var winInfo = atRounds ? "" : ctxSnap ?
      "<span class='hint'>本轮快照 " + histCount + " 条 " + snapTag +
        (ctxSnap.snap_id
          ? " <span class='snap-id' data-snap-id='" + esc(ctxSnap.snap_id) + "' title='点击复制快照编号'>" + esc(ctxSnap.snap_id) + "</span>"
          : "") +
        "</span>" :
      // 口径、来源这些都写在行内了，标题不再挂 title（用户 m45753：尽量不要用悬停）
      "<span class='hint'>" +
      (d.enabled ? "" : "窗口已关闭 · ") +
      // 待发条数统一用后端 pending_counted：pend 是前端渲染的「组」，一批群消息会并成一组，
      // 拿组数当条数会和明细里的待发数对不上账（线上实测 8 组 vs 5 条）
      (echo ? ("窗口已重置" + (pendCount ? " · 待发 " + pendCount + " 条" : ""))
            : ("保留 " + d.keep + " · 阈值 " + d.threshold + " · 当前 " + winTotal + " 条 " + cntTag)) +
      "</span>";
    var html = "";
    if(atRounds){
      // 主体已经由 .win-off 藏起来，这里不能再清空——清了切回来就得整块重画。
      // 指纹只在「这版数据确实没画进 DOM」时作废——数据没变就留着，切回来只显示、不重画。
      if(prevWinSig !== sig) lastWinSig = "";
      if(prevTrendSig !== trendSig) lastTrendSig = "";
      box.onclick = winClick;
      restoreWinScroll(keepWinY);
      return;
    }
    // v3.99（用户 m10577）：决策轮也画进走势图。它和请求轮共用一条时间轴，
    // 只是柱子颜色不同、点开看到的是那一轮的决策快照。
    var trendRows = ctxTrendRounds;
    if(ctxDecTrend.length){
      var seenName = Object.create(null);
      trendRows = [];
      ctxTrendRounds.forEach(function(r){ if(r && r.name) seenName[r.name] = 1; trendRows.push(r); });
      ctxDecTrend.forEach(function(r){
        if(!r || !r.name || seenName[r.name]) return;
        var one = {};
        for(var kk in r){ if(Object.prototype.hasOwnProperty.call(r, kk)) one[kk] = r[kk]; }
        one.outcome = "decision";
        // v4.13（用户 m18728 第 1 条）：决策轮也要吃自己 event_id 挂上的裁切。
        // 以前这里无条件清空，后端就算绑好了 trims_after 也传不到柱子上。
        if(!one.trims_after) one.trims_after = [];
        trendRows.push(one);
      });
      trendRows.sort(function(a, b){ return (Number(b.ts) || 0) - (Number(a.ts) || 0); });
    }
    var trendHtml;
    if(!ctxTrendReq || !ctxTrendDec){
      // v4.83（用户 m08855）：两路趋势数据没齐之前先画骨架，别先画一半再补两次。
      trendHtml = "<div class='trend-hd'><span>正在读取快照…</span><span class='grow'></span></div>" +
                  "<div class='trend'><div class='trend-in'></div></div>";
    } else {
      trendHtml = buildTrend(trendRows, trendCount, d.threshold || 0, ctxTrims,
                        live ? trendCount : (echo ? pendCount : 0), ctxSnap ? ctxSnap.name : "",
                        ctxSnap ? "<span class='mini snap-back' data-snap-back='1' title='回到实时窗口'>← 返回实时</span>" : "",
                        winInfo, echo ? "当前待发" : "实时窗口");
    }
    // v3.94（用户 m08398 图1）：顶上那条「最近一次判定」横幅删掉。判定记录现在是一条
    // 真的 assistant 消息，直接落在它促成的回复前面（后端 window/manager.py 的
    // _decision_slots 定的位置），顶上再挂一张卡既重复又占掉半个屏。
    var trendBox = box.querySelector("[data-ctx-trend]");
    if(bodyUnchanged && trendBox){
      var keepX = trendScrollX(trendBox);
      trendBox.innerHTML = trendHtml;
      restoreTrendScroll(trendBox, keepX);
      revealTrendSel(trendBox.querySelector(".trend"));
      restoreWinScroll(keepWinY);
      fillQuoteMiss();
      return;
    }
    html += "<div data-ctx-trend>" + trendHtml + "</div>";
    if(!msgs.length && !pend.length){
      // 空窗口一律居中一句话（用户 m40335）：刚打开群开关、热重载后还没等到新消息时，
      // 满屏历史或一句小字都显得像坏了，给个「等待新消息进入」就够。
      var why = ctxSnap ? "这一轮没带历史（群聊冷启动的第一轮就是这样），上面是本轮发出去的消息"
              : (echo ? "窗口刚被重置——热重载或重启会清空内存里的窗口。"
                      : "这个会话还没有窗口记录，开关打开后要等它下一次触发 agent。");
      swapWinBody(html + "<div class='waitbox'><div class='wbi'>◌</div>" +
        "<div class='wbt'>等待新消息进入</div>" +
        "<div class='wbs'>" + esc(why) + "<br>群里有人说话就会出现在这里，下一轮触发后转成正式上下文。</div></div>");
      box.onclick = winClick;
      restoreWinScroll(keepWinY);
      return;
    }
    if(fromLast && !ctxSnap){
      html += "<div class='source-note' role='note'><span class='source-mark'>↶</span><span class='source-copy'>" +
        "<strong>窗口已重置" + (d.last_ts ? " · 上次请求 " + esc(fmtTime(d.last_ts)) : "") + "</strong><br>" +
        "热重载或重启后内存里的窗口清空了，这里只显示新进来的消息。下次触发后自动转成正式上下文。</span></div>";
    }
    if(VIEW === "raw"){
      var rawTxt = "";
      var rawLastTs = "";
      var rawN = 0;
      // v3.95（用户 m09171）：编号改成按输出顺序连续排——决策注记要插进待发里，
      // 再按 msgs.length + j 算就会出现跳号。
      // 决策注记的原文一行：「[00:53][对当前聊天消息的决策建议] 判定不回（…）」
      var decRawBody = function(m){
        var b0 = String(m.raw || m.text || "").replace(/^\[决策记录\]\s*(\d{1,2}:\d{2}\s*)?/, "");
        // v4.39（用户 m20312）：注记正文现在以「[HH:MM] 」开头（跟请求原文一致）。
        // 时间拎到最前面，别变成「[对当前聊天消息的决策建议] [01:07] 判定要回」。
        var tm0 = "";
        var mt0 = b0.match(/^\[(\d{1,2}:\d{2})\]\s*/);
        if(mt0){ tm0 = mt0[1]; b0 = b0.slice(mt0[0].length); }
        // v4.65（用户 m36008）：正文里已经带了「[对当前聊天消息的决策建议]」声明，
        // 这里剥掉，名字统一由下面那段补一次。
        b0 = b0.replace(/^\[\s*(?:对当前聊天消息的决策建议|对以上聊天记录的决策建议)\s*\]\s*/, "");
        var t0 = String(m.ts || "") || tm0;
        var nm0 = String(m.name || m.who || "");
        return (t0 ? "[" + t0 + "]" : "") + (nm0 ? "[" + nm0 + "]" : "") + " " + b0;
      };
      var rawLine = function(role, body, pend){
        rawN++;
        rawTxt += "[" + rawN + "] " + role + (pend ? " · 待发" : "") + "\n" + body + "\n\n";
      };
      msgs.forEach(function(m, i){
        var body = rawOf(m);
        if(m.role === "user"){
          var q0r = splitMetaAll(tidyBody(m.text || ""))[0];
          if(q0r && q0r.meta && q0r.meta.ts) rawLastTs = shortTs(q0r.meta.ts);
        }
        // bot 消息统一成「[时间] 【名字】 正文」：老记录被前缀过两次（[22:42] 【桃华】 [22:42] …），
        // 回补的消息又完全没有头（用户 m51324 图1~图3）
        // v3.97（用户 m09615 图3）：决策记录跟普通 bot 消息一个长相——时间走左边那一列，
        // 名字写进名字位，正文就是判定本身。老数据里还留着「[决策记录] HH:MM」前缀，剥掉。
        if(m.decnote) body = decRawBody(m);
        // v4.99（用户 m18435 第 2 条）：工具回调这类消息的时间由后端剥进 m.at，
        // 正文里没有 [HH:MM] 前缀，这里要认它，否则会被标成上一条用户消息的时间。
        else if(m.role === "assistant") body = botRawHead(body, m.who || m.name || "", m.at || rawLastTs);
        var tl = tcRawLine(m);
        if(tl) body = body ? (body + "\n" + tl) : tl;
        rawLine(m.role === "summary" ? "中期摘要" : m.role, body, false);
      });
      pend.forEach(function(m, j){
        // 编号跟历史连着排，别另起一套「[待发] 名字 · 时间」——
        // 原文模式看的是模型会收到什么，形态得跟历史一样（用户 m27043）
        var isBot = (m.mark === "bot");
        rawLine(isBot ? "assistant" : "user", pendRaw(m) || "(空)", !isBot);
      });
      html += "<pre class='rawblk full'>" + esc(rawTxt.replace(/\n+$/, "")) + "</pre>";
    } else {
      html += "<div class='scroll'>";
      var lastTs = "";
      // 连续同一个发言人并成一块（用户 m31327 / m31526）：组头只留一份头像 + 名字，
      // 组内每条只剩「左时间 / 正文 / 右字数」。分组键是 role + 发言人，不是纯 role——
      // 一条 user 消息里常拼着好几个人，按 role 并会把不同人合成一组、名字只剩一个。
      var grpKey = null, grpRole = null, grpUid = "", grpBuf = "", grpHead = "", grpNote = "", grpTools = 0;
      // v3.94（用户 m08398 图2/图3）：还没有对应回复的决策记录（判定不回那种）要画在
      // 待发区之后——它判的就是那几条待发，画在前面像在事前预言。
      var flushGrp = function(){
        if(!grpBuf) return;
        // 组头在组开始时画的，那时还不知道整组用了几次工具，这里补上（0 次就把胶囊删掉）
        var ghead = grpHead
          ? (grpTools > 0 ? grpHead.replace("工具 ×\u0001", "工具 ×" + grpTools)
                          : grpHead.replace("<span class='chb'>工具 ×\u0001</span>", ""))
          : "";
        // 用户 m06193：拉黑的人整组默认折叠（只留组头），点「展开」才看内容
        var blkCls = (UBLK_ON && grpRole === "user" && grpUid && UBLK[grpUid])
          ? (" blk" + (UBLK_OPEN[grpUid] ? " open" : "")) : "";
        html += "<div class='mgrp " + esc(grpRole || "user") + blkCls + "'>" + ghead + grpBuf + "</div>";
        grpBuf = ""; grpHead = ""; grpTools = 0; grpUid = "";
      };
      msgs.forEach(function(m, i){
        if(m.role === "user"){
          var q0 = splitMetaAll(tidyBody(m.text || ""))[0];
          if(q0 && q0.meta && q0.meta.ts) lastTs = shortTs(q0.meta.ts);
        }
        // 用户 m10400 图4：实时窗口以前不认 orig_media，动图在美化档里还是九宫格 +
        // 「（动图 · 共 N 帧…）」那句说明。跟请求快照一个口径：美化档换成聊天里原本那张图。
        var mv = m;
        if(VIEW === "pretty" && m.display && m.display.orig_media && m.display.orig_media.length){
          var mt = stripDeriveNote(m.text || "");
          mv = Object.assign({}, m, { origMedia: true, text: mt, chars: mt.length });
        }
        // v3.94（用户 m08398 图2/图3）：决策记录已经插在它促成的回复之前（后端算的槽位），
        // 窗口末尾不再补一份。单独成组：跟上一条 bot 回复并组会把它的名字吃掉，
        // 看着就像 bot 自己说的话。
        // v4.17（用户 m19717 第 2 条）：决策注记不再单独成一张橙色卡片——它在上下文里
        // 就是一条普通 assistant 消息，页面上也照普通消息画（头像/名字/时间/字数一视同仁）。
        // 以前这里 flushGrp + 单独 mgrp.decnote，看着像另贴的一张贴纸。
        var gk = groupKeyOf(m, i);
        if(gk !== grpKey){
          flushGrp();
          grpKey = gk; grpRole = m.role || "user"; grpUid = uidOf(m);
          grpNote = (VIEW === "mixed" && (m.role === "user" || m.role === "assistant") && !multiOf(m))
            ? mixNoteOf(m) : "";
          grpHead = ((m.role === "user" || m.role === "assistant") && !multiOf(m))
            ? grpHeadHtml(m.role, whoOf(m), uidOf(m), youOf(m), grpNote, "\u0001") : "";
        }
        if(m.tools) grpTools += Number(m.tools) || 0;
        // v3.89（用户 m05321 第 1 条）：裁切快照里的待发要标出来——卡尾挂「待发」，
        // 整张卡换成虚线框（跟实时窗口的待发区长一个样）
        if(m.pending){
          var pCard = msgCards(mv, i + 1, VIEW, lastTs, true,
                               "<span class='chb tag pendtag'>待发</span>");
          pCard = String(pCard).replace(/<div class='mc (user|assistant|summary)/g,
                                          "<div class='mc pend $1");
          grpBuf += pCard;
        } else {
          grpBuf += msgCards(mv, i + 1, VIEW, lastTs, true);
        }
      });
      flushGrp();
      // v3.76（用户本轮图5）：待发区自成一组。以前第一条待发会跟上一条历史并组
      //（同一个人连着发），组头和头像都不画，屏幕上只剩「15:13 @冇似」这样一行，
      // 看着像一条没头没脑的孤儿消息。待发本来就不是历史，分开画更清楚。
      if(pend.length) grpKey = "\u0000pend";
      // v4.03（用户 m11947）：决策注记现在就是窗口本体里的一条真实消息，位置由
      // commit 那一刻决定；前端不再按旧落点把它挪进待发序列（那套锚点已经废了）。
      // 待发也接着拼：不然底部又冒出一堆独立气泡框，跟上面的色块对不上
      pend.forEach(function(m, j){
        var prole = (m.mark === "bot") ? "assistant" : "user";
        // 待发项的 sender 是 QQ 号、name 才是昵称，组头要昵称（头像再用号）
        var pwho = String(m.name || m.sender || "");
        var puid = /^\d{4,}$/.test(String(m.sender || "")) ? String(m.sender) : "";
        var gk2 = prole + "|" + pwho + "|" + puid;
        if(gk2 !== grpKey){
          flushGrp();
          grpKey = gk2; grpRole = prole; grpUid = puid;
          grpHead = grpHeadHtml(prole, pwho, puid, false);
        }
        grpBuf += pendCards(m, msgs.length + j + 1, VIEW, true);
      });
      flushGrp();
      // v3.75（用户本轮第 3 条）：这一轮请求失败时，把原因写在快照结尾。
      // 请求快照里早就有这个字段（failed + error），窗口快照以前完全看不到。
      // 只画在页面里，不进 messages，所以永远不会被拼进下一轮上下文。
      var snapErr = ctxSnap
        ? String(ctxSnap.error || (ctxSnap.failed ? "请求失败" : "") || "")
        : "";
      if(snapErr){
        html += "<div class='wfail'><b>本次请求失败</b><span>" + esc(snapErr) + "</span>" +
                "<i>只是这一轮的结论，不会进入上下文（下一轮不会看到这段）</i></div>";
      }
      html += "</div>";
    }
    swapWinBody(html);
    box.onclick = winClick;
    if(keepTop){
      var ns = box.querySelector(".scroll");
      if(ns) ns.scrollTop = keepTop;
    }
    markCards((d.sid || "") + (ctxSnap ? ":snapshot:" + ctxSnap.name : ""));
    // 拉黑按钮要知道「哪些已经在清单里」，切群才重拉一次（v3.36 用户第 13 条）
    loadPhraseBlock(d.sid || "");
    // 用户 m06193：用户黑名单也要知道「谁已经在名单里」
    ublkLoad(d.sid || "");
    restoreWinScroll(keepWinY);
  }

  // 展开/收起一段长正文。按钮和 .mono 是兄弟节点，从父节点里找——assistant 多段时别串到别的段
  function toggleFold(btn){
    var key = btn.getAttribute("data-fold");
    // 先在本行里找（同时展开好几轮时，别串到别人的历史上去）
    var scope = btn.closest ? btn.closest("td") : null;
    var body = (scope && scope.querySelector("[data-foldbody='" + key + "']")) ||
               document.querySelector("[data-foldbody='" + key + "']");
    if(!body) return;
    var hide = body.style.display !== "none";
    body.style.display = hide ? "none" : "";
    btn.textContent = hide ? "展开" : "收起";
    // v4.02（用户 m11629 图2）：转发卡的高度是按真实行高量的，折叠着的时候
    // offsetTop 全是 0，量不出东西；所以一展开就重量一次，卡才收得住前三行。
    if(!hide) fitForwardCards(body);
  }

  // 两页切换 + 展开全文 + 清空摘要，都挂在「当前上下文窗口」这个面板上
  async function winClick(ev){
    // 用户 m06193：组头名字后面的「拉黑」/「展开·折叠」
    var blkU = ev.target && ev.target.closest ? ev.target.closest("[data-blk-uid]") : null;
    if(blkU){
      var buid = blkU.getAttribute("data-blk-uid") || "";
      var bname = blkU.getAttribute("data-blk-name") || "";
      if(!buid) return;
      var byes = await mdConfirm("把「" + (bname || buid) + "」拉黑？\n\n" +
        "拉黑后他之后发的消息不再进入上下文（模型看不到），窗口里这一组默认折叠，点「展开」还能看。\n" +
        "名单是全局的，所有群一起生效；已经在窗口里的历史消息不动。");
      if(!byes) return;
      ublkSet(buid, bname, "add").then(function(d){
        if(!d || !d.ok){ mdAlert("拉黑失败：" + ((d && d.detail) || "未知错误")); return; }
        mdAlert("已拉黑「" + (bname || buid) + "」 · 名单现在 " + (d.count || 0) + " 人");
      }).catch(function(){ mdAlert("请求失败，看后台日志"); });
      return;
    }
    var blkE = ev.target && ev.target.closest ? ev.target.closest("[data-blk-exp]") : null;
    if(blkE){
      var euid = blkE.getAttribute("data-blk-exp") || "";
      if(!euid) return;
      if(UBLK_OPEN[euid]) delete UBLK_OPEN[euid]; else UBLK_OPEN[euid] = 1;
      if(ctxWin && ctxPane === "window") renderCtxWindow(ctxWin, true);
      return;
    }
    var pb = ev.target && ev.target.closest ? ev.target.closest("[data-pane]") : null;
    if(pb){ setCtxPane(pb.getAttribute("data-pane")); return; }
    var more = ev.target && ev.target.closest ? ev.target.closest("[data-more]") : null;
    if(more){ toggleMore(more); return; }
    // 转发卡：点卡片任意位置弹出大框看完整条目和图，不再原地展开
    // （用户 m03273）。弹层的 ×/遮罩由弹层自己那份监听关掉——它挂在 body 下，进不到
    // 这里；这里只管「开」。（用户 m03343 第 1 条）
    var fw = ev.target && ev.target.closest ? ev.target.closest("[data-fwd]") : null;
    if(fw && !(ev.target.closest && ev.target.closest("a"))){ openFwdPop(fw); return; }
    // 用户 m12814：点引用块跳到被引用的那条消息（当前视图没有就去那一轮快照里找）。
    // 正在选文字时不跳，免得想复制引用原话却被弹走。
    var qj = ev.target && ev.target.closest ? ev.target.closest("[data-qjump]") : null;
    if(qj && !(ev.target.closest && ev.target.closest("a"))){
      var qsel = window.getSelection ? String(window.getSelection() || "") : "";
      if(!qsel){ jumpToQuote(qj.getAttribute("data-qjump") || "", qj); return; }
    }
    var fold = ev.target && ev.target.closest ? ev.target.closest("[data-fold]") : null;
    if(fold){ toggleFold(fold); return; }
    // 点绿柱 = 回到「现在」；点同一根蓝/灰柱两次 = 取消快照
    var lv = ev.target && ev.target.closest ? ev.target.closest("[data-live]") : null;
    if(lv){ ctxSnapRequest++; if(ctxSnap) exitSnap(); return; }
    var bar = ev.target && ev.target.closest ? ev.target.closest("[data-trend]") : null;
    if(bar){ openRoundByName(bar.getAttribute("data-trend"), bar); return; }
    var cr = ev.target && ev.target.closest ? ev.target.closest("[data-clear-rec]") : null;
    if(cr){ clearRecords(); return; }
    var sb = ev.target && ev.target.closest ? ev.target.closest("[data-snap-back]") : null;
    if(sb){ ctxSnapRequest++; exitSnap(); return; }
    // 用户 m00942 第 5 条：折叠柱子开关（只改画法，不动数据）。
    var tf = ev.target && ev.target.closest ? ev.target.closest("[data-trend-fold]") : null;
    if(tf){ toggleTrendFold(tf); return; }
    // 用户 m00942 第 4 条：点快照编号复制。
    var si = ev.target && ev.target.closest ? ev.target.closest("[data-snap-id]") : null;
    if(si){ copySnapId(si); return; }
    // 卡片右上角的「拉黑」（v3.36 用户第 13 条）
    var bk = ev.target && ev.target.closest ? ev.target.closest("[data-blk]") : null;
    if(bk){ toggleBlockPhrase(bk); return; }
    var t = ev.target && ev.target.closest ? ev.target.closest("[data-sumclear]") : null;
    if(!t || !ctxWin) return;
    if(!(await mdConfirm("清空这个会话的中期摘要？窗口历史不受影响，只是模型不再知道更早聊过什么。"))) return;
    var sid = ctxWin.sid;
    post(API + "/summary-clear?sid=" + encodeURIComponent(sid)).then(function(){
      refreshCtxWindow(sid);
    }).catch(function(){});
  }

  // 清空这个会话攒下的旧记录（配置、别名、计数都保留），清完重新开始收集
  async function clearRecords(){
    if(!ctxWin || !ctxWin.sid) return;
    var sid = ctxWin.sid;
    if(!(await mdConfirm("清空 " + sid + " 的记录？\n\n会删掉：请求记录（含调用轨迹）、窗口、裁切与消息日志、中期摘要。\n保留：备注名、隐藏标记、单群配置、计数。\n删了不可恢复。"))) return;
    post(API + "/clear-records?sid=" + encodeURIComponent(sid)).then(function(r){
      if(!r || !r.ok){ mdAlert("清空失败：" + ((r && r.detail) || "未知错误")); return; }
      var c = r.counts || {};
      mdAlert("已清空 " + r.total + " 项\n请求 " + (c.requests || 0) +
            " · 窗口 " + (c.windows || 0) + " · 消息 " + (c.decisions || 0) + " · 摘要 " + (c.summary || 0));
      ctxRounds = []; ctxTrendRounds = []; ctxTrims = []; ctxTrimKeep = 0; ctxSnapKeep = 0; ctxSnapTotal = 0;
      refreshCtxWindow(sid);
    }).catch(function(){ mdAlert("清空失败，看后台日志"); });
  }

  // v3.69（用户 m21003 第 5 条）：点柱子以前要等一份 130 KB 的请求记录读回来（实测 130~330ms），
  // 用户觉得「点了有点卡」。这里做三件事：①点下去立刻把选中框移过去，不等数据；
  // ②读过的轮次留在内存里（最多 24 份），再点同一根是秒开；③鼠标扫过柱子就悄悄预取，
  // 等点下去多半已经在手里了。缓存只放在页面内存里，刷新即失效，不写盘。
  document.addEventListener("pointerover", function(ev){
    var t = ev.target && ev.target.closest ? ev.target : null;
    if(!t || !ctxSid) return;
    // 用户 m17375 第 3 条：表格行也预取。以前只认走势图柱子，从回复记录表
    // 点进去要现等一秒多（转发图证明那一步最慢）；鼠标扫过行就先取一份。
    var b = t.closest("[data-trend]");
    var name = b ? (b.getAttribute("data-trend") || "") : "";
    if(!name){
      var tr = t.closest("tr.round-row[data-round]");
      if(tr) name = tr.getAttribute("data-round") || "";
    }
    if(!name || name === snapPrefetchLast || snapCacheGet(ctxSid, name)) return;
    snapPrefetchLast = name;
    if(snapPrefetchTimer) clearTimeout(snapPrefetchTimer);
    var wait = Math.max(20, 120 - (Date.now() - snapPrefetchAt));
    snapPrefetchTimer = setTimeout(function(){
      snapPrefetchTimer = null;
      var sid = ctxSid;
      if(!sid || snapCacheGet(sid, name)) return;
      snapPrefetchAt = Date.now();
      try{ fetchRound(sid, name).catch(function(){}); }catch(e){}
    }, wait);
  }, true);
  // 用户 m17375 第 3 条：列表画好后空闲预取最新两份快照。用户十有八九点的是最新那条，
  // 等鼠标扫过去再取就来不及了（一份要一秒多）。同一个「最新两份」只取一次。
  function toggleTrendFold(el){
    TREND_FOLD = !TREND_FOLD;
    try{ localStorage.setItem("mdp_trend_fold", TREND_FOLD ? "1" : "0"); }catch(e){}
    if(el) el.classList.toggle("on", TREND_FOLD);
    if(ctxWin) renderCtxWindow(ctxWin, true);
  }

  // 点柱子打开那一轮：先切到「最近请求」，再展开对应行并滚到视野里
  // 点走势图的一根柱子：看「那一轮请求时的窗口快照」，只有窗口，不带 system/轨迹/输出。
  // 用户要的是对照「当时窗口里有什么」，不是复盘整轮请求（那在「最近请求」页里）。
  function openRoundByName(name, barEl){
    if(!name || !ctxSid) return;
    var sid = ctxSid, requestId = ++ctxSnapRequest;
    // 再点一次同一根柱子 = 取消，回到实时窗口
    if(ctxSnap && ctxSnap.name === name){
      exitSnap();
      return;
    }
    // 先把选中框移过去，手指底下立刻有回应；数据回来后再整块重画
    markTrendSel(barEl);
    fetchRound(sid, name).then(function(d){
      if(sid !== ctxSid || requestId !== ctxSnapRequest) return;
      if(!d.ok || !d.record){ mdAlert("那一轮的记录已经不存在了"); return; }
      var r = d.record;
      var snapshot = r.window_snapshot;
      var hasSnapshot = snapshot && Array.isArray(snapshot.messages);
      var vmsgs = hasSnapshot ? snapshot.messages : (r.messages || []).map(function(m, i){
        var projected = snapshotDisplay(m, (r.display_messages || [])[i]);
        var dm = projected.view;
        var t = typeof dm.text === "string" ? dm.text : contentText(m.content);
        return { role: m.role || "user", text: t, raw: rawContent(m.content), chars: t.length, media: dm.media || m.media || [],
                 display: projected.display, name: m.name || "",
                 tools: (m.tool_calls || []).length, tool_call_id: m.tool_call_id || "",
                 // v3.89（用户 m05321 第 1 条）：裁切快照里带「待发」标记的项要一眼认出
                 pending: !!dm.pending,
                 tc: tcOf(m) };
      });
      // 群聊冷启动那一轮根本没发历史（request.messages 是空的），点柱子会看到
      // 一片空白，像是点坏了（用户 m29567）。这里拿本轮实际发出去的正文顶上。
      if(!hasSnapshot && !vmsgs.length && r.user_prompt){
        vmsgs = splitIncoming(String(r.user_prompt)).map(function(t){
          return { role: "user", text: t, raw: t, chars: t.length };
        });
      }
      ctxSnap = { name: name, ts: hasSnapshot ? snapshot.ts : (r.ts || 0), messages: vmsgs,
        counted: hasSnapshot && typeof snapshot.counted === "number" ? snapshot.counted : null,
        user_counted: hasSnapshot && typeof snapshot.user_counted === "number" ? snapshot.user_counted : null,
        size: hasSnapshot ? snapshot.size : vmsgs.length,
        sent_counted: hasSnapshot ? snapshot.sent_counted : null,
        history_counted: hasSnapshot ? snapshot.history_counted : null,
        incoming_counted: hasSnapshot ? snapshot.incoming_counted : null,
        // v3.75（用户本轮第 3 条）：失败原因要能画进窗口快照结尾，以前这个对象
        // 根本没带 error，renderCtxWindow 里那段 .wfail 永远是空的。
        error: r.error || "",
        failed: !!r.failed,
        // v3.89（用户 m05321 第 1 条）：裁切快照里有多少条是待发（标题上写出来）
        pending_n: Number(r.trim_pending_count || 0) || 0,
        result: roundResult(r), legacy: !hasSnapshot,
        // 用户 m00942 第 4 条：标题上那个可点的快照编号（后端 _request_detail_head 补的）。
        snap_id: r.snap_id || "" };
      // 记住此刻视口在面板里的相对位置：快照内容比实时窗口短，
      // 按比例换算回去，视口才不会落到不相干的地方（用户 m42220）。
      // 同时记下绝对位置，退出快照时按它原样回去。
      if(ctxSnapReturnY == null) ctxSnapReturnY = window.scrollY || 0;
      grabWinAnchor("snap");
      if(ctxPane !== "window"){ ctxPane = "window"; try{ localStorage.setItem("mdp_ctx_pane", "window"); }catch(e){} }
      // 不滚到面板顶部：点柱子只是换右栏内容，页面滚动位置要留在原处（用户 m40932）。
      // renderCtxWindow 内部已经用 keepWinY + restoreWinScroll 把整页滚动位置放回去了。
      renderCtxWindow(ctxWin, true);
    }).catch(function(){ if(sid === ctxSid && requestId === ctxSnapRequest) mdAlert("读取那一轮记录失败"); });
  }

  // 「实时窗口」和「最近请求」分两页：窗口页能铺满整屏，请求页保留原来的表格。
  // 左侧会话列表不动，切页只是换右栏。
  // v3.97（用户 m09615 第 6 条）：三个页签各记一份「哪几行是摊开的」，切走再切回来
  // 不用重新点（以前切一趟就整片折回去）。记的是快照名，重建表格后按名字找回。
  function reopenRows(names){
    (names || []).forEach(function(nm){
      var head = document.querySelector("#ctx-rounds tr.round-row[data-round='" + nm + "']");
      if(head && !head.classList.contains("open")) toggleRequestRow(nm);
    });
  }

  function setCtxPane(v){
    if(ctxPane === v){
      // v3.91：已经在决策记录页时再点一下就是「刷新一下」（后台可能刚判定过一轮）
      if(v === "decisions") loadCtxDecisions(ctxSid);
      return;
    }
    var leavingOpen = [];
    Array.prototype.forEach.call(document.querySelectorAll("#ctx-rounds tr.round-row.open"), function(tr){
      var nmX = tr.getAttribute("data-round");
      if(nmX) leavingOpen.push(nmX);
    });
    if(ctxPane) paneOpen[ctxPane] = leavingOpen;
    ctxPane = v;
    // 页签选中态在这里就地刷新：renderCtxWindow 数据没变时会早返回，不再顺手刷这两个按钮。
    Array.prototype.forEach.call(document.querySelectorAll("#ctx-window [data-pane]"), function(b){
      b.classList.toggle("on", b.getAttribute("data-pane") === v);
    });
    try{ localStorage.setItem("mdp_ctx_pane", v); }catch(e){}
    // v3.91（用户 m05749）：三个页签共用下面那一块，圆角拼缝得自己同步。
    var wbL = document.getElementById("ctx-window"), rbL = document.getElementById("ctx-rounds");
    if(wbL) wbL.classList.toggle("linked", v !== "window");
    if(rbL) rbL.classList.toggle("linked", v !== "window");
    // 切页只是换右栏内容，视口位置别动（用户 m42220）：以前这里把 winAnchor 设成 0，
    // 切回窗口时按「面板顶部对齐视口」算，页面在顶部时会被硬生生往下弹 246px。
    winAnchor = null;
    // 必须在这里先记一次位置：渲染过程中窗口面板会被清空，文档高度瞬间塌到一屏以内，
    // 浏览器立刻把 scrollY 夹成 0——等 renderCtxWindow 里再读就已经是 0 了。
    var keepY = window.scrollY || window.pageYOffset || 0;
    // v4.02（用户 m11629 图1）：preserveDetails 会把上一档画好的详情行原样搬回来
    //（在决策页画的、或者切档前那个档位的），于是从决策切回回复记录时，展开的详情还是旧档的长相，
    // 跟顶上选着的档位对不上。这里跟 setView 一个做法：整表重画把 detail 清掉，再按记录重新展开，
    // 展开时 toggleRequestRow 会用当前档位、拿内存里的记录同步重画（不闪，也不多打一次请求）。
    if(v === "rounds"){
      var hydratedR = false;
      if(!ctxRoundsFull && ctxSid){
        // 用户 m14026 第 4 条：内存里没有全量列表时先拿上次的缓存铺上，
        // 不再先闪一屏「正在读取回复记录…」再等接口回来。
        var rc0 = null;
        try{ rc0 = cacheGet("rounds"); }catch(e){}
        if(rc0 && rc0.sid === ctxSid && rc0.items && rc0.items.length){
          ctxRounds = rc0.items;
          ctxRoundsFull = true;
          if(rc0.keep) ctxKeep = Number(rc0.keep) || ctxKeep;
          hydratedR = true;
        }
      }
      if(ctxRoundsFull){
        renderCtxRounds(ctxRounds);
        Array.prototype.forEach.call(document.querySelectorAll("#ctx-rounds tr.round-row"), function(tr){
          tr.classList.remove("open");
        });
        reopenRows(paneOpen.rounds);
      } else {
        var boxR = document.getElementById("ctx-rounds");
        if(boxR) boxR.innerHTML = "<div class='empty'>正在读取回复记录…</div>";
      }
      // 缓存直出后仍要跟后台核对一次，数据变了再整表重画。
      if(ctxSid && (!ctxRoundsFull || hydratedR)) refreshCtxRounds(ctxSid, true);
    }
    else if(v === "decisions"){
      renderCtxDecisions(ctxDecisions);
      Array.prototype.forEach.call(document.querySelectorAll("#ctx-rounds tr.round-row"), function(tr){
        tr.classList.remove("open");
      });
      reopenRows(paneOpen.decisions);
      loadCtxDecisions(ctxSid);
    }
    else document.getElementById("ctx-rounds").style.display = "none";
    // 用户 m14026 第 4 条：force 去掉——数据没变时切回来只把主体显示出来，不重画。
    if(ctxWin) renderCtxWindow(ctxWin);
    // 用切页前的位置兜底再放一次（restoreWinScroll 内部会按新文档高度夹一次）。
    restoreWinScroll(keepY);
  }

  function loadCtxDecisions(sid, force){
    if(!sid) return Promise.resolve();
    if(!force && sid === ctxDecisionsSid && ctxDecisionsAt && Date.now() - ctxDecisionsAt < 15000){
      // 调用方已用内存列表画好当前页；缓存命中无需再重画一次。
      // 决策表只含显示上限内的记录，不覆盖独立的轻量趋势。
      return Promise.resolve();
    }
    if(!force && ctxDecisionsInFlight && ctxDecisionsInFlightSid === sid){
      return ctxDecisionsInFlight;
    }
    var requestId = ++ctxDecisionsRequest;
    ctxDecisionsError = "";
    if(ctxPane === "decisions" && ctxDecisionsSid !== sid) renderCtxDecisions([], true);
    var p = get(API + "/decision-rounds?sid=" + encodeURIComponent(sid) + "&limit=0&_" + Date.now()).then(function(d){
      if(sid !== ctxSid || requestId !== ctxDecisionsRequest) return;
      ctxDecisions = (d && d.items) || [];
      ctxDecisionsSid = sid;
      ctxDecisionsAt = Date.now();
      cachePut("decisions", {sid: sid, items: ctxDecisions});
      ctxSnapKeep = Number(d && (d.snap_show || d.snap_keep) || 0) || ctxSnapKeep;
      ctxSnapTotal = Number(d && d.snap_total || 0) || ctxSnapTotal;
      // 趋势只由 refreshCtxWindow 的轻量结果更新，切页不缩短它。
      if(ctxPane === "decisions") renderCtxDecisions(ctxDecisions, true);
      // 实时窗口顶部那条「最近一次判定」也吃这份数据；拉回来之后要重画一次窗口，
      // 否则要等下一次数据变化（sig 变了）才轮到它。
      else if(ctxPane === "window" && ctxWin) renderCtxWindow(ctxWin, true);
    }).catch(function(e){
      if(sid !== ctxSid || requestId !== ctxDecisionsRequest) return;
      ctxDecisionsError = "读取决策记录失败" + (e && e.message ? "：" + e.message : "") + "，请再点一次重试。";
      // 已有列表继续保留；首次读取失败时给出重试提示，不误报没有记录。
      if(ctxPane === "decisions" && ctxDecisionsSid !== sid) renderCtxDecisions([], true);
    }).then(function(){
      if(ctxDecisionsInFlight === p){
        ctxDecisionsInFlight = null;
        ctxDecisionsInFlightSid = "";
      }
    });
    ctxDecisionsInFlight = p;
    ctxDecisionsInFlightSid = sid;
    return p;
  }

  // 2026-10-31（用户 m05138）：决策只给建议，不再用「要回／不回」下命令。
  // 失败也不再挤在窗口条数那一格，结论列直接写「决策失败」，悬停看当时的报错。
  function refreshCtxRounds(sid, full){
    if(sid !== ctxSid) return;
    // 表格与趋势分别按各自的上限读取；全量列表不能覆盖更长的轻量趋势。
    var wantFull = !!full || ctxPane === "rounds";
    var key = sid + (wantFull ? ":f" : ":l");
    if(ctxRoundsLoading === key) return;
    var requestId = ++ctxRoundsRequest;
    ctxRoundsLoading = key;
    var base = API + "/requests?sid=" + encodeURIComponent(sid) + "&limit=0&trim=1";
    return Promise.all([
      get(base + "&light=1&_=" + Date.now()),
      wantFull ? get(base + "&_=" + Date.now()) : Promise.resolve(null)
    ]).then(function(res){
      if(sid !== ctxSid || requestId !== ctxRoundsRequest) return;
      var d = res[0] || {}, list = wantFull ? (res[1] || {}) : d;
      var items = d.items || [];
      ctxKeep = Number(list.keep || 0) || 0;
      ctxSnapKeep = Number(d.snap_show || d.snap_keep || 0) || 0;
      ctxSnapTotal = Number(d.snap_total || 0) || 0;
      // v4.73（用户 m02417）：裁切快照回走势图，但 v4.07 的口径不变——回复记录表里仍然
      // 只有真实请求（它们不占行、不进「本页合计」的命中率）。两路在这里就分开。
      var _plain = (list.items || []).filter(function(it){
        return !(String(it.outcome || "") === "trim" || it.trim_only === true);
      });
      // 走势图那一路单独判变化：只多了一份裁切快照时，回复记录表没变，
      // 但也得让走势图重画，否则青柱要等到下一轮请求才出现。
      var _trendChanged = JSON.stringify(items) !== JSON.stringify(ctxTrendRounds);
      ctxTrendRounds = items;
      ctxTrendReq = true;
      // v4.83（用户 m08855）：趋势数据也进缓存，下次刷新首屏直接画完整走势图。
      if(_trendChanged) cachePut("trendreq", {version: 3, sid: sid, items: items, snapKeep: ctxSnapKeep, snapTotal: ctxSnapTotal});
      var changed = wantFull && JSON.stringify(_plain) !== JSON.stringify(ctxRounds);
      // 轻量轮询只刷新趋势，不冲掉已加载的完整回复表和它的展开缓存。
      if(wantFull){
        ctxRounds = _plain;
        ctxRoundsFull = true;
        cachePut("rounds", {sid: sid, items: _plain, keep: ctxKeep});
      }
      if(!changed && !_trendChanged && document.querySelector("#ctx-rounds [data-round]")){
        if(ctxWin) renderCtxWindow(ctxWin);
        return;
      }
      if(wantFull && ctxPane === "rounds"){
        var box = document.getElementById("ctx-rounds");
        var scrollX = window.scrollX, scrollY = window.scrollY;
        var table = box.querySelector(".tbl"), tableX = table ? table.scrollLeft : 0;
        renderCtxRounds(_plain, true);
        if(ctxPane === "rounds") reopenRows(paneOpen.rounds);
        table = box.querySelector(".tbl");
        if(table) table.scrollLeft = tableX;
        window.scrollTo(scrollX, scrollY);
      }
      if(ctxWin) renderCtxWindow(ctxWin);
    }).catch(function(){
      // 临时失败保留旧列表与详情，下个周期重试。
    }).then(function(){
      if(requestId === ctxRoundsRequest) ctxRoundsLoading = "";
    });
  }

  // 裁切记录只用来给走势图的柱子标橙色 + 悬停提示，不再单独成块。
  function refreshCtxWindow(sid){
    if(!sid || sid !== ctxSid) return;
    var requestId = ++ctxWindowRequest;
    // 同一代次一起提交窗口和裁切；较早请求即使较晚返回也不能覆盖。
    return Promise.all([
      get(API + "/window?sid=" + encodeURIComponent(sid)).catch(function(){ return null; }),
      // v4.71（用户 m01516 第 1 条）：limit=0 = 要全部保留的窗口快照（后端按 windows_keep 给满）。
      // 以前写死 100：群聊设了 200 份，走势图也只画得出 100 根裁切柱。
      get(API + "/windows?limit=0&sid=" + encodeURIComponent(sid)).catch(function(){ return null; }),
      // 所有页签共用完整轻量趋势，limit=0 由后端按快照显示上限解析。
      get(API + "/decision-rounds?sid=" + encodeURIComponent(sid) + "&limit=0&light=1&_" + Date.now()).catch(function(){ return null; })
    ]).then(function(res){
      if(sid !== ctxSid || requestId !== ctxWindowRequest) return;
      if(res[1]){
        ctxTrims = res[1].items || [];
        ctxTrimKeep = Number(res[1].keep || 0) || 0;
        if(!ctxSnapKeep && ctxTrimKeep) ctxSnapKeep = ctxTrimKeep;
      }
        if(res[2]){
          ctxSnapKeep = Number(res[2].snap_show || res[2].snap_keep || 0) || ctxSnapKeep;
          ctxSnapTotal = Number(res[2].snap_total || 0) || ctxSnapTotal;
          var trend = decisionTrendOf(res[2].items || []);
          ctxDecTrend = trend.items;
          ctxDecFolded = trend.folded;
          ctxTrendDec = true;
        }
      if(res[2]) cachePut("trenddec", {version: 3, sid: sid, items: ctxDecTrend, folded: ctxDecFolded});
      if(res[0]) ctxWin = res[0];
      if(!ctxWin) return;
      cachePut("win", {sid: sid, win: ctxWin, trims: ctxTrims, trimKeep: ctxTrimKeep, snapKeep: ctxSnapKeep, snapTotal: ctxSnapTotal});
      renderCtxWindow(ctxWin);
    });
  }

  function openCtx(sid){
    invalidateCtxRequests();
    ctxSid = sid;
    lastWinSig = "";
    lastTrendSig = "";
    ctxRounds = [];
    ctxTrendRounds = [];
    ctxRoundsFull = false;
    ctxRoundPage = 0;
    ctxDecisionPage = 0;
    ctxTrims = []; ctxTrimKeep = 0;
    ctxDecTrend = [];
    ctxTrendReq = false; ctxTrendDec = false;   // v4.83：换会话重新等两路趋势
    ctxWin = null;
    ctxSnap = null;
    renderCtxList();
    if(ctxPane === "decisions") loadCtxDecisions(sid);
    refreshCtxWindow(sid);
    renderCtxRounds([]);
    refreshCtxRounds(sid);
    // v3.91：决策记录既给「决策记录」页用，也给走势图用；换会话时先拉轻量趋势，
    // 只有停在「决策记录」页才再拉全量列表（带图片与轨迹）。
  }

  function toggleRequestRow(name){
    var rows = document.querySelectorAll("#ctx-rounds tr");
    var target = null, head = null;
    for(var i = 0; i < rows.length; i++){
      var d = rows[i].getAttribute("data-detail");
      if(d === name){ target = rows[i]; }
      if(rows[i].getAttribute("data-round") === name){ head = rows[i]; }
    }
    if(!target || !head) return;
    // 决策表与回复记录现在都是 13 列（v4.84 加「渠道 / 模型」、v4.85 加「回复内容」、v4.88 渠道独立成列），详情单元格的 colspan 跟着这张表走
    var cspan = target.getAttribute("data-cspan") || 9;
    var open = target.style.display !== "none";
    target.style.display = open ? "none" : "";
    head.classList.toggle("open", !open);
    if(open) return;
    if(target.getAttribute("data-loaded")) return;
    // 详情渲染整段收进 paint()：缓存命中时同步画，没命中才发请求
    var paint = function(d){
      if(!d.ok){ target.innerHTML = "<td colspan='" + cspan + "'><div class='hint'>记录不存在</div></td>"; return; }
      var r = d.record || {};
      var errs = r.system_prompt_errors || [];
      // v3.90（用户 m05623 第 2 条）：裁切快照是一次「顺手把旧历史裁掉」的记录，
      // 它没有 system、没有本轮消息、也没有 agent 轨迹。三档都只留 ② 历史消息，
      // 其余段连同它们的空占位一起不画（否则 ① 会留一大片空白）。
      var isTrimSnap = String(r.outcome || "") === "trim" || String(r.trigger_reason || "") === "trim" || r.trim_only === true;
      // v3.91（用户 m05749）：决策快照只有「决策提示词 + 它读到的聊天记录」两段，
      // 没有本轮消息、也没有 agent 轨迹（那一轮可能压根没回）。
      var isDecSnap = String(r.outcome || "") === "decision" || String(r.trigger_reason || "") === "decision" || r.decision_only === true;
      var h = "";
      // 这一轮上下文的构成：system / 历史 / 本轮消息 / 被挪走的动态段
      var partition = snapshotPartition(r);
      var secs = partition.sections;
      if(secs.length){
        var tot = 0;
        if(!tot){ secs.forEach(function(s){ tot += (s.chars || 0); }); }
        if(tot > 0){
          // v3.94（用户 m08398 图6）：决策快照的四段是「决策提示词 / 聊天历史 / 本轮消息 / 动态段」，
          // 不是真发出去的一轮上下文，标题得说实话。
          h += "<div class='hint'>本地快照消息投影" +
               "：共 " + tot + " 字符（按原始内容字符数占比；非服务商缓存实测）</div>";
          h += "<div class='stack'>";
          secs.forEach(function(s, i){
            if(!s.chars) return;
            h += "<span class='s" + i + "' style='width:" + (s.chars / tot * 100).toFixed(2) +
                 "%' title='" + esc(s.label) + " " + s.chars + " 字符'></span>";
          });
          h += "</div><div class='lg'>";
          secs.forEach(function(s, i){
            h += "<span class='k'><i class='s" + i + "'></i>" + esc(s.label) + " " + s.chars +
                 " · " + (s.chars / tot * 100).toFixed(1) + "%</span>";
          });
          h += "</div>";
        }
      }
      // 占比、标题统一取本次 canonical 投影，不依赖旧 sections / total_chars。
      var SEC = {};
      secs.forEach(function(s){ if(s && s.key) SEC[s.key] = Number(s.chars) || 0; });
      function secNum(key, fallback){ return SEC[key] === undefined ? fallback : SEC[key]; }
      // messages 是唯一顺序/索引源；旧 history_messages 不参与分界。
      var boundX = partition.bound;
      var hasSplit = (typeof r.system_static === "string");

      var stat = "", reloc = "", pos = {};
      var statNames = hasSplit ? (r.system_static_names || []) : (r.system_prompt_names || []);
      var relocNames = hasSplit ? (r.system_relocated_names || []) : [];
      if(hasSplit){
        stat = r.system_static || "";
        reloc = partition.relocated;
        pos = r.prompt_positions || {};
      } else {
        stat = r.system_prompt || "";
      }
      if(VIEW === "raw"){
        // 原文档也要跟美化/对照同构：4 个折叠块 + 顶部占比条，只有内容不一样
        // （用户 m33271 图4）。内容仍是模型收到的那串原文，不加任何装饰性分段
        // 标题；编号跨段连续，和美化档的 ①②③④ 一一对得上。
        // 注意这里必须用 += ：占比条是上面拼好的，用 = 会把它整个吞掉。
        var allMsgs = partition.messages;
        var rh = allMsgs.slice(0, boundX);
        // 用户 m26535：② 的字符数按「真正画出来的那几条」加起来算。以前用的是请求里存的
        // 历史字符总数，边界一挪（37 → 32 条）它还挂在旧数上，标题就成了「32 条 · 4016 字符」。
        var charsRh = partition.historyChars;
        if(!isTrimSnap){
        // v3.90（用户 m05623 第 2 条）：裁切快照没有 system，这一块只留一片空白，整段不画。
          h += rawBlock("① 系统提示词",
                        "· " + secNum("system", stat.length) + " 字符 · " + statNames.length +
                        " 项 · 固定系统内容；缓存状态以轨迹 token 统计为准",
                        stat || "（这一轮没有 system 内容）", "");
        }

        var histRaw = "";
        // ② 按轮次归属；本地连续前缀仅独立提示，不是服务商 token 缓存实测。
        var preLenRaw = partition.prefixKnown ? partition.prefixLen : -1;
        var hitsRaw = Math.max(0,Math.min(boundX,preLenRaw)), coldRaw = boundX-hitsRaw;
        var n = 0;
        rh.forEach(function(m, i){
          // v4.33：原来这里按 f 位把消息搬去 ③，② 会被搬空；现在一条都不搬。
          n += 1;
          // v4.27（用户 m22596）：原文模式只放原文，不再插「-- 下面 N 条不命中缓存 --」
          // 这种说明行；命中/未命中看 ② 的标题就够。
          var rbody = rawContent(m.content);
          if(m.role === "assistant"){
            // v4.01（用户 m11166 图1/图2）：决策注记的原文要写成
            // 「[HH:MM][对当前聊天消息的决策建议] 判定不回（理由）」，
            // 不能顶着 bot 的名字、正文还留着「[决策记录] HH:MM」这一截。
            // v4.27（用户 m22571）：原文里不许顶 bot 的名字，认得出是注记就用它的固定名。
            var nm9 = noteNameOf(m);
            var mm9 = String(rbody).match(/^\s*\[决策记录\]\s*(\d{1,2}:\d{2})?\s*/);
            if(mm9 || nm9 === DEC_NOTE_NAME){
              rbody = botRawHead(mm9 ? String(rbody).slice(mm9[0].length) : rbody,
                                 DEC_NOTE_NAME, (mm9 && mm9[1]) || "");
            } else {
              rbody = botRawHead(rbody, nm9, "");
            }
          }
          histRaw += "[" + n + "] " + m.role + "\n" + rbody + "\n";
          if(m.tool_calls && m.tool_calls.length) histRaw += "[tool_calls] " + JSON.stringify(m.tool_calls) + "\n";
          histRaw += "\n";
        });
        // v3.93（用户 m07570 图7）：决策快照的 ② 改叫「聊天历史」，③ 才是本轮输入。
        h += rawBlock("② 历史消息",
                      "· " + rh.length + " 条 · " + charsRh + " 字符" +
                      (preLenRaw >= 0
                        ? (" · 本地前缀复用 " + hitsRaw + " 条 · 非前缀 " + coldRaw + " 条" + (r.cache_compare_source === "legacy_record_estimate" ? "；旧记录估算，非缓存实测" : "；非缓存实测"))
                        : (r.cache_compare_source === "unknown" ? " · 本地前缀未知" : "")),
                      histRaw || "（这一轮没有历史）", "");

        if(!isTrimSnap){
          var currentRaw = "";
          function appendCurrentRaw(m){
            if(!m) return;
            // 用户 m20315 第 3 条：工具回调在原文里也要是 assistant——正文剥掉
            // 「【工具回调 xxx】」声明，交给 botRawHead 补成「[HH:MM] 【工具回调·xxx】」。
            var toolR = isToolNoteMsg(m);
            var body = toolR ? toolNoteBody(contentText(m.content)) : rawContent(m.content);
            if(m.role === "assistant") body = botRawHead(body, noteNameOf(m), toolR ? hhmm(m.ts) : "");
            n += 1;
            currentRaw += "[" + n + "] " + String(m.role || "user") + "\n" + body + "\n";
            if(m.tool_calls && m.tool_calls.length) currentRaw += "[tool_calls] " + JSON.stringify(m.tool_calls) + "\n";
            currentRaw += "\n";
          }
          var dynamicRow = reloc ? {role:"user",content:"<system_reminder>\n" + reloc + "\n</system_reminder>"} : null;
          if(dynamicRow && pos.actual !== "tail") appendCurrentRaw(dynamicRow);
          partition.currentRows.forEach(appendCurrentRaw);
          if(dynamicRow && pos.actual === "tail") appendCurrentRaw(dynamicRow);
          h += rawBlock("③ 本轮消息", "· " + partition.currentCount + " 条 · " + partition.currentChars +
                        " 字符 · 本轮送入（含重送）；按轮次归属，非缓存实测",
                        currentRaw || "（这一轮没有单独的本轮消息）", "");
        }

        if(!isTrimSnap){
        // v3.90（用户 m05623 第 2 条）：裁切快照没有 agent 轨迹，④ 和那句空提示一起不画。
          // v3.93（用户 m07570 图7）：决策快照的 ④ 是决策模型那一次调用（trace 由
          // write_decision_snapshot 落盘时补），模板跟回复记录完全一样。
          // ④ 的统计胶囊跟美化档一模一样（用户 m33400 图1~3：说好三档统一，
          // 结果原文只有一句「思考 · 工具调用 · 工具返回」，没有步数/耗时/命中率）
          // v3.97（用户 m09615 图4）：轨迹挂在快照记录（r）上，d 只是外层壳；
          // 以前读 d.trace 永远是空的，页面才一直说「这一轮没有轨迹记录」。
          var t4 = r.trace || {};
          var tchips = traceChips(t4);
          var trRaw = traceRawText(r.trace);
          // 不再列「最后发出去的消息」：④ 就是模型这一步的输出，用户 m47114 说重复（m33271 同款意见）
          h += rawBlock("④ agent 轨迹", "· 思考 · 工具调用 · 工具返回",
                        (trRaw || "（这一轮没有 agent 轨迹）"), "", tchips);
        }

        target.innerHTML = "<td colspan='" + cspan + "'>" + h + "</td>";
        target.setAttribute("data-loaded", "1");
        // v4.02（用户 m11629 图2）：详情里的转发卡要跟窗口一样只露三行
        fitForwardCards(target);
        // 用户 m28299 图3：详情里的引用也要补（以前这块 DOM 不在扫描范围里）
        fillQuoteMiss();
        restoreRowFolds(target, name);
        return;
      }
      // 四段的标题与副标题三档共用一套文案（用户 m33400：说好统一，结果还是有区别）。
      // ②③ 按轮次归属，独立说明本地前缀对比；均不等同服务商缓存。
      // v3.88（用户 m05012 第 4 条）：没有 system 内容时不画「展开」按钮。原来无条件
      // 渲染，可下面只有一行「这一轮没有 system 内容」，点开没有 body 就再也关不上。
      if(!isTrimSnap){
      // v3.90（用户 m05623 第 2 条）：裁切快照没有 system，这一块只留一片空白，整段不画。
        h += "<div class='seqh'>① 系统提示词" +
             " <span class='dim'>· " + secNum("system", stat.length) + " 字符 · " +
             statNames.length + " 项 · 固定系统内容；缓存状态以轨迹 token 统计为准</span>" +
             "<span class='grow'></span>" +
             (stat ? "<span class='mini' data-fold='sys'>展开</span>" : "") + "</div>";
        if(stat){
          h += "<div class='scroll' data-foldbody='sys' style='display:none'>" +
               systemCardHtml(stat, VIEW) + "</div>";
        } else {
          h += "<div class='empty'>这一轮没有 system 内容</div>";
        }
      }
      if(errs.length){
        h += "<div class='hint' style='color:var(--bad)'>有段渲染失败（已降级为静态文本）：" + esc(errs.join("；")) + "</div>";
      }
      // 三档共享 canonical 消息数组及轮次边界；显示投影只提供媒体/装饰。
      var bound2 = boundX;
      var hist = partition.messages.slice(0, bound2);
      // 用户 m10400 图5：② 历史消息里的转发被引用时，引用块里只剩 [Forward] 这个空标记，
      // 内容其实就在同一份快照的历史里。先把历史编进引用索引（③ 本轮优先，不覆盖），
      // 引用块才能按 id 找到那条转发、把里面的聊天记录画出来。
      Object.keys(QUOTE_HIST_ADDED).forEach(function(k){
        if(QUOTE_IDX[k] === QUOTE_HIST_ADDED[k]) delete QUOTE_IDX[k];
      });
      QUOTE_HIST_ADDED = {};
      var histIdx2 = buildQuoteIdx(hist.map(function(m, i){
        var pj5 = snapshotDisplay(m, (r.display_messages || [])[i]);
        var d5 = pj5.view || {};
        var t5 = typeof d5.text === "string" ? d5.text : contentText(m.content);
        // raw 直接给 content：displayTextOf 只挑 text 段，不会把 JSON 串当引用正文。
        return {role: m.role, text: t5, raw: m.content, chars: t5.length,
                media: d5.media || m.media || [], display: pj5.display,
                who: m.who || "", name: m.name || ""};
      }), []);
      Object.keys(histIdx2).forEach(function(k){
        if(QUOTE_IDX[k]) return;
        QUOTE_IDX[k] = histIdx2[k];
        QUOTE_HIST_ADDED[k] = histIdx2[k];
      });
      var preLen = partition.prefixKnown ? partition.prefixLen : -1;
      var hits2 = Math.max(0,Math.min(bound2,preLen)), miss2 = bound2-hits2;
      var chars2 = partition.historyChars;
      h += "<div class='seqh'>② 历史消息" +
             " <span class='dim' data-histhead='1'>· " + hist.length + " 条 · " + chars2 +
           " 字符" + (preLen >= 0 ? (" · 本地前缀复用 " + hits2 + " 条 · 非前缀 " + miss2 + " 条" + (r.cache_compare_source === "legacy_record_estimate" ? "；旧记录估算，非缓存实测" : "；非缓存实测")) : (r.cache_compare_source === "unknown" ? " · 本地前缀未知" : "")) + "</span>" +
           "<span class='grow'></span><span class='mini' data-fold='hist'>展开</span></div>";
      h += "<div class='scroll' data-foldbody='hist' style='display:none'>";
      if(!hist.length) h += "<div class='empty'>这一轮没有历史</div>";
      var lastTs2 = "";
      // v4.30（用户 m14924）：② 的条数/字符改用「真正画出来的那几条」——f 位的本轮
      // 输入已经搬去 ③，标题还按 messages 全长写就会多算（96 条却只画 95 张）。
      var drawnChars2 = 0;
      var grpKey2 = null, grpRole2 = null, grpBuf2 = "", grpHead2 = "", grpNote2 = "", grpTools2 = 0;
      var flushGrp2 = function(){
        if(!grpBuf2) return;
        var ghead2 = grpHead2
          ? (grpTools2 > 0 ? grpHead2.replace("工具 ×\u0001", "工具 ×" + grpTools2)
                           : grpHead2.replace("<span class='chb'>工具 ×\u0001</span>", ""))
          : "";
        h += "<div class='mgrp " + esc(grpRole2 || "user") + "'>" + ghead2 + grpBuf2 + "</div>";
        grpBuf2 = ""; grpHead2 = ""; grpTools2 = 0;
      };
      var kKeep = 0;
      hist.forEach(function(m, i){
        // 历史只画 canonical 轮次边界内消息，不按缓存前缀挪动。
        var projected = snapshotDisplay(m, (r.display_messages || [])[i]);
        var dm = projected.view;
        var c = typeof dm.text === "string" ? dm.text : contentText(m.content);
        if(m.role !== "user") c = tidyBody(c);
        if(m.role === "user"){
          var q0 = splitMetaAll(c)[0];
          if(q0 && q0.meta && q0.meta.ts) lastTs2 = shortTs(q0.meta.ts);
        }
        // 用户 m21787 图3/图4：记录里的 media 是发给模型的那一份（动图九宫格 / 静态压缩），
        // 美化档要显示聊天里原本那张；后端按 message_id 回索引取好放在 display.orig_media。
        var orig2 = (VIEW === "pretty" && projected.display && projected.display.orig_media && projected.display.orig_media.length)
          ? projected.display.orig_media : null;
        if(orig2) c = stripDeriveNote(c);
        var media2 = orig2 || dm.media || m.media || [];
        var mm = {role: m.role, text: c, raw: rawContent(m.content), chars: c.length, media: media2, origMedia: !!orig2,
                  display: projected.display, who: m.who || "", name: m.name || "",
                  tools: dm.tools === undefined ? (m.tool_calls || []).length : dm.tools, tool_call_id: m.tool_call_id || "",
                  files: m.files || (dm && dm.files) || [],
                  tc: dm.tc || tcOf(m)};
        var gk = groupKeyOf(mm, i);
        if(gk !== grpKey2){
          flushGrp2();
          grpKey2 = gk; grpRole2 = m.role || "user";
          var multi2 = multiOf(mm);
          grpNote2 = (VIEW === "mixed" && (m.role === "user" || m.role === "assistant"))
            ? mixNoteOf(mm) : "";
          // 对照档每条都要有「角色 · N 字 · 原文」表头；一条里拼了好几条群消息时
          // 名字不止一个，就只写角色（用户 m51324 ⑧：②③ 缺表头）
          grpHead2 = ((m.role === "user" || m.role === "assistant") && (VIEW === "mixed" || !multi2))
            ? grpHeadHtml(m.role, multi2 ? "" : whoOf(mm), multi2 ? "" : uidOf(mm), multi2 ? false : youOf(mm), grpNote2, "\u0001") : "";
        }
        if(mm.tools) grpTools2 += Number(mm.tools) || 0;
        drawnChars2 += (c || "").length;
        grpBuf2 += msgCards(mm, (++kKeep), VIEW, lastTs2, true);
        // v3.58（用户 m16875 图2）：调用记录已经在 msgCards 的正文末尾画过了，
        // 这里原来又补一份，同一条消息出现两套；卡片外那份删掉。
      });
      flushGrp2();
      h += "</div>";
      if(!isTrimSnap){
        var curHint = "本轮送入（含重送）；按轮次归属，非缓存实测";
        h += "<div class='seqh'>③ 本轮消息 <span class='dim'>· " + partition.currentCount +
             " 条 · " + partition.currentChars + " 字符 · " + curHint + "</span>" +
             "<span class='grow'></span><span class='mini' data-fold='final'>展开</span></div>";
        h += "<div class='scroll' data-foldbody='final' style='display:none'>";
        var atTail = (pos.actual === "tail"), relocBlock = "";
        if(reloc.length){
          var relocRaw = "<system_reminder>\n" + reloc + "\n</system_reminder>";
          relocBlock = systemCardHtml(VIEW === "mixed" ? relocRaw : reloc, VIEW);
        }
        var msgBlock = "", grpKey3 = null, grpRole3 = null, grpBuf3 = "", grpHead3 = "";
        var flushGrp3 = function(){
          if(!grpBuf3) return;
          msgBlock += "<div class='mgrp " + esc(grpRole3 || "user") + "'>" + grpHead3 + grpBuf3 + "</div>";
          grpBuf3 = ""; grpHead3 = "";
        };
        partition.currentRows.forEach(function(m, idx){
          if(!m) return;
          var role3 = String(m.role || "user"), text3 = tsShortOf(contentText(m.content));
          // 用户 m20315 第 2 条：工具回调在 ③ 里要按 assistant 画——名字用工具名，
          // 正文剥掉「【工具回调 xxx】」声明，图跟着卡片一起出。
          var tool3 = isToolNoteMsg(m);
          if(tool3) text3 = toolNoteBody(text3);
          if(role3 !== "user") text3 = tidyBody(text3);
          var raw3 = tool3 ? text3 : tsShortOf(rawContent(m.content));
          // 用户 m20315 图1：工具回调带的图就是它自己的（nai 发出去的那张），
          // 不能画成「归属未确认的附件」；正文里的 [图片] 占位由缩略图顶掉。
          var media3 = m.media || [], disp3 = snapshotDisplay(m).display;
          // 用户 m21787 图3/图4：③ 同理——美化档用聊天里原本那张图，不用发给模型的派生图。
          var orig3 = (VIEW === "pretty" && disp3 && disp3.orig_media && disp3.orig_media.length) ? disp3.orig_media : null;
          if(orig3){ media3 = orig3; text3 = stripDeriveNote(text3); }
          if(tool3 && media3.length){
            text3 = stripMediaTags(text3, media3.length);
            disp3 = {own_media_known:true, own_media:media3, unassigned_media:[],
                     own_body:text3, direct_reply:null, source:"tool_note"};
          }
          var mm3 = {role:role3,who:tool3 ? String(m.name || "") : (m.who || m.name || ""),name:m.name || "",text:text3,
            raw:raw3,media:media3,display:disp3,chars:text3.length,origMedia:!!orig3,
            files:m.files || [],
            tools:(m.tool_calls || []).length,tool_call_id:m.tool_call_id || "",tc:tcOf(m)};
          var gk3 = groupKeyOf(mm3, "f" + idx);
          if(gk3 !== grpKey3){
            flushGrp3(); grpKey3 = gk3; grpRole3 = role3;
            var multi3 = multiOf(mm3);
            grpHead3 = grpHeadHtml(role3,multi3?"":whoOf(mm3),multi3?"":uidOf(mm3),multi3?false:youOf(mm3),VIEW==="mixed"?mixNoteOf(mm3):"");
          }
          grpBuf3 += msgCards(mm3, idx + 1, VIEW, "", true);
        });
        flushGrp3();
        h += atTail ? (msgBlock + relocBlock) : (relocBlock + msgBlock);
        if(!partition.currentCount) h += "<div class='empty'>这一轮没有单独的本轮消息</div>";
        h += "</div>";
      }
      if(!isTrimSnap){
        // 用户 m25513：详情这条路的记录没走列表的 _mark_cache_reported，但轨迹 totals 里带着 cache_reported，
        // 上游没回报时同样写「上游未报」，别让 ④ 的 chip 还写「缓存命中 0%」。
        var _unrep = !!(r.cache_unreported || (r.trace && r.trace.totals && r.trace.totals.cache_reported === false));
      // v3.90（用户 m05623 第 2 条）：裁切快照没有 agent 轨迹，④ 和那句空提示一起不画。
        // v3.93（用户 m07570 图7）：决策快照的 ④ 同样用 trace，不再整段跳过。
        if(VIEW === "mixed"){
          // 对照档要的是原文，④ 不能还是美化样式（用户 m51324 ⑧）
          h += rawBlock("④ agent 轨迹", "· 思考 · 工具调用 · 工具返回",
                        traceRawText(r.trace) || "（这一轮没有 agent 轨迹）", "", traceChips(r.trace || {}, _unrep));
        } else {
          var th = traceHtml(r.trace, _unrep);
          if(th){
            h += th;
          } else {
            h += "<div class='tail-note'>这一轮还没有轨迹记录。轨迹是这次新加的，开启后再跑一轮就有了。</div>";
          }
        }
      }
      target.innerHTML = "<td colspan='" + cspan + "'>" + h + "</td>";
      target.setAttribute("data-loaded", "1");
      // v4.02（用户 m11629 图2）：同上，别让转发卡在详情里铺开
      fitForwardCards(target);
      // 用户 m28299 图3：详情里的引用同样要补图 / 补原话
      fillQuoteMiss();
      restoreRowFolds(target, name);
    };
    var ckey = String(ctxSid) + "|" + name;
    if(roundRecCache[ckey]){ paint(roundRecCache[ckey]); return; }
    get(API + "/request?sid=" + encodeURIComponent(ctxSid) + "&name=" + encodeURIComponent(name)).then(function(d){
      roundRecCache[ckey] = d;
      paint(d);
    }).catch(function(e){
      target.innerHTML = "<td colspan='" + cspan + "'><div class='hint'>读取失败：" + esc(e.message) + "</div></td>";
    });
  }

  // ---------------------------------------------------------------- 改名 / 隐藏

  function startRename(btn){
    var sid = btn.getAttribute("data-rename");
    var head = btn.parentNode;
    var nm = head.querySelector(".nm");
    if(!nm || head.querySelector(".nm-edit")) return;
    var cur = aliases[sid] || "";
    var inp = document.createElement("input");
    inp.type = "text";
    inp.className = "nm-edit";
    inp.value = cur;
    inp.placeholder = shortSid(sid);
    nm.style.display = "none";
    head.insertBefore(inp, nm);
    inp.focus();
    inp.select();
    var done = false;
    function finish(save){
      if(done) return;
      done = true;
      var v = inp.value.trim();
      inp.remove();
      nm.style.display = "";
      if(!save) return;
      post(API + "/aliases?sid=" + encodeURIComponent(sid) + "&alias=" + encodeURIComponent(v))
        .then(function(d){ aliases = d.items || aliases; renderCtxList(); })
        .catch(function(err){ mdAlert("改名失败：" + err.message); });
    }
    inp.onkeydown = function(e){
      if(e.key === "Enter"){ e.preventDefault(); finish(true); }
      else if(e.key === "Escape"){ e.preventDefault(); finish(false); }
    };
    inp.onblur = function(){ finish(true); };
  }

  // 点垃圾桶先问一句：只隐藏，还是把插件记录也删了
  function askRemove(sid){
    var mask = document.createElement("div");
    mask.className = "mask";
    mask.innerHTML =
      "<div class='dlg'>" +
        "<h3>「" + esc(displayName(sid)) + "」</h3>" +
        "<p>隐藏：只从这个页面拿掉，日志、窗口、请求记录都留着，随时能在设置页恢复。</p>" +
        "<p class='p2'>删除记录：把这个会话在本插件里产生的东西全删掉 —— 消息日志、窗口日志、" +
        "请求记录、备注名、单群配置、中期摘要、攒着的计数。删了恢复不了；" +
        "框架自己的聊天记录不受影响。</p>" +
        "<div class='dlg-btns'>" +
          "<button data-act='cancel'>取消</button>" +
          "<button data-act='hide'>只隐藏</button>" +
          "<button class='danger' data-act='del'>删除记录</button>" +
        "</div>" +
      "</div>";
    document.body.appendChild(mask);
    function close(){ if(mask.parentNode) mask.parentNode.removeChild(mask); }
    mask.addEventListener("click", function(e){
      if(e.target === mask){ close(); return; }
      var btn = e.target.closest ? e.target.closest("[data-act]") : null;
      if(!btn) return;
      var act = btn.getAttribute("data-act");
      close();
      if(act === "hide") hideSession(sid);
      else if(act === "del") deleteSession(sid);
    });
  }

  function hideSession(sid){
    post(API + "/session-hide?sid=" + encodeURIComponent(sid)).then(function(){
      ctxSessions = ctxSessions.filter(function(s){ return s.sid !== sid; });
      renderCtxList();
    }).catch(function(e){ mdAlert("隐藏失败：" + e.message); });
  }

  function deleteSession(sid){
    post(API + "/session-delete?sid=" + encodeURIComponent(sid)).then(function(d){
      var c = d.counts || {}, parts = [];
      if(c.decisions) parts.push("消息日志 " + c.decisions + " 条");
      if(c.windows) parts.push("窗口日志 " + c.windows + " 条");
      if(c.requests) parts.push("请求记录 " + c.requests + " 份");
      // v3.64（用户 m18870）：轨迹并进请求快照，删会话不再单列轨迹。
      if(c.media) parts.push("图片副本 " + c.media + " 张");
      if(c.aliases) parts.push("备注名");
      if(c.overrides) parts.push("单群配置");
      if(c.summary) parts.push("中期摘要");
      if(c.state) parts.push("计数存档");
      mdAlert("已删除「" + displayName(sid) + "」的插件记录：\n" + (parts.join("、") || "（本来就没有记录）"));
      ctxSessions = ctxSessions.filter(function(s){ return s.sid !== sid; });
      renderCtxList();
    }).catch(function(e){ mdAlert("删除失败：" + e.message); });
  }

  // ---------------------------------------------------------------- 交互

  function setAuto(on){
    if(autoTimer){ clearInterval(autoTimer); autoTimer = null; }
    if(on){ autoTimer = setInterval(function(){ if(!document.hidden) load(); }, 5000); startStateTimer(); }
    else stopStateTimer();
    setSwitch("sw-auto", !!on);
  }

  // 5s 主刷新与 1s 状态刷新共用一个在途请求，慢网也不叠发。
  function paintFromCache(){
    var ov = cacheGet("ov");
    // 缓存直出时不动副标题：改成「刷新中…」比真时间短一大截，右边热重载按钮会跳一下
    //（用户 m07864 第 1 条）。占位文案「更新于 --:--:--」本来就等宽，等接口回来再填。
    if(ov){ try{ renderCards(ov, true); }catch(e){} }
    var cs = cacheGet("sessions");
    if(cs){ try{
      ctxSessions = cs.items || [];
      aliases = cs.aliases || aliases;
      if(cs.health) renderHealth(cs.health);
    }catch(e){} }
    // 窗口和请求也直出（v3.19）：只画卡片时首屏才 1013px，真实数据 98ms 后把页面撑到
    // 4163px，这一下布局跳变才是「从设置切回首页卡一下」的来源（实测帧间隔 241~368ms）。
    var wc = cacheGet("win"), rc = cacheGet("rounds");
    if(wc && wc.sid && (!ctxSessions.length || ctxSessions.some(function(s){ return s.sid === wc.sid; }))){
      ctxSid = wc.sid;
      ctxWin = wc.win || null;
      ctxTrims = wc.trims || [];
      ctxTrimKeep = Number(wc.trimKeep || 0) || 0;
      ctxSnapKeep = Number(wc.snapKeep || 0) || ctxSnapKeep;
      ctxSnapTotal = Number(wc.snapTotal || 0) || ctxSnapTotal;
      lastWinSig = "";
      lastTrendSig = "";
    }
    if(rc && rc.sid && rc.sid === ctxSid){
      ctxRounds = rc.items || [];
      ctxRoundsFull = true;
    }
    var dc = cacheGet("decisions");
    if(dc && ctxSid && dc.sid === ctxSid && Array.isArray(dc.items) && dc.items.length){
      ctxDecisions = dc.items;
      ctxDecisionsSid = ctxSid;
      // 缓存只负责首屏直出，仍要立刻向后台核对最新记录。
      ctxDecisionsAt = 0;
    }
    // v4.83（用户 m08855）：走势图的数据也缓存（trendreq/trenddec），首屏直接画上次的完整趋势，
    // 不再先画缓存里的 40 根、再被决策和请求两次补齐（标题会跳 40 → 150 → 220）。
    // 老缓存没有这两份时先不画，等两路都回来一次性画（renderCtxWindow 里的骨架）。
    var tr = cacheGet("trendreq"), td = cacheGet("trenddec");
    if(tr && tr.version === 3 && tr.sid === ctxSid && td && td.version === 3 && td.sid === ctxSid){
      ctxTrendRounds = tr.items || [];
      ctxSnapKeep = Number(tr.snapKeep || 0) || ctxSnapKeep;
      ctxSnapTotal = Number(tr.snapTotal || 0) || ctxSnapTotal;
      ctxDecTrend = td.items || [];
      ctxDecFolded = Number(td.folded || 0) || 0;
      ctxTrendReq = true;
      ctxTrendDec = true;
    } else {
      ctxTrendRounds = [];
      ctxDecTrend = [];
      ctxDecFolded = 0;
    }
    if(rc && rc.keep) ctxKeep = Number(rc.keep) || 0;
    if(cs){ try{ renderCtxList(); }catch(e){} }
    if(ctxWin){ try{ renderCtxWindow(ctxWin); }catch(e){} }
    if(ctxRounds.length){ try{ renderCtxRounds(ctxRounds); }catch(e){} }
    // 有同会话的旧列表就直接画；没有时由渲染函数显示加载态，等后台确认。
    if(ctxPane === "decisions") renderCtxDecisions(ctxDecisions);
  }

  // 用户 m01874：模型没配时首页「设置」按钮亮个红点，点进去设置页里再逐级指到具体那一行。
  // 30 秒问一次后端就够了，别跟着 5 秒的刷新一直打接口。
  var modelDotAt = 0;
  function paintModelDot(missing){
    var b = document.getElementById("btn-settings");
    if(!b) return;
    var old = b.querySelector(".mdot");
    if(old && old.parentNode) old.parentNode.removeChild(old);
    if(!missing || !missing.length) return;
    var names = missing.map(function(it){ return it.label || it.key || ""; }).filter(function(s){ return s; });
    b.insertAdjacentHTML("beforeend", "<span class='mdot' title='" + esc(names.join("、") + " 还没配") + "'></span>");
  }
  function loadModelDot(){
    var now = Date.now();
    if(now - modelDotAt < 30000) return;
    modelDotAt = now;
    get(API + "/model-check").then(function(d){
      paintModelDot((d && d.missing) || []);
    }).catch(function(){});
  }

  function load(){
    // 后台标签页不轮询：5 秒一次的全量刷新在看不见的时候纯属浪费带宽。
    if(document.hidden) return;
    loadOverview();
    loadModelDot();
    getContextSessions().then(function(d){
      cachePut("sessions", {items: d.items || [], aliases: d.aliases || {}, health: d.health || null});
      ctxSessions = d.items || [];
      aliases = d.aliases || aliases;
      if(d.health) renderHealth(d.health);
      renderCtxList();
      if(ctxSid && ctxSessions.some(function(s){ return s.sid === ctxSid; })){
        if(ctxPane === "decisions") loadCtxDecisions(ctxSid);
        refreshCtxWindow(ctxSid);
        refreshCtxRounds(ctxSid);
        // 用户 m14026 第 4 条：回复记录页要的是全量表，趁用户还停在窗口页时后台先拉一次，
        // 等他点过去就是现成的表，不用当场等接口（refreshCtxRounds 内部已有在途去重）。
        if(!ctxRoundsFull && ctxPane !== "rounds") setTimeout(function(){
          if(ctxSid && !ctxRoundsFull) refreshCtxRounds(ctxSid, true);
        }, 1200);
        // 用户 m17375 第 5 条：决策记录页点进去会先空一下再长出来（接口 300ms）。
        // 跟回复记录一样，趁用户还停在别的页签时后台先拉一次，点过去就是现成的。
        // 每个会话只预取一次，别让 5 秒的刷新一直白拉 240KB。
        if(ctxPane !== "decisions" && ctxDecPrefetched !== ctxSid) setTimeout(function(){
          if(ctxSid && ctxPane !== "decisions" && ctxDecPrefetched !== ctxSid){
            ctxDecPrefetched = ctxSid;
            loadCtxDecisions(ctxSid);
          }
        }, 2200);
      }
      else if(ctxSessions.length) openCtx(ctxSessions[0].sid);
      else {
        invalidateCtxRequests();
        ctxSid = ""; ctxWin = null; ctxSnap = null; ctxRounds = []; ctxTrendRounds = []; ctxTrims = []; ctxTrimKeep = 0; ctxSnapKeep = 0; ctxSnapTotal = 0;
        renderCtxWindow(null); renderCtxRounds([]);
      }
    }).catch(function(e){
      document.getElementById("ctx-list").innerHTML = '<div class="empty">读取失败：' + esc(e.message) + "</div>";
    });
  }

  // 状态点每 1 秒自己刷一次：只改那几个字，不重画列表，
  // 免得用户正看着的时候列表跳一下（重画会丢滚动位置和悬停状态）。
  // 受「自动刷新」开关管：关掉就整个停，否则那个开关等于没做（审查 bug）
  function startStateTimer(){
    if(!stateTimer) stateTimer = setInterval(function(){
      if(document.hidden) return;
      // 用户 m11091 第 8 条：滚动时这一秒的活全跳过——改文字也会触发重排，
      // 滚起来就是一顿一顿的。滚动停下后下一拍照常。
      if(isScrolling()) return;
      var map = {};
      ctxSessions.forEach(function(s){ map[s.sid] = s; });
      paintStateNodes(map);
      tickEtas();
      pollStates();
    }, 1000);
  }
  function pollStates(){
    if(document.hidden) return;
    var list = document.getElementById("ctx-list");
    if(!list || !list.offsetParent) return;   // 没在看上下文页就不拉
    getContextSessions(true).then(function(d){
      if(d.health) renderHealth(d.health);
      var map = {};
      (d.items || []).forEach(function(s){ map[s.sid] = s; });
      paintStateNodes(map);
      var _cur = ctxSid ? map[ctxSid] : null;
      if(_cur){
        var _sig = (Number(_cur.rounds) || 0) + ":" + (Number(_cur.last_ts) || 0);
        var _old = pollRoundSig[ctxSid];
        pollRoundSig[ctxSid] = _sig;
        if(_old !== undefined && _old !== _sig){
          if(ctxPane === "decisions") loadCtxDecisions(ctxSid, true);
          else refreshCtxRounds(ctxSid);
        }
      }
      (ctxSessions || []).forEach(function(s){
        var n = map[s.sid];
        if(!n) return;
        s.busy = n.busy; s.stalled = n.stalled;
        s.queued = n.queued; s.busy_seconds = n.busy_seconds; s.busy_since = n.busy_since;
        s.retry_in = n.retry_in; s.retry_count = n.retry_count; s.last_error = n.last_error;
        s.muted = n.muted; s.muted_skipped = n.muted_skipped;
        s.last_mark = n.last_mark; s.last_mark_ago = n.last_mark_ago;
        s.counter = n.counter; s.threshold = n.threshold; s.trigger_mode = n.trigger_mode;
      });
    }).catch(function(){});
  }

  async function togglePlugin(){
    var node = document.getElementById("sw-plugin");
    if(node) node.classList.add("busy");
    try{
      var d = await get(CORE + "/config");
      var cfg = d.config || {};
      cfg.enabled = !pluginEnabled;
      var r = await fetch(CORE + "/config", {
        method: "PUT", credentials: "same-origin",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({config: cfg})
      });
      if(!r.ok) throw new Error("保存失败 HTTP " + r.status);
      var r2 = await fetch(CORE + "/reload", {method: "POST", credentials: "same-origin"});
      if(!r2.ok) throw new Error("重载失败 HTTP " + r2.status);
      setTimeout(load, 800);
    }catch(e){
      mdAlert("切换失败：" + e.message);
    }finally{
      if(node) node.classList.remove("busy");
    }
  }

  document.getElementById("ctx-list").addEventListener("click", function(e){
    // 胶囊在会话项里面，先判它，否则点胶囊会顺带切会话
    var cfgBtn = e.target.closest ? e.target.closest("[data-cfg]") : null;
    if(cfgBtn){
      e.stopPropagation();
      var cfgSid = cfgBtn.getAttribute("data-cfg");
      location.href = "/page/plugin/" + encodeURIComponent(PLUGIN_ID) +
        "/settings/?sid=" + encodeURIComponent(cfgSid);
      return;
    }
    var swm = e.target.closest ? e.target.closest("[data-swm]") : null;
    if(swm){
      e.stopPropagation();
      setSessionEnabled(swm.getAttribute("data-swm"), !swm.classList.contains("on"));
      return;
    }
    var ren = e.target.closest ? e.target.closest("[data-rename]") : null;
    if(ren){ e.stopPropagation(); startRename(ren); return; }
    var del = e.target.closest ? e.target.closest("[data-hide]") : null;
    if(del){ e.stopPropagation(); askRemove(del.getAttribute("data-hide")); return; }
    var fld = e.target.closest ? e.target.closest("[data-foldsid]") : null;
    if(fld){ e.stopPropagation(); toggleFoldSid(fld.getAttribute("data-foldsid")); return; }
    var gfl = e.target.closest ? e.target.closest("[data-grpfold]") : null;
    if(gfl){ e.stopPropagation(); toggleFoldGroup(gfl.getAttribute("data-grpfold")); return; }
    var it = e.target.closest ? e.target.closest("[data-ctx]") : null;
    if(it) openCtx(it.getAttribute("data-ctx"));
  });
  document.getElementById("ctx-rounds").addEventListener("click", function(e){
    var pgBtn = e.target.closest ? e.target.closest("button[data-pg]") : null;
    if(pgBtn){
      if(pgBtn.disabled) return;
      var act = pgBtn.getAttribute("data-pg");
      // 用户 m07111 图3：同一块 #ctx-rounds 里放着两张表，页脚按钮得看自己属于哪一张。
      var pgWrap = (pgBtn.closest ? pgBtn.closest(".pg") : null);
      if(pgWrap && pgWrap.getAttribute("data-pgpane") === "decisions"){
        var dPages2 = Math.max(1, Math.ceil(ctxDecisions.length / CTX_PAGE_SIZE));
        if(act === "first") ctxDecisionPage = 0;
        else if(act === "prev") ctxDecisionPage -= 1;
        else if(act === "next") ctxDecisionPage += 1;
        else if(act === "last") ctxDecisionPage = dPages2 - 1;
        if(ctxDecisionPage < 0) ctxDecisionPage = 0;
        if(ctxDecisionPage > dPages2 - 1) ctxDecisionPage = dPages2 - 1;
        renderCtxDecisions(ctxDecisions, true);
        return;
      }
      var pages = Math.max(1, Math.ceil(ctxRounds.length / CTX_PAGE_SIZE));
      if(act === "first") ctxRoundPage = 0;
      else if(act === "prev") ctxRoundPage -= 1;
      else if(act === "next") ctxRoundPage += 1;
      else if(act === "last") ctxRoundPage = pages - 1;
      renderCtxRounds(ctxRounds, true);
      return;
    }
    // v4.42（用户 m24522 图6）：记录详情里的转发卡以前点不动——[data-fwd] 只在窗口面板的
    // winClick 里接，这边没人管。补一条，跟窗口里一样弹大框。
    var fw2 = e.target.closest ? e.target.closest("[data-fwd]") : null;
    if(fw2 && !(e.target.closest && e.target.closest("a"))){ openFwdPop(fw2); return; }
    // 详情里的「展开全文」和「收起历史」以前点不动——事件只绑在窗口面板上，请求表这边没人接
    var more = e.target.closest ? e.target.closest("[data-more]") : null;
    if(more){ toggleMore(more); return; }
    var fold = e.target.closest ? e.target.closest("[data-fold]") : null;
    if(fold){ toggleFold(fold); return; }
    // 结果格里的「重试 N 次 · 已成功」：点它直接打开重试那一轮的记录（回复就在那份快照里）
    var rl = e.target.closest ? e.target.closest("[data-round-link]") : null;
    if(rl){ e.stopPropagation(); openRoundByName(rl.getAttribute("data-round-link")); return; }
    var tr = e.target.closest ? e.target.closest("tr.round-row") : null;
    if(tr) toggleRequestRow(tr.getAttribute("data-round"));
  });

  document.getElementById("sw-auto").onclick = function(){ setAuto(!autoTimer); };
  document.getElementById("sw-plugin").onclick = togglePlugin;
  // v3.69（用户 m21003 第 1 条）：撤掉切页时的「打开中…」＋整页变淡＋40ms 延迟。
  // 用户觉得那个过渡是多余的多余闪动，直接跳更干净。
  document.getElementById("btn-settings").onclick = function(){
    try{ localStorage.setItem("mdp_page", "settings"); }catch(x){}
    location.href = "/page/plugin/" + encodeURIComponent(PLUGIN_ID) + "/settings/";
  };
  // 用户 m04471 第 1 条：首页右上角加热重载——以前改完设置/代码要回桌面端重载，来回切很烦。
  // 走的就是「设置页切总开关」用的那个 POST /reload，轻量、不动配置。
  async function reloadPlugin(){
    var b = document.getElementById("btn-reload");
    if(b){ b.disabled = true; b.classList.add("busy"); }
    try{
      var r = await fetch(CORE + "/reload", {method: "POST", credentials: "same-origin"});
      if(!r.ok) throw new Error("HTTP " + r.status);
      var sub = document.getElementById("subtitle");
      if(sub) sub.textContent = "已重载，正在刷新…";
      setTimeout(load, 600);
    }catch(e){
      mdAlert("重载失败：" + (e && e.message ? e.message : e));
    }finally{
      if(b){ b.disabled = false; b.classList.remove("busy"); }
    }
  }
  // v3.71（用户 m23596 第 4 条）：设置页拨完开关马上点回首页时，只来得及把配置
  // PUT 出去（keepalive 补发），插件还没来得及热重载。它在 localStorage 留了标记，
  // 首页打开时补一次 reload，免得「配置改了但行为没变」。
  (function(){
    var need = "0";
    try{ need = localStorage.getItem("mdp_need_reload") || "0"; }catch(x){}
    if(need !== "1") return;
    try{ localStorage.removeItem("mdp_need_reload"); }catch(x){}
    fetch(CORE + "/reload", {method: "POST", credentials: "same-origin"})
      .then(function(r){ if(!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .catch(function(){ try{ localStorage.setItem("mdp_need_reload", "1"); }catch(x){} });
  })();
  document.getElementById("btn-reload").onclick = reloadPlugin;
  // 三档开关现在长在窗口标题行里（每次重画都会重建），只能用事件委托
  document.addEventListener("click", function(e){
    if(e.target && e.target.id === "hbar-fix"){ resumeAllMuted(); return; }
    if(e.target && e.target.id === "hbar-wd"){ ackWatchdog(); return; }
    if(e.target && e.target.id === "hbar-fail"){ ackFail(); return; }
    var b = (e.target && e.target.closest) ? e.target.closest("button[data-view]") : null;
    if(b) setView(b.getAttribute("data-view"));
  });

  window.addEventListener("message", function(e){
    var d = e.data;
    if(!d || d.channel !== CHANNEL) return;
    // 只认外层窗口发来的消息（审查报告）：原来只比对 channel 字串，同源页面里
    // 任何窗口都能改主题。e.source 判父窗口在反代/sandbox 下也稳。
    if(e.source && e.source !== parent) return;
    if(d.kind === "context" && d.context){
      var th = d.context.isDark ? "dark" : "light";
      document.documentElement.setAttribute("data-theme", th);
      try{ localStorage.setItem("mdp_theme", th); }catch(x){}
    }
  });
  try { parent.postMessage({channel: CHANNEL, kind: "ready"}, "*"); } catch(err) {}

  paintViewSw();
  watchFolds();
  ltWatch();
  paintFromCache();
  load();
  setAuto(true);
