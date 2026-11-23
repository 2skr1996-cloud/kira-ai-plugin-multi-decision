  var CHANNEL = "kira-plugin-page";
  var parts = location.pathname.split("/");
  var PLUGIN_ID = decodeURIComponent(parts[3] || "kira-ai-plugin-multi-decision");
  var API = "/api/plugin/" + encodeURIComponent(PLUGIN_ID);
  var CORE = "/api/plugins/" + encodeURIComponent(PLUGIN_ID);
  var ICON_PENCIL = "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M12 20h9'/><path d='M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z'/></svg>";
  var ICON_TRASH = "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M3 6h18'/><path d='M8 6V4h8v2'/><path d='M19 6l-1 14H6L5 6'/><path d='M10 11v6'/><path d='M14 11v6'/></svg>";
  var ICON_CHEV = "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M6 9l6 6 6-6'/></svg>";
  var autoTimer = null;
  var curSid = "";
  var CFG = { threshold: 0, decision: true, window: true, keep: 0, trim: 0, mode: "", enabled: true };
  var sEff = {};   // sid -> 该会话的有效配置（overview 里每个 session 带的 eff）
  var sStat = {};  // sid -> 最近 24 小时统计与攒数（胶囊上「实际概率 / 已攒条数」用，v3.23）
  var sEta = {};   // sid -> {kind:"silent"|"rest", at:epoch}：静默/休兜下一次自动冒泡的绝对时刻（v3.53）
  var sFired = {}; // sid -> {silent:ts, rest:ts}：这一轮安静里冒过泡没有（用户 m11091 第 1 条）
  // 取某个会话的配置：有单群覆盖就用覆盖后的有效值，否则回退全局
  function cfgOf(sid){
    var e = sEff[sid];
    if(!e) return CFG;
    return {
      decision: !!e.decision_enabled,
      window: !!e.window_enabled,
      keep: e.window_keep || 0,
      trim: e.window_trim_threshold || 0,
      mode: e.trigger_mode || "",
      threshold: e.trigger_threshold || 0,
      prob: e.trigger_probability || 0,
      enabled: CFG.enabled,
      overridden: e.overridden || 0
    };
  }
  // 群组头右侧的配置摘要（按群取有效值，有覆盖时加「单群」标记）。
  // 返回数组而不是一整串：项一多就换行挤成一团，拆成小气泡各自换行才看得清（用户 m30009 图2）
  // 一位小数、去掉多余的 .0（胶囊里塞不下两位数小数）
  function trimNum(n){
    return (Math.round(n * 10) / 10).toFixed(1).replace(/\.0$/, "");
  }
  // 倒计时文案：静默 / 休兜 开着时标签直接显示还剩多少秒（用户 m08226 第 2 条）
  function etaText(lbl, at){
    var left = Math.max(0, Math.round(Number(at) - Date.now() / 1000));
    return lbl + " " + left + "秒";
  }
  // 触发时刻只显示到分钟：胶囊的 title 里写「上次冒泡 03:12」够用了
  function hhmm(ts){
    var n = Number(ts) || 0;
    if(!n) return "";
    var d = new Date(n * 1000);
    function p(x){ return x < 10 ? "0" + x : "" + x; }
    return p(d.getHours()) + ":" + p(d.getMinutes());
  }
  // 用户 m11091 第 1 条：冒过泡就打勾。还压着消息时后端会给倒计时，那时候优先
  // 显示倒计时——倒计时本身说明「这一轮还没消化完」，打勾反而看不出进度。
  function firedOf(sid){
    var f = sFired[sid] || {};
    return { silent: Number(f.silent) || 0, rest: Number(f.rest) || 0 };
  }
  // v3.71（用户 m23596 第 1 条）：休息时段里静默兜底不生效——引擎进了休息窗口就只按
  // rest_probability 掷骰，不再走静默兜底。所以左栏那颗「静默」胶囊要退回未激活色，
  // 倒计时也停掉（后端仍会给 silent_eta，但这时候它不会真的冒泡）。
  function inRestWindow(ef, sid){
    if(!ef) return false;
    // v3.72（用户 m24260 第 2 条）：曲线模式有自己独立的一套休息时段（band_rest_*），
    // 普通模式用 rest_*；这里按当前模式挑一套，跨零点都支持。
    // 用户 m03349：私聊不走曲线（后端 _bands_on 对私聊直接 False），所以私聊一律用普通那套。
    var band = !!ef.bands_enabled && !isDM(sid);
    // v3.83（用户 m03849 图1）：曲线模式的休息段是段上的月亮，可能有好几段；
    // 后端 _band_rest_view 已经算好「此刻在不在里面」给成 band_rest_now，
    // 比拿第一段的起止去比窗口准（第二段 23:30 之后原来会漏判）。
    if(band && typeof ef.band_rest_now === "boolean") return !!ef.band_rest_enabled && !!ef.band_rest_now;
    if(!(band ? ef.band_rest_enabled : ef.rest_mode_enabled)) return false;
    function mins(v){
      var m = /^(\d{1,2}):(\d{2})$/.exec(String(v || "").trim());
      return m ? (Number(m[1]) * 60 + Number(m[2])) : -1;
    }
    var a = mins(band ? ef.band_rest_start : ef.rest_start);
    var b = mins(band ? ef.band_rest_end : ef.rest_end);
    if(a < 0 || b < 0 || a === b) return false;
    var d = new Date();
    var now = d.getHours() * 60 + d.getMinutes();
    return a < b ? (now >= a && now < b) : (now >= a || now < b);
  }
  function cfgChips(sid){
    var c = cfgOf(sid);
    // 开着就不占位（右边那个开关本身就是「开」），关掉才提示一句
    var out = [];
    if(!c.decision) out.push({t:"已关闭", sec:"top", on:false, tip:"这个会话的决策开关是关的：不计数、不进窗口、不触发"});
    // 私聊没有专属胶囊了（用户 m13250 第 4 条）：跟群聊走同一条链，新开的私聊记录
    // 默认「计数模式 + 阈值 1」——一条就回，效果和原来的必回一样，但设置页里能改。
    // 每条都带 sec：top 是「模式 / 百分比 / 窗口」那行，st 是下面那排两字开关标签
    // （用户 m07864 第 3 条），scope 胶囊在渲染时补在 top 行最右边。
    var mode = c.mode || "";
    // v3.70（用户 m21533 第 6 条）：曲线开着时第一个标签写「曲线模式」，右边那个概率就删掉
    // （概率已经画在曲线的每一段上）；动态调整开着时，就在原来概率那个位置写「动态调整」。
    var efTop = sEff[sid] || {};
    // 用户 m03349：私聊只有「概率模式 / 计数模式」两档，不跟全局的曲线开关走——
    // 后端 _bands_on() 对私聊直接返回 False（曲线 / 动态调整都只给群聊），
    // 前端以前只看 bands_enabled，于是全局一开曲线，私聊卡上就顶着「曲线模式」。
    // 私聊若还留着 adaptive / bands 的旧值，按后端的实际口径归一：adaptive→概率、bands→计数。
    var dmNow = isDM(sid);
    // 私聊只有概率模式和计数模式：曲线也好、动态调整也好，落到私聊都按概率模式显示
  // （两者本来就是「按概率决定回不回」，说成计数模式会让人以为是按条数算）。
  var dmMode = (mode === "bands" || mode === "adaptive") ? "probability" : mode;
    var modeShown = dmNow ? dmMode : mode;
    var bandOn = !!efTop.bands_enabled && !dmNow;
    var adaptOn = (mode === "adaptive") && !dmNow;
      out.push({t: bandOn ? "曲线模式" : (MODE_LABEL[modeShown] || (modeShown ? modeShown : "-")), sec:"top",
                tip: bandOn ? "时段曲线开着：一天按段给概率，普通模式那列的概率不再生效"
                            : (MODE_LABEL[modeShown] || modeShown) + (dmNow ? "（私聊只有概率模式和计数模式）" : "")});
      // v3.23（用户 m45332 第5条）：光一个「阈值 10」看不出现在到哪了。
      // 概率模式显示「最近 24 小时实际概率/配置概率」，计数模式显示「已攒/阈值」。
      // 分母只算用户消息（不含 bot 回复），跟裁切、柱子的口径一致。
      var st0 = sStat[sid] || {};
      var msgs24 = st0.messages || 0, rep24 = st0.replied || 0;
      if(adaptOn){
        // v3.73（用户 m24640 第 3 条）：动态调整是个「一直在自己动」的状态，给它一个
        // 区别于普通标签的紫底，扫一眼就知道这个会话在用动态调整。
        out.push({t: "动态调整", sec:"top", hi:"adapt",
                  tip: "动态调整：按最近的实际回复率自动微调曲线上的概率"});
      } else if(!bandOn){
        if(modeShown === "probability"){
          var actual = msgs24 > 0 ? (rep24 * 100 / msgs24) : 0;
          var actTxt = msgs24 > 0 ? trimNum(actual) : "—";
          // v3.24（用户 m45932 第2条）：前面那个「11.3」是百分比，漏了 %，
          // 写成「11.3%/10%」——左边是最近 24 小时实际、右边是配置值。
          out.push({t: (msgs24 > 0 ? actTxt + "%" : "—") + "/" + (c.prob || 0) + "%", sec:"top",
                    tip: "最近 24 小时：回复 " + rep24 + " 次 / 收到 " + msgs24 + " 条用户消息" +
                         " = " + actTxt + "%（不含 bot 消息）；配置的每条回复概率是 " + (c.prob || 0) + "%"});
        } else if(modeShown === "count"){
          var need = c.threshold || 0;
          var had = st0.counter || 0;
          // 计数模式写「5条/10条」，光两个数字看不出哪个是哪个（v3.24 用户 m45932）
          out.push({t: had + "条/" + (need || "-") + "条", sec:"top",
                    tip: "已经攒了 " + had + " 条用户消息，还差 " + Math.max(0, need - had) +
                         " 条到 " + need + " 条就触发（不含 bot 消息）"});
        } else {
          out.push({t: "阈值 " + (c.threshold || "-"), sec:"top"});
        }
      }
    // v4.52（用户 m28586 第 6 项）：这句话原来跟实现不符——keep 数的是「窗口里
    // 一共留几条」（bot 回复也占名额），trim 数的是「用户消息（窗口内 + 待发）」。
    out.push(c.window
      ? {t: "窗口 " + c.keep + "/" + c.trim, sec:"top",
         tip: "裁切后窗口里一共留 " + c.keep + " 条消息（你发的和 bot 回的都算，工具往返不算）；" +
              "攒到 " + c.trim + " 条用户消息（窗口里的 + 还没发出去的待发）就裁一次"}
      : {t: "窗口关", sec:"top", on:false});
    // 用户 m13250 第 4 条：私聊也走同一条触发链了，后面那些开关（兜底/合并/防抖/休息/静默/休兜）
    // 对私聊同样有效，不再提前 return。
    // 用户 m08226 第 2 条：状态行顺序改成 兜底/合并 → 防抖 → 休息 → 静默 → 休兜。
    // 兜底只有概率模式才有，所以贴着防抖放；计数模式下这一格换成「合并」（连发合并），
    // 位置不动，切换模式时这一排不会跳。静默 / 休兜 开着时标签直接变倒计时
    // （「静默 42秒」），每秒减一次，时刻来自 /overview 与 /context-sessions 的 silent_eta。
    var ef = sEff[sid] || {};
    // v3.81（用户 m02645 图1）：这一格原来只在「概率模式」显示随机口径，曲线模式（mode === "bands"）
    // 掉进下面的 else 显示成「合并」——曲线模式压根不用连发合并，那是计数模式的东西。
    // 现在：掷骰类（概率 / 曲线 / 动态调整）显示「节奏 / 随机」，计数模式才显示「合并」。
    // v3.81（补充）：曲线接管时 c.mode 可能还停在「计数」（engine 的 _bands_on 优先于 mode），
    // 这种会话实际在按段概率掷骰，所以「曲线开着」也算掷骰类，别再掉进「合并」。
    // v3.91（用户 m05749）：开了进阶决策的会话挂一颗胶囊——它比曲线 / 概率优先，
    // 先让决策模型读窗口判回不回，判回才跑完整那一轮；不回就只留一条决策记录。
    // v3.93（用户 m07570 图2）：胶囊改名「决策模式」（设置页里 advanced_enabled 早就叫这个名字，
    // 「进阶」是旧名残留）；位置紧接 top 那一行（也就是「全局配置」下面），颜色保留紫色 hi-adapt——
    // 用户要的就是「用个特殊颜色」，跟节奏/随机那些绿蓝胶囊分开。
    if(!dmNow && ef.advanced_enabled){
      // v3.94（用户 m08398 图9）：位置钉到状态行最右（就是首行「独立配置/全局配置」正下方），
      // 颜色换成紫色实底 hi-dec——以前 hi-adapt 那点浅底打不过下面 .cfg.st .chip.on 的绿，
      // 看着跟别的状态胶囊一样。
      out.push({t: "决策模式", sec: "st", on: true, hi: "dec", right: true,
        tip: "决策模式开着：触发后先让决策模型看窗口，建议回复才跑完整回复；建议不回就只留一条决策记录"});
    }
    var bandOnNow = !!ef.bands_enabled && !dmNow;
    var rhythmMode = (modeShown === "probability" || (!dmNow && (c.mode === "bands" || c.mode === "adaptive")) || bandOnNow);
    var jitSec = Number(ef.jitter_seconds) || 0;
    var eta = sEta[sid] || {};
    var etaAt = Number(eta.at) || 0;
    var etaKind = String(eta.kind || "");
    if(rhythmMode){
      // v3.81（用户 m02645 图2）：伪随机改名「节奏模式」——开 = 按间隔排队回，关 = 每条独立掷骰；
      // 概率、曲线、动态调整三种模式共用同一套（动态调整是强制开，且只对群聊生效）。
      var dynNow = (c.mode === "adaptive") && bandOnNow && !dmNow;
      var pseudoOn = dynNow || !!ef.prob_pseudo;
      out.push({
        // v3.83（用户 m03849 图2）：以前 on:false 把「随机」整颗压成灰色虚线框，看着像这一格没启用。
        // 「随机」也是一直在生效的掷骰方式，所以两种都点亮，只换颜色：节奏绿、随机蓝（hi-rand）。
        t: pseudoOn ? "节奏" : "随机", sec: "st", on: true, hi: pseudoOn ? "" : "rand",
        tip: dynNow
          ? "动态调整强制按节奏走：越等越容易回，平均多久回一次不变"
          : (pseudoOn
            ? "节奏模式：越等越容易回，平均大约隔 " + (ef.trigger_probability ? Math.round(100 / Math.max(1, Number(ef.trigger_probability))) : "N") + " 条一次"
            : "随机：每条都只按设定概率掷一次，不保底")
      });
    } else {
      // 计数模式：这一格是连发合并（掷骰模式不用它，所以不显示）。
      var bmSec = Number(ef.burst_merge_seconds) || 0;
      out.push({
        t: "合并", sec: "st", on: !!ef.burst_merge_enabled,
        tip: ef.burst_merge_enabled
          ? "连发合并开着：连着来几条就合成一轮，最多等 " + bmSec + " 秒"
          : "连发合并关着：来一条就触发一轮"
      });
    }
    out.push({
      t: "防抖", sec: "st", on: !!ef.jitter_enabled,
      tip: ef.jitter_enabled
        ? "消息防抖开着：触发后再等 " + jitSec + " 秒，期间来新消息就重新计时"
        : "消息防抖关着：触发后立刻请求，不等待"
    });
    // v3.72（用户 m24260 第 2 条）：两套休息时段——普通模式 rest_*、曲线模式 band_rest_*，
    // 胶囊按当前模式显示那一套（时间点也用它）。
    var rsBand = !!ef.bands_enabled && !dmNow;
    var rsOn = rsBand ? !!ef.band_rest_enabled : !!ef.rest_mode_enabled;
    var rsS = String((rsBand ? ef.band_rest_start : ef.rest_start) || "");
    var rsE = String((rsBand ? ef.band_rest_end : ef.rest_end) || "");
    var rsCnt = rsBand ? (Number(ef.band_rest_count) || 0) : 0;
    var rsTxt = (rsS && rsE) ? rsS + " → " + rsE + (rsCnt > 1 ? "（共 " + rsCnt + " 段）" : "") : "";
    // v3.83（用户 m03849 图1）：这颗胶囊说的是「现在有没有在休息」，不是「休息功能开没开」。
    // 以前只看开关，白天不在时段里也亮着绿灯，看着像正在休息。
    var rsNow = rsOn && (rsBand ? !!ef.band_rest_now : inRestWindow(ef, sid));
    out.push({
      t: "休息", sec: "st", on: rsNow,
      tip: rsNow
        ? "现在就在休息时段里" + (rsTxt ? "（" + rsTxt + "）" : "") + "：这段时间只按休息概率回"
        : (rsOn
          ? "休息时段开着" + (rsTxt ? "（" + rsTxt + "）" : "") + "，但现在不在时段内：此刻按正常概率回"
          : "休息时段关着：全天按同一个概率")
    });
    var silMin = Number(ef.silent_trigger_minutes) || 0;
    var silOn = !!ef.silent_trigger_enabled;
    // v3.71（用户 m23596 第 1 条）：休息时段里静默兜底不生效 → 胶囊按「没开」显示。
    var silRest = silOn && inRestWindow(ef, sid);
    var silEta = (silOn && !silRest && etaKind === "silent" && etaAt > 0) ? etaAt : 0;
    var silFired = firedOf(sid).silent;
    var silDone = !silEta && silOn && !silRest && silFired > 0;
    // 用户 m02245：待发里只有链接 / 转发 / 图片时兜底不冒泡，胶囊上标一下，
    // 免得倒计时突然消失看着像坏了。
    var silSkip = !silEta && !silDone && silOn && !silRest && firedOf(sid).hold > 0;
    out.push({
      t: silEta ? etaText("静默", silEta) : (silDone ? "静默 ✓" : (silSkip ? "静默 跳过" : "静默")),
      sec: "st", on: silOn && !silRest, ok: silDone,
      eta: silEta, lbl: "静默",
      tip: silRest
        ? "休息时段里静默兜底不生效：现在只按休息时段那个概率掷骰（安静再久也不会因此补一轮）"
        : (silOn
          ? "静默兜底开着：安静 " + silMin + " 分钟还有没处理的消息就补一轮" +
            (silEta ? "；下一次在 " + Math.max(0, Math.round(silEta - Date.now() / 1000)) + " 秒后" : "") +
            (silDone ? "；上次静默冒泡 " + hhmm(silFired) : "") +
            (silSkip ? "；待发里只有链接/转发/图片，这次不冒泡" : "")
          : "静默兜底关着：安静再久也不会主动补一轮")
    });
    var rsMin = Number(ef.rest_silent_minutes) || 0;
    var rsOn = !!ef.rest_silent_enabled;
    var rsEta = (rsOn && etaKind === "rest" && etaAt > 0) ? etaAt : 0;
    var rsFired = firedOf(sid).rest;
    var rsDone = !rsEta && rsOn && rsFired > 0;
    out.push({
      t: rsEta ? etaText("休静", rsEta) : (rsDone ? "休静 ✓" : "休静"),
      sec: "st", on: rsOn, ok: rsDone,
      eta: rsEta, lbl: "休静",
      tip: rsOn
        ? "休息静默开着：休息时段里安静 " + rsMin + " 分钟也补一轮" +
          (rsEta ? "；下一次在 " + Math.max(0, Math.round(rsEta - Date.now() / 1000)) + " 秒后" : "") +
          (rsDone ? "；上次休息静默冒泡 " + hhmm(rsFired) : "")
        : "休息静默关着：休息时段里不补轮"
    });
    return out;
  }
  // 会话卡胶囊统一渲染（用户 m07864 第 3 条）：sec:"st" 走状态行样式，
  // on:true 绿底、on:false 灰虚线框；data-cfg 让整颗胶囊都能点去设置页。
  function ctxChipHtml(x, sid){
    var obj = (x && typeof x === "object");
    var t = obj ? (x.t || "") : String(x === undefined || x === null ? "" : x);
    var sec = obj ? (x.sec || "") : "";
    // v3.73（用户 m24640 第 3 条）：x.hi 给胶囊换个专属颜色（目前只有动态调整用 hi-adapt）
    var cls = "chip" + (sec === "st" ? " st" : "") +
              (obj && x.on === true ? " on" : "") + (obj && x.on === false ? " off" : "") +
              (obj && x.ok === true ? " ok" : "") +
              (obj && x.hi ? " hi-" + x.hi : "") +
              (obj && x.right ? " rt" : "");
    var tip = (obj && x.tip) ? " title='" + esc(x.tip) + "'" : "";
    // 倒计时胶囊（静默 / 休兜）：把目标时刻写在属性上，每秒只改这一颗的文字，
    // 不重画列表——重画会丢滚动位置和悬停状态（用户 m08226 第 2 条）。
    var eta = (obj && x.eta) ? " data-eta='" + esc(String(x.eta)) + "' data-eta-lbl='" + esc(x.lbl || "") + "'" : "";
    return "<i class='" + cls + "'" + tip + eta + " data-cfg='" + esc(sid) + "'>" + esc(t) + "</i>";
  }
  var aliases = {};
  var pluginEnabled = true;
  // 左栏会话卡折叠（用户 m02080）：折叠后只留头像、名字和开关。
  // 选择按会话记在 localStorage，刷新和 5 秒轮询重画都不能把它忘掉。
  var FOLD_KEY = "mdp_fold_sids";
  var GRPFOLD_KEY = "mdp_fold_groups";
  var REASON_LABEL = { count: "攒够条数", probability: "概率触发", curve: "曲线触发",
    adaptive: "自适应阈值", mention: "@ 或叫名字", private: "私聊", silent: "静默冒泡",
    nickname: "叫到昵称",
    // v3.87（用户 m04715 第 1 条）：非请求触发的裁切快照，别在表里露英文 trim
    trim: "窗口裁切",
    // v3.91（用户 m05749）：进阶决策那一层的判定记录
    decision: "决策模式" };
  // 触发模式的中文名（用户 m31156：不要显示 count/probability 这种英文）
  var MODE_LABEL = { count: "计数模式", probability: "概率模式", adaptive: "自适应模式", bands: "曲线模式" };

  function loadJSON(k, dft){
    try { var v = JSON.parse(localStorage.getItem(k) || "null"); return v || dft; } catch(e){ return dft; }
  }
  function saveJSON(k, v){ try { localStorage.setItem(k, JSON.stringify(v)); } catch(e){} }
  function esc(s){
    return String(s == null ? "" : s).replace(/[&<>"']/g, function(c){
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
    });
  }
  function el(tag, cls, text){ var e=document.createElement(tag); if(cls)e.className=cls; if(text!=null)e.textContent=text; return e; }
  function fmtTime(ts){ if(!ts) return "-"; var d=new Date(ts*1000); function p(n){return n<10?"0"+n:""+n;} return (d.getMonth()+1)+"-"+p(d.getDate())+" "+p(d.getHours())+":"+p(d.getMinutes())+":"+p(d.getSeconds()); }
  // 用户 m10848 第 5 条：记录表的时间列以前在窄屏折成「11-19 / 21:06:29」两行。
  // 日期单独包一层，表格放不下时由 CSS 把日期收起来，只留时间。
  function fmtTimeHtml(ts){
    if(!ts) return "-";
    var s = fmtTime(ts), i = s.indexOf(" ");
    if(i < 0) return esc(s);
    var hm = s.slice(i + 1), j = hm.lastIndexOf(":");
    return "<span class='d'>" + esc(s.slice(0, i)) + "</span> " +
           "<span class='t'>" + esc(hm.slice(0, j)) + "</span>" +
           "<span class='s'>" + esc(hm.slice(j)) + "</span>";
  }
  function fmtClock(ts){ if(!ts) return "-"; var d=new Date(ts*1000); function p(n){return n<10?"0"+n:""+n;} return p(d.getHours())+":"+p(d.getMinutes())+":"+p(d.getSeconds()); }
  // 会话 id 现在是「类型:号码」（v2.42 去掉了前面的机器人名字），最多两段，
  // 所以一律按最后两段取，旧的三段式也照样能显示。
  function shortSid(sid){ var p = String(sid||"").split(":"); return p.length >= 2 ? p[p.length-1] : String(sid||""); }
  function isDM(sid){ var p = String(sid||"").split(":"); return p.length >= 2 && p[p.length-2] === "dm"; }
  function displayName(sid){ return aliases[sid] || shortSid(sid); }
  // 会话头像：群走群头像接口、私聊走 QQ 头像接口；webchat 这种没有号码的退回首字圆点。
  // 图片挂了就 onerror 把自己摘掉，露出底下那个首字底衬，不会留个破图。
  function avatarHtml(sid){
    var ch = (displayName(sid) || "?").slice(0, 1);
    var m = /^(?:[^:]+:)?(gm|dm):(\d+)$/.exec(String(sid || ""));
    var img = "";
    if(m){
      var url = (m[1] === "gm")
        ? "https://p.qlogo.cn/gh/" + m[2] + "/" + m[2] + "/100"
        : "https://q1.qlogo.cn/g?b=qq&nk=" + m[2] + "&s=100";
      img = "<img src='" + url + "' referrerpolicy='no-referrer' loading='lazy' alt='' onerror='this.remove()'>";
    }
    return "<span class='avw' data-ch='" + esc(ch) + "'>" + img + "</span>";
  }
  function get(url){ return fetch(url, {credentials:"same-origin"}).then(function(r){ if(!r.ok) throw new Error("HTTP "+r.status); return r.json(); }); }
  function post(url){ return fetch(url, {method:"POST", credentials:"same-origin"}).then(function(r){ if(!r.ok) throw new Error("HTTP "+r.status); return r.json(); }); }

  // 页内模态（v3.26，用户 m46693 ②）：外壳把插件页放在 sandbox iframe 里，没给
  // allow-modals，window.confirm / window.alert 会被浏览器直接忽略——控制台报
  // 「Ignored call to 'confirm()'. The document is sandboxed…」，confirm 恒返回 false，
  // 于是「恢复跟随全局」「清空记录」这类带确认的按钮点下去一点反应都没有。
  // 这里用页面自己的遮罩层实现，返回 Promise，语义跟原生对话框一致，调用方 await 即可。
  function mdDialog(opt){
    opt = opt || {};
    return new Promise(function(resolve){
      var mask = document.createElement("div");
      mask.className = "mask";
      var box = document.createElement("div");
      box.className = opt.note ? "dlg wide" : "dlg";
      // 用户 m13149：说明文字以前跟标题挤在同一个 <p> 里，弹窗只有 520px，
      // 第一句 45 个字必然折行、末行只剩「「展开」还能看。」。带 note 的弹窗
      // 加宽到 680px，说明单独放进 .dlg-note，一行一条，不再折。
      var noteHtml = "";
      if(opt.note){
        var noteLines = (typeof opt.note === "string") ? [opt.note] : (opt.note || []);
        noteHtml = "<div class='dlg-note'>" + noteLines.map(function(s){
          return "<div>" + esc(String(s || "")) + "</div>";
        }).join("") + "</div>";
      }
      box.innerHTML = "<h3>" + esc(opt.title || (opt.alert ? "提示" : "确认操作")) + "</h3>" +
        "<p class='p2'>" + esc(opt.message || "").replace(/\n/g, "<br>") + "</p>" +
        noteHtml +
        "<div class='dlg-btns'>" +
          (opt.alert ? "" : "<button data-act='cancel'>取消</button>") +
          "<button class='" + (opt.danger ? "danger" : "") + "' data-act='ok'>" +
            esc(opt.ok || (opt.alert ? "知道了" : "确定")) + "</button>" +
        "</div>";
      mask.appendChild(box);
      document.body.appendChild(mask);
      var done = false;
      function close(v){
        if(done) return;
        done = true;
        document.removeEventListener("keydown", onkey, true);
        if(mask.parentNode) mask.parentNode.removeChild(mask);
        resolve(v);
      }
      function onkey(e){
        if(e.key === "Escape"){ e.preventDefault(); close(false); }
        else if(e.key === "Enter"){ e.preventDefault(); close(true); }
      }
      document.addEventListener("keydown", onkey, true);
      mask.addEventListener("click", function(e){
        if(e.target === mask){ close(false); return; }
        var b = e.target.closest ? e.target.closest("[data-act]") : null;
        if(!b) return;
        close(b.getAttribute("data-act") === "ok");
      });
      var ok = box.querySelector("[data-act='ok']");
      if(ok && ok.focus) ok.focus();
    });
  }
  function mdConfirm(message, opt){
    opt = opt || {};
    opt.message = message;
    return mdDialog(opt);
  }
  function mdAlert(message, opt){
    opt = opt || {};
    opt.alert = true;
    opt.message = message;
    return mdDialog(opt).then(function(){});
  }

  // 首屏直出（v3.07）：外壳每次切换页面或刷新都会重建 iframe，接口回来之前整块是空的，
  // 看起来像「先闪一下再长出来」。把上一次的概览与会话列表留在 sessionStorage 里，
  // 脚本一执行就先画出来，接口回来再静默覆盖（5 秒自动刷新会纠正过期数字）。
  function cachePut(k, v){
    try{
      var s = JSON.stringify(v);
      // sessionStorage 一共就 5MB 左右；大窗口的 messages 序列化能到好几 MB，
      // 超了就别塞，免得把 ov/sessions 这些更关键的键挤掉（v3.19）
      if(s && s.length > 1500000) return;
      sessionStorage.setItem("mdp_c_" + k, s);
    }catch(e){}
  }
  function cacheGet(k){ try{ var s = sessionStorage.getItem("mdp_c_" + k); return s ? JSON.parse(s) : null; }catch(e){ return null; } }

  // ---------------------------------------------------------------- 概览

  // fromCache=true：这一遍只是拿上次的数据先把版面铺出来，不能动开关状态、
  // 也不能提前释放 booting（否则外壳里那一帧又会看到开关滑动）。
  // 用户 m07085 第 3 条：5 秒一次轮询原本每次都重建 #cards 的 innerHTML，
  // 鼠标正停在某张卡上看 title 时节点被换掉，表现就是「卡一下」。这里对比拼好的
  // HTML，内容没变就不动 DOM（悬停的 title 气泡就不会被打断）。
  var lastCardsHtml = "";
  function renderCards(data, fromCache){
    var box = document.getElementById("cards");
    var t = data.today || {};
    // 口径统一成「最近 24 小时」（用户 m41511）：不再按自然日切，跨零点的夜聊也算进来。
    var tk = t.tokens || {};
    // v3.20（用户 m41961）：副行拆成群聊 / 私聊两个数——「群聊接收 X 条 · 私聊接收 X 条」。
    // 老后端没有 groups/privates 时返回空串，卡片回到只有主值的样子。
    var kindSub = function(key, word, unit){
      var g = t.groups || {}, p = t.privates || {};
      if(typeof g[key] !== "number" && typeof p[key] !== "number") return null;
      // 用户 m42220：必须排成一行。卡片内容宽只有 130px 上下，去掉「接收 / 回复」这两个字
      // （卡片标题已经写了消息 / 回复），「群聊 56 条 · 私聊 0 条」刚好放得下。
      var a = "群聊 " + (g[key] || 0) + unit;
      var b = "私聊 " + (p[key] || 0) + unit;
      return { plain: a + " · " + b, html: "<span class='nowrap'>" + a + " · " + b + "</span>" };
    };

    // v3.72（用户 m24260 第 11 条）：小于 0.1 MB 的目录用 KB 显示——
    // 窗口快照只有几十 KB，写成 0.0 MB 会让人以为这一类根本没占空间。
    function fmtMb(mb, bytes){
      if(!(mb > 0)) return (bytes > 0 ? Math.max(1, Math.round(bytes / 1024)) + " KB" : "0 MB");
      return mb >= 0.1 ? mb.toFixed(1) + " MB" : Math.max(1, Math.round((bytes || mb * 1048576) / 1024)) + " KB";
    }

    // v4.85（用户 m10425 第 5 条）：概览卡的「数量」一列——几千个文件直接写数字太长，
    // 上万就折成「x.x 万」，免得把三列清单撑宽。
    function fmtCnt(n){
      n = Number(n) || 0;
      if(n < 10000) return String(n);
      return (n / 10000).toFixed(n >= 100000 ? 0 : 1) + " 万";
    }

    // 用户 m06348 第 2 条：以前用「轮数 − 回复数」当未回复，失败轮也被算进去，
    // 于是 186 轮只回 90 次时看不出差在哪。现在失败只算失败；真正的沉默由后端
    // 单独给（silent = 结算了但没发出去），并且单独占一张卡片显式写出来。
    var ksMsg = kindSub("messages", "接收", " 条");
    var ksRep = kindSub("replied", "回复", " 次");
    // v4.68（用户 m02472 + m02807）：回复 / 决策两格的主值行直接写成
    // 「30次 / 17秒 / 命中87%」——用户要的是第二行，不是再加一行副行。
    // 数字来自 tracer 的分 kind 汇总（kinds.reply / kinds.decision）：
    // 回复的用时取快照里的 elapsed（决策单独记 decision_elapsed，不含在里面），
    // 命中率是这批轮的 cached / input。
    var kk = function(k){ return (tk.kinds && tk.kinds[k]) || {}; };
    var kr = kk("reply"), kd = kk("decision");
    var secTxt = function(v){
      if(v == null || typeof v !== "number") return "—";
      return (v < 10 ? (Math.round(v * 10) / 10) : Math.round(v)) + "秒";
    };
    // 用户 m12784：主值行改成「48%命中」——数字在前、单位在后，跟「21次」一个写法；
    // 「%命中」整体当单位，走跟其他卡一样的 <small>（11px 淡色）。
    // 百分号不留 3px 间距（small 默认 margin-left:3px），否则会写成「48 %命中」，
    // 所以单独给一个 .pct 类（见 index.html）。
    // null 一律写「命中 —」：回复这一路框架回的 LLMResponse 不带 cached_tokens，
    // 0 是「没记录」不是「没命中」，写成 0% 会让人以为缓存全废了。
    var pctTxt = function(v){
      return (v == null || typeof v !== "number") ? "—" : Math.round(v) + "%";
    };
    var hitTxt = function(v){
      return (v == null || typeof v !== "number") ? "命中 —" : Math.round(v) + "<small class='pct'>%命中</small>";
    };
    var seg = function(s){ return "<span class='nowrap'>" + s + "</span>"; };
    // 主值行三段：段之间用淡一点的斜杠，别让 / 抢了数字的注意力
    var vsep = "<span class='vsep'>/</span>";
    var vline = function(a, b, c){
      return "<span class='vm'>" + seg(a) + vsep + seg(b) + vsep + seg(c) + "</span>";
    };
    // 用户 m16262 图1：用时从主值行挪到副行，主值只剩「次数 / 命中」两段。
    // 两段的写法单独留一个（三段那个 vline 还有别的卡在用，不动）。
    // 用户 m11651：a / b 里可以带 <small>单位</small>（「21<small>次</small>」），
    // seg() 只包一层 nowrap，不会把里面的标签转义掉。
    var vline2 = function(a, b){
      return "<span class='vm'>" + seg(a) + vsep + seg(b) + "</span>";
    };
    // 副行末尾追加一项：前面没有内容时不要留一个孤零零的「 · 」。
    var subTail = function(head, tail){ return head ? (head + " · " + tail) : tail; };
    // 最近 24 小时决策（用户本轮）：决策模型跑了多少次、其中判要回的多少次。
    // 数据来自后端 /overview 的 today.decisions（decision_stats(24) 数盘上
    // decision_only 的快照）。判定不回不发请求，所以这个数跟「请求」卡不是一回事，
    // 悬停提示里说清楚。后端没给（老后端 / 请求记录关着）就写「—」，不假装 0 次。
    var dec = t.decisions || null;
    var decOk = !!(dec && typeof dec.rounds === "number");
    // v4.68（用户 m02472）：请求卡副行按类型拆开——回复轮 = 总轮数 − 决策轮。
    // 决策轮取 decisions.rounds（跟决策卡同一个来源），两段加起来正好等于主值；
    // 成功率也跟着改成「回复成功 / 回复轮」，拿决策轮当分母会把成功率压低一半。
    var decRounds = decOk ? (Number(dec.rounds) || 0) : 0;
    var repRounds = Math.max(0, (Number(t.rounds) || 0) - decRounds);
    var repRate = repRounds > 0 ? ((t.replied || 0) / repRounds * 100).toFixed(1) + "%" : "—";
    // 请求维度的成功轮：总轮数减掉出错的那几轮（决策判「不回」也算请求成功）
    var reqOk = Math.max(0, (Number(t.rounds) || 0) - (Number(t.failed) || 0));
    var items = [
      ["最近24小时消息", t.messages || 0, "条",
        "最近 24 小时收到的群消息条数", ksMsg ? ksMsg.plain : "", ksMsg ? ksMsg.html : ""],
      // 回复紧挨消息（用户 m41961）。
      // 用户 m06761 第 2 条：未回并进这张卡，不再单占一格——副行写「未回 N 次」，
      // 失败轮算在请求卡的失败里，只有「结算了但没发出回复」才算未回。
      // v4.68（用户 m02472）：改名「24小时回复概览」——这格现在同时讲次数、平均用时、
      // 缓存命中，还叫「回复」会让人以为只有次数；群聊/私聊拆分挪进悬浮（一行装不下）。
      ["最近24小时回复概览",
        vline2((t.replied || 0) + "<small>次</small>", hitTxt(kr.cache_hit)), "",
        "真正发出去的回复条数；平均每次 " + secTxt(kr.avg_elapsed) +
        "（回复请求本身的耗时，不含决策），命中率 " + pctTxt(kr.cache_hit) +
        " 按最近 24 小时有记录的 " + (kr.elapsed_rounds || 0) + " 轮回复算" +
        (kr.cache_hit == null
          ? "（框架回给插件的回复结果里没有缓存用量，这一路只能显示 —；决策那一路有）" : ""),
        // 用户 m16262 图1：用时挂到副行尾巴上（「群聊 19 次 · 私聊 0 次 · 104秒」），
        // 主值行只留次数和命中率。
        subTail(ksRep ? ksRep.plain : "", secTxt(kr.avg_elapsed)),
        subTail(ksRep ? ksRep.html : "", seg(secTxt(kr.avg_elapsed)))],
      // 用户 m02472：决策这格跟回复一样，副行写「次数 / 平均用时 / 缓存命中」；
      // 建议回复 / 建议不回 / 决策失败改挂悬停（一行装不下四项）。
      // 建议不回不发请求，所以决策次数跟「请求」卡不是一回事。
      ["最近24小时决策概览",
        decOk ? vline2((dec.rounds || 0) + "<small>次</small>", hitTxt(kd.cache_hit)) : "—", "",
        decOk ? ("最近 24 小时决策模型跑了 " + dec.rounds + " 次；其中建议回复 " +
                 (Number(dec.reply) || 0) + " 次、建议不回 " + (Number(dec.silent) || 0) +
                 " 次" +
                 (Number(dec.failed) ? "、决策失败 " + dec.failed + " 次" : "") +
                 "（建议不回不发请求，不计入请求卡）；平均每次 " + secTxt(kd.avg_elapsed) +
                 "（决策请求本身的耗时），命中率 " + pctTxt(kd.cache_hit) +
                 " 按决策轮的输入算，共 " + (kd.elapsed_rounds || 0) + " 轮")
              : "还没有决策记录：决策模式开着、并且真触发过一轮才有",
        // 用户 m16262 图1：「建议回复 / 建议不回」压成「回复 / 不回」各两个字，
        // 省下的位置给用时（副行四项：回复 N · 不回 N · 失败 N · X秒）。
        decOk ? subTail("回复 " + (Number(dec.reply) || 0) + " · 不回 " + (Number(dec.silent) || 0) +
                 (Number(dec.failed) ? " · 失败 " + dec.failed : ""), secTxt(kd.avg_elapsed)) : "",
        null],
      // 请求和回复拆成两张卡（用户 m41511），成功率只挂在请求这张上。
      // 用户 m05552 第 2 条：失败轮和重试轮以前根本没进统计（结算日志只在成功路径写），
      // 卡片永远 100%。现在后端把失败轮也写进结算日志、重试轮另记 is_retry，
      // 这里副行直接写「成功 N · 失败 N · 重试 N」，点进去能对上请求表里的红行。
      ["最近24小时请求", t.rounds || 0, "次",
        "agent 实际发起的请求轮数（重试轮也各算一次）；其中回复轮 " + repRounds +
        " 次（成功率 " + repRate + "，回复 " + (t.replied || 0) + " 次）、决策轮 " + decRounds +
        " 次；失败 " + (t.failed || 0) + " 轮、其中重试 " + (t.retried || 0) + " 轮",
        // 用户 m02869 + m02934：副行写「成功 / 失败 / 重试」三段。成功按请求轮算——
        // 总轮数减失败轮，决策判「不回」的那几轮请求本身是成功的，不能算失败，
        // 也不能只报「发出回复」的轮数（那会把 190 次缩成 30 次）。
        "成功 " + reqOk + " · 失败 " + (t.failed || 0) + " · 重试 " + (t.retried || 0),
        seg("成功 " + reqOk) + " · " + seg("失败 " + (t.failed || 0)) +
        " · " + seg("重试 " + (t.retried || 0))]
    ];
    // 最近 24 小时 token（用户 m33573 图4）：总 / 输入 / 输出 / 平均缓存命中。
    // 用户 m02952：副行原来写「入 / 出」，跟价目表统一成「输入 / 输出」，不用单字缩写。
    // 用户 m11651：token 卡的 M / K / B 后缀要跟「条」「次」一样小一号。
    // tokText 在 lib-4.js 里（那边不动），这里只对它的返回值做一次包装：
    // 结尾是单位字母就拆出来包 <small>，纯数字（<1000）和「—」原样返回。
    var tokHtml = function(n){
      var s = tokText(n);
      var m = /^([\d.]+)([kKMB])$/.exec(s);
      return m ? (m[1] + "<small>" + m[2] + "</small>") : s;
    };
    if(tk.total){
      items.push(["最近24小时 token", tokHtml(tk.total), "",
                  "输入 " + Math.max(0, (tk.input || 0) - (tk.cached || 0)) + " · 缓存 " + (tk.cached || 0) +
                  " · 输出 " + (tk.output || 0) +
                  (tk.cache_hit == null ? "" : " · 命中 " + tk.cache_hit + "%") +
                  " · " + (tk.rounds || 0) + " 轮有 token 记录（24 小时共 " + (t.rounds || 0) + " 轮请求）",
                  // 卡片副行只有一行宽：命中率放悬停里，副行只留「输入 / 缓存 / 输出」三段，免得被截成省略号。
                  "输入 " + tokText(Math.max(0, (tk.input || 0) - (tk.cached || 0))) + " · 缓存 " + tokText(tk.cached) +
                  " · 输出 " + tokText(tk.output)]);
    }
    // 最近 24 小时花费（用户 m02080 第 4 条）：紧挨着 token 卡，token × 设置页手填单价。
    // 用户 m02580：这张要做成常驻卡片并顶掉「活跃会话」那一格——所以不管有没有填价
    // 都占位；没填价的模型不算钱，副行说清楚，免得用户以为统计漏了。
    var cost = t.cost || {};
    var costTotal = (typeof cost.total === "number") ? cost.total : null;
    {
      var cparts = cost.parts || [];
      var unpriced = cost.unpriced || [];
      var money = function(v){
        if(!(v > 0)) return "¥0";
        return "¥" + (v < 0.01 ? v.toFixed(4) : v.toFixed(2));
      };
      // 用户 m11651：花费卡的 ¥ 是前缀，包成 <small class='pre'> 让符号小一号；
      // 用 .pre 是因为 .v small 的 margin-left 会把前缀推歪（样式主会话在 index.html 里补）。
      // money() 还被悬停提示和副行用着（那些地方要纯文本），所以另写一个，不改 money。
      var moneyHtml = function(v){
        var s = money(v);
        return s.charAt(0) === "¥" ? ("<small class='pre'>¥</small>" + s.slice(1)) : s;
      };
      var shortModel = function(name){
        var s = String(name || "");
        return s.length > 16 ? (s.slice(0, 8) + "…" + s.slice(-6)) : s;
      };
      // 副行按类型拆开：回复 / 决策各多少钱，两段加起来就是主值。
      var ck = cost.by_kind || {};
      // v4.68（用户 m02472）：悬停里每个模型再按回复 / 决策拆一次（pricing 新给的字段）。
      // 只列真的有钱进来的那一路——回复轮框架没回模型名，pricing 当隐藏模型跳过了，
      // 这时候写「回复 ¥0」会让人以为回复不要钱，干脆不列。
      var costTip = (cparts.length ? cparts.map(function(p){
        var sp = [];
        if(ck.reply) sp.push("回复 " + money(p.reply_cost || 0));
        if(ck.decision) sp.push("决策 " + money(p.decision_cost || 0));
        var split = sp.length ? "（" + sp.join(" / ") + "）" : "";
        return shortModel(p.model) + " " + (p.priced ? money(p.cost) : "未定价") +
               "（输入 " + Math.max(0, (p.input || 0) - (p.cached || 0)) + " · 缓存 " + (p.cached || 0) +
               " · 输出 " + (p.output || 0) + "）" + split;
      }).join(" · ") : "还没填价目") + " ｜ 单位：" + (cost.unit || "元 / 百万 token");
      var ckRep = (ck.reply && typeof ck.reply.total === "number") ? ck.reply.total : null;
      var ckDec = (ck.decision && typeof ck.decision.total === "number") ? ck.decision.total : null;
      var costSub, costSubHtml = null;
      if(unpriced.length){
        costSub = unpriced.length + " 个模型未定价";
      } else if(ckRep != null || ckDec != null){
        // 没填价的模型算不出钱：写「未定价」而不是 ¥0（回复那一路的模型名框架没回，
        // 一直是「未记录模型」，写 ¥0 会让人以为回复不要钱）。
        var kMoney = function(v, priced){
          if(v == null) return "—";
          return priced ? money(v) : "未定价";
        };
        var ca = "回复 " + kMoney(ckRep, ck.reply && ck.reply.priced);
        var cb = "决策 " + kMoney(ckDec, ck.decision && ck.decision.priced);
        costSub = ca + " / " + cb;
        costSubHtml = seg(ca) + " / " + seg(cb);
      } else {
        costSub = cost.priced ? "按设置页价目估算" : "还没填价目";
      }
      items.push(["最近24小时花费", costTotal == null ? "—" : moneyHtml(costTotal), "", costTip, costSub, costSubHtml]);
    }
    // 最右一格放内存（用户 m28965）：窗口和待发都吃内存，出问题先看这里。
    // 峰值和明细挂 title，格子里只留一位小数，别把统计条撑高。
    var mem = data.mem || {};
    var tot = (typeof mem.total_mb === "number") ? mem.total_mb : null;
    // v4.84（用户 m09179 第 1 条）：右上角那张柱状图去掉，改成卡片右侧的小清单——
    // 一行一项「名字 + 容量」，七类全列出来，比原来「只列前三项 +N 项」看得全。
    // 目录名太技术，清单里用短名（用户 m30252：插件数据要能看出是哪一块占的）。
    // v3.73（用户 m24640 第 2 条 + m25042）：固定 7 类——缓存记录里那五类
    // （图片缓存 / 快照 / 窗口快照 / 消息日志与统计 / 用量账本）加「摘要」和「其他」；
    // 2026-11-18（用户 m13966）：消息日志和会话统计合成一类，用量账本单独一类。
    // v4.00（用户 m10782）：requests 目录里现在既有回复快照也有决策快照，
    // 目录级统计拆不开，清单上就合称「快照」。
    // 用户 m14620：转发图片（fw_）和其他图片（tw_）以前没登记，全落进「其他」，
    // 副行就出现两个「其他」。现在按真实类目登记。
    var MEM_GROUPS = [["图片缓存", "media_cache"], ["转发图片", "forward_cache"],
                      ["其他图片", "media_other"], ["快照", "requests"],
                      ["窗口快照", "windows"], ["消息日志与统计", "logs"],
                      ["用量账本", "ledger"], ["摘要", "summaries"]];
    // 2026-11-18：后端把这一类从 forward_media 改名成 forward_cache，
    // 旧键留作别名，免得旧数据或旧后端落进「其他」。
    var MEM_ALIAS = { "forward_media": "forward_cache" };
    function memKey(p){ return MEM_ALIAS[p.name] || p.name; }
    var PIE_SHORT = { "图片缓存": "图片", "转发图片": "转发", "其他图片": "其他图",
                      "快照": "快照", "窗口快照": "窗口",
                      "消息日志与统计": "日志", "用量账本": "账本", "摘要": "摘要", "其他": "其他" };
    var memGroups = MEM_GROUPS.map(function(g){
      var mb = 0, bytes = 0, cnt = 0;
      (mem.parts || []).forEach(function(p){
        if(memKey(p) === g[1]){ mb += (p.mb || 0); bytes += (p.bytes || 0); cnt += (Number(p.files) || 0); }
      });
      return { name: g[0], mb: mb, bytes: bytes, cnt: cnt, lbl: fmtMb(mb, bytes) };
    });
    var otherMb = 0, otherBytes = 0, otherCnt = 0;
    (mem.parts || []).forEach(function(p){
      var hit = false;
      MEM_GROUPS.forEach(function(g){ if(memKey(p) === g[1]) hit = true; });
      if(!hit){ otherMb += (p.mb || 0); otherBytes += (p.bytes || 0); otherCnt += (Number(p.files) || 0); }
    });
    // 固定 7 项，其他就算是 0 也留着占位，不然目录一变清单的行数就跟着变。
    memGroups.push({ name: "其他", mb: otherMb, bytes: otherBytes, cnt: otherCnt,
                     lbl: fmtMb(otherMb, otherBytes) });
    // v4.86（用户 m11062）：副行只列最大的几类，别把卡片顶得比旁边六张高。
    // 用户 m14620：用真实类目名，尾巴不再拼一个「其他」——
    // 以前「其他」既可能是没登记的最大类目、又可能是剩下的尾巴，于是同时出现两个。
    var memBySize = memGroups.filter(function(g){ return (g.bytes || 0) > 0 || (g.mb || 0) > 0; })
      // 用户 m14620：后端只给 mb、没给 bytes 时也要按大小排，不然顺序会退回类目表顺序。
      .sort(function(a2, b2){
        return ((b2.bytes || 0) - (a2.bytes || 0)) || ((b2.mb || 0) - (a2.mb || 0));
      });
    // 用户 m13028：第三项固定写「其他」，值是除前两类以外的全部缓存合计。
    // 以前列第三大的类目，卡片窄的时候正好被省略号切掉，等于白占一格。
    var memTop = memBySize.slice(0, 2);
    var memRestMb = 0, memRestBytes = 0;
    memGroups.forEach(function(g){
      if(memTop.indexOf(g) < 0){ memRestMb += (g.mb || 0); memRestBytes += (g.bytes || 0); }
    });
    var memHasRest = memRestMb > 0 || memRestBytes > 0;
    // 三项挤在一行：有「其他」时前两项省掉 MB，只在最后写一次，不然第三项会被省略号切掉。
    // 十位以上不留小数（480.1 → 480），给「其他」多留一点宽度；个位数保留一位，免得 0.4 变成 0。
    var memShort = function(mb, bytes){
      var s = fmtMb(mb, bytes);
      return (mb >= 10) ? s.replace(/^(\d+)\.\d(\s*MB)$/, "$1$2") : s;
    };
    var memSub = memTop.map(function(g){
      var lbl = memHasRest ? memShort(g.mb, g.bytes).replace(/\s*MB$/, "") : g.lbl;
      return (PIE_SHORT[g.name] || g.name) + " " + lbl;
    });
    if(memHasRest) memSub.push("其他 " + memShort(memRestMb, memRestBytes));
    memSub = memSub.join(" · ");
    items.push(["插件数据", tot == null ? "—" : tot.toFixed(1), tot == null ? "" : "MB", "",
                tot == null ? "" : memSub, null, ""]);
    var cardsHtml = items.map(function(it){
      // it[5] 是已经拼好的副行 HTML（群/私聊拆两行），没有就走纯文本 it[4]；it[6] 是右侧小清单
      var subHtml = it[5] || (it[4] ? esc(it[4]) : "");
      var kv = "<div class='k'>" + it[0] + "</div><div class='v'>" + it[1] +
               (it[2] ? "<small>" + it[2] + "</small>" : "") + "</div>";
      var sub = subHtml ? "<div class='sub'>" + subHtml + "</div>" : "";
      // 用户 m02889：这些统计卡不再挂悬浮提示（以前鼠标停上去会弹一长段说明）。
      // it[3] 仍留在数据里当注释用，只是不再渲染成 title。
      var tip = "";
      // v4.86（用户 m11062）：卡片不再分「右侧清单」那一种形态，七张卡统一三行。
      return "<div class='card'" + tip + ">" + kv + sub + "</div>";
    }).join("");
    if(cardsHtml !== lastCardsHtml){ lastCardsHtml = cardsHtml; box.innerHTML = cardsHtml; }
    var p = data.plugin || {};
    CFG.threshold = p.trigger_threshold || 0;
    CFG.decision = !!p.decision_enabled;
    CFG.window = !!p.window_enabled;
    CFG.keep = p.window_keep || 0;
    CFG.trim = p.window_trim_threshold || 0;
    CFG.mode = p.trigger_mode || "";
    CFG.enabled = !!p.enabled;
    sEff = {};
    sEta = {};
    sFired = {};
    (data.sessions || []).forEach(function(s){
      if(s.sid && s.eff) sEff[s.sid] = s.eff;
      if(s.sid && s.silent_eta) sEta[s.sid] = s.silent_eta;   // 静默/休兜倒计时的目标时刻
      if(s.sid) sFired[s.sid] = { silent: s.silent_fired_ts || 0, rest: s.rest_fired_ts || 0, hold: s.silent_hold_ts || 0 };
    });
    pluginEnabled = !!p.enabled;
    if(!fromCache){
      setSwitch("sw-plugin", pluginEnabled);
      if(window.__releaseBoot) requestAnimationFrame(function(){ requestAnimationFrame(window.__releaseBoot); });
    }
    document.getElementById("subtitle").textContent =
      (p.enabled ? "" : "插件已停用 · ") + "更新于 " + fmtClock(Date.now() / 1000);
    aliases = data.aliases || {};
  }

  function setSwitch(id, on){
    var n = document.getElementById(id);
    if(!n) return;
    n.classList.toggle("on", !!on);
    if(id === "sw-plugin"){
      try{ localStorage.setItem("mdp_plugin_enabled", on ? "1" : "0"); }catch(e){}
      document.documentElement.classList.toggle("plugin-on", !!on);
    }
  }

  // ---------------------------------------------------------------- 上下文页

  var ctxSessions = [];
  var ctxSid = "";
  var ctxRounds = [];
  var ctxRoundsFull = false;   // 当前 ctxRounds 是不是全量（回复记录页要全量，走势图 100 条就够）
  // v4.73（用户 m02417）：走势图单存一路——它要带上裁切快照，回复记录页不要。
  var ctxTrendRounds = [];
  var ctxRoundPage = 0;      // 请求表格翻到第几页（0 基）
  var ctxDecisionPage = 0;   // 决策表格翻到第几页（0 基，用户 m07111 图3：决策记录也要翻页）
  var CTX_PAGE_SIZE = 20;    // 每页几轮
  var ctxRoundDetails = Object.create(null); // 同会话、同档位下跨页保留详情节点
  var ctxTrims = [];
  // v4.71（用户 m01516 第 1 条）：窗口快照的保留份数（windows_keep，群 / 私聊各一套）。
  // 走势图标题以前拿的是 trace_keep（回复快照上限=100），磁盘上 200 份也写「最近 100 次」。
  var ctxTrimKeep = 0;
  // v4.90（用户 m01133 第 2 条）：回复 / 裁切 / 决策三种快照合成一个「窗口快照」概念，
  // 盘上总共只留 windows_keep 份。这个数由 /requests 的 snap_keep 带回来，
  // 拿不到时退回 ctxTrimKeep（同一个设置，从 /windows 那边来）。
  // v4.90（用户 m01875 第 1 条）：盘上留多少份跟页面上画多少根分开了——这里优先用
  // snap_show（窗口快照显示条数，默认 200），没有这个字段的老后端才退回 snap_keep。
  var ctxSnapKeep = 0;
  // v4.90（用户 m01875 第 1 条）：盘上还剩多少份窗口快照，由 /requests 的 snap_total 带回来。
  // 走势图标题直接用它，跟缓存表那一行「窗口快照」同一个口径——前端自己拼（回复轮 +
  // 决策轮 + 裁切标记）总会因为「判定要回的决策轮不单独成柱」少掉几十份。
  var ctxSnapTotal = 0;
  // v3.99（用户 m10577）：决策记录也画进走势图（决策轮同样是一份窗口快照）
  var ctxDecTrend = [];
  // v4.90（用户 m01875 第 1 条）：判定「要回」的决策轮不单独成柱（由随后的回复轮代表），
  // 但它也是盘上的一份窗口快照。标题要跟缓存表那一行对得上，就得把它数进去。
  var ctxDecFolded = 0;
  // 用户 m00942 第 5 条：走势图折叠开关（默认关）。打开后只画「裁切（橙）」「裁切快照（青）」
  // 和实时柱——凌晨攒到缓冲上限那几刀才是要看的，其余柱子全收起来。
  var TREND_FOLD = false;
  var ctxTrendReq = false;   // 这一代的请求趋势（/requests）已经回来
  var ctxTrendDec = false;   // 这一代的决策趋势（/decision-rounds）已经回来
  var ctxWin = null;
// 点走势图柱子时，用「那一轮请求里的窗口快照」顶替实时窗口（只读，带返回按钮）
var ctxSnap = null;
var ctxSnapRequest = 0;
// 进/出快照时用「视口在面板里的相对位置」换算（grabWinAnchor / restoreWinScroll）。
// 以前拿实时窗口高度当 min-height 顶住，快照内容短就在底部留一大片空白（用户 m42220）。
  // 右栏两页：window = 当前上下文窗口，rounds = 最近请求（选择记在本地）
  // v3.91（用户 m05749）：多了「决策记录」这一页，pane 三态。
  var ctxPane = (function(){
    try{
      var v = localStorage.getItem("mdp_ctx_pane") || "window";
      return (v === "rounds" || v === "decisions") ? v : "window";
    }catch(e){ return "window"; }
  })();
  var lastWinSig = "";   // 正文指纹：内容不变时保留节点、展开与媒体状态
  var lastTrendSig = "";

  // agent 现在在不在跑、跑了多久 → 用下面那个统一的 fmtDur（v3.19 合并，
  // 原来这里还有一份整数版，同作用域重复声明会把新版覆盖掉）。

  // 运行状态常显（用户 m30428）：以前只有忙的时候才出现，平时那一行是空的，
  // 用户看不出这个会话到底在等什么。现在固定三种：未运行 / 运行中 / 出错重试。
  // 最近一次「没触发」的原因 → 人话（v2.82）。用户要的是「为什么没回」，
  // 不是一行永远不变的「未运行 · 等待触发」。
  var MARK_TEXT = {
    counting: function(s){
      if(s.trigger_mode === "probability") return "概率模式 · 上条已计入";
      if(s.trigger_mode === "adaptive") return "自适应模式 · 已攒 " + (s.counter || 0) + " 条";
      var th = s.threshold || 0;
      return "攒数中 " + (s.counter || 0) + (th ? "/" + th : "") + " 条";
    },
    too_short: function(){ return "上条太短，跳过了"; },
    burst_merged: function(){ return "同一个人连发，合并成 1 条"; },
    rest_skip: function(){ return "休息时段，跳过"; },
    backoff_skip: function(){ return "退避中，先不回"; },
    quota_skip: function(){ return "本小时配额满了"; },
    direct: function(){ return "直触消息"; },
    private: function(){ return "私聊必回"; },
    counted: function(){ return "已计入窗口"; }
  };

  // API 秒数是采样值；本地只补接收后的单调时钟间隔，新 busy_since 立刻换轮。
  var stateClocks = {};
  function sampleStateClock(s){
    if(!s || !s.sid) return;
    if(!s.busy){ delete stateClocks[s.sid]; return; }
    stateClocks[s.sid] = {since: s.busy_since, seconds: Math.max(0, Number(s.busy_seconds) || 0), at: performance.now()};
  }
  function busyElapsed(s){
    var c = s && stateClocks[s.sid];
    if(!c || c.since !== s.busy_since) return s.busy_seconds;
    return c.seconds + Math.max(0, performance.now() - c.at) / 1000;
  }
  function stateTag(s){
    // 手动关闭是配置状态，不是故障；绝不据此修改会话开关。
    if(s && (s.muted || (s.eff && s.eff.session_enabled === false))){
      return "<span class='dot idle'></span><span class='idle'>决策未开启</span>";
    }
    if(s && s.busy){
      if(s.busy_since !== undefined && !(s.busy_since > 0))
        return "<span class='dot wait'></span><b class='wait'>运行中 · 准备请求</b>";
      if(s.stalled) return "<span class='dot bad'></span><b class='bad'>运行超时 " + fmtDur(busyElapsed(s)) + "</b>";
      return "<span class='dot ok'></span><b class='ok'>运行中 · 已请求 " + fmtDur(busyElapsed(s)) + "</b>";
    }
    if(s && s.retry_in > 0){
      return "<span class='dot bad'></span><b class='bad'>运行出错 · " + s.retry_in + " 秒后重试" +
             (s.retry_count ? "（第 " + s.retry_count + " 次）" : "") + "</b>";
    }
    if(s && s.queued) return "<span class='dot wait'></span><b class='wait'>排队中</b>";
    if(s && s.last_error){
      return "<span class='dot bad'></span><b class='bad' title='" + esc(s.last_error) + "'>上次出错：" +
             esc(String(s.last_error).slice(0, 18)) + "</b>";
    }
    // last_mark 描述收消息时的计数，不代表现在仍在运行或上一轮未完成。
    return "<span class='dot idle'></span><span class='idle'>等待触发</span>";
  }
  // 左栏那条竖杠只认四档（用户 m03014 第 3 条）：ok 运行中、bad 出错、wait 等待/排队、
  // idle 关掉。判定顺序和 stateTag 一模一样，改一边记得改另一边。
  // 用户 m05205 第 1 条：等待触发也要亮灯（蓝），以前留成透明，卡片上什么都看不见。
  // 用户 m04917 第 2 条：出错必须立刻能看见 —— 所以 retry_in / last_error 提到 busy 之前判，
  // 否则「180 秒卡死 → 看门狗放开 → 2 秒后重试」这段里 busy 一直为真，竖杠就一直绿着。
  function stateLvl(s){
    if(s && (s.muted || (s.eff && s.eff.session_enabled === false))) return "idle";
    if(s && s.retry_in > 0) return "bad";
    if(s && s.busy){
      if(s.busy_since !== undefined && !(s.busy_since > 0)) return "wait";
      return s.stalled ? "bad" : "ok";
    }
    if(s && s.queued) return "wait";
    if(s && s.last_error) return "bad";
    return "wait";   // 等待触发：亮蓝灯（要求见上）
  }
  // 竖杠本身就是那盏灯：运行时绿、等待/排队蓝、出错红，关掉的会话靠 .it.off 压成深灰。
  // 它绝对定位在卡片左边缘，折叠收起 row2 之后仍然看得见运行状态。
  // 用户 m13708 第 2 条：竖杠以前只在整列表重画时换颜色（5 秒一次），开始/结束触发后
  // 要等好几秒才变色。现在每秒那一拍也用同一个判定把 class 与 title 刷一遍，所以把颜色
  // 对应的提示文字拆成独立函数——两处都调它，避免又出现「同一盏灯两套判定」。
  function stateStripeTitle(s, lv){
    if(lv === "ok") return "运行中 · 已请求 " + fmtDur(busyElapsed(s));
    if(lv === "wait") return (s && s.queued) ? "排队中"
                          : (s && s.busy) ? "运行中 · 准备请求" : "等待触发";
    if(lv === "bad") return (s && s.retry_in > 0) ? ("运行出错 · " + s.retry_in + " 秒后重试")
                                  : (s && s.stalled) ? "运行超时" : "上次出错";
    return (s && (s.muted || (s.eff && s.eff.session_enabled === false))) ? "决策未开启" : "等待触发";
  }
  function stateStripe(s){
    var lv = stateLvl(s);
    return "<span class='stripe " + lv + "' title='" + esc(stateStripeTitle(s, lv)) + "'></span>";
  }

  // ---------------------------------------------------------------- 健康条
  var ctxHealth = null;
  var lastHealthSig = "";
  // 看门狗提示是「已经自愈」的通报，不是持续故障：点过「我已知晓」就该消掉。
  // 这里只存已确认到的次数，下次再放开才会重新变橙（用户 m01927）。
  var WD_ACK_KEY = "mdp_wd_ack";
  // v3.75（用户本轮第 3 条）：请求失败且重试也没救回来的横幅。跟看门狗一样，
  // 记的是「已确认到哪个时间点」，同一个失败只提醒一次。
  var FAIL_ACK_KEY = "mdp_fail_ack";

  function healthSub(h){
    var out = [];
    // v3.51（用户 m07385 第 1 条）：运行状态只看「本次运行」——插件启动/热重载起算，
    // 刷新页面不会清零；跨重载的累计数已经去掉（那个数对不上账，用户说没必要）。
    var up = Number(h.uptime) || 0;
    if(up > 0) out.push("本次运行 " + fmtDur(up));
    if(h.last_msg_ago >= 0){
      out.push(h.last_msg_ago < 60 ? (h.last_msg_ago + " 秒前收到消息") : (fmtDur(h.last_msg_ago) + "前收到消息"));
    } else {
      out.push("还没收到消息");
    }
    var trig = h.triggers || 0;
    out.push("本次触发 " + trig + " 轮");
    if(h.delivered) out.push("投递 " + h.delivered + " · 结算 " + (h.finals || 0));
    if(h.watchdog) out.push("看门狗放开 " + h.watchdog + " 次");
    // 用户 2026-10-24 图3：静音跳过不用再报了（条数只增不减，越看越长）；
    // 换成「这一次放开之后到底重试了几次、最后成没成」——那才是要看的东西。
    var wr = h.watchdog_retry || null;
    if(wr){
      var att = Math.max(0, Number(wr.attempts) || 0);
      var tail = wr.outcome === "success" ? "成功"
               : wr.outcome === "failed" ? "未成功" : "还没结论";
      out.push("重试 " + att + " 次 · " + tail);
    }
    return out.join(" · ");
  }

  function renderHealth(h){
    var bar = document.getElementById("health-bar");
    if(!bar || !h) return;
    ctxHealth = h;
    var wr = h.watchdog_retry || null;
    var sig = JSON.stringify([h.enabled, h.decision, h.muted, h.busy, h.delivered, h.finals,
                              h.watchdog, h.msgs, Math.floor((h.last_msg_ago || 0) / 5),
                              h.triggers, Math.floor((h.uptime || 0) / 5), h.uptime > 600,
                              (h.last_fail && h.last_fail.ts) || 0,
                              wr ? [wr.attempts, wr.outcome, wr.status] : 0]);
    if(sig === lastHealthSig) return;
    lastHealthSig = sig;

    var wdAck = 0;
    try { wdAck = parseInt(localStorage.getItem(WD_ACK_KEY) || "0", 10) || 0; } catch(e){ wdAck = 0; }
    var failAck = 0;
    try { failAck = Number(localStorage.getItem(FAIL_ACK_KEY) || 0) || 0; } catch(e){ failAck = 0; }
    var failTs = (h.last_fail && Number(h.last_fail.ts)) || 0;
    var cls = "hbar", head = "", extra = "", fix = "";
    var muted = h.muted || [];
    var busy = (h.busy || []).filter(function(x){ return muted.indexOf(x.sid) < 0; });
    var stalled = busy.filter(function(x){ return x.stalled; });
    if(!h.enabled || !h.decision){
      head = !h.enabled ? "插件未开启" : "决策未开启";
      extra = "当前为手动关闭状态，不是运行故障";
    } else if(stalled.length){
      cls += " bad";
      head = stalled.length + " 个会话卡住";
      extra = "投递后超过 3 分钟没结算，看门狗到点会自动放开";
    } else if(busy.length){
      cls += " ok";
      head = busy.length + " 个会话运行中";
      extra = "请求尚未完成，等待本轮结算";
    } else if(failTs > failAck){
      // v3.75（用户本轮第 3 条）：红色报错 + 重试也没成功 —— 不能只在某个会话卡的
      // 小红字里躺着，翻到别的页签就没人知道了。整条变红 + 一个「我已知晓」。
      cls += " bad";
      var lf = h.last_fail || {};
      var who = String(lf.sid || "").split(":").pop();
      head = "请求失败，重试也没成功";
      extra = (who ? who + " · " : "") + fmtTime(lf.ts) + " · " +
              String(lf.text || "请求失败").slice(0, 40) +
              (Number(lf.retries) > 0 ? "（重试 " + lf.retries + " 次）" : "");
      fix = "<button id='hbar-fail'>我已知晓</button>";
    } else if((h.watchdog || 0) > wdAck){
      cls += " warn";
      head = "看门狗已强制释放 " + h.watchdog + " 次";
      extra = "有轮次投递后没等到结算，已自动恢复";
      // 它不会自己消失，只能让人点掉：给一个明确的「我知道了」
      fix = "<button id='hbar-wd'>我已知晓</button>";
    } else if(h.watchdog > 0){
      // 已确认过的历史次数：整条回到正常态，次数仍留在下面的灰字里
      cls += " ok";
      head = "运行正常";
    } else if((h.msgs || 0) === 0 && !(h.last_msg_ago >= 0) && (h.uptime || 0) > 600){
      cls += " bad";
      head = fmtDur(h.uptime) + " 一条消息都没收到";
      extra = "机器人可能没连上，或者消息根本没到插件";
    } else {
      cls += " ok";
      head = "运行正常";
    }
    var sub = healthSub(h);
    bar.className = cls;
    bar.innerHTML = "<span class='dot'></span><b>" + esc(head) + "</b>" +
      (extra ? "<span class='hs-sub warn2'>" + esc(extra) + "</span>" : "") +
      "<span class='hs-sub'>" + esc(sub) + "</span>" + fix;
  }

  // v3.75（用户本轮第 3 条）：点掉失败横幅，记住这个失败的时间点
  function ackFail(){
    var ts = (ctxHealth && ctxHealth.last_fail && ctxHealth.last_fail.ts) || 0;
    try { localStorage.setItem(FAIL_ACK_KEY, String(ts)); } catch(e){}
    lastHealthSig = "";
    if(ctxHealth) renderHealth(ctxHealth);
  }

  // 点掉看门狗提示：记住当前次数，整条健康条回到正常态（用户 m01927）
  function ackWatchdog(){
    var n = (ctxHealth && ctxHealth.watchdog) || 0;
    try { localStorage.setItem(WD_ACK_KEY, String(n)); } catch(e){}
    lastHealthSig = "";
    if(ctxHealth) renderHealth(ctxHealth);
  }

  // 一键把静音的会话全部恢复：静音最容易看成卡死，得给条明路
  var lastListSig = "";
  function roundResult(r){
    r = r || {};
    var outcome = String(r.outcome || "");
    var error = r.error ? (typeof r.error === "string" ? r.error : JSON.stringify(r.error)) : "";
    if(r.failed || outcome === "failed" || outcome === "error" || error) return { label: "运行出错", cls: "error", tag: "bad", detail: error };
    // v3.75（用户本轮第 5 条）：这不是一次请求，而是「没人说话时顺手做的一次
    // 窗口裁切」单独落的快照。不能算成「未回复」——那样颜色和统计都说错话。
    if(outcome === "trim") return { label: "窗口裁切", cls: "trim", tag: "cut", detail: "" };
    // 用户 m17492 图5/图6：别的插件（如 nai 出图）完成时发来的是通知事件，框架照样跑一整轮。
    // 那一轮不是谁在说话，也不是空回复——单独一档「工具回调」。
    if(outcome === "notice") return { label: "工具回调", cls: "notice", tag: "cut", detail: "这一轮是别的插件发来的工具完成通知（比如出图完成），不是聊天消息触发的请求，也不算空回复。" };
    // 用户 m35302：建议回复、可这一轮从头到尾没等到回复请求（没跑起来，或者跑到一半断了）。
    // 分不清是失败还是空输出，就按失败画一根，别让这一轮在图上凭空消失。
    if(outcome === "decision" && String(r.reply_state || "") === "missing"){
      return {
        label: "失败",
        cls: "error",
        tag: "bad",
        detail: "建议回复之后，这一轮没有对应的回复请求：可能没跑起来，也可能跑到一半断了。分不清是失败还是空输出，按失败算。",
      };
    }
    // v3.99（用户 m10577）：决策轮的快照不是「未回复」，它压根没发请求。
    if(outcome === "decision") return { label: r.decision_reply ? "建议回复" : "建议不回", cls: "dec", tag: "neutral", detail: String(r.decision_reason || "") };
    if(outcome === "pending" || outcome === "running") return { label: "运行中", cls: "pending", tag: "neutral", detail: "" };
    // 用户 m05552 第 1 条：全站不再写「已回复」，统一叫「请求成功」——这一格说的是
    // 请求有没有跑成功，跟「模型决定不回」不是一回事。
    if(r.replied || outcome === "replied") return { label: "请求成功", cls: "ok", tag: "ok", detail: "" };
    // 用户 m02159 图6：模型明明写了话，可它没包在 <msg> 里，框架解析出 0 条消息，
    // 于是这一轮记成「未回复」。标签上要说清是这种没回，别让人以为模型一声不吭。
    if(r.reply_unwrapped){
      return {
        label: "未回复（没包 <msg>）",
        cls: "no",
        tag: "neutral",
        detail: "模型写了话，但没有包在 <msg> 里，框架解析出 0 条消息，所以没发出去。现在会自动补一层壳再发。"
      };
    }
    // 用户 m35302：跑了、但什么都没产出，这是「空回复」，跟「运行出错」分开写。
    return { label: "空回复", cls: "no", tag: "neutral", detail: "这一轮跑完了，但没有产出能发出去的内容。" };
  }
  // 用户 2026-10-24 图4/图5/图7：出错那一行不能只有「运行出错」——后面到底重试了几次、
  // 最后成没成、回复落在哪一份记录，都要在行里直接看得见；点一下能跳到重试那一轮。
  // 这些字段都是后端读取时现算的（请求记录本身是证据，一个字节都没改）。
  function retryBadge(r){
    r = r || {};
    var out = r.retry_outcome || null;
    // 用户 m05552 第 1 条：原轮只写「运行出错」一个标签，重试几次、成没成由下面各自的
    // 重试轮行自己说。这里的汇总信息不丢，挂到标签的 title 上（见 roundResultHtml）。
    if(out){ return ""; }
    var link = r.retry_of || null;
    if(link){
      // 用户 m04917 第 3 条：原来写「补重试 2 次」，既读不懂（这行自己就是那次重试）、
      // 又跟原轮的「重试 2 次」重复报同一个数字。重试轮只说自己是重试轮 + 现在到哪一步。
      var linkRes = roundResult(r);
      // 用户 m05205 第 3 条：每行重试要说清自己是第几次、现在到哪一步，不能两行都只写「重试轮」。
      // 用户 m05828 第 2 条：「重试失败 X1 / X2」里的 X 看不出是第几次，像倍数，去掉；
      // 第几次只留在悬停提示里。
      // 用户 m05459：重试行只看本行自己的结果和序号。链最终成功时，前面失败的那次
      // 仍然是「首次重试」，不能跟着整条链写成「重试成功」。
      var nth = Math.max(0, Number(link.attempt_index) || 0);
      var attempts = Math.max(0, Number(link.attempts) || 0);
      var status = String(link.status || "");
      var rowBad = linkRes.cls === "error";
      var rowPending = linkRes.cls === "pending";
      var rowOk = !rowBad && !rowPending && linkRes.cls !== "trim";
      var finalFail = rowBad && status !== "scheduled" && status !== "running" &&
                      status !== "unknown" && status !== "succeeded" &&
                      !(attempts > 0 && nth > 0 && nth < attempts);
      var t2;
      if(rowOk) t2 = "重试成功";
      else if(rowBad && finalFail) t2 = "重试失败";
      else if(nth === 1) t2 = "首次重试";
      else if(nth === 2) t2 = "二次重试";
      else if(nth > 2) t2 = "第 " + nth + " 次重试";
      else if(status === "scheduled") t2 = "等待重试";
      else t2 = "重试中";
      var tip2 = "这一行是前面那次出错之后自动补的" +
                 (nth ? "（第 " + nth + " 次重试）" : "") +
                 (attempts > 0 ? "，原轮共安排 " + attempts + " 次" : "") +
                 (link.root_name ? "；点开原轮记录" : "");
      if(status === "scheduled") tip2 += "；正在排队";
      else if(status === "running") tip2 += "；正在运行";
      else if(status === "cancelled") tip2 += "；重试已取消";
      else if(status === "unknown") tip2 += "；重试状态未知";
      else if(rowBad && status === "succeeded") tip2 += "；这次没成功，后面补上了";
      else if(finalFail) tip2 += "；没有更多重试了";
      // 用户 m05552 第 1 条：重试轮自己就是一个结果标签（药丸），不再在「运行出错」
      // 后面再挂一行小字——两行都写「运行出错」分不清哪次是哪次。
      var lcls = "dcon" + (rowOk ? "" : rowBad ? " bad" : " wait");
      if(link.root_name){
        return "<a class='" + lcls + "' data-round-link='" + esc(link.root_name) + "' title='" + esc(tip2) + "'>" +
               esc(t2) + "</a>";
      }
      return "<span class='" + lcls + "' title='" + esc(tip2) + "'>" + esc(t2) + "</span>";
    }
    return "";
  }
  // 用户 m04471 第 3 条：快照标题里的「重试几次 / 成没成 / 补回几条」单独算，
  // 标题只负责拼串。重试成功时回复落在重试轮那份记录里，「实际回复」得把那几条补回来，
  // 否则出错那一轮永远显示 0 条（快照本身不改，只在读的时候合并展示）。
  function snapRetryInfo(snap){
    var info = {retry: "", sent: snap ? snap.sent_counted : undefined};
    if(!snap) return info;
    var ro = snap.retry_outcome || null;
    var rh = snap.retry_history || null;
    if(ro){
      var ratt = Math.max(0, Number(ro.attempts) || 0);
      var rok = (ro.succeeded === true);
      var rpend = (ro.succeeded === null || ro.succeeded === undefined);
      info.retry = " · 重试 " + ratt + " 次" +
        (rok ? " 成功" : rpend ? (ro.status === "scheduled" ? " 等待重试" : " 重试中") : " 未成功");
      if(rok && ro.replied){
        var rbn = Math.max(0, Number(ro.reply_bubbles) || 0);
        if(rbn){
          info.sent = (Number(info.sent) || 0) + rbn;
          info.retry += " · 重试补回 " + rbn + " 条";
        }
      }
    } else if(rh && rh.had_error){
      var hatt = Math.max(0, Number(rh.attempts) || 0);
      info.retry = " · 重试 " + hatt + " 次" +
        (rh.status === "succeeded" && rh.succeeded === true ? " 成功" :
         rh.status === "failed" ? " 未成功" : rh.status === "scheduled" ? " 等待重试" : "");
    }
    return info;
  }

  function resultDcon(result){
    var cls = (result && result.cls) || "";
    if(cls === "error") return "dcon bad";
    if(cls === "ok") return "dcon";
    if(cls === "trim") return "dcon cut";
    // 用户 m17492 图5/图6：通知回调轮跟「窗口裁切」一样用青色，别混进灰「空回复」。
    if(cls === "notice") return "dcon cut";
    if(cls === "pending") return "dcon wait";
    if(cls === "dec") return (result.label === "建议回复") ? "dcon" : "dcon no";
    return "dcon no";
  }

  function roundResultHtml(r){
    r = r || {};
    // 重试轮：这一行本身就是某次重试，只写它是第几次、到哪一步（重试成功 / 重试失败 X1）。
    if(r.retry_of) return retryBadge(r);
    var result = roundResult(r);
    var tip = result.detail || "";
    var ro = r.retry_outcome;
    if(ro){
      var att = Math.max(0, Number(ro.attempts) || 0);
      var rt = "原轮共安排 " + att + " 次重试" +
        (ro.succeeded === true ? "，最终补回来了"
         : (ro.succeeded === null || ro.succeeded === undefined) ? "，还没有结论" : "，没补回来");
      tip = tip ? (tip + " · " + rt) : rt;
    }
    return "<span class='" + resultDcon(result) + "'" + (tip ? " title='" + esc(tip) + "'" : "") + ">" + result.label + "</span>";
  }
  // 悬停只补颜色无法表达的信息；重试史绑定原事件，不改变各次真实请求的快照。
  function retryTip(r){
    var history = (r || {}).retry_history || {};
    var result = roundResult(r);
    if(!history.had_error && result.cls !== "error") return "";
    var tip = "报错";
    var attempts = Number(history.attempts);
    if(!history.had_error || !Number.isFinite(attempts) || attempts < 0) return tip;
    tip += " · 重试 " + Math.floor(attempts) + " 次";
    var status = history.status || "unknown";
    if(status === "unknown" && attempts === 0) return "报错"; // 旧记录不具备重试关联，不能把未知显示成重试零次。
    if(status === "succeeded" && history.succeeded === true) tip += " · 重试成功";
    else if(status === "scheduled") tip += " · 等待重试";
    else if(status === "running") tip += " · 重试中";
    else if(status === "cancelled") tip += " · 已取消";
    else if(status === "not_retried") tip += " · 未重试";
    else if(status === "failed" && attempts > 0) tip += " · 重试未成功";
    else if(status === "failed") tip += " · 未重试";
    else tip += " · 结果未知";
    return tip;
  }
  // 最近几轮窗口条数走势：柱高 = 该轮快照条数，蓝=回了、灰=没回、红=出错、橙=裁过，
  // 红虚线 = 裁切阈值。裁切详情只放在鼠标悬停里，不单独占一块地方。
  // v3.59（用户 m17212 图8）：快照多到一屏放不下时，滚轮横向翻历史；不显示滚动条，也不加按钮。
  // v3.62（用户 m18059 第 6 条）：改回「整块横向滚动」。上一版是按柱子一格一格跳，
  // 用户要的是能一路往前翻看更早的快照。柱子改成固定 20px 宽（不再 flex 均分），100 根就撑出
  // 横向溢出了，滚轮直接滚 .trend 的 scrollLeft：向下 = 往右 = 更早，向上 = 往左 = 更新。
  function buildTrend(recs, size, threshold, trims, liveCount, snapName, backHtml, infoHtml, liveTip){
    // 仅在原事件确实在这批记录中时把自动重试收回原柱；请求表与每次快照仍完整保留。
    var rows = (recs || []).slice();
    var rootIds = Object.create(null), retryCuts = Object.create(null);
    rows.forEach(function(r){ if(r.event_id) rootIds[r.event_id] = true; });
    var selectedRoot = "";
    var arr = rows.filter(function(r){
      var history = r.retry_history || {};
      var merged = history.had_error && history.root_event_id && r.event_id &&
                   history.root_event_id !== r.event_id && rootIds[history.root_event_id];
      if(merged){
        if(Array.isArray(r.trims_after)){
          retryCuts[history.root_event_id] = (retryCuts[history.root_event_id] || []).concat(r.trims_after);
        }
        if(r.name === snapName) selectedRoot = history.root_event_id;
      }
      return !merged;
    }).reverse();   // 时间正序；仅归并展示，不修改真实请求记录。
    var liveN = liveCount || 0;
    // v4.90（用户 m01133 第 2 条）：三种快照合成一个「窗口快照」概念，盘上总共只留
    // windows_keep 份。以前回复轮按 trace_keep、决策轮按 decision_keep、
    // 裁切按 windows_keep 各取各的，画出来三百多根，跟缓存表里那一行永远对不上。
    // v4.90（用户 m01875 第 1 条）：画多少根改由「窗口快照显示条数」决定（默认 200），
    // 盘上留 300 份也不会让图上多出一百根。
    // 左边那根实时柱也算一份（它只是还没落盘），所以先给它留一格。
    var _capN = ctxSnapKeep > 0 ? ctxSnapKeep : (ctxTrimKeep > 0 ? ctxTrimKeep : 0);
    if(_capN > 0){
      var _room = Math.max(1, _capN - (liveN > 0 ? 1 : 0));
      if(arr.length > _room) arr = arr.slice(arr.length - _room);
    }
    // 一次请求都还没有的会话（比如刚建、只攒了几条待发）也要能画出那根绿柱：
    // 左栏写着「窗口 3 条」、右边却一片空白，看着像坏了
    if(!arr.length && !(liveN > 0)) return "";
    // 柱高用真实消息条数（一条 user 里可能装着十几条群消息）。
    // 纵轴上限只按这一串记录里的最大值定：老记录没有 window_counted，
    // 拿实时窗口的条数当基准会把它们全压成一条线。
    // v3.21（用户 m42220）：柱子只数用户侧——bot 自己的回复不占窗口位数，
    // 跟「当前 N 条」的口径（用户消息 + 待发）对齐；老记录没有 window_user 就退回含 bot 的旧口径。
    // v4.57（用户 m29312）：决策轮读的是「窗口 + 待发」，柱子以前只数窗口本体，
    // 12:26:40 那根写 14 条用户，可这一轮真正看到的是 20 条；下一轮并窗后又变 20，
    // 就成了「先吞两条、下一轮再补回来」。这里把待发也算进柱高，
    // 悬停里写明拆分（窗口 X + 待发 Y），跟记录详情 ②+③ 的条数对得上。
    var wc = function(r){
      var base = (typeof r.window_user === "number")
        ? r.window_user
        : (r.window_total || r.window_counted || r.window_size || 0);
      var pu = Number(r.window_pending_user) || 0;
      return base + (pu > 0 ? pu : 0);
    };
    var cutAt = Object.create(null);
    arr.forEach(function(r, i){
      var next = (i + 1 < arr.length) ? arr[i + 1].ts : Infinity;
      var c = null;
      // 后端已按 event_id 归属的空数组也是结论，不再拿邻轮时间覆盖。
      if(Array.isArray(r.trims_after)){
        r.trims_after.forEach(function(t){ if(!c || t.ts > c.ts) c = t; });
      }else{
        (trims || []).forEach(function(t){
          if(t.ts >= r.ts && t.ts < next && (!c || t.ts > c.ts)) c = t;
        });
      }
      (retryCuts[r.event_id] || []).forEach(function(t){ if(!c || t.ts > c.ts) c = t; });
      if(c){
        if(!cutAt[i] || c.ts > cutAt[i].ts) cutAt[i] = c;
      }
    });
    var max = 1;
    arr.forEach(function(r, i){
      var v = wc(r);
      var c0 = cutAt[i];
      if(c0){ var ub0 = Number(c0.user_total_before) || 0; if(ub0 > v) v = ub0; }
      if(v > max) max = v;
    });
    if(liveN > max) max = liveN;
    // v3.23（用户 m45332 第4条）：柱高改成只数用户消息以后，历史峰值常常比
    // 裁切阈值低（60），原来的「只在阈值 ≤ 峰值时才画线」会让虚线整条消失，
    // 看着就像被删了。阈值也参与纵轴刻度，线永远画得出来；留 8% 顶部余量，
    // 免得虚线正好压在容器顶边、标签被顶出去。
    var peak = max;
    if(threshold > 0 && threshold >= max) max = Math.ceil(threshold * 1.08);
    var h = 54;
    // 用户 m06348 第 1 条（推翻 m05828 第 3 条的挪位做法规）：橙色必须落在**这一轮自己**
    // 身上——执行裁切的那一轮组装时窗口已经变矮，柱子自然矮，橙柱应贴在它左边那根更早的
    // 高柱右侧（显示顺序左=最新，所以「他们左边的那个」正是裁切轮）。上一轮把 owner
    // 强行减 1，橙柱全跑到前一根高柱上，三条里对一条错两条；实测被标错的几轮与失败
    // 快照无关（近期 failed 轮都没带裁切）。tip 改成「这一轮把窗口裁到 N 条」。
    var barArr = arr.map(function(r, i){
      var isTrim = String(r.outcome || "") === "trim" || r.trim_only === true;
      // 独立裁切快照保留自己的触发来源；原决策过期也不改成普通快照。
      var isDec = String(r.outcome || "") === "decision" ||
                  (isTrim && String(r.trim_trigger || "") === "decision");
      // 独立裁切不再借邻轮的橙标；决策触发的裁切在自身结果里保留来源。
      var cut = isTrim ? null : (cutAt[i] || null);
      // v4.24（用户 m22078）：这一轮触发了裁切时，柱子按「裁切前」的规模站高——
      // 决策轮自己只有 56 条 user，裁切却是 83 条（user 70），柱子和悬浮都该写 70。
      var v = wc(r);
      if(cut){ var ubv = Number(cut.user_total_before) || 0; if(ubv > v) v = ubv; }
      var ph = Math.max(2, Math.round(v / max * h));
      // 用户 m35302：判定要回、却从头到尾没等到回复请求的那一轮，柱子照旧出现，但画成红的。
      // 分不清是失败还是空输出，一律当失败；空回复不是这个分支，它有自己的回复柱子。
      var _missDec = isDec && String(r.reply_state || "") === "missing";
      var result = isDec ? {cls: (_missDec ? "error" : (isTrim ? "dec cut" : "dec"))} : roundResult(r);
      var errorTip = isDec ? "" : retryTip(r);
      if(errorTip) result = {cls: "error"};
      // v3.22（用户 m42531）：柱高只数用户消息，标题里写死这句话，
      // 免得被当成「窗口里一共多少条」；含 bot 的总数在窗口标题那边。
      // v4.57（用户 m29312）：柱高含待发时写明拆分，免得跟记录详情 ② 的条数对不上。
      var pendU = Number(r.window_pending_user) || 0;
      var tip = fmtTime(r.ts) + " · " + v + " 条 [不包含bot消息]" +
                ((pendU > 0 && !cut) ? "（窗口 " + Math.max(0, v - pendU) + " + 待发 " + pendU + "）" : "") +
                (errorTip ? " · " + errorTip : "");
      if(isTrim){
        // v3.88（用户 m05012 第 2/3 条）：柱子高度按这份快照自己的 user 条数（= 裁后）。
        // v3.89（用户 m05321 第 1 条）：快照现在含待发，悬停要写清其中几条是待发。
        // v4.52（用户 m28586 第 6 项）：悬停和柱子改说同一套数字。柱高 v 是「这份
        // 快照里有几条用户消息（含待发）」；裁前裁后各给「计入」与「用户合计」两组，
        // 再点明这次是谁触发的裁切——以前柱高 70、悬停写 67 → 4，一次裁切三个数。
        var tpN = Number(r.trim_pending_count) || 0;
        var cbT = Number(r.trim_counted_before) || 0;
        var caT = Number(r.trim_counted_after) || 0;
        var ubT = Number(r.trim_user_total_before) || 0;
        var uaT = Number(r.trim_user_total_after) || 0;
        var tgT = String(r.trim_trigger || "");
        var whyT = tgT === "before_request" ? "发请求前裁的"
                 : tgT === "decision" ? "决策判完裁的"
                 : tgT === "idle_check" ? "确认没人再说话才裁的"
                 : tgT === "buffer" ? "攒到缓冲线强制的"
                 : "窗口攒超上限了";
        if(cbT || caT || ubT || uaT){
          tip = fmtTime(r.ts) + " · 窗口裁切：" +
                (cbT || caT ? "计入 " + cbT + " → " + caT + " 条" : "") +
                (ubT || uaT ? (cbT || caT ? " · " : "") + "用户 " + ubT + " → " + uaT + " 条" : "") +
                " · 这份快照 " + v + " 条" +
                (tpN ? "（其中待发 " + tpN + " 条）" : "") + " · " + whyT;
        }else{
          tip = fmtTime(r.ts) + " · 窗口裁切 " + (Number(r.trim_before) || 0) + " → " +
                (Number(r.trim_after) || 0) + " 条 · 这份快照 " + v + " 条 [不包含bot消息]" +
                (tpN ? " · 其中待发 " + tpN + " 条" : "") + " · " + whyT;
        }
        // v4.80（用户 2026-11-07 第 3 条）：能单独成柱的青柱，说明触发它的那一轮已经不在
        // 这批记录里了（认领得到的那几刀早折进橙标，连行都不会返回）。绝大多数是被保留
        // 上限删掉的老快照，悬停里必须说清楚，否则看着像「没人说话也裁了一刀」。
        if(tgT === "decision") tip += " · 触发它的决策轮快照已超出保留上限";
        else if(tgT === "before_request") tip += " · 触发它的回复轮快照已超出保留上限";
      }
      if(_missDec) tip += " · 失败：建议回复之后没等到回复请求";
      if(cut){
        var pct = cut.before_chars ? Math.round((1 - (cut.after_chars || 0) / cut.before_chars) * 100) : null;
        // v3.71（用户 m23596 第 3 条）：触发线现在把「待发」也算进来，所以悬停要说清
        // 「窗口内 X + 待发 Y = 合计 Z」——否则 40 条窗口却被裁了，看着像裁错了。
        var pendB = Number(cut.pending_before) || 0;
        var totB = Number(cut.user_total_before) || 0;
        var ubU = Number(cut.user_total_before) || 0;
        var uaU = Number(cut.user_after) || 0;
        // v4.24（用户 m22078）：裁切段的数字与柱高统一按裁切前的规模说，
        // 也不再写「决策后」这种决策信息。
        tip += " ｜ 裁切：窗口 " + cut.before + " → " + cut.after + " 条" +
               (ubU ? " · 用户 " + ubU + (uaU ? " → " + uaU : "") : "") +
               (pendB > 0 && totB > 0 ? " · 其中待发 " + pendB + " 条" : "") +
               (pct === null ? "" : " · 字符减 " + pct + "%");
      }
      var sel = (snapName && (r.name === snapName || (selectedRoot && r.event_id === selectedRoot))) ? " sel" : "";
      // 用户 m00942 第 5 条：折叠开关打开后只留「裁切（橙）」「裁切快照（青）」
      // 和实时柱，其余柱子不画——凌晨那种攒到缓冲上限的裁切快照才是要看的。
      var keepBar = !TREND_FOLD || !!cut || isTrim;
      return { keep: keepBar,
               html: "<span class='bar " + result.cls + (cut ? " cut" : "") + sel +
                     "' style='height:" + ph + "px' title='" + esc(tip) + "'" +
                     (r.name ? " data-trend='" + esc(r.name) + "'" : "") + "></span>" };
    });
    // 从左往右 = 新到旧（用户 m28965）：最左边是「现在」，越往右越早。
    // cut 的归属在上面 map 里已经按 arr[i+1]（时间正序）算完，这里才反转，顺序不影响裁切。
    barArr.reverse();
    var shownBars = 0;
    var bars = barArr.map(function(b){ if(b.keep) shownBars++; return b.keep ? b.html : ""; }).join("");
    // 最左补一根绿柱：此刻实时窗口的条数。点它等于「回到现在」。
    if(liveN > 0){
      var lh = Math.max(2, Math.round(liveN / max * h));
      bars = "<span class='bar live" + (snapName ? "" : " sel") + "' style='height:" + lh + "px'" +
              " title='" + esc((liveTip || "实时窗口") + " · " + liveN + " 条" + (snapName ? "（点一下回到现在）" : "（正在看）")) + "'" +
              " data-live='1'></span>" + bars;
    }
    var line = "";
    var far = "";
    if(threshold > 0){
      // 线画在 .trend 上（不在滚动层里），底部 +1px 对上柱子的底边（.trend-in 的 padding-bottom）
      line = "<div class='tline' style='bottom:" + (Math.round(threshold / max * h) + 1) + "px'><span>裁切阈值 " + threshold + "</span></div>";
      // 真实峰值还没够阈值时补一句「还差几条」（按用户消息数算）
      if(peak < threshold) far = "距裁切阈值 " + threshold + " 还差 " + (threshold - peak) + " 条";
    }
    // 用户 m13708 第 1 条：这根走势图每根柱子就是一份上下文快照，标题也按「设置里的
    // 保留次数」写（不写当前实际有几根），存不满时括号补实际数；措辞从「请求」改成「快照」。
    // v3.59（用户 m17212 图8）：左边那根实时柱也是一次快照（只是还没落盘），算「已有」时要
    // 带上它——否则 99 根 + 实时这根正好一百次，标题却写「已有 99 次」。
    // v3.87（用户 m04715 第 2 条）：这里不能数 arr——arr 把「原轮 + 自动重试轮」并成了一根，
    // 少算的那些快照让 100 份文件永远显示「已有 99 次」。改成数原始 rows（= 磁盘上的快照份数）。
    // v4.73（用户 m02417）：裁切快照重新有了柱子，rows 里也带它们（trim=1），
    // 份数不能把同一份数两遍——行内的裁切行已经在 trims 那一份里了。
    // 用户 m13708 第 1 条：这根走势图每根柱子就是一份上下文快照。
    // v4.77（用户 m06097 选了「不加深，只改标注」）：标题只报「实际画出来几根」+ 最早那份的时间。
    // 之前三处偏差叠在一起：v4.71（m01516）让份数跟「窗口快照上限」走（/windows 的 keep=200，
    // 那是窗口日志的条数、能盖十天）；份数又拿 rows 去掉裁切行再加 ctxTrims.length，而 ctxTrims
    // 是窗口状态里 56 天内的全部裁切事件（实测 264 条），绝大多数早就不该有快照文件了。
    // 结果标题写「最近 200 次快照」甚至「已有 347 次」，图上却只有 163 根柱子，越往右翻越像缺东西。
    // 现在直接数 arr（= 真正成柱的记录，已把「原轮 + 自动重试轮」并成一根），最早时间取 arr[0].ts
    // （arr 是时间正序，第一个就是最右那根，也就是能翻到的最早一份）。
    // v4.90（用户 m01133 第 2 条）：裁切快照画成柱子上的橙色标记，不单独成柱，可它也是
    // 盘上的一份快照。标题要跟缓存表那一行「窗口快照」对得上，就得把标记也数进去——
    // 否则上限 200、盘上 200 份，标题只写 184，用户又要问「怎么少了」。
    var _markSeen = Object.create(null), _markN = 0;
    rows.forEach(function(r){
      if(!Array.isArray(r.trims_after)) return;
      r.trims_after.forEach(function(t){
        var nm = String((t && t.name) || "") || ("@" + (t && t.ts));
        if(_markSeen[nm]) return;
        _markSeen[nm] = 1;
        _markN += 1;
      });
    });
    // v4.90（用户 m01875 第 1 条）：后端带回盘上的份数就直接用它，跟缓存表那一行对得上；
    // 老后端没有 snap_total 才退回前端拼凑。
    // 标题与 DOM 共用同一份口径：只数当前真正画出的柱子。
    // rows、snap_total 与裁切标记来自不同索引，不能再拿它们拼一个会
    // 随刷新返回顺序变化的数字；shownBars 已经经过重试归并、上限截断和折叠筛选。
    var haveN = shownBars + (liveN > 0 ? 1 : 0);
    // 用户 m00942 第 4 条：标题后面不再跟最早那份的时间，那排文字只留份数。
    var trendTitle = haveN ? ("最近 " + haveN + " 次") : "还没有快照记录";
    return "<div class='trend-hd'><span>" + trendTitle + "</span>" +
           (infoHtml || "") + (backHtml || "") + "<span class='grow'></span>" +
           "<span class='trend-lg'>" +
             "<i class='tb ok'></i>回复" +
             "<i class='tb no'></i>没回" +
             "<i class='tb error'></i>出错" +
             "<i class='tb cut'></i>裁切" +
             "<i class='tb trim'></i>快照" +
              "<i class='tb dec'></i>决策" +
             "<i class='tb live'></i>实时" +
             (far ? "<span class='hint'>" + far + "</span>" : "") + "</span></div>" +
           "<div class='trend'><div class='trend-in'>" +
             (bars || (TREND_FOLD ? "<span class='hint' style='padding:2px 4px'>折叠中：这段时间没有裁切或快照柱子</span>" : "")) +
           "</div>" + line + "</div>";
  }

  // 估算渲染后占几行：CSS 的 clip 是 6 行，按字数估比按字符数准
  // （240 字如果是 4 行，按钮点了也不会有变化，用户会以为坏了）
  function estLines(t){
    var n = 0;
    String(t == null ? "" : t).split("\n").forEach(function(l){
      n += Math.max(1, Math.ceil(l.length / 60));
    });
    return n;
  }
  // 群消息基本就一两行，超过 2 行才折（用户 m27270）。字数只是初判，
  // 真正超没超要等渲染完量高度 —— 见 settleFolds()。
  var FOLD_LINES = 2;
  // 折几行由 CSS 定，但「有没有超出」渲染前猜不准：先按初判挂上 clip，
  // 渲完再量一次，没超出的放开并把「展开」按钮删掉，免得按钮点了没反应。
  // 用户 m12643：bot 卡的正文是按行拆开的块级 .qqmd-p，气泡直接 append 到 .mono 末尾
  // 会落在块级 div 后面、只能另起一行（截图里「104 字」孤零零占一行）。有块级文本容器
  // 就塞进最后一个里面，跟 QQMD_TAIL 的做法一致。
  var TAIL_HOSTS = { "qqmd-p": 1, "qqmd-h": 1, "qtx": 1, "mdp": 1, "mdli": 1 };
  function appendTail(el, node){
    var last = el.lastElementChild;
    if(last && last.classList){
      for(var k in TAIL_HOSTS){ if(last.classList.contains(k)){ last.appendChild(node); return; } }
    }
    el.appendChild(node);
  }
  function settleFolds(){
    var els = document.querySelectorAll(".mono.clip, .qbody.clip");
    for(var i = 0; i < els.length; i++){
      var el = els[i];
      if(!el.isConnected || !el.clientHeight) continue;   // 还没布局，下一轮 mutation 再来
      if(el.scrollHeight <= el.clientHeight + 2){
        el.classList.remove("clip");
        var sib = el.nextElementSibling;
        if(sib && sib.classList && sib.classList.contains("mc-tail")){
          // 用户 m12463：折起来时气泡和「展开」一起放在 .mc-tail 里；实际没超行，
          // 气泡（和已经挂上去的「拉黑」）挪回正文末尾，整条尾巴拆掉。
          var tc = sib.querySelector(".chb.count"), tb = sib.querySelector(".mc-blk");
          // 气泡和「拉黑」可能被包在 .mc-tags 里（用户 m12469 防换行），整壳搬走。
          var tw = (tc && tc.parentNode && tc.parentNode.classList && tc.parentNode.classList.contains("mc-tags")) ? tc.parentNode : null;
          if(tw){ appendTail(el, tw); }
          else { if(tc) appendTail(el, tc); if(tb) appendTail(el, tb); }
          if(sib.parentNode) sib.parentNode.removeChild(sib);
        } else if(sib && sib.classList && sib.classList.contains("more") && sib.parentNode){
          sib.parentNode.removeChild(sib);
        }
      }
    }
  }
  var _foldTimer = 0;
  function watchFolds(){
    if(typeof MutationObserver === "undefined") return;
    var ob = new MutationObserver(function(){
      if(_foldTimer) return;
      _foldTimer = setTimeout(function(){ _foldTimer = 0; settleFolds(); }, 80);
    });
    ob.observe(document.body, {childList: true, subtree: true});
  }
  // 正文渲染：先转义，再把 [Emoji 汪汪 (ID: 277)] 这种文字表情换成真图
  // v4.18（用户 m20749 图3）：纯文本 @ 的昵称可能自带空格（「MCP_management service」），
  // 通用 @ 正则遇到空白就停，徽章只包住前半段、后半段落在外面成了普通字。
  // 这里记一份「这个窗口里见过的昵称」，richText 先用它整段替换，再让通用正则兜剩下的。
  var AT_POOL = [];
  // 用户 m10848：昵称池以前只装「这次打开页面后窗口里说过话的人」，@ 的对象没在窗口里
  // 说过话时，带空格的昵称又会被通用正则截成前半段。池子改成存进 localStorage，
  // 见过一次就记住，下次打开页面照样认得。
  var AT_POOL_KEY = "md_at_pool";
  var AT_POOL_MAX = 300;
  var _atPoolTimer = 0;
  function atPoolSave(){
    if(_atPoolTimer) return;
    _atPoolTimer = setTimeout(function(){
      _atPoolTimer = 0;
      try{ localStorage.setItem(AT_POOL_KEY, JSON.stringify(AT_POOL.slice(-AT_POOL_MAX))); }catch(e){}
    }, 500);
  }
  function atPoolAdd(nm, quiet){
    nm = String(nm == null ? "" : nm).trim();
    if(!nm || nm.length > 40) return;
    // 含空白或方括号的昵称，通用 @ 正则吃不全，整段记下来先替换
    if(nm.indexOf(" ") < 0 && nm.indexOf("\u3000") < 0
       && nm.indexOf("[") < 0 && nm.indexOf("]") < 0) return;
    if(AT_POOL.indexOf(nm) >= 0) return;
    AT_POOL.push(nm);
    if(AT_POOL.length > AT_POOL_MAX) AT_POOL.splice(0, AT_POOL.length - AT_POOL_MAX);
    if(!quiet) atPoolSave();
  }
  (function atPoolLoad(){
    try{
      var arr = JSON.parse(localStorage.getItem(AT_POOL_KEY) || "[]");
      if(Array.isArray(arr)) arr.forEach(function(nm){ atPoolAdd(nm, true); });
    }catch(e){}
  })();

  // 用户 m27528 图1：转发卡片里的 [Emoji 344] / [Emoji 笑哭(ID: 182)] 以前只走 esc，
  // 群里好好的表情到了卡片里就是一行方括号。抽成一个函数，正文与转发条目共用。
  function emojiFaces(s){
    var out = String(s == null ? "" : s)
      .replace(/\[Emoji\s+([^\]]*?)\(ID:\s*(\d+)\)\]/g, function(_, nm, id){ return emojiHtml(id, nm); });
    // 框架的 Emoji.repr 有两种：有名字是 [Emoji 叉腰(ID: 21)]，没名字只剩 [Emoji 21]。
    out = out.replace(/\[Emoji\s+(\d+)\]/g, function(_, id){ return emojiHtml(id, ""); });
    // 只有名字、没有 (ID: n) 的那种（[Emoji 微笑]）取不到图，退成灰字，别把方括号露出来
    out = out.replace(/\[Emoji\s+([^\]()]*?)\s*\]/g, function(_, nm){
      nm = String(nm || "").trim();
      return "<span class='emoji miss'><span class='em-txt'>[" + (nm ? "表情·" + nm : "表情") + "]</span></span>";
    });
    // 后端 emoji_repr 拿不到 id/名字时只写个 [Emoji]，别让它以字面量露在正文里
    return out.replace(/\[Emoji\]/g, "<span class='emoji'>[表情]</span>");
  }
  // Display-only native video tokens. Never infer a playable URL or claim image slots.
  function nativeVideoCard(name, reason, compact){
    var title = esc(name || "视频"), why = reason ? "<span class='video-reason'>" + esc(reason) + "</span>" : "";
    var state = "<span class='video-state'>暂不可播放</span>";
    if(compact) return "<span class='qcard video video-compact'><span class='video-icon' aria-hidden='true'>🎬</span>" +
      "<span class='qc-t'>" + title + "</span><span class='video-kind'>原生视频附件</span>" + why + state + "</span>";
    return "<div class='qcard video'><div class='qc-row'><span class='qc-logo' aria-hidden='true'>🎬</span>" +
      "<div class='qc-main'><div class='qc-t'>" + title + "</div><div class='video-kind'>原生视频附件</div></div></div>" +
      "<div class='qc-ft'>" + why + state + "</div></div>";
  }
  function nativeVideoHtml(text, renderText, compact, allowChinese){
    var t = String(text == null ? "" : text);
    // Only an entire protocol-only node can authorize Chinese video tokens.
    if(allowChinese !== false && /^(?:\[视频\]\s*)+$/.test(t.trim())){
      return (t.match(/\[视频\]/g) || []).map(function(){ return nativeVideoCard("", "", compact); }).join("");
    }
    if(!/\[Video (?:name:|attached\])/i.test(t)) return null;
    // Consume code spans/fences before tokens so examples remain literal, including
    // an unclosed fenced block. Keep the source unchanged for raw/mixed and speech.
    var re = /(^|\n)[ \t]{0,3}(\`{3,}|~{3,})[^\n]*(?:\n|$)[\s\S]*?(?:\n[ \t]{0,3}\2[ \t]*(?=\n|$)|$)|(\`+)[\s\S]*?\3(?!\`)|\[Video name:[ \t]*([^\]\r\n]*)\]|\[Video attached\]/gi;
    var out = "", at = 0, found = false, m;
    while((m = re.exec(t))){
      if(m[2] || m[3]) continue;
      var before = t.slice(at, m.index), name = String(m[4] || "").trim(), reason = "";
      // Only this exact historical suffix is a known cache state; filename
      // parentheses (and any unknown suffix) are part of the actual title.
      var known = /\s*\(Video size over 10MB, not cached\)\s*$/i;
      if(known.test(name)){ name = name.replace(known, "").trim(); reason = "大于10MB未缓存"; }
      if(before.trim()) out += renderText(before);
      out += nativeVideoCard(name, reason, compact);
      at = re.lastIndex; found = true;
    }
    if(!found) return null;
    var rest = t.slice(at);
    return out + (rest.trim() ? renderText(rest) : "");
  }
  function richText(t){
    var video = nativeVideoHtml(t, richText, false);
    if(video !== null) return video;
    // 用户第 12 条：正文这条路不经过 tidyBody，QQ markdown mention / 冗余 "@昵称"
    // 前缀得自己先规整一遍，否则群里一个 @ 到窗口里变成两个（图7）
    t = mentionNorm(t);
    var share = plainShareHtml(t, richText);
    if(share !== null) return share;
    // 用户第 4 条：B 站链接（bilibili.com / b23.tv）在正文里只是不可点的纯文本，
    // 这里画成和分享卡同一套的小卡，整块可点（raw 档走 esc，不受影响）
    var bili = biliHtml(t, richText);
    if(bili !== null) return bili;
    // 用户 m18492：GitHub 链接（仓库 / issue / PR / gist / 文件）也画成同款小卡，
    // 骨架先出、fillGitHub() 拿到仓库信息再填描述与 star。
    var gh = githubHtml(t, richText);
    if(gh !== null) return gh;
    // 用户 m12833 第 2 条：推文链接画成卡片（骨架先出，数据到了再填）
    var tw = tweetHtml(t, richText);
    if(tw !== null) return tw;
    var s = emojiFaces(esc(t));
    // v4.18（用户 m20749 图2）：小程序分享的文本前缀（引用体里最常见）别让方括号露在外面
    s = s.replace(/\[(QQ小程序|小程序|QQ音乐|QQ频道)\]/g, "<span class='qmini-tag'>$1</span>");
    // v3.61（用户 m17581 第 1 条）：框架只给了「这里有一条合并转发」的空壳（正文为空、连 [Forward]
    // 标记都没有），后端在生成窗口/快照文本时补了 [合并转发/内容未获取]；这里画成灰底提示条，
    // 不再留一个只有时间戳的空气泡。原文档不走 richText，仍能看到标记本身。
    s = s.replace(/\[合并转发\/内容未获取\]/g, "<span class='fwd-miss'>合并转发 · 内容未获取</span>");
    // QQ 的 @ 有两种长相：框架原样的 [At 1429260473(nickname: 樱井桃华)]，
    // 以及已经转好的 @樱井桃华。只洗一种就会一蓝一灰（用户 m30009 图1），
    // 这里统一成同一个蓝色标签；用占位符中转，免得把刚生成的 <span> 再洗一遍。
    var stash = [];
    function keepAt(name){ stash.push(name); return "\u0000AT" + (stash.length - 1) + "\u0000"; }
    // v4.18（用户 m20749 图3）：带空格的昵称先整段换成占位符，长名字优先，
    // 免得到时候通用正则把「MCP_management」自己吃成徽章、把 service 留在外面。
    if(AT_POOL.length && s.indexOf("@") >= 0){
      AT_POOL.slice().sort(function(a, b){ return b.length - a.length; }).forEach(function(nm){
        var needle = "@" + esc(nm), at = s.indexOf(needle);
        while(at >= 0){
          s = s.slice(0, at) + keepAt(esc(nm)) + s.slice(at + needle.length);
          at = s.indexOf(needle, at + 1);
        }
      });
    }
    // 用户 m10848：像「Mcp_management service」这种「英文名里带下划线/数字 + 一个空格 + 英文词」
    // 的昵称，池子里没有时通用正则只吃到空格前，后半截落在徽章外面。这里先把这种整段收掉。
    s = s.replace(/(^|[^\w\u4e00-\u9fa5\[])@([A-Za-z][A-Za-z0-9_.\-]*[_0-9][A-Za-z0-9_.\-]*) ([A-Za-z][A-Za-z0-9_.\-]{0,23})/g,
      function(_, pre, a, b){ return pre + keepAt(a + " " + b); });
    // 2026-11-01（用户 m06025 图1）：@[ 后面的方括号不一定是包装，可能是昵称本身
    // （[%ĀĀ␇Ö]我在未来，等你）。只有 ] 后面就是空白/标点/结尾时才按包装剥括号；
    // 后面还接着字的，整段到空白为止，再按昵称规则洗掉装饰性前缀。
    // s 已经过 esc，这里拿到的 nm 也是转义后的文本，不要再 esc 一次。
    s = s.replace(/@\[([^\]\n]{1,40})\](?=\s|$|[，。、；：！？,.;:!?])/g, function(_, nm){ return keepAt(nm); });
    s = s.replace(/@(\[[^\]\n]{1,40}\](?!\()[^\s<>@]{1,60})/g, function(_, nm){ return keepAt(cleanNick(nm)); });
    // v4.49（用户 m25038 第 3 项）：还有一种「昵称在前、号在括号里」的写法
    // [At 大椰果(3503680592)]——嵌套引用的原话里就是这种，以前原样漏成方括号不做徽章。
    // 2026-11-01（用户 m06138 图2）：昵称里带空格（Mcp_management service）时旧字符类
    // 在空格处就断了，整段 [At …] 原样漏出来。这里允许空格，lazy 到电话号码前的左括号。
    s = s.replace(/\[At\s+([^\[\]()]{1,40}?)\s*\(\s*-?\d+\s*\)\s*\]/g, function(_, nm){ return keepAt(esc(String(nm).trim())); });
    // 昵称自己带括号时（[At 1(nickname: 白色(^0^)/季节)]）不能用 [^)]，会在第一个 ) 处截断
    s = s.replace(/\[At\s+(\d+)\(\s*nickname:\s*([\s\S]{1,80}?)\)\s*\]/g, function(_, id, nm){ return keepAt(nm); })
         .replace(/\[At\s+(\d+)\]/g, function(_, id){ return keepAt(id); })
         // v4.11（用户 m17596 第 5 条）：纯文本 @ 的昵称字符集原来只吃
         // [汉字/字母/数字/_-]，群名片里带 ʚ 〜 ♡ ɞ 这类符号时整段认不出，
         // 同一条消息里就一蓝一灰（用户截图）。放宽到「非空白、非括号、非 @、
         // 非中文标点」的 1~24 字；逗号句号仍当分隔符，「@某人，你好」不会被整段吃掉。
         .replace(/(^|[^\w\u4e00-\u9fa5\[])@([^\s\[\]@<>，。、；;：:！!？?（）()「」“”"'…]{1,24})/g, function(_, pre, nm){ return pre + keepAt(nm); });
    s = s.replace(/\u0000AT(\d+)\u0000/g, function(_, i){
      return "<span class='at'>@" + stash[+i] + "</span>";
    });
    // v3.59（用户 m17212 图4）：QQ 的 @ 后面常跟着一串空格/全角空格，换成标签后会被撑成
    // 一大段空白，看着像「@名字 后面断开」。这里把标签前后的连续空白压成一个空格。
    return s.replace(/(<span class='at'>[^<]*<\/span>)[ \t\u00a0\u3000]+/g, "$1 ")
            .replace(/[ \t\u00a0\u3000]+(<span class='at'>)/g, " $1");
  }
  // system 提示词是一大段 markdown-lite（## 标题 / - 列表 / 空行分段）。
  // 以前直接塞进 <pre class='mono'> 当原文看，用户 m32956 图4 说「展开后没美化」。
  // system 提示词里还有 **粗体** 和 反引号代码，只按行分块还是满屏星号
  // （用户 m32956 图4 说「展开后没美化，都是原文」）。手写扫描，不用正则，
  // 省得 esc 过的 & 和转义符在正则里打架。
  function inlineMd(t){
    var s = esc(t), out = "", i = 0, n = s.length, BT = String.fromCharCode(96);
    while(i < n){
      if(s.substr(i, 2) === "**"){
        var j = s.indexOf("**", i + 2);
        if(j > i + 2 && j - i < 140){
          out += "<b>" + s.slice(i + 2, j) + "</b>";
          i = j + 2; continue;
        }
      }
      var ch = s.charAt(i);
      if(ch === BT){
        var k = s.indexOf(BT, i + 1);
        if(k > i + 1 && k - i < 220){
          out += "<code class='ic'>" + s.slice(i + 1, k) + "</code>";
          i = k + 1; continue;
        }
      }
      out += ch; i++;
    }
    return out;
  }

  // 用户 m26166：源码里空行隔开的块，美化后也要空一行——模块之间、段落之间
  // 都跟原文一样只有一个空行。空行断开的块加 mdgap，CSS 给一个行高的上边距。
  function mdLite(t){
    var lines = String(t == null ? "" : t).split(/\r?\n/);
    var out = "", buf = [], gap = false;
    function flush(){
      if(!buf.length) return;
      out += "<div class='mdp" + (gap ? " mdgap" : "") + "'>" + inlineMd(buf.join("\n")) + "</div>";
      buf = [];
      gap = false;
    }
    for(var i = 0; i < lines.length; i++){
      var ln = lines[i], m;
      if((m = ln.match(/^\s*#{1,6}\s+(.+?)\s*#*\s*$/))){
        flush();
        out += "<div class='mdh" + (gap ? " mdgap" : "") + "'>" + inlineMd(m[1]) + "</div>";
        gap = false;
        continue;
      }
      if((m = ln.match(/^\s*[-*+]\s+(.*)$/))){
        flush();
        out += "<div class='mdli" + (gap ? " mdgap" : "") + "'>" + inlineMd(m[1]) + "</div>";
        gap = false;
        continue;
      }
      if((m = ln.match(/^\s*(\d+[.)])\s+(.*)$/))){
        flush();
        out += "<div class='mdli" + (gap ? " mdgap" : "") + "'>" + inlineMd(m[1] + " " + m[2]) + "</div>";
        gap = false;
        continue;
      }
      if(!ln.trim()){ flush(); gap = true; continue; }
      buf.push(ln);
    }
    flush();
    return out || "<div class='mdp'>" + inlineMd(t || "") + "</div>";
  }

  // renderer 是给 QQ 官方机器人卡片用的（用户 m12198）：那种消息是 markdown，
  // 要按卡片画（标题 / 粗体 / 代码块 / 指令按钮），普通正文传空走 richText。
  function bodyHtml(text, empty, foldAt, cls, noFold, renderer){
    var t = text || "";
    // empty 传 null 表示「这条本来就没有正文，别补 (空)」——文件卡、分享卡自己
    // 就是正文，底下再挂个 (空) 很怪（用户 m29392 图4）
    if(!t) return empty === null ? "" :
      "<div class='mono body-empty" + (cls ? " " + cls : "") + "'>" + esc(empty || "(空)") + "</div>";
    // 抖音分享口令画成分享卡（用户 m16357 图1）；原文档（rawtxt）必须保持模型收到的原样
    if(cls !== "rawtxt"){
      var video = nativeVideoHtml(t, function(part){ return bodyHtml(part, "", foldAt, cls, noFold); }, false);
      if(video !== null) return video;
      var dyc = douyinCard(t);
      if(dyc) return dyc.html + (dyc.rest ? bodyHtml(dyc.rest, "", foldAt, cls, noFold) : "");
    }
    // 超过 2 行先折起来，点「展开」再看全部——不截断内容，只收版面。
    // 字数只是初判（一行按 60 字估，比真实宽度保守），渲染完 settleFolds() 会纠正。
    // 用户 m12637：机器人卡片的 CSS 是 3 行才折（.mc.botcard .mono.bot-txt.clip），
    // 这里却按 2 行判 —— 3 行的机器人消息先折上、settleFolds 量完又放开，刷新时
    // 会闪一下「展开（N 字）」。阈值跟 CSS 对齐。
    var foldLines = cls === "bot-txt" ? 3 : FOLD_LINES;
    var long = !noFold && estLines(t) > foldLines;
    var foldChars = cls === "rawtxt" ? t.length : visLen(t);
    var lb = cls === "rawtxt" || foldChars ? "展开（" + foldChars + " 字）" : "展开";
    var inner = cls === "rawtxt" ? esc(t) : (renderer ? renderer(t) : richText(t));
    return "<div class='mono" + (cls ? " " + cls : "") + (long ? " clip" : "") + "'>" + inner + "</div>" +
           (long ? "<span class='more' data-more='1' data-label='" + esc(lb) + "'>" + lb + "</span>" : "");
  }

  // 字数/工具气泡要 inline 跟在正文最后一行后面。挂在正文 div 外面就成了块级兄弟，
  // 会掉到下一行并贴左（用户 m31931 红框：气泡该在正文那一行的右边）。
  // 正文为空（只剩文件卡 / 图片 / 分享卡）时，气泡要挂进前面最后一个块元素里，
  // 否则它自己占一行贴在左边（用户 m32094 图1 下面那几条 155 字 / 84 字）
  // 正文本身就是一张分享卡（抖音口令卡、从正文里抽出来的 B 站卡）时，卡片自己就是
  // 一整块，气泡塞进 <a> 里会被拉成卡片底部的一整行、掉到卡片外面去（用户 m24651 图1：
  // 「待发」「计入」两个标签掉在抖音卡下面，B 站卡没有）。这里判出「整段只有一张卡」，
  // 交给 .chip-line 让卡片在左、气泡贴卡片右侧外面，跟 B 站卡同一个长相。
  function cardOnlyHtml(h){
    var s = String(h == null ? "" : h).trim();
    if(!s) return "";
    if(/^<a class='qcard/i.test(s)){
      var j = s.indexOf("</a>");
      if(j < 0) return "";
      return s.slice(j + 4).trim() ? "" : s;
    }
    if(s.indexOf("<div class='qcard") === 0){
      // 数 div 配对，确认这一整段就是一张卡（后面没有别的块）
      var depth = 0, pos = 0, o, c;
      while(pos < s.length){
        o = s.indexOf("<div", pos);
        c = s.indexOf("</div>", pos);
        if(c < 0) return "";
        if(o >= 0 && o < c){ depth++; pos = o + 4; continue; }
        depth--;
        pos = c + 6;
        if(depth <= 0) return s.slice(pos).trim() ? "" : s;
      }
      return "";
    }
    return "";
  }

  // 用户 m13647：X 卡（.qcard.tw）的「待发 / 计入」标签原来挂在卡片下面，看着像跟卡片脱开。
  // 这里认出「正文就是一张 X 卡」的情况，交给 chipInto 把标签放进卡片右上角。
  // 不能把标签塞进 <a> 里：fillTweets() 拿到数据后是整张 <a> 换掉的，塞进去的标签会被一起换没。
  function twCardOnlyHtml(h){
    var s = String(h == null ? "" : h).trim();
    var m = s.match(/^<div class='mono'[^>]*>([\s\S]*)<\/div>$/);
    if(!m) return "";
    var inner = m[1].trim();
    if(inner.indexOf("<a class='qcard tw'") !== 0) return "";
    var j = inner.indexOf("</a>");
    if(j < 0) return "";
    return inner.slice(j + 4).trim() ? "" : inner;
  }

  function chipInto(pre, body, chip, inCard){
    // 用户 m13647：X 卡的标签放进卡片右上角（卡片外面套一层相对定位的壳，标签绝对定位）
    if(chip && inCard){
      var tw = twCardOnlyHtml(body);
      if(tw) return "<div class='tw-host'>" + tw + "<span class='card-chips'>" + chip + "</span></div>";
    }
    // 整段只有一张卡：气泡挂卡片右边，别落进卡片内部/下面
    var sole = chip ? cardOnlyHtml(body) : "";
    if(sole) return "<div class='chip-line'>" + sole + chip + "</div>";
    if(body) return (pre || "") + tailChip(body, chip);
    pre = pre || "";
    // 分享卡（.qcard）是 flex 列容器，气泡塞进去会被拉成整行灰条 —— 卡片在左、气泡贴右下角
    // v4.18（用户 m20749 图1）：判断原来写的是 class='qcard'（带右引号），只认「纯 .qcard」；
    // 位置分享卡是 class='qcard loc'，于是一路掉进 tailChip 的 lastIndexOf("</div>")，
    // 气泡被塞进 flex 列容器里、被 stretch 成整行（待发一行、计入一行拉满）。
    // v4.18 补（用户 m20945 图1）：抖音卡整条是 <a class='qcard dycard'>，当初为了保住 v3.98
    // 那个「插进 </a> 前」被排除在外，结果它在 <a> 里同样被撑成两行整行 —— 一律走 chip-line，
    // 卡片（含 <a>）当 chip-line 的 flex 子项，气泡放在卡片右边，不再进卡片内部。
    // 转发卡（用户 m07185）：卡里可能嵌着 .qcard.video，以前会命中下面那条「pre 里有
    // qcard」的判断，整张转发卡被塞进 .chip-line 的 flex 里缩成 max-content 的窄框
    // （3 条那张只有 250px），没嵌视频的又把气泡塞进卡里，两张转发卡长得不一样。
    // 转发卡一律把气泡放到卡片外面下方，卡片保持通栏。
    if(chip && pre.indexOf("<div class='fwd'") >= 0)
      return pre + "<div class='mc-tail'>" + chip + "</div>";
    if(chip && pre.indexOf("class='qcard") >= 0)
      return "<div class='chip-line'>" + pre + chip + "</div>";
    return tailChip(pre, chip);
  }

