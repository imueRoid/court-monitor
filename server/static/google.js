"use strict";

let googleInfo;
let googleMode;
let sharedWait;

function showGoogleMode() {
  if (!googleInfo) return;
  const direct = googleMode === "direct";
  $("google-client-details").hidden = !direct;
  $("google-connect").disabled = Boolean(sharedWait) || (direct && !googleInfo.client_configured);
  $("google-mode-hint").textContent = direct
    ? "이 설치에서 본인의 Google Cloud 프로젝트와 OAuth JSON을 사용합니다."
    : "Google 승인과 토큰 갱신에 court.imue.one을 사용합니다. 사건 일정은 이 설치에서 Google Calendar로 직접 보냅니다.";
  if (direct && !googleInfo.client_configured) $("google-client-details").open = true;
}

function renderGoogle(info, configured = info.connected) {
  googleInfo = info;
  googleMode ??= info.shared_available ? (info.connection_method || "shared") : "direct";
  $("google-mode-settings").hidden = !info.shared_available;
  $("google-mode").value = googleMode;
  setCalendarConfigured(configured);
  if (info.connected) {
    $("google-status").textContent = `연결됨 · ${info.calendar_name || "사건 관리"} 전용 캘린더`;
  } else if (configured) {
    $("google-status").textContent = "연결됨 · Google 서비스 계정";
  } else if (info.authorized) {
    $("google-status").textContent = "Google 인증은 완료됐습니다. 전용 캘린더 준비를 다시 시도하세요.";
  } else {
    $("google-status").textContent = "Google Calendar가 아직 연결되지 않았습니다.";
  }
  $("google-connect").textContent = info.connected ? "Google 계정 다시 연결" : "Google 계정 연결";
  $("google-retry").hidden = !(info.pending_authorization || (info.authorized && !info.connected));
  $("google-disconnect").hidden = !(info.connected || info.authorized);
  $("google-message").textContent = info.error || "";
  showGoogleMode();
}

function endSharedWait() {
  sharedWait?.abort();
  sharedWait = null;
  $("google-shared-approval").hidden = true;
  $("google-shared-link").removeAttribute("href");
  showGoogleMode();
}

async function waitForSharedApproval(controller) {
  const deadline = Date.now() + 600_000;
  try {
    while (sharedWait === controller && Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 3_000));
      if (sharedWait !== controller) return;
      const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(90_000)]);
      const update = await api("/google/shared/complete", {}, signal);
      if (sharedWait !== controller) return;
      if (update.status === "connected") {
        renderGoogle(update.google);
        await reload();
        return;
      }
    }
    if (sharedWait === controller) $("google-message").textContent = "승인 시간이 지났습니다. Google 계정 연결을 다시 눌러 주세요.";
  } catch (error) {
    if (sharedWait === controller) $("google-message").textContent = error.message;
  } finally {
    if (sharedWait === controller) endSharedWait();
  }
}

$("google-callback").value = location.origin + "/oauth/google/callback";
if (location.protocol !== "https:" && !["localhost", "127.0.0.1", "[::1]"].includes(location.hostname)) {
  $("google-address-hint").textContent = "직접 인증에는 HTTPS 주소 또는 localhost SSH 터널이 필요합니다.";
}
window.addEventListener("court-ready", event => renderGoogle(event.detail, state.config.calendar_configured));
$("google-mode").addEventListener("change", () => {
  googleMode = $("google-mode").value;
  endSharedWait();
  $("google-message").textContent = "";
});
$("copy-callback").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("google-callback").value);
    $("google-message").textContent = "콜백 주소를 복사했습니다.";
  } catch {
    $("google-callback").select();
    $("google-message").textContent = "선택한 주소를 복사하세요.";
  }
});
$("google-upload").addEventListener("click", () => $("google-client-file").click());
$("google-client-file").addEventListener("change", async () => {
  const input = $("google-client-file");
  const file = input.files[0];
  if (!file) return;
  $("google-upload").disabled = true;
  try {
    if (file.size > 100000) throw new Error("OAuth 클라이언트 JSON 파일을 선택하세요. 최대 100KB입니다.");
    const config = JSON.parse(await file.text());
    googleMode = "direct";
    renderGoogle(await api("/google/client", {config}));
    $("google-message").textContent = "OAuth 앱을 등록했습니다. Google 계정 연결을 눌러 승인하세요.";
    $("google-client-details").open = false;
  } catch (error) {
    $("google-message").textContent = error.message;
  } finally {
    input.value = "";
    $("google-upload").disabled = false;
  }
});
$("google-connect").addEventListener("click", async () => {
  const method = googleMode;
  const mode = $("google-mode");
  $("google-connect").disabled = true;
  mode.disabled = true;
  try {
    const payload = method === "direct" ? {method, redirect_uri: $("google-callback").value} : {method};
    const data = await api("/google/start", payload);
    if (method === "direct") {
      location.assign(data.url);
      return;
    }
    const controller = new AbortController();
    sharedWait = controller;
    $("google-shared-link").href = data.url;
    $("google-shared-approval").hidden = false;
    $("google-message").textContent = "Google 승인 창을 열어 계정을 연결하세요. 승인하면 이 화면에서 연결이 완료됩니다.";
    void waitForSharedApproval(controller);
  } catch (error) {
    $("google-message").textContent = error.message;
  } finally {
    mode.disabled = false;
    showGoogleMode();
  }
});
$("google-shared-cancel").addEventListener("click", () => {
  endSharedWait();
  $("google-message").textContent = "승인 대기를 중단했습니다. Google 계정 연결을 눌러 다시 시작하세요.";
});
$("google-retry").addEventListener("click", async () => {
  $("google-retry").disabled = true;
  try {
    renderGoogle(await api("/google/calendar", {}));
    await reload();
  } catch (error) {
    $("google-message").textContent = error.message;
  } finally {
    $("google-retry").disabled = false;
  }
});
$("google-disconnect").addEventListener("click", async () => {
  endSharedWait();
  try {
    renderGoogle(await api("/google/disconnect", {}));
    await reload();
    $("google-message").textContent = "이 NAS의 저장된 연결 권한을 삭제했습니다. Google 계정의 타사 앱 연결에서도 권한을 취소할 수 있습니다. 기존 캘린더와 일정은 보존됩니다.";
  } catch (error) {
    $("google-message").textContent = error.message;
  }
});
