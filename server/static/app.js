"use strict";
const state = {csrf:"", courts:[], cases:[], selected:null, checked:new Set(), deleting:false, config:null, calendarConfigured:null, scope:"ongoing", scopeLoading:true, scopeTotal:0, query:"", page:1, pageSize:10, pages:1, total:0, metrics:{}, request:0, detailRequests:new Map(), detailErrors:new Map()};
const $ = id => document.getElementById(id);
const detailCard = $("detail-card");
const ongoingTab = $("dashboard-tab"), finishedTab = $("finished-tab"), settingsTab = $("settings-tab");
const dashboardTabs = [ongoingTab, finishedTab, settingsTab];
let activeTab = ongoingTab;
let searchTimer;

function setCalendarConfigured(configured) {
  state.calendarConfigured = configured;
  placeGoogleCard();
}

function placeGoogleCard() {
  if (state.calendarConfigured === null) return;
  const onDashboard = activeTab === ongoingTab && !state.calendarConfigured;
  const destination = $(onDashboard ? "google-setup" : "google-settings");
  const card = $("google-card");
  if (card.parentElement === destination) return;
  const restoreFocus = card.contains(document.activeElement);
  destination.append(card);
  if (restoreFocus) activeTab.focus();
}

function selectDashboardTab(tab) {
  activeTab = tab;
  for (const item of dashboardTabs) {
    const selected = item === tab;
    item.setAttribute("aria-selected", String(selected));
    item.tabIndex = selected ? 0 : -1;
    $(item.getAttribute("aria-controls")).hidden = !selected;
  }
  placeGoogleCard();
  if (tab === settingsTab) return;
  const finished = tab === finishedTab;
  const scope = finished ? "finished" : "ongoing";
  if (scope === state.scope) return;
  clearTimeout(searchTimer);
  state.checked.clear();
  Object.assign(state,{scope,scopeLoading:true,scopeTotal:0,query:"",cases:[],selected:null,page:1,pages:1,total:0});
  $("case-search").value = "";
  $("case-list-title").textContent = finished ? "종료된 사건" : "진행 중인 사건";
  $(finished ? "finished-panel" : "ongoing-cases").append($("case-list"));
  renderCases();
  if(state.config)reload().catch(error=>notice(error.message,true));
}

for (const tab of dashboardTabs) {
  tab.addEventListener("click", () => selectDashboardTab(tab));
  tab.addEventListener("keydown", event => {
    const index = dashboardTabs.indexOf(tab);
    const destination = {
      ArrowLeft: dashboardTabs.at(index - 1),
      ArrowRight: dashboardTabs[(index + 1) % dashboardTabs.length],
      Home: ongoingTab, End: settingsTab,
    }[event.key];
    if (!destination) return;
    event.preventDefault();
    selectDashboardTab(destination);
    destination.focus();
  });
}
const googleResult = new URLSearchParams(location.search).get("google");
if (["error", "cancelled"].includes(googleResult)) selectDashboardTab(settingsTab);

const node = (tag,text,cls) => {const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;};
const fmt = stamp => stamp ? new Intl.DateTimeFormat("ko-KR",{timeZone:state.config?.timezone||"Asia/Seoul",dateStyle:"short",timeStyle:"short"}).format(new Date(stamp*1000)) : "—";
const labels={
  pending:"조회 대기", monitoring:"변론 진행중", awaiting_verdict:"변론 종결됨",
  checking_order:"주문 확인중", decided:"주문·종국결과 확인",
};
function judgmentStage(row) {
  if (row.judgment_stage) return row.judgment_stage;
  if (row.state === "done") return "decided";
  if (row.state === "awaiting_order") return "checking_order";
  return "before_verdict";
}
function caseBadge(row) {
  if (!row.active) return node("span", "일시중지", "badge pause");
  const stage = judgmentStage(row);
  const label = stage === "before_verdict" ? (row.state === "pending" ? "pending" : "monitoring") : stage;
  const waiting = stage === "awaiting_verdict" || stage === "checking_order";
  return node("span", labels[label], waiting ? "badge wait" : "badge");
}
function notice(text,warn=false){$("notice").textContent=text;$("notice").classList.toggle("warn",warn);}
async function api(path,body){const options={credentials:"same-origin",headers:{}};if(body!==undefined){options.method="POST";options.headers={"Content-Type":"application/json","X-CSRF-Token":state.csrf};options.body=JSON.stringify(body);}const response=await fetch("/api"+path,options);const data=await response.json();if(response.status===401)location.assign("/login");if(!response.ok){let error=data.detail;if(Array.isArray(error))error=error.map(x=>x.msg).join(" / ");throw new Error(typeof error==="string"?error:"서버 요청을 처리하지 못했습니다.");}return data;}
function button(text,run){const el=node("button",text,"secondary");el.addEventListener("click",async()=>{el.disabled=true;try{await run();await reload();}catch(error){notice(error.message,true);}finally{el.disabled=false;}});return el;}
function hearingText(hearing) {
  return [hearing.day, hearing.clock, hearing.kind].filter(Boolean).join(" ");
}
function renderCases(){
  const table=$("cases");
  const focusedCase=document.activeElement?.classList.contains("case-link")?document.activeElement.dataset.caseId:null;
  detailCard.remove();
  table.replaceChildren();
  if(!state.cases.some(row=>row.id===state.selected))state.selected=null;
  $("case-list").setAttribute("aria-busy",String(state.scopeLoading));
  $("empty").hidden=state.cases.length>0;
  $("empty").textContent=state.scopeLoading?"사건을 불러오는 중입니다.":state.query?"검색 결과가 없습니다. 다른 검색어를 입력해 주세요.":
    state.scope==="finished"?"종료된 사건이 없습니다.":"진행 중인 사건이 없습니다.";
  $("total").textContent=state.metrics.ongoing||0;
  for(const key of ["monitoring","waiting","pending"])$(key).textContent=state.metrics[key]||0;
  const start=state.total?(state.page-1)*state.pageSize+1:0;
  $("case-count").textContent=state.scopeLoading?"":`전체 ${state.scopeTotal}건 · 검색 결과 ${state.total}건 · ${start}–${Math.min(state.page*state.pageSize,state.total)}건 표시`;
  $("page-info").textContent=`${state.page} / ${state.pages} 페이지`;
  $("previous-page").disabled=state.page<=1;
  $("next-page").disabled=state.page>=state.pages;
  renderSelection();
  for(const row of state.cases){
    const expanded=state.selected===row.id;
    const tr=node("tr",undefined,expanded?"case-expanded":"");
    const checkCell=node("td");
    const check=node("input",undefined,"case-checkbox");
    check.type="checkbox";check.checked=state.checked.has(row.id);check.dataset.caseId=row.id;
    check.disabled=state.deleting;
    check.setAttribute("aria-label",`${row.court} ${row.case_number} ${row.party} 선택`);
    check.addEventListener("change",()=>{if(check.checked)state.checked.add(row.id);else state.checked.delete(row.id);renderSelection();});
    checkCell.append(check);
    const caseCell=node("td");
    const link=node("button",row.case_number,"case-link");
    link.dataset.caseId=row.id;
    link.setAttribute("aria-expanded",String(expanded));
    if(expanded)link.setAttribute("aria-controls","detail-card");
    link.addEventListener("click",()=>{state.selected=expanded?null:row.id;renderCases();if(!expanded)refreshDetail(row);});
    caseCell.append(link,node("div",row.court,"muted"));
    caseCell.append(node("div",row.case_category||"사건 종류 미확인","muted"));
    const status=node("td");
    status.append(caseBadge(row));
    if(row.last_error)status.append(node("div","조회 오류 · 상세 확인","error"));
    const hearing=row.next_hearing;
    const day=node("td",hearing?hearingText(hearing):"확인된 차회 기일 없음");
    const checked=node("td",fmt(row.last_checked));
    checked.append(node("div",`다음: ${row.active?fmt(row.next_check):"일시중지"}`,"muted"));
    const sync=node("td",row.calendar_pending?`${row.calendar_pending}건 대기`:"반영 완료 / 일정 없음");
    if(row.calendar_error)sync.append(node("div",row.calendar_error,"error"));
    const actions=node("td");
    const group=node("div",undefined,"row-actions");
    group.append(button("지금 확인",()=>api(`/cases/${row.id}/refresh`,{})),button(row.active?"중지":"재개",()=>api(`/cases/${row.id}/active`,{active:!row.active})));
    actions.append(group);
    tr.append(checkCell,caseCell,node("td",row.party),status,day,checked,sync,actions);
    table.append(tr);
    if(expanded){
      const detailRow=node("tr",undefined,"case-detail-row");
      const cell=node("td");cell.colSpan=8;
      cell.append(detailCard);detailRow.append(cell);table.append(detailRow);
      renderDetail(row);
    }
    if(focusedCase===row.id)link.focus({preventScroll:true});
  }
}
function renderSelection(){
  const count=state.checked.size;
  $("selection-count").textContent=`${count}건 선택 (다른 페이지의 선택 포함)`;
  $("delete-selected").disabled=!count||state.deleting;
  $("clear-selection").disabled=!count||state.deleting;
  const pageCount=state.cases.filter(row=>state.checked.has(row.id)).length;
  $("select-page").checked=state.cases.length>0&&pageCount===state.cases.length;
  $("select-page").indeterminate=pageCount>0&&pageCount<state.cases.length;
  $("select-page").disabled=!state.cases.length||state.deleting;
}
$("select-page").addEventListener("change",()=>{
  for(const row of state.cases){if($("select-page").checked)state.checked.add(row.id);else state.checked.delete(row.id);}
  renderCases();
});
$("clear-selection").addEventListener("click",()=>{state.checked.clear();renderCases();});
$("delete-selected").addEventListener("click",async()=>{
  const ids=[...state.checked];
  if(!ids.length||state.deleting)return;
  if(!confirm(`선택한 ${ids.length}건의 사건과 저장된 조회 내용·자동 확인 일정을 삭제할까요?\n삭제 후 복구할 수 없습니다. Google Calendar에 이미 등록된 일정은 유지됩니다.`))return;
  state.deleting=true;renderCases();
  try{
    const result=await api("/cases/delete",{ids});
    ids.forEach(id=>state.checked.delete(id));
    if(ids.includes(state.selected))state.selected=null;
    await reload();
    notice(`${result.deleted}건의 사건을 삭제했습니다.`);
  }catch(error){notice(error.message,true);}
  finally{state.deleting=false;renderSelection();for(const checkbox of document.querySelectorAll("#cases .case-checkbox"))checkbox.disabled=false;}
});
function renderDetail(row) {
  renderProgress(row);
  detailCard.hidden = false;
  $("detail-title").textContent = `${row.court} ${row.case_number} · ${row.party}`;
  const stage = judgmentStage(row);
  const pending = stage === "decided" && row.refresh_pending ? " · 상소·확정 정보 조회 중" : "";
  $("detail-time").textContent = `최근 확인 ${fmt(row.last_checked)}${pending}`;
  const detail = $("detail");
  detail.replaceChildren(node("dt", "사건 종류"), node("dd", row.case_category || "확인되지 않음"));
  if (stage === "checking_order" || stage === "decided") {
    const fields = ["판결 주문", "선고일자"];
    if (stage === "decided") fields.push("상소제기 여부", "상소사건번호", "확정 여부");
    for (const key of fields) {
      detail.append(node("dt", key), node("dd", row.observed?.summary?.[key] || "확인되지 않음"));
    }
  } else {
    const next = row.next_hearing;
    const label = next?.kind === "변론" ? "차회 변론기일" : next?.kind === "선고" ? "차회 선고기일" : "차회 기일";
    const description = next ? [hearingText(next), next.location].filter(Boolean).join(" · ") : "확인된 차회 기일 없음";
    detail.append(node("dt", label), node("dd", description));
  }
  const requestError = stage === "decided" ? state.detailErrors.get(row.id) : "";
  $("detail-error").textContent = [requestError, row.last_error, row.calendar_error, ...(row.observed?.warnings || [])].filter(Boolean).join("\n");
}
function renderProgress(row){
  $("progress-panel").hidden=!row.show_progress;
  if(!row.show_progress)return;
  const interval=row.progress_interval||{};
  const hearing=h=>h?hearingText(h):"미정";
  $("progress-range").textContent=(interval.previous?"지난 기일: "+hearing(interval.previous)+"부터":"최초 진행 내역부터")+" → "+(interval.next?"다음 기일: "+hearing(interval.next)+" 이전 (다음 기일 당일 제외)":"현재까지 ("+interval.through+")");
  if(row.observed?.cancelled_verdict_days?.length)$("progress-range").textContent+=" · 취소·변경되었거나 이후 공판·변론이 확인된 기존 선고기일은 제외했습니다.";
  const entries=interval.entries||[];
  $("progress-status").textContent=state.detailErrors.get(row.id)||(row.refresh_pending?"최신 진행 내역을 조회하고 있습니다. 저장된 내역을 먼저 표시합니다.":!("progress" in (row.observed||{}))?"진행 내역을 아직 불러오지 못했습니다.":entries.length?entries.length+"건 · 최신순":"이 구간에 확인된 진행 내역이 없습니다.");
  $("progress-entries").replaceChildren();
  for(const entry of entries){
    const item=node("li");
    item.append(node("time",entry.day),node("div",entry.content||"진행 결과"));
    if(entry.result)item.append(node("div","결과: "+entry.result,"progress-result"));
    $("progress-entries").append(item);
  }
}
async function refreshDetail(row){
  const previous=state.detailRequests.get(row.id)||0;
  if(Date.now()-previous<60000)return;
  state.detailRequests.set(row.id,Date.now());
  state.detailErrors.delete(row.id);
  try{await api("/cases/"+row.id+"/detail-refresh",{});await reload();}
  catch(error){state.detailRequests.delete(row.id);state.detailErrors.set(row.id,"최신 조회 요청 실패: "+error.message);if(state.selected===row.id)renderDetail(row);}
}
async function reload(){
  const request=++state.request;
  const params=new URLSearchParams({scope:state.scope,q:state.query,page:state.page,page_size:state.pageSize});
  let data,logData;
  try{[data,logData]=await Promise.all([api(`/cases?${params}`),api("/logs")]);}
  catch(error){
    if(request!==state.request)return;
    state.scopeLoading=false;
    renderCases();
    throw error;
  }
  if(request!==state.request)return;
  Object.assign(state,{scopeLoading:false,scopeTotal:data.scope_total,cases:data.cases,page:data.page,pages:data.pages,total:data.total,metrics:data.metrics});
  renderCases();
  $("logs").replaceChildren();
  for(const log of logData.logs){const item=node("li");item.append(node("time",fmt(log.at)),node("span",log.message,log.level==="error"?"error":""));$("logs").append(item);}
  if (data.calendar_configured !== state.calendarConfigured) {
    const google = await api("/google/status").catch(() => null);
    if (request !== state.request) return;
    if (google) renderGoogle(google, data.calendar_configured);
    else setCalendarConfigured(data.calendar_configured);
  }
  if(!data.calendar_configured)notice("Google Calendar가 아직 연결되지 않았습니다. 계정을 연결하면 대기 중인 일정을 반영합니다.",true);
  else notice(`자동 확인 중 · 선고일 ${state.config.watch_hours[0]}–${state.config.watch_hours[1]}시, ${state.config.verdict_retry_minutes}분 간격 · 하루 최대 ${state.config.verdict_checks_per_day}회`);
}
function searchCases(){clearTimeout(searchTimer);state.query=$("case-search").value.trim();state.page=1;state.selected=null;reload().catch(error=>notice(error.message,true));}
$("search-form").addEventListener("submit",event=>{event.preventDefault();searchCases();});
$("case-search").addEventListener("input",event=>{clearTimeout(searchTimer);if(!event.isComposing)searchTimer=setTimeout(searchCases,300);});
$("case-search").addEventListener("compositionend",()=>{clearTimeout(searchTimer);searchTimer=setTimeout(searchCases,300);});
$("page-size").addEventListener("change",()=>{state.pageSize=Number($("page-size").value);state.page=1;state.selected=null;reload().catch(error=>notice(error.message,true));});
for(const [id,step] of [["previous-page",-1],["next-page",1]])$(id).addEventListener("click",()=>{state.page=Math.max(1,Math.min(state.pages,state.page+step));state.selected=null;reload().catch(error=>notice(error.message,true));});
$("case-form").addEventListener("submit",async event=>{event.preventDefault();$("add").disabled=true;try{await api("/cases",{court:$("court").value,case_number:$("case-number").value,party:$("party").value});$("case-number").value="";$("party").value="";await reload();}catch(error){notice(error.message,true);}finally{$("add").disabled=false;}});
$("reload").addEventListener("click",()=>reload().catch(error=>notice(error.message,true)));
$("court").addEventListener("input",()=>{const query=$("court").value.replace(/\s/g,"");const matches=state.courts.filter(c=>c.replace(/\s/g,"").includes(query));$("suggestions").replaceChildren();for(const court of matches.slice(0,100)){const option=node("button",court);option.type="button";option.addEventListener("click",()=>{$("court").value=court;$("suggestions").hidden=true;});$("suggestions").append(option);}$("suggestions").hidden=!matches.length;});
document.addEventListener("click",event=>{if(!event.target.closest(".court-field"))$("suggestions").hidden=true;});
$("import").addEventListener("click",()=>$("import-file").click());
$("import-file").addEventListener("change",async()=>{try{const file=$("import-file").files[0];if(!file)return;const data=JSON.parse(await file.text());const records=Array.isArray(data.cases)?data.cases:[data];await api("/cases/import",{cases:records.map(r=>r.query||r)});await reload();}catch(error){notice(error.message,true);}finally{$("import-file").value="";}});
$("export").addEventListener("click",async()=>{try{const data=await api("/export");const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:"application/json"}));const link=node("a");link.href=url;link.download="court-cases.json";link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(error){notice(error.message,true);}});
$("logout").addEventListener("click",async()=>{try{await api("/logout",{});location.assign("/login");}catch(error){notice(error.message,true);}});
$("password-form").addEventListener("submit",async event=>{event.preventDefault();$("password-message").textContent="";if($("new-password").value!==$("confirm-password").value){$("password-message").textContent="새 비밀번호 확인이 일치하지 않습니다.";return;}$("change-password").disabled=true;try{await api("/password",{current:$("current-password").value,new:$("new-password").value});location.assign("/login?changed=1");}catch(error){$("password-message").textContent=error.message;}finally{$("change-password").disabled=false;}});
(async()=>{try{const config=await api("/bootstrap");state.config=config;state.csrf=config.csrf_token;setCalendarConfigured(config.calendar_configured);const build=config.build;$("build-version").textContent=build?("버전 "+build.version+" · "+build.revision.slice(0,7)):"버전 정보 없음";window.dispatchEvent(new CustomEvent("court-ready",{detail:config.google}));state.courts=config.courts;$("session-info").textContent=`이 기기의 로그인 유지 기간: ${config.session_days}일. 암호와 세션은 서버 DB에서 관리합니다.`;await reload();setInterval(()=>reload().catch(error=>notice(error.message,true)),10000);if(!state.courts.length)setTimeout(async()=>{try{state.courts=(await api("/bootstrap")).courts;}catch{}},10000);}catch(error){notice(error.message,true);}})();
