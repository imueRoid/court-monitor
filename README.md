# Court Monitor — 사건 일정 관리

대한민국 법원 사건정보를 조회하고 기일·선고 결과를 본인 Google Calendar에 반영하는 웹 프로그램입니다. NAS의 Rust 웹 서버, 법원 조회, ONNX 인식, SQLite와 Google 동기화가 **하나의 Docker 컨테이너**에서 동작합니다.

- [시놀로지용 Compose](compose.synology.yaml) — **수정 없이 실행**, 접속 포트 **7708**
- [시놀로지 설치 안내](server/SYNOLOGY.md)
- Google Calendar 단계별 안내는 설치 후 **설정 → Google Calendar 연결 → 처음부터 따라 하는 상세 안내** 또는 NAS의 `/guide/google`에서 엽니다. [시놀로지 안내의 Google 연결 절차](server/SYNOLOGY.md#google-독립-인증)
- [일반 Docker 서버 안내](server/README.md), [Windows 프로그램](DESKTOP.md)
- [Windows PC 설치 앱](WINDOWS-PC.md) — 서버 없이 실행, 설정 탭에서 자동 시작·백그라운드 동작 선택
- [Docker Hub 이미지](https://hub.docker.com/r/imueroid/court-monitor)

## 설치

`compose.synology.yaml`을 Container Manager 프로젝트에 그대로 넣고 실행한 뒤 `http://NAS-IP:7708`에 접속합니다. 처음 열리는 화면에서 관리자 비밀번호를 정합니다. YAML에 암호나 Google 키를 입력할 필요가 없습니다. Intel/AMD x86_64 NAS용이며 ARM 이미지는 검증하지 않았습니다.

Google Calendar 연동을 사용할 설치자는 본인 Google Cloud 프로젝트에서 웹 OAuth 클라이언트를 만들고 NAS에 JSON을 등록합니다. **Google 계정 연결**을 눌러 승인하면 본인 계정에 사건 전용 캘린더가 만들어집니다. 앱 안내에서 HTTPS·localhost 설정, 콜백 복사, 콘솔 메뉴와 오류별 해결 방법을 확인할 수 있습니다. [Google Calendar API 콘솔](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com) · [Google Auth Platform 도움말](https://support.google.com/cloud/answer/15544987)

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
cargo test --locked --manifest-path nas/Cargo.toml
python -m pip install -r requirements-server.txt httpx
python -m unittest discover -s tests -p 'test_server*.py' -v
```

GitHub Actions는 서버·공통 클라이언트 검사와 컨테이너의 최초 비밀번호 설정·재시작 세션·OCR 추론을 검증합니다. 앱 버전은 빌드마다 자동 증가하며 관리 화면 하단과 `/health`에서 확인합니다. Docker Hub에 게시하는 시놀로지용 이미지는 단일 플랫폼 Docker V2 형식이며 `latest`, `1`, `1.0`, `1.0.<빌드번호>`, `sha-커밋SHA` 태그를 제공합니다. 이미지 게시에는 이 저장소의 Actions secret `DOCKERHUB_TOKEN`을 사용합니다.

NAS의 Rust 웹 서버는 호환되는 앱 버전을 재부팅 시 또는 관리 화면에서 적용합니다. 일반 앱 변경은 GitHub Releases에 게시하며, Docker Hub 이미지는 기동 코드나 의존성 등 실행 환경이 바뀔 때 갱신합니다. 최초 적용과 이미지 갱신 절차는 [시놀로지 안내](server/SYNOLOGY.md#업데이트와-백업)를 참고하세요. 앱 패키지에서 SQLite 스키마를 바꿀 때는 이전 앱으로 복귀할 수 있도록 하위 호환성을 유지합니다.

실제 사건정보, 원문 응답, 보안문자 이미지, 비밀번호, DB와 Google 인증정보는 GitHub·이미지에 포함하지 않습니다. 공식 사이트 구조나 응답이 바뀌면 조회 코드 수정이 필요할 수 있습니다.


사건번호를 눌러 상세를 펼치면, 판결 주문·종국결과가 아직 확인되지 않은 사건의 **지난 기일 당일부터 다음 기일 이전까지 진행된 모든 소송행위**를 최신순으로 보여줍니다. 서류 제출·접수·송달 등 종류에 따른 필터나 건수 제한은 없습니다. 지난 기일이 없으면 최초 진행 내역부터 표시합니다. 다음 기일이 없으면 현재까지 표시하고, 다음 기일이 있으면 그 당일은 제외합니다. 시간 정보가 없는 진행 내역은 날짜로 구간을 구분합니다. 날짜·내용·결과는 법원의 진행내용 응답에서 가져오며, 원문 JSON과 내부 필드명은 표시하지 않습니다.

상세를 열 때 저장된 내용을 먼저 표시하고 최신 조회를 요청합니다. 최근 1분 내 조회한 정보는 재사용하고 이미 대기·진행 중인 조회는 중복 요청하지 않습니다. 조회 중에도 상세 화면이 유지되며 완료 후 자동 갱신됩니다. 진행 내역과 조회 대기는 SQLite에 저장되어 재시작 후에도 유지됩니다.


선고기일이 잡혀 있어도 선고내용(판결 주문·종국결과)을 확인하기 전까지 진행정보를 계속 표시합니다. 취소·변경·연기된 기일은 유효한 기일에서 제외합니다. 선고내용이 없는 상태에서 기존 선고일보다 뒤에 공판·변론 등이 잡힌 경우에도 기존 선고기일을 무효로 처리하며, 저장된 과거 선고일 때문에 주문 확인 일정이 계속 유지되지 않도록 재계산합니다. 이후 새 선고기일이 지정되면 새 날짜를 기준으로 확인합니다.
\n취소·변경된 과거 선고 일정이 Google Calendar에 남아 있는 경우, 해당 일정에 [취소·변경] 표시를 반영합니다. 새 기일로 이동 가능한 미래 일정은 기존 동기화 방식대로 새 기일로 갱신됩니다.\n
