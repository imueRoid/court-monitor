use crate::config::Settings;
use chrono::{DateTime, Duration, NaiveDate, NaiveTime, TimeZone, Timelike, Utc};
use chrono_tz::Tz;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::LazyLock,
};

pub const UNKNOWN: &str = "확인되지 않음";

#[derive(Clone, Default, Serialize, Deserialize, Debug, PartialEq, Eq)]
#[serde(default)]
pub struct Hearing {
    pub day: String,
    pub clock: String,
    pub kind: String,
    pub location: String,
    pub result: String,
}

impl Hearing {
    pub fn signature(&self) -> String {
        if self.kind == "선고" {
            format!("{}|{}", self.day, self.kind)
        } else {
            format!("{}|{}|{}", self.day, self.kind, self.clock)
        }
    }

    pub fn moment(&self, zone: Tz) -> Option<DateTime<Tz>> {
        let day = NaiveDate::parse_from_str(&self.day, "%Y-%m-%d").ok()?;
        let clock = if self.clock.is_empty() {
            "09:00"
        } else {
            &self.clock
        };
        let time = NaiveTime::parse_from_str(clock, "%H:%M").ok()?;
        zone.from_local_datetime(&day.and_time(time)).earliest()
    }
}

#[derive(Clone, Serialize, Deserialize, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct Cancelled {
    pub day: String,
    pub kind: String,
}

#[derive(Clone, Serialize, Deserialize, Debug, PartialEq, Eq)]
pub struct Progress {
    pub day: String,
    pub content: String,
    pub result: String,
}

#[derive(Clone, Default, Serialize, Deserialize, Debug)]
#[serde(default)]
pub struct Observation {
    pub summary: BTreeMap<String, String>,
    pub hearings: Vec<Hearing>,
    pub cancelled_hearings: BTreeSet<Cancelled>,
    pub cancelled_verdict_days: BTreeSet<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub progress: Option<Vec<Progress>>,
    pub warnings: Vec<String>,
    pub case_category: String,
    pub party_role: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum JudgmentStage {
    BeforeVerdict,
    AwaitingVerdict,
    CheckingOrder,
    Decided,
}

pub fn day(value: &str) -> String {
    let digits: String = value.chars().filter(char::is_ascii_digit).take(8).collect();
    if digits.len() != 8 {
        return String::new();
    }
    NaiveDate::parse_from_str(&digits, "%Y%m%d")
        .map(|d| d.to_string())
        .unwrap_or_default()
}

pub fn clock(value: &str) -> String {
    static CLOCK: LazyLock<regex::Regex> =
        LazyLock::new(|| regex::Regex::new(r"(?:^|\D)([0-2]?\d:[0-5]\d)(?:\D|$)").unwrap());
    let candidate = CLOCK
        .captures(value)
        .map(|m| m[1].to_owned())
        .unwrap_or_else(|| {
            let digits: String = value.chars().filter(char::is_ascii_digit).collect();
            match digits.len() {
                4 | 6 => format!("{}:{}", &digits[..2], &digits[2..4]),
                _ => String::new(),
            }
        });
    NaiveTime::parse_from_str(&candidate, "%H:%M")
        .map(|time| time.format("%H:%M").to_string())
        .unwrap_or_default()
}

pub fn cancelled(value: &str) -> bool {
    ["변경", "취소", "연기"]
        .iter()
        .any(|word| value.contains(word))
}

pub fn trial(hearing: &Hearing) -> bool {
    ["공판", "공판준비", "변론", "변론준비", "심리", "심문"].contains(&hearing.kind.as_str())
}

pub fn kind(value: &str) -> String {
    [
        "선고",
        "변론준비",
        "변론",
        "공판준비",
        "공판",
        "조정",
        "심문",
        "심리",
        "화해",
    ]
    .into_iter()
    .find(|word| value.contains(word))
    .unwrap_or(if value.trim().is_empty() {
        "기일"
    } else {
        value.trim()
    })
    .to_owned()
}

impl Observation {
    pub fn read(value: &str) -> anyhow::Result<Self> {
        let mut observed: Self = serde_json::from_str(value)?;
        observed.normalize();
        Ok(observed)
    }

    pub fn order(&self) -> bool {
        !["", UNKNOWN, "판결선고", "미확인"].contains(&self.text("판결 주문").trim())
    }

    pub fn text(&self, field: &str) -> &str {
        self.summary.get(field).map(String::as_str).unwrap_or("")
    }

    pub fn normalize(&mut self) {
        self.hearings.retain(|h| !day(&h.day).is_empty());
        for hearing in &self.hearings {
            if cancelled(&hearing.result) {
                self.cancelled_hearings.insert(Cancelled {
                    day: hearing.day.clone(),
                    kind: hearing.kind.clone(),
                });
            }
        }
        self.cancelled_verdict_days.extend(
            self.cancelled_hearings
                .iter()
                .filter(|h| h.kind == "선고")
                .map(|h| h.day.clone()),
        );
        self.hearings.retain(|h| {
            !self.cancelled_hearings.contains(&Cancelled {
                day: h.day.clone(),
                kind: h.kind.clone(),
            })
        });
        let actual = day(self.text("선고일자"));
        if self.order() {
            self.cancelled_verdict_days.remove(&actual);
        } else {
            let continued = self
                .hearings
                .iter()
                .filter(|h| trial(h))
                .map(|h| h.day.as_str())
                .max()
                .unwrap_or("");
            self.cancelled_verdict_days.extend(
                self.hearings
                    .iter()
                    .filter(|h| h.kind == "선고" && h.day.as_str() < continued)
                    .map(|h| h.day.clone()),
            );
            if !actual.is_empty() && actual.as_str() < continued {
                self.cancelled_verdict_days.insert(actual.clone());
            }
            if self.cancelled_verdict_days.contains(&actual) {
                self.summary.insert("선고일자".into(), UNKNOWN.into());
            }
        }
        self.hearings
            .retain(|h| h.kind != "선고" || !self.cancelled_verdict_days.contains(&h.day));
        self.hearings
            .sort_by(|a, b| (&a.day, &a.clock, &a.kind).cmp(&(&b.day, &b.clock, &b.kind)));
    }

    pub fn next(&self, now: DateTime<Tz>) -> Option<&Hearing> {
        let today = now.date_naive().to_string();
        self.hearings.iter().find(|h| {
            h.day > today
                || h.day == today
                    && (h.clock.is_empty()
                        || h.moment(now.timezone()).is_some_and(|moment| moment >= now))
        })
    }

    fn verdict_day(&self, remembered: &str) -> String {
        let reported = day(self.text("선고일자"));
        if !reported.is_empty() {
            return reported;
        }
        if let Some(hearing) = self.hearings.iter().rev().find(|h| h.kind == "선고") {
            return hearing.day.clone();
        }
        let resumed = !self.order()
            && self
                .hearings
                .iter()
                .any(|h| trial(h) && h.day.as_str() > remembered);
        if resumed || self.cancelled_verdict_days.contains(remembered) {
            return String::new();
        }
        remembered.to_owned()
    }

    pub fn judgment_stage(
        &self,
        today: &str,
        upcoming: Option<&Hearing>,
        remembered: &str,
    ) -> JudgmentStage {
        if self.order() {
            return JudgmentStage::Decided;
        }
        if let Some(hearing) = upcoming.filter(|h| h.kind == "선고") {
            return if hearing.day.as_str() > today {
                JudgmentStage::AwaitingVerdict
            } else {
                JudgmentStage::CheckingOrder
            };
        }
        let verdict_day = self.verdict_day(remembered);
        if !verdict_day.is_empty() && verdict_day.as_str() <= today {
            JudgmentStage::CheckingOrder
        } else {
            JudgmentStage::BeforeVerdict
        }
    }

    pub fn interval(&self, now: DateTime<Tz>) -> Value {
        let today = now.date_naive().to_string();
        let previous = self.hearings.iter().rev().find(|h| {
            h.day < today
                || h.day == today
                    && !h.clock.is_empty()
                    && h.moment(now.timezone()).is_some_and(|moment| moment <= now)
        });
        let upcoming = self.next(now);
        let end = upcoming.map_or(today.as_str(), |h| h.day.as_str());
        let entries: Vec<_> = self
            .progress
            .iter()
            .flatten()
            .filter(|row| {
                previous.is_none_or(|h| h.day <= row.day)
                    && row.day <= today
                    && (upcoming.is_none() || row.day.as_str() < end)
            })
            .collect();
        json!({"previous":previous,"next":upcoming,"through":end,"entries":entries})
    }

    pub fn calendar_hearings(&self, now: DateTime<Tz>) -> Vec<Hearing> {
        let today = now.date_naive().to_string();
        let upcoming: Vec<_> = self
            .hearings
            .iter()
            .filter(|h| h.moment(now.timezone()).is_some_and(|moment| moment >= now))
            .collect();
        let mut chosen = BTreeMap::new();
        for h in self
            .hearings
            .iter()
            .filter(|h| h.day == today)
            .chain(
                upcoming
                    .iter()
                    .copied()
                    .take_while(|h| h.day == upcoming[0].day),
            )
            .chain(
                self.hearings
                    .iter()
                    .rev()
                    .find(|h| h.kind == "선고" && h.day <= today),
            )
        {
            chosen.insert(h.signature(), h.clone());
        }
        let verdict = day(self.text("선고일자"));
        if !verdict.is_empty() && self.order() {
            let hearing = Hearing {
                day: verdict,
                kind: "선고".into(),
                ..Default::default()
            };
            chosen.entry(hearing.signature()).or_insert(hearing);
        }
        let mut result: Vec<_> = chosen.into_values().collect();
        result.sort_by(|a, b| (&a.day, &a.clock).cmp(&(&b.day, &b.clock)));
        result
    }
}

pub fn local(now: f64, zone: Tz) -> DateTime<Tz> {
    DateTime::from_timestamp_millis((1000.0 * now) as i64)
        .unwrap_or(DateTime::<Utc>::UNIX_EPOCH)
        .with_timezone(&zone)
}

pub fn stamp(moment: DateTime<Tz>) -> f64 {
    moment.timestamp_millis() as f64 / 1000.0
}

pub fn at_hour(now: DateTime<Tz>, hour: u32) -> DateTime<Tz> {
    now.date_naive()
        .and_hms_opt(hour, 0, 0)
        .and_then(|time| now.timezone().from_local_datetime(&time).earliest())
        .unwrap_or(now)
}

pub fn tomorrow(now: DateTime<Tz>, hour: u32) -> DateTime<Tz> {
    at_hour(now + Duration::days(1), hour)
}

pub struct Schedule {
    pub state: &'static str,
    pub next: Option<f64>,
    pub verdict_day: String,
    pub count: u32,
    pub count_day: String,
}

pub fn schedule(
    s: &Settings,
    now: DateTime<Tz>,
    o: &Observation,
    remembered: &str,
    previous_count: u32,
    count_day: &str,
) -> Schedule {
    let today = now.date_naive().to_string();
    let verdict_day = o.verdict_day(remembered);
    let mut count = if count_day == today {
        previous_count
    } else {
        0
    };
    let (state, next) = if o.order() {
        ("done", None)
    } else {
        let start = at_hour(now, s.watch_start_hour);
        let end = at_hour(now, s.watch_end_hour);
        if !verdict_day.is_empty() && verdict_day <= today {
            let first = o
                .hearings
                .iter()
                .find(|h| h.kind == "선고" && h.day == today)
                .and_then(|h| h.moment(now.timezone()))
                .map_or(start, |at| {
                    start.max(at + Duration::minutes(s.hearing_delay_minutes))
                });
            if now >= first {
                count = count.saturating_add(1);
            }
            let due = if now < first && first < end {
                first
            } else if now < end && count < s.verdict_checks_per_day {
                (now + Duration::minutes(s.verdict_retry_minutes)).min(end)
            } else {
                tomorrow(now, s.watch_start_hour)
            };
            ("awaiting_order", Some(stamp(due)))
        } else {
            let mut due = now + Duration::hours(s.base_poll_hours);
            for time in o.hearings.iter().filter_map(|h| h.moment(now.timezone())) {
                let candidate = time + Duration::minutes(s.hearing_delay_minutes);
                if candidate > now {
                    due = due.min(candidate);
                }
            }
            if o.hearings.iter().any(|h| h.day == today) {
                if now.hour() < s.watch_start_hour {
                    due = due.min(start);
                } else if now < end {
                    due = due.min(now + Duration::hours(3)).min(end);
                }
            }
            ("monitoring", Some(stamp(due)))
        }
    };
    Schedule {
        state,
        next,
        verdict_day,
        count,
        count_day: today,
    }
}

pub fn event_payload(
    id: &str,
    court: &str,
    number: &str,
    party: &str,
    h: &Hearing,
    o: &Observation,
    zone: Tz,
) -> Option<Value> {
    let mut title = format!("[{}] {court} {number}", h.kind);
    let mut lines = vec![
        format!("법원: {court}"),
        format!("사건번호: {number}"),
        format!("당사자: {party}"),
        format!("기일: {}", h.kind),
    ];
    if !h.result.is_empty() {
        lines.push(format!("기일 결과: {}", h.result));
    }
    if h.kind == "선고" && o.order() && day(o.text("선고일자")) == h.day {
        title = format!("[선고·주문 확인] {court} {number}");
        lines.extend([
            "".into(),
            "판결 주문 / 종국결과".into(),
            o.text("판결 주문").into(),
            "".into(),
            format!(
                "상소제기 여부: {}",
                o.summary
                    .get("상소제기 여부")
                    .map_or(UNKNOWN, String::as_str)
            ),
            format!(
                "상소사건번호: {}",
                o.summary
                    .get("상소사건번호")
                    .map_or(UNKNOWN, String::as_str)
            ),
        ]);
    }
    let description = lines
        .iter()
        .map(|line| {
            html_escape::encode_text(line)
                .replace('"', "&quot;")
                .replace('\'', "&#x27;")
        })
        .collect::<Vec<_>>()
        .join("<br>");
    let (start, end) = if h.clock.is_empty() {
        let end = NaiveDate::parse_from_str(&h.day, "%Y-%m-%d")
            .ok()?
            .succ_opt()?
            .to_string();
        (json!({"date":h.day}), json!({"date":end}))
    } else {
        let start = h.moment(zone)?;
        let timezone = zone.name();
        (
            json!({"dateTime":start.to_rfc3339(),"timeZone":timezone}),
            json!({"dateTime":(start + Duration::minutes(30)).to_rfc3339(),"timeZone":timezone}),
        )
    };
    Some(
        json!({"summary":title,"description":description,"location":if h.location.is_empty(){court}else{&h.location},
        "visibility":"private","extendedProperties":{"private":{"app":"court-monitor","case_id":id}},"start":start,"end":end}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn verdict_stage(o: &Observation, utc: &str, remembered: &str) -> JudgmentStage {
        let moment = DateTime::parse_from_rfc3339(utc)
            .unwrap()
            .with_timezone(&chrono_tz::Asia::Seoul);
        o.judgment_stage(&moment.date_naive().to_string(), o.next(moment), remembered)
    }

    #[test]
    fn upcoming_verdict_switches_to_checking_at_seoul_midnight() {
        let mut observation = Observation {
            hearings: vec![Hearing {
                day: "2026-11-28".into(),
                clock: "14:00".into(),
                kind: "선고".into(),
                ..Default::default()
            }],
            ..Default::default()
        };
        assert_eq!(
            verdict_stage(&observation, "2026-11-27T14:59:59Z", ""),
            JudgmentStage::AwaitingVerdict
        );
        for utc in [
            "2026-11-27T15:00:00Z",
            "2026-11-28T06:00:00Z",
            "2026-11-30T00:00:00Z",
        ] {
            assert_eq!(
                verdict_stage(&observation, utc, ""),
                JudgmentStage::CheckingOrder
            );
        }
        observation
            .summary
            .insert("판결 주문".into(), "청구를 기각한다.".into());
        assert_eq!(
            verdict_stage(&observation, "2026-11-28T06:00:00Z", ""),
            JudgmentStage::Decided
        );
    }

    #[test]
    fn ordinary_next_hearing_stays_before_verdict_and_cancelled_dates_are_ignored() {
        let mut observation = Observation {
            hearings: vec![
                Hearing {
                    day: "2026-11-22".into(),
                    kind: "선고".into(),
                    result: "기일 변경".into(),
                    ..Default::default()
                },
                Hearing {
                    day: "2026-11-27".into(),
                    kind: "변론".into(),
                    ..Default::default()
                },
                Hearing {
                    day: "2026-11-30".into(),
                    kind: "선고".into(),
                    ..Default::default()
                },
            ],
            ..Default::default()
        };
        observation.normalize();
        assert_eq!(
            verdict_stage(&observation, "2026-11-26T00:00:00Z", "2026-11-22"),
            JudgmentStage::BeforeVerdict
        );
        observation.hearings.clear();
        assert_eq!(
            verdict_stage(&observation, "2026-11-26T00:00:00Z", "2026-11-22"),
            JudgmentStage::BeforeVerdict
        );
        assert_eq!(
            verdict_stage(&observation, "2026-11-26T00:00:00Z", "2026-11-23"),
            JudgmentStage::CheckingOrder
        );
    }
}
