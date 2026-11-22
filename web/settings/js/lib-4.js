  function pricingCalSpanText(){
    var s = pricingCal.start, e = pricingCal.end;
    if(s && e) return pdSpanText(s, e);
    return s ? (pdShort(s) + " 起") : "点日期选区间";
  }
  // 用户 m29947：点开面板时把当前区间灌进日历，左边那个月是「相关月份的前一个月」。
  function seedPricingCal(target, start, end){
    pricingCal.target = target || "trend";
    pricingCal.start = start || "";
    pricingCal.end = end || "";
    var base = pdParse(start || "") || pdToday();
    var prev = new Date(base.getFullYear(), base.getMonth() - 1, 1);
    pricingCal.ym = prev.getFullYear() + "-" + ("0" + (prev.getMonth() + 1)).slice(-2);
  }
  // 用户 m29947：右上角只留一个按钮（显示当前区间），点开才是快捷区间 + 两个日历。
  function pricingTrendRangeLabel(){
    if(pricingTrendMode === "custom" && pricingCustom.start && pricingCustom.end)
      return pdSpanText(pricingCustom.start, pricingCustom.end);
    if(pricingTrendMode === "thisweek") return "本周";
    if(pricingTrendMode === "lastweek") return "上周";
    if(pricingTrendMode === "month") return "本月";
    if(pricingTrendMode === "lastmonth") return "上月";
    return "默认";
  }
  function pricingTrendRangeHtml(){
    return "<button type='button' class='prb pdp-btn" + (pricingCal.open ? " on" : "") +
           "' data-trange='1'>" + esc(pricingTrendRangeLabel()) + " ▾</button>";
  }
  function pricingTrendPopHtml(){
    var opts = [{ k: "default", t: "默认" }, { k: "thisweek", t: "本周" }, { k: "lastweek", t: "上周" },
                { k: "month", t: "本月" }, { k: "lastmonth", t: "上月" }];
    return "<div class='pdp pdp-trend'>" +
      "<div class='pdp-q'>" + opts.map(function(o){
        return "<button type='button' class='pdp-qb" + (pricingTrendMode === o.k ? " on" : "") +
               "' data-prange='" + o.k + "'>" + o.t + "</button>";
      }).join("") + "</div>" +
      pricingCalPairHtml() +
      "<div class='pdp-f'><span class='pdp-span'>" + esc(pricingCalSpanText()) + "</span>" +
      "<button type='button' class='pdp-ok' data-cal-apply='1'>应用</button></div>" +
      "</div>";
  }

  // 环形扇区：外圈 r、内圈 ir，从 a1 扫到 a2（弧度，0 在 3 点方向）。
  function pieArc(cx, cy, r, ir, a1, a2){
    var large = (a2 - a1) > Math.PI ? 1 : 0;
    var pt = function(rad, ang){
      return (cx + rad * Math.cos(ang)).toFixed(2) + " " + (cy + rad * Math.sin(ang)).toFixed(2);
    };
    return "M" + pt(r, a1) + "A" + r + " " + r + " 0 " + large + " 1 " + pt(r, a2) +
           "L" + pt(ir, a2) + "A" + ir + " " + ir + " 0 " + large + " 0 " + pt(ir, a1) + "Z";
  }

  // 用户 m21274：柱子改成按功能——决策 / JEV 决策 / 回复各自的 token 用量，柱顶写数字；
  // 折线还是当天的总花费，右边单独一根价格轴；价格标签永远在点上方。
  var FN_KEYS = ["decision", "jev", "reply"];
  var FN_COLORS = { decision: "#3b82f6", jev: "#8b5cf6", reply: "#10b981" };
  // 用户 m22998：一根柱子按 输入 / 命中 / 输出 从下往上拼三段，色相不变，靠纯度和亮度区分；
  // 左下角图例不另加这三段（用户说不用加注释），还是只标 决策 / JEV 决策 / 回复。
  var FN_SEG = {
    decision: { input: "#3b82f6", cached: "#a5c9fd", output: "#1d4ed8" },
    jev: { input: "#8b5cf6", cached: "#cbb8fd", output: "#6d28d9" },
    reply: { input: "#10b981", cached: "#7ce7c4", output: "#047857" }
  };
  var FN_LABELS = { decision: "决策", jev: "JEV 决策", reply: "回复" };

  // 用户 m29478：折线换成曲线——Catmull-Rom 转三次贝塞尔，控制点夹在绘图区里，不会甩出上下边界。
  function smoothLinePath(pts, top, bottom){
    if(!pts || pts.length < 2) return "";
    var d = "M" + pts[0][0].toFixed(1) + " " + pts[0][1].toFixed(1);
    for(var i = 0; i < pts.length - 1; i++){
      var p0 = pts[i - 1] || pts[i], p1 = pts[i], p2 = pts[i + 1], p3 = pts[i + 2] || p2;
      var t = 0.16;
      var c1x = p1[0] + (p2[0] - p0[0]) * t, c1y = p1[1] + (p2[1] - p0[1]) * t;
      var c2x = p2[0] - (p3[0] - p1[0]) * t, c2y = p2[1] - (p3[1] - p1[1]) * t;
      if(c1y < top) c1y = top;
      if(c1y > bottom) c1y = bottom;
      if(c2y < top) c2y = top;
      if(c2y > bottom) c2y = bottom;
      d += "C" + c1x.toFixed(1) + " " + c1y.toFixed(1) + " " + c2x.toFixed(1) + " " + c2y.toFixed(1) +
           " " + p2[0].toFixed(1) + " " + p2[1].toFixed(1);
    }
    return d;
  }

  // 用户 m29947：记分牌式翻字——只让变了的那个字上下滚，没变的原地不动。
  var _rollPrev = {}, _measCtx = null, _svgFontFam = "";
  function _rollFontFam(){
    if(_svgFontFam) return _svgFontFam;
    try{
      var el = document.querySelector("#p-charts .pchart-svg");
      if(el){
        var f = getComputedStyle(el).fontFamily || "";
        if(f) _svgFontFam = f;
      }
    }catch(e){}
    return _svgFontFam || "sans-serif";
  }
  function _rollMeasure(txt){
    if(!_measCtx){ _measCtx = document.createElement("canvas").getContext("2d"); }
    _measCtx.font = "12px " + _rollFontFam();
    return _measCtx.measureText(txt).width;
  }
  function _rollChars(txt, prev){
    var out = [];
    for(var i = 0; i < txt.length; i++){
      var ch = txt.charAt(i);
      var same = i < prev.length && prev.charAt(i) === ch;
      var up = i < prev.length ? (ch > prev.charAt(i)) : true;
      out.push({ ch: ch, same: same, up: up });
    }
    return out;
  }
  function svgRollText(key, txt, x, y, anchor, cls){
    var prev = _rollPrev[key];
    _rollPrev[key] = txt;
    if(prev === undefined || prev === txt || !pricingAnimDir){
      return "<text class='" + cls + "' x='" + x + "' y='" + y + "' text-anchor='" + anchor + "'>" + esc(txt) + "</text>";
    }
    var total = _rollMeasure(txt);
    var cx = anchor === "end" ? x - total : (anchor === "middle" ? x - total / 2 : x);
    var out = "";
    _rollChars(txt, prev).forEach(function(c){
      var w = _rollMeasure(c.ch);
      if(c.ch !== " "){
        out += "<text class='" + cls + (c.same ? "" : (" droll" + (c.up ? "" : " dn"))) +
               "' x='" + cx.toFixed(2) + "' y='" + y + "' text-anchor='start'>" + esc(c.ch) + "</text>";
      }
      cx += w;
    });
    return out;
  }
  function htmlRollText(key, txt){
    var prev = _rollPrev[key];
    _rollPrev[key] = txt;
    if(prev === undefined || prev === txt || !pricingAnimDir) return esc(txt);
    return _rollChars(txt, prev).map(function(c){
      if(c.ch === " ") return " ";
      return "<span class='droll" + (c.same ? " still" : (c.up ? "" : " dn")) + "'>" + esc(c.ch) + "</span>";
    }).join("");
  }

  function pricingBarChartHtml(width){
    var d = pricingTrend;
    if(!d) return "<div class='pchart-empty'>正在统计…</div>";
    if(d.error) return "<div class='pchart-empty'>图表读取失败：" + esc(d.error) + "</div>";
    var pts = d.points || [];
    if(!pts.length) return "<div class='pchart-empty'>这段时间还没有用量。</div>";
    // 用户 m21274：图再大一点；左右两根轴都要留够地方放刻度。
    var W = Math.max(520, Math.round(width || 900)), H = 440;
    var padL = 62, padR = 66, padT = 34, padB = 38;
    var plotW = W - padL - padR, plotH = H - padT - padB;
    // 用户 m22998：柱高按 输入 + 命中 + 输出 三段之和算（以前只算输入 + 输出，命中没进柱子）。
    // 用户 m23442：kinds.input 是「含命中的总输入」，cached 只是它里面的一部分。
    // 以前把 cached 又加了一遍，柱高多算一截、柱底命中率也跟着偏小。
    var tokOf = function(p, k){
      var b = ((p.kinds || {})[k] || {});
      return (Number(b.input) || 0) + (Number(b.output) || 0);
    };
    var tokMax = 0, dayMax = 0, costMax = 0, tokTotal = 0, costTotal = 0;
    pts.forEach(function(p){
      // 用户 m23442：峰值看的是「这一天几根柱子加起来」最高的那天，不是最高的那一根柱子。
      var daySum = 0;
      FN_KEYS.forEach(function(k){ var tv = tokOf(p, k); daySum += tv; tokMax = Math.max(tokMax, tv); tokTotal += tv; });
      if(daySum > dayMax) dayMax = daySum;
      var pv = Number(p.cost) || 0;
      costMax = Math.max(costMax, pv);
      costTotal += pv;
    });
    // 用户 m29889：左轴固定 1M 一段，顶 = 最高的那根柱子向上取整到 1M 再多 0.3M。
    // 多出来的 0.3M 只是给柱顶数字留位置，本身不画刻度（用户 m29908 第 1 条）。
    var tokTop = tokMax > 0 ? Math.ceil(tokMax / 1e6) * 1e6 + 0.3e6 : 1e6;
    // 右轴固定 0.25 一段，顶 = 当天最高花费向上一档取整。
    var costTop = costMax > 0 ? Math.floor(costMax / 0.25) * 0.25 + 0.25 : 0.25;
    var slot = plotW / pts.length;
    // 用户 m21592：一天里的几根柱子并在一起、不留缝；只在「天」之间留一点空隙，
    // 不然一整排连成墙，看不出哪天是哪天。
    // 用户 m23216：柱图铺满整行以后柱子能宽一截，柱底才写得下两行字；
    // 一天有几根柱子就按几根分槽，只有一根的日子柱子不会被挤成三分之一。
    // 用户 m29560 第 8 条：天与天之间的缝收小（0.86 → 0.94），柱子还能再宽一点。
    var bwWide = Math.min(38, (slot * 0.94) / 3);
    var dense = bwWide < 11;
    // 用户 m22178：「按周」那档的横轴是每周的周一，柱子和悬浮里补一句「那周」。
    var wk = !!(d && d.range === "week");
    // 用户 m29947：数字不再一个个翻进来——翻窗口时整块平移，只有变了的字才滚。
    var animOn = false;
    var numCls = "";
    var numDelay = function(ms){ return ""; };
    var grid = "", bars = "", dots = "", lineDots = "", labels = "", linePts = [];
    // 用户 m29889：左轴 1M 一段，网格线和刻度只画在整 M 上，顶上那 0.3M 不画。
    for(var tv = 0; tv < tokTop; tv += 1e6){
      var gy = (padT + plotH * (1 - tv / tokTop)).toFixed(1);
      grid += "<line class='pgrid' x1='" + padL + "' y1='" + gy + "' x2='" + (W - padR) + "' y2='" + gy + "'/>" +
              "<line class='ptick' x1='" + (padL - 6) + "' y1='" + gy + "' x2='" + padL + "' y2='" + gy + "'/>" +
              "<text class='pylab' x='" + (padL - 8) + "' y='" + (Number(gy) + 4).toFixed(1) + "' text-anchor='end'>" +
              esc(fmtBig(tv)) + "</text>";
    }
    // 右轴 0.25 一段，步长跟左轴对不上，所以只在自己的位置画刻度、不画网格线。
    // 一分钱花费都没有的时候，右边那根轴画出来是假的 0~1 元，只留底下的 0.00。
    for(var ci = 0; ci * 0.25 <= costTop + 1e-9; ci++){
      var cv = ci * 0.25;
      if(costMax <= 0 && cv > 0) break;
      var cy2 = (padT + plotH * (1 - cv / costTop)).toFixed(1);
      grid += "<line class='ptick' x1='" + (W - padR) + "' y1='" + cy2 + "' x2='" + (W - padR + 6) + "' y2='" + cy2 + "'/>" +
              "<text class='pylab' x='" + (W - padR + 8) + "' y='" + (Number(cy2) + 4).toFixed(1) + "' text-anchor='start'>" +
              esc(priceMoneyText(cv)) + "</text>";
    }
    // 左右两根轴线：只写数字看着像「干写」，补上轴线和刻度才像图（用户 m21592）。
    grid += "<line class='paxis' x1='" + padL + "' y1='" + padT + "' x2='" + padL + "' y2='" + (padT + plotH) + "'/>" +
            "<line class='paxis' x1='" + (W - padR) + "' y1='" + padT + "' x2='" + (W - padR) + "' y2='" + (padT + plotH) + "'/>";
    // 用户 m06021：橙色花费曲线是后画的，会正好压在柱底那两行白字上（白字压在橙线上就看不见了）。
    // 这里按「曲线在标签横向范围内的高度」判断有没有撞上：linePts 里只有前面几根柱子的点
    // （当前这根还没 push），所以当前点和下一个点要自己补；两头没有点的时候按相邻两点的斜率
    // 往外延一格，跟后面画曲线时补左右两端的做法一致。
    var curveNearPts = function(i, cx, cy){
      var cp = linePts.length ? [linePts[linePts.length - 1]] : [];
      cp.push([cx, cy]);
      if(i + 1 < pts.length){
        cp.push([cx + slot, padT + plotH - plotH * (Number(pts[i + 1].cost) || 0) / costTop]);
      }
      if(cp.length < 2){ return cp; }
      var c0 = cp[0], c1 = cp[1];
      var k0 = (c1[1] - c0[1]) / ((c1[0] - c0[0]) || 1);
      cp.unshift([cx - slot, Math.max(padT, Math.min(padT + plotH, c0[1] - k0 * slot))]);
      // 用户 m06021：最后一天要是「今天」，曲线不往右延伸，别凭空多算一段。
      var extR = i + 1 < pts.length ||
        String((pts[pts.length - 1] || {}).label || "") !== pdShort(pdFmt(pdToday()));
      if(extR){
        var cn = cp[cp.length - 1], cm = cp[cp.length - 2];
        var kn = (cn[1] - cm[1]) / ((cn[0] - cm[0]) || 1);
        cp.push([cx + slot * 2, Math.max(padT, Math.min(padT + plotH, cn[1] + kn * slot))]);
      }
      return cp;
    };
    var curveHitBox = function(i, cx, cy, box){
      var cp = curveNearPts(i, cx, cy);
      if(cp.length < 2){ return false; }
      var mn = Infinity, mx = -Infinity;
      for(var q = 0; q < cp.length - 1; q++){
        var a = cp[q], b = cp[q + 1];
        if(b[0] < box.l || a[0] > box.r){ continue; }
        var x1 = Math.max(box.l, a[0]), x2 = Math.min(box.r, b[0]);
        if(x2 < x1){ continue; }
        var dy = (b[1] - a[1]) / ((b[0] - a[0]) || 1);
        var y1 = a[1] + dy * (x1 - a[0]), y2 = a[1] + dy * (x2 - a[0]);
        mn = Math.min(mn, y1, y2);
        mx = Math.max(mx, y1, y2);
      }
      // 用户 m06021：曲线有 2px 描边，Catmull-Rom 转贝塞尔还会稍微鼓出去一点，留 3px 余量。
      return mx >= box.t - 3 && mn <= box.b + 3;
    };
    var step = Math.max(1, Math.ceil(pts.length / 16));
    pts.forEach(function(p, i){
      var cx = padL + slot * (i + 0.5);
      // 没有数据的那根柱子（比如 JEV 从来没跑过）不占位置：留着空档会看成「柱子之间有缝」。
      var live = FN_KEYS.filter(function(k){ return tokOf(p, k) > 0; });
      var bw = Math.max(3.5, Math.min(38, (slot * 0.94) / Math.max(1, live.length)));
      var groupW = live.length * bw;
      var x0 = cx - groupW / 2;
      var tallest = 0, tallestK = "";
      live.forEach(function(k){ if(tokOf(p, k) > tallest){ tallest = tokOf(p, k); tallestK = k; } });
      var cost = Number(p.cost) || 0;
      var cy = padT + plotH - plotH * cost / costTop;
      // 用户 m22009：总价标签永远贴着点（不挪位置），柱子上的钱跟它撞上就不写——悬浮里还有，
      // 免得同一小块地方挤两个数，也免得总价被推到离点老远的地方。
      var costLabTxt = priceMoneyText(cost);
      var costLabW = costLabTxt.length * 6.8 + 4;
      var costLabTop = cy - 9 - 13, costLabBot = cy - 9 + 3;
      // 用户 m23216：柱底的字跟着每根柱子走（自己的 token + 自己的命中率），不再写当天合计。
      var pinHtml = "", pinRects = [];
      var labCand = [];
      live.forEach(function(k, j){
        var v = tokOf(p, k);
        var kb = (p.kinds || {})[k] || {};
        var kcost = Number(kb.cost) || 0;
        var x = x0 + j * bw;
        var h = Math.max(1.5, plotH * v / tokTop);
        var y = padT + plotH - h;
        // 用户 m22998：从下往上 输入 → 命中 → 输出，三段严丝合缝拼成一根柱子。
        var segCol = FN_SEG[k] || {};
        // 输入段扣掉命中的那部分，三段加起来正好是「总输入 + 输出」，命中不会被画两遍。
        var segUnc = Math.max(0, (Number(kb.input) || 0) - (Number(kb.cached) || 0));
        var segs = [
          { key: "input", n: segUnc },
          { key: "cached", n: Number(kb.cached) || 0 },
          { key: "output", n: Number(kb.output) || 0 }
        ];
        var segSum = 0;
        segs.forEach(function(sg){ segSum += sg.n; });
        if(segSum <= 0){ segSum = v || 1; }
        var accH = 0;
        segs.forEach(function(sg){
          if(sg.n <= 0){ return; }
          var sh = h * sg.n / segSum;
          var sy = padT + plotH - accH - sh;
          bars += "<rect class='pbar' x='" + x.toFixed(1) + "' y='" + sy.toFixed(1) + "' width='" + bw.toFixed(1) +
                  "' height='" + (sh + 0.35).toFixed(1) + "' fill='" + (segCol[sg.key] || FN_COLORS[k]) + "'></rect>";
          accH += sh;
        });
        // 用户 m23216：这根柱子自己的 token 和自己的命中率，横着写在这根柱子底部。
        // 柱子太窄、两行字放不下就不写（窄的时候柱顶还有钱，悬浮里也还有数）。
        var moneyInPin = false;
        if(v > 0){
          // 命中率 = 命中 / 总输入，跟价目表「缓存命中率」那一列同口径（用户 m23442：之前算得不对）。
          var pHit = Number(kb.cached) || 0;
          var pIn = Number(kb.input) || 0;
          var pL1 = fmtBig(v);
          var pL2 = (pIn > 0 ? Math.min(100, pHit * 100 / pIn) : 0).toFixed(1) + "%";
          // labTextWidth 是按 11px 的柱顶金额估的，这两行更小，量出来大概八折。
          var pW = Math.max(labTextWidth(pL1), labTextWidth(pL2)) * 0.8;
          // 用户 m29560 第 5 条：门槛从 30px 降到 25px——两行字本身只要 25px，
          // 11-14 那根回复柱 27px 高，以前正好被挡掉，看着像「那天没数据」。
          // 但 27px 的柱子塞 12px 两行字会顶出柱顶（白字压到卡片底色上就看不见了），
          // 所以矮柱子换成 10.5 / 9.5px 的紧凑版，整块字仍然落在柱子里面。
          var pinSm = h < 30;
          var pCx = x + bw / 2;
          // 用户 m06021：柱内标签被曲线压住时改走柱顶那套（pinTop），pinCurve 记一下是从曲线挪上来的。
          var pinTop = false, pinCurve = false;
          if(pW <= bw - 1 && h >= (pinSm ? 22 : 25)){
            var pY = padT + plotH - (pinSm ? 16 : 18);
            var pGap = pinSm ? 10 : 12;
            var pBox = { l: pCx - pW / 2 - 1, r: pCx + pW / 2 + 1, t: pY - (pinSm ? 9 : 11), b: pY + (pinSm ? 13 : 14) };
            var pClash = cost > 0 && pBox.r > cx - costLabW / 2 - 2 && pBox.l < cx + costLabW / 2 + 2 &&
                         pBox.b > costLabTop - 2 && pBox.t < costLabBot + 2;
            // 用户 m06021：曲线从标签框里穿过去就当作碰撞，不在柱里写了，改浮到柱顶上方。
            var pCurve = !pClash && curveHitBox(i, cx, cy, pBox);
            if(!pClash && !pCurve){
              pinRects.push(pBox);
              var pinCls = pinSm ? " pinlab-sm" : "";
              pinHtml += "<text class='pinlab" + pinCls + numCls + "' x='" + pCx.toFixed(1) + "' y='" + pY.toFixed(1) +
                         "' style='" + numDelay(i * 14 + j * 26) + "' text-anchor='middle'>" +
                         esc(pL1) + "</text>" +
                         "<text class='pinlab pinlab2" + pinCls + numCls + "' x='" + pCx.toFixed(1) + "' y='" + (pY + pGap).toFixed(1) +
                         "' style='" + numDelay(i * 14 + j * 26 + 40) + "' text-anchor='middle'>" +
                         esc(pL2) + "</text>";
            }else if(pCurve || pClash){
              // 用户 m11919：撞上总价标签时以前什么都不画——11-18 那天花费只有 0.01，
              // 总价标签正好落在柱底，两根柱子的 token 和命中率全被吞掉，图上只剩钱。
              // 撞总价和撞曲线一样，都抬到柱顶去写（pinCurve 让它在柱顶再躲一次总价）。
              pinTop = true;
              pinCurve = true;
            }
          }else if(pW <= bw - 1){
            pinTop = true;
          }
          if(pinTop){
            // 用户 m02330：柱子矮到两行字塞不进去（JEV 那根常常只有一两像素高），以前的处理是
            // 干脆不写，于是那根柱子上永远没有数字。现在挪到柱子顶上写，字换成正文色
            // （柱子外面是卡片底色，白字贴上去看不见）。
            // 用户 m02722：顶出去以后按「价格 / 用量 / 命中」从上往下排，钱在最上面。
            // 用户 m03497：整摞贴着柱顶，跟常规价格标签一样只留 4px 空隙，不再悬在半空。
            var pGapO = 11;
            var kLabO = barCostLabel(kb);
            var moneyO = (!dense || k === tallestK) && kLabO !== costLabTxt;
            var pBoxW = Math.max(pW, moneyO ? labTextWidth(kLabO) * 0.95 : 0);
            var pYo = y - 15;
            // 用户 m06021：被曲线压下来的时候，柱顶上方常常正好是总价标签——把这两行再抬到总价标签
            // 上面去，别把字藏掉（pBoxO 底边是 pYo + 13，这里留 2px 空隙）；抬完要是顶到绘图区上边，
            // 就贴着 padT 放，不画到轴外面。
            if(pinCurve){
              var pBoxT = { l: pCx - pBoxW / 2 - 1, r: pCx + pBoxW / 2 + 1, t: moneyO ? pYo - 22 : pYo - 10, b: pYo + pGapO + 2 };
              if(cost > 0 && pBoxT.r > cx - costLabW / 2 - 2 && pBoxT.l < cx + costLabW / 2 + 2 &&
                 pBoxT.b > costLabTop - 2 && pBoxT.t < costLabBot + 2){
                pYo = costLabTop - 17;
              }
              pYo = Math.max(pYo, padT + (moneyO ? 24 : 12));
            }
            var pBoxO = { l: pCx - pBoxW / 2 - 1, r: pCx + pBoxW / 2 + 1, t: moneyO ? pYo - 22 : pYo - 10, b: pYo + pGapO + 2 };
            var pClashO = cost > 0 && pBoxO.r > cx - costLabW / 2 - 2 && pBoxO.l < cx + costLabW / 2 + 2 &&
                          pBoxO.b > costLabTop - 2 && pBoxO.t < costLabBot + 2;
            if(!pClashO && pBoxO.t >= padT + 2){
              pinRects.push(pBoxO);
              if(moneyO){
                moneyInPin = true;
                pinHtml += "<text class='pval" + numCls + "' x='" + pCx.toFixed(1) + "' y='" + (pYo - 12).toFixed(1) +
                           "' style='fill:" + (FN_COLORS[k] || "var(--muted)") + ";" + numDelay(i * 14 + j * 26) + "' text-anchor='middle'>" +
                           esc(kLabO) + "</text>";
              }
              pinHtml += "<text class='pinlab pinlab-sm pinlab-out" + numCls + "' x='" + pCx.toFixed(1) + "' y='" + pYo.toFixed(1) +
                         "' style='" + numDelay(i * 14 + j * 26) + "' text-anchor='middle'>" +
                         esc(pL1) + "</text>" +
                         "<text class='pinlab pinlab2 pinlab-sm pinlab-out" + numCls + "' x='" + pCx.toFixed(1) + "' y='" + (pYo + pGapO).toFixed(1) +
                         "' style='" + numDelay(i * 14 + j * 26 + 40) + "' text-anchor='middle'>" +
                         esc(pL2) + "</text>";
            }
          }
        }
        // 用户 m22126：柱子顶上的钱横排写一行，不再竖着——竖排读起来费劲。
        // 用户 m22009：写在柱子「外面、正上方」，不压进柱子里面。
        if(!moneyInPin && (!dense || k === tallestK)){
          // 和当天总价一样的就不写了——决策通常就占了大头，同一个数字写两遍只是让图更乱。
          var kLab = barCostLabel(kb);
          if(kLab !== costLabTxt){
            labCand.push({ txt: kLab, cx: x + bw / 2, top: y - 4, col: FN_COLORS[k] });
          }
        }
      });
      // 用户 m22009：总价标签永远贴着点；柱子上的钱要是撞上总价、或撞上隔壁那根柱子的钱，
      // 就只留高的那根（撞掉的那根悬浮里还能看到）。
      var labHtml = "", labRects = [];
      pinRects.forEach(function(b){ labRects.push(b); });
      labCand.sort(function(a, b){ return a.top - b.top; }).forEach(function(c){
        var w = labTextWidth(c.txt);
        var r = { l: c.cx - w / 2, r: c.cx + w / 2, t: c.top - 11, b: c.top };
        var hit = cost > 0 && r.r > cx - costLabW / 2 - 2 && r.l < cx + costLabW / 2 + 2 &&
                  r.b > costLabTop - 2 && r.t < costLabBot + 2;
        for(var m = 0; !hit && m < labRects.length; m++){
          var o = labRects[m];
          if(r.r > o.l - 2 && r.l < o.r + 2 && r.b > o.t - 2 && r.t < o.b + 2){ hit = true; }
        }
        if(hit){ return; }
        labRects.push(r);
        labHtml += "<text class='pval" + numCls + "' x='" + c.cx.toFixed(1) + "' y='" + c.top.toFixed(1) +
                   "' style='fill:" + (c.col || "var(--muted)") + ";" + numDelay(i * 14) + "' text-anchor='middle'>" +
                   esc(c.txt) + "</text>";
      });
      linePts.push([cx, cy]);
      lineDots += "<circle class='pdot' cx='" + cx.toFixed(1) + "' cy='" + cy.toFixed(1) + "' r='2.6'>" +
              "</circle>";
      // 用户 m21274：价格标签永远在点上方，不再上下错开。
      // 用户 m22009：数字压在柱子或折线上就糊成一片，描一圈卡片底色的边（CSS 里 paint-order:stroke）。
      if(cost > 0){
        lineDots += "<text class='pcost" + numCls + "' x='" + cx.toFixed(1) + "' y='" + (cy - 9).toFixed(1) +
                "' style='" + numDelay(i * 14) + "' text-anchor='middle'>" +
                esc(priceMoneyText(cost)) + "</text>";
      }
      // 用户 m22009：柱顶金额在折线之后画，压在折线上也读得清。
      dots += labHtml + pinHtml;
      if(i % step === 0 || i === pts.length - 1){
        labels += "<text class='pxlab" + numCls + "' x='" + cx.toFixed(1) + "' y='" + (H - 11) +
                  "' style='" + numDelay(i * 14) + "' text-anchor='middle'>" + esc(p.label) + "</text>";
      }
    });
    // 用户 m29560 第 4 条：曲线两头都延伸到图的左右边缘，但最新那天不延伸（不画到未来）。
    // 用户 m08731：翻窗口时整块最多滑两格，线只伸到边缘的话，滑到一半两头就断在裁切框里——
    // 按这次要滑的距离再多伸出去，滑到边上时线头正好接上。
    var linePts2 = linePts.slice();
    if(linePts2.length > 1){
      var clampY = function(y){ return Math.max(padT, Math.min(padT + plotH, y)); };
      var panSlots = Math.max(1, Math.min(Math.abs(pricingPanDays) || 1, 2, pts.length));
      var panExtra = panSlots * slot + Math.abs(pricingPanFrom || 0);
      var lx = padL - panExtra;
      // 用户 m08952：左端要接上真实的前一天。以前拿前两个点反推，11-08 比 11-09 低，
      // 反推出来是个假低点，滚轮翻过去线会跳一下。后端把窗口前后各一天一起给了。
      var costY = function(c){ return clampY(padT + plotH - plotH * (Number(c) || 0) / costTop); };
      var prevPt = (d.prev && Number(d.prev.tokens) > 0) ? d.prev : null;
      var la = linePts2[0], lb = linePts2[1];
      var lk = (lb[1] - la[1]) / ((lb[0] - la[0]) || 1);
      if(prevPt){
        var px = la[0] - slot, py = costY(prevPt.cost);
        linePts2.unshift([px, py]);
        if(lx < px){
          var pk = (la[1] - py) / ((la[0] - px) || 1);
          linePts2.unshift([lx, clampY(py + pk * (lx - px))]);
        }
      }else{
        linePts2.unshift([lx, clampY(la[1] + lk * (lx - la[0]))]);
      }
      var lastLab = String((pts[pts.length - 1] || {}).label || "");
      if(lastLab !== pdShort(pdFmt(pdToday()))){
        var rx = W - padR + panExtra;
        var nextPt = (d.next && Number(d.next.tokens) > 0) ? d.next : null;
        var ra = linePts2[linePts2.length - 2], rb = linePts2[linePts2.length - 1];
        var rk = (rb[1] - ra[1]) / ((rb[0] - ra[0]) || 1);
        if(nextPt){
          var nx = rb[0] + slot, ny = costY(nextPt.cost);
          linePts2.push([nx, ny]);
          if(rx > nx){
            var nk = (ny - rb[1]) / ((nx - rb[0]) || 1);
            linePts2.push([rx, clampY(ny + nk * (rx - nx))]);
          }
        }else{
          linePts2.push([rx, clampY(rb[1] + rk * (rx - rb[0]))]);
        }
      }
    }
    var line = linePts2.length > 1 ? "<path class='pline' d='" + smoothLinePath(linePts2, padT, padT + plotH) + "'/>" : "";
    var legend = "<div class='pbar-legend'>" +
      FN_KEYS.map(function(k){
        return "<span class='pbl'><i class='pbl-s' style='background:" + FN_COLORS[k] + "'></i>" + FN_LABELS[k] + "</span>";
      }).join("") +
      "<span class='pbl-sep'></span>" +
      "<span class='pbl'><i class='pbl-l'></i>花费（右轴）</span>" +
      "</div>";
    // 用户 m29947：翻窗口改成整块平移一天的距离——坐标轴和网格不动，只有柱子和横轴日期滑。
    // 用户 m30335：滚了几格就平移几格，滚快了不会只挪一天。
    // 用户 m04883：但最多两格——连滚十几格时整张图横扫过去，看着就是「闪了一下」。
    var panSteps = Math.max(1, Math.min(Math.abs(pricingPanDays) || 1, 2, pts.length));
    var panPx = pricingAnimDir ? (pricingAnimDir > 0 ? 1 : -1) * panSteps * slot + pricingPanFrom : 0;
    var panStyle = pricingAnimDir ? " style='--pdx:" + panPx.toFixed(1) + "px'" : "";
    var plotCls = pricingAnimDir ? "pplot pan" : "pplot";
    // 滚轮只在这块里翻图（左右两根轴之间），轴外面滚的是整页。
    pricingGeom = { W: W, padL: padL, padR: padR, padT: padT, plotH: plotH, slot: slot, n: pts.length };
    // 用户 m30335：平移时把绘图区左右裁掉，滑出去的柱子不会压到两侧刻度上。
    // 裁切放在不动的外层 <g> 上，内层平移时裁切框跟着走就白裁了。
    var clip = "<defs><clipPath id='pclip'><rect x='" + padL + "' y='0' width='" + (W - padL - padR) +
               "' height='" + H + "'/></clipPath></defs>";
    return "<svg class='pchart-svg' viewBox='0 0 " + W + " " + H + "' role='img' aria-label='按天的 token 与花费'>" +
      clip +
      "<g class='pax'>" + grid + "</g>" +
      "<g class='pclipwrap' clip-path='url(#pclip)'>" +
      "<g class='" + plotCls + "'" + panStyle + ">" + bars + "<g class='plinewrap'>" + line + lineDots + "</g>" + dots + labels + "</g>" +
      "</g>" +
      // 用户 m22781：左上角除峰值再给这段的 token 总量，右上角除峰值再给这段的花费总价，跟着区间一起变。
      // 用户 m29947：这两行改成记分牌翻字，只滚变了的字。
      // 用户 m30920：峰值贴着图顶，跟右轴最顶那格价格文字（y = padT + 4）顶部对齐。
      svgRollText("peakTok", "token 峰值 " + fmtBig(dayMax) + "　总量 " + fmtBig(tokTotal),
                  padL + 6, padT + 4, "start", "pylab") +
      svgRollText("peakCost", "花费峰值 " + priceMoneyText(costMax) + (d.currency ? " " + d.currency : "") +
                  "　总价 " + priceMoneyText(costTotal) + (d.currency ? " " + d.currency : ""),
                  W - padR - 6, padT + 4, "end", "pylab") +
      "</svg>" + legend;
  }

  // 用户 m23442：下面三张饼各自选区间（花费占比 / Token 占比 / 调用分布），
  // 每张饼右上角点开是快捷区间 + 两个日历框。
  // 用户 m04883：改成「图在左、数据在右」，每个扇区拉一根细线连到自己那行数据。
  // 用户 m05422：线以前从饼的右沿冒出来，跟扇区对不上——第一段从弧中点直奔图例，
  // 横穿饼身的那截被不透明的饼挡住，露出来的只剩饼右边一小截。现在线画在饼上面，
  // 用户 m06437：双环看着乱、也不紧凑，改成左右两个饼——左边按功能（决策 / JEV / 回复），
  // 右边按模型，两张饼并排一行；功能图例贴左饼左边、模型图例贴右饼右边。中间不再写总数
  // （两张饼写同一个数没意义），总数挪到标题下面。
  // 用户 m06710：引线要回来、模型图例要带百分比、图例一列不能高过饼（超了换列）。
  // 饼 132，图例一行 17px；名字放不下就省略。
  // 用户 m07372：图例字号从 10px 提到 12px，饼从 126 收到 120 给右图例腾宽度。
  // 用户 m07837：饼保持 120——三张饼要并排一行，饼再大就放不下。
  // 用户 m08731：按钮收进左上角、卡顶留白压掉，饼从 120 放到 132；图例行距 20 → 17。
  var PIE_BOX = 132;
  var PIE_R = 58, PIE_IR = 37;
  var PIE_MAX_ROWS = 8;
  var PIE_ROW_H = 17;
  // 模型色要跟功能色（蓝 / 紫 / 绿）分开，不然两张饼分不清谁是谁。
  var PIE_MODEL_COLORS = ["#f59e0b", "#06b6d4", "#ef4444", "#84cc16", "#ec4899", "#f97316", "#14b8a6", "#a78bfa"];

  function pieRingSlices(items, sum, cx, cy, r, ir, startAng, palette){
    var ang = startAng, slices = "";
    items.forEach(function(p, i){
      var frac = sum > 0 ? p.value / sum : 0;
      var a2 = ang + frac * Math.PI * 2;
      var color = p.color || palette[i % palette.length];
      if(frac >= 0.9999){
        slices += "<circle cx='" + cx + "' cy='" + cy + "' r='" + ((r + ir) / 2) + "' fill='none' stroke='" + color +
                  "' stroke-width='" + (r - ir) + "'/>";
      }else if(frac > 0){
        slices += "<path d='" + pieArc(cx, cy, r, ir, ang, a2) + "' fill='" + color + "'/>";
      }
      ang = a2;
    });
    return slices;
  }

  // 引线：从扇区外沿沿半径走到 rr，再沿同心弧绕到目标角度，最后直线落到图例那行左端。
  // 出口取「扇区里离这行最近的角度」——行角落在扇区内就是一条纯径向线。
  function pieLeadPath(cx, cy, r, ang, rr, tx, ty){
    var a1 = Math.atan2(ty - cy, tx - cx);
    var d = a1 - ang;
    while(d > Math.PI) d -= Math.PI * 2;
    while(d < -Math.PI) d += Math.PI * 2;
    var x1 = cx + Math.cos(ang) * (r + 1), y1 = cy + Math.sin(ang) * (r + 1);
    var x2 = cx + Math.cos(ang) * rr, y2 = cy + Math.sin(ang) * rr;
    var x3 = cx + Math.cos(a1) * rr, y3 = cy + Math.sin(a1) * rr;
    return "M" + x1.toFixed(1) + " " + y1.toFixed(1) +
      "L" + x2.toFixed(1) + " " + y2.toFixed(1) +
      "A" + rr + " " + rr + " 0 0 " + (d > 0 ? 1 : 0) + " " + x3.toFixed(1) + " " + y3.toFixed(1) +
      "L" + tx.toFixed(1) + " " + ty.toFixed(1);
  }

  // 引线要等布局落定才知道每行在哪，所以画完饼再量一次，画进覆盖整块的 svg 里。
  function drawPieLeads(wrap){
    var svg = wrap.querySelector(".ppie-leads");
    var pie = wrap.querySelector(".ppie-svg[data-outer='1']");
    if(!svg || !pie) return;
    var wr = wrap.getBoundingClientRect();
    if(!wr.width) return;
    var pr = pie.getBoundingClientRect();
    var cx = pr.left - wr.left + pr.width / 2;
    var cy = pr.top - wr.top + pr.height / 2;
    var spans = String(pie.getAttribute("data-spans") || "").split(";");
    var rows = wrap.querySelectorAll(".pleg-right .pleg");
    var n = Math.min(rows.length, spans.length);
    if(n < 2){ svg.innerHTML = ""; return; }
    // 用户 m07156：每条线先按「贴着圈」的半径试，跟已经画过的弧撞上了才往外让一档；
    // 半径只增不减、行又从上到下排，所以线之间不会交叉。
    var used = [], d = "", kPrev = 0;
    for(var i = 0; i < n; i++){
      var sp = spans[i].split(",");
      var a0 = parseFloat(sp[0]), a1 = parseFloat(sp[1]);
      if(isNaN(a0) || isNaN(a1)) continue;
      var rr = rows[i].getBoundingClientRect();
      var tx = rr.left - wr.left - 3, ty = rr.top - wr.top + rr.height / 2;
      var t = Math.atan2(ty - cy, tx - cx);
      while(t < a0) t += Math.PI * 2;
      while(t > a0 + Math.PI * 2) t -= Math.PI * 2;
      var ang;
      if(t <= a1) ang = t;
      else ang = (t - a1 <= a0 + Math.PI * 2 - t) ? a1 : a0;
      // 别正好压在扇区边界上，往里让一点。
      var inset = Math.min(0.10, (a1 - a0) / 3);
      if(ang === a1) ang -= inset;
      else if(ang === a0) ang += inset;
      var lo = Math.min(ang, t), hi = Math.max(ang, t);
      var k = kPrev;
      while(k < 6){
        var list = used[k] || [], hit = false;
        for(var j = 0; j < list.length; j++){
          if(lo < list[j][1] - 0.02 && hi > list[j][0] + 0.02){ hit = true; break; }
        }
        if(!hit) break;
        k++;
      }
      if(k > 5) k = 5;
      (used[k] = used[k] || []).push([lo, hi]);
      kPrev = k;
      d += pieLeadPath(cx, cy, PIE_R, ang, PIE_R + 5 + k * 3, tx, ty);
    }
    svg.setAttribute("viewBox", "0 0 " + wr.width + " " + wr.height);
    svg.setAttribute("width", wr.width);
    svg.setAttribute("height", wr.height);
    svg.innerHTML = d ? "<path d='" + d + "' fill='none' stroke='currentColor' stroke-width='1' stroke-opacity='.5'/>" : "";
  }

  // 用户 m14524：右图例的百分比列原来钉死 46px，短百分比（2.8%）左边空出一大截，
  // 看着像两组数据之间留了很宽的空。改成按这张卡里最宽的百分比定列宽——
  // 值列右边缘仍然对齐（名字列 1fr 吸收余量），空隙只剩 column-gap。
  function fitPieLegendCols(){
    var el = document.getElementById("p-charts");
    if(!el) return;
    var legs = el.querySelectorAll(".plegend.pleg-right");
    for(var i = 0; i < legs.length; i++){
      var rows = legs[i].querySelectorAll(".pleg");
      if(!rows.length) continue;
      var max = 0;
      for(var j = 0; j < rows.length; j++){
        var p = rows[j].querySelector(".pleg-p");
        if(!p) continue;
        var rg = document.createRange();
        rg.selectNodeContents(p);
        var w = rg.getBoundingClientRect().width;
        if(w > max) max = w;
      }
      if(!max) continue;
      var pw = Math.ceil(max) + 1;
      for(var k = 0; k < rows.length; k++){
        rows[k].style.gridTemplateColumns = "8px minmax(0,1fr) auto " + pw + "px";
      }
    }
  }

  function drawPieLeadsAll(){
    fitPieLegendCols();
    var ws = document.querySelectorAll("#p-charts .ppie-wrap");
    for(var i = 0; i < ws.length; i++) drawPieLeads(ws[i]);
  }

  // 用户 m06997：标题拆两行塞进左饼中间；有空格按空格拆，没有就前后各一半。
  function pieTitleLines(t){
    var s = String(t || "").trim();
    if(!s) return [];
    var sp = s.indexOf(" ");
    if(sp > 0) return [s.slice(0, sp), s.slice(sp + 1)];
    if(s.length <= 2) return [s];
    var half = Math.ceil(s.length / 2);
    return [s.slice(0, half), s.slice(half)];
  }

  // 饼心文字：一行时基线压在圆心下方，两行时上下各让半行。
  function pieCenterSvg(lines, cls1, cls2){
    var n = lines ? lines.length : 0;
    if(!n) return "";
    var y0 = n > 1 ? (PIE_BOX / 2 - 2.5) : (PIE_BOX / 2 + 4);
    var out = "";
    for(var i = 0; i < n; i++){
      out += "<text x='" + (PIE_BOX / 2) + "' y='" + (y0 + i * 13).toFixed(1) + "' text-anchor='middle' class='" +
        (i ? (cls2 || cls1) : cls1) + "'>" + esc(lines[i]) + "</text>";
    }
    return out;
  }

  function pieSvgHtml(inner, outer, note, aria, anim, titleLines, totalLines){
    var sumIn = 0, sumOut = 0;
    inner.forEach(function(p){ sumIn += p.value; });
    outer.forEach(function(p){ sumOut += p.value; });
    // 内圈从「最大那块正中朝上」开始画；外圈从「最大那块正中朝左」开始画——
    // 用户 m06997：小扇区全落在右边、紧挨模型图例，引线不用绕大半圈。
    var startIn = -Math.PI / 2 - (sumIn > 0 ? (inner[0].value / sumIn) * Math.PI : 0);
    var startOut = Math.PI - (sumOut > 0 ? (outer[0].value / sumOut) * Math.PI : 0);
    var ringIn = pieRingSlices(inner, sumIn, PIE_BOX / 2, PIE_BOX / 2, PIE_R, PIE_IR, startIn, PIE_MODEL_COLORS);
    var ringOut = pieRingSlices(outer, sumOut, PIE_BOX / 2, PIE_BOX / 2, PIE_R, PIE_IR, startOut, PIE_MODEL_COLORS);
    // 外圈每块的起止角，画引线用（引线出口取「扇区里离这行最近的角度」）。
    var spans = [], a = startOut;
    outer.forEach(function(p){
      var frac = sumOut > 0 ? p.value / sumOut : 0;
      spans.push(a.toFixed(4) + "," + (a + frac * Math.PI * 2).toFixed(4));
      a += frac * Math.PI * 2;
    });
    var leftLegend = inner.map(function(p, i){
      var pct = sumIn > 0 ? p.value / sumIn * 100 : 0;
      var nm = String(p.name || "");
      if(nm.length > 4) nm = nm.slice(0, 4) + "…";
      return "<div class='pleg pleg-l'>" +
        "<span class='pleg-n'>" + esc(nm) + "</span>" +
        "<span class='pleg-p'>" + pct.toFixed(1) + "%</span>" +
        "<i style='background:" + (p.color || "#94a3b8") + "'></i></div>";
    }).join("");
    // 用户 m06710：模型那行要把百分比写回来；名字放不下由 CSS 省略。
    // 一列不能高过饼：行数超过六行就把行高压到饼高以内，引线按行心重算。
    var rowH = Math.min(PIE_ROW_H, Math.floor(PIE_BOX / Math.max(1, outer.length)));
    var rightLegend = outer.map(function(p, i){
      var pct = sumOut > 0 ? p.value / sumOut * 100 : 0;
      return "<div class='pleg' style='height:" + rowH + "px'>" +
        "<i style='background:" + (p.color || PIE_MODEL_COLORS[i % PIE_MODEL_COLORS.length]) + "'></i>" +
        "<span class='pleg-n'>" + esc(String(p.name || "")) + "</span>" +
        "<span class='pleg-v'>" + esc(p.valueText) + "</span>" +
        "<span class='pleg-p'>" + pct.toFixed(1) + "%</span></div>";
    }).join("");
    var pieStyle = "width:" + PIE_BOX + "px;height:" + PIE_BOX + "px;";
    return "<div class='ppie-wrap'>" +
      "<svg class='ppie-leads' aria-hidden='true'></svg>" +
      "<div class='plegend pleg-left'>" + leftLegend + "</div>" +
      "<svg class='ppie-svg' style='" + pieStyle + "' viewBox='0 0 " + PIE_BOX + " " + PIE_BOX + "' role='img' aria-label='" + esc(aria) + "'>" +
      ringIn + pieCenterSvg(titleLines, "ppie-t1", "ppie-t1") + "</svg>" +
      "<svg class='ppie-svg' data-outer='1' data-spans='" + spans.join(";") + "' style='" + pieStyle + "' viewBox='0 0 " + PIE_BOX + " " + PIE_BOX + "' role='img' aria-label='" + esc(aria) + "'>" +
      ringOut + pieCenterSvg(totalLines, "ppie-t1", "ppie-t2") + "</svg>" +
      "<div class='plegend pleg-right'>" + rightLegend + "</div>" +
      "</div>" +
      (note ? "<div class='pleg-note'>" + esc(note) + "</div>" : "");
  }

  function pricingPieById(id){
    for(var i = 0; i < pricingPies.length; i++) if(pricingPies[i].id === id) return pricingPies[i];
    return null;
  }

  // 快捷区间：今天 / 昨天 / 本周 / 上周 / 本月 / 上月（按真实日历，周一开始）。
  function pricingQuickRange(kind){
    var today = pdToday();
    var monday = function(d){ return pdAdd(d, -((d.getDay() + 6) % 7)); };
    if(kind === "today") return { start: pdFmt(today), end: pdFmt(today) };
    if(kind === "yesterday"){ var y = pdAdd(today, -1); return { start: pdFmt(y), end: pdFmt(y) }; }
    // 用户 m29973：本周 / 上周 都按「最近 7 天」算，不再取自然周——
    // 周一当天点「本周」只剩一天，看着像没数据。
    if(kind === "thisweek") return { start: pdFmt(pdAdd(today, -6)), end: pdFmt(today) };
    if(kind === "lastweek") return { start: pdFmt(pdAdd(today, -13)), end: pdFmt(pdAdd(today, -7)) };
    if(kind === "lastmonth"){
      return { start: pdFmt(new Date(today.getFullYear(), today.getMonth() - 1, 1)),
               end: pdFmt(new Date(today.getFullYear(), today.getMonth(), 0)) };
    }
    return { start: pdFmt(new Date(today.getFullYear(), today.getMonth(), 1)), end: pdFmt(today) };
  }

  // 用户 m06116：三张饼都是双环——内圈按功能（决策 / JEV / 回复），外圈按模型。
  // 内圈三块固定，外圈最多 PIE_MAX_ROWS 块，多出来的并成一块灰的「其他 N 个」。
  var FN_SHORT = { decision: "决策", jev: "JEV", reply: "回复" };

  function pieCapItems(list, fmt){
    if(list.length <= PIE_MAX_ROWS) return list;
    var keep = list.slice(0, PIE_MAX_ROWS - 1), rest = list.slice(PIE_MAX_ROWS - 1);
    var sum = 0;
    rest.forEach(function(p){ sum += p.value; });
    return keep.concat([{ name: "其他 " + rest.length + " 个", value: sum, valueText: fmt(sum), color: "#94a3b8" }]);
  }

  function pricingPieItems(pie){
    var d = pie.data;
    if(!d) return null;
    var kinds = d.kinds || {}, parts = d.parts || [], total = d.total || {};
    var money = function(v){ return priceMoneyText(v) + (d.currency ? " " + d.currency : ""); };
    // 用户 m08100：右图例的数值要带单位（元 / 轮），跟饼心一致。
    var moneyShort = function(v){ return priceMoneyText(v); };
    // 内圈：三个功能块，颜色沿用柱图那三根柱子，两张图一眼能对上。
    var fnItems = function(metric, fmt){
      var out = [];
      FN_KEYS.forEach(function(k){
        var v = Number((kinds[k] || {})[metric]) || 0;
        if(v > 0) out.push({ name: FN_SHORT[k], value: v, valueText: fmt(v), color: FN_COLORS[k] });
      });
      out.sort(function(a, b){ return b.value - a.value; });
      return out;
    };
    // 外圈：按模型。老账本有些轮次没记下模型，按模型加起来会比总数少，
    // 补一块灰的「未记录模型」，中心数字才跟内圈对得上。
    var modelItems = function(metric, fmt, withRest){
      var out = [], sum = 0;
      parts.forEach(function(p){
        var v = Number(p[metric]) || 0;
        if(v > 0){ out.push({ name: String(p.model_name || p.model || ""), value: v, valueText: fmt(v) }); sum += v; }
      });
      if(withRest){
        var rest = (Number(total[withRest]) || 0) - sum;
        if(rest > 0) out.push({ name: "未记录模型", value: rest, valueText: fmt(rest), color: "#94a3b8" });
      }
      out.sort(function(a, b){ return b.value - a.value; });
      return pieCapItems(out, fmt);
    };
    if(pie.id === "calls"){
      return { inner: fnItems("rounds", function(v){ return v + " 轮"; }),
               outer: modelItems("rounds", function(v){ return v + " 轮"; }, "rounds"),
               center: String(Number(total.rounds) || 0), centerSub: "轮", note: "",
               empty: "这段时间还没有调用。" };
    }
    if(pie.id === "tokens"){
      return { inner: fnItems("tokens", fmtBig),
               outer: modelItems("tokens", fmtBig, "tokens"),
               center: fmtBig(Number(total.tokens) || 0), centerSub: "token", note: "",
               empty: "这段时间还没有用量。" };
    }
    // 花费：一个价都没填的时候画不出花费占比，退回按 token 占比画，图不至于空着。
    var totalCost = Number(total.cost) || 0;
    var byCost = totalCost > 0;
    var unsetCount = parts.filter(function(p){
      var t = Number(p.tokens) || ((Number(p.input) || 0) + (Number(p.output) || 0));
      return !!p.unset && t > 0;
    }).length;
    var note = "";
    if(!byCost) note = "还没填单价，先按 token 占比画。";
    else if(unsetCount) note = "另有 " + unsetCount + " 个模型没填单价，没算进占比。";
    return { inner: fnItems(byCost ? "cost" : "tokens", byCost ? money : fmtBig),
             outer: modelItems(byCost ? "cost" : "tokens", byCost ? money : fmtBig, ""),
             center: byCost ? priceMoneyText(totalCost) : fmtBig(Number(total.tokens) || 0),
             centerSub: byCost ? String(d.currency || "") : "token",
             note: note, empty: "这段时间还没有用量。" };
  }

  // 用户 m29947：饼图这边也改成「快捷 + 左右两个日历」，跟柱图一致。
  function pricingPiePopHtml(pie){
    var qs = [["today", "今天"], ["yesterday", "昨天"], ["thisweek", "本周"],
              ["lastweek", "上周"], ["month", "本月"], ["lastmonth", "上月"]];
    return "<div class='pdp'>" +
      "<div class='pdp-q'>" + qs.map(function(o){
        return "<button type='button' class='pdp-qb' data-pie='" + pie.id + "' data-q='" + o[0] + "'>" + o[1] + "</button>";
      }).join("") + "</div>" +
      pricingCalPairHtml() +
      "<div class='pdp-f'><span class='pdp-span'>" + esc(pricingCalSpanText()) + "</span>" +
      "<button type='button' class='pdp-ok' data-cal-apply='1'>应用</button></div>" +
      "</div>";
  }

  function pricingPieCardHtml(pie){
    var span = (pie.start && pie.end) ? pdSpanText(pie.start, pie.end) : "本月";
    var built = (pie.data && !pie.data.error) ? pricingPieItems(pie) : null;
    // 用户 m06997：标题挪进左饼中间（两行），总数挪进右饼中间，卡头只留右上角的区间按钮。
    var head = "<span class='prange pchart-range'><button type='button' class='prb pdp-btn" + (pie.open ? " on" : "") +
      "' data-pie='" + pie.id + "'>" + esc(span) + " ▾</button></span>";
    var body;
    if(!pie.data) body = "<div class='pchart-empty'>正在统计…</div>";
    else if(pie.data.error) body = "<div class='pchart-empty'>图表读取失败：" + esc(pie.data.error) + "</div>";
    else if(!built || (!built.inner.length && !built.outer.length)){
      body = "<div class='pchart-empty'>" + esc((built && built.empty) || "这段时间还没有数据。") + "</div>";
    }else{
      body = pieSvgHtml(built.inner, built.outer, built.note, pie.title, pie.anim,
        pieTitleLines(pie.title),
        [built.center, built.centerSub].filter(function(s){ return !!s; }));
    }
    return "<div class='pchart pchart-pie'>" + head +
      "<div class='pchart-body" + (pie.anim ? " pchart-anim" : "") + "'>" + body + "</div>" +
      (pie.open ? pricingPiePopHtml(pie) : "") + "</div>";
  }

  // 用户 m04883：饼的数据是单独到的，只重画饼卡——柱图那张卡不跟着重建。
  // 用户 m05865：六张饼共用一次请求，回调会连着来六次；合并成一次重画。
  var pricingPiePaintQueued = 0;
  function paintPricingPies(){
    if(pricingPiePaintQueued) return;
    pricingPiePaintQueued = 1;
    Promise.resolve().then(function(){
      pricingPiePaintQueued = 0;
      paintPricingPiesNow();
    });
  }

  function paintPricingPiesNow(){
    var el = document.getElementById("p-charts");
    if(!el) return;
    var cards = el.querySelectorAll(".pchart-pie");
    if(cards.length !== pricingPies.length){ paintPricingTrend(); return; }
    for(var i = 0; i < pricingPies.length; i++){
      cards[i].outerHTML = pricingPieCardHtml(pricingPies[i]);
    }
    // 用户 m08100：弹层先同步摆好再画，别等下一帧——否则会先出现在卡片下面再跳上去，看着闪。
    settlePiePops();
    // 引线要量完布局才画得准，等这一帧排完再补。
    requestAnimationFrame(drawPieLeadsAll);
  }

  // 用户 m07372：弹层默认往下开，饼卡在页面底部时会被视口切掉；放不下就翻到卡片上面。
  function settlePiePops(){
    var el = document.getElementById("p-charts");
    if(!el) return;
    var vh = window.innerHeight || document.documentElement.clientHeight || 0;
    var pops = el.querySelectorAll(".pchart-pie .pdp");
    for(var i = 0; i < pops.length; i++){
      var p = pops[i];
      p.classList.remove("pdp-up");
      var r = p.getBoundingClientRect();
      if(r.bottom > vh - 8 && r.top > r.height + 8) p.classList.add("pdp-up");
    }
  }

  // 用户 m08100：点按钮只开关弹层，不重画整张卡、也不重画柱图——以前整块重画会闪一下还卡。
  function paintPricingPiePopups(){
    var el = document.getElementById("p-charts");
    if(!el) return;
    var cards = el.querySelectorAll(".pchart-pie");
    for(var i = 0; i < pricingPies.length && i < cards.length; i++){
      var pie = pricingPies[i], card = cards[i];
      var btn = card.querySelector(".pdp-btn");
      if(btn){
        if(pie.open) btn.classList.add("on"); else btn.classList.remove("on");
      }
      var pop = card.querySelector(".pdp");
      if(pie.open && !pop) card.insertAdjacentHTML("beforeend", pricingPiePopHtml(pie));
      else if(!pie.open && pop) pop.parentNode.removeChild(pop);
    }
    settlePiePops();
  }

  // 窗口宽度变了，饼和图例的位置跟着变，引线重画一次（防抖）。
  var pieLeadTimer = 0;
  window.addEventListener("resize", function(){
    if(pieLeadTimer) clearTimeout(pieLeadTimer);
    pieLeadTimer = setTimeout(function(){ pieLeadTimer = 0; drawPieLeadsAll(); }, 120);
  });

  // 用户 m05865：六张饼默认同一个区间，别为同一段数据发六次请求。
  var pricingPieInflight = {};

  function loadPricingPie(id, force){
    var pie = pricingPieById(id);
    if(!pie) return Promise.resolve();
    var r = (pie.start && pie.end) ? { start: pie.start, end: pie.end } : pricingQuickRange("month");
    var tag = r.start + ":" + r.end;
    if(!force && pie.data && pie.tag === tag && Date.now() - pie.at < 20000) return Promise.resolve();
    var seq = ++pie.seq;
    var job = pricingPieInflight[tag];
    if(!job){
      job = get(API + "/pricing/trend?range_key=window&start=" + r.start + "&end=" + r.end);
      pricingPieInflight[tag] = job;
      var done = function(){ if(pricingPieInflight[tag] === job) delete pricingPieInflight[tag]; };
      job.then(done, done);
    }
    return job.then(function(d){
      if(seq !== pie.seq) return;
      pie.data = (d && d.ok) ? d : { points: [], parts: [], total: {}, error: "接口没返回数据" };
      pie.tag = tag; pie.at = Date.now(); pie.anim = 1;
      paintPricingPies();
    }).catch(function(e){
      if(seq !== pie.seq) return;
      pie.data = { points: [], parts: [], total: {}, error: String((e && e.message) || e) };
      pie.tag = tag; pie.at = Date.now(); pie.anim = 1;
      paintPricingPies();
    });
  }

  function loadPricingPies(force){
    pricingPies.forEach(function(p){ loadPricingPie(p.id, force); });
  }

  // 用户 m19975：图表按日历切，跟上面表格的区间不是一回事，标题里写清画的是哪一段。
  function pricingTrendSpanText(){
    var pts = (pricingTrend && pricingTrend.points) || [];
    if(!pts.length) return "";
    var a = pts[0].label || "", b = pts[pts.length - 1].label || "";
    var txt = a === b ? a : (a + " ~ " + b);
    // 用户 m21274：按周那一档的横轴是每周的周一，顺手把「几周」写出来。
    if(pricingTrend && pricingTrend.range === "week") txt += "（" + pts.length + " 周）";
    return txt;
  }

  // 用户 m04883：柱图那张卡单独拆出来——翻窗口时只换它，三张饼不跟着重建。
  function pricingBarCardInner(width){
    // 用户 m29947：区间那行日期也按记分牌翻——只滚变了的字。
    var spanHtml = htmlRollText("trendSpan", pricingTrendSpanText());
    // 用户 m09408：弹层要跟着按钮走，所以放进按钮所在的 span 里——span 是 position:relative，弹层以它为定位祖先。
    return "<div class='pchart-h'><b>用量与花费</b><span class='pchart-sub'>" + spanHtml + "</span>" +
      "<span class='prange pchart-range' id='p-trend-range'>" + pricingTrendRangeHtml() +
      (pricingCal.open ? pricingTrendPopHtml() : "") + "</span></div>" +
      "<div class='pchart-body'>" + pricingBarChartHtml(width) + "</div>";
  }

  function pricingBarCardHtml(width){
    // 用户 m22781：那行「柱 = … 折线 = …」的说明删掉，只留区间。
    return "<div class='pchart'>" + pricingBarCardInner(width) + "</div>";
  }

  function pricingChartsInner(width){
    return pricingBarCardHtml(width) + pricingPies.map(pricingPieCardHtml).join("");
  }

  var pricingChartRO = null, pricingChartROEl = null, pricingChartW = 0, pricingChartROTimer = 0;

  // 图按左卡的实际宽度画：以前拿的是整行宽度，viewBox 被缩到 0.57 倍，字小得看不清。
  // 用户 m04883：观察器只建一次——以前每次重画都 disconnect 再新建，快速滚动时白搭开销。
  function watchPricingCharts(){
    var el = document.getElementById("p-charts");
    if(!el || typeof ResizeObserver === "undefined") return;
    if(pricingChartRO && pricingChartROEl === el) return;
    if(pricingChartRO) pricingChartRO.disconnect();
    pricingChartROEl = el;
    pricingChartRO = new ResizeObserver(function(){
      var card = el.querySelector(".pchart");
      var w = card ? Math.round(card.clientWidth) : 0;
      // 只认宽度变化：高度是内容撑出来的，跟着变会自己触发自己。
      // 用户 m07837：差几十像素（滚动条、图例换行）不算变——以前每 0.5 秒整块重画一次，
      // 饼卡的弹层刚打开就被冲掉，看着像「按钮打不开」。真缩放窗口才重画，且防抖一次。
      if(!w || Math.abs(w - pricingChartW) < 40) return;
      if(pricingChartROTimer) clearTimeout(pricingChartROTimer);
      pricingChartROTimer = setTimeout(function(){
        pricingChartROTimer = 0;
        var c2 = el.querySelector(".pchart");
        var w2 = c2 ? Math.round(c2.clientWidth) : 0;
        if(!w2 || Math.abs(w2 - pricingChartW) < 40) return;
        pricingChartW = w2;
        el.innerHTML = pricingChartsInner(w2);
        pricingPies.forEach(function(p){ p.anim = 0; });
        // 整块重画会把饼卡的引线和弹层翻上状态一起冲掉，补一次（弹层同步摆好，别等下一帧）。
        settlePiePops();
        requestAnimationFrame(drawPieLeadsAll);
      }, 260);
    });
    pricingChartRO.observe(el);
  }

  function paintPricingTrend(){
    var el = document.getElementById("p-charts");
    if(!el) return;
    // 用户 m30335：重画前先把上一帧的柱线层抓下来（连它此刻滑到哪儿一起抓），
    // 画完垫在新图下面一起滑出去，两层接起来就是连续滚动。
    var prevPlot = el.querySelector(".pchart-svg .pplot:not(.pghost)");
    var prevHtml = prevPlot ? prevPlot.innerHTML : "";
    var prevTx = 0;
    if(prevPlot){
      var mt = /matrix\(([^)]+)\)/.exec(String(getComputedStyle(prevPlot).transform || ""));
      if(mt){
        var nums = mt[1].split(",");
        prevTx = parseFloat(nums[4]) || 0;
      }
    }
    // 先铺一次骨架量出左卡真实宽度，再按这个宽度重画（柱图在 2.4fr 的左卡里，不是整行宽）。
    // 用户 m04883：宽度量过一次就记住，之后只换柱图那张卡——以前每次翻窗口都要整块重画两遍，
    // 快速滚动时就是「卡一下」。
    var w = pricingChartW;
    if(!w){
      // 先铺一次骨架量宽度，量完把翻字基准还原，免得第二次画的时候「已经一样了」不翻。
      var rollSnap = JSON.parse(JSON.stringify(_rollPrev));
      el.innerHTML = pricingChartsInner(0);
      var card = el.querySelector(".pchart");
      w = card ? Math.round(card.clientWidth) : 0;
      pricingChartW = w;
      _rollPrev = rollSnap;
    }
    pricingPanFrom = prevTx;
    var barCard = el.querySelector(".pchart:not(.pchart-pie)");
    if(barCard){
      barCard.innerHTML = pricingBarCardInner(w);
    }else{
      el.innerHTML = pricingChartsInner(w);
      pricingPies.forEach(function(p){ p.anim = 0; });
    }
    // 用户 m04883：一次跳超过两格时不留上一帧——它只滑两格，跟新图对不上，会一直叠在下面。
    if(pricingAnimDir && prevHtml && Math.abs(pricingPanDays) <= 2){
      var wrap = el.querySelector(".pchart-svg .pclipwrap");
      var cur = wrap ? wrap.querySelector(".pplot:not(.pghost)") : null;
      if(wrap && cur){
        var gs = pricingGeom || {};
        var gslot = Number(gs.slot) || 0;
        var gsteps = Math.max(1, Math.min(Math.abs(pricingPanDays) || 1, 2, Number(gs.n) || 1));
        var g = document.createElementNS("http://www.w3.org/2000/svg", "g");
        g.setAttribute("class", "pplot pghost");
        g.innerHTML = prevHtml;
        // 用户 m30734：上一帧的折线不跟着复制——两层各画一条橙线会交叉成「多余的线」。
        var gl = g.querySelector(".plinewrap");
        if(gl && gl.parentNode) gl.parentNode.removeChild(gl);
        g.style.setProperty("--pdx0", prevTx.toFixed(1) + "px");
        g.style.setProperty("--pdx2", (-pricingAnimDir * gsteps * gslot).toFixed(1) + "px");
        wrap.insertBefore(g, cur);
        // 滑完就撤，别留在 DOM 里跟新图叠着。
        g.addEventListener("animationend", function(){ if(g.parentNode) g.parentNode.removeChild(g); });
      }
    }
    pricingAnimDir = 0;
    pricingPanDays = 0;
    pricingPanFrom = 0;
    pricingPies.forEach(function(p){ p.anim = 0; });
    watchPricingCharts();
  }

  function loadPricingTrend(mode, force){
    var m = (mode === "default" || mode === "thisweek" || mode === "lastweek" ||
             mode === "month" || mode === "lastmonth" || mode === "custom") ? mode : "default";
    pricingTrendMode = m;
    var url, tag;
    if(m !== "month" && m !== "lastmonth"){
      var r = pricingWinRange();
      url = "/pricing/trend?range_key=window&start=" + r.start + "&end=" + r.end;
      tag = "window:" + r.start + ":" + r.end;
    }else{
      url = "/pricing/trend?range_key=" + m;
      tag = m;
    }
    if(!force && pricingTrend && pricingTrendRangeLoaded === tag && Date.now() - pricingTrendAt < 20000){
      paintPricingTrend();
      return Promise.resolve();
    }
    var seq = ++pricingTrendSeq;
    return get(API + url).then(function(d){
      if(seq !== pricingTrendSeq) return;
      pricingTrend = (d && d.ok) ? d : { points: [], parts: [], total: {}, error: "接口没返回数据" };
      pricingTrendAt = Date.now();
      pricingTrendRangeLoaded = tag;
      paintPricingTrend();
    }).catch(function(e){
      if(seq !== pricingTrendSeq) return;
      pricingTrend = { points: [], parts: [], total: {}, error: String((e && e.message) || e) };
      pricingTrendAt = Date.now();
      pricingTrendRangeLoaded = tag;
      paintPricingTrend();
    });
  }

  var pdelTimer = 0;
  function armPricingDelete(btn){
    if(!btn) return;
    btn.setAttribute("data-armed", "1");
    btn.textContent = "确认";
    if(pdelTimer) clearTimeout(pdelTimer);
    pdelTimer = setTimeout(function(){ pdelTimer = 0; disarmPricingDelete(null); }, 3000);
  }
  function disarmPricingDelete(btn){
    var t = btn || document.querySelector("#pricing-box [data-armed='1']");
    if(!t) return;
    t.removeAttribute("data-armed");
    t.textContent = "删除";
  }

  // 绑定用 onclick/oninput 直接赋值（不是 addEventListener）：重绘多次也只会挂一次，不会叠。
  function collectPricing(){
    var models = {};
    Array.prototype.forEach.call(document.querySelectorAll("#pricing-box tr[data-pmodel]"), function(tr){
      var name = tr.getAttribute("data-pmodel");
      var value = function(field){
        var el = tr.querySelector("[data-pfield='" + field + "']");
        return el && el.type === "checkbox" ? !!el.checked : String(el && el.value || "").trim();
      };
      var a=value("in"), b=value("out"), c=value("cache"), prov=value("provider");
      var p=pricingPeak();
      ["in","out","cache"].forEach(function(k){ var v=value("peak."+k); p[k]=v === "" ? null : Number(v); });
      p.enabled=value("peak.enabled"); p.weekends=!value("peak.weekends");
      p.periods=pricingPeriodsFromRow(tr);
      p.start=p.periods.length ? p.periods[0].start : ""; p.end=p.periods.length ? p.periods[0].end : "";
      // Keep legacy all-unset ordinary rows out of the persisted table. A peak
      // schedule/override itself is meaningful, including an explicit free 0.
      // 用户 m23442：weekends 的默认值翻过来了（true = 周末按高峰算），所以「没改过」的判据也跟着翻。
    if(a === "" && b === "" && c === "" && !prov && !p.enabled && p.weekends && !p.periods.length && p.in === null && p.out === null && p.cache === null) return;
      models[name]={in:a === "" ? 0 : Number(a), out:b === "" ? 0 : Number(b), cache:c === "" ? 0 : Number(c), provider:prov, peak:p};
    });
    return models;
  }

  function validatePricing(models){
    var names=Object.keys(models);
    for(var i=0;i<names.length;i++){
      var m=models[names[i]], p=m.peak;
      var prices=[m.in,m.out,m.cache,p.in,p.out,p.cache];
      for(var j=0;j<prices.length;j++){
        if(prices[j] !== null && (!isFinite(prices[j]) || prices[j] < 0)) return "单价必须是非负数；高峰价可留空继承";
      }
    }
    return "";
  }

  var ADAPTIVE_ROWS = [
    { k: "adaptive_step", unit: "%" },
    // v3.84（用户 m04084 第 3 条）：调参间隔——以前是写死的 10 分钟、而且只在「触发并结算」
    // 时才调，概率低的时段可能一小时都调不上一次；现在判定时也调，间隔由这一项管。
    { k: "adaptive_tune_minutes", unit: "分钟", sub: "越小跟得越快" },
    // v3.81（用户 m02645 图2）：「节奏松紧」搬到「节奏模式」那一组了——节奏器现在是概率 /
    // 曲线 / 动态调整三种模式共用，不再只属于动态调整。
    { k: "stats_window_days", unit: "天" }
  ];
  // v3.72（用户第 8 / 9 条）：这两项的上下限 schema 里没写、/config 的 bounds 也不一定有，
  // 页面自己兜一层：数据使用最少 7 天，回看周数 4-12 周（renderAdaptiveRow 里做夹值）。
  // A3（用户 m26791）：上限 52 → 12，跟后端 _BOUNDS 和「分桶最多扫 120 天」对齐。
  var ADAPTIVE_LIM = {
    adaptive_tune_minutes: [1, 120],
    stats_window_days: [7, 365],
    adaptive_weekday_window_weeks: [4, 12]
  };
  // v3.74（用户 m00024 第 4-d 条）：「每轮最多调几个点」太长，把这一行读作「每轮调整 2%」。
  // 只改这张表上的显示名，schema 里的键名与说明不动（后端照旧按 adaptive_step 读）。
  var ADAPTIVE_NAME = { adaptive_step: "每轮调整", adaptive_tune_minutes: "调参间隔" };
  function adaptiveItemsHtml(){
    // 原来「动态调整」列标题上的那个开关（切 trigger_mode=adaptive）随整组撤掉，
    // 挪到这一行最前面——不然页面上再没有入口能切到动态模式。
    var h = "<span class='ad-item ad-mode' data-arow='dynamic'><span class='ad-lb'>动态调整</span>" +
            "<label class='sw2'><input type='checkbox' data-adtrg='1'><span class='tr' data-on='开启' data-off='关闭'></span></label></span>";
    ADAPTIVE_ROWS.forEach(function(it){
      var f = schemaCache[it.k];
      if(!f) return;
      var v = configCache ? configCache[it.k] : null;
      var tip = f.hint || "";
      h += "<span class='ad-item' data-arow='" + esc(it.k) + "' title='" + esc(tip) + "'>" +
           "<span class='ad-lb'>" + esc(ADAPTIVE_NAME[it.k] || nameOf(it.k, f)) + "</span>" +
           "<span class='ad-ctl'>" + fieldControl(it.k, f, v, it.unit, ADAPTIVE_LIM[it.k]) + "</span>" +
           (it.sub ? "<span class='ad-sub'>" + esc(it.sub) + "</span>" : "") + "</span>";
    });
    // v3.72（用户第 9 条）：这一行读作「使用星期数据 [8] 周 [开关]」——数字框在开关左边，
    // 开关关着时框压暗（点开关才解灰，见 renderAdaptiveRow）。
    var fb = schemaCache["adaptive_weekday_bucket"];
    if(fb){
      // 插件还没热重载时，运行中的 schema 里没有这个新键；给个兜底，别把整个框省掉（默认 8 周）。
      var fw = schemaCache["adaptive_weekday_window_weeks"] || { type: "integer", default: 8 };
      var onW = !!(configCache && configCache["adaptive_weekday_bucket"]);
      var wv = (configCache && configCache["adaptive_weekday_window_weeks"] != null) ? configCache["adaptive_weekday_window_weeks"] : 8;
      // v3.74（用户 m00024 第 4-d 条）：「使用星期数据」是另一个功能（按星期分桶统计），
      // 前面加一道竖线跟「每轮调整 / 数据使用」分开。
    h += "<span class='ad-item ad-sep' data-arow='adaptive_weekday_bucket'" + (fb.hint ? " title='" + esc(fb.hint) + "'" : "") + ">" +
           "<span class='ad-lb'>" + esc(nameOf("adaptive_weekday_bucket", fb)) + "</span>" +
           "<span class='ad-ctl" + (onW ? "" : " dimv") + "' data-adwk='1'>" +
             (fw ? fieldControl("adaptive_weekday_window_weeks", fw, wv, "周",
                                ADAPTIVE_LIM["adaptive_weekday_window_weeks"]) : "") +
           "</span>" +
           swPill("adaptive_weekday_bucket", onW, "开启", "关闭", fb.hint || "") + "</span>";
    }
    // v3.84（用户 m04162）：这一行最后加一句实话——现在的动态调整只按固定公式慢慢挪概率，
    // 收益不高，用户自己也说以后可能整块重做；写在页面上，别让人白花力气调。
    h += "<span class='ad-warn' title='现在的动态调整只按固定公式慢慢挪概率，收益不高；以后可能整块重做'>" +
         "⚠ 当前模式收益较低，不建议花精力调整，以后可能会重做</span>";
    return h;
  }
  function paintAdaptiveRow(){
    var box = document.getElementById("bd-adaptive");
    if(!box) return;
    var on = bandsActive();
    var dyn = on && (trgVal() === "adaptive");
    // v3.81（用户 m02645 图3 第二点）：动态调整关掉之后，这一行后面几项（每轮调整 / 数据使用 /
    // 使用星期数据）全部压暗——它们只在动态调整接管时生效，亮着会让人以为还在调。
    // 只压暗、不禁用：允许先把参数备好再开；开关本身不压，否则没法再打开。
    Array.prototype.forEach.call(box.querySelectorAll(".ad-item[data-arow]"), function(it){
      // 开关那一项（data-arow=dynamic）不压暗，否则关掉之后开关自己也灰着看不清
      it.classList.toggle("dimv", !dyn && it.getAttribute("data-arow") !== "dynamic");
    });
    box.classList.toggle("dim", !on);
    var sw = box.querySelector("input[data-adtrg]");
    if(sw){
      sw.checked = dyn;
      sw.disabled = !on;
    }
  }
  var bandStatsData = null;
  function bandStatOf(b){
    var st = bandStatsData;
    if(!st || !st.bands || !st.bands.length) return null;
    var i, x;
    for(i = 0; i < st.bands.length; i++){
      x = st.bands[i];
      if(Number(x.s) === b.s && Number(x.e) === b.e) return x;
    }
    var mid = (b.s + b.e - 1) / 2;
    for(i = 0; i < st.bands.length; i++){
      x = st.bands[i];
      if(mid >= Number(x.s) && mid < Number(x.e)) return x;
    }
    return null;
  }
  function bandShownP(b){
    var st = bandStatOf(b);
    var p = st ? Number(st.p) : NaN;
    return isFinite(p) ? Math.round(p) : b.v;
  }
  // 用户 m03323：开了「使用星期数据」之后，动态调整面板要一天一行地看数据。
  // 这里把 /band-stats?week=1 的 week 数组摆成 [周一 .. 周日]，
  // 每格 = 这个时段在那一天的平均（没统计到就是 null）。
  function bandWeekRows(b){
    var d = bandStatsData;
    if(!d || !Array.isArray(d.week) || !d.week.length) return null;
    var out = [null, null, null, null, null, null, null], j, k;
    var mid = (b.s + b.e - 1) / 2;
    for(j = 0; j < d.week.length; j++){
      var w = d.week[j] || {};
      var wd = Number(w.wd);
      if(!(wd >= 0 && wd <= 6)) continue;
      var rows = w.bands || [], hit = null;
      for(k = 0; k < rows.length; k++){
        if(Number(rows[k].s) === b.s && Number(rows[k].e) === b.e){ hit = rows[k]; break; }
      }
      if(!hit){
        for(k = 0; k < rows.length; k++){
          if(mid >= Number(rows[k].s) && mid < Number(rows[k].e)){ hit = rows[k]; break; }
        }
      }
      out[wd] = { row: hit, days: Number(w.days) || 0, active: Number(w.active_days) || 0 };
    }
    return out;
  }
  // 这一天在这个时段有没有东西可看：没统计到、或那天这个时段压根没人说话，
  // 都当空着（显示「—」并压暗）——「0 条 · 0 次」既不中看也不说明问题。
  function weekCellEmpty(info){
    if(!info || !info.row) return true;
    var r = info.row;
    return !(Number(r.m_day) > 0) && !(Number(r.t_day) > 0);
  }
  function weekCellText(info){
    if(weekCellEmpty(info)) return "—";
    var r = info.row;
    return bpNum(r.m_day, " 条") + " · " + bpNum(r.t_day, " 次触发") +
           " · " + bpNum(r.r_day, " 次回复") + " · " + bpNum(r.reply_rate, "%");
  }
  // 面板开着的时候统计才回来（第一次打开一定是先弹面板、后到数据）：只回填数据格，
  // 不重画面板——重画会把已经输了一半的数字清掉。
  function refreshWeekCells(){
    var pop = document.getElementById("bd-pop");
    if(!pop || !pop.getAttribute("data-wk")) return;
    var idx = Number(pop.getAttribute("data-wk-idx"));
    var b = bandBands && bandBands[idx];
    if(!b) return;
    var rows = bandWeekRows(b);
    Array.prototype.forEach.call(pop.querySelectorAll("[data-wdat]"), function(el){
      var wd = Number(el.getAttribute("data-wdat"));
      var info = rows ? rows[wd] : null;
      el.textContent = weekCellText(info);
      el.classList.toggle("none", weekCellEmpty(info));
      el.title = weekCellEmpty(info)
        ? "这一天在这个时段还没有统计到数据"
        : ("这一天在这个时段统计了 " + info.days + " 天（其中有消息的 " + info.active + " 天）");
    });
  }
  function loadBandStats(){
    var wantWk = !!(configCache && configCache["adaptive_weekday_bucket"]);
    var q = [];
    if(scopeSid) q.push("sid=" + encodeURIComponent(scopeSid));
    if(wantWk) q.push("week=1");
    var url = API + "/band-stats" + (q.length ? ("?" + q.join("&")) : "");
    get(url).then(function(d){
      bandStatsData = (d && d.ok) ? d : null;
      refreshBandBox();
      refreshWeekCells();
    }).catch(function(){ bandStatsData = null; });
  }
  // v3.73（用户 m24640 第 5 条）：曲线模式的休息时段改成「段上的月亮标记」——
  // 不再有开关与起止时间，只剩一个「休息概率」（点了月亮的段按这个概率掷骰）。
  function bandRestProb(){
    var n = Number(configCache && configCache["band_rest_probability"]);
    if(!isFinite(n)){
      var f = schemaCache && schemaCache["band_rest_probability"];
      n = Number(f && f.default);
    }
    if(!isFinite(n)) n = 2;
    return Math.max(0, Math.min(100, Math.round(n)));
  }
  function bandsBlockHtml(){
    var bands = parseBands(String((configCache && configCache.time_bands) || ""));
    return "<input type='hidden' id='bands-val' data-k='time_bands' value='" +
           esc(JSON.stringify(bands)) + "'>" +
           "<div class='bandbox' id='bandbox'>" +
           // v3.74（用户 m00024 第 4-c / 4-e 条）：说明压成一句（具体用法挪到图下面带符号的条目里），
           // 右边加一个「隐藏划分」开关——只想看曲线时把色块、分界线、月亮全收起来。
           "<div class='bd-head'>" +
           "<div class='bd-hint'>一天切 48 格（每格 30 分钟），整条时间轴连续铺满，每段一个触发概率。" +
           "改完自动保存。</div>" +
           // v3.75（用户本轮第 2 条）：右边一列两个控件——「曲线」下拉（勾哪条画哪条，默认全勾）
           // 摆在「隐藏划分」开关左边，顺序就是用户要的「下拉 → 隐藏划分」。
           "<div class='bd-curves'>" +
             "<button type='button' class='bd-cbtn' id='bd-cur-btn' title='这张图要画哪几条曲线'>曲线 ▾</button>" +
             "<div class='bd-cmenu' id='bd-cur-menu' hidden>" +
               "<label><input type='checkbox' data-cur='u'" + (bandShow.u ? " checked" : "") +
                 "><span class='dot u'></span>用户消息</label>" +
               "<label><input type='checkbox' data-cur='b'" + (bandShow.b ? " checked" : "") +
                 "><span class='dot b'></span>bot 回复</label>" +
             "</div>" +
           "</div>" +
           "<label class='sw2 bd-hide' title='只留曲线：藏起时间段色块、分界线与月亮'>" +
             "<span class='ad-lb'>隐藏划分</span>" +
             "<input type='checkbox' id='bd-hide-seg'><span class='tr' data-on='开启' data-off='关闭'></span></label>" +
           "</div>" +
           "<div class='bd-chart' id='bd-chart'></div>" +
           "<div class='bd-axis' id='bd-axis'></div>" +
           "<div class='bd-list' id='bd-list'></div>" +
           // v3.71（用户第 7 条）：动态调整那几项平铺在曲线说明下面一行
           "<div class='bd-adaptive' id='bd-adaptive'></div>" +
           "</div>";
  }
  // 每次改动都写回隐藏 input —— 走既有的自动保存通道（collectSettings 只扫 [data-k]）
  function spreadBandTags(chart){
    if(!chart) return;
    // 这块图在没切到「聊天决策」时是 display:none，量出来全是 0——等它第一次有宽度
    // 再错开一次（ResizeObserver 在 0 → 有宽度时正好触发）。
    if(!chart._bdRo && typeof ResizeObserver === "function"){
      chart._bdRo = new ResizeObserver(function(){ spreadBandTags(chart); });
      chart._bdRo.observe(chart);
    }
    if(!chart.offsetWidth) return;
    var tags = [].slice.call(chart.querySelectorAll(".bd-tag"));
    var dels = [].slice.call(chart.querySelectorAll(".bd-del"));
    // 先把上一次挪过的位置还原，重复调用不会越挪越下
    tags.concat(dels).forEach(function(el){
      if(el.hasAttribute("data-bdtop")) el.style.top = el.getAttribute("data-bdtop");
    });
    var placed = [];
    tags.forEach(function(el){
      if(!el.hasAttribute("data-bdtop")) el.setAttribute("data-bdtop", el.style.top || "");
      var idx = el.getAttribute("data-seg");
      var del = (idx === null) ? null : chart.querySelector(".bd-del[data-del='" + idx + "']");
      if(del && !del.hasAttribute("data-bdtop")) del.setAttribute("data-bdtop", del.style.top || "");
      var w = el.offsetWidth || 0, h = el.offsetHeight || 0;
      if(!w || !h) return;
      var l = el.offsetLeft - w / 2;
      var t0 = el.offsetTop, t = t0;
      var guard = 0;
      while(guard++ < 8){
        var hit = false;
        for(var i = 0; i < placed.length; i++){
          var p = placed[i];
          if(l < p.r && p.l < l + w && t < p.b && p.t < t + h){ hit = true; break; }
        }
        if(!hit) break;
        t += h + 4;
      }
      if(t !== t0){
        el.style.top = t + "px";
        if(del) del.style.top = (del.offsetTop + (t - t0)) + "px";
      }
      placed.push({l:l, r:l + w, t:t, b:t + h});
    });
  }
  function paintBandChart(){
    var chart = document.getElementById("bd-chart");
    if(!chart || !bandBands) return;
    var axis = document.getElementById("bd-axis");
    var list = document.getElementById("bd-list");
    var bands = bandBands;
    var yMax = bandsYMax(bands);
    // v3.74（用户 m00024 第 4-e 条）：隐藏划分时只留曲线（消息量折线 + 概率阶梯线 + 刻度），
    // 色块 / 分界手柄 / 概率胶囊全部由 .no-seg 藏起来。
    chart.classList.toggle("no-seg", !!bandHideSeg);
    // v3.75（用户本轮第 2 条）：两条曲线共用一把尺子（取两个峰值的较大者），
    // 左边 Y 轴的刻度也按这把尺子标，读数才跟线的位置对得上。
    var uMax = 1, bMax = 1;
    (bandSlots || []).forEach(function(n){ if(n > uMax) uMax = n; });
    (bandBotSlots || []).forEach(function(n){ if(n > bMax) bMax = n; });
    var sMax = Math.max(1, bandShow.u ? uMax : 1, bandShow.b ? bMax : 1);
    var html = "<div class='bd-grid'>";
    for(var i = 0; i <= BAND_SLOTS; i += 2){
      html += "<span style='left:" + (i / BAND_SLOTS * 100) + "%'></span>";
    }
    html += "</div>";
    // 横向刻度线 + 左侧 Y 轴刻度尺（单位＝条数，7 天累计到这一格）
    var hgHtml = "", ysHtml = "";
    for(var k = 0; k <= 4; k++){
      var kv = Math.round(sMax * k / 4);
      var ky = 100 - kv * 94 / sMax;
      if(k > 0) hgHtml += "<span style='top:" + ky.toFixed(2) + "%'></span>";
      ysHtml += "<span style='top:" + Math.min(97.5, ky).toFixed(2) + "%'>" + kv + "</span>";
    }
    html += "<div class='bd-hgrid'>" + hgHtml + "</div>";
    if(bandShow.u) html += bandsLineSvg(bandSlots, "bd-line", sMax);
    if(bandShow.b && bandBotSlots) html += bandsLineSvg(bandBotSlots, "bd-line bot", sMax);
    // v3.69（用户 m21003 第 6/7 条）：段本身就是一个淡色块，里面放三样东西——
    // 右上角的月亮（休息时段开关，平时藏着，鼠标停上来才出）、
    // 选中时左右两角的时间、以及叠在上面的概率胶囊（下面那段）。
    bands.forEach(function(b, idx){
      var l = b.s / BAND_SLOTS * 100, wd = (b.e - b.s) / BAND_SLOTS * 100;
      // v3.73（用户 m24640 第 5 条）：休息段在段右上角挂一个月亮（点一下切换）
      var cls = "bd-seg" + (idx === bandSel ? " on" : "") + (b.rest ? " rest" : "");
      html += "<div class='" + cls + "' data-seg='" + idx +
              "' style='left:" + l + "%;width:" + wd + "%'>" +
              "<i class='bd-fill2' style='background:" + bandTint(idx) + "'></i>" +
              "<button type='button' class='bd-moon' data-moon='" + idx + "' title='" +
                (b.rest ? "这一段是休息时段，点一下取消" : "把这一段标成休息时段") + "'>☾</button>" +
              "<span class='bd-time' style='left:2px'>" + slotTime(b.s) + "</span>" +
              "<span class='bd-time' style='right:2px'>" + slotTime(b.e) + "</span>" +
              "</div>";
    });
    html += bandsStepSvg(bands, yMax, "bd-prob");
    bands.forEach(function(b, idx){
      if(idx > 0) html += "<div class='bd-handle' data-edge='" + idx + "' style='left:" +
                          (b.s / BAND_SLOTS * 100) + "%'></div>";
    });
    // v3.71（用户第 6 条）：动态调整开着时，胶囊显示「目前概率」（band-stats 给的 p），
    // 配置值留在 title 里——「配置 10% → 现在 14%」。
    var dynTag = (trgVal() === "adaptive") && bandsActive();
    bands.forEach(function(b, idx){
      var l = b.s / BAND_SLOTS * 100, wd = (b.e - b.s) / BAND_SLOTS * 100;
      var ty = 1 - b.v / yMax;
      var tp = (ty * 252 < 44) ? ("calc(" + (ty * 100).toFixed(1) + "% + 9px)")
                               : ("calc(" + (ty * 100).toFixed(1) + "% - 30px)");
      // v4.90（用户 m01875 第 2 条）：概率胶囊头顶挂一个垃圾桶——切多了的段点它删掉
      // （并进前一段；第一段并进后一段）。只剩一段时禁用，全天至少要有一段。
      // 胶囊贴到图顶时上面没地方，就挪到胶囊下面。
      var _pillTop = (ty * 252 < 44) ? (ty * 252 + 9) : (ty * 252 - 30);
      var _delTop = _pillTop - 21;
      if(_delTop < 1) _delTop = _pillTop + 22;
      html += "<button type='button' class='bd-del' data-del='" + idx + "'" +
              (bands.length > 1 ? "" : " disabled") +
              " title='" + esc(bands.length > 1 ? "删掉这一段（并进前一段）" : "只剩一段了，删不掉") + "'" +
              " style='left:clamp(10px," + (l + wd / 2) + "%,calc(100% - 10px));top:" + _delTop.toFixed(1) + "px'>" +
              "<svg viewBox='0 0 16 16' width='12' height='12' aria-hidden='true'>" +
              "<path d='M3.2 4.6h9.6M6.4 4.6V3.1h3.2v1.5M4.7 4.6l.55 8.1h5.5l.55-8.1' " +
              "fill='none' stroke='currentColor' stroke-width='1.3' stroke-linecap='round' " +
              "stroke-linejoin='round'/></svg></button>";
      // v3.81（用户 m02645 图3）：休息段那一格写的不是段概率，而是「休息概率」
      // （band_rest_probability，默认 2%）——休息段只按它回，段概率对它完全不起作用，
      // 写 10% 只会让人以为点进去改的是段概率。
      if(b.rest){
        var rp = bandRestProb();
        html += "<div class='bd-tag rest' data-seg='" + idx +
                "' title='" + esc("休息时段：只按 " + rp + "% 回，跟段概率无关；点一下改休息概率") +
                "' style='left:clamp(26px," + (l + wd / 2) + "%,calc(100% - 26px));top:" + tp +
                ";color:#6366f1;border-color:rgba(99,102,241,.6)'>☾ " + rp + "%</div>";
        return;
      }
      var shown = dynTag ? bandShownP(b) : b.v;
      var tip = dynTag ? ("配置 " + b.v + "% → 现在 " + shown + "%（按目标自动调）")
                       : (b.v + "%：点一下改这一段的触发概率");
      html += "<div class='bd-tag" + (dynTag ? " dyn" : "") + "' data-seg='" + idx +
              "' title='" + esc(tip) + "'" +
              " style='left:clamp(24px," + (l + wd / 2) + "%,calc(100% - 24px));top:" + tp + ";color:" + bandTint(idx) +
              ";border-color:" + bandTint(idx) + "'>" + shown + "%</div>";
    });
    // v3.70（用户 m21533 第 4 条）：鼠标停在图上时显示的竖虚线（要切开的位置）
    html += "<div class='bd-cut' id='bd-cut' hidden></div>";
    // v3.75（用户本轮第 1 条）：Y 轴刻度尺与「停在哪个位置显示这个位置的条数」的小气泡
    html += "<div class='bd-ys'>" + ysHtml + "</div>";
    html += "<div class='bd-tip' id='bd-tip' hidden></div>";
    chart.innerHTML = html;
    spreadBandTags(chart);
    if(axis){
      var ah = "";
      for(var h = 0; h <= 24; h += 3){
        ah += "<span style='left:" + (h / 24 * 100) + "%'>" + (h < 10 ? "0" : "") + h + ":00</span>";
      }
      axis.innerHTML = ah;
    }
    // v3.69（用户 m21003 第 6 条）：列表里的「切分 / 删除」按钮撤掉了（用户说点了没用），
    // 概率改成在图上点胶囊改、双击段内切分、拖分界合并；这里只留一行汇总。
    if(list){
      // v3.70（用户 m21533 第 4 条）：底部那一行改成左侧的曲线用法注释（原来这里写休息时段）。
      // v3.74（用户 m00024 第 4-c 条）：用法说明从一整段灰字拆成带符号的条目，一条一件事。
      if(bandHideSeg){
        // v3.75（用户本轮第 1 条）：隐藏之后最想问的是「这一格到底多少条」，所以这里直接把
        // 悬停读数写在说明里，别让人再去找。
        list.innerHTML = "<div class='bd-helps'><span class='bd-help'>" +
          "<b>◎</b>划分已隐藏——只显示曲线；鼠标停在图上任意位置＝看那 30 分钟有多少条。" +
          "要把色块 / 分界线 / 月亮放回来，把右上角「隐藏划分」关掉。</span></div>";
      }else{
        list.innerHTML = "<div class='bd-helps'>" +
          "<span class='bd-help'><b>✂</b>贴住格线点一下＝在那儿切开</span>" +
          "<span class='bd-help'><b>⇔</b>拖分界上的小圆点＝移动分界（拖过头就并段）</span>" +
          "<span class='bd-help'><b>%</b>点段上的数字＝改这一段概率</span>" +
          "<span class='bd-help'><b>☾</b>点段右上角的月亮＝这一段是休息时段</span>" +
          "<span class='bd-help'><b>🗑</b>点概率胶囊头顶的垃圾桶＝删掉这一段（并进前一段）</span>" +
          "<span class='bd-help'><b>◷</b>鼠标停在段上＝看它的起止时间</span>" +
          "</div>";
      }
    }
  }
  // 底图数据（近 7 天分布）在 loadHours 回来后重画一次；没有内联块时直接跳过
  function refreshBandBox(){ paintBandChart(); }
  // v3.71（用户第 2 条前半）：以前点胶囊是把裸 <input> 塞进胶囊里（用户嫌丑，说太简陋），
  // 改成在胶囊下面弹一张小卡片：普通模式改这一段的概率，动态模式看这一段的时段统计并设目标。
  function closeBandPop(){
    var el = document.getElementById("bd-pop");
    if(el && el.parentNode) el.parentNode.removeChild(el);
    var mk = document.getElementById("bd-mask");
    if(mk && mk.parentNode) mk.parentNode.removeChild(mk);
  }
  function bpNum(v, unit){
    var n = Number(v);
    if(v === null || v === undefined || !isFinite(n)) return "—";
    return String(Math.round(n * 10) / 10) + (unit || "");
  }
  function openChipInput(chips){
    var inp = chips.querySelector(".chip-in"), add = chips.querySelector(".chip-add");
    if(!inp) return;
    inp.hidden = false;
    if(add) add.hidden = true;
    try{ inp.focus(); }catch(e){}
  }
  function closeChipInput(inp){
    if(!inp) return;
    inp.value = "";
    inp.hidden = true;
    var add = inp.parentNode && inp.parentNode.querySelector(".chip-add");
    if(add) add.hidden = false;
  }
  // v3.50（用户 m06761 第 7 条）：管理员那一行的两个输入框——回车加入、Esc 收起，
  // 跟外号那个输入框一个手感（键名带 ad- 前缀，别人扫 .chip-in 时不会误认）。
  function adminFormOf(node){
    return (node && node.closest) ? node.closest(".adform") : null;
  }
