# 시놀로지 설치 — Compose 수정 없이 실행

Intel/AMD x86_64 CPU와 Container Manager를 사용하는 NAS 기준입니다. 공개 이미지 `imueroid/court-monitor:latest`를 사용하며 소스 빌드·별도 DB 컨테이너가 필요하지 않습니다.

## 설치 순서

1. [compose.synology.yaml](../compose.synology.yaml)을 그대로 내려받습니다. 값을 수정하지 않습니다.
2. File Station에서 프로젝트 폴더를 만듭니다. 예: `/volume1/docker/court-monitor`.
3. Container Manager → 프로젝트 → 생성에서 그 폴더를 선택합니다. Compose 내용을 편집기에 넣거나 `docker-compose.yml`로 업로드하고 시작합니다.
4. `http://NAS-IP:7708`로 접속합니다. 처음 설정 화면에서 관리자 비밀번호를 두 번 입력합니다. 이후에는 로그인 화면이 나옵니다.
5. Calendar 연동을 사용하려면 관리 화면의 **설정 → Google Calendar 연결 → 처음부터 따라 하는 상세 안내**를 엽니다. 안내 주소는 `http://NAS-IP:7708/guide/google`입니다.
6. 안내를 따라 본인의 OAuth 앱을 설정하고, 내려받은 웹 애플리케이션 JSON을 NAS에 등록합니다. **Google 계정 연결**에서 본인 계정을 선택·승인하면 사건 전용 캘린더가 만들어집니다.

SSH로 실행한다면:

```sh
sudo docker compose -f compose.synology.yaml up -d
```

NAS가 구형 `docker-compose` 명령을 사용한다면 같은 인수로 실행합니다. **호스트 네트워크(`network_mode: host`)**를 사용하고 프로그램이 NAS의 **7708 포트**에서 직접 수신합니다. `ports` 매핑은 사용하지 않으며 상태 검사도 7708 포트로 수행합니다. 변경된 Compose를 적용할 때는 같은 프로젝트를 재생성합니다.

## Google 독립 인증

각 설치자가 본인 Google Cloud 프로젝트와 승인 권한을 관리합니다. 설치 후 **설정 → Google Calendar 연결 → 처음부터 따라 하는 상세 안내**에서 실제로 접속 중인 주소의 콜백을 복사할 수 있습니다. 상세 안내에는 설정을 맡길 때 사용할 요청문과 오류별 해결 방법도 있습니다.

1. NAS를 HTTPS 도메인으로 열거나 PC에서 NAS로 localhost SSH 터널을 연 뒤 같은 브라우저로 접속합니다. 예를 들어 HTTPS를 쓰면 `https://court.example.com/oauth/google/callback`, 기본 localhost 터널을 쓰면 `http://localhost:7708/oauth/google/callback`을 Google에 등록합니다. [DSM 역방향 프록시](https://kb.synology.com/en-global/DSM/help/DSM/AdminCenter/system_login_portal_advanced?version=7) · [Google 리디렉션 URI 규칙](https://developers.google.com/identity/protocols/oauth2/web-server#uri-validation)
2. 본인 계정 한 개를 연결하는 새 프로젝트는 Google의 개인 모드를 사용할 수 있습니다. 기존 외부 프로젝트는 **대상**에서 테스트 사용자와 게시 상태를 확인합니다. 외부 테스트 모드의 Calendar 승인은 7일 뒤 만료됩니다. 같은 프로젝트에서 Calendar API를 켜고 앱이 요청하는 `calendar.app.created` 권한을 확인합니다. [개인 모드 설정](https://console.cloud.google.com/auth/overview/personal) · [사용자 유형과 게시 상태](https://support.google.com/cloud/answer/15549945?hl=ko) · [Calendar API 사용 설정](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com) · [Calendar 권한 범위](https://developers.google.com/workspace/calendar/api/auth)
3. 웹 애플리케이션 클라이언트의 승인된 리디렉션 URI를 등록하고, 생성 창에서 시크릿이 포함된 JSON을 내려받아 비공개로 보관합니다. 같은 주소로 NAS에 로그인해 **OAuth JSON 선택하여 등록 → Google 계정 연결**을 진행하고 Google Calendar의 **사건 관리** 캘린더를 확인합니다. [OAuth 클라이언트 콘솔](https://console.cloud.google.com/auth/clients) · [클라이언트 JSON과 시크릿 관리](https://support.google.com/cloud/answer/15549257)

## DB·로그인·재시작

- `court_data` named volume에 SQLite DB와 Google 인증 암호화 키를 저장합니다. 별도 DB 도커는 설치하지 않습니다.
- 비밀번호는 salt + PBKDF2-HMAC-SHA256 600,000회 해시, 관리자 세션은 토큰 해시로 저장합니다. Google 시크릿·토큰은 NAS의 키로 암호화합니다.
- 로그인 유지 기간은 기본 90일입니다. 로그아웃 또는 비밀번호 변경 시 해제할 수 있습니다. 비밀번호 변경은 웹에서 하며 모든 기기의 세션을 무효화합니다.
- 사건·결과·다음 확인 시각·Google 전송 대기열을 DB에 저장합니다. 재부팅 후 같은 볼륨으로 컨테이너가 시작되면 예약을 읽고 이어서 처리합니다. 오래된 조회를 전부 몰아서 실행하지 않습니다.
- Compose의 `restart: unless-stopped`는 비정상 종료·재부팅 후 재실행에 사용합니다. 명시적으로 중지한 컨테이너는 직접 다시 시작해야 할 수 있습니다.
- Google 연결 해제는 이 NAS에 저장한 토큰을 지웁니다. 기존 Google 캘린더와 일정은 보존합니다.

## 업데이트와 백업

Rust NAS 이미지로 처음 한 번 업데이트하세요. 새 이미지를 받은 뒤 **기존 Compose 설정으로 컨테이너를 재생성**합니다. 기존 시작 명령과 상태 검사도 새 이미지에서 동작합니다. 같은 프로젝트 이름과 `/data` 볼륨을 사용하면 사건 데이터, 로그인 세션, Google 연결을 이어서 사용할 수 있습니다.

### 평소 앱 업데이트

- NAS 재부팅이나 컨테이너 시작 시 [GitHub Releases](https://github.com/imueRoid/court-monitor/releases)에서 새 버전을 확인해, 현재 이미지에 호환되는 앱 패키지를 내려받습니다. 다운로드 실패 시 설치된 버전으로 시작하고, 새 앱이 시작되지 않으면 이전 버전으로 돌아갑니다.
- 웹 관리 화면의 **업데이트 적용**을 누르면 검증한 패키지를 설치하고 같은 컨테이너 안에서 앱을 다시 시작합니다. 기존 SQLite 데이터와 로그인 세션을 이어서 사용합니다. 설치된 버전은 화면 하단이나 `/health`에서 확인할 수 있습니다.
- 재부팅 시 자동 적용을 끄려면 서비스 환경에 `COURT_AUTO_UPDATE: "false"`를 추가합니다. 웹 화면에서 수동으로 적용할 수 있습니다.
- 사건 상세에는 선고 전까지 차회 기일을 표시합니다. 변론 중에는 **변론 진행중**, 차회 기일이 선고면 **변론 종결됨**, 선고일에는 **주문 확인중**으로 표시합니다. 민사·행정 사건은 법원에 기록된 종국결과도 판결 내용에 표시합니다. 주문이 확인되면 사건 상세를 다시 열거나 **지금 확인**을 눌러 상소·확정 정보를 갱신할 수 있습니다.
- 첫 화면의 **사건 관리**에는 진행 중인 사건이 표시됩니다. 판결 주문이나 종국결과가 확인되면 **종료된 사건** 탭으로 이동하며, 각 탭의 목록에서 해당 사건을 검색할 수 있습니다.

### Docker 실행 환경이 바뀌었을 때

앱 화면에 Docker 이미지 업데이트가 필요하다고 나오면 이미지와 컨테이너를 갱신합니다. 배포 이미지는 `latest`, `1`, `1.0`, `1.0.<빌드번호>`, `sha-커밋SHA` 태그를 제공합니다. 이미지 태그는 해당 실행 환경이 게시된 앱 버전입니다.

시놀로지용 기본 Compose는 계속 `latest`를 사용하고 `pull_policy: always`를 지정합니다. 따라서 새 버전마다 YAML의 이미지 태그를 고칠 필요가 없습니다. 이 정책은 Compose로 프로젝트를 실행·재생성할 때 이미지를 확인하는 설정이며, 실행 중인 컨테이너를 주기적으로 자동 교체하는 기능은 아닙니다. 컨테이너의 단순 재시작이나 NAS 재부팅만으로는 새 이미지가 적용되지 않을 수 있습니다.

Container Manager의 **이미지 → 해당 이미지 선택 → 작업 → 업데이트**로 새 이미지를 받은 뒤 기존 프로젝트를 재생성/빌드합니다. DSM 버전에 따라 메뉴 표현이 다를 수 있습니다. 업데이트 알림이 보이지 않아도 아래처럼 명시적으로 받을 수 있습니다.

같은 YAML 파일과 프로젝트 이름으로 이미지 갱신을 실행합니다. 기존 설치의 네트워크와 포트, 데이터 볼륨을 사용하세요. `./court_monitor:/data` 같은 바인드 마운트도 같은 경로를 사용합니다.

```sh
# 기존 프로젝트 폴더에서 실행하며, 파일 이름은 실제 프로젝트의 YAML을 사용합니다.
sudo docker compose -f compose.synology.yaml pull court
sudo docker compose -f compose.synology.yaml up -d --no-deps --force-recreate court
```

Container Manager에서 프로젝트 이름을 별도로 지정했다면 두 명령에 동일한 `-p 기존프로젝트이름`을 추가합니다. Docker Compose v1 환경에서는 `docker-compose` 명령을 사용합니다. 매우 오래된 Compose가 `pull_policy`를 지원하지 않으면 그 한 줄만 제외하고 위의 명시적 pull·재생성 방식을 사용합니다.

`1.0.<빌드번호>`처럼 특정 버전에 고정하면 이후 버전을 자동으로 따라가지 않습니다. 보통은 `latest`를 유지하고, 재현·이전 버전 복귀가 필요한 경우에만 숫자 버전 태그를 선택하세요.

이전 배포에는 숫자 버전과 실행 중인 버전 표시가 없었고 OCI 인덱스에 실행 이미지와 `unknown/unknown` 빌드 증명 항목이 함께 들어 있었습니다. 새 배포는 단일 `linux/amd64` Docker V2 매니페스트로 게시해 형식 호환성을 단순화합니다. 게시 후 CI에서 태그들이 같은 다이제스트인지와 버전 라벨·이미지 형식을 실제 레지스트리 응답으로 검사합니다. Container Manager의 알림이 없다는 이유만으로 새 이미지가 없다고 판단하지 마세요.

프로젝트 이름을 바꾸면 다른 볼륨을 사용할 수 있습니다. `down -v` 또는 볼륨 삭제는 DB·비밀번호·세션·Google 연결까지 지우므로 일반 업데이트에 사용하지 않습니다. 백업은 중지 상태에서 `court_data` 볼륨 전체를 보관하세요. DB와 `credentials.key`를 반드시 함께 복원합니다.

공식 참고: [Synology 프로젝트](https://kb.synology.com/en-global/DSM/help/ContainerManager/docker_project), [Google 웹 OAuth](https://developers.google.com/identity/protocols/oauth2/web-server).

업데이트 참고: [Synology 이미지 관리](https://kb.synology.com/index.php/en-us/DSM/help/ContainerManager/docker_image?version=7), [Docker pull](https://docs.docker.com/reference/cli/docker/compose/pull/), [Docker up](https://docs.docker.com/reference/cli/docker/compose/up/), [이미지 게시 형식](https://docs.docker.com/build/exporters/image-registry/).
