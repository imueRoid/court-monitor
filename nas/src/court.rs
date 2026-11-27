use crate::domain::{self, Cancelled, Hearing, Observation, Progress, UNKNOWN};
use anyhow::{bail, ensure, Context, Result};
use base64::{engine::general_purpose::STANDARD, Engine};
use chrono::{Datelike, Utc};
use regex::Regex;
use reqwest::{redirect, Client, Response, Url};
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
const RESPONSE_LIMIT: usize = 16 * 1024 * 1024;
static CASE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^([0-9]{4})([가-힣]+)([0-9]{1,7})$").unwrap());

async fn response_bytes(mut response: Response) -> Result<Vec<u8>> {
    const TOO_LARGE: &str = "법원 서버 응답이 너무 큽니다.";
    ensure!(
        response
            .content_length()
            .is_none_or(|length| length <= RESPONSE_LIMIT as u64),
        "{TOO_LARGE}"
    );
    let mut bytes = Vec::new();
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|error| error.without_url())?
    {
        ensure!(chunk.len() <= RESPONSE_LIMIT - bytes.len(), "{TOO_LARGE}");
        bytes.extend_from_slice(&chunk);
    }
    Ok(bytes)
}

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
    let today = Utc::now()
        .with_timezone(&chrono_tz::Asia::Seoul)
        .date_naive()
        .to_string();
    if day.is_empty() {
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
    for fields in [Some(basic), progress.map(|data| &data["dma_csBasCtt"])]
        .into_iter()
        .flatten()
    {
        let civil = plain(&fields["csUltmtDvsNm"]);
        let result = domain::terminal_result(&civil).unwrap_or(&civil);
        let closing_day = domain::day(&plain(&fields["csUltmtYmd"]));
        if !domain::has_order(result)
            || closing_day > today
            || domain::no_hearing_judgment_cancelled(&judgment)
                && (day.is_empty() || closing_day <= day)
        {
            continue;
        }
        if day.is_empty() {
            day = closing_day.clone();
        }
        if !domain::has_order(&judgment) && !day.is_empty() && day <= today {
            judgment = result.to_owned();
            if !closing_day.is_empty() {
                day = closing_day;
            }
            break;
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
    let appeal_day = domain::day(&plain(&basic["aplYmd"])).replace('-', ".");
    let dismissal_day = domain::day(&plain(&basic["aplRjctnYmd"])).replace('-', ".");
    if !appeal_day.is_empty() {
        appeals.push(format!("제기됨 ({appeal_day})"));
    }
    if appeals.is_empty() {
        let status = if !dismissal_day.is_empty() {
            "제기됨"
        } else if !upper.is_empty() {
            "상소사건 확인됨"
        } else {
            match plain(first(basic, &["aplPrpndYn", "apelPrpndYn", "applPrpndYn"])).as_str() {
                "Y" => "제기됨",
                "N" => domain::NO_APPEAL,
                _ => domain::UNKNOWN_APPEAL,
            }
        };
        appeals.push(status.to_owned());
    }
    if !dismissal_day.is_empty() {
        appeals.push(format!("상소각하 ({dismissal_day})"));
    }
    let result = plain(&basic["aplPrpndRsltNm"]);
    if !result.is_empty() {
        appeals.push(result);
    }
    let mut finality_day = domain::day(&plain(&basic["csCfmtnYmd"]));
    if finality_day.is_empty() && !day.is_empty() {
        finality_day = items(&data["dlt_hrngProgCurst"])
            .chain(
                progress
                    .into_iter()
                    .flat_map(|p| items(&p["dlt_csProgCtt"])),
            )
            .filter(|row| {
                ["progCtt", "progCtt1", "progRslt"].iter().any(|key| {
                    let text: String = plain(&row[key]).split_whitespace().collect();
                    matches!(text.as_str(), "판결확정" | "판결이확정됨")
                })
            })
            .map(|row| domain::day(&plain(&row["progYmd"])))
            .filter(|at| at >= &day)
            .max()
            .unwrap_or_default();
    }
    let finality = if finality_day.is_empty() {
        UNKNOWN.to_owned()
    } else {
        format!("확정됨 ({})", finality_day.replace('-', "."))
    };
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
        ("확정 여부".into(), finality),
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
        Regex::new(r"^\s*(판결\s*선고(?:\s*기일)?|(?:선고|변론준비|변론|공판준비|공판|조정|심문|심리|화해)기일)(?:\s|\(|$)").unwrap()
    });
    let category = category(current, task);
    let mut o = Observation {
        summary: summarize(data, progress, current, kinds),
        warnings,
        case_category: category.to_owned(),
        party_role: party_role(party, category, data),
        ..Default::default()
    };
    for basic in
        std::iter::once(&data["dma_csBasCtt"]).chain(progress.map(|data| &data["dma_csBasCtt"]))
    {
        let result = plain(&basic["csUltmtDvsNm"]);
        if domain::no_hearing_judgment_cancelled(&result) {
            let at = domain::day(&plain(&basic["csUltmtYmd"]));
            if !at.is_empty() {
                o.cancel_no_hearing_verdict(at);
            } else if !o.order() {
                o.summary.insert("판결 주문".into(), result);
            }
        }
    }
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
    for row in items(&data["dlt_rcntDxdyLst"]) {
        let result = plain(&row["dxdyRsltNm"]);
        if !domain::hearing_result_cancelled(&result) {
            continue;
        }
        let day = domain::day(&plain(&row["dxdyYmd"]));
        if !day.is_empty() {
            let kind = domain::kind(&plain(&row["dxdyKndNm"]));
            if kind == "선고" && domain::no_hearing_judgment_cancelled(&result) {
                o.cancel_no_hearing_verdict(day);
            } else {
                o.cancelled_hearings.insert(Cancelled { day, kind });
            }
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
        let cancelled_entry = std::iter::once(content.as_str())
            .chain(parts.iter().map(String::as_str))
            .any(domain::hearing_result_cancelled);
        let result = plain(&row["progRslt"]);
        let result = if let Some(cancel) = parts
            .iter()
            .find(|part| domain::no_hearing_judgment_cancelled(part))
        {
            cancel.clone()
        } else if domain::terminal_result(&result).is_some()
            || domain::no_hearing_judgment_cancelled(&result)
        {
            result
        } else {
            parts
                .iter()
                .find(|part| domain::terminal_result(part).is_some())
                .cloned()
                .unwrap_or(result)
        };
        if content.is_empty() {
            let mut unique = HashSet::new();
            content = parts
                .into_iter()
                .filter(|s| !s.is_empty() && unique.insert(s.clone()))
                .collect::<Vec<_>>()
                .join(" ");
        }
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
            let is_cancelled = domain::hearing_result_cancelled(&result)
                || cancelled_entry
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
        let bytes = response_bytes(res).await?;
        ensure!(
            ok && !bytes
                .windows(b"web firewall".len())
                .any(|word| word.eq_ignore_ascii_case(b"web firewall")),
            "법원 사이트가 접속을 제한했거나 점검 중입니다. 잠시 후 다시 시도하세요."
        );
        Ok(String::from_utf8(bytes)
            .unwrap_or_else(|error| String::from_utf8_lossy(error.as_bytes()).into_owned()))
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
        let mut payload: Value = serde_json::from_slice(&response_bytes(response).await?).context(
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
        Ok(payload["data"].take())
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

    pub async fn lookup(&mut self, court: &str, number: &str, party: &str) -> Result<Observation> {
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
                .challenge_lookup(&general_path, &progress_path, params.clone())
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
        let answer = crate::ocr::recognize_in_worker(image).await?;
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

    #[tokio::test]
    async fn oversized_chunked_response_is_rejected() -> Result<()> {
        use std::{
            io::{BufRead, BufReader, Write},
            net::TcpListener,
        };

        let listener = TcpListener::bind("127.0.0.1:0")?;
        let address = listener.local_addr()?;
        let server = std::thread::spawn(move || -> std::io::Result<()> {
            let (mut socket, _) = listener.accept()?;
            socket.set_read_timeout(Some(Duration::from_secs(5)))?;
            socket.set_write_timeout(Some(Duration::from_secs(5)))?;
            let mut reader = BufReader::new(&mut socket);
            let mut line = String::new();
            while reader.read_line(&mut line)? > 0 && line != "\r\n" {
                line.clear();
            }
            socket.write_all(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")?;
            let chunk = [b'a'; 64 * 1024];
            for _ in 0..=RESPONSE_LIMIT / chunk.len() {
                write!(socket, "{:x}\r\n", chunk.len())?;
                socket.write_all(&chunk)?;
                socket.write_all(b"\r\n")?;
            }
            socket.write_all(b"0\r\n\r\n")
        });
        let response = Client::builder()
            .no_proxy()
            .timeout(Duration::from_secs(5))
            .build()?
            .get(format!("http://{address}"))
            .send()
            .await?;
        assert_eq!(response.content_length(), None);
        let error = response_bytes(response).await.unwrap_err();
        assert_eq!(error.to_string(), "법원 서버 응답이 너무 큽니다.");
        // Closing the connection as soon as the limit is hit can interrupt the writer.
        let _ = tokio::task::spawn_blocking(move || server.join().unwrap()).await?;
        Ok(())
    }

    #[test]
    fn court_fields_produce_roles_appeals_and_progress() {
        let data = json!({
            "dma_csBasCtt":{"csNo":"123","csUltmtDvsNm":"원고일부승","csUltmtYmd":"20260131","aplYmd":"20260206","csCfmtnYmd":"20260227"},
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
        assert_eq!(observation.text("상소제기 여부"), "제기됨 (2026.02.06)");
        assert_eq!(observation.text("상소사건번호"), "가상상급법원 2026누2");
        assert_eq!(observation.text("확정 여부"), "확정됨 (2026.02.27)");
        assert_eq!(observation.hearings[0].clock, "14:30");
        assert_eq!(observation.progress.unwrap().len(), 1);
    }

    #[test]
    fn civil_and_administrative_endings_are_read_from_progress() {
        let mut general = json!({"dma_csBasCtt":{"jdgmnOrdCtt":"판결선고"}});
        let progress =
            json!({"dma_csBasCtt":{"csUltmtYmd":"20260131","csUltmtDvsNm":"종국 : 원고일부승"}});
        let read = |general: &Value, progress: &Value, task, number| {
            snapshot(
                general,
                Some(progress),
                &HashMap::new(),
                task,
                number,
                "가상당사자",
                Vec::new(),
            )
        };
        let civil = read(&general, &progress, "CV", "2024가단1");
        assert!(civil.order());
        assert_eq!(civil.text("판결 주문"), "원고일부승");
        assert_eq!(civil.text("선고일자"), "2026.01.31");

        let progress = json!({"dlt_csProgCtt":[
            {"progYmd":"20260131", "progCtt":"선고기일 (10:00)", "progCtt2":"종국：처분취소"},
            {"progYmd":"20260201", "progCtt":"준비서면에 종국 : 원고승이라고 기재"},
            {"progYmd":"20991231", "progRslt":"종국 : 원고패"}
        ]});
        let administrative = read(&general, &progress, "HS", "2024구합1");
        assert!(administrative.order());
        assert_eq!(administrative.text("판결 주문"), "처분취소");
        assert_eq!(administrative.text("선고일자"), "2026.01.31");
        assert!(administrative.cancelled_verdict_days.is_empty());

        general["dma_csBasCtt"]["jdgmnOrdCtt"] = json!("명시된 판결 주문");
        assert_eq!(
            read(&general, &progress, "HS", "2024구합1").text("판결 주문"),
            "명시된 판결 주문"
        );
    }

    #[test]
    fn progress_ending_does_not_override_a_later_hearing_or_a_cancellation() {
        let general = json!({"dlt_rcntDxdyLst":[
            {"dxdyYmd":"20260131", "dxdyKndNm":"선고기일", "dxdyRsltNm":"종국 : 원고일부승"},
            {"dxdyYmd":"20260205", "dxdyKndNm":"변론기일"}
        ]});
        let progress = json!({"dlt_csProgCtt":[
            {"progYmd":"20260206", "progCtt":"선고기일 취소", "progRslt":"종국 : 원고패"}
        ]});
        let observation = snapshot(
            &general,
            Some(&progress),
            &HashMap::new(),
            "CV",
            "2024가단1",
            "가상당사자",
            Vec::new(),
        );
        assert!(!observation.order());
    }

    #[test]
    fn no_hearing_judgment_cancellation_keeps_the_case_in_progress() {
        for (general, progress) in [
            (
                json!({"dma_csBasCtt":{"jdgmnPrnYmd":"20260131","jdgmnOrdCtt":"원고승"},
                "dlt_rcntDxdyLst":[
                  {"dxdyYmd":"20260131","dxdyKndNm":"판결선고기일","dxdyRsltNm":"종국 : 무변론판결취소"},
                  {"dxdyYmd":"20260205","dxdyKndNm":"변론기일"}
                ]}),
                Value::Null,
            ),
            (
                json!({}),
                json!({"dlt_csProgCtt":[
                    {"progYmd":"20260131","progCtt":"판결선고기일(10:00)", "progCtt2":"무변론 판결 취소"},
                    {"progYmd":"20260205","progCtt":"변론기일 (14:00)"}
                ]}),
            ),
            (
                json!({"dma_csBasCtt":{"csUltmtYmd":"20260131","csUltmtDvsNm":"종국：무변론판결취소"}}),
                Value::Null,
            ),
        ] {
            let o = snapshot(
                &general,
                Some(&progress),
                &HashMap::new(),
                "CV",
                "2024가단1",
                "가상당사자",
                Vec::new(),
            );
            assert!(!o.order());
            assert!(o.cancelled_verdict_days.contains("2026-01-31"));
            assert!(o.hearings.iter().all(|hearing| hearing.kind != "선고"));
            let moment = chrono::DateTime::parse_from_rfc3339("2026-02-02T10:00:00+09:00")
                .unwrap()
                .with_timezone(&chrono_tz::Asia::Seoul);
            assert_eq!(
                o.judgment_stage("2026-02-02", o.next(moment), "2026-01-31"),
                domain::JudgmentStage::BeforeVerdict
            );
        }

        let data = json!({"dma_csBasCtt":{"csUltmtYmd":"20260205","csUltmtDvsNm":"원고일부승"}});
        let progress = json!({"dlt_csProgCtt":[
            {"progYmd":"20260131","progCtt":"판결선고기일(10:00) 무변론판결취소"},
            {"progYmd":"20260205","progCtt":"판결선고기일(10:00)","progRslt":"원고일부승"}
        ]});
        let o = snapshot(
            &data,
            Some(&progress),
            &HashMap::new(),
            "CV",
            "2024가단1",
            "가상당사자",
            Vec::new(),
        );
        assert_eq!(o.text("판결 주문"), "원고일부승");
        assert!(o.cancelled_verdict_days.contains("2026-01-31"));
    }

    #[test]
    fn cancelled_verdict_without_next_hearing_does_not_restore_an_older_verdict() {
        let stale = json!({"dma_csBasCtt":{"jdgmnPrnYmd":"20260115"},
            "dlt_rcntDxdyLst":[{"dxdyYmd":"20260115","dxdyKndNm":"판결선고기일"}]});
        let mut cancelled = stale.clone();
        cancelled["dlt_rcntDxdyLst"].as_array_mut().unwrap().push(
            json!({"dxdyYmd":"20260131","dxdyKndNm":"판결선고기일","dxdyRsltNm":"무변론판결취소"}),
        );
        let read = |data: &Value| {
            snapshot(data, None, &HashMap::new(), "CV", "2024가단1", "가상당사자", Vec::new())
        };
        let first = read(&cancelled);
        assert_eq!(first.no_hearing_cancelled_through, "2026-01-31");
        let mut later = read(&stale);
        later.retain_judgment(&first);
        let moment = chrono::DateTime::parse_from_rfc3339("2026-02-02T10:00:00+09:00")
            .unwrap()
            .with_timezone(&chrono_tz::Asia::Seoul);
        let settings = crate::config::Settings::from_env().unwrap();
        for o in [&first, &later] {
            assert!(o.next(moment).is_none());
            assert_eq!(
                o.judgment_stage("2026-02-02", o.next(moment), "2026-01-15"),
                domain::JudgmentStage::BeforeVerdict
            );
            let plan = domain::schedule(&settings, moment, o, "2026-01-15", 0, "");
            assert_eq!(plan.state, "monitoring");
            assert!(plan.verdict_day.is_empty());
        }

        let mut rescheduled = read(&json!({"dlt_rcntDxdyLst":[
            {"dxdyYmd":"20260205","dxdyKndNm":"판결선고기일"}]}));
        rescheduled.retain_judgment(&first);
        assert_eq!(
            rescheduled.judgment_stage("2026-02-02", rescheduled.next(moment), ""),
            domain::JudgmentStage::AwaitingVerdict
        );

        let mut decided = read(&json!({"dma_csBasCtt":{"csUltmtYmd":"20260205","csUltmtDvsNm":"원고일부승"}}));
        decided.retain_judgment(&first);
        assert!(decided.order());
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

    #[test]
    fn finality_requires_an_official_date_or_judgment_entry() {
        let data =
            json!({"dma_csBasCtt":{"btprtUltmtThrstCtt":"징역 1년","btprtUltmtYmd":"20260131"}});
        let read = |progress: &Value| {
            summarize(&data, Some(progress), "2024고단3", &HashMap::new())["확정 여부"].clone()
        };
        let mut progress = json!({"dlt_csProgCtt":[
            {"progYmd":"20260130","progCtt":"판결 확정"},
            {"progYmd":"20260201","progCtt":"판결 확정 증명 신청"},
            {"progYmd":"20260202","progCtt":"판결선고"}
        ]});
        assert_eq!(read(&progress), UNKNOWN);
        progress["dlt_csProgCtt"]
            .as_array_mut()
            .unwrap()
            .push(json!({"progYmd":"20260206","progCtt":"판결 확정"}));
        assert_eq!(read(&progress), "확정됨 (2026.02.06)");
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
