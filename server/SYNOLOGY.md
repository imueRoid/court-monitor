# 시놀로지 설치 — Compose 수정 없이 실행

Intel/AMD x86_64 CPU와 Container Manager를 사용하는 NAS 기준입니다. 공개 이미지 `imueroid/court-monitor:latest`를 사용하며 소스 빌드·별도 DB 컨테이너가 필요하지 않습니다.

## 설치 순서

1. [compose.synology.yaml](../compose.synology.yaml)을 그대로 내려받습니다. 값을 수정하지 않습니다.
2. File Station에서 프로젝트 폴더를 만듭니다. 예: `/volume1/docker/court-monitor`.
3. Container Manager → 프로젝트 → 생성에서 그 폴더를 선택합니다. Compose 내용을 편집기에 넣거나 `docker-compose.yml`로 업로드하고 시작합니다.
4. `http://NAS-IP:7708`로 접속합니다. 처음 설정 화면에서 관리자 비밀번호를 두 번 입력합니다. 이후에는 로그인 화면이 나옵니다.
5. 관리 화면의 **Google Calendar 연결 → 처음부터 따라 하는 상세 안내**를 엽니다. 안내 주소는 `http://NAS-IP:7708/guide/google`입니다.
6. 자신의 OAuth 앱을 처음 한 번 등록하고, 내려받은 웹 애플리케이션 JSON을 NAS 화면에서 등록합니다. **Google 계정 연결**에서 계정을 선택·승인하면 사건 전용 캘린더가 자동 생성됩니다.

SSH로 실행한다면:

```sh
sudo docker compose -f compose.synology.yaml up -d
```

NAS가 구형 `docker-compose` 명령을 사용한다면 같은 인수로 실행합니다. **호스트 네트워크(`network_mode: host`)**를 사용하고 프로그램이 NAS의 **7708 포트**에서 직접 수신합니다. `ports` 매핑은 사용하지 않으며 상태 검사도 7708 포트로 수행합니다. 변경된 Compose를 적용할 때는 같은 프로젝트를 재생성합니다.

## Google 독립 인증

각 사용자의 OAuth 클라이언트·Google 권한·캘린더·DB는 각자의 NAS에서 관리합니다. 공통 인증 서버가 없습니다. 앱은 자신의 OAuth 클라이언트가 만드는 보조 캘린더 범위인 `calendar.app.created` 권한을 요청합니다. 일반 개인 일정 전체를 수정하는 권한을 추가할 필요가 없습니다.

일반 HTTP NAS IP 주소 대신 HTTPS 주소 또는 localhost SSH 터널로 최초 인증합니다. 상세 안내 페이지에 DSM 역방향 프록시, localhost 터널, Google 프로젝트/API/동의 화면/클라이언트 생성, 콜백 URI 복사, JSON 등록, 재인증과 오류 해결을 설명했습니다. 설정을 도우미에게 맡길 때 복사할 요청문도 있습니다.

실제 Google 계정 승인까지 확인하려면 사용자 본인의 OAuth 설정이 필요합니다. External + Testing 상태의 Calendar 갱신 토큰은 7일 후 만료될 수 있으므로 장기 운영 시 게시·확인 절차를 마치세요.

## DB·로그인·재시작

- `court_data` named volume에 SQLite DB와 Google 인증 암호화 키를 저장합니다. 별도 DB 도커는 설치하지 않습니다.
- 비밀번호는 salt + PBKDF2-HMAC-SHA256 600,000회 해시, 관리자 세션은 토큰 해시로 저장합니다. Google 시크릿·토큰은 NAS의 키로 암호화합니다.
- 로그인 유지 기간은 기본 90일입니다. 로그아웃 또는 비밀번호 변경 시 해제할 수 있습니다. 비밀번호 변경은 웹에서 하며 모든 기기의 세션을 무효화합니다.
- 사건·결과·다음 확인 시각·Google 전송 대기열을 DB에 저장합니다. 재부팅 후 같은 볼륨으로 컨테이너가 시작되면 예약을 읽고 이어서 처리합니다. 오래된 조회를 전부 몰아서 실행하지 않습니다.
- Compose의 `restart: unless-stopped`는 비정상 종료·재부팅 후 재실행에 사용합니다. 명시적으로 중지한 컨테이너는 직접 다시 시작해야 할 수 있습니다.
- Google 연결 해제는 이 NAS에 저장한 토큰을 지웁니다. 기존 Google 캘린더와 일정은 보존합니다.

## 업데이트와 백업

같은 프로젝트에서 이미지를 새로 받고 재생성합니다.

```sh
sudo docker compose -f compose.synology.yaml pull
sudo docker compose -f compose.synology.yaml up -d
```

프로젝트 이름을 바꾸면 다른 볼륨을 사용할 수 있습니다. `down -v` 또는 볼륨 삭제는 DB·비밀번호·세션·Google 연결까지 지우므로 일반 업데이트에 사용하지 않습니다. 백업은 중지 상태에서 `court_data` 볼륨 전체를 보관하세요. DB와 `credentials.key`를 반드시 함께 복원합니다.

공식 참고: [Synology 프로젝트](https://kb.synology.com/en-global/DSM/help/ContainerManager/docker_project), [Google 웹 OAuth](https://developers.google.com/identity/protocols/oauth2/web-server).
