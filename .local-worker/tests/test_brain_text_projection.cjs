'use strict';
// Invoked by the Python stdio/HTTP mock-loop test with an explicitly selected
// installed DSH runtime. It imports only product client code and projects the
// REAL MCP stdio responses captured by that test. No user profile, model or
// live panel is loaded or contacted here.
const assert = require('node:assert/strict');
const fs = require('node:fs');

(async () => {
  const { createMcpToolDefinition } = await import(process.argv[2]);
  const responses = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
  let verified = 0;
  for (const { name, result } of responses) {
    assert.equal(result.content.length, 2);
    const jsonText = result.content[1].text;
    assert.deepEqual(JSON.parse(jsonText), result.structuredContent);
    const tool = createMcpToolDefinition({}, {
      name: 'mcp__fixture__' + name, rawName: name, description: 'offline fixture',
      inputSchema: { type: 'object', properties: {} }, call: async () => result,
    });
    if (result.isError) {
      await assert.rejects(() => tool.execute({}, { signal: new AbortController().signal }),
        error => error.message.includes(jsonText));
    } else {
      const value = await tool.execute({}, { signal: new AbortController().signal });
      assert.deepEqual(value.structuredContent, result.structuredContent);
      const visible = tool.output.render({}, value).map(part => part.text || '').join('\n');
      assert.ok(visible.includes(jsonText), name + ': JSON details absent from model projection');
    }
    verified += 1;
  }
  console.log(JSON.stringify({ verified, model_called: false, live_panel_written: false }));
})().catch(error => {
  // Only synthetic fixture diagnostics can appear here.
  console.error(error);
  process.exitCode = 1;
});
