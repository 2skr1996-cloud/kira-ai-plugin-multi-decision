  function tailChip(html, chip){
    if(!chip) return html;
    if(!html) return chip;
    // A final native video is an inert attachment card, not the last speech div.
    var vi = html.lastIndexOf("<div class='qcard video'");
    if(vi >= 0 && cardOnlyHtml(html.slice(vi)))
      return html.slice(0, vi) + "<div class='chip-line'>" + html.slice(vi) + chip + "</div>";
    // 气泡始终留在正文内；折叠时由 CSS 隐藏重复字数，展开后仍紧贴末尾。
    var i = html.lastIndexOf("</div>");
    if(i < 0){
      // v3.98（用户 m10169 图1）：抖音分享卡整条是一个 <a>（douyinCard），里面没有 </div>，
      // 以前气泡直接拼在 </a> 后面就落到卡片外面，看着像没有「待发 / 计入」。
      // 插到 </a> 之前，气泡跟 B 站卡一样落在卡片右下角那一行。
      if(html.indexOf("class='qcard") >= 0){
        var j = html.lastIndexOf("</a>");
        if(j >= 0) return html.slice(0, j) + chip + html.slice(j);
      }
      return html + chip;
    }
    // 正文末尾常带一个换行（.mono 是 pre-wrap，换行会真的换一行），气泡就被顶到
    // 下一行去了（用户 m33400 图4：「112 字」单独占一行）。插之前把尾部空白吃掉。
    var head = html.slice(0, i).replace(/[\s\u00a0]+$/, "");
    return head + chip + html.slice(i);
  }

  // ---------------------------------------------------------------- 视图模式
  // pretty = 美化（元数据收进头部标签，正文只剩人说的话）
  // mixed  = 对照（卡片外壳还在，正文用原文，方便对着代码核对）
  // raw    = 原文（不做任何装饰，直接给发出去的样子）
  var VIEW = (function(){ try{ return localStorage.getItem("mdp_view") || "pretty"; }catch(e){ return "pretty"; } })();
  function paintViewSw(){
    Array.prototype.forEach.call(document.querySelectorAll("[data-view]"), function(b){
      b.classList.toggle("on", b.getAttribute("data-view") === VIEW);
    });
  }
  // 切档重画后要重新摊开的请求详情块：name -> [foldKey]（用户 m33573：切档全折回去了）
  var restoreFolds = {};
  // 展开过哪几段记在这儿：换美化/对照/原文会整块重画，不记就全折回去（用户 m30252）
  var foldOpen = {};
  function ensureFwdPop(){
    var box = document.getElementById("fwd-pop");
    if(box) return box;
    box = document.createElement("div");
    box.id = "fwd-pop";
    box.className = "fwd-pop";
    box.hidden = true;
    box.innerHTML = "<div class='fp-mask' data-fwdpop='close'></div>" +
      "<div class='fp-box' role='dialog' aria-modal='true' aria-label='合并转发'>" +
      "<div class='fp-hd'><span class='fp-t'></span>" +
      "<button class='fp-x' type='button' data-fwdpop='close' title='关闭' aria-label='关闭'>×</button></div>" +
      "<div class='fp-bd'></div></div>";
    document.body.appendChild(box);
    bindFwdPopClick(box);
    return box;
  }
  // 用户 m03343 第 1 条：转发弹层以前点 × 和遮罩都关不掉——winClick 只挂在几个面板
  // 盒子上（box.onclick = winClick），而 #fwd-pop 是 appendChild 到 body 的，click 永远
  // 进不去。所以弹层自己挂一份：点 ×/遮罩关，点框外空白也关。
  function bindFwdPopClick(box){
    if(!box || box.getAttribute("data-bound")) return;
    box.setAttribute("data-bound", "1");
    box.addEventListener("click", function(ev){
      var t = ev.target;
      if(t && t.closest && t.closest("[data-fwdpop]")){ closeFwdPop(); return; }
      if(t && t.closest && !t.closest(".fp-box")) closeFwdPop();
    });
  }
  function openFwdPop(card){
    var tpl = card.querySelector("template.fwd-full");
    if(!tpl) return;
    var box = ensureFwdPop();
    var hd = card.querySelector(".fwd-hd");
    box.querySelector(".fp-t").textContent = hd ? hd.textContent : "合并转发";
    box.querySelector(".fp-bd").innerHTML = tpl.innerHTML;
    box.hidden = false;
    box.querySelector(".fp-bd").scrollTop = 0;
    document.body.classList.add("fwd-pop-open");
  }
  function closeFwdPop(){
    var box = document.getElementById("fwd-pop");
    if(!box || box.hidden) return;
    box.hidden = true;
    box.querySelector(".fp-bd").innerHTML = "";   // 关掉就把 DOM 清空，图片不再挂在页面上
    document.body.classList.remove("fwd-pop-open");
  }
  // ---- 图片大图层（用户 m03343 第 2 条）
  // 页面上所有缩略图（正文、引用块、待发、转发弹层里）都带 data-img，点一下就地弹大图，
  // 不再 target='_blank' 开新标签页；图按原比例缩进视口，只缩不放，不裁切。
  // 用户 m10400：长图在 86vh 里被压扁看不清，补滚轮缩放、底部缩放条、1:1 与自适应还原。
  // 缩放只改 transform、不动布局，所以「点空白处关闭」的判定范围还是那张自适应尺寸的图。
  var IP = { zoom: 1, tx: 0, ty: 0, fit: 1, one: 1, drag: null, moved: false, pending: false };
  var IP_MIN = 0.2, IP_MAX = 12;
  function ipBox(){ return document.getElementById("img-pop"); }
  function ipImg(){ var b = ipBox(); return b ? b.querySelector(".ip-img") : null; }
  function ipMeasure(){
    var im = ipImg();
    if(!im || !im.naturalWidth) return;
    // offsetWidth 是布局宽度（transform 不影响它），也就是「自适应」时的显示宽度
    var w = im.offsetWidth || im.naturalWidth;
    IP.fit = w / im.naturalWidth;
    IP.one = IP.fit > 0 ? 1 / IP.fit : 1;
  }
  function ipLabel(){
    var box = ipBox();
    if(!box) return;
    var lab = box.querySelector(".ip-zoom");
    var pct = Math.round(IP.zoom * IP.fit * 100);
    var atFit = Math.abs(IP.zoom - 1) < 0.001;
    var atOne = Math.abs(IP.zoom - IP.one) < 0.01;
    if(lab) lab.textContent = atFit ? ("自适应 " + pct + "%") : (atOne ? ("1:1 " + pct + "%") : ("放大 " + pct + "%"));
    var fb = box.querySelector("[data-ipzoom='fit']");
    var ob = box.querySelector("[data-ipzoom='one']");
    if(fb) fb.classList.toggle("on", atFit);
    if(ob) ob.classList.toggle("on", atOne);
    var out = box.querySelector("[data-ipzoom='out']");
    var inn = box.querySelector("[data-ipzoom='in']");
    if(out) out.disabled = IP.zoom <= IP_MIN + 1e-6;
    if(inn) inn.disabled = IP.zoom >= IP_MAX - 1e-6;
  }
  function ipApply(){
    var box = ipBox(), im = ipImg();
    if(!box || !im) return;
    im.style.transform = "translate(" + IP.tx + "px," + IP.ty + "px) scale(" + IP.zoom + ")";
    box.classList.toggle("zoomed", Math.abs(IP.zoom - 1) > 0.001);
    ipLabel();
  }
  function ipReset(){ IP.zoom = 1; IP.tx = 0; IP.ty = 0; ipApply(); }
  // 以 (cx,cy) 为锚点缩放：锚点下面那个像素缩放前后停在原地
  function ipZoomAt(z2, cx, cy){
    var im = ipImg();
    if(!im) return;
    z2 = Math.max(IP_MIN, Math.min(IP_MAX, z2));
    if(Math.abs(z2 - IP.zoom) < 1e-6) return;
    var r = im.getBoundingClientRect();
    var ox = cx - (r.left + r.width / 2);
    var oy = cy - (r.top + r.height / 2);
    var k = 1 - z2 / IP.zoom;
    IP.tx += ox * k;
    IP.ty += oy * k;
    IP.zoom = z2;
    ipApply();
  }
  var BLK_STRIP = " \t\r\n，。！？!?~～、,.;；:：…·•—-_*#@'\"“”‘’";
  var blkList = null;   // {enabled, items, count}：这个会话当前生效的清单
  var blkSid = "";
  function blkNorm(t){
    t = String(t === null || t === undefined ? "" : t);
    var i = 0, j = t.length;
    while(i < j && BLK_STRIP.indexOf(t.charAt(i)) >= 0) i++;
    while(j > i && BLK_STRIP.indexOf(t.charAt(j - 1)) >= 0) j--;
    return t.slice(i, j).toLowerCase();
  }
  // 一张卡片「真正说的那句话」：引用块、展开按钮、字数气泡、卡片标题都不算正文，
  // 不然拉黑存下来的是一串元数据，永远匹配不上下一句一样的话
  // extraSel：额外要剔掉的选择器（用户 m12463 用它把 QQ 菜单卡那排指令胶囊排除在
  // 字数之外——那是按钮不是正文，算进去会把一句 9 个字的问候判成长文）。
  function blockTextOf(card, extraSel){
    var bd = card.querySelector(".mc-bd");
    if(!bd) return "";
    var c = bd.cloneNode(true);
    Array.prototype.forEach.call(c.querySelectorAll(".quote,.more,.chb,.mixnote,.qcard,.qfile,.unassigned-media,.rawtxt,.body-empty,.thumbs,.mc-blk" + (extraSel || "")), function(x){
      if(x.parentNode) x.parentNode.removeChild(x);
    });
    var bodies = c.querySelectorAll(".mono");
    var t = bodies.length ? Array.prototype.map.call(bodies, function(x){ return x.textContent || ""; }).join(" ") : (c.textContent || "");
    // 只拒绝整段媒体占位；人话中讨论 [图片]、[重点] 等字样必须原样保留。
    if(mediaOnlyBody(t)) return "";
    return t.replace(/[\s\u00a0]+/g, " ").trim();
  }
  function markBlockBtn(card){
    if(!card) return;
    // 用户 m03014 第 2 条：转发卡不给「拉黑」——一张合并转发不是「某个人说的一句话」，
    // 拿卡里的文字去比对短语清单只会误伤，按钮挂在转发卡上也容易被当成转发的一部分。
    if(card.querySelector(".fwd")) return;
    // 用户 m01011 第 3 条：按钮要排在小标签（字数、名字、时间…）后面，不再浮在卡片最右边。
    // 普通卡片：插在头部标签行 .grow 之前，正好落在字数气泡后面。
    // 组内卡片（用户 m01522 第 2 条）：头部只有一列时间，塞进去会掉到时间下面，
    // 改挂正文末尾 —— 字数气泡本来就在 .mono 里面，这样读起来就是「… 25 字 拉黑」。
    var hd = card.querySelector(".mc-hd");
    var bd0 = card.querySelector(".mc-bd");
    if((hd && hd.querySelector("[data-blk]")) || (bd0 && bd0.querySelector("[data-blk]"))) return;
    // v3.60（用户 m17581 第 5 条）：只调工具、没有人话的卡片也不给「拉黑」——截图里
    // 「调用 search · 搜索『…』」下面挂着拉黑按钮，点下去拉黑的是工具参数，不是谁说的话。
    if(card.querySelector(".tcall-wrap")) return;
    // 只给「人说的话」加：摘要卡、tool 卡拉黑没意义
    var role = (String(card.className || "").match(/(?:^|\s)(user|assistant)(?:\s|$)/) || [])[1] || "";
    if(!role) return;
    // 用户 m12463：特别长的文不给「拉黑」——短语黑名单是给「一句话」用的，
    // 几百字的正文拉黑进去永远匹配不上，按钮只是碍事。10 字以内才给。
    var txt = blockTextOf(card, ",.qqmd-cmd");
    if(!txt) return;
    if(visLen(txt) > 10) return;
    var b = document.createElement("span");
    b.className = "mc-blk";
    b.setAttribute("data-blk", "1");
    b.setAttribute("data-txt", txt);
    b.textContent = "拉黑";
    // 用户 m12442：按钮要排在字数气泡后面（所有小标签的最后）。以前组内卡片挂到
    // 第一个 .mono 末尾，气泡在最后一段，按钮就跑到正文中间去了。
    var chip = bd0 ? bd0.querySelector(".chb.count") : null;
    if(!chip && hd) chip = hd.querySelector(".chb.count");
    if(chip && chip.parentNode){
      // 用户 m05993 第 1 条：「拉黑」要排在小标签的最后（字数、待发、计入之后），
      // 而且不悬浮时不能占位。以前把字数和按钮一起包进 nowrap 的 .mc-tags、按钮用
      // visibility 隐藏，字数后面就永远空一块。现在挂到同一行最后一个 .chb 后面。
      var row = chip.parentNode;                       // .mc-tags（字数气泡的壳）
      var scope = row.closest ? (row.closest(".mono") || row) : row;
      var last = chip;
      Array.prototype.forEach.call(scope.querySelectorAll(".chb"), function(x){
        if(x !== chip && x.parentNode === scope) last = x;
      });
      last.parentNode.insertBefore(b, last.nextSibling);
      return;
    }
    var grow = hd ? hd.querySelector(".grow") : null;
    if(hd && grow && grow.parentNode === hd){ hd.insertBefore(b, grow); return; }
    var monos = bd0 ? bd0.querySelectorAll(".mono") : null;
    var mono = monos && monos.length ? monos[monos.length - 1] : null;
    if(mono){ mono.appendChild(b); return; }
    if(bd0){ bd0.appendChild(b); return; }
    if(hd){ hd.appendChild(b); return; }
    card.appendChild(b);
  }
  function paintBlockBtns(){
    var box = document.getElementById("ctx-window");
    if(!box || !blkList) return;
    var set = {};
    (blkList.items || []).forEach(function(w){ var k = blkNorm(w); if(k) set[k] = 1; });
    Array.prototype.forEach.call(box.querySelectorAll("[data-blk]"), function(b){
      var on = !!set[blkNorm(b.getAttribute("data-txt"))];
      if(on) b.classList.add("on"); else b.classList.remove("on");
      b.textContent = on ? "已拉黑" : "拉黑";
      // 用户 m07085 新7：短语清单 v3.46 起是全局池（写 data/config/plugins/<插件>.json），
      // 拉黑对所有群生效，不是只对本群，文案不能再写成本群专属。
      b.title = on
        ? "这条已经在「不触发短语」全局黑名单里，点一下解黑（所有群一起生效）。"
        : "把这条消息整条加进「不触发短语」全局黑名单：以后任何群有人发一模一样的一句，都不算一次触发。";
    });
  }
  // 清单按会话缓存：切群才重拉，同一群重画窗口直接用缓存，不然每 5 秒一次刷新都要打接口
  function loadPhraseBlock(sid){
    if(!sid || sid === blkSid) return;
    blkSid = sid;
    blkList = null;
    get(API + "/phrase-block?sid=" + encodeURIComponent(sid)).then(function(d){
      if(!d || !d.ok || sid !== blkSid) return;
      blkList = d;
      paintBlockBtns();
    }).catch(function(){});
  }
  async function toggleBlockPhrase(btn){
    if(!ctxWin || !ctxWin.sid) return;
    var sid = ctxWin.sid, txt = btn.getAttribute("data-txt") || "";
    if(!txt){ mdAlert("这条消息没有可拉黑的文字"); return; }
    var on = btn.classList.contains("on");
    if(!on){
      var show = txt.length > 60 ? (txt.slice(0, 60) + "…") : txt;
      var yes = await mdConfirm("把这条加进全局黑名单？\n\n「" + show + "」\n\n" +
        "以后任何群有人发一模一样的一句，都不算一次触发。清单是全局的，所有群一起生效。");
      if(!yes) return;
    }
    sendBlockPhrase(sid, txt, on ? "remove" : "add");
  }
  function sendBlockPhrase(sid, txt, action){
    var add = (action === "add");
    post(API + "/phrase-block?sid=" + encodeURIComponent(sid) +
         "&text=" + encodeURIComponent(txt) + "&action=" + encodeURIComponent(action)).then(function(r){
      if(!r || !r.ok){
        mdAlert((add ? "拉黑" : "解黑") + "失败：" + ((r && r.detail) || "未知错误"));
        return;
      }
      if(sid === ctxSid){ blkSid = sid; blkList = r; paintBlockBtns(); }
      if(add){
        var msg = (r.changed === false ? "这条本来就在清单里" : "已拉黑") + " · 清单现在 " + r.count + " 条";
        if(r.enabled === false) msg += "\n\n注意：设置里「不触发短语」的开关是关的，这条先记着，打开开关才生效。";
        mdAlert(msg);
      } else {
        mdAlert((r.changed === false ? "清单里没有这条" : "已解黑") + " · 清单现在 " + r.count + " 条");
      }
    }).catch(function(){ mdAlert("请求失败，看后台日志"); });
  }

  function fadeWinIn(){
    var box = document.getElementById("ctx-window");
    if(!box) return;
    Array.prototype.forEach.call(box.children, function(el){
      if(el.classList && el.classList.contains("sec-hd")) return;
      el.classList.add("vswap-in");
    });
  }

  // 拆 user 消息开头那串元数据：
  //   [Oct 19 2026 16:31 Mon] [message_id: xxx] [user_nickname: 我, user_id: admin] | 正文
  // 拆不动的部分原样留在 body 里，绝不吞内容。
  // 有人把群头衔塞进昵称，QQ 那边编码坏了就成了「[%ĀĀ␇Ö]我在未来，等你」这种。
  // 只在拆元数据（美化/对照档）时剥掉开头这段乱码，原文档走原文，一个字都不动。
  function cleanNick(n){
    var raw = String(n == null ? "" : n);
    var t = raw.replace(/^\[[^\[\]]*(?:[\u0080-\u009f\u0100-\u017f\u2400-\u243f])[^\[\]]*\]\s*/, "");
    t = t.trim();
    return t || raw;   // 剥干净了也不吞，宁可难看
  }

  // 3.3.0 起群消息头多了 group_nickname（群名片），真实格式：
  //   [user_nickname: Alice, group_nickname: Team Lead, user_id: u1]
  // 群名片为空时整段不出现，老格式必须完全按原样解析。
  // 昵称和群名片一起显示，分隔符统一用「 · 」；群名片为空、或跟昵称一样时只显示昵称。
  function dispNameOf(nm, gn){
    var a = String(nm == null ? "" : nm).trim();
    var b = String(gn == null ? "" : gn).trim();
    if(!b || b === a) return a;
    return a ? (a + " · " + b) : b;
  }
  // 用户 m03205：昵称和群昵称要分色，光靠一个字符串分不出来——HTML 场景用这个，
  // 群昵称包一层 .gnick（强调色）；纯文本场景（title、data 属性、分组键）仍用 dispNameOf。
  function dispNameHtml(nm, gn){
    var a = String(nm == null ? "" : nm).trim();
    var b = String(gn == null ? "" : gn).trim();
    if(!b || b === a) return esc(a);
    if(!a) return "<span class='gnick'>" + esc(b) + "</span>";
    return esc(a) + "<span class='gnick'> · " + esc(b) + "</span>";
  }
  // 从一段消息头里取群名片（没有就返回空串）。老格式没有这一段，返回空。
  // 群名片里可能有逗号（后端 window/manager.py 的 _NICK_LINE_RE 也是按 ", user_id:" 锚定的），
  // 所以不能按逗号截断：先取到右括号/行尾，再把后面的 user_id 段剥掉。
  function gnickOfRaw(raw){
    var s = String(raw == null ? "" : raw);
    var m = s.match(/group_nickname:\s*([^\]\n]{1,64})/);
    if(!m) return "";
    return cleanNick(m[1].replace(/,\s*user_id:[\s\S]*$/, "").trim());
  }

  // 括号配平：从 s[pos]（必须指向 [）找到配对的 ]，任意层嵌套都认。
  // 转发消息是 [Reply ID: x content: [Forward [时间] 人: [Video name: …]]]，套了三层，
  // 原来那个只认一层的正则整块匹配不上，全漏进正文了（用户 m33994 图1）。
  function bracketEnd(s, pos){
    var d = 0;
    for(var i = pos; i < s.length; i++){
      var ch = s.charAt(i);
      if(ch === "[") d++;
      else if(ch === "]"){ d--; if(d === 0) return i; }
    }
    return -1;
  }

  // 引用的原话里分享卡会丢壳：框架把它写进 Reply 时变成
  //   [Reply ID: x content: [时间] 某人: {"app":…,"title":…,"jumpUrl":…}]
  // [Json card 前缀和结尾那个 ] 都被吃掉，剩一坨裸 JSON 铺在正文里，
  // richText 又从 JSON 里的 b23.tv 认出站点、就地插一张卡把 JSON 劈成两半
  // （用户 m13768：卡片内的东西飞外面了，卡片里没内容）。
  // 从开头配平一对花括号，字符串里的 { } 与转义不算深度。
  function bareJsonEnd(s){
    if(s.charAt(0) !== "{") return -1;
    var d = 0, inStr = false, esc0 = false;
    for(var i = 0; i < s.length; i++){
      var ch = s.charAt(i);
      if(inStr){
        if(esc0){ esc0 = false; continue; }
        if(ch === "\\"){ esc0 = true; continue; }
        if(ch === '"') inStr = false;
        continue;
      }
      if(ch === '"'){ inStr = true; continue; }
      if(ch === "{") d++;
      else if(ch === "}"){ d--; if(d === 0) return i; }
    }
    return -1;
  }
  // 裸 JSON 值不值得当分享卡：卡片类字段至少有一个；纯小对象别乱认
  function looksLikeCard(o){
    if(!o || typeof o !== "object") return false;
    if(Object.prototype.toString.call(o) === "[object Array]") return false;
    var keys = ["jumpUrl", "qqdocurl", "url", "app", "title", "prompt", "desc"];
    for(var i = 0; i < keys.length; i++){ if(o[keys[i]]) return true; }
    return false;
  }

  function splitMeta(t){
    var rest = String(t || ""), m = { ts: "", mid: "", name: "", gnick: "", disp: "", uid: "", at: [] };
    // 括号里可能还套一层括号（Reply ID 的 content 自己带 [时间]），
    // 所以允许一层嵌套，否则 `[Reply ID: x content: [..] 某人: 话]` 会被切半
    var re = /^\[((?:[^\[\]]|\[[^\[\]]*\]){1,600})\]\s*/;
    for(var i = 0; i < 12; i++){
      // 「[昵称] | 正文」那条竖线每轮都清一下，不然后面的 [At …] 剥不到
      rest = rest.replace(/^\s*\|\s*/, "");
      // Reply 先按括号配平单独切：里面可能套着 Forward 和 [Video name: …]（用户 m33994 图1）
      if(rest.indexOf("[Reply ID:") === 0){
        var e2 = bracketEnd(rest, 0);
        if(e2 > 0){
          var rm0 = rest.slice(1, e2).match(/^Reply ID:\s*([\s\S]*?)\s+content:\s*([\s\S]*)$/);
          if(rm0){ m.reply = { id: rm0[1].trim(), text: rm0[2].trim() }; rest = rest.slice(e2 + 1); continue; }
          // v4.19：只有 id 没有原话（框架实测「[Reply 1050111951]」，用户 m21126 图1）
          var ro0 = rest.slice(1, e2).match(/^Reply ID:\s*(-?\d+)\s*$/);
          if(ro0){ m.reply = { id: ro0[1].trim(), text: "" }; rest = rest.slice(e2 + 1); continue; }
        }
      }
      // 正文本身是一整块合并转发的聊天记录（不是引用里那种）：
      // 收进 meta.fwd 单独画卡片，别把「[Forward [时间] 人: 话」这串中括号铺在正文里
      if(rest.indexOf("[Forward") === 0){
        var ef = bracketEnd(rest, 0);
        if(ef > 0){ m.fwd = rest.slice(0, ef + 1); rest = rest.slice(ef + 1); continue; }
      }
      // 引用原话里的分享卡只剩裸 JSON（见 bareJsonEnd 注释）：配平出一整块，
      // 解析成功、又像张卡，就收进 meta.card 当卡片画，别让 JSON 铺在正文里
      if(rest.charAt(0) === "{"){
        var bj = bareJsonEnd(rest);
        if(bj > 0){
          var obj = null;
          try { obj = JSON.parse(rest.slice(0, bj + 1)); } catch(e0){ obj = null; }
          if(looksLikeCard(obj)){ m.card = obj; rest = rest.slice(bj + 1).replace(/^\s+/, ""); continue; }
        }
      }
      var x = rest.match(re);
      if(!x) break;
      var inner = x[1];
      // 四种时间戳都认：框架原样 [Oct 19 2026 23:09 Mon]、v2.26 [10-19 23:09]、v2.27 [23:09]，
      // 以及引用原话里的 ISO [2026-10-23 21:01:00]（用户第 2 条：不能让它当正文露出来）
      if(/^(?:[A-Z][a-z]{2} \d{1,2} \d{4} \d{1,2}:\d{2}|\d{4}-\d{1,2}-\d{1,2}[ T]\d{1,2}:\d{2}|\d{1,2}-\d{1,2} \d{1,2}:\d{2}|\d{1,2}:\d{2})/.test(inner)){
        m.ts = inner; rest = rest.slice(x[0].length); continue;
      }
      if(inner.indexOf("message_id:") === 0){
        m.mid = inner.slice(11).trim(); rest = rest.slice(x[0].length); continue;
      }
      if(inner.indexOf("user_nickname:") >= 0){
        var nm0, gn0 = "";
        // 3.3.0 群消息头是三段：user_nickname / group_nickname / user_id。
        // 群名片为空时 group_nickname 整段不出现，老格式走第二条，行为跟以前完全一样。
        var g = inner.match(/user_nickname:\s*([\s\S]*?),\s*group_nickname:\s*([\s\S]*?),\s*user_id:\s*([\s\S]*)$/);
        if(g){
          nm0 = cleanNick(g[1].trim()); gn0 = cleanNick(g[2].trim()); m.uid = g[3].trim();
        } else {
          var g1 = inner.match(/user_nickname:\s*([\s\S]*?),\s*user_id:\s*([\s\S]*)$/);
          if(g1){ nm0 = cleanNick(g1[1].trim()); m.uid = g1[2].trim(); }
          else {
            // 连 user_id 都没有：整段当昵称，但别把后面的群名片一起吞进名字
            var rest0 = inner.slice(inner.indexOf("user_nickname:") + 14);
            var gn1 = rest0.match(/,\s*group_nickname:\s*([\s\S]*)$/);
            if(gn1){ gn0 = cleanNick(gn1[1].trim()); rest0 = rest0.slice(0, gn1.index); }
            nm0 = cleanNick(rest0.trim());
          }
        }
        // 用户 m04917 第 1 条：有人把群头衔/装饰混进昵称，落下来就是
        // 「[%ĀĀ␇Ö] [@你]我在未来，等你」——那个 [@你] 是昵称里的字，不是插件标记。
        // 留在名字里既不高亮、位置也不在名字后面；收进头部徽章 + 从名字里剥掉。
        var ny = nm0.match(/\[\s*@你\s*\]/);
        if(ny){
          m.you = true;
          nm0 = nm0.replace(/\[\s*@你\s*\]\s*/g, "").trim();
        }
        m.name = nm0;
        m.gnick = gn0;
        m.disp = dispNameOf(nm0, gn0);
        // @ 补全池里只放纯昵称：群名片是「在这个群里叫什么」，不是这个人的名字
        atPoolAdd(nm0);
        rest = rest.slice(x[0].length); continue;
      }
      if(inner.indexOf("group_name:") === 0 || inner.indexOf("group_id:") === 0){
        rest = rest.slice(x[0].length); continue;
      }
      // [@你]：插件打在这条消息上的「在叫机器人」标记，收进头部徽章
      if(inner === "@你"){ m.you = true; rest = rest.slice(x[0].length); continue; }
      // [At 895947462(nickname: JM卟)] -> @JM卟，比一串数字好认
      var am = inner.match(/^At\s+\d+\(\s*nickname:\s*([\s\S]*?)\s*\)$/);
      if(am){ m.at.push(cleanNick(am[1])); rest = rest.slice(x[0].length); continue; }
      // [Reply ID: 991678623 content: [2026-10-19 00:12:36] 某人: 原话]
      // 引用关系有用，但塞在正文里就是一大坨中括号，单独拎出来画成引用块
      var rm = inner.match(/^Reply ID:\s*([\s\S]*?)\s+content:\s*([\s\S]*)$/);
      if(rm){ m.reply = { id: rm[1].trim(), text: rm[2].trim() }; rest = rest.slice(x[0].length); continue; }
      // v4.19（用户 m21126）：框架只给 id 的两种写法都收成引用，别让方括号露在正文里
      var ro = inner.match(/^Reply ID:\s*(-?\d+)\s*$/);
      if(ro){ m.reply = { id: ro[1].trim(), text: "" }; rest = rest.slice(x[0].length); continue; }
      var rr = inner.match(/^Reply\s+(-?\d+)$/);
      if(rr){ m.reply = { id: rr[1].trim(), text: "" }; rest = rest.slice(x[0].length); continue; }
      // [Json card {"app":"com.tencent.miniapp_01","title":"哔哩哔哩","desc":"…","qqdocurl":"…"}]
      // 整段 JSON 塞正文里又长又难看，拆成一张小卡片
      var jc = inner.match(/^Json card\s*(\{[\s\S]*\})$/);
      if(jc){
        try { m.card = JSON.parse(jc[1]); } catch(e){ m.card = { title: "小程序", desc: jc[1] }; }
        rest = rest.slice(x[0].length); continue;
      }
      // [File name: 洛茜-图集.html, file_path: data/temp\…] 用户发来的文件，
      // 留在正文里就是一串中括号，收进头部画成文件小卡（用户 m28965 图1）
      var fm = inner.match(/^File name:\s*([\s\S]*?)(?:,\s*file_path:\s*([\s\S]*))?$/);
      if(fm){
        m.file = { name: (fm[1] || "").trim(), path: (fm[2] || "").trim() };
        rest = rest.slice(x[0].length); continue;
      }
      break;
    }
    rest = rest.replace(/^\s*\|\s*/, "").replace(/^\s+/, "");
    // 用户第 1 条（艾特没美化）：这里以前把 [At 1(nickname: 白色(^0^)/季节)] 拼成纯文本
    // "@白色(^0^)/季节"，而 richText 的 @昵称 正则只吃 [\u4e00-\u9fa5A-Za-z0-9_-]，
    // 碰到 "(" 就断，页面上只剩 "@白色"。改成把 At token 原样塞回去，
    // 交给 richText / atHtml 那条能认括号昵称的正则统一美化（id 用 0 占位，只取昵称）。
    if(m.at.length) rest = m.at.map(function(a){ return "[At 0(nickname: " + a + ")]"; }).join(" ") + " " + rest;
    return { meta: m, body: rest };
  }

  // 用户第 1 条 + 第 12 条：QQ 的 @ 在原文里有两种脏写法，统一在这里洗成框架的 At token，
  // 后面交给 richText / atHtml 那条「能认括号昵称」的正则美化：
  //   ① [@（不）怕水的小二黑](mqqapi://markdown/mention?at_type=1&at_tinyid=1575945754)
  //      —— QQ markdown 消息里的 mention（机器人 / 小程序分享都这么发），
  //      昵称自带括号（白色(^0^)/季节）时只有 At 那条正则整段认得出。
  //   ② "@Enderman[@（不）怕水的小二黑](mqqapi://…)" —— 前面那截 "@Enderman" 是 QQ 把 at
  //      元素文本化出来的冗余前缀（Enderman 是昵称、（不）怕水的小二黑 是群名片，同一个人），
  //      不剥就会渲染成两个 @（用户第 12 条 图7）。
  // 正则里必须带 "[At 0(nickname: " 才命中，手打的「@某人 你好」不会被误伤。
  function mentionNorm(t){
    return String(t == null ? "" : t)
      .replace(/\[@([^\]\n]{1,40})\]\(mqqapi:\/\/[^)\s]*\)/g, "[At 0(nickname: $1)]")
      .replace(/@[^\s\[\]]{1,40}\[At 0\(nickname: /g, "[At 0(nickname: ");
  }

  // 一条 user 消息里常常拼了好几条群消息（换行分隔，每段自己带 [时间] [id] [昵称]）。
  // 逐段拆开，不然中段的元数据全糊在正文里，美化档看着就是一堆中括号。
  // 原始消息里带的是字面量 "\n" 和英文媒体标记，直接展示会很难看
  function tidyBody(t){
    return mentionNorm(String(t == null ? "" : t))
      .replace(/\\n/g, "\n")
      .replace(/\[Image attached\]/gi, "[图片]")
      // 用户 m27528 图2/图3：框架在 Reply 原话、转发条目里写的是短占位 [Image]（没有 attached），
      // 上面那几行都认不出来，引用块里就只剩一行英文方括号。
      // 用户 m08286：引用块里的图片占位符现在带序号（[Image 1]），这里一并认。
      .replace(/\[Image(?:\s*\d+)?\]/g, "[图片]")
      .replace(/\[Sticker\]/g, "[表情]")
      .replace(/\[Sticker attached\]/gi, "[表情]")
      .replace(/\[Voice attached\]/gi, "[语音]")
      // Video tokens retain their identity until the code-aware card renderer.
      .replace(/\[File attached\]/gi, "[文件]")
      .replace(/\[kira_image_ref\]/g, "[图片]")
      .replace(/\[input_image\]/g, "[图片]")
      // QQ 的 markdown 消息（机器人 / 小程序分享）在群里长这样：
      //   [@某人](mqqapi://markdown/mention?at_type=1&at_tinyid=123) 正文 ![img #20px #40px](https://…)
      // 不洗掉的话美化档里就是一串 markdown 链接，比原文还难读。
      // mention 那半截已经挪到 mentionNorm（用户第 1 / 12 条），这里只管图片标记
      .replace(/!\[img[^\]]*\]\([^)\s]*\)/gi, "[图片]")
      // 用户第 2 条：框架存下来的长文本会在半路截断（真实数据里就有结尾是
      // "![img #208px #409px](http" 的），右括号没了上面那条就匹配不到，
      // 会把 markdown 图片语法原样铺在正文 / 引用里，这里兜一下行尾的半截
      .replace(/!\[img[^\]]*\]\([^)\s]*$/i, "[图片]")
      .replace(/\[([^\]\n]{1,30})\]\((?:https?:\/\/|mqqapi:\/\/)[^)\s]*\)/g, "$1");
  }

  // 字数口径：表情 / 艾特 / 图片这些标记不算字。
  // 以前直接 length，一条 7 个字的回复带着 [Emoji 21] 就显示 17 字（用户 m33778 图2）
  function visLen(t){
    var s = String(t == null ? "" : t);
    // 用户 m12748：@ 昵称在页面上是一个徽章，不是他说的话。昵称里可能带空格
    // （`@Angels of the Apocalypse 上号啊`），下面「最多两个英文词」的正则只吃掉
    // 前两个词，剩下的「the Apocalypse」被算成字，待发卡就多算了 2 个字。
    // 窗口里见过的昵称在 AT_POOL 里（lib-1.js），长的先吃，整段剔掉。
    if(typeof AT_POOL !== "undefined" && AT_POOL.length && s.indexOf("@") >= 0){
      AT_POOL.slice().sort(function(a, b){ return b.length - a.length; }).forEach(function(nm){
        if(nm) s = s.split("@" + nm).join(" ");
      });
    }
    s = s.replace(/\[Emoji\s+[^\]]*\]/g, "")
         .replace(/\[Emoji\]/g, "")
         .replace(/\[At\s+[^\]]*\]/g, "")
         .replace(/\[(?:Image|Sticker|Voice|Video|File) attached\]/gi, "")
         .replace(/\[kira_image_ref\]/g, "")
         .replace(/\[图片\]|\[表情\]|\[语音\]|\[视频\]|\[文件\]/g, "");
    // 引用与框架元数据可以嵌套，不用扁平正则误算引用正文。
    var out = "";
    for(var i = 0; i < s.length; i++){
      if(s.charAt(i) === "["){
        var end = bracketEnd(s, i), token = end >= 0 ? s.slice(i + 1, end) : "";
        if(end >= 0 && /^(?:Reply ID:|Forward\b|message_id:|user_nickname:|group_nickname:|user_id:|group_name:|group_id:|Json card\b|File name:|@你$|\d{1,2}:\d{2}|\d{1,2}-\d{1,2} \d{1,2}:\d{2}|[A-Z][a-z]{2} \d{1,2} \d{4} \d{1,2}:\d{2})/.test(token)){
          i = end; continue;
        }
      }
      out += s.charAt(i);
    }
    return out.replace(sharePattern(), "")
      .replace(/点击链接直接打开[。.!！]?/g, "")
      // @昵称是元数据，不是他说的话（用户 m51324 图6：「@Mcp_management service …」多算 15 字）
      // 用户 m05861：昵称里可能带空格，先整段吃掉「@ + 昵称」，后面再跟最多两个英文词；
      // 最多吃两个词，再多就可能把真话一起吞掉。
      // 用户 m17972：昵称以数字/字母开头、后面接着中文时（`@10月绝不摆烂`），原来
      // 「@ + 最多两个英文词」那条先命中，只吃掉 `@10`，剩下的「月绝不摆烂」被算成字。
      // 现在中文和英文并成一条，跟徽章渲染（lib-1 的 richText）认的整段一致。
      // 用户本轮：可选组会把「@某人 https://…」里的「 https」吃掉，剩下「://…」不再被
      // 后面的 URL 正则认出来，字数从 10 变 33，拉黑按钮就不挂了。URL 协议头/域名前面
      // 不许再吃空格；i 只影响 https/www 这两个字面量，别的行为不变。
      // 用户 m17388：群名片里带 ʚ 〜 ♡ ɞ 这类符号时（`@ʚ不安死✟ふぁんし〜♡ɞ 打不打大坝`），
      // 上面这条的字符集只吃汉字/字母/数字/_-，整段昵称没剥掉，待发卡把 5 个字算成 18 字。
      // 字符集与长度上限改成跟徽章渲染（lib-1 的 richText）完全一致：除空白、方括号、@、尖括号
      // 和常见中英文标点之外都算昵称字符，最多 24 个。
      .replace(/(^|[\s。，,、；;：:])@[^\s\[\]@<>，。、；;：:！!？?（）()「」“”"'…]{1,24}(?:[ \t]+(?!https?:\/\/|www\.)[A-Za-z0-9_\-]{1,32})?/gi, "$1")
      // 用户 m22683：QQ 的 @ 昵称是「@[编码]昵称」两截（`@[%ĀĀ␇Ö]我在未来，等你`），
      // 上面两条都从 `[` 就断了，昵称整段留在正文里，一句话被算成十几个字。
      .replace(/(^|[\s。，,、；;：:])@\[[^\]]{0,64}\][^\s]{0,32}/g, "$1")
      .replace(/(?:https?:\/\/|www\.)[^\s<>"「」]+/gi, "")
      .replace(/<\/?[A-Za-z_][^>]*>/g, "")
      .replace(/\s/g, "").length;
  }

  // 表情包在 QQ 侧只有个名字（[叉腰]），图已经单独显示了，正文里再挂一遍就是废话
  // （用户 m33778 图1）。这条消息带图时才剥，纯文字里的方括号不动。
  // 用户 m11091 第 2 条：老写法「带图就剥掉所有 1-8 字方括号词」会把正文里真正的
  // [重点]、[注意] 一起吃掉，看着就像消息缺字。现在先看正文还有没有真文字：
  // 有就一个字都不剥；只有整条除了方括号名什么都不剩（纯表情名）才清掉，
  // 而且我们自己的占位符（[图片]/[表情]/…）留着，图还得能点开。
  var BODY_PH_RE = /^\[(图片|表情|语音|视频|文件)\]$/;
  // v3.62（用户 m18059 第 2 条）：这一段的图已经画成缩略图/「图片已清理」占位了，正文里
  // 对应的 [图片] / [表情] 还会再来一行，看着像同一条消息发了两次。按这段实际画出的媒体数
  // 认领占位：画了几张就吃掉几个占位，多出来的留着（那张图确实没下落）。
  function stripMediaTags(t, n){
    var s = String(t == null ? "" : t), left = Math.max(0, Number(n) || 0);
    if(!left) return s;
    s = s.replace(/\[(?:Image attached|Sticker(?: attached|:[^\]\r\n]*)?|图片|表情|image)\]/gi, function(m){
      if(left <= 0) return m;
      left -= 1;
      return "";
    });
    return s.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").replace(/^\s+|\s+$/g, "");
  }
  // 用户 m21787 图3/图4：记录里的正文带着模型侧派生图的那句说明（「（动图 · 共 34 帧，上面是
  // 等距抽的 9 帧拼图…）」）。美化档既然换回了聊天里原本那张图，这句说明就得一起去掉，
  // 不然图是动图、字还写着「上面是九宫格」。对照 / 原文档保留原样。
  function stripDeriveNote(t){
    return String(t == null ? "" : t)
      .replace(/（动图 · 共 \d+ 帧，[^）]*）/g, "")
      .replace(/（这张是动图，按设置没有发给模型）/g, "")
      .replace(/（另有 \d+ 张动图按设置没有发给模型）/g, "")
      .replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").replace(/^\s+|\s+$/g, "");
  }
  function mediaOnlyBody(t){
    return /^(?:\s*\[(?:Image attached|Sticker(?::[^\]\r\n]*)?|image|图片|表情|语音|视频|文件)\]\s*)+$/i.test(String(t || ""));
  }
  function tidyBody2(t, hasMedia){
    var s = tidyBody(t);
    if(!hasMedia) return s;
    // 用户 m14836 图4：表情包名字里带数字（[甜花13]）时旧正则认不出来，正文里就多挂一段
    // 「[甜花13] 6 字」。名字允许数字、下划线、连字符，长度仍限 8 字以内。
    var names = /\[([一-龥A-Za-z0-9_\-]{1,8})\]/g;
    var prose = s.replace(names, "").replace(/\s/g, "");
    if(prose) return s;
    return s.replace(names, function(m){ return BODY_PH_RE.test(m) ? m : ""; });
  }

  function splitMetaAll(t){
    // 真实记录里偶尔带 CRLF（宿主透传的文本），先归一再分行，否则行尾的 \r 会让
    // forwardHtml 的「空行分条」匹配不到，整张卡糊成一条。
    var lines = String(t || "").replace(/\r\n?/g, "\n").split("\n"), blocks = [], cur = [];
    // 用户第 2 条：引用原话里可能拼着好几条，每段自带 [2026-10-23 21:01:00]（ISO）这种头，
    // 不认它就整段铺在一起，第二条的时间/昵称全糊进正文
    var head = /^\s*\[(?:[A-Z][a-z]{2} \d{1,2} \d{4} \d{1,2}:\d{2}|\d{4}-\d{1,2}-\d{1,2}[ T]\d{1,2}:\d{2}|\d{1,2}-\d{1,2} \d{1,2}:\d{2}|\d{1,2}:\d{2}|message_id:|user_nickname:|group_nickname:)/;
    // 反过来也要防：一整段 [Forward …] 内部每条本身就以 [ISO 时间] 打头，
    // 按 head 切就会把一张转发卡腰斩成两条普通气泡——正文露出「[Forward [时间] 人:」、
    // 第二条只剩「人: 链接]」尾巴多一个 ]、字数还各算一份（用户截图：一条合并转发拆成 33 字 + 5 字）。
    // 所以从「[Forward」起点数中括号，配平之前不切块；残缺数据（始终不闭合）设行数护栏，
    // 免得一条坏转发把后面的消息全吞进同一块。
    function fwdBodyOpen(text){
      var from = text.lastIndexOf("[Forward");
      if(from < 0) return false;
      var d = 0;
      for(var i = from; i < text.length; i++){
        var ch = text.charAt(i);
        if(ch === "[") d++;
        else if(ch === "]"){ d--; if(d <= 0) return false; }
      }
      return d > 0;
    }
    var swallowed = 0;
    lines.forEach(function(ln){
      if(cur.length && head.test(ln)){
        if(fwdBodyOpen(cur.join("\n")) && swallowed < 200){ swallowed++; cur.push(ln); return; }
        blocks.push(cur.join("\n")); cur = []; swallowed = 0;
      }
      cur.push(ln);
    });
    if(cur.length) blocks.push(cur.join("\n"));
    var out = [];
    blocks.forEach(function(x){ if(x.trim()) out.push(splitMeta(x)); });
    return out;
  }

  // 多模态 content 是数组（[{type:'text'},{type:'image_url'},…]），
  // 直接 JSON.stringify 出来就是一坨字典，模型看到的是图，人看到的应该是「[图片]」
  function contentText(c){
    if(c === null || c === undefined) return "";
    if(typeof c === "string") return c;
    if(Array.isArray(c)){
      var out = [], seenImg = 0, flat = "";
      // v4.48（用户 m25438）：原文里本来就带着 [图片]/[表情] 占位时，别再给图片 part 补一个。
      // 以前每张图都无条件补一个「[图片]」，于是「[表情] + [图片]」两行占位只对应一张图；
      // 美化档按「占位数 == 图片数」认领，认不上就把 [图片] 原样留在气泡里（图下面多一行字）。
      // v4.90（用户 m02939 图5）：[Sticker attached] 也是图片占位，以前没算进去，
      // 于是「一张图 + 一个表情」的消息会多补一行 [图片]（20:07 那条转发就是这样）。
      var phRe = /\[(?:Image attached|Sticker(?: attached|:[^\]\r\n]*)?|图片|表情|image)\]/gi;
      c.forEach(function(it){
        if(!it || typeof it !== "object"){ flat += "\n" + String(it); out.push(String(it)); return; }
        var tp = it.type || "";
        if(tp === "text"){ flat += "\n" + (it.text || ""); out.push(it.text || ""); }
        else if(tp === "image_url" || tp === "image" || tp === "kira_image_ref"){
          seenImg += 1;
          var phN = (flat.match(phRe) || []).length;
          if(phN < seenImg){ flat += "\n[图片]"; out.push("[图片]"); }
        }
        else if(tp === "input_audio" || tp === "audio"){ flat += "\n[语音]"; out.push("[语音]"); }
        else { flat += "\n[" + (tp || "附件") + "]"; out.push("[" + (tp || "附件") + "]"); }
      });
      // v4.90（用户 m02939 图5）：老快照里「（这条转发还有 N 张图没发…）」被追加过两遍
      // （回复链上 prepare_messages 被调了两次）。后端已改成幂等，但历史记录还是两份，
      // 显示时把连续重复的那句收成一句——不动落盘数据，只影响页面。
      var _hintRe = /^（(?:这条转发还有 \d+ 张图没发(?:，需要看就调用 view_forward)?|另有 \d+ 张动图按设置没有发给模型|引用的那条已经不在窗口里(?:，需要看就调用 view_quote)?|引用的那条是转发(?:，需要看就调用 view_forward)?)）$/;
      var _uniq = [];
      out.forEach(function(x){
        var t = String(x == null ? "" : x).trim();
        if(_hintRe.test(t) && _uniq.length && String(_uniq[_uniq.length - 1] == null ? "" : _uniq[_uniq.length - 1]).trim() === t) return;
        _uniq.push(x);
      });
      return _uniq.join("\n");
    }

    if(typeof c === "object") return c.text ? String(c.text) : JSON.stringify(c);
    return String(c);
  }

  // 拆 assistant 的 <msg message_id="..."><text>正文</text><emoji>182</emoji></msg>
  function parseAssistant(t){
    var src = String(t || ""), out = [], re = /<msg\b[^>]*>([\s\S]*?)<\/msg>/gi, mm;
    while((mm = re.exec(src))){
      var seg = mm[1], texts = [], emojis = [];
      seg.replace(/<text>([\s\S]*?)<\/text>/gi, function(_, x){ texts.push(x); return ""; });
      seg.replace(/<emoji>([\s\S]*?)<\/emoji>/gi, function(_, x){ emojis.push(x); return ""; });
      out.push({ text: texts.join("\n").trim(), emojis: emojis });
    }
    if(!out.length){
      var plain = src.replace(/<\/?[A-Za-z_][^>]{0,300}>/g, " ").replace(/[ \t]+/g, " ").trim();
      if(plain) out.push({ text: plain, emojis: [] });
    }
    return out;
  }

  // ---------------------------------------------------------------- 美化小工具
  // 时间戳压缩：Oct 19 2026 16:31 Mon -> 10-19 16:31（悬停看原样）
  // 今天的只留 HH:MM，跨天的才带月日 —— 历史里的时间戳（后端 shrink 成 [21:08]）
  // 和待发卡片里的原文（[10-20 21:18]）必须一个长相，否则同一列表里两种格式。
  function pad2(n){ n = String(n); return n.length < 2 ? "0" + n : n; }
  // 时间拆成「日期 / 时间」两行：私聊要看到哪天（用户 m31639）。
  // 历史消息的 meta.ts 只有 [HH:MM]（v2.27 起压掉了日期），日期就空着占位。
  function tsParts(s){
    var t = String(s === undefined || s === null ? "" : s).trim().replace(/^\[/, "").replace(/\]$/, "").trim();
    if(!t) return { d: "", t: "" };
    var m = t.match(/^(\d{1,2})-(\d{1,2})\s+(\d{1,2}:\d{2})/);
    if(m) return { d: pad2(m[1]) + "-" + pad2(m[2]), t: m[3] };
    var m1 = t.match(/^(\d{1,2}:\d{2})$/);
    if(m1) return { d: "", t: m1[1] };
    var one = shortTs(t);
    var m2 = one.match(/^(\d{1,2}-\d{1,2})\s+(\d{1,2}:\d{2})$/);
    if(m2) return { d: pad2(m2[1].split("-")[0]) + "-" + pad2(m2[1].split("-")[1]), t: m2[2] };
    var m3 = one.match(/(\d{1,2}:\d{2})/);
    return { d: "", t: m3 ? m3[1] : one };
  }
  function tsBlock(s, reserve){
    var p = tsParts(s);
    if(!p.t && !reserve) return "";
    // 日期为空就别渲染空行：左列是 22px 定高居中，空行会把时间挤到下半截（v2.85）
    var d = p.d ? "<span class='d'>" + esc(p.d) + "</span>" : "";
    return "<span class='mts' title='" + esc(String(s || "")) + "'>" + d +
           "<span class='t'>" + esc(p.t || "") + "</span></span>";
  }
  function shortTs(s){
    var t = String(s || "").trim();
    var now = new Date(), td = now.getMonth() + 1, dd = now.getDate();
    function pick(mo, day, hm){
      if(mo === td && day === dd) return hm;
      return (mo < 10 ? "0" + mo : mo) + "-" + (day < 10 ? "0" + day : day) + " " + hm;
    }
    var m3 = t.match(/^(\d{1,2})-(\d{1,2})\s+(\d{1,2}:\d{2})$/);
    if(m3) return pick(+m3[1], +m3[2], m3[3]);
    if(/^\d{1,2}:\d{2}$/.test(t)) return t;
    // 用户第 2 条：引用块里嵌套的时间是后端的 ISO 长相 [2026-10-23 21:30:56]，
    // 不认它的话引用头会整串铺出 "2026-10-23 21:30:56"，跟历史卡的 "21:30" 不是一种长相。
    var m4 = t.match(/^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}:\d{2})/);
    if(m4) return pick(+m4[2], +m4[3], m4[4]);
    var m = t.match(/^([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{4})\s+(\d{1,2}:\d{2})/);
    if(!m) return s;
    var mo = {Jan:1,Feb:2,Mar:3,Apr:4,May:5,Jun:6,Jul:7,Aug:8,Sep:9,Oct:10,Nov:11,Dec:12}[m[1]] || 0;
    return pick(mo, +m[2], m[4]);
  }
  // 长 id 截短：call_00_qD6Z3ilaxQ76vffRyLvp3469 -> call_00_qD6Z…
  // ---- 工具调用说人话（v2.87，用户 m32401）----
  // 模型调工具那一步原本只有一句「（这一步只调用了 N 个工具，没有文本输出）」，
  // 结果卡又是一整坨原始 JSON。这里把「调了什么工具、带了什么参数」翻成一句中文，
  // 搜索类的结果再拆成一条条看得懂的条目。
  var TOOL_VERB = {
    search: "搜索", web_search: "搜索", google_search: "搜索", bing_search: "搜索",
    sogou_search: "搜索", tavily_search: "搜索", brave_search: "搜索", duckduckgo: "搜索",
    fetch: "抓取网页", fetch_url: "抓取网页", read_url: "抓取网页", web_fetch: "抓取网页",
    browse: "打开网页", crawl: "抓取网页",
    memory_recall: "回忆", recall: "回忆", search_memory: "回忆", memory_search: "回忆",
    remember: "记住", memory_add: "记住",
    read_file: "读文件", read: "读文件", write_file: "写文件", write: "写文件",
    list_dir: "列目录", ls: "列目录", glob: "找文件", grep: "搜代码",
    bash: "执行命令", shell: "执行命令", exec: "执行命令", run_command: "执行命令",
    generate_image: "生成图片", image_gen: "生成图片", edit_image: "改图",
    send_message: "发消息", schedule_create: "建提醒"
  };
  // 参数里挑一个最能说明「这一步干了什么」的值
  var TOOL_ARG_KEYS = ["query", "q", "keyword", "keywords", "prompt", "text", "url",
                       "path", "file_path", "command", "cmd", "name", "title", "content"];
  function toolArgsOf(call){
    var a = call && call.args;
    if(typeof a === "string"){
      try { a = JSON.parse(a); } catch(e){ return null; }
    }
    return (a && typeof a === "object" && !Array.isArray(a)) ? a : null;
  }
  // 一条调用的形状：search 搜索「赛博朋克 边缘行者 第二季 2026」
  function toolCallLine(call){
    var name = String((call && call.name) || "").trim();
    var verb = TOOL_VERB[name] || "";
    var a = toolArgsOf(call), arg = "";
    if(a){
      for(var i = 0; i < TOOL_ARG_KEYS.length; i++){
        var k = TOOL_ARG_KEYS[i];
        if(a[k] != null && String(a[k]).trim()){ arg = String(a[k]).trim(); break; }
      }
      if(!arg){
        var ks = Object.keys(a);
        for(var j = 0; j < ks.length; j++){
          var v = a[ks[j]];
          if(typeof v === "string" || typeof v === "number"){ arg = String(v); break; }
        }
      }
    }
    if(arg.length > 80) arg = arg.slice(0, 80) + "…";
    return "<span class='tcall'>" +
           "<span class='tverb'>调用</span>" +
           "<span class='tname'>" + esc(name || "工具") + "</span>" +
           (verb ? "<span class='tverb'>· " + esc(verb) + "</span>" : "") +
           (arg ? "<span class='targ'>「" + esc(arg) + "」</span>" : "") +
           "</span>";
  }
  function toolCallList(tc){
    if(!tc || !tc.length) return "";
    var h = "", multi = tc.length > 1;
    // v3.58（用户 m16875 图2）：一次调好几个工具时给每条编号，几行也数得清。
    for(var i = 0; i < tc.length; i++){
      h += "<div class='tcall-row'>" + (multi ? "<span class='tcall-i'>" + (i + 1) + "</span>" : "") +
           toolCallLine(tc[i]) + "</div>";
    }
    return h;
  }
  // 请求页拿到的是原始 messages，tool_calls 还是 OpenAI 那层壳，洗成 {id,name,args}
  function tcOf(m){
    if(m && m.tc && m.tc.length) return m.tc;
    var raw = (m && m.tool_calls) || [];
    var out = [];
    for(var i = 0; i < raw.length; i++){
      var c = raw[i] || {}, fn = c.function || {};
      out.push({ id: String(c.id || ""), name: String(fn.name || ""), args: fn.arguments });
    }
    return out;
  }
  // 搜索类工具的返回是「一串结果对象」：一条条列出来，比铺 47 行 JSON 好读。
  // 认不出结构的（普通对象/数组）还是走 JSON 着色，不硬猜。
  // 搜索结果的 content 常常是整页正文，开头全是「发布 / 消息 / 设置」这类导航词。
  // 按行切，跳过短行和重复标题的行，取第一段真正文（用户 m32401 图1）。
  function snipOf(x, title){
    var raw = String(x.content || x.snippet || x.description || x.summary || x.text || "");
    var lines = raw.split(/\r?\n/).map(function(s){ return s.trim(); })
                   .filter(function(s){ return s.length >= 12; });
    // 网页正文常把导航条/相关推荐一起抓下来：一行堆着好几个游戏名+日期，或者
    // 「17173首页 游戏资讯 全球新闻」这种菜单（用户 m32401 图1）。当摘要没信息量。
    var NAV = /首页|登录|注册|下载|客户端|更多|广告|版权|免责|关于我们|联系我们|网站地图|版权所有|游戏资讯|新闻中心|排行榜|专题|APP|app logo|logo|返回顶部|扫码|关注我们/;
    var score = function(s){
      var v = Math.min(s.length, 120);
      if(/[。，、；：]/.test(s)) v += 20;                       // 像句子
      if((s.match(/20\d\d年\d{1,2}月/g) || []).length >= 2) v -= 60;
      if((s.match(/\s(act|rpg|fps|avg|slg|sim)\b/gi) || []).length >= 2) v -= 40;
      if((s.match(/\|/g) || []).length >= 4) v -= 40;
      if(NAV.test(s)) v -= 70;
      if(title && s.indexOf(title) >= 0) v -= 50;
      return v;
    };
    var pick = "", best = -1e9;
    for(var i = 0; i < lines.length; i++){
      var s = lines[i].replace(/^#+\s*/, "");
      if(!s) continue;
      var v = score(s);
      if(v > best){ best = v; pick = s; }
    }
    if(!pick || best < 20) pick = raw;
    pick = pick.replace(/\s+/g, " ").trim();
    // 正文第一段常常一口气几百字，只留第一句，剩下的没信息量
    var one = pick.match(/^[\s\S]{10,140}?[。！？!?]/);
    if(one) pick = one[0];
    if(pick.length > 160) pick = pick.slice(0, 160) + "…";
    return pick;
  }
  function toolResultHtml(tj){
    if(!tj || !tj.ok || !Array.isArray(tj.v) || !tj.v.length) return "";
    var arr = tj.v, hit = 0;
    for(var i = 0; i < arr.length; i++){
      var x = arr[i];
      if(x && typeof x === "object" && !Array.isArray(x) && (x.url || x.link || x.title || x.name)) hit++;
    }
    if(hit < Math.ceil(arr.length * 0.6)) return "";
    // v3.59（用户 m17212 图3）：外面的标题已经写了「N 条结果」，这里不再重复一行汇总；
    // 每条只留一行「序号 标题 域名」。
    var h = "<div class='tres'>";
    for(var i2 = 0; i2 < arr.length && i2 < 12; i2++){
      var it = arr[i2] || {};
      var url = String(it.url || it.link || "");
      var title = String(it.title || it.name || it.heading || url || ("结果 " + (i2 + 1)));
      var host = "";
      var hm = url.match(/^https?:\/\/([^\/]+)/i);
      if(hm) host = hm[1].replace(/^www\./, "");
      h += "<div class='tres-it'>" +
             "<div class='tres-t'><span class='tres-n'>" + (i2 + 1) + "</span>" + (url
               ? "<a href='" + esc(url) + "' target='_blank' rel='noopener' title='" + esc(title) + "'>" + esc(title) + "</a>"
               : "<span class='tres-tt'>" + esc(title) + "</span>") +
             (host ? "<span class='tres-h' title='" + esc(url) + "'>" + esc(host) + "</span>" : "") + "</div>" +
           "</div>";
    }
    if(arr.length > 12) h += "<div class='tres-more'>还有 " + (arr.length - 12) + " 条没显示</div>";
    h += "</div>";
    return h;
  }
  function shortId(s){
    var t = String(s || "");
    return t.length > 14 ? t.slice(0, 12) + "…" : t;
  }
  // 工具返回的 JSON 常常不合法：网页正文里的换行/制表符没转义就直接塞进字符串了
  // （搜索引擎结果就这样）。JSON.parse 会报 Bad control character，这里先把
  // 字符串字面量里的裸控制字符转成 \uXXXX 再试一次。
  function escapeCtl(s){
    var out = "", inStr = false, esc = false;
    for(var i = 0; i < s.length; i++){
      var ch = s.charAt(i), code = s.charCodeAt(i);
      if(esc){ out += ch; esc = false; continue; }
      if(inStr && ch === "\\"){ out += ch; esc = true; continue; }
      if(ch === '"'){ inStr = !inStr; out += ch; continue; }
      if(inStr && code < 0x20){
        out += "\\u" + ("000" + code.toString(16)).slice(-4);
        continue;
      }
      out += ch;
    }
    return out;
  }
  function tryParse(s){
    try { return {ok:true, v: JSON.parse(s)}; } catch(e){}
    try { return {ok:true, v: JSON.parse(escapeCtl(s))}; } catch(e){}
    return null;
  }
  // 尝试把 tool 返回当 JSON 看；不是对象/数组就当普通文本
  function tryJson(t){
    var s = String(t == null ? "" : t).trim();
    if(!s || (s[0] !== "{" && s[0] !== "[")) return {ok:false};
    var one = tryParse(s);
    if(one) return one;
    // 有些工具把多个对象直接拼在一起：{...}{...}{...}（既没逗号也没外层数组），补上再试。
    // 中间可能夹着换行/空白，不能只认紧挨着的 "}{"（请求记录里就带换行，
    // 窗口里那份被 clean 过才是紧挨的，两边表现不一致——用户 m32401 图1）
    if(s[0] === "{" && /\}\s*\{/.test(s)){
      var joined = tryParse("[" + s.replace(/\}\s*\{/g, "},{") + "]");
      if(joined) return joined;
    }
    return {ok:false};
  }
  // JSON 着色：递归拼，键/字符串/数字/布尔分开上色，字符串过长折一下（原文去「对照」档看）
  function jsonHtml(v, ind){
    ind = ind || 0;
    var pad = new Array(ind + 1).join("  ");
    if(v === null) return "<span class='j-b'>null</span>";
    if(typeof v === "boolean") return "<span class='j-b'>" + (v ? "true" : "false") + "</span>";
    if(typeof v === "number") return "<span class='j-n'>" + v + "</span>";
    if(typeof v === "string"){
      var s = v;
      if(s.length > 300) s = s.slice(0, 300) + "…（共 " + v.length + " 字）";
      return "<span class='j-s'>" + esc(JSON.stringify(s)) + "</span>";
    }
    if(Array.isArray(v)){
      if(!v.length) return "[]";
      if(ind > 6) return "<span class='j-b'>[…" + v.length + " 项]</span>";
      var rows = v.map(function(x){ return pad + "  " + jsonHtml(x, ind + 1); });
      return "[\n" + rows.join(",\n") + "\n" + pad + "]";
    }
    if(typeof v === "object"){
      var ks = Object.keys(v);
      if(!ks.length) return "{}";
      if(ind > 6) return "<span class='j-b'>{…" + ks.length + " 个键}</span>";
      var lines = ks.map(function(k){
        return pad + "  <span class='j-k'>" + esc(JSON.stringify(k)) + "</span>: " + jsonHtml(v[k], ind + 1);
      });
      return "{\n" + lines.join(",\n") + "\n" + pad + "}";
    }
    return esc(String(v));
  }
  // 卡片正文：能当 JSON 看就着色，否则走普通文本（tool 结果给更大的折叠阈值）
  function toolBodyHtml(raw, tj){
    tj = tj || tryJson(raw);
    if(tj.ok){
      // 认得出是一串结果条目就直接排成列表，原始 JSON 去「原文」档看
      var res = toolResultHtml(tj);
      if(res) return res;
      var h = jsonHtml(tj.v, 0), lines = h.split("\n").length;
      var long = lines > 14, lb = "展开（" + lines + " 行）";
      return "<div class='mono jx" + (long ? " clip" : "") + "'>" + h + "</div>" +
             (long ? "<span class='more' data-more='1' data-label='" + esc(lb) + "'>" + lb + "</span>" : "");
    }
    return bodyHtml(raw, "(空)", 800);
  }

  // 引用块：被回复的那条原话单独一块，视觉上缩进 + 竖线，不跟正文糊在一起
  // 用户第 2 条（QQ 群里是「被引用原话在上、回复在下」，窗口里结构/内容对不上）：
  // 原话自己还带着一层壳，以前只剥了「[时间] 昵称:」，剩下的原样铺进 .qtx ——
  // 于是 [Json card {…}] 变成满屏 &quot;、[Image attached] 露出英文字面量、
  // [@昵称](mqqapi://…) 露出 markdown、ISO 时间戳在引用头里整串显示。
  // 现在原话走和正文同一套拆解（splitMetaAll + richText），壳只在 qwho 上留一份，
  // 正文一条不少地留在下面，不会被引用吞掉。
  // 用户 m02993 图10：被引用的那条明明就在本窗口里，却显示「引用了更早的一条消息」——
  // 框架给的引用只有 id（[Reply ID: -252821101]），原话得自己按 id 去窗口里查。
  // 渲染前先把每条 user 消息的 message_id → 昵称 / 时间 / 原话 建成索引。
  // 展示投影不改发送合同：聚合 media 不能证明图片属于当前消息或引用目标。
  function uniqueOwnerMedia(refs){
    refs = Array.isArray(refs) ? refs : [];
    // 仅同一 owner 池内按精确 path / url 证明别名；不猜文件名、内容或跨消息归属。
    var aliases = Object.create(null), parents = [], seen = Object.create(null);
    function root(i){
      while(parents[i] !== i){ parents[i] = parents[parents[i]]; i = parents[i]; }
      return i;
    }
    refs.forEach(function(it, i){
      parents[i] = i;
      var keys = [];
      if(typeof it === "string") keys.push("path:" + it);
      else if(it){
        if(it.path) keys.push("path:" + it.path);
        if(it.url) keys.push("url:" + it.url);
      }
      keys.forEach(function(key){
        if(aliases[key] !== undefined) parents[root(i)] = root(aliases[key]);
        else aliases[key] = i;
      });
    });
    // 先连完别名再保留首项：后到的 path+url 也可能证明前两项其实是同一张图。
    return refs.filter(function(it, i){
      var key = root(i);
      if(seen[key]) return false;
      seen[key] = 1; return true;
    });
  }
  function displayOf(m, legacyMedia){
    var d = m && m.display, known = !!(d && d.own_media_known === true);
    var legacy = Array.isArray(legacyMedia) ? legacyMedia
      : (m && Array.isArray(m.media_refs) ? m.media_refs : (m && Array.isArray(m.media) ? m.media : []));
    // 完整投影数组（包括 []）是权威结果；仅旧数据缺少该数组时回退 legacy。
    var unassigned = d && Array.isArray(d.unassigned_media) ? d.unassigned_media : (known ? [] : legacy);
    // 已按原转发条目证明的数组表示图片槽位；不同条目可有意重复同一张图。
    var orderedSlots = known && d.source === "forward_cache_aligned" && Array.isArray(d.own_media);
    // 用户 m00942 图1：转发卡只画「这条消息自己带的图」时，模型侧按配额只挂了前几张
    // （默认 3 张），最后一条就只剩 🖼。后端另给一份插件缓存里的全部图，只用于渲染；
    // 不去重（同一条转发里可能有意重复同一张），也不改 known——归属结论仍是未知。
    var allMedia = d && Array.isArray(d.forward_media_all) ? d.forward_media_all.slice() : null;
    return { known: known, own: known ? (orderedSlots ? d.own_media.slice() : uniqueOwnerMedia(d.own_media)) : [],
      unassigned: uniqueOwnerMedia(unassigned),
      all: allMedia,
      // 用户 m21787 图1/图2：框架会把被引用那条消息的图也挂到引用它的消息上，靠外层 media
      // 分不清归属。后端按 message_id 回插件索引认回了被引用消息自己的图，放在 direct_reply.media，
      // 引用块优先用它（外层 unassigned_media 里对应的那几张后端已剔掉）。
      reply: d && d.direct_reply && d.direct_reply.id !== undefined
        ? { id: String(d.direct_reply.id),
            media: Array.isArray(d.direct_reply.media) ? d.direct_reply.media.slice() : null }
        : null,
      // 用户 m21787 图3/图4：记录里的 media 是发给模型的那一份（动图九宫格 / 静态压缩），
      // 美化档要显示聊天里原本那张；后端按 message_id 回索引取好放在 orig_media。
      orig: d && Array.isArray(d.orig_media) ? d.orig_media.slice() : null,
      body: d && typeof d.own_body === "string" ? d.own_body : "",
      // 每条转发条目各带几张图（后端对齐成功才有）：卡片据此把图放到真正的那一条，
      // 正文里字面的 [图片] 不占位（用户 m16262 图2）。
      slots: orderedSlots && Array.isArray(d.own_slots) ? d.own_slots.slice() : null,
      hasBody: !!(d && typeof d.own_body === "string") };
  }
  function displayTextOf(m){
    if(!m) return "";
    // 只提取文本段，绝不能把 JSON.stringify(raw content) 当成引用正文。
    if(Array.isArray(m.raw)) return m.raw.filter(function(p){ return p && p.type === "text"; })
      .map(function(p){ return String(p.text || ""); }).join("\n");
    if(typeof m.raw === "string" && m.raw) return m.raw;
    return typeof m.text === "string" ? m.text : (typeof m.body === "string" ? m.body : "");
  }
  function stripReplyTokens(text){
    var s = String(text == null ? "" : text), out = "";
    for(var i = 0; i < s.length; i++){
      if(s.charAt(i) === "["){
        var end = bracketEnd(s, i);
        if(end >= 0){
          var token = s.slice(i + 1, end);
          if(/^Reply(?:\s+ID:|\s+-?\d)/i.test(token)){ i = end; continue; }
          // Forward 是自有内容，内部结构仍交给原转发卡。
          if(/^Forward\b/.test(token)){ out += s.slice(i, end + 1); i = end; continue; }
        }
      }
      out += s.charAt(i);
    }
    // 用户 m01849：框架的 Reply 元素里，空白 Text 段以前会被 repr 成「Text( )」混进引用正文。
    // 后端已不再产出（decision/engine.py 的 _reply_parts），这里兜住已经存下来的旧窗口。
    out = out.replace(/[ \t]*Text\(\s*\)[ \t]*/g, " ");
    return out.replace(/^\s+|\s+$/g, "");
  }
  function ownBodyOf(m, fallback){
    var d = displayOf(m);
    return stripReplyTokens(d.hasBody ? d.body : (typeof fallback === "string" ? fallback : displayTextOf(m)));
  }
  function quoteEntryOf(m, fallback){
    m = m || {};
    var d = displayOf(m), body = ownBodyOf(m, fallback);
    // 归属未确认时，引用块仍要显示被引用消息的图；这些图只属于引用，
    // 不再同时铺到外层的中立附件里。
    var quoteMedia = d.known ? d.own : d.unassigned;
    var projected = { own_media_known: d.known, own_media: d.known ? d.own : [],
      unassigned_media: d.known ? [] : d.unassigned,
      direct_reply: d.reply, source: d.known ? (m.display && m.display.source === "forward_cache_aligned" ? "forward_cache_aligned" : "native_chain") : "unknown" };
    if(d.hasBody) projected.own_body = body;
    // 用户 m00942 图1：引用里的转发卡也要画全所有图，这里必须把完整图集带过去，
    // 否则 quoteEntryOf 重建的 projected 会把它丢掉，引用里又只剩前几张。
    if(d.all && d.all.length) projected.forward_media_all = d.all;
    // name 是展示名（昵称 · 群昵称），who 仍是纯昵称：引用块标题用前者，身份判定用后者
    return { body: body, name: dispNameOf(m.name || m.who, m.gnick), who: String(m.who || m.name || ""),
      gnick: String(m.gnick || ""),
      ts: String(m.ts || ""), media: quoteMedia, display: projected };
  }
  function unassignedMediaHtml(refs, mode){
    if(mode === "mixed" || mode === "raw") return "";
    var html = thumbsHtml(uniqueOwnerMedia(refs));
    return html ? "<div class='unassigned-media' data-media-owner='unknown'><span class='dim'>归属未确认的附件</span>" + html + "</div>" : "";
  }
  var QUOTE_IDX = {};
  // 用户 m10400 图5：记录页 ② 历史消息补进引用索引的条目。换记录时按值回收，
  // 免得开过的记录越攒越多（只删自己加的那份，③ 本轮与窗口重建的条目不动）。
  var QUOTE_HIST_ADDED = {};
  // 用户 m02993 图10：窗口里查不到时，再由后端翻历史记录补一遍；补到的放这儿，
  // 重画时不用再问一次。QUOTE_SID 跟着当前会话走。
  var QUOTE_FAR = {};
  // 每次窗口 GET 内按 id 限次，unknown/空回查留标记供下一次 GET 重试。
  var QUOTE_PIC_TRIED = Object.create(null);
  var QUOTE_SID = "";
  var quoteFarBusy = false;
  function segMediaOf(parts, media){
    var pool = media || [], n = (parts || []).length;
    if(!n) return [];
    if(!pool.length) return parts.map(function(){ return null; });
    if(n === 1) return [pool];
    var claims = claimMediaBySegs(parts, pool);
    if(claims) return claims.map(function(c){ return (c && c.own && c.own.length) ? c.own : null; });
    // 占位总数和图数对不上就认不准是哪张：全算给第一段，至少别丢图
    return parts.map(function(_, i){ return i === 0 ? pool : null; });
  }
  function buildQuoteIdx(msgs, pend){
    var idx = {};
    function take(m, isPend){
      if(!m) return;
      var role = String(m.role || (isPend ? "user" : "assistant")).toLowerCase();
      var src = displayTextOf(m);
      if(role !== "user"){
        var re = /<msg\s+message_id="(-?\d+)"[^>]*>\s*\[([^\]]*)\]\s*<text>([\s\S]*?)<\/text>/gi, mm, rows = [], previousEnd = 0;
        while((mm = re.exec(src))){
          rows.push({ id: mm[1], name: mm[2], body: mm[3], offset: mm.index, previousEnd: previousEnd });
          previousEnd = re.lastIndex;
        }
        rows.forEach(function(row){
          if(idx[row.id]) return;
          var tm = /\[([^\]]{2,20})\]\s*$/.exec(src.slice(row.previousEnd, row.offset));
          var vm = { name: row.name, ts: tm ? tm[1] : "", display: rows.length === 1 ? m.display : null };
          idx[row.id] = quoteEntryOf(vm, row.body);
        });
        return;
      }
      var parts = splitMetaAll(tidyBody(src));
      if(!parts.some(function(p){ return p.meta && p.meta.mid; }) && typeof m.text === "string"){
        parts = splitMetaAll(tidyBody(m.text));
      }
      parts.forEach(function(p){
        var meta = p.meta || {}, mid = String(meta.mid || (parts.length === 1 ? (m.message_id || m.mid || "") : "")).trim();
        if(!mid || idx[mid]) return;
        var mts = String(meta.ts || "").trim();
        if(!mts && m.ts) mts = fmtTime(m.ts).slice(6, 11);
        var fallback = (meta.fwd || "") + (meta.card ? "[Json card " + JSON.stringify(meta.card) + "]" : "") + (meta.file ? "[File name: " + meta.file.name + (meta.file.path ? ", file_path: " + meta.file.path : "") + "]" : "") + String(p.body || "");
        idx[mid] = quoteEntryOf({ name: String(meta.name || m.name || "").replace(/\s+/g, " ").trim(),
          gnick: String(meta.gnick || m.gnick || "").trim(),
          ts: mts, display: parts.length === 1 ? m.display : null }, fallback);
      });
    }
    (msgs || []).forEach(function(m){ take(m, false); });
    (pend || []).forEach(function(m){ take(m, true); });
    return idx;
  }

  function quoteHtml(q, media, outerText){
    if(!q || (!q.text && !q.id)) return "";
    var id = q.id === undefined || q.id === null ? "" : String(q.id);
    // q.id 是外层这条消息的 id；真正被引用的是外层正文里的 Reply ID。
    var rid = String(outerText || q.text || "").match(/\[Reply ID:\s*([^\s\]]+)/);
    if(rid) id = rid[1];
    // 用户 m12814：点引用块跳到被引用的那条消息——当前视图里有就滚过去，没有就去那一轮快照里找。
    // 挂的是被引用那条的 message_id（不是外层这条的），点击处理在 app.js 的 winClick 里。
    var jump = id ? " data-qjump='" + esc(id) + "'" : "";
    var hit = QUOTE_IDX[id], far = QUOTE_FAR[id];
    if(!displayOf(hit).known && far) hit = far;
    // 保留第二参数签名，但不再认领调用方聚合 media 或 q.media。
    var d = displayOf(hit || q), t;
    if(hit) t = ownBodyOf(hit, hit.body);
    else t = ownBodyOf(q, String(q.text || ""));
    // 用户 m21787 图4：后端只按 message_id 认回了图（source=media_index），正文/昵称/时间
    // 还是引用里那行原文——以前这里要求「归属未知」才回退，认回图之后原话就被吞了。
    if(!t && q.text) t = stripReplyTokens(q.text);
    if(!t && !d.known && !hit){
      return "<div class='quote' data-qmiss='" + esc(id) + "'" + jump + "><span class='qwho'>引用了更早的一条消息</span>" +
        "<div class='qmiss'>#" + esc(id) + "</div></div>";
    }
    var m = t.match(/^\[(?!Forward\b)([^\]]{4,40})\]\s*([^:：]{1,40})[:：]\s*([\s\S]*)$/);
    var who = String((hit && (hit.name || hit.who)) || q.who || (m ? m[2] : ""));
    // 用户 m03205：引用块标题里的群昵称也分色；who 仍是纯文本（身份判定用）。
    var whoHtml = "";
    if(!who){
      var nm = String(outerText || q.text || "").match(/user_nickname:\s*([^,\]]+)(?:,\s*group_nickname:\s*([^,\]]+))?/);
      if(nm){ who = dispNameOf(nm[1], nm[2]); whoHtml = dispNameHtml(nm[1], nm[2]); }
    }
    if(!whoHtml) whoHtml = (hit && hit.gnick) ? dispNameHtml(hit.who || "", hit.gnick) : esc(who);
    var when = shortTs(String((hit && hit.ts) || q.ts || (m ? m[1] : "")));
    var body = stripReplyTokens(m ? m[3] : t);
    var qIsFwd = body.trim().indexOf("[Forward") === 0;
    // 用户 m00942 图1：转发卡要画全所有图，引用里的那份也一样——display.forward_media_all
    // 是这条转发消息的完整图集（own_media_known 仍为 false，不影响别处判定）。
    // 用户 m21787 图1/图2：被引用那条消息自己的图由后端按 message_id 认回来了（direct_reply.media），
    // 优先用它——框架会把被引用的图也挂到外层消息上，光看外层 media 分不清归属。
    var qown = Array.isArray(q.media) ? q.media : null;
    // 用户 m07367：被引用的是转发时，q.media 只是「发给模型的那几张」（默认 3 张），
    // 原转发卡用的是 display.forward_media_all（整条转发的全部图）。以前 qown 优先，
    // 引用里就只剩 3 张、还挂着「（已发 3）」，跟上面那张原转发卡对不上。
    // 用户 m07484：被引用的转发在 direct_reply.media 里只有模型侧那几张（默认 3 张），
    // 而 d.own 是后端按转发缓存对齐好的完整图集（source=forward_cache_aligned）。
    // 以前 qown 优先，引用里的转发卡就永远只画 3 张、还挂一句「还有 N 张」。
    var qsrc = (qIsFwd && d.all && d.all.length) ? d.all
      : ((qIsFwd && d.known && d.own && d.own.length > (qown ? qown.length : 0)) ? d.own
        : ((qown && qown.length) ? qown
          : (d.known ? d.own : (d.unassigned || []))));
    // 用户 m21787 图4：引用里既然换成聊天里原本那张图，正文里「（动图 · 共 N 帧…）」这句
    // 派生说明也得一起去掉，否则原图旁边还挂着一句「上面是等距抽的 9 帧拼图」。
    if(qown && qown.length) body = stripDeriveNote(body);
    var qm = qIsFwd ? "" : thumbsHtml(qsrc);
    // known [] 是确定无图；图文并存时不因没有 Image 占位而清掉图或文字。
    if(qm || (d.known && !qsrc.length)) body = tidyBody(body)
      .replace(/\[(?:Image attached|Image(?:\s*\d+)?|图片|表情|image)\]/gi, " ").replace(/^[\s\u00a0]+|[\s\u00a0]+$/g, "");
    var mark = (!d.known && id && !qsrc.length) ? " data-qpic='" + esc(id) + "'" : "";
    var qh = "<span class='qwho'>" + (who ? whoHtml : "引用") +
      (when ? "<span class='dim'> · " + esc(when) + "</span>" : "") + "</span>";
    var fw = forwardHtml(body, qsrc);
    if(fw){
      // 用户 m09904：转发卡只画出一部分图（卡上挂着「（已发 N）」）时也要挂修复标记。
      // 以前只在 qsrc 一张都没有时挂，于是「有 3 张、缺 11 张」的卡片永远不会重画。
      // 只在归属未知（!d.known）时挂：已知的图集再查一次还是同一份，避免反复重画。
      if(!mark && !d.known && id && fw.indexOf("fw-sent") >= 0) mark = " data-qpic='" + esc(id) + "'";
      return "<div class='quote'" + mark + jump + ">" + qh + fw + "</div>";
    }
    // 用户 m00456：引用里只剩 [Forward] 这个空壳——框架当时没把聊天记录取回来，
    // 图倒是认回来了（qsrc 有 9 张），可没有条目就画不出卡，只能落成「内容未获取」。
    // 挂上标记交给 fillQuoteMiss：后端按 message_id 能补回正文，补到就整块重画。
    if(qIsFwd && id && !mark) mark = " data-qpic='" + esc(id) + "'";
    var phOnly = body.trim(), phN = (phOnly.match(/\[(?:Image attached|Image(?:\s*\d+)?|图片|表情|image)\]/gi) || []).length;
    if(!qm && phN && /^(?:\s*\[(?:Image attached|Image(?:\s*\d+)?|图片|表情|image)\])+\s*$/i.test(phOnly)){
      var phRow = "";
      for(var pi = 0; pi < Math.min(phN, 9); pi++) phRow += "<div class='qpic-ph'>图片</div>";
      return "<div class='quote'" + mark + jump + ">" + qh + phRow + "</div>";
    }
    var qbody = quoteBodyHtml(body, !!who, qIsFwd ? qsrc : null);
    // v4.82（用户 m08672）：引用原话太长（菜单、长文）时先折起来，别把整张卡撑爆
    // 用户 m00492：折叠口径跟正文一致——正文 2 行就折，引用不能比原文还长。
    var qfi = quoteFoldInfo(body);
    var qLines = String(qfi.text || "").split("\n").length;
    if(qbody && !qfi.cardish && qfi.text.trim() && estLines(qfi.text) > FOLD_LINES){
      var qlb = "展开全文（" + qLines + " 行）";
      qbody = "<div class='qbody clip'>" + qbody + "</div>" +
        "<span class='more qmore' data-more='1' data-qbody='1' data-label='" + esc(qlb) + "'>" + esc(qlb) + "</span>";
    }
    return "<div class='quote'" + mark + jump + ">" + qh + qm + qbody + "</div>";
  }
  // 引用原话的正文：按「一条消息」的规则渲染，跟正文同一个长相（用户第 2 条）。
  // 单独拆出来是因为原话可能自带多条（换行）、自带 [Json card …] / [File name: …] /
  // [Forward …] / [Image attached] / [At …] / [@昵称](mqqapi://…)。
  // hasHead 表示外层已经画出「昵称 · 时间」，这段就不用再重复一遍前缀。
  // v4.18（用户 m20749 图2）：被引用的如果是小程序分享，框架的 Reply 里只留了
  // 「[QQ小程序]标题」这一行纯文本（没有 Json card 结构，url/title/desc 都拿不到，
  // 还原不出原卡）。以前原样铺出来，引用里就是一串方括号；这里画成同款小卡。
  function miniCardHtml(kind, text){
    var t = String(text || "").trim();
    if(!t) return "";
    return "<div class='qmini'><span class='qmini-ic'>" + esc(String(kind).slice(0, 2)) + "</span>" +
           "<div class='qmini-bd'><span class='qmini-t'>" + esc(t) + "</span>" +
           "<span class='qmini-d'>" + esc(kind) + " · 分享</span></div></div>";
  }

  function quoteBodyHtml(body, hasHead, fwdMedia){
    var t = stripReplyTokens(body);
    // 用户第 1 条（引用里的艾特没美化）+ 第 12 条（双 @）：QQ 的 markdown mention
    // 和冗余 "@昵称" 前缀都交给 mentionNorm 洗（tidyBody 里已经调过），这里不再重复一遍
    var parts = splitMetaAll(tidyBody(t));
    if(!parts.length) parts = [{ meta: { at: [] }, body: t }];
    var out = "";
    parts.forEach(function(p, pi){
      var mt = p.meta || {};
      var pre = cardHtml(mt.card) + fileHtml(mt.file) + fwdHtml(mt.fwd, pi === 0 ? fwdMedia : null);
      // 只画目标自己的正文/卡片，不递归渲染目标的 Reply。
      var head = "";
      // 多段原话（拼了好几条）或者外层没解析出「昵称:」时，把这段自己的时间/昵称
      // 用小字标一行；否则 ISO 时间戳会混进正文，看着像他说的原话
      if((parts.length > 1 && pi > 0) || !hasHead){
        var nm2 = dispNameHtml(mt.name, mt.gnick), wh2 = mt.ts ? shortTs(mt.ts) : "";
        // 外层已经写了「引用」两个字，这段没昵称时就只补时间，别再重复一遍
        if(nm2 || wh2){
          head = "<span class='qwho'>" + (nm2 ? nm2 : "") +
                 (wh2 ? "<span class='dim'>" + (nm2 ? " · " : "") + esc(wh2) + "</span>" : "") + "</span>";
        }
      }
      var txt = String(p.body || "");
      if(!txt && !pre) return;
      // 小程序分享：整段就是「[QQ小程序]标题」，画成小卡，别再当普通正文铺开
      var mq = txt.match(/^\s*\[(QQ小程序|小程序|QQ音乐|QQ频道)\]\s*([\s\S]+)$/);
      if(mq){
        out += head + (pre ? "<div class='qtx'>" + pre + "</div>" : "") +
               "<div class='qtx'>" + miniCardHtml(mq[1], mq[2]) + "</div>";
        return;
      }
      // 用户 m12814 图4：引用里的抖音分享口令（「5.69 CUY:/ … v.douyin.com/xxx/ 复制此链接…」）
      // 以前原样铺成文字；正文那条路有 douyinCard 画成卡片，引用这里也得走一遍。
      var dy = douyinCard(txt);
      if(dy){
        out += head + (pre ? "<div class='qtx'>" + pre + "</div>" : "") +
               "<div class='qtx'>" + dy.html + "</div>" +
               (dy.rest ? "<div class='qtx'>" + richText(dy.rest) + "</div>" : "");
        return;
      }
      out += head + (pre ? "<div class='qtx'>" + pre + "</div>" : "") +
             (txt ? "<div class='qtx'>" + richText(txt) + "</div>" : "");
    });
    // 剥掉 Reply 后为空就保持空，不能把祖先原文重新兜回。
    return out;
  }
  // 用户 m12814 图2：引用原话是一张分享卡时，body 是几百字的裸 JSON（splitMeta 会把它
  // 收成 meta.card 画成卡片），按原始长度估行数会判成「太长」，把卡片裁掉一半。
  // 折叠只看真正会铺成文字的那部分；带卡片 / 文件 / 转发的原话一律不折。
  function quoteFoldInfo(body){
    var t = stripReplyTokens(body);
    // 用户 m12814 图4：抖音分享口令不是 JSON 卡，splitMetaAll 认不出来，但
    // quoteBodyHtml 会把它画成卡片；这里同样按「卡片」处理，别把卡片裁掉一半。
    var dy = douyinCard(t);
    if(dy) return { text: String(dy.rest || ""), cardish: true };
    var parts = splitMetaAll(tidyBody(t));
    if(!parts.length) return { text: t, cardish: false };
    var text = "", cardish = false;
    parts.forEach(function(p){
      var mt = (p && p.meta) || {};
      if(mt.card || mt.file || mt.fwd) cardish = true;
      text += String((p && p.body) || "") + "\n";
    });
    return { text: text.replace(/\s+$/, ""), cardish: cardish };
  }
  // 转发的聊天记录（QQ 那张「群聊的聊天记录」卡片）框架是一行一条转成
  //   [Forward [时间] 名字: 内容  +  空行  +  [时间] 名字: 内容]
  // 直接铺出来就是 9 行 [2026-10-21 22:34:11] 视频解析bot: [图片]（用户 m33994 图1），
  // 这里按条拆成一行一条的小列表，连续同款媒体再合并成 [图片] ×7。
  var FWD_HEAD_ROWS = 3;   // 转发卡默认露几条：前 3 条，第 3 条留半截
  // 一条转发条目里有几个媒体占位：可能是「[图片]」「[图片] ×3」，贴纸被洗成「[表情]」。
  // 图片槽位先按顺序认领（跟 QQ 的图文顺序一致），剩下的图再补给 [表情] 条目；
  // 语音 / 视频 / 文件没有对应的图，不参与认领。
  var FWD_PIC_N = /\[图片\](?:\s*×\s*(\d+))?/g;
  var FWD_EMO_N = /\[表情\](?:\s*×\s*(\d+))?/g;
  function fwdCount(re, text){
    var n = 0, m;
    re.lastIndex = 0;
    while((m = re.exec(String(text || "")))) n += (m[1] ? (parseInt(m[1], 10) || 1) : 1);
    return n;
  }
  function fwdPicN(text){ return fwdCount(FWD_PIC_N, text); }
  function fwdEmoN(text){ return fwdCount(FWD_EMO_N, text); }
  // 嵌套转发：后端把子转发压成一行「[合并转发 4 条：昵称: 摘要 / …]」；
  // 框架历史里被剔掉的嵌套条目只剩空正文。两种都画成小卡，不当成图片。
  var FWD_SUB_RE = /^\[合并转发(?:\s*(\d+)\s*条)?(?:\s*[：:]\s*([\s\S]*?))?\]$/;
  function fwdSubOf(text){
    var m = String(text || "").match(FWD_SUB_RE);
    if(!m) return null;
    return { n: m[1] ? (parseInt(m[1], 10) || 0) : 0, pre: String(m[2] || "").trim(), lost: false };
  }
  // 用户 m19755：转发条目里的分享卡（夸克网盘这类）在框架原文里只是一段裸 JSON，
  // 以前整段铺在条目里，看着像一坨代码。这里认出「[Json card {...}]」与裸 JSON 两种
  // 形态，交给 cardHtml 画成跟正文一样的分享卡。
  function fwdCardOf(t){
    var s = String(t == null ? "" : t).trim();
    if(!s) return null;
    if(s.indexOf("[Json card") === 0){
      var p = s.indexOf("{");
      if(p < 0) return null;
      var pe = bareJsonEnd(s.slice(p));
      if(pe <= 0) return null;
      try {
        var po = JSON.parse(s.slice(p, p + pe + 1));
        if(po && typeof po === "object" && !(po instanceof Array)) return po;
      } catch(e0){}
      return null;
    }
    if(s.charAt(0) !== "{") return null;
    var e = bareJsonEnd(s);
    if(e <= 0) return null;
    try {
      var o = JSON.parse(s.slice(0, e + 1));
      if(looksLikeCard(o)) return o;
    } catch(e1){}
    return null;
  }
  // 预览行只有一行、还是省略号截断，放不下整张卡：只画链接小图标 + 标题。
  function fwdCardChip(c){
    var t = String(c.title || c.prompt || c.desc || c.app || "").trim();
    return "<span class='fw-pic' title='分享卡片'>🔗</span>" + esc(t || "分享卡片");
  }
  function fwdSubChip(sub){
    var label = "合并转发" + (sub.n ? " " + sub.n + " 条" : "");
    var note = sub.pre || (sub.lost ? "内容未展开" : "");
    return "<span class='fwd-sub'><span class='fwb'>" + esc(label) + "</span>" +
           (note ? "<span class='fwp'>" + esc(note) + "</span>" : "") + "</span>";
  }
  // slots（可选）：每条转发条目各带几张图，跟 entries 一一对应；长度对不上就不用，
  // 退回按正文里的 [图片] 占位顺序认领（老行为）。
  function forwardHtml(body, media, slots){
    var t = String(body || "").trim();
    if(t.indexOf("[Forward") !== 0) return "";
    var e = bracketEnd(t, 0);
    var inner = e > 0 ? t.slice(8, e) : t.slice(8);
    inner = inner.replace(/^[\s:：]+/, "");
    var rows = [];
    var entries = inner.split(/\n{2,}/);
    // 用户 m17492 图2：实时窗拿不到后端的槽位表（图片被归到「归属不明」那一堆），
    // 但正文里宿主自己留的 [Image attached] 就是权威槽位——按条目数一遍，
    // 总数正好等于这一条的图片数才敢用，多一张少一张都退回占位顺序认领。
    if((!slots || slots.length !== entries.length) && media && media.length){
      var hostSlots = entries.map(function(raw){
        // 用户 m18581：贴纸（[Sticker attached]）也是媒体槽。只数 [Image attached]
        // 会让槽位总数对不上媒体池，整张表被弃用，贴纸的图就落到后面的图片条目上。
        // 还有一层：正文进到这里之前已经过 tidyBody，[Image attached] / [Sticker attached]
        // 早被洗成 [图片] / [表情] 了，只认英文标记的话总数恒为 0，槽位表永远用不上。
        var s = String(raw || ""), n = 0;
        s.replace(/\[(?:image|sticker)(?:\s+attached|:[^\]\r\n]*)?\]/gi, function(){ n++; return ""; })
         .replace(/\[(?:图片|表情|语音|视频)\](?:\s*×\s*(\d+))?/g, function(_m, k){ n += (k ? (parseInt(k, 10) || 1) : 1); return ""; });
        return n;
      });
      var hostTotal = hostSlots.reduce(function(a, b){ return a + b; }, 0);
      if(hostTotal === media.length) slots = hostSlots;
    }
    var useSlots = !!(slots && slots.length === entries.length);
    entries.forEach(function(raw, nodeIdx){
      var it = String(raw || "").trim();
      if(!it) return;
      var mm = it.match(/^\[([^\]]{4,40})\]\s*([^:：]{1,40})[:：]\s*([\s\S]*)$/);
      var when = mm ? shortTs(mm[1]) : "", who = mm ? mm[2] : "";
      if(/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}/.test(when)) when = when.replace(/^\d{4}-\d{2}-\d{2}[ T]/, "").slice(0, 5);
      var tx = (mm ? mm[3] : it).trim();
      // 这一条真正带几张图：宿主写的是 [Image attached]，tidyBody 之后跟正文自带的
      // [图片] 长得一样，所以张数以背端槽位表为准，-1 表示没有槽位表。
      var slotN = useSlots ? (slots[nodeIdx] | 0) : -1;
      // 用户第 2 条：转发条目里的 [Image attached] 以前原样露英文标记，
      // 现在跟正文一样洗成 [图片]，连续多张压成「[图片] ×8」免得一行糊满
      tx = tidyBody(tx);
      // 转发条目里带的 [Reply 672919715] / [Reply ID: …] 是框架的内部引用标记，
      // 卡片里既画不出引用块，留着就是一串数字噪声（m24522 ④ 一类）。
      tx = tx.replace(/\[Reply(?:\s+ID)?[:：]?\s*\d+[^\]]*\]/g, "").replace(/\s{2,}/g, " ").trim();
      tx = tx.replace(/(\[(?:图片|表情|语音)\])(?:\1)+/g, function(m, one){
        return one + " ×" + (m.length / one.length);
      });
      // 用户 m19755：这一条整段就是一张分享卡时，别把 JSON 当正文铺出来。
      var fcard = fwdCardOf(tx);
      if(fcard){
        rows.push({ who: who, when: when, text: "", sub: null, card: fcard, n: 1, slotN: slotN });
        return;
      }
      // 空正文不是「只有图」，而是框架把嵌套 Forward 从链里剔掉了（见上面 CSS 注释）：
      // 画成「合并转发 · 内容未展开」小卡，别补 [图片] 去抢后面条目的图。
      if(!tx){
        rows.push({ who: who, when: when, text: "", sub: { n: 0, pre: "", lost: true }, n: 1 });
        return;
      }
      var sub = fwdSubOf(tx);
      if(sub){
        rows.push({ who: who, when: when, text: "", sub: sub, n: 1 });
        return;
      }
      rows.push({ who: who, when: when, text: tx, sub: null, n: 1, slotN: slotN });
    });
    if(!rows.length) return "";
    var merged = [];
    rows.forEach(function(r){
      var last = merged[merged.length - 1];
      if(last && last.who === r.who && last.text === r.text && /^\[(图片|表情|语音|视频)\]/.test(r.text)){
        last.n++;
        if(last.slotN >= 0 && r.slotN >= 0) last.slotN += r.slotN; else last.slotN = -1;
        return;
      }
      merged.push(r);
    });
    // 用户 m03273：点「点击查看内容」弹大框看完整内容，别再在卡里往下摊开——
    // 以前是把 thumbs 整块追加在条目后面，展开时两张图堆在最底下，和原来 QQ
    // 接龙的顺序对不上。现在按条目认领图片：文本里有几个 [图片] 占位（含「×N」）
    // 就分几张给它，分不完的并到末尾「其他图片」一组。
    var pool = (media || []).slice();
    var cursor = 0;
    // 第一遍按 [图片] 占位顺序认领（跟 QQ 的图文顺序一致；嵌套转发小卡不占槽位）
    var claims = merged.map(function(r){
      var got = [], want = r.sub ? 0 : (r.slotN >= 0 ? r.slotN : fwdPicN(r.text || "") * (r.n || 1));
      while(want > 0 && cursor < pool.length){ got.push(pool[cursor++]); want--; }
      return got;
    });
    // 第二遍：剩下的图补给 [表情] 条目——贴纸在媒体池里，正文却只写了「[表情]」
    merged.forEach(function(r, idx){
      if(r.sub) return;
      var want = fwdEmoN(r.text || "") * (r.n || 1);
      while(want > 0 && cursor < pool.length){ claims[idx].push(pool[cursor++]); want--; }
    });
    // v4.48（用户 m25038 第 4 项）：卡里三行预览也把真图露出来——只写小图标的话，
    // 图明明取到了，卡片上还是一个「🖼」，看着就像没获取。
    // 用户 m07645 第 1 条：卡片预览最多画 5 张缩略图，第 6 张起在缩略图后面写「还有 N 张」。
    // 以前这里写死 4 张、多出来的直接消失，看着就像「图被清掉了」。
    var FWD_MINI_MAX = 5;
    function fwdMini(list){
      if(!list || !list.length) return "";
      var out = "";
      list.slice(0, FWD_MINI_MAX).forEach(function(it){
        var obj = (typeof it === "string") ? { path: it } : (it || {});
        if(obj.missing) return;
        var p = obj.path || "", direct = obj.url || "";
        if(!p && !direct) return;
        var url = direct || (API + "/media?path=" + encodeURIComponent(p) + (ctxSid ? "&sid=" + encodeURIComponent(ctxSid) : ""));
        out += "<img class='fw-im' loading='lazy' decoding='async' src='" + esc(url) + "'" +
               " data-img='" + esc(url) + "' data-imgt='" + esc(p || direct) + "' alt='图片'" +
               " onerror=\"this.style.display='none'\">";
      });
      return out ? "<span class='fw-ims'>" + out + "</span>" : "";
    }
    // v4.90（用户 m02939 图5）：数这一条真正拿到手的图有几张。
    function fwdShownN(list){
      var n = 0;
      (list || []).forEach(function(it){
        var o = (typeof it === "string") ? { path: it } : (it || {});
        if(!o.missing && (o.path || o.url)) n++;
      });
      return n;
    }
    // 用户 m17145：正文里的 [表情] 不是图片槽。贴纸只有媒体池里真有图时才认领得到，
    // 把它算进 need 会让「（已发 N）」挂在图片明明已经贴上的条目上（12:50 那条）。
    function fwdNeed(r){
      var t = String(r.text || ""), n = r.n || 1;
      return { pic: r.slotN >= 0 ? (r.slotN | 0) : fwdPicN(t) * n, emo: fwdEmoN(t) * n };
    }
    function stripFwdKind(t, kind){
      return String(t || "").replace(new RegExp("\\[" + kind + "\\](?:\\s*×\\s*\\d+)?", "g"), "")
        .replace(/[ \t]{2,}/g, " ").trim();
    }
    // 2026-11-01（用户 m06138 图1）：条目已经有真图时，预览行不再把同一个 [图片]
    // 画成小方块；只有还有图没认领到（被清理/没下发）才保留占位 chip。
    // 图片先占满 [图片] 槽，多出来的才轮到 [表情]（第二遍认领），所以两种占位分开剥。
    function fwdStrip(r, got){
      var need = fwdNeed(r), shown = fwdShownN(got);
      var out = String(r.text || "");
      // 用户 m18581：槽位表把贴纸也算进去了，图到手就两种占位一起剥；
      // 只按 [图片] 剥的话贴纸条目会同时画一张图和一个 🙂。
      if(r.slotN >= 0){
        if(shown >= (r.slotN | 0)) out = stripFwdKind(stripFwdKind(out, "图片"), "表情");
        return out;
      }
      var picGot = Math.min(shown, need.pic), emoGot = Math.max(0, shown - need.pic);
      if(need.pic > 0 && picGot >= need.pic) out = stripFwdKind(out, "图片");
      if(need.emo > 0 && emoGot >= need.emo) out = stripFwdKind(out, "表情");
      return out;
    }
    // m24522 ④⑥：占位不再当字面文字铺出来，统一换成小图标（卡片预览与弹层共用）
    function fwdPicChip(t, compact, allowChinese){
      var video = nativeVideoHtml(t, function(part){ return fwdPicChip(part, compact, false); }, compact, allowChinese);
      if(video !== null) return video;
      var ico = { "图片": "🖼", "表情": "🙂", "语音": "🎙" };
      // 用户 m27528 图1：转发条目的表情也要转，跟正文同一套（以前这里只 esc）
      return emojiFaces(esc(t)).replace(/\[(图片|表情|语音)\](?:\s*×\s*(\d+))?/g, function(all, k, n){
        return "<span class='fw-pic' title='" + k + (n ? " ×" + n : "") + "'>" +
               (ico[k] || k) + (n ? "×" + n : "") + "</span>";
      });
    }
    // 用户 m28299 图1：合并行里的视频卡要按条数重复。以前弹层只画一张卡再补「×N」，
    // 那是图片占位的写法，3 条视频看着像 1 条；缩略图分支（用户 m07484）也走这里。
    function fwdRestHtml(t, n){
      var v = nativeVideoHtml(t, function(part){ return fwdPicChip(part, false); }, false);
      if(v !== null) return n > 1 ? v.repeat(n) : v;
      return fwdPicChip(t, false) + (n > 1 && t ? " ×" + n : "");
    }
    var lis = merged.map(function(r, idx){
      var got = claims[idx] || [];
      var pre = r.sub ? "" : fwdStrip(r, got);
      var _shown = fwdShownN(got);
      // 「（已发 N）」是「还有图没到手」的说明，不是张数统计：图全认领完了就不写
      // （用户 m00942 图2：2 条转发只有 1 张图，图已经画出来了还挂一句「已发 1」）。
      var _need = fwdNeed(r).pic;
      // 用户 m07484：图已经画成缩略图了，就别再在缩略图前面写「🖼 ×5（已发 3）」——
      // 那串字和旁边的图重复，还让人以为只发了 3 张。条目里除图片以外的文字照留，
      // 还差几张在缩略图后面补一句「还有 N 张」。
      var _mini = fwdMini(got);
      // 用户 m07645 第 1 条：超出预览上限的那几张也要算进「还有 N 张」，
      // 否则 5 张图只画 5 张里的前几张、后面一句说明都没有。
      var _left = Math.max(0, _need - Math.min(_shown, _need)) + Math.max(0, _shown - FWD_MINI_MAX);
      var _zh = /^(?:\[视频\]\s*)+$/.test(String(r.text || "").trim());
      var body, tail = "";
      if(r.card){
        body = fwdCardChip(r.card);
      } else if(r.sub){
        body = fwdSubChip(r.sub);
      } else if(_mini){
        var _rest = stripFwdKind(pre, "图片");
        body = _rest ? fwdPicChip(_rest, true, _zh) : "";
        tail = _left > 0 ? "<span class='fw-sent'>还有 " + _left + " 张</span>" : "";
      } else {
        body = fwdPicChip(pre, true, _zh) + (r.n > 1 && pre ? " ×" + r.n : "");
      }
      return "<div class='fwd-it'><span class='fw-w'>" + esc(r.who || "某人") + "</span>" +
             (r.when ? "<span class='fw-t'>" + esc(r.when) + "</span>" : "") +
             "<span class='fw-c'>" + body + _mini + tail + "</span></div>";
    });
    var full = merged.map(function(r, idx){
      var got = claims[idx] || [], body = "";
      if(r.card){
        body = "<div class='fp-tx'>" + cardHtml(r.card) + "</div>";
      } else if(r.sub){
        body = "<div class='fp-tx'>" + fwdSubChip(r.sub) + "</div>";
      } else {
        var _needF = fwdNeed(r);
        var _shownF = fwdShownN(got);
        var _leftF = _needF.pic - Math.min(_shownF, _needF.pic);
        // 有剩余的图（被清理/没下发）就不再写死字面 [图片]，同样交给 chip
        // 用户 m28299 图1（转发的 emoji 没显示）：图都认领完了的那一支原来只 esc，
        // 弹层里 [Emoji 344] 就成了一行方括号。表情与正文同一套，一律过 emojiFaces。
        var _stripped = fwdStrip(r, got);
        var txt = fwdRestHtml(_stripped, r.n);
        var _thumbs = thumbsHtml(got);
        if(_thumbs && _leftF > 0){
          // 用户 m07484：图已经铺出来了，弹层里也不再写「🖼 ×5（已发 3）」，
          // 只留条目里除图片以外的文字，还差几张在缩略图后面补一句。
          var _restF = stripFwdKind(_stripped, "图片");
          txt = _restF ? fwdRestHtml(_restF, r.n) : "";
        }
        body = (txt ? "<div class='fp-tx'>" + txt + (_thumbs ? "" : (_leftF > 0 && _shownF > 0 ? "<span class='fw-sent'>（已发 " + _shownF + "）</span>" : "")) + "</div>" : "") + _thumbs +
               (_thumbs && _leftF > 0 ? "<span class='fw-sent'>还有 " + _leftF + " 张</span>" : "");
      }
      return "<div class='fp-it'><div class='fp-meta'><span class='fw-w'>" + esc(r.who || "某人") + "</span>" +
             (r.when ? "<span class='fw-t'>" + esc(r.when) + "</span>" : "") + "</div>" + body + "</div>";
    });
    if(cursor < pool.length){
      full.push("<div class='fp-it'><div class='fp-meta'><span class='fw-w'>其他图片</span></div>" +
                thumbsHtml(pool.slice(cursor)) + "</div>");
    }
    // 卡里只留前 3 条预览（.fwd-b）；完整条目与图片藏在 <template class="fwd-full"> 里，
    // 点开时才搬进弹层——template 里的图片不会提前加载，也不用在卡内留一条可见的图行。
    // 用户 2026-10-24 图6：卡下面不再写「点击查看内容」，整张卡就是入口，悬停标题变蓝并带提示
    return "<div class='fwd' data-fwd='1'>" +
           "<div class='fwd-hd' title='点击查看完整内容'>合并转发 · " + rows.length + " 条</div>" +
           "<div class='fwd-b'>" + lis.join("") + "</div>" +
           "<template class='fwd-full'>" + full.join("") + "</template></div>";
  }
  // 正文本身就是一整块合并转发时走这里（引用里的那份仍由 quoteHtml 处理）。
  // 一条都解不出来说明框架只给了 {"type":"forward"} 这种空标记——如实写清楚，
  // 不能凭空造内容（用户 m42531 图1 那条 13:16 就只剩一个空竖线）。
  function fwdHtml(s, media, slots){
    if(!s) return "";
    var h = forwardHtml(s, media, slots);
    if(h) return h;
    return "<div class='fwd'><div class='fwd-hd'>合并转发 · 内容未获取</div>" +
           "<div class='fwd-b'><div class='fwd-note'>框架只收到了「这里有一条合并转发」这个标记，" +
           "没有把里面的聊天记录取回来，所以这边看不到内容。</div></div></div>";
  }

  // 真解出卡片（"\[Forward" 开头）时才把媒体交给卡片；只拿到 {"type":"forward"}
  // 空标记时那张卡没有图片位，媒体必须留在卡外，不能把图弄丢。
  function isFwdBody(s){ return !!(s && String(s).trim().indexOf("[Forward") === 0); }

  // v3.36：引用块改走 quoteBodyHtml → richText，艾特美化只剩 richText 里那一处
  // （原来这里还有个只做 At 替换的 atHtml，两处正则要同步维护，已删）
  // [Json card {...}] 小程序 / 分享卡片：只留标题、描述、链接，别把整坨 JSON 铺在正文里
  function safeWebUrl(value){
    var s = String(value || "").trim();
    try { var u = new URL(s); return /^(https?:)$/.test(u.protocol) ? u.href : ""; }
    catch(e){ return ""; }
  }
  // 分享卡（v3.22 用户 m42531 图4/图5）：群里的小程序卡和窗口里的历史卡共用这一套——
  // 左边图标、右边标题与描述、脚下一行来源 + 「打开」，整张卡是一个链接。
  // 分享卡品牌图标（用户 m04325）：内嵌 data URI，离线、内网、深色主题都能显示，不依赖对方站点。
  // B 站 favicon 是单色蓝 TV，抠出形状染白压品牌粉底；小黑盒 favicon 是白底黑标，抠出深色标志放白牌。
  var ICON_BILI = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACgAAAAoCAYAAACM/rhtAAAEPklEQVR42sWYz4scVRDHP9XTM7srq8ksZDEb14NB8QcYf108SMwp4jF4WVBPHjyGQP4AL4FEyEk9CTkY9KAeXchlIxEv6iWCxI3Eg6wiBBOzJmQnM9NfD1avRds90zOzOz5ouvt1V9X31aufDyqGJKuYTxhzVNFWyQKoBGFmmaTHgDeAA8AV4EMzuylpxsw6I4KbMbOOpDbwFvAksAF8ZGZXc5l1mZmk/ZLe1b/jjqT3JB30f5qDVl7g1fTng87jTuB7xmVZLWDh+bCkb5zJ3cDwA0nLdbanwG/ZaVXg+a2kw3X4bX/0VT8v6Qtn0i9c5yTtGcS0AG6P0xT5SNKqy7JaACUlklqSZiUdL2gvZ7ol6bykvbl9SUoL10wAd95pIo9ci8ddVstlWxW4VFKjZP6YpKvOMJPUCyDPSVocsOBF/ycH13Mecp7HSmgaktL8Pc3BmVnPn58GngIeBjJgEbgveL35/AzwJtCU9BPQ8HmABOgDjwIr/p4Fepzni+50CfAL8IOZfZ87oZl1LYSUfcAR4DXgJeDBIT5VFFipSL+Gxc/fga+Az4CLZnZ9O25KmpN0SlI32Ec3XJnKR+bf71VcdWi7JXJPSZrb3mLgJHAivBO2Et+uQRoa9K0ObdRu6lg6wDuppCPAK25T9/yHxG2qwXRHBvQcy1FJl1I34od8pQ0HtwmsAj/6u3YJUL5LjwOvAg84hr5jWkHSuqTNYCsdj/hL01KbpCWX2Qn2uSlp3SR1HXUPaAI3gRfM7GdJs74alRQXGkNbRVoDGma2JekR4DugDXTd1HppcAyF+43cJsysG1JQw5kK6JuZ6hYfVbQhc9woYDCgmVbw/E/KcYa9sQxtCK3LKo2TaTVPk6TI5H435Ce8Nlw1s79yAUVtxrlBtPkCBuVgBeeQpD8kLeQB3O8tSW9L2vB/Nvy9lefPspxakzaXseCyIxYNAzjv97aka/79tt+veXVMLqwAsFWTdn4QwGQED2wXnKldIw9PSssoDVA/hJz+kBS2Y7SjAExDlG94zNx12qRGqYTn6DUXsNdDxkWfrwrak9DW9uLZ0Ao8I+lz//6ppGdDH5FU9cA1aGcn8eJWoV+Zd0bztdrEGrTB20sBpiNmg9sTZJKxaNO6Jw6+6jTk096IuXgYrY0CUBW5uDuBBrtDFqBRvDirq50dqVr/kZVVabAXSqFc1QvAn17VNHe5ok5Kskvetm5VVdTvS9o/xYr6gMuMFfUtSVdS4BJwNDTnLeB1oC1pfco9SSukwVvA1ynwiZ8kLHt0N29eVpj+yBxgC/gV+Dg1szVJF4DnvN2LCT6bErAkdJQt74kvmNlabqBngLOFslx1S6Id2OZi5XMWOA1QdTbzMrBvytt7HfiyeDZj8STJnw8Bh4AlYG7Ekmxcu7sL/AZcNrPLRUzxXK7B/zyKOKwk5cTgadPCFbSpmMX+BoxOh4zi61ruAAAAAElFTkSuQmCC";
  var ICON_HEYBOX = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAGAAAABgCAYAAADimHc4AAAJCElEQVR42u2dy28kVxXGv+/cqn5Ulz0zjt/jR4IgIpEiFGaAJIJFIFHYMIEN/0ASZccGxIY1QkIgwQqJbNhlxSIRIEUEgYSUl0iYQBZRIpI4M+N22z2TdHdV9aO67mHhbk/PTOZhx/0ou67UsheW+9b51XncW9+5RRzOII7n0FEb7rgaemhgJDP+UAYP6w8zow/ZIySzzWS6Snbnj8gTMg+YMA/I7vwRe0LmAWMeGYAJApCFnzGEfsmMP14IWQjKcsC+53ukvNWkKATpwMdkAEYbK3VmZma6VJr5tkip1ukEwQAIzQAMP+yo79+16rjm2XzRnC36HqIguAQg7s1d0grCpCD8EIB63tRddJ2zJAoi5kHPP3Ff0ffCKAg2UxyWmCIA03eJYx4mCZBtEc454pwtTk3dUyqe3AnD2pU0gkghABAAQXShsCKyIobf8Kb9kyF5Ae12M035Ia0ArpakRAyoCMy9JTf/UKlYSsKw0c8PMun5Ic0ABn5SQcQk8mKcBz3fv6+Y9xtR1ChPelg6CgD6vxOgBREbkVnjmq8XfH8lP+VvtRqNTycVxFEBcC2Ifn6grBvI2dLU9JRXyG9EUdQeLG0zAIcP4HoQHQIuyfvpOF8t5KbiKKpfBJBMSn4YNwAZEgBcmx/YAeAbx5wpTU3fmytO1VthvTIJ1dK4AAgAO7C3wyEB2PMG9vIDyUVj+DXPm17O56a3ms16bcAWetQB9L/LAijNLp5+3BBXOp1OdAtv+LwArg9LMUEVkXuMy7O+53tB0NgA0LmazEc3nDEYXubnlx+C45wTYqHluh8BuIzR7alKb0YRiQIc59z88tpZjbt/2tnZfK0/x8PSfk4CAOklPczOzt7r5LxzMHI/gIRAMzeu+KsQKBMQoZBzKOSeXlxee6TbiV6sVqvvDUQIm1YA7E0+mZubW4QpPOE48ghIF0ATureLOd4KS0ESMYAOjNzvFkpfmlssvoKk9dLOzs7WsMvWYQKwAHKzi+uPOaKPQTgDsNkzvkxcqasgiCZAcRx5FFL8yuzi+svVrY2XB/JDKgAQgD116tRazvOfEWKVNB2FhrtuPwrj666xyGRf37eXHxjSiO+K/eHC6dWHO1Hw3CeffPLxMDxBhvPo8IybK0w/JTQrIENV7Y7urldSGfeM7w2UuvvKD6raBRkKzUquMP0UcMYdRhgaCoClpU2XghlA2z3Dj6q0U1W6XXZ/37bxLxJN3la1eSVccG/dsZ+wJIC2KZhZWtpMDYD+9JMR19QKgAq0tM3oyqVLF7cvXfhN3Or8TrtahmqJpNMDsb8r2b2WVFZBY0moqrbvdXL58ta/APx3dnH9O64kV4sBwu4jLDLTBR1cgWwAtKtbG3/pxs1fdmP7D4UaAIVJ2BE9Dso427uDzc7OztZO5cIfuq3wV9Ym76uqO24Ix0ma2N9ikGq1+l7l0oVfQ/EuiMIB8sJxBKAEoCASEPYmn2T3jlbeRl3nAIiTOPobVONxLgydtJg/NknHpTq92v6zKiwFYISIWibp3ElYsta2ANpxXpeTlhheq1Q28surfzbEParS/gzvtaTNJ4oPa5XKxsBe1C3WDDr2p4EOUjS2Ny+8eNSaylMFYB+qBpsBGF44QtagkY0MQAYgGxmADEA2jl0VJFkZirFuLydH7YQvJyUd5QpAlpZWnlQxd0PRVtVrvIGkBZGnTT4qly++MCB51AzAraxL3ok21J5YWLgbxnyXQBFkwhvCES2gBsZ8+cTCwvlapfJhGronnfHGFDVkrjSgUr5piHET41qHXRHU++huDFGEtaCbGDergu5IP0LHuO5jAPIAugM9v5/5qLenVDB7+qIbP2ZPkp4BuCPZR4vCLy4ur//k1PzKA30p43Eqj2X8KYAxDb+Qd50fzS+uPjszc3qlB0GP4uEck7gQI4AWidg48nCu6Px0fmnlB8BS/8mXPcoQZCLOVe6r58iQBnljzPcXlt2fzc0tP3I1X4gdY0Wj6auCFA64z+etVzX7AQVzJp97ZmFl7Zu23fxjnLN+ASaPISqVb+KgFqpOmjyA5fJyDNVtQIsH1WQS7JKIBHKfky/92IP7NKndUepMd+euRahul8vL8TC+W4YT09+Mu53oObX6ARReT5OZHKBKEhARoKJUd2TlJZGQdKDw1OoH3U70HPDmUAA4wxLJVqvVMoCfzy+vPUryCSHmDqDJ7OWHkRl+d26qfqK6o6ovbW9+/PcBGYymaSUsAOz25scv+/7S+cKUedw1zrd6up7mhG2YKQjtzy1OkpdajeSvQVCuDvtAKGcU4tggKFeDAM+fWFh4NS/FJ0X0ARFCgXavV4zjXZEjZ63CWp5v2+YLtUrlo+ua9DTNe0F7bZ+9C/vt0tLamQT2e0KzTjJWaKe3jTDS0QGQU5u3qhfF8oXt8sZb1+VGe9QatQUAg6C2GTbqr5T86Y5Clwk5CSAGb9s1f/veAACa2NeiqHHlFqGDALSYy3VpcpWd8sXng6B2cWDlrUf9qAIDoBs2au/BK5w3qkbErPaUyvHnyA93CgAA2G63W1FQ3+gl2WNxVME1lRIAE4dhEAWNtws5730ITpKysrso3itbOSQA/f9tRtUVP4mnpextuEVRoxo26q97Ba8CcoXELETsTZTQhwUAx/24mhvOBw3DxoWwUXvVK011SZ4mOLWP/HAQAMgAXBsSBEAnDOrv5l3nPxC3QHKdu62mt8sPGYDDzA/NZrMeBrW33KL/oYjOElzS3SdpN8sPGYBDBmEAsBXWK2Gj/rrvTV0GsErKSUBtb7eVGYDRtJp2g6C+ETTqr5eKvkBkhWDpuvyQARhytSQAWmFYf8f1vXdEMQ3K6YH8IBmAEYFoBcGnYVB/o+AXL0FlTohFAFZBmzYAaXyHzF5+iIJgM2zU3iiWvLpQ1mDVV8U/o6ieBgBqUvwSn8H8EEdB8D9o8d8i+NTa5rvNZrOdhuviEXmLUn9LO0mZOFd5xF5jxTvoD84AZOMqgKxDBpMhzNLMFOMpImRStmWz19lmI+uSRPZG7ezVttk75TMPyDxhlPL2zAMm3AMybxhyU4dMQpfIcTX+Yd7VzAx9sPF/CcLgWGXd3jUAAAAASUVORK5CYII=";
  // 用户 m01693：闲鱼分享卡以前直接拿链接域名取 favicon，m.tb.cn 是淘宝的短链，画出来是淘宝图标。
  // 跟 B 站、小黑盒一样把官方图标内嵌，离线也能显示。
  var ICON_XIANYU = "data:image/png;base64,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";
  // 用户 m17212 图7：抖音卡换成官方图标（favicon 内嵌，内网/离线也能显示）。
  var ICON_DY = "data:image/png;base64,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";
  // 用户 m11820 图1/图2：位置分享卡（com.tencent.map / LocationShare）以前只显示
  // app 名「com.tencent.map」和带 [位置] 前缀的地址，看着像没美化；QQ 里它是一张
  // 带地图缩略图的位置卡。这里画成同款小卡：左边地图示意（纯装饰，不是真实地图），
  // 右边地址 + 「位置分享」，[位置]/[地图] 前缀剥掉。
  var LOC_MAP = "<svg class='qcmap' viewBox='0 0 44 44' width='44' height='44' aria-hidden='true'>" +
    "<rect width='44' height='44' rx='10' fill='#e8eef7'/>" +
    "<path d='M-6 33 L50 19' stroke='#cfdaea' stroke-width='7' fill='none'/>" +
    "<path d='M11 -6 L20 50' stroke='#d9e3ef' stroke-width='5' fill='none'/>" +
    "<circle cx='23' cy='22' r='7' fill='#e4574c'/><circle cx='23' cy='22' r='2.6' fill='#fff'/></svg>";
  function isLocCard(c){
    if(!c) return false;
    var s = [c.app, c.view, c.tag, c.title, c.bizsrc].join(" ");
    if(/tencent\.map|LocationShare|地图分享|位置分享/i.test(s)) return true;
    return /^\s*\[(?:位置|地图)\]\s*/.test(String(c.prompt || c.desc || ""));
  }
  function locCardHtml(c){
    var addr = String(c.prompt || c.desc || c.title || "").replace(/^\s*\[(?:位置|地图|位置分享)\]\s*/, "").trim();
    if(!addr) addr = "位置分享";
    return "<div class='qcard loc'><span class='qc-row'>" + LOC_MAP +
      "<span class='qc-main'><span class='qc-t'>" + esc(addr) + "</span>" +
      "<span class='qc-d'>位置分享</span></span></span></div>";
  }
  // 用户 m16357 图1：抖音分享文案整段铺成纯文本。这类消息是转发来的分享口令，真正的
  // 内容只有「链接前那段人话 + 话题标签」，剩下全是口令碎片。这里识别链接画成分享卡。
  var DY_SHARE = /https?:\/\/(?:v\.douyin\.com\/[A-Za-z0-9_\-]+\/?|(?:www\.)?iesdouyin\.com\/share\/[^\s"'<>]+)/i;
  // 分享口令碎片：4.35 dnd:/ 03/24 c@a.aA :1pm 这类「随机码 + 日期 + 时间」一律不是内容
  var DY_CODE = /^(?:\d{1,2}\/\d{1,2}|:\s*\d{1,2}\s*[ap]m|[A-Za-z]{1,5}:\/|[A-Za-z]@[A-Za-z]\.[A-Za-z]{1,4}|\d+(?:\.\d+)?|v@[A-Za-z]\.[A-Za-z]{1,4})\s*/i;
  // 分享话术只清两头：开头「复制打开抖音，看看【某某的作品】」，结尾「复制此链接，打开Dou音搜索，直接观看视频！」
  var DY_HEAD_FIX = /^复制[，,]?\s*(?:打开)?\s*(?:Dou音|抖音)(?:极速版|正式版|火山版)?[，,]?\s*(?:看看)?\s*(?:【[^】]{0,40}】)?\s*(?:的?作品)?[，,]?\s*/;
  // 用户 m00456：口令碎片跟在「！」后面（10/09 :4pm ztr:/ h@O.kc），行尾锚定就整段留下。
  // 只放宽「短语之后到行尾」这一段：允许非中文碎片，后面还有中文就说明是人话，照旧留着。
  var DY_TAIL_FIX = /复制[^。！!]{0,10}(?:Dou音|抖音)[^。！!]{0,40}[。！!]?[^\u4e00-\u9fa5]*$/;
  function douyinCard(t){
    var s = String(t == null ? "" : t);
    var m = s.match(DY_SHARE);
    if(!m) return null;
    var url = m[0];
    var head = s.slice(0, m.index);
    var tail = s.slice(m.index + url.length);
    // v3.69（用户 m21003 第 4 条）：话题标签（#猫咪 #哈基米 那行）不再单独画一行，
    // v3.70（用户 m21533 第 5 条）：话题标签连在一串（`# 哈基米# 猫咪`）时，旧写法按
    // 「空白 + #」找，第二个 # 前面没有空白就漏掉；这里直接按 # 切，并把标签留下来——
    // 整串除了标签没有人话时，拿第一个标签当标题（总比「抖音分享」强）。
    // 从标题里剥掉就完事——用户只想看那句人话。
    var dyTags = [];
    var title = head
      .replace(/#\s*([^\s#]+)/g, function(_, tg){ if(tg) dyTags.push(tg); return " "; })
      .replace(/\s+/g, " ")
      .trim();
    while(DY_CODE.test(title)) title = title.replace(DY_CODE, "");
    title = title.replace(DY_HEAD_FIX, "").replace(/^[，,、:：\s]+/, "").trim();
    // 整串除了标签没有人话时，拿第一个标签当标题（总比「抖音分享」强）
    if(!title) title = dyTags[0] || "抖音分享";
    // 链接后面只剩口令/时间戳（纯 ASCII、没有中文）就整段丢掉；带中文才留着当补充说明
    tail = tail.replace(DY_TAIL_FIX, "").trim();
    if(!/[\u4e00-\u9fa5]/.test(tail)) tail = "";
    tail = tail.replace(/(?:^|\s)#\s*[^\s#]+/g, " ").replace(/\s+/g, " ").trim();
    var host = url.replace(/^https?:\/\//i, "").replace(/\/+$/, "");
    return {
      // 悬停提示只放标题，别把整段口令塞进 title（用户 m16357 图1 的原文里有整串分享口令）
      html: "<a class='qcard dycard' href='" + esc(url) + "' target='_blank' rel='noopener noreferrer' title='" + esc(title) + "'>" +
        "<span class='qc-row'><span class='qc-logo dy' aria-hidden='true'><img src='" + ICON_DY + "' alt=''></span>" +
        "<span class='qc-main'><span class='qc-t'>" + esc(title) + "</span>" +
        "</span><span class='qc-go'>↗</span></span>" +
        "<span class='qc-ft'><span class='qc-src'>抖音</span>" +
        "<span class='qc-open'>打开</span></span></a>",
      rest: tail
    };
  }
  var CARD_SITE = { bili: "哔哩哔哩", heybox: "小黑盒", xianyu: "闲鱼" };
  function cardBrand(c, rawT, host){
    var s = rawT + " " + String(c.app || "") + " " + String(c.tag || "") + " " + String(c.bizsrc || "") + " " + host;
    if(/哔哩|bilibili|b23\.tv/i.test(s)) return "bili";
    if(/小黑盒|xiaoheihe|heybox/i.test(s)) return "heybox";
    if(/闲鱼|xianyu|goofish/i.test(s) || /(^|\.)(goofish\.com|xianyu\.com)$/i.test(host)) return "xianyu";
    return "";
  }
  // 用户 m02993 图9：直播间链接以前和视频卡长得一模一样——同款粉色 B 图标、
  // 同款卡片、标题还写着「哔哩哔哩视频」，点开才发现是直播间。这里统一判「这条
  // 链接是不是一场直播」，认出来就给 LIVE 图标 + 红色「直播」徽标 + 「进入直播间」，
  // 跟视频卡一眼分得开。认不出返回 null。
  function liveInfo(url){
    var u = String(url || ""), host = "", path = "", q = "";
    if(!u) return null;
    try {
      var o = new URL(u);
      host = String(o.hostname || "").replace(/^www\./, "").toLowerCase();
      path = String(o.pathname || "");
      q = String(o.search || "");
    } catch(e){ return null; }
    if(!host) return null;
    var m = null, seg = [];
    if(host === "live.bilibili.com"){
      m = path.match(/\/(\d{2,})/);
      return { brand: "bili", site: "哔哩哔哩", room: m ? m[1] : "" };
    }
    if(/(^|\.)bilibili\.com$/.test(host)){
      m = path.match(/^\/live\/(\d{2,})/);
      if(m) return { brand: "bili", site: "哔哩哔哩", room: m[1] };
      // 视频页 URL 后面挂个 room_id 参数不算直播（分享卡常带一堆无关参数）
      return null;
    }
    if(host === "b23.tv") return null;   // 短链看不出类型，交给 biliKind
    if(/(^|\.)youtube\.com$/.test(host) || host === "youtu.be"){
      m = path.match(/^\/live\/([\w-]{6,})/);
      if(m) return { brand: "yt", site: "YouTube", room: m[1] };
      return null;
    }
    if(/(^|\.)twitch\.tv$/.test(host)){
      seg = path.split("/").filter(Boolean);
      // twitch.tv/videos/123 是点播，twitch.tv/<频道名> 才是直播间
      if(!seg.length) return null;
      if(/^(videos|directory|settings|subscriptions|inventory|wallet|drops|friends|search|downloads)$/i.test(seg[0])) return null;
      return { brand: "twitch", site: "Twitch", room: seg[0] };
    }
    if(host === "live.douyin.com"){
      m = path.match(/\/(\d{3,})/);
      return { brand: "douyin", site: "抖音", room: m ? m[1] : "" };
    }
    if(/(^|\.)douyu\.com$/.test(host)){
      m = path.match(/^\/(?:topic\/)?([\w]{2,})/);
      return { brand: "douyu", site: "斗鱼", room: m ? m[1] : "" };
    }
    if(/(^|\.)huya\.com$/.test(host)){
      m = path.match(/\/([\w]{2,})/);
      return { brand: "huya", site: "虎牙", room: m ? m[1] : "" };
    }
    if(host === "live.kuaishou.com"){
      m = path.match(/\/u\/([\w-]{2,})/);
      return { brand: "kuaishou", site: "快手", room: m ? m[1] : "" };
    }
    return null;
  }
  // 卡片文字去噪：剥掉开头连续的方括号标签（[QQ小程序] / [分享] / [链接] …）。
  // 用户 m24651 图1：B 站卡的 desc 是「[分享]【15周年纪念】…」，标题已经写着同一句，
  // 以前只认「[QQ小程序]」这一个前缀，剥不掉就当成另一段文字又画一行，看着像标题重复。
  function cardCleanText(v){
    return String(v == null ? "" : v).replace(/^(?:\s*\[[^\[\]]{1,12}\])+\s*/, "").trim();
  }
  // 两张文字是不是同一句：去标签、去空白、去末尾省略号后比；一边是另一边的前缀也算
  // （QQ 常把 desc 截断成标题的短版，差一个「…」就永远不相等）。
  function cardSameText(a, b){
    var x = cardCleanText(a).replace(/\s+/g, "").replace(/[…⋯]+$|\.{3,}$/, "").toLowerCase();
    var y = cardCleanText(b).replace(/\s+/g, "").replace(/[…⋯]+$|\.{3,}$/, "").toLowerCase();
    if(!x || !y) return false;
    if(x === y) return true;
    return x.length >= 12 && y.indexOf(x) === 0 || y.length >= 12 && x.indexOf(y) === 0;
  }
  function cardHtml(c){
    if(!c) return "";
    // 位置卡走自己的画法：app 名不是给人看的标题，地址才是（用户 m11820 图1）
    if(isLocCard(c)) return locCardHtml(c);
    var rawT = String(c.title || c.app || "分享卡片").trim();
    var rawD = cardCleanText(c.desc || c.prompt || "");
    var rawU = safeWebUrl(c.qqdocurl) || safeWebUrl(c.jumpUrl) || safeWebUrl(c.url);
    var host = "";
    if(rawU){ try { host = new URL(rawU).hostname.replace(/^www\./, ""); } catch(e1){ host = ""; } }
    var brand = cardBrand(c, rawT, host);
    // 用户 m02993 图9：先按链接判直播（live.bilibili.com / youtube.com/live /
    // twitch.tv/<频道> …），判出来就按直播的品牌画，别再按标题文字猜成普通视频卡。
    var live = liveInfo(rawU);
    if(live && !brand) brand = live.brand;
    // 小黑盒卡片的 desc 固定是「下载小黑盒查看更多精彩内容」，不是正文，占一行反而干扰阅读
    if(brand === "heybox" && /^下载小黑盒查看更多精彩内容$/.test(rawD)) rawD = "";
    // 用户本轮：哔哩哔哩卡的标题位写着「哔哩哔哩」、视频名却被挤成下面一行小字。
    // QQ 的 ark 里 title 就是站名、desc 才是视频名（prompt 里那份带 [QQ小程序] 前缀）。
    // 站名脚下已经有一份了，标题位换成内容名，desc 不再重复一遍。
    var siteName = CARD_SITE[brand] || "";
    if(siteName && rawT.replace(/\s+/g, "") === siteName){
      var contentName = cardCleanText(c.desc || c.prompt || "");
      if(contentName && contentName !== rawT){ rawT = contentName; rawD = ""; }
    }
    // 只展示来源给出的图标；品牌站点用内置图标，缺图时用标题首字，不伪造商品图、价格或卖家。
    var logoUrl = safeWebUrl(c.icon || c.logo || c.app_icon);
    var iconSrc = brand === "bili" ? ICON_BILI : (brand === "heybox" ? ICON_HEYBOX
      : (brand === "xianyu" ? ICON_XIANYU : ""));
    var letter = brand === "xianyu" ? "闲" : (brand === "bili" ? "B" : (rawT ? rawT.slice(0, 1) : "↗"));
    var logo;
    if(iconSrc){
      logo = "<span class='qc-logo " + brand + "' aria-hidden='true'><img src='" + iconSrc + "' alt=''></span>";
    } else if(logoUrl){
      logo = "<img class='qc-logo' loading='lazy' src='" + esc(logoUrl) + "' alt='' referrerpolicy='no-referrer'>";
    } else if(/^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$/i.test(host)){
      // 用户 m33207 第 1 条：认不出的站点（hpoi.net 这种）只显示标题首字「万」太糊，
      // 拿站点自己的 favicon 顶上；取不到（离线 / 站点没图标）就露出底下那个首字，
      // 不会留一张裂图。协议相对写法跟随页面，http 站点也不会被 https 拦。
      logo = "<span class='qc-logo fav' aria-hidden='true'>" + esc(letter) +
             "<img src='//" + esc(host) + "/favicon.ico' alt='' referrerpolicy='no-referrer'" +
             " loading='lazy' onerror='this.remove()'></span>";
    } else {
      logo = "<span class='qc-logo" + (brand ? " " + brand : "") + "' aria-hidden='true'>" + esc(letter) + "</span>";
    }
    // 用户 m33207 第 2 条：认得出的站点一律写站名，别把 api.xiaoheihe.cn、b23.tv
    // 这种短链域名摆在脚下——标题已经是视频名/商品名，来源再写域名就看不懂了。
    // 认不出的站点才退回真实域名（总比不写强）。
    if(live){
      // 用户 m02993 图9：直播不该顶着视频站那套粉色 B 图标——换成红色 LIVE 牌
      logo = "<span class='qc-logo live' aria-hidden='true'>LIVE</span>";
    }
    var srcText = CARD_SITE[brand] || host || "链接";
    // 用户 m02993 图9：站名后面补一句内容类型（哔哩哔哩 · 直播 / · 视频），
    // 光看站名分不出点进去是直播间还是稿件。只认卡片自己带的 kind 字段。
    if(live){
      // 直播间写清是哪个房间，比「哔哩哔哩 · 直播」有用（YouTube 的直播间没有数字房号）
      srcText = (live.site || CARD_SITE[brand] || host || "链接") + "直播间" +
                (live.room ? " " + live.room : "");
    } else if(c.kind && CARD_SITE[brand] && String(c.kind) !== srcText){
      srcText += " · " + String(c.kind);
    }
    // 用户 m02993 图9：标题后面钉一个红色「直播」徽标，跟视频卡一眼分得开
    var tHtml = esc(rawT) + (live ? "<span class='qc-live'>直播</span>" : "");
    var inner = "<span class='qc-row'>" + logo + "<span class='qc-main'>" +
      "<span class='qc-t'>" + tHtml + "</span>" +
      (rawD && !cardSameText(rawD, rawT) ? "<span class='qc-d'>" + esc(rawD) + "</span>" : "") + "</span>" +
      (rawU ? "<span class='qc-go'>↗</span>" : "") + "</span>" +
      (rawU ? "<span class='qc-ft'><span class='qc-src'>" + esc(srcText) + "</span>" +
              "<span class='qc-open'>" + (live ? "进入直播间" : "打开") + "</span></span>" : "");
    // v4.87（用户 m11237 第 4 条）：标题是通用文案（裸链接没带标题）时挂上 data-lt，
    // 渲染完由 fillLinkTitles() 向后端要真实标题再补进来，抓不到就一直保持通用文案。
    var ltUrl = (c.lt && rawU) ? rawU : "";
    var ltAttr = ltUrl ? " data-lt='" + esc(ltUrl) + "'" : "";
    if(rawU) return "<a class='qcard" + (live ? " live" : "") + "'" + ltAttr + " href='" + esc(rawU) + "' target='_blank' rel='noopener noreferrer' title='" + esc(rawU) + "'>" + inner + "</a>";
    return "<div class='qcard" + (live ? " live" : "") + "'" + ltAttr + ">" + inner + "</div>";
  }
  // 用户发来的文件：正文里的 [File name: X, file_path: Y] 已经收进 meta.file，
  // 这里画成一张小卡，别让整条消息只剩一串路径（用户 m28965）
