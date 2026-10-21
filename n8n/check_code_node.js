// Runs the workflow's "Validate and score" Code node outside n8n.
//   node n8n/check_code_node.js
const assert = require('assert');
const wf = require('./inbound-lead-workflow.json');

const code = wf.nodes.find((n) => n.name === 'Validate and score').parameters.jsCode;
const run = (body) => new Function('$json', code)({ body }).json;

const hot = run({ email: 'Jamie@Contoso.example', form_id: 'demo_request', title: 'VP Sales', company_size: '1000+' });
assert.deepStrictEqual([hot.email, hot.score, hot.owner], ['jamie@contoso.example', 90, 'ae_queue']);
assert.strictEqual(run({ email: 'a@b.example', form_id: 'pricing', title: 'Director', company_size: '1-49' }).owner, 'sdr_queue');
assert.strictEqual(run({ email: 'x@gmail.com', form_id: 'pricing' }).score, 0);
assert.strictEqual(run({ email: 'not-an-email', form_id: 'pricing' }).valid, false);
assert.strictEqual(run({ email: 'a@b.example', form_id: 'contact', company_size: '50-199' }).owner, 'nurture');

const names = new Set(wf.nodes.map((n) => n.name));
for (const [from, { main }] of Object.entries(wf.connections)) {
  assert(names.has(from), `unknown source node ${from}`);
  for (const branch of main) for (const link of branch) assert(names.has(link.node), `unknown target ${link.node}`);
}
console.log('workflow checks passed');
