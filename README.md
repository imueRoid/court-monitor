# Court Monitor — 사건 일정 관리

대한민국 법원 사건정보를 조회하고 기일·선고 결과를 본인 Google Calendar에 반영하는 웹 프로그램입니다. 웹 서버·조회 작업자·SQLite가 **하나의 Docker 컨테이너**에서 동작합니다.

- [시놀로지용 Compose](compose.synology.yaml) — **수정 없이 실행**, 접속 포트 **7708**
- [시놀로지 설치 안내](server/SYNOLOGY.md)
- Google 독립 인증 상세 페이지: 설치 후 `http://NAS-IP:7708/guide/google` 또는 관리 화면의 **처음부터 따라 하는 상세 안내**
- [안내 페이지 소스](server/static/google-guide.html) — 준비사항·설정 위임 요청문·단계별 설명·오류 해결
- [일반 Docker 서버 안내](server/README.md), [Windows 프로그램](DESKTOP.md)
- [Windows PC 설치 앱](WINDOWS-PC.md) — 서버 없이 실행, 설정 탭에서 자동 시작·백그라운드 동작 선택
- [Docker Hub 이미지](https://hub.docker.com/r/imueroid/court-monitor)

## 설치

`compose.synology.yaml`을 Container Manager 프로젝트에 그대로 넣고 실행한 뒤 `http://NAS-IP:7708`에 접속합니다. 처음 열리는 화면에서 관리자 비밀번호를 정합니다. YAML에 암호나 Google 키를 입력할 필요가 없습니다. Intel/AMD x86_64 NAS용이며 ARM 이미지는 검증하지 않았습니다.

Google Calendar는 **각 NAS가 독립 인증**합니다. 사용자별 최초 OAuth 앱 등록 후, 웹에서 클라이언트 JSON을 등록하고 **Google 계정 연결**을 누르면 본인 계정의 사건 전용 캘린더를 자동 생성합니다. 공통 인증 서버를 사용하지 않습니다. Google Cloud 설정을 맡길 때 필요한 콜백 URI와 전달용 요청문도 상세 페이지에서 복사할 수 있습니다.

## 기능

- 법원명 입력 검색, 사건 등록·가져오기·조회 이력 DB 저장
- 당사자명·사건번호·법원명·판결 내용·기일 정보 검색, 페이지당 10·20·50건 표시
- 전체 검색 결과를 차회 기일이 빠른 순으로 정렬한 뒤 페이지 나눔, 기일 미확인 사건은 마지막에 표시
- 사건번호를 누르면 바로 아래에 결과 펼치기, 여러 페이지에서 체크한 사건 일괄 삭제
- 판결 주문, 선고일자, 상소제기 여부, 상소사건번호 표시
- 별도 주문이 없고 선고일자가 있으면 공식 JSON의 종국결과 필드 `csUltmtDvsNm` 값을 그대로 표시: 원고승·원고패 같은 문구 목록으로 판별하지 않음
- 차회 기일 등록·변경, 기일 이후 재조회, 선고일 주문 확인 및 캘린더 설명 갱신
- 선고 후 기본 30분 간격·09~19시·하루 최대 8회 확인, 전송 실패 별도 재시도
- 최초 관리자 비밀번호 설정, 기본 90일 로그인, 로그아웃·비밀번호 변경
- 암호·세션은 해시, Google 클라이언트 시크릿·연결 토큰은 NAS 내부 DB에 암호화 저장
- 영구 데이터 볼륨으로 사건·예약·로그인·Google 연결을 재시작 후 복원

Google OAuth 앱이 External + Testing이면 갱신 토큰이 7일 후 만료될 수 있습니다. 장기 운영을 위한 게시·확인 절차는 상세 페이지를 참고하세요. 실제 Google 승인은 각 사용자의 설정 후 진행하며 자동 검사에서는 모의 응답으로 연결 흐름을 검증합니다.

## 개발과 검증

```sh
python -m pip install -r requirements-server.txt httpx
python -m unittest discover -s tests -p 'test_server*.py' -v
```

GitHub Actions는 서버·공통 클라이언트 검사 후 Linux 이미지를 빌드하고 실제 컨테이너의 최초 비밀번호 설정·로그인·재시작 세션·OCR 추론을 확인한 뒤 Docker Hub에 게시합니다. `latest`, `1`, `1.0`, `1.0.<빌드번호>`, `sha-커밋SHA` 태그를 제공합니다. 서버 버전은 빌드마다 자동 증가하며 관리 화면 하단과 `/health`에서 확인합니다. 시놀로지용 이미지는 단일 플랫폼 Docker V2 형식으로 게시하고, Compose는 `pull_policy: always`로 재생성 시 새 이미지를 받습니다. 이미지 게시에는 이 저장소의 Actions secret `DOCKERHUB_TOKEN`을 사용합니다.

실제 사건정보, 원문 응답, 보안문자 이미지, 비밀번호, DB와 Google 인증정보는 GitHub·이미지에 포함하지 않습니다. 공식 사이트 구조나 응답이 바뀌면 조회 코드 수정이 필요할 수 있습니다.
