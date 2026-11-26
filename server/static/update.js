(() => {
  const banner = document.getElementById("update-banner");
  const message = document.getElementById("update-message");
  const apply = document.getElementById("apply-update");
  let latest;
  let updating = false;

  async function check() {
    if (updating) return;
    const update = await api("/update");
    if (updating) return;
    latest = update.latest;
    if (update.status === "available") {
      message.textContent = `새 버전 ${latest}을 적용할 수 있습니다.`;
    } else if (update.status === "failed") {
      message.textContent = `버전 ${latest} 시작에 실패해 이전 버전으로 돌아갔습니다. 다시 시도할 수 있습니다.`;
    } else if (update.status === "image_required") {
      message.textContent = `새 버전 ${latest}을 사용하려면 Docker 이미지를 업데이트해야 합니다.`;
    }
    apply.hidden = !["available", "failed"].includes(update.status);
    banner.hidden = apply.hidden && update.status !== "image_required";
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
    if (!latest || updating) return;
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
    } finally {
      updating = false;
      apply.disabled = false;
    }
  });

  window.addEventListener("court-ready", () => {
    check().catch(() => {});
    setInterval(() => check().catch(() => {}), 300_000);
  }, {once: true});
})();
