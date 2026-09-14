'use strict';
(() => {
  const map = document.getElementById('lidarMap'), scan = document.getElementById('lidarScan');
  const status = document.getElementById('mapStatus');
  let data = {points: [], scan: []}, paused = false, yaw = -.7, pitch = .6, zoom = 1, drag = null;
  const palette=Array.from({length:256},(_,i)=>`hsl(${220-180*i/255} 80% 65%)`);
  let drawPending=false;
  function draw(){
    if(drawPending)return;
    drawPending=true;
    requestAnimationFrame(()=>{drawPending=false;render();});
  }
  function setup(canvas) {
    const rect = canvas.getBoundingClientRect(), ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(rect.width * ratio); canvas.height = Math.round(rect.height * ratio);
    const ctx = canvas.getContext('2d'); ctx.scale(ratio, ratio);
    ctx.fillStyle = '#0a1220'; ctx.fillRect(0, 0, rect.width, rect.height);
    ctx.font = '12px sans-serif'; return [ctx, rect.width, rect.height];
  }
  function render() {
    const [ctx, w, h] = setup(map);
    const pts = data.points;
    let low = [0, 0, 0], high = [2, 0, 0];
    for (const p of pts) for (let i=0;i<3;i++) {low[i]=Math.min(low[i],p[i]); high[i]=Math.max(high[i],p[i]);}
    const center = low.map((v,i)=>(v+high[i])/2);
    const span = Math.max(2, ...high.map((v,i)=>v-low[i]));
    const scale = Math.min(w,h)*.65/span*zoom;
    const cy=Math.cos(yaw),sy=Math.sin(yaw),cp=Math.cos(pitch),sp=Math.sin(pitch);
    function project(p) {
      const x=p[0]-center[0],y=p[1]-center[1],z=p[2]-center[2];
      const a=x*cy-y*sy, b=x*sy+y*cy;
      return [w/2+a*scale,h/2-(z*cp-b*sp)*scale];
    }
    function line(a,b,color,label) {
      const u=project(a),v=project(b);ctx.strokeStyle=color;ctx.lineWidth=1;
      ctx.beginPath();ctx.moveTo(...u);ctx.lineTo(...v);ctx.stroke();
      if(label){ctx.fillStyle=color;ctx.fillText(label,v[0]+5,v[1]-5);}
    }
    const gridStep = Math.max(.5, Math.ceil(span/8*2)/2);
    for(let t=-span;t<=span;t+=gridStep){line([low[0],t,0],[high[0],t,0],'#1e2d40');}
    line([0,0,0],[2,0,0],'#f08c9e','X 2m');
    line([0,0,0],[0,1,0],'#5cd6b0','Y 1m');
    line([0,0,0],[0,0,1],'#7aaafa','Z 1m');
    const colorScale=255/Math.max(.1,high[2]-low[2]);
    for(const p of pts){const q=project(p);ctx.fillStyle=palette[Math.max(0,Math.min(255,Math.round((p[2]-low[2])*colorScale)))];ctx.fillRect(q[0],q[1],1.6,1.6);}
    if(data.position_mm!=null){const q=project([data.position_mm/1000,0,0]);ctx.fillStyle='#fff';ctx.beginPath();ctx.arc(...q,4,0,Math.PI*2);ctx.fill();ctx.fillText('滑台',q[0]+8,q[1]);}
    if(!pts.length){ctx.fillStyle='#8295af';ctx.fillText('等待雷达完整扫描与 ESP32 位置数据',20,30);}
    const [sx,sw,sh]=setup(scan),range=Math.max(1,(data.max_range_mm||6000)/1000);
    const radius=Math.min(sw,sh)*.41;
    sx.strokeStyle='#283b51';sx.fillStyle='#8295af';sx.lineWidth=1;
    for(let i=1;i<=4;i++){sx.beginPath();sx.arc(sw/2,sh/2,radius*i/4,0,2*Math.PI);sx.stroke();sx.fillText(`${(range*i/4).toFixed(1)} m`,sw/2+4,sh/2-radius*i/4+13);}
    sx.beginPath();sx.moveTo(sw/2-radius,sh/2);sx.lineTo(sw/2+radius,sh/2);sx.moveTo(sw/2,sh/2-radius);sx.lineTo(sw/2,sh/2+radius);sx.stroke();
    sx.fillStyle='#49d3e8';
    for(const [angle,mm] of data.scan){const a=angle*Math.PI/180,r=mm/1000/range*radius;sx.fillRect(sw/2+Math.sin(a)*r,sh/2-Math.cos(a)*r,2,2);}
  }
  map.addEventListener('pointerdown',e=>{drag=[e.clientX,e.clientY];map.setPointerCapture(e.pointerId);});
  map.addEventListener('pointermove',e=>{if(!drag)return;yaw+=(e.clientX-drag[0])*.008;pitch=Math.max(-1.5,Math.min(1.5,pitch+(e.clientY-drag[1])*.008));drag=[e.clientX,e.clientY];draw();});
  map.addEventListener('pointerup',()=>drag=null);map.addEventListener('pointercancel',()=>drag=null);
  map.addEventListener('wheel',e=>{e.preventDefault();zoom=Math.max(.2,Math.min(10,zoom*Math.exp(-e.deltaY*.001)));draw();},{passive:false});
  document.getElementById('mapReset').onclick=()=>{yaw=-.7;pitch=.6;zoom=1;draw();};
  document.getElementById('mapPause').onclick=e=>{paused=!paused;e.target.textContent=paused?'继续显示':'暂停显示';};
  document.getElementById('mapClear').onclick=async()=>{try{const r=await fetch('/api/lidar/map/clear',{method:'POST'});if(!r.ok)throw Error('清空失败');data={...data,points:[],scan:[]};draw();}catch(e){status.textContent=e.message;}};
  new ResizeObserver(draw).observe(map);
  document.getElementById('mapFiles').onclick=async()=>{
    const box=document.getElementById('mapArchiveFiles');
    if(!box.hidden){box.hidden=true;return;}
    box.hidden=false;box.textContent='读取文件列表…';
    try{
      const r=await fetch('/api/lidar/files',{cache:'no-store'});if(!r.ok)throw Error('文件列表读取失败');
      const files=await r.json();box.replaceChildren();
      if(!files.length)box.textContent='尚无保存的点云';
      for(const file of files.reverse()){
        const row=document.createElement('div'),a=document.createElement('a');
        a.textContent=file.name;a.href=file.url;a.download='';row.append(a);box.append(row);
      }
    }catch(e){box.textContent=e.message;}
  };
  async function poll(){
    try{
      const r=await fetch('/api/lidar/map',{cache:'no-store'});if(!r.ok)throw Error('地图数据读取失败');
      const incoming=await r.json(), archive=incoming.archive;
      const archiveStatus=document.getElementById('mapArchiveStatus');
      archiveStatus.textContent=archive ? archive.error || `本次服务已保存 ${archive.saved_points.toLocaleString()} 点 · ${archive.current_file || '等待有效扫描'} · 历史文件保留` : '服务版本不支持持续保存，请更新后端';
      archiveStatus.style.color=archive?.error?'#ff8797':'';
      if(!paused){data=incoming;
        const state=!data.enabled?'雷达关闭':!data.connected?'雷达离线':data.age_s==null?'等待完整扫描':data.age_s>2?'数据已停止更新':'实时更新';
        status.textContent=`${state} · ${data.points.length.toLocaleString()} / ${(data.preview_limit||100000).toLocaleString()} 预览点${data.age_s==null?'':` · ${data.age_s.toFixed(1)}秒前`}`;draw();}
    }catch(e){status.textContent='地图连接中断 · '+e.message;document.getElementById('mapArchiveStatus').textContent='连接中断，无法确认保存状态';}
    finally{setTimeout(poll,500);}
  }
  poll();
})();
