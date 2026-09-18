'use strict';

/**
 * Demo 走查脚本。
 *
 * demo/index.html 是单文件前端原型，界面全部由 JS 拼接生成，因此用「正则搜源码」
 * 是搜不到 data-act="enroll" 这类字面量的 —— 那种测试是假验证。
 *
 * 这里改成把它真正跑起来：用一套极简 DOM 桩撑住 document，在 vm 沙箱里执行内嵌
 * 脚本，然后按真实用户的操作顺序派发点击事件，对渲染出来的 HTML 断言。
 *
 * 断言的是「行为」而不是「文本」：报名后人数是否 +1、驳回后是否移出待审核列表、
 * 勾选签到后签到率是否变化、导出的 CSV 行数是否等于报名人数 + 1。
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
    nodes[id] = {
      id,
      innerHTML: '',
      textContent: '',
      value: '',
      lastChild: null,
      appendChild(child) {
        nodes[id].lastChild = child;
      },
      removeChild() {},
      remove() {},
      classList: { add() {}, remove() {}, contains() { return false; } },
    };
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

/** 回填表单字段（模拟用户在输入框里敲字） */
function fill(values) {
  Object.keys(values).forEach((id) => {
    node(id).value = values[id];
  });
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
  '三个角色入口齐全',
  h.includes('data-role="STUDENT"') && h.includes('data-role="CLUB"') && h.includes('data-role="ADMIN"')
);
check('带体验路线指引', h.includes('建议的体验路线'));

/* ---------- 2. 学生视角 ---------- */
section('2. 学生：登录与活动广场');
click('login', { 'data-role': 'STUDENT' });
h = appHtml();
check('侧栏出现「活动广场」「我的报名」', h.includes('活动广场') && h.includes('我的报名'));
check('学生看不到「审核中心」', !h.includes('审核中心'));
check('可见已发布活动', h.includes('手机摄影技巧分享会'));
check('不可见草稿活动', !h.includes('校园夜景采风'));
check('不可见待审核活动', !h.includes('人像摄影体验日'));
check('不可见已驳回活动', !h.includes('电竞友谊赛'));

/* ---------- 3. 报名与名额 ---------- */
section('3. 学生：报名与名额变化');
click('detail', { 'data-id': 2 });
h = appHtml();
check('详情页显示剩余名额', h.includes('剩余名额'));
check('可报名时显示「立即报名」', h.includes('立即报名'));
check('报名前为 12 人', h.includes('当前报名</dt><dd>12 人'));

click('enroll', { 'data-id': 2 });
h = appHtml();
check('报名后名额变为 13 人', h.includes('当前报名</dt><dd>13 人'));
check('报名后标记「你已报名该活动」', h.includes('你已报名该活动'));
check('报名后按钮变为取消报名', h.includes('取消报名'));

click('enroll', { 'data-id': 2 });
check('重复报名不改变人数', appHtml().includes('当前报名</dt><dd>13 人'));

click('unenroll', { 'data-id': 2 });
check('取消报名前弹出确认框', !!node('modal-root').lastChild);
pressModal('ok');
h = appHtml();
const afterCancel = h.match(/当前报名<\/dt><dd>(\d+) 人/);
check(
  '取消后名额回到 12 人',
  !!afterCancel && afterCancel[1] === '12',
  afterCancel ? '实际 ' + afterCancel[1] + ' 人' : '没找到报名人数字段'
);

/* ---------- 4. 报名拦截边界 ---------- */
section('4. 学生：报名拦截边界');
click('detail', { 'data-id': 5 });
check('名额已满时按钮禁用并说明原因', appHtml().includes('名额已满'));
click('detail', { 'data-id': 8 });
check('报名已截止时按钮禁用并说明原因', appHtml().includes('报名已截止'));
click('detail', { 'data-id': 7 });
check('活动进行中时关闭报名通道', appHtml().includes('活动已开始，报名通道已关闭'));
click('detail', { 'data-id': 11 });
check('活动已取消时不可报名', appHtml().includes('该活动已取消'));

/* ---------- 5. 我的报名 ---------- */
section('5. 学生：我的报名');
click('nav', { 'data-page': 'my' });
h = appHtml();
check('含「已签到」记录', h.includes('已签到'));
check('含「缺席」记录（已结束未签到）', h.includes('缺席'));
check('含「待签到」记录（进行中）', h.includes('待签到'));

/* ---------- 6. 社团提交审核 ---------- */
section('6. 社团：提交草稿审核');
click('logout');
click('login', { 'data-role': 'CLUB' });
h = appHtml();
check('进入「我的活动」', h.includes('我的活动'));
check('能看到自己的草稿活动', h.includes('校园夜景采风'));
check('草稿带「提交审核」按钮', h.includes('data-act="submit-audit" data-id="12"'));

click('submit-audit', { 'data-id': 12 });
h = appHtml();
check('提交后草稿变为待审核', h.includes('data-act="withdraw" data-id="12"'));
check('提交后不再显示提交按钮', !h.includes('data-act="submit-audit" data-id="12"'));

/* ---------- 7. 表单校验 ---------- */
section('7. 社团：新建活动与表单校验');
click('nav', { 'data-page': 'club-create' });
h = appHtml();
check('表单已渲染', h.includes('id="f-title"') && h.includes('id="f-capacity"'));

fill({
  'f-title': '',
  'f-location': '传媒楼 201',
  'f-capacity': '40',
  'f-start': toLocal(96),
  'f-end': toLocal(99),
  'f-deadline': toLocal(72),
});
click('submit-audit-new');
check('空标题被拦截', appHtml().includes('活动标题不能为空'));

fill({
  'f-title': '摄影作品点评会',
  'f-location': '传媒楼 201',
  'f-capacity': '40',
  'f-start': toLocal(96),
  'f-end': toLocal(90),
  'f-deadline': toLocal(72),
});
click('submit-audit-new');
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
});
click('submit-audit-new');
h = appHtml();
check('提交成功并回到「我的活动」', h.includes('我的活动'));
check('新活动出现在列表中', h.includes('摄影作品点评会'));
check('新活动状态为待审核', h.includes('data-act="withdraw" data-id="14"'));

/* ---------- 8. 管理员审核 ---------- */
section('8. 管理员：审核通过 / 驳回');
click('logout');
click('login', { 'data-role': 'ADMIN' });
h = appHtml();
check('进入「审核中心」', h.includes('审核中心'));
check('待审核列表含社团新提交的活动', h.includes('data-act="approve" data-id="14"'));
check('待审核列表含其他社团的活动', h.includes('data-act="approve" data-id="3"'));
check('审核卡片展示提交人', h.includes('提交人'));

click('reject', { 'data-id': 9 });
check('驳回时弹出原因输入框', !!node('modal-root').lastChild);
pressModal('ok', '活动时间与校运会冲突，请调整后重新提交');
h = appHtml();
check('驳回后移出待审核列表', !h.includes('data-act="approve" data-id="9"'));
check('驳回原因写入审核日志', h.includes('活动时间与校运会冲突'));

click('approve', { 'data-id': 12 });
h = appHtml();
check('通过后移出待审核列表', !h.includes('data-act="approve" data-id="12"'));
check('审核动作记入日志', h.includes('审核通过'));

/* ---------- 9. 社团复查状态流转 ---------- */
section('9. 社团：驳回后重新提交');
click('logout');
click('login', { 'data-role': 'CLUB' });
h = appHtml();
check('社团能看到驳回原因', h.includes('活动时间与校运会冲突'));
check('被驳回活动可重新提交', h.includes('data-act="resubmit" data-id="9"'));
check('已通过活动进入已发布状态', h.includes('data-act="goto-roster" data-id="12"'));

click('resubmit', { 'data-id': 9 });
check('重新提交后回到待审核', appHtml().includes('data-act="withdraw" data-id="9"'));

/* ---------- 10. 签到与导出 ---------- */
section('10. 社团：签到核验与名单导出');
click('goto-roster', { 'data-id': 1 });
h = appHtml();
check('签到页显示 6 位签到码', h.includes('482913'));
check('签到页显示报名人数', h.includes('报名人数'));
check('初始签到率 56.8%（21 / 37）', h.includes('56.8%'));
check('名单里标记出「我」', h.includes('>我</span>'));

click('toggle-checkin', { 'data-id': 1, 'data-key': 's25' });
check('勾选签到后签到率升到 59.5%', appHtml().includes('59.5%'));

click('toggle-checkin', { 'data-id': 1, 'data-key': 's25' });
check('撤销签到后回到 56.8%', appHtml().includes('56.8%'));

lastBlob = null;
click('export', { 'data-id': 1 });
check('导出名单生成了文件内容', !!lastBlob);
if (lastBlob) {
  const csv = lastBlob.parts[0];
  check('CSV 带 UTF-8 BOM（Excel 打开不乱码）', csv.charCodeAt(0) === 0xfeff);
  check('CSV 表头正确', csv.includes('学号') && csv.includes('签到状态'));
  check('CSV 行数 = 报名人数 + 表头', csv.trim().split('\r\n').length === 38);
}

/* ---------- 11. 统计 ---------- */
section('11. 社团：数据统计');
click('goto-stats');
h = appHtml();
check('统计页显示整体签到率', h.includes('整体签到率'));
check('统计页解释签到率与上座率', h.includes('上座率'));
check('统计页有两列指标', h.includes('签到率') && h.includes('上座率'));

lastBlob = null;
click('export-stats');
check('统计汇总可导出', !!lastBlob && lastBlob.parts[0].includes('签到率'));

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
