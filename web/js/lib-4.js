  var ctxDecisionsInFlightSid = "";
  // 用户 m17375 第 5 条：决策记录后台预取过一次的会话，别重复预取。
  var ctxDecPrefetched = "";

  function decImageFbTip(r){
    var why = String(r.decision_images_fallback_error || "").trim();
    var n = Number(r.decision_images_fallback_attempts) || 0;
    return "带图那次没成功，已退回纯文字重判" +
      (n ? "（带图试了 " + n + " 次）" : "") + (why ? "：" + why : "");
  }

  // 用户 m07898：主模型失败切备用时，主模型那条真实报错以前会被备用成功的结果盖掉，
  // 记录页只剩一个「备用」徽标，看不出为什么换。这里把它挂到徽标和理由的 tooltip 上。
  function decModelErrTip(r){
    var list = Array.isArray(r.model_errors) ? r.model_errors : [];
    var parts = [];
    list.forEach(function(it){
      if(!it) return;
      var err = String(it.error || "").trim();
      if(!err) return;
      var who = String(it.model || "").trim() ||
        (String(it.via || "") === "fallback" ? "备用模型" : "主模型");
      parts.push(who + "：" + err.slice(0, 200));
    });
    return parts.join("；");
  }

  function decisionFailTip(r){
    var err = String(r.error || "").trim().slice(0, 300);
    var tip = "决策没有走通" +
      (r.decision_reply ? "，已按失败放行继续回复" : "，这一轮没有继续回复") +
      (err ? "：" + err : "");
    if(r.decision_images_fallback) tip += "；" + decImageFbTip(r);
    return tip;
  }

  function decisionConcl(r){
    if(r.failed){
      return "<span class='dcon bad' title='" + esc(decisionFailTip(r)) + "'>决策失败</span>";
    }
    if(r.decision_reply){
      // 用户 m35302：建议回复之后没等到回复请求 —— 结论照旧写「建议回复」（那确实是它判的），
      // 后面挂个红标，别让人以为这条回复成功发出去了。分不清失败还是空输出，都算失败。
      var _rs = String(r.reply_state || "");
      if(_rs === "missing"){
        return "<span class='dcon'>建议回复</span> <span class='tag bad' title='" +
          esc("建议回复之后，没有等到回复请求：可能没跑起来，也可能跑到一半断了。分不清是失败还是空输出，按失败算。") +
          "'>失败</span>";
      }
      // 跑了、但什么都没产出，这是空回复，跟出错分开写。
      if(_rs === "empty"){
        return "<span class='dcon'>建议回复</span> <span class='tag neutral' title='" +
          esc("这一轮回复跑完了，但没产出能发出去的内容。") + "'>空回复</span>";
      }
      return "<span class='dcon'>建议回复</span>";
    }
    return "<span class='dcon no'>建议不回</span>";
  }

  function decisionTrendOf(items){
    var all = (items || []).filter(function(x){
      return !!(x && x.name && String(x.outcome || "") === "decision");
    });
    // 判要回只等最终回复快照，决策记录自身不另占窗口快照柱。
    var shown = all.filter(function(x){ return !x.decision_reply; });
    return {items: shown, folded: Math.max(0, all.length - shown.length)};
  }
  // v3.94（用户 m08398 图4/图5）：决策快照的 ③ 要跟回复记录同一种长相，一条条摆出来。
  // 后端存的 transcript 是一行一条（bot 自己以前说过的话前面带【名字】），这里还原成消息。
  // 2026-11-08（J）：名字从写死的【我】改成它在这个会话里的昵称，所以这里认任意【…】开头；
  // 老记录里的【我】同样落进这条规则，不用另开分支。
  function decTranscriptLines(r){
    var ups = (Array.isArray(r.user_prompt_parts) ? r.user_prompt_parts : [])
                .map(function(x){ return (x && x.text) || ""; })
                .filter(function(t){ return t.trim(); });
    var txt = ups.length ? ups.join("\n") : String(r.user_prompt || "");
    var out = [];
    String(txt).split("\n").forEach(function(line){
      var t = line.replace(/\s+$/, "");
      if(!t.trim()) return;
      var m = t.match(/^【[^】]{0,24}】\s?([\s\S]*)$/);
      if(m) out.push({role: "assistant", text: m[1]});
      else out.push({role: "user", text: t});
    });
    return out;
  }

  // v4.87 手机适配：窄屏把表格画成卡片，每格用 ::before 显示列名。
  // 列名直接从表头 <th> 的文本取，不另写一份文案——表头改了这里自动跟着变。
  // 桌面端不读 data-label，渲染结果一个字都不变。
  function labelTableCells(box){
    if(!box) return;
    var table = box.querySelector(".tbl table");
    if(!table) return;
    var ths = table.querySelectorAll("thead th");
    if(!ths.length) return;
    var labels = [];
    for(var i = 0; i < ths.length; i++) labels.push((ths[i].textContent || "").trim());
    var rows = table.querySelectorAll("tbody tr");
    for(var r = 0; r < rows.length; r++){
      var tr = rows[r];
      // 详情行是一整格（colspan），不挂列名
      if(tr.classList && tr.classList.contains("round-detail")) continue;
      var cells = tr.children, k = 0;
      for(var c = 0; c < cells.length; c++){
        var td = cells[c];
        if(!td || td.tagName !== "TD") continue;
        if(td.hasAttribute("colspan")) continue;
        var lb = labels[k] || "";
        if(lb) td.setAttribute("data-label", lb);
        k++;
      }
    }
  }

  // 用户 m11430：日期以前在 1839px 就藏了，那时时间列还有 22px 余量。改成按实测判断——
  // 时间列扣掉内边距后余量不足 3px 才藏日期；不够时先从「回复内容 / 决策理由」那列借（它本来就是
  // 省略号列，少一点只是截得更短），借不出来才藏日期。卡片布局（≤1000）不参与。
  var _fitTimeTimer = 0;
  // 这一列所有行里最宽的那条超出格子多少（>0 就是压到隔壁格了）。
  function _worstOver(rows, idx){
    var w = 0;
    for(var k = 0; k < rows.length; k++){
      var c = rows[k].children[idx];
      if(!c) continue;
      var s = getComputedStyle(c);
      var p = (parseFloat(s.paddingLeft) || 0) + (parseFloat(s.paddingRight) || 0);
      var rg = document.createRange();
      rg.selectNodeContents(c);
      var o = rg.getBoundingClientRect().width + p - c.clientWidth;
      if(o > w) w = o;
    }
    return w;
  }
  // 用户 m01379：命中预估 / 缓存命中 两格以前在 1568 宽就折成「2 步 / 58.9%」两行，
  // 而旁边好几列还有 20–46px 空隙。规则改成：先把所有列的空隙（≥3px 才算）收回来补给
  // 装不下的列，收光了还装不下才隐藏「N 步」前缀——窄屏顶多省略步数，不换行。
  function _padOf(td){
    var s = getComputedStyle(td);
    return (parseFloat(s.paddingLeft) || 0) + (parseFloat(s.paddingRight) || 0);
  }
  // 每一列所有行里最宽的那条自然宽（含内边距）。量的时候给整张表挂 .measuring，
  // 让所有格子连同里面的小标签（token 格的「N 步」胶囊）都按 nowrap 排：
  // 已经折行的格子直接量出来就是格子宽，看不出「其实装不下」。
  function _needAll(rows, n){
    // .tbl 是包着 <table> 的滚动容器，类要挂在它身上 CSS 才匹配得到。
    var tbl = rows[0].closest(".tbl") || rows[0].closest("table");
    var need = [], i, k;
    if(!tbl) return need;
    tbl.classList.add("measuring");
    for(i = 0; i < n; i++){
      var w = 0;
      for(k = 0; k < rows.length; k++){
        var c = rows[k].children[i];
        if(!c) continue;
        var rg = document.createRange();
        rg.selectNodeContents(c);
        var m = rg.getBoundingClientRect().width + _padOf(c);
        if(m > w) w = m;
      }
      need[i] = w;
    }
    tbl.classList.remove("measuring");
    return need;
  }
  function _cwOf(rows, idx){
    var c = rows[0].children[idx];
    return c ? c.clientWidth : 0;
  }
  // 把还有 ≥3px 空隙的列收回来，补给装不下的列。第 3、5 列是省略号列（回复内容 / 回复模型），
  // 本来就该截断，不参与；时间列有自己的 hide-date 逻辑，也不参与。
  function _reclaim(rows, ths, borrow){
    var n = ths.length, i, k;
    var deficit = [], pool = [];
    var need = _needAll(rows, n);
    for(i = 0; i < n; i++){
      // 时间列平时不参与（有自己的 hide-date 逻辑）；藏了日期之后它就有富余，可以补给缺的列。
      if(i === 2 || i === 4) continue;
      if(i === 0 && !rows[0].closest(".tbl").classList.contains("hide-date")) continue;
      var cw = _cwOf(rows, i);
      // need 和 clientWidth 都含内边距，直接比；别拿扣过内边距的 inner 去减，会多算一份 padding。
      if(need[i] > cw) deficit.push({i: i, d: need[i] - cw, cw: cw});
      else if(cw - need[i] >= 3) pool.push({i: i, s: cw - need[i] - 3, cw: cw});
    }
    if(!deficit.length) return need;
    var total = 0, avail = 0;
    for(k = 0; k < deficit.length; k++) total += deficit[k].d;
    for(k = 0; k < pool.length; k++) avail += pool[k].s;
    // 空隙不够时默认一点不动：挪一半只会让别的列也没余量，问题还在。
    // 只有调用方明确要求「借」（borrow）时，才从两个省略号列（决策理由 / 决策模型）借——
    // 它们本来就截断，少几像素只是截得更短，比把渠道列截成「WorkB…」强。
    var donors = [];
    if(avail < total){
      if(!borrow) return need;
      var short = total - avail;
      var cand = [{i: 2, floor: 60}, {i: 4, floor: 50}];
      for(k = 0; k < cand.length && short > 0; k++){
        var dcw = _cwOf(rows, cand[k].i);
        var give = Math.min(short, Math.max(0, dcw - cand[k].floor));
        if(give > 0){
          donors.push({i: cand[k].i, s: give, cw: dcw});
          avail += give;
          short -= give;
        }
      }
      // 借不出来就一点不动：挪一半只会让别的列也没余量，问题还在。
      if(short > 0) return need;
    }
    pool.sort(function(a, b){ return b.s - a.s; });
    var remain = total;
    for(k = 0; k < pool.length && remain > 0; k++){
      var t = Math.min(remain, pool[k].s);
      pool[k].take = t;
      remain -= t;
    }
    for(k = 0; k < donors.length && remain > 0; k++){
      var t2 = Math.min(remain, donors[k].s);
      donors[k].take = t2;
      remain -= t2;
    }
    for(k = 0; k < pool.length; k++){
      if(!pool[k].take) continue;
      ths[pool[k].i].style.width = Math.floor(pool[k].cw - pool[k].take) + "px";
    }
    for(k = 0; k < donors.length; k++){
      if(!donors[k].take) continue;
      ths[donors[k].i].style.width = Math.floor(donors[k].cw - donors[k].take) + "px";
    }
    for(k = 0; k < deficit.length; k++){
      ths[deficit[k].i].style.width = Math.ceil(deficit[k].cw + deficit[k].d) + "px";
    }
    return need;
  }
  // 命中预估 / 缓存命中 两格还装不下（含「N 步」前缀），或者 token 格的步数胶囊折行了。
  // need 传进来就复用，省一次重排。
  function _stepsOver(rows, need){
    if(!need) need = _needAll(rows, 12);
    return need[8] > _cwOf(rows, 8) + 0.5 ||
           need[10] > _cwOf(rows, 10) + 0.5 ||
           need[11] > _cwOf(rows, 11) + 0.5;
  }
  // 除省略号列（决策理由 / 决策模型）、时间列和体积估算（有自己的折行兜底）外，还有列装不下。
  function _overAny(rows){
    var need = _needAll(rows, 13);
    for(var i = 1; i < need.length; i++){
      if(i === 2 || i === 4 || i === 9) continue;
      if(need[i] > _cwOf(rows, i) + 0.5) return true;
    }
    return false;
  }
  function fitCols(){
    var box = document.getElementById("ctx-rounds");
    if(!box) return;
    var tbl = box.querySelector(".tbl");
    if(!tbl) return;
    var rows = tbl.querySelectorAll("tr.round-row");
    var ths = tbl.querySelectorAll("thead th");
    if(!rows.length || ths.length < 3) return;
    var cell = rows[0].children[0];
    // 面板没显示（实时窗口页签）时量不出宽度，保持上一次的判断。
    if(!cell || !cell.clientWidth) return;
    tbl.classList.remove("hide-date");
    tbl.classList.remove("tprob-off");
    tbl.classList.remove("vol-wrap");
    tbl.classList.remove("steps-off");
    tbl.classList.remove("res-wrap");
    for(var t = 0; t < ths.length; t++) ths[t].style.width = "";
    if(innerWidth <= 1000) return;
    // 用户 m01379：先把所有列的空隙收回来，收光了才允许省略 / 换行。
    var need = _reclaim(rows, ths);
    // 命中预估 / 缓存命中 还是装不下 → 隐藏「N 步」前缀（窄屏顶多省略步数，不换行），再收一次。
    if(_stepsOver(rows, need)){
      tbl.classList.add("steps-off");
      for(var t2 = 0; t2 < ths.length; t2++) ths[t2].style.width = "";
      _reclaim(rows, ths);
    }
    // 用户 m02449：触发类型的「概率」二字换行就藏掉它（只留百分比），再收一次。
    var needT = _needAll(rows, 13);
    if(needT[12] > _cwOf(rows, 12) + 0.5){
      tbl.classList.add("tprob-off");
      for(var t3 = 0; t3 < ths.length; t3++) ths[t3].style.width = "";
      _reclaim(rows, ths);
    }
    // 空隙收完、该藏的也藏了还装不下，才从两个省略号列借（它们本来就截断）。
    if(_overAny(rows)){
      for(var t4 = 0; t4 < ths.length; t4++) ths[t4].style.width = "";
      _reclaim(rows, ths, true);
    }
    // 还装不下：按用户 m02449 的建议先藏掉时间列的日期，把省下的宽度让给缺的列，再收一次。
    if(_overAny(rows)){
      tbl.classList.add("hide-date");
      for(var t5 = 0; t5 < ths.length; t5++) ths[t5].style.width = "";
      _reclaim(rows, ths, true);
    }
    // 窄到省略号列借光、日期也藏了还装不下，才让结果列折行：宁可两行，也不能压到隔壁格。
    var needR = _needAll(rows, 2);
    if(needR[1] > _cwOf(rows, 1) + 0.5) tbl.classList.add("res-wrap");
    var cs = getComputedStyle(cell);
    var pad = (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0);
    var rng = document.createRange();
    rng.selectNodeContents(cell);
    var tw = rng.getBoundingClientRect().width;
    // 按百分比本来就够 3px 余量：不动列宽。
    if(cell.clientWidth - pad - tw < 3){
      var need = Math.ceil(tw + pad + 3);
      var delta = need - cell.clientWidth;
      var c3 = rows[0].children[2];
      var w3 = c3 ? c3.clientWidth - delta : 0;
      if(w3 >= 50){
        var base = {};
        for(var b = 1; b < rows[0].children.length; b++) base[b] = _worstOver(rows, b);
        ths[0].style.width = need + "px";
        ths[2].style.width = w3 + "px";
        // 借完还有列被挤到溢出，就放弃日期。第 3、5 列是省略号列，本来就该截断，不参与判断。
        // 只跟「借之前」比：有些格子本来就因为内容锁死一行而溢出（窗口条数里的「回退」标签、
        // 触发类型的「17.6% 概率」），那是既有排版，不该拿来否决日期。
        var bad = false;
        for(var i = 1; i < rows[0].children.length; i++){
          if(i === 2 || i === 4) continue;
          // 0.4px 这种亚像素误差肉眼看不出来，不算挤爆。
          if(_worstOver(rows, i) > Math.max(base[i] || 0, 1)){ bad = true; break; }
        }
        if(bad){
          ths[0].style.width = "";
          ths[2].style.width = "";
          tbl.classList.add("hide-date");
        }
      }else{
        tbl.classList.add("hide-date");
      }
    }
    // 体积估算：装不下就允许折行，别压到隔壁格。
    if(_worstOver(rows, 9) > 0) tbl.classList.add("vol-wrap");
  }
  window.addEventListener("resize", function(){
    if(_fitTimeTimer) clearTimeout(_fitTimeTimer);
    _fitTimeTimer = setTimeout(fitCols, 120);
  });
  function renderCtxDecisions(items, preserveDetails){
    var box = document.getElementById("ctx-rounds");
    if(!box) return;
    box.style.display = (ctxPane === "decisions") ? "" : "none";
    if(ctxDecisionsSid === ctxSid && ctxSid) noteCtxPane("decisions", items);
    else ctxPaneStamp = null;
    if(!preserveDetails) ctxDecisionDetails = Object.create(null);
    else box.querySelectorAll("[data-detail]").forEach(function(row){
      ctxDecisionDetails[row.getAttribute("data-detail")] = row;
    });
    if(!items || !items.length){
      ctxDecisionDetails = Object.create(null);
      // 没读到当前会话的数据之前，不能把初始空数组当成“确认没有记录”。
      if(!ctxDecisionsSid || ctxDecisionsSid !== ctxSid){
        box.innerHTML = "<div class='empty'>" + esc(ctxDecisionsError || "正在读取决策记录…") + "</div>";
        return;
      }
      box.innerHTML = "<div class='empty'>还没有决策记录。决策模式开着、并且有一轮按设置触发了，才会留一条；" +
        "@ 你、点名、私聊这些直接回的轮次不经过决策。</div>";
      return;
    }
    var names = Object.create(null);
    items.forEach(function(r){ names[r.name] = true; });
    Object.keys(ctxDecisionDetails).forEach(function(name){
      if(!names[name]) delete ctxDecisionDetails[name];
    });
    // v3.94（用户 m08398 图7/图8）：列跟回复记录对齐——真模型名、token（入 / 出）、体积估算、
    // 缓存命中都补上，剩下的差异只有决策专属的「结论」和「理由」。
    // v3.98（用户 m10169 图5）：跟回复记录一样，标题行右侧补一句本页合计
    // 用户 m07111 图3：决策记录以前一屏把 100 条全画出来，底下没有任何翻页控件。
    // 跟回复记录同一套：每页 20 条，页脚同一组按钮（走势图不受影响）。
    var dTotal = items.length;
    var dPages = Math.max(1, Math.ceil(dTotal / CTX_PAGE_SIZE));
    if(ctxDecisionPage > dPages - 1) ctxDecisionPage = dPages - 1;
    if(ctxDecisionPage < 0) ctxDecisionPage = 0;
    var dStart = ctxDecisionPage * CTX_PAGE_SIZE;
    var dShown = items.slice(dStart, dStart + CTX_PAGE_SIZE);
    var dPager = "<span class='pg' data-pgpane='decisions'>" +
      "<button data-pg='first'" + (ctxDecisionPage === 0 ? " disabled" : "") + ">«</button>" +
      "<button data-pg='prev'" + (ctxDecisionPage === 0 ? " disabled" : "") + ">‹</button>" +
      "<span class='pg-i'>" + (ctxDecisionPage + 1) + " / " + dPages + "</span>" +
      "<button data-pg='next'" + (ctxDecisionPage >= dPages - 1 ? " disabled" : "") + ">›</button>" +
      "<button data-pg='last'" + (ctxDecisionPage >= dPages - 1 ? " disabled" : "") + ">»</button>" +
      "</span>";
    var dTitle = "<div class='trend-hd' style='margin-bottom:8px'><span>最近 " + items.length + " 次</span>";
    dTitle += recordPageSummary(dShown);
    // 用户 m07562 第 2 条：行尾那句「点一行看那一次读到的聊天记录」去掉。
    dTitle += "<span class='grow'></span>" + dPager + "</div>";
    var html = dTitle +
      // v4.85（用户 m10425 第 8 条）：两张记录表的列名与列序对齐——前面都是
      // 时间 / 结果 / 一格说明（决策写理由、回复写正文）/ 模型那一格。
      "<div class='tbl'><table><thead><tr><th>时间</th><th>结果</th><th class='lft'>决策<wbr>理由</th>" +
      "<th class='lft'>渠道</th>" + "<th class='lft'>决策<wbr>模型</th>" +
      "<th class='lft'>防抖</th>" +
      "<th class='lft'>用时</th><th class='lft'>窗口<wbr>条数</th>" +
      "<th class='lft' title='这一次决策调用的输入 / 输出 token'>token<wbr><span class='nw'>（入/出）</span></th>" +
      "<th class='lft'>体积<wbr>估算</th>" +
      "<th class='lft'>命中<wbr>预估</th><th class='lft'>缓存<wbr>命中</th>" +
      "<th class='lft' title='这一轮是怎么被叫起来的：概率类写触发概率，@ 或叫昵称写来源'>触发<wbr>类型</th></tr></thead><tbody>";
    dShown.forEach(function(r){
      var why = String(r.decision_reason || "").replace(/\s+/g, " ").trim();
      // m01209 图3：决策词不再引导角色说什么，记录页也不再把「打算说」当结论展示。
      // 用户 m07898：切了备用就把主模型的真实报错一起挂在 tooltip 上。
      var fbWhy = decModelErrTip(r);
      var tip = (why || "（模型没给理由）") + (fbWhy ? "；" + fbWhy : "");
      var body = (why || "—");
      // 用户 m17519：理由列改成单行省略（跟回复正文列同一套 .ell），不再按字数硬截。
      // 后端 _requests_payload 把 provider:内部 ID 翻成了真名（model_label）；
      // 老记录没有这个字段就退回原来的键。
      var mlabFull = String(r.model_label || r.decision_model || "—");
      // 用户 m16262 图5：以前这里按 26 个字硬截断再折行，长名字被切成两行。
      // 现在整格只占一行、由 CSS 省略号截尾，这里直接把全名交给 .mn（title 里也是全名）。
      // 用户 m00375 第 8 条：走第三方接口时决策模型只有模型名，渠道单独标在模型前面。
      var mch = String(r.model_channel || "").trim();
      var el = Number(r.decision_elapsed) || 0;
      // v4.39（用户 m20816）：决策也能看图了——结论旁边标一下这一轮真带了几张。
      // 用户 m02038 第 4 条：这一格要的是「请求里实际带了几张图」——转发实际暴露的、
      // 历史里的、待发的都算，所以优先用 payload_metrics 算出来的 image_count，
      // 老记录没有这个字段才退回决策自己数的 decision_image_count。
      var icnt = Number(r.image_count) || Number(r.decision_image_count) || 0;
      var ifb = !!r.decision_images_fallback;
      // 用最窄的写法：这一格本来就只有「30 条」几个字，塞个大徽标会把
      // 「结论 / 理由」两列挤窄（真机 1256 宽时会挤到「要回」竖着排）。
      // v4.65（用户 m36234）：退回纯文字时，把「带图那次」的真实报错和试了几次一起
      // 挂到 tooltip 上——以前只有一句「没成功」，查不出是超时、上游拒图，还是没按 JSON 回。
      var fbTip = ifb ? decImageFbTip(r) : "";
      // 2026-10-31（用户 m05138）：失败只写在结论列，这一格不再塞「决策失败」徽标；
      // 「看图退回」也只在决策成功时作为补充，别把两种结果混在一行。
      var itag;
      if(icnt){
        itag = " <span class='dim' title='" + esc("这一轮请求里带了 " + icnt + " 张图（含转发实际暴露的、历史里的、待发的）" +
            (ifb ? "，" + fbTip : "")) + "'>· 图" + icnt + "</span>";
      }else{
        itag = (ifb && !r.failed) ? " <span class='tag warn' title='" + esc(fbTip) + "'>回退</span>" : "";
      }
      // 用户 m05124 第 8 条：决策轮也要看得到防抖——「防抖 7s + 2s」。
      var del = Number(r.decision_debounce) || 0;
      var tk = r.tokens || {};
      html += "<tr class='round-row' data-round='" + esc(r.name) + "'>";
      html += "<td class='num'>" + fmtTimeHtml(r.ts) + "</td>";
      html += "<td class='res'>" + decisionConcl(r) + "</td>";
      html += "<td class='lft rtxt' title='" + esc(tip) + "'><span class='ell'>" + esc(body) + "</span></td>";
      // 用户 m15537 第 4 条：渠道单独一列（在模型列左边），模型格只留模型名。
      // 用户 m02449：渠道列不换行，装不下就省略号截尾，全名挂在 title 上。
      html += "<td class='chc'" + (mch ? " title='" + esc(mch) + "'" : "") + ">" +
              (mch ? esc(mch) : "<span class='dim'>—</span>") + "</td>";
      html += "<td class='mdl' title='" + esc("模型：" + mlabFull) + "'>" +
              "<span class='mn'><span class='mnt'>" + esc(mlabFull) + "</span>" +
              (r.decision_via === "fallback" ? "<span class='tag pend'" +
                (fbWhy ? " title='" + esc(fbWhy) + "'" : "") + ">备用</span>" : "") + "</span></td>";
      // 用户 m13212：防抖单独占一列，用时列只写判定本身，读起来就是「防抖 7s + 3s」。
      html += "<td class='num'>" + (del ? fmtDur(del) : "<span class='dim'>—</span>") + "</td>";
      html += "<td class='num'>" + (el ? fmtDur(el) : "<span class='dim'>—</span>") + "</td>";
      // 看图张数跟着「窗口条数」走：那是这一轮喂进去多少内容，放结论格里会把
      // 「理由」列挤窄、整行被撑成好几行（真机截图见过）。
      // v4.57（用户 m29312）：这一格跟回复表、走势柱同口径——只数用户消息，并且把
      // 「待发」那批一起算上（决策读的就是窗口 + 待发）。以前写的是含 bot 的总数，
      // 同一轮柱子写 14、表格写 27，两个数对不上，看着像消息被吞了。
      // 用户 m02038 第 4 条：这一格只写「上下文窗口里总共有多少条」+「请求里实际带了多少张图」。
      // 待发不再单独标出来——它还没并进窗口，混进条数里会让人以为窗口有 50 多条。
      var dTot = Number(r.window_total) || 0;
      var dUser = (typeof r.window_user === "number" && r.window_user > 0) ? r.window_user : dTot;
      // 用户 m13767：这一格里的「N 条」和标签锁在一行——以前标签会掉到第二行，看着像两行数据。
      html += "<td class='num'><span class='wn'>" + (dTot || dUser) + " 条" + itag + "</span></td>";
      html += "<td class='num tokc'>" + (tk.input
        ? "<span class='tokw'>" + tokText(tk.input) + " / " + tokText(tk.output) + "</span>"
        : "<span class='dim'>—</span>") + "</td>";
      // v4.41（用户 m21541）：决策的体积估算以前只报文字，带图的轮次看着像没体积。
      // 现在跟回复表同一套：文 X · 图 Y（没图就写「无图」，一眼能分清）。
      var pb = Number(r.payload_bytes) || 0;
      var pdec = payloadInfo(r);
      var picnt = Number(r.image_count) || 0;
      var pcellD;
      if(pdec.known){
        pcellD = "文 " + fmtBytes(r.text_payload_bytes || pb) +
          (picnt ? " · 图 " + (pdec.partial ? "≥" : "") + fmtBytes(r.image_base64_estimated_bytes || 0)
                 : " · <span class='dim'>无图</span>") +
          (pdec.partial && picnt ? "（" + (r.image_unknown_count || 0) + " 张未知）" : "");
      } else {
        pcellD = pb ? fmtBytes(pb) : "<span class='dim'>—</span>";
      }
      html += "<td class='num vol'>" + pcellD + "</td>";
      var est = (r.hit_estimate === null || r.hit_estimate === undefined) ? null : Number(r.hit_estimate);
      html += "<td class='num'>" + (est === null ? "<span class='dim'>—</span>"
        : "<span class='est'>" + est.toFixed(1) + "%</span>") + "</td>";
      var hit = (tk.cache_hit === null || tk.cache_hit === undefined) ? null : Number(tk.cache_hit);
      html += "<td class='num'>" +
        (hit === null ? (r.cache_unreported ? cacheUnreportedCell() : "<span class='dim'>—</span>") : hit.toFixed(1) + "%") + "</td>";
      // v4.01（用户 m11166 图3）：末列叫「触发类型」——概率类（概率 / 曲线 / 动态）
      // 写「触发概率 X %」；@、叫昵称这种没有概率的写来源，跟回复记录同一套叫法。
      var tp = Number(r.trigger_prob) || 0;
      var dsrc = String(r.decision_source || "");
      var tcell;
      if(tp > 0){
        // 用户 m02038 第 7 条：写成「2% 概率」。窄屏下百分号会被折到下一行，
        // 所以整段套 nowrap（以前是靠数字和 % 之间的不换行空格粘住，看着多个空格）。
        // 用户 m11430：窄屏下这格只有 58px，锁死一行会压到右边；改成宽屏锁一行、窄屏折行。
        // 用户 m02449：窄屏下「概率」二字会折到第二行。整段仍锁一行，装不下时由
        // fitCols 给表挂 .tprob-off 把「概率」藏掉，只留百分比（跟「N 步」同一套做法）。
        tcell = "<span class='tprob'>" + (Math.round(tp * 10) / 10) + "%<span class='tpw'> 概率</span></span>";
      } else if(dsrc && dsrc !== "decision"){
        tcell = esc(REASON_LABEL[dsrc] || dsrc);
      } else {
        tcell = "<span class='dim'>—</span>";
      }
      html += "<td class='lft'>" + tcell + "</td>";
      html += "</tr>";
      html += "<tr class='round-detail' data-detail='" + esc(r.name) + "' data-cspan='13' style='display:none'><td colspan='13'><div class='hint'>加载中…</div></td></tr>";
    });
    html += "</tbody></table></div>";
    box.innerHTML = html;
    labelTableCells(box);
    fitCols();
    // 展开着的那几行原样塞回去：节点是从上一版表格里捞的，内容还在，不用重新请求
    box.querySelectorAll("[data-detail]").forEach(function(row){
      var old = ctxDecisionDetails[row.getAttribute("data-detail")];
      if(!old) return;
      row.parentNode.replaceChild(old, row);
      if(old.previousElementSibling) old.previousElementSibling.classList.toggle("open", old.style.display !== "none");
    });
  }

  // 用户 m15537 第 3 条：多步命中不再逐步铺开（一行要 300px，格子只有 130px），
  // 折成一句「3 步 42.1%」。平均值按每步的输入 token 加权——步数多的那步权重更大，
  // 比算术平均更接近整轮的真实占比；某步没有 in 时退回算术平均。
  function stepAvgPct(ss, key){
    if(!Array.isArray(ss) || !ss.length) return null;
    var sum = 0, wsum = 0, plain = 0, n = 0;
    for(var i = 0; i < ss.length; i++){
      var o = ss[i] || {};
      var v = o[key];
      if(v === null || v === undefined || v === "" || isNaN(Number(v))) continue;
      // v4.9x（用户 m02038 第 2 条）：反推出来的步带 full_est（这一步完整 prompt 的
      // token 数），按它加权才是「整轮合计」的口径；没有 full_est 的老记录仍按 in 加权。
      var w = Number(o["full_est"]) || Number(o["in"]) || 0;
      sum += Number(v) * w;
      wsum += w;
      plain += Number(v);
      n++;
    }
    if(!n) return null;
    return wsum > 0 ? (sum / wsum) : (plain / n);
  }

  // 用户 m25513：上游没回报缓存、又反推不出来时，别只画一个「—」——直接写清楚是上游没给。
  // 跟「命中预估」不同：预估是我们自己算的，这一格是上游压根没给证据。
  function cacheUnreportedCell(){
    return "<span class='dim' title='" + esc("这个渠道没回报缓存用量，也反推不出来（它报的是完整 prompt），所以这一格没有数") + "'>上游未报</span>";
  }

  // 每步明细（只给 title 用）：「1步 12.3% · 2步 45.6% · …」，超过 6 步只写前 6 步。
  function stepDetailText(ss, key, unit){
    if(!Array.isArray(ss) || !ss.length) return "";
    var out = [], show = Math.min(ss.length, 6);
    for(var i = 0; i < show; i++){
      var o = ss[i] || {};
      var v = o[key];
      // 用户 m15537：中转站只报未命中那截时，这一步算不出比例（后端给 null），
      // 明细里写「—」而不是跳过，免得「5 步 29.2%」被读成五步的平均。
      if(v === null || v === undefined || v === "" || isNaN(Number(v))){ out.push((i + 1) + "步 —"); continue; }
      out.push((i + 1) + "步 " + Number(v).toFixed(1) + unit);
    }
    if(ss.length > show) out.push("+" + (ss.length - show) + "步");
    return out.join(" · ");
  }

  function renderCtxRounds(items, preserveDetails){
    // 三页共用一个容器：隐藏时只更新数据，不绘制回复表。
    if(ctxPane !== "rounds") return;
    noteCtxPane("rounds", items);
    var box = document.getElementById("ctx-rounds");
    if(!preserveDetails) ctxRoundDetails = Object.create(null);
    else box.querySelectorAll("[data-detail]").forEach(function(row){
      ctxRoundDetails[row.getAttribute("data-detail")] = row;
    });
    var names = Object.create(null);
    items.forEach(function(r){ names[r.name] = true; });
    Object.keys(ctxRoundDetails).forEach(function(name){
      if(!names[name]) delete ctxRoundDetails[name];
    });
    // 标题和「实时窗口 / 最近请求」切换在窗口面板的标题行里，这边不再重复一遍
    box.style.display = (ctxPane === "rounds" || ctxPane === "decisions") ? "" : "none";
    var html = "";
    if(!items.length){
      box.innerHTML = "<div class='empty'>还没有请求记录</div>";
      return;
    }
    // 走势图一直铺到右边（不锁柱子），表格分页列（用户 m30613 要翻页）
    var total = items.length;
    var pages = Math.max(1, Math.ceil(total / CTX_PAGE_SIZE));
    if(ctxRoundPage > pages - 1) ctxRoundPage = pages - 1;
    if(ctxRoundPage < 0) ctxRoundPage = 0;
    var start = ctxRoundPage * CTX_PAGE_SIZE;
    var shown = items.slice(start, start + CTX_PAGE_SIZE);
    // 这一行跟「实时窗口」页的走势图标题行走同一套坐标（.trend-hd，左边 12px），
    // 以前是裸 .hint 贴左边缘，两页切来切去那行字会跳一下（用户 m30613）
    var pager = "<span class='pg'>" +
      "<button data-pg='first'" + (ctxRoundPage === 0 ? " disabled" : "") + ">«</button>" +
      "<button data-pg='prev'" + (ctxRoundPage === 0 ? " disabled" : "") + ">‹</button>" +
      "<span class='pg-i'>" + (ctxRoundPage + 1) + " / " + pages + "</span>" +
      "<button data-pg='next'" + (ctxRoundPage >= pages - 1 ? " disabled" : "") + ">›</button>" +
      "<button data-pg='last'" + (ctxRoundPage >= pages - 1 ? " disabled" : "") + ">»</button>" +
      "</span>";
    // 用户 m14026 第 2 条：标题报实际读到的条数，不再报配置里的保留上限（100）。
    html += "<div class='trend-hd' style='margin-bottom:8px'><span>最近 " + total + " 次</span>";
    html += recordPageSummary(shown);
    html += "<span class='grow'></span>" + pager + "</div>";
    html += "<div class='tbl'><table><thead><tr><th>时间</th><th>结果</th>" +
            // v4.85（用户 m10425 第 8 条）：跟决策表同一列位置放这一轮的说明——决策那边
            // 是「决策理由」，回复这边是真正发出去的正文。两张表因此都是 13 列。
            "<th class='lft' title='这一轮真正发出去的文字；多条气泡用「/」连起来，太长只显示前面一段'>回复<wbr>内容</th>" +
            // v4.84（用户 m09179 第 10 条）：回复快照也记下这一轮用的渠道与模型。
            // 用户 m00375 第 8 条：这里的「渠道」要的是 API 渠道（供应商实例名），
            // 不是 IM 平台；列名改成「回复模型」，机器人账号挪到 title 里。
            "<th class='lft'>渠道</th>" +
            "<th class='lft' title='这一轮真正发出去的模型'>回复<wbr>模型</th>" +
            "<th class='lft'>防抖</th>" +
            "<th class='lft'>用时</th><th class='lft'>窗口<wbr>条数</th>" +
            "<th class='lft' title='这一轮所有步骤的合计；多步（有工具调用）时会把上下文重复计一次'>token<wbr><span class='nw'>（入/出）</span></th>" +
            "<th class='lft'>体积<wbr>估算</th>" +
            "<th class='lft'>命中<wbr>预估</th>" +
            "<th class='lft'>缓存<wbr>命中</th><th class='lft'>触发<wbr>类型</th></tr></thead><tbody>";
    shown.forEach(function(r){
      var tk = r.tokens || {};
      html += "<tr class='round-row' data-round='" + esc(r.name) + "'>";
      // v4.78（用户 2026-11-07 第 5 条）：回复记录不再显示快照编号。编号是「实时窗口快照」
      // 的标识（v4.71 用户 m01516 第 3 条），回复快照是另一种东西，不该共用一套号。
      html += "<td class='num'>" + fmtTimeHtml(r.ts) + "</td>";
      html += "<td class='res'>" + roundResultHtml(r) + "</td>";
      // v4.85（用户 m10425 第 8 条）：这一轮真正发出去的正文。跟决策表的理由列一样只露
      // 前面一段，完整正文挂在 title 上；老快照没有 reply_text，显示「—」。
      var rtxt = String(r.reply_text || "").replace(/\s+/g, " ").trim();
      if(!rtxt){
        html += "<td><span class='dim' title='这条记录里没有正文：v4.85 之前的旧快照，或者这一轮没发文字'>—</span></td>";
      }else{
        // 用户 m13442 图1：正文一长就换行，把整行撑高。改成单行省略，完整正文留在 title 里。
        html += "<td class='lft rtxt' title='" + esc(rtxt) + "'><span class='ell'>" + esc(rtxt) + "</span></td>";
      }
      // v4.84（用户 m09179 第 10 条）：回复快照也记下这一轮用的渠道与模型。
      // v4.84 之前写的快照没有这两个键，显示「—」并在 title 里说明，别当成「没用到模型」。
      // 用户 m00375 第 8 条：这一格要的是 API 渠道（供应商实例名）+ 模型名。
      // 以前写的是 IM 平台（QQ / webchat），用户说那不是他要的渠道。
      // reply_provider 是这次新加的键，老快照没有就只显示模型名；
      // 机器人账号仍留在 title 里，需要时鼠标一停就能看到。
      var rpv = String(r.reply_provider || "").trim();
      var rch = String(r.reply_channel || "").trim();
      var rmd = String(r.reply_model || "").trim();
      // 用户 m15537 第 4 条：渠道单独一列（在模型列左边），模型格只留模型名。
      if(!rpv && !rmd){
        html += "<td class='chc'><span class='dim' title='这条是旧记录，当时没存渠道'>—</span></td>";
        html += "<td class='mdl'><span class='dim' title='这条是旧记录，当时没存模型'>—</span></td>";
      }else{
        html += "<td class='chc'>" + (rpv ? esc(rpv) : "<span class='dim'>—</span>") + "</td>";
        html += "<td class='mdl' title='" + esc("模型：" + (rmd || "未记录")) + "'>" +
                 "<span class='mn'><span class='mnt'>" + esc(rmd || "—") + "</span></span></td>";
      }
      // 用时（用户 m10749 第 7 条）：改成「流水线总耗时」——跟 ④ 明细里那条同口径，
      // 从请求开始到请求结束（模型思考 + 工具调用都算在内），不含回复慢慢吐字那段。
      // 原来用的是 elapsed_trigger（触发 → 请求结束），把前面的防抖等待也算了进去，
      // 于是常看到「API 才 5 秒、这里十几秒」；防抖等待现在只写在 title 里。
      // 用户 m05453：抖动 → 决策 → 回复是三段。这一段只写回复本身（请求开始到结束），
      // 抖动与决策各自单独写，别再把它们混成「防抖 12 秒」。
      var dur = (r.elapsed != null) ? r.elapsed : null;
      // 用户 m06046 图4：回复这一行不再显示防抖。进阶模式下抖动那几秒是「决策之前」
      // 的等待，已经记在决策那一行；这里再取一次算出的是 started - 触发时刻，
      // 把决策耗时也裹了进去，数是错的。回复行只留 决策 / 发送 / 流水线。
      var dbt = 0;
      // 用户 m14406 第 1、2 条：这一格不再挂悬停说明。以前 title 写「中间决策判定用了 2.3s」，
      // 读起来像把决策也算进了这一格；它其实只是回复自己的流水线（请求开始 → 请求结束）。
      // 用户 m05124 第 8 条 / m05453：三段摆进格子里——「决策 5s + 发送 3s + 流水线 12s」。
      // 用户 m06046 图4：回复行不再算防抖（抖动发生在决策之前，算在这里会把决策耗时裹进去）。
      // 用户 m07562 图2：这一格只写「流水线总耗时」，不再拆成
      // 「决策 2.3s + 发送 3.2s + 6.4s」——三个数并排看着像三段不同的耗时，
      // 反而看不出这一轮总共花了多久。
      var durCell;
      if(dur == null && r.elapsed_trigger == null){
        durCell = "<span class='dim'>—</span>";
      } else {
        durCell = (dur != null) ? fmtDur(dur) : fmtDur(r.elapsed_trigger);
      }
      // 用户 m13212：防抖不再并进「流水线」，单独一列——读起来是「防抖 7s + 11s」。
      // 用户 m06046 当年嫌算错的是「把决策耗时也算进回复行」，所以这里只取纯抖动那段：
      // 引擎量到的 debounce_fixed 优先，没有再退回 debounce - 决策耗时。
      var dbo = Number(r.elapsed_debounce) || 0;
      html += "<td class='num'>" + (dbo > 0 ? fmtDur(dbo) : "<span class='dim'>—</span>") + "</td>";
      html += "<td class='num'>" + durCell + "</td>";
      // 用户 m02038 第 4 条：这一格只写「上下文窗口里总共有多少条」+「请求里实际带了多少张图」。
      // 待发不再单独标出来——它还没并进窗口，混进条数里会让人以为窗口有 50 多条。
      var wUser = (typeof r.window_user === "number") ? r.window_user : 0;
      var wAll = r.window_total || r.window_counted || 0;
      // 图片：请求里实际带了多少张（含转发实际暴露的、历史里的、待发的），不是窗口里出现过多少张。
      var rimg = Number(r.image_count) || 0;
      var rimgB = Number(r.image_base64_estimated_bytes) || 0;
      var rimgTag = rimg
        ? " <span class='dim' title='" + esc("这一轮随请求发出 " + rimg + " 张图，base64 " + fmtBytes(rimgB) +
            "；含转发实际暴露的、历史里的、待发的，只要是请求里真带上的都算") + "'>· 图" + rimg + "</span>"
        : "";
      html += "<td class='num'>" + ((wAll || wUser) ? (wAll || wUser) + " 条"
                                     : "<span class='dim'>—</span>") +
                rimgTag + "</td>";
      // 字符数 / 字符每 token / 文本体积各占一列：原来三条全挤在 token 单元格
      // 底下那一行小字里，右边空出一大片，也没法按列比较（用户 m33271 图1）
      // v3.58（用户 m16875 图4）：token 是整轮各步之和。多步（有工具调用）时第二步会把前面的
      // 上下文再发一遍，同一个字符数能算出完全不同的 token，所以这里把步数写在格子里，
      // 比率一律按「首步 input」算——那才是这一份上下文真正花掉的 token。
      var stN = Number(r.steps) || 0;
      var tokTip = "入 / 出 是这一轮所有步骤的合计";
      if(stN > 1) tokTip += "：这一步数下，第二步起会把前面的上下文再发一遍，所以合计会比单步大；字符只算一次请求的文本，两者不能直接比";
      html += "<td class='num tokc'" + (tokTip ? " title='" + esc(tokTip) + "'" : "") + ">" + (tk.input
        ? "<span class='tokw'>" + tokText(tk.input) + " / " + tokText(tk.output) +
          (stN > 1 ? "<span class='stp' title='这一轮走了 " + stN + " 步'>" + stN + " 步</span>" : "") + "</span>"
        : "<span class='dim'>—</span>") + "</td>";
      // v3.63（用户 m18492）：整列「字符」删掉——跟 token 不可比，看分段字数的去请求详情。
      // v3.62（用户 m18059 第 1 条）：字符/token 整列删掉——多步轮的 token 是各步合计、字符只算一次，
      // 这个比率在多步轮上本来就没有可比性，标「1步」也还是容易被读成一整轮的数。
      var pi = payloadInfo(r);
      // 整格只留一行（用户 m41961）：文 14.8 KB · 图 3.95 MB。图片张数和口径说明都挂 title，
      // 不再把「约 3.97 MB」大字单独占一行。
      // v4.41（用户 m21541）：没图的轮次也把「无图」写出来，不然「文 19.3 KB」看不出
      // 是「这轮真没图」还是「有图没算进体积」。
      var rpcnt = Number(r.image_count) || 0;
      var pcell = pi.known
        ? "文 " + fmtBytes(r.text_payload_bytes || 0) +
          (rpcnt ? " · 图 " + (pi.partial ? "≥" : "") + fmtBytes(r.image_base64_estimated_bytes || 0)
                 : " · <span class='dim'>无图</span>") +
          (pi.partial && rpcnt ? "（" + (r.image_unknown_count || 0) + " 张未知）" : "")
        : (pi.total ? pi.label : "<span class='dim'>—</span>");
      html += "<td class='num vol'>" + pcell + "</td>";
      // 命中预估（用户 m41511）：后端按「本轮公共前缀占比」算基准，只在距上一轮
      // 超过 3 小时时打五折（离线 97 轮 MAE 10.1pp）；不再写「（字符）」也不再拿上一轮实测去平均。
      var est = (r.hit_estimate === null || r.hit_estimate === undefined) ? null : r.hit_estimate;
      // v3.22 第 5 条：公式没动（117 轮离线比过约 20 套，字符前缀比例已经最准），
      // 改由后端给一句话说明这一轮为什么低/高：窗口裁切/重建、间隔过期、与实测差多少。
      // v3.24（用户 m45932 第1条）：这一格连悬停注释都不要了 —— 断因那句也不挂，
      // 想要原因去看右侧「缓存命中」列的实际值和对面的窗口走势。
      // v3.62（用户 m18059 第 1 条）：多步轮也要逐步写。第 1 步用后端的老口径（首步文本与上一轮同位置的
      // 公共前缀）；第 2 步起的 prompt 前缀就是上一步的整段输入，所以逐步预估 = 上一步 in ÷ 这一步 in。
      // 用户 m15537：中转站只报「没命中那截」时这一步的 in 会比上一步还小，前缀模型不成立，
      // 后端给 null，这里写「—」不计入平均，免得算出 845.6% 这种数。
      var ss = Array.isArray(r.step_stats) ? r.step_stats : null;
      var estCell;
      if(stN > 1 && ss && ss.length > 1){
        // 用户 m15537 第 3 条：逐步铺开一行要 300px，格子只有 130px，折成三行还把整行拉高。
        // 折成一句「3 步 42.1%」，按每步输入 token 加权平均。
        var eAvg = stepAvgPct(ss, "est");
        if(eAvg === null && est != null) eAvg = Number(est);
        if(eAvg === null){
          estCell = "<span class='dim'>—</span>";
        }else{
          estCell = "<span class='steps'><span class='stp'>" + ss.length + " 步 </span><span class='est'>" + eAvg.toFixed(1) + "%</span></span>";
        }
      } else {
        estCell = (est == null) ? "<span class='dim'>—</span>"
          : "<span class='est'>" + Number(est).toFixed(1) + "%</span>";
      }
      html += "<td class='num'>" + estCell + "</td>";
      // v3.59（用户 m17212 第 1 条）：实测量的是整轮各步合计（第二步会把前面整段再发一遍，
      // 所以天然偏高）；多步轮把首步命中一起写出来，跟上面的预估才是同一个量。
      var fch = (r.first_cache_hit === null || r.first_cache_hit === undefined) ? null : Number(r.first_cache_hit);
      // v3.60（用户 m17581 第 4 条）：多步轮按步写「1步 55.2% · 2步 89.5%」——每一步都是该步 cached ÷ 该步 input，
      // 第二步把前文再发一遍天然就高，混成一个整轮数看不出口径。后端没带 step_stats 的老记录退回「整轮 X%（首步 Y%）」。
      var cacheCell = "";
      if(r.hit_inferred){
        // 中转站不回报 cached_tokens：后端按上报 token 反推，逐步用 hit_est，整轮用 tk.cache_hit。
        var chv = (tk.cache_hit === null || tk.cache_hit === undefined) ? null : (tk.cache_hit + "%");
        if(stN > 1 && ss && ss.length > 1){
          var hAvg = stepAvgPct(ss, "hit_est");
          if(hAvg === null){
            cacheCell = (chv !== null ? chv : cacheUnreportedCell());
          }else{
            cacheCell = "<span class='steps'><span class='stp'>" + ss.length + " 步 </span>≈" + hAvg.toFixed(1) + "%</span>";
          }
        } else {
          var h0 = (ss && ss.length && ss[0] && ss[0].hit_est != null) ? ss[0].hit_est : null;
          cacheCell = (h0 !== null) ? ("≈" + h0 + "%") : (chv !== null ? chv : cacheUnreportedCell());
        }
      } else if(tk.cache_hit === null || tk.cache_hit === undefined){
        cacheCell = r.cache_unreported ? cacheUnreportedCell() : "<span class='dim'>—</span>";
      } else if(stN > 1 && ss && ss.length > 1){
        var nAvg = stepAvgPct(ss, "hit");
        if(nAvg === null){
          cacheCell = tk.cache_hit + "%";
        }else{
          cacheCell = "<span class='steps'><span class='stp'>" + ss.length + " 步 </span>" + nAvg.toFixed(1) + "%</span>";
        }
      } else {
        cacheCell = tk.cache_hit + "%" + ((stN > 1 && fch !== null) ? " <span class='dim'>（首步 " + fch + "%）</span>" : "");
      }
      html += "<td class='num'>" + cacheCell + "</td>";
      // 用户 m16808：回复记录的触发列只写大类，不再带「静默冒泡」「曲线触发 · 18% · 连 N 条」这些细项。
      // 判定顺序：重试轮 → 重试触发；决策通过/决策轮 → 决策触发；静默 → 静默触发；裁切快照保留窗口裁切。
      var rlab;
      if(r.retry_of){
        rlab = "重试触发";
      } else if(r.decision_passed || r.decision_only || r.trigger_reason === "decision"){
        rlab = "决策触发";
      } else if(r.trigger_reason === "silent"){
        rlab = "静默触发";
      } else if(r.trim_only || r.trigger_reason === "trim"){
        rlab = "窗口裁切";
      } else {
        rlab = esc(REASON_LABEL[r.trigger_reason] || r.trigger_reason || "-");
      }
      // v3.90（用户 m05623 第 1 条）：裁切快照在「触发」列挂「待发 N」——那次裁切里
      // 有几条是攒着还没发出去的；以前只有把鼠标停在「窗口条数」上才看得到这个数。
      var trimPendN = Number(r.trim_pending_count) || 0;
      if(trimPendN > 0 && (r.trim_only || r.trigger_reason === "trim")){
        rlab += " <span class='tag pend' title='" + esc("这份快照里有 " + trimPendN + " 条是攒着还没发出去的待发消息（打开快照后是带「待发」标记的那几条）") + "'>待发 " + trimPendN + "</span>";
      }
      html += "<td>" + rlab + "</td></tr>";
      // v3.94：cspan 是详情加载器里的局部变量（renderCtxRounds 里取不到），
      // 这里直接写死并带上 data-cspan，展开时才不会被加载器按 9 列重排。
      // v4.84 加「渠道 / 模型」、v4.85 加「回复内容」、v4.88 把渠道拆成独立列，回复表现在 13 列。
      html += "<tr class='round-detail' data-detail='" + esc(r.name) + "' data-cspan='13' style='display:none'><td colspan='13'><div class='hint'>加载中…</div></td></tr>";
    });
    html += "</tbody></table></div>";
    box.innerHTML = html;
    labelTableCells(box);
    fitCols();
    box.querySelectorAll("[data-detail]").forEach(function(row){
      var old = ctxRoundDetails[row.getAttribute("data-detail")];
      if(!old) return;
      row.parentNode.replaceChild(old, row);
      if(old.previousElementSibling) old.previousElementSibling.classList.toggle("open", old.style.display !== "none");
    });
    prefetchRecentSnaps();
  }

  // 请求索引也要轮询；未变化不碰 DOM，变化时复用详情节点，保留内部展开与媒体状态。
  var ctxRoundsLoads = Object.create(null);
  // 用户 m13250 第 2 条：后端「保留请求数」原样带回来，请求页标题跟着它走
  var ctxKeep = 0;
  var ctxWindowRequest = 0;
  var ctxWindowInFlight = null;
  function invalidateCtxRequests(){
    ++ctxWindowRequest;
    ctxRoundsLoads = Object.create(null);
    ctxWindowInFlight = null;
    ++ctxDecisionsRequest;   // v3.91（用户 m05749）：决策记录跟着会话换代
    ctxDecisions = [];
    ctxDecisionsSid = "";
    ctxDecisionsAt = 0;
    ctxDecisionsError = "";
    ctxDecTrend = [];
    ctxDecFolded = 0;
    ctxTrendReq = false; ctxTrendDec = false;   // v4.83：换代后重新等两路趋势
    ctxSnapKeep = 0;
    ctxSnapTotal = 0;
    ctxDecisionsInFlight = null;
    ctxDecisionsInFlightSid = "";
    ctxDecPrefetched = "";
    roundRecCache = Object.create(null);
    resetCtxPaneCache();
    pollRoundSig = Object.create(null);
    ctxRoundsFull = false;
  }
  function fmtBytes(n){
    if(n === null || n === undefined || !n) return "—";
    if(n < 1024) return n + " B";
    // 用户 m11361：100 KB 起直接用 MB，少一位数字，体积估算那格才排得下一行。
    if(n < 102400) return (n / 1024).toFixed(1) + " KB";
    return (n / 1048576).toFixed(2) + " MB";
  }

  // 时长（秒）→ 人看的短格式：运行状态、消息间隔、请求表「用时」列都用它（v3.19 统一）
  function fmtDur(s){
    if(s === null || s === undefined) return "—";
    s = Math.max(0, s);
    if(s < 1) return Math.max(1, Math.round(s * 1000)) + "ms";
    // 整数秒不补 .0（「5s前收到消息」比「5.0s前」自然），亚秒级才留一位
    if(s < 60) return (s < 10 ? s.toFixed(1).replace(/\.0$/, "") : String(Math.round(s))) + "s";
    var m = Math.floor(s / 60), r = Math.round(s % 60);
    if(m < 60) return m + "m" + (r ? r + "s" : "");
    // 跨小时的间隔（「距上一轮」常见）用 h/d，240m 不如 4h 直观
    var h = Math.floor(m / 60), rm = m % 60;
    if(h < 24) return h + "h" + (rm ? rm + "m" : "");
    var d = Math.floor(h / 24), rh = h % 24;
    return d + "d" + (rh ? rh + "h" : "");
  }

  // v4.68（用户 m02472）：24 小时 token 会到百万级，原来一律折成 k（2119831 → 2119.8k），
  // 位数太多反而看不清量级。现在 k / M / B 三档往上走。
  function tokText(n){
    if(n === null || n === undefined) return "—";
    if(n >= 1e9) return (n / 1e9).toFixed(1) + "B";
    if(n >= 1e6) return (n / 1e6).toFixed(1) + "M";
    return n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);
  }

  // 老 trace 里 sent.text 存的是 MessageChain([Text('...')]) 这种对象字面量
  // （v2.63 起后端改存纯文本，但盘上已有的记录得在这儿救一下）
  function chainPlain(s){
    s = String(s || "");
    if(s.indexOf("MessageChain(") !== 0) return s;
    // 框架的 __repr__ 长这样：MessageChain([Text(正文), Text(正文2)])
    // 正文里可能带括号，所以按括号配对切，不能只用正则贪婪匹配
    var out = "", i = 0;
    for(;;){
      var a = s.indexOf("Text(", i);
      if(a < 0) break;
      var j = a + 5, depth = 1;
      while(j < s.length){
        var ch = s.charAt(j);
        if(ch === "(") depth++;
        else if(ch === ")"){ depth--; if(depth === 0) break; }
        j++;
      }
      out += s.slice(a + 5, j);
      i = j + 1;
    }
    return out || s;
  }

  // 三档共用折叠头，单步也显示真实 token 与命中统计。
  function traceChips(tr, unreported){
    if(!tr || !(tr.steps || []).length) return "";
    var tot = tr.totals || {}, steps = tr.steps || [];
    var h = "<span class='tk'>" + steps.length + " 步</span>" +
      "<span class='tk'>工具 " + ((tr.tools || []).length) + " 次</span>" +
      "<span class='tk'>流水线总耗时 " + (tr.elapsed || 0) + " 秒</span>";
    var calls = steps.map(function(s){ return s.time_consumed; });
    if(calls.length && calls.every(function(n){ return n !== null && n !== undefined && isFinite(Number(n)); })){
      var modelSeconds = calls.reduce(function(n, x){ return n + Number(x); }, 0);
      h += "<span class='tk'>模型调用 " + Number(modelSeconds.toFixed(3)) + " 秒</span>";
    }
    h += "<span class='tk'>输入 " + tokText(tot.input_tokens) + " · 输出 " + tokText(tot.output_tokens) + "</span>";
    if(unreported) h += "<span class='tk'>缓存命中 上游未报</span>";
    else if(tot.cache_hit !== null && tot.cache_hit !== undefined) h += "<span class='tk'>缓存命中 " + tot.cache_hit + "%</span>";
    return h + "<span class='dim'>（思考 · 工具调用与返回 · token）</span>";
  }

  function traceHtml(tr, unreported){
    if(!tr || !(tr.steps || []).length) return "";
    var tot = tr.totals || {};
    var steps = tr.steps || [];
    // 展开按钮跟 ①②③ 一样排最左（用户 m33271 图2：④ 的按钮跑到右边去了）
    // 头行跟 ①②③ 共用 .seqh：原来自己一套 .trace-hd，字号/间距/颜色都不同，
    // 用户 m33400 说三档「说了要做一样，结果还是有区别」—— 现在四段一个模子。
    var h = "<div class='seqh'><span class='mini' data-fold='trace'>展开</span>④ agent 轨迹";
    h += traceChips(tr, unreported) + "<span class='grow'></span></div>";
    h += "<div data-foldbody='trace' style='display:none'>";
    steps.forEach(function(s){
      h += "<div class='tstep'><div class='th'>第 " + (s.index || 0) + " 步";
      h += "<span class='tk'>入 " + tokText(s.input_tokens) + " · 出 " + tokText(s.output_tokens) + "</span>";
      var sRep = (s.cached_reported === false) || (s.cached_reported === undefined && !!unreported);
      if(sRep){
        // 用户 m25513：这一步上游没回报缓存，跟上面那格一样写「上游未报」，别只留一个破折号。
        h += "<span class='tk'>命中 上游未报</span>";
      } else if(s.cached_tokens !== null && s.cached_tokens !== undefined){
        h += "<span class='tk'>命中 " + tokText(s.cached_tokens) +
             (s.cache_hit !== null && s.cache_hit !== undefined ? "（" + s.cache_hit + "%）" : "") + "</span>";
      }
      if(s.time_consumed) h += "<span class='tk'>模型调用 " + s.time_consumed + " 秒</span>";
      h += "</div><div class='tb'>";
      if(s.reasoning){
        h += "<div class='lbl'>思考</div><div class='think'>" + esc(s.reasoning) + "</div>";
      }
      // v3.58（用户 m16875 图3）：④ 里原来调用工具是「一行等宽字」、工具返回是整块 JSON 平铺
      // （search 一次就 3862 字）。现在跟 ① ② ③ 用同一套：调用走 tcall 卡，返回认得出结构就
      // 排成结果卡/着色 JSON 并折叠，认不出还是原文。原文档看 traceRawText，一个字都不改。
      if((s.tool_calls || []).length){
        h += "<div class='lbl'>调用工具</div><div class='tcall-wrap'>" + toolCallList(s.tool_calls) + "</div>";
      }
      (s.tool_results || []).forEach(function(r){
        var resultRaw = r.content == null ? "" : String(r.content);
        var tj = tryJson(resultRaw);
        h += "<div class='lbl'>工具返回 · " + esc(r.name) + " · " + (r.chars || 0) + " 字</div>" +
             "<div class='res-blk'>" + toolBodyHtml(resultRaw, tj) + "</div>";
      });
      if(s.text){
        // 输出与思考共用块布局；美化提取正文，对照完整保留原始 XML/空白。
        var outTxt = "";
        if(VIEW === "mixed") outTxt = s.text;
        else try{
          outTxt = (parseAssistant(s.text) || []).map(function(p){ return p.text; })
                    .filter(function(x){ return !!x; }).join("\n");
        }catch(e){ outTxt = ""; }
        h += "<div class='lbl'>" + ((s.tool_calls || []).length ? "这一步的文本" : "输出") + "</div>" +
             "<div class='think out-blk out mono" + (VIEW === "mixed" ? " rawtxt" : "") + "'>" +
             esc(outTxt || s.text).replace(/\r/g, "&#13;") + "</div>";
      }
      h += "</div></div>";
    });
    h += "</div>";
    // 不重复列“最后实际发出”；模型步骤输出不等于成功发送，实际发送另由 sent 记录。
    return h;
  }

  // 原文模式：agent 执行过程压成纯文本
  function traceRawText(tr){
    if(!tr || !(tr.steps || []).length) return "(这一轮没有轨迹记录)";
    var s = "";
    (tr.steps || []).forEach(function(st){
      var head = "第 " + (st.index || 0) + " 步";
      var meta = "入 " + tokText(st.input_tokens) + " · 出 " + tokText(st.output_tokens);
      if(st.cached_tokens !== null && st.cached_tokens !== undefined) meta += " · 命中 " + tokText(st.cached_tokens);
      if(st.time_consumed) meta += " · 模型调用 " + st.time_consumed + " 秒";
      s += "┌─ " + head + " " + "─".repeat(Math.max(4, 54 - head.length)) + "\n";
      s += "│  " + meta + "\n";
      s += "└" + "─".repeat(58) + "\n";
      if(st.reasoning) s += "  [思考]\n" + indent(st.reasoning) + "\n";
      (st.tool_calls || []).forEach(function(c){
        s += "  [调用工具] " + c.name + "\n" + indent(c.args || "(无参数)") + "\n";
      });
      (st.tool_results || []).forEach(function(x){
        s += "  [工具返回] " + (x.name || "") + "\n" + indent(x.content || "") + "\n";
      });
      if(st.text) s += "  [这一步的输出]\n" + indent(st.text) + "\n";
      s += "\n";
    });
    return s.replace(/\n+$/, "");
  }

  // 原文模式排版用：每行缩进两格（"│   " 那种框线在等宽字体里更好读）
  function indent(t){
    return String(t == null ? "" : t).split("\n").map(function(l){ return "    " + l; }).join("\n");
  }

  // 详情行重新渲染后，把它之前摊开的 ①②③④ 再摊开（用户 m33573：切档后全折回去了）
  function restoreRowFolds(target, name){
    var idxs = restoreFolds[name];
    if(!idxs || !idxs.length) return;
    delete restoreFolds[name];
    var bodies = target.querySelectorAll("[data-foldbody]");
    var btns = target.querySelectorAll("[data-fold]");
    idxs.forEach(function(i){
      if(bodies[i]) bodies[i].style.display = "";
      if(btns[i]) btns[i].textContent = "收起";
    });
  }

  // v4.35（用户 m19642）：同一条消息在 messages 里是「[00:22] [message_id: X] …」，
  // 到了 user_prompt 里会换成「[Oct 30 2026 00:22 Fri] [message_id: X] …」——逐字比对
  // 永远不相等，③ 于是把本轮那几条画了两遍（图4/图5 的红框重复）。这里统一先按
  // message_id 认人，认不出来再去掉开头的时间戳/日期戳比正文，只比、不动。
  function midOfText(t){
    var m = String(t == null ? "" : t).match(/message_id:\s*(-?\d+)/);
    return m ? m[1] : "";
  }
  function headlessText(t){
    var s = String(t == null ? "" : t);
    for(var i = 0; i < 3; i++){
      var m = s.match(/^\s*\[(?:\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2}[^\]]*|[A-Z][a-z]{2} \d{1,2} \d{4}[^\]]*)\]\s*/);
      if(!m) break;
      s = s.slice(m[0].length);
    }
    return s.trim();
  }
  // 同源注记在 messages 带声明，在 incoming 只存正文。仅在双方都是注记时
  // 忽略这层展示声明；用户同文、不同判定时间不能因此被吞掉。
  function decNoteClock(m, text){
    var head = String(text || "").match(/^\s*\[([^\]]+)\]/);
    var hm = head && head[1].match(/(?:^|\s|T)(\d{1,2}:\d{2})(?::\d{2})?(?:\s|$)/);
    if(hm) return hm[1].replace(/^(\d):/, "0$1:");
    var ts = Number(m && m.ts);
    if(ts > 0){
      var d = new Date(ts * 1000);
      return ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
    }
    return "";
  }
  function sameMsgText(a, b, ma, mb){
    var x = String(a == null ? "" : a), y = String(b == null ? "" : b);
    var ix = midOfText(x), iy = midOfText(y);
    if(ix || iy) return !!ix && !!iy && ix === iy;
    var na = !!ma && ma.role !== "user" && isDecNoteLike(ma);
    var nb = !!mb && mb.role !== "user" && isDecNoteLike(mb);
    if(ma && mb && na !== nb) return false;
    if(na && nb){
      var ta = Number(ma.ts), tb = Number(mb.ts);
      if(ta > 0 && tb > 0 && ta !== tb) return false;
      var ca = decNoteClock(ma, x), cb = decNoteClock(mb, y);
      if(ca && cb && ca !== cb) return false;
      return decNoteHead(headlessText(x)).trim() === decNoteHead(headlessText(y)).trim();
    }
    if(x === y) return true;
    return headlessText(x) === headlessText(y);
  }
  // v4.36（用户 m19945 图3）：同一轮里 messages 的消息头是「[01:07] [message_id: …]」，
  // 从 incoming / user_prompt 过来的是框架原样的「[Oct 30 2026 01:07 Fri] …」，一条一个样。
  // 这里把长日期头压成 [HH:MM]，别的字一个不动；决策快照不经过它。
  function tsShortOf(t){
    var s0 = String(t == null ? "" : t);
    return s0.replace(/^\s*\[[A-Z][a-z]{2}\s+\d{1,2}\s+\d{4}\s+(\d{1,2}:\d{2})(?:\s+[A-Z][a-z]{2})?\]/,
                      function(_w, hm){ return "[" + hm + "]"; });
  }
  // v4.65（用户 m36008）：注记发出去时带「[HH:MM] [对当前聊天消息的决策建议] 」前缀，
  // 认注记、画原文都先把这两截剥掉，别把声明画两遍。
  function decNoteHead(t){
    var s0 = String(t == null ? "" : t).replace(/^\s+/, "");
    for(var k0 = 0; k0 < 3; k0++){
      var m0 = s0.match(/^\[(\d{1,2}:\d{2})\]\s*/);
      if(!m0) break;
      s0 = s0.slice(m0[0].length);
    }
    var d0 = s0.match(/^\[\s*(?:对当前聊天消息的决策建议|对以上聊天记录的决策建议)\s*\]\s*/);
    if(d0) s0 = s0.slice(d0[0].length);
    return s0;
  }
  // v4.36（用户 m19945）：原文档在美化档的 isDecNoteRow 赋值之前就要认注记，抽到外面来。
  function isDecNoteLike(m){
    if(!m) return false;
    if(m.decnote === true) return true;
    if(String(m.name || m.who || "") === DEC_NOTE_NAME) return true;
    var dec9 = String(rawContent(m.content) || "");
    if(dec9.indexOf("[决策记录]") === 0) return true;
    if(String(m.role || "") !== "assistant") return false;
    var hd9 = decNoteHead(dec9);
    return hd9.indexOf("建议回复") === 0 || hd9.indexOf("建议不回") === 0
      || hd9.indexOf("判定要回") === 0 || hd9.indexOf("判定不回") === 0;
  }
  // 用户 m20315：工具回调（nai 出图这类异步结果）是插件替 bot 写进窗口的一条
  // assistant 消息，名字是「工具回调·<工具>」，正文前面带一句「【工具回调 x】」声明。
  // 页面要按 assistant 画它（不是 user），声明剥掉，名字用工具名。
  function isToolNoteMsg(m){
    if(!m) return false;
    if(m.toolnote === true || m.tnote === true) return true;
    return String(m.name || "").indexOf("工具回调·") === 0;
  }
  function toolNoteBody(t){
    // 快照里的工具回调正文前面还有 stamp_assistant_time 补的 [HH:MM]，
    // 声明要连时间一起认，时间留着（卡片头部要用），只剥声明。
    return String(t == null ? "" : t).replace(/^(\s*\[\d{1,2}:\d{2}\]\s*)?【工具回调[^】]*】\s*/,
      function(_w, ts){ return ts || ""; });
  }

  // 本地前缀只作独立对比提示，绝不决定消息所属轮次；上游 token 才是缓存实测。
  function localPrefixLen(r){
    var rec=r || {}, msgs=Array.isArray(rec.messages) ? rec.messages : [];
    if(rec.cache_comparable===false) return -1;
    if(typeof rec.cache_prefix_len==="number" && Number.isFinite(rec.cache_prefix_len))
      return Math.max(0,Math.min(Math.floor(rec.cache_prefix_len),msgs.length));
    var flags=typeof rec.cache_tail_flags==="string" ? rec.cache_tail_flags : "", n=0;
    if(!flags) return -1;
    while(n<msgs.length && flags.charAt(n)==="h") n++;
    return n;
  }

  // ② 是上轮结束前的历史：决策截至最后注记，回复截至上次实际回复，trim 保全历史。
  function segBoundX(r){
    var rec=r || {}, msgs=Array.isArray(rec.messages) ? rec.messages : [];
    if(String(rec.outcome || "")==="trim" || String(rec.trigger_reason || "")==="trim" || rec.trim_only===true) return msgs.length;
    var dec=String(rec.outcome || "")==="decision" || String(rec.trigger_reason || "")==="decision" || rec.decision_only===true;
    if(dec){
      for(var di=msgs.length-1;di>=0;di--) if(msgs[di] && msgs[di].role!=="user" && isDecNoteLike(msgs[di])) return di+1;
      return 0;
    }
    var source=Array.isArray(rec.incoming_messages) ? rec.incoming_messages :
      (Array.isArray(rec.user_prompt_parts) ? rec.user_prompt_parts : []).map(function(p){return {role:"user",content:p && p.text};});
    var upper=msgs.length;
    source.forEach(function(m){
      if(!m) return;
      var body=rawContent(m.content), mid=m.role==="user" ? midOfText(body) : "";
      for(var i=0;i<upper;i++){
        var old=msgs[i];
        if(!old || old.role!==m.role) continue;
        // 只有确切同源本轮消息能排除其后的本轮已发输出，历史最后 note 不是上界。
        var exactNote=m.role!=="user" && isDecNoteLike(m) && isDecNoteLike(old) && Number(m.ts)>0 && Number(old.ts)===Number(m.ts) &&
          sameMsgText(rawContent(old.content),body,old,m);
        if(exactNote || (mid && midOfText(rawContent(old.content))===mid)){upper=i;break;}
      }
    });
    for(var ai=upper-1;ai>=0;ai--){
      var reply=msgs[ai];
      if(reply && reply.role==="assistant" && !isDecNoteLike(reply) && !isToolNoteMsg(reply) && String(rawContent(reply.content) || "").trim()) return ai+1;
    }
    return 0;
  }

  // VM 的 text/media 与 ownership projection 是两层，绝不从 VM.media 猜归属。
  // m.display 也可能是旧的完整 VM；显式空数组必须一路保留到 displayOf。
  function snapshotDisplay(m, vm){
    function projection(d){
      return d && typeof d === "object" && !Array.isArray(d) &&
        (typeof d.own_media_known === "boolean" || Array.isArray(d.own_media) ||
         Array.isArray(d.unassigned_media) || typeof d.own_body === "string" ||
         Object.prototype.hasOwnProperty.call(d, "direct_reply")) ? d : null;
    }
    var md = m && m.display;
    var view = md && !projection(md) ? md : (vm || {});
    return {view:view, display:projection(md) || projection(view.display) || undefined};
  }
  function carrySnapshotDisplay(m, vm){
    var d = snapshotDisplay(m, vm), media = d.view.media, files = d.view.files;
    // v4.34（用户 m01303）：快照里的 [文件] 一直是裸文本——后端把文件名/大小投影到了
    // 显示层（files），这里只搬 display / media，files 被丢在半路，卡片就画不出来。
    var hasFiles = Array.isArray(files) && files.length && !(m.files || []).length;
    if(d.display || (Array.isArray(media) && media.length && !(m.media || []).length) || hasFiles){
      var out = Object.assign({}, m);
      if(d.display) out.display = d.display;
      if(Array.isArray(media) && media.length && !(m.media || []).length) out.media = media;
      if(hasFiles) out.files = files;
      return out;
    }
    return m;
  }
  // 三档共用投影，不改快照；history/display 投影不得改变边界、顺序、条数或字符数。
  function snapshotPartition(r){
    var rec=r || {}, msgs=Array.isArray(rec.messages) ? rec.messages.slice() : [];
    var trim=String(rec.outcome || "")==="trim" || String(rec.trigger_reason || "")==="trim" || rec.trim_only===true;
    var dec=String(rec.outcome || "")==="decision" || String(rec.trigger_reason || "")==="decision" || rec.decision_only===true;
    var bound=segBoundX(rec), suffix=msgs.slice(bound), source=[], incoming=[], tail=[];
    if(!trim){
      if(Array.isArray(rec.incoming_messages)) source=rec.incoming_messages;
      else {
        source=(Array.isArray(rec.user_prompt_parts) ? rec.user_prompt_parts : [])
          .filter(function(p){return p && String(p.text || "").trim();})
          .map(function(p){return {role:"user",content:p.text};});
        if(!source.length && dec) source=decTranscriptLines(rec).map(function(p){return {role:p.role,content:p.text};});
        if(!source.length && rec.user_prompt) source=splitIncoming(rec.user_prompt).map(function(t){return {role:"user",content:t};});
      }
    }
    // 同一条本轮注记的重复投影排到触发消息后；独立存储注记仍全部保留。
    if(!dec){
      while(suffix.length && isDecNoteLike(suffix[suffix.length-1])){
        var last=suffix[suffix.length-1];
        if(!source.some(function(m){return m && m.role===last.role && Number(last.ts)>0 && Number(m.ts)===Number(last.ts) &&
          sameMsgText(rawContent(last.content),rawContent(m.content),last,m);})) break;
        tail.unshift(suffix.pop());
      }
    }
    var used={}, viewByIndex={};
    // ③ 本轮消息里的引用按本轮自己的投影取图，别用②历史里同一 id 的旧投影。
    (rec.incoming_display_messages || []).forEach(function(vm, i){
      var m = source[i]; if(!m) return;
      var mid = midOfText(rawContent(m.content));
      if(!mid) return;
      var media = (vm && vm.media) || m.media || [];
      var ts = String(m.ts || "").trim();
      if(!ts){ var tm = String(m.text || m.content || "").match(/^\[(\d{1,2}:\d{2})\]/); if(tm) ts = tm[1]; }
      var raw = String((vm && vm.raw) || m.content || "");
      var body = raw.indexOf("|") >= 0 ? raw.slice(raw.indexOf("|") + 1).trim() : raw;
      var nm = String((vm && vm.name) || m.name || "").replace(/\s+/g, " ").trim();
      if(!nm){
        var nm2 = raw.match(/user_nickname:\s*([^,\]]+)/);
        if(nm2) nm = nm2[1].trim();
      }
      // 用户本轮（G3）：引用卡头也显示「昵称 · 群昵称」；nm 本身保持纯昵称。
      var gn = String((vm && vm.gnick) || m.gnick || "").trim();
      if(!gn) gn = gnickOfRaw(raw);
      QUOTE_IDX[mid] = quoteEntryOf({name: nm, gnick: gn, ts: ts,
        display: {own_media_known: false, own_media: [], unassigned_media: media, direct_reply: null, source: "incoming"}},
        body);
    });
    source.forEach(function(m,i){
      if(!m) return;
      var vm=(rec.incoming_display_messages || [])[i];
      // 优先消费③内的一次投影；②同文不是本轮身份，跨界必须有明确 id / exact ts。
      var match=-1;
      for(var j=bound;j<msgs.length;j++){
        if(!used[j] && msgs[j] && String(msgs[j].role || "user")===String(m.role || "user") &&
           sameMsgText(rawContent(msgs[j].content),rawContent(m.content),msgs[j],m)){match=j;break;}
      }
      if(match<0) for(var h=0;h<bound;h++){
        var old=msgs[h];
        if(used[h] || !old || String(old.role || "user")!==String(m.role || "user")) continue;
        var oldId=midOfText(rawContent(old.content)), newId=midOfText(rawContent(m.content));
        if(((oldId && newId && oldId===newId) || (!oldId && !newId && Number(old.ts)>0 && Number(old.ts)===Number(m.ts))) &&
           sameMsgText(rawContent(old.content),rawContent(m.content),old,m)){match=h;break;}
      }
      if(match>=0){
        viewByIndex[match]=carrySnapshotDisplay(m,vm);
        used[match]=true;return;
      }
      incoming.push(carrySnapshotDisplay(m,vm));
    });
    var current=trim ? [] : suffix.concat(incoming,tail).map(function(m){
      var at=msgs.indexOf(m), canonical=carrySnapshotDisplay(m,(rec.display_messages || [])[at]);
      return carrySnapshotDisplay(canonical,viewByIndex[at]);
    });
    var stat=typeof rec.system_static==="string" ? rec.system_static : (rec.system_prompt || "");
    var reloc=!trim && typeof rec.system_static==="string" ? (rec.system_relocated || "") : "";
    if(reloc && current.some(function(m){return m && String(rawContent(m.content) || "").indexOf(reloc)>=0;})) reloc="";
    function chars(rows){return rows.reduce(function(n,m){
      if(!m) return n;
      if(typeof m.content === "string") n+=m.content.length;
      else if(m.content !== null && m.content !== undefined) n+=JSON.stringify(m.content).length;
      if(m.tool_calls && m.tool_calls.length) n+=JSON.stringify(m.tool_calls).length;
      return n;
    },0);}
    var hc=chars(msgs.slice(0,bound)), bodyChars=chars(current), cc=bodyChars+reloc.length;
    var prefix=localPrefixLen(rec);
    var sections=trim ? [{key:"history",label:"历史消息",chars:hc}] : [
      {key:"system",label:"系统提示词",chars:String(stat).length},
      {key:"history",label:"历史消息",chars:hc},
      {key:"incoming",label:"本轮输入（含重送）",chars:bodyChars},
      {key:"dynamic",label:"动态段",chars:reloc.length}
    ];
    return {messages:msgs,bound:bound,currentRows:current,historyChars:hc,currentChars:cc,
      currentCount:current.filter(function(m){return !!m;}).length+(reloc?1:0),relocated:reloc,
      prefixKnown:!trim && prefix>=0,prefixLen:prefix,sections:sections,trim:trim};
  }

  var sessionsInFlight = null;
  var sessionsLightInFlight = null;
  function getContextSessions(light){
    // 轻量状态拍不能挂在较慢的完整会话列表后面。
    if(light ? sessionsLightInFlight : sessionsInFlight){
      return light ? sessionsLightInFlight : sessionsInFlight;
    }
    var url = API + "/context-sessions" + (light ? "?light=1" : "");
    var p = get(url).then(function(d){
      (d.items || []).forEach(sampleStateClock);
      if(light) sessionsLightInFlight = null; else sessionsInFlight = null;
      return d;
    }, function(e){
      if(light) sessionsLightInFlight = null; else sessionsInFlight = null;
      throw e;
    });
    if(light) sessionsLightInFlight = p; else sessionsInFlight = p;
    return p;
  }
  // 用上一次的缓存先铺满页面，免得外壳重建 iframe 时整块空白。
  var overviewInFlight = null;
  function loadOverview(){
    if(overviewInFlight) return overviewInFlight;
    overviewInFlight = get(API + "/overview?light=1").then(function(d){ cachePut("ov", d); renderCards(d); }).catch(function(e){ document.getElementById("subtitle").textContent = "读取失败：" + e.message; }).then(function(){ overviewInFlight = null; });
    return overviewInFlight;
  }

  var stateTimer = null;
  function paintStateNodes(map){
    var nodes = document.querySelectorAll("#ctx-list .st[data-st]");
    for(var i = 0; i < nodes.length; i++){
      var s = map[nodes[i].getAttribute("data-st")];
      var html = s ? stateTag(s) : "";
      if(nodes[i].innerHTML !== html) nodes[i].innerHTML = html;
    }
    paintStateStripes(map);
  }
  // 竖杠指示灯也每秒更一次（用户 m13708 第 2 条）：只改 class 和 title，不动 DOM 结构，
  // 不会打断滚动或悬停。判定还是 stateLvl 那一个，所以和状态文字永远同步。
  function paintStateStripes(map){
    var its = document.querySelectorAll("#ctx-list .it[data-ctx]");
    for(var j = 0; j < its.length; j++){
      var sid = its[j].getAttribute("data-ctx");
      var s = map[sid];
      if(!s) continue;
      var sp = its[j].querySelector(".stripe");
      if(!sp) continue;
      var lv = stateLvl(s);
      var cls = "stripe " + lv;
      if(sp.className !== cls) sp.className = cls;
      var tt = stateStripeTitle(s, lv);
      if(sp.getAttribute("title") !== tt) sp.setAttribute("title", tt);
    }
  }
  // 静默 / 休兜 的倒计时：每 1 秒只改那几颗胶囊的文字（用户 m08226 第 2 条）
  function tickEtas(){
    var list = document.querySelectorAll("#ctx-list [data-eta]");
    if(!list || !list.length) return;
    var now = Date.now() / 1000;
    for(var i = 0; i < list.length; i++){
      var el = list[i];
      var at = parseFloat(el.getAttribute("data-eta") || "0") || 0;
      var lbl = el.getAttribute("data-eta-lbl") || "";
      var txt = lbl + " " + Math.max(0, Math.round(at - now)) + "秒";
      if(el.textContent !== txt) el.textContent = txt;
    }
  }
  function stopStateTimer(){ if(stateTimer){ clearInterval(stateTimer); stateTimer = null; } }
  // 用户 m17375 第 4 条：新回复要马上出现在记录页。轻量轮询现在也带
  // rounds / last_ts，这两个值一变就说明刚来了一轮，立刻去拉记录，
  // 不用干等 5 秒那一拍（实测 4-5 秒能压到 1-2 秒）。
  var pollRoundSig = Object.create(null);
