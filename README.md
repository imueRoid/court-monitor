# Court Monitor — 사건 일정 관리

대한민국 법원 사건정보를 조회하고 기일·선고 결과를 사건 관리 전용 Google Calendar에 반영하는 웹 프로그램입니다. SQLite를 포함한 **단일 Docker 컨테이너**로 실행합니다.

- [시놀로지용 Compose 파일](compose.synology.yaml) — NAS 접속 포트 **7708**
- [시놀로지 설치·로그인·백업 안내](server/SYNOLOGY.md)
- [일반 Docker 서버와 Google Calendar 설정](server/README.md)
- [Windows 데스크톱 프로그램](DESKTOP.md)
- [Docker Hub 이미지](https://hub.docker.com/r/imueroid/court-monitor)

## 시놀로지 빠른 시작

`compose.synology.yaml` 하나를 내려받아 `ADMIN_PASSWORD`에 최초 관리자 비밀번호(16~256자)를 입력하세요. Container Manager의 프로젝트에 Compose 내용을 넣고 시작한 뒤 `http://NAS-IP:7708`에 접속합니다. 소스코드와 Dockerfile은 NAS에 필요하지 않습니다. Intel/AMD x86_64 NAS용입니다.

Google Calendar를 연결하려면 같은 Compose의 `GOOGLE_CALENDAR_ID`와 `GOOGLE_CREDENTIALS_JSON`을 채웁니다. 전용 캘린더를 서비스 계정에 공유하는 절차는 설치 안내에 있습니다. 실제 키와 암호를 넣은 배포 설정은 공개 저장소에 올리지 마세요.

## 기능

- 법원 이름 입력 검색, 사건 등록·가져오기·조회 이력 저장
- 판결 주문, 선고일자, 상소제기 여부, 상소사건번호 표시
- 민사 주문이 없고 선고일자가 있으면 원고승·원고패 등 공식 종국결과 표시
- 차회 기일 등록·변경, 기일 이후 재조회, 선고일 주문 확인 및 캘린더 설명 갱신
- 선고일 기본 30분 간격·하루 최대 8회 확인, 전송 실패 별도 재시도
- 관리자 로그인 화면, 기본 90일 세션 유지, 로그아웃·비밀번호 변경
- 비밀번호·세션 토큰은 해시로, 사건·요약·예약·캘린더 상태는 SQLite에 저장
- 단일 컨테이너와 영구 데이터 볼륨. Redis·MySQL·PostgreSQL 컨테이너 불필요

관리자 비밀번호는 DB가 처음 생성될 때만 초기 설정을 사용합니다. 이후 비밀번호 변경은 웹 화면에서 하며 모든 기기의 기존 세션을 해제합니다. 컨테이너를 다시 시작해도 DB 볼륨이 유지되면 사건과 로그인 세션이 남습니다.

## 소스에서 빌드

```sh
python3 server/prepare.py
docker compose up -d --build
```

이 개발용 Compose의 기본 주소는 `http://127.0.0.1:8080`입니다. 시놀로지 배포에는 위의 `compose.synology.yaml`을 사용합니다.

## 검사와 이미지 게시

```sh
python -m pip install -r requirements-server.txt httpx
python -m unittest discover -s tests -p 'test_server*.py' -v
```

GitHub Actions는 서버·공통 클라이언트를 검사하고 Linux 이미지를 빌드합니다. 실제 컨테이너에서 로그인, SQLite 세션의 재시작 유지와 OCR 모델 추론을 확인한 뒤 Docker Hub의 `latest`와 `sha-커밋SHA` 태그로 게시합니다. 저장소의 Actions secret `DOCKERHUB_TOKEN`에 Docker Hub 쓰기 토큰을 등록해야 게시할 수 있습니다.

실제 사건정보, 원문 응답, 보안문자 이미지, Google 키, DB와 배포용 암호는 저장소·이미지에 포함하지 않습니다. 법원 사이트 구조가 달라지거나 서버가 데이터를 제공하지 않으면 조회 코드 수정이 필요할 수 있습니다.

공식 서비스: [대한민국 법원 나의 사건검색](https://ssgo.scourt.go.kr/ssgo/index.on?cortId=www). [Google Calendar API](https://developers.google.com/workspace/calendar/api/guides/overview). [ddddocr](https://github.com/sml2h3/ddddocr).
