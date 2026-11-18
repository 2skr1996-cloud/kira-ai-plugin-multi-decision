  function ppReferenceValue(value){
    if(value === null || value === undefined) return "";
    return typeof value === "string" ? value : JSON.stringify(value,null,2);
  }
  function ppEditorInner(s){
    if(!s.loaded){
      return "<div class='pm-note'>" + (s.error ? esc(s.error) : "读取默认模板与引用原文中…") + "</div>" +
        ppButton(s,"reload","重新读取",undefined,!!s.loading);
    }
    var layout = s.layout, textMode = layout.mode === "text";
    // 用户 m13212 图1：新增模块加进哪一段，用两个标签记着（重渲染后不回弹）。
    var zonePick = s.zonePick === "dynamic" ? "dynamic" : "system";
    return "<div class='pm-toolbar'>" +
      // v4.85（用户 m10425 第 2 条）：标题按卡头写（和右边「提示词预览」同一个字号与字重），
      // 下拉紧跟在标题后面，不再用「小灰字 + 表单」那套。
      "<label class='pm-tb-h' for='pp-mode-" + s.kind + "'>编辑方式</label>" +
      "<select class='dipt' id='pp-mode-" + s.kind + "' data-pm-field='mode'>" +
      "<option value='modules'" + (!textMode ? " selected" : "") + ">模块编排</option>" +
      "<option value='text'" + (textMode ? " selected" : "") + ">单段文本</option></select>" +
      // 2026-11-08（J）：底下那排按钮只留一个「恢复默认」，挪到「编辑方式」下拉右边。
      ppButton(s,"inherit",s.sid ? "恢复跟随全局" : "恢复默认",undefined,s.saving) +
      "<span class='pm-state' id='pp-state-" + s.kind + "' role='status' aria-live='polite'></span>" +
      ppButton(s,"save",s.saving ? "保存中…" : "保存此提示词",undefined,s.saving || !s.dirty) + "</div>" +
      // 2026-11-18（J，用户 m11554）：顶部只报「自己改过、不能替你换」的那几段名字；
      // 还在用旧版默认的段，提示挂在模块旁边（见 lib-1.js 的 ppModNoticeHtml）。
      (s.defNotice && s.defNotice.show ?
        "<div class='pm-updnotice'><span class='pm-updnotice-t'>" + esc(ppNoticeTopText(s)) +
        "</span><button type='button' class='pm-updnotice-x' title='知道了，不再提示' aria-label='关闭提示'" +
        " data-pm-kind='" + s.kind + "' data-pm-action='notice-off'>✕</button></div>" : "") +
      (textMode ? "<label class='pm-source-label'>完整 system<textarea class='darea' rows='18' data-pm-field='text'>" + esc(layout.text) + "</textarea></label>" :
        "<div class='pm-modules'>" + (layout.modules.map(function(m,i){ return ppModuleHtml(s,m,i); }).join("") ||
          "<div class='pm-note'>没有模块：保存后将明确使用空 system，不会自动补回默认模块。</div>") + "</div>") +
      // 用户 m04617 图4：新增这块也做成一张小卡（跟上面的模板卡一个风格），
      // 后面多一个下拉：这段是加进系统段，还是加进动态段。
      "<div class='pm-newcard'>" +
        "<div class='pm-newcard-head'><span class='pm-newcard-t'>新增模块</span>" +
          "<span class='pm-newcard-d' title='挑一个模板，选好拼到哪，点「＋ 新增」加进上面'>挑模板 → 选拼到哪 → 加进上面</span></div>" +
        "<div class='pm-newcard-row'>" +
          "<select class='dipt' data-pm-add aria-label='新增模块来源'>" +
            ppTypeOptions(s,{type:"text",key:""},true) + ppCloneOptions(s) + "</select>" +
          // 用户 m13212 图1：右边用标签代替下拉——系统段 / 动态段各一个，点一下就是加到那里。
          "<span class='pm-zone' role='group' aria-label='加到哪一段'>" +
            "<button type='button' class='pm-zone-b" + (zonePick === "dynamic" ? "" : " on") +
              "' data-pm-zone-pick='system' title='新模块拼进系统段'>系统段</button>" +
            "<button type='button' class='pm-zone-b" + (zonePick === "dynamic" ? " on" : "") +
              "' data-pm-zone-pick='dynamic' title='新模块拼进动态段'>动态段</button></span>" +
          // 真正读值的还是这个 select（藏起来），点标签只是改它的值，免得动到新增逻辑。
          "<select class='dipt pm-zone-src' data-pm-zone aria-label='加到哪一段' tabindex='-1' aria-hidden='true'>" +
            "<option value='system'" + (zonePick === "dynamic" ? "" : " selected") + ">加到系统段</option>" +
            "<option value='dynamic'" + (zonePick === "dynamic" ? " selected" : "") + ">加到动态段</option></select>" +
          ppButton(s,"add","＋ 新增") + "</div></div>";
  }
  function promptEditorBlockHtml(kind){
    var s = ppState(kind);
    // 用户 m18454：提示词还没读回来时这块只有一行「读取中」，下面的设置卡就会顶到最上面，
    // 等正文到了再被挤下去，看着像闪一下。加载态挂 pm-loading，CSS 让它先占满整栏高度。
    return "<div class='dcard pp-compact pm-editor" + (s.loaded ? "" : " pm-loading") + "' id='pp-edit-" + kind + "' data-pp-editor='" + kind + "'>" + ppEditorInner(s) + "</div>";
  }
  // 用户 m03247：工具条要当卡头用——它要是留在滚动层里，滚动条滑块就会跟它并排露在右上角。
  // 每次重画编辑器都是一套新 DOM，所以画完把新工具条搬进卡头，卡头里旧的整块丢掉。
  function ppHoistToolbar(s){
    var card = document.getElementById("pp-edit-" + s.kind);
    var head = document.getElementById("pp-head-" + s.kind);
    if(!card || !head) return;
    var bar = card.querySelector(".pm-toolbar");
    if(!bar) return;
    while(head.firstChild) head.removeChild(head.firstChild);
    head.appendChild(bar);
  }
  function ppRenderEditor(s){
    if(!ppCurrent(s)) return;
    var root = document.getElementById("pp-edit-" + s.kind);
    if(!root) return;
    var reference = root.querySelector("[data-pp-reference]");
    if(reference && typeof reference.open === "boolean") s.referenceOpen = reference.open;
    var focus = null, active = document.activeElement;
    if(active && root.contains && root.contains(active) && active.getAttribute){
      var field = active.getAttribute("data-pm-field"), index = active.getAttribute("data-pm-index");
      if(field === "label" || field === "content" || field === "text"){
        focus = {field:field,index:index,start:active.selectionStart,end:active.selectionEnd};
      }
    }
    root.innerHTML = ppEditorInner(s);
    // 用户 m18454：读回来之后去掉加载态，占位高度让给真正的正文。
    root.classList.toggle("pm-loading", !s.loaded);
    ppHoistToolbar(s);
    // 用户 m09506：JEV 卡整块重画完要把「实际请求地址」补上，
    // 否则刚打开页面那会儿这处是空的，要等动一下输入框才出现。
    // 用户 m09981：协议按当前这一页取，不再读全局的那个旧键。
    if(s.kind === "jev"){
      var pgNow = jevPage();
      var protoSel = document.getElementById("jev-proto");
      if(protoSel && protoSel.value !== jevPageProtocol(pgNow)) protoSel.value = jevPageProtocol(pgNow);
      jevPaintEndpoint();
    }
    if(focus){
      var selector = "[data-pm-field='" + focus.field + "']" + (focus.index === null ? "" : "[data-pm-index='" + focus.index + "']");
      var next = root.querySelector(selector);
      if(!next){
        var headEl = document.getElementById("pp-head-" + s.kind);
        if(headEl) next = headEl.querySelector(selector);
      }
      if(next && next.focus){
        try{ next.focus({preventScroll:true}); if(next.setSelectionRange) next.setSelectionRange(focus.start,focus.end); }catch(e){}
      }
    }
    ppPaintStatus(s);
  }
  // 用户 m15201：预览刷新时同一段文本会被重复写两遍（先按旧结果重画一次、拿到响应再画一次），
  // 每次写 textContent 都会让整段一千多字的预览重新排版——滚轮滚动时正好撞上就会顿一下。
  // 内容没变就不碰 DOM。
  function ppSetText(el, text){
    if(!el) return;
    var next = String(text == null ? "" : text);
    if(el.textContent !== next) el.textContent = next;
  }
  function ppPaintStatus(s){
    if(!ppCurrent(s)) return;
    var root = document.getElementById("pp-edit-" + s.kind);
    var node = document.getElementById("pp-state-" + s.kind);
    var bits = [s.sid ? "当前会话独立编排" : "全局编排"];
    if(s.loaded) bits.push(s.dirty ? "未保存草稿" : (s.inherited ? "跟随全局 / 自动默认" : "已保存"));
    if(s.saving) bits.push("保存中（仍可继续编辑）");
    if(s.error) bits.push(s.error);
    if(s.notice) bits.push(s.notice);
    ppSetText(node, bits.join(" · "));
    if(root){
      var owner = document.getElementById("pp-head-" + s.kind) || root;
      var saves = owner.querySelectorAll("[data-pm-action='save']");
      if(!saves.length) saves = root.querySelectorAll("[data-pm-action='save']");
      for(var si = 0; si < saves.length; si++){
        saves[si].disabled = s.saving || !s.dirty;
        ppSetText(saves[si], s.saving ? "保存中…" : "保存此提示词");
      }
    }
    var testBtn = document.getElementById(ppPrefix(s.kind) + "-test");
    if(testBtn) testBtn.disabled = !s.loaded || s.dirty || s.saving;
  }
  function ppCopy(kind, button){
    var s = ppState(kind), text = s && s.preview ? String(s.preview.system || "") : "";
    // 用户 m15201：按钮改成图标了，反馈也换成图标（✓ / ✕）+ 悬浮说明，不再把文字塞进去撑宽。
    var old = button.textContent, oldTitle = button.getAttribute("title") || "";
    function done(ok){
      button.textContent = ok ? "\u2713" : "\u2715";
      button.setAttribute("title", ok ? "已复制" : "没有可复制的预览");
      button.classList.toggle("ok", !!ok);
      setTimeout(function(){
        button.textContent = old;
        button.setAttribute("title", oldTitle);
        button.classList.remove("ok");
      }, 1200);
    }
    if(!text){ done(false); return; }
    function fallback(){
      // 局域网 IP 走 http，是非安全上下文，navigator.clipboard 通常不存在，只能退回 execCommand。
      try{
        var ta = document.createElement("textarea");
        ta.value = text; ta.setAttribute("readonly","");
        ta.style.position = "fixed"; ta.style.left = "-9999px"; ta.style.top = "0";
        document.body.appendChild(ta); ta.select();
        var ok = false;
        try{ ok = document.execCommand("copy"); }catch(e2){ ok = false; }
        document.body.removeChild(ta); done(ok);
      }catch(e){ done(false); }
    }
    if(navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(text).then(function(){ done(true); }, fallback);
    }else fallback();
  }
  function ppPaintPreview(s){
    if(!ppCurrent(s)) return;
    // 用户 m15846 图4：三个页签各有一块预览 DOM，但预热另一页时也会走到这里，
    // 把还没读到的空预览画进隐藏的那一页，切回去就一直是「等待读取模板…」。
    if(s.kind !== ppPane) return;
    var prefix = ppPrefix(s.kind), out = document.getElementById(prefix + "-prev-sys");
    if(!out) return;
    var preview = s.preview;
    ppSetText(out, preview ? String(preview.system || "") : "");
    var status = document.getElementById(prefix + "-prev-status");
    ppSetText(status, s.previewError ? s.previewError : (s.previewPending ? "正在用当前草稿重新组装…" :
      (preview ? "" : "等待读取模板…")));
    // 用户 m03408：不再把每段摊成一串折叠行（静态段上面已经整段预览过），改成
    // 「实时窗口」一条分界线，界线下面把动态段也拼成一段正文，跟 system 预览一个观感。
    var partList = preview ? (preview.parts || []) : [];
    var dynTexts = [], rtSeen = false;
    partList.forEach(function(p){
      var isRt = String(p.name || "") === "realtime" || String(p.type || "") === "realtime_marker";
      if(isRt){ rtSeen = true; return; }
      if(!p.dynamic) return;
      var body = String(p.text || "").replace(/\s+$/, "");
      if(body.trim()) dynTexts.push(body);
    });
    var dynText = dynTexts.join("\n\n");
    var hasDyn = dynTexts.length > 0;
    var div = document.getElementById(prefix + "-prev-div");
    if(div) div.hidden = !preview || !rtSeen || !hasDyn;
    var divSub = document.getElementById(prefix + "-prev-divs");
    ppSetText(divSub, s.rtInfo || "");
    var dynBox = document.getElementById(prefix + "-prev-dynbox");
    if(dynBox) dynBox.hidden = !preview || !hasDyn;
    var dynPre = document.getElementById(prefix + "-prev-dyn");
    ppSetText(dynPre, dynText);
    var warnings = document.getElementById(prefix + "-prev-warnings");
    ppSetText(warnings, preview ? (preview.warnings || []).map(function(w){ return String(w && w.message ? w.message : w); }).join("\n") : "");
    ppPaintStatus(s);
  }
  function ppPreview(s){
    if(!s.loaded) return Promise.resolve(null);
    var seq = ++s.seq, edit = s.editSeq;
    s.previewPending = true; s.previewError = ""; ppPaintPreview(s);
    return ppRequest("/prompt-preview", {kind:s.kind,sid:s.sid,layout:ppClone(s.layout)}).then(function(d){
      if(seq !== s.seq || edit !== s.editSeq) return null;
      var preview = d.preview;
      if(!preview || typeof preview.system !== "string") throw new Error("提示词预览响应缺少完整 system");
      if(typeof d.context_sid === "string") s.contextSid = d.context_sid;
      if(Array.isArray(d.catalog)) s.catalog = d.catalog;
      // 用户 m21698：plugin_segments 已并入 layout.modules，不再单独回传。
      // The payload wraps preview; never replace the draft/left editor with it.
      s.preview = preview; s.previewPending = false; ppPaintPreview(s); return preview;
    }).catch(function(err){
      if(seq !== s.seq || edit !== s.editSeq) return null;
      s.previewPending = false; s.previewError = "预览失败（上次结果仅供参考）：" + err.message; ppPaintPreview(s); return null;
    });
  }
  function ppScheduleStatePreview(s){
    clearTimeout(s.timer);
    // 用户 m23323：提示词页有点卡——每敲一个字就 300ms 后打一次预览，服务端要重排整份 system。
    // 拉到 600ms，输入时少一半往返。
    s.timer = setTimeout(function(){ s.timer = 0; ppPreview(s); },600);
  }
  // 用户 m04343 第 4 条：自写块/默认模板改正文后不重绘整个编辑器（会把光标弄丢、输入变卡），
  // 只就地打补丁：标题右边冒「自定义」小标、字数跟着变、来源下拉的括注也一起改。
  function ppPatchTweak(s, index){
    var box = document.querySelector("[data-pp-editor='" + s.kind + "']");
    var module = s.layout && s.layout.modules ? s.layout.modules[index] : null;
    if(!box || !module || String(module.type || "") !== "text") return;
    var sec = box.querySelector("[data-pm-module='" + index + "']");
    if(!sec) return;
    var head = sec.querySelector(".pm-module-head");
    var nm = String(module.label || "").trim(), defTpl = null;
    (s.catalog || []).forEach(function(c){
      if(!defTpl && c.skeleton && String(c.label || "") === nm) defTpl = c;
    });
    var tweaked = !!(defTpl && String(module.content || "") !== String(defTpl.text || ""));
    // 2026-11-18（J，用户 m12261）：标题后面的橙色「自定义」小标撤掉，来源下拉的括注已经说明了。
    var chars = String(String(module.content || "").length);
    var ch = head ? head.querySelector(".pm-chars") : null;
    if(ch){ ch.textContent = chars + " 字"; ch.title = "正文 " + chars + " 字符"; }
    var sum = sec.querySelector(".pm-sum");
    if(sum) sum.textContent = "正文 " + chars + " 字符（可编辑）";
    var sel = head ? head.querySelector(".pm-src") : null;
    if(sel && sel.options && sel.selectedIndex >= 0){
      sel.options[sel.selectedIndex].textContent = String(module.label || "自写文本") +
        "（" + (tweaked ? "自定义" : (defTpl ? "默认" : "自写")) + "）";
    }
  }
  function ppChanged(s, redraw){
    s.dirty = true; s.editSeq += 1; s.seq += 1; s.previewPending = true;
    s.error = ""; s.notice = ""; s.previewError = "";
    if(redraw) ppRenderEditor(s); else ppPaintStatus(s);
    ppPaintPreview(s); ppScheduleStatePreview(s);
  }
  // 2026-11-18（J）：提示记在「真正持有这份布局」的作用域上——继承全局的卡片跟全局共用一条，
  // 所以关掉一张，其它继承全局的卡片也要一起安静，不然切过去又冒出来。
  function ppClearSiblingNotices(s){
    var owner = s.inherited ? "" : String(s.sid || "");
    Object.keys(PP_STATES).forEach(function(k){
      var o = PP_STATES[k];
      if(!o || o.kind !== s.kind || !o.defNotice) return;
      if((o.inherited ? "" : String(o.sid || "")) !== owner) return;
      o.defNotice = {show:false, fp:String(o.defNotice.fp || ""), modules:[]};
    });
  }
  // 2026-11-18（J，用户 m11554）：顶部只报「自己改过、不能替你换」的那几段名字。
  function ppNoticeTopText(s){
    var list = (s.defNotice && s.defNotice.modules) || [];
    var names = [];
    for(var i = 0; i < list.length; i++){
      if(list[i].mode === "top") names.push("「" + String(list[i].label || "这一段") + "」");
    }
    if(!names.length) return "默认提示词更新了。";
    var shown = names.slice(0, 3).join("");
    if(names.length > 3) shown += " 等 " + names.length + " 段";
    return shown + "的默认提示词有更改。这几段你已经改过，不会被自动替换。";
  }
  // 2026-11-08（J）：关掉「默认提示词更新了」那条提示——只关这一版，换版会再提。
  // 2026-11-18（J）：带 moduleId 只关那一块（模块旁边的 ✕）；不带就整页一起关（顶上的 ✕）。
  function ppDismissNotice(s, moduleId){
    if(!s.loaded || s.noticeOffing) return Promise.resolve(false);
    var mid = String(moduleId || "");
    s.noticeOffing = true;
    return ppRequest("/prompt-notice",{kind:s.kind,sid:s.sid,module:mid}).then(function(d){
      s.noticeOffing = false;
      if(d && d.revision) s.revision = d.revision;
      s.defNotice = (d && d.default_notice) || {show:false, modules:[]};
      if(!mid) ppClearSiblingNotices(s);
      ppRenderEditor(s);
      return true;
    }).catch(function(err){
      s.noticeOffing = false; s.error = "关掉提示失败：" + err.message; ppRenderEditor(s);
      return false;
    });
  }
  // 2026-11-18（J，用户 m11554）：把这一段换成新版默认稿——只换这一块，其它段一个字不动。
  function ppAdoptDefault(s, index){
    var m = (s.layout && s.layout.modules || [])[index];
    if(!m) return false;
    var def = null, list = (s.defaultLayout && s.defaultLayout.modules) || [];
    for(var i = 0; i < list.length; i++){
      if(String(list[i].id || "") === String(m.id || "")){ def = list[i]; break; }
    }
    if(!def) return false;
    m.content = String(def.content || "");
    var mid = String(m.id || "");
    s.defNotice = s.defNotice || {show:false, modules:[]};
    s.defNotice.modules = (s.defNotice.modules || []).filter(function(x){ return String(x.id || "") !== mid; });
    s.defNotice.show = s.defNotice.modules.some(function(x){ return x.mode === "top"; });
    ppChanged(s, true);
    return true;
  }

  function ppSetMode(s, mode){
    var token = ++s.modeSeq, edit = s.editSeq;
    if(!s.loaded || (mode !== "modules" && mode !== "text") || s.layout.mode === mode) return Promise.resolve(false);
    if(mode === "text" && s.layout.text === ""){
      // Flatten the CURRENT server assembly, not a stale preview and not default text.
      return ppPreview(s).then(function(d){
        if(!d || token !== s.modeSeq || edit !== s.editSeq){ ppRenderEditor(s); return false; }
        s.layout.text = String(d.system || ""); s.layout.mode = "text"; ppChanged(s,true); return true;
      });
    }
    s.layout.mode = mode; ppChanged(s,true); return Promise.resolve(true);
  }
  function ppUpdateField(s, field, index, value){
    if(!s.loaded) return;
    if(field === "mode") return ppSetMode(s,value);
    if(field === "text"){ s.layout.text = String(value); ppChanged(s,false); return; }
    var module = s.layout.modules[index];
    if(!module) return;
    if(field === "content" && module.type === "text") module.content = String(value);
    else if(field === "label") module.label = String(value);
    else if(field === "enabled") module.enabled = !!value;
    else if(field === "type"){
      var selected;
      try{ selected = JSON.parse(String(value)); }catch(e){ return; }
      if(!Array.isArray(selected) || selected.length < 2 || !ppTypeAllowed(s,selected[0])) return;
      // 2026-11-18（J，用户 m12261）：下拉里那条「（默认）」= 把这一块正文换回它自己的默认稿，
      // 只动这一段，别的段和整页设置都不碰。
      if(selected.length > 2 && String(selected[2] || "") === "@default"){
        var dft = null;
        s.catalog.forEach(function(c){
          if(!dft && c.skeleton && String(c.label || "") === String(module.label || "")) dft = c;
        });
        if(!dft) return;
        module.type = "text";
        module.key = String(dft.key || "");
        module.content = String(dft.text || "");
        ppChanged(s, true);
        return;
      }
      // 用户 m08123：同一个「判断规则」有两份默认模板，换来源要真的换正文。
      // 只在当前正文仍是旧模板原文时才替换，改过的稿子一个字都不动。
      var prevDef = null;
      s.catalog.forEach(function(c){
        if(!prevDef && c.skeleton && String(c.label || "") === String(module.label || "")) prevDef = c;
      });
      if(selected[0] === "text" && module.type !== "text"){
        if(!ppCanConvert(s,module)){
          s.notice = "引用尚无可用正文，未渲染模板仅供只读参考；请新建自写模块后自行编辑。"; ppRenderEditor(s); return;
        }
        module.content = ppReferenceText(s,module);
      }
      module.type = selected[0]; module.key = String(selected[1] || "");
      // 用户 m16925：人格段换成空人格时自动取消启用；选了具体人格文件才自动勾上。
      // 回复页的空人格不是真的空（会回落到框架原文），所以始终保留启用。
      if(module.type === "persona"){
        module.enabled = !!(s.kind === "reply" || module.key);
      }
      // 用户 m01209 图1/图2：选了哪个模板，模块名就跟着改成那个模板名。
      var picked = ppRef(s, module);
      if(picked && picked.label){
        var nm = String(picked.label);
        // 同一类默认模板有好几条时（比如几个人格文件），把区分名也带上，标题才认得出是哪个。
        var dup = s.catalog.filter(function(c){ return String(c.label || "") === nm; }).length > 1;
        if(dup && picked.tag && (picked.group || "default") !== "custom") nm += " · " + String(picked.tag);
        module.label = nm;
      }
      if(selected[0] === "text" && picked && picked.skeleton && picked.text
         && prevDef && ppTextSame(module.content, prevDef.text)){
        module.content = String(picked.text);
      }
    }else return;
    if(field === "content" || field === "label") ppPatchTweak(s, index);
    ppChanged(s,field === "type");
  }
  // 用户 m01209 图2：要改默认模板，就先自动复制成「我的模板」，绝不原地覆盖默认。
  // 用户 m02159 图3：工具是勾出来的，勾了就立即存（不用跟着提示词保存走），
  // 存的位置和决策真正能调用的工具是同一份。
  function ppToggleTool(s, name, on){
    var cur = (s.toolsSelected || []).map(String), ix = cur.indexOf(name);
    if(on && ix < 0) cur.push(name);
    if(!on && ix >= 0) cur.splice(ix, 1);
    s.toolsSelected = cur;
    // 两页都有工具段，id 会重名：只改当前这一页的那一份。
    var scope = document.querySelector("[data-pp-editor='" + s.kind + "']");
    var sum = scope ? scope.querySelector(".pm-tools-sum") : null;
    // 用户 m20823：这里也按「清单里存在的」数，跟 ppToolBox 的读数保持一致。
    if(sum){
      var on2 = ppToolPicked(s), stale2 = ppToolStale(s);
      sum.textContent = "已选 " + on2.length + " / " + (s.toolChoices || []).length;
      if(stale2.length) sum.setAttribute("title", "另有 " + stale2.length + " 个已勾的工具当前不可用：" + stale2.join("、"));
      else sum.removeAttribute("title");
    }
    var box = scope ? scope.querySelector("[data-pm-tool='" + name + "']") : null;
    if(box && box.parentNode && box.parentNode.classList) box.parentNode.classList.toggle("on", !!on);
    // 用户 m11103：决策链和回复链各存各的，存的时候要带上这是哪一页。
    ppRequest("/prompt-tools", {kind: s.kind, names: cur}).then(function(d){
      if(Array.isArray(d.names)) s.toolsSelected = d.names.map(String);
      s.notice = (s.kind === "reply" ? "回复工具已存，下一次回复就用新的。" : "决策工具已存，下一次判断就用新的。");
      ppPaintStatus(s); ppPreview(s);
    }).catch(function(err){
      s.error = "工具清单没存上：" + err.message; ppPaintStatus(s);
    });
  }
  // 用户 m27528 第 5 条：比「正文有没有真改」——换行归一化后逐字比，
  // textarea 里被 HTML 解析吃掉的那个开头换行不算改。
  function ppTextSame(a, b){
    var norm = function(x){ return String(x == null ? "" : x).replace(/\r\n?/g, "\n").replace(/^\n/, ""); };
    return norm(a) === norm(b);
  }
  function ppPromote(s, index, text){
    var module = s.layout && s.layout.modules ? s.layout.modules[index] : null;
    if(!module || s.promoting) return Promise.resolve(false);
    var body = typeof text === "string" ? text : ppReferenceText(s, module);
    if(!body){ s.notice = "这一段现在是空的，先有正文再转。"; ppRenderEditor(s); ppPaintStatus(s); return Promise.resolve(false); }
    s.promoting = true;
    // 用户 m28586 第 3 条：自动复制出来的名字固定成「原名·复制（自写）」。
    var name = String(module.label || "这一段") + "·复制（自写）";
    return ppRequest("/prompt-templates", {action:"save", name:name, content:body}).then(function(d){
      s.promoting = false;
      var item = (d && d.item) || {};
      if(!item.id) throw new Error("模板库没有返回新模板 id");
      module.type = "custom"; module.key = String(item.id);
      module.label = String(item.name || name); module.content = body;
      s.catalog.push({type:"custom", key:String(item.id), label:module.label, text:body,
                      source:"自定义模板", available:true, group:"custom", tag:"自定义"});
      ppChanged(s, true);
      s.notice = "已存成自定义「" + module.label + "」，默认模板没被改。";
      ppRenderEditor(s);
      return true;
    }).catch(function(err){
      s.promoting = false;
      s.error = "转自定义模板失败（原文没动）：" + err.message;
      ppRenderEditor(s); ppPaintStatus(s);
      return false;
    });
  }
  function promptPrevBlockHtml(kind){
    var prefix = ppPrefix(kind);
    return "<div class='dcard pp-prev' data-pp-prev='" + kind + "'>" +
      "<div class='pp-prev-h'><h4>提示词预览</h4><span class='pp-h4s'>当前草稿 · 自动刷新</span>" +
      "<span class='pp-prev-acts'>" +
        // 用户 m15201：复制 / 放大改成两个图标（⧉ / ⤢），用途写在悬浮说明里。
        "<button class='pm-ico' id='" + prefix + "-copy' type='button' data-pp-copy='" + kind + "' title='复制这份 system'>\u29c9</button>" +
        "<button class='pm-ico' id='" + prefix + "-expand' type='button' data-pp-expand='" + kind + "' title='放大看'>\u2922</button>" +
      "</span></div>" +
      "<div class='pp-prev-box sys' id='" + prefix + "-prev-sysbox'>" +
        "<div class='pp-prev-cap'><span>系统提示词</span><button class='pp-cap-tg' type='button' title='折叠'>\u25be</button></div>" +
        "<pre class='dpre tall pm-system' id='" + prefix + "-prev-sys'></pre></div>" +
      "<div class='pm-note' id='" + prefix + "-prev-status' role='status' aria-live='polite'>等待读取模板…</div>" +
      "<div class='pm-warning' id='" + prefix + "-prev-warnings' role='status'></div>" +
      "<div class='pp-prev-div' id='" + prefix + "-prev-div' hidden>" +
        "<span class='pp-prev-divn'>实时窗口</span><span class='pp-prev-divl'></span>" +
        "<span class='pp-prev-divs' id='" + prefix + "-prev-divs'></span></div>" +
      "<div class='pp-prev-box dyn' id='" + prefix + "-prev-dynbox' hidden>" +
        "<div class='pp-prev-cap'><span>动态注入部分</span><button class='pp-cap-tg' type='button' title='折叠'>\u25be</button></div>" +
        "<pre class='dpre pm-dyn' id='" + prefix + "-prev-dyn'></pre></div>" +
      // 用户 m08755 第 5 条：JEV 页也要「测试」，用实时窗那份真实聊天记录试跑。
      (kind === "decision" || kind === "jev" || kind === "reply"
        ? "<div class='pm-toolbar'><button class='dbtn ghost' id='" + prefix + "-test' type='button' disabled>测试</button>" +
          "<button class='dbtn ghost' id='" + prefix + "-test-clear' type='button' hidden>清空结果</button>" +
          "<span class='dtip' style='margin:0'>只测已保存的提示词，真调一次" +
          (kind === "jev" ? " JEV 判断" : (kind === "reply" ? "回复模型" : "决策模型")) +
          (kind === "decision" ? "。" : (kind === "jev" ? "，不写记录、不发言、不转交。" : "，不写记录、不发言。")) +
          "</span></div>" +
          "<div class='pp-test' id='" + prefix + "-test-box' hidden><h4>试跑结果</h4>" +
          "<div class='pp-test-meta' id='" + prefix + "-test-meta'></div>" +
          "<div class='pp-test-body' id='" + prefix + "-test-out'></div></div>"
        : "") + "</div>";
  }
  // v4.84（用户 m09179 第 7 条）：模板库那套「新建 / 编辑 / 删除」的界面整块撤掉。
  // 自定义页只做一件事：把三个提示词页里改过或自己写的段按来源列出来，就地改正文。
  // 模板库本身还在（改默认模板时自动存一份「原名·复制（自写）」要用它），只是不再单独展示。
  var PP_SELF_NOTICE = "";
  // 用户 m27528 第 5 条：两块提示词里的自写模块，也要在这一页看得到、改得动——
  // 改这里的正文，提示词页那一份跟着变；反过来提示词页改完，这里刷新就是新的。
  // 用户 m28586 第 2 条：默认模板（决策那边「什么时候接 / 什么时候别接 / 节奏与分寸 / 输出规则」）
  // 不划进自定义；只有正文真改过的那一块、或者本来就是自己写的，才算「自定义」。
  function ppDefTplOf(s, m){
    var nm = String((m && m.label) || "").trim(), hit = null;
    if(!nm || !s || !s.catalog) return null;
    s.catalog.forEach(function(c){ if(!hit && c.skeleton && String(c.label || "") === nm) hit = c; });
    return hit;
  }
  function ppModIsCustom(s, m){
    var t = String((m && m.type) || "");
    if(t === "custom") return true;
    if(t !== "text") return false;
    var def = ppDefTplOf(s, m);
    if(!def) return true;
    return String(m.content || "") !== String(def.text || "");
  }
  function ppSelfModRows(){
    var rows = [];
    ["decision", "jev", "reply"].forEach(function(k){
      var st = ppState(k);
      if(!st || !st.loaded || !st.layout || !st.layout.modules) return;
      st.layout.modules.forEach(function(m, i){
        var t = String(m.type || "");
        if(t !== "text" && t !== "custom") return;
        if(!ppModIsCustom(st, m)) return;
        rows.push({kind:k, index:i, name:String(m.label || "自写文本"), body:String(m.content || ""), on:m.enabled !== false});
      });
    });
    return rows;
  }
  // v4.84（用户 m09179 第 7 条）：三竖列，一列一页（决策 / JEV / 回复）。
  // 不再分「改过的默认模板」和「自己写的」两种——只要正文跟默认模板不一样、或者本来就是自写的，
  // 就自动出现在对应那一列里，不用另外存模板。
  var PP_SELF_COLS = [["decision", "决策提示词"], ["jev", "JEV 提示词"], ["reply", "回复提示词"]];
  function ppSelfCardHtml(r){
    // 用户 m18121：标题已经由上面那张小卡带着了，这里不再重复一遍，正文框直接铺满。
    return "<div class='ptpl-card' data-self-card='" + r.kind + "#" + r.index + "'>" +
      "<textarea class='darea ptpl-self' rows='4' aria-label='" + esc(r.name) + "（自写模块正文）'" +
      " data-self-field='content' data-self-kind='" + r.kind + "' data-self-index='" + r.index + "'>\n" + esc(r.body) + "</textarea>" +
      "<div class='ptpl-acts'><button type='button' class='dmini' data-self-act='save' data-self-kind='" + r.kind +
      "' data-self-index='" + r.index + "'>保存这一段</button>" +
      "<button type='button' class='dmini' data-self-act='jump' data-self-kind='" + r.kind +
      "' data-self-index='" + r.index + "'>去提示词页看</button></div></div>";
  }
  // 用户 m18121：三列里先只摆标题小卡，点开谁就在那一列下面展开编辑框（编辑框铺到底部）。
  var ppTplSel = {decision:"", jev:"", reply:""};
  function ppSelfModHtml(){
    var rows = ppSelfModRows();
    var head = "<div class='pm-note ptpl-note'>只列改过或自己写的段，默认模板不列。正文和提示词页是同一份，" +
      "点「保存这一段」那边立刻同步；改默认模板会先复制一份「原名·复制（自写）」，原件不动。</div>";
    var cols = PP_SELF_COLS.map(function(p){
      var k = p[0], list = rows.filter(function(r){ return r.kind === k; });
      var selKey = ppTplSel[k] || "", sel = null;
      list.forEach(function(r){ if(!sel && (r.kind + "#" + r.index) === selKey) sel = r; });
      if(!sel) ppTplSel[k] = "";
      var mini = list.map(function(r){
        var key = r.kind + "#" + r.index, on = !!(sel && key === selKey);
        return "<button type='button' class='ptpl-mini" + (on ? " on" : "") + "' data-self-pick='" + key +
          "' aria-pressed='" + (on ? "true" : "false") + "'><b>" + esc(r.name) + "</b>" +
          "<span class='ptpl-n'>第 " + (r.index + 1) + " 段 · " + r.body.length + " 字符" + (r.on ? "" : " · 已停用") + "</span></button>";
      }).join("");
      return "<div class='ptpl-col'><div class='ptpl-colh'>" + p[1] +
        "<span class='dhead-x'>" + list.length + " 个</span></div>" +
        "<div class='ptpl-list'>" + (list.length ? mini : "<div class='ptpl-empty'>这一页还没有改过或自己写的段。</div>") + "</div>" +
        (sel ? "<div class='ptpl-detail'>" + ppSelfCardHtml(sel) + "</div>" : "") + "</div>";
    }).join("");
    return head + "<div class='ptpl-cols'>" + cols + "</div>";
  }
  function ppTplPageHtml(){
    // v4.85（用户 m10425 第 1 条）：这页不再挂「自定义 N 个」标题，说明并成一段放在最上面。
    return "<div class='dcard pp-compact' id='pp-tpl'>" +
      (PP_SELF_NOTICE ? "<div class='pm-note'>" + esc(PP_SELF_NOTICE) + "</div>" : "") +
      ppSelfModHtml() + "</div>";
  }
  function ppTplRender(){
    var box = document.getElementById("pp-tpl");
    if(!box) return;
    var wrap = document.createElement("div");
    wrap.innerHTML = ppTplPageHtml();
    var next = wrap.firstChild;
    if(next && box.parentNode) box.parentNode.replaceChild(next, box);
  }
  function promptPageBlockHtml(){
    var pane = (ppPane === "reply" || ppPane === "jev" || ppPane === "templates") ? ppPane : "decision";
    return "<div class='pp'><div class='pp-tabs'>" +
      "<button type='button' class='pp-tab" + (pane === "decision" ? " on" : "") + "' data-pp='decision'>决策提示词<span class='pp-sub'>判这一轮回不回</span></button>" +
      "<button type='button' class='pp-tab" + (pane === "jev" ? " on" : "") + "' data-pp='jev'>JEV 提示词<span class='pp-sub'>专用模型判回不回</span></button>" +
      "<button type='button' class='pp-tab" + (pane === "reply" ? " on" : "") + "' data-pp='reply'>回复提示词<span class='pp-sub'>真正开口说话</span></button>" +
      "<button type='button' class='pp-tab" + (pane === "templates" ? " on" : "") + "' data-pp='templates'>自定义<span class='pp-sub'>改过的和自写的</span></button>" +
      "</div><div class='pp-pane' data-pane='decision'" + (pane === "decision" ? "" : " hidden") + "><div class='pp-left'>" +
      "<div class='pp-left-head' id='pp-head-decision' data-pp-editor='decision'></div><div class='pp-left-body'>" +
      promptEditorBlockHtml("decision") + dmodelBlockHtml() + "</div></div><div class='pp-right'>" + promptPrevBlockHtml("decision") + "</div></div>" +
      "<div class='pp-pane' data-pane='jev'" + (pane === "jev" ? "" : " hidden") + "><div class='pp-left'>" +
      "<div class='pp-left-head' id='pp-head-jev' data-pp-editor='jev'></div><div class='pp-left-body'>" +
      promptEditorBlockHtml("jev") + jevBlockHtml() + "</div></div><div class='pp-right'>" + promptPrevBlockHtml("jev") + "</div></div>" +
      "<div class='pp-pane' data-pane='reply'" + (pane === "reply" ? "" : " hidden") + "><div class='pp-left'>" +
      "<div class='pp-left-head' id='pp-head-reply' data-pp-editor='reply'></div><div class='pp-left-body'>" +
      promptEditorBlockHtml("reply") + replyToolBlockHtml() + "</div></div><div class='pp-right'>" + promptPrevBlockHtml("reply") + "</div></div>" +
      "<div class='pp-pane pp-pane-wide' data-pane='templates'" + (pane === "templates" ? "" : " hidden") + ">" +
      ppTplPageHtml() + "</div></div>";
  }
  // Switching panes/scope keeps canonical drafts in PP_STATES, never serializes them to storage.
  // 用户 m27528 第 5 条：自定义页要同时列出各提示词页里的自写模块（v4.84 起是决策 / JEV / 回复三页）。
  // 两个编辑器的数据是点开各自页签才懒加载的——只开过决策页就只列到决策那几段，
  // 看着像「回复那边的自写模块丢了」。进这一页时把缺的那块补上，回来再画一遍。
  function ppSyncPairLabel(){
    Array.prototype.forEach.call(document.querySelectorAll("#settings .jev-col.dcol .drows"), function(g){
      var lb = g.querySelector(".drow > .dlb");
      if(!lb) return;
      var w = Math.round(lb.getBoundingClientRect().width * 100) / 100;
      if(w > 0) g.style.setProperty("--dlbw", w + "px");
    });
  }
  // v4.10：提示词页外层 .panel 带着 overflow:hidden，会把右栏的 position:sticky 一起废掉（右栏不再吸顶）。
  // 只给这一页的 panel 打标记，让它单独改成 overflow:visible，别动全局那条规则。
  function ppMarkPanel(){
    var n = document.querySelector(".pp");
    var panel = n && n.closest ? n.closest(".panel") : null;
    if(panel) panel.classList.add("pp-panel");
  }
  // v4.10：整页不滚，改成左栏、右栏各自滚（页签常驻），高度按窗口现算，省得右栏跟内容一起跑掉。
  function ppLayout(){
    ppMarkPanel();
    ppSyncPairLabel();
    var pp = document.querySelector(".pp"), tabs = document.querySelector(".pp-tabs");
    if(!pp || !tabs) return;
    var top = tabs.getBoundingClientRect().top;
    if(top < 60){ window.scrollTo(0,0); top = tabs.getBoundingClientRect().top; }
    var h = Math.round(window.innerHeight - Math.max(top, 0) - 14);
    pp.style.height = Math.max(420, h) + "px";
  }
  // 用户 m28586 第 4 条：另一页的自定义模块要能出现在「＋ 新增」下拉里，所以进提示词页时
  // 顺手把另一块也读一次；读不到就算了，不影响当前这一页。
  function dcRefresh(){
    if(!DOPT) return;
    // m01209 图3：决策侧的人格下拉已撤，这里不再补填。
    var ms = document.getElementById("dec-model");
    if(ms) ms.innerHTML = dModelOpts(ms.value || String(dVal("decision_model", "")), "跟随默认模型");
    var fs = document.getElementById("dec-fb");
    if(fs) fs.innerHTML = dModelOpts(fs.value || String(dVal("decision_fallback_model", "")), "未选择模型");
    // 用户 m17507：回复设置里的「回复模型 / 备用模型」以前只在页面重画时填一次，
    // 模型清单比页面晚到就永远只剩「跟随默认模型 / 未选择模型」两个空选项，选不了。
    var rm = document.getElementById("rep-model");
    if(rm) rm.innerHTML = dModelOpts(rm.value || String(dVal("reply_model", "")), "跟随默认模型");
    var rf = document.getElementById("rep-fb");
    if(rf) rf.innerHTML = dModelOpts(rf.value || String(dVal("reply_fallback_model", "")), "未选择模型");
    var tb = document.getElementById("dec-tools-box");
    if(tb) tb.innerHTML = dToolsInner();
    var rh = document.getElementById("dec-rounds-hint");
    if(rh) rh.textContent = String(Math.max(0, dVal("reply_tool_rounds", 5)));
  }

  function loadDecisionOptions(){
    get(API + "/decision-options" + (scopeSid ? "?sid=" + encodeURIComponent(scopeSid) : ""))
      .then(function(d){
        if(!d || !d.ok) return;
        DOPT = d;
        dcRefresh();
      })
      .catch(function(){ /* 拿不到就按空清单画，不影响别的页 */ });
  }

  function dScopeSidForDecision(){
    if(scopeSid) return scopeSid;
    var promptState = ppState("decision");
    if(promptState.contextSid) return promptState.contextSid;
    try{
      if(sessCache && sessCache.length){
        var a = sessCache[0] || {};
        return typeof a === "string" ? a : String(a.sid || a.session_id || a.id || "");
      }
    }catch(e){}
    return "";
  }



  // 用户 m16904 第 2、3 条：试跑结果以前是一坨纯文本，模型还显示成内部引用
  // （52acaec3deeb:c725ed8dfa5f4e4e91343d4c1dd6ed76 这种）。现在统一成
  // 「一行信息胶囊 + 分段块」，模型名按设置页下拉里的名字显示。
  function ppModelLabel(ref){
    var v = String(ref || "");
    if(!v) return "";
    var hit = "";
    var sels = document.querySelectorAll("select[data-k$='_model']");
    for(var i = 0; i < sels.length && !hit; i++){
      var op = sels[i].options || [];
      for(var j = 0; j < op.length; j++){
        if(op[j].value === v){ hit = String(op[j].text || ""); break; }
      }
    }
    return hit || v;
  }
  function ppTestMeta(meta){
    var h = "";
    (meta || []).forEach(function(it){
      if(!it) return;
      var k = String(it[0] || "");
      var v = (it[1] === undefined || it[1] === null) ? "" : String(it[1]);
      if(!k || !v) return;
      h += "<span class='pp-test-chip'" + (it[2] ? (" title='" + esc(it[2]) + "'") : "") + "><b>" + esc(k) + "</b><i>" + esc(v) + "</i></span>";
    });
    return h;
  }
  // 用户 m17185：分段块支持挂角标（标签正常 / 不合规），也支持直接给 HTML（回复气泡）。
  function ppTestSec(title, text, tone, extra){
    var ex = extra || {};
    var t = (text === undefined || text === null) ? "" : String(text);
    if(!ex.html && !t) return "";
    var body = ex.html ? ex.html : ("<div class='pp-test-sect'>" + esc(t) + "</div>");
    return "<div class='pp-test-sec" + (tone ? (" " + tone) : "") + "'>" +
      "<div class='pp-test-sech'>" + esc(title) +
      (ex.badge ? ("<span class='pp-test-badge " + (ex.badge.tone || "") + "'>" + esc(ex.badge.text) + "</span>") : "") +
      "</div>" + body + "</div>";
  }
  // 用户 m17185：思考过程里的空行去掉，省点高度。
  function ppTight(text){
    return String(text == null ? "" : text).split(/\r?\n/)
      .filter(function(l){ return l.trim() !== ""; }).join("\n");
  }
  // 用户 m17185：回复按 <msg><text>…</text></msg> 拆成气泡。标签外面还有字、
  // 或者某个 msg 里没有 text，都算不合规——不合规就不包装，原样带标签显示。
  function ppParseReply(raw){
    var t = String(raw == null ? "" : raw).trim();
    var out = { ok: false, bubbles: [], raw: t };
    if(!t) return out;
    var re = /<msg>([\s\S]*?)<\/msg>/g, m, last = 0, outside = "";
    while((m = re.exec(t))){
      if(m.index > last) outside += t.slice(last, m.index);
      last = re.lastIndex;
      var inner = m[1];
      out.bubbles.push({ body: inner.replace(/<\/?text>/g, "").trim(),
                         hasText: /<text>[\s\S]*?<\/text>/.test(inner) });
    }
    outside += t.slice(last);
    out.ok = out.bubbles.length > 0 && outside.trim() === "" &&
      out.bubbles.every(function(b){ return b.hasText && b.body !== ""; });
    return out;
  }
  function ppTestClear(prefix){
    var box = document.getElementById(prefix + "-test-box");
    var clr = document.getElementById(prefix + "-test-clear");
    var mEl = document.getElementById(prefix + "-test-meta");
    var oEl = document.getElementById(prefix + "-test-out");
    if(box) box.hidden = true;
    if(mEl){ mEl.innerHTML = ""; mEl.hidden = true; }
    if(oEl) oEl.innerHTML = "";
    if(clr) clr.hidden = true;
  }
  function ppTestShow(prefix, meta, secs, emptyText){
    var box = document.getElementById(prefix + "-test-box");
    var mEl = document.getElementById(prefix + "-test-meta");
    var oEl = document.getElementById(prefix + "-test-out");
    var clr = document.getElementById(prefix + "-test-clear");
    if(!box || !oEl) return;
    box.hidden = false;
    if(clr) clr.hidden = false;
    if(mEl){ mEl.innerHTML = ppTestMeta(meta); mEl.hidden = !mEl.innerHTML; }
    var h = "";
    (secs || []).forEach(function(s){ if(s) h += ppTestSec(s[0], s[1], s[2], s[3]); });
    oEl.innerHTML = h || ("<div class='pp-test-empty'>" + esc(emptyText || "没有内容") + "</div>");
  }
  function ppTestWait(prefix, text){
    var box = document.getElementById(prefix + "-test-box");
    var mEl = document.getElementById(prefix + "-test-meta");
    var oEl = document.getElementById(prefix + "-test-out");
    var clr = document.getElementById(prefix + "-test-clear");
    if(!box || !oEl) return;
    box.hidden = false;
    if(clr) clr.hidden = false;
    if(mEl){ mEl.innerHTML = ""; mEl.hidden = true; }
    oEl.innerHTML = "<div class='pp-test-empty'>" + esc(text) + "</div>";
  }
  function ppTestFail(prefix, err){
    ppTestShow(prefix, [], [["错误", (err && err.message ? err.message : String(err)), "bad"]]);
  }

  function dcTest(){
    var sid = dScopeSidForDecision();
    ppTestWait("dec", "跑一次决策…（超时就按失败算，别关页面）");
    post(API + "/decision-test" + (sid ? "?sid=" + encodeURIComponent(sid) : ""))
      .then(function(d){
        var r = (d && d.result) || {};
        if(!d || !d.ok){ ppTestShow("dec", [], [["错误", (d && d.error) || "接口没回数据", "bad"]]); return; }
        var meta = [];
        meta.push(["结论", r.failed ? "失败（按「决策失败时」的设置处理）" : (r.reply ? "建议回复" : "建议不回")]);
        if(r.model) meta.push(["模型", ppModelLabel(r.model) + (r.via === "fallback" ? "（备用）" : "")]);
        meta.push(["尝试", (r.attempts || 1) + " 次"]);
        if(r.elapsed) meta.push(["用时", r.elapsed + " 秒"]);
        if(r.tools_used && r.tools_used.length) meta.push(["用了工具", r.tools_used.join("、")]);
        var secs = [];
        if(r.reason) secs.push(["理由", r.reason]);
        if(r.error) secs.push(["错误", r.error, "bad"]);
        if(r.reasoning) secs.push(["思考过程", ppTight(String(r.reasoning).slice(0, 4000))]);
        if(r.raw) secs.push(["原始返回", String(r.raw).slice(0, 1200)]);
        ppTestShow("dec", meta, secs, "模型没给理由");
      })
      .catch(function(err){ ppTestFail("dec", err); });
  }

  // 用户 m08755 第 5 条：JEV 页的「测试」用实时窗那份真实聊天记录试跑，不写记录、不发言、不转交。
  function jevTest(){
    var s = ppState("jev");
    var sid = ppRtSid(s);
    ppTestWait("jev", "跑一次 JEV 判断…（会真调一次，超时按失败算，别关页面）");
    post(API + "/jev-test" + (sid ? "?sid=" + encodeURIComponent(sid) : ""))
      .then(function(d){
        if(!d || !d.ok){ ppTestShow("jev", [], [["错误", (d && d.error) || "接口没回数据", "bad"]]); return; }
        var r = d.result || {};
        var j = r.jev || {};
        var meta = [];
        meta.push(["会话", sid ? sidLabel(sid) : "（没有可用会话，用了空窗口）"]);
        var apiBase = String(j.api_base || "");
        var apiFull = j.backend === "custom"
          ? ("自定义第三方 · " + apiBase + (j.api_model ? (" · " + j.api_model) : ""))
          : "宿主 decision 模型";
        // 用户 m16904：接口那格以前把整条 base URL 铺出来，一行放不下就换行。
        // 现在只留「自定义第三方」，完整地址挂在悬浮说明里；模型名单独一格。
        meta.push(["接口", (j.backend === "custom" && j.model) ? "自定义第三方" : apiFull, apiFull]);
        var prob = (j.prob_reply === null || j.prob_reply === undefined) ? null : Number(j.prob_reply);
        if(prob === null && j.prob_reply_display) meta.push(["概率", String(j.prob_reply_display)]);
        else if(prob !== null && isFinite(prob)){
          meta.push(["概率", (j.prob_reply_display || (Math.round(prob * 100) + "%")) +
                     "（区间 " + Math.round(Number(j.lower || 0) * 100) + "%～" + Math.round(Number(j.upper || 0) * 100) + "%）"]);
        }
        meta.push(["结论", r.failed ? "失败（按「决策失败时」的设置处理）" : (r.reply ? "建议回复" : "建议不回")]);
        var zoneText = {reply:"回复区", no_reply:"不回复区", uncertain:"不确定区"}[String(j.zone || "")];
        if(zoneText) meta.push(["落在", zoneText]);
        if(j.model) meta.push(["模型", ppModelLabel(j.model) + (Number(j.attempts || 1) > 1 ? ("（第 " + j.attempts + " 次才成）") : "")]);
        if(j.elapsed) meta.push(["用时", j.elapsed + " 秒"]);
        var secs = [];
        if(r.reason) secs.push(["理由", String(r.reason)]);
        if(r.failed) secs.push(["错误", String(r.error || j.error || "没给原因"), "bad"]);
        if(j.transfer_skipped) secs.push(["提示", "不确定时设的是「交给决策模型」：试跑不转交，线上会再多调一次普通决策。"]);
        ppTestShow("jev", meta, secs, "这次没给理由");
      })
      .catch(function(err){ ppTestFail("jev", err); });
  }

  // 用户 m01977：回复页也加「测试」——用实时窗那份纯文字记录真调一次回复模型，
  // 顺便把模型的思考过程（上游给了 reasoning_content 才有）显示出来。
  function repTest(){
    var s = ppState("reply");
    var sid = ppRtSid(s);
    ppTestWait("rep", "跑一次回复…（会真调一次，超时按失败算，别关页面）");
    post(API + "/reply-test" + (sid ? "?sid=" + encodeURIComponent(sid) : ""))
      .then(function(d){
        if(!d || !d.ok){ ppTestShow("rep", [], [["错误", (d && d.error) || "接口没回数据", "bad"]]); return; }
        var r = d.result || {};
        var meta = [];
        meta.push(["会话", sid ? sidLabel(sid) : "（没有可用会话，用了空窗口）"]);
        meta.push(["喂进去", (r.messages || 0) + " 条纯文字消息 · 系统提示词 " + (r.system_chars || 0) + " 字"]);
        if(r.model) meta.push(["模型", ppModelLabel(r.model)]);
        if(r.elapsed) meta.push(["用时", r.elapsed + " 秒"]);
        if(r.input_tokens || r.output_tokens){
          meta.push(["token", "入 " + (r.input_tokens || 0) + " · 出 " + (r.output_tokens || 0) +
                     (r.cached_tokens ? (" · 缓存 " + r.cached_tokens) : "")]);
        }
        var secs = [];
        if(!r.ok) secs.push(["错误", String(r.error || "回复模型没有返回"), "bad"]);
        if(r.reasoning) secs.push(["思考过程", ppTight(String(r.reasoning).slice(0, 4000))]);
        if(r.text){
          // 用户 m17185：回复按气泡显示、标签去掉；标签不合规就原样带标签出来，标题边上挂红角标。
          var pr = ppParseReply(r.text);
          if(pr.ok){
            var bub = pr.bubbles.map(function(b){ return "<div class='pp-test-bub'>" + esc(b.body) + "</div>"; }).join("");
            secs.push(["回复", "", "ok", { badge: { text: "标签正常", tone: "ok" }, html: bub }]);
          }else{
            secs.push(["回复", String(r.text).slice(0, 4000), "bad", { badge: { text: "标签不合规", tone: "bad" } }]);
          }
        }
        ppTestShow("rep", meta, secs, "模型没给内容");
      })
      .catch(function(err){ ppTestFail("rep", err); });
  }

  // v3.92（用户 m06951 第 6 条）：提示词页停在哪一半——decision / reply。
  var ppPane = "decision";

  var FWD_MODES = [
    { v: "off", t: "不展开", d: "只留「[合并转发]」，正文和图片都不给模型。" },
    { v: "text", t: "只给文字", d: "展开文字，图片全部换成占位，不请求图片。" },
    { v: "on_demand", t: "按需查看", d: "默认只挂前 3 张图，模型需要时自己调工具看完整内容。" }
  ];
  function fwdModeDesc(v){
    for(var i = 0; i < FWD_MODES.length; i++) if(FWD_MODES[i].v === v) return FWD_MODES[i].d;
    return "";
  }
  function fwdModeHtml(k, f){
    var cur = String((configCache && configCache[k]) || "on_demand");
    return "<div class='setrow inline' data-row='" + esc(k) + "'>" +
      "<div class='nm'>" +
        esc(nameOf(k, f)) + defaultBadge(f, k) + "</div>" +
      "<div class='ds' data-fwddesc='1'>" + esc(fwdModeDesc(cur)) + "</div>" +
      "<div class='ctl'><span class='seg'>" + FWD_MODES.map(function(o){
        return "<button type='button' class='segbtn" + (o.v === cur ? " on" : "") +
          "' data-fwdmode='" + esc(o.v) + "' title='" + esc(o.d) + "'>" + esc(o.t) + "</button>";
      }).join("") + "</span></div></div>";
  }
  function rowHtml(k, f){
    if(k === "forward_mode") return fwdModeHtml(k, f);
    var t = f.type || "string";
    var isList = (t === "multi_select" || t === "list" || t === "array");
    // 用户 m00697 第 17 / 18 条：没有显示描述的行不加悬浮描述；有的，悬浮文字与显示的一致。
    var tip = f.hint || "";
    // v3.80（用户 m02415）：伪随机这一行是个二选一，名字要随开关变成「伪随机 / 真随机」，
    // 所以单独包一层 span（defaultBadge 的角标不要被一起改掉）。
    var nmTxt = esc(nameOf(k, f));
    if(k === "prob_pseudo") nmTxt = "<span id='pseudo-nm'>" + nmTxt + "</span>";
    return "<div class='setrow inline" + (isList ? " wide" : "") + "' data-row='" + esc(k) + "'>" +
      "<div class='nm'>" + nmTxt + defaultBadge(f, k) + "</div>" +
      "<div class='ds'>" + esc(tip) + "</div>" +
      "<div class='ctl" + (isList ? " list" : "") + "'>" + fieldControl(k, f, configCache[k], UNIT_TEXT[k]) + "</div>" +
      "</div>";
  }

  // v3.41（用户本轮第 6 条）：计数 / 概率两个模式开关不再在组标题下面单独占一条（.subsw），
  // 并进各自的那一行：名称 + 精简说明 | 数字框 + 开关，一眼看出「这一档的参数和开关」。
  // 开关仍带 data-sub，change 事件照旧交给 trgSet 切 trigger_mode（不带 data-k，不走普通表单）。
  var SUBROW_MODE = { trigger_threshold: "count", trigger_probability: "probability",
                      trigger_probability_dm: "probability" };
  // v3.49（用户 m06348 第 7 条）：开关标签统一成「开关」两个字（原来写「计数开关 / 概率开关」），
  // 这一档是哪个模式由行名（计数触发 / 触发概率）和悬停 title 说清楚，全名留在 SUBROW_FULL 里。
  var SUBROW_FULL = { count: "计数模式", probability: "概率模式" };
  // v3.44（用户 m04471 第 5 条）：「开关 + 数值」成对参数的开关键 → 数值键。
  // 抖动、连发合并、两处闲时兜底都在这里登记，渲染时并成一行（见 pairRowHtml）。
  var PAIR_NUM = {
    // v3.70（用户 m21533 第 1 条）：休息时段整行回到「起止两个时间点 + 开关」，
    // 由 restPairHtml 单独画（不在这里登记）。
    jitter_enabled: "jitter_seconds",
    burst_merge_enabled: "burst_merge_seconds",
    // v3.81（用户 m03060）：「节奏」开关与「节奏松紧」并成一行——它们讲的是同一件事
    // （隔几条回一次），各占一行像两个独立功能。数字框在左、开关压最右，与上面几对一致。
    prob_pseudo: "adaptive_rhythm_sigma",
    // 用户 m03262：静默兜底 / 休息静默 / 静默初始过滤 / 过滤短句字数这四对也带数字框，
    // 但它们要两两并成一排，改由 SW_PAIR 登记（见下），这里不再重复。
  };
  // 用户 m03059：两个开关并成一排——描述清空后一行一个，右边空一大片。
  // 用户 m03262：带数字框的也并进来（静默兜底 ↔ 休息静默、静默初始过滤 ↔ 过滤短句字数），
  // 所以值改成数组：[右边那项的键, 左边数字键, 右边数字键]，没有数字框就留空串。
  // 语义相近的配一对：视频卡片链接 ↔ 无正文转发（都是转发类），单图片 ↔ 单表情包（都是媒体类）。
  var SW_PAIR = {
    silent_trigger_enabled: ["rest_silent_enabled", "silent_trigger_minutes", "rest_silent_minutes"],
    silent_skip_after_decision: ["silent_skip_short", "silent_skip_after_decision_count", "silent_skip_short_len"],
    silent_skip_video_link: ["silent_skip_empty_forward", "", ""],
    silent_skip_single_image: ["silent_skip_single_sticker", "", ""],
  };
  // v3.70（用户 m21533 第 1 条）：休息时段是普通模式自己的设置，跟曲线无关，
  // 所以这一行仍是「休息时段 [23:00] → [08:00] [开关]」，说明回到「这段时间回复概率大幅降低」。
  function restPairHtml(){
    var f = (schemaCache && schemaCache["rest_mode_enabled"]) || {};
    var hint = f.hint || "";
    var on = !!(configCache && configCache["rest_mode_enabled"]);
    var s1 = (schemaCache && schemaCache["rest_start"]) || {};
    var s2 = (schemaCache && schemaCache["rest_end"]) || {};
    return "<div class='setrow inline restpair' data-row='rest_mode_enabled'>" +
      "<div class='nm'>" +
        esc(nameOf("rest_mode_enabled", f)) + defaultBadge(f, "rest_mode_enabled") + "</div>" +
      "<div class='ds'>" + esc(hint) + "</div>" +
      "<div class='ctl pair'><span class='tspan'>" +
        fieldControl("rest_start", s1, configCache && configCache["rest_start"]) +
        "<b>→</b>" +
        fieldControl("rest_end", s2, configCache && configCache["rest_end"]) +
      "</span>" +
      swPill("rest_mode_enabled", on, "开启", "关闭") +
      "</div></div>";
  }
  function paramRowHtml(k, f, modeKey){
    var on = (trgModeValue() === modeKey);
    return "<div class='setrow inline subrow' data-row='" + esc(k) + "'>" +
      "<div class='nm'>" +
        esc(nameOf(k, f)) + defaultBadge(f, k) + "</div>" +
      "<div class='ds'>" + esc(f.hint || "") + "</div>" +
      "<div class='ctl'>" + fieldControl(k, f, configCache[k], UNIT_TEXT[k]) +
        "<label class='sw2 mod' title='切到" + SUBROW_FULL[modeKey] + "'>" +
          "<input type='checkbox' data-sub='" + modeKey + "'" + (on ? " checked" : "") + ">" +
          "<span class='tr' data-on='开启' data-off='关闭'></span></label>" +
      "</div></div>";
  }

  // v3.44（用户 m04471 第 5 条）：开关 + 数字框并成一行的通用行。
  // 用户抱怨「抖动要加开关」「连发合并加开关」「静默兜底和兜底等待分钟并成一行」——
  // 这些都是「一个开关管一个数值」的成对参数，各占两行太散，合成一行：
  // 名称 + 说明在左，数字框与开关在右，开关永远压最右（和普通模式那两行一个排法）。
  // 数值键在这里标 used（它没进 keys），否则会再渲染一份重复行。
  function pairUnitOf(numKey){
    var k = String(numKey || "");
    if(/_seconds$/.test(k)) return "秒";
    if(/_minutes$/.test(k)) return "分钟";
    if(k === "silent_skip_short_len") return "字";
    if(k === "silent_skip_after_decision_count") return "条";
    // 概率键不加单位：PCT_KEYS 那些键由 fieldControl 自己塞 <b class='pct'>%</b>，
    // 这里再加一个 % 会变成「%%」。
    return "";
  }
  // v3.92（用户 m06951 第 4 条）：把一组的「标题」和「开关（外加一个数值）」并成一行。
  // 用户原话：「节奏模式 标题和选项合并 做成一行」「进阶模式也把选项和标题合并」。
  // 仍然是 .setrow，data-row 与 data-k 都在原位，refreshDimRows / collectSettings 一行都不用改。
  function rowLineHtml(g){
    var onKey = String((g.keys || [])[0] || "");
    var numKey = PAIR_NUM[onKey] || "";
    var f = (schemaCache && schemaCache[onKey]) || {};
    var nf = numKey ? ((schemaCache && schemaCache[numKey]) || {}) : null;
    var on = !!(configCache && configCache[onKey]);
    var hint = g.note || f.hint || "";
    // 节奏那一行名字随开关变成「节奏 / 随机」，refreshDimRows 靠 #pseudo-nm 找它。
    // v3.93 起这行名字就是「节奏模式」，状态词藏在 .rowsub 里不显示（见 CSS）。
    var word = (onKey === "prob_pseudo")
      ? ("<span class='stword' id='pseudo-nm'>" + (on ? "节奏" : "随机") + "</span>")
      : "";
    // v4.08（用户 m16927 第 1 条）：名字不再套 .gt 小标题，改成跟上面的普通行一个格式——
    // 常规字号 + 右边一枚默认值角标。成对行把开关与数值的默认写在一起（「默认 关 · 65」）。
    var badge = defaultBadge(f, onKey);
    var nd = (numKey && nf) ? defaultText(nf, numKey) : "";
    if(nd && nd !== "无"){
      var u0 = pairUnitOf(numKey);
      badge = "<i class='dfv'>" +
        esc("默认 " + defaultText(f, onKey) + " · " + nd + (u0 ? " " + u0 : "")) + "</i>";
    }
    var ctl = "";
    if(numKey && nf){
      ctl += "<span class='nspan'>" + fieldControl(numKey, nf, configCache[numKey], pairUnitOf(numKey)) + "</span>";
    }
    ctl += swPill(onKey, on, "开启", "关闭", hint || g.t);
    // v4.08（用户 m16927 第 3 条）：决策模式那句提醒（多一次模型调用）要像「动态调整」行尾
    // 那条琥珀胶囊一样高亮，所以说明格拆成「文字 + 胶囊」两段，文字先省略、胶囊不被裁。
    var dsHtml = g.warn
      ? ("<div class='ds dsw' title='" + esc(hint) + "'>" +
           "<span class='dtxt'>" + esc(hint) + "</span>" +
           warnPills(g.warn) + "</div>")
      : ("<div class='ds'>" + esc(hint) + "</div>");
    return "<div class='setrow inline rowline' data-row='" + esc(onKey) + "'>" +
      "<div class='nm'>" +
        esc(g.t) + badge + word + "</div>" +
      dsHtml +
      "<div class='ctl pair'>" + ctl + "</div></div>";
  }

  function pairRowHtml(onKey, numKey, label){
    var f = (schemaCache && schemaCache[onKey]) || {};
    var nf = (schemaCache && schemaCache[numKey]) || {};
    var on = !!(configCache && configCache[onKey]);
    var nm = label || nameOf(onKey, f);
    var hint = f.hint || "";
    // v3.81（用户 m03060）：节奏这一行的名字要随开关变「节奏 / 随机」，
    // refreshDimRows 靠 #pseudo-nm 找它；合并成一行后这个 span 得留在名字里。
    var nmHtml = onKey === "prob_pseudo"
      ? "<span id='pseudo-nm'>" + esc(nm) + "</span>"
      : esc(nm);
    var unit = pairUnitOf(numKey);
    // 默认值角标把「开关默认」和「数值默认」写在一起（成对行只有一行，分两个角标太挤）：
    // 「默认 关 · 7 秒」「默认 开 · 3 分钟」。数值默认取 schema，unit 用中文单位。
    var nd = defaultText(nf, numKey);
    var badge = defaultBadge(f, onKey);
    if(nd && nd !== "无"){
      var dTxt = "默认 " + defaultText(f, onKey) + " · " + nd + (unit ? " " + unit : "");
      badge = "<i class='dfv'>" + esc(dTxt) + "</i>";
    }
    return "<div class='setrow inline pairrow' data-row='" + esc(onKey) + "'>" +
      "<div class='nm'>" +
        nmHtml + badge + "</div>" +
      "<div class='ds'>" + esc(hint) + "</div>" +
      "<div class='ctl pair'>" +
        // v3.50（用户 m06761 第 4 条）：单位交给 fieldControl 放进框里（has-unit），不再挂在框外
        "<span class='nspan'>" + fieldControl(numKey, nf, configCache[numKey], unit) + "</span>" +
        swPill(onKey, on, "开启", "关闭") +
      "</div></div>";
  }

  // 用户 m03059：两个开关并成一排，各占一半宽度，名字在左、开关在右。
  // 用户 m03262：带数字框的也走这里——数字框在开关左边，跟 pairRowHtml 一个排法。
  function swPairHtml(k1, k2, n1, n2){
    function cell(k, nk){
      var f = (schemaCache && schemaCache[k]) || {};
      var on = !!(configCache && configCache[k]);
      var ctl = "";
      if(nk){
        var nf = (schemaCache && schemaCache[nk]) || {};
        ctl += "<span class='nspan'>" +
          fieldControl(nk, nf, configCache[nk], pairUnitOf(nk)) + "</span>";
      }
      ctl += swPill(k, on, "开启", "关闭");
      return "<div class='swcell'>" +
        "<div class='nm'>" + esc(nameOf(k, f)) + defaultBadge(f, k) + "</div>" +
        "<div class='ctl pair'>" + ctl + "</div>" +
        "</div>";
    }
    return "<div class='setrow inline swpair' data-row='" + esc(k1) + "'>" +
      cell(k1, n1) + cell(k2, n2) + "</div>";
  }

  // v3.54（用户本轮第 3 条）：配额四行合成两行——每行「名称 + 一句说明 | 数字框 + 单位 + 开关」，
  // 跟聊天决策页里概率 / 计数那几行的紧凑排法一致。数字框仍走 fieldControl（同一个 .ipt 尺寸），
  // 开关带 data-k（走普通表单），互斥置灰 / 数字框禁用见 paintQuotaMutex。
  // 用户 m12007 第 3 条：说明改成「最多请求多少次」（不是「回几次」），单位只留「次」并塞进框里——
  // 原来「次/小时」「次/天」挂在框外，宽窄一变就飘；周期由左边行名（每小时配额 / 每天配额）交代。
  var QUOTA_ROWS = [
    { on: "quota_hour_enabled", num: "quota_max_per_hour", desc: "最多请求多少次", unit: "次" },
    { on: "quota_day_enabled",  num: "quota_max_per_day",  desc: "最多请求多少次", unit: "次" }
  ];
  function quotaRowHtml(it){
    var f = (schemaCache && schemaCache[it.on]) || {};
    var nf = (schemaCache && schemaCache[it.num]) || {};
    var on = !!(configCache && configCache[it.on]);
    var hint = f.hint || "";
    return "<div class='setrow inline quotarow' data-row='" + esc(it.on) + "'>" +
      "<div class='nm'>" +
        esc(nameOf(it.on, f)) + defaultBadge(nf, it.num) + "</div>" +
      "<div class='ds'>" + esc(it.desc) + "</div>" +
      "<div class='ctl pair'>" +
        "<span class='nspan'>" + fieldControl(it.num, nf, configCache[it.num], it.unit) + "</span>" +
        swPill(it.on, on, "开启", "关闭", it.desc) +
      "</div></div>";
  }
  function quotaRowsHtml(){
    return QUOTA_ROWS.map(quotaRowHtml).join("");
  }

  // v3.30（用户 m47796 第 4 条）：艾特一行两个开关。
  // 左边那个（小字「开关」）= 被 @ 直接算一次触发（老「被 @ 必定回复」的行为，走 mention_trigger）；
  // 右边那个（小字「必回」）= 命中就跳过模型判断直接回（用户 m00347 起正式启用，不再灰置）。
  // v3.49（用户 m06348 第 7 条）：可点的开关小字统一成「开关」；灰着那个是另一个功能（必回），
  // 保留它自己的名字，否则一行里两个一模一样的「开关」分不清谁是谁。
  // v3.40（用户本轮第 5 条）：唤醒三件套（艾特 / 外号 / 昵称）都归到「唤醒」组，
  // 一行一个、每行两个开关：左边「触发」照旧可点（mention_trigger / nickname_wake /
  // name_wake），右边「必回」走 *_always_reply 三键，后端真读（decision/engine.py:2934-2957）。
  // 行 data-row 用可点那个键：单群白名单按它判这一整行能不能改。
  function wakeRowHtml(k, ak, label, hintK, hintA){
    var f = (schemaCache && schemaCache[k]) || {};
    var af = (schemaCache && schemaCache[ak]) || {};
    // 用户 m10297 第 2 条：这一行是两个互斥的档，必回开着时触发显示为关（必回优先）。
    var on = wakeShown(k, ak, !!(configCache && configCache[k]));
    var aon = !!(configCache && configCache[ak]);
    return "<div class='setrow inline wakepair' data-row='" + esc(k) + "'>" +
      "<div class='nm'>" +
        esc(label) + defaultBadge(f, k) + "</div>" +
      "<div class='ctl pair'>" +
        sw3(k, ak, aon ? "always" : (on ? "trigger" : "off")) +
      "</div></div>";
  }
  // 用户 m00363：「休息时段 被@ 回不回」也要能在基础设置右下角单独开关，同样分触发 / 必回。
  // 用户 m01432：休息时段默认关闭——睡觉时被叫到默认不理，想理就把触发拨过去。
  // 必回默认关（用户 m09403）。
  function restWakeRowHtml(){
    var k = "rest_wake_trigger", ak = "rest_wake_always";
    var f = (schemaCache && schemaCache[k]) || {};
    var af = (schemaCache && schemaCache[ak]) || {};
    var on = wakeShown(k, ak, !!(configCache && configCache[k]));
    var aon = !!(configCache && configCache[ak]);
    return "<div class='setrow inline wakepair' data-row='" + esc(k) + "'>" +
      "<div class='nm'>" +
        "休息时段" + defaultBadge(f, k) + "</div>" +
      "<div class='ds'>休息时段里被叫到要不要理。</div>" +
      "<div class='ctl pair'>" +
        sw3(k, ak, aon ? "always" : (on ? "trigger" : "off")) +
      "</div></div>";
  }
  // 用户 m10297 第 2 条：「触发」和「必回」是互斥的——必回本来就是「跳过模型判断直接回」，
  // 同一句话再算一次触发等于记两遍；两个都亮也让人看不出到底算几次。
  // 后端行为本来就是这样（decision/engine.py 里 must 命中就先 return，走不到触发那一路），
  // 这里把界面上的互斥做出来：点开一个就把同组另一个关掉（两个都关＝不理，仍然允许）。
  var WAKE_PAIRS = [["mention_trigger", "mention_always_reply"],
                    ["nickname_wake", "nickname_wake_always_reply"],
                    ["name_wake", "name_wake_always_reply"],
                    ["rest_wake_trigger", "rest_wake_always"]];
  function wakePairOf(k){
    for(var i = 0; i < WAKE_PAIRS.length; i++){
      if(WAKE_PAIRS[i][0] === k || WAKE_PAIRS[i][1] === k) return WAKE_PAIRS[i];
    }
    return null;
  }
  function wakeMutex(k, on){
    var pair = wakePairOf(k);
    if(!pair) return false;
    if(!on) return true;              // 只关掉一个，同组另一个不用动
    // 变量名用 twin 不用 other：配额那套老语义（把另一档 checked 改成 false）
    // 是被测试明确禁掉的写法，这里换个名字免得看着像它又回来了。
    var twin = pair[0] === k ? pair[1] : pair[0];
    if(configCache) configCache[twin] = false;
    var el = document.querySelector("#settings [data-k=" + JSON.stringify(twin) + "]");
    if(el) el.checked = false;
    return true;
  }
  // v4.45（用户 m22611）：历史里的动图三档互斥——只读首帧 / 原图发送 / 九帧模式。
  // 开一个就把另外两个关掉；三个都不开＝跳过，历史里的动图不发给模型。
  var GIF_TRIO = ["history_gif_first", "history_gif_original", "history_gif_grid"];
  function gifMutex(k, on){
    if(GIF_TRIO.indexOf(k) < 0) return false;
    if(!on) return true;              // 关掉当前这档 → 三个全关＝跳过
    GIF_TRIO.forEach(function(twin){
      if(twin === k) return;
      if(configCache) configCache[twin] = false;
      var el = document.querySelector("#settings [data-k=" + JSON.stringify(twin) + "]");
      if(el) el.checked = false;
    });
    return true;
  }
  // 老配置里两个都开着（行为上等于只开必回）：渲染时把「触发」显示成关的，
  // 免得一进页面就看见两个都亮；下次保存时自然写成互斥的那一种。
  function wakeShown(k, ak, on){
    if(on && ak && configCache && configCache[ak]){
      configCache[k] = false;
      return false;
    }
    return on;
  }

  function wakeRowsHtml(){
    return wakeRowHtml("mention_trigger", "mention_always_reply", "艾特") +
      wakeRowHtml("nickname_wake", "nickname_wake_always_reply", "外号") +
      wakeRowHtml("name_wake", "name_wake_always_reply", "昵称") +
      restWakeRowHtml();
  }

  // v3.50（用户 m06761 第 7 条）：管理员改成可加多位的胶囊列表——用户原话「管理员 QQ 号的名字，
  // 不是群昵称」「管理员也可以多位，像外号的添加按钮一样添加」。
  // 数据只存一个键 admin_list（JSON 文本 [{name,qq}]）：隐藏 input 承载文本，collectSettings
  // 照常原样提交；老的两个键 admin_name / admin_id 不再露脸，值跟着 configCache 一起走。
  function adminList(){
    var raw = configCache ? configCache.admin_list : "";
    var arr = [];
    try{
      var a = (typeof raw === "string") ? JSON.parse(raw || "[]") : raw;
      if(Array.isArray(a)) arr = a;
    }catch(e){ arr = []; }
    return arr.map(function(x){
      if(x && typeof x === "object"){
        return { name: String(x.name == null ? "" : x.name), qq: String(x.qq == null ? "" : x.qq) };
      }
      return { name: String(x == null ? "" : x), qq: "" };
    }).filter(function(x){ return x.name || x.qq; });
  }
  // 「+」点开才出现两个输入框（名字 / QQ 号）——跟外号那套一个手感，行里平时只占一个加号。
  // v3.51（用户 m07385 第 4 条）：表单是浮层（CSS 绝对定位），并且带一个 ×，不想加就关掉。
  function adminAddHtml(){
    return "<button type='button' class='chip-add' data-aadd='1' title='添加管理员'>+</button>" +
      "<span class='adform' hidden>" +
        "<input class='ad-name' type='text' placeholder='名字' autocomplete='off'>" +
        "<input class='ad-qq' type='text' placeholder='QQ 号' autocomplete='off'>" +
        "<button type='button' class='ado' data-aok='1' title='加入列表'>添加</button>" +
        "<button type='button' class='adx' data-acancel='1' title='取消（Esc）'>×</button>" +
      "</span>";
  }
  // 用户 m07085 第 1 条：管理员拆成两行——第一行只放名字、第二行只放 QQ 号。
  // 没填的那半格显式写出来（「待填名字」/「待填 QQ 号」），别留空让人以为是没渲染。
  function adminNameChipHtml(a, i){
    return "<span class='chip adm' data-ai='" + i + "' data-af='name'>" +
      (a.name ? esc(a.name) : "<em class='aq'>待填名字</em>") +
      "<b data-adel='1'>×</b></span>";
  }
  function adminQqChipHtml(a, i){
    return "<span class='chip adm' data-ai='" + i + "' data-af='qq'>" +
      (a.qq ? esc(a.qq) : "<em class='aq'>待填 QQ 号</em>") +
      "<b data-adel='1'>×</b></span>";
  }
  function adminRowsHtml(){
    var fl = (schemaCache && schemaCache.admin_list) || {};
    var nf = (schemaCache && schemaCache.admin_name) || {};
    var qf = (schemaCache && schemaCache.admin_id) || {};
    var hint = fl.hint || "这一行同时管管理员的名字与 QQ 号：第一行填名字，第二行填 QQ 号，可以加多位。";
    var nhint = nf.hint || "QQ 的昵称，不是群昵称。";
    var qhint = qf.hint || "填 QQ 号即可。";
    var list = adminList();
    // 用户 m07085 第 1 条：两行都挂 data-row='admin_list'（单群白名单还是只认这一个键），
    // 数据也只存 admin_list 一个键——隐藏 input 挂在第一行，两行胶囊靠 adminSync 一起刷新。
    return "<div class='setrow inline wide' data-row='admin_list'>" +
      "<div class='nm'>" +
        esc(nameOf("admin_name", nf)) + "</div>" +
      "<div class='ds'>" + esc(nhint) + "</div>" +
      "<div class='ctl list'><div class='chips' data-admin='1'>" +
        list.map(adminNameChipHtml).join("") + adminAddHtml() +
      "</div>" +
      "<input type='hidden' data-k='admin_list' value='" + esc(JSON.stringify(list)) + "'></div>" +
      "</div>" +
      "<div class='setrow inline wide' data-row='admin_list'>" +
      "<div class='nm'>" +
        esc(nameOf("admin_id", qf)) + "</div>" +
      "<div class='ds'>" + esc(qhint) + "</div>" +
      "<div class='ctl list'><div class='chips' data-admin-qq='1'>" +
        list.map(adminQqChipHtml).join("") + adminAddHtml() +
      "</div></div>" +
      "</div>";
  }
  // 用户 m10749 第 1 条：两个配额开关改成「开一个，另一个变灰」——原来是「勾一个就把另一个
  // 关掉」，用户要的是灰着不动、值仍留着。只改 configCache 与灰化状态，不整页重绘，
  // 免得把焦点和滚动位置冲掉。
  function quotaMutex(k, on){
    if(configCache) configCache[k] = on;
    paintQuotaMutex();
    refreshDimRows();
  }
  // 用户 m10749 第 1 条：按两档开关的值，把「不该点的那一档」置灰（值保留，只是不能再点）。
  // 开关行带 data-k，所以用属性选择器找控件，再用 closest('.setrow') 拿整行加 .dim。
  function paintQuotaMutex(){
    var qh = document.querySelector("#settings [data-k='quota_hour_enabled']");
    var qd = document.querySelector("#settings [data-k='quota_day_enabled']");
    var hOn = quotaSwOn("quota_hour_enabled", qh);
    var dOn = quotaSwOn("quota_day_enabled", qd);
    // 用户 m10749 第 1 条：两档同时开着（老配置）时以「每小时」为准——否则刚点开的那档
    // 会把自己也灰掉，两档一起冻住谁都关不回去。
    quotaSwDim(qd, hOn);
    quotaSwDim(qh, hOn ? false : dOn);
    // v3.54（用户本轮第 3 条）：数字框跟着自己那一档走——开关关着、或被另一档顶掉时禁用；
    // 值仍显示、也仍随表单提交（collectSettings 不看 disabled）。
    quotaNumDim("quota_max_per_hour", hOn);
    quotaNumDim("quota_max_per_day", dOn && !hOn);
  }
  // 用户 m10749 第 1 条：配置里存着就按配置（configCache 才是真值），否则看控件本身。
  function quotaSwOn(k, el){
    if(configCache && Object.prototype.hasOwnProperty.call(configCache, k)) return !!configCache[k];
    return el ? !!el.checked : false;
  }
  // 用户 m10749 第 1 条：灰化＝控件 disabled + 整行 .dim；解除时两样都还原。
  function quotaSwDim(el, dim){
    if(!el) return;
    el.disabled = !!dim;
    var row = el.closest ? el.closest(".setrow") : null;
    if(row && row.classList) row.classList.toggle("dim", !!dim);
  }
  // v3.54（用户本轮第 3 条）：配额数字框的禁用与压暗，口径跟开关一致——
  // 这一档不生效就填不了，但值留着（disabled 的 input 照样会被 collectSettings 读走）。
  function quotaNumDim(numKey, enabled){
    var el = document.querySelector("#settings [data-k='" + numKey + "']");
    if(!el) return;
    el.disabled = !enabled;
    var box = (el.closest ? el.closest(".nspan") : null);
    if(box && box.classList) box.classList.toggle("dimv", !enabled);
  }
  // 一处改动同步三个地方：configCache（下次重绘的来源）、隐藏 input（提交用）、
  // 页面上的胶囊（即时反馈）。不整页重绘，免得把正在敲的框和焦点一起冲掉。
  function syncAdminChips(box, chipsHtml){
    if(!box) return;
    var add = box.querySelector(".chip-add");
    if(!add){ box.innerHTML = chipsHtml + adminAddHtml(); return; }
    Array.prototype.slice.call(box.querySelectorAll(".chip.adm")).forEach(function(n){
      if(n.parentNode) n.parentNode.removeChild(n);
    });
    add.insertAdjacentHTML("beforebegin", chipsHtml);
  }
  function phraseList(){
    var raw = configCache ? configCache.phrase_filter_list : "";
    try{
      var a = (typeof raw === "string") ? JSON.parse(raw || "[]") : raw;
      if(Array.isArray(a)) return a.map(function(x){ return String(x); });
    }catch(e){}
    return [];
  }
  // 用户 m07085 第 7 条：窗口页「短语黑名单」拉黑写的是插件侧全局清单，框架那边的
  // /config 要等重载才同步，所以设置页额外问一次插件自己的 /phrase-block：
  // 全局模式拿它的 items 覆盖缓存并重绘（保存时不会把刚拉黑的又丢掉），
  // 单群模式只当只读参照、不动全局表；401 / 失败一律静默，按框架那份显示。
  var phraseServer = null;
  function phraseBlockUrl(){
    return API + "/phrase-block" + (scopeSid ? "?sid=" + encodeURIComponent(scopeSid) : "");
  }
  // 用户 m07715：清单改成接口驱动——加 / 删 / 恢复都打 /phrase-block，后端返回这一份的
  // 最新状态（items 生效、own_items 本群独立池、unblock_items 本会话解除掉的全局条目）。
  function phraseQs(extra){
    return "?sid=" + encodeURIComponent(scopeSid || "") + (extra || "");
  }
  function phrasePost(qs){
    return fetch(API + "/phrase-block" + qs, { method: "POST", credentials: "same-origin" })
      .then(function(r){ return r.json(); })
      .then(function(d){
        if(d && d.ok) phraseApply(d);
        return d;
      });
  }
  function phraseApply(d){
    phraseServer = {
      items: (d.items || []).map(function(x){ return String(x); }),
      own_items: (d.own_items || []).map(function(x){ return String(x); }),
      unblock_items: (d.unblock_items || []).map(function(x){ return String(x); })
    };
    phrasePaint(phraseServer);
  }
  function phraseChipsHtml(p){
    var items = (p && p.items) || [];
    var off = (p && p.unblock_items) || [];
    var empty = (items.length || off.length) ? "" :
      "<span class='blk-empty'>还没有拉黑任何短语。点右边的 + 加一条。</span>";
    return empty + items.map(function(x){
      return "<span class='chip'>" + esc(x) + "<b data-pdel='1'>×</b></span>";
    }).join("") + off.map(function(x){
      return "<span class='chip off' data-pun='1' title='全局拉黑，本会话已解除；点一下恢复'>" +
        esc(x) + "<em class='aq'>已解除</em></span>";
    }).join("") +
      "<button type='button' class='chip-add' data-padd='1' title='添加短语'>+</button>" +
      "<input class='blk-in' type='text' placeholder='输入短语后回车添加' autocomplete='off' hidden>";
  }
  // 把一份清单写回隐藏 input 与页内胶囊，不整页重绘。
  // 隐藏 input 只承载「本会话独立池」：全局模式承载全局清单，单群模式承载 own_items，
  // 免得通用保存把全局清单整份写进单群覆盖（用户 m07715）。
  function phrasePaint(p){
    var box = document.getElementById("ph-chips");
    if(box) box.innerHTML = phraseChipsHtml(p);
    var keep = scopeSid ? ((p && p.own_items) || []) : ((p && p.items) || []);
    var txt = JSON.stringify(keep);
    if(configCache) configCache.phrase_filter_list = txt;
    var hid = document.querySelector("#settings input[type='hidden'][data-k='phrase_filter_list']");
    if(hid) hid.value = txt;
  }
  function phraseChipText(c){
    var s = "";
    for(var i = 0; i < c.childNodes.length; i++){
      if(c.childNodes[i].nodeType === 3) s += c.childNodes[i].nodeValue;
    }
    return s.trim();
  }
  function loadPhraseBlock(){
    return get(phraseBlockUrl()).then(function(d){
      if(!d || !d.ok || !Array.isArray(d.items)) return;
      phraseApply(d);
    }).catch(function(){ /* 静默：没登录 / 接口报错都按框架那份来 */ });
  }
  // v3.44（用户 m04471 第 5 条）：开关与清单并成一块「短语黑名单」。
  // 用户 m07418：清单不再藏在弹窗里，直接铺在行下面，拉黑了哪些一眼能看到。
  function phraseRowHtml(){
    var on = !!(configCache && configCache.phrase_filter_enabled);
    var f = (schemaCache && schemaCache.phrase_filter_enabled) || {};
    var hint = "命中的纯文字不参与触发判定，消息本身仍留在上下文里。";
    var p = phraseServer || { items: phraseList(), own_items: [], unblock_items: [] };
    var keep = scopeSid ? (p.own_items || []) : (p.items || []);
    return "<div class='setrow inline' data-row='phrase_filter_enabled'>" +
        "<div class='nm'>短语黑名单" +
          defaultBadge(f, "phrase_filter_enabled") + "</div>" +
        "<div class='ds'>" + esc(hint) + "</div>" +
        "<div class='ctl'>" + swPill("phrase_filter_enabled", on, "开启", "关闭", hint) + "</div>" +
      "</div>" +
      "<div class='blk-list'><div class='chips' id='ph-chips'>" + phraseChipsHtml(p) + "</div></div>" +
      "<input type='hidden' data-k='phrase_filter_list' value='" + esc(JSON.stringify(keep)) + "'>";
  }

  // v3.30：用不上的参数行灰掉。普通模式列里计数/概率各自的参数分开灰，伪随机那两个数
  // 还要看「伪随机兜底」开没开。（私聊策略整组已按用户 m12007 第 4 条删除，这里不再有私聊分支。）
  // 用户 m06706：决策模式 与 JEV 决策 互斥。开一个必须当场关另一个，并且**立刻存**
  // （saveNow，不走 700ms 防抖）——这对开关是离散动作，拖一下再切作用范围就可能只
  // 存进一半，后端读到的就是「两个都开」。关掉一个时不动另一个（允许两个都关）。
  // 返回 true 表示这次真的压掉了另一个，调用方要负责存盘。
  // 用户 m06706：两个模式开关互斥，开一个就把另一个真的关掉（不是置灰——它们不能同时生效）。
  // 关掉的只是开关，那一套模型 / 提示词 / 区间都原样留着，来回切不用重填。
  function setSwitchQuiet(key, value){
    var el = document.querySelector("#settings [data-k='" + key + "']");
    if(el) el.checked = !!value;
    if(configCache) configCache[key] = !!value;
  }

  function decisionModeMutex(key, on){
    if(!on) return false;
    var other = key === "advanced_enabled" ? "jev_enabled" : "advanced_enabled";
    var el = document.querySelector("#settings [data-k='" + other + "']");
    var had = !!(el && el.checked);
    setSwitchQuiet(other, false);
    return had;
  }

  function refreshDimRows(){
    var v = trgVal();
    // v3.71（用户第 8 条末）：曲线 / 动态调整接管之后伪随机兜底不参与判定，三行一起灰掉
    var bOn = bandsActive();
    // v3.72（用户第 3 条）：只有「动态调整」真正接管时才不吃伪随机兜底——曲线开着但还停在
    // 计数 / 概率档时照样用它，那三行不能灰（灰了像是曲线模式下这套兜底废了）。
    var dynOn = bOn && (v === "adaptive");
    var colN = document.querySelector("#settings .col[data-col='normal']");
    if(colN){
      Array.prototype.forEach.call(colN.querySelectorAll(".rows [data-row]"), function(row){
        var k = row.getAttribute("data-row");
        var dim = false, off = false;
        // v3.38：休息四项现在也在这一列，它们的灰化由下面曲线那一段统一决定，这里跳过
        if(k.indexOf("rest_") === 0) return;
        if(k === "trigger_threshold") dim = (v !== "count");
        // v3.71（用户第 8 条末）：伪随机三项回到这一列，灰化看三件事——当前是不是概率模式、
        // 兜底开关开没开、曲线 / 动态调整有没有接管（接管了就不吃兜底）。
        else if(k === "trigger_probability" || k === "trigger_probability_dm") dim = (v !== "probability") || bOn;
        // v3.50（用户 m06761 第 6 条）：连发合并是「计数触发」的功能——计数模式没开时
        // 整行灰掉不可点（.off 关掉 pointer-events），它自己的开关就在计数触发那一行，不受影响。
        else if(k === "burst_merge_enabled") off = (v !== "count");
        row.classList.toggle("dim", dim);
        row.classList.toggle("off", off);
      });
    }
    // v3.54（用户本轮第 3 条）：配额四行合成两行后，数字框就在开关那一行里，
    // 灰化与禁用统一交给 paintQuotaMutex（本函数末尾会调一次），这里不再单独扫一遍。
    // v3.53（用户本轮第 4 条）：退避必须开了进阶模式才能用——没开时整组（连标题）灰掉。
    // 只灰显示、不清已保存的值：开关一打开，原来那几个数原样回来。
    // v3.93（用户 m07570 图1 第 3 条）：退避这一组功能还没做完，用户要求
    // 「还没做完的情况下覆盖个灰色」——现在不论开没开决策模式都恒灰。
    // 真做完把这行删掉，就回到「开决策模式才亮」的老口径。
    var BACKOFF_UNFINISHED = true;
    var advSw = document.querySelector("#settings [data-k='advanced_enabled']");
    var advOn = advSw ? !!advSw.checked : false;
    var bOff = BACKOFF_UNFINISHED || !advOn;
    Array.prototype.forEach.call(document.querySelectorAll("#settings [data-grp='backoff']"), function(grp){
      grp.classList.toggle("dim", bOff);
    });
    Array.prototype.forEach.call(document.querySelectorAll("#settings [data-grp='backoff'] [data-row]"), function(row){
      row.classList.toggle("dim", bOff);
    });
    // v3.80（用户 m02415）：伪随机这一项是「真随机 / 伪随机」二选一，标签直接写现在用哪种。
    // 动态调整接管时不灰掉、也不让人关——强制显示成开（那时它按节奏器排队，这一项不参与叠加），
    // 说明行也换成一句实话，免得以为伪随机在算。
    var pSw = document.querySelector("#settings [data-row='prob_pseudo'] input[data-k='prob_pseudo']");
    if(pSw){
      if(dynOn){
        if(pSw.dataset.real == null) pSw.dataset.real = pSw.checked ? "1" : "0";
        pSw.checked = true;
      } else if(pSw.dataset.real != null){
        pSw.checked = pSw.dataset.real === "1";
        delete pSw.dataset.real;
      }
      pSw.disabled = !!dynOn;
      var pRow = pSw.closest("[data-row]");
      var pNm = pRow ? pRow.querySelector("#pseudo-nm") : null;
      var pDs = pRow ? pRow.querySelector(".ds") : null;
      var onNow = pSw.checked;
      var pName = onNow ? "节奏" : "随机";
      // v3.81（用户 m03060）：这两项并成一行后，说明里把「松紧」三档一起带出来——
      // 数字框就在同一行右侧，光写「按间隔排队」看不出那个 65 是干什么的。
      // v3.93（用户 m07570 图1 第 1 条）：描述里那串「0 / 65 / 120 最晚 1.5 倍」太像
      // 参数说明，用户要「优化一下」。改成先说效果、再说那个数字框是干什么的。
      // v4.08（用户 m16927 第 2 条）：用户嫌这三句太长（真机上被省略号截断），各压到 30 字上下。
      // v4.35（用户 m02105）：1920 分辨率下这一格只有 248px（≈19 个字），三句都在截断，
      // 各压到 19 字以内，含义保留「越等越容易中 / 右边那格管松紧」。
      var pHint = dynOn
        ? "动态调整接管，右边那格越大越能等。"
        : (onNow ? "越等越容易中；右边那格越大越能等。"
                 : "每条单独掷一次，不攒也不保底。");
      if(pNm){
        pNm.textContent = pName;
        var pNmBox = pNm.parentNode;
        if(pNmBox && pNmBox.setAttribute) pNmBox.setAttribute("title", pHint);
      }
      if(pDs){ pDs.textContent = pHint; pDs.setAttribute("title", pHint); }
    }
    // v3.71（用户第 8 条末）：休息时段搬到「通用设置」列后，它的回复概率不再被曲线灰掉——
    // 曲线 / 动态模式也照它降概率，灰掉会让人以为曲线模式下这项没用了。
    // v3.83（用户 m03849 图3）：计数模式只按条数算，压根不掷骰——节奏这一组（开关 + 松紧）
    // 整组压暗，代表不生效。只压显示、不禁用，切回概率 / 曲线时原样回来。
    // v3.92（用户 m06951 第 2 条）：压暗判据从「trigger_mode 等于 count」改成「真·计数模式」。
    // 用户原话：「动态调整关了后 不节奏模式是灰色，因为普通模式那边虽然不能用了，
    // 但计数模式开关还开着，导致节奏模式的那边都是灰的」——曲线开着时
    // trigger_mode 可能还停在 count，那一刻回复概率是曲线给的（掽骰），节奏这一组明明在用。
    // 只有「没开曲线 + 当前档是计数」才是真的不掽骰。
    var hardCount = (v === "count") && !bOn;
    // v4.09（用户 m17145 第 1 条）：节奏那一行并进「高级设置」块后不再是独立 .sub，
    // 直接按行压暗——[data-row='prob_pseudo'] 就是那一行（开关与松紧都在里面）。
    Array.prototype.forEach.call(document.querySelectorAll("#settings [data-row='prob_pseudo']"), function(row){
      row.classList.toggle("dim", hardCount);
    });
    // v3.44：成对行（开关 + 数值）——开关关着时只把数值压暗，开关本身仍可点
    Array.prototype.forEach.call(document.querySelectorAll("#settings [data-row].pairrow"), function(row){
      var sw = row.querySelector(".sw2 input[data-k]");
      var ns = row.querySelector(".nspan");
      if(!sw || !ns) return;
      ns.classList.toggle("dimv", !sw.checked);
    });
    // 用户 m03262：并排的两个开关各自带数字框时，开关关着就把自己那格压暗。
    Array.prototype.forEach.call(document.querySelectorAll("#settings [data-row].swpair"), function(row){
      Array.prototype.forEach.call(row.querySelectorAll(".swcell"), function(cell){
        var sw = cell.querySelector(".sw2 input[data-k]");
        var ns = cell.querySelector(".nspan");
        if(!sw || !ns) return;
        ns.classList.toggle("dimv", !sw.checked);
      });
    });
    // 用户 m01445：决策模式（advanced_enabled）一开一关，唤醒那几行的「必回」跟着亮 / 灰。
    Array.prototype.forEach.call(document.querySelectorAll("#settings .sw3"), function(box){
      box.classList.toggle("no-must", !advOn);
    });
    // 用户 m10749 第 1 条：配额两档的灰化跟着一起刷（刷新 / 切页后状态不能丢）
    paintQuotaMutex();
  }

  // v3.13：这个键不允许关闭（接管上下文窗口是核心功能）
  // v3.57（用户 m16357 图 3）：dynamic_to_tail 已从设置页撤掉，锁表里只剩 window_enabled。
  var LOCKED = { window_enabled: 1 };

  // v4.09（用户 m17145 第 1 条）：把「高级设置」这个 sect 组与紧随其后的 rowline 组并成一块。
  // 以前每个 rowline 组各自是一个 .sub.rowsub，各带 14px 外边距 + 12px 内边距 + 一条虚线，
  // 于是「一到高级设置板块就这么宽松」；合并后标题与两行同处一个 .sub，与「闲时兜底」同形。
  var ublkCache = null;
  function ublkChipsHtml(p){
    var items = (p && p.items) || [];
    var off = (p && p.unblock_items) || [];
    // 空表提示跟「+」排在同一行，别让加号孤零零挂在下面一行
    var empty = (items.length || off.length) ? "" :
      "<span class='blk-empty'>还没有拉黑任何人。在窗口页把鼠标放到名字上，点「拉黑」就能加进来；" +
      "也可以点右边的 + 手动填 QQ 号。</span>";
    return empty + items.map(function(it){
      var uid = String(it.uid || "");
      var nm = String(it.name || "");
      var ts = Number(it.ts || 0);
      var when = ts ? new Date(ts * 1000).toLocaleString() : "";
      var tip = "QQ " + uid + (when ? " · " + when : "");
      return "<span class='chip' data-uid='" + esc(uid) + "' data-name='" + esc(nm) + "' title='" + esc(tip) + "'>" +
        (nm ? esc(nm) + "<em class='aq'>" + esc(uid) + "</em>" : "<em class='aq'>" + esc(uid) + "</em>") +
        "<b data-udel='1'>×</b></span>";
    }).join("") + off.map(function(it){
      // 用户 m07715：全局拉黑被本会话解除的，变灰留在原地，点一下恢复
      var uid = String(it.uid || "");
      var nm = String(it.name || "");
      return "<span class='chip off' data-uid='" + esc(uid) + "' data-name='" + esc(nm) + "' data-urestore='1'" +
        " title='全局拉黑，本会话已解除；点一下恢复'>" +
        (nm ? esc(nm) + "<em class='aq'>" + esc(uid) + "</em>" : "<em class='aq'>" + esc(uid) + "</em>") +
        "<em class='aq'>已解除</em></span>";
    }).join("") +
      "<button type='button' class='chip-add' data-uadd='1' title='手动拉黑一个 QQ'>+</button>";
  }
  function ublkPaint(){
    var box = document.getElementById("ublk-chips");
    if(!box) return;
    box.innerHTML = ublkChipsHtml(ublkCache || { items: [], unblock_items: [] });
  }
  function ublkLoad(){
    return get(API + "/user-blacklist?sid=" + encodeURIComponent(scopeSid || "")).then(function(d){
      if(!d || !d.ok) return;
      ublkCache = { items: Array.isArray(d.items) ? d.items : [],
                    unblock_items: Array.isArray(d.unblock_items) ? d.unblock_items : [] };
      ublkPaint();
    }).catch(function(){ /* 静默：没登录 / 接口报错就保持「读取中」 */ });
  }
  function ublackBlockHtml(){
    setTimeout(ublkLoad, 0);
    // 用户 m07157：整块加个总开关。用户 m07715：开关的说明改成「被拉黑的人…」那段，
    // 原来那句「关掉后名单还在…」删掉，说明直接挂在开关这一行右边。
    var uOn = !!(configCache && configCache.user_blacklist_enabled);
    var uF = (schemaCache && schemaCache.user_blacklist_enabled) || {};
    var uHint = "被拉黑的人，消息不进请求上下文；窗口页照常显示，默认折叠，点开还能看。" +
      "拉黑之前已经在窗口里的历史照留，只剔之后的新消息。";
    return "<div class='ublk' id='ublk-box'>" +
      "<div class='setrow inline' data-row='user_blacklist_enabled'>" +
        "<div class='nm'>启用拉黑" +
          defaultBadge(uF, "user_blacklist_enabled") + "</div>" +
        "<div class='ds'>" + esc(uHint) + "</div>" +
        "<div class='ctl'>" + swPill("user_blacklist_enabled", uOn, "开启", "关闭", uHint) + "</div>" +
      "</div>" +
      "<div class='blk-list'>" +
        "<div class='chips' id='ublk-chips'>" + ublkChipsHtml(ublkCache || { items: [], unblock_items: [] }) + "</div>" +
        "<div class='blk-form' id='ublk-form' hidden>" +
          "<input class='ub-name' type='text' placeholder='名字（可空）' autocomplete='off'>" +
          "<input class='ub-qq' type='text' placeholder='QQ 号' autocomplete='off'>" +
          "<button type='button' class='blk-ok' data-uok='1'>拉黑</button>" +
          "<button type='button' class='blk-x' data-ucancel='1' title='取消（Esc）'>×</button>" +
        "</div>" +
      "</div>" +
    "</div>";
  }

  function rowKeysOwned(keys){
    if(!scopeSid) return false;
    for(var i = 0; i < (keys || []).length; i++){
      if(Object.prototype.hasOwnProperty.call(overrideCache, keys[i])) return true;
    }
    return false;
  }
  function paintOwnRows(){
    Array.prototype.forEach.call(document.querySelectorAll("#settings .setrow"), function(r){
      var keys = [];
      var rk = r.getAttribute("data-row");
      if(rk) keys.push(rk);
      Array.prototype.forEach.call(r.querySelectorAll("[data-k]"), function(el){
        var k = el.getAttribute("data-k");
        if(k) keys.push(k);
      });
      r.classList.toggle("own", rowKeysOwned(keys));
    });
  }

  // v3.15：触发页三列里只有一档在生效。trigger_mode 的取值是 count/probability/adaptive，
  // 前两个都算「普通模式」，adaptive 算「高级模式」；进阶模式没做，永远不点亮。
  // v3.16（用户 m40583）：三列标题各挂一个开关，普通模式列里再有两个子开关，
  // 全部读写 #trg-mode 这个隐藏 input；视觉同步（开关态 / 灰化 / 徽标）统一在 syncModeUI。
  var lastNormal = "count";   // 普通模式下上次选的子模式，从高级切回来时用它
  function trgModeValue(){
    var v = configCache ? configCache.trigger_mode : "count";
    return (v === "probability" || v === "adaptive") ? v : "count";
  }
  function trgVal(){
    var el = document.getElementById("trg-mode");
    var v = el ? String(el.value || "count") : "count";
    return (v === "probability" || v === "adaptive") ? v : "count";
  }
  function bandsOn(){
    var inp = document.querySelector("#settings .msw[data-msw='bands'] input");
    if(inp) return !!inp.checked;
    return !!(configCache && configCache.bands_enabled);
  }
  // v3.71（用户第 4 条后半）：私聊作用范围下不画曲线组，也就当它没开——
  // 普通模式列不该跟着整列变灰（私聊恒为必回，那一栏的概率照样能填）。
  function bandsActive(){
    return bandsOn() && !(scopeSid && cacheSidIsDM(scopeSid));
  }
  function syncModeUI(){
    // v3.71（用户第 4 条后半）：私聊没有曲线——bOn 按 false 算、当前档按普通模式算，
    // 普通模式列才不会被整列灰掉、点不动。
    var dmS = !!(scopeSid && cacheSidIsDM(scopeSid));
    var v = trgVal();
    if(v !== "adaptive") lastNormal = v;
    var cur = dmS ? "normal" : ((v === "adaptive") ? "adaptive" : "normal");
    var bOn = dmS ? false : bandsOn();
    // 三列：生效的那列正常，其余两列整块变灰且不可点（标题上的开关不受影响）
    // v3.17：基础页两列也用 data-col（left/right）做「同列多组」，它们没有模式开关，
    // 不该被这套三档灰化规则命中——只处理带 .msw 的列（触发页那三档）。
    // v3.33（第 9 条）：曲线开 → 普通模式整列灰（概率完全由曲线给）；曲线关 → 动态调整
    // 灰且开关禁用（它必须建立在曲线上）。
    Array.prototype.forEach.call(document.querySelectorAll("#settings .col[data-col]"), function(col){
      if(!col.querySelector(".msw")) return;
      var key = col.getAttribute("data-col");
      var off;
      if(key === "band") off = false;
      else if(key === "normal") off = bOn || (cur !== "normal");
      else if(key === "adaptive") off = !bOn;
      else off = (key !== cur);
      col.classList.toggle("off", off);
      // v3.92（用户 m06951 第 1 条）：灰列里那几档模式开关（data-sub）连键盘也禁掉，
      // 不然 Tab 过去按空格照样切档，跟“点不动”不一致。列标题上的 .msw 是回退入口，不动。
      Array.prototype.forEach.call(col.querySelectorAll(".rows input[data-sub]"), function(i){
        i.disabled = off;
      });
      var sw = col.querySelector(".msw");
      if(sw){
        var inp = sw.querySelector("input");
        if(inp){
          if(key === "band") inp.checked = bOn;
          else if(key === "normal") inp.checked = !bOn && (cur === "normal");
          else if(key === "adaptive") inp.checked = bOn && (cur === "adaptive");
          else inp.checked = !off;
          inp.disabled = (key === "adaptive" && !bOn);
        }
        sw.classList.toggle("soon", key === "adaptive" && !bOn);
      }
    });
    // v3.41（第 6 条）：两个模式开关并进参数行（.subrow），不再是标题下面那条 .subsw
    var c = document.querySelector("#settings input[data-sub='count']");
    var p = document.querySelector("#settings input[data-sub='probability']");
    if(c) c.checked = (v === "count");
    if(p) p.checked = (v === "probability");
    // 子模式下用不上的参数行（计数阈值 / 概率 / 伪随机）跟着灰掉
    refreshDimRows();
    // v3.71（用户第 7 条）：曲线下面那行动态调整的亮 / 灰与总开关跟着模式一起刷
    paintAdaptiveRow();
    Array.prototype.forEach.call(document.querySelectorAll("#settings .h3use[data-mode]"), function(el){
      var m = el.getAttribute("data-mode");
      // 曲线开着的时候「高级模式」是曲线 + 动态调整两段共用，两个徽标都该亮
      el.hidden = (m === "bands") ? !bOn : (bOn ? (m !== "adaptive" || cur !== "adaptive") : (m !== cur));
    });
  }
  // 旧调用点（renderSettings 末尾）保留这个名字，内部走同一套同步
