const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function browser(file) {
  let now = 10000, waits = 0;
  const elements = new Map();
  const noop = () => {};
  const ctx = new Proxy({}, {get: () => noop, set: () => true});
  const element = () => ({value:'', textContent:'', innerHTML:'', style:{}, classList:{toggle:noop},
    prepend:noop, add:noop, click:noop, insertAdjacentHTML:noop, querySelector:element,
    getContext:()=>ctx, width:1000, height:300});
  class Socket {static OPEN=1; readyState=1; send(){} close(){this.readyState=3}}
  class Clock extends Date {static now(){return now}}
  const sandbox = {console, Date:Clock, WebSocket:Socket, location:{host:'localhost'},
    performance:{now:()=>now}, setInterval:noop, clearInterval:noop, clearTimeout:noop,
    setTimeout:(fn, ms)=>{now+=ms;waits++;sandbox.onWait?.(ms);fn();return waits},
    document:{getElementById:id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id)},createElement:element},
    localStorage:{getItem:()=>null,setItem:noop}, Option:class{}, confirm:()=>true, alert:noop,
    window:{}, navigator:{}, TextEncoder, Blob, URL};
  sandbox.window=sandbox;
  const context=vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'..',file),'utf8'),context);
  return {context, run:code=>vm.runInContext(code,context), get waits(){return waits}};
}

async function wifiTests(){
  for(const start of [0,10]){
    const b=browser('main/web_wifi/app.js');
    b.run(`latest={position_mm:${start},moving:false,enabled:true,stopped:false};dataRevision=10;lastDataAt=Date.now();
      globalThis.sent=[];command=async text=>sent.push(text);
      globalThis.onWait=()=>{latest.position_mm=10;dataRevision++;lastDataAt=Date.now()};`);
    await b.run('moveTo(10,1000,150,150)');
    assert.equal(b.waits,1,'require a fresh post-ACK frame, no observed moving necessary');
    assert.equal(b.run('sent.join()'),'MOVE_MM 10 1000 150 150');
  }
  for(const problem of ['stopped','disabled','stale','disconnected']){
    const b=browser('main/web_wifi/app.js');
    b.run(`latest={position_mm:10,moving:false,enabled:true,stopped:false};lastDataAt=Date.now();
      command=async()=>{};
      globalThis.onWait=()=>{${problem==='stale'?'':`dataRevision++;lastDataAt=Date.now();`}
        ${problem==='stopped'?'latest.stopped=true;':''}
        ${problem==='disabled'?'latest.enabled=false;':''}
        ${problem==='disconnected'?'socket.readyState=3;':''}
      };`);
    await assert.rejects(b.run('moveTo(10,1000,150,150)'),/停止锁定|数据中断|连接中断/);
  }
  const p=browser('main/web_wifi/app.js');
  assert.equal(p.run(`distancePreset({ppm:80,speed:1500,return_speed:1000,accel:300,decel:100,return_accel:300,return_decel:300}).accel`),225.375);
  assert.equal(p.run(`distancePreset({ramp_unit:'mm',accel:225}).accel`),225);
  // Exercise two complete batches consecutively. Every move finishes between refreshes.
  const b=browser('main/web_wifi/app.js');
  b.run(`Object.entries({condition:'test',batchId:'test',label:1,count:2,start:10,end:1900,
    speed:1500,returnSpeed:1000,accel:225,decel:75,returnAccel:150,returnDecel:150,
    baselineWait:20,endDwell:10,betweenWait:10,distance:1000,note:'',softMin:0,softMax:2000,
    ppm:80,maxSpeed:2000}).forEach(([id,v])=>$(id).value=String(v));
    latest={position_mm:0,moving:false,enabled:false,stopped:false,sensor_test_mode:false};
    globalThis.sent=[];globalThis.downloads=[];globalThis.targetPosition=0;
    command=async text=>{sent.push(text);const parts=text.split(' ');
      if(parts[0]==='MOVE_MM')targetPosition=Number(parts[1]);
      if(parts[0]==='STOP'){latest.stopped=true;latest.enabled=false}
      if(parts[0]==='CLEAR')latest.stopped=false;
      if(parts[0]==='ENABLE')latest.enabled=parts[1]==='1';
    };
    globalThis.onWait=()=>{latest.position_mm=targetPosition;latest.moving=false;dataRevision++;lastDataAt=Date.now()};
    downloadCsv=(lines,name)=>downloads.push({lines,name});`);
  await b.run('startBatch()');
  assert.equal(b.run("$('batchState').textContent"),'批次完成');
  await b.run('startBatch()');
  assert.equal(b.run("$('batchState').textContent"),'批次完成');
  assert.equal(b.run("sent.filter(s=>s.startsWith('MOVE_MM')).length"),12);
  assert.equal(b.run('downloads.length'),2);
  assert.ok(b.run("downloads[0].lines[0].includes('accel_mm')"));
}

async function usbTests(){
  const b=browser('web/app.js');
  b.run(`writer={};latest={position_mm:0,moving:false,enabled:true,stopped:false};
    batch={active:true,stopRequested:false};globalThis.sent=[];sendCommand=async text=>sent.push(text);
    globalThis.onWait=()=>{latest.position_mm=10;totalFrames++;lastDataAt=Date.now()};`);
  await b.run('moveTo(10,1000,150,150)');
  await b.run('waitPosition(10,1000)');
  assert.equal(b.waits,1);
  assert.equal(b.run('sent.join()'),'MOVE_MM 10 1000 150 150');
}

(async()=>{await wifiTests();await usbTests();console.log('web motion: first arrival, stale/fault guards, mm presets and consecutive batches passed')})()
  .catch(error=>{console.error(error);process.exitCode=1});
