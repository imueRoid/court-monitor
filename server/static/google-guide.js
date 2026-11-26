"use strict";
const field = id => document.getElementById(id);
const local = ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
const rawIp = /^\d+(?:\.\d+){3}$/.test(location.hostname) || location.hostname.startsWith("[");
const usableAddress = local || location.protocol === "https:" && !rawIp;
const callback = usableAddress ? `${location.origin}/oauth/google/callback` : "";

field("guide-callback").value = callback;
field("copy-guide-callback").disabled = !usableAddress;
field("guide-address-message").textContent = usableAddress
  ? "위 URI를 Google Cloud에 그대로 등록한 뒤 같은 주소와 브라우저에서 연결을 진행하세요."
  : "현재 주소에서는 Google 콜백을 등록할 수 없습니다. 위의 HTTPS 도메인 또는 localhost 터널 주소로 이 안내를 다시 열어 주세요.";
field("delegate-text").value = `내 NAS의 Court Monitor를 내 Google Calendar에 연결할 수 있도록 설정해 주세요.

콜백 URI  ${callback || "[HTTPS 또는 localhost 주소에서 복사한 값]"}
Google Cloud 프로젝트  [기존 프로젝트 ID 또는 새 프로젝트 생성 여부]
연결할 계정  [캘린더를 사용할 Google 계정]

새 프로젝트에서 저만 사용할 경우 Google Auth Platform의 개인 모드를 확인해 주세요. 기존 외부 앱이라면 게시 상태와 테스트 사용자 등록 여부를 확인해 주세요. 같은 프로젝트에서 Google Calendar API를 켜고, 웹 애플리케이션 OAuth 클라이언트의 승인된 리디렉션 URI를 등록해 주세요. 앱은 https://www.googleapis.com/auth/calendar.app.created 권한을 요청합니다.

클라이언트를 만들 때 JSON을 내려받고 제 NAS의 설정 화면에 등록하겠습니다. Google 로그인, 본인 확인과 최종 권한 승인은 제가 진행합니다. 비밀번호와 시크릿이 든 JSON은 비공개로 보관하겠습니다.`;

async function copy(id, statusId) {
  const input = field(id);
  const status = field(statusId);
  try {
    await navigator.clipboard.writeText(input.value);
    status.textContent = "복사했습니다.";
  } catch {
    input.select();
    status.textContent = "선택한 내용을 복사하세요.";
  }
}

field("copy-guide-callback").addEventListener("click", () => copy("guide-callback", "copy-callback-message"));
field("copy-delegate").addEventListener("click", () => copy("delegate-text", "copy-message"));
field("guide-print").addEventListener("click", () => window.print());
