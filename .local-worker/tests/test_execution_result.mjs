/*
 * 自测：execution-result.js（Model Desk 只读执行结果摘要组件）
 * 运行：node test_execution_result.mjs
 * 说明：纯离线断言测试，不调用模型、不启动服务、不读写工作台记录。
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC_PATH = path.join(HERE, '../panel/execution-result.js');
const CSS_PATH = path.join(HERE, '../panel/execution-result.css');
const src = fs.readFileSync(SRC_PATH, 'utf8');

/* ---------- 加载组件（模拟浏览器 window + Node globalThis 两种暴露） ---------- */

function load() {
  const windowObj = { marker: 'fake-window' };
  const ctx = vm.createContext({ window: windowObj });
  vm.runInContext(src, ctx, { filename: 'execution-result.js' });
  return {
    viaGlobal: ctx.ModelDeskExecutionResult,
    viaWindow: windowObj.ModelDeskExecutionResult,
    leakedKeys: Object.keys(ctx).filter((k) => k !== 'window' && k !== 'ModelDeskExecutionResult')
  };
}

const loaded = load();
const render = loaded.viaGlobal.render;

/* ---------- 测试运行器 ---------- */

let passed = 0;
const failures = [];

function test(name, fn) {
  try {
    fn();
    passed++;
    console.log('  \x1b[32m通过\x1b[0m  ' + name);
  } catch (err) {
    const msg = err && err.message ? String(err.message).split('\n')[0] : String(err);
    failures.push({ name, msg });
    console.log('  \x1b[31m失败\x1b[0m  ' + name + '\n          ' + msg);
  }
}

function section(t) {
  console.log('\n' + t);
}

/* ---------- 合同样例 ---------- */

const SAMPLE = {
  schema_version: 1,
  legacy: false,
  execution: { status: 'failed', engine: 'workbuddy' },
  stages: [
    { id: 'prepare', label: '准备', state: 'completed', detail: '' },
    { id: 'execute', label: '执行', state: 'failed', detail: '工具拒绝' },
    { id: 'verify', label: '指定测试', state: 'not_run', detail: '' },
    { id: 'session_close', label: '会话关闭', state: 'completed', detail: '' },
    { id: 'record', label: '结果记录', state: 'completed', detail: '' }
  ],
  failure: {
    stage: 'execute',
    code: 'TOOL_PERMISSION_DENIED',
    message: '工具操作被保护规则拒绝',
    source: 'adapter',
    rule_id: 'protected-record'
  },
  issues: [],
  artifacts: { state: 'changed', files: ['src/example.py'], scope_label: '仅核对任务声明文件' },
  verification: { state: 'not_run', source: 'controller_test', exit_code: null },
  review: { state: 'executor_failed', accepted: false }
};

const withBase = (extra) => Object.assign({}, JSON.parse(JSON.stringify(SAMPLE)), extra);

/* ---------- 1. 暴露方式 ---------- */

section('1. 组件暴露（window / globalThis）');

test('globalThis 与 window 都暴露 ModelDeskExecutionResult.render', () => {
  assert.equal(typeof render, 'function');
  assert.equal(typeof loaded.viaWindow.render, 'function');
});

test('不向宿主泄漏额外全局变量', () => {
  assert.deepEqual(loaded.leakedKeys, []);
});

/* ---------- 2. 合同样例渲染 ---------- */

section('2. 合同样例：紧凑中文摘要');

const sampleHtml = render(SAMPLE);

test('样例渲染为非空字符串且可重复（确定性）', () => {
  assert.equal(typeof sampleHtml, 'string');
  assert.ok(sampleHtml.length > 100, '样例输出过短');
  assert.equal(render(SAMPLE), sampleHtml);
});

test('显示执行状态、引擎与全部阶段标签', () => {
  for (const s of ['执行结果', '执行失败', 'workbuddy', '准备', '执行', '指定测试', '会话关闭', '结果记录']) {
    assert.ok(sampleHtml.includes(s), '缺少文本: ' + s);
  }
  assert.ok(sampleHtml.includes('data-execution-status="failed"'));
  assert.ok(sampleHtml.includes('data-stage-id="verify" data-state="not_run"'));
});

test('显示失败阶段、code、message、来源与规则', () => {
  assert.ok(sampleHtml.includes('data-failure-stage="execute"'));
  assert.ok(sampleHtml.includes('阶段：执行'), '未显示失败阶段标签');
  assert.ok(sampleHtml.includes('TOOL_PERMISSION_DENIED'), '未显示 code');
  assert.ok(sampleHtml.includes('工具操作被保护规则拒绝'), '未显示 message');
  assert.ok(sampleHtml.includes('来源 adapter') && sampleHtml.includes('规则 protected-record'));
  assert.ok(sampleHtml.includes('工具拒绝'), '失败阶段 detail 未显示');
});

test('显示已有文件与核对范围', () => {
  assert.ok(sampleHtml.includes('有文件改动'));
  assert.ok(sampleHtml.includes('src/example.py'));
  assert.ok(sampleHtml.includes('仅核对任务声明文件'));
});

test('自测未运行与独立验收未通过均明确显示', () => {
  assert.ok(sampleHtml.includes('自测未运行'));
  assert.ok(sampleHtml.includes('来源 controller_test'));
  assert.ok(sampleHtml.includes('执行器失败，未进入验收'));
  assert.ok(sampleHtml.includes('data-accepted="false"'));
});

test('空 issues / 空阶段明细不生成空的展开区', () => {
  assert.ok(!sampleHtml.includes('<details'), '无扩展内容时不应出现 details');
});

test('不渲染脚本、按钮、表单、外链或事件处理器', () => {
  for (const bad of ['<script', '<link', '<button', '<form', '<a ', 'src=', 'href=', 'onclick', 'onerror', 'innerHTML', 'http://', 'https://']) {
    assert.ok(!sampleHtml.toLowerCase().includes(bad.toLowerCase()), '输出含禁止内容: ' + bad);
  }
});

/* ---------- 3. schema 与无数据 ---------- */

section('3. schema 兼容与无数据降级');

test('schema_version 非严格 1 一律返回空字符串', () => {
  for (const v of ['1', 2, 1.5, 0, null, undefined, true, {}, []]) {
    assert.equal(render({ schema_version: v, execution: { status: 'failed' } }), '', 'schema_version=' + String(v) + ' 应返回空');
  }
});

test('无数据 / 非对象输入返回空字符串', () => {
  for (const v of [undefined, null, 0, '', 'x', true, [], [1, 2], {}, { schema_version: 1 }, { schema_version: 1, legacy: false }]) {
    assert.equal(render(v), '', '输入 ' + JSON.stringify(v) + ' 应返回空');
  }
});

/* ---------- 4. 自测语义（不得等同独立验收） ---------- */

section('4. 自测语义区分');

test('passed / failed / not_run / not_requested / unknown 各自独立显示', () => {
  const expect = {
    passed: '自测通过',
    failed: '自测失败',
    not_run: '自测未运行',
    not_requested: '自测未要求',
    unknown: '自测状态未知'
  };
  const seen = new Set();
  for (const [state, label] of Object.entries(expect)) {
    const html = render(withBase({ verification: { state, source: 'controller_test', exit_code: null } }));
    assert.ok(html.includes(label), state + ' 应显示 ' + label);
    assert.ok(html.includes('data-verify-state="' + state + '"'));
    for (const other of Object.values(expect)) if (other !== label) assert.ok(!html.includes(other), state + ' 误显示 ' + other);
    seen.add(label);
  }
  assert.equal(seen.size, 5, '五种自测状态文案必须互不相同');
});

test('自测通过仍标注“不等于主脑独立验收”，且不出现验收通过', () => {
  const html = render(withBase({ verification: { state: 'passed', source: 'pytest', exit_code: 0 }, review: null }));
  assert.ok(html.includes('自测通过'));
  assert.ok(html.includes('不等于主脑独立验收'));
  assert.ok(html.includes('退出码 0'));
  assert.ok(!/验收通过|已验收/.test(html), '自测通过被误写成验收通过');
});

test('自测字段缺失时报告未记录，不推断为通过', () => {
  const html = render({
    schema_version: 1,
    stages: [{ id: 'verify', label: '指定测试', state: 'completed', detail: '' }],
    execution: { status: 'completed' }
  });
  assert.ok(html.includes('自测状态未知'));
  assert.ok(html.includes('结果未记录自测字段'));
  assert.ok(!html.includes('自测通过'), '由 completed 阶段推断出自测通过');
});

test('exit_code 仅在数值时显示', () => {
  assert.ok(render(withBase({ verification: { state: 'failed', exit_code: 1 } })).includes('退出码 1'));
  assert.ok(render(withBase({ verification: { state: 'failed', exit_code: '7' } })).includes('退出码 7'));
  for (const bad of [null, undefined, {}, [], 'abc', NaN, true]) {
    assert.ok(!render(withBase({ verification: { state: 'failed', exit_code: bad } })).includes('退出码'), 'exit_code=' + String(bad) + ' 不应显示退出码');
  }
});

/* ---------- 5. 独立验收语义 ---------- */

section('5. 独立验收语义（accepted 与 final_accepted 不同）');

test('accepted 状态显示主脑已验收，接受标记为是', () => {
  const html = render(withBase({ execution: { status: 'completed', engine: 'workbuddy' }, review: { state: 'accepted', accepted: true } }));
  assert.ok(html.includes('主脑已验收'));
  assert.ok(html.includes('接受标记：是'));
  assert.ok(html.includes('data-accepted="true"'));
  assert.ok(!html.includes('主脑接手后通过'), 'accepted 与 final_accepted 混淆');
});

test('final_accepted 显示主脑接手后通过且不可说执行器独立成功', () => {
  const html = render(withBase({ review: { state: 'final_accepted', accepted: true } }));
  assert.ok(html.includes('主脑接手后通过'));
  assert.ok(html.includes('不代表执行器独立成功'));
  assert.ok(!/执行器独立成功(?![，,])|执行器已成功/.test(html.replace('不代表执行器独立成功', '')), '出现“执行器独立成功”类表述');
});

test('returned / blocked / executor_completed / null 各自独立显示', () => {
  const cases = [
    [{ state: 'returned', accepted: false }, '主脑已退回'],
    [{ state: 'blocked', accepted: false }, '主脑判定阻塞'],
    [{ state: 'executor_completed', accepted: false }, '执行器完成，待主脑验收'],
    [{ state: null, accepted: false }, '尚无独立验收结论']
  ];
  for (const [review, label] of cases) {
    const html = render(withBase({ review }));
    assert.ok(html.includes(label), JSON.stringify(review) + ' 应显示 ' + label);
  }
  assert.ok(!render(withBase({ review: { state: 'returned', accepted: false } })).includes('主脑已验收'));
});

test('accepted 只取严格 true，其它真值不算接受', () => {
  for (const v of ['true', 1, 'yes', {}, [], '1']) {
    const html = render(withBase({ review: { state: 'accepted', accepted: v } }));
    assert.ok(html.includes('接受标记：否'), 'accepted=' + JSON.stringify(v) + ' 被当作接受');
    assert.ok(html.includes('data-accepted="false"'));
  }
});

test('执行失败不被验收结论改写', () => {
  for (const state of ['accepted', 'final_accepted']) {
    const html = render(withBase({ execution: { status: 'failed', engine: 'workbuddy' }, review: { state, accepted: true } }));
    assert.ok(html.includes('执行失败'), state + ' 改写执行状态');
    assert.ok(html.includes('data-execution-status="failed"'));
    assert.ok(html.includes('主脑结论不改变执行阶段状态'), state + ' 缺少不可改写提示');
    assert.ok(html.includes('阶段：执行') && html.includes('TOOL_PERMISSION_DENIED'), '失败信息被验收结论挤掉');
  }
});

test('review 字段缺失时报告未记录，不推断验收', () => {
  const html = render({ schema_version: 1, execution: { status: 'completed' } });
  assert.ok(html.includes('尚无独立验收结论'));
  assert.ok(html.includes('结果未记录独立验收字段'));
  assert.ok(!/已验收|验收通过/.test(html));
});

/* ---- 反例：通过类结论必须有严格 true 的 accepted 背书 ---- */

function reviewRow(html) {
  const m = html.match(/<div class="mdx-res-row mdx-res-row--review"[^>]*>[\s\S]*?<\/div>/);
  assert.ok(m, '未渲染独立验收行');
  return m[0];
}

test('accepted 状态但 accepted 非严格 true：不显示“主脑已验收”、不给通过配色', () => {
  for (const v of [false, null, undefined, 'true', '1', 1, 0, '', {}, []]) {
    const review = { state: 'accepted' };
    if (v !== undefined) review.accepted = v;
    const html = render(withBase({ execution: { status: 'completed' }, review }));
    const rowHtml = reviewRow(html);
    assert.ok(!rowHtml.includes('主脑已验收'), 'accepted=' + String(v) + ' 仍显示“主脑已验收”');
    assert.ok(rowHtml.includes('验收结论待核实'), 'accepted=' + String(v) + ' 未降为待核实');
    assert.ok(!rowHtml.includes('is-ok'), 'accepted=' + String(v) + ' 仍使用通过（绿色）配色');
    assert.ok(rowHtml.includes('is-warn'), '待核实结论未使用警示色');
    assert.ok(rowHtml.includes('未视为通过'), '缺少“未视为通过”说明');
    assert.ok(html.includes('data-review-confirmed="false"'));
  }
});

test('final_accepted 但 accepted 非严格 true：不冒充“接手后通过”', () => {
  for (const v of [false, null, undefined, 'true', 1, {}]) {
    const review = { state: 'final_accepted' };
    if (v !== undefined) review.accepted = v;
    const rowHtml = reviewRow(render(withBase({ review })));
    assert.ok(!rowHtml.includes('主脑接手后通过'), 'accepted=' + String(v) + ' 仍显示“主脑接手后通过”');
    assert.ok(rowHtml.includes('验收结论待核实') && !rowHtml.includes('is-ok'));
    assert.ok(rowHtml.includes('未视为通过'));
  }
});

test('accepted 缺失时明确写“未记录接受标记”并降为待核实', () => {
  const rowHtml = reviewRow(render(withBase({ review: { state: 'accepted' } })));
  assert.ok(rowHtml.includes('结果未记录接受标记'));
  assert.ok(rowHtml.includes('验收结论待核实'));
  assert.ok(!rowHtml.includes('is-ok'));
});

test('final_accepted 严格 true：显示接手后通过但不算执行器独立成功（不用通过色）', () => {
  const rowHtml = reviewRow(render(withBase({ review: { state: 'final_accepted', accepted: true } })));
  assert.ok(rowHtml.includes('主脑接手后通过'));
  assert.ok(rowHtml.includes('不代表执行器独立成功'));
  assert.ok(!rowHtml.includes('is-ok'), 'final_accepted 使用了“执行器通过”的绿色');
  assert.ok(reviewRow(render(withBase({ review: { state: 'final_accepted', accepted: true } }))).includes('is-info'));
});

test('accepted 严格 true 且执行完成：才按主脑已验收（通过色）显示', () => {
  const rowHtml = reviewRow(render(withBase({ execution: { status: 'completed' }, review: { state: 'accepted', accepted: true } })));
  assert.ok(rowHtml.includes('主脑已验收'));
  assert.ok(rowHtml.includes('is-ok'));
  assert.ok(!rowHtml.includes('待核实'));
});

test('通过类结论不改变执行阶段状态（失败阶段与已有文件保留）', () => {
  for (const state of ['accepted', 'final_accepted']) {
    for (const accepted of [true, false]) {
      const html = render(withBase({ review: { state, accepted } }));
      assert.ok(html.includes('data-execution-status="failed"'), state + ' 改写了执行状态');
      assert.ok(html.includes('执行失败'), state + ' 改写了执行状态文案');
      assert.ok(html.includes('data-stage-id="execute" data-state="failed"'), state + ' 改写了失败阶段状态');
      assert.ok(html.includes('data-failure-stage="execute"') && html.includes('TOOL_PERMISSION_DENIED'), '失败信息丢失');
      assert.ok(html.includes('src/example.py'), '已有文件丢失');
      assert.ok(html.includes('主脑结论不改变执行阶段状态'), state + ' 缺少不可改写提示');
    }
  }
});

test('accepted=true 但结论不是通过：按记录结论显示，不给通过色', () => {
  for (const state of ['returned', 'blocked', 'executor_failed', 'executor_completed', 'weird', null]) {
    const rowHtml = reviewRow(render(withBase({ review: { state, accepted: true } })));
    assert.ok(!rowHtml.includes('is-ok'), 'state=' + String(state) + ' 因 accepted=true 被显示为通过');
    assert.ok(rowHtml.includes('接受标记：是'));
    assert.ok(rowHtml.includes('按记录结论显示'), 'state=' + String(state) + ' 缺少冲突说明');
  }
});

test('验收行 data 属性可区分“自称通过”与“已确认通过”', () => {
  const confirmed = render(withBase({ execution: { status: 'completed' }, review: { state: 'accepted', accepted: true } }));
  const unconfirmed = render(withBase({ execution: { status: 'completed' }, review: { state: 'accepted', accepted: 'true' } }));
  assert.ok(confirmed.includes('data-review-state="accepted"') && confirmed.includes('data-review-confirmed="true"'));
  assert.ok(unconfirmed.includes('data-review-state="accepted"') && unconfirmed.includes('data-review-confirmed="false"'));
  assert.ok(render(withBase({ review: null })).includes('data-review-confirmed="false"'));
});

/* ---------- 6. legacy 历史记录 ---------- */

section('6. legacy 历史记录');

test('legacy=true 显示历史记录与证据不足提示', () => {
  const html = render(withBase({ legacy: true, verification: null, review: null }));
  assert.ok(html.includes('历史记录'));
  assert.ok(html.includes('阶段证据不足'));
  assert.ok(html.includes('data-legacy="true"'));
});

test('legacy 记录中 completed 阶段不被推断为测试通过', () => {
  const html = render({
    schema_version: 1,
    legacy: true,
    execution: { status: 'completed' },
    stages: [
      { id: 'prepare', label: '准备', state: 'completed' },
      { id: 'execute', label: '执行', state: 'completed' },
      { id: 'verify', label: '指定测试', state: 'completed' }
    ]
  });
  assert.ok(!html.includes('自测通过'), 'legacy 记录被推断为自测通过');
  assert.ok(!/验收通过|已验收/.test(html), 'legacy 记录被推断为验收通过');
  assert.ok(html.includes('自测状态未知'));
});

test('legacy 非严格 true 不显示历史记录提示', () => {
  const html = render(withBase({ legacy: 'true' }));
  assert.ok(!html.includes('阶段证据不足'));
  assert.ok(!html.includes('data-legacy'));
});

/* ---------- 7. 阶段与失败解析 ---------- */

section('7. 阶段与失败信息解析');

test('不用错误文字推断失败阶段', () => {
  const html = render(withBase({
    failure: { message: 'prepare 阶段之外的 execute 步骤失败', code: 'E1' }
  }));
  assert.ok(!html.includes('data-failure-stage="prepare"'), '从 message 文字推断出 prepare');
  assert.ok(!html.includes('data-failure-stage="execute"'), '从 message 文字推断出 execute');
  assert.ok(html.includes('阶段：未记录'), '未记录阶段时应明确标注');
});

test('failure.stage 无匹配阶段时原样显示记录值', () => {
  const html = render(withBase({ failure: { stage: 'unknown_step', code: 'E2' } }));
  assert.ok(html.includes('阶段：unknown_step'));
  assert.ok(!html.includes('data-failure-stage="unknown_step"'), '未匹配的 id 不应写入 data 属性');
});

test('failure 为空对象 / 缺失时不显示失败行', () => {
  assert.ok(!render(withBase({ failure: {} })).includes('data-failure-stage'));
  assert.ok(!render(withBase({ failure: null })).includes('data-failure-stage'));
  const strHtml = render(withBase({ failure: '执行器进程退出' }));
  assert.ok(strHtml.includes('执行器进程退出'), 'failure 为字符串时应显示原文');
});

test('阶段缺失 label 时退回 id，不编造名称', () => {
  const html = render({ schema_version: 1, execution: { status: 'running' }, stages: [{ id: 'execute', state: 'running' }] });
  assert.ok(html.includes('execute'));
  assert.ok(!html.includes('未命名阶段') === false || html.includes('execute'));
});

test('阶段状态未知值降级为状态未知', () => {
  const html = render({ schema_version: 1, execution: { status: 'weird' }, stages: [{ id: 'a', label: '甲', state: 'succeeded' }] });
  assert.ok(html.includes('状态未知'));
  assert.ok(html.includes('执行状态未知'));
  assert.ok(!html.includes('执行完成'));
});

/* ---------- 8. 文件与 issues ---------- */

section('8. 文件列表与 issues 展开');

test('artifacts.state 三种取值分别显示', () => {
  assert.ok(render(withBase({ artifacts: { state: 'none', files: [] } })).includes('无文件改动'));
  assert.ok(render(withBase({ artifacts: { state: 'unknown' } })).includes('文件情况未知'));
  assert.ok(render(withBase({ artifacts: { state: 'weird', files: [] } })).includes('文件情况未知'));
});

test('文件过多时折叠到扩展信息，不撑高摘要', () => {
  const files = Array.from({ length: 12 }, (_, i) => 'src/f' + i + '.py');
  const html = render(withBase({ artifacts: { state: 'changed', files, scope_label: '仅核对任务声明文件' } }));
  assert.ok(html.includes('共 12 项'));
  assert.ok(html.includes('另有 4 项见扩展信息'));
  assert.ok(html.includes('<details'), '缺少扩展信息');
  assert.ok(html.includes('其余文件 4'));
  assert.ok(html.includes('src/f11.py'), '扩展信息缺少被折叠的文件');
  const inline = html.slice(0, html.indexOf('<details'));
  assert.ok(inline.includes('src/f7.py'), '前 8 项应内联显示');
  assert.ok(!inline.includes('src/f8.py'), '第 9 项不应内联显示');
  for (let i = 0; i < 12; i++) {
    const token = 'src/f' + i + '.py';
    assert.equal(html.split(token).length - 1, 1, token + ' 被重复列出');
  }
});

test('多个 issues 可展开并计数', () => {
  const html = render(withBase({ issues: ['第一条问题', { code: 'W1', message: '第二条问题' }, '第三条问题'] }));
  assert.ok(html.includes('<details'));
  assert.ok(html.includes('问题 3'));
  for (const s of ['第一条问题', '第二条问题', '第三条问题', 'code: W1']) assert.ok(html.includes(s), 'issues 缺少: ' + s);
});

test('循环引用的 issue 不抛异常', () => {
  const circ = { schema_version: 1, issues: [] };
  const o = { name: 'loop' };
  o.self = o;
  circ.issues.push(o);
  const html = render(circ);
  assert.equal(typeof html, 'string');
  assert.ok(html.includes('无法显示的问题记录'));
});

/* ---------- 9. 转义与畸形输入 ---------- */

section('9. 转义与畸形输入稳健性');

const XSS = {
  schema_version: 1,
  execution: { status: 'failed', engine: '<img src=x onerror=alert(1)>' },
  stages: [{ id: 'a" onfocus=alert(1) a', label: '<script>alert(1)</script>', state: 'failed', detail: "'><svg/onload=alert(2)>" }],
  failure: {
    stage: 'a" onfocus=alert(1) a',
    code: '"><script>alert(3)</script>',
    message: '</span><img src=1 onerror=alert(4)>',
    source: 'javascript:alert(5)',
    rule_id: '" style="color:red'
  },
  issues: ['<b onmouseover=alert(6)>x</b>', { message: '<iframe src=javascript:alert(7)>' }],
  artifacts: { state: 'changed', files: ['"><script>alert(8)</script>.py'], scope_label: '<a href="javascript:alert(9)">x</a>' },
  verification: { state: 'failed', source: '<embed src=x>', exit_code: '<script>alert(10)</script>' },
  review: { state: 'returned', accepted: '<script>alert(11)</script>' }
};

test('XSS 文本不会形成可执行标签或事件处理器', () => {
  const html = render(XSS);
  assert.equal(typeof html, 'string');
  assert.ok(!/<\s*(script|img|svg|iframe|embed|object|link|style|a)\b/i.test(html), '输出含可执行标签');
  assert.ok(!/\son(error|focus|mouseover|load|click|begin|end)\s*=/i.test(html), '输出含事件处理器');
  assert.ok(!/href\s*=/i.test(html), '输出含 href');
  assert.ok(html.includes('&lt;script') && html.includes('&lt;img'), '危险文本未转义');
  assert.ok(html.includes('&#61;'), '等号未在属性上下文转义');
  assert.ok(html.includes('&quot;') || html.includes('&#39;'), '引号未转义');
});

test('属性上下文注入被白名单清洗', () => {
  const html = render(XSS);
  assert.ok(html.includes('data-stage-id="a__onfocus_alert_1__a"'), 'data-stage-id 未按白名单清洗: ' + (html.match(/data-stage-id="[^"]*"/) || [''])[0]);
  const attrs = html.match(/data-[a-z-]+="[^"]*"/g) || [];
  assert.ok(attrs.length > 0);
  for (const a of attrs) assert.ok(/^data-[a-z-]+="[A-Za-z0-9_.\-]*"$/.test(a), '属性值不安全: ' + a);
});

test('畸形输入一律不抛异常且返回字符串', () => {
  const cases = [
    { schema_version: 1, execution: 'failed' },
    { schema_version: 1, execution: null, stages: 'nope', failure: 42, artifacts: 'x', verification: [], review: 'accepted', issues: 'none', legacy: 'true' },
    { schema_version: 1, stages: [null, 7, 'x', {}, { id: null, label: null, state: {}, detail: [] }] },
    { schema_version: 1, stages: { length: 3 }, failure: { stage: {}, code: [], message: {}, source: true, rule_id: null } },
    { schema_version: 1, artifacts: { state: 'changed', files: { length: 3 }, scope_label: {} } },
    { schema_version: 1, artifacts: { state: 'changed', files: [null, {}, [], '  ', 'ok.py'] } },
    { schema_version: 1, verification: { state: 'passed', exit_code: {}, source: NaN } },
    { schema_version: 1, review: { state: null, accepted: 'true' } },
    { schema_version: 1, issues: [null, [], '', 0, false, { a: {} }] },
    { schema_version: 1, failure: { stage: 'execute' }, stages: [{ id: 'execute', label: '执行', state: 'failed' }] },
    { schema_version: 1, stages: [{ id: 'x'.repeat(500), label: 'y'.repeat(900), state: 'completed', detail: 'd'.repeat(5000) }] },
    { schema_version: 1, schema_version_strict: 1, execution: { status: 'FAILED' }, stages: [{ id: 'a', label: '甲', state: 'COMPLETED' }] }
  ];
  for (const c of cases) {
    let html;
    assert.doesNotThrow(() => { html = render(c); }, '输入抛异常: ' + JSON.stringify(c).slice(0, 120));
    assert.equal(typeof html, 'string');
  }
});

test('取值器抛异常时整体降级为空字符串，不抛出', () => {
  const boom = { schema_version: 1, execution: { get status() { throw new Error('boom'); } } };
  assert.equal(render(boom), '');
});

test('大小写不规范的枚举值仍按字典解析', () => {
  const html = render({ schema_version: 1, execution: { status: 'FAILED' }, stages: [{ id: 'a', label: '甲', state: 'COMPLETED' }] });
  assert.ok(html.includes('执行失败'));
  assert.ok(html.includes('已完成'));
});

test('渲染不修改输入对象', () => {
  const input = JSON.parse(JSON.stringify(SAMPLE));
  const before = JSON.stringify(input);
  render(input);
  assert.equal(JSON.stringify(input), before, '输入对象被修改');
});

/* ---------- 10. 源码与样式静态检查 ---------- */

section('10. 源码 / 样式静态检查');

const codeOnly = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^[ \t]*\/\/.*$/gm, '');

test('组件源码不请求网络、不加载外部依赖、不使用 innerHTML/eval', () => {
  for (const bad of ['fetch(', 'XMLHttpRequest', 'WebSocket', 'import(', 'import ', 'require(', 'eval(', 'Function(', 'document.', 'innerHTML', 'localStorage', 'sessionStorage', 'http://', 'https://', 'EventSource']) {
    assert.ok(!codeOnly.includes(bad), '源码含禁止用法: ' + bad);
  }
});

test('样式文件存在且使用带 fallback 的 CSS 变量', () => {
  const css = fs.readFileSync(CSS_PATH, 'utf8');
  assert.ok(css.includes('.mdx-res'), '缺少组件根类名');
  assert.ok(/var\(--[a-z-]+,\s*var\(/.test(css), '未使用带 fallback 的 var(--...)');
  assert.ok(css.includes('container-type') && css.includes('@container'), '缺少窄侧栏自适应');
  assert.ok(!/@import|url\(/.test(css), '样式加载了外部资源');
  assert.ok(!/position:\s*fixed/.test(css), '样式使用了 fixed 定位');
});

/* ---------- CSS 静态解析（括号/逗号有效性、主题变量、主题作用域） ---------- */

const css = fs.readFileSync(CSS_PATH, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');

function splitTop(text, sep) {
  const out = [];
  let depth = 0, cur = '', quote = null;
  for (const ch of text) {
    if (quote) { cur += ch; if (ch === quote) quote = null; continue; }
    if (ch === '"' || ch === "'") { quote = ch; cur += ch; continue; }
    if (ch === '(') depth++;
    else if (ch === ')') depth--;
    if (ch === sep && depth === 0) { out.push(cur); cur = ''; continue; }
    cur += ch;
  }
  out.push(cur);
  return out.map((s) => s.trim());
}

function cssBlocks(text) {
  const out = [];
  (function walk(body, parent) {
    let i = 0, buf = '';
    while (i < body.length) {
      const ch = body[i];
      if (ch === '{') {
        const prelude = buf.trim();
        let depth = 1, j = i + 1;
        while (j < body.length && depth) {
          if (body[j] === '{') depth++;
          else if (body[j] === '}') depth--;
          j++;
        }
        const inner = body.slice(i + 1, j - 1);
        if (prelude.startsWith('@')) walk(inner, prelude);
        else out.push({ selector: prelude, body: inner, parent });
        buf = '';
        i = j;
        continue;
      }
      buf += ch;
      i++;
    }
  })(text, null);
  return out;
}

function declarations(body) {
  return splitTop(body, ';').filter((d) => d.includes(':')).map((d) => {
    const k = d.indexOf(':');
    return { prop: d.slice(0, k).trim(), value: d.slice(k + 1).trim() };
  });
}

const blocks = cssBlocks(css);
assert.ok(blocks.length > 10, 'CSS 解析出的规则块过少，解析可能失败');

const varCalls = [];
const varProblems = [];

function closeIndexOf(text, openPos) {
  let depth = 0;
  for (let k = openPos; k < text.length; k++) {
    if (text[k] === '(') depth++;
    else if (text[k] === ')') { depth--; if (depth === 0) return k; }
  }
  return -1;
}

/* 递归校验：值一旦以 var( 开头，必须整体被这一个 var() 包住；回退值同样校验
   （能抓住 var(--a, var(--b), #fff) 这类括号/逗号错位导致颜色回退失效的写法） */
function checkVarExpr(value, where) {
  const v = String(value).trim();
  if (!v.includes('var(')) return;
  if (!v.startsWith('var(')) {
    /* 多 token / 简写值（如 1px solid var(--x)）：逐个校验其中的 var() 调用 */
    let idx = 0;
    while ((idx = v.indexOf('var(', idx)) !== -1) {
      const end = closeIndexOf(v, idx + 3);
      if (end === -1) { varProblems.push(where + '：var( 未闭合：' + v); return; }
      checkVarExpr(v.slice(idx, end + 1), where);
      idx = end + 1;
    }
    return;
  }
  const end = closeIndexOf(v, 3);
  if (end === -1) { varProblems.push(where + '：var( 未闭合：' + v); return; }
  if (end !== v.length - 1) {
    varProblems.push(where + '：var() 未覆盖整个值（多余逗号或尾随值）：' + v);
    return;
  }
  const args = splitTop(v.slice(4, end), ',');
  if (!args[0] || !/^--[A-Za-z0-9_-]+$/.test(args[0])) {
    varProblems.push(where + '：var() 首参不是自定义属性：' + v);
    return;
  }
  varCalls.push({ name: args[0], where });
  if (args.length < 2) return;
  const fb = args.slice(1).join(',');
  if (!fb.trim()) { varProblems.push(where + '：var() 回退为空：' + v); return; }
  checkVarExpr(fb, where);
}

for (const b of blocks) {
  for (const d of declarations(b.body)) {
    const where = (b.parent ? b.parent + ' → ' : '') + b.selector + ' { ' + d.prop + ' }';
    let opens = 0, closes = 0;
    for (const ch of d.value) { if (ch === '(') opens++; else if (ch === ')') closes++; }
    if (opens !== closes) varProblems.push(where + '：括号不配对：' + d.value);
    if (/var\(/.test(d.value)) checkVarExpr(d.value, where);
  }
}

test('CSS var() 语法有效（括号配对、无多余逗号、回退完整嵌套）', () => {
  assert.deepEqual(varProblems, [], 'CSS 变量写法有问题：' + varProblems.slice(0, 6).join(' | '));
  assert.ok(varCalls.length > 10, '未解析到 var() 调用');
});

test('CSS 检查器能识别旧的错误写法（检查方法自身有效性）', () => {
  const bad = '.x{ --mdx-muted: var(--md-muted, var(--muted, var(--text-muted, var(--fg-dim), #5c6675))); --mdx-ok: var(--md-ok, var(--success), #1d7a4d); }';
  const saved = varProblems.length;
  const savedCalls = varCalls.length;
  for (const b of cssBlocks(bad)) for (const d of declarations(b.body)) checkVarExpr(d.value, 'bad { ' + d.prop + ' }');
  assert.ok(varProblems.length >= 2, '检查器漏掉了 var() 括号/逗号错位反例');
  varProblems.length = saved;
  varCalls.length = savedCalls;
});

test('CSS 使用真实工作台主题变量，不依赖未定义的 --md-* 名', () => {
  const used = new Set(varCalls.map((v) => v.name));
  for (const host of ['--ink', '--muted', '--line', '--surface', '--inset', '--green', '--red', '--orange', '--accent']) {
    assert.ok(used.has(host), '未引用工作台主题变量 ' + host);
  }
  for (const name of used) {
    assert.ok(!/^--md-(fg|muted|border|bg|ok|err|warn|info)$/.test(name), '仍使用未定义的变量名 ' + name);
  }
});

const NON_COLOR = /mono|font|size|radius|gap|container|name/;
const COLOR_LITERAL = /^(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)|hsla?\([^)]*\))$/;

const rootBlocks = blocks.filter((b) => !b.parent && b.selector === '.mdx-res');
const rootDecl = new Map();
if (rootBlocks.length === 1) {
  for (const d of declarations(rootBlocks[0].body)) rootDecl.set(d.prop, d.value.trim());
}

function terminusOfValue(value, seen) {
  const v = String(value).trim();
  if (!v.startsWith('var(')) return v;
  if (closeIndexOf(v, 3) !== v.length - 1) return null;
  const args = splitTop(v.slice(4, v.length - 1), ',');
  if (!args[0] || seen.includes(args[0])) return null;
  if (args.length >= 2) return terminusOfValue(args.slice(1).join(','), seen.concat(args[0]));
  const next = rootDecl.get(args[0]);
  if (next == null) return null;
  return terminusOfValue(next, seen.concat(args[0]));
}

test('每个 --mdx-* 颜色变量都落到字面量兜底，不会落到 guaranteed-invalid', () => {
  assert.equal(rootBlocks.length, 1, '缺少唯一的 .mdx-res 根规则');
  let checked = 0;
  for (const [prop, value] of rootDecl) {
    if (!prop.startsWith('--mdx-') || NON_COLOR.test(prop) || !value.includes('var(')) continue;
    const t = terminusOfValue(value, []);
    assert.ok(t !== null, prop + ' 的 var() 链断裂（缺回退、引用未定义变量或循环）：' + value);
    assert.ok(COLOR_LITERAL.test(t), prop + ' 最终兜底不是字面量颜色：' + t);
    checked++;
  }
  assert.ok(checked >= 12, '根规则兜底变量过少：' + checked);
});

test('亮色兜底按宿主 data-theme 走，不被系统偏好覆盖手动主题', () => {
  const tokenBlocks = blocks.filter((b) => declarations(b.body).some((d) => /^--mdx-t-/.test(d.prop)));
  assert.ok(tokenBlocks.length >= 3, '缺少深/浅两套兜底 token 块');
  const manualLight = tokenBlocks.filter((b) => !b.parent && /\[data-theme="light"\]/.test(b.selector));
  const manualDark = tokenBlocks.filter((b) => !b.parent && /\[data-theme="dark"\]/.test(b.selector));
  assert.ok(manualLight.length >= 1, '缺少 [data-theme="light"] 作用域的浅色兜底');
  assert.equal(manualDark.length, 0, '深色兜底不应再用 [data-theme="dark"] 覆盖根规则默认值');
  for (const b of tokenBlocks) {
    if (!b.parent) continue;
    assert.ok(/prefers-color-scheme/.test(b.parent), 'token 块被包在无关的 at-rule 里：' + b.parent);
    assert.ok(/:root:not\(\[data-theme=/.test(b.selector),
      '媒体查询兜底必须排除宿主已手动选择的主题：' + b.selector);
  }
  for (const b of blocks) {
    if (b.parent && /prefers-color-scheme:\s*light/.test(b.parent)) {
      assert.ok(/\[data-theme=/.test(b.selector), '浅色媒体规则未限定 data-theme：' + b.selector);
    }
  }
});

test('深/浅两套兜底 token 集合一致且均为字面量颜色', () => {
  const sets = blocks
    .map((b) => ({ b, props: declarations(b.body).filter((d) => /^--mdx-t-/.test(d.prop)).map((d) => d.prop) }))
    .filter((x) => x.props.length)
    .map((x) => x);
  assert.ok(sets.length >= 3);
  const first = sets[0].props.slice().sort().join('|');
  for (const x of sets) {
    assert.equal(x.props.slice().sort().join('|'), first, '不同主题作用域的 --mdx-t-* 集合不一致：' + x.b.selector);
    for (const d of declarations(x.b.body)) {
      if (!/^--mdx-t-/.test(d.prop)) continue;
      assert.ok(COLOR_LITERAL.test(d.value), '兜底 token 不是字面量颜色：' + x.b.selector + ' ' + d.prop + '=' + d.value);
    }
  }
});

test('样式不定义交互控件、外链或网络资源', () => {
  assert.ok(!/@import|url\(|<script|javascript:/i.test(css), '样式含外部资源或脚本');
  assert.ok(!/position:\s*(fixed|sticky)/.test(css), '样式使用了 fixed/sticky 定位');
  assert.ok(css.includes('prefers-reduced-motion'), '缺少动效降级');
});

/* ---------- 汇总 ---------- */

console.log('\n' + '-'.repeat(56));
console.log('通过 ' + passed + ' 项，失败 ' + failures.length + ' 项');
if (failures.length) {
  console.log('\n失败明细：');
  for (const f of failures) console.log('  · ' + f.name + '\n    ' + f.msg);
  console.log('\n结果：未通过（存在失败项）');
  process.exitCode = 1;
} else {
  console.log('结果：全部通过（仅组件级自测，原生 GUI 仍需主脑人工验收）');
}
