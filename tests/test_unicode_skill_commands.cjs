const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(
  process.env.COMMANDS_JS_PATH || path.join(__dirname, '../static/commands.js'), 'utf8');

async function commands() {
  const context = vm.createContext({
    console,
    window: {},
    localStorage: {getItem() { return null; }, setItem() {}, removeItem() {}},
    t: key => key,
    api: async endpoint => {
      if (endpoint === '/api/commands') return {commands: [
        {name: '插件命令', aliases: ['别名技能'], category: 'Plugin'}
      ]};
      if (endpoint === '/api/commands/bundles') return {bundles: [
        {name: '研究组合', skill_count: 2}
      ]};
      if (endpoint === '/api/skills') return {skills: [
        {name: '活人感写作', description: '按素材写长文'},
        {name: 'AIHOT资讯', description: '实时资讯'},
        {name: '横纵深度报告', description: '历史与同期比较'},
        {name: 'help'}, {name: '别名技能'}, {name: '研究组合'},
        {name: 'plain_skill'}
      ]};
      throw new Error(`Unexpected API request: ${endpoint}`);
    }
  });
  vm.runInContext(source, context);
  await vm.runInContext('loadAgentCommandMetadata(true)', context);
  await vm.runInContext('loadBundleCommands(true)', context);
  await vm.runInContext('loadSkillCommands(true)', context);
  return context;
}

test('Chinese and mixed-language skills survive the autocomplete catalog', async () => {
  const context = await commands();
  const matches = await vm.runInContext("getSlashAutocompleteMatches('/')", context);
  for (const name of ['活人感写作', 'aihot资讯', '横纵深度报告']) {
    assert.ok(matches.some(item => item.name === name && item.source === 'skill'), name);
  }
  assert.ok(matches.some(item => item.name === 'plain-skill'));
});

test('Chinese queries preserve the original skill name for dispatch', async () => {
  const context = await commands();
  const matches = await vm.runInContext("getSlashAutocompleteMatches('/活人')", context);
  assert.equal(matches.length, 1);
  assert.equal(matches[0].name, '活人感写作');
  assert.equal(matches[0].skillName, '活人感写作');
  const parsed = vm.runInContext("parseCommand('/活人感写作 根据素材写一段')", context);
  assert.equal(parsed.name, '活人感写作');
  assert.equal(parsed.args, '根据素材写一段');
});

test('builtins, plugin aliases, and bundles keep priority over matching skills', async () => {
  const context = await commands();
  const matches = await vm.runInContext("getSlashAutocompleteMatches('/')", context);
  assert.equal(matches.filter(item => item.name === 'help').length, 1);
  assert.equal(matches.find(item => item.name === 'help').source, 'builtin');
  assert.equal(matches.some(item => item.name === '别名技能' && item.source === 'skill'), false);
  assert.equal(matches.find(item => item.name === '研究组合').source, 'bundle');
});
