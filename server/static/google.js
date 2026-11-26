"use strict";
function renderGoogle(info, configured=info.connected) {
  setCalendarConfigured(configured);
  if (info.connected) {
    $("google-status").textContent = `연결됨 · ${info.calendar_name || "사건 관리"} 전용 캘린더`;
  } else if (configured) {
    $("google-status").textContent = "연결됨 · Google 서비스 계정";
  } else if (info.authorized) {
    $("google-status").textContent = "Google 인증은 완료됐습니다. 전용 캘린더 준비를 다시 시도하세요.";
  } else {
    $("google-status").textContent = info.client_configured
      ? "OAuth 앱 등록 완료 · Google 계정을 연결해 주세요."
      : "아직 연결되지 않았습니다. 상세 안내를 따라 OAuth 앱을 처음 한 번 등록하세요.";
  }
  $("google-connect").disabled = !info.client_configured;
  $("google-connect").textContent = info.connected ? "Google 계정 다시 연결" : "Google 계정 연결";
  $("google-retry").hidden = !(info.authorized && !info.connected);
  $("google-disconnect").hidden = !(info.connected || info.authorized);
  $("google-message").textContent = info.error || "";
  if (!info.client_configured && !configured) $("google-client-details").open = true;
}
$("google-callback").value=location.origin+"/oauth/google/callback";
if(location.protocol!=="https:"&&!["localhost","127.0.0.1","[::1]"].includes(location.hostname))$("google-address-hint").textContent="현재 HTTP IP 주소로는 Google 인증을 완료할 수 없습니다. 상세 안내의 HTTPS 주소 또는 localhost SSH 터널을 준비한 뒤 그 주소로 접속하세요.";
window.addEventListener("court-ready", event => renderGoogle(event.detail, state.config.calendar_configured));
$("copy-callback").addEventListener("click",async()=>{try{await navigator.clipboard.writeText($("google-callback").value);$("google-message").textContent="콜백 주소를 복사했습니다.";}catch{$("google-callback").select();$("google-message").textContent="선택한 주소를 복사하세요.";}});
$("google-upload").addEventListener("click",()=>$("google-client-file").click());
$("google-client-file").addEventListener("change",async()=>{const input=$("google-client-file");const file=input.files[0];if(!file)return;$("google-upload").disabled=true;try{if(file.size>100000)throw new Error("OAuth 클라이언트 JSON 파일을 선택하세요. 최대 100KB입니다.");const config=JSON.parse(await file.text());renderGoogle(await api("/google/client",{config}));$("google-message").textContent="OAuth 앱을 등록했습니다. Google 계정 연결을 눌러 승인하세요.";$("google-client-details").open=false;}catch(error){$("google-message").textContent=error.message;}finally{input.value="";$("google-upload").disabled=false;}});
$("google-connect").addEventListener("click",async()=>{$("google-connect").disabled=true;try{const data=await api("/google/start",{redirect_uri:$("google-callback").value});location.assign(data.url);}catch(error){$("google-message").textContent=error.message;$("google-connect").disabled=false;}});
$("google-retry").addEventListener("click",async()=>{$("google-retry").disabled=true;try{renderGoogle(await api("/google/calendar",{}));await reload();}catch(error){$("google-message").textContent=error.message;}finally{$("google-retry").disabled=false;}});
$("google-disconnect").addEventListener("click",async()=>{try{renderGoogle(await api("/google/disconnect",{}));await reload();$("google-message").textContent="이 NAS의 저장된 연결 권한을 삭제했습니다. Google 계정의 타사 앱 연결에서도 권한을 취소할 수 있습니다. 기존 캘린더와 일정은 보존됩니다.";}catch(error){$("google-message").textContent=error.message;}});
