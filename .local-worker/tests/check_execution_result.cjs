// Isolated component/browser QA. No workbench, account, model or native app.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {launch} = require('./browser_helpers.cjs');
const panel = path.join(__dirname, '../panel');
const script = fs.readFileSync(path.join(panel, 'execution-result.js'), 'utf8');
const css = fs.readFileSync(path.join(panel, 'style.css'), 'utf8') + '\n' +
  fs.readFileSync(path.join(panel, 'execution-result.css'), 'utf8');
const result = {
  schema_version: 1, legacy: false, execution: {engine: 'workbuddy', status: 'failed'},
  stages: [{id: 'prepare', label: '准备', state: 'completed'},
    {id: 'execute', label: '执行', state: 'completed'},
    {id: 'verify', label: '验证与自测', state: 'completed'},
    {id: 'session_close', label: '会话关闭', state: 'failed'}],
  failure: {stage: 'session_close', code: 'SESSION_CLOSE_UNCONFIRMED',
    message: '文件已生成；本任务原生会话关闭未确认', source: 'adapter'},
  issues: [{stage: 'session_close', code: 'SESSION_CLOSE_UNCONFIRMED', message: '保留原始失败'}],
  artifacts: {state: 'changed', files: ['src/示例.py'], scope_label: '仅核对任务声明文件'},
  verification: {state: 'passed', source: 'adapter_tool', exit_code: 0},
  review: {state: 'executor_failed', accepted: false}
};
(async () => {
  const browser = await launch();
  let requests = 0, checks = 0;
  try {
    for (const width of [360, 1000]) {
      for (const theme of ['light', 'dark']) {
        const page = await browser.newPage({viewport: {width, height: 900},
          colorScheme: theme === 'light' ? 'dark' : 'light'});
        await page.route('**/*', route => {requests++; return route.abort();});
        const errors = [];
        page.on('pageerror', e => errors.push(e.message));
        await page.setContent('<!doctype html><html data-theme="' + theme + '"><head>' +
          '<meta charset="utf-8"><style>' + css + '</style></head>' +
          '<body><main id="fixture" style="margin:16px;max-width:760px"></main></body></html>');
        await page.addScriptTag({content: script});
        await page.evaluate(data => {
          document.querySelector('#fixture').innerHTML = ModelDeskExecutionResult.render(data);
        }, result);
        const measured = await page.evaluate(() => {
          const root = document.querySelector('.mdx-res'), box = document.querySelector('.mdx-res-box');
          const rect = root.getBoundingClientRect();
          return {color: getComputedStyle(root).color, ink: getComputedStyle(document.documentElement).getPropertyValue('--ink').trim(),
            bg: getComputedStyle(box).backgroundColor, right: rect.right,
            overflow: root.scrollWidth > root.clientWidth + 1,
            text: root.textContent, failure: root.querySelector('[data-failure-stage]').dataset.failureStage,
            reviewGreen: root.querySelector('.mdx-res-row--review .is-ok') !== null};
        });
        assert.equal(measured.color, theme === 'dark' ? 'rgb(237, 237, 235)' : 'rgb(32, 32, 32)');
        assert.notEqual(measured.bg, 'rgba(0, 0, 0, 0)');
        assert.ok(!measured.overflow && measured.right <= width);
        assert.equal(measured.failure, 'session_close');
        assert.ok(measured.text.includes('有文件改动') && measured.text.includes('自测通过'));
        assert.ok(measured.text.includes('执行器自测，不等于主脑独立验收'));
        assert.equal(measured.reviewGreen, false);
        checks += 7;
        for (const state of ['accepted', 'final_accepted']) {
          await page.evaluate(data => {
            document.querySelector('#fixture').innerHTML = ModelDeskExecutionResult.render(data);
          }, {...result, review: {state, accepted: false}});
          assert.equal(await page.locator('.mdx-res-row--review .is-ok').count(), 0);
          assert.ok((await page.locator('.mdx-res-row--review').textContent()).includes('验收结论待核实'));
          checks += 2;
        }
        const attack = '<img src="https://invalid.example/x" onerror="globalThis.__attack=1">';
        await page.evaluate(data => {
          document.querySelector('#fixture').innerHTML = ModelDeskExecutionResult.render(data);
        }, {...result, failure: {...result.failure, message: attack},
          stages: [{id: 'x', label: attack, state: 'failed'}], artifacts: {state: 'changed', files: [attack]}});
        assert.equal(await page.locator('#fixture img, #fixture script, #fixture [onerror]').count(), 0);
        assert.equal(await page.evaluate(() => globalThis.__attack), undefined);
        assert.deepEqual(errors, []);
        checks += 3;
        if (process.env.MODEL_DESK_RESULT_SCREENSHOT && width === 360 && theme === 'dark') {
          await page.evaluate(data => {
            document.querySelector('#fixture').innerHTML = ModelDeskExecutionResult.render(data);
          }, result);
          await page.screenshot({path: process.env.MODEL_DESK_RESULT_SCREENSHOT, fullPage: true});
        }
        await page.close();
      }
    }
    assert.equal(requests, 0);
    console.log(JSON.stringify({checks: checks + 1, external_requests: requests, native_gui_verified: false}));
  } finally {
    await browser.close();
  }
})().catch(e => {console.error(e); process.exitCode = 1;});
