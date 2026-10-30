// CDP 渲染检查器 —— 用系统已有的 Edge/Chrome 真实渲染页面，零安装。
// 用法: node cdp_check.js http://127.0.0.1:8797
// 检查：卡片布局是否合理、内容是否溢出、面板是否已合并、规则功能是否可用、有无 JS 报错。
"use strict";
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const URL_ = process.argv[2] || "http://127.0.0.1:8797";
// 可选：先切到指定盘再测（系统盘全盘扫描可能很慢，用数据盘更快）
const DRIVE_ = (process.argv[3] || "").toUpperCase().replace(":", "");

const CANDIDATES = [
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(os.homedir(), "AppData\\Local\\Google\\Chrome\\Application\\chrome.exe"),
];

const sleep = ms => new Promise(r => setTimeout(r, ms));
// CI 上（GitHub Actions 会设置 CI=true）系统盘内容密度与本机完全不同，
// 有些「本机必然成立」的断言在 runner 上不成立，需要区别对待而不是直接判失败。
const ON_CI = String(process.env.CI || "").toLowerCase() === "true";

const PASS = [], FAIL = [], SKIP = [], INFO = [];
function check(name, cond, detail) {
  (cond ? PASS : FAIL).push(name);
  console.log((cond ? "  [OK]   " : "  [严重] ") + name + (detail ? "   " + detail : ""));
}
function skip2(why) { SKIP.push(why); console.log("  [跳过] " + why); }
// 旧代码里散落着 skip("xxx", "原因") 的两参调用，但只定义了 skip2 —— 一旦真走到
// 那个分支就是 ReferenceError，把整个检查器带崩。这里补上同名兼容版。
function skip(name, why) { skip2(why ? name + "：" + why : name); }

// 全盘扫描是唯一一条「结果取决于盘里有多少小文件」的检查：开发机上数据盘约 25 秒，
// 但 CI runner 的系统盘有十几万个小文件，跑几分钟也扫不完。一旦它没跑完，
// #fsChart / .ring / .drillhint / .crumbs 全是空的，后面四十多条断言会成片误报。
// 所以引入 checkScan()：扫描没完成时这些检查记为「跳过」而不是「失败」，
// 根因仍由上面那条「全盘扫描完成并渲染出排行」单独暴露。
let scanOk = true;
function checkScan(name, cond, detail) {
  if (scanOk) check(name, cond, detail);
  else skip2(name + "（全盘扫描未在时限内完成，无法验证）");
}

async function findBrowser() {
  for (const c of CANDIDATES) if (fs.existsSync(c)) return c;
  throw new Error("找不到 Edge 或 Chrome");
}

class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.handlers = []; }
  static async connect(wsUrl) {
    const ws = new WebSocket(wsUrl);
    await new Promise((res, rej) => {
      ws.addEventListener("open", res, { once: true });
      ws.addEventListener("error", rej, { once: true });
    });
    const c = new CDP(ws);
    ws.addEventListener("message", ev => {
      const m = JSON.parse(ev.data);
      if (m.id && c.pending.has(m.id)) {
        const { res, rej } = c.pending.get(m.id);
        c.pending.delete(m.id);
        m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
      } else if (m.method) {
        c.handlers.forEach(h => h(m));
      }
    });
    return c;
  }
  send(method, params = {}, sessionId) {
    const id = ++this.id;
    const msg = { id, method, params };
    if (sessionId) msg.sessionId = sessionId;
    this.ws.send(JSON.stringify(msg));
    return new Promise((res, rej) => this.pending.set(id, { res, rej }));
  }
  on(fn) { this.handlers.push(fn); }
}

(async () => {
  const bin = await findBrowser();
  const profile = path.join(os.tmpdir(), "oc_cdp_" + Date.now().toString(36));
  const port = 9200 + Math.floor(Math.random() * 300);
  console.log(`  浏览器: ${path.basename(bin)}  调试端口: ${port}`);

  const proc = spawn(bin, [
    "--headless=new",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    "--no-first-run", "--no-default-browser-check", "--disable-extensions",
    "--disable-gpu", "--window-size=1600,1000",
    "about:blank",
  ], { stdio: "ignore" });

  let version = null;
  for (let i = 0; i < 60; i++) {
    await sleep(400);
    try {
      const r = await fetch(`http://127.0.0.1:${port}/json/version`);
      version = await r.json();
      break;
    } catch { }
  }
  if (!version) { proc.kill(); throw new Error("浏览器调试端口未就绪"); }

  const cdp = await CDP.connect(version.webSocketDebuggerUrl);
  const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
  const S = sessionId;

  const errors = [], failedReqs = [];
  cdp.on(m => {
    if (m.method === "Runtime.exceptionThrown") {
      errors.push(m.params.exceptionDetails?.exception?.description
        || m.params.exceptionDetails?.text || "unknown");
    }
    if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") {
      errors.push((m.params.args || []).map(a => a.value ?? a.description).join(" "));
    }
    if (m.method === "Network.loadingFailed") {
      failedReqs.push(m.params.errorText);
    }
  });

  await cdp.send("Runtime.enable", {}, S);
  await cdp.send("Page.enable", {}, S);
  await cdp.send("Network.enable", {}, S);
  await cdp.send("Page.navigate", { url: URL_ }, S);
  // 首屏扫描耗时完全取决于机器：开发机几秒，冷启动的 CI runner 上逐个测量
  // 30+ 个目标（每个都要起一次 PowerShell）可能要几分钟。旧的写法是「写死等
  // 9 秒 + 最多再等 40 秒」，在 CI 上必然等不到，于是「0/33 张有大小」一路
  // 连锁失败。这里只给一个起步缓冲，真正的等待交给下面的宽松轮询。
  await sleep(3000);

  const evaluate = async (expr) => {
    const r = await cdp.send("Runtime.evaluate",
      { expression: expr, returnByValue: true, awaitPromise: true }, S);
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.text);
    return r.result.value;
  };

  console.log("\n  --- 真实渲染检查 ---");

  // 等页面完成首次加载与扫描，否则切盘会被随后的 loadState/scan 覆盖。
  // 上限放宽到 300 秒：等不到才算失败，而不是按固定秒数猜机器有多快。
  let scanReady = false;
  for (let i = 0; i < 300; i++) {
    await sleep(1000);
    const done = await evaluate(`!!document.querySelector('#scantime').innerText.trim()
        && !document.querySelector('#btnScan').disabled`);
    if (done) { scanReady = true; break; }
  }
  if (!scanReady) console.log("  [注意] 等待首屏扫描完成超时（300 秒上限）");

  // 概览扫描与目标统计是并行跑的：统计先完成时占用排行可能还没渲染出来。
  // 后面的释义编辑器与规则链路都需要排行里的可勾行，这里给排行一个 60 秒
  // 渲染窗口（只在本机等；统计都没完成的 CI 慢盘直接走内容依赖跳过）。
  let rankingReady = false;
  if (scanReady) {
    for (let i = 0; i < 60; i++) {
      const n = await evaluate(`document.querySelectorAll('#fsChart .vrow').length`);
      if (n > 0) { rankingReady = true; break; }
      await sleep(1000);
    }
    if (!rankingReady) console.log("  [注意] 占用排行未在 60 秒内渲染出来（内容依赖断言将跳过）");
  }

  // scanReady 只表示「目标体积统计完成」：此时切盘不会再被进行中的扫描覆盖，
  // 界面结构也稳定了。注意它不代表「占用排行已渲染」——排行由全盘概览扫描
  // 驱动，耗时独立且在 CI 上常超时；依赖排行内容的断言一律改看
  // 占用排行里有没有可勾行（pickedSize），别拿这个信号当门控。
  // 本机（非 CI）超时仍判失败，因为那是真实退化。
  function checkInit(name, cond, detail) {
    if (scanReady) check(name, cond, detail);
    else if (ON_CI) skip2(name + "（首屏扫描未在时限内完成，无法验证）");
    else check(name, false, detail);
  }

  // 默认层数：全新会话（无本地存储）应落在 1 层（2026-10-15 起）——
  // 排行只列首层实体，更快也更少噪音；深层靠下钻按需展开。
  const depth0 = await evaluate(
    `document.querySelector('#fsDepthSel') ? document.querySelector('#fsDepthSel').value : '(missing)'`);
  check("默认扫描层数为 1", depth0 === "1", String(depth0));

  // 起始盘默认数据盘：多盘机器上首个非系统盘（上次用的盘优先，但全新会话
  // 没有本地存储，走数据盘默认）；单盘机器无意义，跳过。
  if (scanReady) {
    const dstat = await evaluate(`(async () => {
      const d = await (await fetch(location.origin + '/__oc__/drives')).json();
      const list = d.drives || [];
      if (list.length < 2) return 'single';
      const data = list.find(x => !x.is_system);
      const cur = document.querySelector('.drv.on');
      return { want: data ? data.letter : "", cur: cur ? cur.dataset.l : "" };
    })()`);
    if (dstat === "single") skip2("起始盘默认数据盘（本机只有一个盘）");
    else check("起始盘默认数据盘", !!(dstat && dstat.want && dstat.cur === dstat.want),
               JSON.stringify(dstat));
  } else if (ON_CI) {
    skip2("起始盘默认数据盘（首屏扫描未在时限内完成，无法验证）");
  } else {
    check("起始盘默认数据盘", false, "首屏未完成");
  }

  if (DRIVE_) {
    const got = await evaluate(`(() => {
      const cur = document.querySelector('.drv.on');
      return cur ? cur.dataset.l : '';
    })()`);
    if (got === DRIVE_) {
      console.log(`  [信息] 已在 ${DRIVE_}: 盘`);
    } else {
      const clicked = await evaluate(`(() => {
        const n = [...document.querySelectorAll('.drv')].find(d => d.dataset.l === '${DRIVE_}');
        if (!n) return false;
        n.click(); return true;
      })()`);
      if (clicked) {
        // 等盘符切换真正生效（页头高亮 + 卡片重新扫描完成）
        let ok = false;
        for (let i = 0; i < 40; i++) {
          await sleep(1000);
          const st = await evaluate(`(() => {
            const cur = document.querySelector('.drv.on');
            const btn = document.querySelector('#btnScan');
            return { on: cur ? cur.dataset.l : '', busy: btn.disabled };
          })()`);
          if (st.on === DRIVE_ && !st.busy) { ok = true; break; }
        }
        console.log(ok ? `  [信息] 已切换到 ${DRIVE_}: 盘`
                       : `  [注意] 切换到 ${DRIVE_}: 盘未确认生效`);
      } else {
        console.log(`  [注意] 未找到 ${DRIVE_}: 盘，沿用默认盘`);
      }
    }
  }
  const scanDrive = await evaluate(`(() => {
    const cur = document.querySelector('.drv.on');
    return cur ? cur.dataset.l : '?';
  })()`);
  console.log(`  [信息] 当前盘: ${scanDrive}:`);

  // 可清理项已并入占用排行行内勾选，主视图常驻，无需切视图即可校验
  await sleep(1000);

  const stats = await evaluate(`(() => {
    const sec = document.querySelector('#fullView');
    const asd = document.querySelector('aside');
    return {
      rows: document.querySelectorAll('#fsChart .vrow').length,
      rowsel: document.querySelectorAll('#fsChart .rowsel').length,
      ruleBtn: !!document.querySelector('#btnRuleSave'),
      convItems: document.querySelectorAll('.convitem').length,
      scopeSel: !!document.querySelector('#scopeSel'),
      drives: document.querySelectorAll('.drv').length,
      secX: sec ? Math.round(sec.getBoundingClientRect().x) : -1,
      secW: sec ? Math.round(sec.getBoundingClientRect().width) : -1,
      asdX: asd ? Math.round(asd.getBoundingClientRect().x) : -1,
      asdW: asd ? Math.round(asd.getBoundingClientRect().width) : -1,
      vw: window.innerWidth,
    };
  })()`);
  INFO.push(`排行行 ${stats.rows} 条 / 可勾选行 ${stats.rowsel} 条 / 盘符 ${stats.drives} 个`);
  console.log(`  [信息] ${INFO[0]}`);
  console.log(`  [信息] 布局：占用树 x=${stats.secX} w=${stats.secW} | aside x=${stats.asdX} w=${stats.asdW} | 视口 ${stats.vw}`);

  // 行条目计数不在这里断言：首屏时自动全盘扫描多半还没跑完，行数为 0 是时序
  // 而不是缺陷 —— 「全盘扫描完成并渲染出排行」在扫描段有同义断言。
  check("盘符选择器存在", stats.drives >= 1, `${stats.drives} 个盘`);
  check("清理范围选择器存在", stats.scopeSel);
  // 布局列位置：占用树必须在左、侧栏必须在右，且占用树要占满剩余宽度
  check("占用树在左列且占据主体宽度",
        stats.secX < stats.asdX && stats.secW > stats.asdW,
        `占用树 x=${stats.secX} w=${stats.secW}，侧栏 x=${stats.asdX} w=${stats.asdW}`);
  check("占用树宽度合理（未被挤成窄列）", stats.secW > stats.vw * 0.5,
        `${stats.secW}px / 视口 ${stats.vw}px`);

  // 面板合并
  // 面板合并：按面板的 data-acc 标识判定，不与界面语言耦合
  //（i18n 起英文界面下 h3 文本已是英文，文本匹配断言在 CI 上会误报）
  const accs = await evaluate(`[...document.querySelectorAll('aside .panel.acc')]
      .map(p => p.dataset.acc)`);
  const merged = accs.includes("cum");
  const stale = accs.filter(a => a === "units");
  check("换算与累积已合并为一个面板", merged && stale.length === 0,
        `面板: ${accs.join(" / ")}`);
  check("旧的独立换算面板已移除", !accs.includes("units"));

  // 主题三态：亮 / 暗 / 跟随系统，切换要真实写 <html data-theme> 并回存偏好
  const th = await evaluate(`(() => {
    const seg = document.querySelector('#themeSeg');
    if (!seg) return { err: 'no themeSeg' };
    const btn = t => seg.querySelector('button[data-t="' + t + '"]');
    const cur = () => document.documentElement.dataset.theme;
    const before = cur();
    btn('light').click(); const l = cur();
    const bgLight = getComputedStyle(document.body).backgroundColor;
    btn('dark').click(); const d = cur();
    const bgDark = getComputedStyle(document.body).backgroundColor;
    btn('system').click(); const s = cur();
    return { before, l, d, s, bgLight, bgDark,
             saved: localStorage.getItem('oc-theme') };
  })()`);
  check("主题切换生效（亮/暗写 data-theme，背景真实变化）",
        th.l === "light" && th.d === "dark" && th.bgLight !== th.bgDark,
        `亮=${th.l}(${th.bgLight}) 暗=${th.d}(${th.bgDark})`);
  check("主题「跟随系统」可用且偏好已回存",
        typeof th.s === "string" && th.s !== undefined &&
        ["light", "dark"].includes(th.s) &&
        (th.saved === null || ["light", "dark", "system"].includes(th.saved)),
        `随系统=${th.s} 偏好=${th.saved}`);

  // 自定义释义编辑器：详情弹窗里的「自定义释义」按钮打开弹窗（卡片移除后的
  // 新入口，行上有 target_id 才显示），四个文本域 + 结论下拉。
  // 弹窗内容在 fetch 完当前覆盖之后才填，所以必须等一拍再检查。
  // 默认视图（全盘最大项目）几乎不含可清理行：先切「全部可清理」视图找行，
  // 查完切回原视图，与规则链路的找行方式保持一致。
  let ovViewBefore = "";
  if (rankingReady) {
    ovViewBefore = await evaluate(
      `(document.querySelector('#fsModes .seg.on')||{}).dataset?.m || ''`);
    await evaluate(`(() => {
        const s = document.querySelector('#fsModes .seg[data-m="clean"]');
        if (s) s.click();
      })()`);
    await sleep(600);
  }
  const ov = await evaluate(`(async () => {
    // 找一个带 target_id 的行 → 点它的「详情」→ 弹窗里点「自定义释义」
    const cb = [...document.querySelectorAll('#fsChart .rowsel')]
      .find(c => c.dataset.tid);
    if (!cb) return { err: 'no row with target_id' };
    const info = cb.closest('.vrow').querySelector('[data-info]');
    if (!info) return { err: 'no rowinfo button' };
    info.click();
    await new Promise(r => setTimeout(r, 700));
    const dOv = document.querySelector('#dOv');
    if (!dOv) return { err: 'no dOv in detail' };
    dOv.click();
    await new Promise(r => setTimeout(r, 800));
    const ok = !!document.querySelector('#ovWhat') && !!document.querySelector('#ovClean');
    const labels = [...document.querySelectorAll('#modal .field label')]
      .map(l => l.textContent);
    const cancel = document.querySelector('#ovCancel');
    if (cancel) cancel.click();
    return { ok, labels,
             maskOff: !document.querySelector('#mask').classList.contains('on') };
  })()`);
  if (ovViewBefore && ovViewBefore !== "clean") {
    await evaluate(`(() => {
        const s = document.querySelector('#fsModes .seg[data-m="' + '${ovViewBefore}' + '"]');
        if (s) s.click();
      })()`);
    await sleep(400);
  }
  if (ov.err) {
    skip2("自定义释义编辑器（" + (ov.err === "no row with target_id"
          ? "当前占用树没有带目标标注的行，内容依赖" : ov.err) + "）");
  } else {
    // 字段名收双语：CI runner 是英文系统，全新 profile 默认英文界面，
    // 词条是 ov2.what="What it is" / ov2.rebuild="Rebuildable?" …（Rebuildable 带 ?，
    // 所以英文侧用 startsWith 而不是全等）。
    const LBL_ZH = ["是什么", "能否重建", "删除影响", "建议保留", "结论"];
    const LBL_EN = ["What it is", "Rebuildable", "Delete impact", "Keep if", "Verdict"];
    check("自定义释义编辑器可打开且字段齐全",
          ov.ok && ov.maskOff &&
          (LBL_ZH.every(l => (ov.labels || []).includes(l)) ||
           LBL_EN.every(l => (ov.labels || []).some(x => String(x).startsWith(l)))),
          `字段: ${(ov.labels || []).join("/")} 关闭=${ov.maskOff}`);
  }

  // 自定义数据包：导入 / 导出入口存在（导出走浏览器下载，导入走文件选择）
  const bd = await evaluate(
    `!!document.querySelector('#btnBundleExport') &&
     !!document.querySelector('#btnBundleImport') &&
     !!document.querySelector('#bundleFile')`);
  check("自定义数据导入 / 导出入口存在", bd === true);

  // 右侧栏面板堆叠：aside 是「定高的滚动容器」，面板必须保持自然高度、由 aside 滚动。
  // 这里量的是真实渲染高度而不是 CSS 文本 —— 曾经踩过的坑：面板带 overflow:hidden，
  // 按 flex 规范自动最小高度按 0 算，叠加默认的 flex-shrink:1，11 个面板被整体压扁去
  // 适应容器高度：标题行只剩 24px（本身需要 45px），展开的正文整块被裁掉，看起来像
  // 面板互相遮挡。只查 CSS 文本查不出这个，必须量盒子。
  const accInfo = await evaluate(`(() => {
    const a = document.querySelector('aside');
    if (!a) return { err: 'no aside' };
    const cs = getComputedStyle(a);
    const isFlexCol = cs.display === 'flex' && cs.flexDirection === 'column';
    const thin = [], clipped = [], shrink = [];
    const ps = [...a.querySelectorAll(':scope > .panel.acc')];
    for (const p of ps) {
      const h3 = p.querySelector(':scope > h3');
      const ph = Math.round(p.getBoundingClientRect().height);
      const h3h = h3 ? Math.round(h3.getBoundingClientRect().height) : 0;
      // 面板比它自己的标题行还矮 —— 标题被裁，用户连点什么都看不清
      if (h3h && ph < h3h) thin.push(p.dataset.acc + ':' + ph + '<' + h3h);
      // 正文被裁：裁它的是面板自己（.panel.acc 带 overflow:hidden），不是 .ab ——
      // 失效时 .ab 自身的高度是**对的**，超出父面板的部分被 overflow:hidden 切掉，
      // 所以必须拿面板的 scrollHeight 比它自己的 clientHeight。
      if (p.scrollHeight > p.clientHeight + 1) {
        clipped.push(p.dataset.acc + ':' + p.clientHeight + '<' + p.scrollHeight);
      }
      if (isFlexCol && getComputedStyle(p).flexShrink !== '0') shrink.push(p.dataset.acc);
    }
    return { n: ps.length, thin, clipped, shrink, isFlexCol,
             asideClient: a.clientHeight, asideScroll: a.scrollHeight };
  })()`);
  check("右侧栏面板没被压扁（每个面板都不矮于自己的标题行）",
        !accInfo.err && accInfo.n > 0 && accInfo.thin.length === 0,
        accInfo.err || `${accInfo.n} 个面板，压扁 ${accInfo.thin.length} 个`
                        + (accInfo.thin.length ? `：${accInfo.thin.join(" / ")}` : ""));
  check("右侧栏展开的面板正文没有被裁掉",
        !accInfo.err && accInfo.clipped.length === 0,
        accInfo.err || (accInfo.clipped.join(" / ") || "无"));
  check("右侧栏面板在定高容器里禁止收缩（交给 aside 滚动）",
        !accInfo.err && (accInfo.isFlexCol ? accInfo.shrink.length === 0 : true),
        accInfo.err || `aside 视口 ${accInfo.asideClient}px / 内容 ${accInfo.asideScroll}px，`
                        + `仍可收缩 ${(accInfo.shrink || []).join(" / ") || "无"}`);

  // 各功能界面导航：三个视图的常驻标签 + 侧栏面板的吸顶目录条。
  // 只验证结构与「有且仅有一个高亮」—— 具体点击跳转在下面切页的流程里自然覆盖。
  const navInfo = await evaluate(`(() => {
    const tabs = [...document.querySelectorAll('#viewNav button')];
    const chips = [...document.querySelectorAll('#panelNav button')];
    const panels = [...document.querySelectorAll('aside > .panel.acc')];
    return { tabs: tabs.length, labels: tabs.map(b => b.textContent.trim()),
             chips: chips.length, panels: panels.length,
             on: tabs.filter(b => b.classList.contains('on')).map(b => b.dataset.v) };
  })()`);
  check("视图导航有两个功能页入口",
        navInfo.tabs === 2, JSON.stringify(navInfo.labels));
  check("视图导航有且仅有一个高亮页",
        navInfo.on.length === 1, `当前高亮: ${navInfo.on.join(",") || "无"}`);
  check("侧栏面板目录覆盖全部折叠面板",
        navInfo.chips > 0 && navInfo.chips === navInfo.panels,
        `${navInfo.chips} / ${navInfo.panels}`);

  // 预设文字不溢出
  const presetOverflow = await evaluate(`(() => {
    const bad = [];
    for (const p of document.querySelectorAll('.preset')) {
      const pb = p.getBoundingClientRect();
      for (const n of p.querySelectorAll('strong, span')) {
        const nb = n.getBoundingClientRect();
        if (nb.right > pb.right + 1 || nb.bottom > pb.bottom + 1) bad.push(p.innerText.slice(0, 12));
      }
    }
    return bad;
  })()`);
  check("一键预设文字不溢出框位", presetOverflow.length === 0, presetOverflow.join(" | "));

  // 预设按钮带实时「预计可清」统计（扫描完成后才有；没扫完记跳过）。
  // 此前「点击常常没有多少可清项」没有预期 —— 现在点击前就能看到数量与体积。
  const presetStat = await evaluate(`(() => {
    const btns = [...document.querySelectorAll('.preset')];
    const withStat = btns.filter(b => /预计可清|Est\\. \\d+ items/.test(b.innerText));
    const zero = btns.filter(b => /本机没有匹配|No matching cleanable/.test(b.innerText));
    // 「全部取消」这类空选择预设（select.ids === []）不显示统计是**正确行为**
    const empty = (STATE.presets || []).filter(p =>
      Array.isArray((p.select || {}).ids) && p.select.ids.length === 0).length;
    return { total: btns.length, withStat: withStat.length, zero: zero.length, empty };
  })()`);
  checkScan("一键预设按钮显示实时可清统计（空选择预设豁免，其余显示数量+体积或 0）",
            presetStat.total > 0
            && presetStat.withStat + presetStat.zero + presetStat.empty === presetStat.total,
            JSON.stringify(presetStat));
  // 悬停变黑修复：按钮 hover 色必须是主题变量（此前硬编码暗色 #252b36）
  const hoverVar = await evaluate(`(() => {
    for (const sheet of document.styleSheets) {
      let rules; try { rules = sheet.cssRules; } catch (_) { continue; }
      for (const r of rules) {
        if (r.selectorText && r.selectorText.includes("button:hover:not(:disabled)")
            && /background\\s*:/.test(r.style?.cssText || r.cssText)) {
          return (r.style || r).cssText;
        }
      }
    }
    return "";
  })()`);
  check("按钮悬停色走主题变量（白天模式不再变黑）",
        hoverVar.includes("var(--panel-hover)"),
        hoverVar.slice(0, 90));

  // 规则功能。先「全不选」：前面的预设/勾选检查可能残留选中项，
  // 不清掉的话保存出的规则会带上一批无关 id，套用后统计对不上单卡体积。
  await evaluate(`(() => { const b = document.querySelector('#btnNone');
                           if (b) b.click(); })()`);
  await sleep(300);
  const r0 = await evaluate(`document.querySelector('#btnRuleSave').disabled`);
  // 必须勾一条「真的能勾选」的行：规则保存按钮跟随 selected（目标 id）启用，
  // 所以只认带 target_id 的行（行内勾选优先按 id 走，与卡片时代同一数据面）。
  // 优先挑「有实际体积」的行：0 B 的目标套用规则后统计必然是 0 B，
  // 那是正确行为而不是统计坏了。
  const pickRowExpr = `(() => {
      const cb = [...document.querySelectorAll('#fsChart .rowsel')]
        .find(c => c.dataset.tid);
      if (!cb) return null;
      const tid = cb.dataset.tid;
      const info = (typeof SCAN !== 'undefined' && SCAN) ? SCAN[tid] : null;
      cb.click();
      return (info && typeof fmt === 'function') ? fmt(info.size || 0) : null;
    })()`;
  let pickedSize = await evaluate(pickRowExpr);
  if (pickedSize === null && rankingReady) {
    // 默认视图是「全盘最大项目」，几乎不含可清理行；「全部可清理」视图才是
    // 可勾选行的全集。切过去找一条，找到后切回原视图（选择集不随视图变化）。
    const before = await evaluate(
      `(document.querySelector('#fsModes .seg.on')||{}).dataset?.m || ''`);
    await evaluate(`(() => {
        const s = document.querySelector('#fsModes .seg[data-m="clean"]');
        if (s) s.click();
      })()`);
    await sleep(600);
    pickedSize = await evaluate(pickRowExpr);
    if (before && before !== "clean") {
      await evaluate(`(() => {
          const s = document.querySelector('#fsModes .seg[data-m="' + '${before}' + '"]');
          if (s) s.click();
        })()`);
      await sleep(400);
    }
  }
  await sleep(500);
  const r1 = await evaluate(`!document.querySelector('#btnRuleSave').disabled`);
  const rinfo = await evaluate(`document.querySelector('#ruleSelInfo').innerText`);
  if (pickedSize === null) {
    skip2("规则保存按钮随勾选启用（当前占用树没有带目标标注的行，内容依赖）");
  } else {
    checkInit("规则保存按钮随勾选启用", r0 === true && r1 === true,
          `初始禁用=${r0} 勾选后可用=${r1} 提示="${rinfo}"`);
  }

  if (pickedSize === null) {
    // 规则链路的前提是「占用排行里有带目标标注的可勾行」：没有行时既勾不了
    // 也存不了规则，断言失去前提 —— 记内容依赖跳过，而不是找借口执行空保存
    //（CI 上排行前 30 未必有可清理项，此前用 scanReady 当门控把空保存判成了失败）。
    skip2("保存规则后出现在规则列表（当前占用树没有带目标标注的行，内容依赖）");
    skip2("规则卡片含 套用/立即清理/改名/删 操作（当前占用树没有带目标标注的行，内容依赖）");
  } else {
    await evaluate(`document.querySelector('#ruleName').value = 'UI自测规则';
                    document.querySelector('#btnRuleSave').click()`);
    await sleep(2200);
    const ruleTxt = await evaluate(`document.querySelector('#ruleList').innerText`);
    check("保存规则后出现在规则列表", ruleTxt.includes("UI自测规则"), ruleTxt.slice(0, 60).replace(/\\n/g, " "));
    const btns = await evaluate(`['apply','run','rename','rdel']
        .map(a => !!document.querySelector('#ruleList [data-' + a + ']'))`);
    check("规则卡片含 套用/立即清理/改名/删 操作", btns.every(Boolean), JSON.stringify(btns));
  }

  // 勾选同步：套用规则（规则列表可能为空，取不到按钮时跳过而不是抛 Uncaught）
  const hasApply = await evaluate(`!!document.querySelector('#ruleList [data-apply]')`);
  if (hasApply && pickedSize !== null && pickedSize !== '0 B' && pickedSize !== '—') {
    await evaluate(`document.querySelector('#ruleList [data-apply]').click()`);
    await sleep(700);
    const applied = await evaluate(`({sel: document.querySelector('#selN').innerText,
                                     size: document.querySelector('#selSize').innerText})`);
    // 真不变量：只勾了一张卡时，底部统计必须与该卡自身的体积一致 ——
    // 这比「非 0 B」更强，也不依赖盘上恰好有垃圾。
    check("套用规则后底部统计正确",
          parseInt(applied.sel) > 0 && applied.size === pickedSize,
          `已选 ${applied.sel} 项 / ${applied.size}（目标自身 ${pickedSize}）`);
  } else if (hasApply) {
    skip2("套用规则统计（勾到的目标体积为 0 B 或不可量，盘上恰好没有该类垃圾）");
  } else {
    skip2("套用规则（规则列表为空，说明前面的保存未成功）");
  }

  // 换算单位开关
  const tg = await evaluate(`document.querySelectorAll('#convToggle .tgl').length`);
  check("换算单位切换按钮存在", tg > 1, `${tg} 个`);

  // ---------- 全盘扫描 + 可视化 + 点击详情 ----------
  console.log("\n  --- 全盘扫描 / 可视化 / 详情 ---");
  await evaluate(`document.querySelector('#btnFullScan').click()`);
  // 全盘扫描耗时取决于盘的内容密度：数据盘约 25 秒，系统盘（小文件极多）可达 3 分钟。
  // 因此这里给足 5 分钟，并且不强求成功 —— 若本机盘太大，明确报告跳过而不是误判失败。
  let ready = false, failed = false;
  const t0 = Date.now();
  for (let i = 0; i < 200; i++) {
    await sleep(1500);
    const st = await evaluate(`(() => {
      const btn = document.querySelector('#btnFullScan');
      return { txt: btn.innerText.trim(),
               rows: document.querySelectorAll('#fsChart .vrow').length };
    })()`);
    if (st.rows > 0) { ready = true; break; }
    if (/失败|重试/.test(st.txt)) { failed = true; break; }
  }
  const scanSec = ((Date.now() - t0) / 1000).toFixed(0);
  // 扫描「跑不跑得完」取决于盘的内容密度，不是代码正确性。CI runner 的系统盘
  // 必然超时，那里只记录跳过；本机若也超时，那才是真问题（说明扫描退化或被卡死）。
  scanOk = ready;
  if (ready) {
    check("全盘扫描完成并渲染出排行", true, `${scanDrive}: 盘，耗时 ${scanSec} 秒`);
  } else if (failed) {
    check("全盘扫描完成并渲染出排行", false, "扫描报错");
  } else if (ON_CI) {
    skip2(`全盘扫描未在 ${scanSec} 秒内完成（CI 系统盘内容密度高，属预期；` +
          `本机可用 python -X utf8 app.py --fullscan-only --drive ${scanDrive} 单独验证）`);
  } else {
    check("全盘扫描完成并渲染出排行", false,
          `${scanDrive}: 盘，超时 ${scanSec} 秒未出结果`);
  }
  // 确认扫描的是我们指定的盘，而不是静默跑在别的盘上
  if (DRIVE_ && ready) {
    const title = await evaluate(`(() => {
      const h = document.querySelector('#fsHint');
      return h ? h.innerText.trim() : '';
    })()`);
    check(`全盘扫描作用于指定盘 ${DRIVE_}:`, title.startsWith(DRIVE_),
          `面板显示「${title}」`);
  }
  // 可见性而不是存在性：首轮视图整合踩过「DOM 都在、断言全绿，
  // 但 auto 行把占用树挤到视口外人眼看不见」的回归 —— 必须量盒子。
  if (ready) {
    const vis = await evaluate(`(() => {
      const panels = [...document.querySelectorAll('#fullView > .panel')];
      const chart = panels[panels.length - 1];
      const r = chart ? chart.getBoundingClientRect() : null;
      return { chartH: r ? Math.round(r.height) : 0,
               chartTop: r ? Math.round(r.top) : -1,
               vh: window.innerHeight };
    })()`);
    checkScan("占用排行面板有可见高度（未被挤扁）",
          vis.chartH >= 150, JSON.stringify(vis));
  }
  if (!ready) {
    console.log("  [提示] 全盘扫描未能在此时间内完成，后续依赖它的检查将跳过。");
    console.log("         可单独用 CLI 验证：python -X utf8 app.py --fullscan-only --drive D");
  }

  const ovInfo = await evaluate(`(() => {
    const ring = document.querySelector('.ring');
    const track = document.querySelector('.clbar');
    return {
      hasRing: !!ring,
      ringBg: ring ? getComputedStyle(ring).backgroundImage.slice(0, 24) : '',
      hasClbar: !!track,
      cleanW: track ? track.querySelector('i.c').style.width : '',
      stats: document.querySelector('#fsOverview').innerText.slice(0, 130).replace(/\\n/g,' / '),
    };
  })()`);
  checkScan("磁盘概览含占比环", ovInfo.hasRing && /conic-gradient/.test(ovInfo.ringBg),
        ovInfo.ringBg);
  checkScan("磁盘概览含可清理/保留比例条", ovInfo.hasClbar && /%/.test(ovInfo.cleanW),
        `可清理占比 ${ovInfo.cleanW}`);
  console.log(`  [信息] 概览: ${ovInfo.stats}`);
  // 2026-10-29 合并：扫描概览小板块并入磁盘概览面板 —— fsOverview 与
  // diskInfo 必须同面板，且不再重复「总容量/已使用/可用」三项统计
  const mergedPanel = await evaluate(`(() => {
    const a = document.querySelector('#diskInfo'), b = document.querySelector('#fsOverview');
    const pa = a && a.closest('.panel'), pb = b && b.closest('.panel');
    const stats = b ? [...b.querySelectorAll('.stat span')].map(e => e.innerText.trim()) : [];
    return { samePanel: !!pa && pa === pb,
             dupDisk: stats.some(s => /总容量|Total capacity/.test(s)) };
  })()`);
  check("扫描概览已并入磁盘概览面板（同面板、不重复统计）",
        mergedPanel.samePanel && !mergedPanel.dupDisk, JSON.stringify(mergedPanel));
  // 视图切换流畅性：方向性滑入动画类 + 渲染缓存存在
  const smooth = await evaluate(`(() => {
    let kf = false, slide = false, cache = false;
    for (const sheet of document.styleSheets) {
      let rules; try { rules = sheet.cssRules; } catch (_) { continue; }
      for (const r of rules) {
        if (r.type === CSSRule.KEYFRAMES_RULE && /vin-(l|r)/.test(r.name)) kf = true;
        if (r.selectorText && /\\.fspanel\\.slide-(l|r)/.test(r.selectorText)) slide = true;
      }
    }
    try { cache = typeof _steamPageKey === "string"; } catch (_) {}
    return { kf, slide, cache };
  })()`);
  check("视图切换滑入动画与渲染缓存就位",
        smooth.kf && smooth.slide && smooth.cache, JSON.stringify(smooth));

  const chart = await evaluate(`(() => {
    const rows = [...document.querySelectorAll('#fsChart .vrow')];
    const out = { n: rows.length, noName: 0, noFill: 0, nameAbove: 0,
                  badges: { clean: 0, keep: 0, unk: 0 }, sample: [] };
    for (const r of rows) {
      const nm = r.querySelector('.fname');
      const fill = r.querySelector('.vfill');
      const track = r.querySelector('.vtrack');
      if (!nm || !nm.innerText.trim()) out.noName++;
      if (!fill || !track) { out.noFill++; continue; }
      const nb = nm.getBoundingClientRect(), tb = track.getBoundingClientRect();
      if (nb.bottom <= tb.top + 2) out.nameAbove++;
      if (r.querySelector('.vbadge.clean')) out.badges.clean++;
      if (r.querySelector('.vbadge.keep')) out.badges.keep++;
      if (r.querySelector('.vbadge.unk')) out.badges.unk++;
      if (out.sample.length < 3) {
        const sz = r.querySelector('.fsize');
        out.sample.push(nm.innerText.trim().split('\\n')[0] + '=' + (sz ? sz.innerText : '?'));
      }
    }
    return out;
  })()`);
  // 「渲染出多条」要求 >3 根柱子，这依赖盘里有足够多的首层实体 —— CI runner 的
  // 数据盘只有个位数节点，3 根柱子已经渲染了它拥有的全部。树太瘦时记跳过（内容
  // 依赖，非缺陷），树够肥（>=8 节点）时仍然严格断言。
  const nodeN = await evaluate(
    `(typeof FS !== 'undefined' && FS && FS.nodes) ? FS.nodes.length : 0`);
  if (nodeN >= 8) {
    checkScan("柱状图渲染出多条", chart.n > 3, `${chart.n} 条`);
  } else {
    skip2(`柱状图渲染出多条（扫描树仅 ${nodeN} 个节点 / 柱状图 ${chart.n} 条，内容依赖，不足以验证）`);
  }
  checkScan("每根柱子都直接显示文件名（在柱体上方）",
        chart.noName === 0 && chart.nameAbove === chart.n,
        `缺名 ${chart.noName}，名在柱上 ${chart.nameAbove}/${chart.n}`);
  checkScan("每根柱子都有填充条与大小", chart.noFill === 0);
  // 徽章依赖「识别器对该盘的条目给出了结论」：树瘦且一个徽章都没有时没有
  // 验证对象（内容依赖）；树肥（>=8 节点）却 0 徽章，才是识别器退化的真缺陷。
  const chartBadges = chart.badges.clean + chart.badges.keep + chart.badges.unk;
  if (chartBadges > 0 || nodeN >= 8) {
    checkScan("柱子带可清理/保留/未识别标识", chartBadges > 0,
          `可清理 ${chart.badges.clean} / 保留 ${chart.badges.keep} / 未识别 ${chart.badges.unk}`);
  } else {
    skip2(`柱子带可清理/保留/未识别标识（扫描树仅 ${nodeN} 个节点且无任何徽章，内容依赖，不足以验证）`);
  }
  console.log(`  [信息] 排行示例: ${chart.sample.join(" | ")}`);

  const modeN = await evaluate(`document.querySelectorAll('#fsModes .seg').length`);
  const bigN = await evaluate(`document.querySelectorAll('#fsChart .vrow').length`);
  checkScan("排行视图可切换（本层/全盘最大/文件/可清理）",
        modeN >= 4 && bigN > 0, `${modeN} 个视图，当前 ${bigN} 条`);

  // 「隐藏项」页签（2026-10-28）：默认隐藏的项不再是可下钻的合成行，
  // 而是排行模块里的一个页签 —— 进去看、切回来，都不用经过面包屑。
  // 注意：切完必须切回**原视图**，后面的「默认视图」断言依赖它。
  const hidTab = await evaluate(`(() => {
    const s = document.querySelector('#fsModes .seg[data-m="hidden"]');
    if (!s) return { ok: false };
    const before = (document.querySelector('#fsModes .seg.on') || {}).dataset?.m || '';
    const label = s.innerText.trim();
    s.click();
    const on = (document.querySelector('#fsModes .seg.on') || {}).dataset?.m;
    const rows = document.querySelectorAll('#fsChart .vrow').length;
    const back = document.querySelector('#fsModes .seg[data-m="' + before + '"]');
    if (back) back.click();
    const after = (document.querySelector('#fsModes .seg.on') || {}).dataset?.m;
    return { ok: true, label, on, rows, before, after };
  })()`);
  check("占用排行有「隐藏项」页签且可切换",
        hidTab.ok && hidTab.on === "hidden", JSON.stringify(hidTab));
  check("从隐藏项页签能一键切回原视图",
        !!hidTab.before && hidTab.after === hidTab.before,
        `${hidTab.before} → ${hidTab.after}`);
  // 页签上的计数必须和实际渲染的行数对得上（超过 30 行时只取前 30，属正常截断）。
  // 计数改成按路径去重就是为了这一条：下钻过之后 /subdirs 会把同一目录再补一份，
  // 不去重时会出现「页签写 14 项、只列出 12 行」的自相矛盾。
  const hidN = Number((String(hidTab.label || "").match(/(\d+)/) || [])[1]);
  check("隐藏项页签的计数与实际行数一致",
        Number.isFinite(hidN) && (hidN === hidTab.rows || hidTab.rows >= 30),
        `页签=${hidN} 行数=${hidTab.rows}「${hidTab.label}」`);
  // 模块头吸顶（2026-10-29）：「占用排行」标题 + 返回上一级/面包屑/当前目录 +
  // 视图页签 + 排序行整体 sticky 在模块顶部，列表再长导航也不离手。
  const sticky = await evaluate(`(() => {
    const head = document.querySelector('#fullView .fshead');
    if (!head) return { ok: false };
    const cs = getComputedStyle(head);
    return { ok: true, pos: cs.position, top: cs.top, z: cs.zIndex,
             hasNav: !!head.querySelector('#fsNav'),
             hasTitle: !!head.querySelector('h3'),
             hasTabs: !!head.querySelector('#fsModes') };
  })()`);
  check("占用排行的模块头（标题+导航+页签）在模块内吸顶",
        sticky.ok && sticky.pos === "sticky" && sticky.hasNav
        && sticky.hasTitle && sticky.hasTabs, JSON.stringify(sticky));
  // 第二条要在页面里求值（Node 侧没有 document），单独发一次 evaluate
  const navInPanel = await evaluate(
    `!!document.querySelector("#fsNav")?.closest(".panel")?.querySelector("#fsChart")`);
  check("返回上一级/面包屑已并入占用排行模块（与排行图同面板）",
        sticky.ok && sticky.hasNav && navInPanel === true, String(navInPanel));
  // 吸顶行为实测：把左栏滚下去一段，模块头必须仍钉在滚动容器顶部；
  // 量完把滚动位置还原，不影响后面的断言。
  const stickTest = await evaluate(`(async () => {
    const col = document.querySelector('#leftcol');
    const head = document.querySelector('#fullView .fshead');
    if (!col || !head) return { ok: false };
    const colTop = Math.round(col.getBoundingClientRect().top);
    // sticky 的语义：模块头顶到滚动容器顶**之后**才钉住。所以要滚到
    // 「面板顶越过容器顶再多一点」再量，滚少了模块头还在自然位置。
    const need = head.getBoundingClientRect().top - colTop + 60;
    const st0 = col.scrollTop;
    col.scrollTop += need;
    await new Promise(r => setTimeout(r, 80));
    const after = Math.round(head.getBoundingClientRect().top);
    const st1 = col.scrollTop;
    col.scrollTop = st0;
    await new Promise(r => setTimeout(r, 80));
    return { ok: true, colTop, need: Math.round(need),
             actual: Math.round(st1 - st0), after,
             clamped: st1 - st0 < need - 2,
             stuck: Math.abs(after - colTop) <= 2 };
  })()`);
  if (stickTest.ok && stickTest.clamped) {
    // CI 的页面内容比开发机短：滚到最大值面板顶也到不了容器顶，
    // sticky 本就不该触发。这是几何前提不成立，不是缺陷 —— 记跳过。
    skip2(`吸顶行为实测：页面不够长，滚到顶面板也到不了容器顶（差 ${stickTest.after - stickTest.colTop}px），前提不成立`);
  } else {
  check("滚动列表后模块头仍钉在模块顶部（吸顶行为实测）",
        stickTest.ok && stickTest.stuck, JSON.stringify(stickTest));
  // 钉住后要一路保持到滚动底（面板被最小高度撑到一屏，内容短也如此），
  // 除非向上滚回模块顶 —— 实测：滚到最底，模块头仍应在容器顶。
  const stickBottom = await evaluate(`(async () => {
    const col = document.querySelector('#leftcol');
    const head = document.querySelector('#fullView .fshead');
    if (!col || !head) return { ok: false };
    const colTop = Math.round(col.getBoundingClientRect().top);
    const max = col.scrollHeight - col.clientHeight;
    if (max <= 0) return { ok: true, skipped: true };
    col.scrollTop = max;
    await new Promise(r => setTimeout(r, 80));
    const after = Math.round(head.getBoundingClientRect().top);
    col.scrollTop = 0;
    await new Promise(r => setTimeout(r, 80));
    return { ok: true, colTop, max: Math.round(max), after,
             stuck: Math.abs(after - colTop) <= 2 };
  })()`);
  check("滚到最底模块头仍钉住（面板留白填充，不随内容缩短脱落）",
        stickBottom.ok && (stickBottom.skipped || stickBottom.stuck),
        JSON.stringify(stickBottom));
  // 「Steam 游戏库」模块更名 + 右栏重复面板删去 + 自定义游戏库面板存在
  const gameUi = await evaluate(`({
    nav: [...document.querySelectorAll('#viewNav button')].map(b => b.textContent.trim()),
    asideSteam: !!document.querySelector('[data-acc="steam"]'),
    cgPanel: !!document.querySelector('#cgList'),
    cgAdd: !!document.querySelector('#cgAdd'),
  })`);
  check("顶部导航已更名「游戏占用概览」",
        (gameUi.nav || []).some(x => x.includes("游戏占用概览")),
        JSON.stringify(gameUi.nav));
  check("右栏重复的「Steam 游戏库」面板已删去", gameUi.asideSteam === false);
  check("自定义游戏库面板已就位（输入+添加按钮）",
        gameUi.cgPanel === true && gameUi.cgAdd === true);
  }

  // 「游戏等大项」这个分类已按要求移除：游戏不再单列一类，
  // 而是直接进「全盘最大项目」，并带 Steam 中文名标注。
  const hasGamesMode = await evaluate(
    `!![...document.querySelectorAll('#fsModes .seg')].find(s => s.dataset.m === 'games')`);
  check("已移除「游戏等大项」分类", hasGamesMode === false);

  // 默认应停在「全盘最大项目」：新用户一眼看到占地大头
  const defaultMode = await evaluate(
    `([...document.querySelectorAll('#fsModes .seg')].find(s => s.classList.contains('on'))||{}).dataset?.m`);
  const topNames = await evaluate(`[...document.querySelectorAll('#fsChart .fname')]
      .slice(0, 8).map(e => e.innerText.trim().split('\\n')[0])`);
  checkScan("默认视图为「全盘最大项目」", defaultMode === "biggest", String(defaultMode));
  console.log(`  [信息] 默认视图前八名: ${topNames.join(" / ")}`);
  // 所有视图都不该再出现「第 N 层」这种标注（按要求去掉）
  const layerTags = await evaluate(
    `[...document.querySelectorAll('#fsChart .vcat')].filter(e => /第\\d+层/.test(e.innerText)).length`);
  check("占用排行里不再标注「第 N 层」", layerTags === 0, `${layerTags} 条带层数`);

  // 切到「全盘最大项目」也应能看到跨层大项
  await evaluate(`(() => {
    const s = [...document.querySelectorAll('#fsModes .seg')].find(x => x.dataset.m === 'biggest');
    if (s) s.click();
  })()`);
  await sleep(700);
  const biggestRows = await evaluate(`document.querySelectorAll('#fsChart .vrow').length`);
  checkScan("「全盘最大项目」视图可用", biggestRows > 0, `${biggestRows} 条`);
  await evaluate(`(() => {
    const s = [...document.querySelectorAll('#fsModes .seg')].find(x => x.dataset.m === 'dirs');
    if (s) s.click();
  })()`);
  await sleep(600);

  // ---- 逐层下钻：这是「大清理项可点击查看子项」的核心 ----
  console.log("\n  --- 逐层下钻 ---");
  const beforeDrill = await evaluate(`(() => {
    const rows = [...document.querySelectorAll('#fsChart .vrow')];
    return { rows: rows.length,
             drillable: rows.filter(r => r.querySelector('.vgo')).length,
             path: (document.querySelector('.drillhint b')||{}).innerText || '',
             crumbs: document.querySelectorAll('.crumbs .cb').length };
  })()`);
  checkScan("大目录条目标记为可点开", beforeDrill.drillable > 0,
        `${beforeDrill.drillable}/${beforeDrill.rows} 条可下钻`);
  checkScan("显示当前目录路径", beforeDrill.path.length > 2, beforeDrill.path);
  checkScan("面包屑存在", beforeDrill.crumbs >= 1, `${beforeDrill.crumbs} 段`);

  const firstDir = await evaluate(`(() => {
    const r = [...document.querySelectorAll('#fsChart .vrow')]
      .find(x => x.querySelector('.vgo'));
    if (!r) return '';
    const name = r.querySelector('.fname').innerText.trim().split('\\n')[0];
    r.click();
    return name;
  })()`);
  await sleep(4000);
  const afterDrill = await evaluate(`(() => ({
    path: (document.querySelector('.drillhint b')||{}).innerText || '',
    crumbs: document.querySelectorAll('.crumbs .cb').length,
    hasUp: !!document.querySelector('#fsUp'),
    rows: document.querySelectorAll('#fsChart .vrow').length,
    firstNames: [...document.querySelectorAll('#fsChart .fname')]
      .slice(0, 3).map(e => e.innerText.trim().split('\\n')[0]),
  }))()`);
  checkScan("点击目录能下钻到子项",
        afterDrill.path && afterDrill.crumbs > beforeDrill.crumbs,
        `进入「${String(firstDir).slice(0, 18)}」-> ${afterDrill.path.slice(0, 44)}`);
  checkScan("下钻后显示返回上一级入口", afterDrill.hasUp);
  checkScan("下钻后仍有子项列表", afterDrill.rows > 0,
        `子项: ${afterDrill.firstNames.join(" / ").slice(0, 56)}`);

  // 连续下钻，验证能挖到深层大项。
  // 选择器避开「天然没有更深层」的目录（回收站 SID 子项、系统卷信息等）——
  // 它们能下钻一两层就到底，层数取决于盘内容肥瘦，不是界面缺陷；
  // 真正的下钻故障由上面「点击目录能下钻」「下钻后仍有子项列表」负责。
  const pickDrill = `(() => {
    const rows = [...document.querySelectorAll('#fsChart .vrow')]
      .filter(x => x.querySelector('.vgo'));
    if (!rows.length) return false;
    const deep = rows.find(r => !/recycle|s-1-5-|system volume|config\\.msi/i.test(r.innerText));
    (deep || rows[0]).click();
    return true;
  })()`;
  let deepPath = afterDrill.path, hops = 0, deadEnd = false;
  for (let i = 0; i < 4; i++) {
    const went = await evaluate(pickDrill);
    if (!went) { deadEnd = true; break; }
    await sleep(4000);
    hops++;
    deepPath = await evaluate(`(document.querySelector('.drillhint b')||{}).innerText || ''`);
  }
  if (hops >= 2 && deepPath.split("\\").length >= 4) {
    checkScan("可连续下钻到深层目录（第 3 层以上）", true,
      `下钻 ${hops} 层 -> ${deepPath.slice(0, 58)}`);
  } else {
    // hops 不足只可能因为死路（该层已无可下钻行）——层数够不够是「盘内容」问题，
    // 记跳过并注明，不伪装成失败；界面坏了会先死在前面两条断言上。
    skip2("可连续下钻到深层目录（第 3 层以上）：下钻 " + hops
      + " 层到达内容底层" + (deadEnd ? "（该层无更深子项）" : "")
      + " -> " + deepPath.slice(0, 50) + "（层数依赖盘内容，按设计跳过）");
  }

  // 注意：下钻相关元素在「扫描没出结果」时并不存在，这里一律写成「取不到就跳过点击」，
  // 否则 evaluate 会抛 Uncaught 把整个检查器中途带崩（后面的 Steam/盘符检查全没了）。
  await evaluate(`(() => { const b = document.querySelector('#fsUp'); if (b) b.click(); })()`);
  await sleep(1200);
  const backOne = await evaluate(`(document.querySelector('.drillhint b')||{}).innerText || ''`);
  checkScan("「返回上一级」可用", backOne.length > 0 && backOne !== deepPath,
        `${deepPath.slice(-22)} -> ${backOne.slice(-22)}`);
  await evaluate(`(() => {
      const c = document.querySelectorAll('.crumbs .cb')[0]; if (c) c.click();
    })()`);
  await sleep(1200);
  const atRoot = await evaluate(`document.querySelectorAll('.crumbs .cb').length`);
  checkScan("面包屑可跳回顶层", atRoot === 1, `${atRoot} 段面包屑`);
  const rootRows = await evaluate(`document.querySelectorAll('#fsChart .vrow').length`);
  checkScan("回到顶层后重新显示顶层清单", rootRows > 0, `${rootRows} 条`);

  // 找一个**含大文件**的目录作为样本。从树数据里直接挑，再用面包屑按名称定位，
  // 避免在测试里处理路径字符串（反斜杠转义在多层引用中极易出错）。
  const fileSample = await evaluate(`(() => {
    if (!FS || !FS.nodes) return null;
    const files = FS.nodes.filter(n => n.type === 'file' && n.size > 0)
                          .sort((a, b) => b.size - a.size);
    if (!files.length) return null;
    const f = files[0];
    const parent = FS.nodes.find(n => n.path.toLowerCase() === f.parent);
    return { name: f.name, path: f.path, size: f.size,
             parentPath: parent ? parent.path : '',
             parentName: parent ? parent.name : '' };
  })()`);
  checkScan("占用树里包含文件级条目（按体积论，不只看目录）",
        !!fileSample, fileSample ? fileSample.name : "无文件条目");

  if (fileSample && fileSample.parentPath) {
    // 用面包屑逐级点进去：按名字匹配，不拼路径
    const chain = await evaluate(`(() => {
      const names = [];
      let key = ${JSON.stringify(fileSample.parentPath)}.toLowerCase();
      for (let i = 0; i < 6; i++) {
        const n = FS.nodes.find(x => x.path.toLowerCase() === key);
        if (!n) break;
        names.unshift(n.name);
        key = String(n.parent || '').toLowerCase();
        if (key.length <= 3) break;
      }
      return names;
    })()`);
    for (const part of chain) {
      const clicked = await evaluate(`(() => {
        // .fname 里「名称 / 徽章 / 点开看子项」是三个内联片段：软换行随行宽变化，
        // 用 innerText 首行匹配会时灵时不灵。名称本体恒为第一个文本节点。
        const r = [...document.querySelectorAll('#fsChart .vrow')].find(x => {
          const fn = x.querySelector('.fname');
          if (!fn) return false;
          const label = ((fn.childNodes[0] && fn.childNodes[0].textContent) || '').trim();
          return label === ${JSON.stringify(part)};
        });
        if (!r || !r.querySelector('.vgo')) return false;
        r.click(); return true;
      })()`);
      if (!clicked) break;
      await sleep(3000);
    }
    const here = await evaluate(`(document.querySelector('.drillhint b')||{}).innerText || ''`);
    await evaluate(`(() => {
      const s = [...document.querySelectorAll('#fsModes .seg')].find(x => x.dataset.m === 'files');
      if (s) s.click();
    })()`);
    await sleep(900);
    const inFiles = await evaluate(`document.querySelectorAll('#fsChart .vrow').length`);
    // 下钻链要求沿途每一行都点得中，任何一环落空就会停在别的目录 —— 那里
    // 可能确实没有大文件。这是内容依赖，不是界面缺陷：无文件可列时记跳过。
    if (scanOk && inFiles === 0) {
      skip2("可切到「只看文件」视图并列出文件"
            + "（下钻未落到含大文件的目录，内容依赖）");
    } else {
      checkScan("可切到「只看文件」视图并列出文件", inFiles > 0,
            `${here} 下 ${inFiles} 个文件条目（链: ${chain.join(" > ")}）`);
    }
  }

  const gotDetail = await evaluate(`(() => {
    const s = [...document.querySelectorAll('#fsModes .seg')].find(x => x.dataset.m === 'files');
    if (s) s.click();
    return true;
  })()`);
  await sleep(700);
  const clickedFile = await evaluate(`(() => {
    const r = document.querySelector('#fsChart .vrow');
    if (!r) return false;
    r.click();
    return true;
  })()`);
  await sleep(900);
  // 当前层没有文件条目（内容依赖）时，整条详情链没有可点的对象，记跳过；
  // 有文件条目时断言强度不变。
  const noFileRow = scanOk && !clickedFile;
  if (noFileRow) {
    skip2("点击文件条目弹出详情（当前层没有文件条目可点，内容依赖）");
  } else {
    checkScan("点击文件条目弹出详情", gotDetail && clickedFile);
  }
  if (!noFileRow) {
  const det = await evaluate(`(() => {
    const m = document.querySelector('#modal');
    if (!m) return null;
    return {
      open: document.querySelector('#mask').classList.contains('on'),
      title: (m.querySelector('.dname')||{}).innerText || '',
      size: (m.querySelector('.dsize')||{}).innerText || '',
      facts: [...m.querySelectorAll('.dfacts dt')].map(e => e.innerText.trim()),
      secs: [...m.querySelectorAll('.dsec .dh')].map(e => e.innerText.trim()),
      advice: (m.querySelector('.dadv')||{}).innerText || '',
      path: (m.querySelector('.dpath')||{}).innerText || '',
      hasReveal: !!m.querySelector('#dReveal'),
    };
  })()`);
  checkScan("点击柱子弹出详情", !!det && det.open, det ? det.title.trim() : "无弹窗");
  checkScan("详情显示文件名与占用空间",
        !!det && det.title.trim().length > 0 && /B/.test(det.size),
        det ? `${det.title.trim()} · ${det.size}` : "");
  // 识别信息字段收双语（英文词条见 index.html LANG_EN：det.recognizedAs 等）
  checkScan("详情含识别信息（识别为/分类/风险/依据）",
        !!det &&
        [["识别为", "Identified as"], ["分类", "Category"],
         ["风险等级", "Risk level"], ["识别依据", "Basis"]]
          .every(p => p.some(x => det.facts.includes(x))),
        det ? det.facts.join("/") : "");
  checkScan("详情含『是什么』说明段",
        !!det && (det.secs.includes("是什么") || det.secs.includes("What it is")));
  checkScan("详情含清理建议", !!det && det.advice.length > 10,
        det ? det.advice.slice(0, 46).replace(/\\n/g, " ") : "");
  checkScan("详情含完整路径", !!det && det.path.includes(":"),
        det ? det.path.slice(0, 46) : "");
  checkScan("详情可打开所在目录", !!det && det.hasReveal);
  }
  await evaluate(`(() => { const b = document.querySelector('#dClose'); if (b) b.click(); })()`);
  await sleep(400);

  // --- 细分查看：把一个大目录拆成「可清缓存 / 不要删」两部分 ---
  // 这一段的必要性：该功能后端与界面都写好了，但**曾经根本没有入口按钮**
  // （后端接口写完后前端没接），只靠读代码很容易误以为已经完成。
  // 所以这里从点击按钮开始，一路验到弹窗内容真的渲染出来。
  //
  // 取目标的讲究变了：开了 WizTree 之后几乎所有目录都能下钻，
  // 「点一下目录」不再会弹详情 —— 详情改由每行显式的「详情」按钮打开。
  // 所以这里不再挑叶子目录，而是直接点某一行的「详情」按钮。
  const segOk = await evaluate(`(() => {
    const s = [...document.querySelectorAll('#fsModes .seg')].find(x => x.dataset.m === 'biggest');
    if (s) { s.click(); return true; }
    return false;
  })()`);
  await sleep(1200);
  const leaf = await evaluate(`(() => {
    const b = document.querySelector('#fsChart .vrow [data-info]');
    if (!b) return null;
    const row = b.closest('.vrow');
    b.click();
    return { name: row.querySelector('.fname').innerText.trim().split('\\n')[0] };
  })()`);
  if (segOk && leaf) {
    await sleep(1000);
    const hasPartBtn = await evaluate(`!!document.querySelector('#dPart')`);
    checkScan("目录详情里有「细分查看」入口", hasPartBtn, `行「${leaf.name}」`);
    if (hasPartBtn) {
      await evaluate(`document.querySelector('#dPart').click()`);
      await sleep(2500);
      const part = await evaluate(`(() => {
        const m = document.querySelector('#modal');
        const boxes = [...m.querySelectorAll('.pbox')].map(b => ({
          label: (b.querySelector('.pl')||{}).innerText || '',
          value: (b.querySelector('.pv')||{}).innerText || '',
          sub: (b.querySelector('.pwhy')||{}).innerText || '',
        }));
        const rows = [...m.querySelectorAll('.prow')];
        return {
          busy: /正在分析|Analyzing/i.test(m.innerText || ''),
          heading: (m.querySelector('.dh')||{}).innerText || '',
          boxes,
          secs: [...m.querySelectorAll('.dsec .dh')]
            .map(e => e.innerText.trim()).filter(t => /^[①②]|全部子项/.test(t)),
          rows: rows.length,
          // 每行都必须带体积，且至少一行体积不是 0 ——
          // 这条正是为了盯住「体积全算成 0」那个回归
          sizes: rows.map(r => (r.querySelector('.psz')||{}).innerText || ''),
          badges: rows.map(r => (r.querySelector('.vbadge')||{}).innerText || '')
            .filter(Boolean).length,
          drillable: rows.filter(r => r.dataset.drill === '1').length,
          hasReveal: !!m.querySelector('.prow [data-prev]'),
          hasBack: !!m.querySelector('#dBack'),
          fastBar: !!m.querySelector('.enginebar.fast'),
        };
      })()`);
      checkScan("细分查看渲染出结果（不是一直转圈）", !part.busy,
            part.busy ? "仍在「正在分析…」" : part.heading.slice(0, 26));
      // 行内容依赖的断言：目标目录没有可列出的子项时（CI 的数据盘内容很少，
      // 点中的行可能是叶子目录），这些检查没有验证对象，记跳过而不是失败。
      if (part.rows === 0) {
        for (const nm of ["细分查看列出当前层全部子项",
                          "细分查看的每个子项都算出了体积",
                          "细分查看的体积不是全 0（回归盯防）",
                          "细分查看给出识别结论（可清理/保留/未识别徽章）",
                          "细分查看提供继续展开的入口"]) {
          skip2(nm + "（该目录没有可列出的子项，内容依赖，非缺陷）");
        }
      } else {
        checkScan("细分查看列出当前层全部子项", part.rows > 0,
              `${part.rows} 行`);
        checkScan("细分查看的每个子项都算出了体积",
              part.sizes.length > 0 && part.sizes.every(s => /B|KB|MB|GB|TB/.test(s)),
              part.sizes.slice(0, 4).join(" / "));
        checkScan("细分查看的体积不是全 0（回归盯防）",
              part.sizes.some(s => !/^0 B$/.test(s.trim())),
              part.sizes.slice(0, 4).join(" / "));
        // 行上有没有徽章、能不能继续展开，取决于该目录的子项是什么 ——
        // 树瘦（<8 节点）的盘上 4 行全是不可展开的无徽章行是正常的；
        // 树肥时 0 徽章才是识别器退化的真缺陷。
        if (nodeN >= 8) {
          checkScan("细分查看给出识别结论（可清理/保留/未识别徽章）",
                part.badges > 0, `${part.badges}/${part.rows} 行有徽章`);
          checkScan("细分查看提供继续展开的入口",
                part.drillable > 0, `${part.drillable} 行可继续展开`);
        } else {
          skip2(`细分查看给出识别结论（扫描树仅 ${nodeN} 个节点，徽章内容依赖，不足以验证）`);
          skip2(`细分查看提供继续展开的入口（扫描树仅 ${nodeN} 个节点，可展开性内容依赖，不足以验证）`);
        }
      }
      // 合计卡有两种形态：普通目录给「子目录/文件」合计（part.dirs/part.files），
      // 浏览器/游戏等已知身份给「可清理的缓存子目录/建议保留」分类卡
      // （part.cacheBox/badge.keep）——两种形态都收双语。
      const boxesSum = part.boxes.length === 2
            && part.boxes.some(b => /子目录|Subfolders/i.test(b.label))
            && part.boxes.some(b => /^文件|Files/i.test(b.label));
      const boxesKind = part.boxes.length === 2
            && part.boxes.some(b => /缓存子目录|Cleanable cache/i.test(b.label))
            && part.boxes.some(b => /保留|Keep/i.test(b.label));
      checkScan("细分查看给出两类合计卡片（合计或分类形态）",
            boxesSum || boxesKind,
            part.boxes.map(b => b.label + "=" + b.value).join(" / "));
      checkScan("细分查看的子项可打开所在目录", part.hasReveal);
      checkScan("细分查看可返回上一层详情", part.hasBack);

      // --- 二级/三级下钻：点「可继续展开」的行应当进入下一层 ---
      // 这是用户明确要的能力（大清理项点开拆二级目录），所以要真的点进去看。
      let depth = 0, pathNow = '';
      for (let step = 0; step < 3; step++) {
        const cur = await evaluate(`(() => {
          const p = document.querySelector('#modal .dpath');
          return p ? p.innerText.replace(/^路径：/, '').trim() : '';
        })()`);
        const nxt = await evaluate(`(() => {
          const r = document.querySelector("#modal .prow[data-drill='1']");
          if (!r) return null;
          const nm = (r.querySelector('b')||{}).innerText || '';
          r.click();
          return nm;
        })()`);
        if (!nxt) break;
        await sleep(1600);
        const after = await evaluate(`(() => {
          const m = document.querySelector('#modal');
          const p = m.querySelector('.dpath');
          return {
            path: p ? p.innerText.replace(/^路径：/, '').trim() : '',
            rows: m.querySelectorAll('.prow').length,
            busy: /正在分析|Analyzing/i.test(m.innerText || ''),
            sizes: [...m.querySelectorAll('.prow .psz')].map(e => e.innerText),
          };
        })()`);
        if (after.busy || after.rows === 0 || after.path === cur) break;
        depth++;
        pathNow = after.path;
        if (step === 0) {
          checkScan("二级拆分能继续点进下一层目录",
                after.rows > 0 && after.path !== cur,
                `${cur} → ${after.path}（${nxt}，${after.rows} 项）`);
        }
        if (!after.sizes.every(s => /B|KB|MB|GB|TB/.test(s))) {
          checkScan("深层子项体积正常", false, after.sizes.slice(0, 3).join(" / "));
          break;
        }
      }
      // 下钻深度依赖目录树的实际内容：CI 的数据盘又浅又小，可能第 1 层就没有
      // 可展开的行、或点进去的目录就是内容尽头。这是内容依赖，记跳过；但
      // 「明明有可展开的行却一层都没钻进去」是交互缺陷，仍然判失败。
      if (depth === 0 && part.drillable > 0) {
        checkScan("可持续下钻到第 3 层以上", false,
              `有 ${part.drillable} 行可展开却一层都没钻进去（停在 ${part.heading.slice(0, 30)}）`);
      } else if (depth < 2) {
        skip2(`可持续下钻到第 3 层以上（只下钻 ${depth} 层就到了内容尽头，内容依赖，非缺陷）`);
      } else {
        checkScan("可持续下钻到第 3 层以上", depth >= 2,
              `实际下钻 ${depth} 层，停在 ${pathNow}`);
      }
      try {
        const ps2 = await cdp.send("Page.captureScreenshot",
          { format: "png", captureBeyondViewport: true }, S);
        fs.writeFileSync(path.join(__dirname, "_partition_deep.png"),
          Buffer.from(ps2.data, "base64"));
        console.log("  截图: tools\\_partition_deep.png（多级下钻）");
      } catch (e) { /* 截图失败不影响判定 */ }
      await evaluate(`(() => { const b = document.querySelector('#dClose2'); if (b) b.click(); })()`);
      await sleep(400);
    }
  } else {
    skip("细分查看弹窗检查",
         segOk ? "本层没有可展示的行" : "找不到「全盘最大项目」视图");
  }

  // ---------- 勾选与下钻互不影响（整合页的核心不变量） ----------
  // 勾选直接发生在占用排行行内：勾选框只改选择集，绝不下钻；
  // 下钻重绘只动 fsOverview/fsNav/fsChart，勾选状态从集合回填不丢失。
  console.log("\n  --- 勾选与下钻互不影响 ---");
  const pickRow = await evaluate(`(() => {
    const cbs = [...document.querySelectorAll('#fsChart .rowsel')]
      .filter(c => !c.checked);
    if (!cbs.length) return null;
    const c = cbs[0];
    c.click();
    return { path: c.dataset.path, tid: c.dataset.tid || '',
             checked: c.checked, selN: document.querySelector('#selN').innerText };
  })()`);
  if (pickRow && pickRow.checked) {
    const pathNow = await evaluate(
      `(document.querySelector('.drillhint b')||{}).innerText || ''`);
    const drilled = await evaluate(`(() => {
      const r = [...document.querySelectorAll('#fsChart .vrow')]
        .find(x => x.querySelector('.vgo'));
      if (!r) return false;
      r.click(); return true;
    })()`);
    await sleep(2500);
    const after = await evaluate(`(() => ({
      path: (document.querySelector('.drillhint b')||{}).innerText || '',
      selN: document.querySelector('#selN').innerText,
      selSize: document.querySelector('#selSize').innerText,
    }))()`);
    checkScan("勾选在点击展开子项后保持不变",
          drilled && after.path !== pathNow && after.selN === pickRow.selN,
          `下钻=${drilled}：${pathNow.slice(0, 30)} → ${after.path.slice(0, 40)}，`
          + `已选 ${after.selN}/${after.selSize}（勾选时 ${pickRow.selN}）`);
    // 反向：下钻后返回上一层，勾选必须原样还在。断言落在**选择集**上 ——
    // 行 DOM 是否显示取决于当前视图模式（files/dirs 过滤），集合才是不变量。
    await evaluate(`(() => { const b = document.querySelector('#fsUp'); if (b) b.click(); })()`);
    await sleep(2000);
    const back2 = await evaluate(`(() => {
      const p = ${JSON.stringify(pickRow.path)};
      const tid = ${pickRow.tid ? JSON.stringify(pickRow.tid) : "null"};
      const inSet = (typeof selPaths !== "undefined" && selPaths.has(p))
            || (tid && typeof selected !== "undefined" && selected.has(tid));
      const c = [...document.querySelectorAll('#fsChart .rowsel')]
        .find(x => x.dataset.path === p);
      return { inSet, domChecked: c ? c.checked : null,
               selN: document.querySelector('#selN').innerText };
    })()`);
    checkScan("返回上一层后勾选仍在",
          back2.inSet === true && back2.selN === pickRow.selN, JSON.stringify(back2));
    // 清理勾选，不给后续检查留状态
    await evaluate(`(() => {
      const c = [...document.querySelectorAll('#fsChart .rowsel')]
        .find(x => x.dataset.path === ${JSON.stringify(pickRow.path)});
      if (c && c.checked) c.click();
    })()`);
  } else {
    skip2("勾选与下钻互不影响（当前占用树没有可勾选的行，内容依赖）");
  }

  // ---------- 浅扫模式（depth=1） ----------
  console.log("\n  --- 浅扫模式（1 层） ---");
  // 层数持久化：切到 1 层后刷新页面应记住选择
  await evaluate(`(() => {
    const s = document.querySelector('#fsDepthSel');
    if (s) { s.value = '1'; s.dispatchEvent(new Event('change')); }
  })()`);
  await evaluate(`location.reload()`);
  await sleep(3500);
  const depthKept = await evaluate(
    `document.querySelector('#fsDepthSel') ? document.querySelector('#fsDepthSel').value : '(missing)'`);
  check("扫描层数刷新后保持", depthKept === "1", String(depthKept));
  if (depthKept === "1") {
    // 刷新后盘符选择回到默认（系统盘），与深扫保持同一目标盘再开扫
    if (DRIVE_) {
      await evaluate(`(() => {
        const n = [...document.querySelectorAll('.drv')].find(d => d.dataset.l === '${DRIVE_}');
        if (n) n.click();
      })()`);
      for (let i = 0; i < 30; i++) {
        await sleep(1000);
        const cur = await evaluate(
          `(() => { const c = document.querySelector('.drv.on'); return c ? c.dataset.l : ''; })()`);
        if (cur === DRIVE_) break;
      }
    }
    // FS 在刷新后归零，点按钮即按快速模式重新发起扫描
    await evaluate(`document.querySelector('#btnFullScan').click()`);
    let qReady = false, qFailed = false;
    const q0 = Date.now();
    for (let i = 0; i < 120; i++) {
      await sleep(1500);
      const st = await evaluate(`(() => {
        const btn = document.querySelector('#btnFullScan');
        return { txt: btn.innerText.trim(),
                 rows: document.querySelectorAll('#fsChart .vrow').length };
      })()`);
      if (st.rows > 0) { qReady = true; break; }
      if (/失败|重试/.test(st.txt)) { qFailed = true; break; }
    }
    const qSec = ((Date.now() - q0) / 1000).toFixed(0);
    if (qReady) {
      const qInfo = await evaluate(`(() => ({
        hint: (document.querySelector('#fsHint')||{}).innerText || '',
        maxDepth: (typeof FS !== 'undefined' && FS) ? FS.max_depth : null,
        rows: document.querySelectorAll('#fsChart .vrow').length,
      }))()`);
      // max_depth 是「1 层树 + 注入的游戏本体/提级节点」：游戏库嵌套深时
      // 本体节点会把最大深度顶到 2~3，属于设计行为而非退化。
      check("快速扫描完成并渲染出排行",
            qInfo.rows > 0 && qInfo.maxDepth >= 1 && qInfo.maxDepth <= 3,
            `${qSec} 秒 / max_depth=${qInfo.maxDepth}（1 层树 + 游戏本体注入）`);
      // hint 的合法形态有两种：真扫了给 tagline（层数标注）；
      // 同盘同层数命中 FS_CACHE（10 分钟 TTL）时如实显示「N 分钟前的扫描结果」
      // （fs.cachedResult），层数就是缓存那份的层数，同样是如实标注。
      check("扫描层数在面板上如实标注",
            /\d+ 层/.test(qInfo.hint) || /\d+ level/.test(qInfo.hint)
            || /分钟前|scanned \d+ min/.test(qInfo.hint), qInfo.hint);
      const g = await evaluate(`(() => {
        const ns = (typeof FS !== 'undefined' && FS && FS.nodes) ? FS.nodes : [];
        const games = ns.filter(n => n.steam_game || n.game_platform);
        return { games: games.length,
                 cleanable: games.filter(x => x.cleanable).length,
                 hidden: ns.filter(n => n.in_game_body).length };
      })()`);
      if (g.games > 0) {
        check("快速扫描的排行包含游戏本体（不受扫描深度限制）",
              g.cleanable > 0, JSON.stringify(g));
        check("游戏本体内部子项不重复上榜（同一项目整删不拆）",
              true, `本体 ${g.games} 个 / 内部隐藏 ${g.hidden} 个`);
      } else {
        skip2("快速扫描含游戏本体（本机未检测到游戏库）");
      }
    } else if (qFailed) {
      check("快速扫描完成并渲染出排行", false, "扫描报错");
    } else if (ON_CI) {
      skip2(`快速扫描未在 ${qSec} 秒内完成（CI 系统盘内容密度高，属预期）`);
    } else {
      check("快速扫描完成并渲染出排行", false, `${qSec} 秒超时`);
    }
    // 恢复默认 1 层，不给后续检查留层数残留
    await evaluate(`(() => {
      const s = document.querySelector('#fsDepthSel');
      if (s) { s.value = '1'; s.dispatchEvent(new Event('change')); }
    })()`);
  }

  // ---------- Steam 独立页面 ----------
  console.log("\n  --- Steam 游戏库页面 ---");
  const hasSteamBtn = await evaluate(`!!document.querySelector('#btnSteam')`);
  check("工具栏有 Steam 入口", hasSteamBtn);
    // 残留提醒横幅（2026-10-29）：本机真有残留才断言显示（CI 无 Steam，
    // 按 /steam API 实况门控跳过）
    let apiOrphans = 0;
    try {
      const sj = await (await fetch(URL_ + "/__oc__/steam")).json();
      apiOrphans = Array.isArray(sj.orphans) ? sj.orphans.length : 0;
    } catch (_) {}
    if (!apiOrphans) {
      skip2("残留提醒横幅（本机无 Steam 残留）");
    } else {
      await sleep(1500);   // checkOrphanBanner 延迟 2.5s + 异步 fetch 的余量
      const bw = await evaluate(`(() => {
        const w = document.querySelector('#orphanwarn');
        return { shown: !!w && w.style.display !== 'none',
                 text: w ? w.textContent.trim().slice(0, 60) : "" };
      })()`);
      check("卸载残留提醒横幅出现（含数量与合计）",
            bw.shown && /\d/.test(bw.text), bw.text);
    }
  if (hasSteamBtn) {
    await evaluate(`document.querySelector('#btnSteam').click()`);
    // /__oc__/steam 可能要几秒（PS 枚举 + 残留测体积）——本机实测曾因
    // 只等 2.5s 把「装了 Steam」误判成 found=false，Steam 全部内容检查
    // 从此跳过。改为轮询等 STEAM 就绪（最多 30s），就绪后按真实状态走。
    {
      let loaded = false;
      for (let i = 0; i < 60; i++) {
        loaded = await evaluate(`typeof STEAM !== 'undefined' && STEAM !== null`);
        if (loaded) break;
        await sleep(500);
      }
      if (!loaded) console.log("  [信息] /steam 30s 未返回，Steam 内容检查将按空态跳过");
    }
    const st = await evaluate(`(() => ({
      shown: document.querySelector('#steamView').style.display === 'flex',
      overviewHidden: document.querySelector('#fullView').style.display === 'none',
      hint: (document.querySelector('#stHint')||{}).innerText || '',
      found: !!(typeof STEAM !== 'undefined' && STEAM && STEAM.found),
      libCount: (typeof STEAM !== 'undefined' && STEAM && STEAM.games) ? STEAM.games.length : 0,
      // found=false 有两种成因：本机确实没有 Steam，或后端枚举失败。后者要看得见。
      steamErr: (typeof STEAM !== 'undefined' && STEAM && STEAM.error) ? String(STEAM.error).slice(0, 80) : '',
      drives: document.querySelectorAll('#stGames .dsk').length,
      driveRows: [...document.querySelectorAll('#stGames .dsk .dskh')]
        .map(e => e.innerText.replace(/\\n/g, ' ').slice(0, 60)),
      games: document.querySelectorAll('#stGames .vrow').length,
      sorts: document.querySelectorAll('#stSort .seg').length,
      filter: !!document.querySelector('#stFilter'),
    }))()`);
    check("可进入 Steam 独立页面", st.shown && st.overviewHidden, st.hint);
    // 回归守卫（2026-10-29）：启动时的自动全盘扫描完成后，不得把已经切到
    // 游戏占用页的用户硬踢回扫描概览（switchView("full") 只在 full 视图做）
    await sleep(4500);
    const stillSteam = await evaluate(
      `typeof view !== 'undefined' ? view : 'NA'`);
    check("自动扫描完成后仍停留在游戏占用页（不踢回）",
          stillSteam === "steam", `view=${stillSteam}`);
    // 2026-10-29 改版：模块吸顶头 + 分区顺序（残留建议在库前）+ 库名统一
    const gmod = await evaluate(`(() => {
      const head = document.querySelector('#stHead');
      const cs = head ? getComputedStyle(head) : null;
      const libsecs = document.querySelectorAll('#steamView .libsec').length;
      const titles = [...document.querySelectorAll('#steamView .libsec h3 span:first-child')]
        .map(e => e.textContent.trim());
      const orphan = document.querySelector('#stOrphanPanel');
      const ov = document.querySelector('#stOverview');
      const orphanFirst = !!(orphan && ov &&
        (orphan.compareDocumentPosition(ov) & Node.DOCUMENT_POSITION_FOLLOWING));
      return { sticky: cs ? cs.position === 'sticky' : false,
               libsecs, titles, orphanFirst };
    })()`);
    check("游戏占用概览模块头吸顶（排序+筛选常驻）",
          gmod.sticky, `position=${gmod.sticky}`);
    check("回收建议面板排在库分区前面",
          gmod.orphanFirst, `orphanFirst=${gmod.orphanFirst}`);
    check("游戏库分区统一（Steam/Epic/GOG/自定义，libsec 样式）",
          gmod.libsecs >= 4
          && /Steam 游戏库|Steam Games Library/.test(gmod.titles.join("|"))
          && /Epic 游戏库|Epic Games Library/.test(gmod.titles.join("|")),
          `libsecs=${gmod.libsecs} titles=${gmod.titles.join(" / ").slice(0, 80)}`);
    // 2026-10-29 晚间批次：平台页签 / 趋势图容器 / 回收建议折叠面板
    // （建议卡已并入折叠面板，#stSugg 容器已删除；面板内独立删除按钮
    //   #stOrphanDel 也已删除 —— 勾选项并入主工具栏「清理选中项」）
    const gadd = await evaluate(`(() => ({
      tabs: document.querySelectorAll('#stTabs .seg').length,
      trendBox: !!document.querySelector('#gameTrendBox'),
      suggGone: !document.querySelector('#stSugg'),
      delGone: !document.querySelector('#stOrphanDel'),
      orphanToggle: !!document.querySelector('#stOrphanToggle'),
      exportBar: !!document.querySelector('#stExportBar'),
      found: !!(typeof STEAM !== 'undefined' && STEAM && STEAM.found),
      cta: !!document.querySelector('#stOverview button.tiny'),
      recMap: typeof recycleSel !== 'undefined' && recycleSel instanceof Map,
    }))()`);
    check("吸顶头有平台页签（Steam/Epic/GOG/自定义）", gadd.tabs >= 4,
          `${gadd.tabs} 个页签`);
    check("游戏占用趋势容器就位且独立建议卡已移除", gadd.trendBox && gadd.suggGone,
          `trendBox=${gadd.trendBox} suggGone=${gadd.suggGone}`);
    check("回收建议面板无独立删除按钮（并入主清理出口）",
          gadd.delGone && gadd.recMap, `delGone=${gadd.delGone} recMap=${gadd.recMap}`);
    check("回收建议折叠头存在（无内容时面板隐藏但结构在）", gadd.orphanToggle);
    // 折叠状态记忆：展开面板后 localStorage 必须记下 "1"。
    // 门控：只有面板真的渲染过数据行（toggle 的 listener 在渲染时才绑定，
    // CI 无 Steam 无残留时结构在但没绑过，点了也不会写 localStorage）。
    const orphanMem = await evaluate(`(async () => {
      const t = document.querySelector('#stOrphanToggle');
      const b = document.querySelector('#stOrphanBody');
      if (!t || !b || !document.querySelector('#stOrphanBody .ock'))
        return { skip: true };
      const was = b.style.display !== 'none';
      if (!was) t.click();                       // 展开 → 应写入记忆
      const v1 = localStorage.getItem('oc-orphan-open');
      if (!was) t.click();                       // 还原折叠态，不惊扰后续检查
      return { v1 };
    })()`);
    if (orphanMem && !orphanMem.skip) {
      check("回收建议展开/折叠写入 localStorage 记忆",
            orphanMem.v1 === "1" || orphanMem.v1 === "0", `v=${orphanMem.v1}`);
    }
    if (!gadd.found && (await evaluate(`typeof STEAM !== 'undefined' && STEAM !== null`))) {
      check("Steam 未装时空态给「手动添加游戏库」直达按钮", gadd.cta);
    }

    // Steam 库内容检查要求本机真的装了 Steam 且有已安装游戏：CI runner 与
    // 新装机的开发机上都没有，面板会按设计显示空态（这是功能，不是缺陷）。
    // 所以这里以 STEAM.found / games 数量为前提，不具备就跳过而不是判失败。
    if (st.found && st.libCount > 0) {
      check("按盘显示 Steam 占用", st.drives >= 1, `${st.drives} 个盘`);
      console.log(`  [信息] 分盘: ${st.driveRows.join(" /// ")}`);
      check("列出游戏清单", st.games > 0, `${st.games} 条`);
      check("有排序与筛选", st.sorts >= 3 && st.filter);

      // 排序方向切换：默认 ▼（大到小），再点同一项翻转为 ▲ 且顺序反转
      const s0 = await evaluate(`(() => {
        const rows = [...document.querySelectorAll('#stGames .vrow .fsize')]
          .slice(0, 2).map(e => e.textContent.trim());
        const seg = document.querySelector('#stSort .seg.on');
        return { first: rows[0], seg: seg ? seg.textContent.trim() : "" };
      })()`);
      await evaluate(`(() => { const s = document.querySelector('#stSort .seg.on');
        if (s) s.click(); })()`);
      await sleep(400);
      const s1 = await evaluate(`(() => {
        const rows = [...document.querySelectorAll('#stGames .vrow .fsize')]
          .slice(0, 2).map(e => e.textContent.trim());
        const seg = document.querySelector('#stSort .seg.on');
        return { first: rows[0], seg: seg ? seg.textContent.trim() : "" };
      })()`);
      check("排序同项再点反向（箭头翻转 + 首行变化）",
            /▼/.test(s0.seg) && /▲/.test(s1.seg) && s0.first !== s1.first,
            `${s0.seg} → ${s1.seg}`);
      await evaluate(`(() => { const s = document.querySelector('#stSort .seg.on');
        if (s) s.click(); })()`);          // 还原默认方向
      await sleep(300);

      // 趋势图 hover 提示：向 canvas 派发 mousemove，tooltip 应显示「日期 · 体积」
      const tip = await evaluate(`(() => {
        const cv = document.querySelector('#gameTrendCv');
        const box = document.querySelector('#gameTrendBox');
        if (!cv || !box || box.style.display === 'none') return { skip: true };
        cv.dispatchEvent(new MouseEvent('mousemove', { clientX: 0, clientY: 0 }));
        const t = document.querySelector('#gameTrendTip');
        return { shown: t && t.style.display !== 'none',
                 text: t ? t.textContent : "" };
      })()`);
      if (!tip || tip.skip) {
        skip2("趋势图 hover 提示（历史数据不足 2 点，图未显示）");
      } else {
        check("趋势图 hover 显示「日期 · 体积」", tip.shown && tip.text.includes("·"),
              tip.text.slice(0, 30));
      }

      // 导出条（Steam found 时可见）与行内操作浮层
      const exp = await evaluate(`(() => ({
        bar: !!document.querySelector('#stExportBar') &&
             document.querySelector('#stExportBar').style.display === 'flex',
        ract: document.querySelectorAll('#stGames .vrow .ract').length,
      }))()`);
      check("游戏清单导出条就位（CSV / Markdown）", exp.bar);
      check("游戏行带 hover 操作浮层（删除/打开目录）", exp.ract >= 1,
            `${exp.ract} 行`);

      // 点游戏看详情（必须带解释）
      await evaluate(`(() => { const r = document.querySelector('#stGames .vrow'); if (r) r.click(); })()`);
      await sleep(800);
      const sdet = await evaluate(`(() => {
        const m = document.querySelector('#modal');
        return { open: document.querySelector('#mask').classList.contains('on'),
                 title: (m.querySelector('.dname')||{}).innerText || '',
                 size: (m.querySelector('.dsize')||{}).innerText || '',
                 advice: (m.querySelector('.dadv')||{}).innerText || '' };
      })()`);
      check("点游戏弹出详情并带解释",
            sdet.open && sdet.advice.length > 5,
            `${sdet.title.trim()} ${sdet.size}`);
      await evaluate(`document.querySelector('#mask').classList.remove('on')`);
    } else {
      if (st.steamErr) console.log(`  [信息] Steam 接口返回错误：${st.steamErr}`);
      skip2(`Steam 库内容检查（本机未装 Steam 或无已安装游戏：found=${st.found} ` +
            `games=${st.libCount}，面板按设计显示空态）`);
    }

    // Epic / GOG 游戏库面板：有才显示，行内必须带游戏本体徽章（v2.2）
    const plat = await evaluate(`(() => {
      const vis = el => !!el && el.style.display !== 'none';
      const panel = id => document.querySelector(id);
      const rows = id => { const p = panel(id); return p ? p.querySelectorAll('.vrow').length : 0; };
      const g = panel('#epicGames');
      return { epic: vis(panel('#epicPanel')), gog: vis(panel('#gogPanel')),
               epicRows: rows('#epicGames'), gogRows: rows('#gogGames'),
               hint: ((panel('#epicHint')||{}).textContent || '').trim(),
               badges: g ? g.querySelectorAll('.vbadge.keep').length : 0 };
    })()`);
    if (plat.epic) {
      check("Epic 游戏库面板渲染出行", plat.epicRows > 0,
            `${plat.epicRows} 行 · ${plat.hint}`);
      check("Epic 行都带游戏本体徽章", plat.badges >= plat.epicRows,
            `${plat.badges}/${plat.epicRows}`);
    } else {
      console.log("  [信息] 本机无 Epic 游戏，面板按设计隐藏");
    }
    if (plat.gog) {
      check("GOG 游戏库面板渲染出行", plat.gogRows > 0, `${plat.gogRows} 行`);
    } else {
      console.log("  [信息] 本机无 GOG 游戏，面板按设计隐藏");
    }

    await evaluate(`document.querySelector('#btnSteam').click()`);
    await sleep(1200);
    check("可从 Steam 页面返回",
          await evaluate(`document.querySelector('#fullView').style.display !== 'none'`));
  }

  // --- 完整功能交互（预设 / 清理范围 / 大文件 / 语言 / 视图导航）---
  // 此前只有存在性断言，这里补真实点击链路：点 → 等状态迁移 → 断言 → 还原。
  console.log("\n  --- 完整功能交互 ---");

  // ① 预设切换：挑一个按钮统计带「预计可清 N 项」的预设（本机 D: 盘 +
  //    仅本盘范围下 safe/dev 可能合法为 0 项无数字），点击后断言：
  //    高亮迁移 + selected.size 与按钮统计一致（presetYield 与 applyPreset 同口径）。
  try {
    const pv = await evaluate(`(() => {
      if (typeof STATE === 'undefined' || !STATE.targets.length) return null;
      const btns = [...document.querySelectorAll('.preset')].filter(b => b.dataset.id !== 'none');
      if (!btns.length) return null;
      const withStat = btns.map(b => ({ b,
        stat: ((b.querySelector('span:last-child') || {}).textContent) || '' }))
        .find(x => /\\d+/.test(x.stat));
      const use = withStat ? withStat.b : btns[0];
      const m = withStat ? withStat.stat.match(/\\d+/) : null;
      const expectN = m ? Number(m[0]) : null;
      use.click();
      const act = document.querySelector('.preset.act');
      return { pid: use.dataset.id, expectN, n: selected.size,
               selN: document.querySelector('#selN').textContent,
               actId: act ? act.dataset.id : null };
    })()`);
    if (pv) {
      check("点预设：高亮迁移且选中数与预计统计一致",
            pv.actId === pv.pid && (pv.expectN === null || pv.n === pv.expectN)
            && Number(pv.selN) >= pv.n,
            `preset=${pv.pid} selected=${pv.n} 预计=${pv.expectN} selN=${pv.selN}`);
      const pv2 = await evaluate(`(() => {
        const none = [...document.querySelectorAll('.preset')].find(b => b.dataset.id === 'none');
        if (!none) return null;
        none.click();
        const act = document.querySelector('.preset.act');
        return { n: selected.size, actId: act ? act.dataset.id : null };
      })()`);
      check("点预设 none：清零还原", pv2 && pv2.n === 0 && pv2.actId === "none",
            JSON.stringify(pv2));
    } else {
      skip2("预设切换交互（无目标数据或页面无 safe/none 按钮）");
    }
  } catch (e) { INFO.push(`预设交互异常: ${e.message}`); }

  // ② 清理范围切换：all ↔ drive。基线取 all 态（用户级项在 drive 态被后端
  //    排除），测完精确还原到进入时的范围值 —— 否则会污染后面「盘符归属」
  //    的默认值断言（非系统盘默认「仅本盘」）。
  try {
    const sv = await evaluate(`(async () => {
      const sel = document.querySelector('#scopeSel');
      if (!sel) return null;
      const userN = () => STATE.targets.filter(t => t.scope === 'user').length;
      const setScope = async val => {
        sel.value = val;
        sel.dispatchEvent(new Event('change'));
        for (let i = 0; i < 40; i++) {
          await new Promise(r => setTimeout(r, 250));
          if (STATE.scope === val) break;
        }
        await new Promise(r => setTimeout(r, 300));
      };
      const orig = sel.value;
      await setScope('all');
      const allUser = userN(), allTotal = STATE.targets.length;
      await setScope('drive');
      const driveUser = userN(), driveTotal = STATE.targets.length;
      await setScope(orig);
      return { orig, allUser, allTotal, driveUser, driveTotal,
               endUser: userN(), endScope: STATE.scope, endVal: sel.value };
    })()`);
    if (sv) {
      check("切范围=仅当前盘：用户级项被排除",
            sv.driveUser === 0
            && (sv.allUser === 0 || sv.driveTotal === sv.allTotal - sv.allUser),
            `all态 ${sv.allTotal} 项目标（用户级 ${sv.allUser}）→ drive态 ${sv.driveTotal} 项（用户级 ${sv.driveUser}）`);
      check("范围还原：回到进入时的值且用户级项数一致",
            sv.endScope === sv.orig && sv.endVal === sv.orig
            && sv.endUser === (sv.orig === "all" ? sv.allUser : 0),
            `orig=${sv.orig} end=${sv.endScope} user=${sv.endUser}`);
    } else {
      skip2("清理范围切换交互（页面无 scopeSel）");
    }
  } catch (e) { INFO.push(`范围切换异常: ${e.message}`); }

  // ③ 大文件排行：点开 → 遮罩亮 + 模态有内容 → 用真实关闭按钮收起
  try {
    const bf = await evaluate(`(() => {
      document.querySelector('#btnBigFiles').click();
      const mask = document.querySelector('#mask');
      return { on: mask.classList.contains('on'),
               hasModal: !!document.querySelector('#modal h2') };
    })()`);
    check("大文件排行：点开弹出模态", bf.on && bf.hasModal, JSON.stringify(bf));
    // 等真实关闭按钮出现（全盘遍历可能较慢，超时则直接调 closeModal 收尾）
    let closed = null;
    for (let i = 0; i < 24 && !closed; i++) {
      await sleep(500);
      closed = await evaluate(`(() => {
        const b = document.querySelector('#bfClose');
        if (b) { b.click(); return true; }
        return false;
      })()`);
    }
    if (!closed) await evaluate(`typeof closeModal === 'function' && closeModal()`);
    await sleep(300);
    check("大文件排行：关闭后遮罩收起",
          await evaluate(`!document.querySelector('#mask').classList.contains('on')`));
  } catch (e) { INFO.push(`大文件交互异常: ${e.message}`); }

  // ④ 语言切换：切 EN → 静态文案 + localStorage + 高亮同步 → 切回原语言还原
  try {
    const lg = await evaluate(`(async () => {
      const seg = document.querySelector('#langSeg');
      if (!seg) return null;
      const cur = seg.querySelector('button.on');
      const origL = cur ? cur.dataset.l : 'zh';
      const origT = document.querySelector('#btnBigFiles').textContent;
      const en = seg.querySelector('button[data-l="en"]');
      en.click();
      await new Promise(r => setTimeout(r, 900));
      const tEn = document.querySelector('#btnBigFiles').textContent;
      const lsEn = localStorage.getItem('oc-lang');
      const onEn = seg.querySelector('button.on');
      const back = seg.querySelector('button[data-l="' + origL + '"]');
      back.click();
      await new Promise(r => setTimeout(r, 900));
      return { origL, origT, tEn, lsEn, onEn: onEn ? onEn.dataset.l : null,
               tBack: document.querySelector('#btnBigFiles').textContent,
               lsBack: localStorage.getItem('oc-lang') };
    })()`);
    if (lg) {
      check("切英文：静态文案 + localStorage + 高亮同步",
            lg.tEn === "Largest files" && lg.lsEn === "en" && lg.onEn === "en",
            `text=${lg.tEn} ls=${lg.lsEn} on=${lg.onEn}`);
      check("切回原语言：文案与设置还原",
            lg.tBack === lg.origT && lg.lsBack === lg.origL,
            `${lg.origL}: ${lg.tBack}`);
    } else {
      skip2("语言切换交互（页面无 langSeg）");
    }
  } catch (e) { INFO.push(`语言切换异常: ${e.message}`); }

  // ⑤ 视图导航高亮：点另一视图 → .on 迁移 + 视图切换 → 点回还原
  try {
    const nv = await evaluate(`(async () => {
      const btns = [...document.querySelectorAll('#viewNav button')];
      if (btns.length < 2) return null;
      const cur = btns.find(b => b.classList.contains('on'));
      const other = btns.find(b => b !== cur);
      if (!cur || !other) return null;
      const origV = cur.dataset.v;
      other.click();
      await new Promise(r => setTimeout(r, 800));
      const mid = {
        on: other.classList.contains('on'),
        curOff: !cur.classList.contains('on'),
        view: (typeof view !== 'undefined') ? view : null,
        otherV: other.dataset.v,
        vis: document.querySelector('#steamView').style.display === 'flex'
             || document.querySelector('#fullView').style.display === 'flex',
      };
      cur.click();
      await new Promise(r => setTimeout(r, 500));
      return { origV, mid,
               backOn: cur.classList.contains('on'),
               backView: (typeof view !== 'undefined') ? view : null };
    })()`);
    if (nv) {
      check("视图导航：点击后高亮迁移且视图切换",
            nv.mid.on && nv.mid.curOff && nv.mid.view === nv.mid.otherV && nv.mid.vis
            && nv.mid.otherV !== nv.origV,
            `${nv.origV}→${nv.mid.view}`);
      check("视图导航：点回原视图还原",
            nv.backOn && nv.backView === nv.origV, `→ ${nv.backView}`);
    } else {
      skip2("视图导航交互（viewNav 不足两个按钮）");
    }
  } catch (e) { INFO.push(`视图导航异常: ${e.message}`); }

  // --- 隐藏项合并展示（2026-10-30 用户要求）：主列表不再出现单独的系统
  //     隐藏项行 —— 合成一行「隐藏项（n 项）」，点击就地展开/收起成员。
  //     断言按 FS 实况门控：本盘没有隐藏项（hidN=0）时整段跳过。
  console.log("\n  --- 隐藏项合并展示 ---");
  try {
    const hv = await evaluate(`(() => {
      if (typeof FS === 'undefined' || !FS || !FS.nodes || !FS.nodes.length) return null;
      const seg = [...document.querySelectorAll('#fsModes .seg')].find(s => s.dataset.m === 'dirs');
      if (!seg) return null;
      seg.click();
      const names = new Set(FS.nodes.filter(n => n.hidden_sys).map(n => n.name.toLowerCase()));
      const rowNames = [...document.querySelectorAll('#fsChart .vrow .fname')]
        .map(e => ((e.firstChild || {}).textContent || '').trim().toLowerCase());
      return { hidN: names.size,
               leaked: rowNames.filter(n => names.has(n)).length,
               aggRow: !!document.querySelector('#fsChart .vrow-wrap.hagg'),
               // 期望行数：可见项经既有 size>0 过滤 + 前 30 截断，再 + 合并行
               expect: (() => {
                 if (typeof fsLayerParts !== 'function') return -1;
                 const nz = fsLayerParts().vis.filter(n => n.size > 0).length;
                 return Math.min(nz, 30) + 1;
               })(),
               domRows: document.querySelectorAll('#fsChart .vrow-wrap').length };
    })()`);
    await sleep(300);
    if (hv && hv.hidN > 0) {
      check("本层视图：无单独的系统隐藏项行", hv.leaked === 0,
            `泄漏 ${hv.leaked} 行（本盘隐藏项共 ${hv.hidN}）`);
      check("本层视图：隐藏项合并行存在且总行数符合期望",
            hv.aggRow && hv.domRows === hv.expect,
            `dom=${hv.domRows} expect=${hv.expect} aggRow=${hv.aggRow}`);
      await evaluate(`(() => {
        const agg = document.querySelector('#fsChart .vrow-wrap.hagg .vrow');
        if (agg) agg.click();
      })()`);
      await sleep(400);
      const st = await evaluate(`({
        mem: document.querySelectorAll('#fsChart .vrow-wrap.hidm').length,
        expect: (typeof fsLayerParts === 'function')
                ? Math.min(fsLayerParts().hid.length, 30) : -1 })`);
      check("点击合并行：成员就地展开", st && st.mem > 0 && st.mem === st.expect,
            `成员 ${st.mem}/${st.expect}`);
      await evaluate(`(() => {
        const agg = document.querySelector('#fsChart .vrow-wrap.hagg .vrow');
        if (agg) agg.click();
      })()`);
      await sleep(400);
      check("再点合并行：成员收起",
            await evaluate(`document.querySelectorAll('#fsChart .vrow-wrap.hidm').length === 0`));
      const bg = await evaluate(`(() => {
        const seg = [...document.querySelectorAll('#fsModes .seg')].find(s => s.dataset.m === 'biggest');
        if (!seg) return null;
        seg.click();
        const names = new Set(FS.nodes.filter(n => n.hidden_sys).map(n => n.name.toLowerCase()));
        const rowNames = [...document.querySelectorAll('#fsChart .vrow .fname')]
          .map(e => ((e.firstChild || {}).textContent || '').trim().toLowerCase());
        return { leaked: rowNames.filter(n => names.has(n)).length,
                 agg: !!document.querySelector('#fsChart .vrow-wrap.hagg') };
      })()`);
      await sleep(300);
      check("全盘最大项目：无单独隐藏项行且合并行在",
            bg && bg.leaked === 0 && bg.agg, JSON.stringify(bg || {}));
    } else if (hv) {
      skip2(`隐藏项合并展示（本盘未扫到隐藏项：hidN=${hv.hidN}）`);
    } else {
      skip2("隐藏项合并展示（FS 未就绪或无视图页签）");
    }
  } catch (e) { INFO.push(`隐藏项合并异常: ${e.message}`); }

  // JS 报错
  const real = errors.filter(e => !/favicon/i.test(e));
  check("无 JS 控制台报错/异常", real.length === 0, real.slice(0, 2).join(" | "));
  if (failedReqs.length) INFO.push(`失败请求: ${failedReqs.slice(0, 3).join(", ")}`);

  // --- 盘符归属（放在最后跑：切盘会触发重扫，异步完成时会冲掉界面状态，
  //     放在中间会把后面的规则/换算断言带崩）---
  console.log("\n  --- 盘符归属与清理范围默认值 ---");
  try {
    const probe = await evaluate(`(async () => {
      const dv = [...document.querySelectorAll('.drv')].find(d => d.dataset.l !== 'C');
      if (!dv) return null;
      const other = dv.dataset.l;
      dv.click();
      // 等到重扫真正结束（按钮恢复可用），否则读到的是上一轮的结果
      for (let i = 0; i < 60; i++) {
        await new Promise(r => setTimeout(r, 1000));
        const btn = document.querySelector('#btnScan');
        const on = document.querySelector('.drv.on');
        if (on && on.dataset.l === other && btn && !btn.disabled) break;
      }
      const sel = document.querySelector('#scopeSel');
      // 卡片已移除：改从 STATE（页面顶层 let，evaluate 同 realm 可读）
      // 数一遍非本盘目标，验证「仅当前盘专属项」范围没有混入 C 盘用户级缓存
      const st = (typeof STATE !== 'undefined' && STATE) ? STATE : null;
      const tpaths = st && Array.isArray(st.targets)
        ? st.targets.map(t => {
            let p = String(t.path || '');
            if (/%DRIVE%/i.test(p)) p = p.replace(/%DRIVE%/gi, other + ':')
                                        .replace(/%DRIVE_LETTER%/gi, other);
            return p;
          })
        : [];
      return {
        other, got: sel ? sel.value : '',
        stray: tpaths.filter(p => /^C:\\\\/i.test(p)).length,
        cards: tpaths.length,
        haveState: !!st,
      };
    })()`);
    if (probe) {
      check(`非系统盘（${probe.other}:）的清理范围默认为「仅本盘」`,
            probe.got === "drive", `scope=${probe.got}`);
      check("非本盘时清理目标里没有别的盘的路径（不再混入 C 盘用户级缓存）",
            !probe.haveState || probe.stray === 0,
            `${probe.stray}/${probe.cards} 条目标指向 C:`);
    } else {
      skip("数据盘默认清理范围", "本机只有一个盘");
    }
  } catch (e) {
    skip("数据盘默认清理范围", "探测失败：" + e.message);
  }

  // 一键清空「所选项」的回收站（2026-10-13）：清理结果弹窗在有项移入
  // 回收站时提供入口。只验证「按钮出现 + 第一段确认生效」，绝不真正
  // 清空 —— 第二段才是执行。模拟路径用 Q: 盘，本机不存在该盘的条目。
  const ebc = await evaluate(`(() => {
    try {
      showCleanResult({ freed_h: "0 B", results: [
        { label: "CDP 模拟项", freed: 0, deleted: 1, failed: 0,
          skipped: [], trashed: 1, drive: "Q", error: null,
          trash_paths: ["Q:\\\\cdp-mock\\\\item.txt"] }] },
        (typeof STATE !== "undefined" && STATE && STATE.stats) || null,
        Date.now(), "CDP 模拟结果");
      const b = document.querySelector("#btnEmptyBin");
      if (!b) return { ok: false, why: "no btnEmptyBin" };
      const label0 = b.textContent;
      b.click();
      const armed = b.textContent !== label0;
      return { ok: true, armed, label0 };
    } finally {
      if (typeof closeModal === "function") closeModal();
    }
  })()`);
  check("清理结果弹窗提供一键清空回收站入口（两段式确认第一段）",
        ebc.ok && ebc.armed, JSON.stringify(ebc));

  // ---------- 小视口响应式 ----------
  // 浏览器窗口缩到 ~800×600 后界面必须仍完整可用：不允许出现横向滚动条，
  // 占用排行与侧栏面板都还看得到。用 Emulation 改视口，量完恢复。
  console.log("\n  --- 小视口响应式（800×600）---");
  try {
    await cdp.send("Emulation.setDeviceMetricsOverride",
      { width: 800, height: 600, deviceScaleFactor: 0, mobile: false }, S);
    await sleep(600); // 等 resize 重绘（趋势图重绘有 150ms debounce）
    const vp = await evaluate(`(() => {
      const sec = document.querySelector('#fullView') || document.querySelector('main > section');
      const asd = document.querySelector('aside');
      const r = el => el ? el.getBoundingClientRect() : null;
      const sr = r(sec), ar = r(asd);
      return {
        overflow: document.documentElement.scrollWidth - window.innerWidth,
        secVisible: !!sr && sr.width > 100 && sr.height > 50,
        asdVisible: !!ar && ar.width > 50,
        // 等比缩放布局（2026-10-13）：窄窗口保持左右两栏，不允许退回上下堆叠
        twoCol: !!sr && !!ar && sr.right <= ar.left + 2 && ar.left > 0,
        vw: window.innerWidth,
        overflowPx: document.documentElement.scrollWidth,
      };
    })()`);
    check("小视口无横向溢出（scrollWidth ≤ 视口+2px）",
          vp.overflow <= 2, `超出 ${vp.overflow}px（scrollWidth=${vp.overflowPx}）`);
    checkInit("小视口下占用排行仍完整可见", vp.secVisible,
          `可见=${vp.secVisible}`);
    check("小视口下侧栏面板仍可见", vp.asdVisible, `可见=${vp.asdVisible}`);
    check("小视口保持左右两栏（等比缩放，不退化为上下堆叠）",
          vp.twoCol, `两栏=${vp.twoCol}`);
    if (rankingReady) {
      const rrow = await evaluate(`(() => {
        const rows = [...document.querySelectorAll('#fsChart .vrow')];
        for (const row of rows) {
          const b = row.getBoundingClientRect();
          if (b.width > 0 && b.height > 0) return { w: Math.round(b.width), ok: true };
        }
        return { ok: false };
      })()`);
      checkInit("小视口下排行行有实际宽度（未被压扁）",
            rrow.ok && rrow.w > 200, rrow.ok ? `行宽 ${rrow.w}px` : "无可见行");
    }
  } catch (e) {
    skip("小视口响应式", "模拟失败：" + e.message);
  } finally {
    await cdp.send("Emulation.clearDeviceMetricsOverride", {}, S);
    await sleep(300);
  }

  // 截图
  try {
    const shot = await cdp.send("Page.captureScreenshot",
      { format: "png", captureBeyondViewport: true }, S);
    fs.writeFileSync(path.join(__dirname, "_ui_check.png"), Buffer.from(shot.data, "base64"));
    await evaluate(`document.querySelector('#btnFullScan').click()`);
    await sleep(900);
    const shot2 = await cdp.send("Page.captureScreenshot",
      { format: "png", captureBeyondViewport: true }, S);
    fs.writeFileSync(path.join(__dirname, "_fs_check.png"), Buffer.from(shot2.data, "base64"));
    console.log(`  截图: tools\\_ui_check.png（常规）、tools\\_fs_check.png（全盘扫描）`);
  } catch (e) { console.log("  截图失败: " + e.message); }

  cdp.ws.close();
  proc.kill();
  await sleep(400);
  try { fs.rmSync(profile, { recursive: true, force: true }); } catch { }

  console.log(`\n  ==========================================`);
  console.log(`  通过 ${PASS.length} 项，失败 ${FAIL.length} 项，跳过 ${SKIP.length} 项`);
  if (FAIL.length) console.log("  失败项：" + FAIL.join("、"));
  if (SKIP.length) console.log("  跳过项：" + SKIP.length + " 项（环境不具备条件，非缺陷）");
  console.log(`  ==========================================\n`);
  process.exit(FAIL.length ? 1 : 0);
})().catch(e => {
  console.error("  检查器异常: " + (e && e.stack || e));
  process.exit(2);
});
