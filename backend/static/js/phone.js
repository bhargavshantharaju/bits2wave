"use strict";

const $ = id => document.getElementById(id);
const state = {
  username: null, userId: null, role: "civilian", lat: null, lon: null,
  sosIncidentId: localStorage.getItem("nexalert.pendingIncidentId"),
  selectedSosType: null, lastIncidentId: null, sosSubmitting: false,
  allUsers: [], socket: null, messageIds: new Set(), messageLoaded: false,
  online: false, peer: null, peerConnection: null, localStream: null,
  queuedIce: [], muted: false, incomingCaller: null,
};

function safeText(value) { return String(value ?? "").replace(/[&<>\"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function newId(prefix="") { return prefix + (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`); }
function statusMessage(el, text, kind="") { el.textContent=text; el.classList.remove("success","error"); if(kind) el.classList.add(kind); }
function setServerStatus(connected) {
  state.online=connected;
  [$("server-dot"),$("app-server-dot")].forEach(dot=>dot.classList.toggle("ok",connected));
  [$("server-dot"),$("app-server-dot")].forEach(dot=>dot.classList.toggle("offline",!connected));
  $("server-status").textContent=connected?"SERVER ONLINE":"SERVER OFFLINE";
  $("app-server-status").textContent=connected?"SERVER ONLINE":"SERVER OFFLINE";
}
async function checkServer() {
  try { const r=await fetch("/health",{cache:"no-store",signal:AbortSignal.timeout(3500)}); setServerStatus(r.ok); }
  catch (_) { setServerStatus(false); }
  try {
    const r=await fetch("/api/status",{cache:"no-store",signal:AbortSignal.timeout(3500)});
    if(r.ok){const d=await r.json();$("app-transport-status").textContent=d.transport==="simulated"?`SIMULATED LINK · ${d.simulated_transport_connected?"READY":"OFFLINE"}`:"RADIO UNAVAILABLE";}
  } catch (_) { $("app-transport-status").textContent="LINK STATUS UNKNOWN"; }
}
function showScreen(id) { document.querySelectorAll(".screen").forEach(x=>x.classList.remove("active")); $(id).classList.add("active"); }
async function register(event) {
  event?.preventDefault();
  const error=$("reg-error"); error.textContent="";
  let username=localStorage.getItem("nexalert.username");
  if(!username){username=`citizen-${newId().slice(0,18)}`;localStorage.setItem("nexalert.username",username);}
  const role=$("reg-role").value;
  try {
    const response=await fetch("/api/register",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({username,full_name:$("reg-name").value.trim(),role})});
    const data=await response.json();
    if(!response.ok||!data.user) throw new Error(data.error||"Could not join the network. You can still use emergency tools.");
    state.username=data.user.username;state.userId=data.user.id;state.role=data.user.role||role;
    localStorage.setItem("nexalert.username",state.username);
    enterApp(data.user.full_name&&data.user.full_name!=="Anonymous"?data.user.full_name:state.username,true);
  } catch(e) { error.textContent=`${e.message} Use “Continue to emergency tools” to submit an anonymous SOS.`; }
}
function continueAsGuest() { state.username=null;state.userId=null;state.role="civilian";enterApp("Emergency access",false); }
function enterApp(label,joined) {
  $("user-display").textContent=label;
  showScreen("app-screen");buildSosGrid();checkServer();
  if(joined){initSocket();loadUsers();loadMessages();loadAnnouncements();}
  loadIncidents();
  if(navigator.onLine) retryPendingSos();
}

function initSocket() {
  if(typeof io!=="function") { $("chat-state").textContent="Live messaging unavailable; checking saved messages by API.";return; }
  if(state.socket) state.socket.disconnect();
  state.socket=io({withCredentials:true,reconnection:true});
  state.socket.on("connect",()=>{ $("chat-state").textContent="Live network connection";state.socket.emit("join",{});loadUsers(); });
  state.socket.on("disconnect",()=>{ $("chat-state").textContent="Disconnected · messages will send when server returns"; });
  state.socket.on("connect_error",()=>{ $("chat-state").textContent="Live connection unavailable · saved messages remain accessible"; });
  state.socket.on("new_message",appendMessage);
  state.socket.on("system_announcement",addAnnouncementMessage);
  state.socket.on("user_online",({username})=>{updateUserOnlineStatus(username,true);appendSystemNotice(`online-${username}`,`${username} joined the network`);});
  state.socket.on("user_offline",({username})=>{updateUserOnlineStatus(username,false);appendSystemNotice(`offline-${username}`,`${username} left the network`);});
  state.socket.on("call:invite",onCallInvite);
  state.socket.on("call:accepted",onCallAccepted);
  state.socket.on("call:declined",({from})=>{if(from===state.peer)finishCall("Call declined by participant");});
  state.socket.on("call:signal",onCallSignal);
  state.socket.on("call:ended",({from})=>{if(from===state.peer||from===state.incomingCaller)finishCall("Participant ended the call");});
  state.socket.on("call:error",({message})=>finishCall(message||"Call setup failed"));
}

function switchTab(name) {
  document.querySelectorAll(".tab-panel").forEach(panel=>{const active=panel.id===`tab-${name}`;panel.classList.toggle("active",active);panel.hidden=!active;});
  document.querySelectorAll(".nav-btn").forEach(btn=>btn.classList.toggle("active",btn.dataset.tab===name));
  if(name==="map") loadIncidents();
  if(name==="call") loadUsers();
}

async function loadUsers() {
  try {
    const r=await fetch("/api/users",{cache:"no-store"});if(!r.ok)throw new Error("User list unavailable");
    state.allUsers=await r.json();
    const online=state.allUsers.filter(user=>Boolean(user.is_online)).length;
    $("online-count").textContent=`${online} online`;
    const recipient=$("chat-recipient"), old=recipient.value;
    recipient.innerHTML='<option value="">Network broadcast</option>';
    state.allUsers.filter(user=>user.username!==state.username).forEach(user=>{const option=document.createElement("option");option.value=user.username;option.textContent=`${user.is_online?"●":"○"} ${user.full_name||user.username}${user.is_online?" · online":""}`;recipient.append(option);});
    if([...recipient.options].some(o=>o.value===old))recipient.value=old;
    const calls=$("call-participant"), selected=calls.value;
    calls.innerHTML='<option value="">Choose an online participant</option>';
    state.allUsers.filter(user=>user.username!==state.username&&user.is_online).forEach(user=>{const option=document.createElement("option");option.value=user.username;option.textContent=user.full_name||user.username;calls.append(option);});
    if([...calls.options].some(o=>o.value===selected))calls.value=selected;
  } catch (_) { $("online-count").textContent="— online"; }
}
function updateUserOnlineStatus(username,online){const user=state.allUsers.find(x=>x.username===username);if(user)user.is_online=online?1:0;loadUsers();}

function appendMessage(msg) {
  if(!msg||msg.id==null||state.messageIds.has(`m-${msg.id}`))return;
  state.messageIds.add(`m-${msg.id}`);
  const item=document.createElement("article");
  const mine=msg.sender===state.username;
  item.className=`message ${msg.is_broadcast?"broadcast":mine?"sent":"received"}`;
  const text=document.createElement("div");text.textContent=msg.content||"";
  const meta=document.createElement("div");meta.className="message-meta";meta.textContent=`${mine?"You":msg.sender||"Network"} · ${formatTime(msg.timestamp)}`;
  item.append(text,meta);$("messages-container").append(item);$("messages-container").scrollTop=$("messages-container").scrollHeight;
}
function appendSystemNotice(key,text){const marker=`presence-${key}-${Math.floor(Date.now()/30000)}`;if(state.messageIds.has(marker))return;state.messageIds.add(marker);const item=document.createElement("article");item.className="message system";const body=document.createElement("div");body.textContent=text;const meta=document.createElement("div");meta.className="message-meta";meta.textContent=`NETWORK STATUS · ${new Date().toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"})}`;item.append(body,meta);$("messages-container").append(item);$("messages-container").scrollTop=$("messages-container").scrollHeight;}
function addAnnouncementMessage(row) {
  if(!row||row.id==null||state.messageIds.has(`a-${row.id}`))return;
  state.messageIds.add(`a-${row.id}`);
  const item=document.createElement("article");item.className="message system";
  const content=document.createElement("div");content.textContent=`COMMAND CENTRE · ${row.content}`;
  const meta=document.createElement("div");meta.className="message-meta";meta.textContent=`LOCAL APPLICATION BROADCAST · ${formatTime(row.created_at)}`;
  item.append(content,meta);$("messages-container").append(item);$("messages-container").scrollTop=$("messages-container").scrollHeight;
}
function formatTime(value){if(!value)return"time unavailable";const date=new Date(value.endsWith("Z")?value:value.replace(" ","T")+"Z");return Number.isNaN(date.valueOf())?value:date.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"});}
async function loadMessages(){
  if(!state.username){$("chat-state").textContent="Join the network to use chat. Emergency SOS is available without joining.";return;}
  $("chat-state").textContent="Loading saved messages…";
  try{const r=await fetch(`/api/messages?username=${encodeURIComponent(state.username)}`);if(!r.ok)throw new Error("Messages could not be loaded");const rows=await r.json();rows.reverse().forEach(appendMessage);state.messageLoaded=true;$("chat-state").textContent=rows.length?"Saved network messages":"No messages yet";}
  catch(e){$("chat-state").textContent=e.message;}
}
async function loadAnnouncements(){try{const r=await fetch("/api/announcements");if(r.ok)(await r.json()).reverse().forEach(addAnnouncementMessage);}catch(_) {}}
async function sendMessage(event){event?.preventDefault();const input=$("msg-input"),content=input.value.trim();if(!content)return;if(!state.username){$("chat-state").textContent="Join the network before sending messages.";return;}const recipient=$("chat-recipient").value;
  try{const r=await fetch("/api/messages",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({sender:state.username,recipient:recipient||null,content,is_broadcast:recipient?0:1})});const data=await r.json();if(!r.ok)throw new Error(data.error||"Message was not accepted");appendMessage(data.message);input.value="";$("chat-state").textContent=recipient?`Saved and sent to ${recipient}`:"Saved · local application broadcast";}
  catch(e){$("chat-state").textContent=`Message not sent: ${e.message}`;}}

function buildSosGrid(){const grid=$("sos-grid");grid.replaceChildren();Object.entries(window.NEXALERT_TYPES||{}).forEach(([key,meta])=>{const button=document.createElement("button");button.type="button";button.className="sos-btn";button.dataset.type=key;button.innerHTML=`<span class="sos-icon">${safeText(meta.icon)}</span><small>${safeText(meta.label)}</small>`;button.addEventListener("click",()=>selectSosType(key,button));grid.append(button);});}
function selectSosType(type,button){document.querySelectorAll(".sos-btn").forEach(b=>b.classList.toggle("selected",b===button));state.selectedSosType=type;if(!state.sosIncidentId){state.sosIncidentId=newId("INC-");localStorage.setItem("nexalert.pendingIncidentId",state.sosIncidentId);}sendSos();}
async function sendSos(){if(!state.selectedSosType||state.sosSubmitting)return;state.sosSubmitting=true;const status=$("sos-status");statusMessage(status,"Submitting SOS to the application…");
  const payload={alert_type:state.selectedSosType,description:$("sos-desc").value.trim(),landmark:$("sos-landmark").value.trim(),latitude:state.lat,longitude:state.lon,incident_id:state.sosIncidentId,idempotency_key:state.sosIncidentId};if(state.username)payload.username=state.username;
  try{const r=await fetch("/api/sos",{method:"POST",headers:{"Content-Type":"application/json","Idempotency-Key":state.sosIncidentId},body:JSON.stringify(payload)});const data=await r.json();if(!r.ok)throw new Error(data.error||"SOS was not accepted");const alert=data.alert;const labels={saved_locally:"saved locally",queued:"queued for delivery",relayed:"relayed",received_by_command_centre:"received by command centre"};const receipt=alert.receipt_id?` · ${safeText(alert.receipt_id)} (simulated receipt)`:"";statusMessage(status,`${alert.incident_id}: ${labels[alert.delivery_status]||alert.delivery_status}${receipt}`,"success");state.lastIncidentId=alert.incident_id;$("sos-followup").hidden=false;state.selectedSosType=null;state.sosIncidentId=null;localStorage.removeItem("nexalert.pendingIncidentId");$("sos-desc").value="";$("sos-landmark").value="";state.lat=state.lon=null;$("location-status").textContent="Location not attached";loadIncidents();}
  catch(e){const pending=JSON.parse(localStorage.getItem("nexalert.pendingSos")||"{}");pending[state.sosIncidentId]={...payload,username:state.username||undefined};localStorage.setItem("nexalert.pendingSos",JSON.stringify(pending));statusMessage(status,state.online?`SOS not accepted · ${e.message} · retry keeps ${state.sosIncidentId}`:`Server offline · saved on this device only · ${state.sosIncidentId}`,"error");}
  finally{state.sosSubmitting=false;}}
function attachLocation(){if(!navigator.geolocation){$("location-status").textContent="Location unavailable in this browser.";return;}$("location-status").textContent="Requesting one-time location…";navigator.geolocation.getCurrentPosition(p=>{state.lat=p.coords.latitude;state.lon=p.coords.longitude;$("location-status").textContent="Location attached to next SOS only.";},()=>{$("location-status").textContent="Location unavailable; SOS can still be submitted.";},{enableHighAccuracy:false,timeout:10000,maximumAge:60000});}
async function submitFollowup(){if(!state.lastIncidentId)return;const update={};if($("followup-description").value.trim())update.description=$("followup-description").value.trim();if($("followup-landmark").value.trim())update.landmark=$("followup-landmark").value.trim();if(!Object.keys(update).length){statusMessage($("sos-status"),"Add an optional detail first.");return;}try{const r=await fetch(`/api/incidents/${encodeURIComponent(state.lastIncidentId)}/updates`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(update)});const data=await r.json();if(!r.ok)throw new Error(data.error||"Update not saved");statusMessage($("sos-status"),`Update saved to ${state.lastIncidentId}`,"success");$("followup-description").value="";$("followup-landmark").value="";}catch(e){statusMessage($("sos-status"),e.message,"error");}}
async function retryPendingSos(){const pending=JSON.parse(localStorage.getItem("nexalert.pendingSos")||"{}");for(const [key,payload] of Object.entries(pending)){try{const r=await fetch("/api/sos",{method:"POST",headers:{"Content-Type":"application/json","Idempotency-Key":key},body:JSON.stringify(payload)});if(!r.ok)continue;delete pending[key];localStorage.setItem("nexalert.pendingSos",JSON.stringify(pending));const data=await r.json();statusMessage($("sos-status"),`${data.alert.incident_id}: ${data.alert.delivery_status.replaceAll("_"," ")}${data.alert.receipt_id?` · ${data.alert.receipt_id} (simulated receipt)`:""}`,"success");}catch(_){break;}}}

async function loadIncidents(){try{const r=await fetch("/api/incidents",{cache:"no-store"});if(!r.ok)throw new Error("Incident list unavailable");renderIncidentData(await r.json());}catch(e){$("citizen-incident-list").innerHTML=`<p class="empty-state">${safeText(e.message)}</p>`;$("citizen-unknown-list").innerHTML="";}}
function hasCoordinates(x){return Number.isFinite(Number(x.latitude))&&Number.isFinite(Number(x.longitude))&&x.latitude!==null&&x.longitude!==null&&!(Number(x.latitude)===0&&Number(x.longitude)===0);}
function renderIncidentData(rows){const mapped=rows.filter(hasCoordinates);const unknown=rows.filter(x=>!hasCoordinates(x));const map=$("citizen-map");map.replaceChildren();
  if(mapped.length){const latitudes=mapped.map(x=>Number(x.latitude)),longitudes=mapped.map(x=>Number(x.longitude));const minLat=Math.min(...latitudes),maxLat=Math.max(...latitudes),minLon=Math.min(...longitudes),maxLon=Math.max(...longitudes);mapped.forEach((x,i)=>{const button=document.createElement("button");button.className="plot-marker";button.type="button";button.title=`${x.incident_id} · ${x.latitude}, ${x.longitude}`;const xPct=maxLon===minLon?50:8+(Number(x.longitude)-minLon)/(maxLon-minLon)*84;const yPct=maxLat===minLat?50:8+(maxLat-Number(x.latitude))/(maxLat-minLat)*84;button.style.left=`${xPct}%`;button.style.top=`${yPct}%`;button.innerHTML=`<span>${i+1}</span>`;button.onclick=()=>showIncidentRow(x);map.append(button);});}else map.innerHTML='<p class="empty-state">No reports with coordinates</p>';
  $("citizen-incident-list").innerHTML=mapped.length?mapped.map((x,i)=>`<button class="incident-row" data-index="${i}"><strong>${safeText(x.label||x.alert_type)} · ${safeText(x.incident_id)}</strong>${safeText(x.latitude)}, ${safeText(x.longitude)} · ${safeText((x.delivery_status||"").replaceAll("_"," "))}</button>`).join(""):'<p class="empty-state">No mapped reports</p>';
  $("citizen-incident-list").querySelectorAll("[data-index]").forEach((button,i)=>button.addEventListener("click",()=>showIncidentRow(mapped[i])));
  $("citizen-unknown-list").innerHTML=unknown.length?unknown.map(x=>`<button class="incident-row" data-id="${safeText(x.incident_id)}"><strong>${safeText(x.label||x.alert_type)} · ${safeText(x.incident_id)}</strong>Location not reported · ${safeText((x.delivery_status||"").replaceAll("_"," "))}</button>`).join(""):'<p class="empty-state">No unmapped reports</p>';
  $("citizen-unknown-list").querySelectorAll("[data-id]").forEach((button,i)=>button.addEventListener("click",()=>showIncidentRow(unknown[i])));
}
function showIncidentRow(x){statusMessage($("sos-status"),`${x.incident_id} · ${x.latitude==null?"Location unknown":`${x.latitude}, ${x.longitude}`} · ${x.delivery_status.replaceAll("_"," ")}`);switchTab("sos");}

// Local Wi-Fi WebRTC signaling. Media is requested only after explicit action.
function callReady(){if(!window.isSecureContext||!navigator.mediaDevices?.getUserMedia){$("call-state").textContent="Calls unavailable here: microphone access requires HTTPS or localhost in a supported browser.";return false;}if(!state.socket?.connected){$("call-state").textContent="Call signalling is disconnected. Reconnect to the application first.";return false;}return true;}
async function startCall(){const target=$("call-participant").value;if(!target){$("call-state").textContent="Choose an online participant first.";return;}if(!callReady())return;state.peer=target;try{await preparePeer();state.callRole="caller";state.socket.emit("call:invite",{to:target});$("call-state").textContent=`Calling ${target}… waiting for acceptance`;setCallButtons(true);}catch(e){finishCall(`Could not access microphone: ${e.message}`);}}
async function onCallInvite(data){if(state.peerConnection){state.socket.emit("call:decline",{to:data.from});return;}state.incomingCaller=data.from;$("incoming-caller").textContent=`${data.from} is calling`;$("incoming-call").hidden=false;$("call-state").textContent="Incoming call · accept to allow microphone access";switchTab("call");}
async function acceptCall(){if(!callReady()){if(state.incomingCaller)state.socket?.emit("call:decline",{to:state.incomingCaller});state.incomingCaller=null;$("incoming-call").hidden=true;return;}state.peer=state.incomingCaller;state.callRole="callee";try{await preparePeer();$("incoming-call").hidden=true;state.socket.emit("call:accept",{to:state.peer});$("call-state").textContent=`Connecting to ${state.peer}…`;setCallButtons(true);}catch(e){if(state.peer)state.socket?.emit("call:decline",{to:state.peer});finishCall(`Could not access microphone: ${e.message}`);}}
async function preparePeer(){state.localStream=await navigator.mediaDevices.getUserMedia({audio:true,video:false});state.peerConnection=new RTCPeerConnection({iceServers:[]});state.localStream.getTracks().forEach(track=>state.peerConnection.addTrack(track,state.localStream));state.peerConnection.ontrack=e=>{$("remote-audio").srcObject=e.streams[0];};state.peerConnection.onicecandidate=e=>{if(e.candidate&&state.peer)state.socket.emit("call:signal",{to:state.peer,kind:"ice",data:e.candidate});};state.peerConnection.onconnectionstatechange=()=>{const s=state.peerConnection?.connectionState;if(s==="connected")$("call-state").textContent=`Connected to ${state.peer} · local Wi-Fi audio`;if(s==="failed")finishCall("Peer connection failed");if(s==="disconnected")$("call-state").textContent="Call connection interrupted…";};}
async function onCallAccepted(data){if(!state.peerConnection||data.from!==state.peer)return;try{const offer=await state.peerConnection.createOffer();await state.peerConnection.setLocalDescription(offer);state.socket.emit("call:signal",{to:state.peer,kind:"offer",data:offer});$("call-state").textContent=`Setting up encrypted browser audio with ${state.peer}…`;}catch(e){finishCall(`Call setup failed: ${e.message}`);}}
async function onCallSignal(message){if(!state.peerConnection||message.from!==state.peer)return;try{if(message.kind==="offer"){await state.peerConnection.setRemoteDescription(message.data);for(const ice of state.queuedIce)await state.peerConnection.addIceCandidate(ice);state.queuedIce=[];const answer=await state.peerConnection.createAnswer();await state.peerConnection.setLocalDescription(answer);state.socket.emit("call:signal",{to:state.peer,kind:"answer",data:answer});}else if(message.kind==="answer"){await state.peerConnection.setRemoteDescription(message.data);for(const ice of state.queuedIce)await state.peerConnection.addIceCandidate(ice);state.queuedIce=[];}else if(message.kind==="ice"){if(state.peerConnection.remoteDescription)await state.peerConnection.addIceCandidate(message.data);else state.queuedIce.push(message.data);}}catch(e){finishCall(`Call connection failed: ${e.message}`);}}
function declineCall(){if(state.incomingCaller)state.socket.emit("call:decline",{to:state.incomingCaller});state.incomingCaller=null;$("incoming-call").hidden=true;$("call-state").textContent="Call declined";}
function hangupCall(){if(state.peer)state.socket?.emit("call:end",{to:state.peer});finishCall("Call ended");}
function finishCall(message){if(state.localStream)state.localStream.getTracks().forEach(track=>track.stop());state.peerConnection?.close();state.peerConnection=null;state.localStream=null;state.peer=null;state.incomingCaller=null;state.callRole=null;state.queuedIce=[];state.muted=false;$("mute-call").textContent="Mute";$("remote-audio").srcObject=null;$("incoming-call").hidden=true;setCallButtons(false);$("call-state").textContent=message;}
function setCallButtons(active){$("start-call").disabled=active;$("accept-call").disabled=active;$("mute-call").disabled=!active;$("hangup-call").disabled=!active;}
function toggleMute(){if(!state.localStream)return;state.muted=!state.muted;state.localStream.getAudioTracks().forEach(track=>track.enabled=!state.muted);$("mute-call").textContent=state.muted?"Unmute":"Mute";}

document.addEventListener("DOMContentLoaded",()=>{
  $("join-form").addEventListener("submit",register);$("guest-continue").addEventListener("click",continueAsGuest);
  document.querySelectorAll(".nav-btn").forEach(btn=>btn.addEventListener("click",()=>switchTab(btn.dataset.tab)));
  $("message-form").addEventListener("submit",sendMessage);$("attach-location").addEventListener("click",attachLocation);$("send-followup").addEventListener("click",submitFollowup);
  $("start-call").addEventListener("click",startCall);$("accept-call").addEventListener("click",acceptCall);$("decline-call").addEventListener("click",declineCall);$("hangup-call").addEventListener("click",hangupCall);$("mute-call").addEventListener("click",toggleMute);
  checkServer();setInterval(checkServer,10000);setInterval(()=>{if(state.username)loadUsers();},20000);window.addEventListener("online",retryPendingSos);
});
