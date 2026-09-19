import { JSDOM } from 'jsdom';
import fs from 'fs';
const SIGNED = 'https://bedrock-agentcore.us-west-2.amazonaws.com/browser-streams/B/sessions/S/live-view'
  + '?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=SIG&X-Amz-Security-Token=TOK';
const dom = new JSDOM('<!doctype html><div id="display"></div><p id="status"></p>',
  { url: 'https://mcp-sandbox.example/live-view.html', runScripts: 'outside-only', pretendToBeVisual: true });
const w = dom.window;
const seen = [];
class FakeWS { constructor(u,p){ seen.push(String(u)); this.readyState=0; } send(){} close(){} addEventListener(){} removeEventListener(){} }
FakeWS.CONNECTING=0;FakeWS.OPEN=1;FakeWS.CLOSING=2;FakeWS.CLOSED=3;
w.WebSocket = FakeWS;
w.eval(fs.readFileSync(process.env.DCV,'utf8'));
const extras = () => new w.URLSearchParams(new w.URL(SIGNED).search);
const bare = SIGNED.split('?')[0];

async function run(label, cfg) {
  seen.length = 0;
  try { await w.dcv.connect(cfg); } catch (e) {}
  await new Promise(r => setTimeout(r, 600));
  const ws = seen.find(u => u.includes('/live-view')) || seen[0];
  const signed = ws ? ws.includes('X-Amz-Signature') : null;
  console.log(`  ${label}`);
  console.log(`    ws url    : ${ws ? ws.replace('wss://bedrock-agentcore.us-west-2.amazonaws.com','').slice(0,95) : '(none)'}`);
  console.log(`    SIGNED?   : ${signed === null ? 'n/a' : (signed ? 'YES' : 'NO  <-- unsigned')}`);
}

w.dcv.setLogLevel(w.dcv.LogLevel.ERROR);
await run('httpExtraSearchParams TOP-LEVEL (ours)', {
  url: bare, sessionId: 's', authToken: 't', divId: 'display',
  httpExtraSearchParams: extras,
  callbacks: { firstFrame(){}, disconnect(){} },
});
await run("httpExtraSearchParams inside OBSERVERS (AWS's)", {
  url: bare, sessionId: 's', authToken: 't', divId: 'display',
  observers: { httpExtraSearchParams: extras },
});
