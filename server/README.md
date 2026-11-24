# 사건 일정 관리 — Docker 서버

시놀로지에서 소스 빌드 없이 **7708 포트**로 실행하려면 [시놀로지 설치 안내](SYNOLOGY.md)와 저장소 루트의 `compose.synology.yaml`을 사용하세요. 아래는 소스에서 직접 빌드하는 설정입니다.

기존 사건검색 프로그램의 조회 코드를 사용하는 서버 버전입니다. 웹에서 사건을 등록하면 서버가 차회 기일을 확인해 **사건 관리 전용 Google Calendar**에 반영합니다. 선고일에는 주문이 올라오는지 다시 확인하고, 주문이 확인되면 기존 선고 일정의 설명을 갱신합니다.

Synology 전용 기능은 사용하지 않습니다. Linux x86_64 Docker 호스트에서 같은 패키지로 운영하도록 작성했습니다. Synology Container Manager도 같은 CPU·Docker 조건을 충족하면 배포 대상이 될 수 있습니다. ARM64 이미지 실행은 검증하지 않았습니다.

## 동작

| 상황 | 자동 처리 |
|---|---|
| 사건 등록 | 새 보안문자를 받아 로컬 OCR로 처리한 뒤 사건정보 조회 |
| 차회 기일 발견 | 법원·사건번호·기일 종류·시각·장소를 캘린더에 등록 |
| 기일 변경 | 새 응답에서 대체된 미래 기일의 기존 Google 일정 ID를 유지해 이동 |
| 일반적인 진행 중 사건 | 기본 24시간마다 확인하고, 기일 30분 후 다시 확인 |
| 일반 기일 당일 | 차회 기일 반영을 확인하기 위해 추가 조회 |
| 선고일 | 선고 시각 30분 후부터 09–19시 내에서 30분 간격, 하루 최대 8회 확인 |
| 주문 확인 전 | 다음 날에도 제한된 횟수로 계속 확인 |
| 주문 확인 | 해당 선고 일정 설명에 주문·상소정보 추가, 사건 자동 조회 종료 |
| 캘린더 장애 | 사건 결과를 보존하고 캘린더 전송만 별도로 재시도 |
| 서버 재시작 | SQLite에 저장된 사건·예약·미전송 일정을 이어서 처리 |

인식·인증 오류는 사건검색 클라이언트에서 새 보안문자로 최대 3회 시도합니다. 조회 자체가 실패하면 대기시간을 늘려 다시 시도합니다. 선고 확인의 실패도 일일 확인 횟수에 포함합니다. 수동 **지금 확인** 요청은 자동 확인 시간·횟수와 별도로 실행할 수 있습니다.

기일 시각이 없으면 캘린더에는 종일 일정으로 표시합니다. 조회 시각과 다음 확인 시각은 한국시간으로 표시합니다. 이미 지난 기일과 기존 캘린더 일정을 임의로 삭제하지 않습니다. 기존 주문이 비어 있는 민사사건은 원고승·원고패 등 종국결과를 사용합니다.

## 1. 환경 준비

필요한 것은 Docker Engine과 Docker Compose 플러그인입니다. 배포 패키지를 서버의 한 폴더에 압축 해제한 뒤 그 폴더에서 실행합니다.

```sh
python3 server/prepare.py
```

호스트에 Python이 없다면 Docker로 준비할 수 있습니다.

```sh
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" -w /work python:3.12-slim python server/prepare.py
```

이 명령은 `.env`와 `secrets/`를 만들며 기존 파일을 덮어쓰지 않습니다. 최초 관리자 암호는 `secrets/admin-password.txt`에 있습니다. 암호는 이미지에 포함하거나 로그에 출력하지 않습니다. 첫 기동 때 DB에 암호 해시를 저장하고 이후 암호 변경은 웹에서 합니다.

## 2. 사건 전용 Google Calendar 연결

웹 관리 화면의 **Google Calendar 연결 → 처음부터 따라 하는 상세 안내**를 엽니다. 각 NAS가 자신의 OAuth 클라이언트를 등록하고 본인 Google 계정으로 인증합니다. 최초 앱 등록 후 JSON 업로드·계정 선택·권한 승인을 웹에서 진행합니다. 프로그램이 사용자 소유의 사건 전용 캘린더를 자동 생성합니다. 공통 인증 서버나 서비스 계정 공유가 필요하지 않습니다.

안내 페이지는 `/guide/google`에 있으며 콜백 URI·설정 위임 요청문 복사, HTTPS/localhost 주소 준비, Cloud 프로젝트·Calendar API·동의 화면·웹 클라이언트 생성, Testing 토큰 만료와 오류 해결을 포함합니다. 시놀로지 Compose는 어떤 값도 수정하지 않습니다.

기존 서비스 계정 배포를 위한 환경변수·secret 파일 입력은 호환용으로 유지합니다. 신규 설치에는 웹의 독립 OAuth 방식을 권장합니다.

## 3. 실행

```sh
docker compose up -d --build
docker compose logs -f court
```

기본 관리 주소는 `http://127.0.0.1:8080`입니다. 원격 서버라면 SSH 터널로 접속할 수 있습니다.

```sh
ssh -L 8080:127.0.0.1:8080 user@your-server
```

브라우저에서 위 로컬 주소에 접속하고 로그인 화면에 관리자 비밀번호를 입력합니다. **사건 등록**, **지금 확인**, **중지/재개**, **기존 이력 가져오기**를 사용할 수 있습니다. 기존 데스크톱 이력 JSON과 내보낸 사건 JSON을 가져오면 서버에서 새로 조회합니다.

로그인은 기본 90일 동안 유지되며 세션은 SQLite에 보관해 재시작 후에도 유효합니다. 비밀번호 원문과 세션 토큰 원문은 DB에 저장하지 않습니다. 화면에서 로그아웃할 수 있고, 비밀번호를 변경하면 모든 기기의 세션이 해제됩니다. Compose 초기 암호가 웹에서 바꾼 DB 암호를 덮어쓰지 않습니다. SQLite는 웹 서버와 같은 컨테이너에서 동작하므로 별도 DB 컨테이너가 필요하지 않습니다.

### 도메인과 HTTPS로 운영

실제 도메인의 DNS를 서버로 지정하고 `.env`의 `DOMAIN`을 설정합니다. 서버의 80·443 포트가 해당 서버로 연결되는 환경에서 다음 명령을 사용합니다.

```sh
docker compose --profile https up -d --build
```

함께 제공한 Caddy가 인증서와 역방향 프록시를 처리합니다. 이때 접속 주소는 `https://설정한도메인`입니다. `.env`의 기본 `BIND_ADDRESS=127.0.0.1`은 그대로 둘 수 있습니다.

## 조회 주기 설정

`.env`를 수정하고 `docker compose up -d`로 반영합니다.

```dotenv
BASE_POLL_HOURS=24
HEARING_DELAY_MINUTES=30
VERDICT_RETRY_MINUTES=30
VERDICT_CHECKS_PER_DAY=8
WATCH_START_HOUR=9
WATCH_END_HOUR=19
REQUEST_GAP_SECONDS=5
```

여러 사건은 하나의 작업자가 순서대로 조회하며, 사건별 새 조회 시작 사이에는 최소 간격을 둡니다. 법원 응답이나 다른 사건의 조회가 늦으면 예정 시각보다 뒤에 실행될 수 있습니다. 웹 서버는 **workers=1**로 실행하며, 여러 컨테이너가 같은 SQLite 볼륨을 동시에 공유하는 구성은 지원 대상으로 삼지 않습니다.

작업 중 비정상 종료되면 조회 잠금은 최대 10분, 캘린더 잠금은 최대 3분 후 복구됩니다. Google 전송은 지속되는 이벤트 ID와 전송 대기열을 사용해 재시작 후 중복 생성 가능성을 줄입니다. 변경되지 않은 일정은 재전송하지 않습니다.

## 데이터와 업데이트

사건, 기일, 요약, 작업 예약, 캘린더 전송 상태, 관리자 비밀번호 해시, 로그인 세션은 `court_data` named volume에 저장됩니다. 전체 법원 원문 JSON이나 보안문자 토큰은 DB에 저장하지 않습니다. 최근 작업 로그는 최대 5,000건 보관합니다. 소스 빌드 설정에서는 기존 Google 서비스 계정 자격증명을 별도 read-only secret으로 마운트할 수 있으며, 신규 OAuth 클라이언트와 토큰은 웹에서 입력받아 NAS DB에 암호화해 저장합니다.

```sh
docker compose down
docker compose up -d --build
```

위 명령은 named volume을 삭제하지 않습니다. `docker compose down -v`는 사건 DB와 인증서 볼륨을 삭제하므로 이력 보존을 위한 업데이트에는 사용하지 않습니다. 백업은 서비스를 중지한 상태에서 `court_data` 볼륨 전체를 보관하거나, SQLite backup API를 이용해 수행합니다.

전용 캘린더 ID를 바꾸면 새 캘린더에 일정을 다시 동기화합니다. 이전 캘린더의 일정은 자동 삭제하지 않습니다.

## 검증 상태와 남은 연결 작업

- 서버 검사는 관리자 로그인·CSRF, 암호 해시·세션·로그아웃·암호 변경·로그인 제한, 등록·가져오기, 기일 추출, 선고 확인 횟수·시간대, 재시작 복원, 기일 변경, 주문 반영, Google 전송 오류 분리와 중복 방지 등을 포함합니다.
- 실제 제공된 사건 한 건으로 서버 조회·주문 추출·DB 저장·캘린더 전송 대기열 생성을 확인했습니다. 실제 사건정보와 자격증명은 배포 ZIP에 포함하지 않습니다.
- 로컬 웹 관리 화면의 인증·법원 검색·등록·결과 표시·중지·수동 확인을 브라우저에서 확인했습니다. 데스크톱·모바일 레이아웃을 점검했고 JavaScript 오류가 없었습니다.
- Google Calendar 연결·전송은 모의 응답으로 통합 검사합니다. 실제 승인을 통한 동작 확인은 사용자 본인의 OAuth 클라이언트와 계정 연결 후 가능합니다.
- Linux 이미지 빌드·실행·OCR 검사는 GitHub Actions에서 수행합니다. 실제 게시 상태와 결과는 [Actions 이력](https://github.com/imueRoid/court-monitor/actions)에서 확인할 수 있습니다. 개발 PC에서의 Python 검사와 Linux 의존성 사전 확인만으로 컨테이너 실행 성공을 판단하지 않습니다.
- 법원 데이터가 제공되지 않거나 사이트 구조가 바뀌면 자동 처리에는 수정이 필요할 수 있습니다. 자동화 상태와 마지막 조회·캘린더 전송 오류는 관리 화면에서 확인합니다.

개발 환경 검사:

```sh
python -m pip install -r requirements-server.txt httpx
python -m unittest discover -s tests -p 'test_server*.py' -v
```

Docker 이미지는 검증한 버전 목록인 `requirements-server.lock.txt`로 설치합니다.

## 공식 참고 자료

- [Google Calendar 일정 생성과 이벤트 ID](https://developers.google.com/workspace/calendar/api/guides/create-events)
- [이벤트 필드와 날짜·시간 형식](https://developers.google.com/workspace/calendar/api/v3/reference/events)
- [Calendar API 권한 범위](https://developers.google.com/workspace/calendar/api/auth)
- [서비스 계정 인증](https://developers.google.com/identity/protocols/oauth2/service-account)
- [Docker Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/)
- [OWASP 비밀번호 저장 지침](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)
