  var FILE_ICON = { zip: "🗜", rar: "🗜", "7z": "🗜", gz: "🗜", tar: "🗜",
                    pdf: "📕", doc: "📘", docx: "📘", xls: "📗", xlsx: "📗", ppt: "📙", pptx: "📙",
                    txt: "📄", log: "📄", dmp: "🧩", exe: "⚙", apk: "📦", iso: "💿",
                    mp4: "🎬", mov: "🎬", mp3: "🎵", wav: "🎵",
                    png: "🖼", jpg: "🖼", jpeg: "🖼", gif: "🖼", webp: "🖼" };
  function fmtSize(n){
    n = Number(n) || 0;
    if(n <= 0) return "";
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while(n >= 1024 && i < u.length - 1){ n = n / 1024; i++; }
    var v = (i === 0) ? String(Math.round(n)) : n.toFixed(n >= 100 ? 0 : (n >= 10 ? 1 : 2));
    return v + " " + u[i];
  }
  function fileExt(name){
    var mm = String(name || "").match(/\.([A-Za-z0-9]{1,8})$/);
    return mm ? mm[1].toLowerCase() : "";
  }
  function fileHtml(f){
    if(!f || !f.name) return "";
    var pt = String(f.path || "");
    var ext = fileExt(f.name);
    var ic = FILE_ICON[ext] || (String(f.kind || "").indexOf("archive") >= 0 ? "🗜" : "📄");
    var size = fmtSize(f.size);
    // v4.22（用户 m21783）：文件消息画成 QQ 那样的文件卡——图标 + 文件名 + 大小。
    // 路径只在 title 里留着，正文里不再露一长串。
    return "<div class='fcard' title='" + esc(pt ? f.name + " · " + pt : f.name) + "'>" +
           "<span class='fico' aria-hidden='true'>" + esc(ic) + "</span>" +
           "<span class='fmain'><span class='fname'>" + esc(f.name) + "</span>" +
           (size ? "<span class='fsub'>" + esc(size) + "</span>" : "") +
           "</span></div>";
  }
  // v4.22：一条消息里可能叠着好几个文件（合并发送），后端把文件名/大小挂在 m.files 上；
  // 上下文正文里仍然是 [文件]，缓存前缀一个字节都没动。
  function filesHtml(list){
    if(!list || !list.length) return "";
    var out = "";
    list.forEach(function(f){
      if(f && f.name) out += fileHtml({ name: f.name, size: f.size, kind: f.kind });
    });
    return out ? "<div class='fcards'>" + out + "</div>" : "";
  }
  // 别人的表情 / 图片：后端收到消息就存了一份到 data/session_media（v2.60），
  // 这里按 path 走插件的 /media 接口取二进制。没有 media 就是没图，不猜。
  // 对照档要的是「模型看到的那段文字」，图片本身不占版面（用户 m29886）。
  // 原文档的 [图片] 由 rawParts 给，这里只管美化档。
  // 缩略图行：转发卡内部和正文外面都复用它。后端现在给三种形状——
  //   {path} 走插件的 /media；{url} 是 http 链接或 data URL，直接当 src；
  //   {missing:1} 说明原图已被框架清理、插件也没留到副本，画占位，别画裂图。
  // 与 thumbsHtml 一致：缺失图会画占位，无地址且未标缺失的空项不算已显示。
  function renderedThumbCount(list){
    return (Array.isArray(list) ? list : []).filter(function(it){
      var obj = (typeof it === "string") ? { path: it } : (it || {});
      return !!(obj.missing || obj.path || obj.url);
    }).length;
  }
  function thumbsHtml(list){
    if(!list || !list.length) return "";
    var out = "";
    // 用户 m02993 图11：图片有时突然不显示——那一次 /media 没取到（索引一时没命中、
    // 或缓存文件刚被裁掉），回来是 404/非图片，而这里原本只把 <img> 摘掉，页面上就剩
    // 一个空壳，而且同一个 src 不会再试。现在：第一次失败换 &r=<时间戳> 再要一遍
    // （破掉浏览器可能缓存的失败结果，也等于给后端一次自愈的机会）；第二次仍失败才
    // 收成占位，并且把 data-img / role 摘掉——不然点开大图还是一片空白。
    var onerr = " onerror=\"if(!this.getAttribute('data-retry')){this.setAttribute('data-retry','1');" +
                "var s=this.getAttribute('data-src')||this.src;" +
                "this.src=s+(s.indexOf('?')<0?'?':'&')+'r='+Date.now();return;}" +
                "var n=this.parentNode;n.className='thumb bad fr';" +
                "n.removeAttribute('data-img');n.removeAttribute('data-imgt');" +
                "n.setAttribute('role','img');n.removeAttribute('tabindex');" +
                "n.removeChild(this);\"";
    list.forEach(function(it){
      var obj = (typeof it === "string") ? { path: it } : (it || {});
      var p = obj.path || "", direct = obj.url || "";
      // 用户 m02993 图11：缺失占位没有 path，先判 missing 再判空，否则占位被吞。
      if(obj.missing){
        out += "<span class='thumb bad' title='这张图没留下来：框架清理掉了，插件里也没有副本'></span>";
        return;
      }
      if(!p && !direct) return;
      var url = direct || (API + "/media?path=" + encodeURIComponent(p) + (ctxSid ? "&sid=" + encodeURIComponent(ctxSid) : ""));
      // 用户 m03343 第 2 条：点图就地弹大图（data-img），不再 href 开新标签页；
      // data-imgt 给弹层做 title，也方便排查是哪张。
      out += "<span class='thumb' role='button' tabindex='0' data-img='" + esc(url) + "' data-imgt='" + esc(p || direct) + "'" +
             " title='" + esc(p || direct) + "'>" +
             // decoding=async（用户 m11091 第 8 条）：原图动辄一两千像素，同步解码会
             // 卡在滚动那一帧上；交给浏览器在后台线程解
             "<img loading='lazy' decoding='async' data-src='" + esc(url) + "' src='" + esc(url) +
             "' alt='图片'" + onerr + "></span>";
    });
    return out ? "<div class='thumbs'>" + out + "</div>" : "";
  }
  function mediaHtml(m, mode){
    if(mode === "mixed" || mode === "raw") return "";
    return thumbsHtml(m && m.media);
  }
  // 用户本轮图1/图2：美化后的元素顺序要跟原文一致。
  // 以前图片统一堆在正文前面（引用 → @ → 图 → 文字），现在按正文里 [图片]
  // 占位出现的先后逐张插回去；占位数和图片数对不上就返回 null，调用方退回老排法
  // （认不准时宁可不动，别把别人的图挪错位置）。
  function planInlineMedia(text, media, mode){
    try{
      if(mode === "mixed" || mode === "raw") return null;
      if(!media || !media.length) return null;
      var re = /\[(?:Image attached|图片|表情|image)\]/gi;
      var hits = String(text || "").match(re);
      if(!hits || hits.length !== media.length) return null;
      return { parts: String(text || "").split(re), media: media.slice(0, media.length) };
    }catch(e){ return null; }
  }
  function renderInlineMedia(plan){
    var html = "", parts = (plan && plan.parts) || [], media = (plan && plan.media) || [];
    for(var i = 0; i < parts.length; i++){
      var seg = String(parts[i] || "").replace(/^\s+|\s+$/g, "");
      if(seg) html += bodyHtml(seg, null);
      if(i < media.length) html += thumbsHtml([media[i]]);
    }
    return html;
  }
  // 数一条文本里还剩几个图片占位。引用块里的和本段正文里的要分开数，
  // 才知道哪张图该进引用块、哪张该留在卡外（用户 m01680）。
  var IMG_TAG_N = /\[(?:Image attached|图片|表情|image)\]/gi;
  function imgTagCount(t){
    var hits = tidyBody(t).match(IMG_TAG_N);
    return hits ? hits.length : 0;
  }
  // 一条消息里的图按各段自己的占位认领：引用里的归那段的引用块，正文里的归那段卡外。
  // 占位总数和图片数对不上就认不准是哪张，返回 null 让调用方沿用原来的排法。
  function claimMediaBySegs(parts, media){
    var pool = media || [];
    var needTotal = 0;
    parts.forEach(function(q){
      needTotal += (q.meta.reply ? imgTagCount(q.meta.reply.text) : 0) + imgTagCount(q.body);
    });
    if(!needTotal || needTotal !== pool.length) return null;
    var cursor = 0;
    return parts.map(function(q){
      var rn = q.meta.reply ? imgTagCount(q.meta.reply.text) : 0;
      var got = pool.slice(cursor, cursor + rn + imgTagCount(q.body));
      cursor += got.length;
      return { quote: got.slice(0, rn), own: got.slice(rn) };
    });
  }
  // 纯文本/引用内的闲鱼分享也只使用来源标题与 http(s) 链接，不猜商品信息。
  function sharePattern(){
    // 只吃完整分享模板的尾句（可另起一行）；普通聊天里的同句原样保留。
    // 标题两版都在用：「」和【】。只认一种就会匹配失败，卡片画不出来、
    // 正文里还留一行「点击链接直接打开」（用户 m51324 图5/图6）
    return /【闲鱼】\s*(https?:\/\/[^\s「」【】<>]+)([^\n「【]{0,240})(?:「([^」\n]+)」|【([^\n]+?)】(?=\s|$|点击链接))(?:\s*点击链接直接打开[。.!！]?(?=[ \t]*(?:$|\r?\n)))?/g;
  }
  function plainShareHtml(text, renderText){
    var src = String(text || ""), re = sharePattern();
    var out = "", end = 0, m;
    while((m = re.exec(src))){
      var url = safeWebUrl(m[1]);
      if(!url) continue;
      out += renderText(src.slice(end, m.index)) + cardHtml({app: "闲鱼", title: m[3] || m[4] || "", url: url});
      end = re.lastIndex;
      // 用户 m01693：卡片和下面那句话中间空出一大截。模板自己带的那行换行还留在正文里，
      // 卡片又是块级元素，紧跟几个空行就成了一段空白；卡片后面的空白一律吃掉。
      while(end < src.length && /\s/.test(src.charAt(end))) end++;
    }
    return end ? out + renderText(src.slice(end)) : null;
  }
  // 用户第 4 条（B 站链接美化）：群里同一条消息里可能有两种长相，窗口里都只是纯文本——
  //   【标题】 https://www.bilibili.com/video/BV1xx411c7mD?p=1
  //   【标题】 https://b23.tv/xxxxx
  // 统一画成和分享卡（cardHtml）同一套的 .qcard 小卡：左边 B 图标、中间标题、
  // 脚下来源域名 + 「打开」，整张卡是一个 <a>。只认 http(s)（走 safeWebUrl 校验），
  // 标题和链接一律 esc 后拼字符串，不拼 innerHTML。
  // 2026-11-01（用户 m05804 图2）：微信公众号文章链接也走这条，复用同一套卡片。
  function biliPattern(){
    // 只吃 ASCII 可见字符：真链接里不会有中文，不然「…/BV1xx，好家伙」会把
    // 后面的中文连标点一起吞进 href（用户第 4 条）
    // 用户 m02993 图9：直播平台也在里面，但 biliHtml 只给「认得出是直播间」的
    // 非 B 站链接画卡，点播页（youtube.com/watch、twitch.tv/videos）照旧留文本。
    return /(https?:\/\/(?:[\w-]+\.)*(?:bilibili\.com|b23\.tv|mp\.weixin\.qq\.com|youtube\.com|youtu\.be|twitch\.tv|douyin\.com|douyu\.com|huya\.com|kuaishou\.com)\/[^\s<>"'「」【】\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]*)/g;
  }
  // 链接前面紧贴的【标题】：标题里可能还套一层【第14集】，
  // 所以从后往前按【】配平找起点，不能简单地 indexOf("【")
  function biliTitle(pre){
    var s = String(pre || ""), i = s.length - 1;
    while(i >= 0 && (s.charAt(i) === " " || s.charAt(i) === "\t")) i--;
    if(i < 0 || s.charAt(i) !== "】") return { title: "", pre: s };
    var d = 0, j = i;
    for(; j >= 0; j--){
      var ch = s.charAt(j);
      if(ch === "】") d++;
      else if(ch === "【"){ d--; if(!d) break; }
    }
    if(j < 0) return { title: "", pre: s };
    var title = s.slice(j + 1, i).trim();
    if(!title || title.length > 160) return { title: "", pre: s };
    return { title: title, pre: s.slice(0, j) };
  }
  // 用户 m02993 图9：同一条哔哩哔哩链接，直播 / 视频 / 专栏 / 动态 是四种东西，
  // 以前不管哪种都写「哔哩哔哩视频」，点开才发现是直播间。
  // b23.tv 这种短链看不出类型，就返回空串（标题退回「哔哩哔哩链接」）。
  function biliKind(url){
    var u = String(url || ""), host = "";
    try { host = new URL(u).hostname.replace(/^www\./, "").toLowerCase(); } catch(e){ host = ""; }
    if(host === "live.bilibili.com" || /\/live\//i.test(u) || /[?&]room_id=/i.test(u)) return "直播";
    if(/\/video\//i.test(u) || /\/bv[0-9a-z]{8,}/i.test(u)) return "视频";
    if(/\/read\//i.test(u) || /\/cv\d+/i.test(u)) return "专栏";
    if(host === "t.bilibili.com" || /\/opus\//i.test(u)) return "动态";
    return "";
  }
  function isWechatUrl(url){
    return /^https?:\/\/mp\.weixin\.qq\.com\/s\//i.test(String(url || ""));
  }
  function biliHtml(text, renderText){
    // 用户第 4 条：正文 / 引用原话里的 B 站链接（bilibili.com 与 b23.tv 两种长相）
    // 统一画成可点的小卡，视觉跟分享卡（.qcard）一套；只放行 http(s)，
    // 标题和链接一律 esc 后拼字符串，不拼 innerHTML
    var src = String(text || ""), re = biliPattern(), out = "", end = 0, m;
    while((m = re.exec(src))){
      var raw = m[1], url = raw, tail = "";
      // 链接后面紧跟中文标点时不吞进 href（…/BV1xx，好家伙）
      var cutP = url.match(/[，。、；：）】」』！？,.;:!?]+$/);
      if(cutP){ tail = cutP[0]; url = url.slice(0, url.length - tail.length); }
      var href = safeWebUrl(url);
      if(!href) continue;   // 不是 http(s) 就留给下面的文本，绝不拼进 href
      var cut = biliTitle(src.slice(end, m.index));
      // 2026-11-01（用户 m05804 图2）：微信文章不猜标题、不发请求，先给一张
      // 固定标题的站内卡；链接前面若带了【标题】，就用那个标题。
      if(isWechatUrl(href)){
        out += renderText(cut.pre) +
               cardHtml({ app: "微信公众号", title: cut.title || "微信公众号文章", url: href });
        end = m.index + raw.length - tail.length;
        continue;
      }
      // 用户 m02993 图9：非 B 站域名只有认得出是直播间才画卡，否则整段留给文本
      var live = liveInfo(href), hh = "";
      try { hh = new URL(href).hostname.replace(/^www\./, "").toLowerCase(); } catch(e2){ hh = ""; }
      if(!/(^|\.)(bilibili\.com|b23\.tv)$/.test(hh) && !live) continue;
      // 用户 m02993 图9：卡片的标题位写清是哪种内容，别一律「哔哩哔哩视频」；
      // 直播间写「直播间 <房号>」，配 LIVE 图标 + 红色「直播」徽标。
      var kind = live ? "直播" : biliKind(href);
      var defTitle = live ? ("直播间" + (live.room ? " " + live.room : ""))
                          : (kind ? ("哔哩哔哩" + kind) : "哔哩哔哩链接");
      out += renderText(cut.pre) +
             cardHtml({ app: live ? (live.site || "直播") : "哔哩哔哩",
                        title: cut.title || defTitle, kind: kind, url: href,
                        lt: (!live && !cut.title) ? 1 : 0 });
      end = m.index + raw.length - tail.length;
    }
    return end ? out + renderText(src.slice(end)) : null;
  }
  // 正文里夹着的 B 站分享链接：richText → biliHtml 会就地画卡，卡片被塞进 .mono 里，
  // 变成块级兄弟掉在文字下面，还跟着一行「33 字」（用户 m13750 图：卡片在下面、
  // 不在右侧；那条「Mcp_management service」就是这么来的）。这里先把链接抽成卡，
  // 交给外面排版，正文只剩文字，才能跟纯 Json card 那条（喝水喜欢留杯底）长一个样。
  // 返回 null 表示这条正文里没有可抽的卡，调用方一切照旧。
  function pullTextCards(txt){
    var src = String(txt || ""), re = biliPattern(), html = "", rest = "", end = 0, m, hit = false;
    while((m = re.exec(src))){
      var raw = m[1], url = raw, tail = "";
      var cutP = url.match(/[，。、；：）】」』！？,.;:!?]+$/);
      if(cutP){ tail = cutP[0]; url = url.slice(0, url.length - tail.length); }
      var href = safeWebUrl(url);
      if(!href) continue;
      var cut = biliTitle(src.slice(end, m.index));
      // 微信文章和 B 站链接一样先抽出来，正文不会被卡在 .mono 里掉到下一行
      if(isWechatUrl(href)){
        rest += cut.pre;
        html += cardHtml({ app: "微信公众号", title: cut.title || "微信公众号文章", url: href });
        end = m.index + raw.length - tail.length;
        hit = true;
        continue;
      }
      var hh = "", live = liveInfo(href);
      try { hh = new URL(href).hostname.replace(/^www\./, "").toLowerCase(); } catch(e3){ hh = ""; }
      // 只有 B 站自己（以及认得出的直播间）才抽成卡，别的平台留给 richText 原样画
      if(!/(^|\.)(bilibili\.com|b23\.tv)$/.test(hh) && !live) continue;
      var kind = live ? "直播" : biliKind(href);
      var defTitle = live ? ("直播间" + (live.room ? " " + live.room : ""))
                          : (kind ? ("哔哩哔哩" + kind) : "哔哩哔哩链接");
      rest += cut.pre;
      html += cardHtml({ app: live ? (live.site || "直播") : "哔哩哔哩",
                         title: cut.title || defTitle, kind: kind, url: href,
                         lt: (!live && !cut.title) ? 1 : 0 });
      end = m.index + raw.length - tail.length;
      hit = true;
    }
    if(!hit) return null;
    rest += src.slice(end);
    rest = rest.replace(/[ \t]+$/gm, "").replace(/\n{2,}/g, "\n").replace(/^\s+|\s+$/g, "");
    return { html: html, rest: rest };
  }
  // 用户 m12833 第 2 条：推文链接（x.com / twitter.com 的 status）在正文里只是纯文本，
  // 看不出里面说了什么。正文渲染是同步的、浏览器又直连不了 x.com，所以先画一张骨架卡
  // （带 data-tw），整段渲染完由 fillTweets() 向后端要数据再重画成完整卡片。
  // 用户 m00468：链接尾巴的 ?a=...&s=19 没被吃掉，被当成正文画在卡片下面。
  // 2026-11-18：/photo/1 这种路径尾巴同样漏——待发里 X 卡下面挂着一行
  // 「/photo/1?a=…&s=19」。尾巴改成从 / ? # 任一字符起都吃掉。
  var TW_RE = /https?:\/\/(?:www\.|mobile\.)?(?:x|twitter)\.com\/(?:([A-Za-z0-9_]{1,20})\/)?status(?:es)?\/(\d{6,25})(?:[/?#][^\s<>"'「」【】\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]*)?/gi;
  var TW_LOGO = "<svg viewBox='0 0 24 24' width='15' height='15' aria-hidden='true'><path fill='currentColor' d='M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z'/></svg>";
  var TW_CACHE = Object.create(null);
  // 用户 m24364：推文图按群/私聊归属，同一条推文在不同会话各记一次。
  var TW_OWNED = Object.create(null);
  function twSid(){ return String(curSid || ""); }
  function twOwnKey(id){ return twSid() + "|" + id; }
  function twNum(n){
    n = Number(n) || 0;
    if(n >= 100000000) return (n / 100000000).toFixed(1).replace(/\.0$/, "") + "亿";
    if(n >= 10000) return (n / 10000).toFixed(1).replace(/\.0$/, "") + "万";
    return String(n);
  }
  function twDur(s){
    s = Math.round(Number(s) || 0);
    if(s <= 0) return "";
    var m = Math.floor(s / 60), r = s % 60;
    return m + ":" + (r < 10 ? "0" + r : r);
  }
  function tweetSkeleton(id, handle){
    return "<a class='qcard tw' data-tw='" + esc(id) + "' href='https://x.com/i/status/" + esc(id) +
           "' target='_blank' rel='noopener noreferrer'>" +
           "<span class='qc-row'><span class='qc-logo tw'>" + TW_LOGO + "</span>" +
           "<span class='qc-main'><span class='qc-t'>" + (handle ? "@" + esc(handle) : "推文") + "</span>" +
           "<span class='qc-d'>X（推特）</span></span><span class='qc-go'>↗</span></span></a>";
  }
  function tweetHtml(text, renderText){
    // 必须每调用一次新建一个正则对象：renderText 就是 richText，它会递归回到这里，
    // 共用一个全局正则的话 lastIndex 被内层重置，外层 while 永远走不完（死循环到
    // 「Invalid string length」）。用户 m12833 第 2 条落地时的第一个 bug。
    var src = String(text || ""), out = "", end = 0, m;
    var re = new RegExp(TW_RE.source, "gi");
    while((m = re.exec(src))){
      out += renderText(src.slice(end, m.index)) + tweetSkeleton(m[2], m[1] || "");
      end = re.lastIndex;
    }
    return end ? out + renderText(src.slice(end)) : null;
  }
  function tweetCardHtml(d){
    var name = String(d.name || d.handle || "推文");
    // 头像和配图都由后端代取后存进插件自己的 media_cache（浏览器直连 pbs.twimg.com
    // 会卡到超时），这里一律换成同源的 /media；万一后端给的是外链才原样用。
    function twSrc(v){
      var s = String(v || "");
      if(!s) return "";
      return s.indexOf("http") === 0 ? s : (API + "/media?path=" + encodeURIComponent(s));
    }
    var av = twSrc(d.avatar);
    var med = (d.media || []).slice(0, 4);
    var medHtml = "";
    if(med.length){
      medHtml = "<span class='tw-med n" + med.length + "'>" + med.map(function(x){
        var th = twSrc(x.thumb || x.url);
        if(!th) return "";
        return "<span class='tw-mi'><img loading='lazy' decoding='async' referrerpolicy='no-referrer' src='" +
               esc(th) + "' alt='' onerror=\"this.parentNode.classList.add('bad');this.remove()\">" +
               (x.kind === "video" ? "<span class='tw-play'>▶" + (x.dur ? " " + twDur(x.dur) : "") + "</span>" : "") +
               "</span>";
      }).join("") + "</span>";
    }
    var stats = [];
    if(d.replies) stats.push("💬 " + twNum(d.replies));
    if(d.retweets) stats.push("🔁 " + twNum(d.retweets));
    if(d.likes) stats.push("♥ " + twNum(d.likes));
    if(d.views) stats.push("👁 " + twNum(d.views));
    var foot = TW_LOGO + " X（推特）" + (d.when ? " · " + esc(d.when) : "") +
               (stats.length ? " · " + esc(stats.join(" ")) : "");
    return "<a class='qcard tw' href='" + esc(d.url || ("https://x.com/i/status/" + d.id)) + "'" +
           " target='_blank' rel='noopener noreferrer' title='" + esc(name + " · X") + "'>" +
           "<span class='tw-hd'>" +
           (av ? "<img class='tw-av' loading='lazy' referrerpolicy='no-referrer' src='" + esc(av) +
                 "' alt='' onerror=\"this.remove()\">"
               : "<span class='tw-av ph'>" + esc(String(name).slice(0, 1)) + "</span>") +
           "<span class='tw-who'><span class='tw-nm'>" + esc(name) + "</span>" +
           (d.handle ? "<span class='tw-hd2'>@" + esc(d.handle) + "</span>" : "") +
           "</span></span>" +
           (d.text ? "<span class='tw-tx'>" + esc(d.text) + "</span>" : "") +
           medHtml +
           "<span class='qc-ft'><span class='qc-src'>" + foot + "</span>" +
           "<span class='qc-open'>打开</span></span></a>";
  }
  function twPaint(id){
    var d = TW_CACHE[id];
    if(!d) return;
    var cards = [].slice.call(document.querySelectorAll(".qcard[data-tw='" + id + "']"));
    cards.forEach(function(a){
      if(d.ok) a.outerHTML = tweetCardHtml(d);
      else a.classList.add("tw-bad");
    });
  }
  function fillTweets(){
    var nodes = [].slice.call(document.querySelectorAll(".qcard[data-tw]"));
    if(!nodes.length) return;
    var seen = Object.create(null);
    nodes.forEach(function(a){
      var id = a.getAttribute("data-tw") || "";
      if(!id || seen[id]) return;
      seen[id] = 1;
      var got = TW_CACHE[id];
      if(got === null) return;            // 已经在请求里了
      if(got){
        twPaint(id);
        // 图已经在前端缓存里，但服务端可能还没记这个会话的归属；静默补一次。
        if(twSid() && !TW_OWNED[twOwnKey(id)]){
          TW_OWNED[twOwnKey(id)] = 1;
          fetch(API + "/tweet?u=" + encodeURIComponent("https://x.com/i/status/" + id) +
                "&sid=" + encodeURIComponent(twSid())).catch(function(){});
        }
        return;
      }
      TW_CACHE[id] = null;
      fetch(API + "/tweet?u=" + encodeURIComponent("https://x.com/i/status/" + id) +
            "&sid=" + encodeURIComponent(twSid()))
        .then(function(r){ return r.json(); })
        .then(function(d){ TW_CACHE[id] = (d && d.ok) ? d : { ok: false }; if(twSid()) TW_OWNED[twOwnKey(id)] = 1; })
        .catch(function(){ TW_CACHE[id] = { ok: false }; })
        .then(function(){ TW_CACHE[id] && twPaint(id); });
    });
  }
  // v4.87（用户 m11237 第 4 条）：裸链接卡片（对方只发了个 URL，QQ 没带标题）先画通用标题，
  // 这里向后端要真实标题再补进去；抓不到就一直保持通用文案，不阻塞渲染、不闪。
  // 后端按 URL 落盘缓存，同一个链接只真抓一次。
  var LT_CACHE = Object.create(null);   // url -> 标题 | ""（确认没有） | null（请求中）
  function ltApply(el, title){
    var t = el.querySelector(".qc-t");
    if(!t || !title) return;
    t.textContent = String(title);
    el.setAttribute("data-lt-done", "1");
  }
  function fillLinkTitles(){
    var nodes = [].slice.call(document.querySelectorAll(".qcard[data-lt]"));
    for(var i = 0; i < nodes.length; i++){
      var el = nodes[i];
      if(el.getAttribute("data-lt-done") === "1") continue;
      var url = el.getAttribute("data-lt") || "";
      if(!url) continue;
      if(url in LT_CACHE){
        if(LT_CACHE[url]) ltApply(el, LT_CACHE[url]);
        continue;                       // 请求中或已确认没有标题
      }
      LT_CACHE[url] = null;
      (function(u){
        fetch(API + "/link-title?url=" + encodeURIComponent(u))
          .then(function(r){ return r.json(); })
          .then(function(d){ LT_CACHE[u] = (d && d.ok && d.title) ? String(d.title) : ""; })
          .catch(function(){ LT_CACHE[u] = ""; })
          .then(function(){ if(LT_CACHE[u]) fillLinkTitles(); });
      })(url);
    }
  }
  // 快照详情、引用块、转发弹层这些不是走 swapWinBody 的，挂一个观察器兜底；
  // 合并到 150ms 一次，避免轮询重绘时反复扫描。
  var ltObs = null;
  function ltWatch(){
    if(ltObs || typeof MutationObserver === "undefined" || !document.body) return;
    var timer = null;
    ltObs = new MutationObserver(function(){
      if(timer) return;
      timer = setTimeout(function(){ timer = null; fillLinkTitles(); }, 150);
    });
    ltObs.observe(document.body, {childList: true, subtree: true});
  }
  // 卡片/转发/文件已经单独画出来了，正文里残留的分享模板要剥掉，
  // 否则卡片下面还挂一行「点击链接直接打开」（用户 m51324 图5）
  function stripShareText(t){
    var s = String(t == null ? "" : t);
    s = s.replace(sharePattern(), "");
    s = s.replace(/点击链接直接打开[。.!！]?/g, "");
    return s.replace(/[ \t]+\n/g, "\n").replace(/\n{2,}/g, "\n").replace(/^\s+|\s+$/g, "");
  }
  // 美化字数统一按用户可见正文计算，不把时间、昵称、message_id 和 XML 壳算进去。
  function displayCharCount(m, preMeta){
    var text = String(m.text || "");
    if(m.role === "user"){
      var ps = preMeta ? [{body: text}] : splitMetaAll(tidyBody(text));
      return ps.reduce(function(n, p){ return n + visLen(tidyBody2(p.body, m.media && m.media.length)); }, 0);
    }
    if(m.role === "assistant"){
      text = text.replace(/^\[\d{1,2}:\d{2}\]\s*/, "").replace(/^【[^】]{1,24}】\s*/, "");
      return parseAssistant(text).reduce(function(n, s){ return n + visLen(s.text); }, 0);
    }
    return visLen(text);
  }
  // 一条消息 = 一张卡：左侧 role 色条、头部是元数据标签、正文只剩内容
  // 用户 m11091 第 3 条：QQ 官方机器人（塔罗牌、抽卡这类）发的是 markdown 消息，原文：
  //   [00:06] [message_id: …] [user_nickname: AronaBot, user_id: …] |
  //   [At 1575945754(nickname: Enderman)][@某人](mqqapi://markdown/mention?…)
  //   看看老师抽到了什么: …长释义… ![img #208px #409px](https://qqbot.ugcimg.cn/…)
  // 以前按普通群消息铺开：正文里一串 markdown 语法、释义占大半屏、图还要再占一块。
  // 这两个特征只有官方机器人的 markdown 消息才有，普通群消息不会误伤。
  var BOT_MD_RE = /mqqapi:\/\/markdown\/mention|qqbot\.ugcimg\.cn/i;
  function isBotMarkdown(t){ return BOT_MD_RE.test(String(t == null ? "" : t)); }
  function botCardHtml(m, idx, mode, ts, extra, grp){
    var raw = String((mode === "mixed" ? rawOf(m) : m.text) || "");
    // 图片地址必须趁原文还没洗之前抠出来：tidyBody 会把 ![img …](url) 换成「[图片]」，
    // 链接一丢就点不开了
    var pics = [], re = /!\[img[^\]]*\]\(\s*([^)\s]+)\s*\)/gi;
    // 只锚定完整 markdown 命中，别让普通/截断 [图片] 占位把已知图片挪走。
    // 标记仅在此函数的展示副本中存在，并避开原文本身；同 URL 的每次出现各有槽位。
    var marker = "\u0000botpic";
    while(raw.indexOf(marker) >= 0) marker += "_";
    var marked = raw.replace(re, function(_, u){
      var n = pics.length; pics.push(u); return marker + n + "\u0000";
    });
    var parts = splitMetaAll(tidyBody(marked));
    var p = parts[0] || { meta: {}, body: raw };
    var meta = p.meta || {};
    var body = String(p.body || "")
      // @ 已经在头部做成徽章，正文里那串 At token 不再重复
      .replace(/\[At\s+\d+\(\s*nickname:\s*[^)]*\)\]/g, " ")
      // 图片占位先保留，等地址和槽位一一对应后再原位换图。
      .replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
    var slots = body.split(new RegExp(marker + "(\\d+)\\u0000", "g"));
    slots = slots.map(function(t, i){
      return i % 2 ? t : String(t || "").replace(/\[图片\]/g, " ").replace(/[ \t]+\n/g, "\n").trim();
    });
    var bodyText = slots.filter(function(_, i){ return i % 2 === 0; }).join("\n").trim();
    // 用户 m13250 第 1 条：机器人卡片跟 user 消息同一版式——组内左列只放时间，
    // 正文在右列，字数气泡贴在正文末尾；@ 谁缩成正文顶上的一行小标签
    // （组头已经写着是哪个机器人，不必再在卡片头部堆一串徽章）。
    var ats = (meta.at || []).map(function(a){
      return "<span class='k at'>@" + esc(a) + "</span>";
    }).join("");
    var chip = bodyText ? "<span class='chb count'>" + visLen(bodyText) + " 字</span>" : "";
    var hd = "";
    if(!grp){
      hd = "<span class='rq bot'>bot</span>" + (extra || "") + ats;
      hd += "<span class='k nm'>" + esc(meta.name || "机器人") + "</span>";
      if(meta.you) hd += "<span class='k you'>@你</span>";
      if(meta.ts) hd += "<span class='k weak' title='" + esc(meta.ts) + "'>" + esc(shortTs(meta.ts)) + "</span>";
      hd += "<span class='grow'></span>";
      if(meta.mid) hd += "<span class='id' title='" + esc(meta.mid) + "'>#" + esc(meta.mid.slice(0, 8)) + "</span>";
    }
    function botThumbs(list){
      var inner = list.map(function(u){
        var url = safeWebUrl(u);
        if(!url) return "";
        return "<span class='thumb' role='button' tabindex='0' data-img='" + esc(url) + "'" +
               " data-imgt='机器人消息配图' title='点开看大图'>" +
               "<img loading='lazy' decoding='async' src='" + esc(url) + "' alt='图片'" +
               " onerror=\"this.parentNode.classList.add('bad');this.parentNode.removeChild(this);\"></span>";
      }).join("");
      return inner ? "<div class='thumbs botpic'>" + inner + "</div>" : "";
    }
    // 正文仍按 bot-txt 折叠；字数气泡留在最后一段文字，不塞进尾随图片里。
    var bd = "", lastText = -1;
    slots.forEach(function(t, i){ if(i % 2 === 0 && t) lastText = i; });
    slots.forEach(function(t, i){
      if(i % 2) bd += botThumbs([pics[Number(t)]]);
      else if(t) bd += chipInto("", bodyHtml(t, null, 0, "bot-txt"), i === lastText ? chip : "");
    });
    if(!bd) bd = bodyHtml("", pics.length ? null : "(空)", 0, "bot-txt");
    if(grp){
      if(ats) bd = "<div class='segh'>" + ats + "</div>" + bd;
      return "<div class='mc user botcard'><div class='mc-hd'>" +
             tsBlock(meta.ts || ts, true) + "</div><div class='mc-bd'>" + bd + "</div></div>";
    }
    return "<div class='mc user botcard'><div class='mc-hd'>" + hd + "</div>" +
           "<div class='mc-bd'>" + bd + "</div></div>";
  }

  // 用户 m12814：点引用块要能跳到被引用的那条，所以每条消息块都挂上自己的 message_id。
  // user 的 id 在拆段后的 meta.mid 里（一条可能拼了好几条群消息，全挂上）；
  // assistant 的 id 只在 <msg message_id="…"> 壳上，正文里没有单独字段。
  function midAttrOf(m, parts){
    var ids = [];
    (parts || []).forEach(function(p){
      var v = String((p && p.meta && p.meta.mid) || "").trim();
      if(v && ids.indexOf(v) < 0) ids.push(v);
    });
    if(!ids.length){
      var raw = String((m && (m.raw || m.text)) || "");
      var re = /<msg\b[^>]*\bmessage_id\s*=\s*["']([^"']+)["']/gi, mm;
      while((mm = re.exec(raw))){ if(ids.indexOf(mm[1]) < 0) ids.push(mm[1]); }
    }
    return ids.length ? (" data-mid='" + esc(ids.join(" ")) + "'") : "";
  }

  function mcHtml(m, idx, mode, extra, preMeta, ts, grp, tailExtra){
    var role = m.role || "?";
    var ex = extra || "";
    if(role === "summary"){
      return "<div class='mc summary'><div class='mc-hd'><span class='rq system'>" + idx + " · 中期摘要</span>" +
             "<span class='k'>" + (mode === "mixed" ? rawOf(m).length : m.chars) + " 字</span><span class='grow'></span>" +
             "<span class='mini' data-sumclear='1'>清空摘要</span></div>" +
             "<div class='mc-bd'>" + bodyHtml(mode === "mixed" ? rawOf(m) : m.text, "(空)", 0, mode === "mixed" ? "rawtxt" : "") + "</div></div>";
    }
    // mixed（对照档）要看到的就是原文，不能拿被转换过的展示文本充数
    var raw = (mode === "mixed") ? rawOf(m) : (m.text || "");
    // v4.37（用户 m20164）：后端给每条 assistant 补 who = bot 显示名（樱井桃华），注记那几条
    // 也被补上了，而 name 才是「对当前聊天消息的决策建议」。对照档补名字时走 noteNameOf，
    // 不然原文头上会多一个 [樱井桃华]，跟原文模式对不上。
    // 工具回调的时间在 m.at 里（view_of 把 [HH:MM] 剥出去了），对照档不能拿
    // 上一条 user 的时间顶——异步工具的结果可能晚好几分钟。
    if(mode === "mixed" && role === "assistant") raw = botRawHead(raw, noteNameOf(m) || m.who || m.name || "",
      (isToolNoteMsg(m) && m.at) ? String(m.at) : ts);
    // v3.63（用户 m18492 图2）：对照档只渲染正文，工具调用那一整段（[tool_calls] … JSON）根本不在原文里，
    // 「原文 ↔ 美化」就核对不了工具参数。这里按请求页的原文格式补在正文末尾。
    if(mode === "mixed" && role === "assistant"){
      var tcLine = tcRawLine(m);
      if(tcLine) raw = String(raw).replace(/\s+$/, "") + "\n" + tcLine;
    }
    if(mode === "mixed"){
      // 后端 chars 是展示正文口径，对照必须统计当前原文（含元数据与空白）。
      var rawChars = raw.length;
      // 组内（v2.91）：左列只有 46px，角色名 + 字数 + 「原文」挤进去会被截成
      // 「1 · assis」（用户 m32956 图3）。组内改成左列只放时间，这些标签
      // 挪到正文末尾一行小字。
      var mixNote = "<div class='mixnote'>" + esc(role) +
                    (m.tools ? " · tool_calls ×" + m.tools : "") +
                    (m.tool_call_id ? " · " + esc(m.tool_call_id) : "") +
                    " · " + rawChars + " 字 · 原文</div>";
      if(grp){
        // 组内的这些标签由组头右侧的 .gh-note 显示（见 mixNoteOf），正文下面不再多一行
        return "<div class='mc " + esc(role) + "'><div class='mc-hd'>" + tsBlock(ts, true) + "</div>" +
               "<div class='mc-bd'>" + bodyHtml(raw, "(空)", 0, "rawtxt", (role === "assistant" || role === "system")) + "</div></div>";
      }
      var hd = "<span class='rq " + esc(role) + "'>" + idx + " · " + esc(role) + "</span>" + ex +
               (m.tools ? "<span class='k'>tool_calls ×" + m.tools + "</span>" : "") +
               (m.tool_call_id ? "<span class='k'>" + esc(m.tool_call_id) + "</span>" : "") +
               "<span class='k'>" + rawChars + " 字</span><span class='grow'></span><span>原文</span>";
      return "<div class='mc " + esc(role) + "'><div class='mc-hd'>" + hd + "</div>" +
             "<div class='mc-bd'>" + bodyHtml(raw, "(空)", 0, "rawtxt", (role === "assistant" || role === "system")) + "</div></div>";
    }
    var ownerDisplay = displayOf(m, m.media);
    // 用户 m21787 图3/图4：美化档显示聊天里原本那张图（后端按 message_id 回索引取的 orig_media）。
    // 只认调用方显式打的 origMedia 标记，请求快照那边照旧看模型侧那一份。
    if(mode === "pretty" && m.origMedia && ownerDisplay.orig && ownerDisplay.orig.length) ownerDisplay.own = ownerDisplay.orig;
    // 用户 m10400 图3：框架会把被引用那条消息的图也挂到引用它的消息上，引用块画一遍、
    // 正文再画一遍就是重复。引用块已经画了同一批图时，正文这份不再画。
    if(ownerDisplay.reply && ownerDisplay.reply.media && ownerDisplay.reply.media.length && ownerDisplay.own.length){
      var _qset = Object.create(null);
      ownerDisplay.reply.media.forEach(function(r){
        var _p = (typeof r === "string") ? r : String((r && (r.path || r.url)) || "");
        if(_p) _qset[_p] = 1;
      });
      var _ownKept = ownerDisplay.own.filter(function(r){
        var _p = (typeof r === "string") ? r : String((r && (r.path || r.url)) || "");
        return !(_p && _qset[_p]);
      });
      if(_ownKept.length !== ownerDisplay.own.length) ownerDisplay.own = _ownKept;
    }
    var neutralMedia = ownerDisplay.unassigned;
    // 只改视图副本；真实发送用的 legacy m.media 不动。
    m = Object.assign({}, m, { chars: displayCharCount(m, preMeta), media: ownerDisplay.own });
    if(role === "user"){
      // 用户 m11091 第 3 条：官方机器人的 markdown 消息整条折成一张机器人卡片
      if(isBotMarkdown(raw)) return botCardHtml(m, idx, mode, ts, ex, grp);
      // preMeta：外面已经把一条消息拆成多张卡时，直接用拆好的那段元数据
      var parts = preMeta ? [{ meta: preMeta, body: raw }]
                          : splitMetaAll(tidyBody(raw));
      if(!parts.length) parts = [{ meta: { ts: "", mid: "", name: "", uid: "", at: [] }, body: raw }];
      if(parts.length === 1){
        var p0 = parts[0], pm = Object.assign({}, p0.meta);
        if(ownerDisplay.known){
          var oldReply = pm.reply;
          pm.reply = ownerDisplay.reply ? Object.assign({}, oldReply && String(oldReply.id) === ownerDisplay.reply.id ? oldReply : {}, ownerDisplay.reply) : null;
          if(ownerDisplay.hasBody){
            var ownPart = splitMeta(tidyBody(ownBodyOf(m, p0.body)));
            pm.fwd = ownPart.meta.fwd; pm.card = ownPart.meta.card; pm.file = ownPart.meta.file;
            p0 = { meta: pm, body: ownPart.body };
          } else p0 = { meta: pm, body: stripReplyTokens(p0.body) };
        }
        parts = [p0];
      } else {
        // 一个聚合条目没有 per-mid 证据，不能把整份 own_media 认领到某一段。
        neutralMedia = uniqueOwnerMedia(ownerDisplay.own.concat(neutralMedia));
        m = Object.assign({}, m, { media: [] });
      }
      var p = parts[0];
      var h2 = "";
      // 组内（grp）：头像/名字/@ 已经在组头，这里只留时间（放正文左边）
      if(grp){
        // 组内：头像/名字在组头，左列只放时间（日期 + 时间两行，日期空着占位）
        h2 = tsBlock(parts.length === 1 ? p.meta.ts : "", true);
      } else {
      h2 = "<span class='rq user'>user</span>" + ex;
      // 多段时每条正文自带「时间 昵称」，头部再写第一段的只会误导（看着像整条都是那个人那条时间）
      if(parts.length > 1){
        // 头部不再重复时间/昵称（每段正文里已经有一行），改成列出这一段里出现过的人：
        // 只写「N 条消息」看不出是几个人说的（用户 m29241 图2）
        var names = [];
        parts.forEach(function(q){
          var nm2 = String(q.meta.name || "").trim();
          if(nm2 && names.indexOf(nm2) < 0) names.push(nm2);
        });
        h2 += "<span class='k'>" + parts.length + " 条</span>";
        if(names.length){
          h2 += "<span class='k'>" + esc(names.slice(0, 4).join("、")) +
                (names.length > 4 ? " 等 " + names.length + " 人" : "") + "</span>";
        }
      } else {
        if(p.meta.ts) h2 += "<span class='k' title='" + esc(p.meta.ts) + "'>" + esc(shortTs(p.meta.ts)) + "</span>";
        if(p.meta.name) h2 += "<span class='k'>" + esc(p.meta.name) + "</span>";
        if(p.meta.you) h2 += "<span class='k you'>@你</span>";
      }
      if(m.chars) h2 += "<span class='k weak'>" + m.chars + " 字</span>";
      h2 += "<span class='grow'></span>";
      if(p.meta.mid) h2 += "<span class='id' title='" + esc(p.meta.mid) + "'>#" + esc(p.meta.mid.slice(0, 8)) + "</span>";
      }
      var bd2;
      // 字数做成紧贴正文末尾的小气泡（用户 m31639）
      if(parts.length === 1){
        // 转发卡里的图片归卡片，折叠时一起被裁掉；卡外不再重复画一遍
        // （用户 m01011 图2：缩略图露在卡片上面）。
        var fwdIn = isFwdBody(p.meta.fwd);
        // 引用只使用目标投影；本条已知自有图只进自己的 Forward/正文。
        var inQuote = null;
        // 归属未确认时，单段转发也把 unassigned 交给转发卡：否则视图副本的 m.media
        // 已被换成空数组，卡内只剩 🖼 占位，图片又落回卡外（用户 m17217）。
        // 归属未确认时优先用后端给的「这条转发的全部图」：模型侧只挂了前几张，
        // 用 unassigned 画卡会让最后几条只剩占位（用户 m00942 图1）。
        var fwdMedia = fwdIn ? (ownerDisplay.known ? ownerDisplay.own
          : ((ownerDisplay.all && ownerDisplay.all.length) ? ownerDisplay.all : ownerDisplay.unassigned)) : null;
        var fwdSlots = fwdIn && ownerDisplay.known ? ownerDisplay.slots : null;
        var fwdCard = fwdIn ? fwdHtml(p.meta.fwd, fwdMedia, fwdSlots) : "";
        // 卡已经把这些图收进去了，就不能再以「归属未确认的附件」在卡外画一遍。
        if(fwdCard && !ownerDisplay.known) neutralMedia = [];
        var partHtml = quoteHtml(p.meta.reply, null, p.raw || p.content || p.text) + fwdCard +
                       cardHtml(p.meta.card) + fileHtml(p.meta.file) + filesHtml(m.files);
        var hasPart = !!(p.meta.card || p.meta.fwd || p.meta.file ||
                         (m.files && m.files.length) || (m.media && m.media.length) || neutralMedia.length);
        var bodyTxt = tidyBody2(p.body, m.media && m.media.length);
        // 用户本轮图1/图2：图片原来统一堆在正文前面，看着像「引用 → @ → 图 → 文字」。
        // 占位数和图片数正好对得上时，改成按占位在原文里的位置逐张插回去，
        // 于是美化后的顺序跟用户当初打的顺序一致。
        var inlPlan = (fwdIn || inQuote) ? null : planInlineMedia(bodyTxt, m.media, mode);
        // v3.62（用户 m18059 第 2 条）：这段的媒体块已经画出来了（缩略图或「已清理」占位），
        // 正文里的 [图片]/[表情] 占位按画出的张数吃掉，不再多一行纯文本。
        // 已经决定内联的那份不动占位，交给 renderInlineMedia 原位换图。
        // 中立附件仍不认领 owner；只抵消实际画出的占位，多出的和无附件人话保留。
        bodyTxt = stripMediaTags(bodyTxt, !inlPlan ?
          (!(fwdIn || inQuote) ? renderedThumbCount(m.media) : 0) + renderedThumbCount(neutralMedia) : 0);
        // v4.22（用户 m21783）：文件卡已经画出来了，正文里那行 [文件] 不再重复
        if(m.files && m.files.length) bodyTxt = bodyTxt.replace(/\[(?:文件|File attached)\]/gi, " ");
        // 卡片已经画出来了，正文里那段分享模板（【闲鱼】链接…标题…点击链接直接打开）
        // 再来一遍就是重复，剥掉；剥完没正文，也就不该再挂一个「N 字」（用户 m51324 图5）
        if(p.meta.card || p.meta.fwd || p.meta.file) bodyTxt = stripShareText(bodyTxt);
        // v3.69（用户 m21003 第 4 条）：正文画成抖音分享卡时，卡下面不再挂「N 字」。
        var dyBody = !!douyinCard(bodyTxt);
        // 正文里夹着 B 站分享链接时先抽成卡：留在 .mono 里它会掉到文字下面，
        // 还多挂一行「33 字」（用户 m13750 图：卡片在下面、不在右侧）。
        // 抽完正文没字了就只剩卡，跟纯分享卡那条长一个样；还有别的字就正文左、卡片右。
        var pulled = pullTextCards(bodyTxt);
        var canSide = !!(pulled && pulled.rest && !partHtml &&
                         !(m.files && m.files.length) && !(m.media && m.media.length));
        if(canSide){
          // v4.87（用户 m11323）：正文和卡片都换行靠左——正文在上、气泡贴着正文末尾，
          // 卡片另起一行顶格排在下面。以前是并排（chip-line tc），正文吃掉整行剩余宽度，
          // 卡片被挤到最右边，看着像飘走（用户 m11237）。
          bd2 = tailChip(bodyHtml(pulled.rest, null), grp ? (tailExtra || "") : "") + pulled.html;
        } else {
        if(pulled){
          partHtml = pulled.html + partHtml;
          bodyTxt = pulled.rest;
          if(!bodyTxt) hasPart = true;
          // 抽走分享卡之后正文已经变了，内联计划里的分段对不上，退回老排法。
          if(inlPlan) inlPlan = null;
        }
        var chipU = grp ? ((bodyTxt && m.chars && !dyBody ? "<span class='chb count'>" + m.chars + " 字</span>" : "") + (tailExtra || "")) : "";
        // 正文真空、又没有任何卡片/图片时，别只写「(空)」——说清楚是框架只给了元数据
        var bodyPart = inlPlan
          ? renderInlineMedia(inlPlan)
          : bodyHtml(bodyTxt, hasPart ? null : "(这条只有元数据，正文是空的)");
        // 无槽位的老附件无法恢复原始坐标：引用/卡片/已知正文优先，附图保守尾随。
        // 不把附图交给 tailChip，字数/待发标签仍紧贴文字末尾。
        bd2 = chipInto(partHtml, bodyPart, chipU) +
              ((fwdIn || inQuote) || inlPlan ? "" : mediaHtml(m, mode));
        }
      } else {
        // 多条拼在一起时，每条各自带一行「时间 昵称」，中间用虚线隔开。
        // 归属不明的图不硬塞给某一段；已知各段内容画完后再补一份，
        // 避免外图抢在引用/@正文前，也不把别人的图塞进引用里。
        // 例外（用户 m01680）：各段里图片占位的总数正好等于这条例消息的图片数时，
        // 说明这些占位就是这些图的出处，可以逐段认领——引用里的进那段的引用块，
        // 正文里的留在那段卡外；多一张少一张就认不准，退回原来的排法。
        var claims = null; // 无 per-mid 原生投影时，不能按 Reply/占位数认领媒体。
        var segMedia = parts.map(function(){ return { quote: null, own: null }; });
        bd2 = "";
        parts.forEach(function(q, qi){
          var lab = "";
          if(q.meta.ts) lab += "<span class='k' title='" + esc(q.meta.ts) + "'>" + esc(shortTs(q.meta.ts)) + "</span>";
          if(q.meta.name) lab += "<span class='k'>" + esc(q.meta.name) + "</span>";
          if(q.meta.you) lab += "<span class='k you'>@你</span>";
          var segOwn = segMedia[qi].own;
          // 用户 m03014 第 1 条：这一段本身是合并转发时，图片归转发卡（折叠时跟着藏起来），
          // 不再铺到那段卡外——多段消息里转发图片外漏就是这里漏的。
          var segFwd = isFwdBody(q.meta.fwd);
          var segOut = segFwd ? null : segOwn;
          var segHasImg = !!(segOut && segOut.length);
          // 用户本轮图1/图2：这一段自己的图也按段内 [图片] 占位的位置插回去。
          var segRaw = tidyBody2(q.body, claims ? segHasImg : (m.media && m.media.length));
          var segInl = planInlineMedia(segRaw, segOut, mode);
          var segBody = stripMediaTags(
            segRaw,
            (segHasImg && !segInl) ? segOut.length : 0);
          // 一条 user 里拼了十几条群消息时，字数要按「这一条」算，不能整条复用一个数
          // —— 三条 OK / 打游戏去 / 关闭所有浏览器 全显示 79 字（用户 m33994 图3）
          var segN = visLen(segBody);
          var segDy = !!douyinCard(segBody);
          var segChip = grp ? ((segN && !segDy ? "<span class='chb count'>" + segN + " 字</span>" : "") +
                              (qi === parts.length - 1 ? (tailExtra || "") : "")) : "";
          var segHasPart = !!(q.meta.card || q.meta.fwd || q.meta.file ||
                              segHasImg || (!claims && m.media && m.media.length) ||
                              (qi === 0 && m.files && m.files.length));
          bd2 += "<div class='seg'>" + (lab ? "<div class='segh'>" + lab + "</div>" : "") +
                 chipInto(quoteHtml(q.meta.reply, segMedia[qi].quote, q.raw || q.content || q.text) +
                          fwdHtml(q.meta.fwd, segFwd ? segOwn : null) + cardHtml(q.meta.card) +
                           fileHtml(q.meta.file) + (qi === 0 ? filesHtml(m.files) : ""),
                          (segInl ? renderInlineMedia(segInl)
                                  : bodyHtml(segBody, segHasPart ? null : "(这条只有元数据，正文是空的)")),
                          segChip) + (segInl ? "" : mediaHtml({ media: segOut }, mode)) + "</div>";
        });
        // 跨段归属不明时仍只画一份，放在已知各段之后，不插进任何引用里。
        if(!claims) bd2 += mediaHtml(m, mode);
      }
      bd2 += unassignedMediaHtml(neutralMedia, mode);
      return "<div class='mc user'" + midAttrOf(m, parts) + "><div class='mc-hd'>" + h2 + "</div>" +
             "<div class='mc-bd'>" + bd2 + "</div></div>";
    }
    if(role === "assistant"){
      // 插件在发出去的那份上加了名字，这里把它拆成头部徽章，正文不再重复。
      // v2.34 起名字挪到 <msg …> 后面（<msg message_id="…">【桃华】 <text>…），
      // 老消息是整条开头的【桃华】、更老的写 [桃华]，几种都认，否则头部只剩「assistant」。
      var who = m.who ? String(m.who) : "", body = raw;
      // bot 自己说的话现在也带时间（后端 stamp_assistant_time 补的）：
      // <msg …>[00:47] [名字] <text>。时间提到头部去，不然会被当成名字
      // v2.70 起时间戳放到最前面（[00:47] <msg …>[名字] <text>），和 user 一个位置；
      // 老记录是 <msg …>[00:47] [名字]，两种都认
      // v4.90（用户 m00375 第 1 条）：bot 自己的时间要用这条消息自己的。
      // 以前只在 ts 为空时才采用，而调用方传进来的是「上一条 user 消息的时间」，
      // 于是 16:09 发出的回复被标成 16:01，看着像跟 user 同步。
      // 注记的时间后端剥进了 m.at（正文里没有前缀），回复的时间还在正文开头，两种都认。
      if(m.at) ts = String(m.at);
      var tm0 = body.match(/^\[(\d{1,2}:\d{2})\]\s*(<msg\b[^>]*>)\s*/);
      if(tm0){ ts = tm0[1]; body = tm0[2] + body.slice(tm0[0].length); }
      else {
        var tm = body.match(/^(<msg\b[^>]*>)\s*\[(\d{1,2}:\d{2})\]\s*/);
        if(tm){ ts = tm[2]; body = tm[1] + body.slice(tm[0].length); }
        else {
          var tm2 = body.match(/^\[(\d{1,2}:\d{2})\]\s*/);
          if(tm2){ ts = tm2[1]; body = body.slice(tm2[0].length); }
        }
      }
      var pm = who ? null : body.match(/^【([^】]{1,24})】\s*/);
      if(pm){ who = pm[1]; body = body.slice(pm[0].length); }
      else if(!who){
        var pm2 = body.match(/^(<msg\b[^>]*>)\s*(?:\[([^\]\n]{1,24})\]|【([^】\n]{1,24})】)\s*/);
        // <msg …> 要留着：parseAssistant 按它切段，剥掉就整条都不认了
        if(pm2){ who = pm2[2] || pm2[3]; body = pm2[1] + body.slice(pm2[0].length); }
      }
      // 组内：字数做成紧贴正文末尾的小气泡（用户 m31639）。工具次数不挂这里了——它数的是
      // 这一段对话用了几次工具，所以挪到组头名字后面（用户 m17212 图1）。
      // 用户 m21297 图2/图3：工具回调是「先一句提示、后面跟图」。以前媒体块先拼进 bd，
      // 卡片就成了图在上、字在下；工具回调改成正文在前、图在后（其它 assistant 不动）。
      var toolNote = isToolNoteMsg(m);
      var toolMedia = toolNote ? mediaHtml(m, mode) : "";
      var segs = parseAssistant(body), bd = toolNote ? "" : mediaHtml(m, mode);
      // v3.69（用户 m21003 第 4 条）：正文是抖音分享卡时不挂「N 字」
      var dyAsst = !!douyinCard(body) || segs.some(function(x){ return !!douyinCard(x.text); });
      var chipA = grp ? ((m.chars && !dyAsst ? "<span class='chb count'>" + m.chars + " 字</span>" : "") +
                         (tailExtra || "")) : "";
      segs.forEach(function(s, si){
        var em = s.emojis.map(function(x){ return emojiHtml(x); }).join("");
        // 表情要贴在正文那一行的末尾：bodyHtml 给的是 <div class='mono'>，
        // 直接拼在后面会掉到下一行独占一行（用户 m33778 图8）。tailChip 正好插在 </div> 前。
        bd += "<div class='seg'>" +
              // 整段只有一个表情、没有文字时别补「(空)」，否则渲染成「(空) 😘」（用户 m24522 图4）
              tailChip(tailChip(bodyHtml(s.text, (s.emojis && s.emojis.length) ? null : "(空)", 0, "", true), em),
                       si === segs.length - 1 ? chipA : "") + "</div>";
      });
      // v3.58（用户 m16875 图2）：这条消息的工具调用原来画在卡片里一遍、卡片外又补一遍，
      // 同一条消息出现两套「调用 search · 搜索 …」。现在只在正文末尾画一次。
      var tcl = tcOf(m);
      if(!bd && !(toolNote && toolMedia)) bd = m.tools
        ? (tcl.length ? "" :
           "<span class='dim'>（这一步调用了 " + m.tools + " 个工具，没有文本输出）</span>")
        : bodyHtml(body, "(空)", 0, "", true);
      if(grp && !segs.length) bd = tailChip(bd, chipA);
      if(tcl.length) bd += "<div class='tcall-wrap'>" + toolCallList(tcl) + "</div>";
      // 工具回调的图排在提示后面（用户 m21297 图2/图3：先提示、后图）
      if(toolNote) bd += toolMedia;
      bd += unassignedMediaHtml(neutralMedia, mode);
      if(grp){
        // 组内：名字在组头，左列只放时间；时间从正文里剥出来了（ts 变量）
        // v3.60（用户 m17581 第 5 条）：这一条只有工具调用、没有正文时打上 callonly，
        // 让左列时间跟「调用 …」那行对齐（CSS 里调 3px）。
        var callOnly = (!segs.length && tcl.length) ? " callonly" : "";
        return "<div class='mc assistant" + callOnly + "'" + midAttrOf(m, null) + "><div class='mc-hd'>" + tsBlock(ts, true) + "</div>" +
               "<div class='mc-bd'>" + bd + "</div></div>";
      }
      var h3b = "<span class='rq assistant'>" + esc(who || "assistant") + "</span>" + ex +
               (ts ? "<span class='k ts'>" + esc(ts) + "</span>" : "") +
               (m.tools ? "<span class='k'>工具调用 ×" + m.tools + "</span>" : "") +
               (segs.length > 1 ? "<span class='k'>" + segs.length + " 段</span>" : "") +
               (m.chars ? "<span class='k weak'>" + m.chars + " 字</span>" : "") + "<span class='grow'></span>";
      return "<div class='mc assistant'" + midAttrOf(m, null) + "><div class='mc-hd'>" + h3b + "</div><div class='mc-bd'>" + bd + "</div></div>";
    }
    var tj = (role === "tool") ? tryJson(raw) : {ok:false};
    var jk = "";
    if(tj.ok){
      var isRes = !!toolResultHtml(tj);
      jk = Array.isArray(tj.v)
        ? "<span class='k'>" + (isRes ? (tj.v.length + " 条结果") : ("JSON · " + tj.v.length + " 项")) + "</span>"
        : "<span class='k'>JSON · " + Object.keys(tj.v).length + " 个键</span>";
    }
    // 工具返回一句话就是报错的，标出来，别跟正常结果长得一样
    var failed = (role === "tool") && !tj.ok && /失败|错误|error|failed|unable|exception/i.test(String(raw).slice(0, 80));
    var h4 = "<span class='rq " + esc(role) + "'>" + esc(role) + "</span>" +
             (m.name ? "<span class='tname'>" + esc(m.name) + "</span>" : "") + ex +
             (failed ? "<span class='k bad'>失败</span>" : "") + jk +
             (m.tool_call_id ? "<span class='id' title='" + esc(m.tool_call_id) + "'>" + esc(shortId(m.tool_call_id)) + "</span>" : "") +
             (m.chars ? "<span class='k weak'>" + m.chars + " 字</span>" : "") + "<span class='grow'></span>";
    // system 段（动态段、系统提示词）原来当纯文本铺，标题、列表、粗体都不渲染
    // （用户 m33400 图4：「## 核心记忆 (Core Memory)」原样躺着）。这里走 mdLite。
    var body4 = (role === "system")
      ? "<div class='mdbox'>" + mdLite(raw) + "</div>"
      : toolBodyHtml(raw, tj);
    if(role === "system" && grp) h4 = "";
    return "<div class='mc " + esc(role) + (failed ? " fail" : "") + "'><div class='mc-hd'>" + h4 + "</div>" +
           "<div class='mc-bd'>" + body4 + "</div></div>";
  }

  // 原文内容：content 是什么就给什么。多模态数组照样按原样序列化，
  // 不展开、不替换成可读文本（用户 m22917）。
  // 老记录没有 user_prompt_parts（v2.49 之前）：本轮那串其实是一条条消息用
  // 换行拼起来的，按 [HH:MM] [message_id: ...] 的起点重新切开。原文模式是用来
  // 检查请求上下文的，必须一条条摆出来，不能拼回一整坨（用户 m24996）。
  function splitIncoming(text){
    var t = String(text == null ? "" : text);
    if(!t.trim()) return [];
    var re = /\[\d{1,2}:\d{2}\]\s*\[message_id:/g;
    var cuts = [], m;
    while((m = re.exec(t)) !== null){ cuts.push(m.index); }
    if(cuts.length <= 1) return [t];
    var out = [];
    if(cuts[0] > 0){
      var pre = t.slice(0, cuts[0]);
      if(pre.trim()) out.push(pre);
    }
    for(var i = 0; i < cuts.length; i++){
      var end = (i + 1 < cuts.length) ? cuts[i + 1] : t.length;
      var one = t.slice(cuts[i], end).replace(/[\s]+$/, "");
      if(one.trim()) out.push(one);
    }
    return out;
  }

  function rawContent(c){
    if(typeof c === "string") return c;
    if(c === null || c === undefined) return "";
    try { return JSON.stringify(c, null, 2); } catch(e){ return String(c); }
  }

  // 原文块：一个标题 + 一个等宽 pre，不加任何装饰
  var rawSeq = 0;
  // subHtml：④ 的统计想做成小胶囊，纯文本的 sub 表达不了。三档的 ④ 头必须一样
  // （用户 m33400 图1~3：美化有胶囊、原文只有一句话，说好统一结果还是两样）。
  function rawBlock(title, sub, text, cls, subHtml){
    // 原文模式也要能收起来（用户 m32956）：头行跟美化档共用 .seqh，
    // 折叠按钮排最左边，和 ①②③ 的编号挨着，三档的框架长相才对得齐
    var key = "raw" + (++rawSeq);
    return "<div class='seqh'><span class='mini' data-fold='" + key + "'>展开</span>" +
           "<span>" + esc(title) + "</span>" +
           (subHtml || (sub ? "<span class='dim'>" + esc(sub) + "</span>" : "")) +
           "<span class='grow'></span></div>" +
           "<div class='scroll' data-foldbody='" + key + "' style='display:none'>" +
           "<pre class='rawblk" + (cls ? " " + cls : "") + "'>" + esc(text || "") + "</pre></div>";
  }

  // QQ 表情 id 与 qzone 表情图编号差 100（e100 = id 0 = 微笑，实测对得上）。
  // 图挂了就退回文字，别让一条消息因为网络问题变成空白。
  function emojiHtml(id, name){
    var n = parseInt(id, 10);
    // 图挂了只显示 [表情]：带上 id 的「[表情21]」比原文的 [Emoji 21] 还难看
    var label = name ? ("表情·" + esc(name)) : "表情";
    // v3.25：原来上限写死 400，群里的新表情 id 常常过 400（实测 427），
    // 一过界就直接退成文字「[表情]」，看着就是没渲染。
    if(!isFinite(n) || n < 0 || n > 999){
      return "<span class='emoji'>[" + label + "]</span>";
    }
    // v4.41（用户 m24522 图4）：老版 qzone 小黄脸太旧，先取新版表情库
    // （gitee 上的新版 QQ 小黄脸，文件名就是表情 id：id 4 @2x.gif 就是「得意」，
    // 和 qzone 的 e104.gif 是同一张脸的新画法，实测对得上）。
    // v4.49（用户 m25038 第 5 项）：直连 gitee 会被防盗链挡成 403（实测），页面永远掉到
    // 老版 qzone 小黄脸。改走插件自己的 /emoji 代理：后端取一次、缓存到 media_cache/emoji/。
    // v4.52（用户 m16262 图4）：后端现在也覆盖 288 以后的新脸（344 大怨种走 QFace），
    // 老版 qzone 只有 0~211 这些老脸跟 QQ 编号差 100；再往上 e{id+100} 是另一套图，
    // 344 会退回 e344「面无表情」——画错脸比不画还糟，所以只给老脸留这条退路。
    var NEW_SRC = API + "/emoji?id=" + n;
    var oldFace = n <= 211;
    var fb1 = oldFace ? ("https://qzonestyle.gtimg.cn/qzone/em/e" + (n + 100) + ".gif") : "";
    var fb2 = oldFace ? ("https://qzonestyle.gtimg.cn/qzone/em/e" + n + ".gif") : "";
    return "<span class='emoji'><img class='em-img' loading='lazy' alt='" + label + "' title='QQ 表情 " + n +
           (name ? " · " + esc(name) : "") +
           "' src='" + NEW_SRC + "' data-fb='" + fb1 + "' data-fb2='" + fb2 + "'" +
           " onerror=\"var f=this.dataset;if(!f.tried1&&f.fb){f.tried1='1';this.src=f.fb;}else if(!f.tried2&&f.fb2){f.tried2='1';this.src=f.fb2;}else{this.parentNode.classList.add('miss');}\"" +
           " onload=\"this.parentNode.classList.remove('miss')\">" +
           "<span class='em-txt'>[" + label + "]</span></span>";
  }

  // 一条 user 消息里往往拼着十几条群消息（框架把同一批进来的消息合成一条）。
  // 拆成一张卡一条，卡片头各自显示自己的时间与昵称——不拆的话头部只能写
  // 「11 条消息」，里面谁说的全靠正文里的中括号，既难读又容易看错。
  // 决策层给每条待发消息打的标（v2.26）：这一条最后算不算进积攒
  // 后端 media_kind() 给的是英文类型名，直接印出来就是「待发 image」（用户 m33778 图6）
  var MEDIA_LABEL = { image: "图片", record: "语音", sticker: "表情", video: "视频", file: "文件", emoji: "表情" };

  var MARK_LABEL = {
    counted: "计入", burst: "连发", short: "太短", direct: "直达",
    rest: "休息", backoff: "退避", quota: "配额", private: "私聊", bot: "我"
  };
  var MARK_CLASS = {
    counted: "ok", direct: "ok", private: "ok", bot: "ok",
    burst: "no", short: "no", rest: "no", backoff: "no", quota: "no"
  };
  // ---- 按发言人分组（v2.80）----
  // 之前按 role 分组：连续几条 assistant 并一块没问题，但 user 那边一条消息里
  // 常拼着好几个人，按 role 并会把不同人塞进一组、组头只剩一个名字（用户 m31526 图2）。
  // 分组键改成 role + 发言人，组头只留一份「圆形头像 + 名字」。
  function whoOf(m){
    if(!m) return "";
    // v4.32：注记名称优先于兼容接口补上的默认 Bot 姓名，只改展示名。
    if(m.role === "assistant" && noteNameOf(m) === DEC_NOTE_NAME) return DEC_NOTE_NAME;
    // v3.94（用户 m08398 图2）：后端补进来的那条决策记录带 who（「对当前聊天消息的决策建议」），
    // 以前只有 assistant 分支认它，插在用户消息堆里时会退回拿正文首段当名字。
    if(m.who) return String(m.who).trim();
    if(m.role === "assistant"){
      if(m.who) return String(m.who).trim();
      var t = String(rawOf(m) || "");
      var pm = t.match(/^【([^】]{1,24})】/);
      if(pm) return pm[1].trim();
      var pm2 = t.match(/^(?:\[\d{1,2}:\d{2}\]\s*)?<msg\b[^>]*>\s*(?:\[([^\]\n]{1,24})\]|【([^】\n]{1,24})】)/);
      if(pm2) return (pm2[1] || pm2[2]).trim();
      return "";
    }
    if(m.role === "user"){
      var parts = splitMetaAll(tidyBody(m.text || ""));
      if(parts.length && parts[0].meta && parts[0].meta.name) return String(parts[0].meta.name).trim();
      var pm3 = String(rawOf(m) || "").match(/\[user_nickname:\s*([^,\]\n]{1,32})/);
      return pm3 ? pm3[1].trim() : "";
    }
    return "";
  }
  function uidOf(m){
    if(!m || m.role !== "user") return "";
    var parts = splitMetaAll(tidyBody(m.text || ""));
    if(parts.length && parts[0].meta && parts[0].meta.uid) return String(parts[0].meta.uid).trim();
    // 兜底：真实格式是「[user_nickname: 某, user_id: 123]」，user_id 前面没有独立的「[」。
    var pm = String(rawOf(m) || "").match(/user_id:\s*(\d{3,})/);
    return pm ? pm[1] : "";
  }
  function youOf(m){
    if(!m || m.role !== "user") return false;
    var parts = splitMetaAll(tidyBody(m.text || ""));
    return !!(parts.length && parts[0].meta && parts[0].meta.you);
  }
  function multiOf(m){
    if(!m || m.role !== "user") return false;
    return splitMetaAll(tidyBody(m.text || "")).length > 1;
  }
  function groupKeyOf(m, idx){
    if(!m) return "";
    var r = m.role || "?";
    if(r !== "user" && r !== "assistant") return r + "#" + idx;
    // 一条消息里拼着好几个人的，不跟前后合并（里面各段自己带名字）
    if(multiOf(m)) return r + "#multi" + idx;
    // 用户 m05124 第 3 条：决策注记一条算一组。它们的发言人全都叫「对当前聊天消息的决策建议」，
    // 按发言人分组会把上一次和这一次的判定并进同一张卡（图3 那样「两次判定并排」），
    // 看着像同一次判了两遍。注记是「那一次」的结论，本来就该一条一张卡。
    if(r === "assistant" && noteNameOf(m) === DEC_NOTE_NAME) return "note#" + idx;
    return r + "|" + whoOf(m) + "|" + uidOf(m);
  }
  // 圆形头像：user 有 QQ 号就走 qlogo，assistant 没有就用名字首字
  function avatarFor(role, who, uid){
    var ch = String(who || "").trim().slice(0, 1) || (role === "assistant" ? "A" : "U");
    var img = "";
    // bot 自己的 QQ 号框架不给插件，只能设置里手填（schema.bot_qq），拿不到就退回首字圆
    var qq = (role === "assistant") ? String(window.__BOT_QQ || "") : String(uid || "");
    if(/^\d{4,}$/.test(qq)){
      img = "<img class='gh-img' loading='lazy' referrerpolicy='no-referrer' alt='' src='https://q1.qlogo.cn/g?b=qq&nk=" +
            encodeURIComponent(qq) + "&s=100' onload=\"this.parentNode.classList.add('hasimg')\" onerror=\"this.remove()\">";
    }
    return "<span class='gh-av " + (role === "assistant" ? "bot" : "u") + "'>" +
           "<span class='gh-ch'>" + esc(ch) + "</span>" + img + "</span>";
  }
  // 对照档每条消息的「角色 · 字数 · 原文」标签。v2.91 把它从挤爆的左列挪到了
  // 正文下面一行，用户 m33400 图5 又说那样不紧凑 —— 改成挂到组头名字右边，
  // 一组只占一次，正文下面不再多一行。
  function mixNoteOf(m){
    var role = m.role || "?";
    return esc(role) +
           (m.tools ? " · tool_calls ×" + m.tools : "") +
           (m.tool_call_id ? " · " + esc(m.tool_call_id) : "") +
           " · " + rawOf(m).length + " 字 · 原文";
  }

  // 用户 m06193：用户黑名单（按 QQ 号整条剔除）。窗口页把鼠标放到名字上就能拉黑；
  // 拉黑后这一组默认折叠，组头显示「已拉黑 + 展开」，点开还能看。
  // 名单是全局的（用户 m06246 更正），只影响拉黑之后的新消息，已经在窗口里的历史不动。
  var UBLK = {};        // uid -> 1，当前黑名单
  // 用户 m07157：用户黑名单总开关。关掉时窗口页不画「拉黑」按钮、也不折叠，
  // 名单本身还在（设置页照常能看能解黑），只是不再剔除消息。
  var UBLK_ON = true;
  var UBLK_OPEN = {};   // uid -> 1，这一组被手动展开了
  var ublkSid = null, ublkAt = 0;
  function ublkFill(d){
    var m = {};
    ((d && d.items) || []).forEach(function(it){
      var u = String((it && it.uid) || "").trim();
      if(u) m[u] = 1;
    });
    // 名单没变就别重画：窗口每 5 秒重画一次，白重画会打乱首屏计数和滚动位置。
    var on = !(d && d.enabled === false);
    var changed = (on !== UBLK_ON) || Object.keys(m).length !== Object.keys(UBLK).length;
    if(!changed){ for(var k in m){ if(!UBLK[k]){ changed = true; break; } } }
    UBLK_ON = on;
    UBLK = m;
    return changed;
  }
  // 名单按会话缓存 30 秒：窗口每 5 秒重画一次，不能每次都打接口；
  // 但设置页那边刚拉黑的人，切回来最多半分钟就能看到。
  function grpHeadHtml(role, who, uid, you, note, tools){
    var nm = who ? esc(who) : (role === "assistant" ? "assistant" : "user");
    var decName = role === "assistant" && who === DEC_NOTE_NAME;
    var tcChip = (tools === 0 || tools === null || tools === undefined || tools === "") ? ""
      : "<span class='chb'>工具 ×" + tools + "</span>";
    // 用户 m06193：真人名字后面挂「拉黑」（鼠标移上去才显示）。已经拉黑的换成
    // 「已拉黑」常驻胶囊 + 展开/折叠，点开还能看这一组的内容。
    var blkChip = "";
    if(role === "user" && uid && UBLK_ON){
      blkChip = UBLK[uid]
        ? ("<span class='gh-blk on'>已拉黑</span>" +
           "<button type='button' class='gh-exp' data-blk-exp='" + esc(uid) + "'>" +
           (UBLK_OPEN[uid] ? "折叠" : "展开") + "</button>")
        : ("<button type='button' class='gh-blk' data-blk-uid='" + esc(uid) +
           "' data-blk-name='" + esc(who || "") + "'>拉黑</button>");
    }
    return "<div class='mgrp-hd'>" + avatarFor(role, who, uid) +
           "<span class='gh-info'><span class='gh-nm" + (decName ? " dec-name" : "") + "'>" + nm + "</span>" + blkChip + tcChip +
           (you && VIEW !== "mixed" ? "<span class='k you'>@你</span>" : "") +
           (note ? "<span class='gh-note'>" + note + "</span>" : "") +
           "</span></div>";
  }

  function systemCardHtml(text, mode){
    var m = {role: "system", text: text, raw: text, chars: text.length};
    return "<div class='mgrp system'>" + grpHeadHtml("system", "system", "", false, mode === "mixed" ? mixNoteOf(m) : text.length + " 字") +
      mcHtml(m, "", mode, "", null, "", true) + "</div>";
  }

  function markBadge(mk){
    if(!mk) return "";
    var label = MARK_LABEL[mk] || mk;
    var cls = MARK_CLASS[mk] || "";
    return "<span class='chb tag mk " + cls + "'>" + esc(label) + "</span>";
  }

  // tail：卡尾追加的小标签（v3.89 用户 m05321：裁切快照里那些「待发」）
  function msgCards(m, idx, mode, ts, grp, tail){
    mode = mode || VIEW;
    if(mode === "raw") return windowMsgHtml(m, idx, ts, tail);
    if(m.role === "user" && mode === "pretty"){
      // v4.32：先拆原始各段；markdown 图片地址留给该段 botCard 提取。
      var parts = splitMetaAll(String(m.text || "").replace(/\\n/g, "\n"));
      if(parts.length > 1){
        // 整条聚合媒体无 per-mid 证明：不按图片占位猜 owner，不跨 owner 去重。
        var d = displayOf(m), neutral = uniqueOwnerMedia(d.own.concat(d.unassigned));
        var h = "";
        parts.forEach(function(q, qi){
          var body = String(q.body || "").replace(/\s+$/, "");
          if(!body && !q.meta.reply && !q.meta.fwd && !q.meta.card && !q.meta.file &&
             !(qi === 0 && m.files && m.files.length)) return;
          h += mcHtml({ role: "user", text: body, raw: body, media: [],
                        display: { own_media_known: false, own_media: [], unassigned_media: [], direct_reply: null, source: "unknown" },
                        files: (qi === 0 ? (m.files || null) : null),
                        chars: visLen(tidyBody2(body, false)), tools: 0, tool_call_id: "" },
                      idx, mode, "", q.meta, shortTs(q.meta.ts || ""), grp,
                      (qi === parts.length - 1 ? (tail || "") : ""));
        });
        h += unassignedMediaHtml(neutral, mode);
        return h;
      }
    }
    return mcHtml(m, idx, mode, "", null, ts, grp, tail);
  }

  // 待发条目（攒着还没进历史的那些）也走同一套卡片。
  // 之前这里另写了一套：bot 的回复不过 parseAssistant、原文档还会落到
  // 「(非文字消息)」，窗口页和请求页同一类东西长两个样（用户 m27043）。
  // 待发的原文：bot 的 raw 是空串（只有 text、没时间），user 的 raw 还带着
  // 框架的长格式时间 [Oct 20 2026 23:48 Tue]。原文模式要跟历史一个长相，
  // 统一压成 [HH:MM] 开头（用户 m30252 第 2 条）
  function pendRaw(m){
    // 用户第 3 条：对照模式的「待发原文」得跟后端真正拼给模型的文本一致，
    // 框架的长格式时间（[Oct 23 2026 22:55 Fri] / ISO）统一压成 [HH:MM]，
    // 口径跟后端 window/manager.py 的 shrink_timestamp 一样
    var rt = rawOf(m) || "";
    var hh = m.ts ? fmtTime(m.ts).slice(6, 11) : "";
    if(!hh) return rt;
    // 长格式有两版：[Oct 20 2026 23:48 Tue]（raw_line_of）和 [2026-10-23 21:30:56]（ISO）
    var long = rt.match(/^\[(?:[A-Za-z]{3} \d{1,2} \d{4} \d{1,2}:\d{2}(?: [A-Za-z]{3})?|\d{4}-\d{1,2}-\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?)\]\s*/);
    if(long) return "[" + hh + "] " + rt.slice(long[0].length);
    if(!/^\[\d{1,2}:\d{2}\]/.test(rt)) return "[" + hh + "] " + rt;
    return rt;
  }

  function pendCards(m, idx, mode, grp){
    // 用户第 3 条：对照档（原文 ↔ 美化）里的「待发原文」走 pendRaw，
    // 别把 [Oct 23 2026 …] [message_id: …] 这种框架内部格式原样铺出来
    mode = mode || VIEW;
    var isBot = (m.mark === "bot");
    // 后端 push_pending 已经 strip 过，但热重载前推进来的老条目还带框架残留空格，
    // 前端再兜一层，不然字数会多算 1（用户 m32956 图1：显示 6 字其实 5 字）
    var t = (m.text === undefined || m.text === null) ? "" : String(m.text).trim();
    var raw = mode === "pretty" ? displayTextOf({ raw: m.raw, text: t }) : rawOf({ raw: m.raw, text: t });
    // 待发的正文优先用带 meta 段的那份：历史卡能显示「时间 昵称 #id」是因为
    // text 里带着 [HH:MM] [message_id: …] [user_nickname: …]，待发的 text
    // 是洗过的正文，只有 raw 里才有这些，不用 raw 就少一头（用户 m27043）
    var shown = (String(raw).indexOf("[message_id:") >= 0 ||
                 String(raw).indexOf("[user_nickname:") >= 0) ? raw : t;
    // 待发条目后端给的是 media_refs（[{path,mime}]），跟历史项的 media 统一一下
    var mrefs = (m.media_refs && m.media_refs.length) ? m.media_refs
              : (m.media_path ? [{ path: m.media_path }] : []);
    // v3.24（用户 m45932 第4条）：纯表情的待发条目既没有正文、也没有可显示的图
    //（QQ 表情只有个 emoji id，media_refs 是空的），卡片就只剩一个空气泡。
    // 用类型名兜一句话，至少能看出「这儿有条表情」。
    if(!String(t).trim() && !mrefs.length && m.media){
      shown = "[" + (MEDIA_LABEL[m.media] || "消息") + "]";
    }
    // 用户第 3 条（对照模式的「待发原文」不是真正发出去的样子）：
    // 后端 push_pending 存的是框架长格式 [Oct 23 2026 22:55 Fri] [message_id: …] …，
    // 而真正拼给模型的那份在发出去前被 shrink_timestamp 压成了 [22:55] 开头
    // （decision/engine.py:676 / window/manager.py:547），历史卡的原文也是 [22:55]。
    // 这里用 pendRaw 把待发原文压成同一长相，否则上下两张卡一个 [22:52] 一个
    // [Oct 23 2026 22:55 Fri]，看着像两套格式（对照档 mixed 用的就是这个 raw）。
    var rawShown = pendRaw({ raw: m.raw, text: t, ts: m.ts });
    // v3.63（用户 m18492 图1）：只发了一张图/一个表情的待发，正文是空的，原文与对照档就只剩一个 [17:51]，
    // 看着像消息丢了。原文里补一个占位，跟模型真正看到的文本对齐（美化档本来就有这段兜底）。
    // 判「这条没有正文」不能只看原文——原文里还有 [message_id] / 昵称 / 结尾那个竖线，
    // 按剥掉时间戳后的长度判会永远判成有正文（t 才是真正给人看的那份正文）。
    var noBody = !String(t).trim();
    var mediaTag = "[" + (MEDIA_LABEL[m.media] || "图片") + "]";
    if(noBody && mrefs.length){
      rawShown = String(rawShown).trim() + " " + mediaTag;
    }
    // 纯转发条目没有正文（t 为空）：visLen 按口径也不把 [Forward …] 里的内容算字，
    // 所以这里 chars 就是 0，卡片不挂字数标签——是对的，别为了「有数字」硬凑
    // （用户 m02693 第 3 条：待发不能漏掉这条，字数不是重点）。
    // 原文档（raw 档）走的是 vm.text，不再读 raw：这里也要补上同一个占位，否则还是只剩一个时间
    var vmText = (mode === "raw" && noBody && mrefs.length) ? mediaTag : shown;
    var vm = { role: isBot ? "assistant" : "user", text: vmText, raw: rawShown, media: mrefs,
               display: m.display,
               who: isBot ? (m.name || m.sender || "") : "",
               chars: visLen(t), tools: 0, tool_call_id: "" };
    var tags = (isBot ? "<span class='chb'>刚发出</span>"
                      : "<span class='chb tag pendtag'>待发</span>") +
               (m.media && !(m.files && m.files.length) ? "<span class='chb'>" + esc(MEDIA_LABEL[m.media] || m.media) + "</span>" : "") +
               (isBot ? "" : markBadge(m.mark));
    // 时间口径跟历史卡一样只给 HH:MM（fmtTime 带日期和秒，太吵）
    // 组内：标签（刚发出/待发/计入）挪到行尾，头部只留时间
    var full = m.ts ? fmtTime(m.ts) : "";
    var h = mcHtml(vm, idx, mode, grp ? "" : tags, null, grp ? full : full.slice(6, 11), grp, grp ? tags : "");
    // user 的待发还是虚线框着：它确实还没进历史；bot 的已经发出去了，不框
    return isBot ? h : h.replace(/^<div class='mc /, "<div class='mc pend ");
  }

  // 原文档只做视觉排版，文本一律原样：后端给了 raw 就用 raw
  // （多模态数组照样按 JSON 打印），没有才退回 text（用户 m22917）。
  // 多模态 content 是数组：原文档要的是「模型看到的那段文字 + 这里有一张图」，
  // 不是把整个 part 字典打出来。直接 JSON.stringify 的话，每条带图消息后面都拖着
  // {"type":"kira_image_ref","path":"session_media/<hash>/…","mime_type":…}（用户 m29826 图3）
  function rawParts(v){
    if(typeof v === "string") return v;
    if(v === null || v === undefined) return "";
    if(Array.isArray(v)){
      var out = [];
      v.forEach(function(it){
        if(!it || typeof it !== "object"){ out.push(String(it)); return; }
        var tp = it.type || "";
        if(tp === "text") out.push(String(it.text || ""));
        else if(tp === "image_url" || tp === "image" || tp === "kira_image_ref" || tp === "input_image") out.push("[图片]");
        else if(tp === "input_audio" || tp === "audio") out.push("[语音]");
        else out.push("[" + (tp || "附件") + "]");
      });
      return out.join("\n");
    }
    return JSON.stringify(v);
  }

  // bot 消息进原文/对照时统一成「[HH:MM] 【名字】 正文」这一种头：
  // 老记录被前缀过两次（[22:42] [桃华] [22:42] …，用户 m51324 图1），
  // 回补的消息又完全没有头（图2/图3）。两种都在这里归一化。
  // v4.27（用户 m22571）：原文里的决策注记一律用它的固定名，不许顶 bot 的名字。
  // who 是显示名（樱井桃华），name 才是「对当前聊天消息的决策建议」。
  function noteNameOf(m){
    if(!m) return "";
    if(m.decnote === true) return DEC_NOTE_NAME;
    var nm = String(m.name || ""), wh = String(m.who || "");
    if(nm === DEC_NOTE_NAME || wh === DEC_NOTE_NAME) return DEC_NOTE_NAME;
    return nm || wh;
  }

  function botRawHead(t, name, ts){
    var s = String(t == null ? "" : t), times = [], names = [], m, k;
    name = String(name || "");
    for(k = 0; k < 5; k++){
      if((m = s.match(/^\s*\[(\d{1,2}:\d{2})\]\s*/))){ times.push(m[1]); s = s.slice(m[0].length); continue; }
      if(name && (m = s.match(/^\s*(?:\[([^\[\]\n]{1,24})\]|【([^】\n]{1,24})】)\s*/)) && (m[1] || m[2]) === name){
        names.push(m[1] || m[2]); s = s.slice(m[0].length); continue;
      }
      break;
    }
    var hh = times[0] || (/^\d{1,2}:\d{2}$/.test(String(ts || "")) ? ts : "");
    var nm = names[0] || name;
    // v3.93（用户 m07570 图4）：bot 的原文本身就长这样——「<msg …>[名字] <text>…」，
    // 前面再补一次名字就成了「[21:25] [樱花桃华] <msg …>[樱花桃华] <text>…」。
    // <msg> 里已经带了同一个名字，就不再补。
    if(nm && /^\s*<msg\b/.test(s)){
      var head300 = s.slice(0, 300);
      if(head300.indexOf(">[" + nm + "]") >= 0 || head300.indexOf(">【" + nm + "】") >= 0) nm = "";
    }
    return (hh ? "[" + hh + "] " : "") + (nm ? "【" + nm + "】 " : "") + s;
  }

  function rawOf(m){
    if(!m) return "";
    // raw 是空串也要回落 text：bot 的待发条目 raw 就是 ""，不回落就整条变空白（用户 m26991）
    var v = m.raw;
    if(v === undefined || v === null || v === "") v = m.text;
    return rawContent(v);
  }

  // 原文模式里的 tool_calls 行：跟请求页原文逐字一致，bot 上一轮调了什么在窗口里也看得见（用户 m32644/m32663）
  function tcRawLine(m){
    var tc = (m && m.tc) || [];
    if(!tc.length) return '';
    var arr = [];
    for(var i = 0; i < tc.length; i++){
      var c = tc[i] || {};
      arr.push({id: c.id || '', type: 'function', function: {name: c.name || '', arguments: c.args || ''}});
    }
    return '[tool_calls] ' + JSON.stringify(arr);
  }

  function windowMsgHtml(m, idx, ts, tail){
    if(VIEW === "raw"){
      var rt = rawOf(m) || "";
      if(m.role === "assistant") rt = botRawHead(rt, noteNameOf(m) || m.who || m.name || "", ts);
      // v3.63（用户 m18492 图2）：原文档同样补工具调用原文
      var tcl2 = (m.role === "assistant") ? tcRawLine(m) : "";
      if(tcl2) rt = String(rt).replace(/\s+$/, "") + "\n" + tcl2;
      return "<div class='rlm'><span class='rl'>" + (m.role === "summary" ? "中期摘要" : esc(m.role)) +
             (m.pending ? " · 待发" : "") + "</span>\n" +
             esc(rt || "(空)") + "</div>";
    }
    return mcHtml(m, idx, VIEW, "", null, ts);
  }

  // 重画完把整页滚动位置放回去。下一帧再放：innerHTML 之后要等一次布局，
  // 同步设置有时会被随后的高度变化再夹一次。
  // 切档位（美化/对照/原文）时同一批消息的高度能差两千多像素：原文档每条都摊开全文，
  // 死守绝对 scrollY 会让视口落到完全不相干的消息上，看起来就是「闪了一下」（用户 m41961）。
  // 这种时候改按「视口在面板里的相对位置」放：切之前记 0~1 的比例，重画后换算成新位置。
  var winAnchor = null;
  // 进快照前的绝对滚动位置：快照页比实时窗口短，退出时按比例换算会偏，
  // 直接按原数值回去（用户 m42220 的「看快照后回不到原处」）。
  var ctxSnapReturnY = null;
  // mode: "view" = 同一批消息换排版（切档位），按比例锚定；
  //       "snap" = 换成另一批消息（点柱子看快照），整页位置保持不动。
  function grabWinAnchor(mode){
    var box = document.getElementById("ctx-window");
    if(!box) return;
    var r = box.getBoundingClientRect();
    var h = r.height || 0;
    if(h <= 0){ winAnchor = null; return; }
    // 面板整个在视口下方（视口还停在概览/会话列表那边）时没有「面板内位置」可言，
    // 按 0 算会把视口硬拖到面板顶部——从页面顶部点柱子就被往下弹 246px（用户 m42220）。
    if(r.top >= 0){ winAnchor = null; return; }
    var v = (-r.top) / h;
    winAnchor = { mode: mode || "view", a: v > 1 ? 1 : v };
  }

  function restoreWinScroll(y){
    var box = document.getElementById("ctx-window");
    if(box && winAnchor != null){
      var anchor = winAnchor; winAnchor = null;
      requestAnimationFrame(function(){
        var r = box.getBoundingClientRect();
        var h = r.height || 0;
        var want;
        if(anchor.mode === "snap"){
          // 点柱子换的是另一批消息（快照），不是同一批的排版变化：整页滚动位置保持不动，
          // 只在面板缩到视口上方时夹回来，免得视口落到面板下方去（用户 m40932 / m42220）。
          want = (typeof y === "number" && y > 0) ? y : (window.scrollY || 0);
          var top = (window.scrollY || 0) + r.top;
          if(h > 0 && top + h < want + 80) want = Math.max(0, top + h - 80);
        } else {
          if(h <= 0) return;
          want = (window.scrollY || 0) + r.top + anchor.a * h;
          if(want < 0) want = 0;
        }
        var max = Math.max(0, (document.documentElement.scrollHeight || 0) - window.innerHeight);
        // 快照比实时窗口短时页面滚不到原位置，浏览器会把 scrollY 夹到新的底部。
        // 这里不去垫高面板补足——垫出来的就是用户 m42220 抱怨的「底部一大片空白」；
        // 保持位置交给退出快照时的 ctxSnapReturnY 原样回去。
        var target = Math.min(want, max);
        if(Math.abs((window.scrollY || 0) - target) > 2) window.scrollTo(0, target);
      });
      return;
    }
    if(!y) return;
    // 双帧再放：切页那一帧文档高度还在变（请求表刚显示、窗口面板刚清空），
    // 立刻 scrollTo 会被浏览器夹成 0，看起来就是「画面回拉到最顶」（用户 m42220）。
    requestAnimationFrame(function(){
      requestAnimationFrame(function(){
        var max = Math.max(0, (document.documentElement.scrollHeight || 0) - window.innerHeight);
        var target = Math.min(y, max);
        if(Math.abs((window.scrollY || 0) - target) > 2) window.scrollTo(0, target);
      });
    });
  }

  // 退出快照（点绿柱 / 点返回 / 再点同一根柱子）：回到进快照前的绝对位置。
  // 快照态可能被垫高过 min-height，这里一并交给 renderCtxWindow 清掉。
  var SECHD_HTML = "<div class='sec-hd'><span class='pane-sw'>" +
    "<button data-pane='window'>实时窗口</button><button data-pane='rounds'>回复记录</button>" +
    "<button data-pane='decisions'>决策记录</button></span>" +
    "<span class='vsw'><button data-view='pretty'>美化</button><button data-view='mixed'>对照</button>" +
    "<button data-view='raw'>原文</button></span>" +
    // 用户 m00942 第 5 条：切换按钮右边加一个滑动开关，折叠掉没意义的柱子。
    "<span class='sw sw-fold" + (TREND_FOLD ? " on" : "") + "' data-trend-fold='1' " +
    "title='只留裁切、快照和实时柱'><span class='tr'></span></span>" +
    "<span class='grow'></span></div>";

  // v3.62（用户 m18059 第 6 条）：走势图能整块横向滚动后，选中的那根（点旧柱子进来的、
  // 或从请求表点进来的）常落在可视区外，画完把它滚进视野，不然看着像没选中。
  // 用户 m00942 第 3 条：柱子滚到旧日期后点快照会「回弹」——重画整块 innerHTML 会把
  // .trend-in 的 scrollLeft 归零，revealTrendSel 再把选中柱推到右边缘。重画前后把横向
  // 位置接回去，用户看到的就是原地选中。
  function trendScrollX(root){
    var box = root && root.querySelector ? root.querySelector(".trend-in") : null;
    return box ? (box.scrollLeft || 0) : 0;
  }
  function restoreTrendScroll(root, x){
    if(!x) return;
    var box = root && root.querySelector ? root.querySelector(".trend-in") : null;
    if(box) box.scrollLeft = x;
  }

  function revealTrendSel(el){
    if(!el) return;
    requestAnimationFrame(function(){
      var box = el.querySelector(".trend-in") || el;   // v3.63：横向滚动在里层
      var sel = box.querySelector(".bar.sel");
      if(!sel) return;
      var a = sel.getBoundingClientRect(), b = box.getBoundingClientRect();
      if(a.left < b.left) box.scrollLeft -= (b.left - a.left + 8);
      else if(a.right > b.right) box.scrollLeft += (a.right - b.right + 8);
    });
  }

  var scrollingUntil = 0, deferWin = null, deferTimer = 0;
  function isScrolling(){ return Date.now() < scrollingUntil; }
  var DEC_NOTE_NAME = "对当前聊天消息的决策建议";
  function normDecNote(m){
    if(!m || typeof m !== "object" || m.decnote) return m;
    if(String(m.role || "") !== "assistant") return m;
    var nm = String(m.name || m.who || "");
    var raw = String(m.raw || m.text || m.content || "");
    if(nm !== DEC_NOTE_NAME && raw.indexOf("[决策记录]") !== 0) return m;
    var hit = /^\[决策记录\]\s*(\d{1,2}:\d{2})?\s*/.exec(raw);
    var body = hit ? raw.slice(hit[0].length) : raw;
    var o = {};
    for(var k in m){ if(Object.prototype.hasOwnProperty.call(m, k)) o[k] = m[k]; }
    o.decnote = true;
    o.name = DEC_NOTE_NAME;
    o.who = DEC_NOTE_NAME;
    o.raw = body;
    o.text = body;
    o.chars = body.length;
    if(hit && hit[1]) o.ts = hit[1];
    return o;
  }

  function toggleMore(more){
    var holder = more.parentNode;
    // v4.82（用户 m08672）：引用块的长原话也走同一个按钮，只是折叠的是 .qbody
    var isQuote = more.getAttribute("data-qbody") === "1";
    var body = holder ? holder.querySelector(isQuote ? ".qbody" : ".mono") : null;
    if(!body) return false;
    var clipped = body.classList.toggle("clip");
    // 用生成时记下的文案还原（JSON 是「N 行」，普通文本是「N 字」，别拿渲染后的长度糊弄）
    var label = more.getAttribute("data-label") || "展开";
    more.textContent = clipped ? label : "收起";
    // 引用块不参与正文的折叠记忆（它的 data-mi 跟正文不是一套）
    if(isQuote) return true;
    // 记住这一段是展开还是折着，换视图后照样保持（用户 m30252）
    var card = more.closest ? more.closest(".mc") : null;
    if(card && ctxWin){
      var k = (ctxWin.sid || "") + (ctxSnap ? ":snapshot:" + ctxSnap.name : "") + ":" + (card.getAttribute("data-ci") || "") +
              ":" + (more.getAttribute("data-mi") || "");
      foldOpen[k] = !clipped;
    }
    return true;
  }

  // 请求详情里整块收起来（历史动辄几十条，不收起要滚半天）
  var snapCache = {}, snapCacheKeys = [], SNAP_CACHE_MAX = 24;
  function snapCacheKey(sid, name){ return sid + "|" + name; }
  function snapCacheGet(sid, name){
    var k = snapCacheKey(sid, name);
    return Object.prototype.hasOwnProperty.call(snapCache, k) ? snapCache[k] : null;
  }
  function snapCachePut(sid, name, rec){
    if(!sid || !name || !rec) return;
    var k = snapCacheKey(sid, name);
    if(!Object.prototype.hasOwnProperty.call(snapCache, k)){
      snapCacheKeys.push(k);
      while(snapCacheKeys.length > SNAP_CACHE_MAX){ delete snapCache[snapCacheKeys.shift()]; }
    }
    snapCache[k] = rec;
  }
  function fetchRound(sid, name){
    var hit = snapCacheGet(sid, name);
    if(hit) return Promise.resolve({ok: true, record: hit, cached: true});
    return get(API + "/request?sid=" + encodeURIComponent(sid) + "&name=" + encodeURIComponent(name)).then(function(d){
      if(d && d.ok && d.record) snapCachePut(sid, name, d.record);
      return d;
    });
  }
  // 把选中框立刻挪到刚点的柱子上（真正的数据回来后会整块重画，这里只是先给个回应）
  function markTrendSel(el){
    if(!el || !el.closest) return;
    var box = el.closest(".trend");
    if(!box) return;
    [].slice.call(box.querySelectorAll(".bar.sel")).forEach(function(b){ b.classList.remove("sel"); });
    if(el.classList && el.classList.contains("bar")) el.classList.add("sel");
  }
  // 鼠标扫过柱子就先取一份：一进柱子就排队取（20ms 防抖只留最后一根），
  // 但两次预取之间至少隔 120ms，免得横扫时一口气发十几个请求。
  var snapPrefetchTimer = null, snapPrefetchLast = "", snapPrefetchAt = 0;
  var snapPrefetchNewest = "";
  function prefetchRecentSnaps(){
    if(!ctxSid || !ctxRounds || !ctxRounds.length) return;
    var names = [];
    for(var i = 0; i < ctxRounds.length && names.length < 1; i++){
      var nm = ctxRounds[i] && ctxRounds[i].name;
      if(nm && !snapCacheGet(ctxSid, nm)) names.push(nm);
    }
    if(!names.length) return;
    var sig = ctxSid + "|" + names.join(",");
    if(sig === snapPrefetchNewest) return;
    snapPrefetchNewest = sig;
    var sid = ctxSid;
    names.forEach(function(nm, i){
      setTimeout(function(){
        if(sid !== ctxSid || document.hidden || snapCacheGet(sid, nm)) return;
        try{ fetchRound(sid, nm).catch(function(){}); }catch(e){}
      }, 600 + i * 1200);
    });
  }

  // 剪贴板：navigator.clipboard 在 http://127.0.0.1 下可用；万一被拒（老浏览器 / 没权限）
  // 退回 textarea + execCommand，别让「点了没反应」。
  function copyText(text){
    var s = String(text == null ? "" : text);
    if(!s) return Promise.resolve(false);
    if(navigator.clipboard && navigator.clipboard.writeText){
      return navigator.clipboard.writeText(s).then(function(){ return true; },
                                                  function(){ return legacyCopy(s); });
    }
    return Promise.resolve(legacyCopy(s));
  }
  function legacyCopy(s){
    try{
      var ta = document.createElement("textarea");
      ta.value = s;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.top = "-1000px";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      var ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return !!ok;
    }catch(e){ return false; }
  }

  // 用户 m00942 第 4 条：点编号复制，给个「已复制」的即时反馈（1.2 秒后复原）。
  function copySnapId(el){
    var txt = el.getAttribute("data-snap-id") || "";
    if(!txt) return;
    copyText(txt).then(function(ok){
      if(!ok){ mdAlert("复制失败，编号：" + txt); return; }
      var old = el.textContent;
      el.classList.add("done");
      el.textContent = "已复制";
      setTimeout(function(){ el.classList.remove("done"); el.textContent = old; }, 1200);
    });
  }

  // 用户 m00942 第 5 条：折叠柱子开关。柱子还是那些柱子，只是把「回复 / 没回 / 出错 /
  // 决策」这些日常柱子收起来，留下裁切、快照和实时——凌晨攒到缓冲上限那几刀才是要看的。
  var paneOpen = {window: [], rounds: [], decisions: []};
  function payloadInfo(r){
    var known = typeof r.total_estimated_bytes === "number";
    var total = known ? r.total_estimated_bytes : (r.payload_bytes || 0);
    var partial = known && r.payload_estimate_complete === false;
    // 体积只留一个口径（用户 m41511）：真正发出去的是图片 base64，源图大小是另一个数，
    // 摆在一起会让人以为「源比 base64 小」，所以不再显示。
    var note = known ? "文本/JSON " + fmtBytes(r.text_payload_bytes || 0) +
      " · 图片 " + (r.image_count || 0) + " 张 · base64 " + fmtBytes(r.image_base64_estimated_bytes || 0) +
      (partial ? " · " + (r.image_unknown_count || 0) + " 张源图缺失/大小未知" : "") : "旧记录文本JSON体积";
    note += r.payload_measurement === "estimated_native_projection_not_wire_bytes"
      ? "；按框架原生图片请求投影预测，非实际wire抓包字节"
      : "；文本JSON与显式图片估算，非网络实测字节";
    return {total: total, partial: partial, known: known, note: note,
      label: (partial ? "≥ " : "约 ") + fmtBytes(total)};
  }

  // 两页共用本页统计；0 是有效值，缺失值不冒充 0，推断与实测使用同一批记录。
  function recordPageStats(rows){
    var a = {count: rows.length, input: null, output: null, hit: null, estimate: null, delta: null,
      inferred: false, bytes: null, partial: false, hitCount: 0, estCount: 0, pairCount: 0};
    var hs = 0, hw = 0, es = 0, ew = 0, ds = 0, dw = 0;
    function number(v){
      if(v === null || v === undefined || v === "" || typeof v === "boolean") return null;
      var n = Number(v);
      return isFinite(n) && n >= 0 ? n : null;
    }
    function pct(v){ var n = number(v); return n !== null && n <= 100 ? n : null; }
    rows.forEach(function(r){
      var tk = r.tokens || {}, input = number(tk.input), output = number(tk.output);
      if(input !== null) a.input = (a.input === null ? 0 : a.input) + input;
      if(output !== null) a.output = (a.output === null ? 0 : a.output) + output;
      var ss = Array.isArray(r.step_stats) ? r.step_stats : [];
      var multi = Number(r.steps) > 1 && ss.length > 1;
      var hit = pct(tk.cache_hit), est = pct(r.hit_estimate), inferred = false;
      // 与表格单行保持一致：多步用逐步平均，推断轮优先用 hit_est。
      if(multi){
        var se = pct(stepAvgPct(ss, "est"));
        if(se !== null) est = se;
      }
      if(r.hit_inferred){
        var ih = pct(multi ? stepAvgPct(ss, "hit_est") : (ss[0] && ss[0].hit_est));
        if(ih !== null){ hit = ih; inferred = true; }
      } else if(hit !== null && multi){
        var sh = pct(stepAvgPct(ss, "hit"));
        if(sh !== null) hit = sh;
      }
      var weight = input || 0;
      if(inferred){
        var full = 0;
        (multi ? ss : ss.slice(0, 1)).forEach(function(s){
          if(!s || pct(s.hit_est) === null) return;
          full += number(s.full_est) || number(s.in) || 0;
        });
        if(full > 0) weight = full;
      }
      if(weight > 0){
        if(hit !== null){ hs += hit * weight; hw += weight; a.hitCount++; if(inferred) a.inferred = true; }
        if(est !== null){ es += est * weight; ew += weight; a.estCount++; }
        // 偏差保留“预估减命中”，只比较同一批、同时具备两个值的记录。
        if(hit !== null && est !== null){ ds += (est - hit) * weight; dw += weight; a.pairCount++; }
      }
      var p = payloadInfo(r), bytes = number(p.total);
      var bytesKnown = number(r.total_estimated_bytes) !== null || number(r.payload_bytes) !== null;
      if(bytesKnown && bytes !== null) a.bytes = (a.bytes === null ? 0 : a.bytes) + bytes;
      if(!bytesKnown || !p.known || p.partial) a.partial = true;
    });
    if(hw) a.hit = hs / hw;
    if(ew) a.estimate = es / ew;
    if(dw) a.delta = ds / dw;
    return a;
  }

  function recordPageSummary(rows){
    if(!rows.length) return "";
    var a = recordPageStats(rows);
    var tip = "本页 " + a.count + " 轮；入/出为上游原始 usage 合计。命中和预估按输入 token 加权，明确的 0% 也参与；缺失值不当作 0。" +
      "命中有值 " + a.hitCount + " 轮，预估有值 " + a.estCount + " 轮；偏差只按同时有两个值的 " + a.pairCount + " 轮计算，等于预估减命中。";
    if(a.inferred) tip += "≈ 含推断：推断轮按可用逐步统计的完整输入估算加权，最多 8 步；因此不等于原始 cached/input。";
    tip += "请求体为本页各记录保存的文本和图片 base64 投影估算之和，不是多步全过程的网络流量；M/K 按 1024 换算。";
    if(a.partial) tip += "部分体积缺失或仅有旧版文本数据，显示已知下限。";
    var hit = a.hit === null ? "—" : (a.inferred ? "≈" : "") + a.hit.toFixed(1) + "%";
    var est = a.estimate === null ? "—" : a.estimate.toFixed(1) + "%";
    var delta = a.delta === null ? "—" : a.delta.toFixed(1) + "pp";
    var size = a.bytes === null ? "请求体 —" : "请求体" + (a.partial ? "≥ " : "约 ") +
      (a.bytes === 0 ? "0 B" : fmtBytes(a.bytes).replace(/([KM])B$/, "$1"));
    // 用户 m14026 第 3 条：顺序跟表头一致——先「命中预估」再「缓存命中」。
    return "<span class='hint' title='" + esc(tip) + "'>本页：入 " + tokText(a.input) + " · 出 " + tokText(a.output) +
      " · 预估 " + est + " · 命中 " + hit + " · 偏差 " + delta + " · " + size + "</span>";
  }

  // 决策记录与回复记录共用统计及分页布局。
  var ctxDecisions = [];
  var ctxDecisionsRequest = 0;
  var ctxDecisionsSid = "";
  var ctxDecisionsAt = 0;
  var ctxDecisionsError = "";
  // v3.94（用户 m08398）：展开过的那几行留节点。决策表以前每次轮询/切档都整表重建，
  // 刚点开的 ①②③④ 立刻被折回去（用户说的「上个模式切换又把折叠回去了」）。
  var ctxDecisionDetails = Object.create(null);
  // v3.97（用户 m09615 第 7 条）：展开过的详情留在内存里（键是 sid|快照名），切档、
  // 切页签再展开直接用缓存同步重画，不会先闪一下「加载中…」。
  var roundRecCache = Object.create(null);
  var ctxDecisionsInFlight = null;
