  (function(){
    try{
      var nav = (performance.getEntriesByType && performance.getEntriesByType("navigation")[0]) || null;
      var ntype = nav ? String(nav.type || "") : "";
      if(!ntype && performance.navigation){
        ntype = performance.navigation.type === 1 ? "reload" : "navigate";
      }
      // 用户 m30734：插件页在面板的 iframe 里，父页面按 F5 时 iframe 自己拿到的是
      // navigate（不是 reload），只看自己的导航类型就会退回第一页。父页面同源，
      // 顺手读一下它的导航类型，父页面是刷新/前进后退就一起算刷新。
      var ptype = "";
      try{
        var pnav = (window.parent && window.parent !== window && window.parent.performance)
          ? window.parent.performance.getEntriesByType("navigation")[0] : null;
        ptype = pnav ? String(pnav.type || "") : "";
      }catch(e2){ ptype = ""; }
      if(ntype === "reload" || ntype === "back_forward" ||
         ptype === "reload" || ptype === "back_forward"){
        var saved = sessionStorage.getItem("mdp_cur_page") || "";
        if(saved) curPage = saved;
      }
    }catch(e){}
  })();
  // 用户 m07864 第 1 条：头部说明文字统一走 setSub()，顺手记进 sessionStorage。
  // 再进这一页时脚本一开始就把上次的文案写回去，不会先闪「加载中…」再跳成长句。
  (function(){
    var t = "";
    try { t = sessionStorage.getItem(SET_SUB_KEY) || ""; } catch(e){ t = ""; }
    var el = document.getElementById("set-sub");
    if(el && t) el.textContent = t;
  })();
  function jevSavePages(){
    var clean = jevPages().map(function(pg){
      return {
        id: String(pg.id || "1"),
        name: String(pg.name || ""),
        protocol: jevPageProtocol(pg),
        base: String(pg.base || ""),
        model: String(pg.model || "")
      };
    });
    if(configCache) configCache["jev_apis"] = JSON.stringify(clean);
    dirty = true;
    saveNow();
  }
  function jevAddPage(){
    var pages = jevPages();
    if(pages.length >= 12){ alert("最多 12 页。"); return; }
    var id = jevNextPageId();
    pages.push({id: id, name: "", protocol: "systemone", base: "", model: "", api_key_set: false, api_key_masked: ""});
    JEV_PAGE = id;
    JEV_MODELS = [];
    JEV_MODEL_ERR = "";
    jevSavePages();
    jevRerenderCard();
  }
  function jevDelPage(){
    var pages = jevPages();
    if(pages.length <= 1) return;
    var dying = jevPage();
    var gone = String(dying.id || "1");
    // 页删了，它那份密钥也别留在插件数据目录里。
    if(dying.api_key_set) fetch(API + "/jev-key", {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({api_key: "", id: gone})
    });
    JEV_APIS = pages.filter(function(pg){ return String(pg.id) !== gone; });
    JEV_PAGE = String(JEV_APIS[0].id || "1");
    // 选着这一页的模型键要一起清掉，否则会指向不存在的页。
    ["jev_model", "jev_fallback_model"].forEach(function(k){
      if(String(dVal(k, "")) === "custom:" + gone && configCache) configCache[k] = "";
    });
    jevSavePages();
    jevRefresh();
    jevRerenderCard();
  }

  // 用户 m01978 第 3 条：点「填入API」那一行的名字改名。
  function jevRenamePage(){
    var pg = jevPage();
    if(!pg) return;
    mdPrompt({
      title: "给这个接口改名",
      message: "名字会显示在这一行的标签、模型下拉和页签悬浮上。",
      value: jevPageName(pg),
      placeholder: "填入API",
      ok: "保存"
    }).then(function(name){
      if(name === null) return;
      pg.name = String(name || "").trim().slice(0, 24);
      jevSavePages();
      jevRerenderCard();
      jevRefresh();
    });
  }

  function ppLoad(s, discard, warm){
    if(s.loading) return s.loading;
    var edit = s.editSeq;
    s.error = ""; s.seq += 1;
    // 带上时间戳：布局接口是按 kind+sid 取的，浏览器会拿旧的缓存盖上新的默认模板名（用户图1/图6）。
    var url = "/prompt-layout?kind=" + s.kind + "&sid=" + encodeURIComponent(s.sid) + "&_=" + Date.now();
    s.loading = ppRequest(url).then(function(d){
      if(!d.layout || !d.default_layout || !Array.isArray(d.catalog)) throw new Error("提示词接口缺少模板或目录");
      var replace = edit === s.editSeq && (!s.loaded || discard || !s.dirty);
      s.catalog = d.catalog; s.defaultLayout = ppClone(d.default_layout); s.contextSid = String(d.context_sid || "");
      s.toolChoices = Array.isArray(d.tool_choices) ? d.tool_choices : []; s.toolsSelected = Array.isArray(d.tools_selected) ? d.tools_selected.map(String) : [];
      s.officialReference = d.official_reference || null; s.nativeTemplates = d.native_templates || null;
      s.pluginSegs = [];
      if(replace){
        s.layout = ppClone(d.layout); s.revision = d.revision; s.inherited = !!d.inherited;
        s.defNotice = d.default_notice || null;
        ppAdoptSkeletons(s);
        // 用户第 ⑥ 条：布局换了以后，本机记的折叠 id 只认还在的那些，删掉的顺手清掉。
        ppFoldPrune(s);
        s.dirty = false; s.preview = d.preview || null; s.notice = "";
      }else if(d.revision !== s.revision){
        // 用户 m14457 图1：决策/回复两个 tab 各持一份 revision，另一边保存过之后，
        // 这边每勾一次「启用」都拿旧 revision 去存 → 409，磁盘没变，看着像「勾了没生效」。
        // revision 是整份 _data 的哈希，但保存只写自己这个 kind，所以跟进版本号不会覆盖别人。
        s.revision = d.revision;
        s.notice = "服务端版本已有变化；草稿未被覆盖，保存时将检查版本。";
      }
      s.loaded = true; s.loading = null; ppRenderEditor(s); ppPaintPreview(s);
      // 用户 m08755：JEV 页一打开就把真实实时窗取一次，测试才有内容可用。
      // 用户 m09506：决策 / 回复页的实时窗也要真实内容，三页都取。
      if(!s.rtFetched){ s.rtFetched = true; ppLoadWindowText(s).then(function(){ ppRenderEditor(s); ppPaintPreview(s); }); }
      if(s.referencesPending) return ppRefreshReferences(s);
      // 预热另一页只为填「＋ 新增」下拉，用不到预览，别为它重排一遍 system。
      if(warm) return null;
      // 服务端随布局一起给的预览就是这份布局的结果：补骨架 key 只改前端认模块用的 key，
      // 服务端装配文本模块时根本不读它（state/prompt_layout.py 归一化时把 text 的 key 清空），
      // 所以正文不会变，直接用它，不再为同一份布局重排一次。
      if(replace && s.preview && typeof s.preview.system === "string"){
        s.previewPending = false; ppPaintPreview(s); return s.preview;
      }
      return ppPreview(s);
    }).catch(function(err){
      s.loading = null; s.previewPending = false; s.error = "读取失败（草稿保留）：" + err.message; ppRenderEditor(s); ppPaintPreview(s);
      return s.referencesPending ? ppRefreshReferences(s) : null;
    });
    return s.loading;
  }
  function ppSave(s, inherit){
    if(!s.loaded || s.saving) return Promise.resolve(false);
    if(s.loading) return s.loading.then(function(){ return ppSave(s,inherit); });
    // 用户 m09506：存之前再补一次 key——只在载入/ACK 后补的话，第一次保存仍然把空 key 写进服务端，
    // 下次打开又成了一条「同名但没接回模板」的记录。正文不动，只补 key。
    if(!inherit) ppAdoptSkeletons(s);
    var edit = s.editSeq, payload = {kind:s.kind,sid:s.sid,layout:inherit ? null : ppClone(s.layout),expected_revision:s.revision};
    s.saving = true; s.error = ""; s.notice = ""; ppPaintStatus(s);
    return ppRequest("/prompt-layout",payload).then(function(d){
      if(!d.layout || !Array.isArray(d.catalog)) throw new Error("保存响应缺少已确认的模板");
      // ACK this exact save only. Never replace a newer draft typed during the request.
      s.revision = d.revision; s.catalog = d.catalog; s.contextSid = String(d.context_sid || "");
      if(Array.isArray(d.tool_choices)) s.toolChoices = d.tool_choices;
      if(Array.isArray(d.tools_selected)) s.toolsSelected = d.tools_selected.map(String);
      s.officialReference = d.official_reference || null; s.nativeTemplates = d.native_templates || null;
      if(d.default_layout) s.defaultLayout = ppClone(d.default_layout);
      s.inherited = !!d.inherited; s.saving = false;
      if(d.default_notice) s.defNotice = d.default_notice;
      if(edit === s.editSeq){
        s.layout = ppClone(d.layout); ppAdoptSkeletons(s); s.dirty = false; s.preview = d.preview || s.preview;
        s.notice = inherit ? "已恢复继承，无需重载。" : "已保存，无需重载。";
      }else{
        s.dirty = true; s.notice = "先前版本已保存；刚输入的新草稿仍未保存。";
      }
      ppRenderEditor(s); ppPaintPreview(s);
      if(s.referencesPending) ppRefreshReferences(s); else ppPreview(s);
      return true;
    }).catch(function(err){
      s.saving = false;
      var conflict = err.status === 409 || /conflict/i.test(err.code || "");
      s.error = conflict ? "版本冲突：未覆盖服务端，草稿保留。请先复制需要的改动，再刷新页面重新读取。" : "保存失败（草稿保留）：" + err.message;
      ppPaintStatus(s);
      if(s.referencesPending) ppRefreshReferences(s);
      return false;
    });
  }
  function ppModuleAction(s, action, index, selected, zone){
    if(!s.loaded) return;
    var list = s.layout.modules, module = list[index];
    // 用户 m09986 第 8 条：折叠 / 展开一张卡——只是看的时候收起来，不动布局、不标脏。
    if(action === "fold"){
      s.collapsed = s.collapsed || {};
      var fid = String((module && module.id) || index);
      if(s.collapsed[fid]) delete s.collapsed[fid]; else s.collapsed[fid] = 1;
      // 用户第 ⑥ 条：折叠状态记进 localStorage，刷新页面还照旧收着。
      ppFoldSave(s);
      ppRenderEditor(s);
      return;
    }
    if(action === "add"){
      var pair;
      try{ pair = JSON.parse(selected || '["text",""]'); }catch(e){ return; }
      // 用户 m28586 第 4 条：把本页自己的自定义模块再复制一份——照原样复制成一块文本，排在系统段末尾。
      if(Array.isArray(pair) && String(pair[0] || "") === "clone"){
        var spec = String(pair[1] || "").split("#");
        var cst = ppState(String(spec[0] || "")), cmod = null;
        if(cst && cst.layout && cst.layout.modules) cmod = cst.layout.modules[Number(spec[1] || -1)];
        if(!cmod) return;
        var cm = {id:"pm-" + Date.now().toString(36) + "-" + (++ppModuleCounter), type:"text", key:"",
                  label:String(cmod.label || "自写文本"), content:String(cmod.content || ""), enabled:true};
        var cat = list.length;
        for(var ci = 0; ci < list.length; ci++){
          var ct = String((list[ci] || {}).type || "");
          if(ct === "realtime_marker" || ct === "dynamic" || ct === "system_dynamic" || ct === "module"){ cat = ci; break; }
        }
        list.splice(cat, 0, cm);
        ppChanged(s,true); return;
      }
      if(!Array.isArray(pair) || pair.length !== 2 || !ppTypeAllowed(s,pair[0])) return;
      // 用户 m04617 图4：新增条上选了「加到动态段」，就建一块动态段（拼在聊天记录最后）。
      if(String(zone || "") === "dynamic"){
        module = {id:"pm-" + Date.now().toString(36) + "-" + (++ppModuleCounter), type:"dynamic", key:"",
                  label:"动态补充段", content:"", enabled:true};
        list.push(module); ppChanged(s,true); return;
      }
      module = {id:"pm-" + Date.now().toString(36) + "-" + (++ppModuleCounter),type:pair[0],key:String(pair[1] || ""),label:"自写文本",content:"",enabled:true};
      var ref = ppRef(s,module);
      // 用户 m01209 图2：小模块骨架点进来就把正文带上，不用自己从头写。
      if(ref && pair[0] === "text" && ref.skeleton && typeof ref.text === "string") module.content = ref.text;
      if(ref) module.label = String(ref.label || module.type);
      // 用户 m23323 图3：选「系统段」新增的模块落在「实时窗口」上面——系统段永远排在
      // 聊天记录之前，不能因为后加就跑到最底下去。动态段仍按原来的路径追加到最后。
      var at = list.length;
      for(var zi = 0; zi < list.length; zi++){
        var zt = String((list[zi] || {}).type || "");
        if(zt === "realtime_marker" || zt === "dynamic" || zt === "system_dynamic" || zt === "module"){ at = zi; break; }
      }
      list.splice(at, 0, module);
    }else if(action === "remove" && module){
      // 用户 m02159 图3：人格 / 身份 / 实时窗口是固定段，只能放空或换人格，不能整段删掉。
      if(module.type === "persona" || module.type === "identity" || module.type === "realtime_marker"
         || module.type === "plugin_memory" || module.type === "plugin_memory_ctx" || module.type === "module"
         || module.type === "plugin_seg") return;
      list.splice(index,1);
      // 用户第 ⑥ 条：这一段没了，本机记的折叠状态里也别再留着它的 id。
      ppFoldPrune(s);
    }
    else if(action === "up" && module && index > 0){
      // 用户 m26048：动态段（强化格式遵守 / 记忆插件·本轮上下文）注入的是聊天记录末尾，
      // 不是系统提示词，所以只能在「实时窗口」下面挪，不能挪到它上面去。
      var upType = String(module.type || "");
      if((upType === "dynamic" || upType === "plugin_memory_ctx" || upType === "module")
         && String((list[index-1] || {}).type || "") === "realtime_marker") return;
      list.splice(index,1); list.splice(index-1,0,module);
    }
    else if(action === "down" && module && index < list.length-1){
      // 用户 m23323 图3：任何一段都不能越过「实时窗口」往下——越过就跑到聊天记录后面去了。
      if(String((list[index+1] || {}).type || "") === "realtime_marker") return;
      list.splice(index,1); list.splice(index+1,0,module);
    }
    else if(action === "dup" && module){
      // 用户 m27528 第 5 条：模块也能复制一份——副本排在原段后面，名字带「（副本）」后缀
      // （来源下拉里一眼认得出），正文照抄；动副本不碰原段。
      if(module.type === "persona" || module.type === "identity" || module.type === "realtime_marker"
         || module.type === "plugin_memory" || module.type === "plugin_memory_ctx" || module.type === "module"
         || module.type === "plugin_seg") return;
      var cp = {};
      Object.keys(module).forEach(function(k){ cp[k] = module[k]; });
      cp.id = "pm-" + Date.now().toString(36) + "-" + (++ppModuleCounter);
      cp.label = String(module.label || "这一段") + "（副本）";
      if(cp.type !== "text" && cp.type !== "custom"){ cp.type = "text"; cp.key = ""; cp.content = ppReferenceText(s, module); }
      list.splice(index + 1, 0, cp);
    }
    else if(action === "promote" && module){ ppPromote(s, index); return; }
    else if(action === "edit_tpl" && module && module.type === "custom"){
      // v4.84（用户 m09179 第 7 条）：模板库的编辑界面撤了，这个按钮改成跳到自定义页
      // 对应的那张卡上——正文就在那儿改，改完点「保存这一段」。
      ppTplSel[s.kind] = s.kind + "#" + index;
      ppSetPane("templates");
      setTimeout(function(){
        var card = document.querySelector("#pp-tpl [data-self-card='" + s.kind + "#" + index + "']");
        if(card && card.scrollIntoView) card.scrollIntoView({block:"center"});
        var ta = card ? card.querySelector("textarea") : null;
        if(ta && ta.focus) ta.focus();
      }, 80);
      return;
    }
    else if(action === "convert" && module && module.type !== "text"){
      if(!ppCanConvert(s,module)) return;
      module.content = ppReferenceText(s,module); module.type = "text"; module.key = "";
    }else return;
    ppChanged(s,true);
  }
  function ppRefreshReferences(s){
    // A successful ordinary save may land during a module GET/save. Refresh once
    // after that request settles, never race its catalog/revision ACK.
    if(s.saving || s.loading){ s.referencesPending = true; return; }
    s.referencesPending = false;
    var error = s.error, edit = s.editSeq, refresh = ppLoad(s,false);
    // Automatic catalog refresh must not erase a failed save/conflict message.
    if(error){ s.error = error; ppPaintStatus(s); }
    return refresh.then(function(value){
      if(error && edit === s.editSeq && s.error !== error){ s.error = error + (s.error ? " · " + s.error : ""); ppPaintStatus(s); }
      return value;
    });
  }
  function ppReferencesSaved(sid, epoch){
    // 用户 m22158：改「Agent循环数 / 单轮工具上限」这类数字不走工具勾选那条路，
    // 以前只有勾工具（PP_REF_DIRTY 自增）才会重排预览，数字改了右边一直显示旧值。
    // 现在配置一存完就让当前这一页的预览按新值重排一次。
    if(!epoch){
      if(curPage === "prompt" && ppPane !== "templates"){
        var cur = ppState(ppPane);
        if(cur && cur.loaded) ppPreview(cur);
      }
      return;
    }
    if(PP_REF_DIRTY[sid] === epoch) delete PP_REF_DIRTY[sid];
    loadDecisionOptions();
    loadJevOptions();
    Object.keys(PP_STATES).forEach(function(k){
      var s = PP_STATES[k];
      // Global persona/tools can be inherited by any cached scope. Dirty states
      // still keep their original draft/revision in ppLoad.
      if((sid === "" || s.sid === sid) && s.loaded) ppRefreshReferences(s);
    });
  }
  function ppEnsureTplStates(){
    ["decision", "reply", "jev"].forEach(function(k){
      var st = ppState(k);
      if(!st || st.loaded || st.loading) return;
      ppLoad(st, false).then(function(){ if(ppPane === "templates") ppTplRender(); }, function(){});
    });
  }
  function ppSetPane(name){
    ppPane = (name === "reply" || name === "jev" || name === "templates") ? name : "decision";
    Array.prototype.forEach.call(document.querySelectorAll("#settings .pp-tab"),function(b){ b.classList.toggle("on",b.getAttribute("data-pp") === ppPane); });
    Array.prototype.forEach.call(document.querySelectorAll("#settings .pp-pane"),function(p){ p.hidden = p.getAttribute("data-pane") !== ppPane; });
    // 用户 m3096：模型卡跟着子页签走，切过来才量得到标签列宽（隐藏时量出来是 0）。
    ppSyncPairLabel();
    if(ppPane === "templates"){ ppEnsureTplStates(); ppTplRender(); return; }
    ppAutoPreview();
  }
  // 用户 m16307 图1：pair2 那行的标签列要跟网格第一列一样宽，开关才跟上面的下拉左边缘对齐。
  // 第一列是 max-content（决策卡最长标签 5 字、回复卡 4 字，两张卡宽度不一样），所以量出来写进 --dlbw。
  function ppWarmOther(kind){
    // 用户 m06706：JEV 页的自定义模块也要能被另外两页的下拉读到，所以它去预热决策那页。
    var st = ppState(kind === "decision" ? "reply" : "decision");
    if(!st || st.loaded || st.loading) return;
    ppLoad(st, false, true).then(function(){ if(curPage === "prompt" && ppPane === kind) ppRenderEditor(ppState(kind)); }, function(){});
  }
  // 用户 m04756：预热另一页会跟首屏的实时窗 / 预览抢服务端，等这一页画完再补。
  var ppWarmTimer = 0;
  function ppWarmLater(kind){
    if(ppWarmTimer) clearTimeout(ppWarmTimer);
    ppWarmTimer = setTimeout(function(){ ppWarmTimer = 0; ppWarmOther(kind); }, 900);
  }
  function ppAutoPreview(){
    if(curPage !== "prompt") return;
    ppLayout();
    if(ppPane === "templates"){ ppEnsureTplStates(); ppTplRender(); return; }
    var s = ppState(ppPane);
    ppRenderEditor(s); ppPaintPreview(s);
    // 用户 m15846 图4：读到了布局却没拿到预览（预热那条路）时补一次，别让预览区空着。
    var p = (s.loaded && s.preview) ? ppPreview(s) : ppLoad(s,false);
    // 另一页只用来填「＋ 新增」下拉：等当前页首屏出来再读，别跟首屏抢请求。
    Promise.resolve(p).then(function(){ ppWarmLater(ppPane); }, function(){ ppWarmLater(ppPane); });
    return p;
  }

  // 数据比页面晚到时的补填：只重画 option / 勾选清单，不动已经填了一半的 textarea。
  function bindDecisionPage(){
    var box = document.getElementById("settings");
    if(!box || box.__promptBound) return;
    box.__promptBound = true;
    function editorFor(t){ return t && t.closest ? t.closest("[data-pp-editor]") : null; }
    function inputChanged(e){
      var t = e.target;
      if(t && t.getAttribute && t.getAttribute("data-self-field")){
        var sst0 = ppState(t.getAttribute("data-self-kind"));
        var smi0 = Number(t.getAttribute("data-self-index"));
        var sm0 = (sst0 && sst0.layout && sst0.layout.modules) ? sst0.layout.modules[smi0] : null;
        if(sm0){ sm0.content = String(t.value); ppChanged(sst0, false); }
        return;
      }
      var root = editorFor(t);
      if(!root || !t.getAttribute) return;
      var toolName = t.getAttribute("data-pm-tool");
      if(toolName){
        var tst = ppState(root.getAttribute("data-pp-editor"));
        ppToggleTool(tst, toolName, !!t.checked);
        return;
      }
      var field = t.getAttribute("data-pm-field");
      if(field !== "label" && field !== "content" && field !== "text") return;
      var st = ppState(root.getAttribute("data-pp-editor")), at = Number(t.getAttribute("data-pm-index"));
      if(field === "content" && t.getAttribute("data-pm-promote")){
        var mod = st.layout && st.layout.modules ? st.layout.modules[at] : null;
        // 用户 m27528 第 5 条：只有正文真的跟原文不一样了才转「自写」。
        // 点开看看、重新渲染一遍、光标进去又出来，都不算改——以前一碰就悄悄复制出一份副本。
        if(mod && mod.type !== "text" && mod.type !== "custom"){
          if(ppTextSame(t.value, ppReferenceText(st, mod))){ return; }
          ppPromote(st, at, t.value); return;
        }
      }
      ppUpdateField(st,field,at,t.value);
    }
    // 用户 m08755 第 6 条：实时窗的下拉 / 刷新，只重画这一页，不动布局草稿。
    box.addEventListener("change", function(e){
      var t = e.target;
      if(!t || !t.getAttribute || !t.getAttribute("data-rt-sid")) return;
      var ed = editorFor(t); if(!ed) return;
      var s = ppState(ed.getAttribute("data-pp-editor"));
      s.rtSid = String(t.value || ""); s.rtText = ""; s.rtInfo = "读取中…";
      ppRenderEditor(s);
      ppLoadWindowText(s).then(function(){ ppRenderEditor(s); ppPaintPreview(s); });
    }, true);
    box.addEventListener("click", function(e){
      var t = e.target;
      var rf = t && t.closest ? t.closest("[data-rt-reload]") : null;
      if(!rf) return;
      var ed = editorFor(t); if(!ed) return;
      var s = ppState(ed.getAttribute("data-pp-editor"));
      s.rtText = ""; s.rtInfo = "读取中…"; ppRenderEditor(s);
      ppLoadWindowText(s).then(function(){ ppRenderEditor(s); ppPaintPreview(s); });
      e.preventDefault();
    }, true);
    box.addEventListener("input",inputChanged);
    box.addEventListener("change",function(e){
      var t = e.target, root = editorFor(t);
      if(root && t.getAttribute){
        var field = t.getAttribute("data-pm-field");
        if(field === "type" || field === "enabled"){
          var stPM = ppState(root.getAttribute("data-pp-editor"));
          ppUpdateField(stPM,field,Number(t.getAttribute("data-pm-index")),field === "enabled" ? t.checked : t.value);
          // v4.34（用户 m13976 第 1/3 条）：勾「启用」以前只改草稿，不点右下角保存
          // 就等于没开 —— 决策页那个「系统动态段」正是这么勾了却没生效的：开关一眼
          // 就能看出变化，勾完直接存。
          if(field === "enabled") ppSave(stPM,false);
        }
        return; // Module fields have no data-k and never enter generic auto-save.
      }
      var key = t && t.getAttribute ? t.getAttribute("data-k") : "";
      if(key === "decision_tools"){
        var refSid = String(scopeSid || "");
        PP_REF_DIRTY[refSid] = (PP_REF_DIRTY[refSid] || 0) + 1;
        var s = ppState(ppPane);
        if(s.loaded){ s.notice = "工具待普通设置自动保存，引用随后刷新。"; ppPaintStatus(s); }
      }
      if(!t || !t.classList || !t.classList.contains("dtchk")) return;
      var hid = document.getElementById("dec-tools");
      if(!hid) return;
      hid.value = JSON.stringify(dPickedNow());
      hid.dispatchEvent(new Event("change",{bubbles:true}));
    });
    box.addEventListener("click",async function(e){
      var t = e.target;
      if(!t) return;
      var tab = t.closest ? t.closest("[data-pp]") : null;
      if(tab){ ppSetPane(tab.getAttribute("data-pp")); e.preventDefault(); return; }
      // v4.10：右栏标题带上的「复制 / 放大」，以及模块正文折叠状态的记忆（折叠状态存在 s.open，重渲染后不丢）。
      // 用户 m18121：自定义页先只露标题，点一下就在那一列下面展开编辑框；再点一下收起。
      var pick = t.closest ? t.closest("[data-self-pick]") : null;
      if(pick){
        e.preventDefault();
        var pk = pick.getAttribute("data-self-pick") || "";
        var pkind = pk.split("#")[0];
        if(pkind === "decision" || pkind === "jev" || pkind === "reply"){
          ppTplSel[pkind] = (ppTplSel[pkind] === pk) ? "" : pk;
          ppTplRender();
        }
        return;
      }
      var selfBtn = t.closest ? t.closest("[data-self-act]") : null;
      if(selfBtn){
        e.preventDefault();
        var sact = selfBtn.getAttribute("data-self-act");
        var smk = selfBtn.getAttribute("data-self-kind");
        var smi = Number(selfBtn.getAttribute("data-self-index"));
        var sst = ppState(smk);
        if(sact === "save" && sst && sst.loaded){
          ppSave(sst, false).then(function(ok){
            PP_SELF_NOTICE = ok ? "这一段已存进提示词。" : "";
            ppTplRender();
          });
        }else if(sact === "jump" && sst && sst.loaded){
          if(!sst.open) sst.open = {};
          sst.open[smi] = true;
          ppSetPane(smk);
          setTimeout(function(){
            var ed = ppEditorEl(smk);
            var card = ed ? ed.querySelector("[data-pm-module='" + smi + "']") : null;
            if(card && card.scrollIntoView) card.scrollIntoView({block:"center"});
          }, 80);
        }
        return;
      }
      var copyBtn = t.closest ? t.closest("[data-pp-copy]") : null;
      if(copyBtn){ ppCopy(copyBtn.getAttribute("data-pp-copy"), copyBtn); e.preventDefault(); return; }
      var expandBtn = t.closest ? t.closest("[data-pp-expand]") : null;
      if(expandBtn){
        var exKind = expandBtn.getAttribute("data-pp-expand");
        var exPane = document.querySelector(".pp-prev[data-pp-prev='" + exKind + "']");
        if(exPane){
          // 用户 m15201：图标按钮，放大 / 还原靠字形和悬浮说明区分。
          var big = exPane.classList.toggle("big");
          expandBtn.textContent = big ? "\u2921" : "\u2922";
          expandBtn.setAttribute("title", big ? "还原" : "放大看");
        }
        e.preventDefault(); return;
      }
      var sum = t.closest ? t.closest("summary") : null;
      if(sum && sum.classList && sum.classList.contains("pm-sum")){
        var sumDet = sum.parentNode, sumEd = editorFor(sum);
        if(sumDet && sumEd){
          var sumKind = sumEd.getAttribute("data-pp-editor"), sumIx = Number(sumDet.getAttribute("data-pm-open"));
          // 点 summary 时浏览器还没翻转 open，等一拍再读。
          setTimeout(function(){ if(!isNaN(sumIx)) ppState(sumKind).open[sumIx] = !!sumDet.open; },0);
        }
      }
      // 用户 m17185：清空试跑结果，把上面的预览空间还回去。
      if(t.id === "dec-test-clear" || t.id === "jev-test-clear" || t.id === "rep-test-clear"){
        ppTestClear(t.id.replace("-test-clear", ""));
        e.preventDefault(); return;
      }
      // 用户 m17185：预览框右上角的折叠按钮，默认展开。
      var capTg = t.closest ? t.closest(".pp-cap-tg") : null;
      if(capTg){
        var capBox = capTg.closest(".pp-prev-box");
        if(capBox){
          var folded = capBox.classList.toggle("folded");
          capTg.textContent = folded ? "\u25b8" : "\u25be";
          capTg.setAttribute("title", folded ? "展开" : "折叠");
        }
        e.preventDefault(); return;
      }
      if(t.id === "dec-test"){
        var ds = ppState("decision");
        if(ds.loaded && !ds.dirty && !ds.saving) dcTest();
        e.preventDefault(); return;
      }
      if(t.id === "jev-test"){
        var js2 = ppState("jev");
        if(js2.loaded && !js2.dirty && !js2.saving) jevTest();
        e.preventDefault(); return;
      }
      if(t.id === "rep-test"){
        var rs2 = ppState("reply");
        if(rs2.loaded && !rs2.dirty && !rs2.saving) repTest();
        e.preventDefault(); return;
      }
      // 用户 m13212 图1：「系统段 / 动态段」两个标签，点哪个就把藏着那个 select 的值换过去。
      var zoneBtn = t.closest ? t.closest("[data-pm-zone-pick]") : null;
      if(zoneBtn && !zoneBtn.disabled){
        var zRoot = editorFor(zoneBtn), zVal = zoneBtn.getAttribute("data-pm-zone-pick") === "dynamic" ? "dynamic" : "system";
        if(zRoot){
          var zBtns = zRoot.querySelectorAll("[data-pm-zone-pick]");
          Array.prototype.forEach.call(zBtns,function(b){ b.classList.toggle("on", b === zoneBtn); });
          var zSel = zRoot.querySelector("select[data-pm-zone]");
          if(zSel) zSel.value = zVal;
          var zState = ppState(zRoot.getAttribute("data-pp-editor"));
          if(zState) zState.zonePick = zVal;
        }
        e.preventDefault(); return;
      }
      var button = t.closest ? t.closest("[data-pm-action]") : null;
      if(!button || button.disabled) return;
      e.preventDefault();
      var s = ppState(button.getAttribute("data-pm-kind")), action = button.getAttribute("data-pm-action");
      if(action === "save"){ ppSave(s,false); return; }
      if(action === "reload"){
        if(s.dirty && !(await mdConfirm("重新读取会放弃这份未保存草稿。请先复制需要保留的改动，确定继续？",{danger:true,ok:"放弃草稿并读取"}))) return;
        ppLoad(s,true); return;
      }
      if(action === "inherit"){
        if(!(await mdConfirm((s.sid ? "删除当前会话的提示词覆盖，恢复跟随全局？" : "删除全局提示词覆盖，恢复默认？") + "当前草稿会被替换，失败时保留草稿。",{danger:true,ok:"恢复并保存"}))) return;
        ppSave(s,true); return;
      }
      if(action === "notice-off"){ ppDismissNotice(s, button.getAttribute("data-pm-module") || ""); return; }
      if(action === "notice-adopt"){ ppAdoptDefault(s, Number(button.getAttribute("data-pm-index"))); return; }
      var root = editorFor(button), add = root ? root.querySelector("[data-pm-add]") : null;
      var zone = root ? root.querySelector("[data-pm-zone]") : null;
      ppModuleAction(s,action,Number(button.getAttribute("data-pm-index")),add ? add.value : undefined,
        zone ? zone.value : "");
    });
    var resizeFrame = 0;
    window.addEventListener("resize", function(){
      if(resizeFrame) return;
      resizeFrame = requestAnimationFrame(function(){
        resizeFrame = 0;
        if(curPage === "prompt") ppLayout();
        document.querySelectorAll("#settings .setgrp[data-page='" + curPage + "']").forEach(silentFitNotes);
      });
    });
  }

  // 用户 2026-11-06：转发消息处理是左右格式的分段按钮，说明跟着选中项走。
  function adminSync(list){
    var raw = JSON.stringify(list);
    if(configCache) configCache.admin_list = raw;
    var h = document.querySelector("#settings [data-k='admin_list']");
    if(h) h.value = raw;
    // 用户 m07085 第 1 条：名字行 / QQ 号行是同一份数据的两面，改一处两行一起刷新。
    // v3.53（用户本轮第 1 条）：只换胶囊，不碰 .adform 与 .chip-add。原来整块 innerHTML 重建，
    // 连正在点的「添加」按钮和两个输入框一起销毁重建——点一次就重建一次 DOM、输入框跟着失焦，
    // 所以手感发卡、连加两位要反复点。现在先摘旧胶囊，再把新胶囊插在「+」前面，表单节点原样留着；
    // 容器里还没有「+」（首屏 / 老结构）才退回整块重建一次，保证第一次渲染可用。
    syncAdminChips(document.querySelector("#settings [data-admin]"), list.map(adminNameChipHtml).join(""));
    syncAdminChips(document.querySelector("#settings [data-admin-qq]"), list.map(adminQqChipHtml).join(""));
    dirty = true;
    scheduleSave();
  }
  function adminCommit(form){
    if(!form) return;
    var ne = form.querySelector(".ad-name"), qe = form.querySelector(".ad-qq");
    var nmv = (ne && ne.value ? String(ne.value) : "").trim();
    var qqv = (qe && qe.value ? String(qe.value) : "").trim();
    if(!nmv && !qqv) return;   // 两项都空 = 没填，别加一个空胶囊
    var list = adminList();
    var dup = list.some(function(x){ return x.name === nmv && x.qq === qqv; });
    if(!dup) list.push({ name: nmv, qq: qqv });
    adminSync(list);
    // v3.53（用户本轮第 1 条）：加完不关浮层——清空两个框、把「+」继续藏着、焦点回名字框，
    // 连着加下一位不用再点一次「+」。只有 × / Esc / 两个框都空且失焦才收（见 closeAdminForm）。
    if(ne) ne.value = "";
    if(qe) qe.value = "";
    form.hidden = false;
    var addBtn = form.parentNode ? form.parentNode.querySelector(".chip-add") : null;
    if(addBtn) addBtn.hidden = true;
    if(ne && ne.focus) ne.focus();
  }

  // v3.33（用户 m51046 第 11 条）：不触发短语——一行开关 + 一份清单。
  // 用户 m07418：清单从「编辑清单」弹窗改成页内胶囊（跟外号 / 管理员一个手感），
  // 每个短语一枚 chip、末尾「+」点开输入框回车即加；清单本体仍是隐藏 input
  // （data-k=phrase_filter_list，ui_schema 里这个键的 type 是 string），collectSettings 照常提交。
  SET_GROUPS = (function mergeSectRows(list){
    var out = [];
    list.forEach(function(g){
      var last = out[out.length - 1];
      if(g.rowline && last && last.sect && !last._closed){
        (last.merged = last.merged || []).push(g);
        return;
      }
      if(last) last._closed = 1;
      out.push(g);
    });
    return out;
  })(SET_GROUPS);

  // 用户 m06193：用户黑名单——按 QQ 号整条剔除。窗口页把鼠标放到名字上也能拉黑。
  // 用户 m07418：总表从四列表格改成页内胶囊——一人一枚，× 就是解黑，末尾「+」手动填 QQ 拉黑。
  // 拉黑只影响请求上下文：窗口页照常显示（默认折叠），拉黑之前的历史也照留。
  // 性能：整页表单 800+ 节点，首帧缓存和 loadSettings 回包都会重画一遍。
  // 同一份 HTML 不重复写 innerHTML（跟首页 renderCtxList 的 lastListSig 一个思路）。
  // 注意：跳过写入时后面的绑定逻辑照跑——它们都幂等，也不依赖「DOM 每次都是新的」。
  var lastSettingsSig = "";
  function renderSettings(force){
    var box = document.getElementById("settings");
    if(!schemaCache){ lastSettingsSig = ""; box.innerHTML = '<div class="empty">读取配置中…</div>'; return; }
    var used = {};
    // trigger_mode 由三个模式开关承载（不是普通行），标成已用，别被再渲染一份
    used["trigger_mode"] = 1;
    // 隐藏 input 承载 trigger_mode 的真值：collectSettings 只扫 [data-k]，
    // 三个模式开关本身不带 data-k（否则会被当成普通开关项提交 true/false）。
    var html = "<input type='hidden' id='trg-mode' data-k='trigger_mode' value='" + esc(trgModeValue()) + "'>";
    // v3.14（用户 m40145）：相邻的 split 组合成一个左右两栏块——左半身份（昵称/外号），
    // 右半 4 个开关；说明文字跟着 inline 行走名称右侧。
    // v3.15：列数不再写死 1——基础页两列（split:2）、触发页三档三列（split:3），
    // 相邻且同页同列数的组并进同一个 .split2 块，否则每列都会各自成一个窄盒。
    // v3.17（用户 m40932）：splitCols 由字符串改成数组，元素是 {key, html}。
    // 同一个 col 键的相邻组并进同一个 .col（基础页左列 = 身份 + 重试），
    // 不同 col 键（或没写 col）才另起一列。
    // v3.70（用户 m21533 第 3 条）：曲线与动态调整不对私聊开放——私聊作用范围下整组不画。
    var dmScope = !!scopeSid && cacheSidIsDM(scopeSid);
    var splitCols = [], splitPage = "", splitN = 0;
    function flushSplit(){
      if(!splitCols.length) return;
      var inner = "";
      // 每列存的是列内 body（可能多个 .sub），包 .col 这一步留到最后，
      // 这样同一列的第二组能直接追加进 body，不会掉到列外面。
      splitCols.forEach(function(it){
        inner += "<div class='col'" +
                 (it.key ? " data-col='" + esc(it.key) + "'" : "") + ">" + it.body + "</div>";
      });
      html += "<div class='setgrp' data-split='" + splitN + "' data-page='" + esc(splitPage) + "'><div class='split2'>" +
              inner + "</div></div>";
      splitCols = [];
      splitN = 0;
    }
    SET_GROUPS.forEach(function(g){
      if(dmScope && (g.mode === "bands" || g.mode === "adaptive")) return;
      // v3.93（用户 m07570 图1）：小标题组——只画一行标题，把下面几行归到它名下。
      // 没有 rows、不参与灰化，只是给「高级设置」这种分组线用。
      if(g.sect){
        // v4.09（用户 m17145 第 1 条）：标题与并进来的 rowline 行同处一个 .sub——
        // 以前每行各占一个 .sub.rowsub，行间凭空多出两层虚线内边距。
        var sectRows = "";
        (g.merged || []).forEach(function(mg){
          (mg.keys || []).forEach(function(k){ used[k] = 1; });
          sectRows += rowLineHtml(mg);
        });
        // 用户 m18050：小节标题右侧也能挂一枚琥珀提醒（进阶模式那句「多一次调用」）。
        var sectWarn = warnPills(g.warn);
        // 用户 m06021：小节标题右侧也能挂一句说明（进阶模式那句「开启产生额外调用」）——
        // 以前 sect 分支只画 warn 胶囊，note 写了也不显示。
        // 用户 m06705：带 noteGo 的说明画成高亮气泡，点一下切到对应页签。
        var sectNote = g.note ? ("<span class='gnt" + (g.noteGo ? " tip" : "") + "'" +
          (g.noteGo ? (" data-goto='" + esc(g.noteGo) + "'") : "") + ">" + esc(g.note) +
          (g.noteGo ? "<i class='arw'>›</i>" : "") + "</span>") : "";
        var sectSub = "<div class='sub sect'><h3><span class='gt'>" + esc(g.t) + "</span>" + sectWarn + sectNote + "</h3>" +
                      (sectRows ? ("<div class='rows'>" + sectRows + "</div>") : "") + "</div>";
        if(g.split){
          var sn0 = g.split, sCol0 = g.col || "";
          if(splitCols.length && (splitPage !== (g.p || "basic") || splitN !== sn0)) flushSplit();
          splitPage = g.p || "basic";
          splitN = sn0;
          var lastS0 = splitCols[splitCols.length - 1];
          if(sCol0 && lastS0 && lastS0.key === sCol0) lastS0.body += sectSub;
          else splitCols.push({ key: sCol0, body: sectSub });
        } else {
          flushSplit();
          html += "<div class='setgrp' data-page='" + esc(g.p || "basic") + "'>" + sectSub + "</div>";
        }
        return;
      }
      var rows = "";
      (g.keys || []).forEach(function(k){
        // v3.92（用户 m06951）：rowline 组的键由 rowLineHtml 一次画完，这里只标已用。
        if(g.rowline){ used[k] = 1; return; }
        var f = schemaCache[k];
        if(!f) return;
        // v3.44：成对行的数值键（jitter_seconds / *_minutes …）在开关那一行就渲染完了，
        // 这里直接跳过——否则会再出一行重复的裸数字框。
        if(used[k]) return;
        used[k] = 1;
        // v3.54：配额（两行「开关+数字框」）与缓存记录（先表后设置）都由 custom 块整块画，
        // 这里只把键标成已用，免得它们再渲染一份重复行。
        if(g.custom === "quota" || g.custom === "cache" || g.custom === "show" || g.custom === "img" || g.custom === "gif") return;
        // v3.33：开关与清单合成一个两行块，别各占一行
        if(k === "phrase_filter_enabled") return;
        if(k === "phrase_filter_list"){ rows += phraseRowHtml(); return; }
        // v3.41（第 6 条）：普通模式组的这两项自带模式开关，开关挂在行尾
        if(g.sub && SUBROW_MODE[k]){ rows += paramRowHtml(k, f, SUBROW_MODE[k]); return; }
        // v3.70（用户 m21533 第 1 条）：休息时段是「起止两个下拉 + 开关」，单独画
        if(k === "rest_mode_enabled"){ used["rest_start"] = 1; used["rest_end"] = 1; rows += restPairHtml(); return; }
        // 用户 m03059：两个开关并成一排（静默兜底 ↔ 休息静默），值改成数组
        // [右边那项的键, 左边数字键, 右边数字键]，数字键也要标 used。
        if(SW_PAIR[k]){
          var sp = SW_PAIR[k];
          used[sp[0]] = 1;
          if(sp[1]) used[sp[1]] = 1;
          if(sp[2]) used[sp[2]] = 1;
          rows += swPairHtml(k, sp[0], sp[1], sp[2]);
          return;
        }
        // v3.44（用户 m04471 第 5 条）：开关 + 数值并成一行；数值键标 used 交给下一轮跳过
        // 用户 m06372：静默过滤三行两格（SILENT_ROWS / SILENT_NUMS 在 lib-2.js），
        // 每格自带数字框；这一行涉及的开关键与数字键全部标 used，免得再渲染出重复行。
        if(Object.prototype.hasOwnProperty.call(SILENT_ROWS, k)){
          var sr = SILENT_ROWS[k];
          for(var si = 0; si < sr.length; si++){
            used[sr[si]] = 1;
            var sn = SILENT_NUMS[sr[si]] || [];
            for(var sj = 0; sj < sn.length; sj++) used[sn[sj]] = 1;
          }
          rows += silentRowHtml(sr[0], sr[1]);
          return;
        }
        if(PAIR_NUM[k]){ used[PAIR_NUM[k]] = 1; rows += pairRowHtml(k, PAIR_NUM[k]); return; }
        rows += rowHtml(k, f);
      });
      // v3.92（用户 m06951 第 4 条）：rowline 组没有单独标题行，整组就是那一行。
      if(g.rowline){ rows += rowLineHtml(g); }
      // v3.15：进阶模式还没做，只占位——没有键也要画出来，否则那一列空着像漏了
      // v3.28：时段曲线是一整块自定义控件（迷你预览 + 编辑按钮），不是普通行
      if(g.custom === "bands"){ used["time_bands"] = 1; used["bands_enabled"] = 1; rows += bandsBlockHtml(); }
      // v3.40（用户本轮第 5 条）：唤醒组是纯自定义块（三行 × 两个开关）。六个键都在这里标成已用，
      // 否则它们没进 keys，会再渲染一遍重复行。
      if(g.custom === "wake"){
        used["mention_trigger"] = 1; used["mention_always_reply"] = 1;
        used["nickname_wake"] = 1; used["nickname_wake_always_reply"] = 1;
        used["name_wake"] = 1; used["name_wake_always_reply"] = 1;
        // 用户 m00363：组里多出的「休息时段」一行，两个键同样标成已用
        used["rest_wake_trigger"] = 1; used["rest_wake_always"] = 1;
        rows += wakeRowsHtml();
      }
      // v3.50（用户 m06761 第 7 条）：管理员是纯自定义块（胶囊列表 + 添加表单）。
      // admin_list 与老的两个键都标成已用，免得它们再渲染一份。
      if(g.custom === "admins"){
        used["admin_list"] = 1; used["admin_name"] = 1; used["admin_id"] = 1;
        rows += adminRowsHtml();
      }
      if(g.custom === "pricing"){ rows += pricingBlockHtml(); }
      // 用户本轮第 4 条：插件命名——先按插件自己报的名字自动命名，想改的在这里单独覆盖。
      // 后端接口没上线时整块保持 hidden，不挡别的设置项。
      if(g.custom === "probe"){ rows += probeNamesBlockHtml(); }
      // v3.54（用户本轮第 3 条）：配额两行——开关 + 数字框 + 一句说明，见 quotaRowsHtml。
      if(g.custom === "quota"){ rows += quotaRowsHtml(); }
      // 用户 m17752：显示条数三行（回复记录 / 决策记录 / 窗口快照），每行右侧两个数字框。
      if(g.custom === "show"){ rows += showRowsHtml(); }
      // 用户 m09851：窗口页「图片」组——两个看图开关 + 框架那边的图片压缩四项。
      // 用户 m27095：动图三档是分段按钮（一次只能选一个），整块由 gifBlockHtml 画。
      if(g.custom === "gif"){ rows += gifBlockHtml(); }
      if(g.custom === "img"){ rows += imgBlockHtml(); }
      // v3.54（用户本轮第 4 条）：缓存整块——表格 + 合计 + 没进表的兜底行 + 按类型清空（v3.56 起这一页叫「缓存记录」）。
      // 三个上限行也在 cacheBlockHtml 里画（表格要排在最前面），所以 keys 那轮只标已用。
      if(g.custom === "cache"){ rows += cacheBlockHtml(); }
      // 用户 m06193：用户黑名单整块（总表 + 解黑）
      if(g.custom === "ublack"){ rows += ublackBlockHtml(); }
      // m01209 图3：决策侧不再引用人格（人格改为提示词页里的「角色扮演（Persona）」模块挑文件），
      // 这块卡片整段撤掉，只留模型与重试、工具勾选。
      if(g.custom === "dmodel"){ rows += dmodelBlockHtml(); }
      // 用户 m02993 图7：决策这边不再单列「决策可用的工具」——工具改成提示词页的
      // 「工具说明」模块，勾一次两边都用同一份。
      // v3.92（用户 m06951 第 5~7 条）：提示词页整页一块——子页签 + 左设置右预览。
      if(g.custom === "promptpage"){ rows += promptPageBlockHtml(); }
      if(!rows && !g.dev) return;
      // 分组说明（v3.09）：私聊页只有三项，光看键名分不清三种策略怎么互斥
      // v3.40（用户本轮第 6 条）：说明从标题下面一整行灰字改成标题右侧一行内联小字（.gnt），
      // 分栏里超长就省略号截断，全文留在 title 里；标题行不被撑高。
      // v3.41（用户本轮第 4 条）：最后一处整行说明（唤醒组的「必回」）也挪进标题右侧，
      // gnote 这套渲染分支随之删掉，不再有标题下面的独立说明行。
      var note = g.note ? ("<span class='gnt'>" + esc(g.note) + "</span>") : "";
      // 2026-11-08：缓存记录那句作用范围说明从表格上面挪到标题右边（文字由 renderCacheScopeOptions 写）。
      if(g.custom === "cache") note = "<span class='gnt' id='cache-scope-hint'>" + esc(cacheScopeHintText()) + "</span>";
      // 三列里哪一档正在生效（mode）由 refreshModeBadges 按 trigger_mode 的值点亮
      // v3.86（用户 m04620）：g.todo 给「功能还没做完、先别当真」的组挂个小标（摘要组）。
      var badge = g.mode ? "<span class='h3use' data-mode='" + esc(g.mode) + "' hidden>使用中</span>"
                : (g.dev ? "<span class='h3use dev'>开发中</span>" : "")
                  || (g.todo ? "<span class='h3use todo'>" + esc(g.todo) + "</span>" : "");
      var body = rows ? ("<div class='rows'>" + rows + "</div>")
                 : ("<div class='phbox'><b>还在做</b>这一档要模型读完整轮消息再判，等模型接入后开放；" +
                    "先用上面两档。</div>");
      // v3.16（用户 m40583）：每列标题右侧一个开关，点它就切到这一档（互斥，见 syncModeUI）。
      // 开关不带 data-k——它的值写进 #trg-mode，不走普通表单路径。
      // v3.17：colKey 只用来决定「哪些组并进同一列」（基础页 left/right）；
      // 模式开关只有触发页三档才有，不能跟着 col 一起出现，所以单独用 modeKey 判断。
      var colKey = g.col || g.mode || (g.dev ? "dev" : "");
      // v3.53（用户本轮第 4 条）：dev 列不再生成标题上的模式开关——那一列现在装的是真开关
      // （advanced_enabled）和退避，整列 .off 会把 pointer-events 一起关掉，点了没反应。
      var modeKey = g.mode || "";
      // v3.33（第 9 条）：曲线那一列的开关就是 bands_enabled（值直接从表单提交），
      // 另外两档仍是「切到这一档」的入口，不带 data-k（它们的值写进 #trg-mode）。
      // v3.71（用户 m23596 第 4 条）：曲线开关的初始勾选状态必须从配置来。
      // 原来这个 input 从不带 checked，全靠 syncModeUI() 事后写——而 syncModeUI 又是
      // 从「DOM 里这个 input」读 bandsOn()，于是永远读到 false：配置里开着曲线，
      // 一进设置页就被刷成关闭（用户「回首页再回设置就变回去了」就是这一条）。
      var bCfgOn = !!(configCache && configCache.bands_enabled);
      var h3sw = modeKey ? ("<label class='msw' data-msw='" + esc(modeKey) + "' title='切到这一档'>" +
                           "<input type='checkbox'" + (modeKey === "bands" ? " data-k='bands_enabled'" + (bCfgOn ? " checked" : "") : "") +
                           "><span class='tr' data-on='使用中' data-off='未使用'></span></label>") : "";
      // v3.41（第 6 条）：原来那条 .subsw（计数 / 概率两个子开关）取消——两个开关已经并进
      // 各自的参数行（见 paramRowHtml），g.sub 现在是「这一组用行内开关切档」的标记。
      // v3.92：rowline 组不画标题行——标题已经在那一行里了。
      var inner = (g.rowline || g.bare) ? ("<div class='rows'>" + rows + "</div>")
        // 用户 m05550：普通组也能在组名右边挂警告色胶囊（跟 sect 分支的 sectWarn 一个样式）。
        : ("<h3><span class='gt'>" + esc(g.t) + "</span>" +
           warnPills(g.warn) +
           badge + note + h3sw + "</h3>" + body);
      if(g.split){
        var n = g.split;
        if(splitCols.length && (splitPage !== (g.p || "basic") || splitN !== n)) flushSplit();
        splitPage = g.p || "basic";
        splitN = n;
        // 每组的标题 + 说明 + 行包一层 .sub：一列里有两组时靠它拉开间距、加分隔线
        var sub = "<div class='sub" + (g.custom === "bands" ? " sub-bands" : "") +
                  (g.rowline ? " rowsub" : "") + "'" +
                  (g.compact ? " data-compact='1'" : "") +
                  (g.grp ? " data-grp='" + esc(g.grp) + "'" : "") + ">" + inner + "</div>";
        var last = splitCols[splitCols.length - 1];
        if(colKey && last && last.key === colKey){
          last.body += sub;
        } else {
          splitCols.push({ key: colKey, body: sub });
        }
        return;
      }
      flushSplit();
      var cpt = (g.compact ? " data-compact='1'" : "") + (g.solo ? " data-solo='1'" : "");
      // v3.53（用户本轮第 2/4 条）：非分栏的整幅块也要能定位（私聊策略就在这一支里），
      // 灰化逻辑只认 data-grp，不再按 data-page 猜页签。
      var gpt = g.grp ? (" data-grp='" + esc(g.grp) + "'") : "";
      html += "<div class='setgrp'" + cpt + gpt + " data-page='" + esc(g.p || "basic") + "'>" + inner + "</div>";
    });
    flushSplit();
    // v3.70（用户 m21533 第 3 条）：私聊作用范围下补一句说明——上面少了两组（曲线 / 动态调整）。
    if(dmScope){
      html += "<div class='setgrp' data-page='trigger'><div class='rows'>" +
        "<div class='setrow inline' data-row='dm-note'><div class='nm'>私聊</div>" +
        "<div class='ds'>私聊不走时段曲线、也不走动态调整；触发概率默认 100%，就是上面那一栏的值。</div>" +
        "<div class='ctl'></div></div></div></div>";
    }
    // 观察窗那套设置早就下线了（v2.19），留在表单里只会让人以为是新东西
    // v3.09：enabled 由首页开关负责、takeover_default_chat 已并入 decision_enabled，
    // 两个都不再单独露脸，否则会落进表单里让人以为还能单独关。
    // v3.10：wake_words 也进这里——「外号」已经能当唤醒词用，再留一个手填短语框是重复入口。
    // 只是不显示，值照旧保留（collectSettings 会把它原样提交回去）。
    // v3.11：bot_qq 也从表单里撤掉——插件会自己从框架适配器配置里读 bot_pid。
    // v3.13：window_enabled 也撤出表单（核心功能，后端强制开启）
    // v3.32：silent_trigger_minutes 也撤出表单——静默超时只在「群安静下来且还压着没消化的消息」时
    // 触发一次（新消息来了会重置），等待时长没必有暴露给用户，固定按后端默认 30 分钟走。
    // v3.33：silent_trigger_minutes 重新放回表单（静默与休息组），short_msg_chars 下线
    // （被「不触发短语」取代，后端也不再读它）。
    // v3.53（用户本轮第 3 条）：配额改成「每小时 / 每天」两个开关后，旧的 quota_enabled /
    // quota_mode / quota_ratio_percent 全部退役——schema 里已经删了，这里再兜一层，
    // v3.56（用户本轮第 7 条）：「其他」页签撤掉，schema 里没分组的键不再兜底渲染——
    // 老键（observe_* / quota_* / trace_enabled 等）早就从 schema 删了，这一手只会凭空长出第五个页签。
    // 上面那一串 used 标记照旧留着：同一个键被两个分组画两遍才是真问题。
    if(force || html !== lastSettingsSig){
      box.innerHTML = html;
      lastSettingsSig = html;
    }
    // v3.92（用户 m06951 第 5 条）：提示词页的预览自动跑，不用点按钮。
    bindDecisionPage();
    setTimeout(ppAutoPreview, 0);
    // v3.13（用户 m39983）：锁定项——控件禁用、名称后标「已锁定」，值照旧随表单提交
    Array.prototype.forEach.call(box.querySelectorAll("[data-row]"), function(row){
      var k = row.getAttribute("data-row");
      if(!LOCKED[k]) return;
      row.classList.add("locked");
      Array.prototype.forEach.call(row.querySelectorAll("input,select"), function(n){ n.disabled = true; });
      var nm = row.querySelector(".nm");
      if(nm && !nm.querySelector(".lk")) nm.insertAdjacentHTML("beforeend", "<span class='lk'>已锁定</span>");
    });
    if(scopeSid){
      // 单群模式：不在白名单里的键是全局项，置灰并禁止编辑
      Array.prototype.forEach.call(box.querySelectorAll("[data-row]"), function(row){
        var k = row.getAttribute("data-row");
        if(scopeKeys.indexOf(k) >= 0) return;
        row.classList.add("ro");
        Array.prototype.forEach.call(row.querySelectorAll("input,select"), function(n){ n.disabled = true; });
      });
    }
    paintOwnRows();
    // 用户 m10749 第 1 条：首帧 / 重绘后配额两档的灰化状态要跟上（开关可能被另一档顶掉）
    paintQuotaMutex();
    refreshModeBadges();
    bindBandBox();
    bindCacheClear();
    mountCacheBlock();   // v3.62：装「作用范围」选择器 + 按当前作用域重画缓存表（真值等 loadCacheStats 回来再刷）
    mountProbeNames();   // 用户本轮第 4 条：装「插件命名」块（接口没上线就整块不显示）
    renderTabs();
  }

  // 用户本轮第 4 条：插件命名——先按插件自己报的名字自动命名（probe-plugins 的 auto_name），
  // 想改的在「统计」组里单独覆盖（probe-names 的 override），留空就是恢复自动。
  // 后端接口没上线时整块不显示：请求失败就保持 hidden，不报错、不挡别的设置项。
  var probeNamesData = null, probeNamesLoading = false, probeNamesAt = 0;
  var probeNamesMsg = "", probeNamesMsgTone = "";

  function probeNamesBlockHtml(){
    return "<div class='pm-probe' id='pm-probe-names' hidden></div>";
  }

  function probeNamesRowsHtml(){
    var list = (probeNamesData && probeNamesData.plugins) || [];
    var out = "<div class='pm-probe-hd'><span class='pm-probe-t'>插件命名</span>" +
      "<span class='pm-probe-hint'>默认按插件自己报的名字自动命名；想改就在右边填，留空恢复自动。</span></div>";
    out += "<div class='pm-probe-list'>";
    list.forEach(function(p){
      var pid = String((p && p.plugin_id) || "");
      var auto = String((p && p.auto_name) || "");
      var ov = String((p && p.override) || "");
      var pname = String((p && p.plugin_name) || "");
      var kinds = (p && p.kinds && p.kinds.length) ? (" · " + p.kinds.join(" / ")) : "";
      out += "<div class='pm-probe-row'>" +
        "<span class='pm-probe-id' title='" + esc((pname || pid) + kinds) + "'>" + esc(pid) + "</span>" +
        "<input class='pm-probe-in' type='text' data-pid='" + esc(pid) + "' placeholder='" + esc(auto) + "' value='" + esc(ov) + "'>" +
        "<button class='pm-probe-reset' type='button' data-pid='" + esc(pid) + "'" + (ov ? "" : " disabled") + ">恢复自动</button>" +
        "</div>";
    });
    out += "</div>";
    out += "<div class='pm-probe-msg" + (probeNamesMsgTone ? (" " + probeNamesMsgTone) : "") + "'>" + esc(probeNamesMsg) + "</div>";
    return out;
  }

  function paintProbeNames(){
    var box = document.getElementById("pm-probe-names");
    if(!box) return;
    var list = (probeNamesData && probeNamesData.plugins) || [];
    if(!list.length){ box.hidden = true; box.innerHTML = ""; return; }
    box.innerHTML = probeNamesRowsHtml();
    box.hidden = false;
    bindProbeNames();
  }

  function bindProbeNames(){
    var box = document.getElementById("pm-probe-names");
    if(!box || !box.querySelectorAll) return;
    Array.prototype.forEach.call(box.querySelectorAll(".pm-probe-in"), function(inp){
      inp.addEventListener("change", function(){
        var pid = inp.getAttribute("data-pid") || "";
        if(pid) probeSaveName(pid, String(inp.value || ""));
      });
    });
    Array.prototype.forEach.call(box.querySelectorAll(".pm-probe-reset"), function(btn){
      btn.addEventListener("click", function(){
        var pid = btn.getAttribute("data-pid") || "";
        if(!pid) return;
        var inp = box.querySelector ? box.querySelector(".pm-probe-in[data-pid='" + pid + "']") : null;
        if(inp) inp.value = "";
        probeSaveName(pid, "");
      });
    });
  }

  function setProbeMsg(text, tone){
    probeNamesMsg = String(text || "");
    probeNamesMsgTone = String(tone || "");
    var box = document.getElementById("pm-probe-names");
    var el = (box && box.querySelector) ? box.querySelector(".pm-probe-msg") : null;
    if(!el) return;
    el.textContent = probeNamesMsg;
    el.className = "pm-probe-msg" + (probeNamesMsgTone ? (" " + probeNamesMsgTone) : "");
  }

  function loadProbeNames(){
    if(probeNamesLoading) return;
    probeNamesLoading = true;
    probeNamesAt = Date.now();
    fetch(API + "/probe-plugins", { credentials: "same-origin" })
      .then(function(r){ if(!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function(d){
        probeNamesLoading = false;
        var list = (d && d.plugins) || [];
        probeNamesData = (d && d.ok !== false && list.length) ? { plugins: list } : null;
        paintProbeNames();
      })
      .catch(function(){
        probeNamesLoading = false;
        probeNamesData = null;
        paintProbeNames();
      });
  }

  function probeSaveName(pid, name){
    var names = {};
    names[pid] = String(name || "").trim();
    setProbeMsg("保存中…", "");
    fetch(API + "/probe-names", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ names: names })
    })
      .then(function(r){ if(!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function(d){
        if(d && d.ok === false) throw new Error("save rejected");
        setProbeMsg("已保存，价目页下次刷新就用新名字。", "ok");
        if(typeof pricingForgetNames === "function") pricingForgetNames();
        probeNamesData = null;
        probeNamesLoading = false;
        probeNamesAt = 0;
        loadProbeNames();
      })
      .catch(function(){
        setProbeMsg("保存失败，稍后再试。", "bad");
      });
  }

  function mountProbeNames(){
    if(probeNamesData){ paintProbeNames(); return; }
    if(probeNamesLoading) return;
    if(probeNamesAt && Date.now() - probeNamesAt < 30000) return;
    loadProbeNames();
  }

  // 单群（独立配置）页：把本群真正写过的行标出来（用户 m02580：与全局不同的选项要淡橙底）。
  // 判断依据是 overrideCache（本群写过的键）。一行里可能有两个键（休息时段起止）或一列表，
  // 所以除 data-row 还要扫行内 [data-k]，任一命中就算这行是独立配置。
  // 一行算不算「本群独立配置」：只要这行涉及的任一键写在 overrideCache 里就算。
  // 抽成纯函数，离线测试能直接判，不用造一整套 DOM。
  function trgSet(v){
    var el = document.getElementById("trg-mode");
    var nv = (v === "probability") ? "probability" : (v === "adaptive" ? "adaptive" : "count");
    if(el) el.value = nv;
    // 同步写回缓存：切页签会整块重建表单，重建时隐藏 input 的值取自 configCache，
    // 不写回的话点了开关再切页签会看到旧模式（保存的也会是旧值）。
    if(configCache) configCache.trigger_mode = nv;
    syncModeUI();
    dirty = true;
    saveNow();   // v3.71：切模式是离散动作，立刻存（防抖会被「马上点回首页」吃掉）
  }
  // 曲线总闸（v3.33 第 9 条）：标题上的开关就是 bands_enabled，值直接从表单提交
  function paintFromCache(){
    // 作用域下拉先直出（v3.09）：它跟表单无关，每次进页面都要填。不直出的话
    // 首帧只有一个空框，等 /context-sessions 回来才出现「全局设置」——用户说的
    // 「切到设置页下拉框闪一下」就是这个。宽度已在 CSS 固定，选项多了也不抖。
    var sc = cacheGet("scopes");
    if(sc && sc.list && sc.list.length){
      aliasCache = sc.aliases || aliasCache;
      sessCache = sc.list;
      renderScopeOptions(sc.list);
    }
    if(scopeSid) return;
    var c = cacheGet("set");
    if(!c || !c.schema || !Object.keys(c.schema).length) return;
    schemaCache = c.schema;
    configCache = c.config || {};
    boundsCache = c.bounds || {};
    // 用户 m14921：框架那边的图片压缩四项也一起缓存，热刷新首帧就有真实行，
    // 不会先画「读取框架配置失败」再长出来。
    coreImgCache = c.coreImg || null;
    renderSettings();
    dcRefresh();
    // 这里不再把说明改成「刷新中…」：宽度一变，右边的按钮就像被推了一下
    //（用户 m07864 第 1 条）。首帧用的就是上次记下的那句，接口回来再校正数字。
  }

  // 只显示当前页的组；输入框全都在 DOM 里，切页不影响自动保存（值一直在表单里）
  // v3.51（用户 m07385 第 2 条）：淡入动画只在「人点页签」时播。以前每次 renderTabs 都播，
  // 而进页面会连渲染两次（缓存一次 + 拉回配置一次），于是看到内容闪两下。
  // 用户 m10962：提示词 / 缓存记录 / 黑名单这三页不要悬浮提示——能不用悬浮就不用悬浮。
  // 这三页的内容会反复重画（改提示词、拉缓存数据、加删黑名单），所以在 #settings 上挂一个
  // MutationObserver，每次重画后把这三页里的 title 摘掉。导航条和左边那排会话卡片不在
  // #settings 里，别的页也不在名单里，都不受影响。
  var NOTIP_PAGES = { prompt: 1, log: 1, blacklist: 1 };
  function stripTips(root){
    if(!root || !root.querySelectorAll) return;
    var list = root.querySelectorAll("[title]");
    for(var i = 0; i < list.length; i++) list[i].removeAttribute("title");
  }
  function stripNoTipPages(){
    Array.prototype.forEach.call(document.querySelectorAll("#settings .setgrp[data-page]"), function(el){
      if(NOTIP_PAGES[el.getAttribute("data-page")]) stripTips(el);
    });
  }
  var notipObs = null;
  function watchNoTip(){
    if(notipObs || typeof MutationObserver !== "function") return;
    var host = document.getElementById("settings");
    if(!host) return;
    notipObs = new MutationObserver(function(recs){
      var all = false;
      for(var i = 0; i < recs.length; i++){
        var rec = recs[i], t = rec.target;
        if(!t || t.nodeType !== 1) continue;
        // 就地改 title（字数、自定义小标这些）走属性记录，直接摘掉；摘掉本身也会再来一条
        // 记录，那时 hasAttribute 已经是 false，跳过，不会来回打转。
        if(rec.type === "attributes"){
          if(!t.hasAttribute("title") || !t.closest) continue;
          var ab = t.closest(".setgrp[data-page]");
          if(ab && NOTIP_PAGES[ab.getAttribute("data-page")]) t.removeAttribute("title");
          continue;
        }
        if(t === host){ all = true; continue; }
        var box = t.closest ? t.closest(".setgrp[data-page]") : null;
        if(box && NOTIP_PAGES[box.getAttribute("data-page")]) stripTips(box);
      }
      if(all) stripNoTipPages();
    });
    notipObs.observe(host, { childList: true, subtree: true, attributes: true, attributeFilter: ["title"] });
  }
  function applyPage(animate){
    var shown = [];
    Array.prototype.forEach.call(document.querySelectorAll("#settings .setgrp[data-page]"), function(el){
      var on = el.getAttribute("data-page") === curPage;
      var display = on ? "" : "none";
      if(el.style.display !== display) el.style.display = display;
      if(on) shown.push(el);
    });
    // v3.67（用户 m20117 第 7 条）：价目页跟「对哪个群生效」无关，卡片条在那页收起来。
    var cd = document.getElementById("set-cards");
    if(cd) cd.style.display = (curPage === "pricing") ? "none" : "";
    // v3.26（用户 m46693 ①）：换页内容淡入。
    // v3.52（用户 m07864 第 5 条）：删掉 ``void shown[0].offsetWidth``。这行是「强制同步
    // 重排整页」——切页时浏览器必须把刚显示出来的几百行全部量一遍，正是点页签会顿一下
    // 的来源；而元素从 display:none 变成可见时 CSS 动画本来就会重播，这行纯属多余。
    if(animate && shown.length){
      shown.forEach(function(el){ el.classList.remove("pgin"); });
      shown.forEach(function(el){ el.classList.add("pgin"); });
    }else{
      shown.forEach(function(el){ el.classList.remove("pgin"); });
    }
    if(curPage === "prompt") ppAutoPreview();
    // 用户 m10962：这三页的悬浮提示在渲染后统一摘掉（见上面的 NOTIP_PAGES）。
    watchNoTip();
    // 隐藏页量出来的宽度无效；等真正打开该页才量。
    shown.forEach(silentFitNotes);
  }

  // 用户 m01874：把「模型没配」的红点画到三处——页签、提示词页的子页签、具体那一行。
  // 每次重画都先清干净，配置补上后红点要能自己消失。
  function paintModelDots(){
    Array.prototype.forEach.call(document.querySelectorAll(".mdot"), function(n){
      if(n.parentNode) n.parentNode.removeChild(n);
    });
    Array.prototype.forEach.call(document.querySelectorAll(".mdneed"), function(n){ n.classList.remove("mdneed"); });
    var list = modelMissing();
    if(!list || !list.length) return;
    var box = document.getElementById("settings");
    var pages = {};
    list.forEach(function(it){
      var k = String(it.key || "");
      if(!k) return;
      var found = false;
      if(box){
        var ctl = box.querySelector("[data-k='" + k + "']");
        if(ctl && ctl.closest){
          var row = ctl.closest(".drow");
          if(row){
            found = true;
            row.classList.add("mdneed");
            var lb = row.querySelector(".dlb");
            if(lb && it.hint) lb.setAttribute("title", it.hint);
          }
        }
      }
      // 找不到那一行就不亮页签：点进去是空的，比不提示更糟
      if(!found) return;
      pages[String(it.page || "")] = 1;
      // 提示词页是子页签结构，缺的那个模型在别的子页里，子页签也得亮
      if(it.pane){
        var tab = box.querySelector(".pp-tab[data-pp='" + it.pane + "']");
        if(tab) tab.insertAdjacentHTML("beforeend", "<span class='mdot'></span>");
      }
    });
    var tabs = document.getElementById("set-tabs");
    if(!tabs) return;
    Array.prototype.forEach.call(tabs.querySelectorAll("button[data-page]"), function(b){
      if(pages[b.getAttribute("data-page")]) b.insertAdjacentHTML("beforeend", "<span class='mdot'></span>");
    });
  }

  function renderTabs(){
    var box = document.getElementById("set-tabs");
    if(!box) return;
    var has = {};
    Array.prototype.forEach.call(document.querySelectorAll("#settings .setgrp[data-page]"), function(el){
      has[el.getAttribute("data-page")] = 1;
    });
    var list = PAGES.filter(function(p){ return has[p.id]; });
    if(!list.length){ box.innerHTML = ""; return; }
    if(!list.some(function(p){ return p.id === curPage; })) curPage = list[0].id;
    rememberPage();
    box.innerHTML = list.map(function(p){
      return "<button data-page='" + p.id + "'" + (p.id === curPage ? " class='on'" : "") + ">" + esc(p.t) + "</button>";
    }).join("");
    applyPage(false);
    stripNoTipPages();
    paintModelDots();
  }

  // 用户 m04756：刷新时只拉当前页要的数据。以前一进页面就把价目、分布、缓存账本、短语清单、
  // 黑名单全拉一遍，服务端串行处理，提示词页自己的 /prompt-layout 排在队尾要等 600ms 才回来。
  // 现在按页懒加载：切到哪页再补哪页；作用范围变了由 loadSettings 带 force 重取。
  var pageDataDone = {};
  function loadPageData(page, force){
    page = page || curPage;
    if(!force && pageDataDone[page]) return;
    pageDataDone[page] = 1;
    if(page === "trigger"){ loadHours(); loadBandStats(); return; }
    if(page === "window"){ loadPhraseBlock(); return; }
    if(page === "log"){ loadCacheStats(); return; }
    if(page === "blacklist"){ ublkLoad(); return; }
    if(page === "pricing"){ loadPricing(); return; }
    if(page === "prompt"){ loadDecisionOptions(); loadJevOptions(); return; }
  }

  function loadSettings(){
    if(dirty) return;
    var requestedSid = String(scopeSid || ""), requestedScopeVersion = saveScopeVersion, requestedEditVersion = saveEditVersion;
    // 缓存记录页签的作用范围跟页面顶部那个选择器是同一个东西：换会话时把覆盖缓存一起清掉，
    // 等 loadCacheStats -> mountCacheBlock 拿着新 sid 重新拉。
    cacheScopeSid = scopeSid || "";
    cacheScopeOv = null;
    cacheScopeErr = "";
    cacheScopeLoading = false;
    // v3.68（用户 m20632 第 4 条后半）：不再清 cacheStatsData——切作用范围时旧表格原地留着，
    // 等新数据回来替换，避免先闪一下「读取中…」。
    // 用户 m10096：字段定义改由插件自己的 /ui-schema 给——框架抽屉已经置空，
    // /config 返回的 schema 恒为空，再按它画页面会整页空白。
    var tasks = [get(CORE + "/config"), get(API + "/ui-schema")];
    // 全局模式也要拿一次：上下限（bounds）只在 /overrides 里给
    tasks.push(get(API + "/overrides" + (scopeSid ? "?sid=" + encodeURIComponent(scopeSid) : "")));
    // 用户 m09851：窗口页「图片」组要显示框架那边的图片压缩四项（存在 system_config.json），
    // 顺手跟配置一起拉；拉不到就留 null，那一组会显示「读取框架配置失败」。
    tasks.push(get("/api/configuration").catch(function(){ return null; }));
    // 用户 m01874：模型没配的红点跟配置一起拉，表单画完就能标上。
    tasks.push(get(API + "/model-check" + (scopeSid ? "?sid=" + encodeURIComponent(scopeSid) : "")).catch(function(){ return null; }));
    Promise.all(tasks).then(function(res){
      if(requestedScopeVersion !== saveScopeVersion || requestedEditVersion !== saveEditVersion || requestedSid !== String(scopeSid || "") || dirty) return;
      var d = res[0] || {};
      var ov = res[2];
      var coreCfg = (res[3] && res[3].configuration) || null;
      MODEL_MISSING = (res[4] && res[4].missing) || [];
      if(coreCfg){
        coreImgCache = (coreCfg.bot_config && coreCfg.bot_config.image_compression) || {};
      }
      coreImgTried = true;
      schemaCache = (res[1] && res[1].schema) || d.schema || {};
      cacheDefCfg = fillSchemaDefaults(d.config || {});
      var sub = document.getElementById("set-sub");
      if(scopeSid && ov && ov.ok){
        scopeKeys = ov.keys || [];
        configCache = fillSchemaDefaults(ov.effective || {});
        overrideCache = ov.override || {};
        globalCache = ov.global || {};
        boundsCache = ov.bounds || {};
        dmDefaultsCache = ov.dm_defaults || {};
        var n = Object.keys(ov.override || {}).length;
        setSub(sidLabel(scopeSid) + " · 已覆盖 " + n + " 项 · 其余跟随全局（保存立即生效，不用重载）");
        document.getElementById("btn-reset").style.display = "";
      } else {
        scopeKeys = [];
        overrideCache = {};
        globalCache = {};
        dmDefaultsCache = (ov && ov.dm_defaults) || {};
        boundsCache = (ov && ov.bounds) || {};
        configCache = cacheDefCfg;
        setSub(Object.keys(schemaCache).length + " 项 · 改完自动保存（会热重载插件）");
        document.getElementById("btn-reset").style.display = "none";
      }
      renderSettings();
    dcRefresh();
      if(!scopeSid) cachePut("set", {schema: schemaCache, config: configCache, bounds: boundsCache, coreImg: coreImgCache});
      releaseBoot();
      // 用户 m04756：只拉当前页要的数据，别的页切过去再补；作用范围变了要按新 sid 重取。
      loadPageData(curPage, true);
      // 体检条不挡首屏：等当前页的请求先发出去再补。
      setTimeout(checkConfig, 0);
    }).catch(function(e){
      if(requestedScopeVersion !== saveScopeVersion || requestedEditVersion !== saveEditVersion || requestedSid !== String(scopeSid || "") || dirty) return;
      lastSettingsSig = "";   // 这行绕过了 renderSettings，签名要作废，否则下次同 HTML 会被跳过
      document.getElementById("settings").innerHTML = '<div class="empty">读取配置失败：' + esc(e.message) + "</div>";
    });
  }

  function scheduleSave(){
    saveEditVersion += 1;
    lastEditAt = Date.now();
    if(saving) saveAgain = true;
    var msg = document.getElementById("set-msg");
    if(msg) msg.textContent = "修改中…";
    if(saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(function(){ saveTimer = null; saveSettings(); }, 700);
  }

  // v3.71（用户 m23596 第 4 条）：模式开关这种「改一下就离开」的操作不能走 700ms 防抖——
  // 用户拨完曲线开关马上点回首页，防抖还没到点页面就走了，PUT 根本没发出去，
  // 回来一看开关又弹回原样（就是这个 bug）。所以这类离散动作立刻保存。
  function saveNow(){
    saveEditVersion += 1;
    if(saveTimer){ clearTimeout(saveTimer); saveTimer = null; }
    return saveSettings();
  }

  // v3.71（用户 m23596 第 4 条）：离开页面前把还没提交的那次保存补发出去。
  // keepalive 让请求在页面跳转/关闭之后继续跑完；补发成功就不拦用户离开。
  // 补的是 PUT /config（全局）或 POST /overrides（单群覆盖），reload 交给首页那边的待办标记。
  function resetPageDefaults(){
    if(!schemaCache || !configCache) return { done: 0, changed: [], saved: null };
    var list = pageKeyList(), done = 0, changed = [];
    list.forEach(function(k){
      var f = schemaCache[k];
      if(!f || f.default === undefined || f.default === null) return;
      // LOCKED（window_enabled）这类恒开项不跟着默认值走，免得点一下把它关掉
      if(LOCKED[k]) return;
      // 会话模式下不在白名单里的键是全局项（页面上也置灰），这一页恢复默认不该动它
      if(scopeSid && scopeKeys && scopeKeys.indexOf(k) < 0) return;
      var def;
      try{ def = JSON.parse(JSON.stringify(f.default)); }
      catch(e){ def = f.default; }
      var before = pageValOf(k);
      configCache[k] = def;
      if(scopeSid){
        // 会话覆盖也要跟着改：缓存表读的是 /overrides 快照，只改 configCache 的话
        // 重画出来还是旧值，提交回去后端又把「等于全局值」的项丢掉，点了等于没点。
        if(overrideCache) overrideCache[k] = def;
        if(cacheScopeOv){
          if(!cacheScopeOv.override) cacheScopeOv.override = {};
          cacheScopeOv.override[k] = def;
          if(cacheScopeOv.effective) cacheScopeOv.effective[k] = def;
        }
      }
      done += 1;
      if(!pageValSame(before, def)) changed.push({ k: k, from: before, to: def });
    });
    if(!done) return { done: 0, changed: [], saved: null };
    renderSettings(true);   // 表单按新值重画，免得控件还显示旧值（用户可能刚在输入框里改过，必须强制写）
    dirty = true;
    var saved = saveNow();   // 同其它离散编辑一起登记版本，再走原自动保存
    // 保存完按后端真值再画一次：等于全局值的项后端不会存，别在表格上留一个假的「单独」胶囊
    if(scopeSid && saved && saved.then){
      saved.then(function(){ if(cacheScopeSid) loadCacheScope(); });
    }
    return { done: done, changed: changed, saved: saved };
  }

  // 最近一次「用户改了一项」的时刻。热重载会清空内存里的窗口，所以不能每改一项
  // 就重载一次（审查报告）：先等手停下来，再一次性重载。
  function saveSettings(){
    lastEditAt = Date.now();
    if(saving){ saveAgain = true; return; }
    if(saveTimer){ clearTimeout(saveTimer); saveTimer = null; }
    // 一次请求只确认它实际提交的快照；不能在回包时重新收集表单冒充已保存。
    var sid = String(scopeSid || ""), scopeVersion = saveScopeVersion, editVersion = saveEditVersion;
    var values = JSON.parse(JSON.stringify(collectSettings()));
    var promptRefEpoch = PP_REF_DIRTY[sid] || 0;
    function currentScope(){ return scopeVersion === saveScopeVersion && sid === String(scopeSid || ""); }
    saving = true;
    saveAgain = false;
    dirty = true;
    var msg = document.getElementById("set-msg");
    if(msg) msg.textContent = "保存中…";
    var request;
    if(sid){
      request = saveOverrides(sid, values);
    }else{
      request = fetch(CORE + "/config", {
        method: "PUT", credentials: "same-origin",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({config: values})
      }).then(function(r){
        if(!r.ok) throw new Error("HTTP " + r.status);
        // 连续全局编辑只在最后一次配置稳定后重载；切走的旧请求不操作新表单。
        return waitQuiet(1200).then(function(){
          if(currentScope() && saveAgain) return null;
          return fetch(CORE + "/reload", {method: "POST", credentials: "same-origin"});
        });
      }).then(function(r){
        if(r && !r.ok) throw new Error("重载失败 HTTP " + r.status);
        return null;
      });
    }
    return request.then(function(d){
      if(!currentScope() || editVersion !== saveEditVersion || saveAgain) return;
      saveAckVersion = editVersion;
      dirty = false;
      if(sid){
        overrideCache = d.override || {};
        var n = Object.keys(overrideCache).length;
        var base = n ? ("已自动保存 " + n + " 项覆盖（立即生效）") : "已恢复跟随全局";
        if(msg) msg.textContent = resetNote ? (resetNote + " · " + base) : base;
        if(resetNote) clearResetNoteSoon();
        setSub(sidLabel(sid) + " · 已覆盖 " + n + " 项 · 其余跟随全局（改完自动保存，不用重载）");
      }else{
        configCache = values;
        if(msg) msg.textContent = resetNote ? (resetNote + " · 已自动保存") : "已自动保存";
        if(resetNote) clearResetNoteSoon();
      }
      checkConfig();
      // 用户 m01874：刚配好的模型要能把红点消掉，保存完重新问一次后端。
      loadModelCheck().then(paintModelDots);
      ppReferencesSaved(sid, promptRefEpoch);
    }).catch(function(e){
      if(!currentScope()) return;
      dirty = true;
      if(msg) msg.textContent = "自动保存失败：" + ((e && e.message) || String(e));
    }).then(function(){
      // 成功/HTTP失败/业务失败/网络失败共用收尾；仅补发真正存在的新编辑。
      saving = false;
      if(saveAgain){ saveAgain = false; if(dirty) saveSettings(); }
    });
  }

  async function resetOverrides(){
    if(!scopeSid) return;
    if(!(await mdConfirm("让 " + sidLabel(scopeSid) + " 恢复跟随全局设置？", {danger:true, ok:"恢复"}))) return;
    fetch(API + "/overrides?sid=" + encodeURIComponent(scopeSid) + "&values=", {
      method: "POST", credentials: "same-origin"
    }).then(function(r){ return r.json(); }).then(function(d){
      dirty = false;
      document.getElementById("set-msg").textContent = "已恢复跟随全局";
      loadSettings();
      checkConfig();
    }).catch(function(e){
      document.getElementById("set-msg").textContent = "恢复失败：" + e.message;
    });
  }

  function renderHidden(items){
    var panel = document.getElementById("hidden-panel");
    var box = document.getElementById("hidden-list");
    // 用户 m08902 第 6 条：「会话管理」页删掉后这两个元素也不在了，
    // 再往下 panel.setAttribute 会抛错（被 loadHidden 的 catch 吞掉，页面看不出问题）。
    if(!panel || !box) return;
    if(!items || !items.length){ panel.setAttribute("data-has", "0"); renderTabs(); return; }
    panel.setAttribute("data-has", "1");
    var html = "<div class='tbl'><table><thead><tr><th>会话</th><th>sid</th><th></th></tr></thead><tbody>";
    items.forEach(function(sid){
      var _p = String(sid).split(":");
      html += "<tr><td>" + esc(_p.length >= 2 ? _p[_p.length-1] : sid) + "</td>";
      html += "<td class='mono'>" + esc(sid) + "</td>";
      html += "<td><button class='mini' data-show='" + esc(sid) + "'>恢复</button></td></tr>";
    });
    html += "</tbody></table></div>";
    box.innerHTML = html;
    renderTabs();
  }

  function loadHidden(force){
    getOverview(force).then(function(d){ renderHidden(d.hidden || []); }).catch(function(){});
  }

  document.getElementById("set-tabs").addEventListener("click", function(e){
    var b = e.target && e.target.closest ? e.target.closest("button[data-page]") : null;
    if(!b) return;
    curPage = b.getAttribute("data-page");
    rememberPage();
    Array.prototype.forEach.call(document.querySelectorAll("#set-tabs button"), function(x){
      x.classList.toggle("on", x === b);
    });
    applyPage(true);
    loadPageData(curPage);
    // 吸顶页签允许在页面中部换页；留在原滚动位置会停在新页面中段，看着像跳了一下。
    if((window.scrollY || window.pageYOffset || 0) > 4) window.scrollTo(0, 0);
  });

  // 用户 m08123：JEV 自定义接口的密钥——单独一个接口存，不走配置自动保存。
  document.getElementById("settings").addEventListener("click", function(e){
    // 用户 m3096 追加：折叠开关在左栏工具条上，按钮自己带卡片 id。
    var mch = (e.target && e.target.closest) ? e.target.closest("[data-mc-toggle]") : null;
    if(mch){
      var mcid = mch.getAttribute("data-mc-toggle") || "";
      var mcard = mcid ? document.getElementById(mcid) : null;
      if(mcard){
        var mclosed = mcard.classList.toggle("mc-collapsed");
        if(mcid) MC_OPEN[mcid] = !mclosed;
        mch.setAttribute("aria-expanded", mclosed ? "false" : "true");
        // 提示词页会摘掉所有 title（用户 m10962），状态说明改挂 aria-label。
        mch.setAttribute("aria-label", mclosed ? "展开模型设置" : "收起模型设置");
        // 用户 m01754：按钮上直接写出「折叠 / 展开」，不用悬浮也知道现在是什么状态。
        var mst = mch.querySelector(".pp-mc-st");
        if(mst) mst.textContent = mclosed ? "折叠" : "展开";
        // 展开后标签列宽才量得准（--dlbw 只在可见时有效）。
        if(typeof ppSyncPairLabel === "function") ppSyncPairLabel();
      }
      return;
    }
    // 用户 m06705：设置页里的引导气泡（data-goto）点一下切到对应页签。
    var go = (e.target && e.target.closest) ? e.target.closest("[data-goto]") : null;
    if(go){
      var gtb = document.querySelector("#set-tabs button[data-page='" + go.getAttribute("data-goto") + "']");
      if(gtb) gtb.click();
      return;
    }
    var getter = (e.target && e.target.closest) ? e.target.closest("[data-jev-get]") : null;
    if(getter){ jevFetchModels(); return; }
    // 用户 m09981：往右那一列的小页签 1 2 3 … +，以及删除当前页。
    var tab = (e.target && e.target.closest) ? e.target.closest("[data-jev-page]") : null;
    if(tab){ jevPickPage(tab.getAttribute("data-jev-page")); return; }
    var add = (e.target && e.target.closest) ? e.target.closest("[data-jev-pageadd]") : null;
    if(add){ jevAddPage(); return; }
    var del = (e.target && e.target.closest) ? e.target.closest("[data-jev-pagedel]") : null;
    if(del){ var pgDel = jevPage(); mdConfirm("删掉第 " + String(pgDel.id || "1") + " 页？", {title:"删除这一页", danger:true, ok:"删除"}).then(function(ok){ if(ok) jevDelPage(); }); return; }
    // 用户 m01978 第 3 条：点「填入API」那一行的名字改名。
    var ren = (e.target && e.target.closest) ? e.target.closest("[data-jev-rename]") : null;
    if(ren){ jevRenamePage(); return; }
    // 点清单里的模型名 = 填进输入框，并写回当前这一页。
    var mitem = (e.target && e.target.closest) ? e.target.closest("[data-jev-model]") : null;
    if(mitem){
      var minp = document.getElementById("jev-apimodel");
      if(minp){
        minp.value = String(mitem.getAttribute("data-jev-model") || "");
        var pgPick = jevPage();
        pgPick.model = minp.value;
        jevSavePages();
        jevRefresh();
      }
      jevShowModelList(false);
      return;
    }
    if(!(e.target && e.target.closest && e.target.closest(".jev-combo"))) jevShowModelList(false);
    var btn = (e.target && e.target.closest) ? e.target.closest("[data-jev-key]") : null;
    if(!btn) return;
    var act = btn.getAttribute("data-jev-key");
    var inp = document.getElementById("jev-key");
    var note = document.getElementById("jev-key-note");
    var pgKey = jevPage();
    var key = (act === "save" && inp) ? String(inp.value || "").trim() : "";
    if(act === "save" && !key){
      if(note) note.textContent = "先填密钥再保存。";
      return;
    }
    function doKeyPost(){
      if(note) note.textContent = act === "save" ? "保存中…" : "清除中…";
      fetch(API + "/jev-key", {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({api_key: key, api_base: String(pgKey.base || ""), id: String(pgKey.id || "1")})
    }).then(function(r){ return r.json().catch(function(){ return {}; }); })
      .then(function(d){
        if(!d || !d.ok){
          if(note) note.textContent = "失败：" + String((d && d.error) || "未知错误");
          return;
        }
        if(inp) inp.value = "";
        pgKey.api_key_set = !!d.api_key_set;
        pgKey.api_key_masked = String(d.masked || "");
        if(JEVOPT && JEVOPT.config){
          JEVOPT.config.api_key_set = !!d.api_key_set;
          JEVOPT.config.api_key_masked = String(d.masked || "");
        }
        jevRerenderCard();
      })
      .catch(function(err){
        if(note) note.textContent = "失败：" + String((err && err.message) || err);
      });
    }
    if(act === "clear"){
      mdConfirm("清除第 " + String(pgKey.id || "1") + " 页已保存的密钥？", {title:"清除密钥", danger:true, ok:"清除"}).then(function(ok){ if(ok) doKeyPost(); });
      return;
    }
    doKeyPost();
  });

  // 用户 m08755 第 4 条：接口地址边打字边显示真正要打的 URL。
  // 以前那一行读的是已保存的配置，填了阿里地址还照旧显示 typesafe。
  document.getElementById("settings").addEventListener("input", function(e){
    // 用户 m09981：边打字边更新当前这一页（地址实时显示，模型名先记在内存里，change 时存盘）。
    if(e.target && e.target.id === "jev-base"){
      jevPage().base = String(e.target.value || "");
      jevPaintEndpoint();
    }
  });
  // 点进模型名输入框就把清单摊开（「获取」已经拉到过的话）。
  document.getElementById("settings").addEventListener("focusin", function(e){
    if(e.target && e.target.id === "jev-apimodel"){
      jevPaintModelList();
      jevShowModelList(true);
    }
  });

  // 用户 2026-11-06：转发消息处理三列互斥，点哪个存哪个（离散动作，立刻保存）。
  document.getElementById("settings").addEventListener("click", function(e){
    var b = e.target && e.target.closest ? e.target.closest("[data-fwdmode]") : null;
    if(!b) return;
    var v = b.getAttribute("data-fwdmode");
    // 用户 m30204：点已经选中的那一项不该再存一次——按钮上的 .on 就是当前值。
    if(b.classList.contains("on")) return;
    if(!configCache) configCache = {};
    configCache["forward_mode"] = v;
    var box = b.parentNode;
    if(box && box.querySelectorAll){
      [].slice.call(box.querySelectorAll("[data-fwdmode]")).forEach(function(x){
        if(x === b) x.classList.add("on"); else x.classList.remove("on");
      });
    }
    // 说明跟着选中项走，不用把三句都铺在行里。
    var row = b.closest ? b.closest("[data-row]") : null;
    if(row){
      var ds = row.querySelector("[data-fwddesc]");
      if(ds) ds.textContent = fwdModeDesc(v);
      var nm = row.querySelector(".nm");
      if(nm) nm.setAttribute("title", fwdModeDesc(v));
    }
    dirty = true;
    saveNow();
  });
  // 用户 m27095：动图三档也是分段按钮，点哪个存哪个——三个键一次写全，
  // 后端 _gif_mode 按 九帧 → 原图 → 首帧 的顺序取第一个 true，所以只留选中的那个为 true。
  document.getElementById("settings").addEventListener("click", function(e){
    var b = e.target && e.target.closest ? e.target.closest("[data-gifmode]") : null;
    if(!b) return;
    var v = b.getAttribute("data-gifmode");
    // 用户 m30204：点已经选中的那一项不该再存一次——按钮上的 .on 就是当前值。
    if(b.classList.contains("on")) return;
    if(!configCache) configCache = {};
    GIF_MODES.forEach(function(o){ configCache[o.k] = (o.v === v); });
    var box = b.parentNode;
    if(box && box.querySelectorAll){
      [].slice.call(box.querySelectorAll("[data-gifmode]")).forEach(function(x){
        if(x === b) x.classList.add("on"); else x.classList.remove("on");
      });
    }
    // 说明跟着选中项走，不用把三句都铺在行里。
    var row = b.closest ? b.closest("[data-row]") : null;
    if(row){
      var ds = row.querySelector("[data-gifdesc]");
      if(ds) ds.textContent = gifModeDesc(v);
    }
    dirty = true;
    saveNow();
  });
  // 用户 m30656：拼图帧数也是分段按钮，点哪个存哪个。
  document.getElementById("settings").addEventListener("click", function(e){
    var b = e.target && e.target.closest ? e.target.closest("[data-gifframes]") : null;
    if(!b) return;
    var v = parseInt(b.getAttribute("data-gifframes"), 10);
    // 用户 m30204：点已经选中的那一项不该再存一次——按钮上的 .on 就是当前值。
    if(b.classList.contains("on")) return;
    // 用户 m13967：0 是「自动」档，不能被 || 9 吃掉
    if(!isFinite(v) || v < 0) v = 9;
    if(!configCache) configCache = {};
    configCache["gif_frames"] = v;
    var box = b.parentNode;
    if(box && box.querySelectorAll){
      [].slice.call(box.querySelectorAll("[data-gifframes]")).forEach(function(x){
        if(x === b) x.classList.add("on"); else x.classList.remove("on");
      });
    }
    gifFramesPaint();
    dirty = true;
    saveNow();
  });
  // 用户 m31535：单帧边长也是分段按钮，点哪个存哪个；拼图尺寸和 token 估算跟着变。
  document.getElementById("settings").addEventListener("click", function(e){
    var b = e.target && e.target.closest ? e.target.closest("[data-giftile]") : null;
    if(!b) return;
    var v = parseInt(b.getAttribute("data-giftile"), 10) || 384;
    // 用户 m30204：点已经选中的那一项不该再存一次——按钮上的 .on 就是当前值。
    if(b.classList.contains("on")) return;
    if(!configCache) configCache = {};
    configCache["gif_tile"] = v;
    var box = b.parentNode;
    if(box && box.querySelectorAll){
      [].slice.call(box.querySelectorAll("[data-giftile]")).forEach(function(x){
        if(x === b) x.classList.add("on"); else x.classList.remove("on");
      });
    }
    gifTilePaint();
    gifFramesPaint();
    dirty = true;
    saveNow();
  });
  // 用户 m07418：黑名单页两个清单都是页内胶囊——短语的 + / ×、用户黑名单的 + / × 与手动拉黑，
  // 全部就地改：短语走隐藏 input + 自动保存，用户黑名单直接打接口（立刻生效）。
  document.getElementById("settings").addEventListener("click", function(e){
    var t = e.target;
    if(!t || !t.getAttribute) return;
    // 短语：+ 开输入框 / × 删一枚
    if(t.getAttribute("data-padd")){
      var boxP = t.parentNode, inpP = boxP && boxP.querySelector(".blk-in");
      if(inpP){ inpP.hidden = false; t.hidden = true; try{ inpP.focus(); }catch(err){} }
      return;
    }
    if(t.getAttribute("data-pdel")){
      var chipP = t.parentNode;
      var wordP = (chipP && chipP.classList.contains("chip")) ? phraseChipText(chipP) : "";
      if(!wordP) return;
      // 用户 m07715：单群 / 私聊删全局条目＝只对本会话解除，后端返回的 unblock_items 会把它画成灰的
      phrasePost(phraseQs("&text=" + encodeURIComponent(wordP) + "&action=remove"))
        .catch(function(){ /* 静默：接口报错就保持原样 */ });
      return;
    }
    // 用户 m07715：灰胶囊（全局拉黑、本会话已解除）点一下恢复。
    // 点到的可能是里面的 <em>，所以用 closest 往上找，别只看 e.target。
    var chipOff = (t.closest ? t.closest(".chip[data-pun], .chip[data-urestore]") : null);
    if(chipOff){
      if(chipOff.getAttribute("data-pun")){
        var wordO = phraseChipText(chipOff);
        if(!wordO) return;
        phrasePost(phraseQs("&text=" + encodeURIComponent(wordO) + "&action=restore"))
          .catch(function(){ /* 静默 */ });
        return;
      }
      var uidR = chipOff.getAttribute("data-uid") || "";
      if(!uidR) return;
      ublkPost("?uid=" + encodeURIComponent(uidR) + "&action=restore").catch(function(){ /* 静默 */ });
      return;
    }
    // 用户黑名单：+ 开表单 / × 解黑 / 拉黑 / 取消
    if(t.getAttribute("data-uadd")){
      var frmU = document.getElementById("ublk-form");
      if(frmU){
        frmU.hidden = false;
        var neU = frmU.querySelector(".ub-name");
        try{ if(neU) neU.focus(); }catch(err){}
      }
      return;
    }
    if(t.getAttribute("data-ucancel")){ closeBlkForm(blkFormOf(t)); return; }
    if(t.getAttribute("data-uok")){ ublkCommit(); return; }
    if(t.getAttribute("data-udel")){
      var chipU = t.parentNode;
      var uidU = chipU ? (chipU.getAttribute("data-uid") || "") : "";
      if(!uidU) return;
      var nmU = chipU.getAttribute("data-name") || "";
      var whoU = nmU ? ("「" + nmU + "」") : ("QQ " + uidU);
      // 用户 m07715：单群 / 私聊解除全局拉黑只影响本会话，全局名单不动
      var qU = scopeSid
        ? ("把" + whoU + "在本会话解除拉黑？全局名单不动，切回全局还能看到他。")
        : ("把" + whoU + "从黑名单里放出来？");
      mdConfirm(qU, {title:"解除拉黑", ok:"解黑"}).then(function(ok){
        if(!ok) return;
        // 参数走 query：后端按函数签名从 query 取值，JSON body 它读不到（实测解黑没反应）。
        ublkPost("?uid=" + encodeURIComponent(uidU) + "&action=remove").catch(function(){ /* 静默 */ });
      });
      return;
    }
  });
  // 短语输入框：回车加一枚、Esc 收起；失焦且没填就收起（跟外号那套一个手感）
  document.getElementById("settings").addEventListener("keydown", function(e){
    var t = e.target;
    if(t && t.classList && (t.classList.contains("ub-name") || t.classList.contains("ub-qq"))){
      if(e.key === "Escape"){ e.preventDefault(); closeBlkForm(blkFormOf(t)); return; }
      if(e.key !== "Enter") return;
      e.preventDefault();
      ublkCommit();
      return;
    }
    if(!t || !t.classList || !t.classList.contains("blk-in")) return;
    if(e.key === "Escape"){ e.preventDefault(); closeBlkInput(t); return; }
    if(e.key !== "Enter") return;
    e.preventDefault();
    var v = (t.value || "").trim();
    if(!v) return;
    var boxC = t.parentNode;
    var dup = false;
    Array.prototype.forEach.call(boxC.querySelectorAll(".chip"), function(c){
      if(phraseChipText(c) === v) dup = true;
    });
    if(dup){ closeBlkInput(t); return; }
    closeBlkInput(t);
    // 用户 m07715：单群 / 私聊里点「+」新增也写全局（用户拍板），加完所有群一起生效
    phrasePost(phraseQs("&text=" + encodeURIComponent(v) + "&action=add"))
      .then(function(d){
        if(d && d.ok === false) mdAlert("没加上：" + (d.detail || "接口没返回 ok"));
      })
      .catch(function(err){ mdAlert("没加上：" + (err && err.message ? err.message : err)); });
  });
  document.getElementById("settings").addEventListener("focusout", function(e){
    var t = e.target;
    if(t && t.classList && (t.classList.contains("ub-name") || t.classList.contains("ub-qq"))){
      var frmF = blkFormOf(t);
      setTimeout(function(){
        if(!frmF || !document.body.contains(frmF)) return;
        var ae = document.activeElement;
        if(ae && frmF.contains(ae)) return;
        var n0 = frmF.querySelector(".ub-name"), q0 = frmF.querySelector(".ub-qq");
        if(!((n0 && n0.value) || "").trim() && !((q0 && q0.value) || "").trim()) closeBlkForm(frmF);
      }, 120);
      return;
    }
    if(!t || !t.classList || !t.classList.contains("blk-in")) return;
    setTimeout(function(){ if(!(t.value || "").trim()) closeBlkInput(t); }, 120);
  });
  // v3.92（用户 m00697 第 15 条）：三档开关点哪段切哪段——两个隐藏 input 同步，再派一次 change
  // 走既有的自动保存（wakeMutex 的互斥也挂在那条链上）。
  document.getElementById("settings").addEventListener("click", function(e){
    var seg = (e.target && e.target.closest) ? e.target.closest(".sw3 b") : null;
    if(!seg) return;
    var box = seg.parentNode;
    var v = seg.getAttribute("data-v");
    if(!box || !v) return;
    // 用户 m01445：决策模式没开时「必回」拨不过去。
    if(v === "always" && box.classList.contains("no-must")) return;
    box.setAttribute("data-v", v);
    var k1 = box.getAttribute("data-3k"), k2 = box.getAttribute("data-3a");
    var i1 = box.querySelector("input[data-k='" + k1 + "']");
    var i2 = box.querySelector("input[data-k='" + k2 + "']");
    if(i1) i1.checked = (v === "trigger");
    if(i2) i2.checked = (v === "always");
    if(i1) i1.dispatchEvent(new Event("change", {bubbles: true}));
  });
  // v3.92（用户 m00697 第 1 条）：机器人昵称的框跟着输入内容变宽，文字右对齐。
  document.getElementById("settings").addEventListener("input", function(e){
    var el = e.target;
    if(!el || !el.getAttribute || el.getAttribute("data-k") !== "bot_name") return;
    var s = String(el.value || ""), w = 0;
    for(var i = 0; i < s.length; i++) w += (s.charCodeAt(i) > 255 ? 14 : 7.6);
    el.style.width = Math.max(72, Math.min(200, Math.round(w) + 26)) + "px";
  });
  // ===== 不确定段长条（用户 m16307 图2）=====
  // 拖动只改样式，松手才写回两个 hidden input 并派发 change——走原来的上下限夹紧 + 700ms 防抖存盘。
  function jevSliderCommit(box){
    var lo = parseInt(box.getAttribute("data-lo"), 10) || 0;
    var hi = parseInt(box.getAttribute("data-hi"), 10) || 0;
    [["jev_lower", lo], ["jev_upper", hi]].forEach(function(p){
      var n = box.querySelector("input[data-k='" + p[0] + "']");
      if(!n || n.value === String(p[1])) return;
      n.value = String(p[1]);
      n.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }
  (function(){
    var drag = null;
    function pctOf(box, clientX){
      var track = box.querySelector(".jev-slider-track");
      if(!track) return null;
      var r = track.getBoundingClientRect();
      if(r.width <= 0) return null;
      var p = Math.round(((clientX - r.left) / r.width) * 100 / 5) * 5;
      return Math.max(0, Math.min(100, p));
    }
    function apply(box, h, pct){
      var lo = parseInt(box.getAttribute("data-lo"), 10) || 0;
      var hi = parseInt(box.getAttribute("data-hi"), 10) || 0;
      if(h === "lo") lo = Math.min(pct, hi); else hi = Math.max(pct, lo);
      jevSliderPaint(box, lo, hi);
    }
    var host = document.getElementById("settings");
    if(!host) return;
    host.addEventListener("pointerdown", function(e){
      var k = (e.target && e.target.closest) ? e.target.closest(".jev-slider-knob") : null;
      if(!k) return;
      var box = k.closest(".jev-slider");
      if(!box) return;
      e.preventDefault();
      k.focus();
      drag = { box: box, h: k.getAttribute("data-h") };
      k.classList.add("dragging");
      try{ k.setPointerCapture(e.pointerId); }catch(_e){}
    });
    document.addEventListener("pointermove", function(e){
      if(!drag) return;
      var p = pctOf(drag.box, e.clientX);
      if(p === null) return;
      apply(drag.box, drag.h, p);
    });
    function endDrag(){
      if(!drag) return;
      var box = drag.box;
      drag = null;
      Array.prototype.forEach.call(box.querySelectorAll(".jev-slider-knob.dragging"), function(n){ n.classList.remove("dragging"); });
      jevSliderCommit(box);
    }
    document.addEventListener("pointerup", endDrag);
    document.addEventListener("pointercancel", endDrag);
    host.addEventListener("keydown", function(e){
      var k = (e.target && e.target.closest) ? e.target.closest(".jev-slider-knob") : null;
      if(!k) return;
      var step = (e.key === "ArrowLeft" || e.key === "ArrowDown") ? -5 : ((e.key === "ArrowRight" || e.key === "ArrowUp") ? 5 : 0);
      if(!step) return;
      var box = k.closest(".jev-slider");
      if(!box) return;
      e.preventDefault();
      var h = k.getAttribute("data-h");
      var cur = parseInt(box.getAttribute(h === "lo" ? "data-lo" : "data-hi"), 10) || 0;
      apply(box, h, Math.max(0, Math.min(100, cur + step)));
      jevSliderCommit(box);
    });
  })();
  document.getElementById("settings").addEventListener("change", function(e){
    // v3.30：伪随机兜底开关变了，用不上的参数行要跟着灰/亮
    var dk0 = (e.target && e.target.getAttribute) ? e.target.getAttribute("data-k") : null;
    // 用户 m15110：动图质量改了，拼图帧数那行的 token 预估要跟着重算（质量越低估得越少）。
    if(dk0 === "gif_quality") gifFramesPaint(undefined, e.target.value);
    // 用户 m09851：窗口页「图片」组里那四项是框架的图片压缩设置（system_config.json），
    // 不在插件配置里，走 data-core 单独存，别混进 collectSettings。
    var coreF = (e.target && e.target.getAttribute) ? e.target.getAttribute("data-core") : null;
    if(coreF){
      var coreV = (e.target.type === "checkbox") ? !!e.target.checked : (parseInt(e.target.value, 10) || 0);
      saveCoreImg(coreF, coreV);
      return;
    }
    // v3.62（用户 m18059）：缓存记录页签里的两处交互，必须在通用 data-k 分支之前拦掉——
    //   1) 顶部「作用范围」下拉：换页面的作用域（等于顶部那个全局/单群选择器）；
    //   2) data-ck 的数字框：选中某个会话时，写的是它自己的覆盖，不能走全局自动保存（会被后端丢掉）。
    if(e.target && e.target.id === "cache-scope"){
      cachePickScope(e.target.value || "");
      return;
    }
    if(dk0 && e.target.getAttribute && e.target.getAttribute("data-ck") === "1" && cacheScopeSid){
      cacheWriteKey(dk0, e.target.value);
      return;
    }
    if(dk0 === "prob_pseudo") refreshDimRows();
    // 用户 m31535：单帧边长改成档位按钮后不再走 change，尺寸估算在点击处理里刷新。
    // 2026-11-08（J）：不确定区间合成一行两个下拉，选完当场把另一头拉回来，
    // 别让下限跑到上限上面去（后端会判错，用户还得自己找哪一头不对）。
    if(dk0 === "jev_lower" || dk0 === "jev_upper"){
      var loSel = document.querySelector("#settings [data-k='jev_lower']");
      var hiSel = document.querySelector("#settings [data-k='jev_upper']");
      if(loSel && hiSel){
        var loV = parseInt(loSel.value, 10), hiV = parseInt(hiSel.value, 10);
        if(!isNaN(loV) && !isNaN(hiV) && loV > hiV){
          if(dk0 === "jev_lower") hiSel.value = String(loV);
          else loSel.value = String(hiV);
        }
      }
    }
    // 用户 m09981：自定义接口每一页的协议 / 地址 / 模型名，改完写回 jev_apis。
    var pgField = (e.target && e.target.getAttribute) ? e.target.getAttribute("data-jev-pg") : null;
    if(pgField){
      var pg = jevPage();
      if(pgField === "protocol"){
        pg.protocol = String(e.target.value || "systemone");
        JEV_MODELS = [];
        jevSavePages();
        // 协议换了占位符和「实际请求地址」都要跟着变，整张卡重画一次最稳。
        jevRerenderCard();
        return;
      }
      if(pgField === "base"){ pg.base = String(e.target.value || ""); jevPaintEndpoint(); }
      if(pgField === "model"){ pg.model = String(e.target.value || ""); }
      jevSavePages();
      jevRefresh();
      return;
    }
    // v3.53（用户本轮第 4 条）：进阶模式一开一关，下面退避整组的明暗跟着变
    // 用户 m06706：决策模式 / JEV 决策互斥，开一个当场把另一个关掉并存盘。
    // 必须在通用 data-k 分支（末尾那个 scheduleSave）之前拦掉——那里是 700ms 防抖，
    // 半路切走就只存了一半，后端会读到「两个都开」。
    if(dk0 === "advanced_enabled" || dk0 === "jev_enabled"){
      if(e.target.checked) decisionModeMutex(dk0, true);
      if(configCache) configCache[dk0] = !!e.target.checked;
      dirty = true;
      refreshDimRows();
      saveNow();
      return;
    }
    // 用户 2026-11-06：转发图全请求是风险项，打开前先弹一次确认；取消就把开关弹回去。
    if(dk0 === "forward_images_all"){
      var fsw = e.target;
      if(!fsw.checked){
        if(configCache) configCache[dk0] = false;
        dirty = true;
        saveNow();
        return;
      }
      mdDialog({
        title: "转发图片全部请求",
        message: "打开后，合并转发里的图会全部发给模型。\n一条几十张的转发可能让请求涨到十几 MB，也会更贵。\n确定要打开吗？",
        ok: "仍然打开",
        danger: true
      }).then(function(ok){
        fsw.checked = !!ok;
        if(configCache) configCache[dk0] = !!ok;
        dirty = true;
        saveNow();
      });
      return;
    }

    // 用户 m06007：重试取舍这两个开关是双刃剑，拨到开启先弹一次风险确认；
    // 点「点错」就把开关弹回去，不写盘。
    if(dk0 === "retry_take_pending" || dk0 === "timeout_no_retry"){
      var rsw = e.target;
      if(!rsw.checked){
        if(configCache) configCache[dk0] = false;
        dirty = true;
        saveNow();
        return;
      }
      var rf = (schemaCache && schemaCache[dk0]) || {};
      mdDialog({
        title: "是否开启风险选项",
        message: (rf.hint || "") + "\n这是双刃剑功能，开之前想清楚代价。",
        ok: "确定",
        cancel: "点错",
        danger: true
      }).then(function(ok){
        rsw.checked = !!ok;
        if(configCache) configCache[dk0] = !!ok;
        dirty = true;
        saveNow();
      });
      return;
    }
    if(dk0 === "advanced_enabled") refreshDimRows();
    // v3.44：成对行的开关一动，数值框的明暗跟着变
    // 用户 m04896 第 6 条：丢弃文件视频一动，「过滤文件视频」那行的明暗跟着变。
    // 用户 m06021：静默过滤两两并排的开关一动，自己那格条数框的明暗也跟着变。
    if(dk0 && (Object.prototype.hasOwnProperty.call(PAIR_NUM, dk0) ||
               Object.prototype.hasOwnProperty.call(SW_PAIR, dk0) ||
               Object.prototype.hasOwnProperty.call(SILENT_ROWS, dk0) ||
               dk0 === "file_skip_context")) refreshDimRows();
    // v3.53（用户本轮第 3 条）：配额两个开关互斥，逻辑在 quotaMutex 里（单测能直接调）
    if(dk0 === "quota_hour_enabled" || dk0 === "quota_day_enabled"){
      quotaMutex(dk0, !!e.target.checked);
    }
    // 用户 m10297 第 2 条：唤醒组一行里「触发 / 必回」互斥，逻辑在 wakeMutex。
    if(dk0 && wakeMutex(dk0, !!e.target.checked)){
      dirty = true;
    }
    // v3.16（用户 m40583）：三列标题上的模式开关 + 普通模式列里的两个子开关。
    // 都是互斥的：点开哪个就切到哪个，点已开的那个不算关（总得有一档在用）。
    var msw = (e.target && e.target.closest) ? e.target.closest(".msw") : null;
    if(msw){
      var mkey = msw.getAttribute("data-msw");
      if(mkey === "dev"){
        // v3.53（用户本轮第 4 条）：进阶模式已经是真开关（advanced_enabled，写在行里、带 data-k），
        // dev 列标题上不再生成模式开关，这一段已经没有入口；留着只做兜底——不再把开关弹回去，
        // 也不再拦，免得浏览器里残留的旧 DOM 一点就被打回原形。
        return;
      }
      // v3.33（第 9 条）：曲线开关直接写 bands_enabled；动态调整必须先开曲线；
      // 点普通模式 = 关掉曲线回到计数/概率。
      if(mkey === "bands"){
        var bOn = !!e.target.checked;
        if(!bOn && trgVal() === "adaptive") trgSet(lastNormal);
        if(configCache) configCache.bands_enabled = bOn;
        syncModeUI();
        dirty = true;
        saveNow();   // v3.71（用户 m23596 第 4 条）：曲线开关立刻存，否则马上离开就丢
        return;
      }
      if(mkey === "adaptive"){
        if(!e.target.checked){ e.target.checked = true; return; }
        if(!bandsOn()){
          e.target.checked = false;
          syncModeUI();
          var m1 = document.getElementById("set-msg");
          if(m1){
            m1.textContent = "先开启时段曲线，动态调整要按曲线给的概率来算";
            setTimeout(function(){ if(m1.textContent.indexOf("时段曲线") >= 0) m1.textContent = ""; }, 2600);
          }
          return;
        }
        trgSet("adaptive");
        return;
      }
      if(!e.target.checked){ e.target.checked = true; return; }
      var bEl = document.querySelector("#settings .msw[data-msw='bands'] input");
      if(bEl && bEl.checked){
        bEl.checked = false;
        if(configCache) configCache.bands_enabled = false;
      }
      trgSet(lastNormal);
      return;
    }
    var sub = (e.target && e.target.getAttribute) ? e.target.getAttribute("data-sub") : null;
    if(sub){
      if(!e.target.checked){ e.target.checked = true; return; }
      trgSet(sub === "probability" ? "probability" : "count");
      return;
    }
    if(e.target && e.target.getAttribute && e.target.getAttribute("data-k")){
      dirty = true;
      // v3.50（用户 m06761 第 4 条）：开关小字改成 <i class='on'>开</i><i class='off'>关</i>
      // 两段，露哪个交给 CSS 的 input:checked —— 这里不能再改 .st 的 textContent，
      // 那样会把两个 <i> 整段替换掉，CSS 就再也没东西可切了。
      scheduleSave();
    }
  });
  // v3.14（用户 m40145）：外号用「+」添加——点开才出现输入框，加完/取消自动收起。
  // ===== 时段曲线（v3.28，用户 m47321）=====
  // 24 小时按 30 分钟切 48 格，每段一个触发概率百分比。配置里存 JSON 文本：
  // [{"s":0,"e":18,"v":8}, ...]，s/e 是槽位（e 不含），后端 _normalize_bands 兜底规整。
  // 目前只存不用：动态模式方案还没定，等他定了再接进判定。
  function saveCoreImg(field, val){
    var msg = document.getElementById("set-msg");
    if(msg) msg.textContent = "保存中…";
    return get("/api/configuration").then(function(d){
      var cfg = (d && d.configuration) || null;
      if(!cfg) throw new Error("读不到框架配置");
      var bot = cfg.bot_config || (cfg.bot_config = {});
      var ic = bot.image_compression || (bot.image_compression = {});
      ic[field] = val;
      return fetch("/api/configuration", {
        method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(cfg)
      }).then(function(r){
        if(!r.ok) throw new Error("HTTP " + r.status);
        coreImgCache = ic;
        coreImgTried = true;
        if(msg) msg.textContent = "图片压缩已保存";
      });
    }).catch(function(e){
      if(msg) msg.textContent = "图片压缩保存失败：" + ((e && e.message) || e);
      // 失败就把界面拉回真实值，别让用户以为存上了。
      return get("/api/configuration").then(function(d){
        var ic2 = d && d.configuration && d.configuration.bot_config &&
                  d.configuration.bot_config.image_compression;
        if(ic2) coreImgCache = ic2;
        coreImgTried = true;
        renderSettings(true);   // 回滚界面：控件可能刚被用户点过，DOM 与缓存不同，必须强制写
      }).catch(function(){});
    });
  }
  async function cachePickScope(sid){
    sid = cacheNormSid(sid);
    cacheScopeSid = sid;
    if(sid === (scopeSid || "")){ renderCacheScopeOptions(); return; }
    // 改了没保存就切走 = 静默丢改动，先问一声（和顶部选择器同一套规矩）
    if(dirty && !(await mdConfirm("有改动还没存完，切走就丢了。继续吗？"))){
      renderCacheScopeOptions();   // 下拉弹回原来那项，别停在没生效的选择上
      return;
    }
    scopeSid = sid;
    settingsScopeChanged();
    cacheScopeSid = sid;
    cacheScopeOv = null;
    cacheScopeErr = "";
    cacheScopeLoading = false;
    dirty = false;
    renderScopeOptions();   // v3.67：顶部卡片跟着换高亮（下拉框已撤）
    var top = document.getElementById("set-msg");
    if(top) top.textContent = "";
    renderCacheScopeOptions();
    loadSettings();   // 一路重载后 loadCacheStats → mountCacheBlock 会按新 sid 拉覆盖
  }
  // 拉这个会话的覆盖：拿不到就按全局值显示，并把「为什么是全局值」写在提示行里，不装成它的值。
  function schedulePricingSave(){
    if(pricingTimer) clearTimeout(pricingTimer);
    pricingPartialSaved = false;
    setPMsg(pricingPendingMessage(false));
    pricingTimer = setTimeout(function(){ pricingTimer = 0; savePricing(); }, PRICING_DELAY);
  }

  function loadPricing(silent){
    var box = document.getElementById("pricing-box");
    if(!box){ pricingCache = pricingCache || []; return; }
    var hours = String(pricingHours);
    // 同一轮 renderSettings 会调两次 loadPricing；第一次已经回来时 in-flight 守卫抓不到，
    // 这里留 2 秒缓存，第二次直接重画新 DOM，不再多发一份 4.5KB。
    if(!silent && pricingCache && pricingCacheHours === hours && pricingCacheWriteVersion === pricingWriteVersion && Date.now() - pricingCacheAt < 2000){
      paintPricing();
      return Promise.resolve();
    }
    // v4.85：这里原本会把「同 hours 且还在飞」的请求合并成一份，但那样保存成功后的
    // 静默刷新会拿到保存前那份统计（花费 / 请求数停在上一次），同一 tick 里两次
    // loadPricing 也测不出「旧回复不能盖新数据」。现在一律发新请求，靠 pricingLoadSeq
    // 丢弃旧回复；同一次渲染里重复调用由上面那个 2 秒缓存兜住。
    pricingInFlightHours = hours;
    var seq = ++pricingLoadSeq;
    var writeVersion = pricingWriteVersion;
    pricingInFlight = get(API + "/pricing?hours=" + encodeURIComponent(hours)).then(function(d){
      if(seq !== pricingLoadSeq) return;
      var previous = pricingCache || [];
      var rows = ((d && d.rows) || []).filter(function(r){ return !pricingRemoved[r.model]; });
      // Statistics can arrive after an acknowledgement: never replace newer prices.
      if(writeVersion !== pricingWriteVersion){
        rows.forEach(function(r){
          var old = previous.filter(function(p){ return p.model === r.model; })[0];
          if(old){ r.in=old.in; r.out=old.out; r.cache=old.cache; r.peak=pricingPeak(old.peak); }
        });
      }
      previous.forEach(function(r){
        var keep = pricingDraft[r.model] || (writeVersion !== pricingWriteVersion && (r.in != null || r.out != null || r.cache != null));
        if(keep && !pricingRemoved[r.model] && !rows.some(function(n){ return n.model === r.model; })){
          // The older range can lack a newly saved row. Keep its identity/prices,
          // but do not carry token counts from a different statistics window.
          rows.push(Object.assign({},r,{input:0,output:0,cached:0,rounds:0,decision_rounds:0,reply_rounds:0,calls:0}));
        }
      });
      pricingCache = rows;
      pricingCacheAt = Date.now();
      pricingCacheHours = hours;
      pricingCacheWriteVersion = writeVersion;
      pricingUnit = (d && d.unit) || "元 / 百万 token";
      pricingTimezone = (d && d.timezone) || "";
      pricingCost = writeVersion === pricingWriteVersion && d && d.cost ? d.cost : null;
      if(silent){
        var costEl = document.getElementById("p-cost");
        if(costEl) costEl.innerHTML = priceCostHtml();
        paintCostCells(box);
      }else paintPricing();
      // 用户 m19357：图表跟表格是两个接口，表格画完顺手把图也拉一次（20 秒内不重复拉；
      // 保存价目后的静默刷新要强制拉，不然花费还是改价前的）。
      loadPricingTrend(pricingTrendMode, !!silent);
      loadPricingPies(!!silent);
    }).catch(function(e){
      if(seq !== pricingLoadSeq) return;
      // Keep the editable table and drafts on a failed range reload.
      if(box.querySelector("tr[data-pmodel]")) setPMsg("价目读取失败：" + String((e && e.message) || e), "bad");
      else box.innerHTML = "<div class='phint'>价目读取失败：" + esc(String((e && e.message) || e)) + "</div>";
    }).then(function(){
      if(seq === pricingLoadSeq){ pricingInFlight = null; pricingInFlightHours = ""; }
    });
    return pricingInFlight;
  }

  // 用户 m14599：价目表「请求」列的几行字——0 的不写，别的插件调过的补「插件名 次数」。
  function puLinesHtml(r){
    var out = [];
    var calls = Number(r && r.calls) || 0;
    if(calls > 0) out.push("<span class='pl'>请求 " + esc(fmtNum(calls)) + "</span>");
    var dec = Number(r && r.decision_rounds) || 0;
    if(dec > 0) out.push("<span class='pl'>决策 " + esc(fmtNum(dec)) + "</span>");
    var rep = Number(r && r.reply_rounds) || 0;
    if(rep > 0) out.push("<span class='pl'>回复 " + esc(fmtNum(rep)) + "</span>");
    var plugs = (r && r.plugins) || [];
    for(var i = 0; i < plugs.length; i++){
      var p = plugs[i] || {};
      var n = Number(p.calls) || 0;
      if(n <= 0) continue;
      var nm = String(p.plugin_name || p.plugin_id || "");
      if(!nm) continue;
      out.push("<span class='pl pplug' title='" + esc(String(p.plugin_id || nm)) + "'>" +
        esc(nm) + " " + esc(fmtNum(n)) + "</span>");
    }
    // 一行都没有（价目表里存过价、这段时间没调用过）就写个短横，别留个空格子。
    if(!out.length) return "<span class='pl pdim'>—</span>";
    return out.join("");
  }

  function paintPricing(){
    var box = document.getElementById("pricing-box");
    if(!box) return;
    var focus = pricingFocus(box);
    // 用户本轮：费用列按 model 直接查 cost.parts，先建一份映射（parts 是按花费降序的数组）。
    var costParts = Object.create(null);
    if(pricingCost && pricingCost.parts) pricingCost.parts.forEach(function(p){
      if(p && p.model != null) costParts[p.model] = p;
    });
    // 用户 m19842：不做折叠了，只把所选区间里一次都没调用过的模型藏起来（tr.pidle）。
    var list = pricingCache || [];
    var lastVisible = -1, idleCount = 0;
    list.forEach(function(r, i){
      if(r.active === false){ idleCount++; return; }
      lastVisible = i;
    });
    var rows = list.map(function(r, i, arr){
      var d = pricingDraft[r.model] || {};
      var p = pricingPeak(Object.assign(pricingPeak(r.peak), d.peak || {}));
      var inV = d.in !== undefined ? d.in : (r.in == null ? "" : r.in);
      var outV = d.out !== undefined ? d.out : (r.out == null ? "" : r.out);
      var cacheV = d.cache !== undefined ? d.cache : (r.cache == null ? "" : r.cache);
      // 用户 m20332：渠道可以手填（r.provider 是价目表里存的覆盖值），没填时把
      // 自动识别的结果当占位提示，一眼能看出框架认成了什么。
      // 占位符用后端单独给的 provider_auto（不受手填影响），不然保存后占位会变成刚填的值。
      var autoSrc = r.provider_auto || r.provider_id || "来源未记录";
      // v4.9x（用户本轮图2 后半句）：最后一行下面补一根灰线，把价目表收个口（只加最后一行）
      // 用户 m19357：收起时把最近 24 小时没用过的行标成 pidle（CSS 隐藏），
      // 最后一行那根收口灰线跟着「最后一行看得见的」走。
      var trCls = [];
      if(i === lastVisible) trCls.push("plast");
      if(r.active === false) trCls.push("pidle");
      var lastCls = trCls.length ? " class='" + trCls.join(" ") + "'" : "";
      // 用户 m11550：输入只算没命中的那一截，缓存单列——跟上游后台的「输入 / 缓存」口径一致。
      // 后端 r.input 是完整 prompt（含缓存），计价公式要用它，所以这里减掉再显示。
      var unc = Math.max(0, (Number(r.input) || 0) - (Number(r.cached) || 0));
      return "<tr" + lastCls + " data-pmodel='" + esc(r.model) + "'>" +
        "<td class='pm'><span class='pmc'>" +
          "<button type='button' class='pdel' data-pdel='" + esc(r.model) + "' title='删除这个模型的价格，要点两次确认'>删除</button>" +
          "<span class='pname'>" + esc(r.model_name || r.model) + "</span></span>" +
          "<span class='psrcline'><span class='psrc-lb'>渠道</span>" +
            "<input class='psrc-inp' type='text' maxlength='60' data-pfield='provider' placeholder='" +
            esc(autoSrc) + "' value='" +
            esc(d.provider !== undefined ? d.provider : (r.provider || "")) + "'></span></td>" +
        pricePairHtml("in", inV, p.in) + pricePairHtml("cache", cacheV, p.cache) + pricePairHtml("out", outV, p.out) +
        // 用户 m00443：请求 / 决策 / 回复各一行，token 拆成入 / 缓存 / 出三行，再加一列缓存命中率。
        // 用户 m14599：0 的不写（以前每行都挂「决策 0 / 回复 0」，一列全是 0 看不出重点）；
        // 别的插件调过这个模型的，按「插件名 次数」再补几行，名字跟着设置页的改名走。
        "<td class='pu'><span class='puc'>" + puLinesHtml(r) + "</span></td>" +
        "<td class='pt'><span class='puc'>" +
          "<span class='pl' title='输入 " + esc(fmtNum(unc)) + "（未命中）'><i class='plt plt-in'>输入</i> " + esc(fmtBig(unc)) + "</span>" +
          "<span class='pl' title='缓存 " + esc(fmtNum(r.cached)) + "'><i class='plt plt-hit'>命中</i> " + esc(fmtBig(r.cached)) + "</span>" +
          "<span class='pl' title='输出 " + esc(fmtNum(r.output)) + "'><i class='plt plt-out'>输出</i> " + esc(fmtBig(r.output)) + "</span></span></td>" +
        "<td class='ph'>" + hitRateCellHtml(r) + "</td>" +
        // 用户本轮：紧跟缓存命中率的「费用」列——这一行在当前区间的花费。
        "<td class='pcostc'>" +
          costCellHtml(r, costParts[r.model] || null) + "</td>" +
        "<td class='psw'><input type='checkbox' data-pfield='peak.enabled'" + (p.enabled ? " checked" : "") + "></td>" +
        "<td class='psw'><input type='checkbox' data-pfield='peak.weekends'" + (!p.weekends ? " checked" : "") + "></td>" +
        "<td class='pschedule'>" + pricingPeriodsHtml(p) + "</td></tr>";
    }).join("");
    if(!rows) rows = "<tr><td colspan='11' class='pempty'>还没有用过的模型。插件跑过一轮之后这里会自动出现——模型名是自动识别的，不用手动添加。</td></tr>";
    else if(lastVisible < 0) rows = "<tr><td colspan='11' class='pempty'>这段时间没有调用过任何模型，都藏起来了。</td></tr>";
    pricingIdleCount = idleCount;
    box.innerHTML =
      "<div class='ptop'><span class='prange' id='p-range'>" + priceRangeHtml() + "</span>" +
        "<span class='pcost' id='p-cost'>" + priceCostHtml() + "</span>" +
        "</div>" +
      "<table class='ptable'><thead><tr><th>模型 / 来源</th><th>输入单价</th><th>缓存命中单价</th><th>输出单价</th>" +
        "<th>请求</th><th>token</th><th>缓存命中率</th>" +
        "<th>费用</th>" +
        "<th>高峰价</th><th>周末谷价</th>" +
        // v4.85（用户 m10425 第 3 条）：高峰时段按哪个时区算，只放在表头的悬浮提示里。
        "<th" + (pricingTimezone ? " title='高峰时段按服务端本地时间（" + esc(pricingTimezone) + "）算，含起不含止，可跨午夜。'" : "") +
          ">高峰时段</th></tr></thead><tbody>" + rows + "</tbody></table>" +
      // 用户 m23331：底部那两行小字都撤了——「另有 N 个模型没调用」挪到顶部总花费右边，
      // 高峰时段按哪个时区算收进表头「高峰时段」的悬浮提示里。
      // 用户 m19357：表格下面一行两张图（柱 + 折线 / 饼图），内容由 paintPricingTrend 填。
      "<div class='pcharts' id='p-charts'></div>" +
      "<div class='padd'><span class='phint' id='p-msg'></span>" +
        "<span class='pstat' id='p-stat'></span></div>";
    bindPricing();
    paintPricingTrend();
    restorePricingFocus(box, focus);
    if(Object.keys(pricingDraft).length) setPMsg(pricingPendingMessage(pricingPartialSaved));
  }

  // 用户 m10749 第 3 条：删除按钮的「确认」态——第一次点只变成「确认」，3 秒不点第二次自动复位。
  function bindPricing(){
    var box = document.getElementById("pricing-box");
    if(!box) return;
    // 用户 m10749 第 3 条：区间胶囊——点了就换 hours 重新拉一次价目
    var range = document.getElementById("p-range");
    if(range) range.onclick = function(ev){
      var b = (ev.target && ev.target.closest) ? ev.target.closest("[data-phours]") : null;
      if(!b) return;
      var h = Number(b.getAttribute("data-phours")) || 24;
      if(h === Number(pricingHours)) return;
      pricingHours = h;
      loadPricing();
    };
    // v3.44（用户 m04471 第 2 条）：价目行的删除按钮
    // 用户 m10749 第 3 条：第一次点只变「确认」，第二次点才真删。
    box.onclick = function(ev){
      var btn = (ev.target && ev.target.closest) ? ev.target.closest("[data-pdel]") : null;
      if(!btn) return;
      var name = btn.getAttribute("data-pdel") || "";
      if(!name) return;
      if(btn.getAttribute("data-armed") !== "1"){ armPricingDelete(btn); return; }
      if(pdelTimer){ clearTimeout(pdelTimer); pdelTimer = 0; }
      pricingRemoved[name] = true;
      pricingWriteVersion++;
      pricingCache = (pricingCache || []).filter(function(r){ return r.model !== name; });
      try{ delete pricingDraft[name]; }catch(e){}
      paintPricing();
      setPMsg("已删除 " + name);
      // 只删前端不够：traces 里这个模型的 token 还在，下次刷新就被算回来，
      // 所以同步让后端记住（Pricing.hide → pricing.json 的 hidden）。
      post(API + "/pricing/delete?values=" + encodeURIComponent(JSON.stringify({ model: name })))
        .then(function(d){
          if(!d || !d.ok){ setPMsg((d && d.detail) || "删除没落到后端，刷新后可能又出现", "bad"); }
        })
        .catch(function(e){ setPMsg("删除没落到后端：" + String((e && e.message) || e), "bad"); });
    };
    function changed(ev){
      var target=ev.target;
      var tr=target && target.closest ? target.closest("tr[data-pmodel]") : null;
      var field=target && target.getAttribute ? target.getAttribute("data-pfield") : "";
      if(!tr || !field) return;
      var name=tr.getAttribute("data-pmodel");
      var d=pricingDraft[name] || (pricingDraft[name]={});
      var value=target.type === "checkbox" ? !!target.checked : target.value;
      // 用户 m00153：分钟只有 00 / 30；时间框选完（change）才夹一次，选的过程中不动它的值。
      if(target.type === "time" && ev.type === "change"){
        var snapped=snapClock(target.value);
        if(snapped !== target.value) target.value=snapped;
      }
      if(field.indexOf("peak.") === 0){
        var key=field.slice(5);
        if(["in","out","cache","enabled","start","end","weekends"].indexOf(key) < 0) return;
        var peak=d.peak || (d.peak={});
        if(key === "start" || key === "end"){
          peak.periods=pricingPeriodsFromRow(tr);
          peak.start=peak.periods.length ? peak.periods[0].start : "";
          peak.end=peak.periods.length ? peak.periods[0].end : "";
        }else peak[key]=key === "weekends" ? !value : value;
      }else if(["in","out","cache","provider"].indexOf(field) >= 0) d[field]=value;
      else return;
      // 用户 m00153：时间框在原生选择器里每点一下都会发 input，只记草稿；等 change（选完）再保存。
      if(target.type === "time" && ev.type === "input") return;
      schedulePricingSave();
    }
    box.oninput=changed;
    box.onchange=changed;
    // 用户 m19975：图表按日历切——七天 / 本月 / 上月，默认本月。
    // 绑在 #p-charts 上而不是按钮本身：paintPricingTrend 会重画里面的按钮，绑按钮上会丢。
    // 用户 m07372：饼卡的弹层画在饼卡自己身上，paintPricingTrend 只重画柱图那张卡，
    // 所以饼按钮点了 open 变了页面不动。日历和弹层可能归柱图也可能归某张饼，两边都重画。
    function paintPricingCalOwner(){
      paintPricingTrend();
      paintPricingPies();
    }
    var tcharts = document.getElementById("p-charts");
    if(tcharts) tcharts.onclick = function(ev){
      var t = ev.target;
      if(!t || !t.closest) return;
      var b = t.closest("[data-prange]");
      if(b){
        var k = b.getAttribute("data-prange") || "default";
        pricingCal.open = false;
        if(k === pricingTrendMode && pricingTrend){ paintPricingTrend(); return; }
        pricingWinEnd = "";
        loadPricingTrend(k, true);
        return;
      }
      // 用户 m29947：右上角只剩一个按钮，点开是快捷区间 + 左右两个日历。
      var trBtn = t.closest("[data-trange]");
      if(trBtn){
        var wasOpen = pricingCal.open;
        pricingCal.open = !wasOpen;
        if(!wasOpen) seedPricingCal("trend", pricingCustom.start, pricingCustom.end);
        paintPricingCalOwner();
        return;
      }
      var calNav = t.closest("[data-cal-nav]");
      if(calNav){
        var ym0 = pricingCal.ym || pdFmt(pdToday()).slice(0, 7);
        var nd = new Date(Number(ym0.slice(0, 4)), Number(ym0.slice(5, 7)) - 1 + Number(calNav.getAttribute("data-cal-nav")), 1);
        pricingCal.ym = nd.getFullYear() + "-" + ("0" + (nd.getMonth() + 1)).slice(-2);
        paintPricingCalOwner();
        return;
      }
      var calD = t.closest("[data-cal-d]");
      if(calD){
        var dv = calD.getAttribute("data-cal-d");
        if(!pricingCal.start || pricingCal.end || dv < pricingCal.start){
          pricingCal.start = dv; pricingCal.end = "";
        }else{
          pricingCal.end = dv;
        }
        paintPricingCalOwner();
        return;
      }
      var calOk = t.closest("[data-cal-apply]");
      if(calOk){
        if(!pricingCal.start) return;
        var tgt = pricingCal.target || "trend";
        var s0 = pricingCal.start, e0 = pricingCal.end || pricingCal.start;
        if(tgt === "trend"){
          pricingCustom.start = s0;
          pricingCustom.end = e0;
          pricingCal.open = false;
          pricingWinEnd = "";
          loadPricingTrend("custom", true);
        }else{
          var pie4 = pricingPieById(tgt);
          if(!pie4) return;
          pie4.start = s0; pie4.end = e0; pie4.open = false;
          loadPricingPie(pie4.id, true);
        }
        return;
      }
      // 用户 m20864 图5：点饼心改成弹出居中详情窗口，不再把这张卡撑满一行挤开其它卡。
      var hit = t.closest(".ppie-hit");
      if(hit){
        var zcard = hit.closest(".pchart-pie");
        var zpie = zcard ? pricingPieById(zcard.getAttribute("data-pie")) : null;
        if(!zpie) return;
        openPricingPieModal(zpie.id);
        return;
      }
      var qb = t.closest("[data-q]");
      if(qb){
        var pie = pricingPieById(qb.getAttribute("data-pie"));
        if(!pie) return;
        var q = pricingQuickRange(qb.getAttribute("data-q"));
        pie.start = q.start; pie.end = q.end; pie.open = false;
        pricingCal.open = false;
        loadPricingPie(pie.id, true);
        return;
      }
      var tb = t.closest(".pdp-btn");
      if(tb){
        var pie3 = pricingPieById(tb.getAttribute("data-pie"));
        if(!pie3) return;
        var was = pie3.open;
        pricingPies.forEach(function(p){ p.open = false; });
        pricingCal.open = false;
        pie3.open = !was;
        if(pie3.open) seedPricingCal(pie3.id, pie3.start, pie3.end);
        paintPricingPiePopups();
        return;
      }
      // 点别处收起日历（点日历里面不算）
      if(!t.closest(".pdp") && (pricingPies.some(function(p){ return p.open; }) || pricingCal.open)){
        var hadCal = pricingCal.open;
        pricingPies.forEach(function(p){ p.open = false; });
        pricingCal.open = false;
        if(hadCal) paintPricingCalOwner(); else paintPricingPiePopups();
      }
    };
    // 用户 m23442：柱图上滚轮按天翻窗口（七天 / 十四天两档才翻）。
    // 用户 m29947：方向反过来（往下滚 = 往后一天）；只有指针落在左右两根轴之间才翻图，
    // 轴外面滚的是整页，不拦。
    // 用户 m09125：滚轮一下子拉满时，以前每个 wheel 事件立刻翻一天、立刻请求，
    // 视觉上直接闪到最左/最右。改成排队：滚几格排几格。
    // 用户 m17464：一次滚两格要一口气走完，中间不能停一下再走——所以攒格窗口
    // （PRICING_PAN_BATCH_MS）内的滚轮先排队，窗口到了再一次性消费整队；
    // 用户 m18000：一批走完要等这一批的滑动动画真正结束再放下一批，不然下一批会在半路打断上一批。
    var PRICING_PAN_STEP_MS = 0, PRICING_WHEEL_UNIT = 100;
    // 用户 m20864 图5：一次滚轮只走一天；连滚几格排进队列，每格一段等速动画首尾相接。
    var PRICING_PAN_BATCH_MS = 12, PRICING_PAN_MAX_STEPS = 1, PRICING_PAN_QUEUE_MAX = 6;
    var pricingPanBatchTimer = 0;
    // 用户 m09563：窗口已经到头（默认窗口的右端就是今天）时，滚轮再往下翻不动；
    // 以前照样 preventDefault，页面不滚、图也不翻，手感像卡住再闪一下。到头就放行，让整页照常滚。
    function pricingPanClamp(end, dir){
      // 用户 m15463：一屏画几天由实际数据决定（lib-3.js 的 pricingWinSpan），
      // 按取数窗口算会以为能放 28 天，往回翻一格就到底了。
      var today = pdToday(), days = pricingWinSpan();
      var minEnd = pdAdd(new Date(today.getFullYear(), today.getMonth(), 1), days - 1);
      if(minEnd > today) minEnd = today;
      var next = pdAdd(end, dir);
      if(next > today) next = today;
      if(next < minEnd) next = minEnd;
      return next;
    }
    // 队列里还没走完的格子也算上：排到一半就到头了，剩下的滚轮该交给整页。
    function pricingPanAvail(dir){
      var r = pricingWinRange();
      var end = pdParse(r.end) || pdToday();
      var queued = pricingPanClamp(end, pricingPanQueue || 0);
      var today = pdToday(), days = pricingWinSpan();
      var minEnd = pdAdd(new Date(today.getFullYear(), today.getMonth(), 1), days - 1);
      if(minEnd > today) minEnd = today;
      var left = dir > 0 ? pdDays(today, queued) : pdDays(queued, minEnd);
      return left > 0 ? left : 0;
    }
    function pricingPanStep(){
      if(pricingPanBusy || !pricingPanQueue) return;
      if(!pricingWinScrollable()){ pricingPanQueue = 0; return; }
      var dir = pricingPanQueue > 0 ? 1 : -1;
      var r = pricingWinRange();
      var end = pdParse(r.end);
      // 用户 m17464：队列里攒了几格就一次走几格（上限 PRICING_PAN_MAX_STEPS，
      // 连滚十几格也不会整张图横扫过去），中间不再一格一格停。
      var want = Math.min(Math.abs(pricingPanQueue), PRICING_PAN_MAX_STEPS);
      var next = end, moved = 0;
      while(moved < want){
        var cand = pricingPanClamp(next, dir);
        if(pdDays(cand, next) === 0) break;
        next = cand;
        moved += 1;
      }
      if(!moved){ pricingPanQueue = 0; return; }
      pricingPanQueue -= dir * moved;
      // 用户 m30335：连着滚几格就记几格，平移距离按格数算。
      pricingPanDays += dir * moved;
      pricingAnimDir = dir;
      pricingWinEnd = pdFmt(next);
      // 用户 m05422：以前是 45ms 防抖——连着滚的时候一次都不重画，停手才跳一格，
      // 看着像卡住再闪过去。改成每格立刻重画：上一帧滑到哪儿由 pricingPanFrom 接着算，
      // 请求带序号，晚回来的旧响应会被丢掉。
      clearTimeout(pricingWinTimer);
      pricingWinTimer = 0;
      pricingPanBusy = true;
      loadPricingTrend(pricingTrendMode, true).then(function(){
        // 用户 m18000：等这一批的滑动动画走完再放开（pricingPanDurMs 是这一帧算出来的时长）。
        var wait = Math.max(PRICING_PAN_STEP_MS, (pricingPanDurMs || 0) + 8);
        setTimeout(function(){ pricingPanBusy = false; pricingPanStep(); }, wait);
      });
    }
    if(tcharts) tcharts.onwheel = function(ev){
      if(ev.target && ev.target.closest && ev.target.closest(".pchart-pie")) return;
      var svg = ev.target && ev.target.closest ? ev.target.closest(".pchart-svg") : null;
      if(!svg || !pricingGeom) return;
      var rc = svg.getBoundingClientRect();
      if(!rc.width) return;
      var sc = rc.width / pricingGeom.W;
      // 用户 m09686：可触发滚动的范围收窄到图表绘图区——顶部标题行不算，左右各让出半个柱位，
      // 免得鼠标只是从卡片边缘划过就把整页的滚动吃掉。
      var half = pricingGeom.slot * 1 * sc;
      var x0 = rc.left + pricingGeom.padL * sc + half, x1 = rc.left + (pricingGeom.W - pricingGeom.padR) * sc - half;
      var y0 = rc.top + (pricingGeom.padT + 12) * sc, y1 = rc.bottom;
      if(ev.clientX < x0 || ev.clientX > x1 || ev.clientY < y0 || ev.clientY > y1) return;
      var dy = Number(ev.deltaY) || 0;
      if(!dy) return;
      // 用户 m20864 图5：指针落在图里就吃掉滚轮——翻到头也不放行整页。
      // 以前到头或固定区间就把事件让给页面，快滚时整页会突然跟着动。
      ev.preventDefault();
      if(!pricingWinScrollable()){
        // 固定区间（本周 / 上周 / 本月…）不翻窗口，滚轮在图上等于无效，但也不许带动整页。
        pricingWheelAcc = 0;
        return;
      }
      // 一格 = 100px：鼠标滚轮一格约 100；触控板是小步长，攒够一格才算一天。
      var unit = ev.deltaMode === 1 ? 16 : (ev.deltaMode === 2 ? 400 : 1);
      var px = dy * unit;
      var dir = px > 0 ? 1 : -1;
      var avail = pricingPanAvail(dir);
      if(!avail){ pricingWheelAcc = 0; return; }
      if(pricingWheelAcc && (pricingWheelAcc > 0 ? 1 : -1) !== dir) pricingWheelAcc = 0;
      pricingWheelAcc += px;
      var n = Math.floor(Math.abs(pricingWheelAcc) / PRICING_WHEEL_UNIT);
      if(!n) return;
      // 只剩这么多天：多出来的格子不排队，免得排到一半到头、页面又不动。
      if(n > avail){ n = avail; pricingWheelAcc = 0; }
      else pricingWheelAcc -= dir * n * PRICING_WHEEL_UNIT;
      // 用户 m20864 图5：一次只走一天，连滚几格就排几格——每格一段等速动画首尾相接，不再一格停一下。
      pricingPanQueue += dir * n;
      if(pricingPanQueue > PRICING_PAN_QUEUE_MAX) pricingPanQueue = PRICING_PAN_QUEUE_MAX;
      if(pricingPanQueue < -PRICING_PAN_QUEUE_MAX) pricingPanQueue = -PRICING_PAN_QUEUE_MAX;
      if(pricingPanBusy) return;
      clearTimeout(pricingPanBatchTimer);
      pricingPanBatchTimer = setTimeout(function(){ pricingPanBatchTimer = 0; pricingPanStep(); }, PRICING_PAN_BATCH_MS);
    };
  }


  function savePricing(){
    if(pricingTimer){ clearTimeout(pricingTimer); pricingTimer=0; }
    if(pricingSaving){ pricingSaveQueued=true; return; }
    var models=collectPricing();
    var error=validatePricing(models);
    if(error){ setPMsg(error,"bad"); return; }
    var names=Object.keys(models), deferredPeak=Object.create(null), blankOrdinary=Object.create(null);
    Array.prototype.forEach.call(document.querySelectorAll("#pricing-box tr[data-pmodel]"), function(tr){
      blankOrdinary[tr.getAttribute("data-pmodel")]=["in","out","cache"].every(function(k){
        var el=tr.querySelector("[data-pfield='" + k + "']");
        return !String(el && el.value || "").trim();
      });
    });
    names.forEach(function(m){
      if(!pricingPeakIncomplete(models[m].peak)) return;
      deferredPeak[m]=true;
      var old=(pricingCache || []).filter(function(r){ return r.model === m; })[0];
      // Stage incomplete intervals locally, never disable them or overwrite a legal saved peak.
      if(old && old.peak && !pricingPeakIncomplete(pricingPeak(old.peak))) models[m].peak=pricingPeak(old.peak);
      else{
        delete models[m].peak;
        // A peak-only draft must not turn an entirely unpriced row into three explicit free prices.
        if(blankOrdinary[m] && !models[m].provider && (!old || (old.in == null && old.out == null && old.cache == null))) delete models[m];
      }
    });
    names=Object.keys(models);
    var sentDraft=JSON.parse(JSON.stringify(pricingDraft));
    pricingSaving=true;
    setPMsg("保存中…");
    post(API + "/pricing?values=" + encodeURIComponent(JSON.stringify({models:models}))).then(function(d){
      if(!d || !d.ok){ setPMsg((d && d.detail) || "保存失败","bad"); return; }
      var saved=d.models || {};
      pricingWriteVersion++;
      (pricingCache || []).forEach(function(r){
        var s=saved[r.model];
        r.in=s ? s.in : null; r.out=s ? s.out : null; r.cache=s ? s.cache : null;
        r.peak=s && s.peak ? pricingPeak(s.peak) : null;
        r.provider=s ? String(s.provider || "") : "";
      });
      Object.keys(sentDraft).forEach(function(m){
        var current=pricingDraft[m], sent=sentDraft[m];
        if(!current || !models[m]) return;
        ["in","out","cache","provider"].forEach(function(k){ if(current[k] === sent[k]) delete current[k]; });
        if(!deferredPeak[m] && JSON.stringify(current.peak) === JSON.stringify(sent.peak)) delete current.peak;
        if(!Object.keys(current).length) delete pricingDraft[m];
      });
      // 用户 m00153：保存后只静默换掉花费与统计，不重画整张表——重绘会把正在点选的时间框打断。
      pricingPartialSaved=true;
      if(Object.keys(pricingDraft).length) setPMsg(pricingPendingMessage(true));
      else setPMsg(names.length ? "已保存 " + names.length + " 个模型的价目" : "已保存（表里还没有价格）","ok");
      loadPricing(true);
    }).catch(function(e){
      setPMsg("保存失败：" + String((e && e.message) || e),"bad");
    }).then(function(){
      pricingSaving=false;
      if(pricingSaveQueued){ pricingSaveQueued=false; savePricing(); }
    });
  }

  // v3.71（用户第 7 条）：「高级模式 · 动态调整」不再单独占一列，这几项平铺在曲线说明下面一行。
  // 只在曲线开着时可用（动态调整必须建立在曲线上）；私聊作用范围下整组不画。
  // v3.72（用户第 6 / 8 / 9 条）：删掉「最少天数」；「统计回看」跟着 schema 改叫「数据使用」；
  // 「按星期分桶」与「分桶回看」并成一行「使用星期数据 [8] 周 [开关]」（键改 adaptive_weekday_window_weeks）。
  function renderAdaptiveRow(){
    var box = document.getElementById("bd-adaptive");
    if(!box) return;
    box.innerHTML = adaptiveItemsHtml();
    var sw = box.querySelector("input[data-adtrg]");
    if(sw){
      sw.onchange = function(){
        trgSet(this.checked ? "adaptive" : (lastNormal || "count"));
        // 段上的数字跟着换口径：动态模式显示「目前概率」，普通模式显示配置值
        refreshBandBox();
      };
    }
    // v3.72（用户第 7 / 9 条）：这两个框填小了没意义——数据使用最少 7 天、回看 4-52 周。
    // change / 失焦时夹回范围内，框里也改成夹过的数；change 在 #settings 那个委派保存之前跑，
    // 所以存下去的也就是夹过的值。
    Array.prototype.forEach.call(box.querySelectorAll("input[type=number][data-k]"), function(inp){
      var k = inp.getAttribute("data-k");
      var lim = ADAPTIVE_LIM[k];
      if(!lim) return;
      function fix(){
        var n = parseInt(String(inp.value || "").replace(/[^0-9]/g, ""), 10);
        if(!isFinite(n)) n = lim[0];
        if(n < lim[0]) n = lim[0];
        if(n > lim[1]) n = lim[1];
        if(String(n) !== String(inp.value)) inp.value = n;
      }
      inp.addEventListener("change", fix);
      inp.addEventListener("blur", fix);
    });
    // v3.72（用户第 9 条）：「使用星期数据」关着时，周数框压暗；点开关立刻跟着变
    var wkSw = box.querySelector("input[data-k='adaptive_weekday_bucket']");
    var wkBox = box.querySelector("[data-adwk]");
    if(wkSw && wkBox){
      wkSw.onchange = function(){
        wkBox.classList.toggle("dimv", !this.checked);
        // v3.83（用户 m03849 图4）：这个开关原来只改压暗，configCache 要等保存回来才更新——
        // 刚拨完立刻点段上的概率，面板还是旧形态（该有按星期那七行的时候没有），要等一会儿才好。
        // 现在当场把值写进 configCache、把旧统计丢掉重取，面板一定按新状态画。
        if(configCache) configCache["adaptive_weekday_bucket"] = !!this.checked;
        bandStatsData = null;
        closeBandPop();
        loadBandStats();
      };
    }
    paintAdaptiveRow();
  }
  // v3.71（用户第 6 条）：时段统计（GET /band-stats）——段上的「目前概率」与动态模式面板都读它。
  // 接口没就绪 / 报错就退回只显示配置值，面板统计写「暂无数据」。
  function bandCommit(){
    if(!bandBands) return;
    var txt = JSON.stringify(bandBands);
    var el = document.getElementById("bands-val");
    if(el) el.value = txt;
    if(configCache) configCache.time_bands = txt;
    dirty = true;
    scheduleSave();
  }
  // v4.90（用户 m03650 第 2 条）：窄屏上相邻两段的概率胶囊会叠在一起（390px 下
  // 图只有 319px，两个窄段的中心只差 19px，胶囊却有 38px 宽），后一个把前一个的
  // 数字盖住。渲染完按实际位置错开：跟前面任何一个胶囊横向重叠就往下挪一行，
  // 头顶的垃圾桶跟着一起挪，保持「垃圾桶在胶囊正上方」的关系。
  function openBandPop(idx, host){
    if(!bandBands || !bandBands[idx]) return;
    closeBandPop();
    var b = bandBands[idx];
    // v3.81（用户 m02645 图3）：两件事——① 休息时段不走「动态调整」那张面板，它只有一件事：
    // 改休息概率（默认 2%，显示的也得是 2%，不是段上的 10%）；② 开了「使用星期数据」后，
    // 目标值要按 7 天分别设（面板变宽一行一天）。
    // v3.83（用户 m03849 图4）：以页面上那个开关的当前状态为准。configCache 要等保存
    // 回来才更新，刚拨完开关就点段会拿到旧值，面板形态就慢半拍。
    var wkSwNow = document.querySelector("#bd-adaptive input[data-k='adaptive_weekday_bucket']");
    var wkOn = wkSwNow ? !!wkSwNow.checked : !!(configCache && configCache["adaptive_weekday_bucket"]);
    var dyn = (trgVal() === "adaptive") && bandsActive() && !b.rest;
    var stat = bandStatOf(b);
    var pop = document.createElement("div");
    pop.className = "bd-pop" + (dyn ? (wkOn ? " wide wk" : " wide") : " one");
    pop.id = "bd-pop";
    // 用户 m03323：wk 面板里还带着每一天的数据，统计没到时先用「—」占位，
    // 等 loadBandStats 回来由 refreshWeekCells 只回填数据格（不重画面板）。
    if(dyn && wkOn){
      pop.setAttribute("data-wk", "1");
      pop.setAttribute("data-wk-idx", String(idx));
    }
    // v3.73（用户 m24640 第 5/7 条）：这一段标了月亮就是休息时段，面板里多一行「休息概率」
    var restRow = b.rest
      ? ("<div class='bp-row'><span>休息时段回复概率</span>" +
         "<span class='bp-ri'><input type='text' inputmode='numeric' autocomplete='off' maxlength='3'" +
         " id='bp-rp' value='" + bandRestProb() + "'><b>%</b></span></div>")
      : "";
    var h = "";
    if(!dyn){
      if(b.rest){
        // v3.81（用户 m02645 图3 第三点）：休息时段只有「休息概率」一件事——段上的概率
        // 对它不起作用，所以这里根本不显示 bp-v，显示的 2% 就是 bandRestProb 的值。
        h = "<button type='button' class='bp-x' data-bp='cancel' title='关闭'>×</button>" +
            "<div class='bp-t'>" + slotTime(b.s) + "–" + slotTime(b.e) +
            " <i class='bp-moon' title='这段时间被标成休息时段'>☾ 休息时段</i></div>" +
            // 用户 m03291：标签改「休息时段回复概率」，下面那排描述不要了（改完自动保存是隐藏行为，不写也照样存）
            restRow;
      } else {
        // v3.72（用户第 1 条）：只有一件事——改这一段的概率。一个居中的数字框（type=text +
        // inputmode=numeric，没有上下箭头）、只收 0-100 的整数；没有「保存 / 取消」，
        // 改完 300ms 自动落库；右上角一个 × 收起。
        h = "<button type='button' class='bp-x' data-bp='cancel' title='关闭'>×</button>" +
            "<div class='bp-t'>" + slotTime(b.s) + "–" + slotTime(b.e) + "</div>" +
            "<div class='bp-one'><input type='text' inputmode='numeric' autocomplete='off' " +
            "maxlength='3' id='bp-v' value='" + b.v + "'><b>%</b></div>" +
            "<div class='bp-note'>改完自动保存</div>";
      }
    } else {
      var goal;
      if(wkOn){
        // v3.81（用户 m02645 图3 第一点）：开了「使用星期数据」就一天一行——每个星期的数据
        // 都不一样，统一设一个目标等于白设。空白 = 这天不设（后端回落到统一的 tr / tp）。
        // 用户 m03323：既然一天一行，上面那四行「每天平均」也不能再统一写一份——
        // 每行左边写这一天在这个时段的平均（消息 · 触发 · 回复 · 回复率），右边才是这一天的目标。
        var todayIdx = (new Date().getDay() + 6) % 7;
        var trs = b.trw || [], tps = b.tpw || [];
        var wkr = bandWeekRows(b);
        // v3.83（用户 m03849 图5）：右边两列光有列名（还藏在 title 里）小白看不懂，
        // 面板里直接写一行说明：填哪个、另一个怎么来的、留空算什么。
        goal = "<div class='bp-goal bp-goal-wk'>" +
               // v3.84（用户 m04192）：原来这段是一整句自动折行，会从「留空」中间断开。
               // 现在按「怎么填 / 留空算什么」拆成两行，各自都短到不会二次折行。
               "<span class='bp-wnote'>" +
                 "<span class='bp-wn1'>右边两列填一边就行：<b>次数/天</b>＝这天想回几次，" +
                 "<b>概率/条</b>＝每条消息回的概率。</span>" +
                 "<span class='bp-wn1'>留空＝沿用这一段原来的目标；左边<b>这天平均</b>是这一天这个时段的实际数据。</span>" +
               "</span>" +
               "<span class='bp-gl bp-gl-l'>星期</span>" +
               "<span class='bp-gl bp-gl-l' title='这一天在这个时段的平均：消息 · 触发 · 回复 · 回复率'>这天平均</span>" +
               "<span class='bp-gl bp-gl2'>次数/天</span>" +
               "<span class='bp-gl bp-gl2'>概率/条</span>";
        for(var wi = 0; wi < 7; wi++){
          var tv = trs[wi] > 0 ? trs[wi] : (b.tr ? b.tr : "");
          var pv = tps[wi] > 0 ? tps[wi] : (b.tp ? b.tp : "");
          var winfo = wkr ? wkr[wi] : null;
          goal += "<span class='bp-wd" + (wi === todayIdx ? " now" : "") + "'>" + WEEK_LABELS[wi] + "</span>" +
                  "<span class='bp-wdata" + (weekCellEmpty(winfo) ? " none" : "") + "' data-wdat='" + wi + "'>" +
                  esc(weekCellText(winfo)) + "</span>" +
                  "<input type='text' inputmode='numeric' data-trd='" + wi + "' value='" + tv + "' placeholder='-'" +
                  " title='这天想回几次（填了会反算概率）'>" +
                  "<input type='text' inputmode='numeric' data-tpd='" + wi + "' value='" + pv + "' placeholder='-'" +
                  " title='这天每条消息回的概率 %（填了会反算次数）'>";
        }
        goal += "</div>";
      } else {
        goal = "<div class='bp-goal'>" +
               "<span class='bp-gl'>目标回复次数<b>每天</b></span>" +
               "<input type='text' inputmode='numeric' id='bp-tr' value='" + (b.tr ? b.tr : "") + "' placeholder='不设'>" +
               "<span class='bp-gl'>目标回复概率<b>每条消息</b></span>" +
               "<input type='text' inputmode='numeric' id='bp-tp' value='" + (b.tp ? b.tp : "") + "' placeholder='不设'>" +
               "</div>";
      }
      // 用户 m03323：开了星期数据时，这四个「每天平均」已经分散到每一天那一行了，上面不再重复一遍
      var kvHtml = wkOn ? "" : ("<div class='bp-kv'>" +
          "<i>每天平均消息</i><b>" + bpNum(stat && stat.m_day, " 条") + "</b>" +
          "<i>每天触发</i><b>" + bpNum(stat && stat.t_day, " 次") + "</b>" +
          "<i>每天回复</i><b>" + bpNum(stat && stat.r_day, " 次") + "</b>" +
          "<i>平均回复概率</i><b>" + bpNum(stat && stat.reply_rate, "%") + "</b>" +
          "</div>");
      h = "<div class='bp-t'>" + slotTime(b.s) + "–" + slotTime(b.e) + "</div>" +
          kvHtml +
          goal +
          "<div class='bp-note'>目前概率 " + (stat ? (bandShownP(b) + "%") : "暂无数据") +
          "（配置 " + b.v + "%）" +
          // v3.84（用户 m04084 第 2 条）：这一段概率被手工改过，自动调整就停手了——
          // 不说明的话会以为调参坏了（概率一直不动）。
          (stat && stat.manual ? " · 手动接管中，自动调整已暂停" : "") +
          (wkOn ? " · 目标按星期分开设" : "") + "</div>" +
          "<div class='bp-btns'><button type='button' data-bp='cancel'>取消</button>" +
          "<button type='button' class='pri' data-bp='ok'>保存</button></div>";
    }
    pop.innerHTML = h;
    // v3.73（用户 m24640 第 7 条）：改成屏幕正中弹出——原来贴着胶囊下方，
    // 段落在曲线底部时卡片会掉到屏幕很下面；加一层遮罩，点外面就收起。
    var mask = document.createElement("div");
    mask.className = "bd-mask";
    mask.id = "bd-mask";
    document.body.appendChild(mask);
    document.body.appendChild(pop);
    if(dyn && wkOn && !(bandStatsData && bandStatsData.week && bandStatsData.week.length)) loadBandStats();
    // v3.72（用户第 1 条）：普通模式改成「改完自动保存」——输入后 300ms 落库，
    // 失焦 / 回车立刻落库；只收 0-100 的整数，框里也回写成夹过的数。
    var autoT = null;
    function stopAuto(){ if(autoT){ clearTimeout(autoT); autoT = null; } }
    function readPopV(){
      var el = pop.querySelector("#bp-v");
      if(!el) return b.v;
      var n = parseInt(String(el.value || "").replace(/[^0-9]/g, ""), 10);
      if(!isFinite(n)) return b.v;
      return Math.max(0, Math.min(100, n));
    }
    function applyPopV(){
      var nv = readPopV();
      var el = pop.querySelector("#bp-v");
      if(el && String(nv) !== String(el.value)) el.value = nv;
      if(nv === b.v) return;
      b.v = nv;
      bandCommit();
      paintBandChart();
    }
    // v3.73（用户 m24640 第 5 条）：休息概率也走「改完自动保存」
    function readRestP(){
      var el = pop.querySelector("#bp-rp");
      if(!el) return null;
      var n = parseInt(String(el.value || "").replace(/[^0-9]/g, ""), 10);
      if(!isFinite(n)) return null;
      return Math.max(0, Math.min(100, n));
    }
    function applyRestP(){
      var nv = readRestP();
      if(nv === null) return;
      var el = pop.querySelector("#bp-rp");
      if(el && String(nv) !== String(el.value)) el.value = nv;
      if(configCache) configCache["band_rest_probability"] = nv;
      dirty = true;
      scheduleSave();
    }
    var ri = pop.querySelector("#bp-rp");
    if(ri){
      ri.addEventListener("input", function(){
        var s = String(this.value || "").replace(/[^0-9]/g, "").slice(0, 3);
        if(s !== this.value) this.value = s;
        stopAuto();
        autoT = setTimeout(function(){ autoT = null; applyRestP(); }, 300);
      });
      ri.addEventListener("blur", function(){ stopAuto(); applyRestP(); });
      ri.addEventListener("keydown", function(e){
        if(e.key === "Enter"){ e.preventDefault(); stopAuto(); applyRestP(); closeBandPop(); }
      });
    }
    if(!dyn){
      var vi = pop.querySelector("#bp-v");
      if(vi){
        vi.addEventListener("input", function(){
          var s = String(this.value || "").replace(/[^0-9]/g, "").slice(0, 3);
          if(s !== this.value) this.value = s;
          stopAuto();
          autoT = setTimeout(function(){ autoT = null; applyPopV(); }, 300);
        });
        vi.addEventListener("blur", function(){ stopAuto(); applyPopV(); });
        vi.addEventListener("keydown", function(e){
          if(e.key === "Enter"){ e.preventDefault(); stopAuto(); applyPopV(); closeBandPop(); }
        });
      }
    }
    function commit(ok){
      stopAuto();
      applyRestP();
      if(dyn){
        if(ok){
          if(wkOn){
            // v3.81（用户 m02645 图3）：开了星期数据就按 7 天各存一份（trw / tpw）。
            // 哪天留空 = 那天不设（写 0），后端 _band_day_target 读到 0 会回落到统一的 tr / tp。
            var trw = [], tpw = [], i, n;
            for(i = 0; i < 7; i++){
              n = Math.round(Number((pop.querySelector("input[data-trd='" + i + "']") || {}).value));
              trw.push(isFinite(n) && n > 0 ? Math.min(9999, n) : 0);
              n = Math.round(Number((pop.querySelector("input[data-tpd='" + i + "']") || {}).value));
              tpw.push(isFinite(n) && n > 0 ? Math.max(1, Math.min(100, n)) : 0);
            }
            if(trw.some(function(v){ return v > 0; })) b.trw = trw; else delete b.trw;
            if(tpw.some(function(v){ return v > 0; })) b.tpw = tpw; else delete b.tpw;
          } else {
            // 目标回复次数 / 概率写进段对象的 tr / tp（0 或留空 = 不设）
            var tr = Math.round(Number(pop.querySelector("#bp-tr").value));
            var tp = Math.round(Number(pop.querySelector("#bp-tp").value));
            if(isFinite(tr) && tr > 0) b.tr = Math.min(9999, tr); else delete b.tr;
            if(isFinite(tp) && tp > 0) b.tp = Math.max(1, Math.min(100, tp)); else delete b.tp;
          }
          bandCommit();
          paintBandChart();
        }
      } else {
        // 没有保存按钮：关卡片（点 × / 按 Esc）时把已经改好的值落库
        stopAuto();
        applyPopV();
      }
      closeBandPop();
    }
    pop.addEventListener("click", function(e){
      var t = e.target && e.target.closest ? e.target.closest("[data-bp]") : null;
      if(!t) return;
      commit(t.getAttribute("data-bp") === "ok");
    });
    pop.addEventListener("keydown", function(e){
      if(e.key === "Enter"){ e.preventDefault(); commit(true); }
      else if(e.key === "Escape"){ e.preventDefault(); commit(false); }
    });
    var first = pop.querySelector("input");
    if(first && first.focus){ first.focus(); if(first.select) first.select(); }
  }
  // 拖到图外也要停：pointerup 只在模块里注册一次，避免每次重建表单都叠一个监听
  document.addEventListener("pointerup", function(){ bandDrag = null; });
  // v3.71（用户第 2 条前半）：卡片点外面 / 按 Esc 就收起（同样只注册一次，别每次重建表单叠一个）
  document.addEventListener("pointerdown", function(e){
    var pop = document.getElementById("bd-pop");
    if(!pop) return;
    if(pop.contains && pop.contains(e.target)) return;
    closeBandPop();
  }, true);
  document.addEventListener("keydown", function(e){
    if(e.key === "Escape") closeBandPop();
  });
  function bindBandBox(){
    var chart = document.getElementById("bd-chart");
    var list = document.getElementById("bd-list");
    if(!chart || !list) return;
    bandBands = parseBands(bandsRaw());
    bandSel = 0;
    // v3.74（用户 m00024 第 4-e 条）：隐藏划分开关跟着这块图一起绑（切作用范围会整块重建）
    var hideSw = document.getElementById("bd-hide-seg");
    if(hideSw){
      hideSw.checked = !!bandHideSeg;
      hideSw.onchange = function(){ setBandHideSeg(this.checked); paintBandChart(); };
    }
    // v3.75（用户本轮第 2 条）：右上角「曲线」下拉——勾哪条画哪条（默认全勾），
    // 点按钮开合、点别处收起；勾选状态写本地，跟「隐藏划分」一样只是显示偏好。
    var curBtn = document.getElementById("bd-cur-btn");
    var curMenu = document.getElementById("bd-cur-menu");
    if(curBtn && curMenu){
      syncCurMenu();
      curBtn.onclick = function(ev){
        if(ev && ev.stopPropagation) ev.stopPropagation();
        curMenu.hidden = !curMenu.hidden;
      };
      curMenu.onclick = function(ev){ if(ev && ev.stopPropagation) ev.stopPropagation(); };
      Array.prototype.forEach.call(curMenu.querySelectorAll("input[data-cur]"), function(inp){
        inp.onchange = function(){
          setBandShow(this.getAttribute("data-cur"), this.checked);
          paintBandChart();
        };
      });
      document.onclick = function(){ if(curMenu) curMenu.hidden = true; };
    }
    // v3.75（用户本轮第 1 条）：鼠标停在图的哪一格，就在顶上报这一格的两条线读数
    // （7 天累计条数）。隐藏划分之后这条尤其重要——那时整张图只剩曲线。
    // 用户本轮第 2 条：气泡贴着指针走（position:fixed，不受 .bd-chart 的 overflow:hidden 裁剪，
    // 走到图边也不会被切掉一半）。右／下边不够就翻到指针另一侧。
    function placeTip(tip, cx, cy){
      var w = tip.offsetWidth || 120, h = tip.offsetHeight || 20;
      var lx = cx + 14, ly = cy + 16;
      if(lx + w > window.innerWidth - 6) lx = cx - w - 14;
      if(ly + h > window.innerHeight - 6) ly = cy - h - 12;
      tip.style.left = Math.max(4, lx) + "px";
      tip.style.top = Math.max(4, ly) + "px";
    }
    // 用户本轮第 1 条：开划分时读「这一段」的合计（段的时间范围 + 用户／bot 条数 + 这一段的目标概率），
    // 只有把划分藏起来、整张图只剩曲线时，才退回按 30 分钟一格的读数。
    function bandAtSlot(slot){
      if(!bandBands) return null;
      for(var i = 0; i < bandBands.length; i++){
        var b = bandBands[i];
        if(slot >= b.s && slot < b.e) return b;
      }
      return null;
    }
    function bandTipAt(clientX, clientY){
      var tip = document.getElementById("bd-tip");
      if(!tip) return;
      var rect = chart.getBoundingClientRect();
      if(!rect.width){ tip.hidden = true; return; }
      var x = Math.max(0, Math.min(rect.width - 0.01, clientX - rect.left));
      var slot = Math.min(BAND_SLOTS - 1, Math.floor(x / rect.width * BAND_SLOTS));
      var seg = bandHideSeg ? null : bandAtSlot(slot);
      var txt;
      if(seg){
        var su = 0, sb = 0;
        for(var k = seg.s; k < seg.e; k++){
          su += (bandSlots && bandSlots[k]) || 0;
          sb += (bandBotSlots && bandBotSlots[k]) || 0;
        }
        txt = "<b>" + slotTime(seg.s) + "–" + slotTime(seg.e) + "</b>";
        if(bandShow.u) txt += " · 用户 " + su + " 条";
        if(bandShow.b) txt += " · bot " + sb + " 条";
        // v4.90（用户 m01875 第 2 条）：休息段只按休息概率回，段概率对它完全不起作用，
        // 悬停还写「概率 10%」会让人以为点进去改的是段概率。
        txt += seg.rest ? (" · 休息概率 " + bandRestProb() + "%")
                        : (" · 概率 " + bandShownP(seg) + "%");
      } else {
        var u = (bandSlots && bandSlots[slot]) || 0;
        var b = (bandBotSlots && bandBotSlots[slot]) || 0;
        txt = "<b>" + slotTime(slot) + "–" + slotTime(slot + 1) + "</b>";
        if(bandShow.u) txt += " · 用户 " + u + " 条";
        if(bandShow.b) txt += " · bot " + b + " 条";
      }
      tip.innerHTML = txt;
      tip.hidden = false;
      placeTip(tip, clientX, clientY == null ? rect.top + 20 : clientY);
    }
    // v3.70（用户 m21533 第 4 条）：在指针位置切一刀（按 30 分钟格取整）。
    function cutBandAt(clientX){
      if(!bandBands) return false;
      var rect = chart.getBoundingClientRect();
      if(!rect.width) return false;
      var pos = Math.round((clientX - rect.left) / rect.width * BAND_SLOTS);
      pos = Math.max(1, Math.min(BAND_SLOTS - 1, pos));
      for(var i = 0; i < bandBands.length; i++){
        var b = bandBands[i];
        if(b.s < pos && pos < b.e){
          if(b.e - b.s < 2) return false;      // 已经只有一格，切不出两段
          bandBands.splice(i, 1, { s: b.s, e: pos, v: b.v }, { s: pos, e: b.e, v: b.v });
          bandSel = i + 1;
          bandCommit();
          paintBandChart();
          return true;
        }
      }
      return false;
    }
    // v3.71（用户第 2 条前半）：小刀只在指针贴到格线（±6px）时才出现——那时才画竖虚线、
    // 点下去才切分；指针在段中间时是普通箭头，点一下只选中这一段。格线像素位置 = 槽位 / 48 × 宽度。
    var BAND_EDGE_PX = 6;
    // v3.74（用户 m00024 第 4-a 条）：v3.72 那条「光标还得纵向落在概率横线上下 14 像素内」的限制撤掉——
    // 概率线常贴在图表顶部，用户在图表中下部贴格线根本触发不了，看着就是「小刀怎么点都不切」。
    // 现在只看横向离格线多远。
    function bandSlotAt(clientX){
      var rect = chart.getBoundingClientRect();
      if(!rect.width) return null;
      var x = clientX - rect.left;
      var px = rect.width / BAND_SLOTS;          // 一格（30 分钟）的像素宽
      var slot = Math.max(0, Math.min(BAND_SLOTS, Math.round(x / px)));
      return { slot: slot, dist: Math.abs(x - slot * px) };
    }
    function moveCut(clientX){
      var cut = document.getElementById("bd-cut");
      if(!cut) return;
      // 隐藏划分时没有段可切，也不该冒出小刀
      if(bandHideSeg){ cut.hidden = true; chart.classList.remove("near-edge"); return; }
      var hit = bandSlotAt(clientX);
      if(!hit){ chart.classList.remove("near-edge"); cut.hidden = true; return; }
      var near = (hit.dist <= BAND_EDGE_PX) && hit.slot > 0 && hit.slot < BAND_SLOTS;
      chart.classList.toggle("near-edge", near);
      if(!near){ cut.hidden = true; return; }
      cut.hidden = false;
      cut.style.left = (hit.slot / BAND_SLOTS * 100) + "%";
    }
    // v3.74（用户 m00024 第 4-a 条）：pointerdown 里原来会 paintBandChart() 选中这一段——重绘把
    // 刚按下的元素从 DOM 里换掉，浏览器就不再派发 click，于是「贴格线点一下切开」永远走不到。
    // 现在按下只记下选中哪一段，重绘留到 click 里做。
    // 另外原来 pointerdown 设了 bandDrag 却没人清：拖过一次分界之后 bandDrag 一直是数字，
    // 鼠标再动都走拖动分支，小刀再也不出现——这也是「小刀点了没反应」的一半原因。
    var bandJustDragged = false;
    chart.onpointerdown = function(e){
      if(e.target.closest(".bd-tag")) return;
      // v3.73（用户 m24640 第 5 条）：月亮是按钮，别让它顺手选中段或切分
      if(e.target.closest(".bd-moon")) return;
      // v4.90（用户 m01875 第 2 条）：垃圾桶是按钮，别让它顺手选中段
      if(e.target.closest(".bd-del")) return;
      bandJustDragged = false;
      var hd = e.target.closest("[data-edge]");
      if(hd){ bandDrag = Number(hd.getAttribute("data-edge")); e.preventDefault(); return; }
      var f = e.target.closest("[data-seg]");
      if(f) bandSel = Number(f.getAttribute("data-seg"));
    };
    // 松手：只有真的拖过分界才把这一次点击当成「拖动」，否则仍然是一次点击
    chart.onpointerup = function(){
      bandJustDragged = !!bandDrag;
      bandDrag = null;
    };
    chart.onpointercancel = function(){ bandDrag = null; bandJustDragged = false; };
    // v3.68（用户 m20632 第 8 条①）：拖动不再夹在相邻两根分界之间——拖过头就把中间那段整段并掉。
    // 想划出「23:00–09:00」这种大连段，靠的就是能把分界拖过已有的一根。
    chart.onpointermove = function(e){
      // v3.75（用户本轮第 1 条）：先更新顶部读数（跟拖动 / 小刀互不影响）
      bandTipAt(e.clientX, e.clientY);
      // 没在拖分界时：跟着指针画那条竖虚线（提示「在这儿切开」）
      if(!bandDrag){ moveCut(e.clientX); return; }
      if(!bandBands) return;
      var rect = chart.getBoundingClientRect();
      if(!rect.width) return;
      var pos = Math.round((e.clientX - rect.left) / rect.width * BAND_SLOTS);
      pos = Math.max(1, Math.min(BAND_SLOTS - 1, pos));
      var i = bandDrag;
      if(!bandBands[i - 1] || !bandBands[i]) return;
      // 往右拖越过下一根分界：被吞掉的那段并进前一段（保住前一段的概率）
      while(i < bandBands.length - 1 && pos >= bandBands[i].e){
        bandBands[i - 1].e = bandBands[i].e;
        bandBands.splice(i, 1);
        if(!bandBands[i] || !bandBands[i - 1]){ bandDrag = null; bandCommit(); paintBandChart(); return; }
      }
      // 往左拖越过上一根分界：前一段被吞进当前这段
      while(i > 1 && pos <= bandBands[i - 1].s){
        bandBands[i].s = bandBands[i - 1].s;
        bandBands.splice(i - 1, 1);
        i -= 1;
      }
      if(!bandBands[i - 1] || !bandBands[i]) return;
      if(bandBands[i - 1].e === pos && bandBands[i].s === pos) return;
      bandBands[i - 1].e = pos;
      bandBands[i].s = pos;
      bandDrag = i;
      bandCommit();
      paintBandChart();
    };
    chart.onclick = function(e){
      // 点概率胶囊＝弹卡片（改概率 / 看这一段的时段统计）；点段中间＝只选中这一段；
      // 点在格线上（小刀已经出现）＝在那儿切一刀。
      // v3.73（用户 m24640 第 5 条）：点月亮＝把这一段标成 / 取消休息时段
      var mn = e.target.closest(".bd-moon");
      if(mn){
        var mi = Number(mn.getAttribute("data-moon"));
        if(bandBands && bandBands[mi]){
          if(bandBands[mi].rest) delete bandBands[mi].rest;
          else bandBands[mi].rest = true;
          bandCommit();
          paintBandChart();
        }
        return;
      }
      // v4.90（用户 m01875 第 2 条）：点概率胶囊头顶的垃圾桶＝删掉这一段。
      // 并进前一段（保住前一段的概率）；删的是第一段就并进后一段。全天至少留一段。
      var dl = e.target.closest(".bd-del");
      if(dl){
        var di = Number(dl.getAttribute("data-del"));
        if(bandBands && bandBands.length > 1 && bandBands[di]){
          if(di > 0){
            bandBands[di - 1].e = bandBands[di].e;
            bandBands.splice(di, 1);
            bandSel = di - 1;
          }else{
            bandBands[1].s = bandBands[0].s;
            bandBands.splice(0, 1);
            bandSel = 0;
          }
          bandCommit();
          paintBandChart();
        }
        return;
      }
      var tg = e.target.closest(".bd-tag");
      if(tg){ openBandPop(Number(tg.getAttribute("data-seg")), tg); return; }
      // v3.74（用户 m00024 第 4-a 条）：先判「是不是贴住格线要切一刀」。这里不再要求
      // e.target.closest("[data-seg]")——格线位置常常被分界手柄 / 概率胶囊 / 段缝盖住，
      // 原来那句 if(!f) return 直接就把切分挡掉了（这就是「小刀显示着、点了一点反应都没有」）。
      // bandJustDragged：刚拖完分界的那一下不算切分。
      if(!bandJustDragged && !bandHideSeg){
        var hit = bandSlotAt(e.clientX);
        if(hit && hit.dist <= BAND_EDGE_PX && hit.slot > 0 && hit.slot < BAND_SLOTS){
          cutBandAt(e.clientX);
          return;
        }
      }
      var f = e.target.closest("[data-seg]");
      if(!f) return;
      bandSel = Number(f.getAttribute("data-seg"));
      paintBandChart();
    };
    chart.onpointerleave = function(){
      var cut = document.getElementById("bd-cut");
      if(cut) cut.hidden = true;
      var tip = document.getElementById("bd-tip");
      if(tip) tip.hidden = true;
      chart.classList.remove("near-edge");
      // 拖到图外松手时 pointerup 收不到，这里兜一下，别让 bandDrag 一直挂着
      bandDrag = null;
    };
    list.onclick = null;   // 列表只剩那行注释，没有可点的东西了
    paintBandChart();
    // v3.71（用户第 7 条）：曲线下面那行动态调整跟着一起画（切作用范围会整块重建表单）
    renderAdaptiveRow();
  }

  function closeAdminForm(form){
    if(!form) return;
    form.hidden = true;
    var add = form.parentNode && form.parentNode.querySelector(".chip-add");
    if(add) add.hidden = false;
  }
  document.getElementById("settings").addEventListener("keydown", function(e){
    var t = e.target;
    if(t && t.classList && (t.classList.contains("ad-name") || t.classList.contains("ad-qq"))){
      if(e.key === "Escape"){ e.preventDefault(); closeAdminForm(adminFormOf(t)); return; }
      if(e.key !== "Enter") return;
      e.preventDefault();
      adminCommit(adminFormOf(t));
      return;
    }
    if(!t || !t.classList || !t.classList.contains("chip-in")) return;
    if(e.key === "Escape"){ e.preventDefault(); closeChipInput(t); return; }
    if(e.key !== "Enter") return;
    e.preventDefault();
    var v = (t.value || "").trim();
    if(!v) return;
    var chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = v;
    var b = document.createElement("b");
    b.setAttribute("data-del", "1");
    b.textContent = "×";
    chip.appendChild(b);
    // 插到「+」按钮之前：新加的外号排在列表末尾，加号始终跟在最后一个胶囊后面
    var addBtn = t.parentNode.querySelector(".chip-add");
    t.parentNode.insertBefore(chip, addBtn || t);
    t.value = "";
    closeChipInput(t);
    dirty = true;
    scheduleSave();
  });
  // 输入框失焦且没填东西就收起，别把空框留在页面上
  document.getElementById("settings").addEventListener("focusout", function(e){
    var t = e.target;
    // v3.51（用户 m07385 第 4 条）：管理员浮层同理——点别处、一个字都没填就收起来；
    // 填了一半的留着（人可能只是去复制个 QQ 号再回来）。
    if(t && t.classList && (t.classList.contains("ad-name") || t.classList.contains("ad-qq"))){
      var frmF = adminFormOf(t);
      setTimeout(function(){
        if(!frmF || !document.body.contains(frmF)) return;
        var ae = document.activeElement;
        if(ae && frmF.contains(ae)) return;
        var n0 = frmF.querySelector(".ad-name"), q0 = frmF.querySelector(".ad-qq");
        var nv = (n0 && n0.value ? n0.value : "").trim(), qv = (q0 && q0.value ? q0.value : "").trim();
        if(!nv && !qv) closeAdminForm(frmF);
      }, 120);
      return;
    }
    if(!t || !t.classList || !t.classList.contains("chip-in")) return;
    setTimeout(function(){ if(!(t.value || "").trim()) closeChipInput(t); }, 120);
  });
  document.getElementById("settings").addEventListener("click", function(e){
    if(e.target && e.target.getAttribute && e.target.getAttribute("data-add")){
      openChipInput(e.target.parentNode);
      return;
    }
    if(e.target && e.target.getAttribute && e.target.getAttribute("data-del")){
      var chip = e.target.parentNode;
      if(chip && chip.classList.contains("chip")){ chip.remove(); dirty = true; scheduleSave(); }
    }
    // v3.50（用户 m06761 第 7 条）：管理员胶囊的三件事都走 a 前缀的钩子
    // （data-aadd / data-aok / data-adel），免得跟上面外号那套 data-add / data-del 抢。
    if(e.target && e.target.getAttribute && e.target.getAttribute("data-aadd")){
      var boxA = e.target.parentNode, frmA = boxA && boxA.querySelector(".adform");
      if(frmA){
        frmA.hidden = false;
        e.target.hidden = true;
        var ne0 = frmA.querySelector(".ad-name");
        try{ if(ne0) ne0.focus(); }catch(err){}
      }
      return;
    }
    if(e.target && e.target.getAttribute && e.target.getAttribute("data-aok")){
      var frmB = adminFormOf(e.target);
      if(!frmB) frmB = e.target.parentNode && adminFormOf(e.target.parentNode);
      adminCommit(frmB);
      return;
    }
    // v3.51（用户 m07385 第 4 条）：浮层右上角的 ×，点一下收起，什么都不加。
    if(e.target && e.target.getAttribute && e.target.getAttribute("data-acancel")){
      var frmC = adminFormOf(e.target);
      if(!frmC) frmC = e.target.parentNode && adminFormOf(e.target.parentNode);
      closeAdminForm(frmC);
      return;
    }
    if(e.target && e.target.getAttribute && e.target.getAttribute("data-adel")){
      var chipA = e.target.parentNode;
      var ai = (chipA && chipA.getAttribute) ? chipA.getAttribute("data-ai") : null;
      if(ai == null) return;
      var idxA = parseInt(ai, 10);
      var lstA = adminList();
      if(isFinite(idxA) && idxA >= 0 && idxA < lstA.length){ lstA.splice(idxA, 1); adminSync(lstA); }
      return;
    }
  });
  document.getElementById("hidden-list").addEventListener("click", function(e){
    var b = e.target.closest ? e.target.closest("[data-show]") : null;
    if(!b) return;
    post(API + "/session-show?sid=" + encodeURIComponent(b.getAttribute("data-show")))
      .then(function(){ loadHidden(true); })
      .catch(function(err){ mdAlert("恢复失败：" + err.message); });
  });

  document.getElementById("btn-reset").onclick = resetOverrides;
  // v3.93（用户 m07570 图8）：恢复默认只作用当前页，先问一声再动。
  document.getElementById("btn-pagedefault").onclick = async function(){
    var nm = pageTitle(curPage);
    // 2026-11-18（J，用户 m12610）：说明别挂在 title 上（用户不看悬浮），点的时候在确认框里说清楚——
    // 这一页只重置设置项，提示词正文要用编辑器里那个「恢复默认」。
    var ok = await mdConfirm("把「" + nm + "」这一页的设置项全部恢复默认值？\n" +
                             "提示词正文不在这里重置，要用编辑器里的「恢复默认」。",
                             {danger: true, ok: "恢复"});
    if(!ok) return;
    var res = resetPageDefaults();
    var msg = document.getElementById("set-msg");
    if(!res.done){
      if(msg) msg.textContent = "这一页没有可恢复的默认值";
      return;
    }
    // 摘要要等保存回包之后再写：saveSettings 成功时会把这一行换成「已自动保存 N 项覆盖」，
    // 用户只看到覆盖数，还以为恢复默认没生效（用户 m09483 就是这么被绕进去的）。
    resetNote = res.changed.length
      ? ("已恢复默认 " + res.done + " 项（" + res.changed.length + " 项有变化）")
      : ("已恢复默认 " + res.done + " 项（本来就是默认值）");
    if(msg) msg.textContent = resetNote;
  };
  // v3.67（用户 m20117 第 6 条）：点顶部卡片 = 换作用范围（原来那个下拉框的 onchange 就是这段）。
  document.getElementById("set-cards").addEventListener("click", async function(e){
    var b = (e.target && e.target.closest) ? e.target.closest(".scard") : null;
    if(!b) return;
    var sid = b.getAttribute("data-sid") || "";
    if(sid === (scopeSid || "")) return;
    // 改了没保存就切走 = 静默丢改动，得先问一声（审查 bug）
    if(dirty && !(await mdConfirm("有改动还没存完，切走就丢了。继续吗？"))) return;
    scopeSid = sid;
    settingsScopeChanged();
    cacheScopeSid = sid;       // 缓存记录页跟着看同一个作用范围
    dirty = false;
    document.getElementById("set-msg").textContent = "";
    renderScopeOptions();      // 先把高亮挪过去，别等接口回来才动
    loadSettings();
  });
  // v3.71（用户 m23596 第 4 条）：原来这里一律弹「确定离开？」——用户点确认后，
  // 防抖里那次保存就跟着页面一起没了，回来看到开关弹回去。现在先补发（keepalive），
  // 补发成功就放行；真发不出去才拦。
  window.addEventListener("pagehide", function(){ flushPendingSave(); });
  window.addEventListener("beforeunload", function(e){
    var ordinarySaved = flushPendingSave();
    if(!ppHasDrafts() && ordinarySaved) return;
    e.preventDefault();
    e.returnValue = "";
  });
  // v3.69（用户 m21003 第 1 条）：和首页对称，撤掉「打开中…」＋变淡＋40ms 延迟，直接跳。
  document.getElementById("btn-home").onclick = async function(){
    if(ppHasDrafts() && !(await mdConfirm("还有未保存的提示词草稿。离开将丢失草稿，确定继续？",{danger:true,ok:"离开并放弃草稿"}))) return;
    flushPendingSave();   // v3.71：先把还没提交的改动补发出去，再跳
    try{ localStorage.setItem("mdp_page", "home"); }catch(x){}
    location.href = "/page/plugin/" + encodeURIComponent(PLUGIN_ID) + "/decision-log/";
  };
  // 用户 m19731：设置页也能直接重载，不用切回首页再点。走的是首页那个 POST /reload。
  document.getElementById("btn-reload").onclick = async function(){
    var b = this;
    if(b.disabled) return;
    b.disabled = true;
    try{
      flushPendingSave();   // 先把防抖里还没提交的改动补发出去，再重载
      var r = await fetch(CORE + "/reload", {method: "POST", credentials: "same-origin"});
      if(!r.ok) throw new Error("HTTP " + r.status);
      setSub("已重载，正在刷新…");
      setTimeout(function(){ location.reload(); }, 600);
    }catch(e){
      mdAlert("重载失败：" + (e && e.message ? e.message : e));
    }finally{
      b.disabled = false;
    }
  };

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

  try {
    var qs = new URLSearchParams(location.search);
    scopeSid = qs.get("sid") || "";
  } catch(err) { scopeSid = ""; }
  // 用户 m24700 第 ③⑤ 条：设置页顶部一行——宿主版本、七项兼容探测、私有补丁装了几个。
  // 全部通过时安静一行；有跳过项就变琥珀色，名字直接写出来，明细挂在 title 上。
  function loadCompatBar(){
    var bar = document.getElementById("compat-bar");
    if(!bar) return;
    get(API + "/health").then(function(d){
      var compat = (d && d.compat) || {};
      var keys = Object.keys(compat);
      var gate = (d && d.patch_gate) || {};
      var entries = Array.isArray(gate.entries) ? gate.entries : [];
      if(!keys.length && !entries.length) return;   // 老后端没有这两块，别显示 0/0 吓人
      var okCount = keys.filter(function(k){ return compat[k]; }).length;
      var skipped = Array.isArray(gate.skipped) ? gate.skipped : [];
      var host = String((d && d.host) || "");
      if(host && host.charAt(0) !== "v") host = "v" + host;
      var bad = skipped.length > 0 || okCount < keys.length;
      var tips = keys.map(function(k){ return k + "：" + (compat[k] ? "有" : "缺"); });
      skipped.forEach(function(it){
        tips.push("跳过 " + (it.label || it.name || "") + "：" + (it.reason || ""));
      });
      bar.className = "compatbar" + (bad ? " warn" : "");
      bar.title = tips.join("\n");
      bar.innerHTML = "<span class='cb-dot'></span>" +
        "<span>宿主 <b>" + esc(host || "未知") + "</b></span>" +
        "<span>兼容 <b>" + okCount + "/" + keys.length + "</b></span>" +
        "<span>私有补丁 <b>" + Number(gate.applied || 0) + "/" + Number(gate.total || 0) + "</b></span>" +
        (skipped.length
          ? "<span class='cb-skip'>跳过 " +
            skipped.map(function(it){ return esc(it.label || it.name || ""); }).join("、") + "</span>"
          : "");
      bar.hidden = false;
    }).catch(function(){
      bar.className = "compatbar";
      bar.innerHTML = "<span class='cb-dot'></span><span>兼容自检读取失败</span>";
      bar.hidden = false;
    });
  }

  paintFromCache();
  loadCompatBar();
  loadSettings();
  bindDecisionPage();
  // 用户 m04756：作用范围 / 隐藏会话不挡首屏——等配置和当前页的数据先发出去再补。
  setTimeout(function(){ loadScopes(); loadHidden(); }, 0);
