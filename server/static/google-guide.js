"use strict";
const callback=(location.protocol==="file:"?"https://본인NAS주소":location.origin)+"/oauth/google/callback";
document.getElementById("guide-callback").value=callback;
document.getElementById("delegate-text").value=`내 NAS의 Court Monitor를 Google Calendar에 독립 연결하도록 설정해 주세요.\n\n콜백 URI: ${callback}\nGoogle Cloud 프로젝트: [기존 프로젝트 ID 또는 새 프로젝트 생성 여부를 알려드립니다]\n연결할 Google 계정: [로그인한 본인 계정]\n\nGoogle Calendar API를 사용 설정하고, 동의 화면과 테스트 사용자·게시 상태를 확인해 주세요. 웹 애플리케이션 OAuth 클라이언트를 만들고 위 콜백 URI를 승인된 리디렉션 URI에 정확히 등록해 주세요. 권한은 https://www.googleapis.com/auth/calendar.app.created 를 사용합니다. 내려받은 클라이언트 JSON을 이 NAS의 관리 화면에 등록하고 Google 계정 연결 및 전용 캘린더 생성을 확인해 주세요.\n\n비밀번호·시크릿·토큰은 채팅·GitHub·공개 파일에 남기지 말고 NAS의 설정 화면에서만 처리해 주세요. 계정 로그인과 본인 확인은 제가 진행하겠습니다. 실제 외부 권한 승인·인증정보 생성 단계에서는 내용을 확인한 뒤 진행합니다.`;
const local=["localhost","127.0.0.1","[::1]"].includes(location.hostname);
document.getElementById("guide-address-message").textContent=location.protocol==="https:"||local?"현재 주소는 HTTPS 또는 localhost입니다. 위 URI를 Google Cloud에 정확히 등록한 뒤, 같은 주소·브라우저에서 연결하세요.":"현재는 일반 HTTP IP/호스트 주소입니다. 먼저 HTTPS 또는 localhost SSH 터널 주소로 이 페이지를 다시 열어야 Google 인증을 완료할 수 있습니다.";
async function copy(id){const field=document.getElementById(id);try{await navigator.clipboard.writeText(field.value);document.getElementById("copy-message").textContent="복사했습니다.";}catch{field.select();document.getElementById("copy-message").textContent="선택한 내용을 복사하세요.";}}
document.getElementById("copy-delegate").addEventListener("click",()=>copy("delegate-text"));
document.getElementById("copy-guide-callback").addEventListener("click",()=>copy("guide-callback"));
document.getElementById("guide-print").addEventListener("click",()=>window.print());
