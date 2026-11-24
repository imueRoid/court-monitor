# 시놀로지에서 실행 — 포트 7708

Intel/AMD x86_64 CPU와 Container Manager를 사용하는 시놀로지 기준입니다. ARM NAS용 이미지는 제공하지 않습니다.

## Compose 파일 하나로 설치

1. [compose.synology.yaml](../compose.synology.yaml)을 내려받습니다. 소스코드·Dockerfile을 NAS에 복사하거나 이미지를 NAS에서 빌드할 필요가 없습니다.
2. `ADMIN_PASSWORD: ""`에 첫 관리자 비밀번호를 입력합니다. 16~256자여야 하며, 공백인 상태로 처음 실행하면 시작하지 않습니다.
3. File Station에서 프로젝트 폴더(예: `/volume1/docker/court-monitor`)를 만듭니다. Container Manager → **프로젝트 → 생성**에서 이 폴더를 선택하고, Compose 내용을 편집기에 넣거나 `docker-compose.yml`로 업로드합니다.
4. 프로젝트를 시작한 뒤 `http://NAS의-IP:7708`에 접속합니다. 로그인 화면에서 설정한 비밀번호를 입력합니다.

이미지는 `imueroid/court-monitor:latest`를 사용합니다. 외부 접속 포트는 **7708**, 컨테이너 내부 포트는 8080입니다. Compose의 `7708:8080`을 유지하면 됩니다.

SSH로 실행하는 경우:

```sh
sudo docker compose -f compose.synology.yaml up -d
```

NAS에 `docker compose` 대신 `docker-compose` 명령이 설치되어 있다면 같은 인수로 `sudo docker-compose -f compose.synology.yaml up -d`를 사용합니다.

## Google Calendar 연결

사건 관리 전용 캘린더를 만들고 서비스 계정에 일정 변경 권한으로 공유하는 절차는 [서버 안내](README.md#2-사건-전용-google-calendar-연결)를 참고하세요.

Compose의 다음 두 항목을 채웁니다. JSON 파일을 별도로 마운트하지 않아도 됩니다.

```yaml
      GOOGLE_CALENDAR_ID: "전용-캘린더-ID"
      GOOGLE_CREDENTIALS_JSON: |
        {
          "type": "service_account",
          "project_id": "본인의-project-id",
          "private_key_id": "본인의-key-id",
          "private_key": "-----BEGIN PRIVATE KEY-----\n본인의-키\n-----END PRIVATE KEY-----\n",
          "client_email": "본인의-서비스계정@본인의-project-id.iam.gserviceaccount.com",
          "client_id": "본인의-client-id",
          "token_uri": "https://oauth2.googleapis.com/token"
        }
```

내려받은 서비스 계정 JSON 원문을 `|` 아래 들여쓰기해서 넣으세요. 예시의 가짜 키를 사용하면 연결되지 않습니다. 캘린더 설정을 비워 두어도 사건 조회와 로그인은 사용할 수 있고, 캘린더 반영은 대기합니다. 실제 키와 암호를 채운 Compose는 개인 NAS에서만 관리하고 공개 저장소에 올리지 않습니다.

## 로그인·DB·이력

- 별도 DB 서버나 DB 컨테이너가 필요하지 않습니다. Python 웹 서버·조회 작업자·SQLite가 **하나의 컨테이너** 안에서 동작합니다.
- 사건 목록, 최근 조회 결과, 작업 예약, 캘린더 전송 상태, 관리자 비밀번호 해시, 로그인 세션을 `/data/cases.sqlite3`에 저장합니다.
- `/data`는 `court_data` Docker named volume에 보관합니다. 이미지 업데이트나 컨테이너 재시작 후에도 데이터와 로그인이 유지됩니다. 프로젝트 이름을 바꾸면 다른 볼륨이 만들어질 수 있으므로 같은 프로젝트를 계속 사용하세요.
- 비밀번호는 임의 salt와 PBKDF2-HMAC-SHA256 600,000회로 해시한 값만 DB에 저장합니다. 세션 토큰도 해시로 저장하며 브라우저에는 HttpOnly·SameSite 쿠키를 사용합니다.
- 로그인 유지 기간은 기본 **90일**입니다. `SESSION_DAYS`로 1~365일 범위에서 조정할 수 있습니다. 쿠키를 지우거나 시크릿 창을 사용하면 다시 로그인해야 합니다.
- 화면 아래의 **관리자 비밀번호 변경**으로 바꾸면 모든 기기의 세션이 해제됩니다. Compose의 초기 암호를 바꿔도 이미 DB에 저장한 비밀번호를 덮어쓰지 않습니다.
- 처음 등록한 암호가 DB에 저장된 이후에는 Compose의 `ADMIN_PASSWORD` 값을 비워도 다시 시작할 수 있습니다. DB 볼륨을 지운 뒤에는 초기 암호를 다시 설정해야 합니다.

## 업데이트와 백업

Container Manager에서 동일 프로젝트의 이미지를 새로 내려받고 컨테이너를 재생성하거나 다음 명령을 사용합니다.

```sh
sudo docker compose -f compose.synology.yaml pull
sudo docker compose -f compose.synology.yaml up -d
```

`down -v`나 Docker 볼륨 삭제는 비밀번호·세션·사건 DB까지 지우므로 일반 업데이트에 사용하지 않습니다. 전체 백업은 서비스를 중지하고 해당 프로젝트의 `court_data` 볼륨 전체를 보관하세요.

HTTPS 역방향 프록시로만 사용할 때는 `COOKIE_SECURE: "true"`로 설정합니다. DSM의 방화벽을 사용하는 환경이면 사용하는 네트워크에서 TCP 7708 접근이 허용되어 있어야 합니다.

## 이미지 빌드

[GitHub Actions](https://github.com/imueRoid/court-monitor/actions)에서 서버 검사, 실제 Linux 컨테이너 로그인·SQLite 세션 재시작·OCR 모델 추론을 검사한 뒤 Docker Hub에 게시합니다. `latest` 외에 `sha-전체커밋SHA` 태그도 남겨 특정 버전으로 고정할 수 있습니다.

공식 안내: [Synology Container Manager 프로젝트](https://kb.synology.com/en-global/DSM/help/ContainerManager/docker_project), [Docker의 GitHub Actions 빌드](https://docs.docker.com/build/ci/github-actions/).
