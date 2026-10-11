'use strict';

/**
 * Demo 走查脚本（V2.0）。
 *
 * demo/index.html 是单文件前端原型，界面全部由 JS 拼接生成，因此用「正则搜源码」
 * 是搜不到 data-act="enroll" 这类字面量的 —— 那种测试是假验证。
 *
 * 这里改成把它真正跑起来：用一套极简 DOM 桩撑住 document，在 vm 沙箱里执行内嵌
 * 脚本，然后按真实用户的操作顺序派发点击事件，对渲染出来的 HTML 断言。
 *
 * 断言的是「行为」而不是「文本」：报名后人数是否 +1、满员时是否进入候补并给出排位、
 * 退出候补后后面的人是否前移、审核通过后是否按余位落到正式或候补、
 * 取消后队首是否自动递补、导出的 CSV 行数是否等于报名人数 + 1。
 *
 * 输出：每项一行，结尾汇总。有任一项失败则以退出码 1 结束。
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const HTML_PATH = path.join(__dirname, '..', 'demo', 'index.html');
const html = fs.readFileSync(HTML_PATH, 'utf8');

const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
if (!blocks.length) {
  console.error('demo/index.html 里没有找到内嵌脚本');
  process.exit(1);
}
const script = blocks.reduce((a, b) => (b.length > a.length ? b : a));

/* =========================================================
   极简 DOM 桩：只实现 Demo 用到的那几个接口
   ========================================================= */
const nodes = {};

function node(id) {
  if (!nodes[id]) {
    const obj = {
      id,
      textContent: '',
      value: '',
      checked: false,
      _lastChild: null,
      _html: '',
      appendChild(child) {
        obj._lastChild = child;
      },
      removeChild() {},
      remove() {},
      classList: { add() {}, remove() {}, contains() { return false; } },
    };
    // 让 innerHTML 与 lastChild 联动：代码里「清空容器」就是 innerHTML = ''，
    // 这时候子节点也该没了。不联动的话，closeModal() 之后 lastChild 还挂着
    // 上一个弹窗，「被拦下时不该弹框」这类断言会拿到假信号。
    Object.defineProperty(obj, 'innerHTML', {
      get() { return obj._html; },
      set(v) {
        obj._html = v;
        if (v === '') obj._lastChild = null;
      },
    });
    Object.defineProperty(obj, 'lastChild', {
      get() { return obj._lastChild; },
      set(v) { obj._lastChild = v; },
    });
    nodes[id] = obj;
  }
  return nodes[id];
}

function createEl() {
  const el = {
    _inputValue: '',
    className: '',
    innerHTML: '',
    style: {},
    children: [],
    classList: { add() {}, remove() {}, contains() { return false; } },
    setAttribute() {},
    getAttribute() { return null; },
    appendChild(child) {
      el.children.push(child);
    },
    remove() {},
    focus() {},
    click() {},
    addEventListener(type, fn) {
      el['on' + type] = fn;
    },
    querySelector(sel) {
      if (sel === '#modal-input') {
        // 必须真的存在这个输入框才返回它，否则 stub 会比真实 DOM 更"宽容"，
        // 导致没有输入框的弹窗被误判成"没填内容"。
        if (el.innerHTML.indexOf('id="modal-input"') === -1) return null;
        return {
          get value() { return el._inputValue; },
          focus() {},
        };
      }
      return null;
    },
  };
  return el;
}

const handlers = {};

let lastBlob = null;
function BlobStub(parts, opts) {
  this.parts = parts;
  this.opts = opts || {};
  lastBlob = this;
}

const sandbox = {
  console,
  document: {
    getElementById: node,
    createElement: createEl,
    addEventListener(type, fn) {
      handlers[type] = fn;
    },
    body: { appendChild() {} },
  },
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  setTimeout,
  clearTimeout,
  Blob: BlobStub,
  URL: { createObjectURL() { return 'blob:demo'; }, revokeObjectURL() {} },
};
sandbox.window = sandbox;
vm.createContext(sandbox);

/* =========================================================
   驱动：模拟用户操作
   ========================================================= */
function appHtml() {
  return node('app').innerHTML;
}

function click(act, attrs) {
  const target = {
    classList: { contains() { return false; } },
    getAttribute(name) {
      if (name === 'data-act') return act;
      if (attrs && Object.prototype.hasOwnProperty.call(attrs, name)) {
        return String(attrs[name]);
      }
      return null;
    },
    parentElement: null,
  };
  try {
    handlers.click({ target, preventDefault() {} });
  } catch (err) {
    // 把「点击时抛异常」变成一条可读的失败，而不是让整个走查崩掉
    throw new Error(
      '点击 data-act="' + act + '" 时抛出异常：' + err.message +
      '\n' + (err.stack || '').split('\n').slice(0, 4).join('\n')
    );
  }
}

/** 回填表单字段（模拟用户在输入框里敲字 / 勾选复选框） */
function fill(values) {
  Object.keys(values).forEach((id) => {
    const el = node(id);
    if (id === 'f-review') el.checked = !!values[id];
    else el.value = values[id];
  });
}

/** 在渲染出来的 HTML 里，按「data-name」找到某个按钮对应的 data-id。 */
function findId(act, nameValue) {
  const re = new RegExp('data-act="' + act + '" data-name="' + nameValue + '" data-id="(\\d+)"');
  const m = appHtml().match(re);
  return m ? m[1] : null;
}

/** 找第一个匹配 data-act（可选按钮文字收尾）的按钮的 data-id。 */
function firstId(act, tail) {
  const re = new RegExp('data-act="' + act + '" data-id="(\\d+)"' + (tail || ''));
  const m = appHtml().match(re);
  return m ? m[1] : null;
}

function countOf(haystack, needle) {
  return haystack.split(needle).length - 1;
}

/** 从活动详情页读「正式名额」的「已占 / 上限」。 */
function quotaOf(h) {
  const m = h.match(/正式名额<\/dt><dd>(\d+) \/ (\d+) 人/);
  return m ? [Number(m[1]), Number(m[2])] : null;
}

/** 从名单页的四个人数卡里读一个数字（人数上限 / 正式人数 / 候补人数 / 剩余名额）。 */
function rosterStat(h, label) {
  const re = new RegExp(label + '</div><div class="sv"[^>]*>(\\d+)');
  const m = h.match(re);
  return m ? Number(m[1]) : null;
}

function toLocal(hoursFromNow) {
  const d = new Date(Date.now() + hoursFromNow * 3600000);
  const p = (n) => String(n).padStart(2, '0');
  return (
    d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
    'T' + p(d.getHours()) + ':' + p(d.getMinutes())
  );
}

/** 触发弹窗上的按钮 */
function pressModal(which, inputValue) {
  const modal = node('modal-root').lastChild;
  if (!modal) return false;
  if (inputValue !== undefined) modal._inputValue = inputValue;
  const handler = modal.onclick;
  if (!handler) return false;
  handler({
    target: {
      getAttribute(name) {
        return name === 'data-modal' ? which : null;
      },
    },
  });
  return true;
}

/* =========================================================
   断言
   ========================================================= */
let pass = 0;
const failures = [];

function section(title) {
  console.log('\n── ' + title + ' ' + '─'.repeat(Math.max(2, 46 - title.length)));
}

function check(name, cond, extra) {
  if (cond) {
    pass++;
    console.log('  \u2713 ' + name);
  } else {
    failures.push(name);
    console.log('  \u2717 ' + name + (extra ? '   [' + extra + ']' : ''));
  }
}

/* =========================================================
   启动脚本
   ========================================================= */
vm.runInContext(script, sandbox);

console.log('\nDemo 走查：' + path.relative(process.cwd(), HTML_PATH));

/* ---------- 1. 登录页 ---------- */
section('1. 登录页');
let h = appHtml();
check(
  '三个角色入口齐全（学生 / 活动组织教师 / 系统管理员）',
  h.includes('data-role="STUDENT"') && h.includes('data-role="TEACHER"') && h.includes('data-role="ADMIN"')
);
check('带体验路线指引', h.includes('建议的体验路线'));
check('标注 V2.0', h.includes('V2.0'));

/* ---------- 2. 学生：广场与状态标记 ---------- */
section('2. 学生：广场与状态标记');
click('login', { 'data-role': 'STUDENT' });
h = appHtml();
check('侧栏出现「活动广场」「我的报名」', h.includes('活动广场') && h.includes('我的报名'));
check('学生看不到「活动监督」（管理员专属）', !h.includes('活动监督'));
check('可见已发布活动', h.includes('人像摄影体验日'));
check('卡片标记「可报名」', h.includes('可报名'));
check('卡片标记「已满员 · 可候补」', h.includes('已满员 · 可候补'));
check('需审核的活动带「需审核」标记', h.includes('需审核'));
check('不可见草稿活动', !h.includes('校园夜景采风'));
check('不可见已下线活动', !h.includes('电竞友谊赛'));

/* ---------- 3. 学生：报名与取消（名额实时变化） ---------- */
section('3. 学生：报名与取消');
click('detail', { 'data-id': 2 });
h = appHtml();
check('详情页显示剩余名额', h.includes('剩余名额'));
check('可报名时显示「立即报名」', h.includes('立即报名'));
check('报名前是 12 / 60', h.includes('正式名额</dt><dd>12 / 60 人'));

click('enroll', { 'data-id': 2 });
h = appHtml();
check('报名后名额变为 13 / 60', h.includes('正式名额</dt><dd>13 / 60 人'));
check('报名后标记「正式参加」', h.includes('正式参加'));
check('报名后按钮变为取消报名', h.includes('data-act="unenroll" data-id="2"'));
check('未进入锁定期的活动给出可取消截止时间', h.includes('可取消至'));

click('enroll', { 'data-id': 2 });
check('重复报名不改变人数', appHtml().includes('正式名额</dt><dd>13 / 60 人'));

click('unenroll', { 'data-id': 2 });
check('取消报名前弹出确认框', !!node('modal-root').lastChild);
pressModal('ok');
h = appHtml();
check('取消后名额回到 12 / 60', h.includes('正式名额</dt><dd>12 / 60 人'));
check('取消后给出「重新报名」入口', h.includes('重新报名'));

/* ---------- 4. 学生：满员的三条路 ---------- */
section('4. 学生：满员的三条路（候补 / 正式 / 加入候补）');
click('detail', { 'data-id': 5 });
h = appHtml();
check('满员活动的候补者（我）看到「候补中 · 第 2 位」', h.includes('候补中 · 第 2 位'));
check('候补可以随时退出（不占名额，不受名单锁定限制）',
  h.includes('data-act="unenroll" data-id="5"') && !h.includes('距报名截止不足'));

click('detail', { 'data-id': 14 });
h = appHtml();
check('满员活动的正式者（我）看到「正式参加」与取消入口',
  h.includes('正式参加') && h.includes('data-act="unenroll" data-id="14"'));
check('满员活动仍有候补队列在等（队列 2 人）', h.includes('候补队列 2 人'));

click('detail', { 'data-id': 16 });
h = appHtml();
check('满员且未报名：按钮是「加入候补队列」而不是「名额已满」', h.includes('加入候补队列'));
check('给出「自动递补」说明', h.includes('自动递补'));
click('enroll', { 'data-id': 16 });
click('nav', { 'data-page': 'my' });
check('加入候补后「我的报名」显示「候补中 · 第 1 位」', appHtml().includes('候补中 · 第 1 位'));

// 退出候补：后面的人按原顺序自动前移，队列不留空档
click('unenroll', { 'data-id': 5 });
check('退出候补前弹出确认框', !!node('modal-root').lastChild);
pressModal('ok');
click('logout');
click('login', { 'data-role': 'TEACHER' });
click('goto-roster', { 'data-id': 5 });
h = appHtml();
check('退出后队列由 3 人变 2 人', h.includes('候补队列（2 人）'));
check('我退出后，孙悦从第 3 位前移到第 2 位', h.includes('孙悦') && h.includes('第 2 位'));
check('王芳仍在第 1 位（先到先得，顺序不乱）', h.includes('王芳') && h.includes('第 1 位'));
click('logout');
click('login', { 'data-role': 'STUDENT' });

/* ---------- 5. 学生：拦截边界 ---------- */
section('5. 学生：报名与取消的拦截边界');
click('detail', { 'data-id': 8 });
check('报名已截止时按钮禁用并说明原因', appHtml().includes('报名已截止'));
click('detail', { 'data-id': 7 });
check('活动进行中时关闭报名通道', appHtml().includes('活动已开始，报名通道已关闭'));
click('detail', { 'data-id': 11 });
check('活动已取消时不可报名', appHtml().includes('该活动已取消'));
click('detail', { 'data-id': 10 });
check('被管理员下线的活动对学生不可见（详情页也不给看）', appHtml().includes('已被管理员下线，不再展示'));
click('detail', { 'data-id': 1 });
check('活动已开始时不可取消（名单锁定）', appHtml().includes('活动已开始，名单已锁定'));

// 取消报名的锁定规则：距报名截止不足 24 小时起，名单锁定
click('detail', { 'data-id': 4 });
h = appHtml();
check('锁定期的活动给出锁定说明', h.includes('距报名截止不足 24 小时，名单已锁定'));
check('锁定期内不渲染取消按钮', !h.includes('data-act="unenroll" data-id="4"'));
click('unenroll', { 'data-id': 4 });
check('绕过按钮直接调用也会被拦下（不弹确认框）', !node('modal-root').lastChild);

/* ---------- 6. 学生：我的报名（五种状态） ---------- */
section('6. 学生：我的报名');
click('nav', { 'data-page': 'my' });
h = appHtml();
check('含「待审核」记录', h.includes('待审核'));
check('含「正式参加」记录', h.includes('正式参加'));
check('含「候补中 · 第 1 位」（刚加入的候补）', h.includes('候补中 · 第 1 位'));
check('含「已拒绝」记录（带审核时间）', h.includes('已拒绝'));
check('含「已取消」记录', h.includes('已取消'));
check('含「已签到」记录', h.includes('已签到'));
check('含「缺席」记录（已结束未签到）', h.includes('缺席'));
check('含「待签到」记录（进行中）', h.includes('待签到'));
check('「待审核」记录提供「撤销申请」按钮', h.includes('data-act="unenroll" data-id="9"'));
check('页面解释了三种状态的差别', h.includes('报名状态说明'));

/* ---------- 7. 教师：我的活动与直接发布 ---------- */
section('7. 教师：我的活动与直接发布');
click('logout');
click('login', { 'data-role': 'TEACHER' });
h = appHtml();
check('进入「我的活动」', h.includes('我的活动'));
check('能看到自己的草稿活动', h.includes('校园夜景采风'));
check('草稿带「发布活动」按钮', h.includes('data-act="publish-activity" data-id="12"'));
check('人像摄影体验日显示「待审核 4 条」', h.includes('待审核 4 条'));

click('publish-activity', { 'data-id': 12 });
h = appHtml();
check('草稿发布后直接进入已发布（不再有管理员审核环节）', h.includes('data-act="goto-roster" data-id="12"'));
check('发布后不再显示「发布活动」按钮', !h.includes('data-act="publish-activity" data-id="12"'));

click('nav', { 'data-page': 'teacher-create' });
h = appHtml();
check('发布表单已渲染（含需审核开关与资格说明）',
  h.includes('id="f-title"') && h.includes('id="f-capacity"') &&
  h.includes('id="f-review"') && h.includes('id="f-qualification"'));

fill({
  'f-title': '摄影作品点评会',
  'f-location': '传媒楼 201',
  'f-capacity': '40',
  'f-start': toLocal(96),
  'f-end': toLocal(99),
  'f-deadline': toLocal(72),
  'f-review': true,
  'f-qualification': '',
});
click('publish-new');
check('勾选需审核却没填资格说明会被拦截', appHtml().includes('请填写参加资格说明'));

fill({
  'f-title': '摄影作品点评会',
  'f-location': '传媒楼 201',
  'f-capacity': '40',
  'f-start': toLocal(96),
  'f-end': toLocal(90),   // 故意写反
  'f-deadline': toLocal(72),
  'f-review': false,
});
click('publish-new');
h = appHtml();
check('结束时间早于开始时间被拦截', h.includes('结束时间必须晚于开始时间'));
check('校验失败后保留已填内容（不清空表单）', h.includes('value="摄影作品点评会"'));

fill({
  'f-title': '摄影作品点评会',
  'f-location': '传媒楼 201',
  'f-capacity': '40',
  'f-start': toLocal(96),
  'f-end': toLocal(99),
  'f-deadline': toLocal(72),
  'f-review': true,
  'f-qualification': '面向摄影协会成员，需自备相机。',
});
click('publish-new');
h = appHtml();
check('提交成功并回到「我的活动」', h.includes('我的活动'));
check('新活动出现在列表中', h.includes('摄影作品点评会'));
check('新活动直接是已发布状态', h.includes('data-act="goto-roster" data-id="17"'));

/* ---------- 8. 教师：报名审核（通过 ≠ 正式） ---------- */
section('8. 教师：报名审核（通过 ≠ 正式）');
click('goto-roster', { 'data-id': 9 });
h = appHtml();
check('名单页有待审核区', h.includes('待审核申请（4）'));
check('名单页有人数视图（上限 / 正式 / 候补 / 余位）',
  h.includes('人数上限') && h.includes('正式人数') && h.includes('候补人数') && h.includes('剩余名额'));
check('审核区展示参加资格说明', h.includes('面向全体在校学生'));
check('审核按钮带学生姓名便于定位', !!findId('approve-enroll', '王芳'));

click('approve-enroll', { 'data-id': findId('approve-enroll', '王芳') });
h = appHtml();
check('通过（有余位）→ 转为正式', h.includes('正式名单（1 人）') && h.includes('王芳'));
check('通过后该申请移出待审核区', h.includes('待审核申请（3）'));

click('reject-enroll', { 'data-id': findId('reject-enroll', '孙悦') });
check('拒绝前弹出确认框', !!node('modal-root').lastChild);
pressModal('ok');
h = appHtml();
check('拒绝后移出待审核区', h.includes('待审核申请（2）'));
check('被拒绝的人不会出现在候补队列', !h.includes('孙悦'));

click('goto-roster', { 'data-id': 15 });
h = appHtml();
check('满员 + 需审核的活动有待审核申请', h.includes('待审核申请（2）'));
check('该活动的正式人数已经等于上限（3 / 3）',
  rosterStat(h, '正式人数') === 3 && rosterStat(h, '人数上限') === 3);
click('approve-enroll', { 'data-id': findId('approve-enroll', '吴桐') });
h = appHtml();
check('通过（没有余位）→ 排进候补而不是正式',
  h.includes('候补队列（1 人）') && h.includes('第 1 位') && h.includes('吴桐'));
check('通过≠正式：正式人数仍等于上限', rosterStat(h, '正式人数') === 3);
click('reject-enroll', { 'data-id': findId('reject-enroll', '郑楠') });
pressModal('ok');
check('清空后待审核区为 0', appHtml().includes('待审核申请（0）'));

/* ---------- 9. 学生：取消触发自动递补 ---------- */
section('9. 学生：取消触发自动递补');
click('logout');
click('login', { 'data-role': 'STUDENT' });
click('detail', { 'data-id': 14 });
h = appHtml();
check('递补前：我占着正式名额、队列里等 2 人', h.includes('正式参加') && h.includes('候补队列 2 人'));
click('unenroll', { 'data-id': 14 });
check('取消前弹出确认框', !!node('modal-root').lastChild);
pressModal('ok');
h = appHtml();
check('取消后该记录变为「已取消」', h.includes('你已取消这次报名'));
check('取消后名额立即被候补队首接上（队列只剩 1 人）', h.includes('候补队列 1 人'));

click('logout');
click('login', { 'data-role': 'TEACHER' });
click('goto-roster', { 'data-id': 14 });
h = appHtml();
check('教师端确认：正式名额被补满（正式人数 40）', rosterStat(h, '正式人数') === 40);
check('教师端确认：候补队列只剩 1 人且赵启前移到第 1 位',
  h.includes('候补队列（1 人）') && h.includes('赵启') && h.includes('第 1 位'));

/* ---------- 10. 教师：签到核验与名单导出 ---------- */
section('10. 教师：签到核验与名单导出');
click('goto-roster', { 'data-id': 1 });
h = appHtml();
check('签到页显示 6 位签到码', h.includes('482913'));
check('签到页显示 4 个人数指标', h.includes('人数上限') && h.includes('正式人数') && h.includes('候补人数') && h.includes('剩余名额'));
check('正式名单 37 人', h.includes('正式名单（37 人）'));
check('名单里标记出「我」', h.includes('>我</span>'));

const checkedBefore = countOf(h, '撤销签到</button>');
check('初始 21 条已签到', checkedBefore === 21, '实际 ' + checkedBefore);
click('toggle-checkin', { 'data-id': firstId('toggle-checkin', '>标记已签到') });
check('勾选签到后已签到 +1', countOf(appHtml(), '撤销签到</button>') === 22);
click('toggle-checkin', { 'data-id': firstId('toggle-checkin', '>撤销签到') });
check('撤销签到后回到 21', countOf(appHtml(), '撤销签到</button>') === 21);

lastBlob = null;
click('export', { 'data-id': 1 });
check('导出名单生成了文件内容', !!lastBlob);
if (lastBlob) {
  const csv = lastBlob.parts[0];
  check('CSV 带 UTF-8 BOM（Excel 打开不乱码）', csv.charCodeAt(0) === 0xfeff);
  check('CSV 表头含「状态」列（正式 / 待审核 / 候补可区分）', csv.includes('"状态"'));
  check('CSV 行数 = 正式名单 + 表头', csv.trim().split('\r\n').length === 38);
}

/* ---------- 11. 教师：数据统计 ---------- */
section('11. 教师：数据统计');
click('goto-stats');
h = appHtml();
check('统计页显示整体签到率', h.includes('整体签到率'));
check('统计页统计候补与待审核', h.includes('候补中') && h.includes('待审核申请'));
check('统计页分活动列出签到率与上座率口径', h.includes('签到率') && h.includes('上座率'));
lastBlob = null;
click('export-stats');
check('统计汇总可导出（含候补人数列）', !!lastBlob && lastBlob.parts[0].includes('候补人数'));

/* ---------- 12. 管理员：活动监督（下线 / 恢复） ---------- */
section('12. 管理员：活动监督（下线 / 恢复）');
click('logout');
click('login', { 'data-role': 'ADMIN' });
h = appHtml();
check('进入「活动监督」', h.includes('活动监督'));
check('已下线活动带「处理标记」', h.includes('下线处理：') && h.includes('消防演练'));
check('管理员没有教师的名单入口（不介入报名）', !h.includes('data-act="goto-roster"'));
check('监督页说明权限边界', h.includes('不代替教师编辑活动内容'));
check('最近处理记录可追溯', h.includes('最近处理记录') && h.includes('下线活动'));

click('admin-offline', { 'data-id': 2 });
check('下线前弹出原因输入框', !!node('modal-root').lastChild);
pressModal('ok', '内容待核实');
h = appHtml();
check('下线后列表出现处理标记', h.includes('内容待核实'));
check('下线后操作变成「恢复」', h.includes('data-act="admin-restore" data-id="2"'));

click('logout');
click('login', { 'data-role': 'STUDENT' });
click('nav', { 'data-page': 'plaza' });
check('学生端活动广场不再展示被下线的活动', !appHtml().includes('手机摄影技巧分享会'));
click('detail', { 'data-id': 2 });
check('学生直接访问也被挡住', appHtml().includes('已被管理员下线，不再展示'));

click('logout');
click('login', { 'data-role': 'TEACHER' });
h = appHtml();
check('教师端仍可见被下线的活动', h.includes('手机摄影技巧分享会'));
check('教师端看到处理标记（谁、为什么）', h.includes('已被管理员下线') && h.includes('内容待核实'));

click('logout');
click('login', { 'data-role': 'ADMIN' });
click('admin-restore', { 'data-id': 2 });
pressModal('ok');
h = appHtml();
check('恢复后重新可见（按时间重新定位状态）', h.includes('data-act="admin-offline" data-id="2"'));

click('logout');
click('login', { 'data-role': 'STUDENT' });
click('nav', { 'data-page': 'plaza' });
check('学生端重新看到该活动', appHtml().includes('手机摄影技巧分享会'));

/* ---------- 汇总 ---------- */
console.log('\n' + '='.repeat(52));
console.log(failures.length ? '失败' : '全部通过');
console.log('通过 ' + pass + ' 项，失败 ' + failures.length + ' 项');
if (failures.length) {
  console.log('\n失败清单：');
  failures.forEach((f) => console.log('  - ' + f));
}
console.log('='.repeat(52) + '\n');

process.exit(failures.length ? 1 : 0);
