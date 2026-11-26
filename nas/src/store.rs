use crate::{
    config::Settings,
    court::category,
    domain::{at_hour, event_payload, local, schedule, stamp, tomorrow, Hearing, Observation},
};
use anyhow::{anyhow, Result};
use chrono::Timelike;
use chrono_tz::Tz;
use ring::rand::{SecureRandom, SystemRandom};
use rusqlite::{
    functions::FunctionFlags, params, params_from_iter, Connection, OptionalExtension, Row,
    TransactionBehavior,
};
use serde::{de::DeserializeOwned, Deserialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

#[derive(Clone)]
pub struct Store {
    pub path: PathBuf,
}

#[derive(Deserialize)]
pub struct CaseInput {
    pub court: String,
    pub case_number: String,
    pub party: String,
}

#[derive(Clone, Copy, Default, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CaseScope {
    #[default]
    All,
    Ongoing,
    Finished,
}

#[derive(Debug)]
pub struct CaseJob {
    pub id: String,
    pub court: String,
    pub number: String,
    pub party: String,
    pub lease: String,
}

pub struct Event {
    pub id: String,
    pub case_id: String,
    pub signature: String,
    pub hearing: String,
    pub payload: String,
    pub hash: String,
    lease: String,
    failures: u32,
}

fn event_row(row: &Row<'_>) -> rusqlite::Result<Event> {
    Ok(Event {
        id: row.get("id")?,
        case_id: row.get("case_id")?,
        signature: row.get("signature")?,
        hearing: row.get("hearing")?,
        payload: row.get("payload")?,
        hash: row.get("payload_hash")?,
        lease: row.get("lease_token")?,
        failures: row.get("failures")?,
    })
}

pub fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

pub fn random<const N: usize>() -> Result<[u8; N]> {
    let mut bytes = [0; N];
    SystemRandom::new()
        .fill(&mut bytes)
        .map_err(|_| anyhow!("system random unavailable"))?;
    Ok(bytes)
}

pub fn hash(data: impl AsRef<[u8]>) -> String {
    hex::encode(Sha256::digest(data))
}

pub fn meta<T: DeserializeOwned>(db: &Connection, key: &str) -> Result<Option<T>> {
    let value: Option<String> = db
        .query_row("SELECT value FROM meta WHERE key=?1", [key], |r| r.get(0))
        .optional()?;
    value
        .map(|s| serde_json::from_str(&s).map_err(Into::into))
        .transpose()
}

pub fn set_meta(db: &Connection, key: &str, value: impl serde::Serialize) -> Result<()> {
    db.execute(
        "INSERT INTO meta VALUES (?1,?2) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        params![key, serde_json::to_string(&value)?],
    )?;
    Ok(())
}

impl Store {
    pub fn open(path: &Path) -> Result<Self> {
        let path = std::path::absolute(path)?;
        fs::create_dir_all(
            path.parent()
                .ok_or_else(|| anyhow!("database needs a containing directory"))?,
        )?;
        let this = Self { path };
        let db = this.connection()?;
        db.pragma_update(None, "journal_mode", "WAL")?;
        db.execute_batch(include_str!("schema.sql"))?;
        this.write(|db| {
            let has_column = db.query_row("SELECT EXISTS(SELECT 1 FROM pragma_table_info('cases') WHERE name='detail_refresh_at')", [], |r| r.get::<_, bool>(0))?;
            if !has_column {
                db.execute("ALTER TABLE cases ADD COLUMN detail_refresh_at REAL NOT NULL DEFAULT 0", [])?;
            }
            Ok(())
        })?;
        Ok(this)
    }

    pub fn connection(&self) -> Result<Connection> {
        let db = Connection::open(&self.path)?;
        db.busy_timeout(Duration::from_secs(15))?;
        db.pragma_update(None, "foreign_keys", true)?;
        Ok(db)
    }

    pub fn write<T>(&self, f: impl FnOnce(&Connection) -> Result<T>) -> Result<T> {
        let mut db = self.connection()?;
        let transaction = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let value = f(&transaction)?;
        transaction.commit()?;
        Ok(value)
    }

    pub fn meta<T: DeserializeOwned>(&self, key: &str) -> Result<Option<T>> {
        meta(&self.connection()?, key)
    }

    pub fn set_meta(&self, key: &str, value: impl serde::Serialize) -> Result<()> {
        self.write(|db| set_meta(db, key, value))
    }

    pub fn import(&self, cases: &[CaseInput]) -> Result<Vec<String>> {
        self.write(|db| {
            let mut ids = Vec::new();
            let at = now();
            for case in cases {
                let values = params![case.court, case.case_number, case.party];
                let existing = db.query_row("SELECT id FROM cases WHERE court=?1 AND case_number=?2 AND party=?3", values, |r| r.get(0)).optional()?;
                let id = if let Some(id) = existing { id } else {
                    let id = hex::encode(random::<16>()?);
                    db.execute("INSERT INTO cases (id,court,case_number,party,next_check,created) VALUES (?1,?2,?3,?4,?5,?5)",
                        params![id,case.court,case.case_number,case.party,at])?;
                    id
                };
                ids.push(id);
            }
            Ok(ids)
        })
    }

    pub fn delete(&self, ids: &[String]) -> Result<usize> {
        self.write(|db| {
            let mut deleted = 0;
            for id in ids {
                db.execute("DELETE FROM events WHERE case_id=?1", [id])?;
                db.execute("DELETE FROM logs WHERE case_id=?1", [id])?;
                deleted += db.execute("DELETE FROM cases WHERE id=?1", [id])?;
            }
            Ok(deleted)
        })
    }

    pub fn refresh(&self, id: &str) -> Result<bool> {
        self.write(|db| {
            Ok(db.execute(
                "UPDATE cases SET manual=1,next_check=?1 WHERE id=?2",
                params![now(), id],
            )? != 0)
        })
    }

    pub fn active(&self, id: &str, active: bool) -> Result<bool> {
        self.write(|db| Ok(db.execute("UPDATE cases SET active=?1,next_check=CASE WHEN ?1 THEN ?2 ELSE next_check END WHERE id=?3",
            params![active,now(),id])? != 0))
    }

    pub fn detail_refresh(&self, id: &str) -> Result<Option<Value>> {
        self.write(|db| {
            let row = db.query_row("SELECT snapshot,manual,lease_until,last_checked,detail_refresh_at FROM cases WHERE id=?1", [id], |r|
                Ok((r.get::<_,String>(0)?,r.get::<_,bool>(1)?,r.get::<_,f64>(2)?,r.get::<_,Option<f64>>(3)?,r.get::<_,f64>(4)?))).optional()?;
            let Some((snapshot,manual,lease,checked,last)) = row else { return Ok(None); };
            let observed = Observation::read(&snapshot)?;
            let at = now();
            let has_detail = if observed.order() { observed.summary.contains_key("확정 여부") }
                else { observed.progress.is_some() };
            let (queued, reason) = if manual || lease > at { (true, "pending") }
                else if checked.is_some_and(|checked| at-checked<60.0) && has_detail { (false,"fresh") }
                else if last != 0.0 && at-last < 60.0 { (false,"cooldown") }
                else {
                    db.execute("UPDATE cases SET manual=1,next_check=?1,detail_refresh_at=?1 WHERE id=?2",params![at,id])?;
                    (true,"requested")
                };
            Ok(Some(json!({"queued":queued,"reason":reason})))
        })
    }

    pub fn page(
        &self,
        query: &str,
        page: usize,
        size: usize,
        scope: CaseScope,
        zone: Tz,
        calendar_configured: bool,
    ) -> Result<Value> {
        let mut conn = self.connection()?;
        let time = now();
        let moment = local(time, zone);
        let today = moment.date_naive().to_string();
        conn.create_scalar_function(
            "searchable_text",
            4,
            FunctionFlags::SQLITE_DETERMINISTIC,
            |ctx| {
                let court = ctx.get::<String>(0)?;
                let number = ctx.get::<String>(1)?;
                let party = ctx.get::<String>(2)?;
                let raw: Value = serde_json::from_str(&ctx.get::<String>(3)?).unwrap_or_default();
                let mut text = format!(
                    "{court} {number} {party} {} {}",
                    raw["case_category"]
                        .as_str()
                        .filter(|s| !s.is_empty())
                        .unwrap_or_else(|| category(&number, "")),
                    raw["party_role"].as_str().unwrap_or("")
                );
                for key in ["summary", "hearings", "progress"] {
                    append_text(&raw[key], &mut text);
                }
                Ok(fold(&text))
            },
        )?;
        conn.create_scalar_function(
            "case_finished",
            1,
            FunctionFlags::SQLITE_DETERMINISTIC,
            |ctx| Ok(Observation::read(&ctx.get::<String>(0)?).is_ok_and(|o| o.order())),
        )?;
        conn.create_scalar_function(
            "upcoming_key",
            1,
            FunctionFlags::SQLITE_DETERMINISTIC,
            move |ctx| {
                let observed = Observation::read(&ctx.get::<String>(0)?).unwrap_or_default();
                Ok(observed
                    .next(moment)
                    .map(|h| format!("{}T{}", h.day, h.clock)))
            },
        )?;
        let db = conn.transaction()?;
        let terms: Vec<String> = query.split_whitespace().map(fold).collect();
        let mut predicates = vec![
            "instr(searchable_text(c.court,c.case_number,c.party,c.snapshot),?)>0";
            terms.len()
        ];
        match scope {
            CaseScope::All => {}
            CaseScope::Ongoing => predicates.push("NOT case_finished(c.snapshot)"),
            CaseScope::Finished => predicates.push("case_finished(c.snapshot)"),
        };
        let filter = if predicates.is_empty() {
            String::new()
        } else {
            format!("WHERE {}", predicates.join(" AND "))
        };
        let (total, finished, monitoring, waiting): (i64, i64, i64, i64) = db.query_row(
            "WITH status AS MATERIALIZED (SELECT active,state,case_finished(snapshot) AS finished FROM cases)
            SELECT COUNT(*),COALESCE(SUM(finished),0),COALESCE(SUM(active=1 AND NOT finished),0),
            COALESCE(SUM(active=1 AND NOT finished AND state='awaiting_order'),0) FROM status",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )?;
        let ongoing = total - finished;
        let scope_total = match scope {
            CaseScope::All => total,
            CaseScope::Ongoing => ongoing,
            CaseScope::Finished => finished,
        };
        let count: i64 = if terms.is_empty() {
            scope_total
        } else {
            db.query_row(
                &format!("SELECT COUNT(*) FROM cases c {filter}"),
                params_from_iter(terms.iter()),
                |r| r.get(0),
            )?
        };
        let count = usize::try_from(count)?;
        let pages = count.div_ceil(size).max(1);
        let page = page.min(pages);
        let query = format!("SELECT c.*, COALESCE(e.pending,0) AS calendar_pending, COALESCE(e.error,'') AS calendar_error
            FROM cases c LEFT JOIN (SELECT case_id, SUM(payload_hash!=synced_hash) AS pending, MAX(last_error) AS error FROM events GROUP BY case_id) e
            ON e.case_id=c.id {filter} ORDER BY upcoming_key(c.snapshot) ASC NULLS LAST,c.created DESC,c.id DESC LIMIT {size} OFFSET {}",size*(page-1));
        let mut statement = db.prepare(&query)?;
        let mut rows = statement.query(params_from_iter(terms.iter()))?;
        let mut cases = Vec::new();
        while let Some(row) = rows.next()? {
            let observed = Observation::read(&row.get::<_, String>("snapshot")?)?;
            let number = row.get::<_, String>("case_number")?;
            let verdict_day: String = row.get("verdict_day")?;
            let next_hearing = observed.next(moment);
            let judgment_stage = observed.judgment_stage(&today, next_hearing, &verdict_day);
            let case_category = if observed.case_category.is_empty() {
                category(&number, "")
            } else {
                &observed.case_category
            };
            cases.push(json!({"id":row.get::<_,String>("id")?,"court":row.get::<_,String>("court")?,"case_number":number,
                "party":row.get::<_,String>("party")?,"active":row.get::<_,i64>("active")?,"state":row.get::<_,String>("state")?,
                "last_checked":row.get::<_,Option<f64>>("last_checked")?,"next_check":row.get::<_,Option<f64>>("next_check")?,
                "last_error":row.get::<_,String>("last_error")?,"verdict_day":verdict_day,"judgment_stage":judgment_stage,
                "verdict_count":row.get::<_,u32>("verdict_count")?,"calendar_pending":row.get::<_,i64>("calendar_pending")?,
                "calendar_error":row.get::<_,String>("calendar_error")?,"observed":observed,"case_category":case_category,
                "party_role":observed.party_role,"refresh_pending":row.get::<_,bool>("manual")?||row.get::<_,f64>("lease_until")?>time,
                "next_hearing":next_hearing,"show_progress":!observed.order(),
                "progress_interval":if observed.order(){Value::Null}else{observed.interval(moment)}}));
        }
        let pending: i64 = db.query_row(
            "SELECT COUNT(*) FROM events WHERE payload_hash!=synced_hash",
            [],
            |r| r.get(0),
        )?;
        Ok(
            json!({"cases":cases,"total":count,"scope_total":scope_total,"page":page,"page_size":size,"pages":pages,
            "metrics":{"total":total,"ongoing":ongoing,"monitoring":monitoring,"waiting":waiting,"pending":pending},
            "worker_heartbeat":meta::<Value>(&db,"worker_heartbeat")?,"calendar_configured":calendar_configured}),
        )
    }

    pub fn export(&self) -> Result<Value> {
        let db = self.connection()?;
        let mut stmt = db.prepare("SELECT court,case_number,party,snapshot,last_checked FROM cases ORDER BY created DESC,id DESC")?;
        let mut rows = stmt.query([])?;
        let mut cases = Vec::new();
        while let Some(row) = rows.next()? {
            let observed = Observation::read(&row.get::<_, String>(3)?)?;
            cases.push(json!({"query":{"court":row.get::<_,String>(0)?,"case_number":row.get::<_,String>(1)?,"party":row.get::<_,String>(2)?},
                "summary":observed.summary,"last_checked":row.get::<_,Option<f64>>(4)?}));
        }
        Ok(json!({"exported_at":now(),"cases":cases}))
    }

    pub fn logs(&self) -> Result<Vec<Value>> {
        let db = self.connection()?;
        let mut stmt =
            db.prepare("SELECT id,case_id,at,level,message FROM logs ORDER BY id DESC LIMIT 100")?;
        let list = stmt
            .query_map([], |r| {
                Ok(
                    json!({"id":r.get::<_,i64>(0)?,"case_id":r.get::<_,Option<String>>(1)?,
            "at":r.get::<_,f64>(2)?,"level":r.get::<_,String>(3)?,"message":r.get::<_,String>(4)?}),
                )
            })?
            .collect::<rusqlite::Result<_>>()?;
        Ok(list)
    }

    pub fn events(&self, id: &str) -> Result<Vec<Value>> {
        let db = self.connection()?;
        let mut stmt = db.prepare("SELECT id,signature,payload,synced_at,last_error FROM events WHERE case_id=?1 ORDER BY start_ts DESC")?;
        let list = stmt.query_map([id], |r| Ok(json!({"id":r.get::<_,String>(0)?,"signature":r.get::<_,String>(1)?,"payload":r.get::<_,String>(2)?,
            "synced_at":r.get::<_,Option<f64>>(3)?,"last_error":r.get::<_,String>(4)?})))?.collect::<rusqlite::Result<_>>()?;
        Ok(list)
    }

    pub fn claim_case(&self, at: f64, s: &Settings) -> Result<Option<CaseJob>> {
        self.write(|db| {
            let mut stmt = db.prepare("SELECT id,court,case_number,party,state,manual,verdict_count_day,verdict_count
                FROM cases WHERE (active OR manual) AND next_check<=?1 AND lease_until<=?1 ORDER BY next_check")?;
            let mut rows = stmt.query([at])?;
            let moment = local(at,s.timezone);
            let today = moment.date_naive().to_string();
            while let Some(r) = rows.next()? {
                let id: String = r.get(0)?;
                if r.get::<_,String>(4)? == "awaiting_order" && !r.get::<_,bool>(5)? {
                    let capped = r.get::<_,String>(6)? == today && r.get::<_,u32>(7)? >= s.verdict_checks_per_day;
                    if capped || moment.hour() < s.watch_start_hour || moment.hour() >= s.watch_end_hour {
                        let due = if !capped && moment.hour()<s.watch_start_hour { at_hour(moment,s.watch_start_hour) }
                            else { tomorrow(moment,s.watch_start_hour) };
                        db.execute("UPDATE cases SET next_check=?1 WHERE id=?2",params![stamp(due),id])?;
                        continue;
                    }
                }
                let job = CaseJob { id, court:r.get(1)?,number:r.get(2)?,party:r.get(3)?,lease:hex::encode(random::<16>()?) };
                db.execute("UPDATE cases SET manual=0,lease_until=?1,lease_token=?2 WHERE id=?3",params![at+600.0,job.lease,job.id])?;
                return Ok(Some(job));
            }
            Ok(None)
        })
    }

    pub fn complete(&self, job: &CaseJob, mut o: Observation, at: f64, s: &Settings) -> Result<()> {
        self.write(|db| {
            let row = db.query_row("SELECT manual,verdict_day,verdict_count,verdict_count_day,snapshot FROM cases WHERE id=?1 AND lease_token=?2",
                params![job.id,job.lease], |r| Ok((r.get::<_,bool>(0)?,r.get::<_,String>(1)?,r.get::<_,u32>(2)?,r.get::<_,String>(3)?,r.get::<_,String>(4)?))).optional()?;
            let Some((manual,day,count,count_day,snapshot)) = row else { return Ok(()); };
            o.retain_judgment(&Observation::read(&snapshot)?);
            let moment = local(at,s.timezone);
            let plan = schedule(s,moment,&o,&day,count,&count_day);
            let due = if manual {Some(at)} else {plan.next};
            db.execute("UPDATE cases SET state=?1,snapshot=?2,last_checked=?3,next_check=?4,last_error='',failures=0,
                verdict_day=?5,verdict_count=?6,verdict_count_day=?7,lease_until=0,lease_token='' WHERE id=?8",
                params![plan.state,serde_json::to_string(&o)?,at,due,plan.verdict_day,plan.count,plan.count_day,job.id])?;
            let signatures: BTreeSet<_> = o.hearings.iter().map(Hearing::signature).collect();
            for hearing in o.calendar_hearings(moment) {
                observe_event(db, job, hearing, &o, &signatures, moment)?;
            }
            let mut stmt = db.prepare("SELECT * FROM events WHERE case_id=?1")?;
            let events = stmt.query_map([&job.id], event_row)?.collect::<rusqlite::Result<Vec<_>>>()?;
            for event in events {
                let h: Hearing = serde_json::from_str(&event.hearing)?;
                if h.kind != "선고" || !o.cancelled_verdict_days.contains(&h.day) { continue; }
                let mut payload: Value = serde_json::from_str(&event.payload)?;
                let title = payload["summary"].as_str().unwrap_or("");
                if title.starts_with("[취소·변경]") { continue; }
                payload["summary"] = json!(format!("[취소·변경] {title}"));
                payload["description"] = json!(format!("{}<br><br>기일 취소·변경 또는 이후 공판·변론 재개가 확인되어 기존 선고기일을 제외했습니다.",
                    payload["description"].as_str().unwrap_or("")));
                let text = serde_json::to_string(&payload)?;
                db.execute("UPDATE events SET payload=?1,payload_hash=?2,next_sync=?3,last_error='',failures=0 WHERE id=?4",
                    params![text,hash(&text),at,event.id])?;
            }
            log(db,&job.id,at,"info",if plan.state=="done" {"사건정보 조회 완료 · 주문 확인"} else {"사건정보 조회 완료"})
        })
    }

    pub fn fail_case(&self, job: &CaseJob, message: &str, at: f64, s: &Settings) -> Result<()> {
        self.write(|db| {
            let row = db.query_row("SELECT failures,manual,state,verdict_count,verdict_count_day FROM cases WHERE id=?1 AND lease_token=?2",
                params![job.id,job.lease], |r| Ok((r.get::<_,u32>(0)?,r.get::<_,bool>(1)?,r.get::<_,String>(2)?,r.get::<_,u32>(3)?,r.get::<_,String>(4)?))).optional()?;
            let Some((failures,manual,state,mut count,mut count_day)) = row else {return Ok(());};
            let mut due = if manual {at} else {at + f64::from((900 * 2u32.pow(failures.min(6))).min(21600))};
            if state=="awaiting_order" {
                let moment = local(at,s.timezone);
                let today = moment.date_naive().to_string();
                count = if today==count_day { count.saturating_add(1) } else { 1 };
                count_day = today;
                let due_local = local(due,s.timezone);
                if !manual && (count>=s.verdict_checks_per_day || due_local.hour()>=s.watch_end_hour || due_local.date_naive()>moment.date_naive()) {
                    due = stamp(tomorrow(moment,s.watch_start_hour));
                }
            }
            let message: String = message.chars().take(1000).collect();
            let due = (manual || state != "done").then_some(due);
            db.execute("UPDATE cases SET last_error=?1,failures=?2,next_check=?3,verdict_count=?4,verdict_count_day=?5,
                lease_until=0,lease_token='' WHERE id=?6", params![message,failures.saturating_add(1),due,count,count_day,job.id])?;
            log(db,&job.id,at,"error",&message)
        })
    }

    pub fn claim_event(&self, at: f64) -> Result<Option<Event>> {
        self.write(|db| {
            let mut event = db.query_row("SELECT * FROM events WHERE payload_hash!=synced_hash AND next_sync<=?1 AND lease_until<=?1
                ORDER BY next_sync LIMIT 1",[at],event_row).optional()?;
            if let Some(event) = &mut event {
                event.lease = hex::encode(random::<16>()?);
                db.execute("UPDATE events SET lease_token=?1,lease_until=?2 WHERE id=?3",params![event.lease,at+180.0,event.id])?;
            }
            Ok(event)
        })
    }

    pub fn event_synced(&self, event: &Event, at: f64) -> Result<()> {
        self.write(|db| {
            db.execute("UPDATE events SET synced_hash=?1,synced_at=?2,last_error='',failures=0,lease_until=0,lease_token=''
                WHERE id=?3 AND lease_token=?4", params![event.hash,at,event.id,event.lease])?;
            Ok(())
        })
    }

    pub fn event_failed(
        &self,
        event: &Event,
        message: &str,
        at: f64,
        retry_after: u32,
    ) -> Result<()> {
        let delay = retry_after.max((300 * 2u32.pow(event.failures.min(5))).min(3600));
        let message: String = message.chars().take(800).collect();
        self.write(|db| {
            let updated = db.execute("UPDATE events SET last_error=?1,failures=failures+1,next_sync=?2,lease_until=0,lease_token=''
                WHERE id=?3 AND lease_token=?4",params![message,at+f64::from(delay),event.id,event.lease])?;
            if updated != 0 { log(db,&event.case_id,at,"error",&format!("캘린더 동기화 실패: {message}"))?; }
            Ok(())
        })
    }
}

pub fn calendar_target(db: &Connection, target: &str) -> Result<()> {
    let old: Option<String> = meta(db, "calendar_target")?;
    if old.as_deref().unwrap_or("") != target {
        db.execute(
            "UPDATE events SET synced_hash='',next_sync=?1,last_error=''",
            [now()],
        )?;
        set_meta(db, "calendar_target", target)?;
    }
    Ok(())
}

fn observe_event(
    db: &Connection,
    case: &CaseJob,
    mut hearing: Hearing,
    o: &Observation,
    actual: &BTreeSet<String>,
    moment: chrono::DateTime<Tz>,
) -> Result<()> {
    let signature = hearing.signature();
    let at = stamp(moment);
    let mut existing = db
        .query_row(
            "SELECT * FROM events WHERE case_id=?1 AND signature=?2",
            params![case.id, signature],
            event_row,
        )
        .optional()?;
    if existing.is_none()
        && o.warnings.is_empty()
        && hearing
            .moment(moment.timezone())
            .is_some_and(|time| time > moment)
    {
        let mut stmt =
            db.prepare("SELECT * FROM events WHERE case_id=?1 AND start_ts>?2 ORDER BY start_ts")?;
        for e in stmt.query_map(params![case.id, at], event_row)? {
            let event = e?;
            if !actual.contains(&event.signature) {
                existing = Some(event);
                break;
            }
        }
    }
    if let Some(event) = &existing {
        let previous: Hearing = serde_json::from_str(&event.hearing)?;
        if previous.day == hearing.day {
            if hearing.clock.is_empty() {
                hearing.clock = previous.clock;
            }
            if hearing.location.is_empty() {
                hearing.location = previous.location;
            }
        }
    }
    let Some(payload) = event_payload(
        &case.id,
        &case.court,
        &case.number,
        &case.party,
        &hearing,
        o,
        moment.timezone(),
    ) else {
        return Ok(());
    };
    let Some(start) = hearing.moment(moment.timezone()) else {
        return Ok(());
    };
    let text = serde_json::to_string(&payload)?;
    let digest = hash(&text);
    let signature = hearing.signature();
    let hearing = serde_json::to_string(&hearing)?;
    if let Some(event) = existing {
        if event.hash != digest || event.signature != signature {
            db.execute("UPDATE events SET signature=?1,hearing=?2,start_ts=?3,payload=?4,payload_hash=?5,next_sync=?6,last_error='',failures=0 WHERE id=?7",
                params![signature,hearing,stamp(start),text,digest,at,event.id])?;
        }
    } else {
        db.execute("INSERT INTO events(id,case_id,signature,hearing,start_ts,payload,payload_hash,next_sync) VALUES (?1,?2,?3,?4,?5,?6,?7,?8)",
            params![hex::encode(random::<16>()?),case.id,signature,hearing,stamp(start),text,digest,at])?;
    }
    Ok(())
}

fn log(db: &Connection, id: &str, at: f64, level: &str, message: &str) -> Result<()> {
    db.execute(
        "INSERT INTO logs(case_id,at,level,message) VALUES (?1,?2,?3,?4)",
        params![id, at, level, message],
    )?;
    db.execute(
        "DELETE FROM logs WHERE id<=(SELECT COALESCE(MAX(id),0)-5000 FROM logs)",
        [],
    )?;
    Ok(())
}

fn fold(text: &str) -> String {
    text.chars()
        .filter(|c| !c.is_whitespace())
        .flat_map(char::to_lowercase)
        .collect()
}

fn append_text(value: &Value, text: &mut String) {
    match value {
        Value::Array(values) => values.iter().for_each(|item| append_text(item, text)),
        Value::Object(values) => values.values().for_each(|item| append_text(item, text)),
        Value::Null => {}
        Value::String(value) => {
            text.push(' ');
            text.push_str(value);
        }
        other => {
            text.push(' ');
            text.push_str(&other.to_string());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::UNKNOWN;

    #[test]
    fn case_scopes_follow_confirmed_orders_before_search_and_pagination() -> Result<()> {
        let directory = tempfile::tempdir()?;
        let store = Store::open(&directory.path().join("cases.sqlite3"))?;
        let ids = store.import(
            &(1..=4)
                .map(|number| CaseInput {
                    court: "가상법원".into(),
                    case_number: format!("2024가단{number}"),
                    party: "가상당사자".into(),
                })
                .collect::<Vec<_>>(),
        )?;
        let saved = [
            json!({"summary":{"판결 주문":"판결선고"}}),
            json!({"summary":{"판결 주문":"청구를 기각한다."}}),
            json!({"case_category":"민사","hearings":[{"day":"2026-01-31","kind":"선고","result":"종국 : 원고일부승"}]}),
            json!({}),
        ];
        store.write(|db| {
            for (id, snapshot) in ids.iter().zip(&saved) {
                db.execute(
                    "UPDATE cases SET snapshot=?1,state='awaiting_order' WHERE id=?2",
                    params![snapshot.to_string(), id],
                )?;
            }
            Ok(())
        })?;
        store.active(&ids[2], false)?;
        let zone = chrono_tz::Asia::Seoul;
        let ongoing = store.page("가상당사자", 1, 10, CaseScope::Ongoing, zone, false)?;
        let ongoing_ids: BTreeSet<_> = ongoing["cases"]
            .as_array()
            .unwrap()
            .iter()
            .map(|row| row["id"].as_str().unwrap())
            .collect();
        assert_eq!(ongoing_ids, [&ids[0], &ids[3]].map(String::as_str).into());
        assert_eq!(ongoing["scope_total"], 2);
        assert_eq!(ongoing["metrics"]["waiting"], 2);
        let finished = store.page("가상당사자", 9, 1, CaseScope::Finished, zone, false)?;
        assert_eq!(finished["total"], 2);
        assert_eq!(finished["scope_total"], 2);
        assert_eq!(finished["page"], 2);
        assert_eq!(finished["cases"].as_array().unwrap().len(), 1);
        let found = store.page("원고일부승", 1, 10, CaseScope::Finished, zone, false)?;
        assert_eq!(found["total"], 1);
        assert_eq!(found["cases"][0]["id"], ids[2]);
        assert_eq!(found["cases"][0]["judgment_stage"], "decided");
        let hidden = store.page("원고일부승", 1, 10, CaseScope::Ongoing, zone, false)?;
        assert_eq!(hidden["total"], 0);
        Ok(())
    }

    #[test]
    fn completed_case_can_refresh_appeal_and_finality_on_demand() -> Result<()> {
        let directory = tempfile::tempdir()?;
        let store = Store::open(&directory.path().join("cases.sqlite3"))?;
        let settings = Settings::from_env()?;
        let ids = store.import(&[CaseInput {
            court: "가상법원".into(),
            case_number: "2024가단10".into(),
            party: "가상당사자".into(),
        }])?;
        let id = &ids[0];
        let at = now();
        let job = store.claim_case(at, &settings)?.expect("new case queued");
        let decided = Observation {
            summary: [
                ("판결 주문".into(), "청구를 기각한다.".into()),
                ("선고일자".into(), "2026.01.31".into()),
            ]
            .into(),
            ..Default::default()
        };
        store.complete(&job, decided, at, &settings)?;
        assert_eq!(store.detail_refresh(id)?.unwrap()["reason"], "requested");
        assert_eq!(store.detail_refresh(id)?.unwrap()["reason"], "pending");

        let at = now();
        let job = store
            .claim_case(at, &settings)?
            .expect("completed case queued");
        let refreshed = Observation {
            summary: [
                ("판결 주문".into(), UNKNOWN.into()),
                ("상소제기 여부".into(), "제기됨".into()),
                ("상소사건번호".into(), "2026나10".into()),
                ("확정 여부".into(), "확정됨 (2026.02.27)".into()),
            ]
            .into(),
            ..Default::default()
        };
        store.complete(&job, refreshed, at, &settings)?;
        let page = store.page("", 1, 10, CaseScope::All, settings.timezone, false)?;
        let row = &page["cases"][0];
        let summary = &row["observed"]["summary"];
        assert_eq!(row["judgment_stage"], "decided");
        assert!(row["next_check"].is_null());
        assert_eq!(summary["판결 주문"], "청구를 기각한다.");
        assert_eq!(summary["선고일자"], "2026.01.31");
        assert_eq!(summary["상소사건번호"], "2026나10");
        assert_eq!(summary["확정 여부"], "확정됨 (2026.02.27)");
        assert_eq!(store.detail_refresh(id)?.unwrap()["reason"], "fresh");

        assert!(store.refresh(id)?);
        let at = now();
        let job = store
            .claim_case(at, &settings)?
            .expect("manual refresh queued");
        store.fail_case(&job, "일시적인 조회 실패", at, &settings)?;
        let page = store.page("", 1, 10, CaseScope::All, settings.timezone, false)?;
        assert!(page["cases"][0]["next_check"].is_null());
        assert_eq!(page["cases"][0]["last_error"], "일시적인 조회 실패");
        Ok(())
    }
}
