  var CHANNEL = "kira-plugin-page";
  var parts = location.pathname.split("/");
  var PLUGIN_ID = decodeURIComponent(parts[3] || "kira-ai-plugin-multi-decision");
  var API = "/api/plugin/" + encodeURIComponent(PLUGIN_ID);
  var CORE = "/api/plugins/" + encodeURIComponent(PLUGIN_ID);

  // v3.08 重排（用户 m38915）：原来 4 个页签里「触发与配额」20 项、「窗口与记录」17 项，
  // 一列排下来要滚很久。拆成 6 个主题页签 + 两列排布，每页 5–12 项，一屏能看完。
  var SET_GROUPS = [
    // v3.09（用户 m39057）：① 删掉「启用插件」——首页顶部那个开关就是它，两处重复
    // 且容易一边开一边关；②「接管群聊触发判定」与「接管默认聊天插件」合并成一项：
    // 后者只决定要不要停掉内置 default-chat，单独关掉只会让两个插件同时回复，
    // 从来不是用户想要的组合，现在跟着前者一起走（后端 main.py 同步绑定）。
    // v3.11（用户 m39492）：①「被 @ 必定回复」并入「总开关」一栏——两个都是
    // 「这个群要不要让它开口」的总闸；②身份区只留真正要填的：名字、外号、
    // 两个唤醒开关，QQ 号改由框架配置自动读，不再占一行。
    // v3.13（用户 m39983）：「总开关」改名「主要开关」，并且这两组改成单列（solo）——
    // 4 个开关全部排在同一列右侧、从上往下。
    // v3.14（用户 m40145）：身份与开关并排成一屏——左半只填「机器人昵称」「机器人外号」
    // （外号不再常驻输入框，改列表末尾一个「+」），右半放 4 个开关；说明文字挪到名称右侧。
    // v3.17（用户 m40932）：重试不再单独占一整块——并进左列，排在身份下面。
    // 两块用同一个 col 键（left），renderSettings 会把同 col 的组塞进同一个 .col，
    // 于是左列 = 身份 + 重试，右列 = 4 个开关；行样式跟着 split 组统一成 inline
    // （名称 + 说明 + 右侧控件），跟身份行、开关行对齐成一条竖线。
    // v3.46（用户 m05828 第 4 条）：身份组补「管理员名字」「管理员 QQ 号」——
    // 名字填 QQ 真实名字（不是群昵称），QQ 号要真号；两项都只进上下文身份段。
    // v3.50（用户 m06761 第 7 条）：管理员从「名字 + QQ 号两个单值框」改成可加多位的胶囊列表
    // （custom: "admins"）。老的两个键 admin_name / admin_id 仍在 schema 里、值原样随表单提交，
    // 只是不再占行；页面只提交 admin_list（[{name,qq}] 的 JSON 文本）。
    { p: "basic", t: "身份", split: 2, col: "left",
      keys: ["bot_name", "nicknames"], custom: "admins" },
    // v3.45（用户 m05552 第 4 条）：看门狗超时也进「重试」组，放第一条——它管的是「多久
    // 没等到结算就当这轮失败」，是重试链的起点；写死 180 秒时卡一次要等到 3 分钟才重试。
    // 用户 m07859：重试 / 重试间隔 / 单次超时只留这一份，决策链和回复链都读它，
    // 提示词页那两张卡里不再各留一份。
    { p: "basic", t: "API 重试", split: 2, col: "left", keys: ["watchdog_seconds", "retry_max", "retry_delay"] },
    // 用户 m05550：重试的两个取舍开关都是双刃剑（开了可能跑题 / 开了可能漏掉一条），
    // 单独一组、默认都关，组名右边挂警告色胶囊提醒一句。
    { p: "basic", t: "重试取舍", split: 2, col: "left", warn: ["双刃剑", "未经充分测试"],
      keys: ["retry_take_pending", "timeout_no_retry"] },
    // v3.30（用户 m47796 第 4 条）：@ 拆成一行两个开关——「艾特触发」可点，
    // 用户 m00347：必回三个开关正式启用（不再灰着）——触发＝叫到算一次触发，
    // 必回＝跳过模型判断直接回。
    // v3.40（用户本轮第 5 条）：艾特 / 外号 / 昵称三条都归到「唤醒」一组，每行两个开关，
    // 「主要开关」只留 decision_enabled（custom 挪到唤醒组）。
    // 用户 m00363：组里再加一行「休息时段」，同样两个开关（触发 / 必回）。
    { p: "basic", t: "主要开关", split: 2, col: "right", keys: ["decision_enabled"] },
    { p: "basic", t: "唤醒", split: 2, col: "right", custom: "wake",
      // v3.41（用户本轮第 4 条）：必回那条说明从标题下面整行（gnote）挪到标题右侧（note → h3 里的 .gnt），
      // 跟其它组一个样子：一行内联小字，超长省略，全文留在 title 里。
      // v3.42（用户 m03616 第 2 条）：补上「触发」是什么——原来只讲了必回，
      // 刚上手不知道左边那列管什么。
      note: "触发＝激活判定；必回＝跳过判定" },
    // 用户 m10749 第 2 条：配额整组从「限额设置」页搬到基础设置右下角，紧跟「唤醒」组。
    // 两个开关开一个、另一个变灰（值保留，见 paintQuotaMutex），不再互相关掉。
    // v3.54（用户本轮第 3 条）：四行合成两行——每行「开关 + 数字框 + 一句说明」，
    // 行由 quotaRowsHtml 整块画（custom: "quota"），keys 仍写这四个键（标成已用，防被再渲染一份）。
    { p: "basic", t: "配额", split: 2, col: "right", custom: "quota",
      warn: ["未经充分测试"],
      keys: ["quota_hour_enabled", "quota_max_per_hour", "quota_day_enabled", "quota_max_per_day"] },
    // v3.15（用户 m40317）：触发页按「普通 / 高级 / 进阶」三档重排成三列。
    // v3.16（用户 m40583）：不再单列一行「当前模式」下拉——三列标题右侧各一个开关，
    // 打开哪列就切到哪档，另外两列变灰；普通模式列下面再挂「触发 / 概率」两个子开关。
    // trigger_mode 的真值由 #trg-mode 隐藏 input 承载（三个开关都是它的入口），
    // 所以这里不再列 trigger_mode 的 keys，否则会多出一个重复的下拉。
    { p: "trigger", t: "普通模式", split: 3, mode: "normal", sub: 1,
      // v3.40（用户本轮第 8 条）：删掉「计数：攒够 N 条回一次；概率：每条按百分比随机回。」
      // 那行说明——两个子开关就在下面，名字已经说清了。
      // v3.30（用户 m47796 第 2 条）：概率模式下可选「伪随机」——攒够 N 轮还没中就抬概率。
      // v3.31（用户 m48323 第 2/3 条）：兜底起始条数不再手填，按概率自动算
      // （10% → 10 条、33% → 3 条，四舍五入），空出来的位置改成「每轮没中再加多少」。
      // v3.42（用户 m03616 第 5 条）：随机兜底三项搬去「通用设置」——
      // 曲线模式下也吃这套兜底（engine.py 的 _should_trigger 两条分支都用了 prob_pseudo）。
      // v3.71（用户第 8 条末）：这三项又搬回这一列——v3.71 起曲线 / 动态调整不再吃伪随机兜底
      // （引擎只在普通概率模式里用它），挂在「全模式生效的设置」里反而会让人以为曲线也用它。
      // v3.44（用户 m04471 第 5 条）：连发合并只对计数模式生效（engine.py 的防线是
      // `if not prob_mode and …`），从「通用设置」搬回这列，紧跟两条计数参数。
      // v3.50（用户 m06761 第 6 条）：连发合并是「计数触发」的功能，排在它下面、触发概率上面；
      // 计数模式没开时整行灰掉不可点（refreshDimRows 给这行加 .off）。
      // v3.73（用户 m24640 第 6 条）：随机兜底三项再搬到「通用设置」下面那组——
      // 曲线模式也照用，所以不能再挂在「只属于普通模式」的这一列里。
      keys: ["trigger_threshold", "burst_merge_enabled", "trigger_probability",
        "trigger_probability_dm"] },
    // v3.72（用户第 2 条）：休息时段从「通用设置」搬回这一列——曲线模式现在有自己的休息时段
    // （band_rest_*，画在曲线上），这一组只管计数 / 概率模式；曲线一开这列整体变灰，正好说明
    // 「这时候看曲线上那条带子」。
    { p: "trigger", t: "到点休息时", col: "normal", split: 3,
      note: "计数 / 概率模式生效；曲线模式下点那段右上角的月亮",
      keys: ["rest_mode_enabled", "rest_start", "rest_end", "rest_probability"] },
    // v3.31（用户 m48323 第 6 条）：高级模式原来的位置（三列中间）给节奏。
    // v3.33（用户 m51046 第 6/10 条）：静默与休息单独成一组（同一列，靠分隔线隔开）——
    // 「群安静多久兜底」「休息时段那几分钟兜底」各自能开关、各自能填分钟数；
    // 「短消息字数」换成「不触发短语」清单（第 11 条），整条消息等于清单里的词就不回。
    // v3.36（用户 m00613）：兜底回复、休息时段两组整体收进一列——它们回答的本来就是
    // 「什么时候该开口」，各占半屏太空；组内用分段小标题隔开。
    // v3.37：休息开始/结束并成一行「休息时段」（custom 走 rest_mode_enabled 特判）。
    // v3.38：休息时段整组搬去普通模式列；节奏列只剩两种模式都用的项，拆成两小节，
    // 组内那行灰色分段小标题取消，改用和列标题同款的实心小标题（竖条 + 细线）。
    // v3.39（用户本轮）：列名「节奏与静默」→「通用设置」——这一列管的都是
    // 「两种模式都照用」的东西（节奏、短语过滤、两处兜底）；叫「高级设置」会跟
    // 下面的「高级模式·时段曲线」「高级模式·动态调整」和第三列「进阶模式」撞名。
    { p: "trigger", t: "通用设置", col: "rhythm", split: 3,
      // v3.40（用户本轮第 8 条）：删掉「两种模式都用得上：…」那行说明。
      // v3.42（用户 m03616 第 5 条）：随机兜底搬到这里（普通模式、曲线、概率三处共用）。
      // v3.44（用户 m04471 第 5 条）：抖动加成对行（开关默认关 + 固定秒数），
      // 连发合并搬去普通模式列，短语两项并成一行「短语黑名单」；
      // 组标题右侧补一句「全模式生效的设置」——这列的东西不跟着模式切换失效。
      note: "全模式都生效的设置",
      // v3.51（用户 m07385 第 5 条）：初始增加概率排在每轮增加概率前面——
      // 先定起点，再看每轮往上加多少，读起来才是顺着来的。
      // v3.71（用户第 8 条末）：伪随机三项搬回普通模式列，这一列只剩抖动与短语清单。
      // 用户 m05691 第 3 条：抖动最多叠几跳也是设置项，默认 3 次（7+7+7）。
      keys: ["jitter_enabled", "jitter_seconds", "jitter_max_rounds",
        "jitter_skip_must_go"] },
    // v3.39：兜底两项合成一小节——普通静默兜底与休息时段兜底都是「安静多久补一次」，
    // 曲线开着也照用（engine.py 2024-2058：曲线只接管回复概率，不接管兜底窗口）。
    // v3.44（用户 m04471 第 5 条）：组名「安静下来时」→「闲时兜底」，说明改成一句能覆盖
    // 两行的话；两行各由「开关 + 分钟」并成一行（PAIR_NUM 特判），不再一个参数占两行。
    // 休息那条的 hint 也不再写「开了曲线也照用」——这一列本来就是全模式生效。
    // 用户 m03262：组名「闲时兜底」→「闲时请求」，说明改成一句讲清两种静默；
    // 按类型过滤的 6 个开关拆出去单独成组（见下面的「静默过滤」）。
    { p: "trigger", t: "闲时请求", col: "rhythm", split: 3,
      note: "聊天静默时或休息静默时补一次扫尾请求",
      keys: ["silent_trigger_enabled", "silent_trigger_minutes",
        "rest_silent_enabled", "rest_silent_minutes"] },
    // 用户 m03262：过滤项单独一组——上面两行管「多久补一次」，这里管「哪些消息不值得补」，
    // 混在一组里读起来像同一套参数。6 个开关两两并排（见 lib-2.js 的 SW_PAIR）。
    // 用户 m03450：这组是双刃剑——过滤配多了会漏掉该补的那一轮，标题右边挂个胶囊提醒。
    { p: "trigger", t: "静默过滤", col: "rhythm", split: 3, warn: ["双刃剑"],
      note: "这些类型的消息不触发静默兜底",
      keys: ["silent_skip_after_decision", "silent_skip_after_decision_count",
        "silent_skip_short", "silent_skip_short_len",
        "silent_skip_video_link", "silent_skip_empty_forward",
        "silent_skip_single_image", "silent_skip_single_sticker"] },
    // v3.93（用户 m07570 图1 第 1 条）：节奏模式与决策模式说的都是「这一轮怎么决定
    // 回不回」，用户要在这两行上面加一条「高级设置」小标题把它们归进去。
    // 用户 m01961：决策模式搬去右列，这一节只剩节奏模式一行。
    { p: "trigger", t: "高级设置", col: "rhythm", split: 3, sect: 1 },
    // v3.81（用户 m02645 图2）：这一组从「伪随机（加概率）」换成「节奏模式」——
    // 开 = 按间隔排队回（state/band_rhythm.py），关 = 每条独立掷骰，概率模式 / 曲线模式 /
    // 动态调整共用同一套；「节奏松紧」也从动态调整那一行搬到这里。动态调整是强制开，
    // 不看这个开关（engine._judge 里 force=True）。
    // v3.92（用户 m06951 第 4 条）：这一组改成「标题 + 开关」一行（rowline: 1），
    // 标题不再单独占一行——用户原话「节奏模式 标题和选项合并 做成一行」。
    { p: "trigger", t: "节奏模式", col: "rhythm", split: 3, rowline: 1,
      // v3.93（用户 m07570 图1 第 1 条）：描述太省略，重写一句看得懂的人话。
      note: "开 = 按节奏排队，越等越容易中；关 = 每条单独掷骰",
      // v3.83（用户 m03849 图3）：加 grp 是为了让 refreshDimRows 能整组定位——
      // 计数模式不掷骰，这一组要整块压暗。
      grp: "rhythm",
      keys: ["prob_pseudo", "adaptive_rhythm_sigma"] },
    // 用户 m01961：左边那行「决策模式」搬到右边来，右边补一条「进阶模式」小标题——
    // 退避模式本来就排在进阶模式那一列下面（v3.53），这次把标题补回去。
    // 标题与这一行同处一个 .sub（v4.09 的 sect 合并），不会多出两层虚线内边距。
    { p: "trigger", t: "进阶模式", col: "dev", split: 3, sect: 1,
      // 用户 m18050：原来两行行尾各挂一枚「多一次调用，「提示词」页设置」胶囊，重复又占宽；
      // 现在整句搬到这个小节标题右侧，逗号去掉。
      warn: "多一次调用「提示词」页设置" },
    // v3.92（用户 m06951 第 4 条）：进阶模式改名「决策模式」，开关从提示词页搬回设置页，
    // 跟节奏模式一样「标题 + 开关」一行——这两行说的都是「这一轮怎么决定回不回」。
    // 提示词页只留人格 / 判断提示词 / 模型 / 工具那些设置（见那页顶上的两个子页签）。
    // 描述按用户原话改写：「开启后单独调用模型决定是否回复」。
    { p: "trigger", t: "决策模式", col: "dev", split: 3, rowline: 1,
      // v3.93（用户 m07570 图1 第 2 条）：用户要在描述后面补一句提醒——
      // 开它等于每轮多一次模型调用，而且判定用的提示词在「提示词」页那半边。
      // v4.36（用户 m02314）：说明和胶囊都压到一行装得下——「使用快速模型进行决策」+「多一次调用，「提示词」页设置」。
      // v4.67（用户 m02347）：把「使用模型」补成「使用快速模型」，默认就是快速模型，写明更直观。
      note: "使用快速模型进行决策",
      // 用户 m18050：这句提醒已搬到「进阶模式」小节标题右侧，行尾不再重复。
      keys: ["advanced_enabled"] },
    // 用户 m06706：JEV 决策——用接入的专用决策模型（如 Jev）走 choice() 判要不要开口，
    // 不让它写正文。与上面那条「决策模式」互斥：开一个当场关另一个（saveNow 成对存），
    // 但两边各自的模型 / 提示词 / 区间都留着，来回切不用重配。
    { p: "trigger", t: "JEV 决策", col: "dev", split: 3, rowline: 1,
      note: "用专用决策模型判断是否回复",
      // 用户 m18050：提醒已搬到「进阶模式」标题右侧。
      keys: ["jev_enabled"] },
    // v3.36：进阶模式还是占位块，等真做出来再补内容。
    // v3.37：节奏不再跨栏，它回到第三列（三列等宽）。
    // v3.53（用户本轮第 4 条）：退避并进进阶模式那一列，排在它下面。没开进阶模式时整组灰掉，
    // 但只灰显示、不清已存的值（见 refreshDimRows 里 data-grp='backoff' 那段）。
    // v3.92（用户 m06951 第 3 条）：组名「拒绝退避」→「退避模式」，并像摘要组那样挂上
    // 「还没做完」徽标——功能本身用户还要自己想清楚，这一轮只改名 + 标记，
    // 行为一个字节不动。
    { p: "trigger", t: "退避模式", split: 3, col: "dev", grp: "backoff", todo: "还没做完",
      note: "这一组还没做完，先整组灰着；开关值会照存，等做完了才生效。",
      keys: ["backoff_enabled", "backoff_start_rejects", "backoff_base_seconds",
        "backoff_max_seconds", "backoff_bypass_count"] },
    // v3.31（用户 m48323 第 5 条）：时段曲线独占一行左侧，高级模式落在它右边——
    // 动态模式必定要用曲线（第 8 条：高级设置直接体现在曲线上）。
    // v3.31（第 1 条）：曲线改成内联直编，不再弹窗。
    // v3.33（用户 m51046 第 9 条）：曲线是「高级模式」的总闸——标题右侧那个开关就是
    // bands_enabled。开了它：按曲线给的每段概率回，普通模式整列灰掉，休息时段的回复概率
    // 也不再单独生效（v3.39：只接管概率，休息时段兜底窗口照用）。
    { p: "trigger", t: "高级模式 · 时段曲线", split: "band", col: "band", custom: "bands", mode: "bands",
      note: "一天分段给概率" },
    // v3.71（用户第 7 条）：这一组整组撤掉——曲线说明下面那一行就是它的位置
    // （见 renderAdaptiveRow / #bd-adaptive），六个数字单独占一列太浪费版面。
    // adaptive_target_percent 随组下线（用户第 5 条：不再手填全局目标）。
    // 用户 m12007 第 4 条：私聊策略整组删除——原话「私聊策略完全删除，因为目前的设置
    // 也可能配置出私聊够用的情况了」。群聊那套触发条件（计数 / 概率 / 时段曲线 / 动态调整）
    // 对私聊一样生效，没必要再单开一套「必回 / 计数 / 概率」；私聊现在恒为必回。
    // 用户 m10749 第 2 条：配额组已搬到基础设置（见上面「唤醒」组之后），限额页整页撤掉。
    // v3.13（用户 m39983）：window_enabled 从表单里撤掉——接管上下文窗口是这个插件的
    // 核心功能，关掉它等于插件不存在，不该给人一个关掉自己的按钮（后端强制 True）。
    // v3.53（用户本轮第 5 条）：加「裁切缓冲条数」——一直没触发请求时，窗口攒到「保留 + 缓冲」
    // 条就强制裁一次，别让上下文无限长。
    // v3.86（用户 m04644）：三个裁切参数排左列——裁切后保留条数 / 裁切触发阈值 / 裁切缓冲条数。
    // 两列网格是「按顺序从左到右填」：偶数位落左列、奇数位落右列，所以要隔一个插一个杂项，
    // 右列才是「历史里保留几张原图 / 保护工具调用组 / 裁切点选择方式」。
    { p: "window", t: "窗口", keys: ["window_keep", "history_images", "window_trim_threshold",
        "protect_tool_groups", "window_buffer", "window_drop_tools"] },
    // 用户 m05550：基础设置页那组「窗口显示」挪到这一页——它管的就是窗口列多少条。
    // 用户 m17752：它管的是页面上列多少条，跟磁盘上留多少份不是一回事，别和「窗口快照上限」混着看。
    { p: "window", t: "窗口显示", custom: "show",
      note: "只影响窗口显示条数，磁盘上留多少份由「窗口快照上限」决定",
      keys: ["trace_keep", "trace_keep_dm", "decision_keep", "decision_keep_dm",
             "window_show", "window_show_dm"] },
    // 用户 m09851：两个「看图」开关从提示词页的模型卡搬到这里，跟图片压缩摆一块——
    // 它们管的都是「图片怎么进请求」，跟选哪个模型不是一回事。
    { p: "window", t: "图片", custom: "img",
      keys: ["decision_see_images", "reply_see_images"] },
    // v4.45（用户 m22611）：历史里的动图怎么发给模型，三档互斥、默认九帧拼图；
    // 三个都不开＝跳过（图上不来，正文只留 [图片]）。判定在后端 _gif_mode。
    { p: "window", t: "动图设置",
      note: "三档互斥（开一个关另两个）；全关＝跳过",
      keys: ["history_gif_first", "history_gif_original", "history_gif_grid"] },   // 用户 m10096：trim_strategy 已从设置里撤下（只剩机械裁切一种）
    // 用户 2026-11-06：转发消息怎么进请求——三档 + 一个风险开关。
    { p: "window", t: "转发消息",
      note: "默认按需查看：只挂前 3 张图，模型需要时自己调工具看完整内容",
      keys: ["forward_mode", "forward_drop_gif", "forward_images_all"] },
    // v3.67（用户 m20117 第 1 条）：中期摘要的保留份数 / 天数原本也归到这一组。
    // v4.84：这三项（私聊上限 / 群聊上限 / 保留天数）挪去「缓存记录」页——它们管的是留存，
    // 跟缓存占用摆在一起更顺；这一页只留「启用 / 字数上限 / 每条压多少字」三个压缩参数。
    // v3.86（用户 m04620）：这一组功能还没接进引擎，标题右边挂「功能未实现」，做完再去掉。
    { p: "window", t: "摘要", todo: "功能未实现", keys: ["summary_enabled", "summary_max_chars", "summary_line_chars"] },
    // v3.57（用户 m16357 图 3）：撤掉窗口页最后一组「实验功能」——它只剩 dynamic_to_tail
    // 一项，本来就锁定不可改，环境信息也已并入系统提示词，不该再摆给用户看。
    // （原来的「实验设置」整页在 m10749 第 4 条就撤了，PAGES 里没有 lab。）
    // 用户 m13250 第 5、6 条：原来三组（请求日志 / 请求明细 / 缓存记录）
    // 并成一块——
    //  · 「记录请求上下文」开关撤掉：这个插件本来就要一直记，没有关它的理由；
    //  · 「日志保留天数」撤掉：请求快照与请求记录一一对应，条数统一由「保留请求数」管，
    //    消息日志 / 窗口快照改成固定 60 天滚动（就是这个键原来的默认值），不用再问用户；
    //  · 一块里只剩「保留请求数」+ 图片缓存三条上限 + 一张各类缓存占用表（见 cacheBlockHtml）。
    // solo：表格要整幅宽，挤在两列里看不全。
    // v4.84：中期摘要的三项（私聊上限 / 群聊上限 / 保留天数）从「摘要」组挪到这一组——
    // 它们管的是留存，跟缓存占用摆在一起更顺。三项直接长在表格里「中期摘要」那一行上
    // （后端 _cache_kinds 给了 limit_key / days_key），不再由 cacheExtraHtml 补在表格下面。
    { p: "log", t: "缓存记录", solo: 1,
      keys: ["trace_keep", "media_cache_limit", "media_cache_keep_days",
             "summaries_keep_dm", "summaries_keep", "summaries_keep_days"],
      custom: "cache" },
    // 用户 m06193：缓存记录与 API 价目之间加一页「黑名单」。
    // 短语黑名单从聊天决策页的「通用设置」搬过来——它只影响触发判定，消息本身还在上下文里；
    // 用户黑名单是新的：按 QQ 号整条剔除，连请求都不进（窗口页照常显示，默认折叠）。
    { p: "blacklist", t: "短语黑名单", solo: 1,
      keys: ["phrase_filter_enabled", "phrase_filter_list"] },
    { p: "blacklist", t: "用户黑名单", custom: "ublack", solo: 1, keys: [],
      warn: "未经充分测试" },
    // v3.37（用户 m02080 第 4 条）：API 价目。价目是按模型名动态长的表，不是固定键，
    // 所以不进 schema、不占 keys，走 custom 整块渲染（和时段曲线一样）。
    { p: "pricing", t: "模型单价", custom: "pricing", solo: 1,
      // v4.85（用户 m10425 第 3 条）：把原来压在表格下面的那段说明并进这里，页面上不再重复。
      note: "单位是元 / 百万 token，照各家官网的价目填就行。只有填了单价的模型才算花费；" +
            "高峰价留空就按普通价算，普通价填 0 就是免费。" },
    // v3.91（用户 m05749 / m05856）：进阶模式做完了——开关、人格、提示词、模型、工具、预览
    // 全都放在「提示词」页（窗口设置右边那一页）。这几组必须写在数组最末：renderSettings 按
    // SET_GROUPS 顺序把「同页 + 同 split」的相邻组并进同一个 .split2 块，夹在聊天决策页中间
    // 会把那一页的三列切开（退避所在的那一列会单独成块）。
    // 退避组没开进阶时整组灰掉，refreshDimRows 认的是 #settings 里 advanced_enabled 那个开关，
    // 它在 DOM 里（只是显示在提示词页），所以挪页不影响灰化。
    // v3.92（用户 m06951 第 4 条）：这一组整组撤掉——开关搬去聊天决策页的「决策模式」那一行，
    // 提示词页不再放它。refreshDimRows 找 advanced_enabled 仍能找到（同一个 #settings 里，
    // 切页只是 display:none），退避组的灰化不受影响。
    // v3.92（用户 m06951 第 5~7 条）：这一页重排成「顶上一半一半两个子页签 + 左边设置右边预览」。
    // 四块原来的位置也变了：聊天规则搬去「回复提示词」（它只在开口那一轮发出去），
    // 预览改成右栏自动刷新、只留一个「测试」按钮。整页由一块 custom 接管，不再拆成四组。
    { p: "prompt", t: "提示词", custom: "promptpage", solo: 1, bare: 1 }
  ];
  // 设置页按页签分栏（用户 m33573：一页全堆着太乱，照 MaiBot 那样多分几页）
  var PAGES = [
    { id: "basic",   t: "基础设置" },
    { id: "trigger", t: "聊天决策" },
    // v3.53（用户本轮第 2 条）：私聊设置页删掉，私聊策略并进聊天决策页最后一块。
    // 页签由 renderTabs 按 data-page 自动扫出来，这里少一项就自动少一个按钮；
    // URL 上残留 page=private 时 renderTabs 的兜底会把 curPage 拨回第一个页签。
    { id: "window",  t: "窗口设置" },
    // v3.91（用户 m05749）：进阶决策的开关、人格、提示词、模型、工具、预览全在这一页。
    { id: "prompt",  t: "提示词" },
    // v3.56（用户本轮第 7 条）：「缓存设置」页改名「缓存记录」（这一页现在主要是各类缓存的账本 + 上限）；
    // 「其他」页签撤掉——它只是兜 schema 里没分组的键，一直是空的。
    { id: "log",     t: "缓存记录" },
    { id: "blacklist", t: "黑名单" },
    // 用户 m09143：这一页现在主要是统计（用量、花费、占比），页签名从「API 价目」改成「API 统计」。
    { id: "pricing", t: "API 统计" }
    // 用户 m08902 第 6 条：「会话管理」页其实早就不存在了——内容块随某次改动删掉，
    // 只剩这里一条定义和 hidden-panel 的显示判断。页签是按 DOM 里的块自动扫出来的，
    // 所以界面上从来没出现过它。这里把死条目一并删掉。
  ];
  var curPage = "basic";
  // 用户 m29560 第 2 条：刷新时留在原来那一页，只有重新进插件才回首页。
  // 用 sessionStorage + 导航类型判断：reload / 前进后退才恢复，正常进入一律回第一页。
  function rememberPage(){
    try{ sessionStorage.setItem("mdp_cur_page", curPage); }catch(e){}
  }
  var SET_SUB_KEY = "mdp_set_sub_text";
  function setSub(text){
    text = String(text || "");
    var el = document.getElementById("set-sub");
    if(!el || !text) return;
    el.textContent = text;
    try { sessionStorage.setItem(SET_SUB_KEY, text); } catch(e){}
  }
  var schemaCache = null, configCache = null;
  var boundsCache = {};   // 数字项的上下限，来自后端 main.py 的 _BOUNDS
  var dirty = false;
  var scopeSid = "";      // 空 = 编辑全局配置；非空 = 编辑这个会话的覆盖
  var scopeKeys = [];      // 允许单群覆盖的键（后端白名单）
  var overrideCache = {};  // 单群：本群真正写过的键，其余跟随全局
// v3.70（用户 m21533 第 2 条）：私聊的特例默认值（触发概率 100%），来自 /overrides 的 dm_defaults。
// 私聊作用范围下这一栏直接显示默认值、不显示「跟随全局」，但没手动改就不写进覆盖。
var dmDefaultsCache = {};
var DM_SPECIAL = { trigger_probability: 1 };
  var globalCache = {};    // 单群：全局值，用作输入框占位提示
  var aliasCache = {};
  var sessCache = [];
  // v3.37：API 价目（模型 → 每百万 token 的输入/输出单价）
  var pricingCache = [];      // 后端给的模型行：{model, in, out, input, output, cached, rounds}
  var pricingUnit = "元 / 百万 token";
  var pricingDraft = {};      // 还没保存的输入，重绘后要能留住
  var pricingHours = 24;      // 用户 m10749 第 3 条：价目统计区间（24 / 168 / 720 小时）
  var pricingCost = null;     // 用户 m10749 第 3 条：区间花费（后端统计失败时是 null）

  function sidLabel(sid){
    var alias = aliasCache[sid];
    if(alias) return alias + "（" + sid + "）";
    var parts = String(sid).split(":");
    var kind = parts.length >= 2 ? parts[parts.length-2] : "";
    var num = parts.length >= 2 ? parts[parts.length-1] : sid;
    return (kind === "gm" ? "群 " : "私聊 ") + num;
  }

  function esc(s){
    return String(s == null ? "" : s).replace(/[&<>"']/g, function(c){
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
    });
  }
  // 用户 m06007：组标题右边可以挂多枚提醒胶囊（双刃剑 / 未经充分测试），
  // 传字符串或数组都行，返回拼好的 HTML。
  function warnPills(w){
    if(!w) return "";
    var arr = Object.prototype.toString.call(w) === "[object Array]" ? w : [w];
    return arr.map(function(t){
      return "<span class='warnpill' title='" + esc(t) + "'>" + esc(t) + "</span>";
    }).join("");
  }
  function get(url){ return fetch(url, {credentials:"same-origin"}).then(function(r){ if(!r.ok) throw new Error("HTTP "+r.status); return r.json(); }); }
  function post(url){ return fetch(url, {method:"POST", credentials:"same-origin"}).then(function(r){ if(!r.ok) throw new Error("HTTP "+r.status); return r.json(); }); }

  // 用户 m01874：模型没配的红点。后端 /model-check 只报「真的会用到、现在又没有」的那几项，
  // 备用模型留空不算。三级：首页「设置」按钮 → 设置页签 → 具体那一行。
  var MODEL_MISSING = [];
  function modelMissing(){ return MODEL_MISSING; }
  function loadModelCheck(){
    var url = API + "/model-check" + (scopeSid ? "?sid=" + encodeURIComponent(scopeSid) : "");
    return get(url).then(function(d){
      MODEL_MISSING = (d && d.missing) || [];
      return MODEL_MISSING;
    }).catch(function(){
      MODEL_MISSING = [];
      return MODEL_MISSING;
    });
  }

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
      box.className = "dlg";
      box.innerHTML = "<h3>" + esc(opt.title || (opt.alert ? "提示" : "确认操作")) + "</h3>" +
        "<p class='p2'>" + esc(opt.message || "").replace(/\n/g, "<br>") + "</p>" +
        "<div class='dlg-btns'>" +
          (opt.alert ? "" : "<button data-act='cancel'>" + esc(opt.cancel || "取消") + "</button>") +
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
  // 用户 m01978 第 3 条：改名要能打字，原生 prompt 在 sandbox iframe 里同样被忽略，
  // 这里照 mdDialog 的样子做一个带输入框的版本，返回 Promise<string|null>。
  function mdPrompt(opt){
    opt = opt || {};
    return new Promise(function(resolve){
      var mask = document.createElement("div");
      mask.className = "mask";
      var box = document.createElement("div");
      box.className = "dlg";
      box.innerHTML = "<h3>" + esc(opt.title || "改个名字") + "</h3>" +
        (opt.message ? "<p class='p2'>" + esc(opt.message).replace(/\n/g, "<br>") + "</p>" : "") +
        "<input class='dlg-input' type='text' maxlength='" + (opt.maxlength || 24) + "' value='" + esc(opt.value || "") +
        "' placeholder='" + esc(opt.placeholder || "") + "'>" +
        "<div class='dlg-btns'><button data-act='cancel'>取消</button>" +
        "<button data-act='ok'>" + esc(opt.ok || "确定") + "</button></div>";
      mask.appendChild(box);
      document.body.appendChild(mask);
      var inp = box.querySelector(".dlg-input");
      var done = false;
      function close(v){
        if(done) return;
        done = true;
        document.removeEventListener("keydown", onkey, true);
        if(mask.parentNode) mask.parentNode.removeChild(mask);
        resolve(v);
      }
      function onkey(e){
        if(e.key === "Escape"){ e.preventDefault(); close(null); }
        else if(e.key === "Enter"){ e.preventDefault(); close(inp ? inp.value : ""); }
      }
      document.addEventListener("keydown", onkey, true);
      mask.addEventListener("click", function(e){
        if(e.target === mask){ close(null); return; }
        var b = e.target.closest ? e.target.closest("[data-act]") : null;
        if(!b) return;
        close(b.getAttribute("data-act") === "ok" ? (inp ? inp.value : "") : null);
      });
      if(inp){ inp.focus(); inp.select(); }
    });
  }

  // 首屏直出用的会话级缓存（v3.08）：刷新时先拿上次的 schema+配置把表单画出来，
  // 再等接口覆盖。接口慢的时候不用盯着「读取配置中…」空屏。
  function cachePut(k, v){ try{ sessionStorage.setItem("mdp_s_" + k, JSON.stringify(v)); }catch(e){} }
  function cacheGet(k){ try{ var s = sessionStorage.getItem("mdp_s_" + k); return s ? JSON.parse(s) : null; }catch(e){ return null; } }
  function releaseBoot(){
    if(window.__releaseBoot) requestAnimationFrame(function(){ requestAnimationFrame(window.__releaseBoot); });
  }

  // 分钟数 → HH:MM（半小时一档的下拉用；slotTime 收的是 48 格索引，别混用）
  function minTime(mi){
    var h = Math.floor(mi / 60), m = mi % 60;
    return (h < 10 ? "0" : "") + h + ":" + (m ? "30" : "00");
  }
  // v3.37（用户本轮）：半小时一档的时间下拉；单群模式下第一项是「跟随全局」。
  // 继承时不再同时选中实际时间项（老代码两个 selected，浏览器取后者，看着像没跟随全局）。
  function timeOptions(v, inherit, gv){
    // v3.68（用户 m20632 第 5 条）：同样只写「跟随全局」，不把全局值拼进选项文字
    var h = inherit ? "<option value='' selected>跟随全局</option>" : "";
    for(var mi = 0; mi < 1440; mi += 30){
      var tv = minTime(mi);
      h += "<option value='" + tv + "'" + (!inherit && String(v || "") === tv ? " selected" : "") + ">" + tv + "</option>";
    }
    return h;
  }
  // v3.37（用户本轮）：每项边上标默认值——改过之后不用翻文档也知道出厂是多少。
  // 值取自后端 schema 的 default（/config 会下发）；开关显示开/关，清单显示条目。
  // v3.37：枚举默认值是英文键，界面上要显示成中文，否则一行里冒出 "默认 count" 很突兀
  var ENUM_TEXT = { count: "计数", probability: "概率", adaptive: "自适应",
                    mechanical: "机械截断", llm: "模型摘要" };
  // v3.63（用户 m19536 第 7 条）：下拉里的英文选项换成中文——值是英文原样提交（契约不动），
  // 只换给人看的文字。按「键 → 值 → 中文」写，没写的键/值仍原样显示。
  var OPTION_TEXT = {
    trim_strategy: { mechanical: "机械裁切" },
    forward_mode: { off: "不展开", text: "只给文字", on_demand: "按需查看" }
  };
  // v3.42（用户 m03616 第 5 条）：这几个键的默认值要念成百分比——
  // 「默认 20」在一堆「条 / 秒 / 分钟」里会看串，标成「默认 20%」才不用猜。
  // v3.71（用户第 5 条）：adaptive_target_percent 随「动态调整」那一组一起下线——目标回复率
  // 不再手填一个全局值，改成每段单独设（见曲线卡片的时段面板）。
  var PCT_KEYS = { trigger_probability: 1, trigger_probability_dm: 1, rest_probability: 1,
                   // v3.72（用户第 2 条）：曲线模式自己的休息概率也是百分点
                   band_rest_probability: 1,
                   // v3.81（用户 m02645 图2）：旧的「加多少概率」两项已退役，节奏松紧是无单位刻度
                    };
  // 用户 m10202：「切换模型」在界面上按分钟填，存盘仍是秒（后端与老配置都按秒）。
  // 值 = 一秒换多少分钟，60 表示「界面数字 × 60 = 存盘秒数」。
  var MIN_KEYS = { decision_fallback_cooldown: 60, reply_fallback_cooldown: 60, jev_fallback_cooldown: 60 };
  // 用户 m01432：唤醒那几行的默认值按三档的说法写——「默认 触发 / 默认 关闭」。
  // 用户 m03761：其余开关的默认值角标也统一成「默认 开启 / 默认 关闭」，不再写「开 / 关」。
  var WAKE_DEF_TEXT = { mention_trigger: 1, nickname_wake: 1, name_wake: 1, rest_wake_trigger: 1 };
  function defaultText(f, k){
    var d = f ? f.default : undefined;
    var t = (f && f.type) || "string";
    if(t === "switch" || t === "boolean" || t === "bool"){
      var wd = WAKE_DEF_TEXT[String(k || "")];
      return d ? (wd ? "触发" : "开启") : "关闭";
    }
    if(d === null || d === undefined || d === "") return "无";
    if(Array.isArray(d)) return d.length ? d.join(" / ") : "无";
    var s = String(d);
    // 用户 m10202：按分钟显示的键，默认值角标也跟着换算（存的是秒）。
    var mk = MIN_KEYS[String(k || "")];
    if(mk && isFinite(Number(d))) s = String(Math.round(Number(d) / mk));
    // 用户 2026-11-07：枚举角标先查下拉选项的中文名（如 forward_mode 的 on_demand → 按需查看），
    // 再退到老的那张枚举表，最后原样显示。
    if(t === "enum" || t === "select") s = (OPTION_TEXT[String(k || "")] || {})[s] || ENUM_TEXT[s] || s;
    if(PCT_KEYS[String(k || "")]) s = s + "%";
    return s.length > 12 ? s.slice(0, 12) + "…" : s;
  }
  // v3.41（用户本轮第 5 条）：默认值是空的就不标角标——bot_name / nicknames 这种「留空取
  // 适配器昵称」的键，标成「默认 无」只会让人以为自己漏填了什么。
  function defaultBadge(f, k){
    if(!f) return "";
    // v3.70（用户 m21533 第 2 条）：私聊 scope 里带「私聊默认」的键，角标写私聊的默认值——
    // 框里显示 100 时别拿全局默认（20%）去标，两处对不上。
    if(scopeSid && DM_SPECIAL[k] && dmDefaultsCache[k] != null && cacheSidIsDM(scopeSid)){
      var dt = String(dmDefaultsCache[k]);
      if(PCT_KEYS[String(k || "")]) dt = dt + "%";
      return "<i class='dfv' title='私聊默认值'>私聊默认 " + esc(dt) + "</i>";
    }
    var t = defaultText(f, k);
    if(t === "无" || t === "-") return "";
    return "<i class='dfv'>默认 " + esc(t) + "</i>";
  }
  // v3.50（用户 m06761 第 4 条）：开关上的小字跟着状态显示「开 / 关」——用户原话「开的时候
  // 显示开启，关的时候显示关闭」。两个 <i> 一起写进标签，露哪个由 CSS 的 :checked 决定（见 .sw2 .st），
  // 不靠重渲染也不靠 JS 改字，重绘后不会掉。on/off 两个参数留给「触发」这类别的叫法。
  function stHtml(on, off){
    return "<span class='st'><i class='on'>" + esc(on) + "</i><i class='off'>" + esc(off) + "</i></span>";
  }
  var ST_SW = stHtml("开启", "关闭");
  // 用户 m02469：改名用首页那把铅笔（web/index.html 的 ICON_PENCIL 同款）。
  var ICON_PENCIL = "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M12 20h9'/><path d='M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z'/></svg>";
  // v3.92（用户 m00697 第 10 条）：开关统一成「字写在轨道里」的胶囊——轨道里直接写状态，
  // 白色滑块滑到对应一侧。onTxt / offTxt 是轨道里那两个字，默认「开启 / 关闭」；
  // 主要开关写「接管 / 关闭」，其余写「开启 / 关闭」。真实 input 仍在标签里，collectSettings 照旧读 checked。
  function swPill(k, on, onTxt, offTxt, title, cls){
    return "<label class='sw2" + (cls ? " " + cls : "") + "'" + (title ? " title='" + esc(title) + "'" : "") + ">" +
      "<input type='checkbox' data-k='" + esc(k) + "'" + (on ? " checked" : "") + ">" +
      "<span class='tr' data-on='" + esc(onTxt || "开启") + "' data-off='" + esc(offTxt || "关闭") + "'></span></label>";
  }
  // v3.92（用户 m00697 第 15 条）：唤醒那几行是「关闭 / 触发 / 必回」三档，一条轨道三段、滑块跟着走。
  // 两个键（触发 / 必回）互斥，仍各留一个隐藏 input 带 data-k，提交链路不用改。
  function sw3(k, ak, v, title){
    var cur = (v === "always") ? "always" : (v === "trigger" ? "trigger" : "off");
    // 用户 m01445：没开决策模式时「必回」不生效，整段灰掉、点不动。
    var mustOk = !!(configCache && configCache["advanced_enabled"]);
    return "<span class='sw3" + (mustOk ? "" : " no-must") + "' data-v='" + cur + "' data-3k='" + esc(k) + "' data-3a='" + esc(ak) + "'" +
      (title ? " title='" + esc(title) + "'" : "") + ">" +
      "<input type='checkbox' data-k='" + esc(k) + "'" + (cur === "trigger" ? " checked" : "") + ">" +
      "<input type='checkbox' data-k='" + esc(ak) + "'" + (cur === "always" ? " checked" : "") + ">" +
      "<i class='thumb'></i>" +
      "<b data-v='off'>关闭</b><b data-v='trigger'>触发</b><b data-v='always'>必回</b></span>";
  }
  // 框内单位：计数类（条数）是「次」，成对参数按数值键推（秒 / 分钟，见 pairUnitOf）。
  // v3.92（用户 m00697 第 8 / 9 条）：重试那三个框、窗口显示那三行也带单位，单位写在框里。
  var UNIT_TEXT = { trigger_threshold: "次",
    watchdog_seconds: "秒", retry_max: "次", retry_delay: "秒",
    trace_keep: "条", trace_keep_dm: "条", decision_keep: "条", decision_keep_dm: "条",
    window_show: "条", window_show_dm: "条" };
  // v3.92（用户 m00697 第 11 条）：主要开关轨道里写「接管 / 关闭」，其余写「开启 / 关闭」。
  var SW_TEXT = { decision_enabled: ["接管", "关闭"] };
  // v3.72（用户第 7 / 9 条）：第 5 个参数是调用方给的 [下限, 上限]，用来兜 schema 里没写
  // min/max、后端 /config 也没下发 bounds 的键（数据使用 / 回看周数）。
  function fieldControl(k, f, v, unit, minmax){
    var t = f.type || "string";
    var swTx = SW_TEXT[k] || ["开启", "关闭"];
    if(t === "switch" || t === "boolean" || t === "bool"){
      // v3.49（用户 m06348 第 7 条）：开关上的小字先统一成「开关」；v3.50（用户 m06761 第 4 条）
      // 又改回随状态显示「开 / 关」——名字仍在左边 .nm 里，小字只报状态。
      return swPill(k, v, swTx[0], swTx[1]);
    }
    // 单群模式：只显示本群写过的值，没写过就空着 + 占位提示全局值，
    // 这样「留空 = 跟随全局」在界面上看得见，不用去记白名单。
    // v3.70（用户 m21533 第 2 条）：私聊的「触发概率」是特例——默认 100%，不跟随全局，
    // 所以这一栏当成本群自己的值显示（而不是空着写「跟随全局」）；没手动改就不提交覆盖。
    var dmv = (scopeSid && DM_SPECIAL[k] && dmDefaultsCache[k] != null && cacheSidIsDM(scopeSid))
      ? dmDefaultsCache[k] : null;
    var inherit = !!scopeSid && dmv === null && !Object.prototype.hasOwnProperty.call(overrideCache, k);
    var dmAttr = (dmv === null) ? "" : " data-dmdef='1'";
    var gv = globalCache[k];
    // v3.68（用户 m20632 第 5 条）：占位只写「跟随全局」，不再把全局值拼在后面（「跟随全局：3」看着像已设了 3）
    var ph = inherit ? " placeholder='跟随全局'" : "";
    if(t === "integer" || t === "float" || t === "number"){
      // 上下限跟后端 _BOUNDS 对齐：不然填个 0 或负数，后端 _coerce 悄悄夹回去，
      // 用户还以为自己设生效了（审查 bug）
      var bd = minmax || boundsCache[k] || [];
      var lim = (bd[0] == null ? "" : " min='" + esc(bd[0]) + "'") + (bd[1] == null ? "" : " max='" + esc(bd[1]) + "'");
      var stp = (t === "integer") ? " step='1'" : " step='any'";
      // v3.42（用户 m03616 第 5 条）：百分比键要跟一个 %，单位贴在控件上。
      // v3.43（用户本轮第 3 条 / 图 8）：% 不再挂在框外（框外那个 % 会跟 gap 一起飘开，
      // 而且框宽不统一时一列看下来对不齐），改成包一层 .ipt：框内右侧留 21px，% 绝对定位在里面。
      // v3.50（用户 m06761 第 4 条）：单位（次 / 秒 / 分钟）跟 % 一样进框——原来单位挂在框外，
      // 窄列里会被折到下一行、还跟着 gap 飘。这里只加一个 has-unit 变体类，单位仍是 <b>。
      var u = unit ? String(unit) : "";
      return "<span class='ipt" + (PCT_KEYS[k] ? " has-pct" : "") + (u ? (" has-unit" + (u.length > 1 ? " uw2" : "")) : "") + "'>" +
        "<input type='number' data-k='" + esc(k) + "' value='" + esc(inherit ? "" : (v == null ? (dmv == null ? "" : dmv) : v)) + "'" + ph + lim + stp + dmAttr + ">" +
        (PCT_KEYS[k] ? "<b class='pct'>%</b>" : "") +
        (u ? "<b class='unit'>" + esc(u) + "</b>" : "") + "</span>";
    }
    // v3.33（用户 m51046 第 7 条）：休息开始/结束按半小时一档选，不用手打 23:30。
    // 框架的 /config 只下发 name/type/default/hint（自定义标记会被丢掉），
    // 所以这两个键直接按名字判，不依赖 schema 里的 time30 标记。
    // v3.72：曲线休息时段（band_rest_start / band_rest_end）也走半小时一档下拉——
    // /config 会把 schema 里的 time30 标记丢掉，所以同样按名字兜底识别。
    if(f.time30 || k === "rest_start" || k === "rest_end" || k === "band_rest_start" || k === "band_rest_end"){
      return "<select data-k='" + esc(k) + "'>" + timeOptions(v, inherit, gv) + "</select>";
    }
    if(t === "enum"){
      var opts = f.options || f.enum || [];
      var otx = OPTION_TEXT[k] || {};
      return "<select data-k='" + esc(k) + "'>" + opts.map(function(o){
        var lbl = (otx[o] != null) ? otx[o] : o;
        return "<option value='" + esc(o) + "'" + (String(o) === String(v) ? " selected" : "") + ">" + esc(lbl) + "</option>";
      }).join("") + "</select>";
    }
    if(t === "multi_select" || t === "list" || t === "array"){
      var arr = Array.isArray(v) ? v : [];
      // v3.14（用户 m40145）：外号不常驻输入框，改成列表末尾一个「+」，点开才出现输入框。
      // collectSettings 只扫 .chip，这个按钮和隐藏输入框不会被当成数据。
      return "<div class='chips' data-k='" + esc(k) + "' data-list='1'>" +
        arr.map(function(x){ return "<span class='chip'>" + esc(x) + "<b data-del='1'>×</b></span>"; }).join("") +
        "<button type='button' class='chip-add' data-add='1' title='添加'>+</button>" +
        "<input class='chip-in' placeholder='输入后回车添加' hidden></div>";
    }
    // 用户 m00697 第 1 条：机器人昵称的框跟着内容走、文字右对齐（跟左边名字一样宽）。
    if(k === "bot_name"){
      var bs = String(inherit ? "" : (v == null ? "" : v));
      var bw = 0;
      for(var bi = 0; bi < bs.length; bi++) bw += (bs.charCodeAt(bi) > 255 ? 14 : 7.6);
      bw = Math.max(72, Math.min(200, Math.round(bw) + 26));
      return "<input type='text' data-k='bot_name' value='" + esc(bs) + "'" + ph + " style='width:" + bw + "px;text-align:right'>";
    }
    return "<input type='text' data-k='" + esc(k) + "' value='" + esc(inherit ? "" : (v == null ? "" : v)) + "'" + ph + ">";
  }

  // v3.49（用户 m06348 第 7 条）：设置页想显示成什么名字就在这一层改——schema 里的 name 归后端
  // 维护（键名、提交契约都不动），页面只把叫法换成用户要的：休息开关 → 休息时段、抖动 → 消息防抖。
  var NAME_TEXT = { rest_mode_enabled: "休息时段", jitter_enabled: "消息防抖" };
  function nameOf(k, f){
    return (NAME_TEXT[k] != null) ? NAME_TEXT[k] : ((f && f.name) || k);
  }

  // 一行设置：名称 + 说明 + 控件排在同一行，说明在名称右侧（超长省略号，全文在 title 里）。
  // v3.14：inline 行先把说明挪到名称右边。v3.41（用户本轮第 7 条）：非分栏的行原来还留着
  // 「说明压到名称下面第二行」（.hh）那一套，跟分栏里的行两种样子；现在合成一条路径，
  // 说明一律跟着名称走。列表类（外号 / 短语）控件仍占第二行整宽，否则胶囊没地方放。
  // ---- 进阶决策 · 提示词页（v3.91，用户 m05749 / m05834 / m05856 / m05902）----------
  // 这一页的控件全部走普通表单：每个可写的都带 data-k，collectSettings() 照 schema 的键
  // 在 #settings 里找它，点保存（或自动保存）就一起 PUT 上去，不用另开保存接口。
  // 人格 / 工具 / 模型三份清单是 /decision-options 给的，DOPT 缓存一次。
  var DOPT = null;

  function dVal(k, dft){
    var v = (configCache ? configCache[k] : null);
    if(v === null || v === undefined || v === "") return (dft === undefined ? "" : dft);
    return v;
  }

  function dPickedNow(){
    var box = document.getElementById("dec-tools-box");
    if(box){
      var arr = [];
      Array.prototype.forEach.call(box.querySelectorAll("input.dtchk"), function(c){
        if(c.checked) arr.push(c.value);
      });
      return arr;
    }
    return dPickedRaw();
  }

  function dPickedRaw(){
    var raw = dVal("decision_tools", "");
    if(Object.prototype.toString.call(raw) === "[object Array]") return raw.map(String);
    var s = String(raw || "").trim();
    if(!s) return [];
    try{
      var arr = JSON.parse(s);
      return (Object.prototype.toString.call(arr) === "[object Array]") ? arr.map(String) : [];
    }catch(e){
      return s.split(",").map(function(x){ return x.trim(); }).filter(function(x){ return !!x; });
    }
  }

  function dPersonaOpts(cur){
    var h = "<option value=''" + (cur ? "" : " selected") + ">不用人格</option>";
    var list = (DOPT && DOPT.personas) || [];
    var seen = false;
    list.forEach(function(p){
      var nm = String((p && (p.name || p.persona_name)) || "");
      if(!nm) return;
      if(nm === cur) seen = true;
      h += "<option value='" + esc(nm) + "'" + (nm === cur ? " selected" : "") + ">" + esc(nm) + "</option>";
    });
    if(cur && !seen) h += "<option value='" + esc(cur) + "' selected>" + esc(cur) + "（框架里已经没有了）</option>";
    return h;
  }

  function dModelOpts(cur, emptyText){
    var h = "<option value=''" + (cur ? "" : " selected") + ">" + esc(emptyText) + "</option>";
    var list = (DOPT && DOPT.models) || [];
    var seen = false;
    list.forEach(function(m){
      var v = String((m && m.value) || "");
      if(!v) return;
      var lb = String(m.label || v);
      if(m.fast) lb += "（框架快速模型）";
      else if(m.main) lb += "（框架默认模型）";
      if(v === cur) seen = true;
      h += "<option value='" + esc(v) + "'" + (v === cur ? " selected" : "") + ">" + esc(lb) + "</option>";
    });
    if(cur && !seen) h += "<option value='" + esc(cur) + "' selected>" + esc(cur) + "（已不在框架配置里）</option>";
    return h;
  }

  function dToolsInner(){
    var list = (DOPT && DOPT.tools) || [];
    if(!list.length){
      return "<div class='dempty'>还没拿到工具清单。插件跑过一轮真实请求之后就有了；现在勾了也只会写进提示词。</div>";
    }
    var picked = dPickedNow();
    var h = "";
    list.forEach(function(t){
      var nm = String((t && t.name) || "");
      if(!nm) return;
      var ds = String((t && t.description) || "").split("\n")[0];
      if(ds.length > 96) ds = ds.slice(0, 96) + "…";
      h += "<label class='dtool'><input type='checkbox' class='dtchk' value='" + esc(nm) + "'" +
           (picked.indexOf(nm) >= 0 ? " checked" : "") + ">" +
           "<span class='dtn'><b>" + esc(nm) + "</b>" + (ds ? "<i>" + esc(ds) + "</i>" : "") + "</span></label>";
    });
    return h;
  }

  // 用户 m13442 第 4 条：新加的键在用户还没存过之前 configCache 里没有，
  // 这时按 schema 的默认值显示，别让输入框空着（空着在单群模式下等于「跟随全局」）。
  // 用户 m09611：单位搬进框里（跟 JEV 卡同一套 .ipt.has-unit），不再挂在框右边。
  function dnum(k, unit, dft){
    var v = dVal(k, dft === undefined ? "" : dft);
    var u = unit ? String(unit) : "";
    // 用户 m10202：按分钟填的键，显示时把存盘的秒换算成分钟，提交时再乘回去。
    var mul = MIN_KEYS[String(k || "")] || 0;
    if(mul && v !== "" && v !== null && v !== undefined && isFinite(Number(v))) v = Math.round(Number(v) / mul);
    return "<span class='ipt" + (u ? (" has-unit" + (u.length > 1 ? " uw2" : "") + (u.length >= 4 ? " uw3" : "")) : "") + "'><input class='dipt num' type='number' data-k='" +
      esc(k) + "'" + (mul ? " data-mul='" + mul + "'" : "") + " value='" + esc(v) + "'>" + (u ? "<b class='unit'>" + esc(u) + "</b>" : "") + "</span>";
  }
  // 用户 m10202：两行并一行——每格「短标签 + 控件」，跟 JEV 卡同一套 .drow.pair2。
  function dPair(l1, c1, l2, c2){
    function cell(l, c){
      return "<span class='p2'><label class='dlb'>" + esc(l) + "</label>" + c + "</span>";
    }
    return "<div class='drow pair2'>" + cell(l1, c1) + cell(l2, c2) + "</div>";
  }

  // 用户 m16307 图2：不确定段从「一行两个下拉」改成一根长条——每 5% 一格刻度，
  // 25%~75% 那段高亮，中间显示「25%~75%」。两个 hidden input 保留 data-k，
  // 保存链路（collectSettings / 上下限夹紧 / 700ms 防抖存盘）跟原来的下拉完全一样。
  // 存着的值不是 5 的倍数（老配置手填过 43）也照原位画，拖一下才吸附到 5 的倍数。
  function dRange(klo, khi, dlo, dhi){
    var lo = parseInt(dVal(klo, dlo), 10); if(isNaN(lo)) lo = dlo;
    var hi = parseInt(dVal(khi, dhi), 10); if(isNaN(hi)) hi = dhi;
    lo = Math.max(0, Math.min(100, lo));
    hi = Math.max(0, Math.min(100, hi));
    if(lo > hi){ var t = lo; lo = hi; hi = t; }
    return "<div class='jev-slider' data-lo='" + lo + "' data-hi='" + hi + "'>" +
      "<div class='jev-slider-track'>" +
        "<div class='jev-slider-clip'>" +
          "<div class='jev-slider-fill' style='left:" + lo + "%;right:" + (100 - hi) + "%'></div>" +
          "<div class='jev-slider-ticks'></div>" +
        "</div>" +
        "<button type='button' class='jev-slider-knob' data-h='lo' style='left:" + lo + "%' aria-label='不确定段下限'></button>" +
        "<button type='button' class='jev-slider-knob' data-h='hi' style='left:" + hi + "%' aria-label='不确定段上限'></button>" +
        "<span class='jev-slider-tag' style='left:" + Math.min(92, Math.max(8, (lo + hi) / 2)) + "%'>" + lo + "%~" + hi + "%</span>" +
      "</div>" +
      "<input type='hidden' data-k='" + esc(klo) + "' value='" + lo + "'>" +
      "<input type='hidden' data-k='" + esc(khi) + "' value='" + hi + "'>" +
    "</div>";
  }

  function dmodelBlockHtml(){
    var act = String(dVal("decision_fail_action", "allow")) === "block" ? "block" : "allow";
    // 用户 m08153：这一页的「!」图标和悬浮说明全删了；hint 参数留着不渲染，
    // 免得下面十几处调用点全改一遍。
    function row(label, ctl, hint){
      return "<div class='drow'><label class='dlb'>" + esc(label) + "</label>" + ctl + "</div>";
    }
    // 用户 m02993 图7：删掉「决策模型」这条小标题；备用模型挪到决策模型右边同一行；
    // 失败时的选项说人话（跳过决策、直接回复）；下面整块「决策可用的工具」也删了
    // ——工具已经改成提示词页的「工具说明」模块，在那里勾一次，两边都用同一份。
    return "<div class='dcard'>" +
      "<input type='hidden' data-k='decision_tools' id='dec-tools' value='" +
        esc(JSON.stringify(dPickedRaw())) + "'>" +
      // 用户 m04343 第 8 条：两个模型选择框各自贴在标题右边，标题和下面六行同一列左对齐。
      // 用户 m09611：跟 JEV 卡一样套成两列小卡——左边模型与失败处理，右边工具那几项。
      "<div class='jev-cols'>" +
        "<div class='jev-col dcol'><div class='drows'>" +
          row("决策模型", "<select class='dipt' data-k='decision_model' id='dec-model'>" +
            dModelOpts(String(dVal("decision_model", "")), "跟随默认模型") + "</select>",
            "它只负责看窗口判断回不回；真正的回复还是框架默认模型来写。") +
          row("备用模型", "<select class='dipt' data-k='decision_fallback_model' id='dec-fb'>" +
            dModelOpts(String(dVal("decision_fallback_model", "")), "未选择模型") + "</select>",
            "主模型连续失败到「失败切换」那个次数，才切到它。") +
          // 用户 m08678：决策页保留自己的一套，默认值与回复一致，可各自调整。
          // 用户 m10202：这两行并成一行；等待时间按分钟填（存盘仍是秒）。
          // 用户 m15201：「连续失败切换」改名「失败切换」，「单轮工具调用上限」改名「单轮工具上限」。
          dPair("失败切换", swPill("decision_fail_switch", dVal("decision_fail_switch", true)),
            "切换模型", dnum("decision_fallback_cooldown", "分钟", 1800)) +
          row("决策失败时", "<select class='dipt' data-k='decision_fail_action' id='dec-fail'>" +
            "<option value='allow'" + (act === "allow" ? " selected" : "") + ">跳过决策调用回复</option>" +
            "<option value='block'" + (act === "block" ? " selected" : "") + ">这一轮不回</option></select>",
            "决策模型挂了的时候：跳过判断、直接交给回复模型，还是这一轮干脆不回。") +
          // 用户 m09851：「决策看图」搬到窗口设置页的「图片」组了，这里不再重复一份。
        "</div></div>" +
        "<div class='jev-col dcol'><div class='drows'>" +
          row("Agent循环次数", dnum("decision_tool_loop", "轮", 5), "最多调用几轮工具，最后判断那一轮不算。") +
          row("单轮工具上限", dnum("decision_tool_calls_per_turn", "个", 5), "一轮里最多调几个工具，多出来的不执行。") +
          row("工具调用超时", dnum("decision_tool_timeout", "秒", 180), "单个工具最多等多久，0 = 不限制。") +
          row("工具输出上限", dnum("decision_tool_output_tokens", "token", 10000), "工具结果最多留多少，超了保留首尾，0 = 不限制。") +
        "</div></div>" +
      "</div></div>";
  }

  // 用户 m13442 第 4 条：框架「代理设置」里那四项改由插件接管，回复这边单独一张卡，
  // 跟决策那张各存各的——决策调工具和回复调工具本来就不是一回事。
  // 用户 m06995 第 2 项：回复这条链也补齐模型与看图，跟决策卡一一对应，
  // 只少「决策失败时」——回复没有「跳过决策」这回事。
  function replyToolBlockHtml(){
    // 用户 m08153：这一页的「!」图标和悬浮说明全删了；hint 参数留着不渲染。
    function row(label, ctl, hint){
      return "<div class='drow'><label class='dlb'>" + esc(label) + "</label>" + ctl + "</div>";
    }
    return "<div class='dcard'>" +
      // 用户 m09611：跟 JEV 卡一样套成两列小卡——左边模型与看图，右边工具那几项。
      "<div class='jev-cols'>" +
        "<div class='jev-col dcol'><div class='drows'>" +
          row("回复模型", "<select class='dipt' data-k='reply_model' id='rep-model'>" +
            dModelOpts(String(dVal("reply_model", "")), "跟随默认模型") + "</select>",
            "真正写回复的那个模型。留空 = 用框架的默认模型。") +
          row("备用模型", "<select class='dipt' data-k='reply_fallback_model' id='rep-fb'>" +
            dModelOpts(String(dVal("reply_fallback_model", "")), "未选择模型") + "</select>",
            "主模型连续失败到「失败切换」那个次数，才切到它。") +
          // 用户 m07859：重试 / 重试间隔 / 单次超时统一在「基础设置 · 重试」里改，这里不再重复。
          // 用户 m10202：这两行并成一行；等待时间按分钟填（存盘仍是秒）。
          // 用户 m15201：跟决策卡一样，「连续失败切换」改名「失败切换」，「单轮工具调用上限」改名「单轮工具上限」。
          dPair("失败切换", swPill("reply_fail_switch", dVal("reply_fail_switch", true)),
            "切换模型", dnum("reply_fallback_cooldown", "分钟", 1800)) +
          // 用户 m09851：「回复看图」搬到窗口设置页的「图片」组了，这里不再重复一份。
        "</div></div>" +
        "<div class='jev-col dcol'><div class='drows'>" +
          row("Agent循环次数", dnum("reply_tool_rounds", "轮", 5), "回复时最多调用几轮工具，最后写回复那一轮不算。") +
          row("单轮工具上限", dnum("reply_tool_calls_per_turn", "个", 5), "一轮里最多调几个工具，多出来的不执行。") +
          row("工具调用超时", dnum("reply_tool_timeout", "秒", 180), "单个工具最多等多久，0 = 不限制。") +
          row("工具输出上限", dnum("reply_tool_output_tokens", "token", 10000), "工具结果最多留多少，超了保留首尾，0 = 不限制。") +
        "</div></div>" +
      "</div></div>";
  }

  // ---- JEV 决策（用户 m06706）------------------------------------------
  // 开关在「设置 · 进阶模式」那一行（跟「决策模式」互斥）；这一页只放模型与不确定区间。
  // 用户 m09981：模型下拉同时列 KiraAI 的 decision 类模型与下面每一页自定义接口——
  // 宿主是 provider:model，自定义是 custom:<页号>，选哪条就走哪条，不再二选一。
  var JEVOPT = null;
  var JEV_MODELS = [];
  var JEV_MODEL_ERR = "";
  var JEV_APIS = [];
  var JEV_PAGE = "1";

  // 用户 m09981：自定义接口可以有多页，每页一套协议 / 地址 / 模型名，密钥仍单独存。
  // 页面改的是 configCache 里的 jev_apis（JSON 字符串），后端按它拼请求。
  // 用户 m01978 第 3 条：每个自定义接口页有自己的名字（默认「填入API」，用户 m02950 改名），
  // 这一行的标签、模型下拉、页签悬浮都用它，不再写「接口 1」。
  function jevPageName(pg){
    var nm = String((pg && pg.name) || "").trim();
    if(nm) return nm;
    var id = String((pg && pg.id) || "1");
    return id === "1" ? "填入API" : ("填入API " + id);
  }
  function jevPages(){
    if(!JEV_APIS.length){
      var cfg = (JEVOPT && JEVOPT.config) || {};
      JEV_APIS = [{
        id: "1",
        name: "",
        protocol: String(cfg.api_protocol || dVal("jev_api_protocol", "") || "systemone"),
        base: String(cfg.api_base || dVal("jev_api_base", "") || ""),
        model: String(cfg.api_model || dVal("jev_api_model", "") || ""),
        api_key_set: !!cfg.api_key_set,
        api_key_masked: String(cfg.api_key_masked || "")
      }];
      JEV_PAGE = "1";
    }
    return JEV_APIS;
  }
  function jevPage(id){
    var pages = jevPages();
    var want = String(id || JEV_PAGE || "1");
    for(var i = 0; i < pages.length; i++){
      if(String(pages[i].id) === want) return pages[i];
    }
    return pages[0];
  }
  function jevPageProtocol(pg){
    return String((pg && pg.protocol) || "systemone").toLowerCase() === "openai" ? "openai" : "systemone";
  }
  function jevNextPageId(){
    var top = 0;
    jevPages().forEach(function(pg){
      var n = parseInt(String(pg.id || ""), 10);
      if(isFinite(n) && n > top) top = n;
    });
    return String(top + 1);
  }
  function jevPickPage(id){
    JEV_PAGE = String(id || "1");
    JEV_MODELS = [];
    JEV_MODEL_ERR = "";
    jevRerenderCard();
  }
  function jevModelOpts(cur, emptyText){
    var h = "<option value=''" + (cur ? "" : " selected") + ">" + esc(emptyText) + "</option>";
    var list = (JEVOPT && JEVOPT.models) || [];
    var seen = false;
    list.forEach(function(m){
      var v = String((m && m.value) || "");
      if(!v) return;
      if(v === cur) seen = true;
      h += "<option value='" + esc(v) + "'" + (v === cur ? " selected" : "") + ">" +
        esc(String(m.label || v)) + "</option>";
    });
    jevPages().forEach(function(pg){
      var v = "custom:" + String(pg.id || "1");
      if(v === cur) seen = true;
      h += "<option value='" + esc(v) + "'" + (v === cur ? " selected" : "") + ">" +
        esc(jevPageName(pg)) + " · " + esc(String(pg.model || "").trim() || "还没填模型") + "</option>";
    });
    if(cur && !seen) h += "<option value='" + esc(cur) + "' selected>" + esc(cur) + "（已不在清单里）</option>";
    return h;
  }

  function jevEmptyText(){
    if(JEVOPT && JEVOPT.status && JEVOPT.status.available === false && !(JEVOPT.models || []).length){
      return "当前宿主不支持 decision 模型";
    }
    return "请选择模型";
  }

  // 用户 m08755：接口地址只填到 api 前缀，真正要打的那条由插件补路径。
  // 下面这个跟后端的 api_endpoint / api_chat_url 同一套规则，只用来「边打字边看」。
  function jevEndpointOf(raw, proto){
    var text = String(raw || "").trim().replace(/\/+$/, "");
    if(!text) return "";
    var low = text.toLowerCase();
    if(String(proto || "systemone").toLowerCase() === "openai"){
      if(low.slice(-17) === "/chat/completions") return text;
      if(low.slice(-13) === "/v1/systemone") return text.slice(0, -10) + "/chat/completions";
      if(low.slice(-3) === "/v1") return text + "/chat/completions";
      return text + "/v1/chat/completions";
    }
    if(low.slice(-13) === "/v1/systemone") return text;
    if(low.slice(-3) === "/v1") return text + "/systemone";
    return text + "/v1/systemone";
  }
  function jevPaintEndpoint(){
    var node = document.getElementById("jev-endpoint");
    if(!node) return;
    var base = document.getElementById("jev-base");
    var proto = document.getElementById("jev-proto");
    var ep = jevEndpointOf(base ? base.value : "", proto ? proto.value : "systemone");
    node.textContent = ep ? ("→ " + ep) : "→ 填了接口地址就显示真正要打的地址";
  }
  function jevBlockHtml(){
    var act = String(dVal("jev_action", "no_reply"));
    if(act !== "reply" && act !== "decision") act = "no_reply";
    var pages = jevPages();
    var pg = jevPage();
    var proto = jevPageProtocol(pg);
    // 用户 m01978 第 5 条：没写说明的行就别挂那个 i 图标，也别留空 title。
    // 用户 m02469：这一卡里的悬浮说明全删了，i 图标也一起去掉；hint 参数留着不渲染，
    // 免得下面十几处调用点全改一遍。
    function row(label, ctl, hint){
      return "<div class='drow'><label class='dlb'>" + esc(label) + "</label>" + ctl + "</div>";
    }
    // 用户 m01978 第 1/2 条：两项并一行——每格「短标签 + 控件」，控件自己撑开。
    function pair2(l1, c1, l2, c2, h1, h2){
      function cell(l, c){
        return "<span class='p2'><label class='dlb'>" + esc(l) + "</label>" + c + "</span>";
      }
      return "<div class='drow pair2'>" + cell(l1, c1) + cell(l2, c2) + "</div>";
    }
    // 用户 m01978 第 1 条：单位（次 / 秒）搬进框里，跟基础设置页 .ipt.has-unit 一套做法。
    function dnumIn(k, unit, dft){
      var v = dVal(k, dft === undefined ? "" : dft);
      var u = unit ? String(unit) : "";
      // 用户 m10202：按分钟填的键，显示时把存盘的秒换算成分钟，提交时再乘回去。
      var mul = MIN_KEYS[String(k || "")] || 0;
      if(mul && v !== "" && v !== null && v !== undefined && isFinite(Number(v))) v = Math.round(Number(v) / mul);
      return "<span class='ipt" + (u ? (" has-unit" + (u.length > 1 ? " uw2" : "") + (u.length >= 4 ? " uw3" : "")) : "") + "'><input class='dipt num' type='number' data-k='" +
        esc(k) + "'" + (mul ? " data-mul='" + mul + "'" : "") + " value='" + esc(v) + "'>" + (u ? "<b class='unit'>" + esc(u) + "</b>" : "") + "</span>";
    }
    // 用户 m01978 第 3 条：这一行的标签就是接口页的名字，点一下能改名。
    // 用户 m02469：名字后面挂一把铅笔（跟首页改名那个图标同款），悬浮说明去掉。
    function rowName(ctl, hint){
      return "<div class='drow'><label class='dlb'>" +
        "<span class='jev-pgname' data-jev-rename='1'><span class='jev-pgnm'>" + esc(jevPageName(pg)) + "</span>" + ICON_PENCIL + "</span>" +
        "</label>" + ctl + "</div>";
    }
    function rowS(label, ctl, hint){
      return "<div class='drow stack'><label class='dlb'>" + esc(label) + "</label>" + ctl + "</div>";
    }
    function col(title, body){
      return "<div class='jev-col'>" +
        (title ? "<div class='jev-col-h'><span>" + esc(title) + "</span></div>" : "") +
        body + "</div>";
    }
    // ---- 左边：模型与不确定区间 ----
    var fwBody = "<div class='drows'>" +
        row("JEV 模型", "<select class='dipt' data-k='jev_model' id='jev-model'>" +
          jevModelOpts(String(dVal("jev_model", "")), jevEmptyText()) + "</select>") +
        row("备用模型", "<select class='dipt' data-k='jev_fallback_model' id='jev-fb'>" +
          jevModelOpts(String(dVal("jev_fallback_model", "")), "未选择模型") + "</select>") +
        // 用户 m01978 第 1 条：这两项并成一行，名字缩短，单位（次 / 秒）搬进框里。
        pair2("失败切换", swPill("jev_fail_switch", dVal("jev_fail_switch", true)),
          "切换模型", dnumIn("jev_fallback_cooldown", "分钟", 1800)) +
        // 用户 m02469：名字从「不确定区间」改成「不确定段」。
        // 用户 m16307 图2：两个下拉换成一根长条（dRange）。
        row("不确定段", dRange("jev_lower", "jev_upper", 25, 75)) +
        // 用户 m01978 第 2 条：这两项也并成一行。
        // v4.85（用户 m10425 第 6 条）：JEV 自己收不了图（decision 类模型只有 choice()），
        // 「不确定 → 交给决策模型」那一次转交要不要带图，用户 m09851 起统一由窗口页
        // 「图片」组里的「决策看图」决定，这里不再单独留一个「回退带图」开关。
        row("不确定时", "<select class='dipt' data-k='jev_action' id='jev-act'>" +
          "<option value='no_reply'" + (act === "no_reply" ? " selected" : "") + ">不回复</option>" +
          "<option value='reply'" + (act === "reply" ? " selected" : "") + ">回复</option>" +
          "<option value='decision'" + (act === "decision" ? " selected" : "") + ">交给决策模型</option></select>") +
      "</div>";
    // ---- 右边：第一列是接口页签，第二列是当前页的表单 ----
    // 用户 m01978 第 3 条：页签还是编号，悬浮能看到这一页的名字。
    var tabs = "<div class='jev-tabs'>" + pages.map(function(p){
        var on = String(p.id) === String(pg.id);
        return "<button type='button' class='jev-tab" + (on ? " on" : "") + "' data-jev-page='" +
          esc(String(p.id)) + "' title='" + esc(jevPageName(p)) + "'>" + esc(String(p.id)) + "</button>";
      }).join("") +
      "<button type='button' class='jev-tab jev-tab-add' data-jev-pageadd='1' title='再加一个第三方接口'>+</button></div>";
    var keyState = pg.api_key_set ? "已保存" : "还没保存";
    var keyTitle = pg.api_key_set ? ("已保存（" + String(pg.api_key_masked || "") + "）") : "还没保存";
    var apiBody = "<div class='jev-api'>" +
        "<div class='jev-api-body'><div class='drows'>" +
          row("选择协议", "<select class='dipt' data-jev-pg='protocol' id='jev-proto'>" +
            "<option value='systemone'" + (proto === "systemone" ? " selected" : "") + ">SystemOne（Jev 那套）</option>" +
            "<option value='openai'" + (proto === "openai" ? " selected" : "") + ">OpenAI 兼容（模型按 JSON 回概率）</option></select>") +
          rowName("<input class='dipt' data-jev-pg='base' id='jev-base' " +
            "placeholder='" + (proto === "openai" ? "https://…/compatible-mode/v1" : "https://api.typesafe.ai") + "' " +
            "value='" + esc(String(pg.base || "")) + "'>") +
          row("填入密钥", "<span class='jev-keyline'>" +
            "<input type='password' id='jev-key' autocomplete='off' " +
            "placeholder='" + (pg.api_key_set ? "已保存，留空则不改" : "填 API Key") + "'>" +
            "<button type='button' class='dbtn' data-jev-key='save'>保存</button>" +
            "<button type='button' class='dbtn' data-jev-key='clear'>清除</button>" +
            "<span class='jev-keystate' id='jev-key-note'></span>" +
          "</span>") +
          row("模型选择",
            "<span class='jev-datarow jev-combo'><input class='dipt' data-jev-pg='model' id='jev-apimodel' autocomplete='off' readonly " +
            "placeholder='点「获取」选择模型' value='" + esc(String(pg.model || "")) + "'>" +
            "<button type='button' class='dbtn' data-jev-get='1' id='jev-get'>获取</button>" +
            // 面板必须放在 .jev-combo 里面，才按这个输入框定位；放外面会按整页定位。
            "<div class='jev-modellist' id='jev-model-list' hidden></div></span>") +
        "</div>" + "</div>" +
        // 用户 m02835：页签放在右卡左下角，和右下角的「已保存」同一行。
        // 用户 m02950：「删除这一页」从表单底下挪到右卡右下，跟在保存状态左边。
        "<div class='jev-foot'>" +
          "<div class='jev-api-tabs'>" + tabs + "</div>" +
          "<span class='jev-foot-end'>" +
            (pages.length > 1 ? "<button type='button' class='dbtn' data-jev-pagedel='1'>删除这一页</button>" : "") +
            "<span class='jev-keystate' id='jev-key-state' title='" + esc(keyTitle) + "'>" + esc(keyState) + "</span>" +
          "</span>" +
        "</div>" +
      "</div>";
    return "<div class='dcard' id='jev-card'>" +
      "<div class='jev-cols'>" +
        // v4.85（用户 m10425 第 6 条）：左边这列不再挂「决策设置」标题。
        col("", fwBody) +
        col("", apiBody) +
      "</div>" +
    "</div>";
  }

  function jevRerenderCard(){
    var old = document.getElementById("jev-card");
    if(!old || !old.parentNode) return;
    var box = document.createElement("div");
    box.innerHTML = jevBlockHtml();
    var fresh = box.firstChild;
    if(fresh) old.parentNode.replaceChild(fresh, old);
    jevPaintEndpoint();
  }

  function jevRefresh(){
    if(!JEVOPT) return;
    var ms = document.getElementById("jev-model");
    if(ms) ms.innerHTML = jevModelOpts(ms.value || String(dVal("jev_model", "")), jevEmptyText());
    var fs = document.getElementById("jev-fb");
    if(fs) fs.innerHTML = jevModelOpts(fs.value || String(dVal("jev_fallback_model", "")), "未选择模型");
  }

  // 用户 m09506：模型清单不再用 <datalist>（那边点了展不开）。改成自己画的下拉面板：
  // 「获取」拿到清单就摊开，输入框里打字就地过滤，点一条就填进去。
  function jevModelListEl(){ return document.getElementById("jev-model-list"); }
  function jevPaintModelList(){
    var box = jevModelListEl();
    if(!box) return;
    if(JEV_MODEL_ERR){
      box.innerHTML = "<div class='jev-empty'>" + esc(JEV_MODEL_ERR) + "</div>";
      return;
    }
    var cur = String(jevPage().model || "").trim();
    var rows = JEV_MODELS.slice();
    if(cur && rows.indexOf(cur) < 0) rows.unshift(cur);
    box.innerHTML = rows.length
      ? rows.map(function(m){ return "<div data-jev-model='" + esc(m) + "'>" + esc(m) + "</div>"; }).join("")
      : "<div class='jev-empty'>先点「获取」拉取模型清单</div>";
  }
  function jevShowModelList(show){
    var box = jevModelListEl();
    if(!box) return;
    box.hidden = !show;
  }
  function jevFetchModels(){
    var base = document.getElementById("jev-base");
    var btn = document.getElementById("jev-get");
    var typed = base ? String(base.value || "").trim() : "";
    var proto = jevPageProtocol(jevPage());
    if(btn){ btn.disabled = true; btn.textContent = "获取中…"; }
    JEV_MODEL_ERR = "";
    get(API + "/jev-api-models?id=" + encodeURIComponent(JEV_PAGE) + "&base=" + encodeURIComponent(typed) + "&protocol=" + encodeURIComponent(proto))
      .then(function(d){
        if(btn){ btn.disabled = false; btn.textContent = "获取"; }
        JEV_MODELS = ((d && d.models) || []).map(String);
        if(!JEV_MODELS.length){
          JEV_MODEL_ERR = "没取到" + (d && d.error ? "：" + String(d.error) : "模型");
        }
        jevPaintModelList();
        jevShowModelList(true);
      })
      .catch(function(err){
        if(btn){ btn.disabled = false; btn.textContent = "获取"; }
        JEV_MODELS = [];
        JEV_MODEL_ERR = "没取到：" + String((err && err.message) || err);
        jevPaintModelList();
        jevShowModelList(true);
      });
  }

  // /jev-options 回来得比首屏渲染晚：把卡重画一次，密钥状态与实际请求地址才是新的。
  // 正在这张卡里打字就不动它（免得输入框被抽走）。
  function jevRerenderCardIfIdle(){
    var card = document.getElementById("jev-card");
    if(!card) return;
    var a = document.activeElement;
    if(a && card.contains(a)) return;
    jevRerenderCard();
  }

  function loadJevOptions(){
    get(API + "/jev-options" + (scopeSid ? "?sid=" + encodeURIComponent(scopeSid) : ""))
      .then(function(d){
        if(!d || !d.ok) return;
        JEVOPT = d;
        JEV_APIS = (d.apis || []).map(function(pg){
          return {
            id: String(pg.id || "1"),
            name: String(pg.name || ""),
            protocol: String(pg.protocol || "systemone"),
            base: String(pg.base || ""),
            model: String(pg.model || ""),
            api_key_set: !!pg.api_key_set,
            api_key_masked: String(pg.api_key_masked || "")
          };
        });
        JEV_PAGE = JEV_APIS.length ? String(JEV_APIS[0].id || "1") : "1";
        jevRefresh();
        jevRerenderCardIfIdle();
      })
      .catch(function(){ /* 拿不到就按空清单画，不影响别的页 */ });
  }

  function dtoolsBlockHtml(){
    var hint = "勾选的工具作为原生工具发送，是否写入 system 由工具模块决定；模型要调就真的去调，最多连着调几轮由「回复提示词」页的「Agent循环次数」决定。";
    return "<div class='dcard'><div class='dhead'>决策可用的工具" +
      "<span class='dhead-x'>最多 <b id='dec-rounds-hint'>5</b> 轮</span></div>" +
      "<input type='hidden' data-k='decision_tools' id='dec-tools' value='" + esc(JSON.stringify(dPickedRaw())) + "'>" +
      "<div class='dtools' id='dec-tools-box'>" + dToolsInner() + "</div></div>";
  }

  // Shared prompt editor: drafts never enter data-k/config/reload or browser storage.
  // The server is the only assembler; even unsaved previews use POST /prompt-preview.
  var PP_STATES = Object.create(null), ppModuleCounter = 0;
  var PP_REF_DIRTY = Object.create(null);
  function ppClone(value){ return JSON.parse(JSON.stringify(value)); }
  function ppState(kind, sid){
    // 用户 m06706：JEV 是第三块提示词，跟决策 / 回复各存各的布局。
    kind = (kind === "reply" || kind === "jev") ? kind : "decision";
    sid = sid === undefined ? String(scopeSid || "") : String(sid || "");
    var key = JSON.stringify([sid, kind]);
    if(!PP_STATES[key]) PP_STATES[key] = {
      kind:kind, sid:sid, loaded:false, loading:null, layout:null, defaultLayout:null,
      catalog:[], officialReference:null, nativeTemplates:null, referenceOpen:false,
      toolChoices:[], toolsSelected:[],
      revision:null, inherited:false, contextSid:"", dirty:false, saving:false,
      editSeq:0, seq:0, modeSeq:0, timer:0, preview:null, previewPending:false, referencesPending:false, open:Object.create(null),
      previewError:"", error:"", notice:""
    };
    return PP_STATES[key];
  }
  function ppCurrent(s){ return s.sid === String(scopeSid || ""); }
  function ppHasDrafts(){
    return Object.keys(PP_STATES).some(function(k){ return PP_STATES[k].dirty || PP_STATES[k].saving; });
  }
  function ppPrefix(kind){ return kind === "reply" ? "rep" : (kind === "jev" ? "jev" : "dec"); }
  function ppRequest(path, body){
    var opts = {credentials:"same-origin"};
    if(body !== undefined){
      opts.method = "POST";
      opts.headers = {"Content-Type":"application/json"};
      opts.body = JSON.stringify(body);
    }
    opts.cache = "no-store";
    return fetch(API + path, opts).then(function(r){
      return r.json().catch(function(){ return {}; }).then(function(d){
        if(!r.ok || !d || d.ok !== true){
          var err = new Error(String((d && (d.error || d.detail)) || ("HTTP " + r.status)));
          err.code = String((d && d.code) || ""); err.status = r.status;
          throw err;
        }
        return d;
      });
    });
  }
  function ppTypeAllowed(s, type){
    // 用户 m06706：JEV 那一页没有工具、也没有决策专用的两段（判断规则是普通文本块）。
    if(s.kind === "jev"){
      return type === "text" || type === "identity" || type === "persona" ||
        type === "realtime_marker" || type === "dynamic_marker" || type === "dynamic" ||
        type === "system_dynamic" || type === "system_memory" || type === "system_time";
    }
    return type === "text" || type === "identity" || type === "persona" || type === "tools" ||
      type === "realtime_marker" || type === "dynamic_marker" || type === "dynamic" ||
      type === "system_dynamic" || type === "system_memory" || type === "system_time" ||
      (type === "framework" && s.kind === "reply") ||
      ((type === "decision_prompt" || type === "decision_format") && s.kind === "decision");
  }
  // 用户 m02159 图3：身份 / 工具 / 位置不是「模板」，新增下拉里不再列；它们本来就在固定位置上。
  function ppAddHidden(c){
    // 用户 m03879 图4/图6：已经被锁死、位置固定的段别再出现在新增下拉里——加进去也只能原样待着。
    // 用户 m03879 图5：这一串里还列着「角色扮演（人格）」，它同样是页面上固定的一块（锁定、不能删），
    // 换人格直接改那一块的下拉就行，不需要再从这儿加一份。动态段同理（整个页只留一块固定的）。
    return c.type === "identity" || c.type === "tools" || c.type === "realtime_marker" ||
      c.type === "system_dynamic" || c.type === "system_memory" || c.type === "system_time" ||
      c.type === "dynamic_marker" || c.type === "dynamic" ||
      c.type === "persona" ||
      (c.type === "framework" && String(c.key || "") === "tools");
  }
  // 用户 m04343 图5/图6：人格 / 工具 / 身份 / 实时窗 / 系统动态段在页面上各有一块固定的，
  // 换来源也换不出别的东西，来源下拉里不再列它们（新增下拉早就藏了，这次连来源一起藏）。
  function ppFixedPicker(c){
    // 用户 m04617 图8：动态段不算固定段了，来源下拉里要能选它（换成别的动态写法）。
    return c.type === "persona" || c.type === "tools" || c.type === "identity" ||
      c.type === "realtime_marker" || c.type === "system_dynamic" ||
      c.type === "system_memory" || c.type === "system_time" ||
      c.type === "dynamic_marker" ||
      (c.type === "framework" && String(c.key || "") === "tools");
  }
  function ppRef(s, module){
    return s.catalog.filter(function(c){
      return c.type === module.type && String(c.key || "") === String(module.key || "");
    })[0] || null;
  }
  function ppReferenceText(s, module){
    var ref = ppRef(s, module);
    return ref && typeof ref.text === "string" ? ref.text : "";
  }
  // 用户 m03879：系统动态段是框架自己拼进去的，卡片上直接把预览里那一段真正的字显示出来，
  // 否则永远是「0 字」，玩家看不出这一格到底注入了什么。
  function ppRenderedPart(s, name){
    var parts = (s && s.preview && s.preview.parts) || [];
    for(var i = 0; i < parts.length; i++){
      // 用户 m23323 图2：人格段的 part.name 是人格文件名（不是 persona），只按 name 找会漏，
      // 卡片就退回目录里那份 60 字预览——看着像「选了人设反而没注入」。type 也一起认。
      if(String(parts[i].name || "") === name || String(parts[i].type || "") === name) return parts[i];
    }
    // 用户 m10297 图1：系统动态段 / 动态段被「启用」关掉时，后端不再把它放进 parts
    // （关着就不该进右侧预览），但左边卡片还得照实显示这一格有多少字、写了什么——
    // 所以后端另给一份 inactive，这里当回退值用。
    var off = (s && s.preview && s.preview.inactive) || [];
    for(var j = 0; j < off.length; j++){
      if(String(off[j].name || "") === name) return off[j];
    }
    return null;
  }
  function ppCanConvert(s, module){
    var ref = ppRef(s,module);
    return !!ref && ref.available !== false && typeof ref.text === "string";
  }
  function ppTypeOptions(s, module, forAdd){
    // 用户 m02159 图3：模块名和下拉必须说的是同一个名字——自写文本这块就用它自己的标题，
    // 下拉第一项写成「什么时候接（自写）」，不会莫名其妙变回「自写文本」。
    var isTextNow = !!module && module.type === "text";
    var headName = isTextNow && String(module.label || "").trim() ? String(module.label).trim() : "自写文本";
    // 用户 m02993 图8：三个小模块（什么时候接 / 什么时候别接 / 节奏与分寸）本来就是默认模板，
    // 下拉里要标「（默认模板）」；只有真的自己写出来的才标「（自写）」。
    var defHit = null;
    if(isTextNow){
      s.catalog.forEach(function(c){
        if(!defHit && c.skeleton && String(c.label || "") === headName) defHit = c;
      });
    }
    // 用户 m04343 第 4 条：默认模板（三块小模块 / 输出规则）改过正文就算「自定义」，下拉也这么标。
    var tweakedTpl = !!(defHit && String(module.content || "") !== String(defHit.text || ""));
    // 用户 m04617 图7：那一条「自写文本（自定义）」只属于「＋ 新增」下拉；
    // 某一块本来就不是文本（人格 / 工具 / 输出规则）时再列它，等于让人把这一格改成空白文本。
    var refs = [], seen = {};
    if(forAdd || isTextNow){
      if(!forAdd && defHit){
        // 用户 m08755 第 7 条：模板下拉别重复。当前这一块本来就是某份默认模板时，
        // 直接用它自己那条（key 指回目录里那份），不再另造一条同名的空 key 合成项——
        // 那条合成项会让下面的回退逻辑再补一条，两条都选不中，来回切还变成「自定义」。
        var hitCopy = {};
        for(var hk in defHit){ if(Object.prototype.hasOwnProperty.call(defHit, hk)) hitCopy[hk] = defHit[hk]; }
        hitCopy.suffix = (tweakedTpl ? "自定义" : ppSuffixOf(defHit));
        refs.push(hitCopy);
        seen[defHit.type + "\n" + String(defHit.key || "")] = true;
        // 2026-11-18（J，用户 m12261）：正文改过以后，下拉里还要留着这一块自己的默认版本，
        // 选它就把正文换回默认稿（key 仍指回同一份模板），不用整页「恢复默认」。
        if(tweakedTpl){
          var defCopy = {};
          for(var dk in defHit){ if(Object.prototype.hasOwnProperty.call(defHit, dk)) defCopy[dk] = defHit[dk]; }
          defCopy.suffix = "默认";
          defCopy.restoreDefault = true;
          refs.push(defCopy);
        }
      }else{
        refs.push({type:"text", key:"", label:headName, available:true, group:"custom",
                   suffix:(forAdd ? "自定义" : (defHit ? (tweakedTpl ? "自定义" : "默认") : (isTextNow ? "自写" : "自定义")))});
        seen["text\n"] = true;
      }
    }
    s.catalog.forEach(function(c){
      var key = c.type + "\n" + String(c.key || "");
      if(!ppTypeAllowed(s, c.type) || seen[key]) return;
      if(forAdd && ppAddHidden(c)) return;
      if(!forAdd && ppFixedPicker(c)) return;
      // 用户 m04617 图7：目录里那条「自写文本」只给「＋ 新增」用，来源下拉里不再出现；
      // 文本块自己由下面 fallback 那一条代表，名字跟标题一致。
      if(!forAdd && c.type === "text" && String(c.key || "") === "") return;
      // 用户 m09506：判断规则这块的下拉别混进「动态段」模板——动态段有自己的段位，换不到这里来。
      if(c.type === "dynamic" && module.type !== "dynamic") return;
      if(c.type === "dynamic_marker" && module.type !== "dynamic") return;
      // 同名的那条默认模板已经由第一项代表（比如「输出规则」），再列一条就重了。
      if(isTextNow && String(c.label || "") === headName && !(c.type === "text" && String(c.key || "") === "")) return;
      seen[key] = true; refs.push(c);
    });
    var selected = JSON.stringify([module.type, String(module.key || "")]);
    if(!refs.some(function(c){ return JSON.stringify([c.type,String(c.key || "")]) === selected; })){
      // 当前这块的类型不在目录里（旧布局、或被新增下拉藏起来的固定段）：留一条同名项，
      // 否则下拉会把「工具说明」显示成「自写文本」，看着像数据丢了。
      var sfx = (module.type === "text")
        ? (defHit ? (tweakedTpl ? "自定义" : ppSuffixOf(defHit)) : "自写")
        : ({text:"自定义", custom:"自定义", decision_format:"默认", decision_prompt:"默认",
        persona:"人格", identity:"只读", tools:"工具", realtime_marker:"位置", dynamic_marker:"位置",
        dynamic:"", framework:"默认"}[module.type] || "自定义");
      refs.push({type:module.type,key:module.key,label:module.label || module.type,group:"custom",suffix:sfx});
    }
    // 用户 m01209 图4：自己的模板一律排前面，默认模板统统排后面。
    function own(c){ return (c.group || "default") === "custom"; }
    refs = refs.filter(own).concat(refs.filter(function(c){ return !own(c); }));
    // 只在「同名又同类型」时才用 tag 区分（比如几个人格文件）；「角色扮演」既有框架原段
    // 又有当前人格，它们后缀已经不同（原段 / 人格），再叠一层 tag 就变成两个括注。
    var counts = {};
    refs.forEach(function(c){ var k = String(c.label || c.type) + '\n' + c.type; counts[k] = (counts[k] || 0) + 1; });
    return refs.map(function(c){
      var value = c.restoreDefault
        ? JSON.stringify([c.type, String(c.key || ""), "@default"])
        : JSON.stringify([c.type, String(c.key || "")]);
      var label = String(c.label || c.type);
      // 同一类默认模板有多条（比如好几个人格文件）时带上区分名，否则下拉里全一样。
      if(!own(c) && counts[String(c.label || c.type) + '\n' + c.type] > 1 && c.tag) label += " · " + String(c.tag);
      // 用户 m01209 图2/图4：只有真正的模板才标「默认」；身份/工具/人格/位置/
      // 自己拆的小模块各自标清是什么，下拉一眼能分。
      var sfxTxt = ppSuffixOf(c);
      var suffix = sfxTxt ? "（" + sfxTxt + "）" : "";
      return "<option value='" + esc(value) + "'" + (selected === value ? " selected" : "") + ">" +
        esc(label + suffix) + (c.available === false ? "（不可用）" : "") + "</option>";
    }).join("");
  }
  // 用户 m28586 第 4 条：自定义模块也要出现在「＋ 新增」的下拉里，方便随时加回来。
  // v4.84：只列当前这一页自己的自定义块——跨页搬来的那份跟本页的混在一起分不清，
  // 搬过来之后正文、名字都一样，看着像重复项。要跨页用，去那一页复制。
  function ppCloneOptions(s){
    var out = "", seen = {};
    var st = ppState(s.kind);
    if(st && st.loaded && st.layout && st.layout.modules){
      st.layout.modules.forEach(function(m, i){
        if(!ppModIsCustom(st, m)) return;
        var nm = String(m.label || "自写文本"), body = String(m.content || "");
        var sig = nm + "|" + body;
        if(seen[sig]) return;
        seen[sig] = true;
        out += "<option value='" + esc(JSON.stringify(["clone", s.kind + "#" + i])) + "'>" +
          esc(nm + "（自定义）") + "</option>";
      });
    }
    return out ? ("<optgroup label='你的自定义模块'>" + out + "</optgroup>") : "";
  }
  function ppButton(s, action, text, index, disabled){
    return "<button type='button' class='dmini' data-pm-kind='" + s.kind + "' data-pm-action='" + action + "'" +
      (index === undefined ? "" : " data-pm-index='" + index + "'") + (disabled ? " disabled" : "") + ">" + text + "</button>";
  }
  function ppIcon(s, action, text, title, index, disabled){
    return "<button type='button' class='pm-ico' title='" + esc(title) + "' aria-label='" + esc(title) + "'" +
      " data-pm-kind='" + s.kind + "' data-pm-action='" + action + "'" +
      (index === undefined ? "" : " data-pm-index='" + index + "'") + (disabled ? " disabled" : "") + ">" + text + "</button>";
  }
  function ppSuffixOf(c){
    // 用户 m15846：下拉里的括注只留「默认 / 自定义 / 自写 / 人格 / 只读 / 工具 / 位置」这几种。
    // 「默认模板」缩成「默认」，「原段」也归到「默认」；动态段自己就是一段模板，不再括注。
    if(String(c.type || "") === "dynamic") return "";
    var sfx = String(c.suffix || "");
    if(sfx === "默认模板" || sfx === "原段") return "默认";
    if(sfx) return sfx;
    return (c.group || "default") === "custom" ? "自定义" : "默认";
  }
  // 用户 m09506：以前点中过那条同名的「空 key」选项——正文是模板的字，key 却是空的，
  // 下拉里就多出一条看着像「默认模板」的重复项，来回切还会变成「自定义」。
  // 载入时按标题把它接回目录里那份默认模板：正文一个字都不动，只把 key 补回去。
  function ppAdoptSkeletons(s){
    if(!s.layout || !Array.isArray(s.layout.modules)) return 0;
    var n = 0;
    s.layout.modules.forEach(function(m){
      if(String(m.type || "") !== "text" || String(m.key || "") !== "") return;
      var nm = String(m.label || "").trim();
      if(!nm) return;
      var hit = null;
      (s.catalog || []).forEach(function(c){
        if(!hit && c.skeleton && String(c.label || "").trim() === nm) hit = c;
      });
      if(hit && String(hit.key || "")){ m.key = String(hit.key); n += 1; }
    });
    return n;
  }
  // 用户 m02159 图3：人格段的菜单不是「选模板」，而是「选人格」；默认空人格＝这一段跳过。
  function ppPersonaOptions(s, module){
    var refs = (s.catalog || []).filter(function(c){ return c.type === "persona"; });
    var cur = String(module.key || "");
    // m02993 图3：回复页没选人格文件时并不真的空——渲染会回退到框架原文的人设段（预览里 persona 段还在），
    // 所以文案分开写，别让人以为回复页的人格被关掉了。
    // 用户 m03879：下拉里那串长说明挤掉了标题旁边的描述，缩成跟标题同款的短句。
    var emptyTxt = (s.kind === "reply") ? "（空人格 · 用框架原文）" : "（空人格 · 不注入）";
    var out = "<option value='[\"persona\",\"\"]'" + (cur ? "" : " selected") + ">" + emptyTxt + "</option>";
    refs.forEach(function(c){
      var key = String(c.key || "");
      // 用户 m02993 图3：这一格是「选人格」，下拉里就该是人格名，别再复读一遍「角色扮演」。
      out += "<option value='" + esc(JSON.stringify(["persona", key])) + "'" + (cur === key ? " selected" : "") + ">" +
        esc(String(c.tag || c.label || key)) + "</option>";
    });
    if(cur && !refs.some(function(c){ return String(c.key || "") === cur; })){
      out += "<option value='" + esc(JSON.stringify(["persona", cur])) + "' selected>（找不到这个文件：" + esc(cur) + "）</option>";
    }
    return out;
  }
  // 用户 m02159 图3：工具不再占一条模板，而是勾在「工具说明」段下面，注入和可调用同源。
  // 用户 m20823：「已选 N / M」的 N 只数当前清单里存在的；名单里残留的旧名字另外算。
  function ppToolPicked(s){
    var list = Array.isArray(s.toolChoices) ? s.toolChoices : [];
    var avail = {};
    list.forEach(function(t){ avail[String(t.name || "")] = 1; });
    return (Array.isArray(s.toolsSelected) ? s.toolsSelected : []).map(String).filter(function(n){ return avail[n]; });
  }
  function ppToolStale(s){
    var list = Array.isArray(s.toolChoices) ? s.toolChoices : [];
    var avail = {};
    list.forEach(function(t){ avail[String(t.name || "")] = 1; });
    return (Array.isArray(s.toolsSelected) ? s.toolsSelected : []).map(String).filter(function(n){ return !avail[n]; });
  }
  function ppToolBox(s, module, index){
    var list = Array.isArray(s.toolChoices) ? s.toolChoices : [];
    var picked = (Array.isArray(s.toolsSelected) ? s.toolsSelected : []).map(String);
    // 用户 m20823：勾选名单里会留下已经不存在的工具（换插件、关插件之后），
    // 「已选」只数这一页真能勾到的，别出现「已选 16 / 9」这种大于总数的读数。
    var pickedOn = ppToolPicked(s), stale = ppToolStale(s);
    if(!list.length) return "<div class='pm-hint'>这个会话没有可用工具，这一段会注入空清单。</div>";
    // 用户 m02993 图2：工具清单收进一个折叠里，跟别的模板「正文」一样要点开才看。
    // 没勾任何工具时默认展开——不然会出现「已选 0 / 4」却找不到东西可勾。
    // 用户 m04343 图3：清单默认折叠，要点开才看（summary 上一直写着「已选 N / M」）。
    return "<details class='pm-tools-fold'>" +
      "<summary class='pm-sum'><span>工具清单</span>" +
      "<span class='pm-tools-sum' id='pp-toolsum-" + s.kind + "'" +
        (stale.length ? " title='另有 " + stale.length + " 个已勾的工具当前不可用：" + esc(stale.join("、")) + "'" : "") +
        ">已选 " + pickedOn.length + " / " + list.length + "</span></summary>" +
      "<div class='pm-tools'><div class='pm-tools-head'>" + (s.kind === "reply"
        ? "勾哪个就把哪个写进这一段，回复链也只挂这几个工具（全不勾＝一个都不挂）。新装插件带的工具会自动勾上。"
        : "勾哪个就把哪个写进这一段，决策链也只挂这几个工具（全不勾＝一个都不挂）。新工具要自己勾。") + "</div>" +
      list.map(function(t){
        var name = String(t.name || "");
        var on = picked.indexOf(name) >= 0;
        return "<label class='pm-tool" + (on ? " on" : "") + "' title='" + esc(name + "：" + String(t.description || "")) + "'><input type='checkbox' data-pm-tool='" + esc(name) + "'" +
          (on ? " checked" : "") + "><span class='pm-tool-name'>" + esc(name) + "</span>" +
          "<span class='pm-tool-desc'>" + esc(String(t.description || "")) + "</span></label>";
      }).join("") + "</div></details>";
  }
  // 用户 m08755 第 6 条：实时窗改成读真正的窗口内容（只读），右边下拉选哪个群组。
  // 只是「看」和「测试用」：不写进提示词，保存的布局也不含这段正文。
  function ppRtSid(s){
    if(s.rtSid) return String(s.rtSid);
    return String(scopeSid || s.contextSid || dScopeSidForDecision() || "");
  }
  function ppRtOptions(s){
    var cur = ppRtSid(s), out = "", seen = {};
    (sessCache || []).forEach(function(it){
      var sid = typeof it === "string" ? it : String(it.sid || it.session_id || it.id || "");
      if(!sid || seen[sid]) return; seen[sid] = 1;
      out += "<option value='" + esc(sid) + "'" + (sid === cur ? " selected" : "") + ">" + esc(sidLabel(sid)) + "</option>";
    });
    if(!out) out = "<option value='" + esc(cur) + "' selected>" + esc(cur ? sidLabel(cur) : "（没有可用会话）") + "</option>";
    else if(cur && !seen[cur]) out += "<option value='" + esc(cur) + "' selected>" + esc(sidLabel(cur)) + "</option>";
    return out;
  }
  function ppLoadWindowText(s){
    var sid = ppRtSid(s);
    if(!sid){ s.rtText = ""; s.rtInfo = "还没选群组"; return Promise.resolve(); }
    s.rtLoading = true; s.rtInfo = "读取中…";
    return get(API + "/window-text?sid=" + encodeURIComponent(sid) + "&_=" + Date.now())
      .then(function(d){
        s.rtLoading = false;
        if(!d || !d.ok){ s.rtText = ""; s.rtInfo = "读不到实时窗：" + String((d && d.error) || "接口没回数据"); return; }
        s.rtText = String(d.text || "");
        s.rtInfo = "共 " + Number(d.messages || 0) + " 条 · " + s.rtText.length + " 字";
      })
      .catch(function(err){ s.rtLoading = false; s.rtText = ""; s.rtInfo = "读不到实时窗：" + String((err && err.message) || err); });
  }

  // 用户 m21698：记忆插件那两段改成布局里的真模块后，这里不再单独画只读卡（ppPluginSegHtml 已删）。

  // 2026-11-18（J，用户 m11554）：默认稿改了哪一块，就在那一块旁边提一句——一次性的。
  // 点「换成新版」把这一段换成新版默认稿（其它段一个字不动），点 ✕ 只关这一块。
  function ppModNoticeHtml(s, module, index){
    var list = (s.defNotice && s.defNotice.modules) || [];
    var mid = String((module && module.id) || "");
    var hit = null;
    for(var i = 0; i < list.length; i++){
      if(String(list[i].id || "") === mid && list[i].mode === "inline"){ hit = list[i]; break; }
    }
    if(!hit) return "";
    return "<div class='pm-modnotice'>" +
      "<span class='pm-modnotice-t'>这一段的默认提示词更新了</span>" +
      "<button type='button' class='dmini pm-modnotice-go' data-pm-action='notice-adopt'" +
      " data-pm-kind='" + s.kind + "' data-pm-index='" + index + "'>换成新版</button>" +
      "<button type='button' class='pm-modnotice-x' title='知道了，不再提示' aria-label='关闭提示'" +
      " data-pm-kind='" + s.kind + "' data-pm-action='notice-off' data-pm-module='" + esc(mid) + "'>✕</button></div>";
  }
  function ppModuleHtml(s, module, index){
    var typ = String(module.type || "text");
    // 用户 m02993 图5：只标位置的那条「动态尾段」单行占位撤了——动态段现在是一块
    // 正经模板（能编辑、能放空）。旧布局里残留的 dynamic_marker 一律当动态段处理。
    if(typ === "dynamic_marker"){ typ = "dynamic"; }
    var ref = ppRef(s, module), custom = typ === "text" || typ === "custom" || typ === "dynamic";
    var isPersona = typ === "persona", isTools = typ === "tools", isIdentity = typ === "identity";
    var isRealtime = typ === "realtime_marker", isDynamic = typ === "dynamic";
    var isSystemMem = typ === "system_memory", isSystemTime = typ === "system_time";
    var isSystemDyn = isSystemMem || isSystemTime || typ === "system_dynamic";
    // 用户 m23028：记忆插件注入的两段（固定说明 / 本轮上下文）现在是布局里的真模块——
    // 正文由插件在请求时现给，卡片只显示 + 只让你挪位置；金色标出来，跟框架自己的段分开。
    var isPluginMem = typ === "plugin_memory", isPluginCtx = typ === "plugin_memory_ctx";
    var isPlugin = isPluginMem || isPluginCtx;
    // 用户 m02159 图3：人格段锁住不可删，但可以放空、可以挪位置；身份段与实时窗口段同理。
    // 用户 m02993 图2：工具模板也锁定（不能删），但位置可以挪。
    // 用户 m04617 图3/图8：动态段本来就是自己加的模板，要能选、能删、能挪，
    // 所以它不再算锁定段；真正锁死的只有人格 / 身份 / 工具 / 实时窗 / 系统动态段。
    // 用户 m05124 图1：「强化格式遵守」这块是锁定的（标「锁定」，没有 ✕），正文照旧能改。
    // 用户 m26048：位置也能挪了，只是不能越过实时窗口——它注入的是聊天记录末尾。
    var locked = isPersona || isIdentity || isRealtime || isTools || isSystemDyn || isDynamic || isPlugin;
    // 用户 m04343 第 3 条：动态段「不锁定也不固定」，只是位置被钉在实时窗口下面、
    // 系统动态段上面——所以它不留删除键，也不挂锁标，只把 ↑↓ 变灰。
    // 用户 m04617 图3：只有实时窗是「固定」（位置钉死），别的都只是「锁定」（不能随便删）。
    var lockText = isRealtime ? "固定" : "锁定";
    var lockTip = isPersona ? "人格段是固定的一块，不能删；可以放空，也可以换人格、挪位置。"
      : isIdentity ? "「你的身份」由框架自动填写，只能看不能改；位置可以挪。"
      : isTools ? "工具段是固定的一段，不能删；勾选写在这一段下面，位置可以挪。"
      : isRealtime ? "实时窗口只标位置：永远排在系统模板下面、动态段上面，不能删。"
      : isDynamic ? "动态段不能删，正文可以改；位置只能在实时窗口下面挪，不能挪到实时窗口上面。"
      : isSystemMem ? "记忆段是框架自己拼在 system 末尾的动态内容，只标位置，不能删；位置可以挪。"
      : isSystemTime ? "时间段是框架自己拼在 system 末尾的动态内容，只标位置，不能删；位置可以挪。"
      : isPluginMem ? "记忆插件注入：正文由记忆插件在请求时现给，这里只决定它放在哪；不能删。"
      : isPluginCtx ? "记忆插件 · 本轮上下文：每轮现算，这里只决定它放在哪；不能删。"
      : "系统动态段是框架自己拼在 system 末尾的那些动态内容，只标位置，不能删。";
    // 用户 m02993 图1：实时窗口与动态段标的是「位置」，位置由框架钉死（系统提示词下面、最后一段），
    // 所以上下移一律禁用，只留启用/停用。用户 m25796：记忆段 / 时间段不再跟着钉死——
    // 它们只是「锁定」（不能删、不能改），位置可以挪。
    // 用户 m04617 图8：动态段能上下挪（默认还是排在实时窗下面、系统动态段上面）。
    // 用户 m26048：「强化格式遵守」也不再钉死位置，只剩实时窗口是固定的。
    var fixedPos = isRealtime;
    // 用户 m27528 第 5 条：角色扮演（人格）这一段锁死——只能看，不能在这儿改，
    // 换人设请用右上角的下拉。以前它能改，一改就悄悄复制成一份「我的副本」。
    var ro = isIdentity || isSystemDyn || isPersona || isPlugin;
    // 用户 m04617 图6：选了人格以后，卡片上的字数得跟真正注入的那一段一致，
    // 不能再显示目录里那份预览（60 字的空骨架），否则看着像「选了反而没注入」。
    // 用户 m05691：动态段默认不含正文（跟着默认模板走），字数与正文都按真正注入的那段显示。
    var rendered = isSystemMem ? ppRenderedPart(s, "system_memory")
      : isSystemTime ? ppRenderedPart(s, "system_time")
      : isSystemDyn ? ppRenderedPart(s, "system_dynamic")
      : isPersona ? ppRenderedPart(s, "persona")
      : isPlugin ? ppRenderedPart(s, typ)
      : isDynamic ? ppRenderedPart(s, "dynamic") : null;
    // 用户 m14457 图1：停用的模块以前照样把整段正文铺在卡上（只是变淡），
    // 看着像「关了右边预览还留着」——那其实是左边卡片。停用就明说，不再铺全文。
    var off = !!(module.enabled === false);
    // 用户 m23323 图1：停用也照实显示正文与字数——卡片是「这一段会收到什么」的展示位，
    // 换成一串「未启用…」的说明字反而看不出内容（系统动态段就是这么被读成空白的）。
    var body = rendered ? String(rendered.text || "")
      : ((typ === "text" || isDynamic || isPlugin) ? String(module.content || "") : ppReferenceText(s, module));
    // 用户 m09506：实时窗的真实内容与「读取哪个群组」不止 JEV 页要有，决策 / 回复页也一样。
    var rtLive = isRealtime;
    if(rtLive){ body = String(s.rtText || ""); }
    // 2026-11-18（J，用户 m12261）：标题后面那个橙色「自定义」小标撤掉——来源下拉已经写了「（自定义）」。
    // 用户 m03879 图3/图7：不会变的那些段标题不需要改，描述直接写在标题右边就够了。
    var editableTitle = (typ === "text" || typ === "custom");
    // 用户 m05124 图1：「你的身份」「系统动态段」标题右边的小字撤掉（右侧的位置标签已经说明清楚），
    // 实时窗那句「只标位置 · 不注入」搬进正文区右边，跟可编辑框同一行。
    var headDesc = isPersona ? (module.key ? ("人格：" + String(module.key))
        : ((s.kind === "reply") ? "空人格 · 用框架原文" : "空人格 · 不注入"))
      : (isTools ? "勾选注入"
        // 用户 m10962 第 1 条：动态段标题右边那句「拼在聊天记录最后」也去掉，位置标签已经说明清楚。
        : (isDynamic ? ""
          : (isPlugin ? (isPluginMem ? "插件现给" : "每轮现算")
            : (typ === "framework" ? "原段" : (typ === "custom" ? "自定义" : "")))));
    // 用户 m03879 图3：位置标签只说明「注入在哪」，放到「N 字」左边。
    // 用户 m04343 第 4 条：框架原段（系统模板）也挂「系统段」标签，跟系统动态段一个样。
    var isSysSeg = isSystemDyn || isIdentity || isTools || typ === "framework" || isPluginMem;
    var posLabel = isRealtime ? "实时窗" : (isPlugin ? "插件段" : (isSysSeg ? "系统段" : (isDynamic ? "动态段" : "")));
    var posCls = isRealtime ? "pm-pos-rt" : (isPlugin ? "pm-pos-plugin" : (isSysSeg ? "pm-pos-sys" : "pm-pos-dyn"));
    var source, note = ref && ref.note ? String(ref.note) : "";
    if(isPersona){
      source = module.key ? ("人格文件：" + String(module.key))
        : ((s.kind === "reply") ? "跟随框架人设段" : "空人格");
      // m02993 图3：回复页没选人格文件时用的是框架原文的人设段，不是「什么都不注入」。
      if(!module.key) note = (s.kind === "reply")
        ? "没选人格文件时，这一段用框架原文的人设段。"
        : "没选人格文件时这一段整段跳过，不注入任何字。";
    }else if(isTools){
      source = "勾选注入：清单写在这一段下面";
    }else if(isRealtime){
      source = "位置说明：这一段告诉模型聊天记录放在哪，不注入 system";
    }else if(isSystemMem){
      source = "位置说明：框架自己拼在 system 末尾的动态内容（记忆）";
    }else if(isSystemTime){
      source = "位置说明：框架自己拼在 system 末尾的动态内容（时间）";
    }else if(isSystemDyn){
      source = "位置说明：框架自己拼在 system 末尾的动态内容（会话信息 / 聊天环境 / 记忆 / 时间）";
    }else if(isPlugin){
      source = isPluginMem ? "记忆插件注入：正文由插件在请求时现给，这里只决定它放在哪"
        : "记忆插件 · 本轮上下文：每轮现算，这里只决定它放在哪";
    }else if(isDynamic){
      source = "动态段：拼在本轮聊天记录最后，紧贴模型开口的位置";
    }else{
      source = typ === "text" ? "自定义：只在这一段里，随便改"
        : (typ === "custom" ? "自定义模板：保存的固定文本"
          : (ref ? String(ref.source || "引用当前原文") : "目录中没有此引用"));
    }
    var chars = String(body ? body.length : 0);
    // 用户 m23323 图3：系统段「不允许往下」——排在实时窗口上面那一条的下移键直接禁掉。
    var nextType = (index + 1 < s.layout.modules.length) ? String((s.layout.modules[index + 1] || {}).type || "") : "";
    var nextIsRt = nextType === "realtime_marker";
    var open = !!(s.open && s.open[index]);
    var tip = source + (note ? " · " + note : "");
    var foot = "";
    if(isPersona){
      var plist = (s.catalog || []).filter(function(c){ return c.type === "persona"; });
      foot = (module.key && !plist.some(function(c){ return String(c.key || "") === String(module.key); }))
        ? "<div class='pm-hint'>找不到人格文件「" + esc(String(module.key)) + "」：这一段会跳过，去人格库里确认名字。</div>" : "";
    }
    // 来源下拉：只有还能换内容的段才给（人格 / 框架原段 / 自写）；位置段与身份段没得选。
    // 用户 m04617 图8：动态段也要有下拉——它本身就是一段模板，能换成别的写法。
    var hasSelect = !(isIdentity || isTools || isRealtime || isSystemDyn || isPlugin);
    // 用户 m28586 第 3 条：右边那个「编辑（存成我的模板）」按钮撤掉。规则就一句话——
    // 直接在下面的正文框里改字，改完自动复制一份存进模板库（名字变成「原名·复制（自写）」），
    // 默认模板一个字都不动。这里只剩「自定义模板」那块去模板库改正文的入口。
    var convert = (typ === "custom" && module.key) ? ppButton(s,"edit_tpl","改这个模板",index,false) : "";
    var noteText = isPersona ? ""
      : (note || (isPlugin && rendered && rendered.sample_only
          ? "插件这轮没回传正文，下面显示的是默认模板（不会注入）。" : "")
        || ((!custom && !isTools && !isPlugin && (!ref || ref.available === false)) ? "这个引用现在没内容，预览会给警告。" : ""));
    // 用户 m04343 第 4 条：来源下拉搬到标题右边同一行（标题照旧能改名）；下面那行只留说明和按钮。
    var picker = hasSelect
      ? "<select class='dipt pm-src' aria-label='模块来源' title='换这一段用哪个模板' data-pm-field='type'" +
        " data-pm-index='" + index + "'>" +
        (isPersona ? ppPersonaOptions(s, module) : ppTypeOptions(s, module)) + "</select>"
      : "";
    var rtPick = rtLive
      ? "<span class='pm-rtpick'><label>读取</label><select class='dipt' data-rt-sid='1' aria-label='选择读取哪个群组的实时窗'>" +
        ppRtOptions(s) + "</select><span class='pm-rtinfo'>" + esc(String(s.rtInfo || "")) + "</span>" +
        "<button type='button' class='dmini' data-rt-reload='1'>刷新</button></span>"
      : "";
    var metaHtml = (noteText || convert)
      ? "<div class='pm-meta'>" +
        (noteText ? "<span class='pm-src-note' title='" + esc(tip) + "'>" + esc(noteText) + "</span>" : "") +
        (convert ? "<span class='pm-convert'>" + convert + "</span>" : "") + "</div>"
      : "";
    var modNoticeHtml = ppModNoticeHtml(s, module, index);
    return "<section class='pm-module" + (locked ? " pm-locked" : "") + (isRealtime ? " pm-realtime" : "") +
      (isDynamic ? " pm-dynamic-mod" : "") + (isSystemDyn ? " pm-sysdyn" : "") + (isPlugin ? " pm-plugin" : "") +
      "' data-pm-module='" + index + "'>" +
      "<div class='pm-module-head'><span class='pm-idx'>" + (index + 1) + "</span>" +
        "<label class='pm-enabled'><input type='checkbox' data-pm-field='enabled' data-pm-index='" + index + "'" +
        (module.enabled !== false ? " checked" : "") + ">启用</label>" +
        "<div class='pm-head-main'>" +
        (editableTitle
          ? "<input class='pm-label' aria-label='模块名称' data-pm-field='label' data-pm-index='" + index + "'" +
            " value='" + esc(module.label || "") + "'>"
          : "<span class='pm-title' title='" + esc(module.label || "") + "'>" + esc(module.label || "") + "</span>" +
            ((headDesc && !hasSelect) ? "<span class='pm-desc' title='" + esc(headDesc) + "'>" + esc(headDesc) + "</span>" : "")) +
        "</div>" +
        // 用户 m05124 第 1 条：下拉列只在真有「模块来源」可选时占位；身份 / 工具 / 实时窗 /
        // 动态段 这些钉死来源的段，那一格留给标题和描述，别空一块把字挤成「勾选…」。
        (rtLive ? "<div class='pm-pick pm-pick-rt'>" + rtPick + "</div>"
                : (picker ? "<div class='pm-pick'>" + picker + "</div>" : "")) +
        "<div class='pm-end'>" +
        (posLabel ? "<span class='pm-pos " + posCls + "' title='这一段注入在框架钉好的位置上'>" + posLabel + "</span>"
                  : "<span class='pm-pos pm-pos-none'>·</span>") +
        "<span class='pm-chars' title='正文 " + esc(chars) + " 字符'>" + esc(chars) + " 字</span>" +
        "<div class='pm-move'>" + ppIcon(s,"up","\u2191","上移",index,
          fixedPos || index === 0 ||
          ((isDynamic || isPluginCtx) && index > 0 && String((s.layout.modules[index - 1] || {}).type || "") === "realtime_marker")) +
        ppIcon(s,"down","\u2193","下移",index,fixedPos || nextIsRt || index === s.layout.modules.length - 1) + (locked
          ? (lockText ? "<span class='pm-lock' title='" + esc(lockTip) + "'>" + lockText + "</span>" : "")
          : ppIcon(s,"remove","\u2715","删除",index) + ppIcon(s,"dup","\u29c9","复制一段出来",index)) + "</div></div></div>" +
      modNoticeHtml +
      metaHtml +
      (isTools ? ppToolBox(s, module, index) :
      "<details class='pm-body'" + (open ? " open" : "") + " data-pm-open='" + index + "'>" +
        "<summary class='pm-sum'>" + (isPersona ? "人格正文 " : "正文 ") + esc(chars) + " 字符" +
        // 用户 m10866：动态段那句「拼在聊天记录最后」删掉，位置标签已经说明清楚。
        (isDynamic ? "（动态段 · 可编辑）"
          : (isRealtime ? "（真实实时窗 · 只读）"
            : (isPlugin ? "（插件注入 · 只读，位置可挪）"
              : (custom ? "（可编辑）" : (isPersona ? "（只读 · 换人设请用右上角的下拉）"
                : (ro ? "（框架自动填写，只能看不能改）" : "（引用原文，只读）")))))) + "</summary>" +
        "<div class='pm-body-row'>" +
        // 用户 m23323 图3：<textarea> 后面紧跟的第一个换行会被 HTML 解析吃掉，
        // 正文以空行开头时回填一次就少一行、越编辑越少。这里先自己补一个换行。
        "<textarea class='darea pm-content' rows='5' aria-label='" +
        (custom ? "模块文本"
          : (isPersona ? "人格正文（只读）"
            : (isIdentity ? "你的身份（只读）"
              : (isSystemMem ? "记忆段（只读）"
                : (isSystemTime ? "时间段（只读）"
                  : (isSystemDyn ? "系统动态段（只读）"
                    : (isRealtime ? "实时窗口说明"
                      : (isPlugin ? (isPluginMem ? "记忆插件注入（只读）" : "记忆插件 · 本轮上下文（只读）")
                        : "改这里会自动存成你自己的模板")))))))) +
        "' data-pm-field='content' data-pm-index='" + index + "'" +
        (rtLive ? " readonly" : (custom || isRealtime || isDynamic ? "" : (ro ? " readonly" : " data-pm-promote='1'"))) + ">\n" + esc(body) + "</textarea>" +
        // 用户 m05124 图1：这句「只标位置」挪到上面第二行（正文摘要）去了，正文行里不再重复。
        "</div>" +
        (isRealtime ? (rtLive
          ? ("<div class='pm-hint'>这里显示的是框架里那个会话的真实窗口内容，只读：上面的「读取」可以换会话。" +
            "测试窗只留「用户发的纯文字」，bot 自己的话、工具往返、图片都过滤掉了，只取最近 10 条。" +
            (s.kind === "jev" ? "点下面的「测试」就用它跑一次 JEV 判断。" : "点下面的「测试」用的也是它。") +
            "它不会写进提示词，保存的布局里也没有这段正文。</div>")
          : "<div class='pm-hint'>这段只是给模型标出「聊天记录长这样、放在这里」，不会当成指令执行；想更像真记录就往下多写两行。</div>") : "") +
        (isPersona ? "<div class='pm-hint'>人格正文只能看不能改：要动人设，去人设文件里改，或者用右上角的下拉换一份。在这里敲字不会被存下来。</div>" : "") +
        (!custom && !ro && !isRealtime ? "<div class='pm-hint'>直接在这里改：改完自动存成一份「" + esc(String(module.label || "这一段")) +
          "·复制（自写）」，算你自己的；默认模板不会被改。</div>" : "") +
        foot + "</details>") +

      (!custom && !isPersona && !isTools && ref && ref.available === false && !body && ref.template ?
        "<details class='pseg pm-reference'><summary><span class='psn'>未渲染的引用模板（只读参考）</span></summary>" +
        "<div class='pm-note'>尚未绑定本轮变量，不是当前生效词；不会自动转为正文。可复制到新建自写模块后自行处理。</div>" +
        "<textarea class='darea pm-reference-text' rows='6' readonly aria-label='未渲染的引用模板（只读）'>\n" +
        esc(ppReferenceValue(ref.template)) + "</textarea></details>" : "") +
      "</section>";
  }
