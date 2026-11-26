# 사건 일정 관리 · Linux Docker 서버

Linux x86_64 서버에서 저장소의 [`compose.yaml`](../compose.yaml)로 Rust 웹 서버를 직접 빌드하는 방법입니다. Synology의 공개 이미지 설치와 웹 앱 업데이트는 [시놀로지 설치 안내](SYNOLOGY.md)에서 확인할 수 있습니다.

## 1. 환경 파일 만들기

Docker Engine과 Docker Compose 플러그인을 준비합니다. 저장소의 루트에서 다음 명령을 실행합니다.

```sh
python3 server/prepare.py
```

Python이 설치된 Docker 컨테이너로도 준비할 수 있습니다.

```sh
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" -w /work python:3.12-slim python server/prepare.py
```

이 명령은 `.env`와 `secrets/`를 만들고 최초 관리자 비밀번호를 `secrets/admin-password.txt`에 저장합니다. 이미 생성된 파일이 있으면 그 내용을 사용합니다. 이후 비밀번호는 웹 화면에서 바꿀 수 있습니다.

## 2. 빌드하고 접속하기

```sh
docker compose up -d --build
docker compose logs -f court
```

기본 주소는 `http://127.0.0.1:8080`입니다. 원격 서버는 아래처럼 SSH 터널을 연 뒤 같은 PC의 브라우저에서 기본 주소를 엽니다.

```sh
ssh -N -L 8080:127.0.0.1:8080 user@your-server
```

관리자 비밀번호로 로그인해 사건을 등록합니다. **기존 이력 가져오기**에서 Windows 조회 이력 JSON 또는 웹에서 내보낸 사건 목록을 가져오면 서버가 사건을 다시 조회합니다. 사건 관리와 종료된 사건 탭은 NAS 앱과 같은 방식으로 동작합니다. [사건 조회와 화면](SYNOLOGY.md#사건-조회와-화면)

### HTTPS로 접속하기

포트 80·443으로 연결되는 도메인을 확보하고 `.env`에 `DOMAIN`을 설정합니다. 기본 `BIND_ADDRESS=127.0.0.1`을 사용하면 내부 관리 포트는 서버의 루프백에 바인딩됩니다.

```sh
docker compose --profile https up -d --build
```

[`server/Caddyfile`](Caddyfile)의 설정에 따라 Caddy가 인증서와 역방향 프록시를 처리합니다. 브라우저에서 `https://설정한도메인`을 엽니다.

## 3. Google Calendar 연결

웹의 **설정 → Google Calendar 연결 → 처음부터 따라 하는 상세 안내**를 엽니다. 각 설치자가 본인 Google Cloud 프로젝트에서 웹 OAuth 클라이언트를 만들고 JSON을 등록한 다음 Google에서 승인하면, 본인 계정에 사건 전용 캘린더가 만들어집니다. [Google 연결 절차](SYNOLOGY.md#google-독립-인증) · [Google Auth Platform 도움말](https://support.google.com/cloud/answer/15544987)

앱의 `/guide/google`에서 현재 주소의 콜백을 복사합니다. 예를 들어 기본 SSH 터널에서 앱을 열면 콜백은 `http://127.0.0.1:8080/oauth/google/callback`입니다. 성공적으로 연결한 뒤 사건에 기일이 확인되면 Google Calendar의 **사건 관리** 캘린더에 반영됩니다.

### 기존 서비스 계정 호환 설정

서비스 계정으로 운영하던 설치는 `secrets/google-service-account.json`과 `.env`의 `GOOGLE_CALENDAR_ID`를 계속 사용할 수 있습니다. 해당 캘린더는 서비스 계정에 일정 수정 권한으로 공유합니다. 서비스 계정 JSON과 웹 화면에 올리는 OAuth 클라이언트 JSON은 각각 지정된 설정 경로에서 사용합니다. [Google 서비스 계정 인증 안내](https://developers.google.com/identity/protocols/oauth2/service-account) · [Docker Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/)

## 자동 조회와 일정 반영

| 상황 | 기본 동작 |
| --- | --- |
| 사건 등록 또는 일반 확인 | 작업자가 사건을 순서대로 조회합니다. 진행 중인 사건은 기본 24시간마다 확인합니다. |
| 기일 확인과 변경 | 확인된 기일을 전송 대기열에 넣습니다. 시간이 없으면 종일 일정으로 반영합니다. 대체된 미래 일정은 기존 Google 이벤트 ID를 이용해 시간을 수정합니다. |
| 선고일 | 선고 시각 30분 후부터 한국시간 09~19시에 30분 간격으로 하루 최대 8회 주문을 확인합니다. |
| 주문 또는 종국결과 확인 | 종료된 사건 탭에서 볼 수 있습니다. 선고 일정이 있으면 주문 등 확인한 정보를 반영합니다. |
| 캘린더 전송 실패 | 사건 조회 결과를 저장하고 캘린더 전송을 별도로 재시도합니다. |

기본값을 조정하려면 `.env`에 필요한 값을 저장하고 `docker compose up -d`로 반영합니다.

```dotenv
BASE_POLL_HOURS=24
HEARING_DELAY_MINUTES=30
VERDICT_RETRY_MINUTES=30
VERDICT_CHECKS_PER_DAY=8
WATCH_START_HOUR=9
WATCH_END_HOUR=19
REQUEST_GAP_SECONDS=5
```

수동 **지금 확인** 요청은 자동 주문 확인의 시간대와 횟수 외에도 사용할 수 있습니다. 법원 응답이 늦거나 앞선 조회가 진행 중이면 예정 시각보다 늦어질 수 있습니다. 하나의 `court` 서비스가 하나의 SQLite 볼륨을 사용합니다.

## 로컬 빌드 갱신과 백업

최신 소스를 가져온 뒤 같은 프로젝트에서 다음 명령을 실행합니다. HTTPS 프로필로 실행했다면 갱신할 때도 `--profile https`를 사용합니다.

```sh
docker compose up -d --build
```

사건, 예약, Google 연결, 캘린더 전송 대기열과 로그인 정보는 `court_data`에 있습니다. 컨테이너는 같은 볼륨에서 이어서 처리합니다. 백업은 서비스를 중지한 상태에서 볼륨 전체를 보관하고, 복원할 때 SQLite와 `credentials.key`를 함께 복원합니다. `docker compose down -v`는 볼륨까지 지우는 명령이므로 데이터가 필요한 설치의 갱신 시에는 피합니다.

[개발 환경과 검증](../docs/DEVELOPMENT.md) · [GitHub Actions](https://github.com/imueRoid/court-monitor/actions)
