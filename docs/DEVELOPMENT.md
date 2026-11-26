# 개발과 배포

[저장소 소개](../README.md) · [시놀로지 운영](../server/SYNOLOGY.md) · [Linux Docker 운영](../server/README.md) · [Windows PC](../WINDOWS-PC.md)

## NAS 앱

Rust 앱의 시작점은 [`nas/src/main.rs`](../nas/src/main.rs)입니다. 법원 조회는 `nas/src/court.rs`, 조회 예약과 사건 상태는 `nas/src/domain.rs`와 `nas/src/worker.rs`, SQLite와 검색은 `nas/src/store.rs`에서 처리합니다. Google Calendar 연결과 일정 전송은 `nas/src/google.rs`에서 처리합니다. 웹 UI와 Google 설정 안내는 [`server/static/`](../server/static/)을 수정합니다.

Docker 빌드는 [`Dockerfile`](../Dockerfile)에서 Rust 바이너리와 ONNX 실행 자산을 준비합니다. Rust 의존성은 [`nas/Cargo.lock`](../nas/Cargo.lock), OCR 배포 자산은 [`nas/ocr-assets.requirements`](../nas/ocr-assets.requirements)에서 고정합니다.

## Python과 Windows PC

[`desktop_monitor.py`](../desktop_monitor.py)는 설치형 Windows PC 앱의 시작점입니다. 자동 시작과 트레이 동작을 지원합니다. [`build_windows.py`](../build_windows.py)로 실행 파일을 빌드하고 [`installer/windows.iss`](../installer/windows.iss)로 설치 프로그램을 만듭니다. [`WINDOWS-PC.md`](../WINDOWS-PC.md)는 설치 파일에 포함되며 Windows 앱의 안내 열기에서도 사용됩니다.

[`app.py`](../app.py), [`build_exe.py`](../build_exe.py), [`setup.bat`](../setup.bat), [`run.bat`](../run.bat)는 기존 Windows 단일 사건 조회 도구용입니다. `court_client.py`, `case_summary.py`, `captcha_ocr.py` 등 루트의 Python 모듈은 이 도구, Windows 앱, 호환 코드에서 함께 사용합니다. [단일 사건 조회 도구 안내](windows-single-case.md)

[`server/`](../server/)의 Python 서버 코드는 호환 경로와 회귀 검증에 쓰입니다. 일반 Linux Docker 배포는 `compose.yaml`과 `Dockerfile`로 Rust 서버를 빌드합니다. [Linux Docker 빌드 안내](../server/README.md)

## 자동 검사

활성 Python 가상환경에서 아래 명령을 실행합니다. GitHub Actions는 Rust 1.100.0과 Python 3.12로 검사합니다.

```sh
cargo test --locked --manifest-path nas/Cargo.toml
python -m pip install -r requirements-server.lock.txt httpx
python -m unittest discover -s tests -p 'test_server*.py' -v
python -m unittest discover -s tests -p 'test_client.py' -v
python -m unittest discover -s tests -p 'test_summary.py' -v
python -m unittest discover -s tests -p 'test_ocr.py' -v
```

[`tests/container_smoke.py`](../tests/container_smoke.py)는 Docker 컨테이너의 최초 설정, 로그인, 재시작, 버전 표시와 OCR을 확인합니다. [`tests/test_nas_runtime.py`](../tests/test_nas_runtime.py)는 기존 데이터베이스에서 앱 업데이트와 시작 실패 시 복귀를 확인합니다. OAuth와 사건 처리는 테스트 응답과 합성 데이터를 사용합니다. [GitHub Actions 검사 이력](https://github.com/imueRoid/court-monitor/actions)

## 배포 파일

[배포 워크플로](../.github/workflows/docker-publish.yml)는 앱을 검증한 다음 [`scripts/package_update.py`](../scripts/package_update.py)로 NAS 앱 ZIP과 매니페스트를 만듭니다. 호환 앱 업데이트에는 Rust 실행 파일과 웹 정적 파일이 들어가며, 사용 중인 NAS는 [GitHub Releases](https://github.com/imueRoid/court-monitor/releases/latest)에서 매니페스트와 패키지를 받습니다.

Docker 실행 환경이 바뀌면 `imueroid/court-monitor`에 단일 `linux/amd64` 이미지를 Docker V2 매니페스트로 게시합니다. 태그는 `latest`, `1`, `1.0`, `1.0.<빌드번호>`, `sha-<커밋SHA>`입니다. 이미지 게시 자격 증명은 GitHub Actions의 `DOCKERHUB_TOKEN` secret으로 관리하고 [레지스트리 검증 스크립트](../tests/verify_published_image.py)로 게시 결과를 확인합니다.

SQLite 스키마를 바꿀 때는 이전 앱으로 복귀해도 데이터가 읽히도록 호환성을 유지합니다. 개발 검증에는 가상 사건과 테스트 계정을 사용하며, 실제 사건 응답, DB, Google 인증정보와 암호는 개인 데이터 볼륨이나 비공개 환경에서 관리합니다.
