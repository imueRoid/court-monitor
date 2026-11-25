"use strict";
const state = {csrf:"", courts:[], cases:[], selected:null, checked:new Set(), deleting:false, config:null, query:"", page:1, pageSize:10, pages:1, total:0, metrics:{}, request:0};
const $ = id => document.getElementById(id);
const detailCard = $("detail-card");
const node = (tag,text,cls) => {const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;};
const fmt = stamp => stamp ? new Intl.DateTimeFormat("ko-KR",{timeZone:state.config?.timezone||"Asia/Seoul",dateStyle:"short",timeStyle:"short"}).format(new Date(stamp*1000)) : "—";
const labels={pending:"조회 대기",monitoring:"기일 확인 중",awaiting_order:"주문 확인 대기",done:"주문·종국결과 확인"};
function notice(text,warn=false){$("notice").textContent=text;$("notice").classList.toggle("warn",warn);}
async function api(path,body){const options={credentials:"same-origin",headers:{}};if(body!==undefined){options.method="POST";options.headers={"Content-Type":"application/json","X-CSRF-Token":state.csrf};options.body=JSON.stringify(body);}const response=await fetch("/api"+path,options);const data=await response.json();if(response.status===401)location.assign("/login");if(!response.ok){let error=data.detail;if(Array.isArray(error))error=error.map(x=>x.msg).join(" / ");throw new Error(typeof error==="string"?error:"서버 요청을 처리하지 못했습니다.");}return data;}
function button(text,run){const el=node("button",text,"secondary");el.addEventListener("click",async()=>{el.disabled=true;try{await run();await reload();}catch(error){notice(error.message,true);}finally{el.disabled=false;}});return el;}
function nextHearing(row){return row.next_hearing;}
function renderCases(){
  const table=$("cases");
  const focusedCase=document.activeElement?.classList.contains("case-link")?document.activeElement.dataset.caseId:null;
  detailCard.remove();
  table.replaceChildren();
  if(!state.cases.some(row=>row.id===state.selected))state.selected=null;
  $("empty").hidden=state.cases.length>0;
  $("empty").textContent=state.query?"검색 결과가 없습니다. 다른 검색어를 입력해 주세요.":"첫 사건을 등록해 주세요.";
  for(const key of ["total","monitoring","waiting","pending"])$(key).textContent=state.metrics[key]||0;
  const start=state.total?(state.page-1)*state.pageSize+1:0;
  $("case-count").textContent=`전체 ${state.metrics.total||0}건 · 검색 결과 ${state.total}건 · ${start}–${Math.min(state.page*state.pageSize,state.total)}건 표시`;
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
    link.addEventListener("click",()=>{state.selected=expanded?null:row.id;renderCases();});
    caseCell.append(link,node("div",row.court,"muted"));
    const status=node("td");
    status.append(node("span",row.active?(labels[row.state]||row.state):"일시중지",`badge ${row.state==="awaiting_order"?"wait":""} ${row.active?"":"pause"}`));
    if(row.last_error)status.append(node("div","조회 오류 · 상세 확인","error"));
    const hearing=nextHearing(row);
    const day=node("td",hearing?`${hearing.day} ${hearing.clock} ${hearing.kind}`:"확인된 차회 기일 없음");
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
function renderDetail(row){detailCard.hidden=false;$("detail-title").textContent=`${row.court} ${row.case_number} · ${row.party}`;$("detail-time").textContent=`최근 확인 ${fmt(row.last_checked)}`;$("detail").replaceChildren();for(const key of ["판결 주문","선고일자","상소제기 여부","상소사건번호"]){$("detail").append(node("dt",key),node("dd",row.observed?.summary?.[key]||"확인되지 않음"));}$("detail-error").textContent=[row.last_error,row.calendar_error,...(row.observed?.warnings||[])].filter(Boolean).join("\n");}
async function reload(){
  const request=++state.request;
  const params=new URLSearchParams({q:state.query,page:state.page,page_size:state.pageSize});
  const [data,logData]=await Promise.all([api(`/cases?${params}`),api("/logs")]);
  if(request!==state.request)return;
  Object.assign(state,{cases:data.cases,page:data.page,pages:data.pages,total:data.total,metrics:data.metrics});
  renderCases();
  $("logs").replaceChildren();
  for(const log of logData.logs){const item=node("li");item.append(node("time",fmt(log.at)),node("span",log.message,log.level==="error"?"error":""));$("logs").append(item);}
  if(!data.calendar_configured)notice("Google Calendar가 아직 연결되지 않았습니다. 위 연결 화면에서 계정을 연결하면 대기 중인 일정을 반영합니다.",true);
  else notice(`자동 확인 중 · 선고일 ${state.config.watch_hours[0]}–${state.config.watch_hours[1]}시, ${state.config.verdict_retry_minutes}분 간격 · 하루 최대 ${state.config.verdict_checks_per_day}회`);
}
let searchTimer;
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
(async()=>{try{const config=await api("/bootstrap");state.config=config;state.csrf=config.csrf_token;const build=config.build;$("build-version").textContent=build?("버전 "+build.version+" · "+build.revision.slice(0,7)):"버전 정보 없음";window.dispatchEvent(new CustomEvent("court-ready",{detail:config.google}));state.courts=config.courts;$("session-info").textContent=`이 기기의 로그인 유지 기간: ${config.session_days}일. 암호와 세션은 서버 DB에서 관리합니다.`;await reload();setInterval(()=>reload().catch(error=>notice(error.message,true)),10000);if(!state.courts.length)setTimeout(async()=>{try{state.courts=(await api("/bootstrap")).courts;}catch{}},10000);}catch(error){notice(error.message,true);}})();
