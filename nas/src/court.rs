use crate::domain::{self, Cancelled, Hearing, Observation, Progress, UNKNOWN};
use anyhow::{bail, ensure, Context, Result};
use base64::{engine::general_purpose::STANDARD, Engine};
use chrono::{Datelike, Local, Utc};
use regex::Regex;
use reqwest::{redirect, Client, Url};
use serde_json::{json, Map, Value};
use std::{
    collections::{BTreeMap, BTreeSet, HashMap, HashSet},
    sync::LazyLock,
    time::Duration,
};
use unicode_normalization::UnicodeNormalization;

const BASE: &str = "https://ssgo.scourt.go.kr";
const ENTRY: &str = "/ssgo/index.on?cortId=www";
const W2: &str = "http://www.inswave.com/websquare";
const XF: &str = "http://www.w3.org/2002/xforms";
static CASE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^([0-9]{4})([가-힣]+)([0-9]{1,7})$").unwrap());

pub fn number(value: &str) -> Result<String> {
    let value: String = value.chars().filter(|ch| !ch.is_whitespace()).collect();
    let parts = CASE
        .captures(&value)
        .context("사건번호를 연도·사건부호·일련번호 형식으로 입력하세요.")?;
    let year = parts[1].parse::<i32>()?;
    ensure!(
        (1900..=Utc::now().year()).contains(&year),
        "사건번호의 연도를 확인하세요."
    );
    let serial: u32 = parts[3].parse()?;
    ensure!(serial > 0, "사건 일련번호는 1 이상이어야 합니다.");
    Ok(format!("{}{}{serial}", &parts[1], &parts[2]))
}

pub fn category(value: &str, task: &str) -> &'static str {
    match task {
        "CV" | "DC" | "DJ" => return "민사",
        "HG" | "HY" | "EY" => return "형사",
        "HS" => return "행정",
        "KS" => return "가사",
        "PT" => return "특허",
        "SB" => return "소년",
        "HP" => return "회생·파산",
        _ => {}
    }
    let Some(parts) = CASE.captures(value) else {
        return "";
    };
    match &parts[2] {
        "가합" | "가단" | "가소" | "나" | "다" | "머" | "재가합" | "재가단" | "재가소" | "재나"
        | "재다" | "재머" | "차" | "차전" | "카" | "카합" | "카단" | "카기" | "카명" | "카불" => {
            "민사"
        }
        "고합" | "고단" | "고정" | "고약" | "고약전" | "노" | "도" | "재고합" | "재고단"
        | "재고정" | "재고약" | "재노" | "재도" => "형사",
        "구" | "구합" | "구단" | "누" | "두" | "아" | "우" | "재구" | "재구합" | "재구단"
        | "재누" | "재두" | "재아" | "재우" => "행정",
        "드합" | "드단" | "너" | "느합" | "느단" | "르" | "므" => "가사",
        "허" | "후" | "재허" | "재후" => "특허",
        _ => "",
    }
}

pub fn plain(value: &Value) -> String {
    static BREAK: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"(?i)<br\s*/?>|</(?:p|div)>").unwrap());
    static TAG: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"<[^>]+>").unwrap());
    let raw = match value {
        Value::String(s) => s.clone(),
        Value::Null => return String::new(),
        other => other.to_string(),
    };
    html_escape::decode_html_entities(&TAG.replace_all(&BREAK.replace_all(&raw, "\n"), ""))
        .trim()
        .to_owned()
}

fn first<'a>(data: &'a Value, keys: &[&str]) -> &'a Value {
    keys.iter()
        .map(|key| &data[key])
        .find(|v| !v.is_null() && v.as_str() != Some(""))
        .unwrap_or(&Value::Null)
}

fn items(data: &Value) -> impl Iterator<Item = &Value> {
    data.as_array().into_iter().flatten()
}

fn qualified_name(value: &Value) -> String {
    plain(value)
        .nfc()
        .filter(|ch| !ch.is_whitespace())
        .flat_map(char::to_lowercase)
        .collect()
}

fn find_role(party: &str, candidates: Vec<(String, String)>) -> Option<String> {
    if party.is_empty() {
        return None;
    }
    let masked = |ch: char| "○●□*＊".contains(ch);
    let exact = candidates
        .iter()
        .filter(|(name, role)| name == party && !role.is_empty())
        .map(|(_, role)| role.as_str())
        .collect::<BTreeSet<_>>();
    let roles = if exact.is_empty() {
        candidates
            .iter()
            .filter(|(name, role)| {
                !role.is_empty()
                    && name.chars().count() == party.chars().count()
                    && name.chars().any(masked)
                    && name.chars().any(|c| !masked(c))
                    && name
                        .chars()
                        .zip(party.chars())
                        .all(|(a, b)| masked(a) || a == b)
            })
            .map(|(_, role)| role.as_str())
            .collect()
    } else {
        exact
    };
    if roles.is_empty() {
        None
    } else if roles.len() == 1 {
        roles.first().map(|s| s.to_string())
    } else {
        Some(String::new())
    }
}

fn party_role(party: &str, cat: &str, data: &Value) -> String {
    let party = qualified_name(&json!(party));
    let basic = &data["dma_csBasCtt"];
    let official = items(&data["dlt_btprtCttLst"])
        .map(|row| (qualified_name(&row["btprtNm"]), plain(&row["btprtDvsNm"])))
        .collect();
    if let Some(role) = find_role(&party, official) {
        return role;
    }
    let mut options = Vec::new();
    if ["민사", "행정"].contains(&cat) {
        options.push((qualified_name(&basic["rprsClmntNm"]), "원고".to_owned()));
        options.push((qualified_name(&basic["rprsAcsdNm"]), "피고".to_owned()));
    }
    if cat == "형사" {
        let selected = plain(&basic["acsdDvsNm"]);
        let selected = if selected.is_empty() {
            "피고인"
        } else {
            &selected
        };
        let seq = plain(&basic["btprtSeq"]);
        options.extend([
            (qualified_name(&basic["btprtNm"]), selected.to_owned()),
            (qualified_name(&basic["rprsAcsNm"]), "피고인".to_owned()),
            (qualified_name(&basic["prstrNm"]), "검사".to_owned()),
        ]);
        for row in items(&data["dlt_acsCttLst"]) {
            let role = if !seq.is_empty() && seq == plain(&row["btprtSeq"]) {
                selected
            } else {
                "피고인"
            };
            options.push((
                qualified_name(first(row, &["btprtNmOrg", "btprtNm", "acsNm"])),
                role.to_owned(),
            ));
        }
    }
    find_role(&party, options).unwrap_or_default()
}

fn official_number(value: &Value, kinds: &HashMap<String, String>) -> String {
    let raw: String = plain(value)
        .chars()
        .filter(|ch| !ch.is_whitespace())
        .collect();
    if CASE.is_match(&raw) {
        return raw;
    }
    if raw.len() != 14 || !raw.bytes().all(|b| b.is_ascii_digit()) {
        return String::new();
    }
    let Some((kind, _)) = kinds.iter().find(|(_, code)| code.as_str() == &raw[4..7]) else {
        return String::new();
    };
    let serial = raw[7..].trim_start_matches('0');
    if serial.is_empty() {
        String::new()
    } else {
        format!("{}{kind}{serial}", &raw[..4])
    }
}

fn summarize(
    data: &Value,
    progress: Option<&Value>,
    current: &str,
    kinds: &HashMap<String, String>,
) -> BTreeMap<String, String> {
    static BREAKS: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\n[ \t]*\n(?:[ \t]*\n)+").unwrap());
    let basic = &data["dma_csBasCtt"];
    let judgment = plain(first(
        basic,
        &[
            "btprtUltmtThrstCtt",
            "jdgmnOrdCtt",
            "jdgmnThrstCtt",
            "jdgmnCtt",
        ],
    ));
    let mut judgment = BREAKS.replace_all(&judgment, "\n\n").into_owned();
    let mut day = domain::day(&plain(first(
        basic,
        &["jdgmnPrnYmd", "jdgmnYmd", "sntncYmd"],
    )));
    if day.is_empty() && (!judgment.is_empty() || plain(&basic["crmcsUltmtDvsNm"]).contains("선고"))
    {
        day = domain::day(&plain(&basic["btprtUltmtYmd"]));
    }
    if day.is_empty() {
        let today = Local::now().date_naive().to_string();
        for row in progress
            .into_iter()
            .flat_map(|p| items(&p["dlt_csProgCtt"]))
        {
            let text = ["progCtt", "progCtt1", "progRslt"]
                .map(|k| plain(&row[k]))
                .join(" ");
            let at = domain::day(&plain(&row["progYmd"]));
            if text.contains("판결선고")
                && !domain::cancelled(&text)
                && !text.contains("예정")
                && at <= today
            {
                day = day.max(at);
            }
        }
    }
    let civil = plain(&basic["csUltmtDvsNm"]);
    if !civil.is_empty() {
        if day.is_empty() {
            day = domain::day(&plain(&basic["csUltmtYmd"]));
        }
        if judgment.is_empty() && !day.is_empty() {
            judgment = civil;
        }
    }
    let upper_raw = first(
        basic,
        &["hcrtUserCsNo", "hcrtCsNo", "apelUserCsNo", "applUserCsNo"],
    );
    let mut upper = official_number(upper_raw, kinds);
    let mut court = plain(first(basic, &["hcrtCortNm", "apelCortNm", "applCortNm"]));
    for row in items(&data["dlt_inscrtDtsLst"]) {
        let candidate = official_number(&row["userCsNo"], kinds);
        if !upper_raw.is_null()
            && (plain(&row["csNo"]) == plain(upper_raw) || !upper.is_empty() && upper == candidate)
        {
            if !candidate.is_empty() {
                upper = candidate;
            }
            let new_court = plain(&row["cortNm"]);
            if !new_court.is_empty() {
                court = new_court;
            }
            break;
        }
    }
    if upper.is_empty() {
        let next = CASE.captures(current).and_then(|p| match &p[2] {
            "고합" | "고단" | "고정" => Some("노"),
            "노" => Some("도"),
            "가합" | "가단" | "가소" => Some("나"),
            "나" => Some("다"),
            "구합" | "구단" => Some("누"),
            "누" => Some("두"),
            _ => None,
        });
        if let Some(wanted) = next {
            for row in items(&data["dlt_inscrtDtsLst"]) {
                let candidate = official_number(first(row, &["userCsNo", "csNo"]), kinds);
                if CASE.captures(&candidate).is_some_and(|m| &m[2] == wanted) {
                    upper = candidate;
                    court = plain(&row["cortNm"]);
                    break;
                }
            }
        }
    }
    let mut appeals = Vec::new();
    for (kind, role, date) in [
        ("항소", "apelCrmcsLwstRltnrDvsCd", "acsApelPrpndYmd"),
        ("상고", "applCrmcsLwstRltnrDvsCd", "acsApplPrpndYmd"),
    ] {
        let code = plain(&basic[role]);
        let who = match code.as_str() {
            "01" => "검사",
            "02" => "피고인",
            "03" => "변호인",
            "04" => "대리인",
            "05" => "보조인",
            "06" => "증인",
            "07" => "감정인",
            "08" => "통역인",
            "09" => "번역인",
            "10" => "청구인",
            "11" => "피의자",
            "12" => "배상신청인",
            "13" => "직권",
            "14" => "쌍방",
            "99" => "기타",
            _ => "",
        };
        let date = domain::day(&plain(&basic[date])).replace('-', ".");
        let detail = [who, date.as_str()]
            .into_iter()
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join(" · ");
        if !detail.is_empty() {
            appeals.push(format!("{kind} 제기됨 ({detail})"));
        }
    }
    if appeals.is_empty() {
        let status = if !upper.is_empty() {
            "상소사건 확인됨"
        } else {
            match plain(first(basic, &["aplPrpndYn", "apelPrpndYn", "applPrpndYn"])).as_str() {
                "Y" => "제기됨",
                "N" => "제기 내역 없음",
                _ => "제기 내역 확인되지 않음",
            }
        };
        appeals.push(status.to_owned());
    }
    let result = plain(&basic["aplPrpndRsltNm"]);
    if !result.is_empty() {
        appeals.push(result);
    }
    let upper = if upper.is_empty() {
        String::new()
    } else {
        [court, upper]
            .into_iter()
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join(" ")
    };
    let shown = |s: String| if s.is_empty() { UNKNOWN.to_owned() } else { s };
    [
        ("판결 주문".into(), shown(judgment)),
        ("선고일자".into(), shown(day.replace('-', "."))),
        ("상소제기 여부".into(), appeals.join(" / ")),
        ("상소사건번호".into(), shown(upper)),
    ]
    .into()
}

fn snapshot(
    data: &Value,
    progress: Option<&Value>,
    kinds: &HashMap<String, String>,
    task: &str,
    current: &str,
    party: &str,
    warnings: Vec<String>,
) -> Observation {
    static HEARING: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"^\s*(판결선고|(?:선고|변론준비|변론|공판준비|공판|조정|심문|심리|화해)기일)(?:\s|\(|$)").unwrap()
    });
    let category = category(current, task);
    let mut o = Observation {
        summary: summarize(data, progress, current, kinds),
        warnings,
        case_category: category.to_owned(),
        party_role: party_role(party, category, data),
        ..Default::default()
    };
    let mut hearings = BTreeMap::new();
    for row in items(&data["dlt_rcntDxdyLst"]) {
        let day = domain::day(&plain(&row["dxdyYmd"]));
        if day.is_empty() {
            continue;
        }
        let h = Hearing {
            day,
            clock: domain::clock(&plain(&row["dxdyHm"])),
            kind: domain::kind(&plain(&row["dxdyKndNm"])),
            location: plain(&row["dxdyPlcNm"]),
            result: plain(&row["dxdyRsltNm"]),
        };
        hearings.insert(h.signature(), h);
    }
    // Capture cancellations before deduplication so a later row cannot hide a cancellation.
    for row in
        items(&data["dlt_rcntDxdyLst"]).filter(|r| domain::cancelled(&plain(&r["dxdyRsltNm"])))
    {
        let day = domain::day(&plain(&row["dxdyYmd"]));
        if !day.is_empty() {
            o.cancelled_hearings.insert(Cancelled {
                day,
                kind: domain::kind(&plain(&row["dxdyKndNm"])),
            });
        }
    }
    let mut progress_rows = Vec::new();
    let mut seen = HashSet::new();
    for row in items(&data["dlt_hrngProgCurst"]).chain(
        progress
            .into_iter()
            .flat_map(|p| items(&p["dlt_csProgCtt"])),
    ) {
        let day = domain::day(&plain(&row["progYmd"]));
        if day.is_empty() {
            continue;
        }
        let mut content = plain(&row["progCtt"]);
        let parts = ["progCtt1", "progCtt2", "progCtt3", "progDts"].map(|k| plain(&row[k]));
        let text = format!("{content} {}", parts.join(" "));
        if content.is_empty() {
            let mut unique = HashSet::new();
            content = parts
                .into_iter()
                .filter(|s| !s.is_empty() && unique.insert(s.clone()))
                .collect::<Vec<_>>()
                .join(" ");
        }
        let result = plain(&row["progRslt"]);
        if (!content.is_empty() || !result.is_empty())
            && seen.insert((day.clone(), content.clone(), result.clone()))
        {
            progress_rows.push(Progress {
                day: day.clone(),
                content,
                result: result.clone(),
            });
        }
        if let Some(m) = HEARING.captures(&text) {
            let kind = domain::kind(&m[1]);
            let is_cancelled = domain::cancelled(&result)
                || domain::cancelled(&text)
                    && !["신청", "통지서", "제출", "접수"]
                        .iter()
                        .any(|w| text.contains(w));
            if is_cancelled {
                o.cancelled_hearings.insert(Cancelled { day, kind });
            } else if !hearings.values().any(|h| h.day == day && h.kind == kind) {
                let h = Hearing {
                    day,
                    kind,
                    clock: domain::clock(&text),
                    result,
                    ..Default::default()
                };
                hearings.insert(h.signature(), h);
            }
        }
    }
    progress_rows.sort_by(|a, b| b.day.cmp(&a.day));
    o.progress = Some(progress_rows);
    o.hearings = hearings.into_values().collect();
    o.normalize();
    o
}

struct CourtError {
    code: String,
    message: String,
}
impl std::fmt::Display for CourtError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}
impl std::fmt::Debug for CourtError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}
impl std::error::Error for CourtError {}

#[derive(Clone)]
struct Screen {
    action: String,
    maps: Vec<(String, Vec<String>)>,
}

pub struct Court {
    http: Client,
    pub courts: BTreeMap<String, String>,
    kinds: HashMap<String, String>,
    tasks: HashMap<String, String>,
    routes: HashMap<String, (String, String)>,
    banned: HashSet<String>,
    screens: HashMap<String, Screen>,
}

fn endpoint(path: &str) -> Result<Url> {
    ensure!(
        path.starts_with("/ssgo/") && !path.contains('\\'),
        "공식 사건검색 경로만 사용할 수 있습니다."
    );
    let url = Url::parse(&format!("{BASE}{path}"))?;
    ensure!(
        url.host_str() == Some("ssgo.scourt.go.kr") && url.path().starts_with("/ssgo/"),
        "공식 사건검색 경로만 사용할 수 있습니다."
    );
    Ok(url)
}

fn screen(xml: &str) -> Result<Screen> {
    let document =
        roxmltree::Document::parse(xml).context("공식 조회 화면 구조를 읽지 못했습니다.")?;
    let submission = document
        .descendants()
        .find(|n| n.has_tag_name((XF, "submission")) && n.attribute("id") == Some("sbm_search"))
        .context("공식 화면에서 조회 요청을 찾지 못했습니다.")?;
    let reference = submission.attribute("ref").unwrap_or("");
    let maps = document
        .descendants()
        .filter(|n| n.has_tag_name((W2, "dataMap")))
        .filter_map(|map| {
            let name = map.attribute("id")?;
            if !reference.contains(name) {
                return None;
            }
            let keys = map
                .descendants()
                .filter(|n| n.has_tag_name((W2, "key")))
                .filter_map(|k| k.attribute("id").map(str::to_owned))
                .collect();
            Some((name.to_owned(), keys))
        })
        .collect();
    let action = submission.attribute("action").unwrap_or_default();
    endpoint(action)?;
    Ok(Screen {
        action: action.to_owned(),
        maps,
    })
}

impl Court {
    pub async fn connect() -> Result<Self> {
        let http=Client::builder().cookie_store(true).timeout(Duration::from_secs(30))
            .user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36")
            .redirect(redirect::Policy::custom(|attempt| {
                let url=attempt.url();
                if url.scheme()=="https" && url.port_or_known_default()==Some(443) &&
                   url.host_str()==Some("ssgo.scourt.go.kr") && url.path().starts_with("/ssgo/") && attempt.previous().len()<5 {
                    attempt.follow()
                } else {attempt.error("unexpected court redirect")}
            })).build()?;
        let entry = Self::text(&http, ENTRY).await?;
        let cbx = Self::text(&http, "/ssgo/cm/js/biz/ssgocbx.js").await?;
        let common = Self::text(&http, "/ssgo/cm/js/ssgo.js").await?;
        let courts = Regex::new(r#"addItem\(\s*"(\d{6})"\s*,\s*"([^"\n]+)"\s*\)"#)?
            .captures_iter(&cbx)
            .map(|m| (m[2].to_owned(), m[1].to_owned()))
            .collect::<BTreeMap<_, _>>();
        let kinds = Regex::new(r#"addItem\(\s*"(\d{3})"\s*,\s*"([가-힣]+)"\s*\)"#)?
            .captures_iter(&cbx)
            .map(|m| (m[2].to_owned(), m[1].to_owned()))
            .collect::<HashMap<_, _>>();
        let tasks = Regex::new(r#""ctt\.csDvsNm\.([가-힣]+)"\s*:\s*"([A-Z]+)""#)?
            .captures_iter(&entry)
            .map(|m| (m[1].to_owned(), m[2].to_owned()))
            .collect();
        let routes=Regex::new(r#"(?s)\b([A-Z]{2})\s*:\s*\{\s*ID\s*:\s*"[A-Z]{2}".*?CS_GNRL_CTT\s*:\s*"([^"]+)"\s*,\s*CS_(?:PROG|ROG)_CTT\s*:\s*"([^"]*)""#)?
            .captures_iter(&common).map(|m|(m[1].to_owned(),(format!("/ssgo{}",&m[2]),if m[3].is_empty(){String::new()}else{format!("/ssgo{}",&m[3])}))).collect::<HashMap<_,_>>();
        let banned = Regex::new(r#""clt\.ssgo\.ban\.word\.list"\s*:\s*"([^"]+)""#)?
            .captures(&entry)
            .and_then(|m| m.get(1))
            .map(|m| m.as_str().split(',').map(str::to_owned).collect())
            .unwrap_or_default();
        ensure!(
            !courts.is_empty() && !kinds.is_empty() && !routes.is_empty(),
            "법원 목록 또는 조회 화면 구조가 변경되었습니다. 프로그램 업데이트가 필요합니다."
        );
        let this = Self {
            http,
            courts,
            kinds,
            tasks,
            routes,
            banned,
            screens: HashMap::new(),
        };
        this.post(
            "/ssgo/ssgo10l/selectHmpgMain.on",
            json!({"dma_search":{"cortCd":"","cdScope":"ALL","csNoHistLst":"",
            "csDvsCd":"","csYr":"","csSerial":"","btprNm":"","answer":"","fullCsNo":""}}),
        )
        .await?;
        Ok(this)
    }

    async fn text(http: &Client, path: &str) -> Result<String> {
        let res = http
            .get(endpoint(path)?)
            .header("referer", format!("{BASE}{ENTRY}"))
            .header("origin", BASE)
            .send()
            .await
            .map_err(|e| e.without_url())
            .context("법원 서버에 연결할 수 없습니다.")?;
        let ok = res.status().is_success();
        let text = res.text().await?;
        ensure!(
            ok && !text.to_ascii_lowercase().contains("web firewall"),
            "법원 사이트가 접속을 제한했거나 점검 중입니다. 잠시 후 다시 시도하세요."
        );
        Ok(text)
    }

    async fn post(&self, path: &str, body: Value) -> Result<Value> {
        let tail = if path.ends_with("/getCaptchaInf.on") {
            "sbm_captcha".to_owned()
        } else if path.ends_with("/selectHmpgMain.on") {
            "sbm_search".to_owned()
        } else {
            let tab = if path.contains("ProgCtt.on") { 2 } else { 1 };
            format!("wfSsgoDetail_ssgoCsDetailTab_contents_ssgoTab{tab}_body_sbm_search")
        };
        let response = self
            .http
            .post(endpoint(path)?)
            .header("origin", BASE)
            .header("referer", format!("{BASE}{ENTRY}"))
            .header("accept", "application/json")
            .header("content-type", "application/json;charset=UTF-8")
            .header(
                "submissionid",
                format!("mf_ssgoTopMainTab_contents_content1_body_{tail}"),
            )
            .json(&body)
            .send()
            .await
            .map_err(|e| e.without_url())
            .context("법원 서버에 연결할 수 없습니다.")?;
        let status = response.status();
        let payload: Value = response.json().await.context(
            "서버가 JSON 대신 다른 화면을 반환했습니다. 사이트 점검 또는 접속 제한을 확인하세요.",
        )?;
        let errors = &payload["errors"];
        let has_errors = match errors {
            Value::Null => false,
            Value::Object(o) => !o.is_empty(),
            Value::Array(a) => !a.is_empty(),
            Value::String(s) => !s.is_empty(),
            _ => true,
        };
        if !status.is_success()
            || payload
                .get("status")
                .is_some_and(|v| v.as_i64() != Some(200))
            || has_errors
        {
            let message = plain(&errors["errorMessage"]);
            let message = if message.is_empty() {
                plain(&payload["message"])
            } else {
                message
            };
            return Err(CourtError {
                code: plain(&errors["errorCode"]),
                message: if message.is_empty() {
                    format!("법원 서버 요청 실패 (HTTP {status})")
                } else {
                    message
                },
            }
            .into());
        }
        ensure!(
            payload["data"].is_object(),
            "서버 응답에 사건 데이터가 없습니다."
        );
        Ok(payload["data"].clone())
    }

    async fn query(&mut self, path: &str, params: &Value, page: &Value) -> Result<Value> {
        if !self.screens.contains_key(path) {
            let definition = screen(&Self::text(&self.http, path).await?)?;
            self.screens.insert(path.to_owned(), definition);
        }
        let definition = &self.screens[path];
        let mut body = Map::new();
        for (name, keys) in &definition.maps {
            let values = if name == "dma_pageInfo" { page } else { params };
            let map = keys
                .iter()
                .map(|k| {
                    (
                        k.clone(),
                        values.get(k).cloned().unwrap_or_else(|| json!("")),
                    )
                })
                .collect::<Map<_, _>>();
            body.insert(name.clone(), Value::Object(map));
        }
        self.post(&definition.action, Value::Object(body)).await
    }

    pub async fn lookup(
        &mut self,
        court: &str,
        number: &str,
        party: &str,
        ocr: &mut crate::ocr::Ocr,
    ) -> Result<Observation> {
        let parts = CASE.captures(number).context("사건번호를 확인하세요.")?;
        let (_year, kind, serial) = (&parts[1], &parts[2], &parts[3]);
        let court_code = self
            .courts
            .get(court)
            .context("목록에서 법원을 선택하세요.")?;
        ensure!(
            party.chars().count() >= 2 && !self.banned.contains(party),
            "당사자명을 확인하세요."
        );
        let task = self
            .tasks
            .get(kind)
            .context("지원하지 않는 사건구분입니다.")?
            .clone();
        let code = self
            .kinds
            .get(kind)
            .context("지원하지 않는 사건구분입니다.")?;
        let (general_path, progress_path) = self
            .routes
            .get(&task)
            .context("공식 조회 화면 구조가 변경되었습니다.")?
            .clone();
        let params = json!({"cortCd":court_code,"csYear":&parts[1],"csDvsCd":code,"csDvsNm":kind,"csSerial":serial,"btprtNm":party});
        for attempt in 0..3 {
            match self
                .challenge_lookup(&general_path, &progress_path, params.clone(), ocr)
                .await
            {
                Ok((general, progress, warnings)) => {
                    return Ok(snapshot(
                        &general,
                        progress.as_ref(),
                        &self.kinds,
                        &task,
                        number,
                        party,
                        warnings,
                    ))
                }
                Err(err) => {
                    let message = err.to_string();
                    let retry = err
                        .downcast_ref::<CourtError>()
                        .is_some_and(|e| e.code == "W_0107")
                        || ["보안문자", "자동 입력", "캡차", "captcha"]
                            .iter()
                            .any(|word| message.to_lowercase().contains(word));
                    if !retry {
                        return Err(err);
                    }
                    if attempt == 2 {
                        bail!("자동 인증을 3회 시도했지만 조회하지 못했습니다. 잠시 후 사건 조회를 다시 눌러 주세요.");
                    }
                }
            }
        }
        unreachable!("bounded lookup attempts always return")
    }

    async fn challenge_lookup(
        &mut self,
        general_path: &str,
        progress_path: &str,
        mut params: Value,
        ocr: &mut crate::ocr::Ocr,
    ) -> Result<(Value, Option<Value>, Vec<String>)> {
        let challenge = self
            .post("/ssgo/ssgo10l/getCaptchaInf.on", json!({}))
            .await?;
        let image = challenge["dma_captchaInf"]["image"]
            .as_str()
            .filter(|s| !s.is_empty())
            .context("보안문자를 받지 못했습니다.")?;
        let token = challenge["dma_captchaInf"]["answer"]
            .as_str()
            .filter(|s| !s.is_empty())
            .context("보안문자를 받지 못했습니다.")?;
        ensure!(image.len() <= 2_000_000, "보안문자 이미지가 너무 큽니다.");
        let image = STANDARD
            .decode(image.rsplit(',').next().unwrap_or_default())
            .context("보안문자 이미지 형식이 변경되었습니다.")?;
        let answer = ocr.recognize(&image)?;
        params["captchaAnswer"] = json!(format!("{answer}{token}"));
        let general = self.query(general_path, &params, &Value::Null).await?;
        let basic = &general["dma_csBasCtt"];
        ensure!(
            !plain(&basic["csNo"]).is_empty(),
            "조회된 사건이 없습니다. 법원·사건번호·당사자명을 확인하세요."
        );
        for key in ["csNo", "encCsNo", "btprtSeq"] {
            params[key] = basic.get(key).cloned().unwrap_or_else(|| json!(""));
        }
        params["captchaAnswer"] = json!("");
        params["progCttDvs"] = json!("0");
        params["inqScop"] = json!("01");
        let mut warnings = Vec::new();
        let progress = if progress_path.is_empty() {
            None
        } else {
            match self
                .query(
                    progress_path,
                    &params,
                    &json!({"pageNo":1,"pageSize":500,"totalYn":"Y"}),
                )
                .await
            {
                Ok(data) => Some(data),
                Err(err) => {
                    warnings.push(format!("일반내용은 조회되었으나 진행내용 조회 실패: {err}"));
                    None
                }
            }
        };
        Ok((general, progress, warnings))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn court_fields_produce_roles_appeals_and_progress() {
        let data = json!({
            "dma_csBasCtt":{"csNo":"123","csUltmtDvsNm":"원고일부승","csUltmtYmd":"20260131"},
            "dlt_inscrtDtsLst":[{"userCsNo":"2026누2","cortNm":"가상상급법원"}],
            "dlt_btprtCttLst":[{"btprtNm":"김○○","btprtDvsNm":"원고"},{"btprtNm":"이가상","btprtDvsNm":"피고"}],
            "dlt_rcntDxdyLst":[{"dxdyYmd":"20260131","dxdyHm":"1430","dxdyKndNm":"선고기일"}]
        });
        let progress = json!({"dlt_csProgCtt":[
            {"progYmd":"20260120","progCtt":"피고 의견서 제출"},
            {"progYmd":"20260120","progCtt":"피고 의견서 제출"}
        ]});
        let observation = snapshot(
            &data,
            Some(&progress),
            &HashMap::new(),
            "HS",
            "2024구합1",
            "김가상",
            Vec::new(),
        );
        assert_eq!(observation.case_category, "행정");
        assert_eq!(observation.party_role, "원고");
        assert_eq!(observation.text("판결 주문"), "원고일부승");
        assert_eq!(observation.text("상소사건번호"), "가상상급법원 2026누2");
        assert_eq!(observation.hearings[0].clock, "14:30");
        assert_eq!(observation.progress.unwrap().len(), 1);
    }

    #[test]
    fn normalizes_resumed_trial_and_keeps_ambiguous_roles_unknown() {
        let data = json!({
            "dma_csBasCtt":{"jdgmnPrnYmd":"20260115"},
            "dlt_btprtCttLst":[{"btprtNm":"김○○","btprtDvsNm":"원고"},{"btprtNm":"김○○","btprtDvsNm":"피고"}],
            "dlt_rcntDxdyLst":[{"dxdyYmd":"20260115","dxdyKndNm":"선고기일"},{"dxdyYmd":"20260122","dxdyKndNm":"변론기일"}]
        });
        let observation = snapshot(
            &data,
            None,
            &HashMap::new(),
            "CV",
            "2024가합1",
            "김가상",
            Vec::new(),
        );
        assert_eq!(observation.party_role, "");
        assert!(observation.cancelled_verdict_days.contains("2026-01-15"));
        assert_eq!(observation.text("선고일자"), UNKNOWN);
        assert_eq!(observation.hearings[0].kind, "변론");
    }

    #[tokio::test]
    #[ignore = "reads the public court directory and screen schema"]
    async fn public_court_directory_and_screen() -> Result<()> {
        let court = Court::connect().await?;
        assert!(court.courts.keys().any(|name| name.contains("지방법원")));
        let (general, progress) = &court.routes["CV"];
        for url in [general, progress] {
            let definition = screen(&Court::text(&court.http, url).await?)?;
            assert!(!definition.maps.is_empty());
        }
        Ok(())
    }
}
