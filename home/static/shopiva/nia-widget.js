(function(){'use strict';
function init(el){
 if(el.dataset.niaReady)return; el.dataset.niaReady='1';
 const role=el.dataset.role||'customer';
 el.innerHTML='<div class="nia-widget-main"><div class="nia-ring"><svg viewBox="0 0 118 118"><circle cx="59" cy="59" r="50"></circle><circle class="nia-ring-live" cx="59" cy="59" r="50"></circle></svg><div class="nia-core">◉</div></div><div class="nia-copy"><div class="nia-kicker">Nia · '+role+' operations</div><h3>Nia Live Copilot</h3><p class="nia-status" data-nia-status>Idle — ready for Shopiva data.</p><div class="nia-actions"><button class="nia-action primary" data-nia-monitor type="button">🎙️ Start voice monitor</button><button class="nia-action" data-nia-refresh type="button">↻ Refresh</button></div></div></div><div class="nia-stats"><div class="nia-stat"><b data-s1>—</b><span data-l1>Orders</span></div><div class="nia-stat"><b data-s2>—</b><span data-l2>Stock</span></div><div class="nia-stat"><b data-s3>—</b><span data-l3>Today</span></div></div><div class="nia-widget-note">Nia can listen and answer by voice. The microphone is used only while the voice session is active.</div><div class="nia-digest" data-nia-digest style="display:none"></div>';
 const status=el.querySelector('[data-nia-status]'),btn=el.querySelector('[data-nia-monitor]'),refresh=el.querySelector('[data-nia-refresh]');
 const csrf=el.dataset.csrfToken||'';
 let stream=null,ctx=null,analyser=null,raf=0,pc=null,dc=null,audio=null,voiceActive=false;
 function setStatus(text){status.textContent=text}
 function setRefreshState(active){
  refresh.disabled=active;
  refresh.textContent=active?'↻ Refreshing…':'↻ Refresh';
 }
 async function refreshStats(){
  if(refresh.disabled)return;
  setRefreshState(true);
  const wasVoiceActive=voiceActive;
  if(!wasVoiceActive)setStatus('Refreshing Shopiva data…');
  try{
   const r=await fetch('/ai/nia/dashboard-context/',{
    method:'GET',
    headers:{'X-Requested-With':'XMLHttpRequest','Accept':'application/json'},
    credentials:'same-origin',
    cache:'no-store'
   });
   let d=null;
   try{d=await r.json()}catch(_){}
   if(!r.ok)throw new Error((d&&d.error)||('Refresh failed (HTTP '+r.status+').'));
   if(!d||!d.ok)throw new Error((d&&d.error)||'Shopiva data could not be loaded.');
   const s=d.stats||{};
   el.querySelector('[data-s1]').textContent=s.primary_value??'0';el.querySelector('[data-l1]').textContent=s.primary_label||'Orders';
   el.querySelector('[data-s2]').textContent=s.secondary_value??'0';el.querySelector('[data-l2]').textContent=s.secondary_label||'Stock';
   el.querySelector('[data-s3]').textContent=s.tertiary_value??'0';el.querySelector('[data-l3]').textContent=s.tertiary_label||'Today';
   if(role==='admin'&&d.digest){const box=el.querySelector('[data-nia-digest]');box.textContent='Daily operations digest: '+d.digest;box.style.display='block'}
   if(!voiceActive)setStatus('Live Shopiva data updated just now.');
  }catch(e){
   if(!voiceActive)setStatus('Live stats unavailable — '+(e.message||'refresh failed'));
  }finally{
   setRefreshState(false);
  }
 }
 function draw(){
  if(!analyser)return;
  const data=new Uint8Array(analyser.fftSize);analyser.getByteTimeDomainData(data);
  let sum=0;for(let i=0;i<data.length;i++){const x=(data[i]-128)/128;sum+=x*x}
  const level=Math.min(1,Math.sqrt(sum/data.length)*3.2);
  const ring=el.querySelector('.nia-ring-live');ring.style.strokeWidth=String(5+level*9);ring.style.opacity=String(.45+level*.5);ring.style.strokeDasharray=(5+level*16)+' '+(11-level*5);
  raf=requestAnimationFrame(draw)
 }
 function stop(message){
  if(raf)cancelAnimationFrame(raf);
  try{stream&&stream.getTracks().forEach(t=>t.stop());ctx&&ctx.close();dc&&dc.close();pc&&pc.close();audio&&audio.remove()}catch(_){}
  stream=null;ctx=null;analyser=null;dc=null;pc=null;audio=null;voiceActive=false;el.dataset.active='false';
  btn.disabled=false;btn.textContent='🎙️ Start voice monitor';
  if(message)setStatus(message);
  else setStatus('Idle — ready for Shopiva data.');
 }
 async function startVoice(){
  if(voiceActive){stop();return}
  if(!window.RTCPeerConnection||!navigator.mediaDevices?.getUserMedia){setStatus('Live voice is not supported in this browser.');return}
  if(!csrf){setStatus('Voice security token is unavailable. Refresh the page and try again.');return}
  try{
   setStatus('Requesting microphone permission…');btn.disabled=true;
   const mediaPromise=navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
   stream=await Promise.race([
    mediaPromise,
    new Promise((_,reject)=>setTimeout(()=>reject(new Error('Microphone permission timed out. Check the browser microphone permission for Shopiva and try again.')),15000))
   ]);
   pc=new RTCPeerConnection();
   audio=document.createElement('audio');audio.autoplay=true;audio.setAttribute('aria-hidden','true');audio.style.display='none';document.body.appendChild(audio);
   pc.ontrack=e=>{if(e.streams[0])audio.srcObject=e.streams[0]};
   stream.getTracks().forEach(t=>pc.addTrack(t,stream));
   ctx=new(window.AudioContext||window.webkitAudioContext)();await ctx.resume();analyser=ctx.createAnalyser();analyser.fftSize=512;ctx.createMediaStreamSource(stream).connect(analyser);
   dc=pc.createDataChannel('oai-events');
   dc.onopen=()=>{voiceActive=true;el.dataset.active='true';btn.disabled=false;btn.textContent='⏹ Stop Nia voice';setStatus('🟢 Nia is listening — ask your question.');draw()};
   dc.onclose=()=>{if(voiceActive)stop()};
   dc.onerror=()=>{setStatus('Nia voice connection encountered an error.');stop()};
   dc.onmessage=async e=>{
    try{
     const ev=JSON.parse(e.data);
     if(ev.type==='error'){setStatus(ev.error?.message||'Nia voice error.');return}
     if(ev.type==='input_audio_buffer.speech_started')setStatus('🎙️ Listening…');
     if(ev.type==='input_audio_buffer.speech_stopped')setStatus('Thinking…');
     if(ev.type==='response.created')setStatus('🔊 Nia is answering…');
     if(ev.type==='response.audio_transcript.done'&&ev.transcript)setStatus('🔊 Nia: '+ev.transcript);
     if(ev.type==='response.done'&&voiceActive)setStatus('🟢 Nia is listening — ask another question.');
     if(ev.type==='response.function_call_arguments.done'){
      const args=JSON.parse(ev.arguments||'{}');
      const response=await fetch('/ai/realtime/action/',{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':csrf,'X-Requested-With':'XMLHttpRequest'},credentials:'same-origin',body:JSON.stringify({action:ev.name,...args})});
      const out=await response.json();
      if(dc&&dc.readyState==='open'){
       dc.send(JSON.stringify({type:'conversation.item.create',item:{type:'function_call_output',call_id:ev.call_id,output:JSON.stringify(out)}}));
       dc.send(JSON.stringify({type:'response.create'}));
      }
     }
    }catch(_){}
   };
   const offer=await pc.createOffer();await pc.setLocalDescription(offer);
   const response=await fetch('/ai/realtime/call/',{method:'POST',headers:{'Content-Type':'application/sdp','X-CSRFToken':csrf,'X-Requested-With':'XMLHttpRequest'},credentials:'same-origin',body:offer.sdp});
   if(!response.ok){let message='Could not connect Nia voice.';try{const d=await response.json();message=d.error||message}catch(_){}throw new Error(message)}
   await pc.setRemoteDescription({type:'answer',sdp:await response.text()});
   btn.disabled=false;
  }catch(e){
   const name=e&&e.name;
   let message=e&&e.message;
   if(name==='NotAllowedError')message='Microphone access was blocked. Allow microphone access for shopivakenya.top, then try again.';
   else if(name==='NotFoundError')message='No microphone was found. Connect a microphone and try again.';
   else if(name==='NotReadableError')message='The microphone is busy or unavailable. Close other apps using it, then try again.';
   else if(name==='SecurityError')message='Browser security blocked microphone access on this page.';
   stop(message||'Unable to start Nia voice.');
  }
 }
 btn.addEventListener('click',startVoice);
 refresh.addEventListener('click',refreshStats);
 refreshStats();
 setInterval(refreshStats,30000);
}
function boot(){document.querySelectorAll('[data-nia-widget]').forEach(init)}
window.ShopivaNiaWidget={boot};document.readyState==='loading'?document.addEventListener('DOMContentLoaded',boot):boot();
})();