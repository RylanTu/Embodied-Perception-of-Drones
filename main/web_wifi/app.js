'use strict';
const $=id=>document.getElementById(id),num=id=>Number($(id).value),wait=ms=>new Promise(r=>setTimeout(r,ms));
const CSV=['schema_version','batch_id','trial_id','trial_index','phase','condition','label','obstacle_distance_mm','commanded_speed_mm_s','accel_mm','decel_mm','note','host_time','esp_uptime_ms','sample_sequence','pressure_pa_1','pressure_pa_2','pressure_pa_3','pressure_pa_4','pressure_pa_5','temperature_c_1','temperature_c_2','temperature_c_3','temperature_c_4','temperature_c_5','valid_mask','position_mm','moving','enabled','stopped','sensor_test_mode'];
const colors=['#43b7d6','#ef7aa8','#e7b45d','#4ac997','#a58ae6'];
const sensorReasons=['正常','TCA9548A通道选择失败','SDP3x启动失败','SDP3x读取失败','压力CRC错误','温度CRC错误','比例因子CRC错误','比例因子为0','未发现TCA9548A（已扫描0x70～0x77）','I²C SDA被拉低','I²C SCL被拉低','I²C SDA和SCL均被拉低','直连模式未启用此通道'];
let lastDataAt=0,dataRevision=0;
let socket,latest,pending=new Map(),history=Array.from({length:5},()=>[]),batch=null,frameTimes=[];
function log(text){const line=document.createElement('div');line.textContent=`${new Date().toLocaleTimeString()}  ${text}`;$('log').prepend(line)}
function root(command){return command.trim().split(/\s+/)[0].toUpperCase()}
function send(command){if(!socket||socket.readyState!==WebSocket.OPEN)throw Error('WiFi数据链路未连接');socket.send(command)}
function command(text,timeout=3000){const key=root(text);return new Promise((resolve,reject)=>{if(pending.has(key))return reject(Error(`${key}命令尚未完成`));const timer=setTimeout(()=>{pending.delete(key);reject(Error(`${key}等待ESP32响应超时`))},timeout);pending.set(key,{resolve,reject,timer});try{send(text)}catch(error){clearTimeout(timer);pending.delete(key);reject(error)}})}
function connect(){socket=new WebSocket(`ws://${location.host}/ws`);socket.onopen=()=>{send('HELLO');$('link').textContent='WiFi在线';$('link').className='pill ok';log('WebSocket已连接')};socket.onclose=()=>{$('link').textContent='连接中断';$('link').className='pill bad';pending.forEach(p=>{clearTimeout(p.timer);p.reject(Error('WiFi连接中断'))});pending.clear();setTimeout(connect,1000)};socket.onerror=()=>socket.close();socket.onmessage=event=>{try{handle(JSON.parse(event.data))}catch(error){log(error.message)}}}
function handle(message){if(message.type==='hello'){log(`设备就绪，协议${message.protocol}`);return}if(message.type==='reply'){const p=pending.get(message.command);if(p){clearTimeout(p.timer);pending.delete(message.command);message.ok?p.resolve(message):p.reject(Error(`${message.command}失败：${message.message}(${message.code})`))}return}if(message.type==='data')handleData(message)}
function handleData(data){latest=data;lastDataAt=Date.now();dataRevision++;frameTimes.push(performance.now());while(frameTimes.length&&frameTimes[0]<performance.now()-1000)frameTimes.shift();$('rate').textContent=`${frameTimes.length} Hz`;$('motion').textContent=`位置 ${data.position_mm.toFixed(3)} mm · ${data.moving?'运动中':data.stopped?'停止锁定':'停止'} · ${data.sensor_test_mode?'模拟':'真实'}`;$('pressures').innerHTML=data.p.map((v,i)=>{const valid=data.valid_mask&(1<<i),reason=sensorReasons[data.diag?.[i]]||`未知故障${data.diag?.[i]}`,detail=valid?'正常':`${reason} · 累计${data.sensor_errors?.[i]??'?'}次 · ESP错误${data.i2c_errors?.[i]??'?'}`;return `<div class="number"><small>通道${i+1} · ${detail}</small>${v.toFixed(4)} Pa</div>`}).join('');const bus=data.i2c,busBox=$('sensorBus');if(bus){const address=bus.mux_address<0?'未发现':`0x${bus.mux_address.toString(16).padStart(2,'0').toUpperCase()}`,device=bus.direct_mode?'直连SDP3x':'TCA9548A';busBox.textContent=`${bus.direct_mode?'直连模式':'复用器模式'} · SDA GPIO${bus.sda_gpio}=${bus.sda_level?'高':'低'} · SCL GPIO${bus.scl_gpio}=${bus.scl_level?'高':'低'} · ${device} ${address} · 探测错误 ${bus.probe_error}`;busBox.className='sensor-bus '+(bus.mux_address>=0&&bus.sda_level&&bus.scl_level?'ok':'bad')}data.p.forEach((v,i)=>{history[i].push(v);if(history[i].length>320)history[i].shift()});draw();if(batch?.recording)record(data)}
function draw(){const c=$('chart'),x=c.getContext('2d'),w=c.width,h=c.height,all=history.flat(),lo=Math.min(0,...all),hi=Math.max(1,...all),span=hi-lo||1;x.clearRect(0,0,w,h);x.strokeStyle='#263448';x.beginPath();x.moveTo(0,h-(0-lo)/span*h);x.lineTo(w,h-(0-lo)/span*h);x.stroke();history.forEach((a,k)=>{x.strokeStyle=colors[k];x.lineWidth=1.6;x.beginPath();a.forEach((v,i)=>{const px=i*w/319,py=h-(v-lo)/span*h;i?x.lineTo(px,py):x.moveTo(px,py)});x.stroke()})}
setInterval(()=>{if(socket?.readyState===WebSocket.OPEN)socket.send('HEARTBEAT')},200);
function settings(){const s={ramp_unit:'mm',condition:$('condition').value.trim(),batch_id:$('batchId').value.trim(),label:num('label'),count:num('count'),start:num('start'),end:num('end'),speed:num('speed'),return_speed:num('returnSpeed'),accel:num('accel'),decel:num('decel'),return_accel:num('returnAccel'),return_decel:num('returnDecel'),baseline_wait:num('baselineWait'),end_dwell:num('endDwell'),between_wait:num('betweenWait'),distance:num('distance'),note:$('note').value,soft_min:num('softMin'),soft_max:num('softMax'),ppm:num('ppm'),max_speed:num('maxSpeed')};if(!s.condition||!s.batch_id)throw Error('实验条件和批次名称不能为空');if(s.count<1||s.count>500||s.start===s.end)throw Error('实验次数或起止位置无效');if(Math.min(s.start,s.end)<s.soft_min||Math.max(s.start,s.end)>s.soft_max)throw Error('起止位置超出软限位');if(Math.max(s.speed,s.return_speed)>s.max_speed||s.max_speed>2000)throw Error('速度超过最高速度');if(Object.entries(s).some(([k,v])=>typeof v==='number'&&!Number.isFinite(v)))throw Error('参数必须为有限数值');if(Math.min(s.speed,s.return_speed,s.ppm)<=0||Math.min(s.accel,s.decel,s.return_accel,s.return_decel,s.baseline_wait,s.end_dwell,s.between_wait)<0)throw Error('速度须大于0，缓启停距离和等待时间不能为负数');return s}
function csvCell(value){const text=String(value??'');return /[",\r\n]/.test(text)?`"${text.replaceAll('"','""')}"`:text}
function record(d){if(batch.lastSeq!==null){const gap=(d.seq-batch.lastSeq-1)>>>0;if(gap<100000)batch.dropped+=gap}batch.lastSeq=d.seq;const s=batch.s,values=[5,s.batch_id,`${s.batch_id}_${String(batch.index).padStart(4,'0')}`,batch.index,'measure',s.condition,s.label,s.distance,s.speed,s.accel,s.decel,s.note,new Date().toISOString(),d.ms,d.seq,...d.p,...d.t,d.valid_mask,d.position_mm,+d.moving,+d.enabled,+d.stopped,+d.sensor_test_mode];batch.lines.push(values.map(csvCell).join(','));batch.rows++;$('rows').textContent=batch.rows;$('dropped').textContent=batch.dropped}
async function moveTo(target,speed,accel,decel){
 if(batch?.active&&batch.stop)throw Error('批次已停止');
 const initial=latest?.position_mm??target;
 await command(`MOVE_MM ${target} ${speed} ${accel} ${decel}`);
 const after=dataRevision,started=Date.now();
 const deadline=started+(Math.abs(target-initial)+2*(accel+decel))/Math.max(.1,speed)*1000+15000;
 while(Date.now()<deadline){
  if(batch?.active&&batch.stop)throw Error('批次已停止');
  if(socket?.readyState!==WebSocket.OPEN)throw Error('WiFi连接中断');
  if(Date.now()-Math.max(started,lastDataAt)>2000)throw Error('等待到位时ESP32数据中断');
  if(latest&&dataRevision>after){
   if(latest.stopped||!latest.enabled)throw Error('等待到位时滑台已停止锁定或失能');
   if(!latest.moving&&Math.abs(latest.position_mm-target)<=.25)return;
  }
  await wait(40);
 }
 throw Error(`等待到达${target} mm超时`);
}
async function stopAndWait(){
 await command('STOP');
 const after=dataRevision,deadline=Date.now()+5000;
 while(Date.now()<deadline){
  if(dataRevision>after&&latest&&!latest.moving)return;
  if(socket?.readyState!==WebSocket.OPEN)throw Error('WiFi连接中断');
  await wait(40);
 }
 throw Error('停止后运动状态未释放');
}
function distancePreset(input){
 const data={...input};
 if(data.ramp_unit==='mm')return data;
 if(data.ramp_unit&&data.ramp_unit!=='ms')throw Error('未知缓启停单位');
 const minimum=200/Number(data.ppm);
 for(const [key,speed] of [['accel','speed'],['decel','speed'],['return_accel','return_speed'],['return_decel','return_speed']]){
  data[key]=Math.round((Math.max(Number(data[speed]),minimum)+minimum)*Number(data[key])/2)/1000;
  if(!Number.isFinite(data[key])||data[key]<0)throw Error('旧方案缓启停参数无效，请重新设置');
 }
 data.ramp_unit='mm';
 log('旧方案缓启停已从ms换算为mm，请核对后保存');
 return data;
}
async function delay(ms){const end=Date.now()+ms;while(Date.now()<end){if(batch?.stop)throw Error('批次已停止');await wait(Math.min(100,end-Date.now()))}}
function downloadCsv(lines,name){const blob=new Blob(['\ufeff'+lines.join('\r\n')+'\r\n'],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
async function startBatch(){if(batch?.active)throw Error('已有批次运行中');if(!latest)throw Error('尚未收到ESP32数据');const s=settings();if(latest.sensor_test_mode&&!confirm('当前为模拟数据，仍然继续？'))return;if(!confirm(`开始${s.count}次自动实验？`))return;batch={active:true,recording:false,stop:false,pause:false,s,index:0,rows:0,dropped:0,lastSeq:null,lines:[CSV.join(',')]};$('startBatch').disabled=true;try{await stopAndWait();await command('CLEAR');await command(`CONFIG ${s.soft_min} ${s.soft_max} ${s.ppm} ${s.max_speed}`);await command('ENABLE 1');for(let i=1;i<=s.count;i++){while(batch.pause&&!batch.stop){$('batchState').textContent='已暂停';await wait(100)}batch.index=i;$('batchState').textContent=`第${i}/${s.count}次：返回起点`;await moveTo(s.start,s.return_speed,s.return_accel,s.return_decel);$('batchState').textContent=`第${i}/${s.count}次：等待基线`;await delay(s.baseline_wait);batch.lastSeq=null;batch.recording=true;$('batchState').textContent=`第${i}/${s.count}次：正向采集`;await moveTo(s.end,s.speed,s.accel,s.decel);await delay(s.end_dwell);batch.recording=false;$('progress').textContent=`${i}/${s.count}`;$('bar').style.width=`${i/s.count*100}%`;$('batchState').textContent=`第${i}/${s.count}次：复位`;await moveTo(s.start,s.return_speed,s.return_accel,s.return_decel);if(i<s.count)await delay(s.between_wait)}await command('ENABLE 0');$('batchState').textContent='批次完成'}catch(error){$('batchState').textContent=`批次结束：${error.message}`;log(error.message);try{await command('STOP')}catch{}}finally{batch.recording=false;batch.active=false;$('startBatch').disabled=false;const stamp=new Date().toISOString().replaceAll('-','').replaceAll(':','').slice(0,15);downloadCsv(batch.lines,`${stamp}_${s.batch_id}.csv`)}}
function savePreset(){try{const name=$('presetName').value.trim();if(!name)throw Error('请输入方案名称');const data=JSON.parse(localStorage.getItem('drone-presets')||'{}');data[name]=settings();localStorage.setItem('drone-presets',JSON.stringify(data));loadPresetList(name);log(`已保存参数：${name}`)}catch(error){alert(error.message)}}
const ids={condition:'condition',batch_id:'batchId',label:'label',count:'count',start:'start',end:'end',speed:'speed',return_speed:'returnSpeed',accel:'accel',decel:'decel',return_accel:'returnAccel',return_decel:'returnDecel',baseline_wait:'baselineWait',end_dwell:'endDwell',between_wait:'betweenWait',distance:'distance',note:'note',soft_min:'softMin',soft_max:'softMax',ppm:'ppm',max_speed:'maxSpeed'};
function loadPresetList(selected=''){const data=JSON.parse(localStorage.getItem('drone-presets')||'{}'),select=$('presetSelect');select.innerHTML='<option value="">选择已保存方案</option>';Object.keys(data).sort().forEach(name=>select.add(new Option(name,name)));select.value=selected}
function loadPreset(){const name=$('presetSelect').value,stored=JSON.parse(localStorage.getItem('drone-presets')||'{}')[name];if(!stored)return;const data=distancePreset(stored);Object.entries(ids).forEach(([key,id])=>{if(data[key]!==undefined)$(id).value=data[key]});$('presetName').value=name;log(`已载入参数：${name}`)}
$('savePreset').onclick=savePreset;$('loadPreset').onclick=()=>{try{loadPreset()}catch(e){alert(e.message)}};$('startBatch').onclick=()=>startBatch().catch(e=>alert(e.message));$('pauseBatch').onclick=()=>{if(batch?.active){batch.pause=!batch.pause;$('pauseBatch').textContent=batch.pause?'继续':'完成本次后暂停'}};$('stopBatch').onclick=()=>{if(batch){batch.stop=true;batch.recording=false}command('STOP').catch(e=>log(e.message))};$('applyMotion').onclick=()=>{const s=settings();command(`CONFIG ${s.soft_min} ${s.soft_max} ${s.ppm} ${s.max_speed}`).catch(e=>alert(e.message))};$('enable').onclick=async()=>{try{await command('CLEAR');await command('ENABLE 1')}catch(e){alert(e.message)}};$('move').onclick=()=>moveTo(num('target'),num('manualSpeed'),num('manualAccel'),num('manualDecel')).catch(e=>alert(e.message));$('disable').onclick=()=>command('ENABLE 0').catch(e=>alert(e.message));$('stop').onclick=()=>command('STOP').catch(e=>alert(e.message));$('applyTest').onclick=()=>command(`TEST ${$('testEnabled').value} ${num('testBaseline')} ${num('testAmplitude')} ${num('testPeriod')} ${num('testWidth')}`).catch(e=>alert(e.message));
loadPresetList();connect();
