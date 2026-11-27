(() => {
  const banner = document.getElementById("update-banner");
  const message = document.getElementById("update-message");
  const apply = document.getElementById("apply-update");
  const guide = document.getElementById("image-update-guide");
  let latest;
  let updating = false;
  let checking = false;

  async function check() {
    if (checking || updating) return;
    checking = true;
    try {
      const update = await api("/update");
      if (updating || update.status === "unavailable") return;
      latest = update.latest;
      if (update.status === "available") {
        message.textContent = `새 버전 ${latest}을 적용할 수 있습니다.`;
      } else if (update.status === "failed") {
        message.textContent = `버전 ${latest} 시작에 실패해 이전 버전으로 돌아갔습니다. 다시 시도할 수 있습니다.`;
      } else if (update.status === "image_required") {
        message.textContent = `Docker 이미지 업데이트 필요 · 새 버전 ${latest}을 사용하려면 기존 프로젝트에서 이미지를 갱신한 뒤 같은 데이터 볼륨으로 컨테이너를 재생성하세요.`;
      }
      const imageRequired = update.status === "image_required";
      guide.hidden = !imageRequired;
      apply.hidden = !["available", "failed"].includes(update.status);
      banner.classList.toggle("warn", imageRequired);
      banner.hidden = apply.hidden && guide.hidden;
    } finally {
      checking = false;
    }
  }

  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  async function waitForVersion(version) {
    const deadline = Date.now() + 100_000;
    while (Date.now() < deadline) {
      await pause(1500);
      try {
        const response = await fetch("/health", {
          cache: "no-store", signal: AbortSignal.timeout(3000)
        });
        if (response.ok && (await response.json()).version === version) return true;
      } catch {
        // The service is briefly unavailable while its processes restart.
      }
    }
    return false;
  }

  apply.addEventListener("click", async () => {
    if (!latest || !state.csrf || updating || apply.hidden) return;
    updating = true;
    apply.disabled = true;
    try {
      message.textContent = `버전 ${latest}을 내려받아 검증하고 있습니다.`;
      const update = await api("/update", {version: latest});
      message.textContent = `버전 ${update.version} 적용 중 · 앱이 잠시 다시 시작됩니다.`;
      if (await waitForVersion(update.version)) {
        location.reload();
        return;
      }
      message.textContent = "새 버전을 확인하지 못했습니다. 페이지를 새로고침해 적용 상태를 확인하세요.";
    } catch (error) {
      message.textContent = error.message;
      if (error.status === 409) {
        updating = false;
        await check().catch(() => {});
      }
    } finally {
      updating = false;
      apply.disabled = false;
    }
  });

  apply.disabled = !state.csrf;
  window.addEventListener("court-ready", () => { apply.disabled = updating; }, {once: true});
  check().catch(() => {});
  setInterval(() => check().catch(() => {}), 300_000);
})();
